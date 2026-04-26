"""Per-:class:`DataType` Modbus encoding defaults.

The Watlow PM registry stores one ``relative_addr`` per parameter but
does not (today) carry per-row word-order overrides. Watlow's "Modbus
Data Map 1" defaults to **high-word first / big-endian** for FLOAT and
S32/U32 — the values exposed here.

Per-row overrides land via :attr:`ParameterSpec.word_order` when the
JSON registry grows the column. ``word_order=None`` on a spec means
"use the family default", which for PM is :class:`WordOrder.HIGH_LOW`.
"""

from __future__ import annotations

from dataclasses import dataclass

from anymodbus import ByteOrder, WordOrder

from watlowlib.errors import WatlowProtocolUnsupportedError
from watlowlib.protocol.modbus.ops import ModbusFn
from watlowlib.protocol.stdbus.tlv import DataType

__all__ = [
    "ModbusEncoding",
    "encoding_for",
]


@dataclass(frozen=True, slots=True)
class ModbusEncoding:
    """How a :class:`DataType` lays out across Modbus registers.

    Attributes:
        register_count: Number of 16-bit registers the value occupies.
            FLOAT / S32 / U32 → 2 regs; U16 / U8 / PACKED → 1.
            STRING is length-driven and reads its register count from
            the spec.
        word_order: Inter-register order for multi-register values.
            ``HIGH_LOW`` matches PM "Data Map 1" defaults.
        byte_order: Within-register byte order. Big-endian on every
            Watlow family observed to date.
        read_fn: Default Modbus function for a read of this type.
            Always :attr:`ModbusFn.READ_HOLDING` for the PM —
            input-register-only parameters land here when a registry
            row needs the override.
    """

    register_count: int
    word_order: WordOrder
    byte_order: ByteOrder
    read_fn: ModbusFn = ModbusFn.READ_HOLDING


_DEFAULT_BYTE_ORDER = ByteOrder.BIG
_DEFAULT_WORD_ORDER = WordOrder.HIGH_LOW

# PM family defaults. Per design §5: FLOAT → 2 regs / HIGH_LOW / BIG;
# U16 → 1 reg; S32 / U32 → 2 regs / HIGH_LOW; U8 → 1 reg low byte;
# PACKED → 1 reg.
_DEFAULTS: dict[DataType, ModbusEncoding] = {
    DataType.FLOAT: ModbusEncoding(2, _DEFAULT_WORD_ORDER, _DEFAULT_BYTE_ORDER),
    DataType.S32: ModbusEncoding(2, _DEFAULT_WORD_ORDER, _DEFAULT_BYTE_ORDER),
    DataType.U32: ModbusEncoding(2, _DEFAULT_WORD_ORDER, _DEFAULT_BYTE_ORDER),
    DataType.U16: ModbusEncoding(1, _DEFAULT_WORD_ORDER, _DEFAULT_BYTE_ORDER),
    DataType.U8: ModbusEncoding(1, _DEFAULT_WORD_ORDER, _DEFAULT_BYTE_ORDER),
    DataType.PACKED: ModbusEncoding(1, _DEFAULT_WORD_ORDER, _DEFAULT_BYTE_ORDER),
    # STRING uses register_count from the ParameterSpec (length-driven);
    # the family default mirrors the PM3 part-number string at 8 regs.
    DataType.STRING: ModbusEncoding(8, _DEFAULT_WORD_ORDER, _DEFAULT_BYTE_ORDER),
}


def _coerce_word_order(raw: str | None) -> WordOrder | None:
    """Coerce a registry ``word_order`` string to :class:`WordOrder`.

    The registry stores ``word_order`` as a :class:`str` for forward
    compatibility (so loading the JSON does not depend on the
    :mod:`anymodbus` enum). ``None`` → keep the table default.
    Unknown labels raise — better to fail loud at variant time than
    silently fall back.
    """
    if raw is None:
        return None
    try:
        return WordOrder(raw)
    except ValueError as exc:
        msg = f"unknown word_order label: {raw!r}"
        raise WatlowProtocolUnsupportedError(msg) from exc


def encoding_for(
    data_type: DataType,
    *,
    word_order_override: str | None = None,
    register_count_override: int | None = None,
) -> ModbusEncoding:
    """Return the Modbus encoding for ``data_type``.

    Args:
        data_type: The wire data-type tag from the registry.
        word_order_override: Per-row override (from
            :attr:`ParameterSpec.word_order`). ``None`` → use the
            table default.
        register_count_override: Per-row override (mainly for
            :attr:`DataType.STRING`, which is length-driven).

    Raises:
        WatlowProtocolUnsupportedError: ``data_type`` has no Modbus
            mapping yet (e.g. a future tag added to the codec but
            unmapped here).
    """
    base = _DEFAULTS.get(data_type)
    if base is None:
        msg = f"data type {data_type.name} has no Modbus encoding"
        raise WatlowProtocolUnsupportedError(msg)
    word_order = _coerce_word_order(word_order_override) or base.word_order
    register_count = register_count_override or base.register_count
    if word_order is base.word_order and register_count == base.register_count:
        return base
    return ModbusEncoding(
        register_count=register_count,
        word_order=word_order,
        byte_order=base.byte_order,
        read_fn=base.read_fn,
    )
