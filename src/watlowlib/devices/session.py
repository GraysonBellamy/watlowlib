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
from typing import TYPE_CHECKING, Any

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
)
from watlowlib.protocol.base import ProtocolKind

if TYPE_CHECKING:
    from watlowlib.commands.base import Command
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
        registry: ParameterRegistry,
        family: ControllerFamily,
        address: int,
        port: str,
    ) -> None:
        self._client = client
        self._registry = registry
        self._family = family
        self._address = address
        self._port = port
        self._availability: dict[str, Availability] = {}

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
        serialization invariant.
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
    def family(self) -> ControllerFamily:
        """Best-known controller family for this session."""
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
        # snapshotted before the lock releases.
        async with self._client.lock:
            try:
                reply = await self._client.execute(
                    wire_request,
                    timeout=bound_timeout,
                    command_name=command.name,
                )
            except (
                WatlowNoSuchObjectError,
                WatlowNoSuchAttributeError,
                WatlowProtocolUnsupportedError,
            ) as exc:
                self._availability[cache_key] = Availability.UNSUPPORTED
                _log.warning(
                    "command unsupported: protocol=%s cmd=%s key=%s exc=%s",
                    kind.value,
                    command.name,
                    cache_key,
                    exc,
                )
                raise
            except WatlowProtocolError:
                raise
            except WatlowError as exc:
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
            _log.warning(
                "command unsupported: protocol=%s cmd=%s key=%s exc=%s",
                kind.value,
                command.name,
                cache_key,
                exc,
            )
            raise
        except WatlowProtocolError:
            # Decode-failure parity with the inside-lock branch above:
            # NoSuchInstance / IllegalDataValue / generic decode errors
            # don't transition availability per design §5b.
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
        """
        target = getattr(request, "name_or_id", None)
        if target is None:
            return command.name
        try:
            spec = self._registry.resolve(target)
        except Exception:
            return f"{command.name}:{target}"
        return f"{command.name}:{spec.parameter_id}"
