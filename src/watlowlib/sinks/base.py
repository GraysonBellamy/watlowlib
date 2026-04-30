"""Sink Protocol, ``sample_to_row`` flattener, and the :func:`pipe` driver.

A :class:`SampleSink` is the minimal shape the recorder's downstream
consumer needs: :meth:`open`, :meth:`write_many`, :meth:`close`, and
the matching async context-manager methods. The in-tree sinks
(:class:`~watlowlib.sinks.memory.InMemorySink`,
:class:`~watlowlib.sinks.csv.CsvSink`,
:class:`~watlowlib.sinks.jsonl.JsonlSink`,
:class:`~watlowlib.sinks.sqlite.SqliteSink`) all satisfy this Protocol;
third-party sinks (Parquet, Postgres, Kafka, ...) can slot in without
touching library code.

:func:`pipe` is the v1 acquisition glue. It reads per-tick batches
out of the recorder's receive stream, buffers them up to ``batch_size``
(or ``flush_interval`` seconds, whichever comes first), and calls
``sink.write_many`` to flush. On stream exhaustion it drains any
remaining buffer and returns an :class:`AcquisitionSummary` with
``samples_emitted`` reflecting the count actually handed to the sink.

Design reference: ``docs/design.md`` §6.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

import anyio

from watlowlib._logging import get_logger
from watlowlib.streaming.recorder import AcquisitionSummary

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from types import TracebackType
    from typing import Self

    from watlowlib.streaming.sample import Sample

__all__ = [
    "SampleSink",
    "pipe",
    "sample_to_row",
]


_logger = get_logger("sinks")


class SampleSink(Protocol):
    """Minimal shape of an acquisition sink.

    Sinks own their storage handle lifecycle. Concrete implementations
    typically follow this call sequence:

    1. ``await sink.open()`` — allocate file descriptors, DB
       connections, etc. Safe to call again on an already-open sink.
    2. ``await sink.write_many(samples)`` — one or more times.
       ``samples`` is a :class:`~collections.abc.Sequence` so the sink
       knows the full batch up front (CSV column inference, Parquet
       row groups, parameterised inserts).
    3. ``await sink.close()`` — flush and release the handle.
       Idempotent.

    The async context-manager methods provide an ``async with sink:``
    shape for the common case of "open → write → close" in one block.
    """

    async def open(self) -> None:
        """Allocate the sink's backing resource (file handle, DB conn, ...)."""
        ...

    async def write_many(self, samples: Sequence[Sample]) -> None:
        """Append ``samples`` to the sink.

        ``Sequence`` (not ``Iterable``) because every in-tree sink wants
        ``len()`` — CSV schema inference, batched parameterised inserts,
        Parquet row-group bookkeeping.
        """
        ...

    async def close(self) -> None:
        """Flush and release the backing resource. Idempotent."""
        ...

    async def __aenter__(self) -> Self:
        """Open the sink and return ``self`` for chaining."""
        ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Close the sink on exit."""
        ...


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def sample_to_row(sample: Sample) -> dict[str, float | int | str | bool | None]:
    """Flatten a :class:`Sample` into a single row dict for tabular sinks.

    Long-format schema (one row per parameter read), stable across all
    in-tree sinks:

    - ``device`` — manager-assigned name (or controller transport label).
    - ``address`` — bus address.
    - ``protocol`` — wire protocol that produced the read (string).
    - ``parameter`` — canonical parameter name.
    - ``parameter_id`` — registry parameter id.
    - ``instance`` — 1-indexed loop / channel selector.
    - ``value`` — decoded value, coerced to a sink-friendly scalar
      (bools become ``"true"`` / ``"false"`` strings so SQLite type
      inference doesn't pin the column to INTEGER for the run).
    - ``unit`` — display string, or ``None`` when the registry doesn't
      carry per-parameter unit metadata.
    - ``requested_at`` / ``received_at`` / ``midpoint_at`` — ISO 8601
      strings.
    - ``latency_s`` — poll round-trip in seconds.

    The sample's ``raw`` payload is intentionally **not** in the row:
    bytes don't fit cleanly into CSV / JSONL / SQLite affinities, and
    tabular sinks are for time-series queries, not byte-level
    diagnostics. Callers that need ``raw`` consume :class:`Sample`
    directly via :class:`~watlowlib.sinks.memory.InMemorySink`.
    """
    raw_value = sample.value
    coerced: float | int | str | None
    if isinstance(raw_value, bool):
        # Coerce before the int-isinstance check below; bool is an int.
        coerced = "true" if raw_value else "false"
    elif isinstance(raw_value, int | float | str):
        coerced = raw_value
    else:
        # raw_value is None — Sample.value's type rules out anything else.
        coerced = None

    return {
        "device": sample.device,
        "address": sample.address,
        "protocol": sample.protocol.value,
        "parameter": sample.parameter,
        "parameter_id": sample.parameter_id,
        "instance": sample.instance,
        "value": coerced,
        "unit": sample.unit,
        "requested_at": sample.requested_at.isoformat(),
        "received_at": sample.received_at.isoformat(),
        "midpoint_at": sample.midpoint_at.isoformat(),
        "latency_s": sample.latency_s,
    }


# ---------------------------------------------------------------------------
# pipe() driver
# ---------------------------------------------------------------------------


async def pipe(
    stream: AsyncIterator[Sequence[Sample]],
    sink: SampleSink,
    *,
    batch_size: int = 64,
    flush_interval: float = 1.0,
) -> AcquisitionSummary:
    r"""Drain ``stream`` into ``sink`` with buffered flushes.

    Reads per-tick batches from the recorder and accumulates the
    individual :class:`Sample`\ s into a list. A flush happens when
    either threshold is first crossed:

    - the buffer reaches ``batch_size`` samples, or
    - ``flush_interval`` seconds have elapsed since the last flush.

    The time-based check fires on every incoming batch, so the actual
    inter-flush latency is bounded below by the recorder's tick
    period: ``effective_flush_period ≈ max(flush_interval,
    1 / rate_hz)``. For low-rate acquisitions (rate_hz < 1 / flush_interval)
    the recorder cadence dominates; for high-rate acquisitions the
    configured ``flush_interval`` dominates. Either way, on stream
    exhaustion any leftover buffer is flushed before the summary is
    returned.

    The ``samples_late`` / ``max_drift_ms`` fields on the returned
    summary stay at zero — those are recorder-layer concepts. The
    recorder emits its own summary on CM exit; this summary is the
    sink-side view.

    Args:
        stream: The async iterator yielded by
            :func:`~watlowlib.streaming.record`.
        sink: Any :class:`SampleSink`. Must already be open.
        batch_size: Buffer threshold in samples (not batches).
        flush_interval: Time threshold in seconds between flushes.

    Returns:
        An :class:`AcquisitionSummary` with ``samples_emitted`` set to
        the count actually handed to the sink.

    Raises:
        ValueError: ``batch_size < 1`` or ``flush_interval <= 0``.
    """
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size!r}")
    if flush_interval <= 0:
        raise ValueError(f"flush_interval must be > 0, got {flush_interval!r}")

    started_at = datetime.now(UTC)
    emitted = 0
    buffer: list[Sample] = []
    last_flush = anyio.current_time()

    async def _flush() -> None:
        nonlocal emitted
        if not buffer:
            return
        await sink.write_many(buffer)
        emitted += len(buffer)
        buffer.clear()

    async for batch in stream:
        buffer.extend(batch)
        now = anyio.current_time()
        if len(buffer) >= batch_size or (now - last_flush) >= flush_interval:
            await _flush()
            last_flush = now

    await _flush()
    finished_at = datetime.now(UTC)
    _logger.info(
        "sinks.pipe_done sink=%s samples_emitted=%s duration_s=%.3f",
        type(sink).__name__,
        emitted,
        (finished_at - started_at).total_seconds(),
    )
    return AcquisitionSummary(
        started_at=started_at,
        finished_at=finished_at,
        samples_emitted=emitted,
        samples_late=0,
        max_drift_ms=0.0,
    )
