"""Transport layer — moves bytes; knows nothing about Watlow.

The :class:`Transport` Protocol is the structural interface every
backend implements. :class:`SerialSettings` is the port-configuration
dataclass consumed by :class:`SerialTransport`. Tests use
:class:`FakeTransport` instead.

See ``docs/design.md`` §3 / §4.
"""

from __future__ import annotations

from watlowlib.transport.base import (
    ByteSize,
    Parity,
    SerialSettings,
    StopBits,
    Transport,
)
from watlowlib.transport.fake import FakeSlave, FakeTransport, ScriptedReply
from watlowlib.transport.serial import SerialTransport

__all__ = [
    "ByteSize",
    "FakeSlave",
    "FakeTransport",
    "Parity",
    "ScriptedReply",
    "SerialSettings",
    "SerialTransport",
    "StopBits",
    "Transport",
]
