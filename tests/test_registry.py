"""Registry loader, alias resolution, and RWES → SafetyTier derivation."""

from __future__ import annotations

from typing import Any, cast

import pytest

from watlowlib import (
    PARAMETERS,
    SafetyTier,
    Unit,
    UnitKind,
    WatlowValidationError,
)
from watlowlib.errors import WatlowProtocolError
from watlowlib.protocol.stdbus import DataType
from watlowlib.registry import RwesFlag, classify_family
from watlowlib.registry.families import ControllerFamily
from watlowlib.registry.parameters import _build_spec  # pyright: ignore[reportPrivateUsage]
from watlowlib.registry.units import coerce_unit, resolve_unit


def test_registry_loads_pm_parameters() -> None:
    # PM3 captures show ~400+ usable rows after stripping placeholder
    # / member-based entries; pick a stable lower bound that still
    # catches a regression that nukes the loader.
    assert len(PARAMETERS) > 100


def test_setpoint_resolves_to_7001() -> None:
    spec = PARAMETERS.resolve("setpoint")
    assert spec.parameter_id == 7001
    assert spec.cls == 7
    assert spec.member == 1
    assert spec.data_type is DataType.FLOAT
    assert spec.rwes is RwesFlag.RWES
    assert spec.safety is SafetyTier.PERSISTENT


def test_pv_alias_resolves() -> None:
    pv = PARAMETERS.resolve("pv")
    assert pv.parameter_id == 4001
    assert pv.name == "process_value"


def test_resolve_by_int_id() -> None:
    spec = PARAMETERS.resolve(1001)
    assert spec.name == "hardware_id"
    assert spec.data_type is DataType.S32
    assert spec.safety is SafetyTier.READ_ONLY


def test_unknown_name_raises() -> None:
    with pytest.raises(WatlowValidationError, match="unknown parameter name"):
        PARAMETERS.resolve("not_a_real_parameter")


def test_unknown_id_raises() -> None:
    with pytest.raises(WatlowValidationError, match="unknown parameter id"):
        PARAMETERS.resolve(99999)


def test_safety_tier_derivation() -> None:
    # R → READ_ONLY
    assert PARAMETERS.resolve("hardware_id").safety is SafetyTier.READ_ONLY
    # RWES → PERSISTENT
    assert PARAMETERS.resolve("setpoint").safety is SafetyTier.PERSISTENT


def test_validate_instance_in_range() -> None:
    spec = PARAMETERS.resolve("setpoint")
    PARAMETERS.validate_instance(spec, 1)
    PARAMETERS.validate_instance(spec, spec.max_instance)


def test_validate_instance_out_of_range() -> None:
    spec = PARAMETERS.resolve("setpoint")
    with pytest.raises(WatlowValidationError, match="instance"):
        PARAMETERS.validate_instance(spec, 0)
    with pytest.raises(WatlowValidationError, match="instance"):
        PARAMETERS.validate_instance(spec, spec.max_instance + 1)


def test_classify_family_pm() -> None:
    assert classify_family("PM3R1CA-AAAAAAA") is ControllerFamily.PM
    assert classify_family("pm6") is ControllerFamily.PM


def test_classify_family_unknown() -> None:
    assert classify_family("ABC123") is ControllerFamily.UNKNOWN
    assert classify_family("") is ControllerFamily.UNKNOWN


def test_heat_proportional_band_range_includes_thousands_separators() -> None:
    """The PM JSON range string is ``0.001 to 9,999.000`` — thousand separators.

    Pre-fix, the range regex stopped at the comma and yielded
    ``(0.001, 9.0)`` — so the device returning a real-world PB of
    124.75 (typical for a thermocouple input in °F) would be rejected
    by ``validate_value`` on write-back. Hardware-day-2026-04-26
    findings §2.3.
    """
    spec = PARAMETERS.resolve("heat_proportional_band")
    assert spec.range_min == pytest.approx(0.001)
    assert spec.range_max is not None
    assert spec.range_max >= 9999.0
    # Round-tripping a real device-reported value must not raise.
    PARAMETERS.validate_value(spec, 124.75)


def test_cool_proportional_band_range_includes_thousands_separators() -> None:
    spec = PARAMETERS.resolve("cool_proportional_band")
    assert spec.range_max is not None
    assert spec.range_max >= 9999.0


# ---------------------------------------------------------------------------
# Unit metadata + resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("process_value", UnitKind.TEMPERATURE),
        ("setpoint", UnitKind.TEMPERATURE),
        ("cool_hysteresis", UnitKind.TEMPERATURE),  # easy to miss — see units-plan
        ("output_power", UnitKind.PERCENT),
        ("fixed_power", UnitKind.PERCENT),
        ("display_units", UnitKind.ENUMERATION),  # parameter 17050
        ("units", UnitKind.ENUMERATION),  # parameter 3005, panel
        ("part_number", UnitKind.STRING),
        ("device_name", UnitKind.STRING),
        ("hardware_id", UnitKind.DIMENSIONLESS),
    ],
)
def test_unit_kind_classification(name: str, expected: UnitKind) -> None:
    spec = PARAMETERS.resolve(name)
    assert spec.unit_kind is expected


def test_display_units_id_is_17050() -> None:
    """Plan asserts: ``read_parameter("display_units")`` targets 17050, not 3005."""
    spec = PARAMETERS.resolve("display_units")
    assert spec.parameter_id == 17050


def test_units_id_is_3005() -> None:
    """``read_parameter("units")`` targets 3005 (front-panel), not the colliding LNR row."""
    spec = PARAMETERS.resolve("units")
    assert spec.parameter_id == 3005


def test_loader_rejects_missing_unit_kind() -> None:
    """Loading a row with no ``unit_kind`` field fails loud."""
    row = {
        "parameter_id": 99999,
        "class_id": 99,
        "member_id": 99,
        "name": "Fake - Stub",
        "data_type": "IEEE Float",
        "rwes": "R",
        # unit_kind omitted on purpose
    }
    with pytest.raises(WatlowProtocolError, match="unit_kind"):
        _build_spec(row)


def test_loader_rejects_unknown_unit_kind() -> None:
    """A typo in ``unit_kind`` surfaces at load time, not silently at read time."""
    row = {
        "parameter_id": 99998,
        "class_id": 99,
        "member_id": 98,
        "name": "Fake - Stub",
        "data_type": "IEEE Float",
        "unit_kind": "kelvin",  # not a real UnitKind
        "rwes": "R",
    }
    with pytest.raises(WatlowProtocolError, match="kelvin"):
        _build_spec(row)


# resolve_unit truth table — pure mapping, no I/O.
def test_resolve_unit_temperature_takes_display_unit() -> None:
    assert resolve_unit(UnitKind.TEMPERATURE, Unit.FAHRENHEIT) is Unit.FAHRENHEIT
    assert resolve_unit(UnitKind.TEMPERATURE, Unit.CELSIUS) is Unit.CELSIUS


def test_resolve_unit_temperature_passes_none_through() -> None:
    """When the device rejected 17050, temperature readings get ``unit=None``."""
    assert resolve_unit(UnitKind.TEMPERATURE, None) is None


def test_resolve_unit_percent_is_percent() -> None:
    assert resolve_unit(UnitKind.PERCENT, None) is Unit.PERCENT
    assert resolve_unit(UnitKind.PERCENT, Unit.FAHRENHEIT) is Unit.PERCENT


@pytest.mark.parametrize("kind", [UnitKind.DIMENSIONLESS, UnitKind.ENUMERATION, UnitKind.STRING])
def test_resolve_unit_other_kinds_return_none(kind: UnitKind) -> None:
    assert resolve_unit(kind, Unit.FAHRENHEIT) is None


# coerce_unit alias table.
@pytest.mark.parametrize(
    ("alias", "expected"),
    [
        ("C", Unit.CELSIUS),
        ("c", Unit.CELSIUS),
        ("celsius", Unit.CELSIUS),
        ("degC", Unit.CELSIUS),
        ("°C", Unit.CELSIUS),
        ("F", Unit.FAHRENHEIT),
        ("Fahrenheit", Unit.FAHRENHEIT),
        ("degF", Unit.FAHRENHEIT),
        ("%", Unit.PERCENT),
        ("percent", Unit.PERCENT),
    ],
)
def test_coerce_unit_accepts_aliases(alias: str, expected: Unit) -> None:
    assert coerce_unit(alias) is expected


def test_coerce_unit_passes_unit_through() -> None:
    assert coerce_unit(Unit.CELSIUS) is Unit.CELSIUS


@pytest.mark.parametrize("bad", ["kelvin", "rankine", "k", ""])
def test_coerce_unit_rejects_unknown_alias(bad: str) -> None:
    with pytest.raises(WatlowValidationError, match="unknown unit alias"):
        coerce_unit(bad)


def test_coerce_unit_rejects_int() -> None:
    """Raw enumeration codes belong on the lower-level write_parameter path."""
    with pytest.raises(WatlowValidationError):
        coerce_unit(cast("Any", 30))
