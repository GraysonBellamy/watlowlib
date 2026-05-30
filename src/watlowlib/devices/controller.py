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

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Self

from watlowlib.commands.parameters import (
    READ_PARAMETER,
    WRITE_PARAMETER,
    ReadParameterRequest,
    WriteParameterRequest,
)
from watlowlib.devices._reading import reading_from_entry
from watlowlib.devices.capability import Capability
from watlowlib.devices.loop import ControllerLoop
from watlowlib.devices.snapshot import WatlowDeviceSnapshot
from watlowlib.errors import WatlowValidationError
from watlowlib.registry.units import Unit, coerce_unit, display_code_for_unit

if TYPE_CHECKING:
    from collections.abc import Sequence
    from types import TracebackType

    from watlowlib.devices.models import DeviceInfo, ParameterEntry, Reading
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
        # Full cached :class:`DeviceInfo` populated by :meth:`identify`.
        # Drives :meth:`snapshot` so no wire I/O is needed to render
        # the controller's identity. ``None`` until identify runs.
        self._device_info: DeviceInfo | None = None

    @property
    def session(self) -> Session:
        """Underlying session used for command dispatch."""
        return self._session

    @property
    def serial_settings(self) -> SerialSettings:
        """Serial framing the controller was opened with.

        Exposed so an identity strategy (see
        :mod:`watlowlib.devices.profile`) can stamp it onto the
        :class:`DeviceInfo` it builds.
        """
        return self._serial_settings

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
        return await reading_from_entry(self._session, entry)

    async def read_setpoint(self, *, instance: int = 1, timeout: float | None = None) -> Reading:
        """Read the active setpoint for ``instance``."""
        entry = await self.read_parameter("setpoint", instance=instance, timeout=timeout)
        return await reading_from_entry(self._session, entry)

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
        return await reading_from_entry(self._session, entry)

    # --- EEPROM write management (Series SD register 17) ----------------

    async def set_persistent_writes(
        self,
        enabled: bool,
        *,
        confirm: bool = False,
        timeout: float | None = None,
    ) -> None:
        """Toggle whether subsequent writes persist to non-volatile memory.

        Series-SD-specific. The SD persists every register write to
        EEPROM by default, so a high-rate writer (ramping setpoints, a
        tuning loop) can wear the EEPROM out and brick the controller.
        Writing ``0`` to register 17 keeps subsequent writes in RAM
        only; the device resets register 17 to ``1`` on every power
        cycle, so call ``set_persistent_writes(False)`` once after each
        power-up before a burst of writes (see ``sd_manual.txt`` p.84).

        Args:
            enabled: ``True`` → persist writes to EEPROM (the power-on
                default); ``False`` → keep writes in RAM only (lost on
                power cycle, but spares the EEPROM).
            confirm: The write itself is gated like any other parameter
                write — pass ``confirm=True`` to acknowledge it.
            timeout: Per-write timeout override.

        Raises:
            WatlowConfirmationRequiredError: ``confirm`` is ``False``.
            WatlowValidationError: the bound profile's registry has no
                ``eeprom_write_enable`` parameter (e.g. an EZ-ZONE PM,
                which has no such register).
        """
        await self.write_parameter(
            "eeprom_write_enable",
            1 if enabled else 0,
            confirm=confirm,
            timeout=timeout,
        )

    # --- Comms unit label (inspection facade for parameter 17050) -------

    async def read_comms_unit_label(self, *, timeout: float | None = None) -> Unit | None:
        """Read (and cache) the value parameter 17050 reports.

        Inspection / diagnostics helper. **Does not** drive
        :class:`Reading.unit`: on at least one PM3 firmware revision
        17050 is a label-only register that changes the enum the
        device reports for itself but does not affect the scale of
        temperature values exchanged over comms.

        To tell watlowlib what scale temperatures actually travel in
        over the wire, pass ``assert_wire_temperature_unit=`` to
        :func:`watlowlib.open_device`. That assertion is what feeds
        :class:`Reading.unit`.

        Distinct from ``read_parameter("units")``, which targets
        parameter 3005 (front-panel display). The two can disagree on
        a real device.

        Returns ``None`` if the device doesn't report a known code.
        """
        del timeout  # comms_unit_label() is cached + uses session defaults
        return await self._session.comms_unit_label()

    async def set_comms_unit_label(
        self,
        unit: Unit | str,
        *,
        confirm: bool = False,
        timeout: float | None = None,
    ) -> Unit | None:
        """Set parameter 17050 ("Communications - Display Units").

        Accepts a :class:`Unit` or a case-insensitive string alias
        (``"C"`` / ``"F"`` / ``"celsius"`` / ``"fahrenheit"`` /
        ``"degC"`` / ``"degF"`` / ``"°C"`` / ``"°F"``).
        :attr:`Unit.PERCENT` is rejected pre-I/O — the register is
        temperature-only.

        Raw enumeration codes (15 / 30) are not accepted here. Callers
        who want the lower-level path use
        ``write_parameter("display_units", 30)``.

        .. warning::

            On at least one PM3 firmware revision this register is
            **label-only**: writing it changes the enum the device
            reports when 17050 is read back, but does not change the
            scale of temperature values exchanged over comms. This
            setter therefore does **not** affect
            :class:`Reading.unit`. To tell watlowlib what scale
            temperatures are actually on, pass
            ``assert_wire_temperature_unit=`` to
            :func:`watlowlib.open_device`.

        Persistent write (parameter 17050 is RWE); pass ``confirm=True``
        to acknowledge the EEPROM write. The session raises
        :class:`WatlowConfirmationRequiredError` pre-I/O if missing.

        Returns the device-echoed label after the write. ``None`` if
        the device's echo decodes outside the known codes.
        """
        resolved = coerce_unit(unit)
        code = display_code_for_unit(resolved)
        if code is None:
            raise WatlowValidationError(
                "set_comms_unit_label accepts CELSIUS / FAHRENHEIT only; "
                "PERCENT is not a valid display-unit code",
            )
        # PERSISTENT write — session enforces ``confirm=True`` pre-I/O.
        await self.write_parameter(
            "display_units",
            code,
            confirm=confirm,
            timeout=timeout,
        )
        self._session.invalidate_comms_unit_label()
        return await self._session.comms_unit_label()

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
        # Device-neutral: the bound profile owns the family-specific
        # identity sequence (EZ-ZONE PM reads 1009/1001/1002/serial;
        # Series SD reads the numeric 10/11/13 + serial 7-8 + reg 18).
        info = await self._session.profile.identify(
            self,
            timeout=timeout,
            strict=strict,
            query_configured_protocol=query_configured_protocol,
        )
        # Cache for ``self.loop(n)``'s eager validator and ``snapshot``.
        # Identify is the canonical place that sets these — open()
        # doesn't have the identity yet, and a device's loop count /
        # capabilities never change mid-session.
        self._loops = info.loops
        self._capabilities = info.capabilities
        self._device_info = info
        return info

    # --- Snapshot --------------------------------------------------------

    async def snapshot(self, *, name: str | None = None) -> WatlowDeviceSnapshot:
        """Return an I/O-free :class:`WatlowDeviceSnapshot`.

        Built from cached identity (populated by :meth:`identify`,
        which :func:`watlowlib.open_device` calls by default) plus
        the session's last error and per-command availability cache.
        Does **not** issue any reads — safe to call from monitoring
        loops at high cadence.

        Args:
            name: Override the snapshot's ``name`` field. Defaults to
                the controller's transport label, matching the
                manager-assigned name surfaced into emitted samples.
        """
        info = self._device_info
        model = info.part_number.raw if info is not None else None
        firmware = (
            str(info.firmware_id) if info is not None and info.firmware_id is not None else None
        )
        serial = info.serial_number if info is not None else None
        # Snapshot is built from cached state; availability_summary is
        # a frozen view of the session's UNSUPPORTED-marked commands.
        availability = {
            key: state
            for key, state in self._session.availability_summary().items()
            if state.name == "UNSUPPORTED"
        }
        return WatlowDeviceSnapshot(
            name=name if name is not None else self._transport.label,
            model=model,
            firmware=firmware,
            serial=serial,
            connected=self._transport.is_open,
            last_error=self._session.last_error,
            recoverable_error_count=self._session.recoverable_error_count,
            captured_at=datetime.now(UTC),
            family=info.family if info is not None else None,
            capabilities=self._capabilities if self._capabilities is not None else Capability.NONE,
            availability_summary=availability,
        )
