"""``watlow-diag probe-unit`` — diagnose the wire-side temperature unit.

Compares a known reference temperature (the value the user reads on
the device's front panel) against the value the device reports over
comms, and infers which scale the wire is on. Emits a recommendation
for ``open_device(..., assert_wire_temperature_unit=...)``.

Background: on at least one PM3 firmware revision parameter 17050
("Communications - Display Units") is **label-only** — writing it
changes the enum the device reports for itself but does not affect
the scale of temperature values exchanged over comms. The library
therefore refuses to infer ``Reading.unit`` from 17050. This probe
provides the empirical answer the library cannot derive on its own.
See ``docs/devices.md`` §Units for the user-facing contract.

Read-only by construction: the probe only **reads** parameters. It
records:

- The comms-side numeric value of the selected reference parameter
  (default: ``setpoint`` on loop 1). The user supplies the panel
  reading + panel unit via ``--panel-shows`` / ``--panel-unit``.
- The values of every C/F-coded "unit" register the registry knows
  about (3005, 17050), for forensic correlation.
- The decoded part-number / firmware id, so the report is
  unambiguous about which SKU + revision was probed.

The inference compares ``comms_value`` against the panel reading
converted to both °C and °F. Whichever conversion matches within
``--epsilon`` is the wire scale. If neither matches, the report
flags ``inconclusive`` and dumps the raw numbers for the operator
to interpret.

Typical session::

    # 1. Set SP=50 on the front panel; the device shows °C.
    # 2. Run:
    watlow-diag probe-unit COM6 --panel-shows 50 --panel-unit C
    # → "Wire scale is FAHRENHEIT — pass
    #    assert_wire_temperature_unit=Unit.FAHRENHEIT to open_device."
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import TYPE_CHECKING, NotRequired, TypedDict

from watlowlib.cli._common import (
    add_open_args,
    controller_from_args,
    run_cli,
)
from watlowlib.errors import WatlowError
from watlowlib.registry.units import Unit, coerce_unit, unit_from_display_code

if TYPE_CHECKING:
    from watlowlib.devices.controller import Controller

__all__ = ["infer_wire_unit", "main", "to_celsius", "to_fahrenheit"]


class IdentityReport(TypedDict, total=False):
    """JSON-friendly identity block for the probe report."""

    error: str
    part_number: str | None
    family: str
    hardware_id: int | None
    firmware_id: int | None
    protocol: str
    address: int


class ReferenceReport(TypedDict):
    """Reference value captured from CLI args plus the wire readback."""

    parameter: str
    instance: int
    panel_shows: float
    panel_unit: str
    comms_value: float | None


class LabelRegisterRow(TypedDict):
    """One forensic label-register readback row."""

    parameter_id: int
    name: str
    status: str
    error: NotRequired[str]
    raw_value: NotRequired[object]
    decoded: NotRequired[str | None]


type InferenceReport = dict[str, object]


class ProbeUnitReport(TypedDict):
    """Complete probe-unit report."""

    device: IdentityReport
    reference: ReferenceReport
    label_registers: list[LabelRegisterRow]
    inference: InferenceReport


# Registers that report a C/F enum code on a PM. Probed read-only as
# part of the forensic record. Pairs are ``(parameter_id, name)``.
_UNIT_LABEL_REGISTERS: tuple[tuple[int, str], ...] = (
    (3005, "units"),  # Display - Units (front-panel)
    (17050, "display_units"),  # Communications - Display Units (label)
)

# Sentinel emitted when a candidate parameter wasn't reachable.
_UNREACHABLE = "unreachable"


def main(argv: list[str] | None = None) -> int:
    """Entry point. Returns the process exit code."""
    parser = argparse.ArgumentParser(
        prog="watlow-diag probe-unit",
        description=(
            "Diagnose the wire-side temperature unit by comparing a "
            "known front-panel reading against the comms readback. "
            "Read-only; emits a recommendation for "
            "open_device(assert_wire_temperature_unit=...)."
        ),
    )
    add_open_args(parser)
    parser.add_argument(
        "--panel-shows",
        type=float,
        required=True,
        help="The numeric value currently displayed on the device's front "
        "panel for the selected parameter (default parameter: setpoint).",
    )
    parser.add_argument(
        "--panel-unit",
        type=str,
        required=True,
        help='Unit the panel reading is in ("C", "F", "celsius", ...).',
    )
    parser.add_argument(
        "--parameter",
        type=str,
        default="setpoint",
        help=(
            "Registry parameter to compare against. Must be a temperature "
            "parameter (e.g. 'setpoint', 'process_value'). Default: setpoint."
        ),
    )
    parser.add_argument(
        "--instance",
        type=int,
        default=1,
        help="Loop / channel instance (1-indexed, default: 1).",
    )
    parser.add_argument(
        "--epsilon",
        type=float,
        default=0.2,
        help=(
            "Match tolerance when comparing comms readback to the "
            "panel-reading converted to each candidate scale. "
            "Default: 0.2 (covers single-digit rounding on the panel)."
        ),
    )
    parser.add_argument(
        "--json",
        dest="emit_json",
        action="store_true",
        help="Emit the full report as JSON on stdout. Default is a human-readable summary.",
    )
    args = parser.parse_args(argv)
    return run_cli(lambda: _async_main(args))


async def _async_main(args: argparse.Namespace) -> int:
    panel_unit = coerce_unit(args.panel_unit)
    if panel_unit is Unit.PERCENT:
        sys.stderr.write(
            "error: --panel-unit must be a temperature scale (C / F); % is not valid here.\n",
        )
        return 2

    controller = await controller_from_args(args)
    async with controller as ctl:
        info_block = await _read_identity(ctl, timeout=args.timeout)
        labels = await _read_label_registers(ctl, timeout=args.timeout)
        comms_value = await _read_reference_value(
            ctl,
            name=args.parameter,
            instance=args.instance,
            timeout=args.timeout,
        )

    inference = infer_wire_unit(
        panel_value=args.panel_shows,
        panel_unit=panel_unit,
        comms_value=comms_value,
        epsilon=args.epsilon,
    )

    report: ProbeUnitReport = {
        "device": info_block,
        "reference": {
            "parameter": args.parameter,
            "instance": args.instance,
            "panel_shows": args.panel_shows,
            "panel_unit": panel_unit.value,
            "comms_value": comms_value,
        },
        "label_registers": labels,
        "inference": inference,
    }

    if args.emit_json:
        sys.stdout.write(json.dumps(report, indent=2) + "\n")
    else:
        sys.stdout.write(_format_text(report))
    return 0


# --- Inference (pure, tested in isolation) ----------------------------------


def infer_wire_unit(
    *,
    panel_value: float,
    panel_unit: Unit,
    comms_value: float | None,
    epsilon: float,
) -> InferenceReport:
    """Decide which scale the wire is on.

    Compares ``comms_value`` against the panel reading converted to
    both °C and °F. Whichever conversion matches within ``epsilon``
    is the wire scale. If neither matches, returns
    ``verdict="inconclusive"`` with the raw deltas so the operator
    can interpret manually.

    Pure function: no I/O, no global state. Returns a JSON-friendly
    dict.
    """
    if comms_value is None:
        return {
            "verdict": "no_comms_value",
            "detail": (
                "the reference parameter did not return a numeric value; "
                "check the parameter name and that the device responds"
            ),
        }
    panel_in_c = to_celsius(panel_value, panel_unit)
    panel_in_f = to_fahrenheit(panel_value, panel_unit)
    delta_c = abs(comms_value - panel_in_c)
    delta_f = abs(comms_value - panel_in_f)
    matches_c = delta_c <= epsilon
    matches_f = delta_f <= epsilon

    base: InferenceReport = {
        "verdict": "inconclusive",
        "panel_as_celsius": round(panel_in_c, 4),
        "panel_as_fahrenheit": round(panel_in_f, 4),
        "delta_celsius": round(delta_c, 4),
        "delta_fahrenheit": round(delta_f, 4),
        "epsilon": epsilon,
    }
    if matches_c and matches_f:
        # Only possible when panel_value happens to be the °C == °F
        # crossover (-40°). Flag it loudly.
        base["verdict"] = "ambiguous"
        base["detail"] = (
            "panel reading is on the °C/°F crossover (e.g. -40); "
            "pick a non-crossover reference and retry"
        )
        return base
    if matches_c:
        base["verdict"] = "celsius"
        base["recommendation"] = "open_device(..., assert_wire_temperature_unit=Unit.CELSIUS)"
        return base
    if matches_f:
        base["verdict"] = "fahrenheit"
        base["recommendation"] = "open_device(..., assert_wire_temperature_unit=Unit.FAHRENHEIT)"
        return base
    base["verdict"] = "inconclusive"
    base["detail"] = (
        "comms readback does not match the panel reading in either "
        "scale within epsilon; the parameter may not be the one shown "
        "on the panel, or the device may be mid-transition"
    )
    return base


def to_celsius(value: float, unit: Unit) -> float:
    if unit is Unit.CELSIUS:
        return value
    if unit is Unit.FAHRENHEIT:
        return (value - 32.0) * 5.0 / 9.0
    raise ValueError(f"not a temperature unit: {unit!r}")


def to_fahrenheit(value: float, unit: Unit) -> float:
    if unit is Unit.FAHRENHEIT:
        return value
    if unit is Unit.CELSIUS:
        return value * 9.0 / 5.0 + 32.0
    raise ValueError(f"not a temperature unit: {unit!r}")


# --- I/O helpers ------------------------------------------------------------


async def _read_identity(
    ctl: Controller,
    *,
    timeout: float,
) -> IdentityReport:
    """Capture part-number / firmware-id for the report header."""
    try:
        info = await ctl.identify(timeout=timeout)
    except WatlowError as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {
        "part_number": info.part_number.raw or None,
        "family": info.family.value,
        "hardware_id": info.hardware_id,
        "firmware_id": info.firmware_id,
        "protocol": info.protocol.value,
        "address": info.address,
    }


async def _read_label_registers(
    ctl: Controller,
    *,
    timeout: float,
) -> list[LabelRegisterRow]:
    """Read every C/F-coded label register; return JSON-friendly rows."""
    rows: list[LabelRegisterRow] = []
    for pid, name in _UNIT_LABEL_REGISTERS:
        row: LabelRegisterRow = {"parameter_id": pid, "name": name, "status": "pending"}
        try:
            entry = await ctl.read_parameter(pid, timeout=timeout)
        except WatlowError as exc:
            row["status"] = _UNREACHABLE
            row["error"] = f"{type(exc).__name__}: {exc}"
            rows.append(row)
            continue
        raw_value = entry.value
        row["status"] = "ok"
        row["raw_value"] = raw_value
        decoded: Unit | None
        decoded = (
            unit_from_display_code(int(raw_value)) if isinstance(raw_value, int | float) else None
        )
        row["decoded"] = decoded.value if decoded is not None else None
        rows.append(row)
    return rows


async def _read_reference_value(
    ctl: Controller,
    *,
    name: str,
    instance: int,
    timeout: float,
) -> float | None:
    """Read the parameter the user is comparing against. ``None`` on miss."""
    try:
        entry = await ctl.read_parameter(name, instance=instance, timeout=timeout)
    except WatlowError:
        return None
    if isinstance(entry.value, int | float):
        return float(entry.value)
    return None


# --- Reporting --------------------------------------------------------------


def _format_text(report: ProbeUnitReport) -> str:
    """Render ``report`` as a human-readable text block."""
    out: list[str] = []
    device = report["device"]
    out.append("watlow-diag probe-unit")
    out.append("=" * 56)
    if "error" not in device:
        out.append(
            f"Device: {device.get('part_number') or '(no part number)'} "
            f"family={device.get('family')} "
            f"hw={device.get('hardware_id')} "
            f"fw={device.get('firmware_id')} "
            f"protocol={device.get('protocol')} "
            f"address={device.get('address')}",
        )
    else:
        err = device.get("error", "?")
        out.append(f"Device: identify failed ({err})")
    out.append("")

    ref = report["reference"]
    out.append("Reference comparison:")
    out.append(
        f"  panel shows : {ref['panel_shows']} {ref['panel_unit']}",
    )
    out.append(
        f"  comms reads : {ref['comms_value']!r} ({ref['parameter']} instance={ref['instance']})",
    )
    out.append("")

    out.append("Label-register readback (forensic; not authoritative):")
    label_rows = report["label_registers"]
    for row in label_rows:
        pid = row["parameter_id"]
        name = row["name"]
        status = row["status"]
        if status == "ok":
            raw_value = row.get("raw_value")
            decoded = row.get("decoded") or "?"
            out.append(
                f"  {pid:<5} {name!s:<18} raw={raw_value!r:<6} decoded={decoded}",
            )
        else:
            out.append(
                f"  {pid:<5} {name!s:<18} {status}: {row.get('error', '?')}",
            )
    out.append("")

    inf = report["inference"]
    if inf:
        verdict = inf.get("verdict")
        out.append(f"Inference: {verdict}")
        if "panel_as_celsius" in inf:
            out.append(
                f"  panel-as-°C = {inf['panel_as_celsius']}, "
                f"delta vs comms = {inf['delta_celsius']}",
            )
            out.append(
                f"  panel-as-°F = {inf['panel_as_fahrenheit']}, "
                f"delta vs comms = {inf['delta_fahrenheit']}",
            )
        if "recommendation" in inf:
            out.append("")
            out.append(f"  → {inf['recommendation']}")
        elif "detail" in inf:
            out.append(f"  detail: {inf['detail']}")
    return "\n".join(out) + "\n"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
