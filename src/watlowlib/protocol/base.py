"""Protocol seam: :class:`ProtocolKind` enum + :class:`ProtocolClient` Protocol.

The :class:`Session` holds a :class:`ProtocolClient` and dispatches every
command through ``execute(...)``. Variants are pure functions of
``(ctx, request)`` — the client owns the wire codec and the per-port
serialization (``lock``).

Standard Bus and Modbus RTU specialize the request type:

- :class:`watlowlib.protocol.stdbus.client.StdBusProtocolClient` is
  ``ProtocolClient[bytes, StdBusReply]`` because watlowlib owns the
  inner-payload codec; the variant produces raw bytes ready to be
  framed.
- :class:`watlowlib.protocol.modbus.client.ModbusProtocolClient` is
  ``ProtocolClient[ModbusOp, tuple[int, ...]]`` because ``anymodbus``
  owns the wire codec; handing it bytes would be a layer violation.

See ``docs/design.md`` §4.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    import anyio

__all__ = ["ProtocolClient", "ProtocolKind"]


class ProtocolKind(StrEnum):
    """Wire protocol selected for a session.

    ``AUTO`` triggers the conservative Std Bus → Modbus probe.
    """

    AUTO = "auto"
    STDBUS = "stdbus"
    MODBUS_RTU = "modbus_rtu"


@runtime_checkable
class ProtocolClient[Request_contra, Reply_co](Protocol):
    """Per-device protocol client.

    Implementations own the wire codec and the per-port lock. The
    :class:`watlowlib.devices.session.Session` is the only caller; it
    holds ``lock`` for the duration of a single command (request +
    reply).
    """

    @property
    def lock(self) -> anyio.Lock:
        """Per-client lock acquired by :meth:`Session.execute`.

        One lock per port — a single :class:`Session` serializes its
        own traffic, and :class:`watlowlib.manager.WatlowManager`
        enforces one protocol per port across sessions.
        """
        ...

    @property
    def disposed(self) -> bool:
        """Whether :meth:`dispose` has been called."""
        ...

    def dispose(self) -> None:
        """Mark the client unusable. Subsequent ``execute`` calls raise.

        Synchronous because dispose is called from teardown paths that
        don't always have an event loop. The client is responsible for
        closing its transport (or signalling the owning :class:`Session`
        to do so) — this method just trips the flag.
        """
        ...

    @property
    def kind(self) -> ProtocolKind:
        """The :class:`ProtocolKind` this client speaks."""
        ...

    async def execute(
        self,
        request: Request_contra,
        *,
        address: int,
        timeout: float | None = None,
        command_name: str = "",
    ) -> Reply_co:
        """Send ``request`` to ``address``, return the typed reply.

        ``address`` travels with every call so one client can serve
        multiple devices on a multi-drop RS-485 segment without
        re-construction. Std Bus accepts ``1..16``, Modbus RTU accepts
        ``1..247``.

        ``timeout`` overrides :attr:`watlowlib.config.DEFAULTS.io_timeout_s`
        for this call only. ``command_name`` is threaded into log
        events and error contexts; it is informational, not load-bearing
        for dispatch.
        """
        ...
