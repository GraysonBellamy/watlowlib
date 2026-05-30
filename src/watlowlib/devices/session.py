"""The :class:`Session` — single dispatch point for every command.

The session is the **only** place that gates, logs, and updates
:class:`Availability`. Variants are pure ``(ctx, request) → response``
functions; protocol clients only own the wire. Per ``docs/design.md``
invariant 2, no other layer touches these concerns.

Responsibilities (in order, per ``execute``):

1. Resolve the protocol variant. ``UNSUPPORTED`` is sticky — short-
   circuit pre-I/O on a typed error.
2. Enforce ``confirm=True`` for :attr:`SafetyTier.PERSISTENT` writes.
3. Acquire the per-port lock on the protocol client.
4. Variant ``encode`` → ``client.execute`` → variant ``decode``.
5. Map success / typed errors to availability transitions and log a
   structured event.

Variant signatures differ across protocols (see
``docs/design.md`` §5):

- Std Bus variants take ``decode(reply, ctx)`` — the reply already
  carries the parameter selector echoed by the device.
- Modbus variants take ``decode(words, ctx, request)`` — the wire
  carries no echo, so the variant re-resolves the spec from the
  request to interpret the words.
"""

from __future__ import annotations

import time
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from watlowlib._lock import maybe_acquire
from watlowlib._logging import get_logger
from watlowlib.commands.base import CommandContext
from watlowlib.config import DEFAULTS
from watlowlib.devices.capability import Availability, SafetyTier
from watlowlib.errors import (
    ErrorContext,
    WatlowConfirmationRequiredError,
    WatlowError,
    WatlowNoSuchAttributeError,
    WatlowNoSuchObjectError,
    WatlowProtocolError,
    WatlowProtocolUnsupportedError,
    WatlowTransportError,
)
from watlowlib.protocol.base import ProtocolKind
from watlowlib.registry.units import Unit, unit_from_display_code

if TYPE_CHECKING:
    from collections.abc import Mapping

    from watlowlib.commands.base import Command
    from watlowlib.devices.profile import DeviceProfile
    from watlowlib.protocol.base import ProtocolClient
    from watlowlib.registry.families import ControllerFamily
    from watlowlib.registry.parameters import ParameterRegistry

__all__ = ["Session"]

_log = get_logger("session")


class Session:
    """Owns availability cache, gates, and the dispatch loop.

    A :class:`Session` is bound to exactly one :class:`ProtocolClient`
    for its lifetime — one protocol per port (invariant 1).
    """

    def __init__(
        self,
        client: ProtocolClient[Any, Any],
        *,
        profile: DeviceProfile,
        address: int,
        port: str,
        wire_temperature_unit: Unit | None = None,
    ) -> None:
        self._client = client
        # The profile is the device-type bundle (family + registry +
        # framing + identity). ``registry`` / ``family`` stay exposed as
        # thin delegating properties so the streaming / manager layers
        # that learned those names keep working.
        self._profile = profile
        self._registry = profile.registry
        self._family = profile.family
        self._address = address
        self._port = port
        self._availability: dict[str, Availability] = {}
        # Scale of temperature values on the wire. An explicit
        # ``wire_temperature_unit`` (from
        # ``open_device(assert_wire_temperature_unit=...)``) wins;
        # otherwise the profile's own ``wire_temperature_unit`` seeds it
        # (the Series SD knows it speaks °F; the EZ-ZONE PM profile
        # leaves it ``None`` so the user must assert). Drives
        # :class:`Reading.unit` / :class:`Sample.unit` for temperature
        # parameters; ``None`` means "do not guess". The library makes
        # **no** attempt to derive this from PM parameter 17050 — on at
        # least one PM3 firmware that register is label-only and does
        # not govern the wire scale. See ``docs/devices.md`` §Units.
        self._wire_temperature_unit: Unit | None = (
            wire_temperature_unit
            if wire_temperature_unit is not None
            else profile.wire_temperature_unit
        )
        self._wire_temperature_unit_warned: bool = False
        # Lazy cache of parameter 17050's reported value. Kept purely
        # as an inspection helper exposed via
        # :meth:`Controller.read_comms_unit_label` — **does not** feed
        # :class:`Reading.unit`. ``_comms_unit_label_loaded`` distinguishes
        # "haven't asked yet" from "asked and got nothing"; subsequent
        # calls don't repeat the wire turn-around after a rejection.
        self._comms_unit_label: Unit | None = None
        self._comms_unit_label_loaded: bool = False
        # Most recent error's :class:`ErrorContext`, captured from any
        # :class:`WatlowError` that propagates out of :meth:`execute`.
        # Drives :class:`DeviceSnapshot.last_error`. Cleared by
        # :meth:`clear_last_error` (manual reset for diagnostics).
        self._last_error: ErrorContext | None = None
        # Count of transient transport hiccups the session swallowed
        # and retried. Watlow has no current transient class (§F of
        # the unified spec — deferred), so this counter stays at zero
        # unless a future transport path increments it. Reset on
        # :meth:`open` / fresh transport.
        self.recoverable_error_count: int = 0

    @property
    def protocol_kind(self) -> ProtocolKind:
        """The wire protocol this session speaks."""
        return self._client.kind

    @property
    def client(self) -> ProtocolClient[Any, Any]:
        """The bound protocol client.

        Exposed for the ``watlow-raw`` escape hatch and for diagnostics
        that need to issue an unframed wire op outside the registry.
        Callers must acquire :attr:`ProtocolClient.lock` before
        :meth:`ProtocolClient.execute` to honour the per-port
        serialization invariant, and must pass this session's
        :attr:`address` (or another concrete address for multi-drop
        diagnostics) to ``execute``.
        """
        return self._client

    @property
    def address(self) -> int:
        """Session bus address."""
        return self._address

    @property
    def port(self) -> str:
        """Transport label (for logs / error context)."""
        return self._port

    @property
    def profile(self) -> DeviceProfile:
        """The device profile (family + registry + framing + identity)."""
        return self._profile

    @property
    def family(self) -> ControllerFamily:
        """Best-known controller family for this session (delegates to the profile)."""
        return self._family

    @property
    def registry(self) -> ParameterRegistry:
        """Parameter registry bound to this session.

        Exposed for the streaming layer so polling code can resolve a
        name / id to a :class:`ParameterSpec` without an extra import
        of the module-level :data:`PARAMETERS`.
        """
        return self._registry

    def dispose(self) -> None:
        """Dispose the bound protocol client."""
        self._client.dispose()

    def availability(self, command_name: str) -> Availability:
        """Cached availability for ``command_name``."""
        return self._availability.get(command_name, Availability.UNKNOWN)

    def availability_summary(self) -> Mapping[str, Availability]:
        """Return the current command availability cache.

        Includes every command the session has dispatched; the
        snapshot path filters to the UNSUPPORTED entries.
        """
        return MappingProxyType(dict(self._availability))

    @property
    def last_error(self) -> ErrorContext | None:
        """Most-recent error context captured by :meth:`execute`."""
        return self._last_error

    def clear_last_error(self) -> None:
        """Reset :attr:`last_error` to ``None`` (manual diagnostics aid)."""
        self._last_error = None

    def _record_error(self, exc: WatlowError) -> None:
        """Remember ``exc.context`` as the most-recent error."""
        self._last_error = exc.context

    async def execute[Req, Resp](
        self,
        command: Command[Req, Resp],
        request: Req,
        *,
        confirm: bool = False,
        timeout: float | None = None,
    ) -> Resp:
        """Dispatch ``command`` with ``request`` and return the typed response."""
        kind = self._client.kind
        # Variant resolution. The session picks the variant matching
        # the bound protocol; one protocol per port (invariant 1).
        # Resolve to a single ``variant`` local so the rest of the
        # method is protocol-agnostic; we still branch on ``kind``
        # for the encode/decode call shapes (stdbus takes ``reply``;
        # modbus takes ``words, ctx, request``).
        if kind is ProtocolKind.STDBUS:
            stdbus_variant = command.stdbus
            if stdbus_variant is None:
                raise WatlowProtocolUnsupportedError(
                    f"command {command.name!r} has no Std Bus variant",
                    context=self._error_context(command, request),
                )
            modbus_variant = None
        elif kind is ProtocolKind.MODBUS_RTU:
            modbus_variant = command.modbus
            if modbus_variant is None:
                raise WatlowProtocolUnsupportedError(
                    f"command {command.name!r} has no Modbus variant",
                    context=self._error_context(command, request),
                )
            stdbus_variant = None
        else:
            raise WatlowProtocolUnsupportedError(
                f"session has unsupported protocol kind {kind!r}",
                context=self._error_context(command, request),
            )

        # Cache key. We key on ``command_name:parameter_id`` for
        # registry-driven commands so that one ``read_parameter("foo")``
        # rejection doesn't sticky-block every other parameter; bare
        # commands fall back to ``command.name``.
        cache_key = self._cache_key(command, request)

        cached = self._availability.get(cache_key, Availability.UNKNOWN)
        if cached is Availability.UNSUPPORTED:
            raise WatlowProtocolUnsupportedError(
                f"command {command.name!r} is unsupported on this device",
                context=self._error_context(command, request),
            )

        # Safety gate: PERSISTENT writes need explicit confirm.
        if command.safety is SafetyTier.PERSISTENT and not confirm:
            raise WatlowConfirmationRequiredError(
                f"command {command.name!r} is PERSISTENT and requires confirm=True",
                context=self._error_context(command, request),
            )

        ctx = CommandContext(
            registry=self._registry,
            family=self._family,
            address=self._address,
            port=self._port,
        )

        bound_timeout = timeout if timeout is not None else DEFAULTS.io_timeout_s

        # Encode under the variant. Errors here are pre-I/O — typically
        # validation failures — and should propagate untouched.
        # Exactly one of ``stdbus_variant`` / ``modbus_variant`` is
        # non-None per the resolution above; the type narrowing is
        # explicit so neither mypy nor pyright needs an ``assert`` it
        # can't enforce at runtime under ``-O``.
        wire_request: Any
        if stdbus_variant is not None:
            wire_request = stdbus_variant.encode(ctx, request)
        elif modbus_variant is not None:
            wire_request = modbus_variant.encode(ctx, request)
        else:  # pragma: no cover — variant resolution above guarantees one is set
            raise WatlowProtocolUnsupportedError(
                f"command {command.name!r} variant resolution lost",
                context=self._error_context(command, request),
            )

        started = time.monotonic()
        # Hold the per-port client lock only for the I/O turn-around.
        # Decode is CPU-only and does not need to block the next
        # request waiting on the same RS-485 segment; ``reply`` is
        # snapshotted before the lock releases. ``maybe_acquire``
        # reuses an existing acquisition when the caller (e.g.
        # ``poll_controller``'s tick batch, or
        # ``WatlowManager._run_group``'s port batch) already holds
        # the lock — avoids the FIFO-queue starvation that would
        # otherwise let unrelated writers interleave between a
        # batch's reads.
        async with maybe_acquire(self._client.lock):
            try:
                reply = await self._client.execute(
                    wire_request,
                    address=self._address,
                    timeout=bound_timeout,
                    command_name=command.name,
                )
            except (
                WatlowNoSuchObjectError,
                WatlowNoSuchAttributeError,
                WatlowProtocolUnsupportedError,
            ) as exc:
                self._availability[cache_key] = Availability.UNSUPPORTED
                self._record_error(exc)
                _log.warning(
                    "command unsupported: protocol=%s cmd=%s key=%s exc=%s",
                    kind.value,
                    command.name,
                    cache_key,
                    exc,
                )
                raise
            except WatlowProtocolError as exc:
                self._record_error(exc)
                raise
            except WatlowError as exc:
                self._record_error(exc)
                _log.warning(
                    "command error: protocol=%s cmd=%s key=%s exc=%s",
                    kind.value,
                    command.name,
                    cache_key,
                    exc,
                )
                raise

        # Decode outside the lock — pure compute on the captured reply.
        try:
            if stdbus_variant is not None:
                response = stdbus_variant.decode(reply, ctx)
            else:
                # ``modbus_variant is not None`` per the resolution above;
                # mypy/pyright follow the narrowing without an ``assert``.
                response = modbus_variant.decode(reply, ctx, request)  # type: ignore[union-attr]
        except (
            WatlowNoSuchObjectError,
            WatlowNoSuchAttributeError,
            WatlowProtocolUnsupportedError,
        ) as exc:
            # Decode-side "we don't have this": same availability
            # transition as the wire-side rejection above.
            self._availability[cache_key] = Availability.UNSUPPORTED
            self._record_error(exc)
            _log.warning(
                "command unsupported: protocol=%s cmd=%s key=%s exc=%s",
                kind.value,
                command.name,
                cache_key,
                exc,
            )
            raise
        except WatlowProtocolError as exc:
            # Decode-failure parity with the inside-lock branch above:
            # NoSuchInstance / IllegalDataValue / generic decode errors
            # don't transition availability per design §5b.
            self._record_error(exc)
            raise

        elapsed = time.monotonic() - started
        self._availability[cache_key] = Availability.SUPPORTED
        _log.debug(
            "session exec ok protocol=%s cmd=%s key=%s elapsed=%.4fs",
            kind.value,
            command.name,
            cache_key,
            elapsed,
        )
        return response

    def wire_temperature_unit(self) -> Unit | None:
        """Return the user-asserted scale of temperature values on the wire.

        This is what :class:`Reading.unit` / :class:`Sample.unit` get
        tagged with for temperature parameters. ``None`` when the user
        did not pass ``assert_wire_temperature_unit=`` to
        :func:`watlowlib.open_device`; in that case temperature
        readings carry ``unit=None`` rather than guess.

        Pure accessor — no I/O. Logs a one-shot WARN the first time
        an asserted value is consumed so the user-assertion shows up
        plainly in capture logs.
        """
        if self._wire_temperature_unit is not None and not self._wire_temperature_unit_warned:
            _log.warning(
                "trusting user-asserted wire temperature unit %s for port=%s "
                "address=%s; not independently verified by the library "
                "(parameter 17050 is label-only on at least one PM firmware)",
                self._wire_temperature_unit.value,
                self._port,
                self._address,
            )
            self._wire_temperature_unit_warned = True
        return self._wire_temperature_unit

    def set_wire_temperature_unit(self, unit: Unit | None) -> None:
        """Set the wire temperature scale from an authoritative source.

        Called by an identity strategy that has read the device's own
        unit register (e.g. the Series SD reg 18). Unlike the PM path —
        where the unit register can lie, so the value stays a user
        assertion — this is the device telling us its comms scale
        directly, so it is honest to adopt it. Resets the one-shot
        "trusting user-asserted unit" warning since this value is
        device-sourced, not user-asserted.
        """
        self._wire_temperature_unit = unit
        self._wire_temperature_unit_warned = True

    async def comms_unit_label(self) -> Unit | None:
        """Return the value parameter 17050 reports for this session.

        Inspection helper, not a source of truth: on at least one PM3
        firmware revision 17050 is a label-only register that does not
        govern the wire scale. :class:`Reading.unit` is sourced from
        :meth:`wire_temperature_unit` instead.

        Reads the parameter on first call and caches the result for
        the lifetime of the session. Returns ``None`` when the device
        rejects the read or reports an unknown code; the cache
        distinguishes "haven't asked yet" from "asked and got nothing"
        so a rejection does not cost another wire turn-around.

        Invalidated by :meth:`invalidate_comms_unit_label` (called by
        :meth:`Controller.set_comms_unit_label` after a successful
        write).
        """
        if self._comms_unit_label_loaded:
            return self._comms_unit_label
        self._comms_unit_label = await self._fetch_comms_unit_label()
        self._comms_unit_label_loaded = True
        return self._comms_unit_label

    def invalidate_comms_unit_label(self) -> None:
        """Drop the cached 17050 value so the next read re-queries the device."""
        self._comms_unit_label = None
        self._comms_unit_label_loaded = False

    async def _fetch_comms_unit_label(self) -> Unit | None:
        r"""Read parameter 17050 and map the device code to :class:`Unit`.

        Swallows protocol / transport errors to ``None`` — same
        graceful-degrade policy as :meth:`Controller._safe_read_int`,
        so a device that doesn't expose 17050 still returns from this
        helper, just with no label.
        """
        # Imported here to avoid a circular import: commands/parameters.py
        # depends on devices/models.py which depends on registry/units.py;
        # session.py is one step removed and pulls these in lazily.
        from watlowlib.commands.parameters import (  # noqa: PLC0415
            READ_PARAMETER,
            ReadParameterRequest,
        )

        try:
            entry = await self.execute(
                READ_PARAMETER,
                ReadParameterRequest("display_units", instance=1),
            )
        except (WatlowProtocolError, WatlowTransportError):
            return None
        value = entry.value
        if not isinstance(value, int | float):
            return None
        return unit_from_display_code(int(value))

    def _error_context(
        self,
        command: Command[Any, Any],
        request: object,
    ) -> ErrorContext:
        return ErrorContext(
            command_name=command.name,
            protocol=self._client.kind,
            port=self._port or None,
            address=self._address or None,
            instance=getattr(request, "instance", None),
        )

    def _cache_key(self, command: Command[Any, Any], request: object) -> str:
        """Build a per-request availability cache key.

        For registry-driven commands the key includes the resolved
        parameter id, so a ``read_parameter("foo")`` rejection
        doesn't poison ``read_parameter("bar")``. For everything else
        the key is the bare command name.

        Falls back to the unresolved target on registry-lookup failure
        (typo, alias miss). Programmer-error shapes (``AttributeError``,
        ``TypeError`` from a malformed request object) are *not*
        caught — they should surface, not silently degrade caching.
        """
        target = getattr(request, "name_or_id", None)
        if target is None:
            return command.name
        try:
            spec = self._registry.resolve(target)
        except WatlowError:
            return f"{command.name}:{target}"
        return f"{command.name}:{spec.parameter_id}"
