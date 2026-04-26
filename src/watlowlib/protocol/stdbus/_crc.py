"""BACnet MS/TP CRC algorithms (ASHRAE 135 Clause 9).

Standard Bus serial frames carry a one-byte header CRC over
``{frame_type, dst, src, len_hi, len_lo}`` and a two-byte data CRC over
the payload bytes (little-endian on the wire).
"""

from __future__ import annotations


def header_crc8(data: bytes) -> int:
    """Compute the BACnet MS/TP header CRC-8.

    Returns the on-wire byte (one's complement of the running CRC).
    """
    crc = 0xFF
    for b in data:
        x = crc ^ b
        x = x ^ (x << 1) ^ (x << 2) ^ (x << 3) ^ (x << 4) ^ (x << 5) ^ (x << 6) ^ (x << 7)
        crc = ((x & 0xFE) ^ ((x >> 8) & 0x01)) & 0xFF
    return (~crc) & 0xFF


def data_crc16(data: bytes) -> int:
    """Compute the BACnet MS/TP data CRC-16.

    Returns the host-order int; on the wire, send little-endian via
    :func:`data_crc16_le_bytes`.
    """
    crc = 0xFFFF
    for b in data:
        low = (crc & 0xFF) ^ b
        crc = (
            (crc >> 8)
            ^ (low << 8)
            ^ (low << 3)
            ^ (low << 12)
            ^ (low >> 4)
            ^ (low & 0x0F)
            ^ ((low & 0x0F) << 7)
        ) & 0xFFFF
    return (~crc) & 0xFFFF


def data_crc16_le_bytes(data: bytes) -> bytes:
    """Encode the data CRC as the two on-wire bytes (little-endian)."""
    return data_crc16(data).to_bytes(2, "little")


__all__ = ["data_crc16", "data_crc16_le_bytes", "header_crc8"]
