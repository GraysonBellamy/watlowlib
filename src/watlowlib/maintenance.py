"""Confirmed, port-level maintenance operations.

One-shot helpers that open a transport, perform a single confirmed
reconfiguration, verify the result, and close — for callers who do not
want to hold a live :class:`~watlowlib.devices.controller.Controller`
session. Off the normal :func:`~watlowlib.open_device` path; every
function here mutates persistent device state and refuses without
``confirm=True``.

Surfaces, per design doc §6:

- :func:`change_baud` — write parameter 17002 (Modbus baud) with the
  enum encoding for the new rate, then reopen at the new baud and
  identify.
- :func:`change_modbus_address` — write parameter 17007 (Modbus
  address) and reopen at the new slave address.
- :func:`change_stdbus_address` — write parameter 17001 (Standard
  Bus address) and reopen at the new MS/TP MAC.
- :func:`change_protocol_mode` — write parameter 17009 (Protocol)
  with the wide-enum encoding for Standard Bus (1286) or Modbus
  (1057), then reopen at the new framing and identify.

All four return the post-change :class:`DeviceInfo` from the verify
pass. On verification failure the underlying transport is closed and
the helper re-raises the underlying error after logging a recovery
hint at WARNING — the device may require a power-cycle before the
new comm config takes effect on some firmware revisions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio

from watlowlib._logging import get_logger
from watlowlib.devices.capability import Capability
from watlowlib.devices.factory import open_device
from watlowlib.devices.models import DeviceHealth
from watlowlib.errors import (
    ErrorContext,
    WatlowConfigurationError,
    WatlowConfirmationRequiredError,
    WatlowError,
)
from watlowlib.protocol.base import ProtocolKind
from watlowlib.registry.families import pm_comms_code
from watlowlib.transport.base import SerialSettings

if TYPE_CHECKING:
    from watlowlib.devices.models import DeviceInfo

__all__ = [
    "MODBUS_BAUD_CODES",
    "PROTOCOL_MODE_CODES",
    "change_baud",
    "change_modbus_address",
    "change_protocol_mode",
    "change_stdbus_address",
]

_logger = get_logger("maintenance")


# Wide-enumeration codes from `pm_parameters.json` (parameter 17009).
PROTOCOL_MODE_CODES: dict[ProtocolKind, int] = {
    ProtocolKind.STDBUS: 1286,
    ProtocolKind.MODBUS_RTU: 1057,
}

# Enumeration codes from `pm_parameters.json` (parameter 17002).
# Watlow PM exposes 9600 / 19200 / 38400 over Modbus; Std Bus baud is
# fixed at 38400 (factory default) and not maintenance-configurable.
MODBUS_BAUD_CODES: dict[int, int] = {
    9600: 188,
    19200: 189,
    38400: 190,
}

_VERIFY_DELAY_S: float = 0.5

# Per Modbus RTU spec / Watlow PM manuals.
_MODBUS_ADDRESS_MIN: int = 1
_MODBUS_ADDRESS_MAX: int = 247
# Std Bus MS/TP MAC range exposed to users (1..16, mapped to 0x10..0x1F).
_STDBUS_ADDRESS_MIN: int = 1
_STDBUS_ADDRESS_MAX: int = 16


def _require_confirm(*, confirm: bool, op: str) -> None:
    """Raise :class:`WatlowConfirmationRequiredError` if ``confirm`` is False."""
    if not confirm:
        raise WatlowConfirmationRequiredError(
            f"maintenance.{op} requires confirm=True; call refused with no I/O issued",
        )


async def change_baud(
    port: str,
    *,
    target_baud: int,
    current_protocol: ProtocolKind = ProtocolKind.MODBUS_RTU,
    address: int = 1,
    serial_settings: SerialSettings | None = None,
    timeout: float | None = None,
    confirm: bool = False,
) -> DeviceInfo:
    """Open ``port``, change the Modbus baud rate, reopen, identify.

    Watlow PM exposes baud-rate configuration only on the Modbus side
    (parameter 17002). Std Bus baud is fixed at the factory default
    (38400) and is not maintenance-configurable from the host.

    The host opens at ``current_protocol`` / ``serial_settings`` (the
    current framing, before the change), writes the new code, closes,
    waits ``_VERIFY_DELAY_S``, then reopens at the new baud and
    identifies. ``target_baud`` must be one of the keys in
    :data:`MODBUS_BAUD_CODES`.

    Returns:
        The post-change :class:`DeviceInfo`.

    Raises:
        WatlowConfirmationRequiredError: ``confirm=False``.
        WatlowConfigurationError: ``target_baud`` is not a supported
            Modbus baud rate, or ``current_protocol`` is not Modbus.
    """
    _require_confirm(confirm=confirm, op="change_baud")
    if target_baud not in MODBUS_BAUD_CODES:
        raise WatlowConfigurationError(
            f"target_baud {target_baud!r} is not supported; "
            f"choose one of {sorted(MODBUS_BAUD_CODES.keys())!r}",
            context=ErrorContext(port=port, address=address),
        )
    if current_protocol is not ProtocolKind.MODBUS_RTU:
        raise WatlowConfigurationError(
            "change_baud is only supported over Modbus RTU; "
            "Std Bus baud is fixed at 38400 by the factory.",
            context=ErrorContext(port=port, protocol=current_protocol),
        )
    code = MODBUS_BAUD_CODES[target_baud]
    _logger.warning(
        "maintenance.change_baud port=%s address=%s target_baud=%s "
        "code=%s — verify the device responds at the new baud "
        "after this returns; power-cycle the device if it does not.",
        port,
        address,
        target_baud,
        code,
    )

    settings = serial_settings or SerialSettings(port=port)
    ctl = await open_device(
        port,
        protocol=current_protocol,
        address=address,
        serial_settings=settings,
    )
    try:
        async with ctl:
            await ctl.write_parameter(
                17002,
                code,
                instance=1,
                confirm=True,
                timeout=timeout,
            )
    except WatlowError:
        _logger.warning(
            "maintenance.change_baud write_failed port=%s address=%s",
            port,
            address,
        )
        raise

    await anyio.sleep(_VERIFY_DELAY_S)

    new_settings = SerialSettings(
        port=settings.port,
        baudrate=target_baud,
        bytesize=settings.bytesize,
        parity=settings.parity,
        stopbits=settings.stopbits,
        rtscts=settings.rtscts,
        xonxoff=settings.xonxoff,
        exclusive=settings.exclusive,
    )
    return await _verify(
        port,
        protocol=current_protocol,
        address=address,
        serial_settings=new_settings,
        op="change_baud",
        timeout=timeout,
    )


async def change_modbus_address(
    port: str,
    *,
    target_address: int,
    current_address: int = 1,
    serial_settings: SerialSettings | None = None,
    timeout: float | None = None,
    confirm: bool = False,
) -> DeviceInfo:
    """Open ``port`` over Modbus, write parameter 17007, reopen, identify.

    Modbus slave addresses are 1..247.

    Returns:
        The post-change :class:`DeviceInfo`.

    Raises:
        WatlowConfirmationRequiredError: ``confirm=False``.
        WatlowConfigurationError: ``target_address`` is out of range.
    """
    _require_confirm(confirm=confirm, op="change_modbus_address")
    if not _MODBUS_ADDRESS_MIN <= target_address <= _MODBUS_ADDRESS_MAX:
        raise WatlowConfigurationError(
            f"target_address {target_address!r} out of Modbus range "
            f"{_MODBUS_ADDRESS_MIN}..{_MODBUS_ADDRESS_MAX}",
            context=ErrorContext(port=port, address=target_address),
        )
    _logger.warning(
        "maintenance.change_modbus_address port=%s current_address=%s "
        "target_address=%s — subsequent calls must address the new slave id.",
        port,
        current_address,
        target_address,
    )
    settings = serial_settings or SerialSettings(port=port)
    ctl = await open_device(
        port,
        protocol=ProtocolKind.MODBUS_RTU,
        address=current_address,
        serial_settings=settings,
    )
    try:
        async with ctl:
            await ctl.write_parameter(
                17007,
                target_address,
                instance=1,
                confirm=True,
                timeout=timeout,
            )
    except WatlowError:
        _logger.warning(
            "maintenance.change_modbus_address write_failed port=%s current_address=%s",
            port,
            current_address,
        )
        raise

    await anyio.sleep(_VERIFY_DELAY_S)
    return await _verify(
        port,
        protocol=ProtocolKind.MODBUS_RTU,
        address=target_address,
        serial_settings=settings,
        op="change_modbus_address",
        timeout=timeout,
    )


async def change_stdbus_address(
    port: str,
    *,
    target_address: int,
    current_address: int = 1,
    serial_settings: SerialSettings | None = None,
    timeout: float | None = None,
    confirm: bool = False,
) -> DeviceInfo:
    """Open ``port`` over Std Bus, write parameter 17001, reopen, identify.

    Std Bus accepts MS/TP MAC values 1..16 (mapped to ``0x10..0x1F`` on
    the wire).

    Returns:
        The post-change :class:`DeviceInfo`.

    Raises:
        WatlowConfirmationRequiredError: ``confirm=False``.
        WatlowConfigurationError: ``target_address`` is out of range.
    """
    _require_confirm(confirm=confirm, op="change_stdbus_address")
    if not _STDBUS_ADDRESS_MIN <= target_address <= _STDBUS_ADDRESS_MAX:
        raise WatlowConfigurationError(
            f"target_address {target_address!r} out of Std Bus range "
            f"{_STDBUS_ADDRESS_MIN}..{_STDBUS_ADDRESS_MAX}",
            context=ErrorContext(port=port, address=target_address),
        )
    _logger.warning(
        "maintenance.change_stdbus_address port=%s current_address=%s "
        "target_address=%s — subsequent calls must address the new MS/TP MAC.",
        port,
        current_address,
        target_address,
    )
    settings = serial_settings or SerialSettings(port=port)
    ctl = await open_device(
        port,
        protocol=ProtocolKind.STDBUS,
        address=current_address,
        serial_settings=settings,
    )
    try:
        async with ctl:
            await ctl.write_parameter(
                17001,
                target_address,
                instance=1,
                confirm=True,
                timeout=timeout,
            )
    except WatlowError:
        _logger.warning(
            "maintenance.change_stdbus_address write_failed port=%s current_address=%s",
            port,
            current_address,
        )
        raise

    await anyio.sleep(_VERIFY_DELAY_S)
    return await _verify(
        port,
        protocol=ProtocolKind.STDBUS,
        address=target_address,
        serial_settings=settings,
        op="change_stdbus_address",
        timeout=timeout,
    )


async def change_protocol_mode(
    port: str,
    *,
    target: ProtocolKind,
    current_protocol: ProtocolKind = ProtocolKind.AUTO,
    address: int = 1,
    serial_settings: SerialSettings | None = None,
    timeout: float | None = None,
    confirm: bool = False,
) -> DeviceInfo:
    """Open ``port``, write parameter 17009 (Protocol), reopen, identify.

    The on-wire encoding (per :data:`PROTOCOL_MODE_CODES`) is:

    - :attr:`ProtocolKind.STDBUS` -> ``1286``
    - :attr:`ProtocolKind.MODBUS_RTU` -> ``1057``

    Before issuing the write, the helper opens the port on
    ``current_protocol`` and runs :meth:`Controller.identify`. If the
    captured part number's comms position-8 character indicates the
    target protocol is not present in the SKU's hardware (e.g.
    ``PM3R1CA-AAAAAAA`` is Std-Bus-only), the helper raises
    :class:`WatlowConfigurationError` *before* the EEPROM write —
    avoiding the silent-failure mode where 17009 persists but the
    runtime stack never carries the new protocol.

    The verify pass after the write opens at the *target* protocol's
    factory framing (38400 8-N-1 for Std Bus, 9600 8-E-1 for Modbus
    RTU) rather than inheriting the caller's pre-switch
    ``serial_settings`` — without this substitution, a Std-Bus →
    Modbus switch would try to talk Modbus at the Std-Bus 38400 8-N-1
    framing and time out even when the switch worked.

    Returns the post-change :class:`DeviceInfo` once the device responds
    over the new protocol.

    Raises:
        WatlowConfirmationRequiredError: ``confirm=False``.
        WatlowConfigurationError: ``target`` is not a wire protocol
            this maintenance helper can lower (only :attr:`STDBUS` and
            :attr:`MODBUS_RTU` are accepted; :attr:`AUTO` is rejected),
            or the captured part number proves the SKU does not
            include hardware support for ``target``.
    """
    _require_confirm(confirm=confirm, op="change_protocol_mode")
    if target not in PROTOCOL_MODE_CODES:
        raise WatlowConfigurationError(
            f"change_protocol_mode target must be STDBUS or MODBUS_RTU; got {target!r}",
            context=ErrorContext(port=port, protocol=target, address=address),
        )
    code = PROTOCOL_MODE_CODES[target]
    _logger.warning(
        "maintenance.change_protocol_mode port=%s address=%s target=%s "
        "code=%s — the device may require a power-cycle before it accepts "
        "frames on the new protocol.",
        port,
        address,
        target.value,
        code,
    )
    settings = serial_settings or SerialSettings(port=port)
    ctl = await open_device(
        port,
        protocol=current_protocol,
        address=address,
        serial_settings=settings,
    )
    try:
        async with ctl:
            # Pre-write SKU gate. Capability.HAS_MODBUS is decoded
            # from the part number's comms position-8 character; if
            # the SKU never shipped Modbus and the user
            # is asking for Modbus, refuse here rather than burning a
            # confirmed write that the runtime stack will silently
            # ignore.
            info = await ctl.identify(timeout=timeout)
            _check_sku_supports_protocol(info, target=target, port=port, address=address)
            await ctl.write_parameter(
                17009,
                code,
                instance=1,
                confirm=True,
                timeout=timeout,
            )
    except WatlowError:
        _logger.warning(
            "maintenance.change_protocol_mode write_failed port=%s address=%s",
            port,
            address,
        )
        raise

    await anyio.sleep(_VERIFY_DELAY_S)
    verify_settings = SerialSettings.factory_for(target, port=port)
    return await _verify(
        port,
        protocol=target,
        address=address,
        serial_settings=verify_settings,
        op="change_protocol_mode",
        timeout=timeout,
    )


def _check_sku_supports_protocol(
    info: DeviceInfo,
    *,
    target: ProtocolKind,
    port: str,
    address: int,
) -> None:
    """Raise :class:`WatlowConfigurationError` if ``info``'s SKU lacks ``target``.

    When ``identify`` returned :attr:`DeviceHealth.FAILED` the helper
    refuses the write outright: with no part number captured the SKU
    gate has no signal, and historically the permissive path here
    let 17009 writes land on Std-Bus-only PM3 SKUs, where the verify
    pass then timed out with no typed error. Callers who know the
    SKU does support the target protocol should re-run with a
    ``current_protocol`` / ``serial_settings`` that lets ``identify``
    succeed before reaching this gate.

    When :func:`pm_comms_code` is silent (non-PM family, or PM with
    no decoded options string) the gate stays permissive — refusing
    those would block every non-PM caller from a legitimate switch.
    """
    if info.health is DeviceHealth.FAILED:
        raise WatlowConfigurationError(
            "change_protocol_mode: identify() returned health=FAILED so the "
            "SKU gate has no part number to verify. Refusing the EEPROM write "
            "to avoid persisting protocol 17009 on hardware whose comms code "
            "may not include the target stack — re-run after the device "
            "responds to identify(), or switch protocol manually with "
            "confirm=True after verifying the SKU supports it.",
            context=ErrorContext(
                port=port,
                protocol=target,
                address=address,
                parameter_id=17009,
            ),
        )
    code = pm_comms_code(info.part_number)
    if code is None:
        # Non-PM family or PM with no options string — no gate.
        return

    if target is ProtocolKind.MODBUS_RTU and not (info.capabilities & Capability.HAS_MODBUS):
        raise WatlowConfigurationError(
            f"part number {info.part_number.raw!r} has comms position 8 = "
            f"{code!r} which does not include the Modbus RTU stack on this SKU; "
            "Watlow comms codes that ship Modbus RTU: 1, 2, B, D, E, F. "
            "The 17009 write would persist but the device would never answer "
            "on Modbus — refusing the write.",
            context=ErrorContext(
                port=port,
                protocol=target,
                address=address,
                parameter_id=17009,
            ),
        )
    # Std Bus is included on every PM SKU per the ordering guide
    # (position 8 = 'A' → "None; Standard Bus still included") so
    # there's no symmetric refusal here. If a future PM variant ships
    # Modbus-only, extend this gate with an explicit Std Bus
    # capability bit.


async def _verify(
    port: str,
    *,
    protocol: ProtocolKind,
    address: int,
    serial_settings: SerialSettings,
    op: str,
    timeout: float | None,
) -> DeviceInfo:
    """Open ``port`` at the new framing, identify, return DeviceInfo.

    Wraps the verify pass that every maintenance helper finishes with.
    Uses :meth:`Controller.identify(strict=True)` so a load-bearing
    part-number read failure surfaces as the underlying transport /
    protocol error rather than a happy-looking
    ``DeviceInfo(family=unknown)``. On failure, logs a recovery hint
    at WARNING and re-raises.
    """
    try:
        verify_ctl = await open_device(
            port,
            protocol=protocol,
            address=address,
            serial_settings=serial_settings,
        )
        async with verify_ctl:
            return await verify_ctl.identify(strict=True, timeout=timeout)
    except WatlowError:
        _logger.warning(
            "maintenance.%s verify_failed port=%s address=%s "
            "protocol=%s — the write may have landed but the device "
            "is not responding at the new settings; power-cycle and "
            "retry identify() manually. If this is a protocol switch "
            "and the device's part number's comms position 8 is 'A', "
            "the SKU does not include the target protocol's hardware "
            "stack.",
            op,
            port,
            address,
            protocol.value,
        )
        raise
