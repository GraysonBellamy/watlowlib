"""Unit tests for :class:`ModbusProtocolClient` against a stub :class:`Slave`."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Sequence
from anymodbus import (
    IllegalDataAddressError,
    IllegalDataValueError,
    IllegalFunctionError,
    SlaveDeviceFailureError,
)

from watlowlib.errors import (
    WatlowConnectionError,
    WatlowModbusIllegalDataAddressError,
    WatlowModbusIllegalDataValueError,
    WatlowModbusIllegalFunctionError,
    WatlowModbusSlaveFailureError,
    WatlowProtocolUnsupportedError,
)
from watlowlib.protocol.base import ProtocolKind
from watlowlib.protocol.modbus import ModbusFn, ModbusOp, ModbusProtocolClient


@dataclass
class _Call:
    """One recorded call against the stub :class:`StubSlave`."""

    method: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]


def _instantiate_modbus_exc(cls: type[Exception]) -> Exception:
    """Build an instance of an anymodbus exception class.

    ``ModbusExceptionResponse`` subclasses require ``function_code``
    as a keyword-only arg; the rest accept a positional message.
    """
    from anymodbus import ModbusExceptionResponse

    if issubclass(cls, ModbusExceptionResponse):
        return cls(function_code=3)
    return cls("scripted")


class StubSlave:
    """Minimal :class:`anymodbus.Slave` stand-in for tests.

    Records every call and returns scripted responses (or raises a
    scripted exception). Tests assert on :attr:`calls` to verify the
    ModbusOp lowered correctly.
    """

    def __init__(
        self,
        *,
        read_holding_response: tuple[int, ...] = (),
        read_input_response: tuple[int, ...] = (),
        raise_on: type[Exception] | None = None,
    ) -> None:
        self.calls: list[_Call] = []
        self._read_holding = read_holding_response
        self._read_input = read_input_response
        self._raise = raise_on

    def _maybe_raise(self) -> None:
        if self._raise is not None:
            raise _instantiate_modbus_exc(self._raise)

    async def read_holding_registers(self, address: int, *, count: int) -> tuple[int, ...]:
        self.calls.append(_Call("read_holding_registers", (address,), {"count": count}))
        self._maybe_raise()
        return self._read_holding

    async def read_input_registers(self, address: int, *, count: int) -> tuple[int, ...]:
        self.calls.append(_Call("read_input_registers", (address,), {"count": count}))
        self._maybe_raise()
        return self._read_input

    async def write_register(self, address: int, value: int) -> None:
        self.calls.append(_Call("write_register", (address, value), {}))
        self._maybe_raise()

    async def write_registers(self, address: int, values: Sequence[int]) -> None:
        self.calls.append(_Call("write_registers", (address, tuple(values)), {}))
        self._maybe_raise()


@pytest.fixture
def slave() -> StubSlave:
    return StubSlave(read_holding_response=(0x43C4, 0x0000))


@pytest.fixture
def client(slave: StubSlave) -> ModbusProtocolClient:
    return ModbusProtocolClient(lambda _addr: slave, port="fake://test")


@pytest.mark.anyio
async def test_kind(anyio_backend: object, client: ModbusProtocolClient) -> None:
    _ = anyio_backend
    assert client.kind is ProtocolKind.MODBUS_RTU
    assert client.disposed is False


@pytest.mark.anyio
async def test_read_holding(
    anyio_backend: object, client: ModbusProtocolClient, slave: StubSlave
) -> None:
    _ = anyio_backend
    op = ModbusOp(fn=ModbusFn.READ_HOLDING, address=2160, count=2)
    words = await client.execute(op, address=1, command_name="read_setpoint")
    assert words == (0x43C4, 0x0000)
    assert slave.calls == [
        _Call("read_holding_registers", (2160,), {"count": 2}),
    ]


@pytest.mark.anyio
async def test_read_input(anyio_backend: object) -> None:
    _ = anyio_backend
    slave = StubSlave(read_input_response=(0x0042,))
    client = ModbusProtocolClient(lambda _addr: slave)
    op = ModbusOp(fn=ModbusFn.READ_INPUT, address=100, count=1)
    words = await client.execute(op, address=2)
    assert words == (0x0042,)
    assert slave.calls == [
        _Call("read_input_registers", (100,), {"count": 1}),
    ]


@pytest.mark.anyio
async def test_write_register(
    anyio_backend: object, client: ModbusProtocolClient, slave: StubSlave
) -> None:
    _ = anyio_backend
    op = ModbusOp(fn=ModbusFn.WRITE_REGISTER, address=10, count=1, values=(0x1234,))
    result = await client.execute(op, address=1)
    assert result == ()
    assert slave.calls == [_Call("write_register", (10, 0x1234), {})]


@pytest.mark.anyio
async def test_write_registers(
    anyio_backend: object, client: ModbusProtocolClient, slave: StubSlave
) -> None:
    _ = anyio_backend
    op = ModbusOp(
        fn=ModbusFn.WRITE_REGISTERS,
        address=2160,
        count=2,
        values=(0x43C4, 0x0000),
    )
    result = await client.execute(op, address=1)
    assert result == ()
    assert slave.calls == [_Call("write_registers", (2160, (0x43C4, 0x0000)), {})]


@pytest.mark.anyio
async def test_dispose_blocks_execute(anyio_backend: object, client: ModbusProtocolClient) -> None:
    _ = anyio_backend
    client.dispose()
    assert client.disposed is True
    op = ModbusOp(fn=ModbusFn.READ_HOLDING, address=0, count=1)
    with pytest.raises(WatlowConnectionError):
        await client.execute(op, address=1)


@pytest.mark.anyio
async def test_slave_provider_receives_per_call_address(anyio_backend: object) -> None:
    """The slave_provider gets the per-call address — not a constructor default."""
    _ = anyio_backend
    seen: list[int] = []
    slave = StubSlave(read_holding_response=(0,))

    def provider(addr: int) -> StubSlave:
        seen.append(addr)
        return slave

    client = ModbusProtocolClient(provider)
    op = ModbusOp(fn=ModbusFn.READ_HOLDING, address=0, count=1)
    await client.execute(op, address=3)
    await client.execute(op, address=7)
    assert seen == [3, 7]


@pytest.mark.anyio
async def test_illegal_function_remaps_to_unsupported(
    anyio_backend: object,
) -> None:
    _ = anyio_backend
    slave = StubSlave(raise_on=IllegalFunctionError)
    client = ModbusProtocolClient(lambda _addr: slave)
    op = ModbusOp(fn=ModbusFn.READ_HOLDING, address=0, count=1)
    with pytest.raises(WatlowModbusIllegalFunctionError) as info:
        await client.execute(op, address=1)
    # And inherits the protocol-unsupported tag the session looks for.
    assert isinstance(info.value, WatlowProtocolUnsupportedError)
    # Original anymodbus exception preserved on __cause__.
    assert isinstance(info.value.__cause__, IllegalFunctionError)


@pytest.mark.anyio
async def test_illegal_data_address_remaps_to_unsupported(
    anyio_backend: object,
) -> None:
    _ = anyio_backend
    slave = StubSlave(raise_on=IllegalDataAddressError)
    client = ModbusProtocolClient(lambda _addr: slave)
    op = ModbusOp(fn=ModbusFn.READ_HOLDING, address=99999 % 0x10000, count=1)
    with pytest.raises(WatlowModbusIllegalDataAddressError) as info:
        await client.execute(op, address=1)
    assert isinstance(info.value, WatlowProtocolUnsupportedError)


@pytest.mark.anyio
async def test_illegal_data_value_does_not_unsupported(
    anyio_backend: object,
) -> None:
    _ = anyio_backend
    slave = StubSlave(raise_on=IllegalDataValueError)
    client = ModbusProtocolClient(lambda _addr: slave)
    op = ModbusOp(fn=ModbusFn.WRITE_REGISTER, address=0, count=1, values=(0,))
    with pytest.raises(WatlowModbusIllegalDataValueError) as info:
        await client.execute(op, address=1)
    # Crucially NOT a WatlowProtocolUnsupportedError — bad value is not absence.
    assert not isinstance(info.value, WatlowProtocolUnsupportedError)


@pytest.mark.anyio
async def test_slave_failure_does_not_unsupported(anyio_backend: object) -> None:
    _ = anyio_backend
    slave = StubSlave(raise_on=SlaveDeviceFailureError)
    client = ModbusProtocolClient(lambda _addr: slave)
    op = ModbusOp(fn=ModbusFn.READ_HOLDING, address=0, count=1)
    with pytest.raises(WatlowModbusSlaveFailureError) as info:
        await client.execute(op, address=1)
    assert not isinstance(info.value, WatlowProtocolUnsupportedError)


@pytest.mark.anyio
async def test_address_validation_happens_per_call(anyio_backend: object) -> None:
    """Address range is validated on every execute, not at construction."""
    from watlowlib.errors import WatlowConfigurationError

    _ = anyio_backend
    slave = StubSlave(read_holding_response=(0,))
    client = ModbusProtocolClient(lambda _addr: slave)
    op = ModbusOp(fn=ModbusFn.READ_HOLDING, address=0, count=1)
    with pytest.raises(WatlowConfigurationError, match="out of range"):
        await client.execute(op, address=0)
    with pytest.raises(WatlowConfigurationError, match="out of range"):
        await client.execute(op, address=248)


@dataclass
class _NoOpSlave:
    """Slave that accepts but never returns to test timeout."""

    waited: list[float] = field(default_factory=list)

    async def read_holding_registers(self, address: int, *, count: int) -> tuple[int, ...]:
        _ = address, count
        import anyio

        # Block forever; the client's timeout should fire.
        await anyio.sleep(60)
        return ()

    async def read_input_registers(self, address: int, *, count: int) -> tuple[int, ...]:
        _ = address, count
        return ()

    async def write_register(self, address: int, value: int) -> None:
        _ = address, value

    async def write_registers(self, address: int, values: Sequence[int]) -> None:
        _ = address, values


@pytest.mark.anyio
async def test_timeout_raises_watlow_timeout(anyio_backend: object) -> None:
    from watlowlib.errors import WatlowTimeoutError

    _ = anyio_backend
    slave = _NoOpSlave()
    client = ModbusProtocolClient(lambda _addr: slave)
    op = ModbusOp(fn=ModbusFn.READ_HOLDING, address=0, count=1)
    with pytest.raises(WatlowTimeoutError, match="timed out"):
        await client.execute(op, address=1, timeout=0.05)
