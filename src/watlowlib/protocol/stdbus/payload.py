"""Watlow Standard Bus inner payload codec.

The payload nested inside the BACnet MS/TP outer frame
(:mod:`watlowlib.protocol.stdbus.framing`) is a tagged-attribute
service that resembles CIP common services in spirit, with Watlow-
specific service codes::

    request   ::= 01 SERVICE [MODE] CLASS MEMBER INSTANCE [VALUE]
    response  ::= 02 SERVICE_OR_ERROR ...

    SERVICE = 0x03 (read) | 0x04 (write)
    ERROR   = SERVICE_OR_ERROR with high bit set; payload = 02 ERROR
    VALUE   = TYPE_TAG [LENGTH] DATA

``MODE`` only appears in **read requests** as the byte after ``0x03``.
For all normal use, ``MODE = 0x01`` (single-attribute read). See
``docs/protocol-stdbus-findings.md`` ("Block / multi-parameter reads")
for the ``MODE = 0x02`` (Get_Attributes_All) shape, which is partially
decoded and not yet exposed.

Confirmed against a live EZ-ZONE PM3 (2026-04-25); every tag and error
code below has at least one captured wire fixture in
``tests/test_codec.py``. The TLV codec lives in
:mod:`watlowlib.protocol.stdbus.tlv`; selector / direction constants
and :class:`ErrorCode` live in
:mod:`watlowlib.protocol.stdbus.tables`.
"""

from __future__ import annotations

from dataclasses import dataclass

from watlowlib.errors import (
    ErrorContext,
    WatlowNoSuchAttributeError,
    WatlowNoSuchInstanceError,
    WatlowNoSuchObjectError,
    WatlowProtocolError,
)
from watlowlib.protocol.stdbus.tables import (
    DIR_REQUEST,
    DIR_RESPONSE,
    FN_READ,
    FN_WRITE,
    ErrorCode,
)
from watlowlib.protocol.stdbus.tlv import (
    DataType,
    decode_value,
    encode_value,
)

__all__ = [
    "DIR_REQUEST",
    "DIR_RESPONSE",
    "FN_READ",
    "FN_WRITE",
    "DataType",
    "ErrorCode",
    "ErrorResponse",
    "ReadRequest",
    "ReadResponse",
    "StdBusError",
    "WriteRequest",
    "WriteResponse",
    "decode_payload",
    "encode_read_request",
    "encode_write_request",
    "join_param",
    "raise_for_error_code",
    "split_param",
]


@dataclass(frozen=True, slots=True)
class ReadRequest:
    """A decoded inner read request."""

    cls: int
    member: int
    instance: int


@dataclass(frozen=True, slots=True)
class WriteRequest:
    """A decoded inner write request."""

    cls: int
    member: int
    instance: int
    value: float | int | str
    type_tag: int  # DataType


@dataclass(frozen=True, slots=True)
class ReadResponse:
    """A decoded inner read response."""

    cls: int
    member: int
    instance: int
    value: float | int | str
    type_tag: int


@dataclass(frozen=True, slots=True)
class WriteResponse:
    """A decoded inner write response."""

    cls: int
    member: int
    instance: int
    value: float | int | str
    type_tag: int


@dataclass(frozen=True, slots=True)
class ErrorResponse:
    """A decoded inner error response."""

    code: int  # ErrorCode value


class StdBusError(RuntimeError):
    """The controller returned an explicit error response.

    Library callers should prefer :func:`raise_for_error_code`, which
    surfaces the typed ``WatlowNoSuch*`` subclasses defined in
    :mod:`watlowlib.errors` (those are what the session catches and
    maps to :class:`Availability`). ``StdBusError`` is retained as a
    convenience for ad-hoc decoding of captured payloads outside the
    session.
    """

    def __init__(self, code: int, label: str | None = None) -> None:
        try:
            label = label or ErrorCode(code).name
        except ValueError:
            label = label or f"unknown(0x{code:02X})"
        super().__init__(f"Standard Bus error 0x{code:02X} ({label})")
        self.code = code


def raise_for_error_code(
    code: int,
    *,
    context: ErrorContext | None = None,
) -> None:
    """Raise the typed :class:`watlowlib.errors.WatlowError` for ``code``.

    Maps Std Bus error bytes to the typed subclasses the session uses
    for :class:`Availability` updates. Unknown error codes raise
    :class:`watlowlib.errors.WatlowProtocolError`.
    """
    if code == ErrorCode.NO_SUCH_OBJECT:
        raise WatlowNoSuchObjectError(
            f"Standard Bus error 0x{code:02X} (NO_SUCH_OBJECT)",
            context=context,
        )
    if code == ErrorCode.NO_SUCH_ATTRIBUTE:
        raise WatlowNoSuchAttributeError(
            f"Standard Bus error 0x{code:02X} (NO_SUCH_ATTRIBUTE)",
            context=context,
        )
    if code == ErrorCode.NO_SUCH_INSTANCE:
        raise WatlowNoSuchInstanceError(
            f"Standard Bus error 0x{code:02X} (NO_SUCH_INSTANCE)",
            context=context,
        )
    raise WatlowProtocolError(
        f"Standard Bus error 0x{code:02X} (unknown)",
        context=context,
    )


def split_param(param_id: int) -> tuple[int, int]:
    """Split a Watlow Parameter ID into ``(class, member)``.

    The published "Parameter ID" in user manuals is
    ``Class * 1000 + Member`` — ``4001`` decodes to ``(4, 1)``,
    ``8003`` decodes to ``(8, 3)``.
    """
    return divmod(param_id, 1000)


def join_param(cls: int, member: int) -> int:
    """Recompose a Parameter ID from its ``class`` and ``member`` parts."""
    return cls * 1000 + member


def encode_read_request(param_id: int, instance: int = 1) -> bytes:
    """Build the inner payload for reading a single parameter."""
    cls, member = split_param(param_id)
    return bytes([DIR_REQUEST, FN_READ, 0x01, cls, member, instance])


def encode_write_request(
    param_id: int,
    value: float | int | str | bytes,
    *,
    instance: int = 1,
    type_tag: int = DataType.FLOAT,
) -> bytes:
    """Build the inner payload for writing a single parameter."""
    cls, member = split_param(param_id)
    head = bytes([DIR_REQUEST, FN_WRITE, cls, member, instance])
    return head + encode_value(type_tag, value)


def decode_payload(
    payload: bytes,
) -> ReadResponse | WriteResponse | ReadRequest | WriteRequest | ErrorResponse:
    """Parse an inner Watlow payload into a structured response/request/error."""
    if len(payload) < 2:
        raise ValueError(f"payload too short: {payload.hex()}")
    direction, function = payload[0], payload[1]
    if direction not in (DIR_REQUEST, DIR_RESPONSE):
        raise ValueError(f"unknown direction byte 0x{direction:02X}")

    # Error response: 0x02 followed by an error code with high bit set.
    if direction == DIR_RESPONSE and function & 0x80:
        if len(payload) != 2:
            raise ValueError(f"error response with unexpected trailing bytes: {payload.hex()}")
        return ErrorResponse(function)

    if function == FN_READ:
        if direction == DIR_REQUEST:
            if len(payload) < 6 or payload[2] != 0x01:
                raise ValueError(f"unexpected read-request shape: {payload.hex()}")
            return ReadRequest(payload[3], payload[4], payload[5])
        # read response: 02 03 01 CC MM II <typed value>
        if len(payload) < 7 or payload[2] != 0x01:
            raise ValueError(f"unexpected read-response shape: {payload.hex()}")
        cls, mem, inst = payload[3], payload[4], payload[5]
        value, tag, _ = decode_value(payload[6:])
        return ReadResponse(cls, mem, inst, value, tag)

    if function == FN_WRITE:
        # write request/response: dd 04 CC MM II <typed value>
        if len(payload) < 6:
            raise ValueError(f"unexpected write shape: {payload.hex()}")
        cls, mem, inst = payload[2], payload[3], payload[4]
        value, tag, _ = decode_value(payload[5:])
        return (
            WriteRequest(cls, mem, inst, value, tag)
            if direction == DIR_REQUEST
            else WriteResponse(cls, mem, inst, value, tag)
        )

    raise ValueError(f"unknown function byte 0x{function:02X}")
