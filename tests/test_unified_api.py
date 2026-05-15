"""Cross-cutting tests for the unified-API spec (UNIFIED_API_HANDOFF.md).

Each test pins one acceptance criterion from §6 of the handoff. Failures
here mean the cross-library shape has drifted — fix the library, not
the test.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from watlowlib import (
    AcquisitionSummary,
    DeviceResult,
    DiscoveryResult,
    FakeTransport,
    PollSourceAdapter,
    ProtocolKind,
    Recording,
    Sample,
    SerialSettings,
    WatlowDeviceSnapshot,
    record,
)
from watlowlib.errors import WatlowConnectionError, WatlowTimeoutError
from watlowlib.testing import open_test_controller
from watlowlib.units import to_pint

# Captured PM3 PV (4001) round-trip — same fixture as test_streaming.
_REQ_READ_PV = bytes.fromhex("55 FF 05 10 00 00 06 E8 01 03 01 04 01 01 E3 99")
_RSP_READ_PV = bytes.fromhex(
    "55 FF 06 00 10 00 0B 88 02 03 01 04 01 01 08 45 1E 3C D4 A7 28",
)


# ---------------------------------------------------------------------------
# Cross-lib import symmetry (§6 acceptance criterion)
# ---------------------------------------------------------------------------


def test_cross_lib_import_symmetry() -> None:
    """Every name in the unified spec is reachable from the top-level import."""
    from watlowlib import (
        DeviceResult,
        DeviceSnapshot,
        DiscoveryResult,
        PollSourceAdapter,
        Recording,
        WatlowDeviceSnapshot,
        find_devices,
        open_device,
        sample_to_row,
    )
    from watlowlib.units import to_pint

    exports = (
        DeviceResult,
        DeviceSnapshot,
        DiscoveryResult,
        PollSourceAdapter,
        Recording,
        WatlowDeviceSnapshot,
        find_devices,
        open_device,
        sample_to_row,
    )
    assert all(export is not None for export in exports)
    assert to_pint("C") == "degC"


# ---------------------------------------------------------------------------
# DeviceResult factories (§E.0)
# ---------------------------------------------------------------------------


def test_device_result_success_factory() -> None:
    r: DeviceResult[int] = DeviceResult.success(42)
    assert r.value == 42
    assert r.error is None
    assert r.ok is True


def test_device_result_failure_factory() -> None:
    err = WatlowTimeoutError("nope")
    r: DeviceResult[int] = DeviceResult.failure(err)
    assert r.value is None
    assert r.error is err
    assert r.ok is False


def test_device_result_keyword_construction_still_works() -> None:
    """Factories are additive — direct kwargs construction must still work."""
    r = DeviceResult[int](value=7, error=None)
    assert r.ok
    assert r.value == 7


# ---------------------------------------------------------------------------
# Sample timestamp contract (§C)
# ---------------------------------------------------------------------------


def test_sample_timestamp_fields_present() -> None:
    """Every Sample carries the §C floor: t_mono_ns, t_utc, optional midpoint."""
    now = datetime.now(UTC)
    sample = Sample(
        device="ctl1",
        address=1,
        protocol=ProtocolKind.STDBUS,
        parameter="process_value",
        parameter_id=4001,
        instance=1,
        value=72.4,
        unit=None,
        t_mono_ns=12345,
        t_utc=now,
        t_midpoint_mono_ns=None,
        requested_at=now,
        received_at=now,
        latency_s=0.001,
        raw=b"",
    )
    assert sample.t_mono_ns == 12345
    assert sample.t_utc is now
    assert sample.t_midpoint_mono_ns is None
    # I/O provenance retained alongside the canonical timestamps.
    assert sample.requested_at is now
    assert sample.received_at is now
    assert sample.latency_s == 0.001


# ---------------------------------------------------------------------------
# DiscoveryResult shape (§B)
# ---------------------------------------------------------------------------


def test_discovery_result_field_set() -> None:
    """DiscoveryResult exposes the §B field set."""
    err = WatlowConnectionError("port busy")
    row = DiscoveryResult(
        ok=False,
        port="/dev/ttyUSB0",
        address=1,
        baudrate=38400,
        protocol=ProtocolKind.STDBUS,
        device_info=None,
        error=err,
        elapsed_s=0.123,
    )
    assert row.ok is False
    assert row.port == "/dev/ttyUSB0"
    assert row.address == 1
    assert row.baudrate == 38400
    assert row.protocol is ProtocolKind.STDBUS
    assert row.device_info is None
    assert row.error is err
    assert row.elapsed_s == 0.123


# ---------------------------------------------------------------------------
# to_pint (§K)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("inp", "expected"),
    [
        ("C", "degC"),
        ("celsius", "degC"),
        ("°C", "degC"),
        ("F", "degF"),
        ("fahrenheit", "degF"),
        ("%", "percent"),
        ("percent", "percent"),
        (None, None),
        ("bogus_unit", None),
    ],
)
def test_to_pint_string_input(inp: str | None, expected: str | None) -> None:
    assert to_pint(inp) == expected


def test_to_pint_covers_every_unit_enum_value() -> None:
    """Every Unit enum value must map to a pint string."""
    from watlowlib.registry.units import Unit

    for unit in Unit:
        result = to_pint(unit)
        assert result is not None, f"Unit.{unit.name} has no pint mapping"


# ---------------------------------------------------------------------------
# PollSourceAdapter (§E)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_poll_source_adapter_relabels(anyio_backend: object) -> None:
    """The adapter sets Sample.device to the caller-provided name."""
    _ = anyio_backend
    transport = FakeTransport({_REQ_READ_PV: _RSP_READ_PV})
    ctl = await open_test_controller(
        transport,
        protocol=ProtocolKind.STDBUS,
        address=1,
        serial_settings=SerialSettings(port="fake://test"),
    )
    async with ctl:
        adapter = PollSourceAdapter("ctl-A", ctl)
        samples = await adapter.poll_many(["process_value"])
    assert len(samples) == 1
    assert samples[0].device == "ctl-A"  # caller-provided name, not transport label


@pytest.mark.anyio
async def test_poll_source_adapter_name_filter(anyio_backend: object) -> None:
    """`names=` excluding the adapter's name returns []."""
    _ = anyio_backend
    transport = FakeTransport({_REQ_READ_PV: _RSP_READ_PV})
    ctl = await open_test_controller(
        transport,
        protocol=ProtocolKind.STDBUS,
        address=1,
        serial_settings=SerialSettings(port="fake://test"),
    )
    async with ctl:
        adapter = PollSourceAdapter("ctl-A", ctl)
        samples = await adapter.poll_many(["process_value"], names=("other",))
    assert samples == []


@pytest.mark.anyio
async def test_poll_source_adapter_drives_record(anyio_backend: object) -> None:
    """A PollSourceAdapter satisfies the recorder's PollSource Protocol."""
    _ = anyio_backend
    transport = FakeTransport({_REQ_READ_PV: _RSP_READ_PV})
    ctl = await open_test_controller(
        transport,
        protocol=ProtocolKind.STDBUS,
        address=1,
        serial_settings=SerialSettings(port="fake://test"),
    )
    async with ctl:
        adapter = PollSourceAdapter("ctl-A", ctl)
        flat: list[Sample] = []
        async with record(
            adapter,
            parameters=["process_value"],
            rate_hz=50.0,
            duration=0.04,
        ) as recording:
            flat.extend([sample async for batch in recording.stream for sample in batch])
    assert flat, "expected at least one sample"
    assert all(s.device == "ctl-A" for s in flat)


# ---------------------------------------------------------------------------
# Recording wrapper + mutable AcquisitionSummary (§M)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_recording_exposes_summary_and_rate(anyio_backend: object) -> None:
    """record() yields a Recording with stream / summary / rate_hz."""
    _ = anyio_backend
    transport = FakeTransport({_REQ_READ_PV: _RSP_READ_PV})
    ctl = await open_test_controller(
        transport,
        protocol=ProtocolKind.STDBUS,
        address=1,
        serial_settings=SerialSettings(port="fake://test"),
    )
    async with (
        ctl,
        record(
            ctl,
            parameters=["process_value"],
            rate_hz=25.0,
            duration=0.08,
        ) as recording,
    ):
        assert isinstance(recording, Recording)
        assert recording.rate_hz == 25.0
        # finished_at must be None while running.
        in_flight_finished_at = recording.summary.finished_at
        assert in_flight_finished_at is None
        await anext(recording.stream)
    # On exit finished_at is set and counters are populated.
    finished_at = recording.summary.finished_at
    assert finished_at is not None
    assert recording.summary.samples_emitted >= 1


def test_acquisition_summary_is_mutable() -> None:
    """The recorder writes counters in place; consumers read."""
    summary = AcquisitionSummary(started_at=datetime.now(UTC))
    summary.samples_emitted += 1
    summary.samples_late += 1
    summary.max_drift_ms = 2.5
    assert summary.samples_emitted == 1
    assert summary.samples_late == 1
    assert summary.max_drift_ms == 2.5


# ---------------------------------------------------------------------------
# Controller.snapshot() (§H)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_snapshot_pre_identify_has_no_model(anyio_backend: object) -> None:
    """Before identify() runs, snapshot model/firmware/serial are None."""
    _ = anyio_backend
    transport = FakeTransport({_REQ_READ_PV: _RSP_READ_PV})
    ctl = await open_test_controller(
        transport,
        protocol=ProtocolKind.STDBUS,
        address=1,
        serial_settings=SerialSettings(port="fake://test"),
    )
    async with ctl:
        snap = await ctl.snapshot()
    assert isinstance(snap, WatlowDeviceSnapshot)
    assert snap.model is None
    assert snap.firmware is None
    assert snap.serial is None
    assert snap.family is None
    # No I/O during snapshot — connected reflects transport state.


@pytest.mark.anyio
async def test_snapshot_after_identify_carries_cached_info(
    anyio_backend: object,
) -> None:
    """After identify(), snapshot returns cached model/family — no further I/O."""
    _ = anyio_backend
    # Captured PM3 identify replies (part_number etc. as in test_discovery).
    from watlowlib.protocol.stdbus.framing import Frame, encode_frame
    from watlowlib.protocol.stdbus.payload import encode_read_request
    from watlowlib.protocol.stdbus.tables import (
        HOST_MAC,
        FrameType,
        addr_to_mac,
    )

    mac = addr_to_mac(1)

    def _req(payload: bytes) -> bytes:
        return encode_frame(
            Frame(
                frame_type=FrameType.DATA_EXPECTING_REPLY,
                dst=mac,
                src=HOST_MAC,
                payload=payload,
            ),
        )

    def _reply(payload: bytes) -> bytes:
        return encode_frame(
            Frame(
                frame_type=FrameType.DATA_NOT_EXPECTING_REPLY,
                dst=HOST_MAC,
                src=mac,
                payload=payload,
            ),
        )

    script = {
        _req(encode_read_request(1001, instance=1)): _reply(
            bytes.fromhex("0203010101010600000018"),
        ),
        _req(encode_read_request(1009, instance=1)): _reply(
            bytes.fromhex(
                "0203010109010910504D33523143412D414141414141410000",
            ),
        ),
        _req(encode_read_request(1002, instance=1)): _reply(
            bytes.fromhex("0203010102010600000018"),
        ),
    }
    transport = FakeTransport(script)
    ctl = await open_test_controller(
        transport,
        protocol=ProtocolKind.STDBUS,
        address=1,
        serial_settings=SerialSettings(port="fake://test"),
    )
    async with ctl:
        await ctl.identify()
        snap = await ctl.snapshot(name="ctl-A")
    assert snap.name == "ctl-A"
    assert snap.model is not None
    assert snap.model.startswith("PM3R1CA")
    assert snap.firmware == "24"


# ---------------------------------------------------------------------------
# Session.recoverable_error_count (§J)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_session_recoverable_error_count_starts_zero(
    anyio_backend: object,
) -> None:
    """The counter is wired but dormant — stays at 0 until a transient path increments it."""
    _ = anyio_backend
    transport = FakeTransport({_REQ_READ_PV: _RSP_READ_PV})
    ctl = await open_test_controller(
        transport,
        protocol=ProtocolKind.STDBUS,
        address=1,
        serial_settings=SerialSettings(port="fake://test"),
    )
    async with ctl:
        assert ctl.session.recoverable_error_count == 0


__all__: list[str] = []
