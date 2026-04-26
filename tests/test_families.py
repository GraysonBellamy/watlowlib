"""PM part-number decoder tests.

Covers :func:`classify_family`, :func:`decode_part_number`,
:func:`default_loops`, and :func:`capabilities_for_family` against a
range of real PM SKUs and a few negative cases (foreign families,
malformed strings).
"""

from __future__ import annotations

import pytest

from watlowlib import (
    Capability,
    ControllerFamily,
    PartNumber,
    classify_family,
)
from watlowlib.devices.capability import capabilities_for_family
from watlowlib.registry.families import decode_part_number, default_loops


def test_classify_family_pm() -> None:
    assert classify_family("PM3R1CA-AAAAAAA") is ControllerFamily.PM
    assert classify_family("pm6c1aa-aaaaaaa") is ControllerFamily.PM


def test_classify_family_other_known() -> None:
    assert classify_family("RM4E1AA") is ControllerFamily.RM
    assert classify_family("ST1A1AA") is ControllerFamily.ST
    assert classify_family("F4T1XXX") is ControllerFamily.F4T


def test_classify_family_unknown() -> None:
    assert classify_family("") is ControllerFamily.UNKNOWN
    assert classify_family("XYZ") is ControllerFamily.UNKNOWN
    assert classify_family("WATLOW") is ControllerFamily.UNKNOWN


# --- decode_part_number ---------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected_size", "expected_control", "expected_loops"),
    [
        # Captured PM3 single-loop ramp-soak controller.
        ("PM3R1CA-AAAAAAA", "3", "R", 1),
        # PM3 plain controller.
        ("PM3C1AA-AAAAAAA", "3", "C", 1),
        # PM6 dual-loop controller (the "U" code).
        ("PM6U1AA-AAAAAAA", "6", "U", 2),
        # PM9 process-value display (single loop).
        ("PM9V1AA", "9", "V", 1),
        # PM3 limit module.
        ("PM3L1AA-AAAAAAA", "3", "L", 1),
    ],
)
def test_decode_pm_known_skus(
    raw: str,
    expected_size: str,
    expected_control: str,
    expected_loops: int,
) -> None:
    part = decode_part_number(raw)
    assert part.raw == raw
    assert part.family is ControllerFamily.PM
    assert part.details.get("case_size") == expected_size
    assert part.details.get("control_type") == expected_control
    assert default_loops(part) == expected_loops


def test_decode_pm_carries_options_string() -> None:
    part = decode_part_number("PM3R1CA-AAAAAAA")
    assert part.details.get("options") == "AAAAAAA"


def test_decode_pm_friendly_labels() -> None:
    """Recognised control + power digits get human-readable labels."""
    part = decode_part_number("PM3R1CA-AAAAAAA")
    assert part.details.get("control_label") == "ramp_soak_controller"
    assert part.details.get("power_label") == "100-240VAC"


def test_decode_pm_unknown_control_letter_keeps_raw() -> None:
    """Unknown control letters stay in the raw fragment without a friendly label."""
    part = decode_part_number("PM3Z1AA-AAAAAAA")
    assert part.details.get("control_type") == "Z"
    assert "control_label" not in part.details


def test_decode_pm_malformed_falls_through() -> None:
    """Strings that don't match the PM ordering pattern still classify as PM."""
    part = decode_part_number("PM_GARBAGE")
    assert part.family is ControllerFamily.PM
    assert part.details == {}  # decoder bailed cleanly


def test_decode_non_pm_passes_through_family() -> None:
    """Non-PM families decode to a bare PartNumber until their decoder lands."""
    part = decode_part_number("RM4E1AA")
    assert part.family is ControllerFamily.RM
    assert part.details == {}
    # default_loops on an unknown decoder returns 1 (not a guess).
    assert default_loops(part) == 1


def test_decode_empty_string() -> None:
    part = decode_part_number("")
    assert part.family is ControllerFamily.UNKNOWN
    assert part.details == {}


# --- capability priors ----------------------------------------------


def test_capability_priors_pm_seed_none() -> None:
    """PM gets ``Capability.NONE`` because SKUs vary per dimension."""
    assert capabilities_for_family(ControllerFamily.PM) is Capability.NONE


def test_capability_priors_rm_seed_profile() -> None:
    """RM is a documented ramp/soak family — gets PROFILE + HAS_PROFILES.

    The prior carries both bits because RM ships with the ramp/soak
    engine on every SKU. ``HAS_PROFILES`` is the per-SKU bit derived
    from a PM part number's control_type; ``PROFILE`` is the
    family-level prior from before the per-SKU decoder. Both are
    present here so callers gating on either form see the bit set.
    """
    caps = capabilities_for_family(ControllerFamily.RM)
    assert Capability.PROFILE in caps
    assert Capability.HAS_PROFILES in caps


def test_capability_priors_limit_seed() -> None:
    assert capabilities_for_family(ControllerFamily.EZZONE_LIMIT) is Capability.LIMIT


def test_capability_priors_unknown_seed_none() -> None:
    assert capabilities_for_family(ControllerFamily.UNKNOWN) is Capability.NONE


def test_partnumber_default_details_is_empty_mapping() -> None:
    """Backwards-compatible: ``PartNumber(raw=..., family=...)`` still works."""
    part = PartNumber(raw="X", family=ControllerFamily.UNKNOWN)
    assert part.details == {}


# --- Per-SKU capability decode (post-2026-04-26) ------------------------


@pytest.mark.parametrize(
    ("part_str", "expected_present", "expected_absent"),
    [
        # Std-Bus-only: comms position 8 = 'A' → no Modbus, no BT, no Eth.
        # output_2 = 'A' → no cooling. control_type = 'R' → profile/has_profiles.
        (
            "PM3R1CA-AAAAAAA",
            (Capability.HAS_PROFILES, Capability.PROFILE),
            (
                Capability.HAS_MODBUS,
                Capability.HAS_COOLING,
                Capability.HAS_BLUETOOTH,
                Capability.HAS_ETHERNET,
            ),
        ),
        # Modbus-equipped (comms position 8 = '1'), cool output (output_2 != 'A').
        (
            "PM6C1JJ-1AAAAAA",
            (Capability.HAS_MODBUS, Capability.HAS_COOLING),
            (Capability.HAS_PROFILES, Capability.HAS_BLUETOOTH, Capability.HAS_ETHERNET),
        ),
        # Bluetooth + Modbus (position 8 = 'B').
        (
            "PM6C1JJ-BAAAAAA",
            (Capability.HAS_MODBUS, Capability.HAS_BLUETOOTH, Capability.HAS_COOLING),
            (Capability.HAS_PROFILES, Capability.HAS_ETHERNET),
        ),
        # Ethernet (position 8 = '3').
        (
            "PM6C1JJ-3AAAAAA",
            (Capability.HAS_ETHERNET, Capability.HAS_COOLING),
            (Capability.HAS_MODBUS, Capability.HAS_PROFILES, Capability.HAS_BLUETOOTH),
        ),
    ],
)
def test_capabilities_for_part_number_pm_decodes_position_8_and_outputs(
    part_str: str,
    expected_present: tuple[Capability, ...],
    expected_absent: tuple[Capability, ...],
) -> None:
    """Decoded part-number capabilities match the Watlow ordering guide.

    ``PM3R1CA-AAAAAAA`` is the canonical "Std-Bus-only, single-output"
    SKU (comms position 8 = ``A`` → no Modbus / no Bluetooth / no
    Ethernet; output_2 = ``A`` → no cooling). The other rows exercise
    the comms code (Modbus / BT / Ethernet) and cooling-output
    dimensions independently.
    """
    from watlowlib.registry.families import capabilities_for_part_number

    part = decode_part_number(part_str)
    caps = capabilities_for_part_number(part)
    for bit in expected_present:
        assert bit in caps, f"{part_str!r} should set {bit!r}; got {caps!r}"
    for bit in expected_absent:
        assert bit not in caps, f"{part_str!r} should not set {bit!r}; got {caps!r}"


def test_pm_comms_supports_modbus_helper() -> None:
    """The position-8 → Modbus helper agrees with the capability decoder."""
    from watlowlib.registry.families import (
        capabilities_for_part_number,
        pm_comms_supports_modbus,
    )

    # Std-Bus-only.
    part_a = decode_part_number("PM3R1CA-AAAAAAA")
    assert pm_comms_supports_modbus(part_a) is False
    assert Capability.HAS_MODBUS not in capabilities_for_part_number(part_a)

    # Modbus-equipped.
    part_b = decode_part_number("PM6C1JJ-1AAAAAA")
    assert pm_comms_supports_modbus(part_b) is True
    assert Capability.HAS_MODBUS in capabilities_for_part_number(part_b)
