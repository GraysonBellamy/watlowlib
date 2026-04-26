"""Protocol layer — framing, parsing, and protocol-client adapters.

Standard Bus and Modbus RTU each have a full subpackage under here. The
shared :class:`ProtocolClient` Protocol and :class:`ProtocolKind` enum
live at this level. See ``docs/design.md`` §4.
"""

from __future__ import annotations

from watlowlib.protocol.base import ProtocolClient, ProtocolKind
from watlowlib.protocol.client import make_protocol_client

__all__ = [
    "ProtocolClient",
    "ProtocolKind",
    "make_protocol_client",
]
