"""``watlow-diag tap`` — passive Std-Bus frame capture (read-only).

Opens the transport, scans bytes for the Std Bus preamble ``55 FF``,
reassembles each complete BACnet MS/TP frame, decodes it, and writes
the decoded view to stdout (or a file). Never writes to the device.

Std Bus frames don't use line termination, so the tap reads byte-by-
byte through the transport's ``read_exact``. Modbus traffic should be
captured by ``watlow-diag stream`` (raw hex) — Modbus framing requires
either a 3.5-character idle gap or a slave-address heuristic the
library does not own a passive parser for.

Read-only. No safety gate required.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import anyio

from watlowlib.cli._common import (
    add_open_args,
    resolve_open_args,
    run_cli,
)
from watlowlib.errors import WatlowFrameError, WatlowTimeoutError
from watlowlib.protocol.stdbus.framing import FrameError, decode_frame
from watlowlib.transport.base import SerialSettings
from watlowlib.transport.serial import SerialTransport

if TYPE_CHECKING:
    from watlowlib.transport.base import Transport

__all__ = ["capture_frames", "main"]


_PREAMBLE = b"\x55\xff"
_HEADER_LEN = 5  # FT, DST, SRC, LEN_HI, LEN_LO
_DCRC_LEN = 2
_PREAMBLE_SCAN_LIMIT = 4096


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="watlow-diag tap",
        description=(
            "Passive Std-Bus frame capture — reads complete BACnet MS/TP "
            "frames for a fixed duration and prints them. Never writes "
            "to the device. For raw byte capture (Modbus or unknown "
            "framing), use `watlow-diag stream` instead."
        ),
    )
    add_open_args(parser)
    parser.add_argument(
        "--duration",
        type=float,
        default=5.0,
        help="Capture window in seconds (default: 5.0).",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=None,
        help="Append captured frames (one JSON record per line) to this "
        "file (default: stdout text).",
    )
    parser.add_argument(
        "--max-frames",
        type=int,
        default=None,
        help="Stop after capturing this many frames (default: unlimited).",
    )
    args = parser.parse_args(argv)
    return run_cli(lambda: _async_main(args))


async def _async_main(args: argparse.Namespace) -> int:
    port, _protocol, _address, settings = resolve_open_args(args)
    transport = _resolve_transport(port, settings)
    if not transport.is_open:
        await transport.open()
    try:
        captured = await capture_frames(
            transport,
            duration=args.duration,
            max_frames=args.max_frames,
        )
    finally:
        await transport.close()

    if args.out is not None:
        with Path(args.out).open("a", encoding="utf-8") as fh:
            fh.writelines(json.dumps(frame) + "\n" for frame in captured)
    else:
        for frame in captured:
            sys.stdout.write(_format_frame(frame))
    sys.stdout.write(f"tap: captured {len(captured)} frame(s)\n")
    return 0


async def capture_frames(
    transport: Transport,
    *,
    duration: float,
    max_frames: int | None = None,
) -> list[dict[str, object]]:
    """Read complete Std Bus frames from ``transport`` for ``duration`` seconds.

    The transport must already be open; the caller owns close(). Each
    captured frame is decoded into a JSON-friendly dict so callers can
    serialize directly. On framing errors the dict carries
    ``status="frame_error"`` plus the raw bytes so downstream tooling
    can re-attempt decoding.
    """
    captured: list[dict[str, object]] = []
    deadline = anyio.current_time() + duration
    while anyio.current_time() < deadline:
        if max_frames is not None and len(captured) >= max_frames:
            break
        remaining = max(0.001, deadline - anyio.current_time())
        try:
            raw = await _read_one_frame(transport, timeout=remaining)
        except WatlowTimeoutError:
            break
        except WatlowFrameError as exc:
            captured.append({"status": "frame_error", "error": str(exc)})
            continue
        try:
            decoded = decode_frame(raw)
        except FrameError as exc:
            captured.append(
                {
                    "status": "decode_error",
                    "error": str(exc),
                    "raw_hex": raw.hex().upper(),
                },
            )
            continue
        captured.append(
            {
                "status": "ok",
                "raw_hex": raw.hex().upper(),
                "frame_type": f"0x{decoded.frame_type:02X}",
                "dst": f"0x{decoded.dst:02X}",
                "src": f"0x{decoded.src:02X}",
                "payload_hex": decoded.payload.hex().upper(),
            },
        )
    return captured


async def _read_one_frame(transport: Transport, *, timeout: float) -> bytes:
    """Scan for ``55 FF``, read the header, then the payload + DCRC.

    Mirrors :class:`watlowlib.protocol.stdbus.client.StdBusProtocolClient`
    internal frame reader, lifted here so the tap CLI doesn't depend on
    a private method.
    """
    # Preamble scan: 1-byte read until we see ``55 FF`` in sequence.
    scanned = bytearray()
    while True:
        b = await transport.read_exact(1, timeout=timeout)
        if b == b"\x55":
            b2 = await transport.read_exact(1, timeout=timeout)
            if b2 == b"\xff":
                break
            scanned.extend(b)
            scanned.extend(b2)
        else:
            scanned.extend(b)
        if len(scanned) > _PREAMBLE_SCAN_LIMIT:
            raise WatlowFrameError(
                f"no Std Bus preamble in {len(scanned)} bytes",
            )

    header = await transport.read_exact(_HEADER_LEN, timeout=timeout)
    plen = (header[3] << 8) | header[4]
    if plen == 0:
        return _PREAMBLE + header
    body = await transport.read_exact(plen + _DCRC_LEN, timeout=timeout)
    return _PREAMBLE + header + body


def _format_frame(frame: dict[str, object]) -> str:
    """One-line text view of a captured frame dict."""
    if frame["status"] == "ok":
        return (
            f"{frame['frame_type']}  "
            f"dst={frame['dst']} src={frame['src']}  "
            f"payload={frame['payload_hex']}\n"
        )
    return f"{frame['status']}: {frame.get('error', '')}\n"


def _resolve_transport(
    port_or_transport: str,
    settings: SerialSettings | None,
) -> Transport:
    """Build a :class:`SerialTransport` from a port string."""
    s = settings if settings is not None else SerialSettings(port=port_or_transport)
    return SerialTransport(s)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
