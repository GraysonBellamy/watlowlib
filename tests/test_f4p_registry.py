"""Unit tests for the Series F4P registry and its decimal-register scaling.

Hardware-free: parses ``f4p_parameters.json``, drives the Modbus read
variant directly against :data:`F4P_PARAMETERS`, and exercises
:meth:`ParameterRegistry.with_decimal_places`, the mechanism that lets
``f4p_identify`` derive the temperature scale from register 606
instead of a constant in the JSON.
"""

from __future__ import annotations

import json
import math
from importlib.resources import files

import pytest

from watlowlib import (
    F4P_PARAMETERS,
    SafetyTier,
    UnitKind,
    WatlowValidationError,
)
from watlowlib.commands import READ_PARAMETER, CommandContext, ReadParameterRequest
from watlowlib.protocol.modbus import ModbusFn
from watlowlib.protocol.stdbus.tlv import DataType
from watlowlib.registry import RwesFlag
from watlowlib.registry.families import ControllerFamily

CTX = CommandContext(registry=F4P_PARAMETERS)
#: The registry as ``f4p_identify`` rebinds it on a unit showing one decimal.
TENTHS = F4P_PARAMETERS.with_decimal_places(606, 1)
CTX_TENTHS = CommandContext(registry=TENTHS)

#: Temperature rows whose scale is fixed rather than driven by register
#: 606: the IR-301 manual's cold-junction value, tenths in the system
#: unit (confirmed on the bench against the F4P's own CJC register).
_FIXED_SCALE_TEMPERATURES = frozenset({104})


def _raw_rows() -> list[dict[str, object]]:
    text = files("watlowlib.data").joinpath("f4p_parameters.json").read_text(encoding="utf-8")
    rows: list[dict[str, object]] = json.loads(text)
    return rows


# --- JSON shape ----------------------------------------------------


def test_f4p_json_rows_are_well_formed() -> None:
    rows = _raw_rows()
    assert len(rows) >= 60
    required = {
        "parameter_id",
        "canonical",
        "name",
        "description",
        "range",
        "default",
        "data_type",
        "unit_kind",
        "rwes",
        "relative_addr",
        "absolute_addr",
        "max_instance",
        "sheet",
    }
    seen_ids: set[int] = set()
    seen_names: set[str] = set()
    for row in rows:
        missing = required - row.keys()
        assert not missing, f"row {row.get('parameter_id')} missing {sorted(missing)}"
        pid = int(str(row["parameter_id"]))
        assert pid not in seen_ids, f"duplicate parameter_id {pid}"
        seen_ids.add(pid)
        name = str(row["canonical"])
        assert name not in seen_names, f"duplicate canonical {name!r}"
        seen_names.add(name)
        # Bare-register map: the register *is* the parameter id, and the
        # absolute address is the 4xxxx holding-register form.
        assert row["relative_addr"] == pid
        assert row["absolute_addr"] == 400001 + pid
        assert row["data_type"] in ("signed 16-bit", "unsigned 16-bit")
        # Read-only slice: writes wait on the blackbody owner.
        assert row["rwes"] == "R"
        if "decimal_register" in row:
            assert row["decimal_register"] == 606
            # The live scale is derived at identify time; a static scale
            # on the same row would be contradictory.
            assert "scale" not in row


def test_f4p_registry_loads_every_row_read_only() -> None:
    assert len(F4P_PARAMETERS) == len(_raw_rows())
    for spec in F4P_PARAMETERS:
        assert spec.rwes is RwesFlag.R
        assert spec.safety is SafetyTier.READ_ONLY
        assert spec.family_hints == frozenset({ControllerFamily.F4P})
        assert spec.register_count == 1
        assert spec.max_instance == 1


# --- Canonical names -----------------------------------------------


@pytest.mark.parametrize(
    ("name", "pid"),
    [
        ("process_value", 100),
        ("pv", 100),
        ("setpoint", 300),
        ("sp", 300),
        ("output_power", 103),
        ("units", 901),
        ("input_error", 101),
        ("auto_manual", 200),
        ("alarm_1_status", 102),
        ("alarm_1_low_setpoint", 302),
        ("alarm_1_high_setpoint", 303),
        ("alarm_2_low_setpoint", 321),
        ("alarm_2_high_setpoint", 322),
        ("model_code", 0),
        ("serial_number_high", 1),
        ("serial_number_low", 2),
        ("software_id", 3),
        ("input_1_decimal", 606),
        ("show_temperature_unit", 1923),
    ],
)
def test_f4p_canonical_names_and_aliases(name: str, pid: int) -> None:
    assert F4P_PARAMETERS.resolve(name).parameter_id == pid
    assert F4P_PARAMETERS.resolve(pid).parameter_id == pid


def test_f4p_shares_canonical_names_with_pm_and_sd() -> None:
    """A generic adapter can ask every family for the same workhorse names."""
    from watlowlib import PARAMETERS, SD_PARAMETERS

    for name in ("process_value", "setpoint", "output_power"):
        for registry in (PARAMETERS, SD_PARAMETERS, F4P_PARAMETERS):
            assert registry.has(name), name


# --- Decimal-register binding ----------------------------------------


def test_f4p_temperature_rows_follow_register_606() -> None:
    """Every temperature row except the documented fixed-scale ones is 606-bound."""
    for spec in F4P_PARAMETERS:
        if spec.unit_kind is not UnitKind.TEMPERATURE:
            assert spec.decimal_register is None, spec.name
            continue
        if spec.parameter_id in _FIXED_SCALE_TEMPERATURES:
            assert spec.decimal_register is None
            assert spec.scale == 0.1
            continue
        assert spec.decimal_register == 606, spec.name
        # Factory default: no decimal places until identify reads 606.
        assert spec.scale == 1.0, spec.name


def test_with_decimal_places_rescales_only_bound_rows() -> None:
    assert TENTHS is not F4P_PARAMETERS
    assert TENTHS.resolve("process_value").scale == 0.1
    assert TENTHS.resolve("setpoint").scale == 0.1
    assert TENTHS.resolve("alarm_1_low_setpoint").scale == 0.1
    assert TENTHS.resolve("proportional_band_1a").scale == 0.1
    # Fixed-scale and unscaled rows are untouched (and shared).
    assert TENTHS.resolve("output_power") is F4P_PARAMETERS.resolve("output_power")
    assert TENTHS.resolve("units") is F4P_PARAMETERS.resolve("units")
    assert TENTHS.resolve("cold_junction_temperature").scale == 0.1
    # The source registry is never mutated.
    assert F4P_PARAMETERS.resolve("process_value").scale == 1.0


def test_with_decimal_places_keeps_every_lookup_path() -> None:
    """Names, aliases, and ids all resolve to the *same* rescaled spec."""
    by_name = TENTHS.resolve("process_value")
    assert TENTHS.resolve("pv") is by_name
    assert TENTHS.resolve(100) is by_name
    assert len(TENTHS) == len(F4P_PARAMETERS)
    assert [s.parameter_id for s in TENTHS] == [s.parameter_id for s in F4P_PARAMETERS]


@pytest.mark.parametrize(("places", "scale"), [(0, 1.0), (1, 0.1), (2, 0.01), (3, 0.001)])
def test_with_decimal_places_scale_matches_register_606_codes(places: int, scale: float) -> None:
    rescaled = F4P_PARAMETERS.with_decimal_places(606, places)
    assert rescaled.resolve("process_value").scale == scale


def test_with_decimal_places_is_identity_when_nothing_changes() -> None:
    assert F4P_PARAMETERS.with_decimal_places(606, 0) is F4P_PARAMETERS
    # A register no row is bound to changes nothing either.
    assert F4P_PARAMETERS.with_decimal_places(616, 2) is F4P_PARAMETERS
    # Re-applying the same setting to an already rescaled registry is a no-op.
    assert TENTHS.with_decimal_places(606, 1) is TENTHS


def test_with_decimal_places_rejects_negative() -> None:
    with pytest.raises(WatlowValidationError, match="non-negative"):
        F4P_PARAMETERS.with_decimal_places(606, -1)


# --- Modbus decode path --------------------------------------------


def test_f4p_process_value_decodes_verified_frame_word() -> None:
    """Reply ``0a 03 02 17 52 93 88`` carries 0x1752 = 5970 → 597.0 °C at one decimal."""
    assert READ_PARAMETER.modbus is not None
    entry = READ_PARAMETER.modbus.decode(
        (0x1752,),
        CTX_TENTHS,
        ReadParameterRequest("process_value"),
    )
    assert isinstance(entry.value, float)
    assert math.isclose(entry.value, 597.0)
    assert entry.spec.unit_kind is UnitKind.TEMPERATURE
    # Before identify the same word is the raw integer.
    raw = READ_PARAMETER.modbus.decode((0x1752,), CTX, ReadParameterRequest("process_value"))
    assert raw.value == 5970
    assert isinstance(raw.value, int)


def test_f4p_alarm_deviation_is_signed_and_scaled() -> None:
    """Register 302 read as 0xFFFB (two's complement -5) → -0.5 °C deviation."""
    assert READ_PARAMETER.modbus is not None
    spec = F4P_PARAMETERS.resolve("alarm_1_low_setpoint")
    assert spec.data_type is DataType.S16
    entry = READ_PARAMETER.modbus.decode(
        (0xFFFB,),
        CTX_TENTHS,
        ReadParameterRequest("alarm_1_low_setpoint"),
    )
    assert isinstance(entry.value, float)
    assert math.isclose(entry.value, -0.5)


def test_f4p_output_power_scales_tenths() -> None:
    assert READ_PARAMETER.modbus is not None
    entry = READ_PARAMETER.modbus.decode((130,), CTX, ReadParameterRequest("output_power"))
    assert isinstance(entry.value, float)
    assert math.isclose(entry.value, 13.0)
    assert entry.spec.unit_kind is UnitKind.PERCENT


def test_f4p_power_limits_scale_tenths_like_output_power() -> None:
    """Register 714 read 450 on the bench: a 45.0 % cap, not 450 %."""
    assert READ_PARAMETER.modbus is not None
    entry = READ_PARAMETER.modbus.decode(
        (450,), CTX, ReadParameterRequest("output_1a_high_power_limit")
    )
    assert isinstance(entry.value, float)
    assert math.isclose(entry.value, 45.0)
    assert entry.spec.unit_kind is UnitKind.PERCENT


def test_f4p_cjc_register_is_untagged_tenths() -> None:
    """Register 1500 reports °F whatever register 901 says, so it carries no unit."""
    assert READ_PARAMETER.modbus is not None
    spec = F4P_PARAMETERS.resolve("cold_junction_temperature")
    assert spec.unit_kind is UnitKind.DIMENSIONLESS
    assert spec.decimal_register is None
    entry = READ_PARAMETER.modbus.decode((711,), CTX, ReadParameterRequest(1500))
    assert isinstance(entry.value, float)
    assert math.isclose(entry.value, 71.1)


def test_f4p_enum_reads_stay_int() -> None:
    assert READ_PARAMETER.modbus is not None
    for name, word in (
        ("alarm_1_status", 2),
        ("auto_manual", 1),
        ("units", 1),
        ("model_code", 5280),
    ):
        entry = READ_PARAMETER.modbus.decode((word,), CTX_TENTHS, ReadParameterRequest(name))
        assert entry.value == word
        assert isinstance(entry.value, int)
        assert not isinstance(entry.value, bool)


def test_f4p_read_op_is_single_holding_register() -> None:
    assert READ_PARAMETER.modbus is not None
    op = READ_PARAMETER.modbus.encode(CTX, ReadParameterRequest("process_value"))
    assert op.fn is ModbusFn.READ_HOLDING
    assert op.address == 100
    assert op.count == 1
