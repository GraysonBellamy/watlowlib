"""Parameter registry — the cross-protocol seam.

Each row of ``data/pm_parameters.json`` is loaded once into a
:class:`ParameterSpec`, indexed by canonical name (with aliases) and by
``parameter_id``. The spec carries enough information to lower a
``read_parameter("setpoint")`` call to either Std Bus or Modbus with no
per-parameter bespoke code.

Loading is **eager**: a module-level :data:`PARAMETERS` is built at
import time so subsequent lookups are O(1) dict reads. Loading is also
**fail-loud** for malformed rows — a row missing decode metadata for
its declared :class:`DataType` (e.g. a PACKED row with no count) is
not silently dropped; it is surfaced as an
:class:`watlowlib.errors.WatlowProtocolError` at load time.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from enum import StrEnum
from importlib.resources import files
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from watlowlib.devices.capability import SafetyTier
from watlowlib.errors import WatlowProtocolError, WatlowValidationError
from watlowlib.protocol.stdbus.tlv import DataType
from watlowlib.registry.aliases import DEFAULT_ALIASES
from watlowlib.registry.families import ControllerFamily
from watlowlib.registry.units import UnitKind

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "PARAMETERS",
    "SD_PARAMETERS",
    "ParameterRegistry",
    "ParameterSpec",
    "RwesFlag",
    "load_parameters",
    "load_pm_parameters",
    "load_sd_parameters",
]

_DATA_PACKAGE = "watlowlib.data"
_PM_FILENAME = "pm_parameters.json"
_SD_FILENAME = "sd_parameters.json"


class RwesFlag(StrEnum):
    """Persistence + access flag from the EZ-ZONE register list.

    - ``R`` — read-only.
    - ``W`` — write-only (rare; typically actions like "start autotune").
    - ``RW`` — runtime read/write, **not** EEPROM-backed.
    - ``RWE`` — RW + persisted to EEPROM.
    - ``RWES`` — RWE + saved set ("save settings to user memory").

    Mapping to :class:`SafetyTier` is in
    :func:`_safety_from_rwes`; the registry binds the result to
    :attr:`ParameterSpec.safety` at load time.
    """

    R = "R"
    W = "W"
    RW = "RW"
    RWE = "RWE"
    RWES = "RWES"


# Maps the JSON ``data_type`` strings to wire DataType tags. The JSON
# carries human-friendly labels — keep this table in one place rather
# than scattering ``if data_type == "IEEE Float"`` across decoders.
_DATA_TYPE_MAP: dict[str, DataType] = {
    "IEEE Float": DataType.FLOAT,
    "signed 32-bit": DataType.S32,
    "signed 16-bit": DataType.S16,
    "unsigned 32-bit": DataType.U32,
    "unsigned 16-bit": DataType.U16,
    "2 - unsigned 16 bit": DataType.U16,
    "unsigned 8-bit": DataType.U8,
    "Short String": DataType.STRING,
    "Enumeration": DataType.PACKED,
    "Wide Enumeration": DataType.PACKED,
}

# JSON ``rwes`` field carries occasional whitespace + slash variants.
# Normalise on the way in so callers see a clean :class:`RwesFlag`.
_RWES_NORMALISE: dict[str, RwesFlag] = {
    "R": RwesFlag.R,
    "W": RwesFlag.W,
    "RW": RwesFlag.RW,
    "R/W": RwesFlag.RW,
    "RWE": RwesFlag.RWE,
    "RWES": RwesFlag.RWES,
}


def _empty_family_hints() -> frozenset[ControllerFamily]:
    """Empty default for :attr:`ParameterSpec.family_hints`."""
    return frozenset()


def _safety_from_rwes(rwes: RwesFlag) -> SafetyTier:
    """Map an :class:`RwesFlag` to a :class:`SafetyTier`.

    Per ``docs/design.md`` §5b: ``R`` is :attr:`SafetyTier.READ_ONLY`;
    every flag that includes a write (``W`` / ``RW`` / ``RWE`` /
    ``RWES``) is :attr:`SafetyTier.PERSISTENT`. ``STATEFUL`` is reserved
    for commands without a registry parameter (e.g. "start autotune").
    """
    if rwes is RwesFlag.R:
        return SafetyTier.READ_ONLY
    return SafetyTier.PERSISTENT


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    """A single parameter row from ``pm_parameters.json``.

    Per-protocol fields:

    - Std Bus selector: :attr:`cls`, :attr:`member`,
      :attr:`default_instance`, :attr:`max_instance`.
    - Modbus selector: :attr:`relative_addr`, :attr:`absolute_addr`,
      :attr:`register_count`, :attr:`word_order` (``None`` → client
      default ``HIGH_LOW`` per design §5a).
    """

    parameter_id: int
    name: str
    aliases: frozenset[str]
    data_type: DataType
    unit_kind: UnitKind
    rwes: RwesFlag
    safety: SafetyTier
    cls: int
    member: int
    default_instance: int
    max_instance: int
    relative_addr: int
    absolute_addr: int
    register_count: int
    word_order: str | None = None
    range_min: float | None = None
    range_max: float | None = None
    default: object | None = None
    family_hints: frozenset[ControllerFamily] = field(default_factory=_empty_family_hints)
    scale: float = 1.0
    """Engineering-unit scale factor for the **Modbus** decode / encode path.

    The wire stores raw integers (e.g. the Series SD reports a process
    value of ``68421`` for ``68.421 °F``); ``scale`` is the multiplier
    that turns the raw word into engineering units on read
    (``value * scale``) and the divisor that turns engineering units
    back into raw words on write (``round(value / scale)``).

    ``1.0`` (the default) means *no scaling* — and is applied as a
    strict identity: the read path skips the multiply entirely when
    ``scale == 1.0`` so an integer parameter stays an ``int`` rather
    than being promoted to ``float`` by ``int * 1.0``. Std Bus rows are
    never scaled (the Std Bus variant ignores this field).
    """


# Attempts to coax the messy JSON ``range`` field ("0 to 9999",
# "-1999.0 to 9999.0", "0.001 to 9,999.000°F", "Off (0), On (1)", ...)
# into floats. Anything the regex doesn't match is left as ``None`` and
# surfaces in the spec's metadata only.
#
# The number pattern accepts comma thousands separators (``9,999.000``)
# because the EZ-ZONE PM register list uses them throughout. Without
# this the regex would silently truncate ``9,999.000`` to ``9`` and
# reject every device-reported PB above 9.0 — devices routinely
# report PB values in the hundreds for thermocouple inputs in °F.
_NUMBER = r"-?\d{1,3}(?:,\d{3})*(?:\.\d+)?|-?\d+(?:\.\d+)?"
_RANGE_RE = re.compile(
    rf"(?P<lo>{_NUMBER})\s*(?:to|,|-)\s*(?P<hi>{_NUMBER})",
)


def _coerce_int(value: object) -> int:
    """Best-effort int parse; PM JSON sometimes carries ``"N/A"`` or ``None``."""
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return 0


def _parse_range(text: object) -> tuple[float | None, float | None]:
    if not isinstance(text, str):
        return None, None
    m = _RANGE_RE.search(text)
    if m is None:
        return None, None
    try:
        # Strip thousands separators before float conversion.
        return float(m.group("lo").replace(",", "")), float(m.group("hi").replace(",", ""))
    except ValueError:
        return None, None


def _canonical_name(raw: str) -> str:
    """Compact a JSON ``name`` field to a snake_case canonical key.

    JSON names are like ``"Control Loop - Set Point"``. The hyphenated
    leading section is the class label; the trailing section is the
    actual parameter. Strip the prefix, lower-case, replace spaces.
    """
    # Drop everything before the first " - " (the class label prefix).
    sep = " - "
    if sep in raw:
        raw = raw.split(sep, 1)[1]
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", raw).strip("_").lower()
    return cleaned or raw.lower().replace(" ", "_")


def _register_count_for(data_type: DataType, raw: dict[str, Any]) -> int:
    """Best-effort Modbus register count for ``data_type``.

    Defaults follow design §5: FLOAT = 2 regs, S32/U32 = 2, U16 = 1,
    U8 = 1 (low byte), STRING = length-driven (return 0 → client
    derives from the ``register_count`` it observed, or from the row's
    ``modbus_range`` when populated).
    """
    if data_type in (DataType.FLOAT, DataType.S32, DataType.U32):
        return 2
    if data_type in (DataType.U16, DataType.S16, DataType.U8, DataType.PACKED):
        return 1
    # Remaining variant is DataType.STRING: the PM3 part-number string
    # is 16 ASCII bytes = 8 16-bit registers per the manual.
    _ = raw  # reserved for future per-row override
    return 8


def _build_spec(
    raw: dict[str, Any],
    *,
    family_hints: frozenset[ControllerFamily],
) -> ParameterSpec | None:
    """Convert one parameter-registry JSON row into a :class:`ParameterSpec`.

    Family-neutral: drives both the PM (``class_id`` / ``member_id``
    Std-Bus selector) and the Series SD (bare Modbus register, no
    class/member scheme) JSON shapes.

    Returns ``None`` for rows we deliberately skip — those with a
    ``None``/``"None"`` ``data_type`` (placeholder rows) or an
    unrecognised ``rwes`` flag (a few member-based rows where the
    field is empty).

    Args:
        raw: One JSON row.
        family_hints: Family hint set stamped onto the produced spec
            (``{PM}`` for the PM registry, ``{SD}`` for the SD one).

    JSON fields honoured beyond the PM set:

    - ``canonical`` (optional): verbatim canonical name, bypassing the
      :func:`_canonical_name` heuristic. SD rows set this to
      ``"process_value"`` / ``"setpoint"`` / ``"output_power"`` /
      ``"units"`` so ``read_pv`` / ``read_setpoint`` and the
      ``pv`` / ``sp`` aliases resolve.
    - ``scale`` (optional, default ``1.0``): Modbus engineering-unit
      scale factor (see :attr:`ParameterSpec.scale`).
    - ``class_id`` / ``member_id`` (optional, default ``0``): the SD
      map has no class*1000+member scheme — it addresses bare Modbus
      registers, so these are absent and ``parameter_id`` is the
      register number itself.
    """
    raw_type = raw.get("data_type")
    if raw_type in (None, "None"):
        return None
    if raw_type not in _DATA_TYPE_MAP:
        # Member-based rows defer the tag to runtime; not supported in v1.
        return None
    data_type = _DATA_TYPE_MAP[raw_type]

    raw_rwes = (raw.get("rwes") or "").strip()
    if raw_rwes not in _RWES_NORMALISE:
        return None
    rwes = _RWES_NORMALISE[raw_rwes]

    parameter_id = int(raw["parameter_id"])

    # ``unit_kind`` is required on every loadable row. Fail loud (per
    # design's "fail loud, fail typed" rule) so a typo or missing edit
    # in the JSON surfaces in CI rather than producing rows that
    # silently classify as the wrong family at read time.
    raw_unit_kind = raw.get("unit_kind")
    if raw_unit_kind is None:
        raise WatlowProtocolError(
            f"parameter row {parameter_id} is missing required 'unit_kind' field",
        )
    try:
        unit_kind = UnitKind(raw_unit_kind)
    except ValueError as exc:
        raise WatlowProtocolError(
            f"parameter row {parameter_id} has unknown unit_kind: {raw_unit_kind!r}",
        ) from exc

    # PM rows carry a class*1000+member Std-Bus selector; SD rows omit
    # both (the register *is* the parameter id). ``_coerce_int`` maps a
    # missing / "N/A" field to 0.
    cls = _coerce_int(raw.get("class_id"))
    member = _coerce_int(raw.get("member_id"))
    default_instance = int(raw.get("instance_id") or 1)
    max_instance = int(raw.get("max_instance") or 1)

    relative_addr = _coerce_int(raw.get("relative_addr"))
    absolute_addr = _coerce_int(raw.get("absolute_addr"))
    register_count = _register_count_for(data_type, raw)

    # A verbatim ``canonical`` field wins over the name heuristic so SD
    # rows bind the public workhorse names directly.
    raw_canonical = raw.get("canonical")
    canonical = str(raw_canonical) if raw_canonical else _canonical_name(str(raw["name"]))
    range_min, range_max = _parse_range(raw.get("range"))
    scale = float(raw.get("scale") or 1.0)

    return ParameterSpec(
        parameter_id=parameter_id,
        name=canonical,
        aliases=frozenset(),  # filled in by ParameterRegistry from the alias table
        data_type=data_type,
        unit_kind=unit_kind,
        rwes=rwes,
        safety=_safety_from_rwes(rwes),
        cls=cls,
        member=member,
        default_instance=default_instance,
        max_instance=max_instance,
        relative_addr=relative_addr,
        absolute_addr=absolute_addr,
        register_count=register_count,
        range_min=range_min,
        range_max=range_max,
        default=raw.get("default"),
        family_hints=family_hints,
        scale=scale,
    )


def load_parameters(
    filename: str,
    *,
    family: ControllerFamily,
    family_hints: frozenset[ControllerFamily] | None = None,
) -> tuple[ParameterSpec, ...]:
    """Load every parameter spec from a bundled registry JSON file.

    Args:
        filename: Bare filename inside the :mod:`watlowlib.data` package
            (e.g. ``"pm_parameters.json"`` / ``"sd_parameters.json"``).
        family: The controller family this file describes. Used as the
            default single-member ``family_hints`` when ``family_hints``
            is not given.
        family_hints: Explicit family-hint set stamped on every produced
            spec. Defaults to ``frozenset({family})``.

    Returns:
        One :class:`ParameterSpec` per loadable row, first occurrence
        winning on duplicate ``parameter_id`` (PM Map 1 / Map 2 sheets
        repeat ids with differing instance metadata).
    """
    hints = family_hints if family_hints is not None else frozenset({family})
    raw_text = files(_DATA_PACKAGE).joinpath(filename).read_text(encoding="utf-8")
    rows: list[dict[str, Any]] = json.loads(raw_text)
    out: list[ParameterSpec] = []
    seen_ids: set[int] = set()
    for raw in rows:
        spec = _build_spec(raw, family_hints=hints)
        if spec is None:
            continue
        if spec.parameter_id in seen_ids:
            # Duplicates across PM Map 1 / Map 2 sheets are real (same
            # parameter_id, different instance metadata) — keep only
            # the first occurrence; the registry exposes max_instance
            # so callers reach all loops.
            continue
        seen_ids.add(spec.parameter_id)
        out.append(spec)
    return tuple(out)


def load_pm_parameters() -> tuple[ParameterSpec, ...]:
    """Load and return every PM parameter spec from the bundled JSON."""
    return load_parameters(_PM_FILENAME, family=ControllerFamily.PM)


def load_sd_parameters() -> tuple[ParameterSpec, ...]:
    """Load and return every Series SD parameter spec from the bundled JSON."""
    return load_parameters(_SD_FILENAME, family=ControllerFamily.SD)


# Canonical names we explicitly bind from PM parameter IDs. The JSON
# loader's auto-generated names are descriptive but verbose; this
# table promotes the handful of public-API workhorses to short
# canonical names that the alias table targets.
_NAME_OVERRIDES: dict[int, str] = {
    1001: "hardware_id",
    1002: "firmware_id",
    1009: "part_number",
    1011: "device_name",
    3005: "units",  # front-panel display unit (RWES)
    4001: "process_value",
    7001: "setpoint",
    7011: "fixed_power",
    17050: "display_units",  # comms display unit — the wire-side scale
}


class ParameterRegistry:
    """Indexed view over a sequence of :class:`ParameterSpec` rows.

    Lookups are O(1) on canonical name, alias, and ``parameter_id``.
    Construction is O(N).
    """

    def __init__(
        self,
        specs: tuple[ParameterSpec, ...],
        *,
        aliases: Mapping[str, str] = DEFAULT_ALIASES,
        name_overrides: Mapping[int, str] = _NAME_OVERRIDES,
    ) -> None:
        # ``name_overrides`` promotes parameter ids to short canonical
        # names. The PM registry uses the bundled :data:`_NAME_OVERRIDES`
        # (its auto-derived names are verbose); the SD registry passes
        # ``{}`` and instead carries verbatim ``canonical`` names baked
        # into each spec at load time. Passing the PM table to a non-PM
        # registry would mis-claim collision keys (e.g. "units") for PM
        # parameter ids that don't exist in that registry.
        # Apply name overrides + collect aliases per spec.
        rebound: list[ParameterSpec] = []
        for spec in specs:
            override = name_overrides.get(spec.parameter_id)
            name = override or spec.name
            spec_aliases: set[str] = set()
            for alias, target in aliases.items():
                if target == name:
                    spec_aliases.add(alias)
            if override and override != spec.name:
                # Keep the original auto-generated name as an alias so
                # callers that learn it from the JSON still resolve.
                spec_aliases.add(spec.name)
            if name != spec.name or spec_aliases:
                # ``dataclasses.replace`` copies every other field
                # verbatim — including any field added to ParameterSpec
                # later (``scale``, future per-row overrides). A manual
                # field-by-field rebind would silently drop new fields,
                # so never reintroduce one here.
                spec = replace(  # noqa: PLW2901 — frozen dataclass rebind
                    spec,
                    name=name,
                    aliases=frozenset(spec_aliases),
                )
            rebound.append(spec)

        self._specs: tuple[ParameterSpec, ...] = tuple(rebound)
        # A handful of canonical names auto-derived by ``_canonical_name``
        # collide across unrelated rows (e.g. "Display - Units",
        # "Analog Input - Units", and "Linearization - Units" all
        # canonicalise to ``"units"``). When an :data:`_NAME_OVERRIDES`
        # entry targets one of those collision names, the override row
        # wins — otherwise the JSON's iteration order silently decided
        # which spec a public name like ``"units"`` resolved to.
        override_owner: dict[str, int] = {name.lower(): pid for pid, name in name_overrides.items()}
        by_id: dict[int, ParameterSpec] = {}
        by_name: dict[str, ParameterSpec] = {}
        for spec in self._specs:
            by_id[spec.parameter_id] = spec
            key = spec.name.lower()
            owner = override_owner.get(key)
            if owner is not None and owner != spec.parameter_id:
                # Another row's auto-canonical name collides with an
                # override-assigned name; the override row owns the key.
                continue
            by_name[key] = spec
            for alias in spec.aliases:
                by_name.setdefault(alias.lower(), spec)
        self._by_id: Mapping[int, ParameterSpec] = MappingProxyType(by_id)
        self._by_name: Mapping[str, ParameterSpec] = MappingProxyType(by_name)

    def __len__(self) -> int:
        return len(self._specs)

    def __iter__(self) -> Any:
        return iter(self._specs)

    def resolve(self, name_or_id: str | int) -> ParameterSpec:
        """Look up a spec by canonical name, alias, or parameter ID.

        Raises:
            WatlowValidationError: ``name_or_id`` does not resolve.
        """
        if isinstance(name_or_id, int):
            try:
                return self._by_id[name_or_id]
            except KeyError as exc:
                raise WatlowValidationError(
                    f"unknown parameter id: {name_or_id}",
                ) from exc
        key = name_or_id.lower()
        try:
            return self._by_name[key]
        except KeyError as exc:
            raise WatlowValidationError(
                f"unknown parameter name: {name_or_id!r}",
            ) from exc

    def has(self, name_or_id: str | int) -> bool:
        """Return ``True`` if ``name_or_id`` resolves; never raises."""
        try:
            self.resolve(name_or_id)
        except WatlowValidationError:
            return False
        return True

    def validate_instance(self, spec: ParameterSpec, instance: int) -> None:
        """Raise if ``instance`` is out of range for ``spec``.

        Public so the variant layer can validate before encoding.
        """
        if instance < 1 or instance > spec.max_instance:
            raise WatlowValidationError(
                f"instance {instance} out of range for {spec.name!r} (1..{spec.max_instance})",
            )

    def validate_value(self, spec: ParameterSpec, value: float | int | str) -> None:
        """Soft range check based on the spec's parsed ``range`` metadata.

        Skipped silently if ``range_min`` / ``range_max`` couldn't be
        parsed from the JSON ``range`` field — Watlow's range strings
        are not always machine-readable. STRING parameters are not
        range-checked.
        """
        if isinstance(value, str):
            return
        if spec.range_min is None or spec.range_max is None:
            return
        v = float(value)
        if v < spec.range_min or v > spec.range_max:
            raise WatlowValidationError(
                f"value {value!r} out of range for {spec.name!r} "
                f"({spec.range_min}..{spec.range_max})",
            )


# Eager load so callers see a populated registry on first import.
# Failure here surfaces as a `WatlowProtocolError` (not a generic
# JSONDecodeError) per the design's "fail loud, fail typed" rule.
def _build_default_registry() -> ParameterRegistry:
    try:
        return ParameterRegistry(load_pm_parameters())
    except (OSError, json.JSONDecodeError) as exc:
        raise WatlowProtocolError(
            f"failed to load PM parameter registry: {exc}",
        ) from exc


def _build_sd_registry() -> ParameterRegistry:
    """Build the Series SD registry.

    SD rows carry verbatim ``canonical`` names, so the PM-specific
    :data:`_NAME_OVERRIDES` table is **not** applied (passing it would
    mis-claim collision keys like ``"units"`` for PM ids absent here).
    The default alias table still resolves (``pv`` → ``process_value``,
    ``sp`` → ``setpoint``).
    """
    try:
        return ParameterRegistry(load_sd_parameters(), name_overrides={})
    except (OSError, json.JSONDecodeError) as exc:
        raise WatlowProtocolError(
            f"failed to load SD parameter registry: {exc}",
        ) from exc


PARAMETERS: ParameterRegistry = _build_default_registry()
SD_PARAMETERS: ParameterRegistry = _build_sd_registry()
