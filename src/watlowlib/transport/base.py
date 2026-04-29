"""Transport :pep:`544` Protocol + :class:`SerialSettings`.

The transport surface is intentionally small — Standard Bus is fully
length-prefixed (BACnet MS/TP outer frame), so the protocol client
only needs ``write`` and ``read_exact``. ``read_available`` exists for
draining the line between auto-detect probes; ``drain_input`` is the
synchronous flush used after a framing error before the next attempt.

Default serial framing for Standard Bus on the EZ-ZONE PM family is
**38400 8-N-1** per the PM manuals; Modbus RTU on the same family is
configurable across 9600 / 19200 / 38400 / 57600 / 115200. The
:class:`SerialSettings` defaults match the Std Bus factory state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from anyserial import ByteSize, Parity, StopBits

from watlowlib.errors import WatlowConfigurationError

if TYPE_CHECKING:
    from watlowlib.protocol.base import ProtocolKind

__all__ = [
    "ByteSize",
    "Parity",
    "SerialSettings",
    "StopBits",
    "Transport",
]


def _coerce_bytesize(value: object) -> ByteSize:
    """Coerce ``value`` to :class:`anyserial.ByteSize`.

    Accepts the enum directly, ``int`` (5/6/7/8), or string form.
    Raises :class:`WatlowConfigurationError` on anything else — better
    UX than letting an unconverted ``int`` leak into anyserial's
    termios layer where it crashes deep with ``NoneType.iflag``.
    """
    if isinstance(value, ByteSize):
        return value
    try:
        return ByteSize(str(value))
    except ValueError as exc:
        raise WatlowConfigurationError(
            f"invalid bytesize {value!r}; expected ByteSize, int, or one of "
            f"{[m.value for m in ByteSize]!r}",
        ) from exc


def _coerce_parity(value: object) -> Parity:
    """Coerce ``value`` to :class:`anyserial.Parity`.

    Accepts the enum directly or string form
    (``"none"``/``"odd"``/``"even"``/``"mark"``/``"space"``).
    """
    if isinstance(value, Parity):
        return value
    if isinstance(value, str):
        try:
            return Parity(value.lower())
        except ValueError as exc:
            raise WatlowConfigurationError(
                f"invalid parity {value!r}; expected Parity or one of "
                f"{[m.value for m in Parity]!r}",
            ) from exc
    raise WatlowConfigurationError(
        f"invalid parity {value!r}; expected Parity or str",
    )


def _coerce_stopbits(value: object) -> StopBits:
    """Coerce ``value`` to :class:`anyserial.StopBits`.

    Accepts the enum directly, ``int`` (1 / 2), ``float`` (1.0 / 1.5 /
    2.0), or string form.
    """
    if isinstance(value, StopBits):
        return value
    # ``bool`` is a subclass of ``int``; reject explicitly so
    # ``stopbits=True`` doesn't silently pass through as 1.
    if isinstance(value, bool):
        raise WatlowConfigurationError(
            f"invalid stopbits {value!r}; bool is not a valid stopbits value",
        )
    if isinstance(value, int):
        key = str(value)
    elif isinstance(value, float):
        key = str(int(value)) if value.is_integer() else str(value)
    elif isinstance(value, str):
        key = value
    else:
        raise WatlowConfigurationError(
            f"invalid stopbits {value!r}; expected StopBits, int, float, or str",
        )
    try:
        return StopBits(key)
    except ValueError as exc:
        raise WatlowConfigurationError(
            f"invalid stopbits {value!r}; expected StopBits or one of "
            f"{[m.value for m in StopBits]!r}",
        ) from exc


class Transport(Protocol):
    """Byte-level transport.

    Every I/O boundary takes an explicit ``timeout``. On expiry,
    implementations raise :class:`watlowlib.errors.WatlowTimeoutError`
    — never return an empty or partial ``bytes`` silently. Backend
    exceptions normalise to
    :class:`watlowlib.errors.WatlowTransportError` (or a subclass)
    with ``__cause__`` preserving the original exception.

    Lifecycle is single-shot: :meth:`open` once, :meth:`close` once.
    """

    async def open(self) -> None:
        """Open the underlying port. Re-open on an already-open transport is an error."""
        ...

    async def close(self) -> None:
        """Close the underlying port. Safe to call when already closed."""
        ...

    async def write(self, data: bytes, *, timeout: float) -> None:
        """Write every byte of ``data``. Bounded by ``timeout``."""
        ...

    async def read_exact(self, n: int, *, timeout: float) -> bytes:
        """Read exactly ``n`` bytes.

        Raises :class:`watlowlib.errors.WatlowTimeoutError` if fewer
        than ``n`` bytes arrive before ``timeout``. Partial buffers are
        retained for the next call — implementations must not discard
        them.
        """
        ...

    async def read_available(
        self,
        *,
        idle_timeout: float,
        max_bytes: int | None = None,
    ) -> bytes:
        """Read until the line goes idle for ``idle_timeout`` seconds.

        Never raises on idle expiry — an idle timeout is the *expected*
        exit. Returns whatever was accumulated (possibly empty). Used
        for best-effort drain and ``ProtocolKind.AUTO`` probe gaps.
        """
        ...

    async def drain_input(self) -> None:
        """Discard any buffered input bytes. Best-effort; never raises."""
        ...

    @property
    def is_open(self) -> bool:
        """Whether :meth:`open` has run without a matching :meth:`close`."""
        ...

    @property
    def label(self) -> str:
        """Short identifier (port path, ``"fake://..."``) used in errors."""
        ...


@dataclass(frozen=True, slots=True)
class SerialSettings:
    """Serial-port configuration for :class:`SerialTransport`.

    Mirrors :class:`anyserial.SerialConfig` plus a ``port`` path. Default
    framing is **38400 8-N-1**, the EZ-ZONE PM Standard Bus factory
    setting. ``exclusive`` defaults ``True`` because Standard Bus is
    poll/response and won't tolerate a second writer.

    The ``__post_init__`` accepts ``int`` / ``float`` / ``str`` shorthand
    at runtime for the framing fields (``bytesize=8``, ``parity="none"``,
    ``stopbits=1``) and normalises to the enum. The static field types
    are the enums themselves so ``mypy --strict`` users must pass
    :class:`anyserial.ByteSize` / :class:`anyserial.Parity` /
    :class:`anyserial.StopBits` directly; the runtime shorthand is
    primarily for CLI argument parsing and interactive scripts.
    """

    port: str
    baudrate: int = 38400
    bytesize: ByteSize = ByteSize.EIGHT
    parity: Parity = Parity.NONE
    stopbits: StopBits = StopBits.ONE
    rtscts: bool = False
    xonxoff: bool = False
    exclusive: bool = True

    def __post_init__(self) -> None:
        # Same trick the stdlib uses for frozen-dataclass __post_init__
        # normalisation. The coercers accept the widened input types
        # (int/float/str) at runtime; mypy sees the field as the enum
        # already so the call type-checks without ignores.
        object.__setattr__(self, "bytesize", _coerce_bytesize(self.bytesize))
        object.__setattr__(self, "parity", _coerce_parity(self.parity))
        object.__setattr__(self, "stopbits", _coerce_stopbits(self.stopbits))

    @classmethod
    def factory_for(cls, protocol: ProtocolKind, *, port: str) -> SerialSettings:
        """Return the EZ-ZONE PM factory framing for ``protocol``.

        - ``STDBUS`` → 38400 8-N-1 (the Standard Bus factory default).
        - ``MODBUS_RTU`` → 9600 8-E-1 (the Modbus RTU factory default
          per the EZ-ZONE PM manual).

        ``AUTO`` raises :class:`WatlowConfigurationError` — there is no
        single factory framing for AUTO, the detector probes both.
        Callers crossing protocol boundaries (the maintenance helpers
        that switch protocol, ``watlow-discover --protocol both``)
        should rebuild settings per protocol via this method instead
        of inheriting whatever framing the previous call used.
        """
        # Lazy import to keep ``transport.base`` a leaf module — the
        # ProtocolKind enum lives under protocol/, which depends on
        # transport indirectly.
        from watlowlib.protocol.base import ProtocolKind  # noqa: PLC0415

        if protocol is ProtocolKind.STDBUS:
            return cls(port=port, baudrate=38400, parity=Parity.NONE)
        if protocol is ProtocolKind.MODBUS_RTU:
            return cls(port=port, baudrate=9600, parity=Parity.EVEN)
        raise WatlowConfigurationError(
            f"SerialSettings.factory_for: no single factory framing for {protocol!r}; "
            "AUTO probes both Std Bus and Modbus, build a concrete protocol's "
            "settings instead.",
        )
