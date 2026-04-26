"""Controller families and per-family part-number decoders.

The :class:`ControllerFamily` enum is the family discriminator used
across the library. :func:`classify_family` parses the leading
characters of a part number to that enum; :func:`decode_part_number`
runs the family's full decoder when one is registered.

The EZ-ZONE PM decoder is the only full per-family decoder today.
Other families fall through to the discriminator-only stub.
"""

from __future__ import annotations

import re
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping

    from watlowlib.devices.capability import Capability
    from watlowlib.devices.models import PartNumber

__all__ = [
    "ControllerFamily",
    "capabilities_for_part_number",
    "classify_family",
    "decode_part_number",
    "default_loops",
    "pm_comms_code",
    "pm_comms_supports_modbus",
]


class ControllerFamily(StrEnum):
    """Watlow controller family discriminator.

    Membership here is **advisory** — :class:`watlowlib.devices.session.Session`
    treats family hints as priors, not gates, unless the session was
    opened with ``strict=True``. See ``docs/design.md`` §5b.
    """

    PM = "pm"  # EZ-ZONE PM (reference family; full part-number decoder)
    RM = "rm"  # EZ-ZONE RM
    ST = "st"  # EZ-ZONE ST
    EZZONE_LIMIT = "ezzone_limit"  # EZ-ZONE Limit
    F4T = "f4t"  # F4T
    UNKNOWN = "unknown"


# --- Family discriminator -------------------------------------------


def classify_family(part_number: str) -> ControllerFamily:
    """Return the :class:`ControllerFamily` for a part-number string.

    Only the leading family discriminator is parsed; per-family digit
    decoding is in :func:`decode_part_number`.
    """
    head = part_number.strip().upper()
    if head.startswith("PM"):
        return ControllerFamily.PM
    if head.startswith("RM"):
        return ControllerFamily.RM
    if head.startswith("ST"):
        return ControllerFamily.ST
    if head.startswith("F4T"):
        return ControllerFamily.F4T
    return ControllerFamily.UNKNOWN


# --- EZ-ZONE PM decoder ---------------------------------------------

# PM ordering format (see EZ-ZONE PM user guide ch. 9):
#
#   P M [size] [control] [power] [out1] [out2] - [opt 1..7]
#
# Position 1-2: "PM"
# Position 3:   case size — 3 = 1/16 DIN, 6 = 1/4 DIN, 8 = 1/8 DIN,
#               9 = 1/32 DIN. Other digits (4) appear in some
#               datasheets but are rare; the decoder keeps the literal
#               character and lets callers interpret it.
# Position 4:   control type — C controller, R ramp/soak controller,
#               E integrated limit+control, L limit only, V process-
#               value display, U dual-loop controller (some PM6+).
# Position 5:   power input — 1 = 100-240 VAC, 3 = 24 V AC/DC.
# Positions 6-7: output 1 / 2 codes (per the order matrix). Stored
#               verbatim — the order matrix has dozens of entries and
#               we don't bake the lookup table; callers that need
#               the human label resolve from Watlow's order guide.
# Trailing "-": options string (7 chars in stock SKUs but the count
#               varies); kept verbatim. Capture A-Z0-9 *and* dashes
#               so multi-segment options pass through whole.
_PM_PART_RE = re.compile(
    r"""
    ^PM
    (?P<size>[A-Z0-9])
    (?P<control>[A-Z])
    (?P<power>[A-Z0-9])
    (?P<output_1>[A-Z0-9])
    (?P<output_2>[A-Z0-9])
    (?:-(?P<options>[A-Z0-9-]+))?
    $
    """,
    re.VERBOSE,
)

# Decoded labels for the small enumerations we *do* know with
# confidence; everything else stays as the raw character so a future
# update can extend the table without breaking callers.
_PM_CONTROL_LABELS: Mapping[str, str] = MappingProxyType(
    {
        "C": "controller",
        "R": "ramp_soak_controller",
        "E": "integrated_limit_control",
        "L": "limit",
        "V": "process_value_display",
        "U": "dual_loop_controller",
    }
)
_PM_POWER_LABELS: Mapping[str, str] = MappingProxyType(
    {
        "1": "100-240VAC",
        "3": "24V_AC_DC",
    }
)

# Default number of control loops by PM (case_size, control_type).
# PM3 / PM9 are single-loop in every documented configuration. PM6 /
# PM8 default to single-loop for C / R / E / L / V; "U" (dual-loop)
# unlocks two loops. Loop count drives the public ``loop(n)`` validator
# on :class:`Controller`; better to seed conservatively and let the
# registry's ``max_instance`` widen the cap when callers reach for
# loop 2 explicitly.
_PM_LOOPS: Mapping[tuple[str, str], int] = MappingProxyType(
    {
        # case_size, control_type → loops
        ("3", "C"): 1,
        ("3", "R"): 1,
        ("3", "E"): 1,
        ("3", "L"): 1,
        ("3", "V"): 1,
        ("6", "U"): 2,
        ("8", "U"): 2,
        ("9", "U"): 2,
    }
)


def _decode_pm(raw: str) -> tuple[ControllerFamily, dict[str, str]]:
    """Parse a PM part-number string. Unrecognised tails fall through.

    Returns a tuple of ``(family, details)``. ``family`` is always
    :attr:`ControllerFamily.PM` — the caller has already classified —
    and ``details`` is a flat ``str → str`` map carrying the parsed
    fragments. Unparsed inputs return an empty ``details`` so callers
    can still construct a :class:`PartNumber` from the bare family.
    """
    head = raw.strip().upper()
    m = _PM_PART_RE.match(head)
    if m is None:
        return ControllerFamily.PM, {}
    parts = m.groupdict()
    details: dict[str, str] = {
        "case_size": parts["size"],
        "control_type": parts["control"],
        "power_input": parts["power"],
        "output_1": parts["output_1"],
        "output_2": parts["output_2"],
    }
    if parts.get("options"):
        details["options"] = parts["options"]
    # Friendly labels for the enumerations we recognise.
    label = _PM_CONTROL_LABELS.get(parts["control"])
    if label is not None:
        details["control_label"] = label
    plabel = _PM_POWER_LABELS.get(parts["power"])
    if plabel is not None:
        details["power_label"] = plabel
    return ControllerFamily.PM, details


# Position-8 comms code → capability bits, per Watlow EZ-ZONE PM
# ordering guide. Position 8 is the *first* character of the options
# string (positions 1-7 are "PM" + 5 mandatory chars; position 8
# starts the 7-char options block). Position 8 = 'A' means
# "no comms option ordered; Standard Bus still included" — common on
# bench / OEM SKUs.
_PM_COMMS_MODBUS: frozenset[str] = frozenset({"1", "2", "B", "D", "E", "F"})
_PM_COMMS_BLUETOOTH: frozenset[str] = frozenset({"B", "E", "F", "G", "H", "J", "K"})
_PM_COMMS_ETHERNET: frozenset[str] = frozenset({"3", "G"})

# Control-type characters that include the profile / ramp-soak engine.
_PM_CONTROL_PROFILES: frozenset[str] = frozenset({"R", "B", "E", "N", "T"})

# output_2 == 'A' means "none" — no second control output. Anything
# else is some flavour of cooling output.
_PM_OUTPUT_NONE = "A"


def pm_comms_code(part: PartNumber) -> str | None:
    """Return the position-8 comms character of a PM part number, or ``None``.

    Position 8 is the first character of the 7-char options block —
    e.g. ``PM3R1CA-AAAAAAA`` has comms code ``A`` (Standard Bus only).
    Returns ``None`` for non-PM families and for PM part numbers
    without a parsed options string.
    """
    if part.family is not ControllerFamily.PM:
        return None
    options = part.details.get("options", "")
    if not options:
        return None
    return options[0]


def pm_comms_supports_modbus(part: PartNumber) -> bool:
    """Whether the part's comms position-8 character carries Modbus."""
    code = pm_comms_code(part)
    return code is not None and code in _PM_COMMS_MODBUS


def capabilities_for_part_number(part: PartNumber) -> Capability:
    """Decode :class:`Capability` bits from a parsed :class:`PartNumber`.

    PM is the only family decoded today; other families return the
    family prior unchanged. Bits derived here are *facts about the
    SKU* — they do not depend on the device responding to any
    particular query, only on the part-number string.

    Returns the family prior OR-ed with any decoded bits, so callers
    can use this as the authoritative seed for
    :attr:`DeviceInfo.capabilities` after :meth:`Controller.identify`
    captures the part number.
    """
    # Imported here to keep families.py a leaf module — the capability
    # enum lives under devices/ but families/ is depended on by
    # registry/parameters.py at import time.
    from watlowlib.devices.capability import (  # noqa: PLC0415
        Capability as _Capability,
    )
    from watlowlib.devices.capability import (  # noqa: PLC0415
        capabilities_for_family,
    )

    caps = capabilities_for_family(part.family)
    if part.family is not ControllerFamily.PM:
        return caps

    output_2 = part.details.get("output_2", "")
    if output_2 and output_2 != _PM_OUTPUT_NONE:
        caps |= _Capability.HAS_COOLING

    control = part.details.get("control_type", "")
    if control in _PM_CONTROL_PROFILES:
        caps |= _Capability.HAS_PROFILES | _Capability.PROFILE

    code = pm_comms_code(part)
    if code is not None:
        if code in _PM_COMMS_MODBUS:
            caps |= _Capability.HAS_MODBUS
        if code in _PM_COMMS_BLUETOOTH:
            caps |= _Capability.HAS_BLUETOOTH
        if code in _PM_COMMS_ETHERNET:
            caps |= _Capability.HAS_ETHERNET

    return caps


def decode_part_number(raw: str) -> PartNumber:
    """Decode ``raw`` into a populated :class:`PartNumber`.

    Dispatches to the per-family decoder based on
    :func:`classify_family`. Families without a decoder fall through
    to a bare :class:`PartNumber` carrying only the family
    discriminator.
    """
    # Imported here so this module stays a dependency-light leaf — the
    # ``models`` module pulls a handful of registry types and we don't
    # want a circular import on the family enum.
    from watlowlib.devices.models import PartNumber  # noqa: PLC0415

    family = classify_family(raw)
    if family is ControllerFamily.PM:
        _, details = _decode_pm(raw)
        return PartNumber(raw=raw, family=family, details=MappingProxyType(details))
    return PartNumber(raw=raw, family=family, details=MappingProxyType({}))


def default_loops(part: PartNumber) -> int:
    """Return the default loop count for the controller behind ``part``.

    Used by :class:`Controller.identify` to seed
    :attr:`DeviceInfo.loops` and by :meth:`Controller.loop` to
    validate the ``n`` argument. Returns ``1`` whenever the family or
    digits are unknown — never raises.
    """
    if part.family is ControllerFamily.PM:
        case = part.details.get("case_size", "")
        ctrl = part.details.get("control_type", "")
        return _PM_LOOPS.get((case, ctrl), 1)
    # RM is multi-loop in production but no decoder is wired up yet;
    # default to 1 so callers don't get surprise behaviour.
    return 1
