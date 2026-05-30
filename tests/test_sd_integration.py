"""End-to-end Series SD read slice against a captured COM11 round-trip.

The fixture ``sd_modbus_pv_setpoint.jsonl`` is a **real** capture from
the bench SD on COM11 (Modbus RTU, addr 10, 9600 8-N-1). This is the P1
"does it actually work" gate: the SD profile + SD_PARAMETERS + scaled
S32/S16 decode all the way through the public facade.

Live values at capture time (panel-confirmed): PV 68.2 °F,
SP 62.96 °F, output power 82.8 %, units °F, no input error.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from watlowlib import (
    SERIES_SD_PROFILE,
    Controller,
    ProtocolKind,
    Unit,
    WatlowConfirmationRequiredError,
)
from watlowlib.devices.session import Session
from watlowlib.protocol.modbus import ModbusProtocolClient
from watlowlib.testing import FakeSlave, FakeTransport, controller_from_fixture

SD_FIXTURE = Path(__file__).parent / "fixtures" / "sd_modbus_pv_setpoint.jsonl"


def _sd_controller_over_slave(slave: FakeSlave) -> Controller:
    """Build an SD controller wired to a scriptable Modbus slave (writes recorded)."""
    transport = FakeTransport(label="fake://sd-write")
    client = ModbusProtocolClient(slave_provider=lambda _addr: slave, port=transport.label)
    session = Session(client, profile=SERIES_SD_PROFILE, address=10, port=transport.label)
    from watlowlib.transport.base import SerialSettings

    return Controller(
        session,
        transport,
        serial_settings=SerialSettings(port="fake://sd-write", baudrate=9600),
    )


@pytest.mark.anyio
async def test_sd_read_pv_decodes_fahrenheit(anyio_backend: object) -> None:
    """read_pv() on the SD fixture returns the live PV in °F."""
    _ = anyio_backend
    controller = await controller_from_fixture(SD_FIXTURE, profile=SERIES_SD_PROFILE)
    async with controller as ctl:
        pv = await ctl.read_pv()
        assert pv.protocol is ProtocolKind.MODBUS_RTU
        assert pv.value is not None
        # 0x00010A68 = 68200 raw → ÷1000 = 68.2 °F (live-captured).
        assert math.isclose(pv.value, 68.2)
        # SERIES_SD_PROFILE asserts the wire scale is °F (manual: "all
        # temperature parameters through Modbus are in °F").
        assert pv.unit is Unit.FAHRENHEIT


@pytest.mark.anyio
async def test_sd_read_setpoint_decodes(anyio_backend: object) -> None:
    _ = anyio_backend
    controller = await controller_from_fixture(SD_FIXTURE, profile=SERIES_SD_PROFILE)
    async with controller as ctl:
        sp = await ctl.read_setpoint()
        assert sp.value is not None
        assert math.isclose(sp.value, 62.96)
        assert sp.unit is Unit.FAHRENHEIT


@pytest.mark.anyio
async def test_sd_output_power_signed_scaled(anyio_backend: object) -> None:
    """Reg 26 (S16, ÷100) decodes to a signed percent."""
    _ = anyio_backend
    controller = await controller_from_fixture(SD_FIXTURE, profile=SERIES_SD_PROFILE)
    async with controller as ctl:
        entry = await ctl.read_parameter("output_power")
        assert isinstance(entry.value, float)
        assert math.isclose(entry.value, 82.8)


@pytest.mark.anyio
async def test_sd_enum_reads_stay_int(anyio_backend: object) -> None:
    """Unscaled enum registers (units, input_error) decode as plain ints."""
    _ = anyio_backend
    controller = await controller_from_fixture(SD_FIXTURE, profile=SERIES_SD_PROFILE)
    async with controller as ctl:
        units = await ctl.read_parameter("units")
        assert units.value == 0
        assert isinstance(units.value, int)
        input_error = await ctl.read_parameter("input_error")
        assert input_error.value == 0
        assert isinstance(input_error.value, int)


@pytest.mark.anyio
async def test_sd_setpoint_write_lowers_scaled_words(anyio_backend: object) -> None:
    """set_setpoint(62.96, confirm=True) lowers to raw 62960 across two regs."""
    _ = anyio_backend
    slave = FakeSlave()
    ctl = _sd_controller_over_slave(slave)
    async with ctl:
        echo = await ctl.set_setpoint(62.96, confirm=True)
        # Echo is the engineering-unit value, not the raw word.
        assert echo.value is not None
        assert math.isclose(echo.value, 62.96)
    # The wire write hit reg 27 with the scaled high/low words.
    assert slave.writes == [("write_registers", 27, (0x0000, 0xF5F0))]


@pytest.mark.anyio
async def test_sd_setpoint_write_requires_confirm(anyio_backend: object) -> None:
    """Setpoint is persistent (RWE) — a write without confirm is gated pre-I/O."""
    _ = anyio_backend
    slave = FakeSlave()
    ctl = _sd_controller_over_slave(slave)
    async with ctl:
        with pytest.raises(WatlowConfirmationRequiredError):
            await ctl.set_setpoint(62.96)
    # Gate fires before any wire traffic.
    assert slave.writes == []


@pytest.mark.anyio
async def test_sd_set_persistent_writes_targets_reg_17(anyio_backend: object) -> None:
    """set_persistent_writes(False) writes 0 to register 17 (EEPROM disable)."""
    _ = anyio_backend
    slave = FakeSlave()
    ctl = _sd_controller_over_slave(slave)
    async with ctl:
        await ctl.set_persistent_writes(False, confirm=True)
        await ctl.set_persistent_writes(True, confirm=True)
    assert slave.writes == [
        ("write_register", 17, (0,)),
        ("write_register", 17, (1,)),
    ]
