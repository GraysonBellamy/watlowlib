"""Unit tests for the Series SD registry + scaled Modbus read / write path.

Hardware-free: drives the Modbus variants directly against
:data:`SD_PARAMETERS` so the engineering-unit scaling
(:attr:`ParameterSpec.scale`) and the ``S16`` data type are exercised
without a serial port.
"""

from __future__ import annotations

import math

from watlowlib.commands import (
    READ_PARAMETER,
    WRITE_PARAMETER,
    CommandContext,
    ReadParameterRequest,
    WriteParameterRequest,
)
from watlowlib.protocol.modbus import ModbusFn
from watlowlib.protocol.stdbus.tlv import DataType
from watlowlib.registry.parameters import SD_PARAMETERS
from watlowlib.registry.units import UnitKind

CTX = CommandContext(registry=SD_PARAMETERS)


def test_sd_registry_resolves_canonical_and_aliases() -> None:
    for name, pid in (("process_value", 20), ("pv", 20), ("setpoint", 27), ("sp", 27)):
        assert SD_PARAMETERS.resolve(name).parameter_id == pid


def test_sd_process_value_decodes_scaled_float() -> None:
    """PV 68421 raw (S32, ÷1000) → 68.421 °F — the live COM11 value."""
    assert READ_PARAMETER.modbus is not None
    # 68421 == 0x00010B45 → high word 0x0001, low word 0x0B45 (HIGH_LOW).
    entry = READ_PARAMETER.modbus.decode(
        (0x0001, 0x0B45),
        CTX,
        ReadParameterRequest("process_value"),
    )
    assert isinstance(entry.value, float)
    assert math.isclose(entry.value, 68.421)
    assert entry.spec.unit_kind is UnitKind.TEMPERATURE


def test_sd_output_power_is_signed_and_scaled() -> None:
    """Reg 26 is S16 ÷100; -8280 raw → -82.80 %."""
    assert READ_PARAMETER.modbus is not None
    spec = SD_PARAMETERS.resolve("output_power")
    assert spec.data_type is DataType.S16
    entry = READ_PARAMETER.modbus.decode(
        (0xDFA8,),  # -8280 two's complement
        CTX,
        ReadParameterRequest("output_power"),
    )
    assert isinstance(entry.value, float)
    assert math.isclose(entry.value, -82.80)


def test_sd_unscaled_enum_read_preserves_int() -> None:
    """scale==1.0 must NOT promote an integer enum read to float."""
    assert READ_PARAMETER.modbus is not None
    entry = READ_PARAMETER.modbus.decode(
        (1,),
        CTX,
        ReadParameterRequest("auto_manual"),
    )
    assert entry.value == 1
    assert isinstance(entry.value, int)
    assert not isinstance(entry.value, bool)


def test_sd_read_op_uses_single_register_for_s16() -> None:
    assert READ_PARAMETER.modbus is not None
    op = READ_PARAMETER.modbus.encode(CTX, ReadParameterRequest("output_power"))
    assert op.fn is ModbusFn.READ_HOLDING
    assert op.address == 26
    assert op.count == 1


def test_sd_setpoint_write_scales_engineering_units_to_raw() -> None:
    """Writing 62.96 °F → round(62.96 / 0.001) = 62960 raw → S32 words."""
    assert WRITE_PARAMETER.modbus is not None
    op = WRITE_PARAMETER.modbus.encode(CTX, WriteParameterRequest("setpoint", 62.96))
    assert op.fn is ModbusFn.WRITE_REGISTERS
    assert op.address == 27
    # 62960 == 0x0000F5F0 → high word 0x0000, low word 0xF5F0 (HIGH_LOW).
    assert op.values == (0x0000, 0xF5F0)


def test_sd_output_power_write_scales_to_signed_register() -> None:
    """Writing -82.80 % → round(-82.80 / 0.01) = -8280 → single S16 register."""
    assert WRITE_PARAMETER.modbus is not None
    op = WRITE_PARAMETER.modbus.encode(CTX, WriteParameterRequest("output_power", -82.80))
    assert op.fn is ModbusFn.WRITE_REGISTER
    assert op.address == 26
    assert op.values == (0xDFA8,)


def test_sd_write_echo_returns_unscaled_request_value() -> None:
    """The write decode echoes the engineering-unit value, not the raw word."""
    assert WRITE_PARAMETER.modbus is not None
    entry = WRITE_PARAMETER.modbus.decode(
        (),
        CTX,
        WriteParameterRequest("setpoint", 62.96),
    )
    value = entry.value
    assert isinstance(value, int | float)
    assert not isinstance(value, bool)
    assert math.isclose(float(value), 62.96)
