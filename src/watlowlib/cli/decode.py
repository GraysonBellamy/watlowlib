"""``watlow-decode`` — offline frame / register decoder.

Reads hex bytes from stdin or ``--file``, decodes them as a Standard
Bus frame (preamble 55 FF + outer + inner) or as a Modbus register
tuple (paired with a :class:`ParameterSpec` for type interpretation),
and prints the decoded view.

The CLI is intentionally read-only and does not touch a serial port —
the use case is sniffing a saved capture, debugging a CRC failure, or
sanity-checking what a controller returned in a discovery sweep.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from typing import TYPE_CHECKING, cast

from watlowlib.protocol.stdbus.framing import FrameError, decode_frame
from watlowlib.protocol.stdbus.payload import (
    ErrorResponse,
    ReadRequest,
    ReadResponse,
    WriteRequest,
    WriteResponse,
    decode_payload,
)
from watlowlib.protocol.stdbus.tables import ErrorCode

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="watlow-decode",
        description="Decode captured Watlow frames offline (Std Bus by default).",
    )
    parser.add_argument(
        "hex",
        nargs="?",
        help="Hex bytes to decode. Whitespace is ignored. Reads from stdin if omitted.",
    )
    parser.add_argument(
        "--file",
        "-f",
        type=argparse.FileType("r", encoding="utf-8"),
        help="Read hex bytes from a file instead of stdin.",
    )
    parser.add_argument(
        "--protocol",
        choices=("stdbus",),
        default="stdbus",
        help="Wire protocol of the captured bytes (default: stdbus).",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    raw = _read_input(args)
    try:
        data = bytes.fromhex(raw)
    except ValueError as exc:
        parser.error(f"input is not valid hex: {exc}")

    if args.protocol == "stdbus":
        result = _decode_stdbus(data)
    else:  # pragma: no cover — argparse pins choices
        parser.error(f"unsupported protocol: {args.protocol}")

    if args.format == "json":
        print(json.dumps(result, indent=2, default=_json_default))
    else:
        _print_text(result)
    return 0


def _read_input(args: argparse.Namespace) -> str:
    if args.hex:
        return str(args.hex)
    if args.file is not None:
        return str(args.file.read())
    if sys.stdin.isatty():
        sys.stderr.write("watlow-decode: reading hex from stdin (Ctrl-D to end)\n")
    return sys.stdin.read()


def _decode_stdbus(data: bytes) -> dict[str, object]:
    out: dict[str, object] = {"protocol": "stdbus", "raw_hex": data.hex().upper()}
    try:
        frame = decode_frame(data)
    except FrameError as exc:
        out["frame_error"] = str(exc)
        return out
    out["frame"] = {
        "type": f"0x{frame.frame_type:02X}",
        "dst": f"0x{frame.dst:02X}",
        "src": f"0x{frame.src:02X}",
        "payload_hex": frame.payload.hex().upper(),
    }
    try:
        decoded = decode_payload(frame.payload)
    except ValueError as exc:
        out["payload_error"] = str(exc)
        return out
    out["payload"] = _payload_to_dict(decoded)
    return out


def _payload_to_dict(
    payload: ReadRequest | WriteRequest | ReadResponse | WriteResponse | ErrorResponse,
) -> dict[str, object]:
    base: dict[str, object] = {"kind": type(payload).__name__}
    base.update(asdict(payload))
    if isinstance(payload, ErrorResponse):
        try:
            base["code_label"] = ErrorCode(payload.code).name
        except ValueError:
            base["code_label"] = "UNKNOWN"
    if isinstance(payload, ReadRequest | ReadResponse | WriteRequest | WriteResponse):
        base["parameter_id"] = payload.cls * 1000 + payload.member
    return base


def _print_text(result: dict[str, object]) -> None:
    print(f"protocol: {result['protocol']}")
    print(f"raw_hex:  {result['raw_hex']}")
    if "frame_error" in result:
        print(f"frame error: {result['frame_error']}")
        return
    frame = _expect_dict(result["frame"])
    print(
        f"frame:    type={frame['type']} dst={frame['dst']} src={frame['src']} "
        f"payload={frame['payload_hex']}"
    )
    if "payload_error" in result:
        print(f"payload error: {result['payload_error']}")
        return
    payload = _expect_dict(result["payload"])
    kind = payload.pop("kind")
    fields = " ".join(f"{k}={v!r}" for k, v in payload.items())
    print(f"payload:  {kind} {fields}")


def _expect_dict(value: object) -> dict[str, object]:
    """Narrow ``value`` to the typed dict the caller knows it built."""
    assert isinstance(value, dict)  # noqa: S101 — narrows for type-checker only
    typed: dict[str, object] = {}
    for k, v in cast("dict[object, object]", value).items():
        typed[str(k)] = v
    return typed


def _json_default(obj: object) -> object:
    if isinstance(obj, bytes | bytearray):
        return bytes(obj).hex().upper()
    raise TypeError(f"unserialisable: {type(obj).__name__}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
