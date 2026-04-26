"""Alarm-state decoder placeholder.

EZ-ZONE PM exposes per-alarm-instance status as a PACKED 16-bit word
at parameter ``10005`` (class 10 / alarms, member 5,
``max_instance=8``). Watlow does not publish a stable public bit
map for that PACKED word — different firmware revisions and SKU
configurations move bits around — so a decoder would either need RE
provenance we don't have or speculative bit guesses that fail silently
on real hardware.

Until that bit map is captured, :func:`read_alarms` raises
:class:`WatlowProtocolUnsupportedError`. The public signature returning
:class:`AlarmState` is stable, so swapping in a real decoder later is
non-breaking.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from watlowlib.errors import ErrorContext, WatlowProtocolUnsupportedError

if TYPE_CHECKING:
    from watlowlib.devices.models import AlarmState
    from watlowlib.devices.session import Session

__all__ = ["read_alarms"]


async def read_alarms(session: Session, *, instance: int = 1) -> AlarmState:
    """Raise :class:`WatlowProtocolUnsupportedError`.

    Args:
        session: The session whose facade triggered the call.
        instance: 1-indexed alarm instance, threaded into the error
            context so callers can see which loop/alarm was asked
            for.

    Raises:
        WatlowProtocolUnsupportedError: Always — see module docstring
            for why the decoder is intentionally absent.
    """
    raise WatlowProtocolUnsupportedError(
        "alarm-state decoding is not implemented: PM exposes alarm status as a "
        "PACKED 16-bit word at parameter 10005 but the bit layout has no public, "
        "firmware-stable spec yet.",
        context=ErrorContext(
            command_name="read_alarms",
            protocol=session.protocol_kind,
            port=session.port or None,
            address=session.address or None,
            parameter_id=10005,
            instance=instance,
        ),
    )
