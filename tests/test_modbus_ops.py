"""Unit tests for :class:`ModbusOp` invariants."""

from __future__ import annotations

import pytest

from watlowlib.protocol.modbus import ModbusFn, ModbusOp


def test_read_op_minimal() -> None:
    op = ModbusOp(fn=ModbusFn.READ_HOLDING, address=2160, count=2)
    assert op.fn is ModbusFn.READ_HOLDING
    assert op.address == 2160
    assert op.count == 2
    assert op.values is None


def test_write_register_requires_one_value() -> None:
    op = ModbusOp(fn=ModbusFn.WRITE_REGISTER, address=10, count=1, values=(0x1234,))
    assert op.values == (0x1234,)


def test_write_registers_values_count_must_match() -> None:
    with pytest.raises(ValueError, match="length"):
        ModbusOp(
            fn=ModbusFn.WRITE_REGISTERS,
            address=0,
            count=2,
            values=(1, 2, 3),  # mismatched
        )


def test_write_register_rejects_multi_value() -> None:
    with pytest.raises(ValueError, match="exactly one value"):
        ModbusOp(
            fn=ModbusFn.WRITE_REGISTER,
            address=0,
            count=1,
            values=(1, 2),
        )


def test_read_op_rejects_values() -> None:
    with pytest.raises(ValueError, match="does not accept values"):
        ModbusOp(
            fn=ModbusFn.READ_HOLDING,
            address=0,
            count=1,
            values=(1,),
        )


def test_write_op_requires_values() -> None:
    with pytest.raises(ValueError, match="requires non-None values"):
        ModbusOp(fn=ModbusFn.WRITE_REGISTER, address=0)


@pytest.mark.parametrize("addr", [-1, 0x1_0000])
def test_address_range(addr: int) -> None:
    with pytest.raises(ValueError, match="register address"):
        ModbusOp(fn=ModbusFn.READ_HOLDING, address=addr, count=1)


@pytest.mark.parametrize("count", [0, 126])
def test_read_count_range(count: int) -> None:
    with pytest.raises(ValueError, match=r"count \d+ out of range"):
        ModbusOp(fn=ModbusFn.READ_HOLDING, address=0, count=count)


@pytest.mark.parametrize("count", [0, 124])
def test_write_registers_count_range(count: int) -> None:
    with pytest.raises(ValueError, match=r"count \d+ out of range"):
        ModbusOp(
            fn=ModbusFn.WRITE_REGISTERS,
            address=0,
            count=count,
            values=tuple(range(max(count, 0))),
        )


def test_write_register_rejects_count_other_than_one() -> None:
    with pytest.raises(ValueError, match="expects count=1"):
        ModbusOp(fn=ModbusFn.WRITE_REGISTER, address=0, count=2, values=(0, 0))
