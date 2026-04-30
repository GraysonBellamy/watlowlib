"""Typed exception hierarchy for :mod:`watlowlib`.

Every library exception inherits from :class:`WatlowError` and carries a
structured :class:`ErrorContext`. See ``docs/design.md`` §8.

``ErrorContext`` selector fields are populated per-protocol:

- Std Bus failures fill ``cls`` / ``member`` / ``instance``.
- Modbus failures fill ``register_address`` / ``function_code``.

Wire-level fields (``request`` / ``response`` / ``elapsed_s``) are best-
effort and may be ``None`` for failures raised before I/O.

:class:`WatlowCapabilityWarning`, :class:`WatlowCapabilityError`, and
:class:`WatlowFirmwareError` are reserved as part of the planned
capability-gate hierarchy (design §5). They are exported for callers
that want to ``except`` on them without pinning a future minor; the
library does not currently emit any of the three. Capability mismatches
on writes (e.g. cool-side gains on a heat-only PM) currently surface as
:class:`WatlowConfigurationError`.
"""

from __future__ import annotations

from dataclasses import dataclass, field, fields, replace
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Self, cast

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from watlowlib.protocol.base import ProtocolKind


_EMPTY_EXTRA: Mapping[str, Any] = MappingProxyType({})


def _empty_extra() -> Mapping[str, Any]:
    return _EMPTY_EXTRA


@dataclass(frozen=True, slots=True)
class ErrorContext:
    """Structured context attached to every :class:`WatlowError`.

    Fields are best-effort — missing data is ``None`` rather than raising.

    ``extra`` accepts any ``Mapping`` and is always frozen into a read-only
    :class:`types.MappingProxyType` at construction so the shared empty
    sentinel can never be mutated through ``error.context.extra[k] = v``.
    """

    command_name: str | None = None
    protocol: ProtocolKind | None = None
    port: str | None = None
    address: int | None = None
    parameter_id: int | None = None
    cls: int | None = None
    member: int | None = None
    instance: int | None = None
    register_address: int | None = None
    function_code: int | None = None
    request: bytes | None = None
    response: bytes | None = None
    elapsed_s: float | None = None
    extra: Mapping[str, Any] = field(default_factory=_empty_extra)

    def __post_init__(self) -> None:
        if not isinstance(self.extra, MappingProxyType):
            object.__setattr__(self, "extra", MappingProxyType(dict(self.extra)))

    def merged(self, **updates: Any) -> Self:
        """Return a new context with ``updates`` overlaid. Unknown keys go to ``extra``."""
        known: dict[str, Any] = {}
        extra_updates: dict[str, Any] = {}
        for key, value in updates.items():
            if key in _CONTEXT_KNOWN_FIELDS:
                known[key] = value
            else:
                extra_updates[key] = value

        new_extra: Mapping[str, Any] = (
            MappingProxyType({**self.extra, **extra_updates}) if extra_updates else self.extra
        )
        return replace(self, **known, extra=new_extra)


_CONTEXT_KNOWN_FIELDS: frozenset[str] = frozenset(
    f.name for f in fields(ErrorContext) if f.name != "extra"
)


_EMPTY_CONTEXT = ErrorContext()


def _context_field(
    label: str,
    value: object | None,
    formatter: Callable[[object], str] = str,
) -> str | None:
    if value is None:
        return None
    return f"{label}={formatter(value)}"


def _format_protocol(value: object) -> str:
    return cast("ProtocolKind", value).name


def _format_hex2(value: object) -> str:
    return f"0x{cast('int', value):02X}"


def _format_hex4(value: object) -> str:
    return f"0x{cast('int', value):04X}"


def _format_elapsed(value: object) -> str:
    return f"{cast('float', value):.3f}"


def _context_bits(ctx: ErrorContext) -> list[str]:
    bits = [
        bit
        for bit in (
            _context_field("command", ctx.command_name),
            _context_field("protocol", ctx.protocol, _format_protocol),
            _context_field("port", ctx.port),
            _context_field("address", ctx.address),
            _context_field("cls", ctx.cls, _format_hex2),
            _context_field("member", ctx.member, _format_hex2),
            _context_field("instance", ctx.instance),
            _context_field("register", ctx.register_address, _format_hex4),
            _context_field("function", ctx.function_code, _format_hex2),
            _context_field("parameter_id", ctx.parameter_id),
            _context_field("elapsed_s", ctx.elapsed_s, _format_elapsed),
            _context_field("request", ctx.request, repr),
            _context_field("response", ctx.response, repr),
        )
        if bit is not None
    ]
    if ctx.extra:
        bits.append(f"extra={dict(ctx.extra)!r}")
    return bits


class WatlowError(Exception):
    """Base class for every :mod:`watlowlib` exception.

    Carries a typed :class:`ErrorContext`. The ``message`` is the human-readable
    summary; the context is the machine-readable detail.
    """

    context: ErrorContext

    def __init__(self, message: str = "", *, context: ErrorContext | None = None) -> None:
        super().__init__(message)
        self.context = context if context is not None else _EMPTY_CONTEXT

    def with_context(self, **updates: Any) -> Self:
        """Return a copy of this error with its context updated.

        Useful when an inner layer raises and an outer layer wants to enrich
        the context (for instance adding ``port`` or ``elapsed_s``).
        """
        cls = type(self)
        new = cls.__new__(cls)
        new.args = self.args
        try:
            new.__dict__.update(self.__dict__)
        except AttributeError:  # pragma: no cover — no slotted subclass today
            for slot in getattr(cls, "__slots__", ()):
                if hasattr(self, slot):
                    object.__setattr__(new, slot, getattr(self, slot))
        new.context = self.context.merged(**updates)
        new.__cause__ = self.__cause__
        new.__context__ = self.__context__
        new.__traceback__ = self.__traceback__
        return new

    def __str__(self) -> str:
        base = super().__str__()
        bits = _context_bits(self.context)
        return f"{base} [{', '.join(bits)}]" if bits else base


# --- Configuration -------------------------------------------------------


class WatlowConfigurationError(WatlowError):
    """Configuration-level error (bad args, conflicting settings)."""


class WatlowConfirmationRequiredError(WatlowConfigurationError):
    """A PERSISTENT / DANGEROUS command was attempted without ``confirm=True``."""


class WatlowValidationError(WatlowConfigurationError):
    """Request validation failed before I/O (bad instance, bad value)."""


# --- Transport -----------------------------------------------------------


class WatlowTransportError(WatlowError):
    """I/O-layer error from the transport."""


class WatlowTimeoutError(WatlowTransportError):
    """A transport read or write timed out."""


class WatlowConnectionError(WatlowTransportError):
    """Could not open / lost the connection to the device."""


# --- Protocol ------------------------------------------------------------


class WatlowProtocolError(WatlowError):
    """Protocol-level error (framing, parsing, unrecognised reply)."""


class WatlowFrameError(WatlowProtocolError):
    """Bad CRC, bad length, malformed framing, etc."""


class WatlowProtocolUnsupportedError(WatlowProtocolError):
    """The active protocol cannot satisfy this command on this device.

    Sticky for the session: subsequent attempts at the same command
    short-circuit with this error pre-I/O. Set on Std Bus ``0x81`` /
    ``0x83`` and on Modbus ``IllegalFunction`` / ``IllegalDataAddress``
    per ``docs/design.md`` §5b.
    """


# --- Std Bus -------------------------------------------------------------


class WatlowNoSuchObjectError(WatlowProtocolError):
    """Standard Bus error 0x81 — invalid class.

    The device does not expose the requested object class. Maps to
    :class:`Availability.UNSUPPORTED` in the session cache.
    """


class WatlowNoSuchAttributeError(WatlowProtocolError):
    """Standard Bus error 0x83 — valid class, invalid member.

    Maps to :class:`Availability.UNSUPPORTED` in the session cache.
    """


class WatlowNoSuchInstanceError(WatlowProtocolError):
    """Standard Bus error 0x84 — valid class+member, invalid instance.

    The *parameter* exists but the requested loop / channel does not.
    Does **not** affect availability (a different instance may succeed).
    """


# --- Modbus --------------------------------------------------------------


class WatlowModbusError(WatlowProtocolError):
    """Base class for Modbus-layer errors.

    Wraps every ``anymodbus`` exception so callers see one error
    hierarchy regardless of protocol. ``__cause__`` preserves the
    original ``anymodbus`` exception for callers that need it.
    """


class WatlowModbusIllegalFunctionError(WatlowModbusError, WatlowProtocolUnsupportedError):
    """Modbus exception 0x01 — slave does not implement the function.

    Maps to :class:`Availability.UNSUPPORTED` in the session cache.
    Inherits :class:`WatlowProtocolUnsupportedError` so the session's
    sticky-unsupported handling treats it like Std Bus ``0x81`` /
    ``0x83``.
    """


class WatlowModbusIllegalDataAddressError(WatlowModbusError, WatlowProtocolUnsupportedError):
    """Modbus exception 0x02 — register address not allowed.

    Maps to :class:`Availability.UNSUPPORTED` in the session cache.
    """


class WatlowModbusIllegalDataValueError(WatlowModbusError):
    """Modbus exception 0x03 — bad argument, not absence.

    Does **not** affect availability (the parameter exists; the
    write value was simply rejected).
    """


class WatlowModbusSlaveFailureError(WatlowModbusError):
    """Modbus exception 0x04 — unrecoverable slave-side error.

    Does **not** affect availability — non-response is not a refusal
    of the parameter (per design §5b table).
    """


class WatlowModbusTimeoutError(WatlowModbusError, WatlowTimeoutError):
    """Modbus request timed out at the protocol layer.

    Inherits :class:`WatlowTimeoutError` so callers with existing
    timeout-handling code see this as a transport timeout. Does **not**
    affect availability.
    """


# --- Capability ----------------------------------------------------------


class WatlowCapabilityError(WatlowError):
    """Command is not available on this device / firmware / family.

    Reserved for the planned capability-gate hierarchy. The library
    does not currently raise this — capability mismatches surface as
    :class:`WatlowConfigurationError` today (see ``commands/loop.py``).
    """


class WatlowFirmwareError(WatlowCapabilityError):
    """Command is outside the supported firmware window.

    Reserved alongside :class:`WatlowCapabilityError`; not currently
    emitted by the library.
    """


# --- Warnings ------------------------------------------------------------


class WatlowCapabilityWarning(UserWarning):
    """Reserved warning class — not currently emitted.

    Planned use is non-strict family-prior mismatches (attempt the
    command, warn, update availability from the device's response).
    The library does not currently emit this; the warning class is
    exported so callers can pre-register filters.
    """


# --- Sinks ---------------------------------------------------------------


class WatlowSinkError(WatlowError):
    """Base class for :mod:`watlowlib.sinks` errors."""


class WatlowSinkSchemaError(WatlowSinkError):
    """The target sink's existing schema does not match what the row carries.

    Raised when a sink configured with ``create_table=False`` is pointed
    at a missing or incompatible backing schema.
    """


class WatlowSinkWriteError(WatlowSinkError):
    """A sink-backend write failed (CREATE TABLE, INSERT, file IO, ...).

    ``__cause__`` preserves the backend-native exception (``sqlite3.Error``,
    ``OSError``, ...) for callers that want to inspect it.
    """


class WatlowSinkDependencyError(WatlowSinkError):
    """An optional sink extra is not installed.

    Raised by sinks behind a ``[parquet]`` / ``[postgres]`` extra when the
    backing dependency (``pyarrow``, ``asyncpg``) is missing at
    :meth:`open` time. Instantiating the sink succeeds on bare-core
    installs; the dependency check is deferred so import-time errors
    don't break callers that never reach for the extra.
    """


__all__ = [
    "ErrorContext",
    "WatlowCapabilityError",
    "WatlowCapabilityWarning",
    "WatlowConfigurationError",
    "WatlowConfirmationRequiredError",
    "WatlowConnectionError",
    "WatlowError",
    "WatlowFirmwareError",
    "WatlowFrameError",
    "WatlowModbusError",
    "WatlowModbusIllegalDataAddressError",
    "WatlowModbusIllegalDataValueError",
    "WatlowModbusIllegalFunctionError",
    "WatlowModbusSlaveFailureError",
    "WatlowModbusTimeoutError",
    "WatlowNoSuchAttributeError",
    "WatlowNoSuchInstanceError",
    "WatlowNoSuchObjectError",
    "WatlowProtocolError",
    "WatlowProtocolUnsupportedError",
    "WatlowSinkDependencyError",
    "WatlowSinkError",
    "WatlowSinkSchemaError",
    "WatlowSinkWriteError",
    "WatlowTimeoutError",
    "WatlowTransportError",
    "WatlowValidationError",
]
