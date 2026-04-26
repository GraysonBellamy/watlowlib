"""Command-variant unit tests — pure encode/decode, no transport."""

from __future__ import annotations

import math

import pytest

from watlowlib import PARAMETERS
from watlowlib.commands import (
    READ_PARAMETER,
    WRITE_PARAMETER,
    CommandContext,
    ReadParameterRequest,
    WriteParameterRequest,
)
from watlowlib.errors import (
    WatlowNoSuchAttributeError,
    WatlowNoSuchObjectError,
    WatlowValidationError,
)
from watlowlib.protocol.stdbus import (
    Frame,
    decode_frame,
    decode_payload,
)
from watlowlib.protocol.stdbus.types import StdBusReply

CTX = CommandContext(registry=PARAMETERS)

# Captured PM3 round-trips lifted from tests/test_codec.py — same wire
# bytes, exercised through the new variant layer.
CANON_READ_PV_RSP = bytes.fromhex("55 FF 06 00 10 00 0B 88 02 03 01 04 01 01 08 45 1E 3C D4 A7 28")
CANON_WRITE_SP_RSP = bytes.fromhex("55 FF 06 00 10 00 0A 76 02 04 07 01 01 08 43 C4 00 00 82 03")


def _reply(raw: bytes) -> StdBusReply:
    frame = decode_frame(raw)
    return StdBusReply(frame=frame, payload=decode_payload(frame.payload), raw_frame=raw)


def test_read_parameter_encode_pv() -> None:
    assert READ_PARAMETER.stdbus is not None
    payload = READ_PARAMETER.stdbus.encode(CTX, ReadParameterRequest("pv"))
    assert payload.hex() == "010301040101"


def test_read_parameter_encode_setpoint_instance_2() -> None:
    assert READ_PARAMETER.stdbus is not None
    payload = READ_PARAMETER.stdbus.encode(CTX, ReadParameterRequest("setpoint", instance=2))
    # cls=7 member=1 instance=2
    assert payload.hex() == "010301070102"


def test_read_parameter_decode_pv_response() -> None:
    assert READ_PARAMETER.stdbus is not None
    entry = READ_PARAMETER.stdbus.decode(_reply(CANON_READ_PV_RSP), CTX)
    assert entry.spec.parameter_id == 4001
    assert entry.spec.name == "process_value"
    assert entry.instance == 1
    assert isinstance(entry.value, float)
    assert isinstance(entry.value, float)
    assert math.isclose(entry.value, 2531.7783, rel_tol=1e-4)


def test_write_parameter_encode_setpoint() -> None:
    assert WRITE_PARAMETER.stdbus is not None
    payload = WRITE_PARAMETER.stdbus.encode(CTX, WriteParameterRequest("setpoint", 392.0))
    assert payload.hex() == "01040701010843c40000"


def test_write_parameter_decode_echo() -> None:
    assert WRITE_PARAMETER.stdbus is not None
    entry = WRITE_PARAMETER.stdbus.decode(_reply(CANON_WRITE_SP_RSP), CTX)
    assert entry.spec.parameter_id == 7001
    assert isinstance(entry.value, float)
    assert math.isclose(entry.value, 392.0)


def test_read_parameter_decode_no_such_object() -> None:
    # 0x81 error response wrapped in a frame.
    inner = bytes.fromhex("02 81")
    frame = Frame(frame_type=0x06, dst=0x00, src=0x10, payload=inner)
    from watlowlib.protocol.stdbus.framing import encode_frame

    raw = encode_frame(frame)
    reply = _reply(raw)
    assert READ_PARAMETER.stdbus is not None
    with pytest.raises(WatlowNoSuchObjectError):
        READ_PARAMETER.stdbus.decode(reply, CTX)


def test_read_parameter_decode_no_such_attribute() -> None:
    inner = bytes.fromhex("02 83")
    from watlowlib.protocol.stdbus.framing import encode_frame

    raw = encode_frame(Frame(frame_type=0x06, dst=0x00, src=0x10, payload=inner))
    reply = _reply(raw)
    assert READ_PARAMETER.stdbus is not None
    with pytest.raises(WatlowNoSuchAttributeError):
        READ_PARAMETER.stdbus.decode(reply, CTX)


def test_write_parameter_validates_instance() -> None:
    assert WRITE_PARAMETER.stdbus is not None
    with pytest.raises(WatlowValidationError, match="instance"):
        WRITE_PARAMETER.stdbus.encode(CTX, WriteParameterRequest("setpoint", 100.0, instance=99))


def test_read_parameter_validates_unknown_name() -> None:
    assert READ_PARAMETER.stdbus is not None
    with pytest.raises(WatlowValidationError, match="unknown parameter"):
        READ_PARAMETER.stdbus.encode(CTX, ReadParameterRequest("not_a_real_param"))


def test_write_string_parameter_rejects_numeric() -> None:
    assert WRITE_PARAMETER.stdbus is not None
    # "device_name" is the writable Short String parameter (1011, R/W).
    with pytest.raises(WatlowValidationError, match="string"):
        WRITE_PARAMETER.stdbus.encode(CTX, WriteParameterRequest("device_name", 42))
