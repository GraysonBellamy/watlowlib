r"""``watlow-configure`` — confirmed configuration operations.

Four maintenance subcommands wrap :mod:`watlowlib.maintenance`::

    watlow-configure change-baud PORT --target-baud BAUD \\
        [--current-protocol modbus_rtu] [--address N] --confirm

    watlow-configure change-modbus-address PORT \\
        --target-address N [--current-address N] --confirm

    watlow-configure change-stdbus-address PORT \\
        --target-address N [--current-address N] --confirm

    watlow-configure change-protocol-mode PORT --target {stdbus,modbus_rtu} \\
        [--current-protocol auto] [--address N] --confirm

Every subcommand refuses without ``--confirm``. Output is a
human-readable summary of the post-change :class:`DeviceInfo`.

The standard ``--baud`` / ``--parity`` / ``--stopbits`` flags from
:mod:`watlowlib.cli._common` describe the *current* serial framing
the host opens at; the ``--target-*`` flags describe what the
device should switch to.
"""

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING

from watlowlib.cli._common import (
    add_open_args,
    resolve_open_args,
    run_cli,
)
from watlowlib.devices.models import DeviceHealth, DeviceInfo
from watlowlib.errors import WatlowError, WatlowValidationError
from watlowlib.maintenance import (
    MODBUS_BAUD_CODES,
    change_baud,
    change_modbus_address,
    change_protocol_mode,
    change_stdbus_address,
)
from watlowlib.protocol.base import ProtocolKind

if TYPE_CHECKING:
    from collections.abc import Awaitable

__all__ = ["build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    """Return the top-level argparse parser for ``watlow-configure``."""
    parser = argparse.ArgumentParser(
        prog="watlow-configure",
        description=(
            "Confirmed configuration operations — change baud, change "
            "address (Std Bus or Modbus), switch protocol mode. Every "
            "subcommand requires --confirm."
        ),
    )
    subs = parser.add_subparsers(dest="op", required=True)

    sp_baud = subs.add_parser(
        "change-baud",
        help="Write parameter 17002 (Modbus baud), reopen at the new baud, identify.",
    )
    add_open_args(sp_baud)
    sp_baud.add_argument(
        "--target-baud",
        type=int,
        required=True,
        choices=sorted(MODBUS_BAUD_CODES),
        help="New baud rate (one of 9600 / 19200 / 38400).",
    )
    sp_baud.add_argument(
        "--confirm",
        action="store_true",
        help="Required: acknowledge the destructive nature of the operation.",
    )

    sp_modbus_addr = subs.add_parser(
        "change-modbus-address",
        help="Write parameter 17007 (Modbus address) and reopen at the new slave id.",
    )
    add_open_args(sp_modbus_addr)
    sp_modbus_addr.add_argument(
        "--target-address",
        type=int,
        required=True,
        help="New Modbus slave address (1..247).",
    )
    sp_modbus_addr.add_argument(
        "--confirm",
        action="store_true",
        help="Required: acknowledge the destructive nature of the operation.",
    )

    sp_stdbus_addr = subs.add_parser(
        "change-stdbus-address",
        help="Write parameter 17001 (Std Bus address) and reopen at the new MS/TP MAC.",
    )
    add_open_args(sp_stdbus_addr)
    sp_stdbus_addr.add_argument(
        "--target-address",
        type=int,
        required=True,
        help="New Std Bus address (1..16; mapped to MAC 0x10..0x1F on the wire).",
    )
    sp_stdbus_addr.add_argument(
        "--confirm",
        action="store_true",
        help="Required: acknowledge the destructive nature of the operation.",
    )

    sp_proto = subs.add_parser(
        "change-protocol-mode",
        help="Write parameter 17009 (Protocol) and reopen at the new framing.",
    )
    add_open_args(sp_proto)
    sp_proto.add_argument(
        "--target",
        choices=("stdbus", "modbus_rtu"),
        required=True,
        help="Target wire protocol after the switch.",
    )
    sp_proto.add_argument(
        "--confirm",
        action="store_true",
        help="Required: acknowledge the destructive nature of the operation.",
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.confirm:
        sys.stderr.write(
            f"error: watlow-configure {args.op} is destructive; pass --confirm to execute\n",
        )
        return 2
    return run_cli(lambda: _async_main(args))


async def _async_main(args: argparse.Namespace) -> int:
    if args.op == "change-baud":
        return await _run_change_baud(args)
    if args.op == "change-modbus-address":
        return await _run_change_modbus_address(args)
    if args.op == "change-stdbus-address":
        return await _run_change_stdbus_address(args)
    if args.op == "change-protocol-mode":
        return await _run_change_protocol_mode(args)
    raise WatlowValidationError(  # pragma: no cover — argparse guards this
        f"unknown op {args.op!r}",
    )


async def _run_change_baud(args: argparse.Namespace) -> int:
    port, protocol, address, settings = resolve_open_args(args)
    return await _run_op(
        op="change-baud",
        coro=change_baud(
            port,
            target_baud=args.target_baud,
            current_protocol=(
                protocol if protocol is not ProtocolKind.AUTO else ProtocolKind.MODBUS_RTU
            ),
            address=address,
            serial_settings=settings,
            timeout=args.timeout,
            confirm=True,
        ),
    )


async def _run_change_modbus_address(args: argparse.Namespace) -> int:
    port, _protocol, address, settings = resolve_open_args(args)
    return await _run_op(
        op="change-modbus-address",
        coro=change_modbus_address(
            port,
            target_address=args.target_address,
            current_address=address,
            serial_settings=settings,
            timeout=args.timeout,
            confirm=True,
        ),
    )


async def _run_change_stdbus_address(args: argparse.Namespace) -> int:
    port, _protocol, address, settings = resolve_open_args(args)
    return await _run_op(
        op="change-stdbus-address",
        coro=change_stdbus_address(
            port,
            target_address=args.target_address,
            current_address=address,
            serial_settings=settings,
            timeout=args.timeout,
            confirm=True,
        ),
    )


async def _run_change_protocol_mode(args: argparse.Namespace) -> int:
    port, current_protocol, address, settings = resolve_open_args(args)
    target = ProtocolKind(args.target)
    return await _run_op(
        op="change-protocol-mode",
        coro=change_protocol_mode(
            port,
            target=target,
            current_protocol=current_protocol,
            address=address,
            serial_settings=settings,
            timeout=args.timeout,
            confirm=True,
        ),
    )


async def _run_op(*, op: str, coro: Awaitable[DeviceInfo]) -> int:
    """Run the maintenance coroutine and emit a structured result block.

    The block carries an explicit ``status: ok | sku_blocked | verify_failed
    | unsupported_protocol | error`` line and a ``recovery:`` hint
    when the operation didn't reach the OK terminal state. The
    explicit status replaces the older "complete:" header that
    printed even on partial-failure DeviceInfos with ``<unknown>``
    fields, which read as success at a glance.
    """
    try:
        info = await coro
    except WatlowError as exc:
        sys.stdout.write(_format_failure(op=op, exc=exc))
        return 1
    sys.stdout.write(_format_info(info, op=op))
    return 0 if info.health is DeviceHealth.OK else 1


def _format_info(info: DeviceInfo, *, op: str) -> str:
    if info.health is DeviceHealth.OK:
        status = "ok"
        recovery = None
    elif info.health is DeviceHealth.PARTIAL:
        status = "partial"
        recovery = (
            "the device answered the part-number probe but a secondary "
            "identity field was silent — re-run identify() if that field "
            "is load-bearing for your tooling."
        )
    else:
        status = "verify_failed"
        recovery = (
            "the device did not respond at the new framing within the "
            "verify timeout. Power-cycle the controller and re-run "
            "identify(). If the SKU's part number's comms position 8 "
            "is 'A', the hardware does not include the target protocol "
            "and the EEPROM write will not change runtime behaviour."
        )

    lines = [
        f"{op} complete:",
        f"  status:       {status}",
        f"  family:       {info.family.value}",
        f"  protocol:     {info.protocol.value}",
        f"  address:      {info.address}",
        f"  baudrate:     {info.serial_settings.baudrate}",
        f"  parity:       {info.serial_settings.parity.value}",
        f"  stopbits:     {info.serial_settings.stopbits.value}",
        f"  part_number:  {info.part_number.raw or '<unknown>'}",
        f"  hardware_id:  {info.hardware_id if info.hardware_id is not None else '<unknown>'}",
        f"  firmware_id:  {info.firmware_id if info.firmware_id is not None else '<unknown>'}",
    ]
    if info.configured_protocol is not None:
        lines.append(f"  configured:   {info.configured_protocol.value}")
        if info.protocol_mismatch:
            lines.append("  mismatch:     EEPROM protocol differs from active protocol")
    if recovery is not None:
        lines.append(f"  recovery:     {recovery}")
    return "\n".join(lines) + "\n"


def _format_failure(*, op: str, exc: WatlowError) -> str:
    """Structured failure block — the bookend of :func:`_format_info`."""
    lines = [
        f"{op} failed:",
        f"  status:       {_status_for_error(exc)}",
        f"  reason:       {type(exc).__name__}: {exc}",
    ]
    recovery = _recovery_for_error(exc)
    if recovery is not None:
        lines.append(f"  recovery:     {recovery}")
    return "\n".join(lines) + "\n"


def _status_for_error(exc: WatlowError) -> str:
    msg = str(exc).lower()
    if "comms position" in msg:
        return "sku_blocked"
    name = type(exc).__name__
    if name == "WatlowConfigurationError":
        return "configuration_error"
    if name == "WatlowTimeoutError":
        return "verify_failed"
    if name == "WatlowConnectionError":
        return "connection_lost"
    return "error"


def _recovery_for_error(exc: WatlowError) -> str | None:
    msg = str(exc).lower()
    if "comms position" in msg:
        return (
            "the SKU does not include the target protocol's hardware. "
            "Pick a different target, or order a controller with the "
            "matching comms option."
        )
    name = type(exc).__name__
    if name == "WatlowTimeoutError":
        return (
            "no response within the timeout. Power-cycle the controller "
            "and re-run; check the wiring + termination of the RS-485 "
            "segment if the device persists in being silent."
        )
    if name == "WatlowConnectionError":
        return "the transport disconnected — check the USB-485 cable and re-run."
    return None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
