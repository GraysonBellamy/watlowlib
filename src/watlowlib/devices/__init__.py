"""Device facade — :class:`Controller`, :class:`Session`, and dataclasses.

The facade is the public surface; everything else
(:mod:`watlowlib.protocol`, :mod:`watlowlib.commands`,
:mod:`watlowlib.registry`) is implementation detail callers don't
have to import.
"""

from __future__ import annotations

from watlowlib.devices.capability import Availability, Capability, SafetyTier
from watlowlib.devices.controller import Controller
from watlowlib.devices.discovery import (
    DEFAULT_DISCOVERY_ADDRESSES,
    DEFAULT_DISCOVERY_BAUDRATES,
    DEFAULT_DISCOVERY_PROTOCOLS,
    find_devices,
)
from watlowlib.devices.factory import open_controller, open_device
from watlowlib.devices.kind import ControllerFamily, classify_family
from watlowlib.devices.loop import ControllerLoop
from watlowlib.devices.models import (
    AlarmState,
    DeviceInfo,
    FindResult,
    LoopState,
    ParameterEntry,
    PartNumber,
    Reading,
)
from watlowlib.devices.session import Session

__all__ = [
    "DEFAULT_DISCOVERY_ADDRESSES",
    "DEFAULT_DISCOVERY_BAUDRATES",
    "DEFAULT_DISCOVERY_PROTOCOLS",
    "AlarmState",
    "Availability",
    "Capability",
    "Controller",
    "ControllerFamily",
    "ControllerLoop",
    "DeviceInfo",
    "FindResult",
    "LoopState",
    "ParameterEntry",
    "PartNumber",
    "Reading",
    "SafetyTier",
    "Session",
    "classify_family",
    "find_devices",
    "open_controller",
    "open_device",
]
