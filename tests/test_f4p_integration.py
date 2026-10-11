"""End-to-end Series F4P read slice against the captured IR-301 round-trips.

``f4p_modbus_identify.jsonl`` holds the CRC-verified frames from the
bench ISDC IR-301 blackbody controller (a Watlow F4P inside) on COM8,
Modbus RTU address 10, 19200 8-N-1, plus the surrounding status
snapshot. These tests are the "does it actually work" gate for the F4P
profile: model check, decimal-register scaling, unit from register
901, and the public facade reads, all without a serial port.

Writes are out of scope for the F4P; every test asserts nothing hit
the fake slave's write path.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from anymodbus.crc import crc16_modbus

from watlowlib import (
    F4P_PROFILE,
    Capability,
    Controller,
    ControllerFamily,
    ProtocolKind,
    Unit,
    WatlowProtocolError,
)
from watlowlib.devices.models import DeviceHealth
from watlowlib.devices.profile import F4P_MODEL_CODE
from watlowlib.devices.session import Session
from watlowlib.protocol.modbus import ModbusProtocolClient
from watlowlib.testing import FakeSlave, FakeTransport, controller_from_fixture, load_fixture
from watlowlib.transport.base import SerialSettings

F4P_FIXTURE = Path(__file__).parent / "fixtures" / "f4p_modbus_identify.jsonl"

#: Verified bench traffic (request, reply) for FC03 single-register reads
#: at address 10, from the probe log. Both CRCs were checked by hand.
VERIFIED_FRAMES: dict[int, tuple[bytes, bytes]] = {
    0: (bytes.fromhex("0a 03 00 00 00 01 85 71"), bytes.fromhex("0a 03 02 14 a0 12 fd")),
    100: (bytes.fromhex("0a 03 00 64 00 01 c4 ae"), bytes.fromhex("0a 03 02 17 52 93 88")),
    300: (bytes.fromhex("0a 03 01 2c 00 01 45 44"), bytes.fromhex("0a 03 02 17 70 13 91")),
}


def _f4p_controller_over_slave(slave: FakeSlave) -> Controller:
    """Build an F4P controller wired to a scriptable Modbus slave."""
    transport = FakeTransport(label="fake://f4p")
    client = ModbusProtocolClient(slave_provider=lambda _addr: slave, port=transport.label)
    session = Session(client, profile=F4P_PROFILE, address=10, port=transport.label)
    return Controller(
        session,
        transport,
        serial_settings=SerialSettings(port="fake://f4p", baudrate=19200),
    )


def _identity_script(**overrides: tuple[int, ...]) -> dict[tuple[str, int], tuple[int, ...]]:
    """The six registers ``f4p_identify`` reads, with per-test overrides."""
    base: dict[int, tuple[int, ...]] = {
        0: (F4P_MODEL_CODE,),
        1: (1,),
        2: (8883,),
        3: (10,),
        606: (1,),
        901: (1,),
        100: (5970,),
    }
    for key, words in overrides.items():
        base[int(key.removeprefix("r"))] = words
    return {("read_holding_registers", reg): words for reg, words in base.items()}


# --- Fixture provenance ---------------------------------------------


def test_fixture_rounds_match_verified_frames() -> None:
    """The fixture words for registers 0 / 100 / 300 are the bytes the F4P sent."""
    fixture = load_fixture(F4P_FIXTURE)
    assert fixture.protocol is ProtocolKind.MODBUS_RTU
    assert fixture.address == 10
    assert fixture.serial_settings.baudrate == 19200
    by_address = {r.address: r for r in fixture.modbus_rounds}
    for register, (request, reply) in VERIFIED_FRAMES.items():
        # CRC-16/Modbus, little-endian trailer, on both directions.
        for frame in (request, reply):
            assert crc16_modbus(frame[:-2]).to_bytes(2, "little") == frame[-2:]
        # FC03, one register, address 10.
        assert request[:6] == bytes([10, 3]) + register.to_bytes(2, "big") + (1).to_bytes(2, "big")
        assert reply[:3] == bytes([10, 3, 2])
        word = int.from_bytes(reply[3:5], "big")
        assert by_address[register].response_words == (word,)


# --- Identify ---------------------------------------------------------


@pytest.mark.anyio
async def test_f4p_identify_from_fixture(anyio_backend: object) -> None:
    _ = anyio_backend
    controller = await controller_from_fixture(F4P_FIXTURE, profile=F4P_PROFILE)
    async with controller as ctl:
        info = await ctl.identify()
        assert info.family is ControllerFamily.F4P
        assert info.part_number.raw == ""
        assert info.part_number.family is ControllerFamily.F4P
        assert info.hardware_id == F4P_MODEL_CODE
        assert info.firmware_id == 10
        assert info.serial_number == "018883"
        assert info.health is DeviceHealth.OK
        assert info.capabilities is Capability.NONE
        assert info.loops == 1
        assert info.address == 10
        assert info.protocol is ProtocolKind.MODBUS_RTU
        assert info.configured_protocol is None
        # Session state derived from the device: scale from 606, unit from 901.
        assert ctl.session.registry.resolve("process_value").scale == 0.1
        assert ctl.session.wire_temperature_unit() is Unit.CELSIUS
        # The profile's own registry is untouched by the rebind.
        assert F4P_PROFILE.registry.resolve("process_value").scale == 1.0


@pytest.mark.anyio
async def test_f4p_reads_after_identify(anyio_backend: object) -> None:
    _ = anyio_backend
    controller = await controller_from_fixture(F4P_FIXTURE, profile=F4P_PROFILE)
    async with controller as ctl:
        await ctl.identify()
        pv = await ctl.read_pv()
        assert pv.protocol is ProtocolKind.MODBUS_RTU
        assert pv.value is not None
        assert math.isclose(pv.value, 597.0)
        assert pv.unit is Unit.CELSIUS
        sp = await ctl.read_setpoint()
        assert sp.value is not None
        assert math.isclose(sp.value, 600.0)
        assert sp.unit is Unit.CELSIUS
        power = await ctl.read_parameter("output_power")
        assert isinstance(power.value, float)
        assert math.isclose(power.value, 13.0)
        # Deviation alarm bands: ±0.5 °C and ±10 °C, two's-complement words.
        lo1 = await ctl.read_parameter("alarm_1_low_setpoint")
        hi1 = await ctl.read_parameter("alarm_1_high_setpoint")
        lo2 = await ctl.read_parameter("alarm_2_low_setpoint")
        hi2 = await ctl.read_parameter("alarm_2_high_setpoint")
        assert [lo1.value, hi1.value, lo2.value, hi2.value] == [-0.5, 0.5, -10.0, 10.0]
        # Configuration rows read live: type S thermocouple, deviation
        # alarms, a 45.0 % high power limit (tenths, like % power), the
        # F4P's own CJC register in °F, 60 Hz mains, firmware 24.00.
        assert (await ctl.read_parameter("input_1_sensor_type")).value == 9
        assert (await ctl.read_parameter("alarm_1_type")).value == 2
        limit = await ctl.read_parameter("output_1a_high_power_limit")
        assert isinstance(limit.value, float)
        assert math.isclose(limit.value, 45.0)
        cjc = await ctl.read_parameter("cold_junction_temperature")
        assert isinstance(cjc.value, float)
        assert math.isclose(cjc.value, 71.1)
        revision = await ctl.read_parameter("software_revision")
        assert isinstance(revision.value, float)
        assert math.isclose(revision.value, 24.0)
        assert (await ctl.read_parameter("line_frequency")).value == 60
        # Enumerations stay plain ints.
        for name, expected in (
            ("alarm_1_status", 2),
            ("alarm_2_status", 0),
            ("input_error", 0),
            ("auto_manual", 1),
            ("output_1a_type", 3),
            ("system_error_status", 0),
            ("show_temperature_unit", 1),
        ):
            entry = await ctl.read_parameter(name)
            assert entry.value == expected, name
            assert isinstance(entry.value, int), name


@pytest.mark.anyio
async def test_f4p_reads_before_identify_are_raw_integers(anyio_backend: object) -> None:
    """Without identify the registry keeps its factory default: no decimals, no unit."""
    _ = anyio_backend
    controller = await controller_from_fixture(F4P_FIXTURE, profile=F4P_PROFILE)
    async with controller as ctl:
        pv = await ctl.read_pv()
        assert pv.value == 5970.0
        assert pv.unit is None


@pytest.mark.anyio
async def test_f4p_poll_many_tags_units_after_identify(anyio_backend: object) -> None:
    """The streaming path resolves through the rebound registry too."""
    _ = anyio_backend
    controller = await controller_from_fixture(F4P_FIXTURE, profile=F4P_PROFILE)
    async with controller as ctl:
        await ctl.identify()
        samples = await ctl.poll_many(["process_value", "output_power", "alarm_1_status"])
    by_name = {s.parameter: s for s in samples}
    assert by_name["process_value"].value == 597.0
    assert by_name["process_value"].unit is Unit.CELSIUS
    assert by_name["output_power"].value == 13.0
    assert by_name["output_power"].unit is Unit.PERCENT
    assert by_name["alarm_1_status"].value == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("decimals", "units_code", "expected_pv", "expected_unit"),
    [
        (0, 0, 5970.0, Unit.FAHRENHEIT),
        (1, 1, 597.0, Unit.CELSIUS),
        (2, 0, 59.7, Unit.FAHRENHEIT),
        (3, 1, 5.97, Unit.CELSIUS),
    ],
)
async def test_f4p_identify_derives_scale_and_unit_from_device(
    anyio_backend: object,
    decimals: int,
    units_code: int,
    expected_pv: float,
    expected_unit: Unit,
) -> None:
    """The same raw word decodes per the live 606 / 901 values, not a constant."""
    _ = anyio_backend
    slave = FakeSlave(_identity_script(r606=(decimals,), r901=(units_code,)))
    ctl = _f4p_controller_over_slave(slave)
    async with ctl:
        info = await ctl.identify()
        assert info.health is DeviceHealth.OK
        pv = await ctl.read_pv()
        assert pv.value is not None
        assert math.isclose(pv.value, expected_pv)
        assert pv.unit is expected_unit
    assert slave.writes == []


@pytest.mark.anyio
async def test_f4p_identify_rejects_foreign_model(anyio_backend: object) -> None:
    """A device answering register 0 with anything but 5280 is not an F4P."""
    _ = anyio_backend
    slave = FakeSlave(_identity_script(r0=(1234,)))
    ctl = _f4p_controller_over_slave(slave)
    async with ctl:
        info = await ctl.identify()
        assert info.health is DeviceHealth.FAILED
        assert info.hardware_id is None
        assert info.firmware_id is None
        assert info.serial_number is None
        assert info.family is ControllerFamily.F4P
        # Nothing derived: registry and unit stay at the profile defaults.
        assert ctl.session.registry is F4P_PROFILE.registry
        assert ctl.session.wire_temperature_unit() is None
        with pytest.raises(WatlowProtocolError, match="expected 5280"):
            await ctl.identify(strict=True)
    # Only register 0 was touched; the secondary reads never ran.
    assert {r[1] for r in slave.reads} == {0}
    assert slave.writes == []


@pytest.mark.anyio
async def test_f4p_identify_silent_model_register(anyio_backend: object) -> None:
    _ = anyio_backend
    slave = FakeSlave({})  # every read is unscripted → typed protocol error
    ctl = _f4p_controller_over_slave(slave)
    async with ctl:
        info = await ctl.identify()
        assert info.health is DeviceHealth.FAILED
        assert info.hardware_id is None
        with pytest.raises(WatlowProtocolError):
            await ctl.identify(strict=True)


@pytest.mark.anyio
async def test_f4p_identify_partial_when_decimal_register_silent(anyio_backend: object) -> None:
    """Model matched but 606 did not answer: PARTIAL, default scale, unit still set."""
    _ = anyio_backend
    script = _identity_script()
    del script[("read_holding_registers", 606)]
    slave = FakeSlave(script)
    ctl = _f4p_controller_over_slave(slave)
    async with ctl:
        info = await ctl.identify()
        assert info.health is DeviceHealth.PARTIAL
        assert info.hardware_id == F4P_MODEL_CODE
        assert info.firmware_id == 10
        assert ctl.session.registry.resolve("process_value").scale == 1.0
        assert ctl.session.wire_temperature_unit() is Unit.CELSIUS


@pytest.mark.anyio
async def test_f4p_identify_partial_when_unit_register_silent(anyio_backend: object) -> None:
    _ = anyio_backend
    script = _identity_script()
    del script[("read_holding_registers", 901)]
    slave = FakeSlave(script)
    ctl = _f4p_controller_over_slave(slave)
    async with ctl:
        info = await ctl.identify()
        assert info.health is DeviceHealth.PARTIAL
        assert ctl.session.registry.resolve("process_value").scale == 0.1
        assert ctl.session.wire_temperature_unit() is None
        pv = await ctl.read_pv()
        assert pv.value == 597.0
        assert pv.unit is None


@pytest.mark.anyio
async def test_f4p_identify_tolerates_missing_serial(anyio_backend: object) -> None:
    _ = anyio_backend
    script = _identity_script()
    del script[("read_holding_registers", 1)]
    del script[("read_holding_registers", 2)]
    slave = FakeSlave(script)
    ctl = _f4p_controller_over_slave(slave)
    async with ctl:
        info = await ctl.identify()
        # Serial is informational; health keys off software / 606 / 901.
        assert info.health is DeviceHealth.OK
        assert info.serial_number is None


@pytest.mark.anyio
async def test_f4p_identify_is_read_only_fc03(anyio_backend: object) -> None:
    """Identify plus the facade reads issue holding-register reads and nothing else."""
    _ = anyio_backend
    slave = FakeSlave(_identity_script(r300=(6000,), r103=(130,)))
    ctl = _f4p_controller_over_slave(slave)
    async with ctl:
        await ctl.identify()
        await ctl.read_pv()
        await ctl.read_setpoint()
        await ctl.read_parameter("output_power")
    assert slave.writes == []
    assert {method for method, _addr, _count in slave.reads} == {"read_holding_registers"}
    assert all(count == 1 for _method, _addr, count in slave.reads)
    assert [addr for _m, addr, _c in slave.reads][:6] == [0, 1, 2, 3, 606, 901]


@pytest.mark.anyio
async def test_f4p_identify_rebinds_on_repeat(anyio_backend: object) -> None:
    """A second identify after the decimal setting changed re-derives the scale."""
    _ = anyio_backend
    slave = FakeSlave(_identity_script(r606=(1,)))
    ctl = _f4p_controller_over_slave(slave)
    async with ctl:
        await ctl.identify()
        assert ctl.session.registry.resolve("process_value").scale == 0.1
        slave.add_script("read_holding_registers", 606, (0,))
        await ctl.identify()
        assert ctl.session.registry.resolve("process_value").scale == 1.0
        pv = await ctl.read_pv()
        assert pv.value == 5970.0


def test_f4p_profile_shape() -> None:
    assert F4P_PROFILE.name == "f4p"
    assert F4P_PROFILE.family is ControllerFamily.F4P
    assert F4P_PROFILE.default_protocol is ProtocolKind.MODBUS_RTU
    assert F4P_PROFILE.default_serial.baudrate == 19200
    assert F4P_PROFILE.default_serial.parity.value == "none"
    assert F4P_PROFILE.wire_temperature_unit is None
    assert F4P_PROFILE.supported_baudrates == frozenset({9600, 19200})
