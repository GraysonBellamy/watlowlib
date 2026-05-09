"""Reentrant-safe acquisition for per-port protocol locks.

:class:`anyio.Lock` is non-reentrant: a second ``async with lock`` from
the holding task deadlocks. :func:`maybe_acquire` inspects
``lock.statistics().owner`` and skips re-acquisition when the current
task already holds the lock.

Used to compose batched and unbatched callers under one lock without
threading "lock held" flags through every layer:

- :meth:`watlowlib.streaming._poll.poll_controller` acquires the
  per-port lock once for an entire tick.
- :meth:`watlowlib.devices.session.Session.execute` ordinarily
  acquires per-call but, when invoked from inside a tick batch, sees
  the lock is owned by the current task and reuses the acquisition.
- :meth:`watlowlib.manager.WatlowManager._run_group` acquires the
  shared port lock once around all devices on that port; the inner
  :func:`poll_controller` acquisitions then skip.

Atomicity follows from the call graph rather than from API flags.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import anyio

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

__all__ = ["maybe_acquire"]


@asynccontextmanager
async def maybe_acquire(lock: anyio.Lock) -> AsyncGenerator[None]:
    """Acquire ``lock`` unless the current task already holds it.

    Owner identity is compared via ``==`` because the asyncio backend
    of :mod:`anyio` returns a fresh :class:`AsyncIOTaskInfo` from each
    :func:`anyio.get_current_task` call; the dataclass equality on
    ``id`` is what carries the identity.
    """
    owner = lock.statistics().owner
    if owner is not None and owner == anyio.get_current_task():
        yield
        return
    async with lock:
        yield
