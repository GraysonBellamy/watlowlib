"""Timed sample — one parameter read with send/receive provenance.

A :class:`Sample` is the unit the recorder emits into its memory-object
stream. Watlow polls a *small group* of parameters per device per tick
(unlike Alicat, which returns one wide ``DataFrame`` per poll), so a
recorder tick produces N×M samples — one per (device, parameter) pair
that succeeded — each one carrying:

- ``midpoint_at`` — best point-estimate of the on-device acquisition
  instant (halfway between request and reply). Use this for aligning
  Watlow values against other sensor streams.
- ``monotonic_ns`` — :func:`time.monotonic_ns` at the read boundary,
  for drift analysis only (no calendar meaning).
- ``raw`` — the wire payload that produced the value. Available for
  diagnostics; tabular sinks drop it.

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
        unit: Display string for the value's unit, or ``None`` if the
            registry doesn't carry per-parameter unit metadata. v1
            leaves this ``None`` for every PM parameter — the registry
            doesn't carry per-row units yet.
        monotonic_ns: :func:`time.monotonic_ns` at the read site,
            roughly the midpoint of send/receive. Used for scheduling
            / drift analysis only — never displayed.
        requested_at: Wall-clock ``datetime`` (UTC) captured just
            before the read leaves the host.
        received_at: Wall-clock ``datetime`` (UTC) captured just after
            the reply is decoded.
        midpoint_at: ``(requested_at + received_at) / 2`` — the
            preferred point estimate of the sample instant. Use this
            when aligning Watlow samples against other sensor streams.
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
    unit: str | None
    monotonic_ns: int
    requested_at: datetime
    received_at: datetime
    midpoint_at: datetime
    latency_s: float
    raw: bytes
