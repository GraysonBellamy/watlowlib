"""Shared helpers for the ``watlow-*`` CLIs.

Every command that opens a controller accepts the same surface: a
positional ``port``, optional serial-framing overrides
(``--baud`` / ``--parity`` / ``--stopbits``), a ``--protocol``
selector (``stdbus``/``modbus_rtu``/``auto``), an ``--address``,
a ``--timeout``, and a ``--fixture`` test-injection flag.

This module factors that into :func:`add_open_args` plus the
resolution helpers used by each command's ``main``.

The ``--fixture`` flag is the integration-test seam: pass a JSONL
capture file and the CLI runs against a scripted
:class:`FakeTransport` (or the controller built by
:func:`controller_from_fixture`) instead of a real serial port.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

import anyio
from anyserial import Parity, StopBits

from watlowlib.errors import WatlowError, WatlowValidationError
from watlowlib.protocol.base import ProtocolKind
from watlowlib.transport.base import SerialSettings

if TYPE_CHECKING:
    import argparse
    from collections.abc import Awaitable, Callable

    from watlowlib.devices.controller import Controller

__all__ = [
    "PARITY_CHOICES",
    "STOPBITS_CHOICES",
    "add_open_args",
    "controller_from_args",
    "parity_from_name",
    "resolve_open_args",
    "run_cli",
    "stopbits_from_number",
]


PARITY_CHOICES: tuple[str, ...] = ("odd", "even", "none")
STOPBITS_CHOICES: tuple[int, ...] = (1, 2)


def parity_from_name(name: str) -> Parity:
    """Resolve the CLI ``--parity`` choice string to an :class:`anyserial.Parity`."""
    return _PARITY_BY_NAME[name]


def stopbits_from_number(number: int) -> StopBits:
    """Resolve the CLI ``--stopbits`` integer to an :class:`anyserial.StopBits`."""
    return _STOPBITS_BY_NUMBER[number]


_PARITY_BY_NAME: dict[str, Parity] = {
    "odd": Parity.ODD,
    "even": Parity.EVEN,
    "none": Parity.NONE,
}

_STOPBITS_BY_NUMBER: dict[int, StopBits] = {
    1: StopBits.ONE,
    2: StopBits.TWO,
}


def add_open_args(parser: argparse.ArgumentParser, *, port_required: bool = True) -> None:
    """Register the shared ``open_device`` arguments on ``parser``.

    ``port_required=False`` makes ``port`` optional — used by
    discovery / decode CLIs where the no-port form has its own meaning.
    """
    parser.add_argument(
        "port",
        nargs=None if port_required else "?",
        help='Serial-port path ("/dev/ttyUSB0", "COM3", ...). '
        "Ignored when --fixture is supplied; pass any placeholder.",
    )
    parser.add_argument(
        "--baud",
        type=int,
        default=None,
        help="Override the protocol-specific default baud "
        "(38400 for Std Bus; 9600 for Modbus RTU on factory PM).",
    )
    parser.add_argument(
        "--parity",
        choices=sorted(_PARITY_BY_NAME),
        default=None,
        help="Override the default parity (none for Std Bus; even for Modbus PM).",
    )
    parser.add_argument(
        "--stopbits",
        type=int,
        choices=sorted(_STOPBITS_BY_NUMBER),
        default=None,
        help="Override the default 1 stop bit.",
    )
    parser.add_argument(
        "--protocol",
        choices=("auto", "stdbus", "modbus_rtu"),
        default="stdbus",
        help="Wire protocol to speak (default: stdbus).",
    )
    parser.add_argument(
        "--address",
        type=int,
        default=1,
        help="Bus address — Std Bus 1..16, Modbus 1..247 (default: 1).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=1.0,
        help="Per-call I/O timeout in seconds (default: 1.0).",
    )
    parser.add_argument(
        "--fixture",
        type=str,
        default=None,
        help=(
            "Test seam: path to a JSONL capture file. "
            "When supplied, the CLI runs against a Controller built by "
            "watlowlib.testing.controller_from_fixture instead of a "
            "real serial port. ``--port`` is then ignored."
        ),
    )


def resolve_open_args(
    args: argparse.Namespace,
) -> tuple[str, ProtocolKind, int, SerialSettings | None]:
    """Return ``(port, protocol, address, serial_settings)``.

    Raises:
        WatlowValidationError: ``--fixture`` is not set and ``port`` is
            absent.
    """
    if args.fixture is not None:
        # The fixture path is honoured by ``controller_from_args``.
        # The other fields keep argparse defaults so downstream callers
        # can print them in error messages without juggling None.
        return args.fixture, ProtocolKind(args.protocol), int(args.address), None
    if args.port is None:
        raise WatlowValidationError(
            "port is required when --fixture is not supplied",
        )
    settings = _build_serial_settings(
        args.port,
        baudrate=args.baud,
        parity_name=args.parity,
        stopbits_number=args.stopbits,
    )
    return args.port, ProtocolKind(args.protocol), int(args.address), settings


def _build_serial_settings(
    port: str,
    *,
    baudrate: int | None,
    parity_name: str | None,
    stopbits_number: int | None,
) -> SerialSettings | None:
    """Build :class:`SerialSettings` only when at least one override is set.

    Returning ``None`` lets :func:`open_device` apply its own
    protocol-aware default (38400 8-N-1 for Std Bus, 9600 8-E-1 for
    Modbus RTU on factory PMs).
    """
    if baudrate is None and parity_name is None and stopbits_number is None:
        return None
    kwargs: dict[str, Any] = {"port": port}
    if baudrate is not None:
        kwargs["baudrate"] = baudrate
    if parity_name is not None:
        kwargs["parity"] = _PARITY_BY_NAME[parity_name]
    if stopbits_number is not None:
        kwargs["stopbits"] = _STOPBITS_BY_NUMBER[stopbits_number]
    return SerialSettings(**kwargs)


async def controller_from_args(args: argparse.Namespace) -> Controller:
    """Open and return a :class:`Controller` from CLI args.

    Honours ``--fixture`` first; falls back to
    :func:`watlowlib.open_device` otherwise. The returned controller
    is *opened* (the detector or the factory has already run) and
    ready to be used inside ``async with``.
    """
    if args.fixture is not None:
        # Lazy import — testing pulls in the full Controller graph; we
        # don't want a CLI's ``--help`` to load the fixture loader.
        from watlowlib.testing import controller_from_fixture  # noqa: PLC0415

        return await controller_from_fixture(args.fixture)
    from watlowlib.devices.factory import open_device  # noqa: PLC0415

    port, protocol, address, settings = resolve_open_args(args)
    return await open_device(
        port,
        protocol=protocol,
        address=address,
        serial_settings=settings,
    )


def run_cli(coro_factory: Callable[[], Awaitable[int]]) -> int:
    """Run an async CLI body, mapping :class:`WatlowError` to a clean exit.

    On success the coroutine's return value is the exit code. On
    :class:`WatlowError` the message is written to stderr and the
    exit code is ``1`` — keeps the user-facing failure mode quiet
    instead of dumping a traceback for an expected condition (no
    response, framing error, port not found, etc.).
    """
    try:
        return anyio.run(coro_factory)
    except WatlowError as exc:
        sys.stderr.write(f"error: {type(exc).__name__}: {exc}\n")
        return 1
