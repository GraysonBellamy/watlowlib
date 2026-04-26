"""Watlow Standard Bus protocol — BACnet MS/TP framing + Watlow payload.

This subpackage exposes the codec primitives so reverse-engineering
tools and offline decode utilities can use them directly. The
:class:`StdBusProtocolClient` sits on top, and the high-level
:class:`watlowlib.devices.controller.Controller` facade dispatches
through it via :class:`watlowlib.devices.session.Session`.
"""

from __future__ import annotations

from watlowlib.protocol.stdbus._crc import (
    data_crc16,
    data_crc16_le_bytes,
    header_crc8,
)
from watlowlib.protocol.stdbus.client import StdBusProtocolClient
from watlowlib.protocol.stdbus.framing import (
    ADDR_OFFSET,
    HOST_MAC,
    PREAMBLE,
    Frame,
    FrameError,
    FrameType,
    addr_to_mac,
    decode_frame,
    encode_frame,
    mac_to_addr,
)
from watlowlib.protocol.stdbus.payload import (
    DIR_REQUEST,
    DIR_RESPONSE,
    FN_READ,
    FN_WRITE,
    DataType,
    ErrorCode,
    ErrorResponse,
    ReadRequest,
    ReadResponse,
    StdBusError,
    WriteRequest,
    WriteResponse,
    decode_payload,
    encode_read_request,
    encode_write_request,
    join_param,
    raise_for_error_code,
    split_param,
)
from watlowlib.protocol.stdbus.tlv import decode_value, encode_value
from watlowlib.protocol.stdbus.types import StdBusFrame, StdBusReply, StdBusReplyPayload

__all__ = [
    "ADDR_OFFSET",
    "DIR_REQUEST",
    "DIR_RESPONSE",
    "FN_READ",
    "FN_WRITE",
    "HOST_MAC",
    "PREAMBLE",
    "DataType",
    "ErrorCode",
    "ErrorResponse",
    "Frame",
    "FrameError",
    "FrameType",
    "ReadRequest",
    "ReadResponse",
    "StdBusError",
    "StdBusFrame",
    "StdBusProtocolClient",
    "StdBusReply",
    "StdBusReplyPayload",
    "WriteRequest",
    "WriteResponse",
    "addr_to_mac",
    "data_crc16",
    "data_crc16_le_bytes",
    "decode_frame",
    "decode_payload",
    "decode_value",
    "encode_frame",
    "encode_read_request",
    "encode_value",
    "encode_write_request",
    "header_crc8",
    "join_param",
    "mac_to_addr",
    "raise_for_error_code",
    "split_param",
]
