"""Tests for ``watlowlib.testing``.

The test surface promotes :class:`FakeTransport`, :class:`FakeSlave`,
and the JSONL fixture loader to a public API. These tests assert the
contract a downstream package can rely on:

- :func:`load_fixture` parses the captured PM3 JSONL files.
- :func:`controller_from_fixture` returns a :class:`Controller` that
  drives the full facade (Std Bus + Modbus paths).
- :class:`FakeTransport`'s new ordered-queue mode and
  :attr:`unmatched_writes` capture work as advertised.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from watlowlib import (
    Availability,
    ProtocolKind,
    WatlowConfirmationRequiredError,
)
from watlowlib.testing import (
    FakeSlave,
    FakeTransport,
    Fixture,
    ModbusRound,
    StdBusRound,
    controller_from_fixture,
    load_fixture,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
PM3_STDBUS = FIXTURES_DIR / "pm3_stdbus_pv_setpoint.jsonl"
PM3_MODBUS = FIXTURES_DIR / "pm3_modbus_pv_setpoint.jsonl"


def test_load_fixture_parses_stdbus_capture() -> None:
    fixture = load_fixture(PM3_STDBUS)
    assert fixture.protocol is ProtocolKind.STDBUS
    assert fixture.address == 1
    labels = [r.label for r in fixture.stdbus_rounds]
    assert "read_pv" in labels
    assert "write_setpoint" in labels
    # Every round carries valid hex.
    for round_ in fixture.stdbus_rounds:
        assert round_.request.startswith(b"\x55\xff")
        assert round_.response.startswith(b"\x55\xff")


def test_load_fixture_parses_modbus_capture() -> None:
    fixture = load_fixture(PM3_MODBUS)
    assert fixture.protocol is ProtocolKind.MODBUS_RTU
    rounds_by_label = {r.label: r for r in fixture.modbus_rounds}
    assert rounds_by_label["read_pv"].method == "read_holding_registers"
    assert rounds_by_label["read_pv"].address == 360
    assert rounds_by_label["read_pv"].response_words == (0x43C4, 0x0000)
    assert rounds_by_label["write_setpoint"].method == "write_registers"
    assert rounds_by_label["write_setpoint"].values == (0x43C4, 0x0000)


@pytest.mark.anyio
async def test_controller_from_fixture_stdbus_drives_facade(anyio_backend: object) -> None:
    """Std Bus fixture replays a full read_pv / set_setpoint scenario."""
    _ = anyio_backend
    controller = await controller_from_fixture(PM3_STDBUS)
    async with controller as ctl:
        pv = await ctl.read_pv()
        assert pv.protocol is ProtocolKind.STDBUS
        assert pv.value is not None
        assert math.isclose(pv.value, 2531.78, rel_tol=1e-3)

        # Confirm gate fires before any I/O hits the fake.
        with pytest.raises(WatlowConfirmationRequiredError):
            await ctl.set_setpoint(392.0)

        echo = await ctl.set_setpoint(392.0, confirm=True)
        assert echo.value is not None
        assert math.isclose(echo.value, 392.0)

        assert ctl.session.availability("read_parameter:4001") is Availability.SUPPORTED


@pytest.mark.anyio
async def test_controller_from_fixture_modbus_drives_facade(anyio_backend: object) -> None:
    """Modbus fixture replays through the same facade path."""
    _ = anyio_backend
    controller = await controller_from_fixture(PM3_MODBUS)
    async with controller as ctl:
        pv = await ctl.read_pv()
        assert pv.protocol is ProtocolKind.MODBUS_RTU
        assert pv.value is not None
        assert math.isclose(pv.value, 392.0)

        echo = await ctl.set_setpoint(392.0, confirm=True)
        assert echo.value is not None
        assert math.isclose(echo.value, 392.0)


@pytest.mark.anyio
async def test_fake_transport_ordered_queue_consumes_in_fifo(
    anyio_backend: object,
) -> None:
    """Same request bytes can yield different replies via the queue."""
    _ = anyio_backend
    req = b"\x01\x02"
    rsp_a = b"\xaa"
    rsp_b = b"\xbb"
    transport = FakeTransport(queue=[(req, rsp_a), (req, rsp_b)])
    await transport.open()
    try:
        await transport.write(req, timeout=0.1)
        assert await transport.read_exact(1, timeout=0.1) == rsp_a
        await transport.write(req, timeout=0.1)
        assert await transport.read_exact(1, timeout=0.1) == rsp_b
        # Queue exhausted: a third write has no reply, so the request
        # is recorded as unmatched.
        await transport.write(req, timeout=0.1)
        assert transport.unmatched_writes == (req,)
    finally:
        await transport.close()


@pytest.mark.anyio
async def test_fake_transport_unmatched_writes_recorded(anyio_backend: object) -> None:
    """Writes with no scripted reply land on ``unmatched_writes``."""
    _ = anyio_backend
    transport = FakeTransport({b"\x10": b"\x20"})
    await transport.open()
    try:
        await transport.write(b"\x10", timeout=0.1)
        await transport.write(b"\x99", timeout=0.1)
        assert transport.writes == (b"\x10", b"\x99")
        assert transport.unmatched_writes == (b"\x99",)
    finally:
        await transport.close()


def test_fixture_dataclasses_are_frozen() -> None:
    """The public dataclasses must stay immutable."""
    round_ = StdBusRound(label="x", request=b"a", response=b"b")
    with pytest.raises(AttributeError):
        round_.label = "y"  # type: ignore[misc]

    modbus = ModbusRound(label="x", method="read_holding_registers", address=0, count=2)
    with pytest.raises(AttributeError):
        modbus.address = 1  # type: ignore[misc]


def test_fake_slave_records_writes() -> None:
    slave = FakeSlave({("read_holding_registers", 360): (0x43C4, 0x0000)})

    # Reads use the script.
    import anyio

    async def _drive() -> None:
        result = await slave.read_holding_registers(360, count=2)
        assert result == (0x43C4, 0x0000)
        await slave.write_registers(2160, [0x43C4, 0x0000])

    anyio.run(_drive)

    assert slave.reads == [("read_holding_registers", 360, 2)]
    assert slave.writes == [("write_registers", 2160, (0x43C4, 0x0000))]


def test_fixture_is_a_dataclass() -> None:
    """Smoke-test :class:`Fixture` constructs cleanly."""
    fixture = Fixture(protocol=ProtocolKind.STDBUS, address=1)
    assert fixture.stdbus_rounds == ()
    assert fixture.modbus_rounds == ()
