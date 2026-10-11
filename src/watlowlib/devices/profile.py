"""Device profiles — the first-class "what kind of controller is this".

A :class:`DeviceProfile` bundles the four things that used to be an
implicit "always EZ-ZONE PM" assumption scattered across the library:

- the :class:`~watlowlib.registry.families.ControllerFamily`,
- the :class:`~watlowlib.registry.parameters.ParameterRegistry` that
  decodes its parameters,
- the wire protocol + serial framing it speaks at the factory, and
- how to :func:`identify` it.

Three profiles ship today:

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
- :data:`F4P_PROFILE` — the Series F4P single-channel controller (the
  Watlow inside the ISDC IR-301 blackbody source). Modbus RTU at 19200
  8-N-1, bare-register map, identity from register 0 (model code
  5280). ``identify`` reads register 606 (input 1 decimal places) and
  rebinds the session registry at the matching scale, and register
  901 for the wire temperature unit. Every registry row is read-only
  for now; writes wait on the blackbody owner's go-ahead.

:data:`DEVICE_PROFILES` is the tuple discovery and tooling iterate;
:func:`profile_named` resolves a profile from its ``name``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from watlowlib._logging import get_logger
from watlowlib.devices.capability import Capability
from watlowlib.devices.models import DeviceHealth, DeviceInfo, PartNumber
from watlowlib.errors import (
    ErrorContext,
    WatlowConfigurationError,
    WatlowProtocolError,
    WatlowTransportError,
)
from watlowlib.protocol.base import ProtocolKind
from watlowlib.registry.families import (
    ControllerFamily,
    capabilities_for_part_number,
    decode_part_number,
    default_loops,
)
from watlowlib.registry.parameters import F4P_PARAMETERS, PARAMETERS, SD_PARAMETERS
from watlowlib.registry.units import Unit
from watlowlib.transport.base import Parity, SerialSettings

if TYPE_CHECKING:
    from watlowlib.devices.controller import Controller
    from watlowlib.registry.parameters import ParameterRegistry

__all__ = [
    "DEVICE_PROFILES",
    "EZZONE_PROFILE",
    "F4P_MODEL_CODE",
    "F4P_PROFILE",
    "SERIES_SD_PROFILE",
    "DeviceProfile",
    "IdentifyStrategy",
    "ezzone_identify",
    "f4p_identify",
    "profile_named",
    "series_sd_identify",
]


_log = get_logger("profile")


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
        name: Stable short identifier (``"ezzone"`` / ``"series_sd"`` /
            ``"f4p"``); :func:`profile_named` resolves it.
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
        supported_baudrates: Baud rates the device can be configured to,
            when the family fixes the set (the F4P offers only 9600 and
            19200). :func:`~watlowlib.devices.discovery.find_devices`
            skips the other bauds of its default sweep for such a
            profile. ``None`` means "any baud" (the PM and SD are
            configurable across the usual range) and nothing is pruned.
    """

    name: str
    family: ControllerFamily
    registry: ParameterRegistry
    default_protocol: ProtocolKind
    default_serial: SerialSettings
    identify: IdentifyStrategy
    wire_temperature_unit: Unit | None = None
    supported_baudrates: frozenset[int] | None = None


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


# --- Series F4P identity --------------------------------------------

#: Value of F4P register 0 ("Model, Diagnostic") on every Series F4P.
F4P_MODEL_CODE: int = 5280

#: F4P register 606 ("Decimal, Analog Input 1"): the setting that fixes
#: the implied decimal places of every input-1-derived register.
_F4P_DECIMAL_REGISTER: int = 606


async def f4p_identify(
    controller: Controller,
    *,
    timeout: float | None = None,
    strict: bool = False,
    query_configured_protocol: bool = False,
) -> DeviceInfo:
    """Identify a Series F4P controller from its numeric registers.

    Register 0 must read :data:`F4P_MODEL_CODE` (5280); anything else
    yields ``health=FAILED`` (or raises when ``strict``) so a foreign
    device answering the same address is never reported as an F4P.
    Then, in order: serial number (registers 1-2), software number
    (3), the input-1 decimal-places setting (606), and the system
    temperature unit (901).

    Two of those reads feed session state rather than the returned
    :class:`DeviceInfo`:

    - **606** decides how many implied decimal places the temperature
      registers carry. The profile registry loads at the factory
      default (none); this strategy rebinds a copy scaled by
      ``10 ** -places`` through :meth:`Session.bind_registry`, so every
      later ``read_pv`` / ``read_setpoint`` lowers correctly without a
      scale baked into the JSON.
    - **901** (0 = °F, 1 = °C) sets the session's wire temperature
      unit from the live value. Register 1923 is **not** consulted:
      the IR-301 manual labels it "Show °F or °C", but the F4P manual
      defines it as the toggle for the unit indicator on the upper
      display.

    Health is ``OK`` when the software number, decimal setting, and
    unit all answered; ``PARTIAL`` when the model matched but any of
    those were missing (scaling or unit then rests on the profile
    defaults and a warning is logged); ``FAILED`` when the model read
    failed or did not match.

    Never reads a PM parameter name; see :func:`series_sd_identify`.
    """
    del query_configured_protocol  # the F4P has no protocol-mode register
    session = controller.session

    if strict:
        entry = await controller.read_parameter("model_code", timeout=timeout)
        model = int(entry.value) if isinstance(entry.value, int | float) else None
    else:
        model = await _safe_read_int(controller, "model_code", timeout=timeout)

    if model != F4P_MODEL_CODE:
        if strict:
            raise WatlowProtocolError(
                f"register 0 reads {model!r}; expected {F4P_MODEL_CODE} for a Series F4P",
                context=ErrorContext(
                    port=session.port,
                    protocol=session.protocol_kind,
                    address=session.address,
                    parameter_id=0,
                ),
            )
        return _f4p_info(
            controller,
            hardware_id=None,
            firmware_id=None,
            serial_number=None,
            health=DeviceHealth.FAILED,
        )

    serial_hi = await _safe_read_int(controller, "serial_number_high", timeout=timeout)
    serial_lo = await _safe_read_int(controller, "serial_number_low", timeout=timeout)
    software_id = await _safe_read_int(controller, "software_id", timeout=timeout)
    decimals = await _safe_read_int(controller, "input_1_decimal", timeout=timeout)
    units_code = await _safe_read_int(controller, "units", timeout=timeout)

    if decimals is not None:
        session.bind_registry(
            session.profile.registry.with_decimal_places(_F4P_DECIMAL_REGISTER, decimals),
        )
    else:
        _log.warning(
            "f4p identify: register 606 (decimal places) did not answer on %s addr=%d; "
            "temperature registers keep the registry default scale",
            session.port,
            session.address,
        )
    if units_code is not None:
        session.set_wire_temperature_unit(
            Unit.CELSIUS if units_code == 1 else Unit.FAHRENHEIT,
        )
    else:
        _log.warning(
            "f4p identify: register 901 (temperature unit) did not answer on %s addr=%d; "
            "temperature readings carry unit=None",
            session.port,
            session.address,
        )

    serial_number: str | None = None
    if serial_hi is not None or serial_lo is not None:
        # The manual gives the serial as one six-digit number split
        # across registers 1 (first part) and 2 (second part); joined as
        # first * 10000 + second. Not yet checked against a nameplate.
        serial_number = f"{(serial_hi or 0) * 10000 + (serial_lo or 0):06d}"

    secondary_missing = software_id is None or decimals is None or units_code is None
    return _f4p_info(
        controller,
        hardware_id=model,
        firmware_id=software_id,
        serial_number=serial_number,
        health=DeviceHealth.PARTIAL if secondary_missing else DeviceHealth.OK,
    )


def _f4p_info(
    controller: Controller,
    *,
    hardware_id: int | None,
    firmware_id: int | None,
    serial_number: str | None,
    health: DeviceHealth,
) -> DeviceInfo:
    """Assemble the F4P :class:`DeviceInfo` (no ASCII part number on the wire)."""
    session = controller.session
    return DeviceInfo(
        part_number=PartNumber(raw="", family=ControllerFamily.F4P),
        hardware_id=hardware_id,
        firmware_id=firmware_id,
        serial_number=serial_number,
        family=ControllerFamily.F4P,
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

#: Series F4P: the ISDC IR-301 bench unit runs **19200 8-N-1** (19200 is
#: the F4P factory default and its framing is fixed at 8-N-1). The F4P
#: Communications menu offers only 9600 and 19200, so discovery prunes
#: the other bauds of its default sweep for this profile.
#: ``wire_temperature_unit`` stays ``None``: register 901 is read at
#: identify time and sets the session scale from the live value.
F4P_PROFILE: DeviceProfile = DeviceProfile(
    name="f4p",
    family=ControllerFamily.F4P,
    registry=F4P_PARAMETERS,
    default_protocol=ProtocolKind.MODBUS_RTU,
    default_serial=SerialSettings(port="", baudrate=19200, parity=Parity.NONE),
    identify=f4p_identify,
    wire_temperature_unit=None,
    supported_baudrates=frozenset({9600, 19200}),
)

#: Every known profile, in discovery-iteration order.
DEVICE_PROFILES: tuple[DeviceProfile, ...] = (EZZONE_PROFILE, SERIES_SD_PROFILE, F4P_PROFILE)


def profile_named(name: str) -> DeviceProfile:
    """Return the profile in :data:`DEVICE_PROFILES` whose ``name`` matches.

    Case-insensitive. Raises :class:`WatlowConfigurationError` for an
    unknown name so CLIs and config loaders fail before any I/O.
    """
    key = name.strip().lower()
    for profile in DEVICE_PROFILES:
        if profile.name == key:
            return profile
    known = ", ".join(p.name for p in DEVICE_PROFILES)
    raise WatlowConfigurationError(
        f"unknown device profile {name!r}; expected one of: {known}",
    )
