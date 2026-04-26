"""Typed Modbus instruction emitted by :class:`ModbusVariant`.

A :class:`ModbusOp` is a small, protocol-neutral description of one
Modbus transaction: ``read`` or ``write``, holding or input register,
starting address, count, and (for writes) the register words to
transmit. The :class:`ModbusProtocolClient` lowers it onto the
matching :class:`anymodbus.Slave` method.

Variants emit ``ModbusOp`` rather than wire bytes because
:mod:`anymodbus` already owns the PDU codec — handing it bytes would
be a layer violation (see ``docs/design.md`` §5).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

__all__ = ["ModbusFn", "ModbusOp"]


class ModbusFn(StrEnum):
    """Modbus function selector.

    The four operations exercised by the registry-driven workhorse
    (:data:`READ_PARAMETER` / :data:`WRITE_PARAMETER`). Coil and
    discrete-input ops are intentionally absent; they would be added
    if a registry parameter ever needed them.
    """

    READ_HOLDING = "read_holding"
    READ_INPUT = "read_input"
    WRITE_REGISTER = "write_register"
    WRITE_REGISTERS = "write_registers"


@dataclass(frozen=True, slots=True)
class ModbusOp:
    """One Modbus transaction in protocol-neutral form.

    Attributes:
        fn: The Modbus function to invoke.
        address: Zero-based register address. The Watlow registry
            stores this as ``relative_addr``; the legacy 4xxxxx /
            3xxxxx notation lives only in :attr:`ParameterSpec.absolute_addr`.
        count: Number of 16-bit registers to read. Ignored on
            single-register writes; required on
            :attr:`ModbusFn.WRITE_REGISTERS` so the client can
            pre-validate ``len(values) == count``.
        values: Register words to write (one ``int`` per 16-bit
            register), or ``None`` for read ops. Single-register
            writes use ``values=(word,)``.
    """

    fn: ModbusFn
    address: int
    count: int = 1
    values: tuple[int, ...] | None = None

    def __post_init__(self) -> None:
        # The Modbus address space is 16-bit. An out-of-range address
        # is a programming error — fail fast at construction so the
        # blame lands on the variant that produced the op.
        if self.address < 0 or self.address > 0xFFFF:
            msg = f"register address {self.address} out of range (0..0xFFFF)"
            raise ValueError(msg)
        # Per-FC count limits (Modbus Application Protocol §6):
        # FC03 read_holding_registers / FC04 read_input_registers cap
        # at 125 (PDU width); FC16 write_multiple_registers caps at
        # 123; FC06 write_register is exactly 1 register.
        if self.fn in (ModbusFn.READ_HOLDING, ModbusFn.READ_INPUT):
            if self.count < 1 or self.count > 125:
                msg = f"{self.fn.value} count {self.count} out of range (1..125)"
                raise ValueError(msg)
        elif self.fn is ModbusFn.WRITE_REGISTERS:
            if self.count < 1 or self.count > 123:
                msg = f"{self.fn.value} count {self.count} out of range (1..123)"
                raise ValueError(msg)
        elif self.fn is ModbusFn.WRITE_REGISTER and self.count != 1:
            msg = f"{self.fn.value} expects count=1, got {self.count}"
            raise ValueError(msg)
        if self.fn in (ModbusFn.WRITE_REGISTER, ModbusFn.WRITE_REGISTERS):
            if self.values is None:
                msg = f"{self.fn.value} requires non-None values"
                raise ValueError(msg)
            if self.fn is ModbusFn.WRITE_REGISTER and len(self.values) != 1:
                msg = f"write_register expects exactly one value, got {len(self.values)}"
                raise ValueError(msg)
            if self.fn is ModbusFn.WRITE_REGISTERS and len(self.values) != self.count:
                msg = f"write_registers values length {len(self.values)} != count {self.count}"
                raise ValueError(msg)
        elif self.values is not None:
            msg = f"{self.fn.value} does not accept values"
            raise ValueError(msg)
