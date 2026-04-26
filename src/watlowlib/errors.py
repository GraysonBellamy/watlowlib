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

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from watlowlib.protocol.base import ProtocolKind


@dataclass(frozen=True, slots=True)
class ErrorContext:
    """Structured context attached to every :class:`WatlowError`.

    Fields are best-effort — missing data is ``None`` rather than raising.
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


class WatlowError(Exception):
    """Base class for every :mod:`watlowlib` exception."""

    def __init__(self, message: str = "", *, context: ErrorContext | None = None) -> None:
        super().__init__(message)
        self.context = context or ErrorContext()


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
