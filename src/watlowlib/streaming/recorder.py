"""Absolute-target recorder — ``record()`` emits timed :class:`Sample` batches.

:func:`record` is the v1 acquisition primitive. It drives a
:class:`PollSource` (an opened :class:`~watlowlib.devices.controller.Controller`
or a :class:`~watlowlib.manager.WatlowManager`) at an absolute-target
cadence and publishes the polled :class:`Sample` values into an
:class:`anyio.abc.ObjectReceiveStream` as per-tick batches.

Key invariants:

- **Absolute-target scheduling.** Target times are computed from
  :func:`anyio.current_time` at ``record()``-entry, not from a running
  monotonic; drift across cycles is bounded by one tick and never
  accumulates. ``anyio.sleep_until`` advances to the next target slot;
  overruns skip missed slots and increment ``samples_late``.
- **Structured concurrency.** The producer task lives strictly inside
  the async CM body. The CM yields the receive stream, user code
  iterates it, and on CM exit the task group is cancelled and joined
  before the CM returns.
- **Wall-clock provenance.** ``datetime.now(UTC)`` is captured at the
  send/receive boundaries of each device's poll and attached to the
  emitted :class:`Sample` — used for sink timestamps, never for
  scheduling.
- **Backpressure.** ``buffer_size`` sets the memory-object stream
  capacity; :class:`OverflowPolicy` controls what happens when the
  producer wants to enqueue but the consumer is behind.

The recorder consumes a :class:`PollSource` — a narrow Protocol both
:class:`~watlowlib.devices.controller.Controller` and
:class:`~watlowlib.manager.WatlowManager` satisfy. Kept as a Protocol
so the recorder is unit-testable against a lightweight stub.

Design reference: ``docs/design.md`` §6.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Protocol

import anyio

from watlowlib._logging import get_logger
from watlowlib.errors import WatlowConnectionError
from watlowlib.streaming.sample import Sample

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable

    from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

__all__ = [
    "AcquisitionSummary",
    "OverflowPolicy",
    "PollSource",
    "Recording",
    "record",
]


# Default backoff schedule for ``auto_reconnect``: small first, capped
# at 30s. The recorder retries on every tick anyway, so we just throttle
# how aggressive each individual reconnect attempt is.
_RECONNECT_BACKOFF_S: tuple[float, ...] = (0.5, 1.0, 2.0, 5.0, 10.0, 30.0)


_logger = get_logger("streaming")


class OverflowPolicy(Enum):
    """What ``record()`` does when the receive-stream buffer is full.

    The producer runs on an absolute-target schedule; the consumer
    drains at its own pace. Slow consumers create backpressure — this
    knob picks how the recorder responds.
    """

    BLOCK = "block"
    """Await the slow consumer. Default. Silent drops are surprising
    in a data-acquisition setting, so the recorder blocks the producer
    rather than quietly discarding samples."""

    DROP_NEWEST = "drop_newest"
    """Drop the batch that was about to be enqueued. Counted as late."""

    DROP_OLDEST = "drop_oldest"
    """Evict the oldest queued batch and enqueue the newest. Useful for
    real-time monitoring where the latest reading matters more than
    historical buffer contents. Each evicted batch is counted as late."""


@dataclass(slots=True)
class AcquisitionSummary:
    """Per-run summary owned and mutated by the recorder.

    **Mutability contract** (§M of UNIFIED_API_HANDOFF.md): the
    recorder is the *only* writer. Counters update in place during
    the run so progress-polling consumers (TUIs, dashboards) see live
    values. Consumers must treat this object as read-only.

    ``finished_at`` is ``None`` while the recording is in flight and
    is set on context-manager exit. Percentile fields
    (``tick_duration_ms_p50`` / ``p99``) are materialized at exit
    only because percentiles are batch-computed; the in-flight
    counters reflect the latest observation.

    Attributes:
        started_at: Wall-clock at the first scheduled tick.
        finished_at: Wall-clock at producer shutdown, or ``None``
            while running.
        samples_emitted: Count of per-tick batches actually pushed
            onto the receive stream. A tick that produced zero samples
            (every device errored) still counts as one emitted batch.
        samples_late: Count of ticks that missed their target slot
            (producer overran the previous tick, or overflow policy
            dropped the batch). Auto-reconnect ticks also count as
            late.
        max_drift_ms: Largest observed positive drift of an emitted
            batch relative to its absolute target, in milliseconds.
            A healthy run stays well under one period; values
            approaching ``1000 / rate_hz`` indicate the device or
            consumer is saturating the schedule.
        tick_duration_ms_p50: Median wall-clock duration of a single
            ``source.poll_many(...)`` call across the run, in
            milliseconds. Set on exit only. Compares directly to
            ``1000 / rate_hz`` — if it approaches the period, the
            schedule is saturated.
        tick_duration_ms_p99: 99th-percentile tick duration, in
            milliseconds. Set on exit only. Surfaces rare-but-bad
            cases where a tick stalled behind a contended port lock
            or a slow EEPROM commit.
        disconnects: Count of WatlowConnectionError events the
            producer absorbed under ``auto_reconnect=True``. Always
            ``0`` when ``auto_reconnect`` was off.
    """

    started_at: datetime
    finished_at: datetime | None = None
    samples_emitted: int = 0
    samples_late: int = 0
    max_drift_ms: float = 0.0
    tick_duration_ms_p50: float = 0.0
    tick_duration_ms_p99: float = 0.0
    disconnects: int = 0


@dataclass(slots=True)
class Recording[T]:
    """Container yielded by :func:`record` — stream + live summary + rate.

    Cross-library shape (alicat / sartorius / watlow / nidaq) so
    consumers consume the same ``recording.stream`` /
    ``recording.summary`` / ``recording.rate_hz`` accessors regardless
    of vendor.

    Per-library payload (the ``T`` parameter):

    - alicat / sartorius: ``Recording[Mapping[str, Sample]]``
    - watlow: ``Recording[Sequence[Sample]]`` — per-tick batches
    - nidaq: ``Recording[DaqReading]`` (polled) /
      ``Recording[DaqBlock]`` (block)

    Attributes:
        stream: Async iterator of per-tick payloads. Consume with
            ``async for batch in recording.stream``.
        summary: Live :class:`AcquisitionSummary` — the recorder
            mutates this in place; consumers read.
            ``summary.finished_at`` is ``None`` while running and is
            populated on context-manager exit.
        rate_hz: The cadence the recorder is running at, captured at
            ``record()`` entry. Useful for queue-sizing downstream
            buffers.
    """

    stream: AsyncIterator[T]
    summary: AcquisitionSummary
    rate_hz: float


class PollSource(Protocol):
    r"""Minimal shape the recorder needs from its dispatcher.

    Both :class:`~watlowlib.devices.controller.Controller` (solo) and
    :class:`~watlowlib.manager.WatlowManager` (multi-device) satisfy
    this Protocol. Using a Protocol keeps :func:`record` testable
    against a lightweight stub without standing up a full controller +
    transport pipeline.

    The contract is intentionally narrow: per call, return a flat
    :class:`~collections.abc.Sequence` of :class:`Sample`\ s — one
    per (device, parameter) read that succeeded. Failed reads are
    dropped from the batch and logged by the source; the recorder
    never sees them.
    """

    async def poll_many(
        self,
        parameters: Sequence[str | int],
        *,
        names: Sequence[str] | None = None,
        instances: Sequence[int] = (1,),
    ) -> Sequence[Sample]:
        """Read every ``parameters`` × ``instances`` combination on every device.

        Args:
            parameters: Parameter names or registry IDs.
            names: Subset of device names to poll (Manager-only;
                Controller ignores). ``None`` polls everything the
                source manages.
            instances: 1-indexed loop / channel numbers per device.
                Single-loop devices use ``(1,)`` (the default).

        Returns:
            A flat :class:`Sequence` of :class:`Sample`. Empty when
            every poll failed.
        """
        ...


@asynccontextmanager
async def record(
    source: PollSource,
    *,
    parameters: Sequence[str | int],
    rate_hz: float,
    duration: float | None = None,
    names: Sequence[str] | None = None,
    instances: Sequence[int] = (1,),
    overflow: OverflowPolicy = OverflowPolicy.BLOCK,
    buffer_size: int = 64,
    auto_reconnect: bool = False,
    reconnect_factory: Callable[[], Awaitable[PollSource]] | None = None,
) -> AsyncGenerator[Recording[Sequence[Sample]]]:
    """Record polled samples into a receive stream at an absolute cadence.

    Usage::

        async with record(
            controller, parameters=["process_value", "setpoint"], rate_hz=2, duration=10
        ) as recording:
            async for batch in recording.stream:
                for sample in batch:
                    print(sample.parameter, sample.value)
            # recording.summary is live; recording.summary.finished_at is None
            # while running and set on CM exit.

    The CM yields a :class:`Recording[Sequence[Sample]]` exposing
    ``.stream`` (async iterator of per-tick :class:`Sample` batches),
    ``.summary`` (live :class:`AcquisitionSummary` — recorder is sole
    writer), and ``.rate_hz`` (the cadence the recorder is running
    at).

    Each batch is a flat :class:`Sequence` — one entry per (device,
    parameter, instance) read that succeeded. Failed reads are dropped
    by the source and logged at WARN.

    Args:
        source: Any :class:`PollSource` (a :class:`Controller` or a
            :class:`WatlowManager`).
        parameters: Parameter names or registry IDs to poll each tick.
        rate_hz: Target cadence. Absolute targets are computed
            ``target[n] = start + n * (1 / rate_hz)``. Must be > 0.
        duration: Total acquisition duration in seconds. ``None``
            means "until the caller exits the CM".
        names: Subset of device names to poll per tick. ``None`` polls
            everything the source manages. Ignored for solo controllers.
        instances: 1-indexed loop / channel numbers per device. Single-
            loop devices use ``(1,)``.
        overflow: Backpressure policy when the receive-stream buffer
            is full. See :class:`OverflowPolicy`.
        buffer_size: Receive-stream capacity, in per-tick batches.
        auto_reconnect: When ``True``, treat
            :class:`WatlowConnectionError` raised by ``source.poll_many``
            as a transient transport drop rather than a fatal error.
            The producer logs ``recorder.disconnected``, waits per the
            backoff schedule, and either rebuilds the source via
            ``reconnect_factory`` (if supplied) or simply retries the
            same ``source.poll_many`` on the next tick. ``samples_late``
            ticks up for each tick missed during the gap.
        reconnect_factory: When supplied alongside ``auto_reconnect``,
            invoked to rebuild the :class:`PollSource` after a
            disconnect. Useful when the source's transport needs to be
            re-opened explicitly (e.g. a fresh
            :func:`watlowlib.open_device` call). The returned source
            replaces ``source`` for subsequent ticks. Without a
            factory, the recorder relies on ``source.poll_many`` itself to
            recover (which works for callers that wrap their own
            transport-reopen logic inside ``poll_many``).

    Yields:
        A :class:`Recording[Sequence[Sample]]` exposing ``.stream``,
        ``.summary``, and ``.rate_hz``.

    Raises:
        ValueError: ``rate_hz <= 0``, ``duration <= 0``, or
            ``buffer_size < 1``.
    """
    if rate_hz <= 0:
        raise ValueError(f"rate_hz must be > 0, got {rate_hz!r}")
    if duration is not None and duration <= 0:
        raise ValueError(f"duration must be > 0 or None, got {duration!r}")
    if buffer_size < 1:
        raise ValueError(f"buffer_size must be >= 1, got {buffer_size!r}")
    if not parameters:
        raise ValueError("parameters must be a non-empty sequence")

    period = 1.0 / rate_hz
    total_ticks = None if duration is None else max(1, round(duration * rate_hz))

    send_stream, receive_stream = anyio.create_memory_object_stream[Sequence[Sample]](
        max_buffer_size=buffer_size,
    )
    # Producer-side clone of the receive stream — used to evict the
    # oldest queued batch under DROP_OLDEST. Cloning here (before the
    # consumer starts iterating) keeps the eviction path off the
    # consumer's iterator and avoids racing with it.
    drop_rx = receive_stream.clone()

    started_at = datetime.now(UTC)
    summary = AcquisitionSummary(started_at=started_at)
    tick_durations_ms: list[float] = []
    _logger.info(
        "recorder.start rate_hz=%s duration_s=%s overflow=%s buffer_size=%s names=%s",
        rate_hz,
        duration,
        overflow.value,
        buffer_size,
        list(names) if names is not None else None,
    )

    async with anyio.create_task_group() as tg, receive_stream:

        async def _producer_entrypoint() -> None:
            await _run_producer(
                source,
                send_stream,
                drop_rx,
                tuple(parameters),
                tuple(instances),
                names,
                period,
                total_ticks,
                overflow,
                summary,
                tick_durations_ms,
                auto_reconnect=auto_reconnect,
                reconnect_factory=reconnect_factory,
            )

        tg.start_soon(_producer_entrypoint)
        try:
            yield Recording(stream=receive_stream, summary=summary, rate_hz=rate_hz)
        finally:
            # Cancel + drain before the CM returns — producer lifetime
            # is strictly nested inside the ``async with``.
            tg.cancel_scope.cancel()

    finished_at = datetime.now(UTC)
    p50, p99 = _tick_percentiles(tick_durations_ms)
    summary.finished_at = finished_at
    summary.tick_duration_ms_p50 = p50
    summary.tick_duration_ms_p99 = p99
    _logger.info(
        "recorder.stop emitted=%s late=%s max_drift_ms=%.3f "
        "tick_p50_ms=%.3f tick_p99_ms=%.3f duration_s=%.3f",
        summary.samples_emitted,
        summary.samples_late,
        summary.max_drift_ms,
        summary.tick_duration_ms_p50,
        summary.tick_duration_ms_p99,
        (finished_at - started_at).total_seconds(),
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _tick_percentiles(values: list[float]) -> tuple[float, float]:
    """Compute (p50, p99) over ``values`` with linear interpolation.

    Returns ``(0.0, 0.0)`` for an empty input. For a single value,
    both percentiles equal that value. Otherwise uses the standard
    ``i = p * (n - 1)`` indexing with linear interpolation between
    adjacent ranks — same convention as :func:`numpy.percentile` with
    its default ``linear`` method.
    """
    if not values:
        return 0.0, 0.0
    sorted_v = sorted(values)
    n = len(sorted_v)
    if n == 1:
        return sorted_v[0], sorted_v[0]

    def _q(p: float) -> float:
        k = (n - 1) * p
        f = int(k)
        c = min(f + 1, n - 1)
        if f == c:
            return sorted_v[f]
        return sorted_v[f] * (c - k) + sorted_v[c] * (k - f)

    return _q(0.5), _q(0.99)


async def _run_producer(
    source: PollSource,
    send_stream: MemoryObjectSendStream[Sequence[Sample]],
    drop_rx: MemoryObjectReceiveStream[Sequence[Sample]],
    parameters: tuple[str | int, ...],
    instances: tuple[int, ...],
    names: Sequence[str] | None,
    period: float,
    total_ticks: int | None,
    overflow: OverflowPolicy,
    summary: AcquisitionSummary,
    tick_durations_ms: list[float],
    *,
    auto_reconnect: bool = False,
    reconnect_factory: Callable[[], Awaitable[PollSource]] | None = None,
) -> None:
    """Drive the absolute-cadence poll loop.

    Scheduling uses :func:`anyio.current_time` so :func:`anyio.sleep_until`
    interprets targets against the same clock. Mixing
    :func:`time.monotonic` values would produce subtly wrong sleeps.

    When ``auto_reconnect`` is ``True``, a
    :class:`watlowlib.errors.WatlowConnectionError` from
    ``source.poll_many`` is treated as a transient gap: the tick is counted
    as late, and the producer waits per the
    :data:`_RECONNECT_BACKOFF_S` schedule before retrying — either
    against the same source (no factory) or against a freshly-built
    one (factory supplied).
    """
    start = anyio.current_time()
    tick = 0
    backoff_idx = 0
    active_source = source
    try:
        while total_ticks is None or tick < total_ticks:
            target = start + tick * period
            now = anyio.current_time()
            if now > target + period:
                # Overran by more than one full period — skip to the
                # next valid slot rather than trying to catch up.
                missed = int((now - target) / period)
                summary.samples_late += missed
                tick += missed
                target = start + tick * period
            if anyio.current_time() < target:
                await anyio.sleep_until(target)

            tick_start = time.monotonic()
            try:
                batch = await active_source.poll_many(
                    parameters,
                    names=names,
                    instances=instances,
                )
            except WatlowConnectionError as exc:
                if not auto_reconnect:
                    raise
                summary.samples_late += 1
                summary.disconnects += 1
                wait_s = _RECONNECT_BACKOFF_S[min(backoff_idx, len(_RECONNECT_BACKOFF_S) - 1)]
                _logger.warning(
                    "recorder.disconnected reason=%s tick=%d backoff_s=%.2f",
                    exc,
                    tick,
                    wait_s,
                )
                await anyio.sleep(wait_s)
                if reconnect_factory is not None:
                    try:
                        active_source = await reconnect_factory()
                        _logger.info("recorder.reconnected tick=%d", tick)
                        backoff_idx = 0
                    except WatlowConnectionError:
                        backoff_idx += 1
                else:
                    backoff_idx += 1
                tick += 1
                continue

            backoff_idx = 0
            tick_duration_ms = (time.monotonic() - tick_start) * 1_000.0
            tick_durations_ms.append(tick_duration_ms)
            drift_s = anyio.current_time() - target
            summary.max_drift_ms = max(summary.max_drift_ms, drift_s * 1_000.0)

            await _publish(send_stream, drop_rx, batch, overflow, summary)
            tick += 1
    finally:
        await send_stream.aclose()
        await drop_rx.aclose()


async def _publish(
    send_stream: MemoryObjectSendStream[Sequence[Sample]],
    drop_rx: MemoryObjectReceiveStream[Sequence[Sample]],
    batch: Sequence[Sample],
    overflow: OverflowPolicy,
    summary: AcquisitionSummary,
) -> None:
    """Enqueue ``batch`` per the configured :class:`OverflowPolicy`."""
    if overflow is OverflowPolicy.BLOCK:
        await send_stream.send(batch)
        summary.samples_emitted += 1
        return
    if overflow is OverflowPolicy.DROP_NEWEST:
        try:
            send_stream.send_nowait(batch)
        except anyio.WouldBlock:
            summary.samples_late += 1
            _logger.warning(
                "recorder.drop_newest reason=consumer_backpressure",
            )
            return
        summary.samples_emitted += 1
        return
    if overflow is OverflowPolicy.DROP_OLDEST:
        # Try the unblocked send first; if full, evict the oldest queued
        # batch through the producer-side receive clone and retry.
        try:
            send_stream.send_nowait(batch)
            summary.samples_emitted += 1
            return
        except anyio.WouldBlock:
            pass
        while True:
            try:
                drop_rx.receive_nowait()
                summary.samples_late += 1
                _logger.warning(
                    "recorder.drop_oldest reason=consumer_backpressure",
                )
            except anyio.WouldBlock:
                # Consumer won the race and made space after our failed send.
                pass
            try:
                send_stream.send_nowait(batch)
                summary.samples_emitted += 1
                return
            except anyio.WouldBlock:
                # Still full; loop and evict another queued item.
                continue
    raise AssertionError(f"unreachable overflow policy: {overflow!r}")
