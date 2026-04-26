"""Loop-level facade helpers — PID and output reads.

Loop operations compose registry-driven reads/writes from
:mod:`watlowlib.commands.parameters` rather than introducing new
wire-level commands. Each underlying call already has variants on
both Std Bus and Modbus, so PID / output operations work uniformly
across protocols without protocol-specific code in this module
(cross-cutting invariant 2: variants own the wire; this module owns
aggregation).

Persistent writes (``write_pid``) propagate ``confirm=True`` into
each underlying parameter write — the session enforces the
:class:`SafetyTier.PERSISTENT` gate per write, so a missing confirm
fails pre-I/O on the first gain rather than half-applying the gain
set.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from watlowlib.commands.parameters import (
    READ_PARAMETER,
    WRITE_PARAMETER,
    ReadParameterRequest,
    WriteParameterRequest,
)
from watlowlib.devices.capability import Capability
from watlowlib.devices.models import Reading
from watlowlib.errors import (
    ErrorContext,
    WatlowConfigurationError,
    WatlowProtocolError,
    WatlowProtocolUnsupportedError,
    WatlowTransportError,
)

if TYPE_CHECKING:
    from watlowlib.devices.session import Session

__all__ = [
    "PidGains",
    "read_output",
    "read_pid",
    "write_pid",
]


@dataclass(frozen=True, slots=True)
class PidGains:
    """Decoded PID gain set for one loop.

    Fields default to ``None`` when the controller doesn't expose the
    parameter (e.g. cooling fields on a heat-only PM SKU). Callers
    that need a strict-shape view can check ``not_none()``.
    """

    heat_proportional_band: float | None = None
    cool_proportional_band: float | None = None
    time_integral: float | None = None
    time_derivative: float | None = None
    dead_band: float | None = None

    def not_none(self) -> bool:
        """``True`` iff every gain was successfully read."""
        return all(
            v is not None
            for v in (
                self.heat_proportional_band,
                self.cool_proportional_band,
                self.time_integral,
                self.time_derivative,
                self.dead_band,
            )
        )


# Canonical parameter names (resolved through the registry; aliases
# also accepted at call time). Centralising them here keeps the
# field/parameter mapping in one place — both ``read_pid`` and
# ``write_pid`` consult the same table. The third element flags
# cool-side parameters: those are gated when the controller's
# capabilities don't include :attr:`Capability.HAS_COOLING`.
_PID_FIELDS: tuple[tuple[str, str, bool], ...] = (
    ("heat_proportional_band", "heat_proportional_band", False),
    ("cool_proportional_band", "cool_proportional_band", True),
    ("time_integral", "time_integral", False),
    ("time_derivative", "time_derivative", False),
    ("dead_band", "dead_band", True),  # dead band is meaningful only with cooling
)

_OUTPUT_PARAMETER = "output_power"


async def read_pid(
    session: Session,
    *,
    instance: int = 1,
    capabilities: Capability | None = None,
) -> PidGains:
    """Read every PID gain for ``instance`` (loop number, 1-indexed).

    Issues one parameter read per gain through the session — the
    ``output_power`` and PID parameters live in different rows of
    the EZ-ZONE registry so a single contiguous Modbus read isn't an
    option here. Reads run sequentially under the session lock so the
    snapshot is consistent against concurrent writers on the same
    port.

    Cool-side parameters (``cool_proportional_band``, ``dead_band``)
    are gated on :attr:`Capability.HAS_COOLING` when ``capabilities``
    is supplied: SKUs with no second control output (PM ``output_2 ==
    'A'``) expose the cool registers but they hold uninitialised bits
    that decode as garbage floats (e.g. ``0xCDCDCDCD ≈ 3.4e12``).
    Skip the read entirely on those devices and report ``None``.

    When ``capabilities`` is ``None`` the gate is permissive — every
    field is read, matching the pre-capability behaviour. Per design
    §5b, parameters the controller rejects with a typed unsupported
    error still surface as ``None`` rather than raising.
    """
    values: dict[str, float | None] = {}
    has_cooling = capabilities is None or bool(capabilities & Capability.HAS_COOLING)
    for field_name, parameter_name, cool_only in _PID_FIELDS:
        if cool_only and not has_cooling:
            values[field_name] = None
            continue
        value = await _safe_read_float(session, parameter_name, instance=instance)
        values[field_name] = value
    return PidGains(**values)


async def write_pid(
    session: Session,
    gains: PidGains,
    *,
    instance: int = 1,
    confirm: bool = False,
    capabilities: Capability | None = None,
) -> PidGains:
    """Write the supplied gains for ``instance`` and return what was applied.

    Only fields with a non-``None`` value are written; fields left
    ``None`` skip the wire entirely (callers can read-modify-write
    just one gain without disturbing the rest). Persistent writes
    require ``confirm=True`` — the session raises
    :class:`watlowlib.errors.WatlowConfirmationRequiredError`
    pre-I/O on the first underlying call if the gate is missing.

    Cool-side fields (``cool_proportional_band``, ``dead_band``) are
    refused with :class:`WatlowConfigurationError` when
    ``capabilities`` is supplied without
    :attr:`Capability.HAS_COOLING` — writing to the cool registers on
    a single-output PM is at best a no-op and at worst silently
    corrupts adjacent cool-side state.
    """
    has_cooling = capabilities is None or bool(capabilities & Capability.HAS_COOLING)
    applied: dict[str, float | None] = {}
    for field_name, parameter_name, cool_only in _PID_FIELDS:
        value = getattr(gains, field_name)
        if value is None:
            applied[field_name] = None
            continue
        if cool_only and not has_cooling:
            raise WatlowConfigurationError(
                f"PID field {field_name!r} requires a cooling output, but the "
                "controller's capabilities do not include Capability.HAS_COOLING "
                "(PM output_2 == 'A' or equivalent). Pass a PidGains with the "
                "cool-side fields left as None.",
                context=ErrorContext(
                    command_name="write_pid",
                    instance=instance,
                ),
            )
        entry = await session.execute(
            WRITE_PARAMETER,
            WriteParameterRequest(parameter_name, value, instance=instance),
            confirm=confirm,
        )
        # Echo the device-reported value (Modbus echoes the request,
        # Std Bus echoes the parsed write response).
        if isinstance(entry.value, int | float):
            applied[field_name] = float(entry.value)
        else:
            applied[field_name] = value
    return PidGains(**applied)


async def read_output(session: Session, *, instance: int = 1) -> Reading:
    """Read the loop's working output (``output_power``).

    Returns a :class:`Reading` matching the rest of the facade so
    callers don't have to remember a different return shape per
    operation. Unsupported on devices without the
    ``output_power`` parameter — surfaces as
    :class:`watlowlib.errors.WatlowProtocolUnsupportedError`.
    """
    entry = await session.execute(
        READ_PARAMETER,
        ReadParameterRequest(_OUTPUT_PARAMETER, instance=instance),
    )
    value = float(entry.value) if isinstance(entry.value, int | float) else None
    return Reading(
        value=value,
        unit=None,
        received_at=datetime.now(UTC),
        monotonic_ns=time.monotonic_ns(),
        raw=entry.raw,
        protocol=session.protocol_kind,
    )


# --- Internals ------------------------------------------------------


async def _safe_read_float(
    session: Session,
    parameter_name: str,
    *,
    instance: int,
) -> float | None:
    """Read a numeric parameter, returning ``None`` on absence.

    Same swallow policy as :meth:`Controller._safe_read_int` — a
    parameter that the controller doesn't expose lands as ``None``
    so the snapshot still includes whatever it does support. Only
    catches errors that genuinely indicate "this parameter isn't
    here" (typed unsupported / no-such errors) and transport
    timeouts (devices vary in how they handle a parameter they
    don't implement). Real connection / configuration errors
    propagate.
    """
    try:
        entry = await session.execute(
            READ_PARAMETER,
            ReadParameterRequest(parameter_name, instance=instance),
        )
    except (WatlowProtocolError, WatlowProtocolUnsupportedError, WatlowTransportError):
        return None
    if isinstance(entry.value, int | float):
        return float(entry.value)
    return None
