"""Pack / unpack Modbus 16-bit register words.

The :class:`ModbusProtocolClient` reads and writes raw register
tuples — variants are responsible for converting to and from typed
Python values.

Heavy lifting lives in :mod:`anymodbus.decoders`; this module is a
thin adapter that maps Watlow's :class:`DataType` tags onto
:class:`anymodbus.RegisterType` and preserves PM-specific quirks
(``U8`` is low-byte-only on the wire; ``PACKED`` and ``U16`` wrap on
overflow rather than range-failing).
"""

from __future__ import annotations

from anymodbus import ByteOrder, RegisterType, WordOrder, decode, encode

from watlowlib.errors import ErrorContext, WatlowProtocolError
from watlowlib.protocol.base import ProtocolKind
from watlowlib.protocol.stdbus.tlv import DataType

__all__ = [
    "decode_words",
    "encode_value_to_words",
]

# Watlow ``DataType`` → anymodbus ``RegisterType`` plus the expected
# register count. ``U8`` and ``STRING`` have Watlow-specific behaviour
# and are handled inline.
_REGISTER_TYPE: dict[DataType, tuple[RegisterType, int]] = {
    DataType.FLOAT: (RegisterType.FLOAT32, 2),
    DataType.S32: (RegisterType.INT32, 2),
    DataType.U32: (RegisterType.UINT32, 2),
    DataType.U16: (RegisterType.UINT16, 1),
    DataType.PACKED: (RegisterType.UINT16, 1),
}


def _ctx() -> ErrorContext:
    """Build a minimal context for codec-layer protocol errors.

    The codec doesn't see command_name / port / address — those are
    populated when the variant layer wraps the codec failure. Carrying
    the protocol field lets callers filter on Modbus-side decode
    errors without re-parsing the message.
    """
    return ErrorContext(protocol=ProtocolKind.MODBUS_RTU)


def _check_count(words_len: int, expected: int, type_name: str) -> None:
    if words_len != expected:
        plural = "s" if expected != 1 else ""
        msg = f"{type_name} expects {expected} register{plural}, got {words_len}"
        raise WatlowProtocolError(msg, context=_ctx())


def decode_words(
    words: tuple[int, ...],
    *,
    data_type: DataType,
    word_order: WordOrder,
    byte_order: ByteOrder,
) -> float | int | str:
    """Decode ``words`` into a typed Python value per ``data_type``.

    Raises:
        WatlowProtocolError: ``words`` is the wrong length for
            ``data_type``, or the underlying decode fails.
    """
    if data_type is DataType.U8:
        # PM packs U8 in the low byte of a single register; mask off
        # any garbage in the high byte rather than letting it through.
        _check_count(len(words), 1, "U8")
        return int(words[0]) & 0xFF
    if data_type is DataType.STRING:
        return decode(
            words,
            type=RegisterType.STRING,
            word_order=word_order,
            byte_order=byte_order,
        )
    mapping = _REGISTER_TYPE.get(data_type)
    if mapping is None:
        msg = f"data type {data_type.name} has no Modbus decode rule"
        raise WatlowProtocolError(msg, context=_ctx())
    register_type, expected = mapping
    _check_count(len(words), expected, data_type.name)
    return decode(
        words,
        type=register_type,
        word_order=word_order,
        byte_order=byte_order,
    )


def encode_value_to_words(
    value: float | int | str | bytes,
    *,
    data_type: DataType,
    register_count: int,
    word_order: WordOrder,
    byte_order: ByteOrder,
) -> tuple[int, ...]:
    """Encode ``value`` to a tuple of 16-bit register words.

    ``register_count`` is taken from the spec / encoding so STRING
    can pad / truncate to the device's expected width.

    Raises:
        WatlowProtocolError: ``value`` cannot be packed for ``data_type``
            (e.g. STRING longer than ``register_count * 2``).
    """
    if data_type is DataType.U8:
        if not isinstance(value, int | float):
            msg = f"U8 requires a numeric value, got {type(value).__name__}"
            raise WatlowProtocolError(msg, context=_ctx())
        return (int(value) & 0xFF,)
    if data_type is DataType.STRING:
        if isinstance(value, bytes):
            try:
                text = value.decode("ascii")
            except UnicodeDecodeError as exc:
                raise WatlowProtocolError(str(exc), context=_ctx()) from exc
        elif isinstance(value, str):
            text = value
        else:
            msg = f"STRING requires str or bytes, got {type(value).__name__}"
            raise WatlowProtocolError(msg, context=_ctx())
        capacity = register_count * 2
        raw_len = len(text.encode("ascii"))
        if raw_len > capacity:
            msg = f"STRING value of {raw_len} bytes exceeds register capacity {capacity}"
            raise WatlowProtocolError(msg, context=_ctx())
        return encode(
            text,
            type=RegisterType.STRING,
            register_count=register_count,
            word_order=word_order,
            byte_order=byte_order,
        )
    mapping = _REGISTER_TYPE.get(data_type)
    if mapping is None:
        msg = f"data type {data_type.name} has no Modbus encode rule"
        raise WatlowProtocolError(msg)
    register_type, _expected = mapping
    if not isinstance(value, int | float):
        msg = f"{data_type.name} requires a numeric value, got {type(value).__name__}"
        raise WatlowProtocolError(msg, context=_ctx())
    # ``U16`` and ``PACKED`` ride on a UINT16 register; the legacy
    # codec masked with ``& 0xFFFF`` rather than rejecting overflow,
    # so preserve that wrap-on-overflow behaviour.
    if data_type is DataType.U16 or data_type is DataType.PACKED:
        return (int(value) & 0xFFFF,)
    numeric: int | float = float(value) if register_type is RegisterType.FLOAT32 else int(value)
    return encode(
        numeric,
        type=register_type,
        word_order=word_order,
        byte_order=byte_order,
    )
