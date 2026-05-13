"""Unit vocabulary for Watlow parameters.

Two enums:

- :class:`Unit` — the concrete unit a temperature/percent value is
  reported in (``°C`` / ``°F`` / ``%``). Attached to
  :class:`Reading.unit` and :class:`Sample.unit`.
- :class:`UnitKind` — the structural unit *family* of a parameter as
  declared in the registry JSON (``temperature`` / ``percent`` /
  ``dimensionless`` / ``enumeration`` / ``string``). Used by
  :func:`resolve_unit` to compute the concrete :class:`Unit` for a
  temperature parameter given the (separately-determined) wire scale.

Watlow PM controllers expose **two** display-unit registers — 3005
("Display - Units", front panel) and 17050 ("Communications - Display
Units"). On at least one PM3 firmware (id 5678), **17050 is label-
only**: writing it changes the enum reported when 17050 is read back
but does not change the scale of values exchanged over comms. The
internal storage unit (the scale temperatures actually travel in over
the wire) is governed by something else — and on devices where it
cannot be determined empirically, the library refuses to guess.

Consequence for this module: :func:`resolve_unit` no longer assumes
17050 is the wire scale. The caller (the session) supplies an
explicit ``temperature_unit`` derived from the
``assert_wire_temperature_unit`` user-assertion (or ``None`` when no
assertion was made). ``Reading.unit = None`` is the honest answer for
temperature reads when the wire scale is unknown.

See ``docs/devices.md`` §Units for the user-facing contract.
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
    who want the lower-level path use
    ``write_parameter("display_units", 30)``.
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


def resolve_unit(kind: UnitKind, temperature_unit: Unit | None) -> Unit | None:
    """Resolve a parameter's :class:`UnitKind` to a concrete :class:`Unit`.

    - ``TEMPERATURE`` → ``temperature_unit`` (passes the caller's
      asserted wire scale through, or ``None`` when none was asserted).
    - ``PERCENT`` → :attr:`Unit.PERCENT`.
    - Everything else → ``None``.

    Pure mapping; no I/O. The caller (typically
    :class:`watlowlib.devices.session.Session`) is responsible for
    determining the wire scale and passing it in.
    """
    if kind is UnitKind.TEMPERATURE:
        return temperature_unit
    if kind is UnitKind.PERCENT:
        return Unit.PERCENT
    return None
