"""``watlow-diag snapshot`` — dump every supported registry parameter.

Walks every readable :class:`~watlowlib.registry.parameters.ParameterSpec`
in the bundled PM registry, sends each through
:meth:`Controller.read_parameter`, and records the outcome. Useful for
capability discovery on a new firmware revision: the output reveals
which parameters the device exposes, which are sticky-unsupported, and
what their decoded values look like.

Read-only. Never writes anything destructive.
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
from watlowlib.errors import WatlowError
from watlowlib.registry.parameters import PARAMETERS

if TYPE_CHECKING:
    from watlowlib.devices.controller import Controller
    from watlowlib.registry.parameters import ParameterSpec

__all__ = ["main"]


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="watlow-diag snapshot",
        description=(
            "Read every supported registry parameter on the connected "
            "Watlow controller. Capability-discovery aid; read-only by "
            "construction."
        ),
    )
    add_open_args(parser)
    parser.add_argument(
        "--out",
        type=str,
        default=None,
        help="Write JSON results to FILE instead of human-readable text on stdout.",
    )
    parser.add_argument(
        "--include",
        nargs="+",
        type=lambda s: int(s, 0),
        default=None,
        help="Restrict the snapshot to this explicit parameter-id list "
        "(default: every readable spec in the registry).",
    )
    parser.add_argument(
        "--instance",
        type=int,
        default=1,
        help="Loop / channel instance to read (1-indexed, default: 1).",
    )
    args = parser.parse_args(argv)
    return run_cli(lambda: _async_main(args))


async def _async_main(args: argparse.Namespace) -> int:
    controller = await controller_from_args(args)
    selected = _select_specs(args.include)
    async with controller as ctl:
        results = await _probe_all(
            ctl,
            specs=selected,
            instance=args.instance,
            timeout=args.timeout,
        )

    if args.out is not None:
        Path(args.out).write_text(json.dumps(results, indent=2), encoding="utf-8")
        sys.stdout.write(f"snapshot: wrote {len(results)} results to {args.out}\n")
    else:
        sys.stdout.write(_format_text(results))
    return 0


def _select_specs(include: list[int] | None) -> tuple[ParameterSpec, ...]:
    """Return the spec list to probe.

    Without ``--include``, walk every readable spec in the registry.
    With ``--include``, filter to the requested IDs (silently dropping
    IDs the registry doesn't know).
    """
    if include is None:
        return tuple(PARAMETERS)
    requested = set(include)
    return tuple(spec for spec in PARAMETERS if spec.parameter_id in requested)


async def _probe_all(
    ctl: Controller,
    *,
    specs: tuple[ParameterSpec, ...],
    instance: int,
    timeout: float,
) -> list[dict[str, object]]:
    """Read each spec in ``specs`` and record the outcome JSON-friendly."""
    results: list[dict[str, object]] = []
    for spec in specs:
        entry: dict[str, object] = {
            "parameter_id": spec.parameter_id,
            "name": spec.name,
            "data_type": spec.data_type.name,
            "rwes": spec.rwes.value,
            "instance": instance,
        }
        try:
            value_entry = await ctl.read_parameter(
                spec.parameter_id,
                instance=instance,
                timeout=timeout,
            )
        except WatlowError as exc:
            entry["status"] = "error"
            entry["error_type"] = type(exc).__name__
            entry["error_message"] = str(exc)
        else:
            entry["status"] = "ok"
            entry["value"] = value_entry.value
            entry["raw_hex"] = value_entry.raw.hex().upper()
        results.append(entry)
    return results


def _format_text(results: list[dict[str, object]]) -> str:
    """Render ``results`` as a human-readable text block."""
    lines: list[str] = []
    n_ok = sum(1 for r in results if r["status"] == "ok")
    lines.append(f"snapshot: {n_ok}/{len(results)} parameters responded")
    lines.append("")
    for r in results:
        pid = r["parameter_id"]
        name = r["name"]
        if r["status"] == "ok":
            value = r["value"]
            lines.append(
                f"  id={pid:<6} {name!s:<40} ok       value={value!r}",
            )
        else:
            err_t = r.get("error_type", "")
            msg = r.get("error_message", "")
            lines.append(f"  id={pid:<6} {name!s:<40} {err_t}: {msg}")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
