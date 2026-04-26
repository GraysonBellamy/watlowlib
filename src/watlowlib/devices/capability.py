"""Three small enums that set the contract between layers.

- :class:`SafetyTier` — derived from RWES; gates ``confirm=True`` writes.
- :class:`Capability` — coarse hardware-feature bitmap. Bits are added
  when a captured family needs them and existing values stay stable.
- :class:`Availability` — per-command session cache state.

This module is **leaf** — it imports nothing from
:mod:`watlowlib.devices` siblings, so the registry and command layers
can pull these enums without an import cycle. See ``docs/design.md``
§5b.
"""

from __future__ import annotations

from enum import Flag, IntEnum, StrEnum, auto
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from watlowlib.registry.families import ControllerFamily

__all__ = [
    "Availability",
    "Capability",
    "SafetyTier",
    "capabilities_for_family",
]


class SafetyTier(IntEnum):
    """How dangerous a command is to invoke.

    - ``READ_ONLY`` (R) — no state change.
    - ``STATEFUL`` — runtime state change but not EEPROM-backed.
      Reserved for commands like "start autotune"; no PM parameter
      maps here today, but the tier exists so future commands have a
      place to live.
    - ``PERSISTENT`` (RW / RWE / RWES) — EEPROM-backed; requires
      ``confirm=True`` at the facade.
    """

    READ_ONLY = 0
    STATEFUL = 1
    PERSISTENT = 2


class Capability(Flag):
    """Coarse hardware capability bits.

    Bits are derived from a decoded part number when one is available
    (see :func:`watlowlib.registry.families.capabilities_for_part_number`)
    and fall back to a per-family prior otherwise. The session widens
    the set at runtime when a command succeeds against a parameter
    that proves the capability.

    The vocabulary is small on purpose — most Watlow gating is by
    :class:`watlowlib.registry.families.ControllerFamily` and by
    :attr:`watlowlib.registry.parameters.ParameterSpec.parameter_id`,
    not by per-feature bits. New bits are added when captured family
    behaviour requires them.
    """

    NONE = 0
    PROFILE = auto()  # ramp / soak engine (F4T / RM)
    LIMIT = auto()  # over/under-temperature limit module
    HAS_COOLING = auto()  # second control output present (output_2 != 'A')
    HAS_MODBUS = auto()  # comms position-8 includes the Modbus stack
    HAS_PROFILES = auto()  # control_type supports the profile engine
    HAS_BLUETOOTH = auto()  # comms position-8 includes Bluetooth
    HAS_ETHERNET = auto()  # comms position-8 includes Ethernet


class Availability(StrEnum):
    """Per-command session state.

    Sticky for the session: once a command transitions to
    :attr:`UNSUPPORTED`, the session short-circuits subsequent
    invocations with a typed error pre-I/O. The transition table
    lives in ``docs/design.md`` §5b.
    """

    UNKNOWN = "unknown"  # never tried this session
    SUPPORTED = "supported"  # observed working this session
    UNSUPPORTED = "unsupported"  # device rejected with a "no such" code


# --- Per-family capability priors -----------------------------------

# Seeded conservatively per design §5b: the prior carries only what is
# true for *every* SKU in the family. Per-SKU bits (cooling, modbus,
# profile, bluetooth, ethernet) are decoded from the part number once
# ``identify()`` reads it; the prior is what callers see when no part
# number has been captured yet (e.g. AUTO-detected device that didn't
# expose the part_number parameter).
_FAMILY_PRIORS_RAW: dict[str, Capability] = {
    "pm": Capability.NONE,
    "rm": Capability.PROFILE | Capability.HAS_PROFILES,  # RM ships with the ramp/soak engine
    "st": Capability.NONE,
    "ezzone_limit": Capability.LIMIT,
    "f4t": Capability.PROFILE | Capability.HAS_PROFILES,
    "unknown": Capability.NONE,
}


def capabilities_for_family(family: ControllerFamily) -> Capability:
    """Return the capability prior for ``family``.

    The session promotes observed capabilities at runtime and the part-
    number decoder fills in per-SKU bits via
    :func:`watlowlib.registry.families.capabilities_for_part_number`.
    PM is intentionally :attr:`Capability.NONE` because PM SKUs vary
    across every dimension (cooling / modbus / profile / comms).
    """
    return _FAMILY_PRIORS.get(family.value, Capability.NONE)


_FAMILY_PRIORS: Mapping[str, Capability] = MappingProxyType(_FAMILY_PRIORS_RAW)
