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
    Controller,
    FakeTransport,
    ProtocolKind,
    SerialSettings,
    Unit,
    WatlowConfirmationRequiredError,
    WatlowNoSuchObjectError,
    WatlowValidationError,
    open_controller,
)
from watlowlib.protocol.stdbus.framing import Frame, encode_frame

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


# ---------------------------------------------------------------------------
# Display unit (parameter 17050) — units-plan
# ---------------------------------------------------------------------------

# Parameter 17050 (display_units) read at instance 1.
REQ_READ_DU = bytes.fromhex("55ff0510000006e801030111320101b9")
# Read response → 30 (Fahrenheit) / 15 (Celsius).
RSP_READ_DU_F = bytes.fromhex("55ff060010000a760203011132010f01001e8a93")
RSP_READ_DU_C = bytes.fromhex("55ff060010000a760203011132010f01000f8292")
# Write display_units = 15 (Celsius) and the device's PACKED echo.
REQ_WRITE_DU_C = bytes.fromhex("55ff0510000009ed01041132010f01000fc814")
RSP_WRITE_DU_C = bytes.fromhex("55ff06001000097702041132010f01000fcfc2")
# 0x81 NO_SUCH_OBJECT — a device that doesn't implement 17050.
RSP_READ_DU_ERR = bytes.fromhex("55ff06001000028f028176a9")


async def _open_stdbus(
    script: dict[bytes, bytes],
) -> tuple[FakeTransport, Controller]:
    transport = FakeTransport(script)
    settings = SerialSettings(port="fake://test")
    controller = await open_controller(
        transport,
        protocol=ProtocolKind.STDBUS,
        address=1,
        serial_settings=settings,
    )
    return transport, controller


@pytest.mark.anyio
async def test_read_pv_carries_display_unit_fahrenheit(anyio_backend: object) -> None:
    """Temperature reads tag their ``Reading.unit`` from cached 17050."""
    _ = anyio_backend
    transport, controller = await _open_stdbus(
        {REQ_READ_DU: RSP_READ_DU_F, REQ_READ_PV: RSP_READ_PV},
    )
    async with controller as ctl:
        pv = await ctl.read_pv()
        assert pv.unit is Unit.FAHRENHEIT
        # Cache hot: a second PV read must NOT re-query 17050.
        await ctl.read_pv()
        assert transport.writes.count(REQ_READ_DU) == 1


@pytest.mark.anyio
async def test_read_pv_carries_display_unit_celsius(anyio_backend: object) -> None:
    _ = anyio_backend
    _transport, controller = await _open_stdbus(
        {REQ_READ_DU: RSP_READ_DU_C, REQ_READ_PV: RSP_READ_PV},
    )
    async with controller as ctl:
        pv = await ctl.read_pv()
        assert pv.unit is Unit.CELSIUS


@pytest.mark.anyio
async def test_read_pv_with_no_17050_support_yields_unit_none(
    anyio_backend: object,
) -> None:
    """Device that rejects 17050 → ``Reading.unit = None`` and no re-query."""
    _ = anyio_backend
    transport, controller = await _open_stdbus(
        {REQ_READ_DU: RSP_READ_DU_ERR, REQ_READ_PV: RSP_READ_PV},
    )
    async with controller as ctl:
        pv = await ctl.read_pv()
        assert pv.unit is None
        # The cache remembers "asked and got nothing".
        await ctl.read_pv()
        assert transport.writes.count(REQ_READ_DU) == 1


@pytest.mark.anyio
async def test_set_display_units_round_trip(anyio_backend: object) -> None:
    """``set_display_units(CELSIUS, confirm=True)`` writes 15 and re-reads as Celsius."""
    _ = anyio_backend
    transport, controller = await _open_stdbus(
        {
            REQ_READ_DU: RSP_READ_DU_C,
            REQ_WRITE_DU_C: RSP_WRITE_DU_C,
        },
    )
    async with controller as ctl:
        result = await ctl.set_display_units(Unit.CELSIUS, confirm=True)
        assert result is Unit.CELSIUS
        assert REQ_WRITE_DU_C in transport.writes
        # Post-write re-read of 17050 — exactly one wire read.
        assert transport.writes.count(REQ_READ_DU) == 1


@pytest.mark.anyio
async def test_set_display_units_accepts_string_alias(anyio_backend: object) -> None:
    _ = anyio_backend
    _transport, controller = await _open_stdbus(
        {REQ_READ_DU: RSP_READ_DU_C, REQ_WRITE_DU_C: RSP_WRITE_DU_C},
    )
    async with controller as ctl:
        result = await ctl.set_display_units("celsius", confirm=True)
        assert result is Unit.CELSIUS


@pytest.mark.anyio
async def test_set_display_units_requires_confirm(anyio_backend: object) -> None:
    """No-confirm raises pre-I/O — the write bytes never hit the wire."""
    _ = anyio_backend
    transport, controller = await _open_stdbus({})
    async with controller as ctl:
        with pytest.raises(WatlowConfirmationRequiredError):
            await ctl.set_display_units(Unit.CELSIUS)
        assert REQ_WRITE_DU_C not in transport.writes


@pytest.mark.anyio
async def test_set_display_units_rejects_percent(anyio_backend: object) -> None:
    _ = anyio_backend
    transport, controller = await _open_stdbus({})
    async with controller as ctl:
        with pytest.raises(WatlowValidationError, match="PERCENT"):
            await ctl.set_display_units(Unit.PERCENT, confirm=True)
        assert transport.writes == ()


@pytest.mark.anyio
async def test_set_display_units_rejects_unknown_alias(anyio_backend: object) -> None:
    _ = anyio_backend
    transport, controller = await _open_stdbus({})
    async with controller as ctl:
        with pytest.raises(WatlowValidationError, match="unknown unit alias"):
            await ctl.set_display_units("kelvin", confirm=True)
        assert transport.writes == ()


@pytest.mark.anyio
async def test_set_display_units_rejects_raw_int(anyio_backend: object) -> None:
    """Raw 17050 codes (15, 30) go through ``write_parameter``, not this facade."""
    _ = anyio_backend
    transport, controller = await _open_stdbus({})
    async with controller as ctl:
        with pytest.raises(WatlowValidationError):
            await ctl.set_display_units(30, confirm=True)  # type: ignore[arg-type]
        assert transport.writes == ()


@pytest.mark.anyio
async def test_read_output_carries_percent(anyio_backend: object) -> None:
    """``read_output`` returns a Reading with ``unit=Unit.PERCENT``; no 17050 fetch."""
    _ = anyio_backend
    from watlowlib import PARAMETERS
    from watlowlib.commands import (
        READ_PARAMETER,
        CommandContext,
        ReadParameterRequest,
    )
    from watlowlib.protocol.stdbus.tlv import DataType, encode_value

    ctx = CommandContext(registry=PARAMETERS)
    spec_op = PARAMETERS.resolve("output_power")
    assert READ_PARAMETER.stdbus is not None
    op_payload = READ_PARAMETER.stdbus.encode(ctx, ReadParameterRequest("output_power"))
    req_op = encode_frame(Frame(frame_type=0x05, dst=0x10, src=0x00, payload=op_payload))
    rsp_payload = bytes([0x02, 0x03, 0x01, spec_op.cls, spec_op.member, 1]) + encode_value(
        DataType.FLOAT,
        42.5,
    )
    rsp_op = encode_frame(Frame(frame_type=0x06, dst=0x00, src=0x10, payload=rsp_payload))

    transport, controller = await _open_stdbus({req_op: rsp_op})
    async with controller as ctl:
        out = await ctl.loop(1).read_output()
        assert out.unit is Unit.PERCENT
        assert out.value is not None
        # Percent read does NOT consult 17050.
        assert REQ_READ_DU not in transport.writes
