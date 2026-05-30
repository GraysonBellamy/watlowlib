"""End-to-end facade tests for the Modbus path.

Drives the full :class:`Controller` facade against a stub
:class:`anymodbus.Slave`. Asserts the cross-cutting invariants:

- ``Reading.protocol == MODBUS_RTU`` for any read produced over the
  Modbus client.
- ``Availability.UNKNOWN → SUPPORTED`` after a successful call.
- ``Availability.UNKNOWN → UNSUPPORTED`` on Modbus
  ``IllegalFunction`` / ``IllegalDataAddress``.
- ``WatlowConfirmationRequiredError`` raises pre-I/O on missing
  ``confirm=True`` for setpoint writes (RWES → PERSISTENT).
- The same script that works on Std Bus works unchanged here, simply
  by switching the protocol kind.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, cast

import pytest
from anymodbus import IllegalDataAddressError, IllegalFunctionError
from anyserial import Parity

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

from tests.test_modbus_client import _instantiate_modbus_exc  # pyright: ignore[reportPrivateUsage]
from watlowlib import (
    Availability,
    Controller,
    ProtocolKind,
    SerialSettings,
    WatlowConfirmationRequiredError,
)
from watlowlib.devices.profile import EZZONE_PROFILE
from watlowlib.devices.session import Session
from watlowlib.errors import (
    WatlowModbusIllegalDataAddressError,
    WatlowModbusIllegalFunctionError,
    WatlowProtocolUnsupportedError,
)
from watlowlib.protocol.modbus import ModbusProtocolClient

# Setpoint relative_addr = 2160 (FLOAT, 2 regs); PV at 360 (FLOAT, 2 regs).
# 392.0 → 0x43C40000 → (0x43C4, 0x0000) HIGH_LOW.
_SETPOINT_ADDR = 2160
_PV_ADDR = 360


class _StubBusTransport:
    """Minimal :class:`Transport`-shaped stand-in for :class:`ModbusBusTransport`.

    The real adapter opens a serial port via :func:`anymodbus.open_modbus_rtu`.
    For tests we only need the lifecycle hooks (``is_open`` / ``open`` /
    ``close``) — the protocol client uses its own injected slave provider,
    so the transport never has to speak Modbus.
    """

    def __init__(self, label: str = "fake://modbus") -> None:
        self._label = label
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def label(self) -> str:
        return self._label

    async def open(self) -> None:
        self._open = True

    async def close(self) -> None:
        self._open = False

    async def write(self, data: bytes, *, timeout: float) -> None:
        _ = data, timeout
        msg = "stub transport never writes"
        raise NotImplementedError(msg)

    async def read_exact(self, n: int, *, timeout: float) -> bytes:
        _ = n, timeout
        msg = "stub transport never reads"
        raise NotImplementedError(msg)

    async def read_available(self, *, idle_timeout: float, max_bytes: int | None = None) -> bytes:
        _ = idle_timeout, max_bytes
        return b""

    async def drain_input(self) -> None:
        return None


class _ScriptedSlave:
    """Stub :class:`anymodbus.Slave` driven by a per-(method, address) script.

    Script entries are either:
    - a tuple of register words (returned as-is from a read), or
    - a :class:`type` subclass of :class:`Exception` (raised on the call).

    Writes are recorded in :attr:`writes` (always) and, if the address
    has a script entry that's an exception class, raise it.
    """

    def __init__(self, script: dict[tuple[str, int], Any]) -> None:
        self._script = script
        self.writes: list[tuple[str, int, Any]] = []

    async def read_holding_registers(self, address: int, *, count: int) -> tuple[int, ...]:
        return self._read("read_holding_registers", address, count=count)

    async def read_input_registers(self, address: int, *, count: int) -> tuple[int, ...]:
        return self._read("read_input_registers", address, count=count)

    async def write_register(self, address: int, value: int) -> None:
        self.writes.append(("write_register", address, value))
        self._maybe_raise(("write_register", address))

    async def write_registers(self, address: int, values: Sequence[int]) -> None:
        self.writes.append(("write_registers", address, tuple(values)))
        self._maybe_raise(("write_registers", address))

    def _read(self, method: str, address: int, *, count: int) -> tuple[int, ...]:
        key = (method, address)
        if key not in self._script:
            msg = f"no scripted response for {key} (count={count})"
            raise KeyError(msg)
        entry = self._script[key]
        if isinstance(entry, type) and issubclass(entry, Exception):
            raise _instantiate_modbus_exc(entry)
        # ``entry`` is the response tuple at this point — runtime narrowed.
        result = tuple(cast("Iterable[int]", entry))
        if len(result) != count:
            msg = (
                f"scripted response length {len(result)} does not match "
                f"requested count={count} for {key}"
            )
            raise AssertionError(msg)
        return result

    def _maybe_raise(self, key: tuple[str, int]) -> None:
        entry = self._script.get(key)
        if isinstance(entry, type) and issubclass(entry, Exception):
            raise _instantiate_modbus_exc(entry)


def _build_controller(slave: _ScriptedSlave) -> Controller:
    """Wire a stub Slave through the real :class:`Session` + :class:`Controller`."""
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
        transport,
        serial_settings=SerialSettings(port="fake://modbus", baudrate=9600, parity=Parity.EVEN),
    )


@pytest.mark.anyio
async def test_facade_modbus_round_trip(anyio_backend: object) -> None:
    """The Std Bus round-trip script works unchanged over Modbus."""
    _ = anyio_backend
    slave = _ScriptedSlave(
        {
            # PV: arbitrary FLOAT response — exact value isn't asserted on,
            # only that the read decodes and Reading.protocol is MODBUS_RTU.
            ("read_holding_registers", _PV_ADDR): (0x4393, 0x717E),
            ("read_holding_registers", _SETPOINT_ADDR): (0x43C4, 0x0000),
        }
    )
    controller = _build_controller(slave)
    async with controller as ctl:
        assert ctl.session.protocol_kind is ProtocolKind.MODBUS_RTU
        assert ctl.session.availability("read_parameter:4001") is Availability.UNKNOWN

        pv = await ctl.read_pv()
        assert pv.protocol is ProtocolKind.MODBUS_RTU
        assert pv.value is not None
        assert ctl.session.availability("read_parameter:4001") is Availability.SUPPORTED

        # Setpoint write requires confirm=True (RWES → PERSISTENT).
        with pytest.raises(WatlowConfirmationRequiredError):
            await ctl.set_setpoint(392.0)
        assert slave.writes == []

        echo = await ctl.set_setpoint(392.0, confirm=True)
        assert echo.protocol is ProtocolKind.MODBUS_RTU
        assert echo.value is not None
        assert math.isclose(echo.value, 392.0)
        assert slave.writes == [("write_registers", _SETPOINT_ADDR, (0x43C4, 0x0000))]

        sp = await ctl.read_setpoint()
        assert sp.protocol is ProtocolKind.MODBUS_RTU
        assert sp.value is not None
        assert math.isclose(sp.value, 392.0)


@pytest.mark.anyio
async def test_modbus_illegal_function_flips_unsupported(anyio_backend: object) -> None:
    """Modbus ``IllegalFunction`` flips the cache to UNSUPPORTED and stickies."""
    _ = anyio_backend
    slave = _ScriptedSlave({("read_holding_registers", _PV_ADDR): IllegalFunctionError})
    controller = _build_controller(slave)
    async with controller as ctl:
        with pytest.raises(WatlowModbusIllegalFunctionError):
            await ctl.read_pv()
        assert ctl.session.availability("read_parameter:4001") is Availability.UNSUPPORTED
        # Second call short-circuits pre-I/O.
        with pytest.raises(WatlowProtocolUnsupportedError):
            await ctl.read_pv()


@pytest.mark.anyio
async def test_modbus_illegal_data_address_flips_unsupported(
    anyio_backend: object,
) -> None:
    """Modbus ``IllegalDataAddress`` flips the cache to UNSUPPORTED."""
    _ = anyio_backend
    slave = _ScriptedSlave({("read_holding_registers", _PV_ADDR): IllegalDataAddressError})
    controller = _build_controller(slave)
    async with controller as ctl:
        with pytest.raises(WatlowModbusIllegalDataAddressError):
            await ctl.read_pv()
        assert ctl.session.availability("read_parameter:4001") is Availability.UNSUPPORTED
