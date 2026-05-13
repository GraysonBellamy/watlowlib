"""Shared :class:`Reading` construction from a :class:`ParameterEntry`.

Both :class:`Controller` and the loop-level commands need to wrap a
generic :class:`ParameterEntry` into a typed :class:`Reading` with the
unit attached. Centralising the helper keeps unit-resolution in
exactly one place instead of duplicated across call sites.

For temperature parameters the unit is sourced from the session's
user-asserted wire temperature unit (:meth:`Session.wire_temperature_unit`),
which is ``None`` unless the caller passed
``assert_wire_temperature_unit=`` to :func:`watlowlib.open_device`.
Parameter 17050 ("Communications - Display Units") is **not**
consulted — on at least one PM3 firmware revision it is a label-only
register and would silently mis-tag values. See ``docs/devices.md``
§Units.
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

    - ``TEMPERATURE`` → the session's user-asserted wire temperature
      unit, or ``None`` when the user did not pass
      ``assert_wire_temperature_unit=`` to :func:`open_device`.
    - ``PERCENT`` → :attr:`Unit.PERCENT`.
    - Everything else → ``None``.

    Non-numeric entry values (strings, ``None``) produce
    ``Reading.value = None``.
    """
    temperature_unit = (
        session.wire_temperature_unit() if entry.spec.unit_kind is UnitKind.TEMPERATURE else None
    )
    value = float(entry.value) if isinstance(entry.value, int | float) else None
    return Reading(
        value=value,
        unit=resolve_unit(entry.spec.unit_kind, temperature_unit),
        received_at=datetime.now(UTC),
        monotonic_ns=time.monotonic_ns(),
        raw=entry.raw,
        protocol=session.protocol_kind,
    )
