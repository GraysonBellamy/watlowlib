"""Sync facade — blocking wrappers over the async core.

The sync surface targets scripts, notebooks, and REPL use. The async
core remains canonical; every sync facade routes coroutines through a
:class:`SyncPortal` (an :class:`anyio.from_thread.BlockingPortal`
wrapper) so the event loop runs on a background thread.

What ships here:

- :class:`SyncPortal` — single dispatch primitive used by the rest of
  the sync facade.
- :class:`Watlow` / :class:`SyncController` — sync mirror of
  :class:`~watlowlib.devices.controller.Controller`.
- :class:`SyncWatlowManager` — sync mirror of
  :class:`~watlowlib.manager.WatlowManager`.
- :func:`record` / :func:`pipe` — sync mirrors of the streaming
  primitives.
- :class:`SyncSinkAdapter` + per-sink wrappers (``SyncCsvSink``,
  ``SyncJsonlSink``, ``SyncSqliteSink``, ``SyncInMemorySink``,
  ``SyncParquetSink``, ``SyncPostgresSink``).

Design reference: ``docs/design.md`` §6 (sync portal).
"""

from __future__ import annotations

from watlowlib.sync.controller import SyncController, SyncControllerLoop, Watlow
from watlowlib.sync.manager import SyncWatlowManager
from watlowlib.sync.portal import SyncAsyncIterator, SyncPortal, run_sync
from watlowlib.sync.recording import pipe, record
from watlowlib.sync.sinks import (
    SyncCsvSink,
    SyncInMemorySink,
    SyncJsonlSink,
    SyncParquetSink,
    SyncPostgresSink,
    SyncSinkAdapter,
    SyncSqliteSink,
)

__all__ = [
    "SyncAsyncIterator",
    "SyncController",
    "SyncControllerLoop",
    "SyncCsvSink",
    "SyncInMemorySink",
    "SyncJsonlSink",
    "SyncParquetSink",
    "SyncPortal",
    "SyncPostgresSink",
    "SyncSinkAdapter",
    "SyncSqliteSink",
    "SyncWatlowManager",
    "Watlow",
    "pipe",
    "record",
    "run_sync",
]
