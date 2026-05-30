"""Type-tag value codec for the Watlow attribute service.

The inner payload that follows the request/response selector is a
tagged value: one tag byte, optionally a length byte, then data. The
codec resolves both fixed-width tags (``U8`` / ``U16`` / ``U32`` /
``S32`` / ``FLOAT``) and length-prefixed tags (``STRING`` / ``PACKED``).

Confirmed against a live EZ-ZONE PM3 (2026-04-25); every tag has at
least one captured fixture in [tests/test_codec.py](tests/test_codec.py).
"""

from __future__ import annotations

import struct
from enum import IntEnum

__all__ = [
    "DataType",
    "decode_value",
    "encode_value",
]


class DataType(IntEnum):
    """Wire data-type tag bytes.

    Tags fall into two families:

    - **fixed-width**: ``U8``, ``U16``, ``U32``, ``S32``, ``FLOAT`` —
      the value follows the tag with no length byte.
    - **length-prefixed**: ``STRING``, ``PACKED`` — a length byte
      follows the tag.

    The "Wide Enumeration" data type from the EZ-ZONE register list
    shares tag ``0x0F`` with ``Enumeration``. In every live capture so
    far the count byte is ``1`` (single 16-bit word). Multi-word
    behaviour is implemented but unverified.

    ``S16`` (signed 16-bit, one register) is **not** a real Std Bus
    wire tag — no Std Bus row maps to it, and the Std Bus
    :func:`encode_value` / :func:`decode_value` switches raise on its
    tag. It exists purely as a registry :class:`DataType` for Modbus
    families (the Series SD stores signed 16-bit power / percent
    values) and is given a deliberately out-of-band value (``0x86``)
    so it can never collide with an on-the-wire Std Bus tag.
    """

    U8 = 0x01  # 1-byte unsigned (no length byte)
    U16 = 0x03  # 2-byte BE unsigned (no length byte)
    U32 = 0x05  # 4-byte BE unsigned (no length byte)
    S32 = 0x06  # 4-byte BE signed (no length byte)
    FLOAT = 0x08  # 4-byte BE IEEE-754 (no length byte)
    STRING = 0x09  # length byte + ASCII (commonly NUL-terminated within length)
    PACKED = 0x0F  # count byte (= 16-bit words) + count*2 bytes BE
    S16 = 0x86  # signed 16-bit (1 register); Modbus-only — not a Std Bus wire tag


def encode_value(type_tag: int, value: float | int | str | bytes) -> bytes:
    """Encode ``value`` under ``type_tag`` to wire bytes (tag included)."""
    if type_tag == DataType.STRING:
        # STRING is the only branch that accepts a str/bytes payload;
        # the numeric branches all coerce via int()/float() and would
        # silently produce zero-filled buffers if a str slipped through.
        if not isinstance(value, str | bytes):
            raise TypeError(f"STRING type tag requires str or bytes, got {type(value).__name__}")
        b = value.encode("ascii") if isinstance(value, str) else bytes(value)
        if len(b) > 0xFF:
            raise ValueError(f"string too long: {len(b)}")
        return bytes([DataType.STRING, len(b)]) + b
    # Numeric branches: reject str/bytes early with a clear message.
    if isinstance(value, str | bytes):
        raise TypeError(
            f"type tag 0x{type_tag:02X} requires a numeric value, got {type(value).__name__}"
        )
    if type_tag == DataType.FLOAT:
        return bytes([DataType.FLOAT]) + struct.pack(">f", float(value))
    if type_tag == DataType.U8:
        if not 0 <= int(value) <= 0xFF:
            raise ValueError(f"u8 value out of range: {value}")
        return bytes([DataType.U8, int(value)])
    if type_tag == DataType.U16:
        if not 0 <= int(value) <= 0xFFFF:
            raise ValueError(f"u16 value out of range: {value}")
        return bytes([DataType.U16]) + struct.pack(">H", int(value))
    if type_tag == DataType.U32:
        if not 0 <= int(value) <= 0xFFFFFFFF:
            raise ValueError(f"u32 value out of range: {value}")
        return bytes([DataType.U32]) + struct.pack(">I", int(value))
    if type_tag == DataType.S32:
        if not -(2**31) <= int(value) <= 2**31 - 1:
            raise ValueError(f"s32 value out of range: {value}")
        return bytes([DataType.S32]) + struct.pack(">i", int(value))
    if type_tag == DataType.PACKED:
        if not 0 <= int(value) <= 0xFFFF:
            raise ValueError(f"packed-int(1) value out of range: {value}")
        return bytes([DataType.PACKED, 0x01]) + struct.pack(">H", int(value))
    raise ValueError(f"unsupported type tag for encode: 0x{type_tag:02X}")


def decode_value(buf: bytes) -> tuple[float | int | str, int, int]:
    """Decode a single value starting at ``buf[0]``.

    Returns ``(value, type_tag, consumed_bytes)``.
    """
    if not buf:
        raise ValueError("empty value buffer")
    tag = buf[0]
    if tag == DataType.U8:
        if len(buf) < 2:
            raise ValueError("truncated u8 value")
        return buf[1], tag, 2
    if tag == DataType.U16:
        if len(buf) < 3:
            raise ValueError("truncated u16 value")
        (v_u16,) = struct.unpack(">H", buf[1:3])
        return v_u16, tag, 3
    if tag == DataType.U32:
        if len(buf) < 5:
            raise ValueError("truncated u32 value")
        (v_u32,) = struct.unpack(">I", buf[1:5])
        return v_u32, tag, 5
    if tag == DataType.S32:
        if len(buf) < 5:
            raise ValueError("truncated s32 value")
        (v_s32,) = struct.unpack(">i", buf[1:5])
        return v_s32, tag, 5
    if tag == DataType.FLOAT:
        if len(buf) < 5:
            raise ValueError("truncated float value")
        (v_f,) = struct.unpack(">f", buf[1:5])
        return v_f, tag, 5
    if tag == DataType.STRING:
        if len(buf) < 2:
            raise ValueError("truncated string header")
        n = buf[1]
        if len(buf) < 2 + n:
            raise ValueError("truncated string body")
        raw = bytes(buf[2 : 2 + n])
        # Trim trailing NULs for convenience but keep the raw length
        # intact in caller paths.
        s = raw.rstrip(b"\x00").decode("ascii", errors="replace")
        return s, tag, 2 + n
    if tag == DataType.PACKED:
        if len(buf) < 2:
            raise ValueError("truncated packed-int header")
        n = buf[1]  # count of 16-bit words
        if len(buf) < 2 + 2 * n:
            raise ValueError("truncated packed-int body")
        # PACKED with count=1 is the canonical enum / 16-bit case
        # (the most common shape on the wire).
        if n == 1:
            (v_packed,) = struct.unpack(">H", buf[2:4])
            return v_packed, tag, 4
        # count>=2: pack words MSW-first into a single integer
        # (provisional; only confirmed once we observe a real Wide
        # Enum that exceeds 16 bits).
        v_wide = 0
        for i in range(n):
            (w,) = struct.unpack(">H", buf[2 + 2 * i : 4 + 2 * i])
            v_wide = (v_wide << 16) | w
        return v_wide, tag, 2 + 2 * n
    raise ValueError(f"unknown type tag: 0x{tag:02X} (rest: {buf[:8].hex()})")
