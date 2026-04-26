"""Unit tests for Modbus per-DataType encoding defaults."""

from __future__ import annotations

import pytest
from anymodbus import ByteOrder, WordOrder

from watlowlib.errors import WatlowProtocolUnsupportedError
from watlowlib.protocol.modbus import ModbusFn
from watlowlib.protocol.modbus.tables import encoding_for
from watlowlib.protocol.stdbus.tlv import DataType


@pytest.mark.parametrize(
    ("data_type", "expected_count"),
    [
        (DataType.FLOAT, 2),
        (DataType.S32, 2),
        (DataType.U32, 2),
        (DataType.U16, 1),
        (DataType.U8, 1),
        (DataType.PACKED, 1),
    ],
)
def test_default_register_count(data_type: DataType, expected_count: int) -> None:
    enc = encoding_for(data_type)
    assert enc.register_count == expected_count
    assert enc.word_order is WordOrder.HIGH_LOW
    assert enc.byte_order is ByteOrder.BIG
    assert enc.read_fn is ModbusFn.READ_HOLDING


def test_word_order_override() -> None:
    enc = encoding_for(DataType.FLOAT, word_order_override="low_high")
    assert enc.word_order is WordOrder.LOW_HIGH
    # other fields preserved
    assert enc.register_count == 2
    assert enc.byte_order is ByteOrder.BIG


def test_register_count_override_for_string() -> None:
    enc = encoding_for(DataType.STRING, register_count_override=16)
    assert enc.register_count == 16


def test_unknown_word_order_raises() -> None:
    with pytest.raises(WatlowProtocolUnsupportedError, match="word_order"):
        encoding_for(DataType.FLOAT, word_order_override="not-a-word-order")
