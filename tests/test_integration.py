"""End-to-end facade tests via :class:`FakeTransport`.

Drives the full ``open_test_controller → identify → read_pv → set_setpoint
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
)
from watlowlib.protocol.stdbus.framing import Frame, encode_frame
from watlowlib.testing import open_test_controller

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
    controller = await open_test_controller(
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
    controller = await open_test_controller(
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
    controller = await open_test_controller(
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
# Temperature unit tagging — see docs/devices.md §Units
#
# The library never infers ``Reading.unit`` from parameter 17050. On at
# least one PM3 firmware revision 17050 is a label-only register that
# does not govern the wire scale; trusting it silently mis-tags values.
# Temperature readings carry ``unit=None`` unless the caller passes
# ``assert_wire_temperature_unit=`` to ``open_device`` — an explicit,
# externally-verified user assertion.
#
# Parameter 17050 is still reachable through the inspection facade
# (``read_comms_unit_label`` / ``set_comms_unit_label``) but that path
# is **decoupled** from ``Reading.unit``.
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
    *,
    assert_wire_temperature_unit: Unit | str | None = None,
) -> tuple[FakeTransport, Controller]:
    """Build a Controller over a scripted FakeTransport.

    Test convenience: ``assert_wire_temperature_unit`` accepts the
    same shapes (``Unit`` / alias / ``None``) as
    :func:`watlowlib.open_device`. We coerce here via the same
    factory helper so the string-alias path is exercised end-to-end.
    """
    from watlowlib.devices.factory import coerce_wire_temperature_unit

    transport = FakeTransport(script)
    settings = SerialSettings(port="fake://test")
    controller = await open_test_controller(
        transport,
        protocol=ProtocolKind.STDBUS,
        address=1,
        serial_settings=settings,
        wire_temperature_unit=coerce_wire_temperature_unit(
            assert_wire_temperature_unit,
        ),
    )
    return transport, controller


# --- Default behaviour: no assertion → no unit tag ---------------------------


@pytest.mark.anyio
async def test_read_pv_default_yields_unit_none(anyio_backend: object) -> None:
    """No ``assert_wire_temperature_unit`` → temperature reads carry ``unit=None``.

    The library refuses to guess. Critically, it does **not** consult
    parameter 17050 to fill the gap — that would re-introduce the bug
    described in ``docs/devices.md`` §Units.
    """
    _ = anyio_backend
    transport, controller = await _open_stdbus({REQ_READ_PV: RSP_READ_PV})
    async with controller as ctl:
        pv = await ctl.read_pv()
        assert pv.unit is None
        # Critical: no 17050 read on the wire.
        assert REQ_READ_DU not in transport.writes


# --- Asserted unit: tag matches the asserted scale ---------------------------


@pytest.mark.anyio
async def test_read_pv_with_asserted_unit_fahrenheit(anyio_backend: object) -> None:
    """``assert_wire_temperature_unit=FAHRENHEIT`` → Reading.unit == FAHRENHEIT."""
    _ = anyio_backend
    transport, controller = await _open_stdbus(
        {REQ_READ_PV: RSP_READ_PV},
        assert_wire_temperature_unit=Unit.FAHRENHEIT,
    )
    async with controller as ctl:
        pv = await ctl.read_pv()
        assert pv.unit is Unit.FAHRENHEIT
        # The library still does not read 17050 for this — the user
        # told us the scale; 17050 is not consulted.
        assert REQ_READ_DU not in transport.writes


@pytest.mark.anyio
async def test_read_pv_with_asserted_unit_celsius(anyio_backend: object) -> None:
    _ = anyio_backend
    _transport, controller = await _open_stdbus(
        {REQ_READ_PV: RSP_READ_PV},
        assert_wire_temperature_unit=Unit.CELSIUS,
    )
    async with controller as ctl:
        pv = await ctl.read_pv()
        assert pv.unit is Unit.CELSIUS


# --- Regression test: the divergence case ----------------------------------


@pytest.mark.anyio
async def test_label_only_17050_does_not_drive_reading_unit(
    anyio_backend: object,
) -> None:
    """Regression: when 17050 reports CELSIUS but the wire is in FAHRENHEIT,
    the library must not tag the FAHRENHEIT value as CELSIUS.

    This is the symmetric case the original verification could not
    distinguish: the device reports ``17050=°C`` but the actual wire
    value is in °F (verified on PM3C1AJ firmware id 5678; see
    ``docs/devices.md`` §Units). The library refuses to
    consult 17050 for ``Reading.unit``, so:

    - Without assertion: ``unit=None`` (honest).
    - With ``assert_wire_temperature_unit=FAHRENHEIT``: tag matches
      the actual scale of ``value`` (truthful).

    The previously buggy behaviour — tagging a °F value with °C
    because 17050 reported °C — is impossible to reach via this API
    surface now.
    """
    _ = anyio_backend
    # Device reports 17050 = °C, but the PV / SP frames carry °F-scaled
    # values (the PM3 firmware's label-only quirk).
    script = {
        REQ_READ_DU: RSP_READ_DU_C,
        REQ_READ_PV: RSP_READ_PV,
    }

    # 1. No assertion → ``Reading.unit = None``. The library does not
    #    fall back to 17050.
    transport_a, controller_a = await _open_stdbus(script)
    async with controller_a as ctl:
        pv = await ctl.read_pv()
        assert pv.unit is None, "must not infer unit from 17050"
        assert REQ_READ_DU not in transport_a.writes, "must not read 17050 to derive Reading.unit"
        # The inspection facade still works and reports what 17050 says.
        label = await ctl.read_comms_unit_label()
        assert label is Unit.CELSIUS, "read_comms_unit_label faithfully reports 17050's value"
        # ...but the inspection read does NOT retroactively change tags
        # on prior readings.
        assert pv.unit is None

    # 2. With explicit assertion → tag matches the wire's actual scale,
    #    even though 17050 disagrees.
    _transport_b, controller_b = await _open_stdbus(
        script,
        assert_wire_temperature_unit=Unit.FAHRENHEIT,
    )
    async with controller_b as ctl:
        pv = await ctl.read_pv()
        assert pv.unit is Unit.FAHRENHEIT, (
            "tag must match the asserted wire scale, not 17050's label"
        )
        # And reading 17050 still reports °C — the registers are
        # decoupled from the assertion.
        label = await ctl.read_comms_unit_label()
        assert label is Unit.CELSIUS


# --- Inspection facade (read/set_comms_unit_label) ---------------------------


@pytest.mark.anyio
async def test_read_comms_unit_label_round_trip(anyio_backend: object) -> None:
    """``read_comms_unit_label`` reads parameter 17050 and caches the result."""
    _ = anyio_backend
    transport, controller = await _open_stdbus({REQ_READ_DU: RSP_READ_DU_F})
    async with controller as ctl:
        first = await ctl.read_comms_unit_label()
        assert first is Unit.FAHRENHEIT
        # Hot cache — second call is a no-op on the wire.
        second = await ctl.read_comms_unit_label()
        assert second is Unit.FAHRENHEIT
        assert transport.writes.count(REQ_READ_DU) == 1


@pytest.mark.anyio
async def test_read_comms_unit_label_handles_rejection(
    anyio_backend: object,
) -> None:
    """A device that rejects 17050 → ``read_comms_unit_label()`` returns None."""
    _ = anyio_backend
    transport, controller = await _open_stdbus({REQ_READ_DU: RSP_READ_DU_ERR})
    async with controller as ctl:
        first = await ctl.read_comms_unit_label()
        assert first is None
        # The cache remembers "asked and got nothing".
        await ctl.read_comms_unit_label()
        assert transport.writes.count(REQ_READ_DU) == 1


@pytest.mark.anyio
async def test_set_comms_unit_label_round_trip(anyio_backend: object) -> None:
    """``set_comms_unit_label(CELSIUS, confirm=True)`` writes 15 and re-reads °C."""
    _ = anyio_backend
    transport, controller = await _open_stdbus(
        {REQ_READ_DU: RSP_READ_DU_C, REQ_WRITE_DU_C: RSP_WRITE_DU_C},
    )
    async with controller as ctl:
        result = await ctl.set_comms_unit_label(Unit.CELSIUS, confirm=True)
        assert result is Unit.CELSIUS
        assert REQ_WRITE_DU_C in transport.writes
        assert transport.writes.count(REQ_READ_DU) == 1


@pytest.mark.anyio
async def test_set_comms_unit_label_does_not_change_reading_unit(
    anyio_backend: object,
) -> None:
    """Setting 17050 does **not** retroactively change ``Reading.unit``.

    The label register and the wire scale are decoupled by design.
    """
    _ = anyio_backend
    _transport, controller = await _open_stdbus(
        {
            REQ_READ_DU: RSP_READ_DU_C,
            REQ_WRITE_DU_C: RSP_WRITE_DU_C,
            REQ_READ_PV: RSP_READ_PV,
        },
    )
    async with controller as ctl:
        await ctl.set_comms_unit_label(Unit.CELSIUS, confirm=True)
        pv = await ctl.read_pv()
        # No assertion was made, so ``unit`` stays ``None`` regardless
        # of the 17050 write.
        assert pv.unit is None


@pytest.mark.anyio
async def test_set_comms_unit_label_accepts_string_alias(anyio_backend: object) -> None:
    _ = anyio_backend
    _transport, controller = await _open_stdbus(
        {REQ_READ_DU: RSP_READ_DU_C, REQ_WRITE_DU_C: RSP_WRITE_DU_C},
    )
    async with controller as ctl:
        result = await ctl.set_comms_unit_label("celsius", confirm=True)
        assert result is Unit.CELSIUS


@pytest.mark.anyio
async def test_set_comms_unit_label_requires_confirm(anyio_backend: object) -> None:
    """No-confirm raises pre-I/O — the write bytes never hit the wire."""
    _ = anyio_backend
    transport, controller = await _open_stdbus({})
    async with controller as ctl:
        with pytest.raises(WatlowConfirmationRequiredError):
            await ctl.set_comms_unit_label(Unit.CELSIUS)
        assert REQ_WRITE_DU_C not in transport.writes


@pytest.mark.anyio
async def test_set_comms_unit_label_rejects_percent(anyio_backend: object) -> None:
    _ = anyio_backend
    transport, controller = await _open_stdbus({})
    async with controller as ctl:
        with pytest.raises(WatlowValidationError, match="PERCENT"):
            await ctl.set_comms_unit_label(Unit.PERCENT, confirm=True)
        assert transport.writes == ()


@pytest.mark.anyio
async def test_set_comms_unit_label_rejects_unknown_alias(
    anyio_backend: object,
) -> None:
    _ = anyio_backend
    transport, controller = await _open_stdbus({})
    async with controller as ctl:
        with pytest.raises(WatlowValidationError, match="unknown unit alias"):
            await ctl.set_comms_unit_label("kelvin", confirm=True)
        assert transport.writes == ()


@pytest.mark.anyio
async def test_set_comms_unit_label_rejects_raw_int(anyio_backend: object) -> None:
    """Raw 17050 codes (15, 30) go through ``write_parameter``, not this facade."""
    _ = anyio_backend
    transport, controller = await _open_stdbus({})
    async with controller as ctl:
        with pytest.raises(WatlowValidationError):
            await ctl.set_comms_unit_label(30, confirm=True)  # type: ignore[arg-type]
        assert transport.writes == ()


# --- assert_wire_temperature_unit validation -------------------------------


@pytest.mark.anyio
async def test_assert_wire_temperature_unit_rejects_percent(
    anyio_backend: object,
) -> None:
    """``Unit.PERCENT`` is not a temperature scale — reject pre-I/O."""
    _ = anyio_backend
    from watlowlib import open_device

    with pytest.raises(WatlowValidationError, match="PERCENT"):
        await open_device(
            "fake://test",
            assert_wire_temperature_unit=Unit.PERCENT,
        )


@pytest.mark.anyio
async def test_assert_wire_temperature_unit_accepts_string_alias(
    anyio_backend: object,
) -> None:
    """String aliases are accepted (case-insensitive)."""
    _ = anyio_backend
    _transport, controller = await _open_stdbus(
        {REQ_READ_PV: RSP_READ_PV},
        assert_wire_temperature_unit="celsius",
    )
    async with controller as ctl:
        pv = await ctl.read_pv()
        assert pv.unit is Unit.CELSIUS


# --- Percent reads (independent of 17050 / wire_temperature_unit) ----------


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
