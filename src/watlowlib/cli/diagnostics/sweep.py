"""``watlow-diag sweep`` — registry-parameter sweep across an ID range.

Walks every parameter ID in ``[--start, --end]`` (inclusive) — both
those known to the bundled registry and (optionally) every ID in the
range, including IDs the registry doesn't carry. Sends each through
:meth:`Controller.read_parameter` and records the response shape.

Read-only by default. Pass ``--write`` to flip persistent values to
``--write-value`` instead, in which case
``--i-understand-this-is-destructive`` is required — the sweep will
flip every writable parameter in the range and the user gives up
control of which ones land.

The default exclude shield blocks the comm-config parameters
(:data:`DEFAULT_SWEEP_EXCLUDE`) that would brick the session if they
flipped under a write sweep — Standard-Bus address (17001), Modbus
baud (17002), Modbus parity (17003), Modbus address (17007),
protocol mode (17009), Non-Volatile Save (17051). Pass
``--include-comm-config`` to lift the shield (still gated by
``--i-understand-this-is-destructive``).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from watlowlib.cli._common import (
    add_open_args,
    controller_from_args,
    run_cli,
)
from watlowlib.cli.diagnostics._gate import require_destructive_ack
from watlowlib.errors import WatlowError
from watlowlib.registry.parameters import PARAMETERS

if TYPE_CHECKING:
    from watlowlib.devices.controller import Controller
    from watlowlib.registry.parameters import ParameterSpec

__all__ = ["DEFAULT_SWEEP_EXCLUDE", "main"]


# Comm-config parameters that would brick the session if a sweep
# flipped them. Each is RWE / persistent. Shielded by default.
DEFAULT_SWEEP_EXCLUDE: frozenset[int] = frozenset(
    {
        17001,  # Communications - Standard Bus Address
        17002,  # Communications - Baud Rate (Modbus)
        17003,  # Communications - Parity (Modbus)
        17007,  # Communications - Modbus Address
        17009,  # Communications - Protocol
        17051,  # Communications - Non-Volatile Save
    },
)


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="watlow-diag sweep",
        description=(
            "Sweep registry parameter IDs in [start, end] and record "
            "the response shape. Reads only by default; --write makes "
            "the sweep destructive."
        ),
    )
    add_open_args(parser)
    parser.add_argument(
        "--start",
        type=lambda s: int(s, 0),
        default=1000,
        help="First parameter ID to sweep (default: 1000).",
    )
    parser.add_argument(
        "--end",
        type=lambda s: int(s, 0),
        default=20000,
        help="Last parameter ID to sweep, inclusive (default: 20000).",
    )
    parser.add_argument(
        "--instance",
        type=int,
        default=1,
        help="Loop / channel instance (1-indexed, default: 1).",
    )
    parser.add_argument(
        "--registry-only",
        action="store_true",
        help="Only sweep IDs the bundled registry carries (faster; "
        "default behaviour anyway since open IDs raise pre-I/O).",
    )
    parser.add_argument(
        "--include-comm-config",
        action="store_true",
        help="Disable the built-in exclude shield (17001/17002/17003/17007/17009/17051).",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Flip every writable parameter in the range to --write-value. "
        "Destructive; requires --i-understand-this-is-destructive.",
    )
    parser.add_argument(
        "--write-value",
        type=lambda s: int(s, 0),
        default=0,
        help="Value to write when --write is set (default: 0).",
    )
    parser.add_argument(
        "--out",
        type=str,
        default=None,
        help="Write JSON results to FILE instead of human-readable text.",
    )
    parser.add_argument(
        "--i-understand-this-is-destructive",
        action="store_true",
        dest="ack_destructive",
        help="Required when --write is set: acknowledge the destructive nature.",
    )
    args = parser.parse_args(argv)

    if args.write:
        require_destructive_ack(acked=args.ack_destructive, op="sweep --write")

    return run_cli(lambda: _async_main(args))


async def _async_main(args: argparse.Namespace) -> int:
    if args.start > args.end:
        sys.stderr.write(f"error: --start ({args.start}) > --end ({args.end})\n")
        return 1

    excluded: frozenset[int] = frozenset() if args.include_comm_config else DEFAULT_SWEEP_EXCLUDE

    if args.registry_only:
        specs = tuple(
            spec
            for spec in PARAMETERS
            if args.start <= spec.parameter_id <= args.end and spec.parameter_id not in excluded
        )
    else:
        # Walk every ID in the range that the registry knows; IDs not in
        # the registry would raise WatlowValidationError before any I/O.
        specs = tuple(
            spec
            for spec in PARAMETERS
            if args.start <= spec.parameter_id <= args.end and spec.parameter_id not in excluded
        )

    controller = await controller_from_args(args)
    async with controller as ctl:
        results = await _sweep(
            ctl,
            specs=specs,
            instance=args.instance,
            timeout=args.timeout,
            do_write=bool(args.write),
            write_value=args.write_value,
        )

    if args.out is not None:
        Path(args.out).write_text(json.dumps(results, indent=2), encoding="utf-8")
        sys.stdout.write(f"sweep: wrote {len(results)} results to {args.out}\n")
    else:
        sys.stdout.write(_format_text(results, excluded=excluded))
    return 0


async def _sweep(
    ctl: Controller,
    *,
    specs: tuple[ParameterSpec, ...],
    instance: int,
    timeout: float,
    do_write: bool,
    write_value: int,
) -> list[dict[str, object]]:
    """Read each spec; optionally flip writable specs to ``write_value``."""
    results: list[dict[str, object]] = []
    for spec in specs:
        entry: dict[str, object] = {
            "parameter_id": spec.parameter_id,
            "name": spec.name,
            "instance": instance,
            "rwes": spec.rwes.value,
        }
        try:
            read_entry = await ctl.read_parameter(
                spec.parameter_id,
                instance=instance,
                timeout=timeout,
            )
        except WatlowError as exc:
            entry["read_status"] = "error"
            entry["read_error_type"] = type(exc).__name__
            entry["read_error_message"] = str(exc)
        else:
            entry["read_status"] = "ok"
            entry["read_value"] = read_entry.value

        if do_write and spec.rwes.value != "R":
            try:
                await ctl.write_parameter(
                    spec.parameter_id,
                    write_value,
                    instance=instance,
                    confirm=True,
                    timeout=timeout,
                )
            except WatlowError as exc:
                entry["write_status"] = "error"
                entry["write_error_type"] = type(exc).__name__
                entry["write_error_message"] = str(exc)
            else:
                entry["write_status"] = "ok"

        results.append(entry)
    return results


def _format_text(
    results: list[dict[str, object]],
    *,
    excluded: frozenset[int],
) -> str:
    """Render the sweep results as a human-readable text block."""
    n_ok = sum(1 for r in results if r["read_status"] == "ok")
    lines = [
        f"sweep: {n_ok}/{len(results)} parameters responded "
        f"(excluded: {len(excluded)} default-shielded)",
        "",
    ]
    for r in results:
        pid = r["parameter_id"]
        name = r["name"]
        rwes = r["rwes"]
        if r["read_status"] == "ok":
            value = r["read_value"]
            tag = f"value={value!r}"
        else:
            err_t = r.get("read_error_type", "")
            msg = r.get("read_error_message", "")
            tag = f"{err_t}: {msg}"
        line = f"  id={pid:<6} {name!s:<40} rwes={rwes:<5} {tag}"
        if "write_status" in r:
            wstatus = r["write_status"]
            if wstatus == "ok":
                line += "  [write=ok]"
            else:
                w_err = r.get("write_error_type", "")
                line += f"  [write={w_err}]"
        lines.append(line)
    return "\n".join(lines) + "\n"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
