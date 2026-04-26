"""``watlow-discover`` — sweep a port for Watlow controllers.

Walks the configured address range on Standard Bus (MAC ``0x10..0x1F``
→ addresses ``1..16``) and / or Modbus RTU (slave ``1..N``), running
the same probe ``identify()`` uses against each candidate. Each result
is one :class:`watlowlib.devices.models.DiscoveryResult` row.

Sweeping is opt-in for everything past the conservative defaults — the
full Modbus address space is 1..247 but a single segment rarely has
more than a handful of devices, and a 247-slot sweep takes minutes at
the per-probe budget.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import TYPE_CHECKING, Any

import anyio
from anyserial import Parity

from watlowlib.devices.discovery import (
    DEFAULT_MODBUS_RANGE,
    DEFAULT_STDBUS_RANGE,
    sweep_modbus,
    sweep_stdbus,
)
from watlowlib.errors import WatlowError
from watlowlib.protocol.base import ProtocolKind
from watlowlib.transport.base import SerialSettings

if TYPE_CHECKING:
    from collections.abc import Sequence

    from watlowlib.devices.models import DiscoveryResult

__all__ = ["build_parser", "main", "parse_addresses"]

# Std Bus factory baud (the EZ-ZONE PM ships at 38400 8-N-1 on Std Bus).
_DEFAULT_BAUD = 38400


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="watlow-discover",
        description="Sweep a serial port for Watlow controllers (Std Bus and/or Modbus RTU).",
    )
    parser.add_argument("--port", required=True, help="Serial-port path (e.g. /dev/ttyUSB0).")
    parser.add_argument(
        "--protocol",
        choices=("stdbus", "modbus_rtu", "both"),
        default="stdbus",
        help="Which protocol to sweep (default: stdbus).",
    )
    parser.add_argument(
        "--addresses",
        action="append",
        default=[],
        help=(
            "Address range like '1-16' or '1,2,5,10'. Repeatable. "
            "Defaults to 1-16 for both protocols."
        ),
    )
    parser.add_argument(
        "--baud",
        type=int,
        action="append",
        default=[],
        help="Baud rate. Repeatable to try multiple bauds (default: 38400).",
    )
    parser.add_argument(
        "--parity",
        default=None,
        help="Parity (none/even/odd). Default: protocol-specific.",
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
    try:
        addresses = parse_addresses(args.addresses) if args.addresses else None
    except ValueError as exc:
        parser.error(str(exc))

    bauds: list[int] = list(args.baud) or [_DEFAULT_BAUD]
    rows: list[DiscoveryResult] = []
    try:
        for baud in bauds:
            rows.extend(anyio.run(_sweep, args, baud, addresses))
    except WatlowError as exc:
        print(f"watlow-discover: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if args.format == "json":
        print(json.dumps([_row_to_dict(r) for r in rows], indent=2, default=_json_default))
    else:
        for row in rows:
            print(_format_row(row))
    return 0


async def _sweep(
    args: argparse.Namespace,
    baud: int,
    addresses: tuple[int, ...] | None,
) -> list[DiscoveryResult]:
    rows: list[DiscoveryResult] = []
    explicit_baud = baud != _DEFAULT_BAUD or args.baud
    explicit_parity = args.parity is not None
    if args.protocol in ("stdbus", "both"):
        settings = _settings_for_protocol(
            args.port,
            ProtocolKind.STDBUS,
            baud=baud,
            parity=args.parity,
            explicit_baud=bool(explicit_baud),
            explicit_parity=explicit_parity,
        )
        ranges = addresses if addresses is not None else DEFAULT_STDBUS_RANGE
        rows.extend(
            [r async for r in sweep_stdbus(args.port, addresses=ranges, serial_settings=settings)],
        )
    if args.protocol in ("modbus_rtu", "both"):
        settings = _settings_for_protocol(
            args.port,
            ProtocolKind.MODBUS_RTU,
            baud=baud,
            parity=args.parity,
            explicit_baud=bool(explicit_baud),
            explicit_parity=explicit_parity,
        )
        ranges = addresses if addresses is not None else DEFAULT_MODBUS_RANGE
        rows.extend(
            [r async for r in sweep_modbus(args.port, addresses=ranges, serial_settings=settings)],
        )
    return rows


def parse_addresses(specs: list[str]) -> tuple[int, ...]:
    """Parse one or more ``--addresses`` specs into a flat tuple.

    Each spec is either a single integer (``5``), a comma-separated list
    (``1,2,5,10``), or an inclusive range (``1-16``). Specs are merged
    in argument order with duplicates preserved (the underlying sweep
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


def _settings_for_protocol(
    port: str,
    protocol: ProtocolKind,
    *,
    baud: int,
    parity: str | None,
    explicit_baud: bool,
    explicit_parity: bool,
) -> SerialSettings:
    """Build :class:`SerialSettings` honouring the protocol's factory framing.

    When the user did not pass ``--baud`` / ``--parity`` explicitly,
    fall back to the target protocol's factory framing (38400 8-N-1
    for Std Bus, 9600 8-E-1 for Modbus RTU). Without this, a
    ``--protocol both`` sweep would probe Modbus PMs at the Std Bus
    factory framing and miss every Modbus device on the bus.
    """
    factory = SerialSettings.factory_for(protocol, port=port)
    resolved_baud = baud if explicit_baud else factory.baudrate
    resolved_parity = (
        Parity(parity.lower()) if (parity is not None and explicit_parity) else factory.parity
    )
    return SerialSettings(port=port, baudrate=resolved_baud, parity=resolved_parity)


def _row_to_dict(row: DiscoveryResult) -> dict[str, Any]:
    info_dict: dict[str, Any] | None = None
    if row.info is not None:
        info_dict = {
            "part_number": row.info.part_number.raw,
            "family": row.info.family.value,
            "hardware_id": row.info.hardware_id,
            "firmware_id": row.info.firmware_id,
            "serial_number": row.info.serial_number,
            "loops": row.info.loops,
            "capabilities": row.info.capabilities.value,
        }
    return {
        "port": row.port,
        "address": row.address,
        "protocol": row.protocol.value if row.protocol is not None else None,
        "baudrate": row.serial_settings.baudrate,
        "parity": row.serial_settings.parity.value,
        "info": info_dict,
        "error": str(row.error) if row.error is not None else None,
    }


def _format_row(row: DiscoveryResult) -> str:
    proto = row.protocol.value if row.protocol is not None else "?"
    if row.info is not None:
        return (
            f"  ✓ {proto:<11} addr={row.address:<3} "
            f"baud={row.serial_settings.baudrate:<6} "
            f"part={row.info.part_number.raw or '-':<16} "
            f"family={row.info.family.value}"
        )
    error = type(row.error).__name__ if row.error is not None else "no-reply"
    return f"  · {proto:<11} addr={row.address:<3} baud={row.serial_settings.baudrate:<6} {error}"


def _json_default(obj: object) -> object:
    if isinstance(obj, bytes | bytearray):
        return bytes(obj).hex().upper()
    return str(obj)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
