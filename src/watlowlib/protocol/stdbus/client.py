"""Std Bus protocol client.

:class:`StdBusProtocolClient` wraps a :class:`Transport` and provides
the framed request → typed reply round-trip the
:class:`watlowlib.devices.session.Session` calls into. The client owns:

- the per-port :class:`anyio.Lock` (one Std Bus device may be
  conversing on a port at a time)
- the request → frame assembly (BACnet MS/TP outer + payload + CRCs)
- the read loop (preamble scan, header parse + HCRC, payload + DCRC,
  inner-payload decode)

The variant layer (``commands/parameters.py``) hands ``execute`` raw
inner-payload bytes; the client returns a :class:`StdBusReply`.
``Session.execute`` decides whether the reply payload is an error and
maps Std Bus codes (``0x81`` / ``0x83`` / ``0x84``) to typed
:class:`watlowlib.errors.WatlowError` subclasses via
:func:`watlowlib.protocol.stdbus.payload.raise_for_error_code`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio

from watlowlib._logging import get_logger
from watlowlib.config import DEFAULTS
from watlowlib.errors import (
    ErrorContext,
    WatlowConnectionError,
    WatlowFrameError,
)
from watlowlib.protocol.base import ProtocolKind
from watlowlib.protocol.stdbus.framing import (
    PREAMBLE,
    Frame,
    FrameError,
    decode_frame,
    encode_frame,
)
from watlowlib.protocol.stdbus.payload import decode_payload
from watlowlib.protocol.stdbus.tables import (
    HOST_MAC,
    FrameType,
    addr_to_mac,
)
from watlowlib.protocol.stdbus.types import StdBusReply

if TYPE_CHECKING:
    from watlowlib.transport.base import Transport

__all__ = ["StdBusProtocolClient"]

# Header bytes after the preamble: FT, DST, SRC, LEN_HI, LEN_LO, HCRC.
_HEADER_LEN = 6
# Data CRC bytes after the payload.
_DCRC_LEN = 2
# Maximum stray bytes the preamble scan tolerates before declaring the
# line wedged. 256 covers a runt frame plus a full max-size payload of
# garbage; well above anything we expect from a clean PM3.
_PREAMBLE_SCAN_LIMIT = 256
# Hard cap on the wire payload length we will commit to reading. The
# Watlow attribute service over Std Bus tops out well below this — a
# part-number response is ~32 bytes — so 512 leaves comfortable
# headroom while preventing a hostile / corrupt 16-bit length field
# from steering ``read_exact`` into a 64 KB allocation.
_MAX_PAYLOAD_LEN = 512

_log = get_logger("stdbus")


class StdBusProtocolClient:
    """:class:`watlowlib.protocol.base.ProtocolClient` for Standard Bus."""

    def __init__(self, transport: Transport, *, address: int) -> None:
        # Validate eagerly — better here than deep in the read loop.
        self._dst_mac = addr_to_mac(address)
        self._address = address
        self._transport = transport
        self._lock = anyio.Lock()
        self._disposed = False

    @property
    def lock(self) -> anyio.Lock:
        """Per-client lock used to serialize requests on one port."""
        return self._lock

    @property
    def disposed(self) -> bool:
        """Whether this client has been disposed."""
        return self._disposed

    @property
    def kind(self) -> ProtocolKind:
        """Wire protocol kind served by this client."""
        return ProtocolKind.STDBUS

    @property
    def address(self) -> int:
        """The Standard Bus address (1..16) this client targets."""
        return self._address

    def dispose(self) -> None:
        """Mark this client unusable for future ``execute`` calls."""
        self._disposed = True

    async def execute(
        self,
        request: bytes,
        *,
        timeout: float | None = None,
        command_name: str = "",
    ) -> StdBusReply:
        """Send the inner ``request`` payload and return the framed reply.

        Args:
            request: Inner Watlow payload bytes (e.g. produced by
                :func:`watlowlib.protocol.stdbus.payload.encode_read_request`).
            timeout: Optional per-call override of
                :attr:`watlowlib.config.DEFAULTS.io_timeout_s`.
            command_name: Threaded into log events for traceability.

        Raises:
            WatlowConnectionError: client is disposed or transport not open.
            WatlowFrameError: framing failure (bad preamble, CRC mismatch,
                truncated body).
            WatlowTimeoutError: read or write exceeded the bound.
        """
        if self._disposed:
            raise WatlowConnectionError(
                "StdBusProtocolClient is disposed",
                context=ErrorContext(
                    command_name=command_name or None,
                    protocol=ProtocolKind.STDBUS,
                    port=self._transport.label,
                    address=self._address,
                ),
            )
        bound = timeout if timeout is not None else DEFAULTS.io_timeout_s

        frame = Frame(
            frame_type=FrameType.DATA_EXPECTING_REPLY,
            dst=self._dst_mac,
            src=HOST_MAC,
            payload=request,
        )
        wire = encode_frame(frame)

        await self._transport.write(wire, timeout=bound)
        raw = await self._read_frame(bound, command_name=command_name)
        try:
            decoded = decode_frame(raw)
        except FrameError as exc:
            raise WatlowFrameError(
                f"Std Bus frame decode failed: {exc}",
                context=ErrorContext(
                    command_name=command_name or None,
                    protocol=ProtocolKind.STDBUS,
                    port=self._transport.label,
                    address=self._address,
                    request=wire,
                    response=raw,
                ),
            ) from exc

        try:
            payload = decode_payload(decoded.payload)
        except ValueError as exc:
            raise WatlowFrameError(
                f"Std Bus payload decode failed: {exc}",
                context=ErrorContext(
                    command_name=command_name or None,
                    protocol=ProtocolKind.STDBUS,
                    port=self._transport.label,
                    address=self._address,
                    request=wire,
                    response=raw,
                ),
            ) from exc

        _log.debug(
            "stdbus exec ok cmd=%s addr=%d req_len=%d rsp_len=%d",
            command_name or "<anon>",
            self._address,
            len(wire),
            len(raw),
        )
        return StdBusReply(frame=decoded, payload=payload, raw_frame=raw)

    async def _read_frame(self, timeout: float, *, command_name: str) -> bytes:
        """Read one BACnet MS/TP frame (preamble through DCRC) from the wire.

        Tolerates up to ``_PREAMBLE_SCAN_LIMIT`` bytes of leading
        garbage before the ``55 FF`` preamble — covers stray autoprint
        chatter or the tail of a partially-received frame from a
        previous wedged call.
        """
        # Preamble scan: read 1 byte at a time until we see 0x55 then
        # 0xFF. ``read_exact(1)`` deliberately holds onto pushback so
        # the second-byte mismatch path doesn't lose data.
        scanned = bytearray()
        while True:
            b = await self._transport.read_exact(1, timeout=timeout)
            if b == b"\x55":
                b2 = await self._transport.read_exact(1, timeout=timeout)
                if b2 == b"\xff":
                    break
                scanned.extend(b)
                scanned.extend(b2)
            else:
                scanned.extend(b)
            if len(scanned) > _PREAMBLE_SCAN_LIMIT:
                raise WatlowFrameError(
                    f"no Std Bus preamble in {len(scanned)} bytes",
                    context=ErrorContext(
                        command_name=command_name or None,
                        protocol=ProtocolKind.STDBUS,
                        port=self._transport.label,
                        address=self._address,
                        response=bytes(scanned),
                    ),
                )

        header = await self._transport.read_exact(_HEADER_LEN, timeout=timeout)
        # LEN is the 4th and 5th bytes after preamble (FT, DST, SRC, LEN_HI, LEN_LO).
        plen = (header[3] << 8) | header[4]
        if plen == 0:
            return PREAMBLE + header
        if plen > _MAX_PAYLOAD_LEN:
            # Don't allocate from untrusted length — a corrupt or
            # hostile header could otherwise stretch the read into a
            # 64 KB block. Resync to the next preamble instead.
            raise WatlowFrameError(
                f"Std Bus payload length {plen} exceeds cap "
                f"{_MAX_PAYLOAD_LEN}; treating as wire corruption",
                context=ErrorContext(
                    command_name=command_name or None,
                    protocol=ProtocolKind.STDBUS,
                    port=self._transport.label,
                    address=self._address,
                    response=PREAMBLE + header,
                ),
            )
        body = await self._transport.read_exact(plen + _DCRC_LEN, timeout=timeout)
        return PREAMBLE + header + body
