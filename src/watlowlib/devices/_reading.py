"""Shared :class:`Reading` construction from a :class:`ParameterEntry`.

Both :class:`Controller` and the loop-level commands need to wrap a
generic :class:`ParameterEntry` into a typed :class:`Reading` with the
display unit attached. Centralising the helper keeps the temperature
display-unit fetch (parameter 17050, session-cached) in exactly one
place instead of duplicated across call sites.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from watlowlib.devices.models import Reading
from watlowlib.registry.units import UnitKind, resolve_unit

if TYPE_CHECKING:
    from watlowlib.devices.models import ParameterEntry
    from watlowlib.devices.session import Session

__all__ = ["reading_from_entry"]


async def reading_from_entry(session: Session, entry: ParameterEntry) -> Reading:
    """Build a :class:`Reading` from a generic :class:`ParameterEntry`.

    The unit attached to the reading is resolved from the parameter's
    :attr:`ParameterSpec.unit_kind`:

    - ``TEMPERATURE`` → the session's cached display unit (one lazy
      read of parameter 17050 on first call, cached for the session).
    - ``PERCENT`` → :attr:`Unit.PERCENT`.
    - Everything else → ``None``.

    Non-numeric entry values (strings, ``None``) produce
    ``Reading.value = None``.
    """
    display = await session.display_unit() if entry.spec.unit_kind is UnitKind.TEMPERATURE else None
    value = float(entry.value) if isinstance(entry.value, int | float) else None
    return Reading(
        value=value,
        unit=resolve_unit(entry.spec.unit_kind, display),
        received_at=datetime.now(UTC),
        monotonic_ns=time.monotonic_ns(),
        raw=entry.raw,
        protocol=session.protocol_kind,
    )
