"""``ControllerLoop`` and PID command tests.

Covers the multi-loop sub-facade returned by :meth:`Controller.loop`
and the loop-level PID / output helpers in
:mod:`watlowlib.commands.loop`. Drives the facade end-to-end through
:class:`FakeTransport` for Std Bus and through a stub
:class:`anymodbus.Slave` for Modbus.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, cast

import pytest

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

from anyserial import Parity

from watlowlib import (
    PARAMETERS,
    Controller,
    ControllerLoop,
    FakeTransport,
    ProtocolKind,
    SerialSettings,
    WatlowConfirmationRequiredError,
    WatlowValidationError,
)
from watlowlib.commands import PidGains, read_pid, write_pid
from watlowlib.devices.profile import EZZONE_PROFILE
from watlowlib.devices.session import Session
from watlowlib.protocol.modbus import ModbusProtocolClient
from watlowlib.testing import open_test_controller

# --- Fixtures -------------------------------------------------------


# Captured PM3 round-trip for setpoint at instance=2 (cls=7 mem=1 inst=2).
# We use the Modbus path for multi-loop testing since the registry's
# PM3 captures only cover instance=1 on Std Bus.

# Setpoint relative_addr = 2160; instance=2 register would be at a
# per-loop offset, but the current code raises on Modbus instance > 1
# (multi-loop Modbus arithmetic is not yet implemented).


def test_loop_validates_zero_or_negative() -> None:
    """``loop(0)`` / ``loop(-1)`` are rejected at construction."""

    # ControllerLoop's __init__ validates without touching transport.
    # Build a minimal Controller-like stub so we can exercise the
    # validator without opening anything.
    class _Stub:
        loops: int | None = None

    stub = _Stub()
    with pytest.raises(WatlowValidationError, match="1-indexed"):
        ControllerLoop(stub, 0)  # type: ignore[arg-type]
    with pytest.raises(WatlowValidationError, match="1-indexed"):
        ControllerLoop(stub, -1)  # type: ignore[arg-type]


def test_loop_validates_against_known_loop_count() -> None:
    """When loop count is known, ``loop(n)`` rejects out-of-range eagerly."""

    class _Stub:
        loops: int | None = 1

    stub = _Stub()
    # loop(1) accepted.
    ControllerLoop(stub, 1)  # type: ignore[arg-type]
    with pytest.raises(WatlowValidationError, match="out of range"):
        ControllerLoop(stub, 2)  # type: ignore[arg-type]


def test_loop_lazy_when_loop_count_unknown() -> None:
    """Before identify(), ``loop(n)`` defers to per-spec validation."""

    class _Stub:
        loops: int | None = None

    stub = _Stub()
    # No raise — registry validation kicks in at first read instead.
    sub = ControllerLoop(stub, 4)  # type: ignore[arg-type]
    assert sub.number == 4


# --- Std Bus end-to-end via FakeTransport ---------------------------


# PV (4001) read at MAC 0x10, instance=1, captured.
REQ_READ_PV = bytes.fromhex("55 FF 05 10 00 00 06 E8 01 03 01 04 01 01 E3 99")
RSP_READ_PV = bytes.fromhex("55 FF 06 00 10 00 0B 88 02 03 01 04 01 01 08 45 1E 3C D4 A7 28")
REQ_READ_PART = bytes.fromhex("55FF0510000006E80103010109019E6E")
RSP_READ_PART = bytes.fromhex(
    "55FF0600100018780203010109010910504D33523143412D41414141414141000AB4"
)


@pytest.mark.anyio
async def test_loop_one_lowers_to_instance_one_stdbus(anyio_backend: object) -> None:
    """``controller.loop(1).read_pv()`` issues the same wire bytes as ``read_pv``."""
    _ = anyio_backend
    transport = FakeTransport({REQ_READ_PV: RSP_READ_PV})
    controller = await open_test_controller(
        transport,
        protocol=ProtocolKind.STDBUS,
        address=1,
        serial_settings=SerialSettings(port="fake://test"),
    )
    async with controller as ctl:
        baseline = await ctl.read_pv()
        loop1 = ctl.loop(1)
        from_loop = await loop1.read_pv()
        assert from_loop.value == baseline.value
        # Both calls used the same scripted request.
        assert transport.writes.count(REQ_READ_PV) == 2


@pytest.mark.anyio
async def test_loop_validates_after_identify_caches_loops(
    anyio_backend: object,
) -> None:
    """After identify on PM3, ``loops == 1`` and ``loop(2)`` raises at construction."""
    _ = anyio_backend
    transport = FakeTransport({REQ_READ_PART: RSP_READ_PART})
    controller = await open_test_controller(
        transport,
        protocol=ProtocolKind.STDBUS,
        address=1,
        serial_settings=SerialSettings(port="fake://test"),
    )
    async with controller as ctl:
        info = await ctl.identify()
        assert info.loops == 1
        assert ctl.loops == 1
        with pytest.raises(WatlowValidationError, match="out of range"):
            ctl.loop(2)


# --- PID over Modbus stub -------------------------------------------


def _instantiate_modbus_exc(cls: type[BaseException]) -> BaseException:
    from anymodbus import ModbusExceptionResponse

    if issubclass(cls, ModbusExceptionResponse):
        return cls(function_code=3)
    return cls("scripted")


class _ScriptedSlave:
    """Stub Slave parametrized by ``(method, address) → reply`` map."""

    def __init__(self, script: dict[tuple[str, int], Any]) -> None:
        self.script = script
        self.writes: list[tuple[str, int, Any]] = []

    async def read_holding_registers(self, address: int, *, count: int) -> tuple[int, ...]:
        return self._lookup("read_holding_registers", address, count=count)

    async def read_input_registers(self, address: int, *, count: int) -> tuple[int, ...]:
        return self._lookup("read_input_registers", address, count=count)

    async def write_register(self, address: int, value: int) -> None:
        self.writes.append(("write_register", address, value))

    async def write_registers(self, address: int, values: Sequence[int]) -> None:
        self.writes.append(("write_registers", address, tuple(values)))

    def _lookup(self, method: str, address: int, *, count: int) -> tuple[int, ...]:
        key = (method, address)
        if key not in self.script:
            msg = f"no scripted response for {key}"
            raise KeyError(msg)
        entry = self.script[key]
        if isinstance(entry, type) and issubclass(entry, BaseException):
            raise _instantiate_modbus_exc(entry)
        result: tuple[int, ...] = tuple(cast("Iterable[int]", entry))
        if len(result) != count:
            msg = f"length {len(result)} != count {count} for {key}"
            raise AssertionError(msg)
        return result


def _build_modbus_controller(slave: _ScriptedSlave) -> Controller:
    """Wire a :class:`_ScriptedSlave` through the real Session + Controller."""

    class _StubBusTransport:
        def __init__(self) -> None:
            self._open = False

        @property
        def is_open(self) -> bool:
            return self._open

        @property
        def label(self) -> str:
            return "fake://modbus"

        async def open(self) -> None:
            self._open = True

        async def close(self) -> None:
            self._open = False

    transport = _StubBusTransport()
    client = ModbusProtocolClient(
        slave_provider=lambda _addr: slave,
        port=transport.label,
    )
    session = Session(
        client,
        profile=EZZONE_PROFILE,
        address=1,
        port=transport.label,
    )
    return Controller(
        session,
        transport,  # type: ignore[arg-type]
        serial_settings=SerialSettings(port="fake://modbus", baudrate=9600, parity=Parity.EVEN),
    )


# Modbus relative addresses (from the PM registry, instance=1):
_HEAT_PB_ADDR = PARAMETERS.resolve("heat_proportional_band").relative_addr  # 1890
_COOL_PB_ADDR = PARAMETERS.resolve("cool_proportional_band").relative_addr  # 1892
_TIME_INT_ADDR = PARAMETERS.resolve("time_integral").relative_addr  # 1894
_TIME_DER_ADDR = PARAMETERS.resolve("time_derivative").relative_addr  # 1896
_DEAD_BAND_ADDR = PARAMETERS.resolve("dead_band").relative_addr  # 1898
_OUTPUT_POWER_ADDR = PARAMETERS.resolve("output_power").relative_addr  # 1908


def _f32(value: float) -> tuple[int, int]:
    """Encode a float as two HIGH_LOW big-endian Modbus words."""
    import struct

    raw = struct.pack(">f", value)
    hi = (raw[0] << 8) | raw[1]
    lo = (raw[2] << 8) | raw[3]
    return (hi, lo)


@pytest.mark.anyio
async def test_loop_read_pid_aggregates_five_reads(anyio_backend: object) -> None:
    _ = anyio_backend
    slave = _ScriptedSlave(
        {
            ("read_holding_registers", _HEAT_PB_ADDR): _f32(25.0),
            ("read_holding_registers", _COOL_PB_ADDR): _f32(15.0),
            ("read_holding_registers", _TIME_INT_ADDR): _f32(180.0),
            ("read_holding_registers", _TIME_DER_ADDR): _f32(30.0),
            ("read_holding_registers", _DEAD_BAND_ADDR): _f32(2.0),
        }
    )
    controller = _build_modbus_controller(slave)
    async with controller as ctl:
        gains = await ctl.loop(1).read_pid()
    assert gains.heat_proportional_band is not None
    assert math.isclose(gains.heat_proportional_band, 25.0)
    assert gains.cool_proportional_band == pytest.approx(15.0)
    assert gains.time_integral == pytest.approx(180.0)
    assert gains.time_derivative == pytest.approx(30.0)
    assert gains.dead_band == pytest.approx(2.0)
    assert gains.not_none() is True


@pytest.mark.anyio
async def test_loop_write_pid_requires_confirm(anyio_backend: object) -> None:
    """PID writes are PERSISTENT — without ``confirm=True`` we get pre-I/O failure."""
    _ = anyio_backend
    slave = _ScriptedSlave({})
    controller = _build_modbus_controller(slave)
    async with controller as ctl:
        gains = PidGains(heat_proportional_band=12.0)
        with pytest.raises(WatlowConfirmationRequiredError):
            await ctl.loop(1).write_pid(gains)
        assert slave.writes == []


@pytest.mark.anyio
async def test_loop_write_pid_skips_none_fields(anyio_backend: object) -> None:
    """Fields left ``None`` produce no wire writes."""
    _ = anyio_backend
    slave = _ScriptedSlave({})
    controller = _build_modbus_controller(slave)
    async with controller as ctl:
        # heat_pb / time_integral registry ranges max out at 9.0 in
        # the parsed JSON metadata; stay inside both bands.
        gains = PidGains(heat_proportional_band=8.0, time_integral=2.0)
        applied = await ctl.loop(1).write_pid(gains, confirm=True)
    # Only 2 writes, one per non-None field; the rest skipped.
    assert len(slave.writes) == 2
    assert applied.cool_proportional_band is None
    assert applied.heat_proportional_band == pytest.approx(8.0)
    assert applied.time_integral == pytest.approx(2.0)


@pytest.mark.anyio
async def test_loop_read_output(anyio_backend: object) -> None:
    _ = anyio_backend
    slave = _ScriptedSlave(
        {("read_holding_registers", _OUTPUT_POWER_ADDR): _f32(42.5)},
    )
    controller = _build_modbus_controller(slave)
    async with controller as ctl:
        out = await ctl.loop(1).read_output()
    assert out.protocol is ProtocolKind.MODBUS_RTU
    assert out.value is not None
    assert math.isclose(out.value, 42.5, rel_tol=1e-4)


@pytest.mark.anyio
async def test_read_pid_swallows_unsupported_per_field(anyio_backend: object) -> None:
    """Cool-only field absent → PidGains.cool_proportional_band is None."""
    _ = anyio_backend
    from anymodbus import IllegalDataAddressError

    slave = _ScriptedSlave(
        {
            ("read_holding_registers", _HEAT_PB_ADDR): _f32(25.0),
            ("read_holding_registers", _COOL_PB_ADDR): IllegalDataAddressError,
            ("read_holding_registers", _TIME_INT_ADDR): _f32(180.0),
            ("read_holding_registers", _TIME_DER_ADDR): _f32(30.0),
            ("read_holding_registers", _DEAD_BAND_ADDR): _f32(2.0),
        }
    )
    controller = _build_modbus_controller(slave)
    async with controller as ctl:
        gains = await read_pid(ctl.session, instance=1)
    assert gains.cool_proportional_band is None
    assert gains.not_none() is False
    assert gains.heat_proportional_band == pytest.approx(25.0)


@pytest.mark.anyio
async def test_write_pid_propagates_confirm_to_each_field(anyio_backend: object) -> None:
    """Helper-level ``write_pid`` call exercises confirmation gate per field."""
    _ = anyio_backend
    slave = _ScriptedSlave({})
    controller = _build_modbus_controller(slave)
    async with controller as ctl:
        gains = PidGains(heat_proportional_band=10.0)
        with pytest.raises(WatlowConfirmationRequiredError):
            await write_pid(ctl.session, gains, instance=1)
        assert slave.writes == []


@pytest.mark.anyio
async def test_read_pid_skips_cool_fields_without_has_cooling(anyio_backend: object) -> None:
    """When capabilities lack HAS_COOLING, the cool-side reads are skipped.

    The fake slave intentionally has *no* script for the cool
    parameters — if the gate weren't honoured, the read would fall
    through and either error or return garbage. With the gate, the
    cool fields surface as ``None`` without ever hitting the wire.
    Hardware-day-2026-04-26 findings §2.3.
    """
    _ = anyio_backend
    from watlowlib import Capability

    slave = _ScriptedSlave(
        {
            ("read_holding_registers", _HEAT_PB_ADDR): _f32(25.0),
            ("read_holding_registers", _TIME_INT_ADDR): _f32(180.0),
            ("read_holding_registers", _TIME_DER_ADDR): _f32(30.0),
        }
    )
    controller = _build_modbus_controller(slave)
    async with controller as ctl:
        gains = await read_pid(ctl.session, instance=1, capabilities=Capability.NONE)
    assert gains.heat_proportional_band == pytest.approx(25.0)
    assert gains.cool_proportional_band is None
    assert gains.dead_band is None
    # Cool-side scripts were intentionally absent — confirming the
    # read returned ``None`` is itself proof the gate skipped the wire
    # call (otherwise the unscripted lookup would have raised KeyError
    # in the slave stub).


@pytest.mark.anyio
async def test_write_pid_refuses_cool_fields_without_has_cooling(
    anyio_backend: object,
) -> None:
    """Writing a cool-side field on a no-cooling SKU raises pre-I/O."""
    _ = anyio_backend
    from watlowlib import Capability, WatlowConfigurationError

    slave = _ScriptedSlave({})
    controller = _build_modbus_controller(slave)
    async with controller as ctl:
        gains = PidGains(cool_proportional_band=12.0)
        with pytest.raises(WatlowConfigurationError, match="HAS_COOLING"):
            await write_pid(
                ctl.session,
                gains,
                instance=1,
                confirm=True,
                capabilities=Capability.NONE,
            )
    assert slave.writes == []
