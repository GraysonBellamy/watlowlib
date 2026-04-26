"""Registry loader, alias resolution, and RWES → SafetyTier derivation."""

from __future__ import annotations

import pytest

from watlowlib import (
    PARAMETERS,
    SafetyTier,
    WatlowValidationError,
)
from watlowlib.protocol.stdbus import DataType
from watlowlib.registry import RwesFlag, classify_family
from watlowlib.registry.families import ControllerFamily


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
