r"""Internal polling helper — turns ``read_parameter`` calls into :class:`Sample`\ s.

Shared between :meth:`Controller.poll_many` and
:meth:`WatlowManager.poll_many` so the timing / sample-construction
logic lives in exactly one place. Failures are caught and logged,
never raised — the caller (:func:`record`) treats absence as "drop
this row from the batch".

Atomicity: the per-port lock is acquired **once** for the whole batch
via :func:`watlowlib._lock.maybe_acquire`. Inner
:meth:`Controller.read_parameter` → :meth:`Session.execute` calls see
the lock owned by the current task and reuse the acquisition. This
keeps unrelated writers from interleaving between the batch's reads
and bounds tick latency to one queue traversal instead of N.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from watlowlib._lock import maybe_acquire
from watlowlib._logging import get_logger
from watlowlib.errors import WatlowError
from watlowlib.registry.units import UnitKind, resolve_unit
from watlowlib.streaming.sample import Sample

if TYPE_CHECKING:
    from collections.abc import Sequence

    from watlowlib.devices.controller import Controller

__all__ = ["poll_controller"]


_logger = get_logger("streaming.poll")


async def poll_controller(
    controller: Controller,
    *,
    name: str,
    parameters: Sequence[str | int],
    instances: Sequence[int] = (1,),
) -> list[Sample]:
    """Read every (parameter, instance) on ``controller`` and return the samples.

    One :class:`Sample` per successful (parameter, instance) read.
    Failed reads are dropped from the batch and logged at WARN.
    Sequential because every poll on one controller serialises through
    the per-port lock anyway — running them in a task group would just
    queue them up at the lock. The lock is held for the whole batch
    so a queued writer cannot land between read N and read N+1.
    """
    samples: list[Sample] = []
    session = controller.session
    registry = session.registry
    address = session.address
    protocol = session.protocol_kind

    async with maybe_acquire(session.client.lock):
        for ident in parameters:
            try:
                spec = registry.resolve(ident)
            except WatlowError as err:
                _logger.warning(
                    "poll.unknown_parameter device=%s parameter=%r err=%s",
                    name,
                    ident,
                    err,
                )
                continue
            # Temperature parameters get the user-asserted wire scale
            # (set via ``open_device(assert_wire_temperature_unit=...)``)
            # or ``None`` when no assertion was made. Pure accessor; no
            # I/O. The library does not consult parameter 17050 — on at
            # least one PM3 firmware it is label-only and would silently
            # mis-tag values. See ``docs/devices.md`` §Units.
            if spec.unit_kind is UnitKind.TEMPERATURE:
                temperature_unit = session.wire_temperature_unit()
            else:
                temperature_unit = None
            unit = resolve_unit(spec.unit_kind, temperature_unit)
            for instance in instances:
                requested_at = datetime.now(UTC)
                sent_ns = time.monotonic_ns()
                try:
                    entry = await controller.read_parameter(spec.name, instance=instance)
                except WatlowError as err:
                    _logger.warning(
                        "poll.read_failed device=%s parameter=%s instance=%s err=%s",
                        name,
                        spec.name,
                        instance,
                        err,
                    )
                    continue
                received_at = datetime.now(UTC)
                recv_ns = time.monotonic_ns()
                t_mono_ns = (sent_ns + recv_ns) // 2
                latency_s = (received_at - requested_at).total_seconds()
                t_utc = requested_at + (received_at - requested_at) / 2
                samples.append(
                    Sample(
                        device=name,
                        address=address,
                        protocol=protocol,
                        parameter=spec.name,
                        parameter_id=spec.parameter_id,
                        instance=instance,
                        value=entry.value,
                        unit=unit,
                        t_mono_ns=t_mono_ns,
                        t_utc=t_utc,
                        t_midpoint_mono_ns=None,
                        requested_at=requested_at,
                        received_at=received_at,
                        latency_s=latency_s,
                        raw=entry.raw,
                    ),
                )
    return samples
