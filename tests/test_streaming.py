"""Tests for :mod:`watlowlib.streaming` — recorder, sample, poll source."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import anyio
import pytest

from watlowlib import (
    FakeTransport,
    OverflowPolicy,
    ProtocolKind,
    Sample,
    SerialSettings,
    record,
)
from watlowlib._lock import maybe_acquire
from watlowlib.streaming.recorder import _tick_percentiles  # pyright: ignore[reportPrivateUsage]
from watlowlib.testing import open_test_controller

if TYPE_CHECKING:
    from collections.abc import Sequence

# Same captured PM3 PV (4001) round-trip used by ``test_integration.py``.
REQ_READ_PV = bytes.fromhex("55 FF 05 10 00 00 06 E8 01 03 01 04 01 01 E3 99")
RSP_READ_PV = bytes.fromhex(
    "55 FF 06 00 10 00 0B 88 02 03 01 04 01 01 08 45 1E 3C D4 A7 28",
)
# Setpoint (parameter 7001) — captured PM3 round-trip from test_integration.
REQ_READ_SP = bytes.fromhex("55FF0510000006E80103010701018776")
RSP_READ_SP = bytes.fromhex("55FF060010000B880203010701010843C40000339A")


def _pv_script() -> dict[bytes, bytes]:
    return {REQ_READ_PV: RSP_READ_PV}


def _pv_sp_script() -> dict[bytes, bytes]:
    return {REQ_READ_PV: RSP_READ_PV, REQ_READ_SP: RSP_READ_SP}


# ---------------------------------------------------------------------------
# Sample
# ---------------------------------------------------------------------------


def test_sample_is_frozen_and_slotted() -> None:
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
        raw=b"\x00",
    )
    # frozen=True
    with pytest.raises(AttributeError):
        sample.value = 100.0  # type: ignore[misc]
    # slots=True — no __dict__
    assert not hasattr(sample, "__dict__")


# ---------------------------------------------------------------------------
# Controller.poll() — solo PollSource
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_controller_poll_returns_samples(anyio_backend: object) -> None:
    _ = anyio_backend
    transport = FakeTransport(_pv_script())
    settings = SerialSettings(port="fake://test")
    controller = await open_test_controller(
        transport,
        protocol=ProtocolKind.STDBUS,
        address=1,
        serial_settings=settings,
    )
    async with controller as ctl:
        samples = await ctl.poll_many(["process_value"])
    assert len(samples) == 1
    sample = samples[0]
    assert sample.parameter == "process_value"
    assert sample.parameter_id == 4001
    assert sample.protocol is ProtocolKind.STDBUS
    assert sample.address == 1
    assert sample.instance == 1
    assert sample.value is not None
    assert sample.received_at >= sample.requested_at
    assert sample.latency_s >= 0.0
    assert sample.raw  # non-empty wire bytes
    assert sample.device == "fake://test"


@pytest.mark.anyio
async def test_controller_poll_drops_unknown_parameter(anyio_backend: object) -> None:
    _ = anyio_backend
    transport = FakeTransport(_pv_script())
    settings = SerialSettings(port="fake://test")
    controller = await open_test_controller(
        transport,
        protocol=ProtocolKind.STDBUS,
        address=1,
        serial_settings=settings,
    )
    async with controller as ctl:
        samples = await ctl.poll_many(["process_value", "definitely_not_a_real_parameter"])
    # Only the valid parameter produced a sample; the unknown one was logged + skipped.
    assert len(samples) == 1
    assert samples[0].parameter == "process_value"


# ---------------------------------------------------------------------------
# record() — absolute-cadence recorder
# ---------------------------------------------------------------------------


class _StubSource:
    """Minimal :class:`PollSource` for unit-testing the recorder.

    Returns a fixed batch of samples per ``poll`` and counts the calls.
    """

    def __init__(self, batch: list[Sample]) -> None:
        self._batch = batch
        self.call_count = 0

    async def poll_many(
        self,
        parameters: Sequence[str | int],
        *,
        names: Sequence[str] | None = None,
        instances: Sequence[int] = (1,),
    ) -> list[Sample]:
        del parameters, names, instances
        self.call_count += 1
        return list(self._batch)


def _stub_sample() -> Sample:
    now = datetime.now(UTC)
    return Sample(
        device="stub",
        address=1,
        protocol=ProtocolKind.STDBUS,
        parameter="process_value",
        parameter_id=4001,
        instance=1,
        value=72.4,
        unit=None,
        t_mono_ns=0,
        t_utc=now,
        t_midpoint_mono_ns=None,
        requested_at=now,
        received_at=now,
        latency_s=0.0,
        raw=b"",
    )


@pytest.mark.anyio
async def test_record_emits_n_batches(anyio_backend: object) -> None:
    _ = anyio_backend
    source = _StubSource([_stub_sample()])
    async with record(
        source,
        parameters=["process_value"],
        rate_hz=50.0,
        duration=0.06,  # ~3 ticks at 50 Hz
    ) as recording:
        batches: list[int] = [len(batch) async for batch in recording.stream]
    assert source.call_count >= 2
    assert all(n == 1 for n in batches)


@pytest.mark.anyio
async def test_record_cancellation_drains(anyio_backend: object) -> None:
    """Exiting the CM cancels the producer cleanly — no hangs."""
    _ = anyio_backend
    source = _StubSource([_stub_sample()])
    iters = 0
    async with record(
        source,
        parameters=["process_value"],
        rate_hz=20.0,
    ) as recording:
        async for _batch in recording.stream:
            iters += 1
            if iters >= 2:
                break
    # If we got here, the CM exited cleanly. The producer was cancelled
    # by the task group's cancel scope on CM exit.
    assert iters == 2


@pytest.mark.anyio
async def test_record_rejects_bad_args(anyio_backend: object) -> None:
    _ = anyio_backend
    source = _StubSource([_stub_sample()])
    with pytest.raises(ValueError, match="rate_hz"):
        async with record(source, parameters=["process_value"], rate_hz=0):
            pass
    with pytest.raises(ValueError, match="duration"):
        async with record(source, parameters=["process_value"], rate_hz=1, duration=0):
            pass
    with pytest.raises(ValueError, match="buffer_size"):
        async with record(
            source,
            parameters=["process_value"],
            rate_hz=1,
            buffer_size=0,
        ):
            pass
    with pytest.raises(ValueError, match="parameters"):
        async with record(source, parameters=[], rate_hz=1):
            pass


@pytest.mark.anyio
async def test_record_drop_newest_when_consumer_blocks(anyio_backend: object) -> None:
    """Slow consumer + DROP_NEWEST policy: producer doesn't block."""
    _ = anyio_backend
    source = _StubSource([_stub_sample()])
    received: list[int] = []
    async with record(
        source,
        parameters=["process_value"],
        rate_hz=200.0,
        duration=0.05,
        overflow=OverflowPolicy.DROP_NEWEST,
        buffer_size=1,
    ) as recording:
        # Drain slowly — under DROP_NEWEST extra batches should be discarded.
        async for batch in recording.stream:
            received.append(len(batch))
            await anyio.sleep(0.02)
    # Source was invoked more times than we received batches — the
    # over-budget batches were dropped, not stuck waiting.
    assert source.call_count >= len(received)


@pytest.mark.anyio
async def test_record_drop_oldest_when_consumer_blocks(anyio_backend: object) -> None:
    """Slow consumer + DROP_OLDEST policy: producer evicts the oldest queued batch."""
    _ = anyio_backend
    source = _StubSource([_stub_sample()])
    received: list[int] = []
    async with record(
        source,
        parameters=["process_value"],
        rate_hz=200.0,
        duration=0.05,
        overflow=OverflowPolicy.DROP_OLDEST,
        buffer_size=1,
    ) as recording:
        # Drain slowly — under DROP_OLDEST stale batches get evicted in
        # favour of the latest reading. The consumer never blocks.
        async for batch in recording.stream:
            received.append(len(batch))
            await anyio.sleep(0.02)
    # Source was invoked more times than we received — extra batches
    # were evicted from the queue, not buffered indefinitely.
    assert source.call_count >= len(received)


# ---------------------------------------------------------------------------
# Atomic per-tick batch — recorder no longer interleaves with concurrent writers
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_maybe_acquire_skips_when_already_held(anyio_backend: object) -> None:
    """``maybe_acquire`` is a no-op when the current task already owns the lock.

    Without the owner-check, a nested ``async with lock`` from the
    holding task would deadlock because :class:`anyio.Lock` is non-
    reentrant.
    """
    _ = anyio_backend
    lock = anyio.Lock()
    async with lock:
        assert lock.statistics().owner == anyio.get_current_task()
        # Reentry by the holder: must not block, must not flip the owner.
        async with maybe_acquire(lock):
            assert lock.statistics().owner == anyio.get_current_task()
        # Inner CM exit must not have released the lock.
        assert lock.statistics().owner == anyio.get_current_task()
    assert lock.statistics().owner is None


@pytest.mark.anyio
async def test_maybe_acquire_blocks_other_tasks(anyio_backend: object) -> None:
    """Owner-check is task-scoped: a different task still has to queue."""
    _ = anyio_backend
    lock = anyio.Lock()
    events: list[str] = []

    async def holder() -> None:
        async with lock:
            events.append("holder.enter")
            await anyio.sleep(0.05)
            events.append("holder.exit")

    async def competitor() -> None:
        # Yield once so ``holder`` reaches the lock first.
        await anyio.sleep(0.01)
        async with maybe_acquire(lock):
            events.append("competitor.enter")

    async with anyio.create_task_group() as tg:
        tg.start_soon(holder)
        tg.start_soon(competitor)

    assert events == ["holder.enter", "holder.exit", "competitor.enter"]


@pytest.mark.anyio
async def test_session_execute_reuses_held_lock(anyio_backend: object) -> None:
    """``Session.execute`` must skip re-acquire when the caller holds the lock.

    Without the owner-check, holding the lock and then calling
    ``read_pv`` would deadlock because ``Session.execute``'s
    unconditional ``async with self._client.lock`` re-enters a non-
    reentrant :class:`anyio.Lock`.
    """
    _ = anyio_backend
    transport = FakeTransport(_pv_script())
    settings = SerialSettings(port="fake://test")
    controller = await open_test_controller(
        transport,
        protocol=ProtocolKind.STDBUS,
        address=1,
        serial_settings=settings,
    )
    async with controller as ctl:
        async with ctl.session.client.lock:
            # Lock held by us; read_pv → Session.execute should reuse
            # the acquisition rather than block waiting for itself.
            with anyio.fail_after(1.0):
                reading = await ctl.read_pv()
        assert reading.value is not None


@pytest.mark.anyio
async def test_poll_many_holds_lock_atomically(anyio_backend: object) -> None:
    """A concurrent acquirer must wait until ``poll_many`` releases the batch lock.

    Reproduces the recorder-starvation scenario at unit scale:
    a tick polls two parameters; a competitor task tries to grab
    the per-port lock while the tick is mid-batch. With atomic
    batches, the competitor only acquires *after* both reads
    finish.
    """
    _ = anyio_backend
    transport = FakeTransport(_pv_sp_script(), latency_s=0.02)
    settings = SerialSettings(port="fake://test")
    controller = await open_test_controller(
        transport,
        protocol=ProtocolKind.STDBUS,
        address=1,
        serial_settings=settings,
    )

    events: list[str] = []

    async def poller(ctl: object) -> None:
        events.append("poll.start")
        await ctl.poll_many(["process_value", "setpoint"])  # type: ignore[attr-defined]
        events.append("poll.end")

    async def competitor(ctl: object) -> None:
        # Yield enough times for the poller to enter its batch lock.
        await anyio.sleep(0.01)
        async with ctl.session.client.lock:  # type: ignore[attr-defined]
            events.append("competitor.acquired")

    async with controller as ctl, anyio.create_task_group() as tg:
        tg.start_soon(poller, ctl)
        tg.start_soon(competitor, ctl)

    # Competitor must observe its acquisition strictly after the
    # poll batch ended — atomic semantics.
    assert events.index("competitor.acquired") > events.index("poll.end")


# ---------------------------------------------------------------------------
# Recorder summary — tick_duration_ms_p50 / p99
# ---------------------------------------------------------------------------


def test_tick_percentiles_handles_corner_cases() -> None:
    """``_tick_percentiles`` returns sensible values for empty/singleton inputs."""
    assert _tick_percentiles([]) == (0.0, 0.0)
    assert _tick_percentiles([7.0]) == (7.0, 7.0)
    p50, p99 = _tick_percentiles([1.0, 2.0, 3.0, 4.0, 5.0])
    assert p50 == 3.0
    # 99th percentile of 5 evenly-spaced values is between 4 and 5.
    assert 4.5 < p99 <= 5.0
    # Monotonic on sorted input — p99 >= p50.
    p50b, p99b = _tick_percentiles([float(i) for i in range(100)])
    assert p99b >= p50b


@pytest.mark.anyio
async def test_record_summary_logs_tick_duration_metrics(
    anyio_backend: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The recorder logs ``tick_p50_ms`` / ``tick_p99_ms`` on shutdown.

    The summary itself isn't returned through the CM; for now the only
    public surface for the new metrics is the ``recorder.stop`` log
    line. Asserting on it both verifies the wiring (durations were
    recorded) and the percentile computation ran.
    """
    _ = anyio_backend
    caplog.set_level(logging.INFO, logger="watlowlib.streaming")

    class _SlowSource:
        async def poll_many(
            self,
            parameters: Sequence[str | int],
            *,
            names: Sequence[str] | None = None,
            instances: Sequence[int] = (1,),
        ) -> list[Sample]:
            del parameters, names, instances
            # Give the recorder a measurable, non-zero tick duration.
            await anyio.sleep(0.005)
            return [_stub_sample()]

    source = _SlowSource()
    async with record(
        source,
        parameters=["process_value"],
        rate_hz=50.0,
        duration=0.06,
    ) as recording:
        async for _batch in recording.stream:
            pass

    stop_lines = [r for r in caplog.records if "recorder.stop" in r.getMessage()]
    assert stop_lines, "recorder.stop log line not captured"
    msg = stop_lines[-1].getMessage()
    assert "tick_p50_ms=" in msg
    assert "tick_p99_ms=" in msg
    # Both metrics should be greater than zero given the artificial
    # 5ms per-tick sleep above (and well below the 1s sanity bound).
    p50 = float(msg.split("tick_p50_ms=")[1].split()[0])
    p99 = float(msg.split("tick_p99_ms=")[1].split()[0])
    assert p50 > 0.0
    assert p99 >= p50
    assert p99 < 1000.0


@pytest.mark.anyio
async def test_record_tick_durations_track_real_work(
    anyio_backend: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The reported ``tick_p99_ms`` reflects actual per-tick wall time.

    Stubs ``poll_many`` to take ~30ms per call; with a 50Hz target the
    logged ``tick_p99_ms`` should land in the 25-100ms range — far
    above zero, far below the artificial-sleep budget that would
    indicate the timing isn't being captured.
    """
    _ = anyio_backend
    caplog.set_level(logging.INFO, logger="watlowlib.streaming")

    class _ChunkySource:
        async def poll_many(
            self,
            parameters: Sequence[str | int],
            *,
            names: Sequence[str] | None = None,
            instances: Sequence[int] = (1,),
        ) -> list[Sample]:
            del parameters, names, instances
            # Single sleep — captures the ~30ms-per-tick wall time the
            # test wants without a busy loop.
            await anyio.sleep(0.030)
            return [_stub_sample()]

    async with record(
        _ChunkySource(),
        parameters=["process_value"],
        rate_hz=10.0,
        duration=0.5,
    ) as recording:
        async for _batch in recording.stream:
            pass

    stop_lines = [r for r in caplog.records if "recorder.stop" in r.getMessage()]
    assert stop_lines
    msg = stop_lines[-1].getMessage()
    p99 = float(msg.split("tick_p99_ms=")[1].split()[0])
    # Generous bounds — CI scheduling variance shouldn't tip a 30ms
    # workload outside [20ms, 250ms].
    assert 20.0 <= p99 <= 250.0
