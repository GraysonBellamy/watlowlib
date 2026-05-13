"""Public dataclasses returned by the :class:`Controller` facade.

All frozen, ``slots=True``. ``py.typed`` ships.

See ``docs/design.md`` §6a.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from watlowlib.devices.capability import Capability
    from watlowlib.protocol.base import ProtocolKind
    from watlowlib.registry.families import ControllerFamily
    from watlowlib.registry.parameters import ParameterSpec
    from watlowlib.registry.units import Unit
    from watlowlib.transport.base import SerialSettings

__all__ = [
    "AlarmState",
    "DeviceHealth",
    "DeviceInfo",
    "DiscoveryResult",
    "LoopState",
    "ParameterEntry",
    "PartNumber",
    "Reading",
]


class DeviceHealth(StrEnum):
    """Outcome of an :meth:`Controller.identify` call.

    Used by callers (the maintenance verify pass, the configure CLI,
    discovery rows) to distinguish "the device answered every probe"
    from "the device answered some probes but not the load-bearing
    part-number read." Sentinel values stay the same enum across the
    public API so downstream code can branch on it.
    """

    OK = "ok"  # every identity probe succeeded
    PARTIAL = "partial"  # part_number captured but a secondary field missed
    FAILED = "failed"  # part_number could not be read — capability decoding skipped


@dataclass(frozen=True, slots=True)
class Reading:
    """A single timestamped value from the controller.

    ``protocol`` is set by the variant decoder, not by the facade —
    it reflects which wire protocol produced the value (per
    ``docs/design.md`` invariant 7).
    """

    value: float | None
    unit: Unit | None
    received_at: datetime
    monotonic_ns: int
    raw: bytes
    protocol: ProtocolKind


@dataclass(frozen=True, slots=True)
class ParameterEntry:
    """Generic registry-driven read/write result.

    Returned by :data:`watlowlib.commands.READ_PARAMETER` and
    :data:`watlowlib.commands.WRITE_PARAMETER`. The
    :class:`Controller` translates an entry into a :class:`Reading` /
    :class:`PartNumber` / etc. when the public API guarantees a
    richer shape.
    """

    spec: ParameterSpec
    instance: int
    value: float | int | str | bool | None
    raw: bytes


@dataclass(frozen=True, slots=True)
class PartNumber:
    """Parsed part-number string returned by ``read_part_number``.

    Per-family digit decoding is contributed by
    :mod:`watlowlib.registry.families`. Decoded fragments live in
    :attr:`details` as a free-form mapping so each family can populate
    only what its ordering format defines, and so adding fragments to
    the PM decoder later is non-breaking.

    The EZ-ZONE PM decoder populates case size, control type, power
    input, three output codes, and options string. Other families fall
    through to a stub: only :attr:`family` is set, and :attr:`details`
    is empty.
    """

    raw: str
    family: ControllerFamily
    details: Mapping[str, str] = field(default_factory=lambda: {})


@dataclass(frozen=True, slots=True)
class AlarmState:
    """Decoded alarm bits for one loop."""

    loop: int
    high: bool | None
    low: bool | None
    silenced: bool | None
    raw_bits: int


@dataclass(frozen=True, slots=True)
class LoopState:
    """Snapshot of one loop. Composed from several reads."""

    loop: int
    pv: Reading
    setpoint: Reading
    output_pct: float | None
    raw: Mapping[str, bytes] = field(default_factory=lambda: {})


@dataclass(frozen=True, slots=True)
class DeviceInfo:
    """Identity + connection metadata for an open controller.

    Returned by :meth:`Controller.identify`. Capabilities are decoded
    from the part number when one is captured (see
    :func:`watlowlib.registry.families.capabilities_for_part_number`)
    and OR-ed with the family prior; unobserved bits stay zero rather
    than being guessed.

    ``protocol`` is the wire protocol the host is *currently* talking;
    ``configured_protocol`` is what the device's persistent EEPROM
    parameter (PM 17009) reports. They normally match, but when they
    diverge the helper :attr:`protocol_mismatch` flags it — useful
    for catching SKU/firmware combinations where the user wrote a new
    protocol but the runtime stack didn't pick it up (e.g. comms
    position-8 = 'A', no Modbus stack present even though 17009 reads
    1057).
    """

    part_number: PartNumber
    hardware_id: int | None
    firmware_id: int | None
    serial_number: str | None
    family: ControllerFamily
    protocol: ProtocolKind
    address: int
    capabilities: Capability
    serial_settings: SerialSettings
    loops: int
    health: DeviceHealth = DeviceHealth.OK
    configured_protocol: ProtocolKind | None = None

    @property
    def protocol_mismatch(self) -> bool:
        """``True`` when EEPROM says one protocol and we're talking another.

        Always ``False`` when :attr:`configured_protocol` is ``None``
        (i.e. ``identify`` did not query parameter 17009).
        """
        return (
            self.configured_protocol is not None and self.configured_protocol is not self.protocol
        )


@dataclass(frozen=True, slots=True)
class DiscoveryResult:
    """One row from the discovery sweep."""

    port: str
    serial_settings: SerialSettings
    address: int
    protocol: ProtocolKind | None
    info: DeviceInfo | None
    error: object | None  # WatlowError; typed as object to avoid the import cycle
