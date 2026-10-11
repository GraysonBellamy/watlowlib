"""Writes to ``rwes = R`` rows are rejected before any wire traffic.

``R`` used to set only ``SafetyTier.READ_ONLY`` on the spec; the write
variants still encoded and sent the request, leaving the device to
refuse it. For the Series F4P, whose every row is ``R`` because the
blackbody it controls is a live calibration source, that would have let
``set_setpoint(confirm=True)`` reach the controller.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from watlowlib import (
    EZZONE_PROFILE,
    F4P_PROFILE,
    PARAMETERS,
    SERIES_SD_PROFILE,
    Controller,
    ProtocolKind,
    WatlowValidationError,
)
from watlowlib.commands import WRITE_PARAMETER, CommandContext, WriteParameterRequest
from watlowlib.devices.session import Session
from watlowlib.protocol.modbus import ModbusProtocolClient
from watlowlib.registry.parameters import F4P_PARAMETERS
from watlowlib.testing import (
    FakeSlave,
    FakeTransport,
    controller_from_fixture,
    open_test_controller,
)
from watlowlib.transport.base import SerialSettings

if TYPE_CHECKING:
    from watlowlib.devices.profile import DeviceProfile

FIXTURES = Path(__file__).parent / "fixtures"


def _modbus_controller(profile: DeviceProfile, slave: FakeSlave, *, address: int) -> Controller:
    transport = FakeTransport(label="fake://ro")
    client = ModbusProtocolClient(slave_provider=lambda _addr: slave, port=transport.label)
    session = Session(client, profile=profile, address=address, port=transport.label)
    return Controller(session, transport, serial_settings=SerialSettings(port="fake://ro"))


def test_validate_writable_rejects_r_rows_only() -> None:
    with pytest.raises(WatlowValidationError, match="read-only"):
        F4P_PARAMETERS.validate_writable(F4P_PARAMETERS.resolve("setpoint"))
    with pytest.raises(WatlowValidationError, match="read-only"):
        PARAMETERS.validate_writable(PARAMETERS.resolve("process_value"))
    # Writable rows pass silently.
    PARAMETERS.validate_writable(PARAMETERS.resolve("setpoint"))


def test_modbus_write_variant_refuses_r_row_before_encoding() -> None:
    assert WRITE_PARAMETER.modbus is not None
    ctx = CommandContext(registry=F4P_PARAMETERS)
    with pytest.raises(WatlowValidationError, match="read-only"):
        WRITE_PARAMETER.modbus.encode(ctx, WriteParameterRequest("setpoint", 500.0))


def test_stdbus_write_variant_refuses_r_row_before_encoding() -> None:
    assert WRITE_PARAMETER.stdbus is not None
    ctx = CommandContext(registry=PARAMETERS)
    with pytest.raises(WatlowValidationError, match="read-only"):
        WRITE_PARAMETER.stdbus.encode(ctx, WriteParameterRequest("hardware_id", 1))


@pytest.mark.anyio
async def test_f4p_setpoint_write_never_reaches_the_wire(anyio_backend: object) -> None:
    _ = anyio_backend
    slave = FakeSlave({})
    ctl = _modbus_controller(F4P_PROFILE, slave, address=10)
    async with ctl:
        with pytest.raises(WatlowValidationError, match="read-only"):
            await ctl.set_setpoint(500.0, confirm=True)
        with pytest.raises(WatlowValidationError, match="read-only"):
            await ctl.write_parameter("output_1a_high_power_limit", 45.0, confirm=True)
    assert slave.writes == []
    assert slave.reads == []


@pytest.mark.anyio
async def test_f4p_fixture_reads_still_work_after_refused_write(anyio_backend: object) -> None:
    _ = anyio_backend
    controller = await controller_from_fixture(
        FIXTURES / "f4p_modbus_identify.jsonl", profile=F4P_PROFILE
    )
    async with controller as ctl:
        await ctl.identify()
        with pytest.raises(WatlowValidationError):
            await ctl.set_setpoint(500.0, confirm=True)
        pv = await ctl.read_pv()
        assert pv.value == 597.0


@pytest.mark.anyio
async def test_sd_setpoint_write_still_allowed(anyio_backend: object) -> None:
    """RWE rows are unaffected: the SD set point still lowers to the wire."""
    _ = anyio_backend
    slave = FakeSlave({})
    ctl = _modbus_controller(SERIES_SD_PROFILE, slave, address=10)
    async with ctl:
        await ctl.set_setpoint(62.96, confirm=True)
    assert slave.writes == [("write_registers", 27, (0x0000, 0xF5F0))]


@pytest.mark.anyio
async def test_pm_write_to_r_row_over_stdbus_sends_nothing(anyio_backend: object) -> None:
    _ = anyio_backend
    transport = FakeTransport({}, label="fake://pm")
    ctl = await open_test_controller(
        transport, protocol=ProtocolKind.STDBUS, address=1, profile=EZZONE_PROFILE
    )
    async with ctl:
        with pytest.raises(WatlowValidationError, match="read-only"):
            await ctl.write_parameter("process_value", 1.0, confirm=True)
    assert not transport.writes
