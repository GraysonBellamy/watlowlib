"""Friendly aliases for canonical parameter names.

Aliases like ``pv`` → ``process_value`` and ``sp`` → ``setpoint`` back
the public ``read_parameter("pv")`` / ``read_parameter("setpoint")``
entry points. The :class:`watlowlib.registry.parameters.ParameterRegistry`
consults this table when resolving a string that doesn't match a
canonical name directly. Aliases are case-insensitive.

Adding new aliases is non-breaking — registry resolution is lookup,
not generative.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["DEFAULT_ALIASES"]


_RAW: dict[str, str] = {
    "pv": "process_value",
    "process_val": "process_value",
    "sp": "setpoint",
    "set_point": "setpoint",
    "out": "output",
    "out_pct": "output",
    "fw": "firmware_id",
    "firmware": "firmware_id",
    "hw": "hardware_id",
    "hardware": "hardware_id",
    "part": "part_number",
    "model": "part_number",
}

#: Default alias → canonical-name table. Read-only at runtime.
DEFAULT_ALIASES: Mapping[str, str] = MappingProxyType({k.lower(): v for k, v in _RAW.items()})
