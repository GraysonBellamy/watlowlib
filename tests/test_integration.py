"""End-to-end facade tests via :class:`FakeTransport`.

Drives the full ``open_controller → identify → read_pv → set_setpoint
→ read_setpoint`` flow against scripted PM3 captures. Asserts the
session-level invariants:

- ``Reading.protocol == STDBUS``
- ``Availability.UNKNOWN → SUPPORTED`` after success
- ``Availability.UNKNOWN → UNSUPPORTED`` on ``0x81`` / ``0x83``
- ``WatlowConfirmationRequiredError`` raises pre-I/O on missing
  ``confirm=True``
"""

from __future__ import annotations

import math

import pytest

from watlowlib import (
    Availability,
    FakeTransport,
    ProtocolKind,
    SerialSettings,
    WatlowConfirmationRequiredError,
    WatlowNoSuchObjectError,
    open_controller,
)

# ---- captured PM3 round-trips ------------------------------------------

# PV (4001) read at MAC 0x10 → 2531.78 °F
REQ_READ_PV = bytes.fromhex("55 FF 05 10 00 00 06 E8 01 03 01 04 01 01 E3 99")
RSP_READ_PV = bytes.fromhex("55 FF 06 00 10 00 0B 88 02 03 01 04 01 01 08 45 1E 3C D4 A7 28")

# Setpoint (7001) write 392.0 °F
REQ_WRITE_SP = bytes.fromhex("55 FF 05 10 00 00 0A EC 01 04 07 01 01 08 43 C4 00 00 EB 77")
RSP_WRITE_SP = bytes.fromhex("55 FF 06 00 10 00 0A 76 02 04 07 01 01 08 43 C4 00 00 82 03")

# Setpoint (7001) read → 392.0 °F (same float as the write echo)
REQ_READ_SP = bytes.fromhex("55FF0510000006E80103010701018776")
RSP_READ_SP = bytes.fromhex("55FF060010000B880203010701010843C40000339A")

# Hardware ID (1001) read → 28 (ARM CPU)
REQ_READ_HW = bytes.fromhex("55FF0510000006E80103010101015EA0")
RSP_READ_HW = bytes.fromhex("55FF060010000B88020301010101060000001C5666")

# Part number (1009) read → "PM3R1CA-AAAAAAA"
REQ_READ_PART = bytes.fromhex("55FF0510000006E80103010109019E6E")
RSP_READ_PART = bytes.fromhex(
    "55FF0600100018780203010109010910504D33523143412D41414141414141000AB4"
)

# Serial number (1007 or 1032 — neither is in our captures; identify()
# tolerates absence). We don't script a reply, so the read times out
# silently — identify() falls through to None for the field.


def _build_script() -> dict[bytes, bytes]:
    return {
        REQ_READ_PV: RSP_READ_PV,
        REQ_WRITE_SP: RSP_WRITE_SP,
        REQ_READ_SP: RSP_READ_SP,
        REQ_READ_HW: RSP_READ_HW,
        REQ_READ_PART: RSP_READ_PART,
    }


@pytest.mark.anyio
async def test_facade_round_trip(anyio_backend: object) -> None:
    _ = anyio_backend
    transport = FakeTransport(_build_script())
    settings = SerialSettings(port="fake://test")
    controller = await open_controller(
        transport,
        protocol=ProtocolKind.STDBUS,
        address=1,
        serial_settings=settings,
    )
    async with controller as ctl:
        # Initial state: every command is UNKNOWN.
        assert ctl.session.availability("read_parameter:4001") is Availability.UNKNOWN

        pv = await ctl.read_pv()
        assert pv.protocol is ProtocolKind.STDBUS
        assert pv.value is not None
        assert math.isclose(pv.value, 2531.78, rel_tol=1e-3)
        assert ctl.session.availability("read_parameter:4001") is Availability.SUPPORTED

        # Setpoint write requires confirm=True.
        with pytest.raises(WatlowConfirmationRequiredError):
            await ctl.set_setpoint(392.0)
        # Pre-I/O gate — no traffic should have hit the wire.
        assert REQ_WRITE_SP not in transport.writes

        echo = await ctl.set_setpoint(392.0, confirm=True)
        assert echo.protocol is ProtocolKind.STDBUS
        assert echo.value is not None
        assert math.isclose(echo.value, 392.0)

        sp = await ctl.read_setpoint()
        assert sp.value is not None
        assert math.isclose(sp.value, 392.0)


@pytest.mark.anyio
async def test_facade_identify(anyio_backend: object) -> None:
    _ = anyio_backend
    transport = FakeTransport(_build_script())
    settings = SerialSettings(port="fake://test")
    controller = await open_controller(
        transport,
        protocol=ProtocolKind.STDBUS,
        address=1,
        serial_settings=settings,
    )
    async with controller as ctl:
        info = await ctl.identify()
    assert info.protocol is ProtocolKind.STDBUS
    assert info.address == 1
    assert info.part_number.raw == "PM3R1CA-AAAAAAA"
    assert info.family.value == "pm"
    assert info.hardware_id == 28


@pytest.mark.anyio
async def test_unsupported_object_marks_availability(anyio_backend: object) -> None:
    """A 0x81 reply flips the cache to UNSUPPORTED and stickies."""
    _ = anyio_backend
    # Build a synthetic 0x81 reply for a fabricated read request that
    # the registry happens to know about.
    from watlowlib.protocol.stdbus.framing import (
        Frame,
        encode_frame,
    )

    # Request: read parameter 4001 (PV) — same bytes as REQ_READ_PV.
    err_reply = encode_frame(
        Frame(frame_type=0x06, dst=0x00, src=0x10, payload=bytes.fromhex("02 81"))
    )
    transport = FakeTransport({REQ_READ_PV: err_reply})
    settings = SerialSettings(port="fake://test")
    controller = await open_controller(
        transport,
        protocol=ProtocolKind.STDBUS,
        address=1,
        serial_settings=settings,
    )
    async with controller as ctl:
        with pytest.raises(WatlowNoSuchObjectError):
            await ctl.read_pv()
        assert ctl.session.availability("read_parameter:4001") is Availability.UNSUPPORTED
        # Second call short-circuits — no new write hits the wire.
        write_count_before = len(transport.writes)
        from watlowlib.errors import (
            WatlowProtocolUnsupportedError,
        )

        with pytest.raises(WatlowProtocolUnsupportedError):
            await ctl.read_pv()
        assert len(transport.writes) == write_count_before
