"""Library defaults read once at import time.

Timeouts and retry counts are kept here so callers can adjust them
without monkey-patching deeper modules. Values match the captures used
to build ``docs/protocol-stdbus-findings.md`` against an EZ-ZONE PM3.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Defaults:
    """Library-wide default values."""

    #: Per-call I/O timeout in seconds. PM3 round-trip @ 38400 8N1 is
    #: well under 50 ms; 1.0 s leaves headroom for slow USB-RS485
    #: bridges and is the same default sartoriuslib uses.
    io_timeout_s: float = 1.0

    #: Idle window used by ``Transport.read_available`` between
    #: protocol probes during ``ProtocolKind.AUTO`` detection.
    drain_idle_s: float = 0.05

    #: Std Bus default baud rate from the PM manuals.
    stdbus_baud: int = 38400


DEFAULTS = Defaults()


__all__ = ["DEFAULTS", "Defaults"]
