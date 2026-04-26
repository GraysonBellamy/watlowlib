"""Sync controller facade — portal-driven wrapper over :class:`Controller`.

Each :class:`SyncController` holds a reference to an async
:class:`~watlowlib.devices.controller.Controller` and a
:class:`~watlowlib.sync.portal.SyncPortal`; every public method is a
one-liner that hands the underlying coroutine to the portal.

The :class:`Watlow` namespace exposes a ``Watlow.open(...)`` context
manager that drives the async
:func:`~watlowlib.devices.factory.open_device` through the portal.

Design reference: ``docs/design.md`` §6.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING

from watlowlib.devices.factory import open_device
from watlowlib.protocol.base import ProtocolKind
from watlowlib.sync.portal import SyncPortal

if TYPE_CHECKING:
    from collections.abc import Generator, Sequence

    from watlowlib.commands.loop import PidGains
    from watlowlib.devices.controller import Controller
    from watlowlib.devices.loop import ControllerLoop
    from watlowlib.devices.models import (
        AlarmState,
        DeviceInfo,
        ParameterEntry,
        Reading,
    )
    from watlowlib.devices.session import Session
    from watlowlib.streaming.sample import Sample
    from watlowlib.transport.base import SerialSettings

__all__ = ["SyncController", "SyncControllerLoop", "Watlow"]


class SyncControllerLoop:
    """Blocking view over a single control loop (mirror of :class:`ControllerLoop`).

    Returned by :meth:`SyncController.loop`; never instantiated directly.
    Lifetime is bound to the parent :class:`SyncController` and its
    portal — closing the controller is the only cleanup needed.
    """

    __slots__ = ("_loop", "_portal")

    def __init__(self, async_loop: ControllerLoop, portal: SyncPortal) -> None:
        self._loop = async_loop
        self._portal = portal

    @property
    def number(self) -> int:
        """The 1-indexed loop number this view binds."""
        return self._loop.number

    def read_pv(self, *, timeout: float | None = None) -> Reading:
        """Blocking :meth:`ControllerLoop.read_pv`."""
        return self._portal.call(self._loop.read_pv, timeout=timeout)

    def read_setpoint(self, *, timeout: float | None = None) -> Reading:
        """Blocking :meth:`ControllerLoop.read_setpoint`."""
        return self._portal.call(self._loop.read_setpoint, timeout=timeout)

    def set_setpoint(
        self,
        value: float,
        *,
        confirm: bool = False,
        timeout: float | None = None,
    ) -> Reading:
        """Blocking :meth:`ControllerLoop.set_setpoint`."""
        return self._portal.call(
            self._loop.set_setpoint,
            value,
            confirm=confirm,
            timeout=timeout,
        )

    def read_output(self) -> Reading:
        """Blocking :meth:`ControllerLoop.read_output`."""
        return self._portal.call(self._loop.read_output)

    def read_pid(self) -> PidGains:
        """Blocking :meth:`ControllerLoop.read_pid`."""
        return self._portal.call(self._loop.read_pid)

    def write_pid(self, gains: PidGains, *, confirm: bool = False) -> PidGains:
        """Blocking :meth:`ControllerLoop.write_pid`."""
        return self._portal.call(self._loop.write_pid, gains, confirm=confirm)

    def read_alarms(self) -> AlarmState:
        """Blocking :meth:`ControllerLoop.read_alarms`."""
        return self._portal.call(self._loop.read_alarms)


class SyncController:
    """Blocking facade over :class:`watlowlib.devices.controller.Controller`.

    Instances are produced by :meth:`Watlow.open` or yielded by the
    sync manager; users do not call this constructor directly.
    """

    def __init__(self, controller: Controller, portal: SyncPortal) -> None:
        self._ctl = controller
        self._portal = portal

    # ------------------------------------------------------------------ props

    @property
    def session(self) -> Session:
        """Underlying async :class:`Session` (advanced escape-hatch)."""
        return self._ctl.session

    @property
    def loops(self) -> int | None:
        """Cached loop count — passes through :attr:`Controller.loops`."""
        return self._ctl.loops

    @property
    def portal(self) -> SyncPortal:
        """The :class:`SyncPortal` this controller routes coroutines through."""
        return self._portal

    # ------------------------------------------------------------------ loop view

    def loop(self, n: int) -> SyncControllerLoop:
        """Return a sync sub-facade bound to loop ``n`` (1-indexed)."""
        return SyncControllerLoop(self._ctl.loop(n), self._portal)

    # ------------------------------------------------------------------ identity

    def identify(self, *, timeout: float | None = None) -> DeviceInfo:
        """Blocking :meth:`Controller.identify`."""
        return self._portal.call(self._ctl.identify, timeout=timeout)

    # ------------------------------------------------------------------ generic parameter

    def read_parameter(
        self,
        name_or_id: str | int,
        *,
        instance: int = 1,
        timeout: float | None = None,
    ) -> ParameterEntry:
        """Blocking :meth:`Controller.read_parameter`."""
        return self._portal.call(
            self._ctl.read_parameter,
            name_or_id,
            instance=instance,
            timeout=timeout,
        )

    def write_parameter(
        self,
        name_or_id: str | int,
        value: float | int | str,
        *,
        instance: int = 1,
        confirm: bool = False,
        timeout: float | None = None,
    ) -> ParameterEntry:
        """Blocking :meth:`Controller.write_parameter`."""
        return self._portal.call(
            self._ctl.write_parameter,
            name_or_id,
            value,
            instance=instance,
            confirm=confirm,
            timeout=timeout,
        )

    # ------------------------------------------------------------------ workhorses

    def read_pv(self, *, instance: int = 1, timeout: float | None = None) -> Reading:
        """Blocking :meth:`Controller.read_pv`."""
        return self._portal.call(self._ctl.read_pv, instance=instance, timeout=timeout)

    def read_setpoint(self, *, instance: int = 1, timeout: float | None = None) -> Reading:
        """Blocking :meth:`Controller.read_setpoint`."""
        return self._portal.call(self._ctl.read_setpoint, instance=instance, timeout=timeout)

    def set_setpoint(
        self,
        value: float,
        *,
        instance: int = 1,
        confirm: bool = False,
        timeout: float | None = None,
    ) -> Reading:
        """Blocking :meth:`Controller.set_setpoint`."""
        return self._portal.call(
            self._ctl.set_setpoint,
            value,
            instance=instance,
            confirm=confirm,
            timeout=timeout,
        )

    # ------------------------------------------------------------------ streaming

    def poll(
        self,
        parameters: Sequence[str | int],
        *,
        names: Sequence[str] | None = None,
        instances: Sequence[int] = (1,),
    ) -> list[Sample]:
        """Blocking :meth:`Controller.poll`."""
        return self._portal.call(
            self._ctl.poll,
            parameters,
            names=names,
            instances=instances,
        )

    # ------------------------------------------------------------------ lifecycle

    def close(self) -> None:
        """Blocking :meth:`Controller.aclose`. Idempotent."""
        if not self._portal.running:
            return
        self._portal.call(self._ctl.aclose)


def wrap_controller(controller: Controller, portal: SyncPortal) -> SyncController:
    """Return a :class:`SyncController` wrapping ``controller`` on ``portal``.

    Package-private helper used by :class:`SyncWatlowManager`.
    """
    return SyncController(controller, portal)


def unwrap_sync_controller[T](source: T | SyncController) -> T | Controller:
    """Return the async :class:`Controller` inside ``source`` if wrapped.

    Package-private helper used by :class:`SyncWatlowManager`.
    """
    if isinstance(source, SyncController):
        return source._ctl  # pyright: ignore[reportPrivateUsage]
    return source


class Watlow:
    """Namespace for the sync controller entry point.

    Use :meth:`Watlow.open` as a context manager::

        from watlowlib.sync import Watlow

        with Watlow.open("/dev/ttyUSB0") as ctl:
            print(ctl.read_pv())
    """

    @staticmethod
    @contextmanager
    def open(
        port: str,
        *,
        protocol: ProtocolKind | None = None,
        address: int = 1,
        serial_settings: SerialSettings | None = None,
        portal: SyncPortal | None = None,
    ) -> Generator[SyncController]:
        """Open a sync :class:`SyncController` scoped to a ``with`` block.

        Mirrors :func:`watlowlib.open_device` parameter-for-parameter
        (modulo the portal plumbing). The sync CM drives the async
        factory through a :class:`SyncPortal`; the portal is created
        per-call unless one is passed in via ``portal=``.
        """
        effective_protocol = protocol if protocol is not None else ProtocolKind.STDBUS

        with ExitStack() as stack:
            active_portal = portal if portal is not None else stack.enter_context(SyncPortal())
            controller = active_portal.call(
                open_device,
                port,
                protocol=effective_protocol,
                address=address,
                serial_settings=serial_settings,
            )
            # ``open_device`` returns a controller that may or may not
            # already be open: AUTO returned by the detector is open;
            # STDBUS / MODBUS_RTU need ``__aenter__`` to run open().
            # ``Controller.__aenter__`` short-circuits when already open
            # and returns ``self``; calling it through the portal here
            # gives us the same lifecycle as ``async with`` does.
            active_portal.call(controller.__aenter__)
            try:
                yield wrap_controller(controller, active_portal)
            finally:
                # Close the underlying transport through the portal.
                active_portal.call(controller.aclose)
