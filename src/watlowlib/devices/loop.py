"""Per-loop sub-facade returned by :meth:`Controller.loop`.

A :class:`ControllerLoop` is a thin view over a :class:`Controller`
that pre-binds an ``instance`` argument. It validates the loop number
once at construction (cross-cutting invariant 6: 1-indexed everywhere)
and forwards every operation to the parent controller's session,
threading the loop index as the registry instance.

The sub-facade is **stateless** beyond the loop number — it does not
duplicate the controller's transport, lock, or availability cache.
Multiple :class:`ControllerLoop` instances over the same controller
share the underlying session safely; concurrent calls serialize on
the protocol client's lock.

This module intentionally has no protocol-specific code. PID,
output, and alarm helpers live in :mod:`watlowlib.commands.loop` and
:mod:`watlowlib.commands.alarms` so the facade-only logic and the
parameter aggregation logic stay separate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from watlowlib.commands.alarms import read_alarms as _read_alarms
from watlowlib.commands.loop import (
    PidGains,
)
from watlowlib.commands.loop import (
    read_output as _read_output,
)
from watlowlib.commands.loop import (
    read_pid as _read_pid,
)
from watlowlib.commands.loop import (
    write_pid as _write_pid,
)
from watlowlib.errors import WatlowValidationError

if TYPE_CHECKING:
    from watlowlib.devices.controller import Controller
    from watlowlib.devices.models import AlarmState, Reading

__all__ = ["ControllerLoop"]


class ControllerLoop:
    """A view over one control loop on a :class:`Controller`.

    Construct via :meth:`Controller.loop`; never instantiated
    directly by user code. The sub-facade lives only as long as the
    parent controller's session — closing the controller is the only
    cleanup needed.
    """

    __slots__ = ("_controller", "_loop")

    def __init__(self, controller: Controller, loop_number: int) -> None:
        if loop_number < 1:
            raise WatlowValidationError(
                f"loop number must be 1-indexed and >= 1; got {loop_number}",
            )
        # If the controller has identified the device, validate
        # eagerly. Otherwise defer to the registry's per-spec
        # ``validate_instance`` at first call: a registered parameter
        # with ``max_instance=1`` will raise a clear
        # ``WatlowValidationError`` when ``loop(2).read_pv()`` is
        # invoked. That keeps ``Controller.loop(2)`` cheap when called
        # before identify, but still fails before I/O.
        loops = controller.loops
        if loops is not None and loop_number > loops:
            raise WatlowValidationError(
                f"loop {loop_number} out of range for this device (1..{loops})",
            )
        self._controller = controller
        self._loop = loop_number

    @property
    def number(self) -> int:
        """The 1-indexed loop number this view binds."""
        return self._loop

    # --- Process ------------------------------------------------------

    async def read_pv(self, *, timeout: float | None = None) -> Reading:
        """Read this loop's process value."""
        return await self._controller.read_pv(instance=self._loop, timeout=timeout)

    async def read_setpoint(self, *, timeout: float | None = None) -> Reading:
        """Read this loop's active setpoint."""
        return await self._controller.read_setpoint(instance=self._loop, timeout=timeout)

    async def set_setpoint(
        self,
        value: float,
        *,
        confirm: bool = False,
        timeout: float | None = None,
    ) -> Reading:
        """Write this loop's setpoint (RWES → ``confirm=True`` required)."""
        return await self._controller.set_setpoint(
            value,
            instance=self._loop,
            confirm=confirm,
            timeout=timeout,
        )

    async def read_output(self) -> Reading:
        """Read this loop's working output (``output_power``)."""
        return await _read_output(self._controller.session, instance=self._loop)

    # --- PID ----------------------------------------------------------

    async def read_pid(self) -> PidGains:
        """Read every PID gain for this loop. Missing gains return ``None``.

        Cool-side gains (``cool_proportional_band``, ``dead_band``)
        are skipped when the controller's identified capabilities
        lack :attr:`Capability.HAS_COOLING` (e.g. PM ``output_2 ==
        'A'``). Pre-identify, the gate is permissive.
        """
        return await _read_pid(
            self._controller.session,
            instance=self._loop,
            capabilities=self._controller.capabilities,
        )

    async def write_pid(self, gains: PidGains, *, confirm: bool = False) -> PidGains:
        """Write the supplied gains for this loop.

        Persistent — passing ``confirm=True`` is required. Fields
        left ``None`` on ``gains`` skip the wire entirely. Setting a
        cool-side field on a controller without
        :attr:`Capability.HAS_COOLING` raises
        :class:`watlowlib.errors.WatlowConfigurationError`.
        """
        return await _write_pid(
            self._controller.session,
            gains,
            instance=self._loop,
            confirm=confirm,
            capabilities=self._controller.capabilities,
        )

    # --- Alarms -------------------------------------------------------

    async def read_alarms(self) -> AlarmState:
        """Read the alarm word for this loop.

        Currently raises :class:`watlowlib.errors.WatlowProtocolUnsupportedError` —
        see :func:`watlowlib.commands.alarms.read_alarms` for why the
        decoder is not yet wired up.
        """
        return await _read_alarms(self._controller.session, instance=self._loop)
