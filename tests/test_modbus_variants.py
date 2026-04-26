"""Unit tests for the Modbus :class:`ReadParameter` / :class:`WriteParameter` variants."""

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
from watlowlib.errors import WatlowProtocolUnsupportedError
from watlowlib.protocol.modbus import ModbusFn

CTX = CommandContext(registry=PARAMETERS)


def test_read_parameter_modbus_encode_setpoint() -> None:
    assert READ_PARAMETER.modbus is not None
    op = READ_PARAMETER.modbus.encode(CTX, ReadParameterRequest("setpoint"))
    spec = PARAMETERS.resolve("setpoint")
    assert op.fn is ModbusFn.READ_HOLDING
    assert op.address == spec.relative_addr
    # FLOAT → 2 registers.
    assert op.count == 2
    assert op.values is None


def test_read_parameter_modbus_encode_pv() -> None:
    assert READ_PARAMETER.modbus is not None
    op = READ_PARAMETER.modbus.encode(CTX, ReadParameterRequest("pv"))
    spec = PARAMETERS.resolve("process_value")
    assert op.fn is ModbusFn.READ_HOLDING
    assert op.address == spec.relative_addr
    assert op.count == 2


def test_read_parameter_modbus_decode_setpoint() -> None:
    assert READ_PARAMETER.modbus is not None
    # 392.0 → 0x43C40000 → (0x43C4, 0x0000) under HIGH_LOW.
    entry = READ_PARAMETER.modbus.decode(
        (0x43C4, 0x0000),
        CTX,
        ReadParameterRequest("setpoint"),
    )
    assert entry.spec.parameter_id == 7001
    assert entry.instance == 1
    assert isinstance(entry.value, float)
    assert math.isclose(entry.value, 392.0)
    # raw bytes follow the wire word order (high word first, big-endian).
    assert entry.raw == bytes.fromhex("43c40000")


def test_write_parameter_modbus_encode_setpoint() -> None:
    assert WRITE_PARAMETER.modbus is not None
    op = WRITE_PARAMETER.modbus.encode(CTX, WriteParameterRequest("setpoint", 392.0))
    spec = PARAMETERS.resolve("setpoint")
    assert op.fn is ModbusFn.WRITE_REGISTERS
    assert op.address == spec.relative_addr
    assert op.count == 2
    assert op.values == (0x43C4, 0x0000)


def test_write_parameter_modbus_decode_returns_request_value() -> None:
    assert WRITE_PARAMETER.modbus is not None
    entry = WRITE_PARAMETER.modbus.decode(
        (),  # writes return no words
        CTX,
        WriteParameterRequest("setpoint", 392.0),
    )
    assert entry.spec.parameter_id == 7001
    assert entry.value == 392.0
    assert entry.instance == 1
    assert entry.raw == b""


def test_modbus_variant_rejects_multi_loop_for_now() -> None:
    """Guard against silent wrong-register reads on multi-loop."""
    assert READ_PARAMETER.modbus is not None
    spec = PARAMETERS.resolve("setpoint")
    if spec.max_instance < 2:
        pytest.skip("setpoint is single-instance on PM single-loop registry")
    with pytest.raises(WatlowProtocolUnsupportedError, match="not implemented"):
        READ_PARAMETER.modbus.encode(CTX, ReadParameterRequest("setpoint", instance=2))
