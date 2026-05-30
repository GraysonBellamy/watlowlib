"""Parameter and family registry.

The registry is the cross-protocol seam: each parameter row carries
both Std Bus selector (``cls`` / ``member`` / ``instance``) and Modbus
selector (``relative_addr`` / ``absolute_addr`` / ``register_count``)
metadata so command variants can lower a single
``read_parameter(...)`` call to either protocol with no per-parameter
bespoke code. See ``docs/design.md`` §5a.
"""

from __future__ import annotations

from watlowlib.registry.aliases import DEFAULT_ALIASES
from watlowlib.registry.enumerations import EnumerationRow, load_enumerations
from watlowlib.registry.families import ControllerFamily, classify_family
from watlowlib.registry.parameters import (
    PARAMETERS,
    SD_PARAMETERS,
    ParameterRegistry,
    ParameterSpec,
    RwesFlag,
    load_parameters,
    load_pm_parameters,
    load_sd_parameters,
)
from watlowlib.registry.units import (
    Unit,
    UnitKind,
    coerce_unit,
    display_code_for_unit,
    resolve_unit,
    unit_from_display_code,
)

__all__ = [
    "DEFAULT_ALIASES",
    "PARAMETERS",
    "SD_PARAMETERS",
    "ControllerFamily",
    "EnumerationRow",
    "ParameterRegistry",
    "ParameterSpec",
    "RwesFlag",
    "Unit",
    "UnitKind",
    "classify_family",
    "coerce_unit",
    "display_code_for_unit",
    "load_enumerations",
    "load_parameters",
    "load_pm_parameters",
    "load_sd_parameters",
    "resolve_unit",
    "unit_from_display_code",
]
