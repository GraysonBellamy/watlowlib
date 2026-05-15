"""Timed sample — one parameter read with send/receive provenance.

A :class:`Sample` is the unit the recorder emits into its memory-object
stream. Watlow polls a *small group* of parameters per device per tick
(unlike Alicat, which returns one wide ``DataFrame`` per poll), so a
recorder tick produces N×M samples — one per (device, parameter) pair
that succeeded.

Timestamp contract (uniform across the sibling libraries):

- ``t_mono_ns`` — :func:`time.monotonic_ns` midpoint of the request/
  reply round-trip; canonical join key for cross-stream alignment
  (monotonic, never wall-clock).
- ``t_utc`` — wall-clock midpoint of the request/reply round-trip
  (tz-aware UTC). Used for human-readable sink timestamps.
- ``t_midpoint_mono_ns`` — optional integration-window midpoint in
  monotonic nanoseconds. For polled reads this is ``None``; sensors
  with integration windows (e.g. multi-sample averages) populate it.

I/O provenance stays alongside the canonical timestamps:
``requested_at`` / ``received_at`` / ``latency_s`` are the per-round-
trip wire boundaries, available for diagnostics but not the join key.

The shape is deliberately long-format (one row per parameter) so the
SQLite cross-vendor test can union Watlow rows with Alicat rows
under one schema.

Design reference: ``docs/design.md`` §6.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime

    from watlowlib.protocol.base import ProtocolKind
    from watlowlib.registry.units import Unit

__all__ = ["Sample"]


@dataclass(frozen=True, slots=True)
class Sample:
    """One parameter read with full timing provenance.

    Attributes:
        device: Manager-assigned name (or controller label for solo
            recordings). Stable downstream identifier that follows the
            value into sinks.
        address: Bus address of the polled device. Std Bus 1..16,
            Modbus RTU 1..247.
        protocol: Wire protocol that decoded this read. Set from the
            session's protocol kind, not the reading metadata, so a
            mixed-protocol recording records the source per row.
        parameter: Canonical parameter name (e.g. ``"process_value"``).
        parameter_id: Registry parameter id (e.g. ``4001``).
        instance: 1-indexed loop / channel selector used for the read.
        value: The decoded scalar. ``None`` when the device reported
            the value as unavailable (sensor-fail, overload, ...).
        unit: Concrete :class:`Unit` from the Watlow polling path, a
            free-form string for cross-vendor rows (Alicat's ``"psia"``,
            ``"sccm"`` etc. via
            ``examples/mixed_watlow_alicat_sqlite.py``), or ``None``
            when the parameter has no unit (counts, IDs, time
            constants). The Watlow recorder always populates a
            :class:`Unit`; the ``str`` branch only fires for hand-built
            cross-vendor samples.
        t_mono_ns: :func:`time.monotonic_ns` midpoint of the request/
            reply round-trip — canonical join key. Monotonic since OS
            boot; no calendar meaning.
        t_utc: Wall-clock midpoint of the request/reply round-trip
            (tz-aware UTC). Preferred point estimate when aligning
            Watlow samples against other sensor streams in human time.
        t_midpoint_mono_ns: Optional integration-window midpoint in
            monotonic nanoseconds. ``None`` for single polled reads;
            sensors that average over a window populate it.
        requested_at: Wall-clock ``datetime`` (UTC) captured just
            before the read leaves the host.
        received_at: Wall-clock ``datetime`` (UTC) captured just after
            the reply is decoded.
        latency_s: ``(received_at - requested_at).total_seconds()`` —
            precomputed for convenience.
        raw: The wire payload that produced the value. Available for
            diagnostics; tabular sinks drop it.
    """

    device: str
    address: int
    protocol: ProtocolKind
    parameter: str
    parameter_id: int
    instance: int
    value: float | int | str | bool | None
    unit: Unit | str | None
    t_mono_ns: int
    t_utc: datetime
    t_midpoint_mono_ns: int | None
    requested_at: datetime
    received_at: datetime
    latency_s: float
    raw: bytes
