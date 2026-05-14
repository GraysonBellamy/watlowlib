"""``watlow-discover`` — scan local serial ports for Watlow controllers.

Thin wrapper over :func:`watlowlib.find_devices`. The CLI iterates the
cartesian product of ``ports × baudrates × protocols × addresses`` and
prints one row per probe attempt.

With no flags the CLI runs the default scan: every visible serial
port (via ``anyserial.list_serial_ports``), bauds ``38400 / 19200 /
9600``, both protocols (Standard Bus and Modbus RTU), address ``1``
only. That matches what a GUI Discover dialog wants — fast, narrow,
"is anything plugged into this rig" — and lands a typical four-port
sweep in under 15 seconds.

For deeper sweeps (address ranges, custom bauds, a specific port) pass
the matching flags; the parser accepts repeatable ``--port`` /
``--baud`` flags and range / list specs like ``--addresses 1-16`` or
``--addresses 1,2,5``.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import TYPE_CHECKING, Any

import anyio

from watlowlib.devices.discovery import (
    DEFAULT_DISCOVERY_ADDRESSES,
    DEFAULT_DISCOVERY_BAUDRATES,
    find_devices,
)
from watlowlib.errors import WatlowError
from watlowlib.protocol.base import ProtocolKind

if TYPE_CHECKING:
    from collections.abc import Sequence

    from watlowlib.devices.models import FindResult

__all__ = ["build_parser", "main", "parse_addresses"]


_PROTOCOL_CHOICES: dict[str, tuple[ProtocolKind, ...]] = {
    "stdbus": (ProtocolKind.STDBUS,),
    "modbus_rtu": (ProtocolKind.MODBUS_RTU,),
    "both": (ProtocolKind.STDBUS, ProtocolKind.MODBUS_RTU),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="watlow-discover",
        description=(
            "Scan local serial ports for Watlow controllers (Std Bus and/or "
            "Modbus RTU). With no flags, scans every visible port at "
            "38400/19200/9600 baud, both protocols, address 1."
        ),
    )
    parser.add_argument(
        "--port",
        action="append",
        default=[],
        help=(
            "Serial-port path. Repeatable. Omit to scan every port the OS exposes via anyserial."
        ),
    )
    parser.add_argument(
        "--protocol",
        choices=tuple(_PROTOCOL_CHOICES),
        default="both",
        help="Which protocol(s) to probe (default: both).",
    )
    parser.add_argument(
        "--addresses",
        action="append",
        default=[],
        help=(
            "Address range like '1-16' or '1,2,5,10'. Repeatable. "
            "Defaults to address 1 only — pass an explicit range for "
            "multi-drop buses."
        ),
    )
    parser.add_argument(
        "--baud",
        type=int,
        action="append",
        default=[],
        help=("Baud rate. Repeatable. Defaults to 38400 / 19200 / 9600."),
    )
    parser.add_argument(
        "--probe-timeout",
        type=float,
        default=0.5,
        help=(
            "Per-probe budget in seconds (default: 0.5). Caps the "
            "identify() call so silent buses bail fast."
        ),
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    parser.add_argument(
        "--responsive-only",
        action="store_true",
        help="Suppress silent / errored rows in the output.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        addresses = (
            parse_addresses(args.addresses) if args.addresses else DEFAULT_DISCOVERY_ADDRESSES
        )
    except ValueError as exc:
        parser.error(str(exc))

    bauds: tuple[int, ...] = tuple(args.baud) if args.baud else DEFAULT_DISCOVERY_BAUDRATES
    protocols = _PROTOCOL_CHOICES[args.protocol]
    ports: list[str] | None = args.port or None

    async def _scan() -> list[FindResult]:
        return await find_devices(
            ports=ports,
            addresses=addresses,
            baudrates=bauds,
            protocols=protocols,
            per_probe_timeout_s=args.probe_timeout,
        )

    try:
        rows = anyio.run(_scan)
    except WatlowError as exc:
        print(f"watlow-discover: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.responsive_only:
        rows = [r for r in rows if r.ok]

    if args.format == "json":
        print(json.dumps([_row_to_dict(r) for r in rows], indent=2, default=_json_default))
    else:
        for row in rows:
            print(_format_row(row))
    return 0


def parse_addresses(specs: list[str]) -> tuple[int, ...]:
    """Parse one or more ``--addresses`` specs into a flat tuple.

    Each spec is either a single integer (``5``), a comma-separated list
    (``1,2,5,10``), or an inclusive range (``1-16``). Specs are merged
    in argument order with duplicates preserved (the underlying scan
    is idempotent).
    """
    out: list[int] = []
    for spec in specs:
        for raw_chunk in spec.split(","):
            chunk = raw_chunk.strip()
            if not chunk:
                continue
            if "-" in chunk:
                lo_str, hi_str = chunk.split("-", 1)
                try:
                    lo, hi = int(lo_str), int(hi_str)
                except ValueError as exc:
                    raise ValueError(f"bad range {chunk!r}: {exc}") from exc
                if hi < lo:
                    raise ValueError(f"range {chunk!r}: high < low")
                out.extend(range(lo, hi + 1))
            else:
                try:
                    out.append(int(chunk))
                except ValueError as exc:
                    raise ValueError(f"bad address {chunk!r}: {exc}") from exc
    return tuple(out)


def _row_to_dict(row: FindResult) -> dict[str, Any]:
    info_dict: dict[str, Any] | None = None
    if row.info is not None:
        configured = row.info.configured_protocol
        info_dict = {
            "part_number": row.info.part_number.raw,
            "family": row.info.family.value,
            "hardware_id": row.info.hardware_id,
            "firmware_id": row.info.firmware_id,
            "serial_number": row.info.serial_number,
            "loops": row.info.loops,
            "capabilities": row.info.capabilities.value,
            "health": row.info.health.value,
            "configured_protocol": configured.value if configured is not None else None,
        }
    return {
        "port": row.port,
        "address": row.address,
        "baudrate": row.baudrate,
        "protocol": row.protocol.value,
        "ok": row.ok,
        "info": info_dict,
        "error": str(row.error) if row.error is not None else None,
    }


def _format_row(row: FindResult) -> str:
    proto = row.protocol.value
    if row.info is not None:
        return (
            f"  ✓ {row.port:<14} {proto:<11} addr={row.address:<3} "
            f"baud={row.baudrate:<6} "
            f"part={row.info.part_number.raw or '-':<16} "
            f"fw={row.info.firmware_id} hw={row.info.hardware_id} "
            f"health={row.info.health.value}"
        )
    error = type(row.error).__name__ if row.error is not None else "no-reply"
    return f"  · {row.port:<14} {proto:<11} addr={row.address:<3} baud={row.baudrate:<6} {error}"


def _json_default(obj: object) -> object:
    if isinstance(obj, bytes | bytearray):
        return bytes(obj).hex().upper()
    return str(obj)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
