"""BACnet MS/TP outer frame for Watlow Standard Bus.

A Standard Bus serial frame is::

    +---------+-----+-----+-----+--------+------+----------+----------+
    | 55 FF   | FT  | DST | SRC | LEN_BE | HCRC | PAYLOAD  | DCRC_LE  |
    +---------+-----+-----+-----+--------+------+----------+----------+
                BACnet MS/TP outer frame                Watlow inner

- ``FT`` is the frame type — only ``0x05`` (request) and ``0x06``
  (response) are honoured by EZ-ZONE PM controllers in our captures.
- ``DST`` and ``SRC`` are MS/TP MAC addresses. Host is ``0x00``;
  controllers occupy ``0x10..0x1F`` for Standard Bus addresses
  ``1..16`` (see :func:`addr_to_mac`).
- ``LEN`` is the 16-bit big-endian payload length.
- ``HCRC`` is :func:`watlowlib.protocol.stdbus._crc.header_crc8` over
  ``{FT, DST, SRC, LEN_HI, LEN_LO}``.
- ``DCRC`` is :func:`watlowlib.protocol.stdbus._crc.data_crc16`
  over ``PAYLOAD``, transmitted little-endian.
"""

from __future__ import annotations

from dataclasses import dataclass

from watlowlib.protocol.stdbus._crc import data_crc16_le_bytes, header_crc8
from watlowlib.protocol.stdbus.tables import (
    ADDR_OFFSET,
    HOST_MAC,
    FrameType,
    addr_to_mac,
    mac_to_addr,
)

PREAMBLE = b"\x55\xff"


@dataclass(frozen=True, slots=True)
class Frame:
    """Decoded BACnet MS/TP frame as seen on Standard Bus."""

    frame_type: int
    dst: int
    src: int
    payload: bytes


class FrameError(ValueError):
    """A wire frame failed structural or CRC validation."""


def encode_frame(frame: Frame) -> bytes:
    """Serialise ``frame`` to wire bytes (preamble through data CRC).

    Raises:
        FrameError: ``frame.payload`` exceeds the 16-bit length field.
    """
    if len(frame.payload) > 0xFFFF:
        raise FrameError(f"payload too long: {len(frame.payload)}")
    header = bytes(
        [
            frame.frame_type & 0xFF,
            frame.dst & 0xFF,
            frame.src & 0xFF,
            (len(frame.payload) >> 8) & 0xFF,
            len(frame.payload) & 0xFF,
        ]
    )
    hcrc = bytes([header_crc8(header)])
    if frame.payload:
        return PREAMBLE + header + hcrc + frame.payload + data_crc16_le_bytes(frame.payload)
    return PREAMBLE + header + hcrc


def decode_frame(buf: bytes) -> Frame:
    """Parse wire bytes into a :class:`Frame`, verifying both CRCs.

    The caller is expected to have already framed on the ``55 FF``
    preamble — :class:`watlowlib.protocol.stdbus.client.StdBusProtocolClient`
    handles that during read.

    Raises:
        FrameError: short buffer, wrong preamble, header CRC mismatch,
            truncated body, or data CRC mismatch.
    """
    if len(buf) < 8:
        raise FrameError(f"frame too short: {len(buf)} bytes")
    if buf[:2] != PREAMBLE:
        raise FrameError(f"bad preamble: {buf[:2].hex()}")
    frame_type = buf[2]
    dst = buf[3]
    src = buf[4]
    plen = (buf[5] << 8) | buf[6]
    hcrc = buf[7]
    expected_hcrc = header_crc8(buf[2:7])
    if hcrc != expected_hcrc:
        raise FrameError(f"header CRC mismatch: got 0x{hcrc:02X}, want 0x{expected_hcrc:02X}")
    if plen == 0:
        return Frame(frame_type, dst, src, b"")
    expected_total = 8 + plen + 2
    if len(buf) < expected_total:
        raise FrameError(f"frame truncated: have {len(buf)}, need {expected_total}")
    payload = buf[8 : 8 + plen]
    dcrc = buf[8 + plen : 8 + plen + 2]
    expected_dcrc = data_crc16_le_bytes(payload)
    if dcrc != expected_dcrc:
        raise FrameError(f"data CRC mismatch: got {dcrc.hex()}, want {expected_dcrc.hex()}")
    return Frame(frame_type, dst, src, payload)


__all__ = [
    "ADDR_OFFSET",
    "HOST_MAC",
    "PREAMBLE",
    "Frame",
    "FrameError",
    "FrameType",
    "addr_to_mac",
    "decode_frame",
    "encode_frame",
    "mac_to_addr",
]
