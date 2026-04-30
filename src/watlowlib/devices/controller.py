"""The :class:`Controller` facade — public API for one device.

Single-device surface:

- :meth:`identify`
- :meth:`read_pv` / :meth:`read_setpoint` / :meth:`set_setpoint`
- :meth:`read_parameter` / :meth:`write_parameter`
- :meth:`loop` (multi-loop access), PID, alarms

Lifecycle is async-context-manager: ``async with await open_device(...)``
opens the transport on ``__aenter__`` and disposes the protocol client
+ closes the transport on ``__aexit__``.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self

from watlowlib.commands.parameters import (
    READ_PARAMETER,
    WRITE_PARAMETER,
    ReadParameterRequest,
    WriteParameterRequest,
)
from watlowlib.devices.loop import ControllerLoop
from watlowlib.devices.models import (
    DeviceHealth,
    DeviceInfo,
    ParameterEntry,
    PartNumber,
    Reading,
)
from watlowlib.errors import WatlowProtocolError, WatlowTransportError
from watlowlib.protocol.base import ProtocolKind
from watlowlib.registry.families import (
    ControllerFamily,
    capabilities_for_part_number,
    decode_part_number,
    default_loops,
)

# Wide-enumeration codes for parameter 17009 (Protocol). Mirrors the
# ``maintenance.PROTOCOL_MODE_CODES`` table so ``identify`` can decode
# the EEPROM-resident protocol setting without importing maintenance
# (which would create a cycle: maintenance imports the controller
# factory, the factory builds a controller).
_PROTOCOL_CODE_TO_KIND: dict[int, ProtocolKind] = {
    1286: ProtocolKind.STDBUS,
    1057: ProtocolKind.MODBUS_RTU,
}

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import TracebackType

    from watlowlib.devices.capability import Capability
    from watlowlib.devices.session import Session
    from watlowlib.streaming.sample import Sample
    from watlowlib.transport.base import SerialSettings, Transport

__all__ = ["Controller"]


class Controller:
    """Async facade for a single Watlow controller."""

    def __init__(
        self,
        session: Session,
        transport: Transport,
        *,
        serial_settings: SerialSettings,
    ) -> None:
        self._session = session
        self._transport = transport
        self._serial_settings = serial_settings
        # Cached loop count populated by :meth:`identify`. ``None`` means
        # "we haven't asked yet" — :meth:`loop` then defers validation
        # to the registry's per-spec ``max_instance``. Concrete count is
        # what the part-number decoder produced from the captured part
        # string (PM3 → 1, PM6/8/9 + ``U`` control → 2, etc.).
        self._loops: int | None = None
        # Cached SKU-derived capabilities populated by :meth:`identify`.
        # ``None`` until the part number has been decoded; downstream
        # operations that gate on bits (cool-side PID, etc.) treat
        # ``None`` as "no information, no gate" so calls work pre-
        # identify without surprising the user.
        self._capabilities: Capability | None = None

    @property
    def session(self) -> Session:
        """Underlying session used for command dispatch."""
        return self._session

    @property
    def loops(self) -> int | None:
        """Cached loop count (set after :meth:`identify`).

        ``None`` until the device's part number has been decoded;
        :meth:`loop` accepts any 1-indexed value while ``loops`` is
        ``None`` and falls back to per-spec validation at the first
        wire call. After :meth:`identify`, ``loops`` reflects the
        decoded value.
        """
        return self._loops

    @property
    def capabilities(self) -> Capability | None:
        """Cached SKU capabilities (set after :meth:`identify`).

        ``None`` pre-identify so capability-gated operations behave
        permissively until the part number is captured. After
        :meth:`identify`, callers can branch on
        :attr:`Capability.HAS_COOLING` etc. without re-issuing
        identify.
        """
        return self._capabilities

    def loop(self, n: int) -> ControllerLoop:
        """Return a sub-facade bound to loop ``n`` (1-indexed).

        ``n`` is validated eagerly when :attr:`loops` is known,
        otherwise per-spec ``max_instance`` validation kicks in at the
        first wire call. Multi-loop access is the public way to reach
        loop 2 on dual-loop devices —
        :meth:`Controller.read_pv` defaults to ``instance=1``.
        """
        return ControllerLoop(self, n)

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        del exc_type, exc, tb
        await self.close()

    async def close(self) -> None:
        """Close the underlying transport and dispose the protocol client."""
        # The session holds a reference to the protocol client; dispose
        # it so any pending caller learns the controller is gone before
        # the transport close races them.
        try:
            self._session.dispose()
        finally:
            await self._transport.close()

    # --- Generic parameter API ------------------------------------------

    async def read_parameter(
        self,
        name_or_id: str | int,
        *,
        instance: int = 1,
        timeout: float | None = None,
    ) -> ParameterEntry:
        """Read any registry parameter.

        ``instance=1`` is the default for single-loop devices and the
        first loop / channel on multi-loop devices.
        """
        return await self._session.execute(
            READ_PARAMETER,
            ReadParameterRequest(name_or_id, instance=instance),
            timeout=timeout,
        )

    async def write_parameter(
        self,
        name_or_id: str | int,
        value: float | int | str,
        *,
        instance: int = 1,
        confirm: bool = False,
        timeout: float | None = None,
    ) -> ParameterEntry:
        """Write any registry parameter.

        Persistent (RWE / RWES) writes require ``confirm=True``;
        the session raises :class:`WatlowConfirmationRequiredError`
        before any I/O if the gate is missing.
        """
        return await self._session.execute(
            WRITE_PARAMETER,
            WriteParameterRequest(name_or_id, value, instance=instance),
            confirm=confirm,
            timeout=timeout,
        )

    # --- Workhorse readers ----------------------------------------------

    async def read_pv(self, *, instance: int = 1, timeout: float | None = None) -> Reading:
        """Read the process value for ``instance`` (loop number, 1-indexed)."""
        entry = await self.read_parameter("process_value", instance=instance, timeout=timeout)
        return self._reading_from_entry(entry)

    async def read_setpoint(self, *, instance: int = 1, timeout: float | None = None) -> Reading:
        """Read the active setpoint for ``instance``."""
        entry = await self.read_parameter("setpoint", instance=instance, timeout=timeout)
        return self._reading_from_entry(entry)

    async def set_setpoint(
        self,
        value: float,
        *,
        instance: int = 1,
        confirm: bool = False,
        timeout: float | None = None,
    ) -> Reading:
        """Write the setpoint and return the device-echoed value as a :class:`Reading`.

        Setpoint is RWES — pass ``confirm=True`` to acknowledge the
        EEPROM write. The returned reading is the device's echo of
        the value it accepted.
        """
        entry = await self.write_parameter(
            "setpoint",
            value,
            instance=instance,
            confirm=confirm,
            timeout=timeout,
        )
        return self._reading_from_entry(entry)

    # --- Streaming ------------------------------------------------------

    async def poll(self, *, instance: int = 1, timeout: float | None = None) -> Reading:
        """Read the active process value — the canonical no-arg snapshot.

        Equivalent to :meth:`read_pv`. The no-arg form aligns with the
        ecosystem ``poll()`` convention shared by ``alicatlib.Device``,
        ``sartoriuslib.Balance``, and ``nidaqlib.DaqSession``: a
        single, default-shaped reading per call.

        For multi-parameter polling use :meth:`poll_many`.
        """
        return await self.read_pv(instance=instance, timeout=timeout)

    async def poll_many(
        self,
        parameters: Sequence[str | int],
        *,
        names: Sequence[str] | None = None,
        instances: Sequence[int] = (1,),
    ) -> list[Sample]:
        r"""Read every (parameter × instance) and return them as :class:`Sample`\ s.

        Satisfies the :class:`watlowlib.streaming.PollSource` Protocol so
        a solo :class:`Controller` can drive :func:`watlowlib.streaming.record`
        directly without a manager. ``names`` is accepted for Protocol
        compatibility but ignored — a Controller has only one device.

        Failed reads are dropped from the returned list and logged at
        WARN. The recorder treats absence as "drop this row from the
        batch" and continues with the next tick.
        """
        del names  # solo controller has no name-keyed device map
        from watlowlib.streaming._poll import poll_controller  # noqa: PLC0415 — avoid cycle

        return await poll_controller(
            self,
            name=self._transport.label,
            parameters=parameters,
            instances=instances,
        )

    # --- Identity --------------------------------------------------------

    async def identify(
        self,
        *,
        timeout: float | None = None,
        strict: bool = False,
        query_configured_protocol: bool = False,
    ) -> DeviceInfo:
        """Read the identity parameters and return a :class:`DeviceInfo`.

        Reads (in order): part number (1009), hardware id (1001),
        firmware id (1002), serial number. Missing secondary fields
        stay ``None`` and the result's :attr:`DeviceInfo.health` is
        promoted from :attr:`DeviceHealth.OK` to
        :attr:`DeviceHealth.PARTIAL`. If the part-number read itself
        fails, the result's health is :attr:`DeviceHealth.FAILED` and
        capability decoding is skipped (the family prior still
        applies).

        Args:
            timeout: Per-read timeout override.
            strict: If ``True``, raise the underlying error when the
                part-number read fails instead of returning a
                ``health=FAILED`` info. Use this in maintenance code
                paths that need to know the device actually answered
                before declaring success.
            query_configured_protocol: If ``True``, also read parameter
                17009 (Protocol) and populate
                :attr:`DeviceInfo.configured_protocol`. Off by default
                because the read costs an extra round-trip; the
                maintenance verify pass and the discover CLI opt in.

        Raises:
            WatlowError: When ``strict=True`` and the part-number read
                fails. The original transport / protocol error class
                is preserved.
        """
        if strict:
            entry = await self.read_parameter("part_number", timeout=timeout)
            part_raw = entry.value if isinstance(entry.value, str) else None
        else:
            part_raw = await self._safe_read_str("part_number", timeout=timeout)
        hw_id = await self._safe_read_int("hardware_id", timeout=timeout)
        fw_id = await self._safe_read_int("firmware_id", timeout=timeout)
        serial_str = await self._safe_read_str("serial_number", timeout=timeout)

        if part_raw:
            part = decode_part_number(part_raw)
            capabilities = capabilities_for_part_number(part)
            # PARTIAL when part_number is fine but a secondary read missed.
            secondary_missing = hw_id is None or fw_id is None
            health = DeviceHealth.PARTIAL if secondary_missing else DeviceHealth.OK
        else:
            part = PartNumber(raw="", family=ControllerFamily.UNKNOWN)
            # No part number → no SKU decode; capability table degrades
            # to the UNKNOWN family prior (NONE).
            capabilities = capabilities_for_part_number(part)
            health = DeviceHealth.FAILED

        configured_protocol: ProtocolKind | None = None
        if query_configured_protocol:
            code = await self._safe_read_int(17009, timeout=timeout)
            if code is not None:
                configured_protocol = _PROTOCOL_CODE_TO_KIND.get(code)

        loops = default_loops(part)
        # Cache for ``self.loop(n)``'s eager validator. Identify is the
        # canonical place that gets to set this — open() doesn't have
        # the part number yet, and a SKU's loop count / capabilities
        # never change mid-session.
        self._loops = loops
        self._capabilities = capabilities
        return DeviceInfo(
            part_number=part,
            hardware_id=hw_id,
            firmware_id=fw_id,
            serial_number=serial_str,
            family=part.family,
            protocol=self._session.protocol_kind,
            address=self._session.address,
            capabilities=capabilities,
            serial_settings=self._serial_settings,
            loops=loops,
            health=health,
            configured_protocol=configured_protocol,
        )

    # --- Internals ------------------------------------------------------

    async def _safe_read_int(
        self,
        name_or_id: str | int,
        *,
        timeout: float | None,
    ) -> int | None:
        """Read a numeric parameter, returning ``None`` on absence.

        Used by :meth:`identify` so a missing identity field doesn't
        torpedo the rest of the snapshot. Swallows protocol errors
        (parameter absent / unsupported) and transport timeouts (no
        reply on the bus); real connection / configuration errors
        propagate so the user sees them.
        """
        try:
            entry = await self.read_parameter(name_or_id, timeout=timeout)
        except (WatlowProtocolError, WatlowTransportError):
            return None
        if isinstance(entry.value, int | float):
            return int(entry.value)
        return None

    async def _safe_read_str(
        self,
        name_or_id: str | int,
        *,
        timeout: float | None,
    ) -> str | None:
        """Read a string parameter, returning ``None`` on absence.

        Same swallow policy as :meth:`_safe_read_int`.
        """
        try:
            entry = await self.read_parameter(name_or_id, timeout=timeout)
        except (WatlowProtocolError, WatlowTransportError):
            return None
        if isinstance(entry.value, str):
            return entry.value
        return None

    def _reading_from_entry(self, entry: ParameterEntry) -> Reading:
        value = float(entry.value) if isinstance(entry.value, int | float) else None
        return Reading(
            value=value,
            unit=None,  # PM registry doesn't carry per-parameter unit yet.
            received_at=datetime.now(UTC),
            monotonic_ns=time.monotonic_ns(),
            raw=entry.raw,
            protocol=self._session.protocol_kind,
        )
