"""``read_comms_unit_label`` must degrade to ``None`` on profiles without 17050.

The helper reads PM parameter 17050 ("Communications - Display Units").
The Series SD and Series F4P registries have no such row, and the
registry lookup happens before any I/O, so the helper used to raise
``WatlowValidationError`` instead of returning ``None``. Adapters that
prime the label right after ``identify()`` (capa does) could then not
open either device at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from watlowlib import EZZONE_PROFILE, F4P_PROFILE, SERIES_SD_PROFILE, Unit
from watlowlib.devices.controller import Controller
from watlowlib.devices.session import Session
from watlowlib.protocol.modbus import ModbusProtocolClient
from watlowlib.testing import FakeSlave, FakeTransport, controller_from_fixture
from watlowlib.transport.base import SerialSettings

if TYPE_CHECKING:
    from watlowlib.devices.profile import DeviceProfile

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("fixture", "profile"),
    [
        ("f4p_modbus_identify.jsonl", F4P_PROFILE),
        ("sd_modbus_pv_setpoint.jsonl", SERIES_SD_PROFILE),
    ],
    ids=["f4p", "series_sd"],
)
async def test_read_comms_unit_label_is_none_without_display_units_row(
    anyio_backend: object,
    fixture: str,
    profile: DeviceProfile,
) -> None:
    _ = anyio_backend
    controller = await controller_from_fixture(FIXTURES / fixture, profile=profile)
    async with controller as ctl:
        assert await ctl.read_comms_unit_label() is None
        # Cached: the second call is still a no-op.
        assert await ctl.read_comms_unit_label() is None


@pytest.mark.anyio
async def test_read_comms_unit_label_skips_the_wire_without_display_units_row(
    anyio_backend: object,
) -> None:
    """No request reaches the slave when the registry cannot name the row."""
    _ = anyio_backend
    slave = FakeSlave({})
    transport = FakeTransport(label="fake://f4p")
    client = ModbusProtocolClient(slave_provider=lambda _addr: slave, port=transport.label)
    session = Session(client, profile=F4P_PROFILE, address=10, port=transport.label)
    ctl = Controller(session, transport, serial_settings=SerialSettings(port="fake://f4p"))
    async with ctl:
        assert await ctl.read_comms_unit_label() is None
    assert slave.reads == []
    assert slave.writes == []


@pytest.mark.anyio
async def test_read_comms_unit_label_still_reads_17050_on_pm(anyio_backend: object) -> None:
    """The PM path is unchanged: 17050 is read and decoded."""
    _ = anyio_backend
    from watlowlib import PARAMETERS

    addr = PARAMETERS.resolve("display_units").relative_addr
    slave = FakeSlave({("read_holding_registers", addr): (15,)})  # 15 = °C
    transport = FakeTransport(label="fake://pm")
    client = ModbusProtocolClient(slave_provider=lambda _addr: slave, port=transport.label)
    session = Session(client, profile=EZZONE_PROFILE, address=1, port=transport.label)
    ctl = Controller(session, transport, serial_settings=SerialSettings(port="fake://pm"))
    async with ctl:
        assert await ctl.read_comms_unit_label() is Unit.CELSIUS
    assert [r[1] for r in slave.reads] == [addr]
