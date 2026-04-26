"""``watlow-raw`` — escape hatch for raw protocol access.

The facade is the supported surface; ``watlow-raw`` exists for the
days when you need to reach a parameter the registry doesn't cover or
debug a wire-level surprise. Two subcommands:

- ``stdbus`` — emit one Std Bus read/write at the inner-payload level
  (class / member / instance), framed and CRC-stamped by the
  :class:`StdBusProtocolClient`.
- ``modbus`` — emit one :class:`ModbusOp`, lowered onto an
  :class:`anymodbus.Slave` by the :class:`ModbusProtocolClient`.

Per the design doc's open question 2, the Modbus side takes a typed
:class:`ModbusOp` (option *b*) rather than raw register tuples.
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
from watlowlib.protocol.modbus.ops import ModbusFn, ModbusOp
from watlowlib.protocol.stdbus.payload import (
    encode_read_request,
    encode_write_request,
)
from watlowlib.protocol.stdbus.tlv import DataType
from watlowlib.transport.base import SerialSettings

if TYPE_CHECKING:
    from collections.abc import Sequence

    from watlowlib.devices.session import Session
    from watlowlib.protocol.stdbus.types import StdBusReply

__all__ = ["build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="watlow-raw",
        description="Send a raw Std Bus or Modbus operation. Escape hatch — prefer the facade.",
    )
    parser.add_argument("--port", required=True, help="Serial-port path.")
    parser.add_argument("--address", type=int, default=1, help="Bus address (default: 1).")
    parser.add_argument("--baud", type=int, help="Baud rate (default: protocol-specific).")
    parser.add_argument("--parity", default=None, help="Parity (none/even/odd).")
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    sub = parser.add_subparsers(dest="protocol", required=True)

    std = sub.add_parser("stdbus", help="Issue one Std Bus read or write.")
    std.add_argument("--service", choices=("read", "write"), required=True)
    std.add_argument("--class", type=int, dest="cls", required=True, help="Class byte (0..255).")
    std.add_argument("--member", type=int, required=True, help="Member byte (0..255).")
    std.add_argument("--instance", type=int, default=1, help="Instance (1-indexed, default: 1).")
    std.add_argument(
        "--type",
        dest="data_type",
        default="FLOAT",
        help="Type tag for write (FLOAT/S32/U32/U16/U8/STRING/PACKED). Read ignores it.",
    )
    std.add_argument("--value", help="Value to write (parsed per --type).")

    mb = sub.add_parser("modbus", help="Issue one Modbus operation.")
    mb.add_argument(
        "--fn",
        choices=tuple(fn.value for fn in ModbusFn),
        required=True,
        help="Modbus function selector.",
    )
    mb.add_argument("--register", type=int, required=True, help="Zero-based register address.")
    mb.add_argument("--count", type=int, default=1, help="Register count for reads (default: 1).")
    mb.add_argument(
        "--values",
        help="Comma-separated 16-bit register values for writes (e.g. '0x43C4,0x0000').",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return anyio.run(_run, args)
    except WatlowError as exc:
        print(f"watlow-raw: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"watlow-raw: {exc}", file=sys.stderr)
        return 2


async def _run(args: argparse.Namespace) -> int:
    if args.protocol == "stdbus":
        result = await _run_stdbus(args)
    else:
        result = await _run_modbus(args)
    if args.format == "json":
        print(json.dumps(result, indent=2, default=_json_default))
    else:
        for key, value in result.items():
            print(f"{key}: {value}")
    return 0


async def _run_stdbus(args: argparse.Namespace) -> dict[str, Any]:
    parameter_id = args.cls * 1000 + args.member
    if args.service == "read":
        payload = encode_read_request(parameter_id, instance=args.instance)
    else:
        if args.value is None:
            raise ValueError("write service requires --value")
        data_type = _resolve_data_type(args.data_type)
        coerced = _coerce_value(args.value, data_type)
        payload = encode_write_request(
            parameter_id,
            coerced,
            instance=args.instance,
            type_tag=data_type,
        )

    settings = _settings(args, default_parity="none", default_baud=38400)
    controller = await open_device(
        args.port,
        protocol=ProtocolKind.STDBUS,
        address=args.address,
        serial_settings=settings,
    )
    async with controller as ctl:
        client = ctl.session.client
        async with client.lock:
            reply: StdBusReply = await client.execute(payload, command_name="watlow-raw:stdbus")
    return {
        "service": args.service,
        "parameter_id": parameter_id,
        "instance": args.instance,
        "request_hex": payload.hex().upper(),
        "reply_payload_hex": reply.frame.payload.hex().upper(),
        "reply_decoded": repr(reply.payload),
    }


async def _run_modbus(args: argparse.Namespace) -> dict[str, Any]:
    fn = ModbusFn(args.fn)
    values: tuple[int, ...] | None = None
    if args.values:
        values = tuple(int(part, 0) for part in args.values.split(","))
    op = ModbusOp(
        fn=fn,
        address=args.register,
        count=args.count if values is None else len(values),
        values=values,
    )

    settings = _settings(args, default_parity="even", default_baud=9600)
    controller = await open_device(
        args.port,
        protocol=ProtocolKind.MODBUS_RTU,
        address=args.address,
        serial_settings=settings,
    )
    async with controller as ctl:
        session: Session = ctl.session
        client = session.client
        async with client.lock:
            words = await client.execute(op, command_name="watlow-raw:modbus")
    return {
        "fn": fn.value,
        "register": op.address,
        "count": op.count,
        "values": list(values) if values is not None else None,
        "words": list(words),
    }


def _settings(
    args: argparse.Namespace,
    *,
    default_parity: str,
    default_baud: int,
) -> SerialSettings:
    parity = (args.parity or default_parity).lower()
    return SerialSettings(
        port=args.port,
        baudrate=args.baud or default_baud,
        parity=Parity(parity),
    )


def _resolve_data_type(name: str) -> int:
    try:
        return int(DataType[name.upper()])
    except KeyError as exc:
        raise ValueError(f"unknown --type {name!r}") from exc


def _coerce_value(value: str, data_type: int) -> float | int | str | bytes:
    if data_type == DataType.FLOAT:
        return float(value)
    if data_type in (DataType.S32, DataType.U32, DataType.U16, DataType.U8, DataType.PACKED):
        return int(value, 0)
    if data_type == DataType.STRING:
        return value
    raise ValueError(f"unsupported data type 0x{data_type:02X}")


def _json_default(obj: object) -> object:
    if isinstance(obj, bytes | bytearray):
        return bytes(obj).hex().upper()
    return str(obj)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
