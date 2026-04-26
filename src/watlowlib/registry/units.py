"""Compact unit vocabulary for Watlow parameters.

Watlow doesn't have anything like Alicat's gas zoo. Most parameters
are unitless or temperature; output values are percent. The enums are
small intentionally — each one comes from an observed field on a real
PM3 capture or a parameter whose unit is forced by Watlow firmware
(e.g. percent for output).

Bound to :class:`watlowlib.devices.models.Reading.unit` by the command
variants.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["OutputUnit", "TemperatureUnit"]


class TemperatureUnit(StrEnum):
    """Display unit for a temperature parameter."""

    CELSIUS = "C"
    FAHRENHEIT = "F"


class OutputUnit(StrEnum):
    """Display unit for an output parameter."""

    PERCENT = "%"
