"""Device profiles — the first-class "what kind of controller is this".

A :class:`DeviceProfile` bundles the four things that used to be an
implicit "always EZ-ZONE PM" assumption scattered across the library:

- the :class:`~watlowlib.registry.families.ControllerFamily`,
- the :class:`~watlowlib.registry.parameters.ParameterRegistry` that
  decodes its parameters,
- the wire protocol + serial framing it speaks at the factory, and
- how to :func:`identify` it.

Two profiles ship today:

- :data:`EZZONE_PROFILE` — the reference EZ-ZONE PM family. Its
  ``identify`` is the historical :meth:`Controller.identify` body
  (part number 1009, hardware/firmware ids, serial, optional 17009).
  ``wire_temperature_unit`` is ``None`` — the PM firmware can lie about
  its own unit register, so the user must assert the scale.
- :data:`SERIES_SD_PROFILE` — the Series SD PID controller. Modbus RTU
  at 9600 8-N-1, bare-register map, identity from numeric registers
  (10/11/13 + serial 7-8) and reg 18 for the wire unit. Temperatures
  travel in °F by default (manual: "all temperature parameters through
  Modbus are in °F"), so ``wire_temperature_unit=FAHRENHEIT``.

:data:`DEVICE_PROFILES` is the tuple discovery and tooling iterate.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from watlowlib.devices.capability import Capability
from watlowlib.devices.models import DeviceHealth, DeviceInfo, PartNumber
from watlowlib.errors import WatlowProtocolError, WatlowTransportError
from watlowlib.protocol.base import ProtocolKind
from watlowlib.registry.families import (
    ControllerFamily,
    capabilities_for_part_number,
    decode_part_number,
    default_loops,
)
from watlowlib.registry.parameters import PARAMETERS, SD_PARAMETERS
from watlowlib.registry.units import Unit
from watlowlib.transport.base import Parity, SerialSettings

if TYPE_CHECKING:
    from watlowlib.devices.controller import Controller
    from watlowlib.registry.parameters import ParameterRegistry

__all__ = [
    "DEVICE_PROFILES",
    "EZZONE_PROFILE",
    "SERIES_SD_PROFILE",
    "DeviceProfile",
    "IdentifyStrategy",
    "ezzone_identify",
    "series_sd_identify",
]


# EEPROM-resident protocol codes for PM parameter 17009 (Protocol).
# Mirrors ``maintenance.PROTOCOL_MODE_CODES`` so ``ezzone_identify`` can
# decode the configured protocol without importing maintenance.
_PROTOCOL_CODE_TO_KIND: dict[int, ProtocolKind] = {
    1286: ProtocolKind.STDBUS,
    1057: ProtocolKind.MODBUS_RTU,
}


class IdentifyStrategy(Protocol):
    """How a profile turns an open controller into a :class:`DeviceInfo`.

    Implementations are pure with respect to the controller's cached
    identity — they read parameters and return a :class:`DeviceInfo`;
    :meth:`Controller.identify` is responsible for caching the result.
    """

    async def __call__(
        self,
        controller: Controller,
        *,
        timeout: float | None = None,
        strict: bool = False,
        query_configured_protocol: bool = False,
    ) -> DeviceInfo:
        """Return identity information for ``controller``."""
        ...


@dataclass(frozen=True, slots=True)
class DeviceProfile:
    """A first-class controller type.

    Attributes:
        name: Stable short identifier (``"ezzone"`` / ``"series_sd"``).
        family: The controller family this profile describes.
        registry: Parameter registry used to decode this device's
            parameters.
        default_protocol: Wire protocol opened when the caller does not
            pass one explicitly.
        default_serial: Factory serial framing for ``default_protocol``.
            Its ``port`` is a placeholder — :func:`open_device` applies
            the real port via :func:`dataclasses.replace`.
        identify: Strategy that produces a :class:`DeviceInfo`.
        wire_temperature_unit: The scale temperatures travel in over the
            wire, when the profile knows it for certain. ``None`` means
            "do not guess — the user must assert it" (the EZ-ZONE PM
            contract; some PM firmware misreports its own unit register).
    """

    name: str
    family: ControllerFamily
    registry: ParameterRegistry
    default_protocol: ProtocolKind
    default_serial: SerialSettings
    identify: IdentifyStrategy
    wire_temperature_unit: Unit | None = None


# --- shared identity helpers ----------------------------------------


async def _safe_read_str(
    controller: Controller,
    name_or_id: str | int,
    *,
    timeout: float | None,
) -> str | None:
    """Read a string parameter, returning ``None`` on absence.

    Swallows protocol errors (parameter absent / unsupported) and
    transport timeouts (no reply on the bus); connection / config
    errors propagate so the user sees them.
    """
    try:
        entry = await controller.read_parameter(name_or_id, timeout=timeout)
    except (WatlowProtocolError, WatlowTransportError):
        return None
    if isinstance(entry.value, str):
        return entry.value
    return None


async def _safe_read_int(
    controller: Controller,
    name_or_id: str | int,
    *,
    timeout: float | None,
) -> int | None:
    """Read a numeric parameter, returning ``None`` on absence."""
    try:
        entry = await controller.read_parameter(name_or_id, timeout=timeout)
    except (WatlowProtocolError, WatlowTransportError):
        return None
    if isinstance(entry.value, int | float):
        return int(entry.value)
    return None


# --- EZ-ZONE PM identity (moved verbatim from Controller.identify) --


async def ezzone_identify(
    controller: Controller,
    *,
    timeout: float | None = None,
    strict: bool = False,
    query_configured_protocol: bool = False,
) -> DeviceInfo:
    """Identify an EZ-ZONE PM controller.

    Reads (in order): part number (1009), hardware id (1001), firmware
    id (1002), serial number. Missing secondary fields stay ``None`` and
    promote the result health from OK to PARTIAL. A failed part-number
    read yields ``health=FAILED`` (or raises when ``strict``).
    """
    session = controller.session
    if strict:
        entry = await controller.read_parameter("part_number", timeout=timeout)
        part_raw = entry.value if isinstance(entry.value, str) else None
    else:
        part_raw = await _safe_read_str(controller, "part_number", timeout=timeout)
    hw_id = await _safe_read_int(controller, "hardware_id", timeout=timeout)
    fw_id = await _safe_read_int(controller, "firmware_id", timeout=timeout)
    serial_str = await _safe_read_str(controller, "serial_number", timeout=timeout)

    if part_raw:
        part = decode_part_number(part_raw)
        capabilities = capabilities_for_part_number(part)
        secondary_missing = hw_id is None or fw_id is None
        health = DeviceHealth.PARTIAL if secondary_missing else DeviceHealth.OK
    else:
        part = PartNumber(raw="", family=ControllerFamily.UNKNOWN)
        capabilities = capabilities_for_part_number(part)
        health = DeviceHealth.FAILED

    configured_protocol: ProtocolKind | None = None
    if query_configured_protocol:
        code = await _safe_read_int(controller, 17009, timeout=timeout)
        if code is not None:
            configured_protocol = _PROTOCOL_CODE_TO_KIND.get(code)

    return DeviceInfo(
        part_number=part,
        hardware_id=hw_id,
        firmware_id=fw_id,
        serial_number=serial_str,
        family=part.family,
        protocol=session.protocol_kind,
        address=session.address,
        capabilities=capabilities,
        serial_settings=controller.serial_settings,
        loops=default_loops(part),
        health=health,
        configured_protocol=configured_protocol,
    )


# --- Series SD identity ---------------------------------------------


async def series_sd_identify(
    controller: Controller,
    *,
    timeout: float | None = None,
    strict: bool = False,
    query_configured_protocol: bool = False,
) -> DeviceInfo:
    """Identify a Series SD controller from its numeric registers.

    The SD has **no ASCII model-name register** — identity is numeric:
    Software ID (10), Software Version (11), Software Build (13), and
    Serial Number (regs 7-8). Reg 18 reports the comms temperature unit
    (°F (0) / °C (1)); reading it lets us set the session's wire scale
    honestly rather than trusting the profile default.

    Never reads a PM parameter name — an absent name would raise
    :class:`WatlowValidationError` in ``encode`` (pre-I/O), which the
    safe-read helpers do **not** catch.
    """
    del query_configured_protocol  # SD has no 17009 protocol register
    session = controller.session

    if strict:
        entry = await controller.read_parameter("software_id", timeout=timeout)
        software_id = int(entry.value) if isinstance(entry.value, int | float) else None
    else:
        software_id = await _safe_read_int(controller, "software_id", timeout=timeout)
    software_version = await _safe_read_int(controller, "software_version", timeout=timeout)
    software_build = await _safe_read_int(controller, "software_build", timeout=timeout)
    serial_hi = await _safe_read_int(controller, "serial_number_high", timeout=timeout)
    serial_lo = await _safe_read_int(controller, "serial_number_low", timeout=timeout)

    # Reg 18: comms temperature unit. 0 → °F, 1 → °C. Override the
    # session's wire scale from the live value (honest, no guess).
    units_code = await _safe_read_int(controller, "units", timeout=timeout)
    if units_code is not None:
        session.set_wire_temperature_unit(Unit.CELSIUS if units_code == 1 else Unit.FAHRENHEIT)

    serial_number: str | None = None
    if serial_hi is not None or serial_lo is not None:
        serial_number = f"{serial_hi or 0:04d}{serial_lo or 0:04d}"

    # firmware_id carries the software version; build is appended when
    # present so the snapshot shows the full firmware identity.
    firmware_id = software_version
    _ = software_build  # reserved for a future richer FirmwareVersion mapping

    if software_id is not None:
        health = DeviceHealth.OK if firmware_id is not None else DeviceHealth.PARTIAL
    else:
        health = DeviceHealth.FAILED

    return DeviceInfo(
        part_number=PartNumber(raw="", family=ControllerFamily.SD),
        hardware_id=software_id,
        firmware_id=firmware_id,
        serial_number=serial_number,
        family=ControllerFamily.SD,
        protocol=session.protocol_kind,
        address=session.address,
        capabilities=Capability.NONE,
        serial_settings=controller.serial_settings,
        loops=1,
        health=health,
        configured_protocol=None,
    )


# --- The profiles ----------------------------------------------------

EZZONE_PROFILE: DeviceProfile = DeviceProfile(
    name="ezzone",
    family=ControllerFamily.PM,
    registry=PARAMETERS,
    default_protocol=ProtocolKind.STDBUS,
    default_serial=SerialSettings(port="", baudrate=38400, parity=Parity.NONE),
    identify=ezzone_identify,
    wire_temperature_unit=None,
)

#: Series SD bench framing is **9600 8-N-1** (not the PM Modbus 8-E-1
#: factory default) — confirmed on the COM11 bench unit.
SERIES_SD_PROFILE: DeviceProfile = DeviceProfile(
    name="series_sd",
    family=ControllerFamily.SD,
    registry=SD_PARAMETERS,
    default_protocol=ProtocolKind.MODBUS_RTU,
    default_serial=SerialSettings(port="", baudrate=9600, parity=Parity.NONE),
    identify=series_sd_identify,
    wire_temperature_unit=Unit.FAHRENHEIT,
)

#: Every known profile, in discovery-iteration order.
DEVICE_PROFILES: tuple[DeviceProfile, ...] = (EZZONE_PROFILE, SERIES_SD_PROFILE)
