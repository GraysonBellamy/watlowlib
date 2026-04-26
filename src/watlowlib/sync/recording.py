"""Sync wrappers for :func:`watlowlib.streaming.record` and :func:`watlowlib.sinks.pipe`.

:func:`record` — sync context manager wrapping the async recorder. The
produced iterator is blocking; on CM exit the underlying async task
group is cancelled and joined by the portal.

:func:`pipe` — sync drain loop matching
:func:`watlowlib.sinks.pipe`'s batch / time flush semantics.
Rebuilt in sync-land rather than wrapping the async driver so
buffering stays under sync control and the time threshold uses
:func:`time.monotonic`, not :func:`anyio.current_time`.

Design reference: ``docs/design.md`` §6.
"""

from __future__ import annotations

import time
from contextlib import ExitStack, contextmanager
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from watlowlib.sinks.base import pipe as async_pipe
from watlowlib.streaming.recorder import (
    AcquisitionSummary,
    OverflowPolicy,
    PollSource,
)
from watlowlib.streaming.recorder import (
    record as async_record,
)
from watlowlib.sync.manager import SyncWatlowManager
from watlowlib.sync.portal import SyncAsyncIterator, SyncPortal
from watlowlib.sync.sinks import SyncSinkAdapter

if TYPE_CHECKING:
    from collections.abc import Generator, Iterator, Sequence

    from watlowlib.sinks.base import SampleSink
    from watlowlib.streaming.sample import Sample

__all__ = [
    "AcquisitionSummary",
    "OverflowPolicy",
    "pipe",
    "record",
]


def _resolve_poll_source(
    source: SyncWatlowManager | PollSource,
) -> PollSource:
    """Return the async :class:`PollSource` inside ``source``."""
    if isinstance(source, SyncWatlowManager):
        inner = source._mgr  # pyright: ignore[reportPrivateUsage]
        if inner is None:
            raise RuntimeError("SyncWatlowManager is not open")
        return inner
    return source


def _resolve_portal(
    explicit: SyncPortal | None,
    source: SyncWatlowManager | PollSource,
    sink: SyncSinkAdapter | SampleSink | None,
) -> SyncPortal | None:
    """Pick the portal that recording + sink I/O share."""
    if explicit is not None:
        return explicit
    if isinstance(source, SyncWatlowManager):
        return source.portal
    if isinstance(sink, SyncSinkAdapter):
        try:
            return sink.portal
        except RuntimeError:
            return None
    return None


@contextmanager
def record(
    source: SyncWatlowManager | PollSource,
    *,
    parameters: Sequence[str | int],
    rate_hz: float,
    duration: float | None = None,
    names: Sequence[str] | None = None,
    instances: Sequence[int] = (1,),
    overflow: OverflowPolicy = OverflowPolicy.BLOCK,
    buffer_size: int = 64,
    portal: SyncPortal | None = None,
) -> Generator[Iterator[Sequence[Sample]]]:
    """Sync :func:`watlowlib.streaming.record`.

    If ``source`` is a :class:`SyncWatlowManager`, its portal is
    reused — the recorder and manager must share an event loop. Pass
    ``portal=`` to override.
    """
    poll_source = _resolve_poll_source(source)
    with ExitStack() as stack:
        active_portal = _resolve_portal(portal, source, None) or stack.enter_context(SyncPortal())
        async_cm = async_record(
            poll_source,
            parameters=parameters,
            rate_hz=rate_hz,
            duration=duration,
            names=names,
            instances=instances,
            overflow=overflow,
            buffer_size=buffer_size,
        )
        async_stream = stack.enter_context(active_portal.wrap_async_context_manager(async_cm))
        sync_iter = stack.enter_context(active_portal.wrap_async_iter(async_stream))
        yield sync_iter


def pipe(
    stream: Iterator[Sequence[Sample]],
    sink: SyncSinkAdapter | SampleSink,
    *,
    batch_size: int = 64,
    flush_interval: float = 1.0,
    portal: SyncPortal | None = None,
) -> AcquisitionSummary:
    """Sync :func:`watlowlib.sinks.pipe`."""
    if batch_size < 1:
        raise ValueError(f"batch_size must be >= 1, got {batch_size!r}")
    if flush_interval <= 0:
        raise ValueError(f"flush_interval must be > 0, got {flush_interval!r}")

    if isinstance(sink, SyncSinkAdapter):
        flush = sink.write_many
    else:
        resolved: SyncPortal | None = portal
        if resolved is None and isinstance(stream, SyncAsyncIterator):
            resolved = stream._portal  # pyright: ignore[reportPrivateUsage]
        if resolved is None:
            raise RuntimeError(
                "pipe: passing an async SampleSink requires a portal — "
                "wrap the sink in a SyncSinkAdapter or pass portal=.",
            )
        async_sink = sink
        active: SyncPortal = resolved

        def flush(samples: Sequence[Sample]) -> None:
            active.call(async_sink.write_many, samples)

    started_at = datetime.now(UTC)
    emitted = 0
    buffer: list[Sample] = []
    last_flush = time.monotonic()

    for batch in stream:
        buffer.extend(batch)
        now = time.monotonic()
        if len(buffer) >= batch_size or (now - last_flush) >= flush_interval:
            flush(buffer)
            emitted += len(buffer)
            buffer.clear()
            last_flush = now

    if buffer:
        flush(buffer)
        emitted += len(buffer)
        buffer.clear()

    finished_at = datetime.now(UTC)
    return AcquisitionSummary(
        started_at=started_at,
        finished_at=finished_at,
        samples_emitted=emitted,
        samples_late=0,
        max_drift_ms=0.0,
    )


# Keep a reference so :func:`pipe`'s docstring referring to the async
# driver resolves in sphinx without pulling in a second import at
# call time.
_ = async_pipe
