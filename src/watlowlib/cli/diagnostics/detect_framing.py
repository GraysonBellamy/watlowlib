"""``watlow-diag detect-framing`` — wire-side framing probe.

Walks the cartesian product of ``(stdbus, modbus_rtu) x bauds x parities``
against a serial port and reports any combination that returns a
parseable identity frame within the timeout. Read-only by construction
— the probe issues parameter ``1001`` (Hardware ID) reads only and
never writes.

The intended use case is field recovery: a controller's front-panel
buttons are broken (or absent), and the host has lost track of the
configured framing. The recommended recovery procedure of "walk
through ``Setup → COM → AdS / bAUd / PAr / PCo``" is unavailable;
this CLI is the wire-side fallback.

Example::

    watlow-diag detect-framing /dev/ttyUSB0 --address 1 --json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING

import anyio
from anyserial import Parity

from watlowlib.devices.factory import open_device
from watlowlib.errors import WatlowError
from watlowlib.protocol.base import ProtocolKind
from watlowlib.transport.base import SerialSettings

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

__all__ = ["build_parser", "main"]


# Bauds the EZ-ZONE PM commonly speaks. Std Bus is fixed at 38400 by
# the factory; Modbus PM ships at 9600 8-E-1 but the user can change
# it. Listing the full PM-supported set lets the probe find devices
# whose framing has drifted from the factory default.
_DEFAULT_BAUDS: tuple[int, ...] = (38400, 19200, 9600, 57600, 115200)
_DEFAULT_PARITIES: tuple[Parity, ...] = (Parity.NONE, Parity.EVEN, Parity.ODD)
_DEFAULT_PROTOCOLS: tuple[ProtocolKind, ...] = (
    ProtocolKind.STDBUS,
    ProtocolKind.MODBUS_RTU,
)
_DEFAULT_TIMEOUT_S: float = 0.5


@dataclass(frozen=True, slots=True)
class _Hit:
    """One framing combination that elicited a parseable identity reply."""

    protocol: str
    baudrate: int
    parity: str
    address: int
    part_number: str
    family: str
    hardware_id: int | None


@dataclass(frozen=True, slots=True)
class _Miss:
    """One framing combination that did not respond."""

    protocol: str
    baudrate: int
    parity: str
    address: int
    error: str


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="watlow-diag detect-framing",
        description=(
            "Sweep (protocol x baud x parity) on a serial port and report "
            "any combination that returns a parseable Watlow identity reply. "
            "Read-only — never writes."
        ),
    )
    parser.add_argument(
        "port",
        help="Serial-port path (e.g. /dev/ttyUSB0).",
    )
    parser.add_argument(
        "--address",
        type=int,
        default=1,
        help="Bus address to probe (default: 1).",
    )
    parser.add_argument(
        "--baud",
        type=int,
        action="append",
        default=[],
        help=(
            "Baud rate to try. Repeatable. Defaults to "
            f"{', '.join(str(b) for b in _DEFAULT_BAUDS)}."
        ),
    )
    parser.add_argument(
        "--parity",
        choices=("none", "even", "odd"),
        action="append",
        default=[],
        help="Parity to try. Repeatable. Defaults to none, even, odd.",
    )
    parser.add_argument(
        "--protocol",
        choices=("stdbus", "modbus_rtu", "both"),
        default="both",
        help="Protocols to probe (default: both).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=_DEFAULT_TIMEOUT_S,
        help=f"Per-probe timeout in seconds (default: {_DEFAULT_TIMEOUT_S}).",
    )
    parser.add_argument(
        "--format",
        choices=("text", "json"),
        default="text",
        help="Output format (default: text).",
    )
    parser.add_argument(
        "--show-misses",
        action="store_true",
        help="Include silent rows in the output (default: hits only).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    bauds = tuple(args.baud) if args.baud else _DEFAULT_BAUDS
    parities = (
        tuple(_PARITY_BY_NAME[name] for name in args.parity) if args.parity else _DEFAULT_PARITIES
    )
    protocols: tuple[ProtocolKind, ...]
    if args.protocol == "stdbus":
        protocols = (ProtocolKind.STDBUS,)
    elif args.protocol == "modbus_rtu":
        protocols = (ProtocolKind.MODBUS_RTU,)
    else:
        protocols = _DEFAULT_PROTOCOLS

    hits, misses = anyio.run(
        _run,
        args.port,
        protocols,
        bauds,
        parities,
        args.address,
        args.timeout,
    )
    return _emit(hits, misses, fmt=args.format, show_misses=args.show_misses)


_PARITY_BY_NAME: dict[str, Parity] = {
    "none": Parity.NONE,
    "even": Parity.EVEN,
    "odd": Parity.ODD,
}


async def _run(
    port: str,
    protocols: Iterable[ProtocolKind],
    bauds: Iterable[int],
    parities: Iterable[Parity],
    address: int,
    timeout_s: float,
) -> tuple[list[_Hit], list[_Miss]]:
    hits: list[_Hit] = []
    misses: list[_Miss] = []
    for protocol in protocols:
        for baud in bauds:
            for parity in parities:
                settings = SerialSettings(port=port, baudrate=baud, parity=parity)
                hit, miss = await _probe(
                    port=port,
                    protocol=protocol,
                    settings=settings,
                    address=address,
                    timeout_s=timeout_s,
                )
                if hit is not None:
                    hits.append(hit)
                if miss is not None:
                    misses.append(miss)
    return hits, misses


async def _probe(
    *,
    port: str,
    protocol: ProtocolKind,
    settings: SerialSettings,
    address: int,
    timeout_s: float,
) -> tuple[_Hit | None, _Miss | None]:
    try:
        controller = await open_device(
            port,
            protocol=protocol,
            address=address,
            serial_settings=settings,
        )
    except WatlowError as exc:
        return None, _Miss(
            protocol=protocol.value,
            baudrate=settings.baudrate,
            parity=settings.parity.value,
            address=address,
            error=f"open: {type(exc).__name__}",
        )

    try:
        async with controller as ctl:
            try:
                info = await ctl.identify(timeout=timeout_s)
            except WatlowError as exc:
                return None, _Miss(
                    protocol=protocol.value,
                    baudrate=settings.baudrate,
                    parity=settings.parity.value,
                    address=address,
                    error=f"identify: {type(exc).__name__}",
                )
            if not info.part_number.raw and info.hardware_id is None:
                return None, _Miss(
                    protocol=protocol.value,
                    baudrate=settings.baudrate,
                    parity=settings.parity.value,
                    address=address,
                    error="silent",
                )
            return _Hit(
                protocol=protocol.value,
                baudrate=settings.baudrate,
                parity=settings.parity.value,
                address=address,
                part_number=info.part_number.raw,
                family=info.family.value,
                hardware_id=info.hardware_id,
            ), None
    except WatlowError as exc:
        return None, _Miss(
            protocol=protocol.value,
            baudrate=settings.baudrate,
            parity=settings.parity.value,
            address=address,
            error=f"close: {type(exc).__name__}",
        )


def _emit(
    hits: list[_Hit],
    misses: list[_Miss],
    *,
    fmt: str,
    show_misses: bool,
) -> int:
    if fmt == "json":
        payload = {
            "hits": [asdict(h) for h in hits],
            "misses": [asdict(m) for m in misses] if show_misses else [],
        }
        print(json.dumps(payload, indent=2))
    else:
        if not hits:
            print("no framing combination elicited a parseable identity reply.")
        for hit in hits:
            print(
                f"  + {hit.protocol:<11} baud={hit.baudrate:<6} parity={hit.parity:<5} "
                f"addr={hit.address:<3} part={hit.part_number or '<unknown>'} "
                f"family={hit.family}"
            )
        if show_misses:
            for miss in misses:
                print(
                    f"  - {miss.protocol:<11} baud={miss.baudrate:<6} parity={miss.parity:<5} "
                    f"addr={miss.address:<3} {miss.error}"
                )
    # Non-zero exit when no hits — useful for scripted recovery loops.
    return 0 if hits else 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
