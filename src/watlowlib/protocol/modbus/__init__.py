"""Modbus RTU adapter.

Thin wrapper over the in-house :mod:`anymodbus` package. The module
owns:

- :class:`ModbusOp` — typed instruction emitted by Modbus variants.
- :class:`ModbusProtocolClient` — :class:`ProtocolClient` for the
  Modbus wire.
- :class:`ModbusBusTransport` — :class:`Transport`-shaped adapter that
  hands the client a live :class:`anymodbus.Bus` on demand.

The codec for the Modbus PDU itself lives in :mod:`anymodbus`; this
package never touches wire bytes. Modbus variants emit a typed
:class:`ModbusOp`, not bytes — see ``docs/design.md`` §5.
"""

from __future__ import annotations

from watlowlib.protocol.modbus.client import ModbusProtocolClient
from watlowlib.protocol.modbus.errors import remap_modbus_exception
from watlowlib.protocol.modbus.ops import ModbusFn, ModbusOp
from watlowlib.protocol.modbus.tables import (
    ModbusEncoding,
    encoding_for,
)
from watlowlib.protocol.modbus.transport import ModbusBusTransport

__all__ = [
    "ModbusBusTransport",
    "ModbusEncoding",
    "ModbusFn",
    "ModbusOp",
    "ModbusProtocolClient",
    "encoding_for",
    "remap_modbus_exception",
]
