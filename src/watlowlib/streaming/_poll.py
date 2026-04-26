r"""Internal polling helper — turns ``read_parameter`` calls into :class:`Sample`\ s.

Shared between :meth:`Controller.poll` and
:meth:`WatlowManager.poll` so the timing / sample-construction logic
lives in exactly one place. Failures are caught and logged, never
raised — the caller (:func:`record`) treats absence as "drop this row
from the batch".
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from watlowlib._logging import get_logger
from watlowlib.errors import WatlowError
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
    queue them up at the lock.
    """
    samples: list[Sample] = []
    session = controller.session
    registry = session.registry
    address = session.address
    protocol = session.protocol_kind

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
            mono = (sent_ns + recv_ns) // 2
            latency_s = (received_at - requested_at).total_seconds()
            midpoint = requested_at + (received_at - requested_at) / 2
            samples.append(
                Sample(
                    device=name,
                    address=address,
                    protocol=protocol,
                    parameter=spec.name,
                    parameter_id=spec.parameter_id,
                    instance=instance,
                    value=entry.value,
                    unit=None,
                    monotonic_ns=mono,
                    requested_at=requested_at,
                    received_at=received_at,
                    midpoint_at=midpoint,
                    latency_s=latency_s,
                    raw=entry.raw,
                ),
            )
    return samples
