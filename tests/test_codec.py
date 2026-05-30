"""Codec round-trip and decode tests against canonical sample frames.

The CANON_* fixtures are the six wire frames documented in
``docs/protocol-stdbus.md``. The LIVE_* fixtures are decoded payloads
from a live EZ-ZONE PM3 capture (2026-04-25) — every wire data type
the codec supports has at least one entry below.
"""

from __future__ import annotations

import math

import pytest

from watlowlib.errors import WatlowValidationError
from watlowlib.protocol.stdbus import (
    DataType,
    ErrorCode,
    ErrorResponse,
    FrameType,
    ReadResponse,
    WriteResponse,
    addr_to_mac,
    decode_frame,
    decode_payload,
    encode_frame,
    encode_read_request,
    encode_write_request,
)

# ----- Frame layer -----

CANON_READ_PV_REQ = bytes.fromhex("55 FF 05 10 00 00 06 E8 01 03 01 04 01 01 E3 99")
CANON_READ_PV_RSP = bytes.fromhex("55 FF 06 00 10 00 0B 88 02 03 01 04 01 01 08 45 1E 3C D4 A7 28")
CANON_WRITE_SP_REQ = bytes.fromhex("55 FF 05 10 00 00 0A EC 01 04 07 01 01 08 43 C4 00 00 EB 77")
CANON_WRITE_SP_RSP = bytes.fromhex("55 FF 06 00 10 00 0A 76 02 04 07 01 01 08 43 C4 00 00 82 03")
CANON_READ_HEAT_REQ = bytes.fromhex("55 FF 05 10 00 00 06 E8 01 03 01 08 03 01 F0 0F")
CANON_READ_HEAT_RSP = bytes.fromhex("55 FF 06 00 10 00 0A 76 02 03 01 08 03 01 0F 01 00 47 C5 6B")


def test_frame_roundtrip_read_pv_req() -> None:
    f = decode_frame(CANON_READ_PV_REQ)
    assert f.frame_type == FrameType.DATA_EXPECTING_REPLY
    assert f.dst == 0x10
    assert f.src == 0x00
    assert f.payload.hex() == "010301040101"
    assert encode_frame(f) == CANON_READ_PV_REQ


def test_frame_roundtrip_all_canon_samples() -> None:
    for raw in (
        CANON_READ_PV_REQ,
        CANON_READ_PV_RSP,
        CANON_WRITE_SP_REQ,
        CANON_WRITE_SP_RSP,
        CANON_READ_HEAT_REQ,
        CANON_READ_HEAT_RSP,
    ):
        assert encode_frame(decode_frame(raw)) == raw


def test_addr_mapping() -> None:
    assert addr_to_mac(1) == 0x10
    assert addr_to_mac(16) == 0x1F
    # Out-of-range addresses raise the typed WatlowValidationError (not a
    # bare ValueError) so the dispatch / discovery path can catch it as a
    # WatlowError and emit a structured ok=False row instead of aborting.
    with pytest.raises(WatlowValidationError, match="out of range"):
        addr_to_mac(0)
    with pytest.raises(WatlowValidationError, match="out of range"):
        addr_to_mac(17)


# ----- Payload layer -----


def test_encode_read_request_pv() -> None:
    assert encode_read_request(4001).hex() == "010301040101"


def test_encode_read_request_heat_algo() -> None:
    assert encode_read_request(8003).hex() == "010301080301"


def test_encode_write_request_setpoint_392() -> None:
    assert encode_write_request(7001, 392.0).hex() == "01040701010843c40000"


def test_decode_read_pv_response() -> None:
    f = decode_frame(CANON_READ_PV_RSP)
    rsp = decode_payload(f.payload)
    assert isinstance(rsp, ReadResponse)
    assert (rsp.cls, rsp.member, rsp.instance) == (4, 1, 1)
    assert rsp.type_tag == DataType.FLOAT
    assert isinstance(rsp.value, float)
    assert math.isclose(rsp.value, 2531.7783, rel_tol=1e-4)


def test_decode_write_sp_response() -> None:
    f = decode_frame(CANON_WRITE_SP_RSP)
    rsp = decode_payload(f.payload)
    assert isinstance(rsp, WriteResponse)
    assert (rsp.cls, rsp.member, rsp.instance) == (7, 1, 1)
    assert isinstance(rsp.value, float)
    assert math.isclose(rsp.value, 392.0)


def test_decode_read_heat_algo_response() -> None:
    f = decode_frame(CANON_READ_HEAT_RSP)
    rsp = decode_payload(f.payload)
    assert isinstance(rsp, ReadResponse)
    assert (rsp.cls, rsp.member, rsp.instance) == (8, 3, 1)
    assert rsp.type_tag == DataType.PACKED
    assert rsp.value == 71  # PID enumeration in PM manual


# ----- Live-capture-derived type-tag fixtures (PM3, 2026-04-25) -----

LIVE_S32_HW_ID = bytes.fromhex("02 03 01 01 01 01 06 00 00 00 1C")
LIVE_U8_OPS_PAGE = bytes.fromhex("02 03 01 03 02 01 01 02")
LIVE_U16_READ_LOCK = bytes.fromhex("02 03 01 03 0A 01 03 00 05")
LIVE_STRING_PART = bytes.fromhex(
    "02 03 01 01 09 01 09 10 50 4D 33 52 31 43 41 2D 41 41 41 41 41 41 41 00"
)
LIVE_STRING_DEVICE = bytes.fromhex("02 03 01 01 0B 01 09 0B 45 5A 2D 5A 4F 4E 45 20 50 4D 00")
LIVE_U32_TICK = bytes.fromhex("02 03 01 10 06 01 05 FB 9D 48 F7")


def test_decode_s32_hw_id() -> None:
    rsp = decode_payload(LIVE_S32_HW_ID)
    assert isinstance(rsp, ReadResponse)
    assert (rsp.cls, rsp.member, rsp.instance) == (1, 1, 1)
    assert rsp.type_tag == DataType.S32
    assert rsp.value == 28  # ARM CPU


def test_decode_u8_ops_page() -> None:
    rsp = decode_payload(LIVE_U8_OPS_PAGE)
    assert isinstance(rsp, ReadResponse)
    assert rsp.type_tag == DataType.U8
    assert rsp.value == 2


def test_decode_u16_read_lock() -> None:
    rsp = decode_payload(LIVE_U16_READ_LOCK)
    assert isinstance(rsp, ReadResponse)
    assert rsp.type_tag == DataType.U16
    assert rsp.value == 5


def test_decode_string_part_number() -> None:
    rsp = decode_payload(LIVE_STRING_PART)
    assert isinstance(rsp, ReadResponse)
    assert rsp.type_tag == DataType.STRING
    assert rsp.value == "PM3R1CA-AAAAAAA"


def test_decode_string_device_name() -> None:
    rsp = decode_payload(LIVE_STRING_DEVICE)
    assert isinstance(rsp, ReadResponse)
    assert rsp.type_tag == DataType.STRING
    assert rsp.value == "EZ-ZONE PM"


def test_decode_u32_tick_counter() -> None:
    rsp = decode_payload(LIVE_U32_TICK)
    assert isinstance(rsp, ReadResponse)
    assert rsp.type_tag == DataType.U32
    assert rsp.value == 0xFB9D48F7


# ----- Error response payloads (live PM3 capture) -----

LIVE_ERR_NO_OBJECT = bytes.fromhex("02 81")
LIVE_ERR_NO_ATTRIBUTE = bytes.fromhex("02 83")
LIVE_ERR_NO_INSTANCE = bytes.fromhex("02 84")


def test_decode_error_no_object() -> None:
    rsp = decode_payload(LIVE_ERR_NO_OBJECT)
    assert isinstance(rsp, ErrorResponse)
    assert rsp.code == ErrorCode.NO_SUCH_OBJECT


def test_decode_error_no_attribute() -> None:
    rsp = decode_payload(LIVE_ERR_NO_ATTRIBUTE)
    assert isinstance(rsp, ErrorResponse)
    assert rsp.code == ErrorCode.NO_SUCH_ATTRIBUTE


def test_decode_error_no_instance() -> None:
    rsp = decode_payload(LIVE_ERR_NO_INSTANCE)
    assert isinstance(rsp, ErrorResponse)
    assert rsp.code == ErrorCode.NO_SUCH_INSTANCE
