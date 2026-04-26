"""Std Bus typed reply shape returned by :class:`StdBusProtocolClient.execute`.

The reply preserves the full inner-payload object and the surrounding
frame so command variants can decode without a second pass through the
wire. ``raw_frame`` is the on-wire bytes (preamble through data CRC)
for diagnostics.
"""

from __future__ import annotations

from dataclasses import dataclass

from watlowlib.protocol.stdbus.framing import Frame
from watlowlib.protocol.stdbus.payload import (
    ErrorResponse,
    ReadRequest,
    ReadResponse,
    WriteRequest,
    WriteResponse,
)

__all__ = ["StdBusFrame", "StdBusReply", "StdBusReplyPayload"]

#: Decoded inner payload — every shape ``decode_payload`` can produce.
type StdBusReplyPayload = ReadResponse | WriteResponse | ReadRequest | WriteRequest | ErrorResponse

# Re-export Frame under the design's preferred name; aligns callers
# that use ``StdBusFrame`` with §5 of design.md.
StdBusFrame = Frame


@dataclass(frozen=True, slots=True)
class StdBusReply:
    """Result of a single :meth:`StdBusProtocolClient.execute` call.

    Attributes:
        frame: Decoded outer BACnet MS/TP frame (frame_type, dst, src,
            payload).
        payload: Decoded inner Watlow payload — read response, write
            response, or error.
        raw_frame: The complete on-wire bytes (preamble through data
            CRC). Stored for diagnostics; used by the session's WARNING
            log path on errors.
    """

    frame: StdBusFrame
    payload: StdBusReplyPayload
    raw_frame: bytes
