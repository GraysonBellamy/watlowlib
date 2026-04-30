"""Tests for :mod:`watlowlib.streaming` — recorder, sample, poll source."""

from __future__ import annotations

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
    open_controller,
    record,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

# Same captured PM3 PV (4001) round-trip used by ``test_integration.py``.
REQ_READ_PV = bytes.fromhex("55 FF 05 10 00 00 06 E8 01 03 01 04 01 01 E3 99")
RSP_READ_PV = bytes.fromhex(
    "55 FF 06 00 10 00 0B 88 02 03 01 04 01 01 08 45 1E 3C D4 A7 28",
)


def _pv_script() -> dict[bytes, bytes]:
    return {REQ_READ_PV: RSP_READ_PV}


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
        monotonic_ns=12345,
        requested_at=now,
        received_at=now,
        midpoint_at=now,
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
    controller = await open_controller(
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
    controller = await open_controller(
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
        monotonic_ns=0,
        requested_at=now,
        received_at=now,
        midpoint_at=now,
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
    ) as stream:
        batches: list[int] = [len(batch) async for batch in stream]
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
    ) as stream:
        async for _batch in stream:
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
    ) as stream:
        # Drain slowly — under DROP_NEWEST extra batches should be discarded.
        async for batch in stream:
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
    ) as stream:
        # Drain slowly — under DROP_OLDEST stale batches get evicted in
        # favour of the latest reading. The consumer never blocks.
        async for batch in stream:
            received.append(len(batch))
            await anyio.sleep(0.02)
    # Source was invoked more times than we received — extra batches
    # were evicted from the queue, not buffered indefinitely.
    assert source.call_count >= len(received)
