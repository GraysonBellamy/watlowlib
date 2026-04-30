"""Sync manager facade — portal-driven wrapper over :class:`WatlowManager`.

:class:`SyncWatlowManager` wraps the async
:class:`~watlowlib.manager.WatlowManager` through a
:class:`~watlowlib.sync.portal.SyncPortal`. Every coroutine method
becomes a blocking method here; the synchronous :meth:`get` stays
synchronous and delegates directly.

Lifecycle mirrors the async side: the class is a ``with`` context
manager. By default each instance owns its own portal; callers that
need several facades to share one event loop can pass ``portal=`` to
reuse a long-lived :class:`SyncPortal`.

Design reference: ``docs/design.md`` §6.
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import TYPE_CHECKING, Self

from watlowlib.manager import DeviceResult, ErrorPolicy, WatlowManager
from watlowlib.protocol.base import ProtocolKind
from watlowlib.registry.families import ControllerFamily
from watlowlib.sync.controller import (
    SyncController,
    unwrap_sync_controller,
    wrap_controller,
)
from watlowlib.sync.portal import SyncPortal

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from types import TracebackType

    from watlowlib.devices.controller import Controller
    from watlowlib.sinks.base import SampleSink
    from watlowlib.streaming.recorder import AcquisitionSummary, OverflowPolicy
    from watlowlib.streaming.sample import Sample
    from watlowlib.sync.sinks import SyncSinkAdapter
    from watlowlib.transport.base import SerialSettings, Transport

__all__ = [
    "DeviceResult",
    "ErrorPolicy",
    "SyncWatlowManager",
]


class SyncWatlowManager:
    """Blocking facade over :class:`watlowlib.manager.WatlowManager`."""

    def __init__(
        self,
        *,
        error_policy: ErrorPolicy = ErrorPolicy.RAISE,
        portal: SyncPortal | None = None,
    ) -> None:
        self._error_policy = error_policy
        self._portal_override = portal
        self._stack: ExitStack | None = None
        self._portal: SyncPortal | None = None
        self._mgr: WatlowManager | None = None
        self._wrapped: dict[str, SyncController] = {}
        self._entered = False

    # --------------------------------------------------------------- properties

    @property
    def error_policy(self) -> ErrorPolicy:
        """The :class:`ErrorPolicy` this manager was constructed with."""
        return self._error_policy

    @property
    def names(self) -> tuple[str, ...]:
        """Insertion-ordered tuple of managed controller names."""
        mgr = self._mgr
        if mgr is None:
            return ()
        return mgr.names

    @property
    def closed(self) -> bool:
        """``True`` once :meth:`close` or ``__exit__`` has run."""
        mgr = self._mgr
        return mgr is None or mgr.closed

    @property
    def portal(self) -> SyncPortal:
        """The :class:`SyncPortal` this manager's coroutines run on."""
        portal = self._portal
        if portal is None:
            raise RuntimeError("SyncWatlowManager is not open")
        return portal

    # --------------------------------------------------------------- lifecycle

    def __enter__(self) -> Self:
        if self._entered:
            raise RuntimeError("SyncWatlowManager is not reusable after exit")
        self._entered = True
        stack = ExitStack()
        try:
            portal = (
                self._portal_override
                if self._portal_override is not None
                else stack.enter_context(SyncPortal())
            )
            mgr = WatlowManager(error_policy=self._error_policy)
            stack.enter_context(portal.wrap_async_context_manager(mgr))
            self._portal = portal
            self._mgr = mgr
            self._stack = stack
        except BaseException:
            stack.close()
            self._portal = None
            self._mgr = None
            raise
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        stack, self._stack = self._stack, None
        self._wrapped.clear()
        self._mgr = None
        self._portal = None
        if stack is not None:
            stack.__exit__(exc_type, exc, tb)

    # --------------------------------------------------------------- add/remove

    def add(
        self,
        name: str,
        source: SyncController | Controller | str | Transport,
        *,
        protocol: ProtocolKind = ProtocolKind.STDBUS,
        address: int = 1,
        serial_settings: SerialSettings | None = None,
        family: ControllerFamily = ControllerFamily.UNKNOWN,
    ) -> SyncController:
        """Blocking :meth:`WatlowManager.add`.

        Accepts a :class:`SyncController` as ``source`` in addition to
        the async shapes — the wrapper is unwrapped to the underlying
        :class:`Controller` before delegation.
        """
        mgr = self._require_mgr()
        async_source: Controller | str | Transport = unwrap_sync_controller(source)
        async_controller = self.portal.call(
            mgr.add,
            name,
            async_source,
            protocol=protocol,
            address=address,
            serial_settings=serial_settings,
            family=family,
        )
        wrapped = wrap_controller(async_controller, self.portal)
        self._wrapped[name] = wrapped
        return wrapped

    def remove(self, name: str) -> None:
        """Blocking :meth:`WatlowManager.remove`."""
        mgr = self._require_mgr()
        self._wrapped.pop(name, None)
        self.portal.call(mgr.remove, name)

    def get(self, name: str) -> SyncController:
        """Return the sync wrapper for the controller registered under ``name``."""
        cached = self._wrapped.get(name)
        if cached is not None:
            return cached
        mgr = self._require_mgr()
        async_controller = mgr.get(name)
        wrapped = wrap_controller(async_controller, self.portal)
        self._wrapped[name] = wrapped
        return wrapped

    def close(self) -> None:
        """Blocking :meth:`WatlowManager.close` — idempotent."""
        self._wrapped.clear()
        mgr = self._mgr
        if mgr is None:
            return
        portal = self._portal
        if portal is None:
            return
        portal.call(mgr.close)

    # --------------------------------------------------------------- concurrent I/O

    def poll(
        self,
        parameters: Sequence[str | int],
        *,
        names: Sequence[str] | None = None,
        instances: Sequence[int] = (1,),
    ) -> list[Sample]:
        """Blocking :meth:`WatlowManager.poll_many`."""
        mgr = self._require_mgr()
        return self.portal.call(
            mgr.poll_many,
            parameters,
            names=names,
            instances=instances,
        )

    def record_to_sink(
        self,
        *,
        parameters: Sequence[str | int],
        rate_hz: float,
        duration: float | None = None,
        sink: SyncSinkAdapter | SampleSink,
        names: Sequence[str] | None = None,
        instances: Sequence[int] = (1,),
        overflow: OverflowPolicy | None = None,
        buffer_size: int = 64,
        batch_size: int = 64,
        flush_interval: float = 1.0,
    ) -> AcquisitionSummary:
        """Record polled samples directly into a sink — one-call convenience.

        Combines :func:`watlowlib.sync.record` and
        :func:`watlowlib.sync.pipe` into a single blocking call. The
        manager's portal is reused for both legs so the recorder and
        the sink share an event loop. ``sink`` may be either a
        :class:`SyncSinkAdapter` (preferred, opened externally) or a
        bare async :class:`SampleSink` — in the latter case this
        method opens the sink against the manager's portal and closes
        it after the recording finishes.

        Returns the :class:`AcquisitionSummary` from
        :func:`watlowlib.sync.pipe`.
        """
        # Lazy imports — sink machinery pulls heavy deps (anyio sink
        # primitives, sqlite, etc.) and we want the surface importable
        # without that until the user reaches for streaming.
        from watlowlib.streaming.recorder import OverflowPolicy as _OverflowPolicy  # noqa: PLC0415
        from watlowlib.sync.recording import pipe, record  # noqa: PLC0415
        from watlowlib.sync.sinks import SyncSinkAdapter  # noqa: PLC0415

        self._require_mgr()
        active_overflow = overflow if overflow is not None else _OverflowPolicy.BLOCK
        portal = self.portal

        with ExitStack() as stack:
            sink_for_pipe: SyncSinkAdapter | SampleSink
            if isinstance(sink, SyncSinkAdapter):
                # Caller-owned sync wrapper — no open / close here.
                sink_for_pipe = sink
            else:
                # Bare async sink — wrap on this manager's portal so it
                # shares the recorder's event loop, and own the
                # open/close lifecycle through the ExitStack.
                wrapped = SyncSinkAdapter(sink, portal=portal)
                stack.enter_context(wrapped)
                sink_for_pipe = wrapped

            stream = stack.enter_context(
                record(
                    self,
                    parameters=parameters,
                    rate_hz=rate_hz,
                    duration=duration,
                    names=names,
                    instances=instances,
                    overflow=active_overflow,
                    buffer_size=buffer_size,
                    portal=portal,
                ),
            )
            return pipe(
                stream,
                sink_for_pipe,
                batch_size=batch_size,
                flush_interval=flush_interval,
                portal=portal,
            )

    def execute_each[T](
        self,
        op: Callable[[Controller], Awaitable[T]],
        names: Sequence[str] | None = None,
    ) -> dict[str, DeviceResult[T]]:
        """Blocking :meth:`WatlowManager.execute_each`.

        ``op`` receives the **async** :class:`Controller` so existing
        coroutines compose. If you have a sync helper, wrap it in an
        async stub or run it on the portal yourself.
        """
        mgr = self._require_mgr()
        return self.portal.call(mgr.execute_each, op, names)

    # --------------------------------------------------------------- internals

    def _require_mgr(self) -> WatlowManager:
        mgr = self._mgr
        if mgr is None:
            raise RuntimeError("SyncWatlowManager is not open")
        return mgr
