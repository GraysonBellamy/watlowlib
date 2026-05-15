"""Pint-compatible unit-string export.

:func:`to_pint` maps a :class:`watlowlib.Unit` (or a recognised alias
string) into a pint-compatible unit string. ``pint`` itself is **not**
a runtime dependency of :mod:`watlowlib` — this helper returns plain
strings so downstream tools that use pint can feed them straight into
``pint.UnitRegistry.parse_expression``, while consumers that don't
use pint can ignore the output.

Lossy by design: if a unit has gauge/absolute or any other
distinction pint doesn't model, the loss is accepted — :func:`to_pint`
returns the lossy string. Callers that need the disambiguator handle
it themselves.

The mapping table here mirrors the table that previously lived in
``capa/src/capa/devices/watlow.py``. Moving it into :mod:`watlowlib`
means every consumer (capa, the in-house dashboards, downstream
clients) shares one source of truth.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

from watlowlib.errors import WatlowValidationError
from watlowlib.registry.units import Unit, coerce_unit

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = ["to_pint"]


#: Every :class:`Unit` enum value maps to a pint-compatible string.
#: Watlow's vocabulary is tiny — temperature (°C / °F) plus percent.
_WATLOW_UNIT_TO_PINT: Mapping[Unit, str] = MappingProxyType(
    {
        Unit.CELSIUS: "degC",
        Unit.FAHRENHEIT: "degF",
        Unit.PERCENT: "percent",
    },
)


def to_pint(unit: Unit | str | None) -> str | None:
    """Return a pint-compatible unit string.

    Accepts a :class:`Unit`, a case-insensitive string alias, or
    ``None``. Returns:

    - ``None`` when ``unit`` is ``None`` or when a string alias is
      unrecognised (lossy by design — callers that care distinguish
      ``None`` themselves).
    - ``"degC"`` / ``"degF"`` / ``"percent"`` for the three Watlow
      units.

    String inputs go through :func:`watlowlib.registry.units.coerce_unit`
    so capitalisation and the common aliases (``"C"``, ``"celsius"``,
    ``"degC"``, ``"°C"``, ...) all map the same way as the rest of
    the library.
    """
    if unit is None:
        return None
    try:
        resolved = coerce_unit(unit)
    except WatlowValidationError:
        return None
    return _WATLOW_UNIT_TO_PINT.get(resolved)
