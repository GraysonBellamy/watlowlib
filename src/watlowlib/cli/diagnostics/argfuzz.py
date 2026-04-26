"""``watlow-diag argfuzz`` — boundary-value writes against one parameter.

Sends a sequence of values to one chosen parameter and records the
response per iteration. Used to map a parameter's accepted argument
range — e.g. discover where the device starts returning
``IllegalDataValue`` for a setpoint write under a given firmware.

Read-only by default — every value is written via
:meth:`Controller.read_parameter` after a probe write attempt; pass
``--write`` to actually issue the writes (gated by
``--i-understand-this-is-destructive`` because the user picked the
parameter and the library cannot predict the side-effects).
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
from watlowlib.errors import WatlowError, WatlowValidationError
from watlowlib.registry.parameters import PARAMETERS

if TYPE_CHECKING:
    from watlowlib.devices.controller import Controller

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="watlow-diag argfuzz",
        description=(
            "Probe one parameter with a sequence of values and record "
            "each response. Reads only by default; --write makes the "
            "fuzzer destructive."
        ),
    )
    add_open_args(parser)
    parser.add_argument(
        "--parameter",
        required=True,
        help="Parameter name or ID to probe (e.g. 'setpoint' or 7001).",
    )
    parser.add_argument(
        "--instance",
        type=int,
        default=1,
        help="Loop / channel instance (1-indexed, default: 1).",
    )
    parser.add_argument(
        "--mode",
        choices=("range", "values"),
        default="range",
        help="range: sweep --start..--end inclusive in --step increments. "
        "values: try each entry in --values verbatim.",
    )
    parser.add_argument(
        "--start",
        type=float,
        default=0.0,
        help="First value to try in range mode (default: 0.0).",
    )
    parser.add_argument(
        "--end",
        type=float,
        default=100.0,
        help="Last value to try in range mode, inclusive (default: 100.0).",
    )
    parser.add_argument(
        "--step",
        type=float,
        default=10.0,
        help="Step size in range mode (default: 10.0).",
    )
    parser.add_argument(
        "--values",
        nargs="+",
        type=float,
        default=None,
        help="Explicit value list (values mode only).",
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="Issue writes (destructive). Without --write the fuzzer "
        "only re-reads the parameter and records the value.",
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
        help="Required when --write is set: acknowledge that the "
        "fuzzer may mutate persistent state.",
    )
    args = parser.parse_args(argv)

    if args.write:
        require_destructive_ack(acked=args.ack_destructive, op="argfuzz --write")

    return run_cli(lambda: _async_main(args))


async def _async_main(args: argparse.Namespace) -> int:
    parameter_key = _parse_parameter(args.parameter)
    spec = PARAMETERS.resolve(parameter_key)
    values = _build_values(args)

    controller = await controller_from_args(args)
    async with controller as ctl:
        results = await _fuzz(
            ctl,
            parameter_id=spec.parameter_id,
            instance=args.instance,
            values=values,
            timeout=args.timeout,
            do_write=bool(args.write),
        )

    if args.out is not None:
        Path(args.out).write_text(json.dumps(results, indent=2), encoding="utf-8")
        sys.stdout.write(f"argfuzz: wrote {len(results)} results to {args.out}\n")
    else:
        sys.stdout.write(_format_text(results, parameter=spec.name, parameter_id=spec.parameter_id))
    return 0


def _parse_parameter(value: str) -> str | int:
    """Coerce a CLI argument into a registry-resolvable key."""
    if value.isdigit() or (value.startswith(("0x", "0X")) and value[2:].isalnum()):
        return int(value, 0)
    return value


def _build_values(args: argparse.Namespace) -> list[float]:
    """Resolve --mode + --start/--end/--step / --values to a value list."""
    if args.mode == "values":
        if not args.values:
            raise WatlowValidationError(
                "--mode=values requires --values <v1> <v2> ...",
            )
        return list(args.values)
    if args.step <= 0:
        raise WatlowValidationError(
            f"--step must be > 0 in range mode, got {args.step!r}",
        )
    if args.start > args.end:
        raise WatlowValidationError(
            f"--start ({args.start}) > --end ({args.end}) in range mode",
        )
    out: list[float] = []
    v = args.start
    # Integer-step floating loop is fine here; we round to avoid the
    # usual "0.30000000000000004"-style noise from binary float drift.
    n_steps = max(1, round((args.end - args.start) / args.step) + 1)
    for i in range(n_steps):
        out.append(round(args.start + i * args.step, 6))
        v = out[-1]
        if v >= args.end:
            break
    if not out or out[-1] < args.end:
        out.append(args.end)
    return out


async def _fuzz(
    ctl: Controller,
    *,
    parameter_id: int,
    instance: int,
    values: list[float],
    timeout: float,
    do_write: bool,
) -> list[dict[str, object]]:
    """Send each value to ``parameter_id``; collect outcomes."""
    results: list[dict[str, object]] = []
    for value in values:
        entry: dict[str, object] = {
            "parameter_id": parameter_id,
            "instance": instance,
            "value_attempted": value,
        }
        if do_write:
            try:
                write_entry = await ctl.write_parameter(
                    parameter_id,
                    value,
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
                entry["echo_value"] = write_entry.value

        # Read back regardless of write outcome — the device's current
        # state is informative either way.
        try:
            read_entry = await ctl.read_parameter(
                parameter_id,
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
        results.append(entry)
    return results


def _format_text(
    results: list[dict[str, object]],
    *,
    parameter: str,
    parameter_id: int,
) -> str:
    """Render argfuzz results as a human-readable text block."""
    n_write_ok = sum(1 for r in results if r.get("write_status") == "ok")
    n_read_ok = sum(1 for r in results if r["read_status"] == "ok")
    lines = [
        f"argfuzz {parameter!s} (id={parameter_id}): "
        f"{n_write_ok} write-ok, {n_read_ok}/{len(results)} read-ok",
        "",
    ]
    for r in results:
        attempted = r["value_attempted"]
        line = f"  attempt={attempted!r:<14}"
        if "write_status" in r:
            wstatus = r["write_status"]
            if wstatus == "ok":
                echo = r.get("echo_value")
                line += f" write=ok echo={echo!r:<14}"
            else:
                err_t = r.get("write_error_type", "")
                line += f" write={err_t}"
        if r["read_status"] == "ok":
            line += f" read={r['read_value']!r}"
        else:
            err_t = r.get("read_error_type", "")
            line += f" read={err_t}"
        lines.append(line)
    return "\n".join(lines) + "\n"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
