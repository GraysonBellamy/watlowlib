"""Unit vocabulary for Watlow parameters.

Two enums:

- :class:`Unit` — the concrete display unit a value is reported in
  (``°C`` / ``°F`` / ``%``). Attached to :class:`Reading.unit` and
  :class:`Sample.unit`.
- :class:`UnitKind` — the structural unit *family* of a parameter as
  declared in the registry JSON (``temperature`` / ``percent`` /
  ``dimensionless`` / ``enumeration`` / ``string``). Used by
  :func:`resolve_unit` to compute the concrete :class:`Unit` for a
  given device display-unit setting.

Watlow's PM has **two** display-unit registers (3005 panel, 17050
comms); 17050 is the unit the device uses on the wire and is therefore
the one we tag readings with. See ``docs/units-plan.md`` for the
rationale and pitfalls.
"""

from __future__ import annotations

from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, overload

from watlowlib.errors import WatlowValidationError

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "Unit",
    "UnitKind",
    "coerce_unit",
    "display_code_for_unit",
    "resolve_unit",
    "unit_from_display_code",
]


class Unit(StrEnum):
    """Concrete display unit attached to a :class:`Reading` value."""

    CELSIUS = "C"
    FAHRENHEIT = "F"
    PERCENT = "%"


class UnitKind(StrEnum):
    """Structural unit family of a parameter, as declared by the registry.

    Maps to a concrete :class:`Unit` at read time via
    :func:`resolve_unit`. ``TEMPERATURE`` resolves to °C or °F depending
    on the device's comms display setting (parameter 17050); the rest
    are independent of device state.
    """

    TEMPERATURE = "temperature"
    PERCENT = "percent"
    DIMENSIONLESS = "dimensionless"
    ENUMERATION = "enumeration"
    STRING = "string"


# Device enumeration codes for parameter 17050 (Communications - Display
# Units). Source: pm_parameters.json range field "°F (30), °C (15)".
_DISPLAY_UNIT_CODES: Mapping[int, Unit] = MappingProxyType(
    {
        15: Unit.CELSIUS,
        30: Unit.FAHRENHEIT,
    },
)

#: Reverse of :data:`_DISPLAY_UNIT_CODES` for the setter path. Excludes
#: :attr:`Unit.PERCENT` — the display-units register is temperature-only.
_DISPLAY_CODE_FOR_UNIT: Mapping[Unit, int] = MappingProxyType(
    {unit: code for code, unit in _DISPLAY_UNIT_CODES.items()},
)


_UNIT_STRING_ALIASES: Mapping[str, Unit] = MappingProxyType(
    {
        "c": Unit.CELSIUS,
        "celsius": Unit.CELSIUS,
        "degc": Unit.CELSIUS,
        "°c": Unit.CELSIUS,
        "f": Unit.FAHRENHEIT,
        "fahrenheit": Unit.FAHRENHEIT,
        "degf": Unit.FAHRENHEIT,
        "°f": Unit.FAHRENHEIT,
        "%": Unit.PERCENT,
        "percent": Unit.PERCENT,
        "pct": Unit.PERCENT,
    },
)


def unit_from_display_code(code: int) -> Unit | None:
    """Return the display unit for a raw 17050 device code, if known."""
    return _DISPLAY_UNIT_CODES.get(code)


def display_code_for_unit(unit: Unit) -> int | None:
    """Return the raw 17050 device code for a temperature display unit."""
    return _DISPLAY_CODE_FOR_UNIT.get(unit)


@overload
def coerce_unit(value: Unit) -> Unit: ...


@overload
def coerce_unit(value: str) -> Unit: ...


def coerce_unit(value: object) -> Unit:
    """Normalise a :class:`Unit`-or-string into a :class:`Unit`.

    Case-insensitive on the string side. Raises
    :class:`WatlowValidationError` on an unknown alias so the setter
    can fail pre-I/O before any wire bytes go out.

    Raw integer device codes (15, 30) are **not** accepted — callers
    who want the lower-level path use ``write_parameter("display_units", 30)``.
    """
    if isinstance(value, Unit):
        return value
    if not isinstance(value, str):
        raise WatlowValidationError(
            f"unit must be a Unit or string alias, got {type(value).__name__}",
        )
    alias = value.strip().lower()
    try:
        return _UNIT_STRING_ALIASES[alias]
    except KeyError as exc:
        raise WatlowValidationError(
            f"unknown unit alias: {value!r}",
        ) from exc


def resolve_unit(kind: UnitKind, display_unit: Unit | None) -> Unit | None:
    """Resolve a parameter's :class:`UnitKind` to a concrete :class:`Unit`.

    - ``TEMPERATURE`` → the device's current display unit (may be
      ``None`` if the device rejected the 17050 read).
    - ``PERCENT`` → :attr:`Unit.PERCENT`.
    - Everything else → ``None``.

    Pure mapping; no I/O.
    """
    if kind is UnitKind.TEMPERATURE:
        return display_unit
    if kind is UnitKind.PERCENT:
        return Unit.PERCENT
    return None
