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
    ParameterRegistry,
    ParameterSpec,
    RwesFlag,
    load_pm_parameters,
)
from watlowlib.registry.units import OutputUnit, TemperatureUnit

__all__ = [
    "DEFAULT_ALIASES",
    "PARAMETERS",
    "ControllerFamily",
    "EnumerationRow",
    "OutputUnit",
    "ParameterRegistry",
    "ParameterSpec",
    "RwesFlag",
    "TemperatureUnit",
    "classify_family",
    "load_enumerations",
    "load_pm_parameters",
]
