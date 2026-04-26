"""Multi-controller orchestrator — :class:`WatlowManager`.

The manager coordinates many :class:`~watlowlib.devices.controller.Controller`
instances across one or more serial ports. Operations on different
physical ports run concurrently through :func:`anyio.create_task_group`;
operations on the same port serialise through that port's shared
:class:`~watlowlib.protocol.base.ProtocolClient` lock.

Port identity is **canonicalised** before comparison so a controller
referenced via both ``/dev/ttyUSB0`` and ``/dev/serial/by-id/...``
(or ``COM3`` and ``com3`` on Windows) collapses to one client —
critical for the single-in-flight invariant. Pre-built
:class:`Transport` sources use the object's :func:`id` as the key so
caller-owned transports aren't accidentally shared.

**Per-port protocol lock.** The same RS-485 segment can only carry one
wire protocol at a time. The manager locks the port to the protocol
of the first device added; subsequent ``add(...)`` calls on that port
must use the same protocol or raise :class:`WatlowConfigurationError`.

Resource lifecycle goes through an internal tracking structure that
unwinds LIFO on :meth:`close` or ``__aexit__``. Per-port clients are
ref-counted so the last :meth:`remove` on a shared port triggers the
transport close. Pre-built :class:`Controller` sources have no port
entry — the caller retains lifecycle ownership.

Design reference: ``docs/design.md`` §6.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio

from watlowlib._logging import get_logger
from watlowlib.devices.controller import Controller
from watlowlib.devices.session import Session
from watlowlib.errors import (
    ErrorContext,
    WatlowConfigurationError,
    WatlowConnectionError,
    WatlowError,
    WatlowValidationError,
)
from watlowlib.protocol.base import ProtocolClient, ProtocolKind
from watlowlib.protocol.client import make_protocol_client
from watlowlib.registry.families import ControllerFamily
from watlowlib.registry.parameters import PARAMETERS
from watlowlib.streaming._poll import poll_controller
from watlowlib.transport.base import SerialSettings
from watlowlib.transport.serial import SerialTransport

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence
    from types import TracebackType
    from typing import Self

    from watlowlib.streaming.sample import Sample
    from watlowlib.transport.base import Transport

__all__ = [
    "DeviceResult",
    "ErrorPolicy",
    "WatlowManager",
]


_logger = get_logger("manager")


class ErrorPolicy(Enum):
    """How the manager surfaces per-device failures.

    Under :attr:`RAISE`, the manager collects every controller's result
    and — if any call failed — raises an :class:`ExceptionGroup`
    containing the per-device exceptions after the task group joins.
    Under :attr:`RETURN`, each controller produces a
    :class:`DeviceResult` and the caller inspects ``.error`` per entry.
    """

    RAISE = "raise"
    RETURN = "return"


@dataclass(frozen=True, slots=True)
class DeviceResult[T]:
    """Per-device result container — value **or** error, never both.

    :attr:`protocol` is populated from the controller's session so error
    rows from the streaming layer can still record which protocol
    produced the failure.
    """

    value: T | None
    error: WatlowError | None
    protocol: ProtocolKind | None = None

    @property
    def ok(self) -> bool:
        """``True`` when the controller produced a value (``error is None``)."""
        return self.error is None


# ---------------------------------------------------------------------------
# Port canonicalization
# ---------------------------------------------------------------------------


_WINDOWS_DEVICE_PREFIX = "\\\\.\\"


def _canonical_port_key(port: str) -> str:
    r"""Collapse equivalent port names to a single key.

    POSIX: follows symlinks via :meth:`Path.resolve` so
    ``/dev/ttyUSB0`` and ``/dev/serial/by-id/...-if00-port0`` resolve
    to the same physical device. Falls back to the raw string if the
    path doesn't exist (useful under test fixtures).

    Windows: strips the ``\\.\`` device-namespace prefix and uppercases,
    so ``COM3`` / ``com3`` / ``\\.\COM3`` all match.
    """
    if sys.platform == "win32":
        return port.removeprefix(_WINDOWS_DEVICE_PREFIX).upper()
    path = Path(port)
    return str(path.resolve(strict=False)) if path.exists() else port


# ---------------------------------------------------------------------------
# Internal tracking structures
# ---------------------------------------------------------------------------


def _empty_refs() -> set[str]:
    return set()


@dataclass(slots=True)
class _PortEntry:
    """Ref-counted per-port resources shared across controllers on the bus.

    ``client`` is the shared protocol client; every controller on the
    same bus runs through it so its lock serialises I/O across
    controller objects. ``protocol`` is set on first use and locks the
    port to that wire protocol.
    """

    key: str
    transport: Transport
    client: ProtocolClient[Any, Any] | None
    owns_transport: bool
    protocol: ProtocolKind | None = None
    refs: set[str] = field(default_factory=_empty_refs)


@dataclass(slots=True)
class _DeviceEntry:
    """One managed :class:`Controller` + its port ref.

    ``port_key`` is ``None`` when ``source`` was a pre-built
    :class:`Controller` and the caller retains full lifecycle ownership;
    the manager's teardown path is a no-op for those entries.
    """

    name: str
    controller: Controller
    port_key: str | None


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class WatlowManager:
    """Coordinator for many controllers across one or more serial ports.

    Operations run concurrently across different physical ports (via
    :func:`anyio.create_task_group`) and serialise on the same-port
    client lock. Per-controller failures are surfaced per
    :attr:`error_policy`:

    - :attr:`ErrorPolicy.RAISE`: the manager still collects results
      from every controller, then raises an :class:`ExceptionGroup` if
      any failed.
    - :attr:`ErrorPolicy.RETURN`: per-name :class:`DeviceResult`
      containers carry ``.value`` or ``.error``.

    Usage::

        async with WatlowManager() as mgr:
            await mgr.add("ctl1", "/dev/ttyUSB0", address=1)
            await mgr.add("ctl2", "/dev/ttyUSB1", address=1)
            samples = await mgr.poll(["process_value", "setpoint"])
    """

    def __init__(self, *, error_policy: ErrorPolicy = ErrorPolicy.RAISE) -> None:
        self._error_policy = error_policy
        self._devices: dict[str, _DeviceEntry] = {}
        self._ports: dict[str, _PortEntry] = {}
        self._state_lock = anyio.Lock()
        self._closed = False

    # -------------------------------------------------------------------- props

    @property
    def error_policy(self) -> ErrorPolicy:
        """The :class:`ErrorPolicy` this manager was constructed with."""
        return self._error_policy

    @property
    def names(self) -> tuple[str, ...]:
        """Insertion-ordered tuple of managed controller names."""
        return tuple(self._devices.keys())

    @property
    def closed(self) -> bool:
        """``True`` once :meth:`close` has been called."""
        return self._closed

    # --------------------------------------------------------- context manager

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        del exc_type, exc, tb
        await self.close()

    # --------------------------------------------------------------- add/remove

    async def add(
        self,
        name: str,
        source: Controller | str | Transport,
        *,
        protocol: ProtocolKind = ProtocolKind.STDBUS,
        address: int = 1,
        serial_settings: SerialSettings | None = None,
        family: ControllerFamily = ControllerFamily.UNKNOWN,
    ) -> Controller:
        """Register and open a controller under ``name``.

        The ``source`` discriminates lifecycle ownership:

        - :class:`Controller` — pre-built (via
          :func:`watlowlib.open_device` outside the manager). The
          manager only tracks the name mapping; it does *not* take
          lifecycle ownership.
        - ``str`` — serial port path (``"/dev/ttyUSB0"``, ``"COM3"``).
          The manager creates a transport, canonicalises the port key,
          and shares the transport + client across controllers on the
          same bus. Mixing Std Bus and Modbus on a shared physical
          port is refused; one serial link has one active protocol.
        - :class:`Transport` — duck-typed transport. The manager builds
          a session against it but does *not* take transport ownership.

        Args:
            name: Unique manager-level identifier.
            source: One of the three lifecycle shapes above.
            protocol: Wire protocol (``STDBUS`` or ``MODBUS_RTU``).
                Ignored when ``source`` is a pre-built :class:`Controller`.
                ``AUTO`` is rejected — open the controller via
                :func:`open_device` first and register the resulting
                :class:`Controller`.
            address: Bus address. Std Bus accepts ``1..16``; Modbus RTU
                accepts ``1..247``.
            serial_settings: Override default serial framing. Only
                honoured when ``source`` is a port-string.
            family: Best-known :class:`ControllerFamily`. Defaults to
                :attr:`ControllerFamily.UNKNOWN`; the session uses this
                for capability priors and is updated by ``identify()``
                on the returned controller.

        Returns:
            The opened :class:`Controller`.

        Raises:
            WatlowValidationError: ``name`` already exists or an
                invalid combination of kwargs was supplied.
            WatlowConfigurationError: protocol mismatches an existing
                lock on the same port, or ``protocol=AUTO``.
            WatlowConnectionError: Manager is closed.
        """
        async with self._state_lock:
            self._check_open()
            if name in self._devices:
                raise WatlowValidationError(
                    f"manager: name {name!r} already in use",
                    context=ErrorContext(address=address),
                )
            if serial_settings is not None and not isinstance(source, str):
                raise WatlowValidationError(
                    "manager.add(serial_settings=...) only applies to string port "
                    "sources; pre-built Transport / Controller carry their own settings",
                )

            port_key, port_entry, controller = await self._resolve_source(
                source,
                protocol=protocol,
                address=address,
                serial_settings=serial_settings,
                family=family,
            )

            self._devices[name] = _DeviceEntry(
                name=name,
                controller=controller,
                port_key=port_key,
            )
            if port_entry is not None:
                port_entry.refs.add(name)

            _logger.info(
                "manager.add device_name=%s port_key=%s protocol=%s address=%s",
                name,
                port_key,
                controller.session.protocol_kind.value,
                controller.session.address,
            )
            return controller

    async def remove(self, name: str) -> None:
        """Unregister and close the controller named ``name``.

        If ``name`` was the last controller on a shared port, the
        transport for that port is closed too. A pre-built
        :class:`Controller` source is only dropped from the manager's
        registry — the caller retains lifecycle ownership.
        """
        async with self._state_lock:
            self._check_open()
            if name not in self._devices:
                raise WatlowValidationError(
                    f"manager: no controller named {name!r}",
                )
            entry = self._devices.pop(name)
            await self._teardown_device(entry)
            _logger.info("manager.remove device_name=%s", name)

    def get(self, name: str) -> Controller:
        """Return the controller registered under ``name``."""
        try:
            return self._devices[name].controller
        except KeyError:
            raise WatlowValidationError(
                f"manager: no controller named {name!r}",
            ) from None

    async def close(self) -> None:
        """Tear down every managed controller and port (LIFO)."""
        async with self._state_lock:
            if self._closed:
                return
            for name in reversed(list(self._devices.keys())):
                entry = self._devices.pop(name)
                try:
                    await self._teardown_device(entry)
                except Exception as err:
                    _logger.warning(
                        "manager.close_device_failed device_name=%s error=%r",
                        name,
                        err,
                    )
            self._closed = True

    # ----------------------------------------------------------- concurrent I/O

    async def poll(
        self,
        parameters: Sequence[str | int],
        *,
        names: Sequence[str] | None = None,
        instances: Sequence[int] = (1,),
    ) -> list[Sample]:
        """Poll every (or named) controller concurrently across ports.

        Returns a flat list of :class:`Sample` — one per (device,
        parameter, instance) read that succeeded. Failed reads are
        dropped from the list and logged at WARN. Cross-port reads run
        concurrently; same-port reads serialise on the shared client
        lock.

        This satisfies the :class:`watlowlib.streaming.PollSource`
        Protocol so a manager can drive :func:`watlowlib.streaming.record`
        directly.
        """
        targets = self._resolve_names(names)
        groups = self._group_by_port(targets)

        result_lock = anyio.Lock()
        all_samples: list[Sample] = []

        async def _run_group(member_names: list[str]) -> None:
            local: list[Sample] = []
            for member in member_names:
                entry = self._devices[member]
                local.extend(
                    await poll_controller(
                        entry.controller,
                        name=member,
                        parameters=parameters,
                        instances=instances,
                    ),
                )
            async with result_lock:
                all_samples.extend(local)

        async with anyio.create_task_group() as tg:
            for member_names in groups.values():
                tg.start_soon(_run_group, member_names)

        return all_samples

    async def execute_each[T](
        self,
        op: Callable[[Controller], Awaitable[T]],
        names: Sequence[str] | None = None,
    ) -> dict[str, DeviceResult[T]]:
        """Run ``op(controller)`` on every (or named) controller concurrently.

        General-purpose dispatcher used for cross-device snapshots
        (``identify``, ``read_pid``, etc.) where each controller runs
        the same coroutine and the result is keyed by name. Cross-port
        runs concurrently; same-port serialises on the shared client
        lock.

        Under :attr:`ErrorPolicy.RAISE` the method still returns a
        complete result mapping but re-raises an :class:`ExceptionGroup`
        of every per-device error after the task group joins.
        """
        targets = self._resolve_names(names)
        groups = self._group_by_port(targets)
        results: dict[str, DeviceResult[T]] = {}
        errors: list[WatlowError] = []
        result_lock = anyio.Lock()

        async def _run_group(member_names: list[str]) -> None:
            for member in member_names:
                entry = self._devices[member]
                controller = entry.controller
                protocol = controller.session.protocol_kind
                try:
                    value = await op(controller)
                except WatlowError as err:
                    async with result_lock:
                        results[member] = DeviceResult(
                            value=None,
                            error=err,
                            protocol=protocol,
                        )
                        errors.append(err)
                else:
                    async with result_lock:
                        results[member] = DeviceResult(
                            value=value,
                            error=None,
                            protocol=protocol,
                        )

        async with anyio.create_task_group() as tg:
            for member_names in groups.values():
                tg.start_soon(_run_group, member_names)

        if self._error_policy is ErrorPolicy.RAISE and errors:
            raise ExceptionGroup("manager.execute_each: one or more controllers failed", errors)
        return results

    # ----------------------------------------------------------------- internals

    def _check_open(self) -> None:
        if self._closed:
            raise WatlowConnectionError("manager is closed")

    def _resolve_names(self, names: Sequence[str] | None) -> tuple[str, ...]:
        if names is None:
            return tuple(self._devices.keys())
        targets = tuple(names)
        unknown = [n for n in targets if n not in self._devices]
        if unknown:
            raise WatlowValidationError(
                f"manager: unknown controller name(s) {sorted(unknown)!r}",
            )
        return targets

    def _group_by_port(self, names: Sequence[str]) -> dict[str, list[str]]:
        """Group target names by canonical port key for concurrent dispatch."""
        groups: dict[str, list[str]] = {}
        for n in names:
            entry = self._devices[n]
            port_key = entry.port_key if entry.port_key is not None else f"solo:{n}"
            groups.setdefault(port_key, []).append(n)
        return groups

    async def _resolve_source(
        self,
        source: Controller | str | Transport,
        *,
        protocol: ProtocolKind,
        address: int,
        serial_settings: SerialSettings | None,
        family: ControllerFamily,
    ) -> tuple[str | None, _PortEntry | None, Controller]:
        """Map ``source`` to ``(port_key, port_entry, controller)``."""
        if isinstance(source, Controller):
            return None, None, source

        if isinstance(source, str):
            port_key = _canonical_port_key(source)
        else:
            # Duck-typed Transport (anything else satisfying the Protocol).
            port_key = f"transport:{id(source)}"

        port_entry = self._ports.get(port_key)
        fresh_port = port_entry is None
        if port_entry is None:
            port_entry = self._build_port_entry(
                source,
                port_key=port_key,
                protocol=protocol,
                serial_settings=serial_settings,
            )
            self._ports[port_key] = port_entry

        try:
            controller = await self._open_controller_on_port(
                port_entry,
                protocol=protocol,
                address=address,
                serial_settings=serial_settings if isinstance(source, str) else None,
                family=family,
            )
        except BaseException:
            # Cleanup must never replace the original exception (which
            # may be ``Cancelled``). Swallow any error from teardown so
            # the original raise wins.
            if fresh_port and not port_entry.refs:
                try:
                    await self._maybe_teardown_port(port_key, port_entry)
                except BaseException as cleanup_err:
                    _logger.warning(
                        "manager.add_cleanup_failed port_key=%s error=%r",
                        port_key,
                        cleanup_err,
                    )
            raise
        return port_key, port_entry, controller

    def _build_port_entry(
        self,
        source: str | Transport,
        *,
        port_key: str,
        protocol: ProtocolKind,
        serial_settings: SerialSettings | None,
    ) -> _PortEntry:
        """Construct a fresh :class:`_PortEntry` for ``source``."""
        if isinstance(source, str):
            settings = serial_settings or SerialSettings(port=source)
            transport: Transport
            if protocol is ProtocolKind.MODBUS_RTU:
                # Lazy import — keep the Std-Bus path off the anymodbus
                # dep graph for users who never reach for Modbus.
                from watlowlib.protocol.modbus.transport import (  # noqa: PLC0415
                    ModbusBusTransport,
                )

                transport = ModbusBusTransport(settings)
            elif protocol is ProtocolKind.STDBUS:
                transport = SerialTransport(settings)
            else:
                raise WatlowConfigurationError(
                    "manager.add: ProtocolKind.AUTO is not supported here; "
                    "open the controller with open_device(..., protocol=AUTO) "
                    "and register the resulting Controller via manager.add(name, controller)",
                )
            owns_transport = True
        else:
            transport = source
            owns_transport = False
        return _PortEntry(
            key=port_key,
            transport=transport,
            client=None,
            owns_transport=owns_transport,
        )

    async def _open_controller_on_port(
        self,
        port_entry: _PortEntry,
        *,
        protocol: ProtocolKind,
        address: int,
        serial_settings: SerialSettings | None,
        family: ControllerFamily,
    ) -> Controller:
        """Build a :class:`Controller` against ``port_entry``'s shared client.

        Mirrors :func:`watlowlib.devices.factory.open_controller` but
        reuses the per-port :class:`ProtocolClient` so all controllers
        on the bus share one I/O-serialising lock.
        """
        if protocol is ProtocolKind.AUTO:
            raise WatlowConfigurationError(
                "manager.add: ProtocolKind.AUTO is not supported here; "
                "open the controller with open_device(..., protocol=AUTO) "
                "and register the resulting Controller via manager.add(name, controller)",
            )

        if port_entry.protocol is not None and port_entry.protocol is not protocol:
            raise WatlowConfigurationError(
                "manager.add: cannot mix Std Bus and Modbus sessions on the same port",
                context=ErrorContext(
                    protocol=protocol,
                    port=port_entry.transport.label,
                ),
            )

        transport = port_entry.transport
        if not transport.is_open:
            await transport.open()

        if port_entry.client is None:
            port_entry.client = make_protocol_client(
                protocol,
                transport,
                address=address,
            )
            port_entry.protocol = protocol

        settings = serial_settings or SerialSettings(port=transport.label)
        session = Session(
            port_entry.client,
            registry=PARAMETERS,
            family=family,
            address=address,
            port=transport.label,
        )
        return Controller(session, transport, serial_settings=settings)

    async def _teardown_device(self, entry: _DeviceEntry) -> None:
        """Release a controller's port ref, closing the transport on last ref.

        Calling :meth:`Controller.aclose` would close the underlying
        transport, which is shared across controllers on one RS-485
        bus. Instead, the manager releases the port ref and only
        closes the transport via :meth:`_maybe_teardown_port` once no
        controllers remain on that port. Pre-built :class:`Controller`
        sources have no port entry — the caller keeps full lifecycle
        responsibility.
        """
        if entry.port_key is None:
            return
        port_entry = self._ports.get(entry.port_key)
        if port_entry is None:
            return
        port_entry.refs.discard(entry.name)
        if not port_entry.refs:
            await self._maybe_teardown_port(entry.port_key, port_entry)

    async def _maybe_teardown_port(self, port_key: str, port_entry: _PortEntry) -> None:
        if port_entry.client is not None and not port_entry.client.disposed:
            try:
                port_entry.client.dispose()
            except Exception as err:
                _logger.warning(
                    "manager.dispose_client_failed port_key=%s error=%r",
                    port_key,
                    err,
                )
        if port_entry.owns_transport:
            try:
                if port_entry.transport.is_open:
                    await port_entry.transport.close()
            except Exception as err:
                _logger.warning(
                    "manager.close_port_failed port_key=%s error=%r",
                    port_key,
                    err,
                )
        self._ports.pop(port_key, None)
