"""Unit tests for the Modbus pack / unpack codec."""

from __future__ import annotations

import math

import pytest
from anymodbus import ByteOrder, WordOrder

from watlowlib.errors import WatlowProtocolError
from watlowlib.protocol.modbus.codec import decode_words, encode_value_to_words
from watlowlib.protocol.stdbus.tlv import DataType


def test_float_round_trip_high_low() -> None:
    words = encode_value_to_words(
        392.0,
        data_type=DataType.FLOAT,
        register_count=2,
        word_order=WordOrder.HIGH_LOW,
        byte_order=ByteOrder.BIG,
    )
    # 392.0 → 0x43C40000 → high word 0x43C4, low word 0x0000.
    assert words == (0x43C4, 0x0000)
    value = decode_words(
        words,
        data_type=DataType.FLOAT,
        word_order=WordOrder.HIGH_LOW,
        byte_order=ByteOrder.BIG,
    )
    assert isinstance(value, float)
    assert math.isclose(value, 392.0)


def test_float_low_high_word_order() -> None:
    # Same float value but the device emits low word first.
    words_lh = encode_value_to_words(
        392.0,
        data_type=DataType.FLOAT,
        register_count=2,
        word_order=WordOrder.LOW_HIGH,
        byte_order=ByteOrder.BIG,
    )
    assert words_lh == (0x0000, 0x43C4)
    value = decode_words(
        words_lh,
        data_type=DataType.FLOAT,
        word_order=WordOrder.LOW_HIGH,
        byte_order=ByteOrder.BIG,
    )
    assert isinstance(value, float)
    assert math.isclose(value, 392.0)


def test_s32_round_trip_negative() -> None:
    words = encode_value_to_words(
        -1,
        data_type=DataType.S32,
        register_count=2,
        word_order=WordOrder.HIGH_LOW,
        byte_order=ByteOrder.BIG,
    )
    assert words == (0xFFFF, 0xFFFF)
    value = decode_words(
        words,
        data_type=DataType.S32,
        word_order=WordOrder.HIGH_LOW,
        byte_order=ByteOrder.BIG,
    )
    assert value == -1


def test_u32_round_trip_large() -> None:
    big = 2**31 + 7
    words = encode_value_to_words(
        big,
        data_type=DataType.U32,
        register_count=2,
        word_order=WordOrder.HIGH_LOW,
        byte_order=ByteOrder.BIG,
    )
    value = decode_words(
        words,
        data_type=DataType.U32,
        word_order=WordOrder.HIGH_LOW,
        byte_order=ByteOrder.BIG,
    )
    assert value == big


def test_u16_round_trip() -> None:
    words = encode_value_to_words(
        0xABCD,
        data_type=DataType.U16,
        register_count=1,
        word_order=WordOrder.HIGH_LOW,
        byte_order=ByteOrder.BIG,
    )
    assert words == (0xABCD,)
    value = decode_words(
        words,
        data_type=DataType.U16,
        word_order=WordOrder.HIGH_LOW,
        byte_order=ByteOrder.BIG,
    )
    assert value == 0xABCD


def test_u8_low_byte() -> None:
    words = encode_value_to_words(
        0xCD,
        data_type=DataType.U8,
        register_count=1,
        word_order=WordOrder.HIGH_LOW,
        byte_order=ByteOrder.BIG,
    )
    assert words == (0xCD,)
    value = decode_words(
        words,
        data_type=DataType.U8,
        word_order=WordOrder.HIGH_LOW,
        byte_order=ByteOrder.BIG,
    )
    assert value == 0xCD


def test_string_round_trip_pads_and_strips() -> None:
    # 8-register STRING field; "PM3R1CA" fits in 7 bytes, plus NUL pad.
    words = encode_value_to_words(
        "PM3R1CA",
        data_type=DataType.STRING,
        register_count=8,
        word_order=WordOrder.HIGH_LOW,
        byte_order=ByteOrder.BIG,
    )
    assert len(words) == 8
    value = decode_words(
        words,
        data_type=DataType.STRING,
        word_order=WordOrder.HIGH_LOW,
        byte_order=ByteOrder.BIG,
    )
    assert value == "PM3R1CA"


def test_decode_wrong_count_raises() -> None:
    with pytest.raises(WatlowProtocolError, match="FLOAT expects 2"):
        decode_words(
            (0x4348,),
            data_type=DataType.FLOAT,
            word_order=WordOrder.HIGH_LOW,
            byte_order=ByteOrder.BIG,
        )


def test_string_overflow_raises() -> None:
    with pytest.raises(WatlowProtocolError, match="exceeds register capacity"):
        encode_value_to_words(
            "A" * 5,
            data_type=DataType.STRING,
            register_count=2,  # capacity = 4 bytes
            word_order=WordOrder.HIGH_LOW,
            byte_order=ByteOrder.BIG,
        )
