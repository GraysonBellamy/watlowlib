"""I/O-free identity snapshot — :class:`DeviceSnapshot` / :class:`WatlowDeviceSnapshot`.

Cross-library shape (mirrors :mod:`alicatlib`, :mod:`sartoriuslib`,
:mod:`nidaqlib`) so consumers can render a unified "device status"
view across vendors. Always built from cached state — no wire I/O.

Population path:

1. :func:`watlowlib.open_device` calls :meth:`Controller.identify` by
   default (opt out with ``identify=False``); the resulting
   :class:`DeviceInfo` is cached on the controller for snapshot use.
2. :meth:`Session.execute` records the last error context as it
   propagates failures, and the per-command :class:`Availability`
   cache tracks UNSUPPORTED commands. Both feed the snapshot.
3. :meth:`Controller.snapshot` reads the cached pieces and produces a
   :class:`WatlowDeviceSnapshot` without issuing any reads.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime

    from watlowlib.devices.capability import Availability, Capability
    from watlowlib.errors import ErrorContext
    from watlowlib.registry.families import ControllerFamily

__all__ = ["DeviceSnapshot", "WatlowDeviceSnapshot"]


@dataclass(frozen=True, slots=True)
class DeviceSnapshot:
    """Cross-library identity + connection summary (no I/O).

    Attributes:
        name: Caller-supplied device name (manager-assigned, or the
            transport label for a solo controller).
        model: Best-known model / part-number string, ``None`` until
            :meth:`Controller.identify` has run.
        firmware: Firmware id as a string, or ``None``.
        serial: Serial-number string, or ``None``.
        connected: ``True`` when the underlying transport is open.
        last_error: Most recent :class:`ErrorContext` recorded by the
            session, or ``None``.
        recoverable_error_count: Session counter for swallowed-and-
            retried transient errors. Watlow keeps this dormant until
            a transient transport class is introduced; the field
            stays at zero today.
        captured_at: Wall-clock at snapshot construction (tz-aware
            UTC).
    """

    name: str
    model: str | None
    firmware: str | None
    serial: str | None
    connected: bool
    last_error: ErrorContext | None
    recoverable_error_count: int
    captured_at: datetime


@dataclass(frozen=True, slots=True)
class WatlowDeviceSnapshot(DeviceSnapshot):
    """Watlow-specific extension of :class:`DeviceSnapshot`.

    Attributes:
        family: Decoded :class:`ControllerFamily`, or ``None`` before
            :meth:`identify` has run.
        capabilities: SKU-decoded :class:`Capability` flags.
        availability_summary: Frozen mapping of command names that
            the session has marked :attr:`Availability.UNSUPPORTED`.
            The mapping is bounded by the parameter registry size
            (typically small).
    """

    family: ControllerFamily | None
    capabilities: Capability
    availability_summary: Mapping[str, Availability] = field(
        default_factory=lambda: {},
    )
