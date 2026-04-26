"""``watlow-read`` — one-shot parameter read from a live device.

Opens a controller (Std Bus, Modbus RTU, or AUTO), reads one or more
parameters by name or ID, prints them in text or JSON format, and
exits. Tests drive the same code path against fixtures via
``--fixture`` so the CLI has no real-serial dependency in CI.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import TYPE_CHECKING, Any

import anyio
from anyserial import Parity

from watlowlib.devices.factory import open_device
from watlowlib.errors import WatlowError
from watlowlib.protocol.base import ProtocolKind
from watlowlib.testing import controller_from_fixture
from watlowlib.transport.base import SerialSettings

if TYPE_CHECKING:
    from collections.abc import Sequence

    from watlowlib.devices.controller import Controller
    from watlowlib.devices.models import ParameterEntry

__all__ = ["build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="watlow-read",
        description="Read one or more parameters from a Watlow controller.",
    )
    parser.add_argument(
        "--port",
        help="Serial-port path (e.g. /dev/ttyUSB0, COM3). Required unless --fixture is set.",
    )
    parser.add_argument(
        "--fixture",
        help=(
            "Path to a JSONL capture for offline replay. Mutually exclusive "
            "with --port; the protocol / address come from the capture."
        ),
    )
    parser.add_argument(
        "--protocol",
        choices=("auto", "stdbus", "modbus_rtu"),
        default="stdbus",
        help="Wire protocol (default: stdbus).",
    )
    parser.add_argument("--address", type=int, default=1, help="Bus address (default: 1).")
    parser.add_argument("--baud", type=int, help="Baud rate (default: protocol-specific).")
    parser.add_argument("--parity", default=None, help="Parity (none/even/odd).")
    parser.add_argument(
        "--instance",
        type=int,
        default=1,
        help="Loop / channel instance (1-indexed, default: 1).",
    )
    parser.add_argument(
        "--parameter",
        "-p",
        action="append",
        default=[],
        help="Parameter name or id. Repeatable. At least one required.",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Per-call timeout in seconds (overrides the library default).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.parameter:
        parser.error("at least one --parameter / -p is required")
    if not args.port and not args.fixture:
        parser.error("either --port or --fixture is required")
    if args.port and args.fixture:
        parser.error("--port and --fixture are mutually exclusive")

    try:
        return anyio.run(_run, args)
    except WatlowError as exc:
        print(f"watlow-read: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


async def _run(args: argparse.Namespace) -> int:
    controller = await _open_controller(args)
    rows: list[dict[str, Any]] = []
    async with controller as ctl:
        protocol_value = ctl.session.protocol_kind.value
        address = ctl.session.address
        port_label = ctl.session.port
        for name_or_id in args.parameter:
            entry = await ctl.read_parameter(
                _coerce_name_or_id(name_or_id),
                instance=args.instance,
                timeout=args.timeout,
            )
            rows.append(
                _entry_to_row(
                    entry,
                    protocol=protocol_value,
                    address=address,
                    port=port_label,
                ),
            )

    if args.format == "json":
        print(json.dumps(rows, indent=2, default=_json_default))
    else:
        for row in rows:
            print(_format_row(row))
    return 0


async def _open_controller(args: argparse.Namespace) -> Controller:
    if args.fixture:
        return await controller_from_fixture(args.fixture)
    protocol = ProtocolKind(args.protocol)
    serial_settings = _build_serial_settings(args)
    return await open_device(
        args.port,
        protocol=protocol,
        address=args.address,
        serial_settings=serial_settings,
    )


def _build_serial_settings(args: argparse.Namespace) -> SerialSettings | None:
    if args.baud is None and args.parity is None:
        return None
    kwargs: dict[str, Any] = {"port": args.port}
    if args.baud is not None:
        kwargs["baudrate"] = args.baud
    if args.parity is not None:
        kwargs["parity"] = Parity(str(args.parity).lower())
    return SerialSettings(**kwargs)


def _coerce_name_or_id(value: str) -> str | int:
    if value.isdigit():
        return int(value)
    return value


def _entry_to_row(
    entry: ParameterEntry,
    *,
    protocol: str,
    address: int,
    port: str,
) -> dict[str, Any]:
    return {
        "port": port,
        "protocol": protocol,
        "address": address,
        "name": entry.spec.name,
        "parameter_id": entry.spec.parameter_id,
        "instance": entry.instance,
        "value": entry.value,
        "data_type": entry.spec.data_type.name,
        "raw_hex": entry.raw.hex().upper(),
    }


def _format_row(row: dict[str, Any]) -> str:
    return (
        f"{row['name']:<24} "
        f"id={row['parameter_id']:<6} "
        f"inst={row['instance']:<2} "
        f"value={row['value']!r:<20} "
        f"type={row['data_type']:<6} "
        f"raw={row['raw_hex']}"
    )


def _json_default(obj: object) -> object:
    if isinstance(obj, bytes | bytearray):
        return bytes(obj).hex().upper()
    raise TypeError(f"unserialisable: {type(obj).__name__}")


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
