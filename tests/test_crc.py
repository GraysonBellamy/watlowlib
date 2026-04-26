"""BACnet MS/TP CRCs verified against canonical sample frames.

Frames come from the Standard Bus reverse-engineering work (see
``docs/protocol-stdbus-findings.md``). Each is split into:

- header bytes (frame_type, dst, src, len_hi, len_lo) → :func:`header_crc8`
- payload bytes → :func:`data_crc16`, transmitted little-endian via
  :func:`data_crc16_le_bytes`
"""

from __future__ import annotations

import pytest

from watlowlib.protocol.stdbus._crc import data_crc16_le_bytes, header_crc8

# (label, header_bytes, expected_hcrc, payload_bytes, expected_dcrc_le)
SAMPLES = [
    (
        "read PV(4001) req @ addr 1",
        bytes.fromhex("05 10 00 00 06"),
        0xE8,
        bytes.fromhex("01 03 01 04 01 01"),
        bytes.fromhex("E3 99"),
    ),
    (
        "read PV(4001) rsp @ addr 1",
        bytes.fromhex("06 00 10 00 0B"),
        0x88,
        bytes.fromhex("02 03 01 04 01 01 08 45 1E 3C D4"),
        bytes.fromhex("A7 28"),
    ),
    (
        "write SP(7001)=392.0 req @ addr 1",
        bytes.fromhex("05 10 00 00 0A"),
        0xEC,
        bytes.fromhex("01 04 07 01 01 08 43 C4 00 00"),
        bytes.fromhex("EB 77"),
    ),
    (
        "write SP(7001)=392.0 rsp @ addr 1",
        bytes.fromhex("06 00 10 00 0A"),
        0x76,
        bytes.fromhex("02 04 07 01 01 08 43 C4 00 00"),
        bytes.fromhex("82 03"),
    ),
    (
        "read HeatAlgo(8003) req @ addr 1",
        bytes.fromhex("05 10 00 00 06"),
        0xE8,
        bytes.fromhex("01 03 01 08 03 01"),
        bytes.fromhex("F0 0F"),
    ),
    (
        "read HeatAlgo(8003) rsp @ addr 1",
        bytes.fromhex("06 00 10 00 0A"),
        0x76,
        bytes.fromhex("02 03 01 08 03 01 0F 01 00 47"),
        bytes.fromhex("C5 6B"),
    ),
]


@pytest.mark.parametrize(
    ("label", "hdr", "want", "_p", "_d"),
    [(s[0], s[1], s[2], s[3], s[4]) for s in SAMPLES],
)
def test_header_crc(label: str, hdr: bytes, want: int, _p: bytes, _d: bytes) -> None:
    got = header_crc8(hdr)
    assert got == want, f"{label}: got 0x{got:02X}, want 0x{want:02X}"


@pytest.mark.parametrize(
    ("label", "_h", "_w", "payload", "want"),
    [(s[0], s[1], s[2], s[3], s[4]) for s in SAMPLES],
)
def test_data_crc(label: str, _h: bytes, _w: int, payload: bytes, want: bytes) -> None:
    got = data_crc16_le_bytes(payload)
    assert got == want, f"{label}: got {got.hex()}, want {want.hex()}"
