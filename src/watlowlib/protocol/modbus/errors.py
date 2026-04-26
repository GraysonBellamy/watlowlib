"""Remap :mod:`anymodbus` exceptions to typed :class:`WatlowError`.

The :class:`ModbusProtocolClient` wraps every :mod:`anymodbus` call
site so callers see one error hierarchy regardless of protocol.
``__cause__`` preserves the original exception for callers that need
the upstream type.

Mapping (per ``docs/design.md`` §8 + §5b table):

- ``IllegalFunctionError``        → :class:`WatlowModbusIllegalFunctionError`     → UNSUPPORTED
- ``IllegalDataAddressError``     → :class:`WatlowModbusIllegalDataAddressError`  → UNSUPPORTED
- ``IllegalDataValueError``       → :class:`WatlowModbusIllegalDataValueError`    → unchanged
- ``SlaveDeviceFailureError``     → :class:`WatlowModbusSlaveFailureError`        → unchanged
- ``SlaveDeviceBusyError``        → :class:`WatlowModbusSlaveFailureError`        → unchanged
- ``AcknowledgeError``            → :class:`WatlowModbusSlaveFailureError`        → unchanged
- ``GatewayPath/Target*``         → :class:`WatlowModbusSlaveFailureError`        → unchanged
- ``MemoryParityError``           → :class:`WatlowModbusSlaveFailureError`        → unchanged
- ``FrameTimeoutError``           → :class:`WatlowModbusTimeoutError`             → unchanged
- ``BusClosedError``              → :class:`WatlowConnectionError`                → unchanged
- ``ConnectionLostError``         → :class:`WatlowConnectionError`                → unchanged
- ``CRCError`` / ``FrameError``   → :class:`WatlowFrameError`                     → unchanged
- ``ConfigurationError``          → :class:`WatlowConfigurationError`             → unchanged
- ``UnexpectedResponseError``     → :class:`WatlowProtocolError`                  → unchanged
- ``ModbusUnknownExceptionError`` → :class:`WatlowModbusError`                    → unchanged
- ``ModbusUnsupportedFunctionError`` → :class:`WatlowModbusIllegalFunctionError`  → UNSUPPORTED
- any other ``ModbusError``       → :class:`WatlowModbusError`                    → unchanged
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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
    WatlowError,
    WatlowFrameError,
    WatlowModbusError,
    WatlowModbusIllegalDataAddressError,
    WatlowModbusIllegalDataValueError,
    WatlowModbusIllegalFunctionError,
    WatlowModbusSlaveFailureError,
    WatlowModbusTimeoutError,
    WatlowProtocolError,
)

if TYPE_CHECKING:
    from watlowlib.errors import ErrorContext

__all__ = ["remap_modbus_exception"]


def remap_modbus_exception(
    exc: Exception,
    *,
    context: ErrorContext | None = None,
) -> WatlowError:
    """Wrap ``exc`` in the typed :class:`WatlowError` for its kind.

    The caller is expected to ``raise`` the returned exception with
    ``from exc`` so ``__cause__`` preserves the original.

    Returns ``exc`` re-typed as a :class:`WatlowError` subclass; never
    returns ``None``. Unmapped :mod:`anymodbus` errors fall back to
    :class:`WatlowModbusError`. Non-:mod:`anymodbus` errors fall
    through to a bare :class:`WatlowProtocolError` rather than being
    swallowed — calling sites should normally only feed this function
    instances of :class:`ModbusError`.
    """
    msg = str(exc)
    # Order matters: subclasses before parents. Most exception-response
    # types share a single base (``ModbusExceptionResponse``), so the
    # narrowest class always lands first.
    if isinstance(exc, IllegalFunctionError):
        return WatlowModbusIllegalFunctionError(msg, context=context)
    if isinstance(exc, ModbusUnsupportedFunctionError):
        # Library-side "we don't implement this function code" — same
        # availability outcome as the wire-side IllegalFunction.
        return WatlowModbusIllegalFunctionError(msg, context=context)
    if isinstance(exc, IllegalDataAddressError):
        return WatlowModbusIllegalDataAddressError(msg, context=context)
    if isinstance(exc, IllegalDataValueError):
        return WatlowModbusIllegalDataValueError(msg, context=context)
    if isinstance(
        exc,
        SlaveDeviceFailureError
        | SlaveDeviceBusyError
        | AcknowledgeError
        | GatewayPathUnavailableError
        | GatewayTargetFailedToRespondError
        | MemoryParityError,
    ):
        return WatlowModbusSlaveFailureError(msg, context=context)
    if isinstance(exc, FrameTimeoutError):
        return WatlowModbusTimeoutError(msg, context=context)
    if isinstance(exc, BusClosedError | ConnectionLostError):
        return WatlowConnectionError(msg, context=context)
    if isinstance(exc, CRCError | FrameError):
        return WatlowFrameError(msg, context=context)
    if isinstance(exc, ConfigurationError):
        return WatlowConfigurationError(msg, context=context)
    if isinstance(exc, UnexpectedResponseError):
        return WatlowProtocolError(msg, context=context)
    if isinstance(exc, ModbusUnknownExceptionError | ModbusError):
        return WatlowModbusError(msg, context=context)
    return WatlowProtocolError(msg, context=context)
