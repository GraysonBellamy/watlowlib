"""Command + variant primitives.

A :class:`Command` is a pure descriptor: it pairs a request type with
one variant per protocol. Variants are pure functions of
``(ctx, request)`` and never touch transport. The
:class:`watlowlib.devices.session.Session` owns dispatch (gates,
logging, availability) and is the *only* place that calls the variant.

Std Bus variants emit raw inner-payload bytes — watlowlib owns that
codec. Modbus variants emit a typed :class:`ModbusOp` instruction
because ``anymodbus`` already owns the wire codec; handing it bytes
would be a layer violation.

See ``docs/design.md`` §5.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from watlowlib.devices.capability import Capability, SafetyTier
from watlowlib.registry.families import ControllerFamily

if TYPE_CHECKING:
    from watlowlib.firmware import FirmwareVersion
    from watlowlib.protocol.modbus.ops import ModbusOp
    from watlowlib.protocol.stdbus.types import StdBusReply
    from watlowlib.registry.parameters import ParameterRegistry

__all__ = [
    "Command",
    "CommandContext",
    "ModbusVariant",
    "StdBusVariant",
]


def _empty_family_hints() -> frozenset[ControllerFamily]:
    return frozenset()


@dataclass(frozen=True, slots=True)
class CommandContext:
    """Context available to every variant during ``encode`` / ``decode``.

    Attributes:
        registry: Parameter spec lookup. Callers typically pass
            :data:`watlowlib.registry.PARAMETERS`.
        family: Best-known family for the device. ``UNKNOWN`` is
            treated as "no prior" — variants attempt anyway and let
            availability be observed from the response.
        address: Bus address, threaded through for richer
            :class:`watlowlib.errors.ErrorContext` on failure.
        port: Transport label, threaded through for the same reason.
    """

    registry: ParameterRegistry
    family: ControllerFamily = ControllerFamily.UNKNOWN
    address: int = 0
    port: str = ""


class StdBusVariant[Req, Resp](Protocol):
    """Std Bus encode + decode for one :class:`Command`.

    Variants are stateless — implementations are typically ``@dataclass``
    instances stored once on the :class:`Command` definition.
    """

    def encode(self, ctx: CommandContext, request: Req) -> bytes:
        """Produce inner Watlow payload bytes for ``request``."""
        ...

    def decode(self, reply: StdBusReply, ctx: CommandContext) -> Resp:
        """Convert ``reply`` to the typed response."""
        ...


class ModbusVariant[Req, Resp](Protocol):
    """Modbus encode + decode for one :class:`Command`.

    Variants emit a typed :class:`ModbusOp` rather than wire bytes
    because :mod:`anymodbus` already owns the PDU codec — handing it
    bytes would be a layer violation. The
    :class:`watlowlib.protocol.modbus.client.ModbusProtocolClient`
    lowers the op onto the matching :class:`anymodbus.Slave` method
    and returns the raw register tuple, which the variant ``decode``
    converts into the typed response.

    Variants are stateless — implementations are typically
    ``@dataclass`` instances stored once on the :class:`Command`
    definition.
    """

    def encode(self, ctx: CommandContext, request: Req) -> ModbusOp:
        """Produce a :class:`ModbusOp` for ``request``."""
        ...

    def decode(self, words: tuple[int, ...], ctx: CommandContext, request: Req) -> Resp:
        """Convert raw Modbus register words to the typed response.

        ``request`` is threaded through so the variant can recover
        per-request context (e.g. which parameter spec to populate
        the response with) without re-resolving from the registry.
        Writes return ``words=()`` — variants for writes typically
        echo the request value rather than parsing words.
        """
        ...


@dataclass(frozen=True, slots=True)
class Command[Req, Resp]:
    """A pure descriptor — request type + per-protocol variants.

    Attributes:
        name: Human-readable name; threaded into log events and error
            contexts.
        stdbus: Std Bus variant, or ``None`` if the command has no Std
            Bus binding.
        modbus: Modbus variant, or ``None`` if the command has no
            Modbus binding.
        family_hints: Advisory family priors. ``frozenset()`` means
            "no prior — attempt anywhere".
        capability_hints: Advisory capability metadata. The v1
            session does not hard-gate on this field.
        safety: Determines whether ``confirm=True`` is required at
            the facade.
        min_firmware: Optional firmware-floor metadata. The v1 session
            does not enforce it.
    """

    name: str
    stdbus: StdBusVariant[Req, Resp] | None = None
    modbus: ModbusVariant[Req, Resp] | None = None
    family_hints: frozenset[ControllerFamily] = field(default_factory=_empty_family_hints)
    capability_hints: Capability = Capability.NONE
    safety: SafetyTier = SafetyTier.READ_ONLY
    min_firmware: FirmwareVersion | None = None
