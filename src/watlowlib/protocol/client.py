"""Factory shell for :class:`ProtocolClient` construction.

``make_protocol_client`` is the single entry point used by
:func:`watlowlib.devices.factory.open_device` to build a client of the
right shape. ``AUTO`` is rejected here — the detector resolves it to a
concrete :class:`ProtocolKind` before constructing the client.

The factory takes a :class:`Transport` rather than reaching back into
``open_device`` so tests can drive any client over a
:class:`FakeTransport`. The Modbus branch expects a
:class:`watlowlib.protocol.modbus.transport.ModbusBusTransport` —
:mod:`anymodbus` owns its own serial handle, so the byte-level
:class:`Transport` methods are not used (see ``docs/design.md`` §4 and
the comment in ``protocol/modbus/transport.py``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from watlowlib.errors import WatlowConfigurationError
from watlowlib.protocol.base import ProtocolClient, ProtocolKind

if TYPE_CHECKING:
    from watlowlib.transport.base import Transport

__all__ = ["make_protocol_client"]


def make_protocol_client(
    kind: ProtocolKind,
    transport: Transport,
) -> ProtocolClient[Any, Any]:
    """Build an address-agnostic :class:`ProtocolClient` for ``kind`` over ``transport``.

    The returned client takes a destination address per
    :meth:`ProtocolClient.execute` call, so one client can serve every
    device on a multi-drop RS-485 segment.

    Args:
        kind: The wire protocol. ``AUTO`` is rejected here — the
            detector must resolve it to a concrete kind first.
        transport: An open or openable :class:`Transport`. Lifecycle is
            the caller's responsibility — the client does not call
            ``open()`` on construction. For ``MODBUS_RTU`` this must be
            a :class:`ModbusBusTransport`.

    Raises:
        WatlowConfigurationError: ``kind`` is ``AUTO`` (use the
            detector), or the ``transport`` shape doesn't match
            ``kind``.
    """
    if kind is ProtocolKind.STDBUS:
        # Imported lazily so the modbus / detect branches don't pull
        # the stdbus subpackage when they aren't needed.
        from watlowlib.protocol.stdbus.client import StdBusProtocolClient  # noqa: PLC0415

        return StdBusProtocolClient(transport)
    if kind is ProtocolKind.MODBUS_RTU:
        from watlowlib.protocol.modbus.client import ModbusProtocolClient  # noqa: PLC0415
        from watlowlib.protocol.modbus.transport import ModbusBusTransport  # noqa: PLC0415

        if not isinstance(transport, ModbusBusTransport):
            raise WatlowConfigurationError(
                "ProtocolKind.MODBUS_RTU requires a ModbusBusTransport "
                f"(got {type(transport).__name__}).",
            )
        bus_transport = transport
        return ModbusProtocolClient(
            slave_provider=bus_transport.bus.slave,
            port=bus_transport.label,
        )
    if kind is ProtocolKind.AUTO:
        raise WatlowConfigurationError(
            "ProtocolKind.AUTO must be resolved by the detector before reaching "
            "make_protocol_client.",
        )
    # StrEnum is closed; an unknown value would have failed at parse time.
    raise WatlowConfigurationError(f"unsupported protocol kind: {kind!r}")
