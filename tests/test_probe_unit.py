"""Tests for ``watlow-diag probe-unit``.

The inference math is a pure function with no I/O; the I/O wrapper is
covered by a CLI smoke test that runs against a FakeTransport-backed
controller (via ``--fixture`` is not enough — the existing PM3 fixture
doesn't include the 17050 / 3005 reads — so we exercise the dispatcher
arg-validation path here and leave the live-device verification to the
operator).
"""

from __future__ import annotations

import pytest

from watlowlib.cli.diagnostics import main as diag_main
from watlowlib.cli.diagnostics.probe_unit import (
    infer_wire_unit,
    to_celsius,
    to_fahrenheit,
)
from watlowlib.registry.units import Unit

# ---------------------------------------------------------------------------
# Pure conversion helpers
# ---------------------------------------------------------------------------


def test_to_celsius_identity_for_celsius() -> None:
    assert to_celsius(50.0, Unit.CELSIUS) == 50.0


def test_to_celsius_converts_fahrenheit() -> None:
    assert to_celsius(122.0, Unit.FAHRENHEIT) == pytest.approx(50.0)


def test_to_fahrenheit_identity_for_fahrenheit() -> None:
    assert to_fahrenheit(122.0, Unit.FAHRENHEIT) == 122.0


def test_to_fahrenheit_converts_celsius() -> None:
    assert to_fahrenheit(50.0, Unit.CELSIUS) == pytest.approx(122.0)


def test_to_celsius_rejects_percent() -> None:
    with pytest.raises(ValueError, match="not a temperature unit"):
        to_celsius(1.0, Unit.PERCENT)


# ---------------------------------------------------------------------------
# infer_wire_unit — the core decision
# ---------------------------------------------------------------------------


def test_infer_wire_unit_matches_fahrenheit_when_comms_in_fahrenheit() -> None:
    """The handoff scenario: panel reads 50°C, comms returns 122.0.

    The library should report wire=FAHRENHEIT and recommend the
    matching ``assert_wire_temperature_unit=`` kwarg.
    """
    result = infer_wire_unit(
        panel_value=50.0,
        panel_unit=Unit.CELSIUS,
        comms_value=122.0,
        epsilon=0.2,
    )
    assert result["verdict"] == "fahrenheit"
    assert "FAHRENHEIT" in str(result["recommendation"])


def test_infer_wire_unit_matches_celsius_when_comms_matches_panel_in_celsius() -> None:
    """Panel reads 50°C, comms returns 50.0 — wire is in CELSIUS."""
    result = infer_wire_unit(
        panel_value=50.0,
        panel_unit=Unit.CELSIUS,
        comms_value=50.0,
        epsilon=0.2,
    )
    assert result["verdict"] == "celsius"
    assert "CELSIUS" in str(result["recommendation"])


def test_infer_wire_unit_panel_in_fahrenheit_comms_in_celsius() -> None:
    """Panel reads 122°F, comms returns 50.0 — wire is in CELSIUS."""
    result = infer_wire_unit(
        panel_value=122.0,
        panel_unit=Unit.FAHRENHEIT,
        comms_value=50.0,
        epsilon=0.2,
    )
    assert result["verdict"] == "celsius"


def test_infer_wire_unit_inconclusive_when_no_match() -> None:
    """Comms readback doesn't match either scale → ``inconclusive``."""
    result = infer_wire_unit(
        panel_value=50.0,
        panel_unit=Unit.CELSIUS,
        comms_value=999.0,
        epsilon=0.2,
    )
    assert result["verdict"] == "inconclusive"
    assert "recommendation" not in result
    assert "detail" in result


def test_infer_wire_unit_ambiguous_at_minus_40() -> None:
    """At -40°, °C and °F are equal — the probe must flag ambiguity."""
    result = infer_wire_unit(
        panel_value=-40.0,
        panel_unit=Unit.CELSIUS,
        comms_value=-40.0,
        epsilon=0.2,
    )
    assert result["verdict"] == "ambiguous"


def test_infer_wire_unit_no_comms_value() -> None:
    """A failed parameter read short-circuits to a clear no-comms verdict."""
    result = infer_wire_unit(
        panel_value=50.0,
        panel_unit=Unit.CELSIUS,
        comms_value=None,
        epsilon=0.2,
    )
    assert result["verdict"] == "no_comms_value"


def test_infer_wire_unit_epsilon_tightens_matches() -> None:
    """A tiny ``epsilon`` must reject panel-rounding-equivalent matches."""
    # Panel reading 50°C, comms reading 50.5 — within 0.5 of celsius but
    # not within 0.1.
    loose = infer_wire_unit(
        panel_value=50.0,
        panel_unit=Unit.CELSIUS,
        comms_value=50.5,
        epsilon=1.0,
    )
    assert loose["verdict"] == "celsius"
    tight = infer_wire_unit(
        panel_value=50.0,
        panel_unit=Unit.CELSIUS,
        comms_value=50.5,
        epsilon=0.1,
    )
    assert tight["verdict"] == "inconclusive"


def test_infer_wire_unit_records_deltas_in_output() -> None:
    """The report keeps both panel-as-°C and panel-as-°F deltas for diagnostics."""
    result = infer_wire_unit(
        panel_value=50.0,
        panel_unit=Unit.CELSIUS,
        comms_value=122.0,
        epsilon=0.2,
    )
    assert "panel_as_celsius" in result
    assert "panel_as_fahrenheit" in result
    assert "delta_celsius" in result
    assert "delta_fahrenheit" in result


# ---------------------------------------------------------------------------
# CLI surface
# ---------------------------------------------------------------------------


def test_diag_probe_unit_listed_in_dispatcher_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``watlow-diag --help`` advertises probe-unit."""
    with pytest.raises(SystemExit) as exc_info:
        diag_main(["--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    assert "probe-unit" in out


def test_diag_probe_unit_rejects_percent_panel_unit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--panel-unit %`` is rejected pre-I/O with a helpful message."""
    # Use --fixture so the CLI doesn't try to open a real serial port;
    # the panel-unit validation happens before any I/O, so the fixture
    # content doesn't matter.
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "pm3_stdbus_pv_setpoint.jsonl"
    rc = diag_main(
        [
            "probe-unit",
            "_",
            "--fixture",
            str(fixture),
            "--panel-shows",
            "50",
            "--panel-unit",
            "%",
        ],
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "temperature scale" in err.lower()


def test_diag_probe_unit_rejects_unknown_panel_unit_alias(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``--panel-unit kelvin`` raises a clean WatlowError exit (rc=1)."""
    from pathlib import Path

    fixture = Path(__file__).parent / "fixtures" / "pm3_stdbus_pv_setpoint.jsonl"
    rc = diag_main(
        [
            "probe-unit",
            "_",
            "--fixture",
            str(fixture),
            "--panel-shows",
            "50",
            "--panel-unit",
            "kelvin",
        ],
    )
    assert rc == 1
    err = capsys.readouterr().err
    assert "unknown unit alias" in err.lower() or "kelvin" in err.lower()
