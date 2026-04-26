"""Unit tests for the :mod:`anymodbus` exception remap."""

from __future__ import annotations

import pytest
from anymodbus import (
    AcknowledgeError,
    BusClosedError,
    ConfigurationError,
    ConnectionLostError,
    CRCError,
    FrameError,
    FrameTimeoutError,
    GatewayPathUnavailableError,
    GatewayTargetFailedToRespondError,
    IllegalDataAddressError,
    IllegalDataValueError,
    IllegalFunctionError,
    MemoryParityError,
    ModbusError,
    ModbusUnknownExceptionError,
    ModbusUnsupportedFunctionError,
    SlaveDeviceBusyError,
    SlaveDeviceFailureError,
    UnexpectedResponseError,
)

from watlowlib.errors import (
    WatlowConfigurationError,
    WatlowConnectionError,
    WatlowFrameError,
    WatlowModbusError,
    WatlowModbusIllegalDataAddressError,
    WatlowModbusIllegalDataValueError,
    WatlowModbusIllegalFunctionError,
    WatlowModbusSlaveFailureError,
    WatlowModbusTimeoutError,
    WatlowProtocolError,
    WatlowProtocolUnsupportedError,
    WatlowTimeoutError,
)
from watlowlib.protocol.modbus.errors import remap_modbus_exception

# anymodbus splits exception constructors into two camps:
# - ``ModbusExceptionResponse`` subclasses take ``function_code`` (kwarg),
# - other ``ModbusError`` subclasses take a positional message.
# This helper picks the right shape per class so the parametrized
# remap test doesn't have to know.
_KEYWORD_FN_CODE: tuple[type[Exception], ...] = (
    AcknowledgeError,
    GatewayPathUnavailableError,
    GatewayTargetFailedToRespondError,
    IllegalDataAddressError,
    IllegalDataValueError,
    IllegalFunctionError,
    MemoryParityError,
    SlaveDeviceBusyError,
    SlaveDeviceFailureError,
)


def _make(cls: type[Exception]) -> Exception:
    if cls is ModbusUnknownExceptionError:
        return cls(function_code=3, exception_code=99)
    if issubclass(cls, _KEYWORD_FN_CODE):
        return cls(function_code=3)  # type: ignore[call-arg]
    return cls("boom")


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (IllegalFunctionError, WatlowModbusIllegalFunctionError),
        (ModbusUnsupportedFunctionError, WatlowModbusIllegalFunctionError),
        (IllegalDataAddressError, WatlowModbusIllegalDataAddressError),
        (IllegalDataValueError, WatlowModbusIllegalDataValueError),
        (SlaveDeviceFailureError, WatlowModbusSlaveFailureError),
        (SlaveDeviceBusyError, WatlowModbusSlaveFailureError),
        (AcknowledgeError, WatlowModbusSlaveFailureError),
        (GatewayPathUnavailableError, WatlowModbusSlaveFailureError),
        (GatewayTargetFailedToRespondError, WatlowModbusSlaveFailureError),
        (MemoryParityError, WatlowModbusSlaveFailureError),
        (FrameTimeoutError, WatlowModbusTimeoutError),
        (BusClosedError, WatlowConnectionError),
        (ConnectionLostError, WatlowConnectionError),
        (CRCError, WatlowFrameError),
        (FrameError, WatlowFrameError),
        (ConfigurationError, WatlowConfigurationError),
        (UnexpectedResponseError, WatlowProtocolError),
        (ModbusUnknownExceptionError, WatlowModbusError),
    ],
)
def test_remap_known_exception(source: type[Exception], expected: type) -> None:
    exc = _make(source)
    out = remap_modbus_exception(exc)
    assert isinstance(out, expected)


def test_illegal_function_is_protocol_unsupported() -> None:
    out = remap_modbus_exception(IllegalFunctionError(function_code=3))
    # The session relies on this hierarchy to flip Availability.UNSUPPORTED.
    assert isinstance(out, WatlowProtocolUnsupportedError)


def test_illegal_data_address_is_protocol_unsupported() -> None:
    out = remap_modbus_exception(IllegalDataAddressError(function_code=3))
    assert isinstance(out, WatlowProtocolUnsupportedError)


def test_frame_timeout_is_watlow_timeout() -> None:
    out = remap_modbus_exception(FrameTimeoutError("slow"))
    assert isinstance(out, WatlowTimeoutError)


def test_unmapped_modbus_error_falls_back_to_modbus_base() -> None:
    # ModbusError on its own (no specific subclass) should still wrap.
    out = remap_modbus_exception(ModbusError("generic"))
    assert isinstance(out, WatlowModbusError)


def test_non_modbus_exception_falls_back_to_protocol_error() -> None:
    out = remap_modbus_exception(ValueError("not modbus"))
    assert isinstance(out, WatlowProtocolError)
    assert not isinstance(out, WatlowModbusError)
