"""Serial-port transport backed by :mod:`anyserial`.

:class:`SerialTransport` wraps :class:`anyserial.SerialPort`. Every I/O
call is bounded by :func:`anyio.fail_after` (reads, writes) or
:func:`anyio.move_on_after` (idle-timeout reads). Backend exceptions
normalise to :mod:`watlowlib.errors` types with ``__cause__`` preserved.
"""

from __future__ import annotations

import contextlib
import errno
from typing import TYPE_CHECKING

import anyio
from anyserial import (
    FlowControl,
    PortBusyError,
    PortNotFoundError,
    SerialClosedError,
    SerialConfig,
    SerialDisconnectedError,
    SerialError,
    open_serial_port,
)

from watlowlib.errors import (
    ErrorContext,
    WatlowConnectionError,
    WatlowTimeoutError,
    WatlowTransportError,
)

# kernel errnos that mean "the port is gone, the cable was yanked,
# or the fd is no longer talking to anything." Distinguished from
# generic ``OSError`` so callers that pattern-match on
# :class:`WatlowConnectionError` reliably catch USB-485 disconnects
# instead of seeing a raw ``OSError(EIO)``.
#
# The anyserial backend maps some of these to
# :class:`SerialDisconnectedError`, but mid-write EIO from the kernel
# surfaces as a bare ``OSError`` that bypasses the typed wrappers.
_DISCONNECT_ERRNOS: frozenset[int] = frozenset(
    {
        errno.EIO,  # USB removed mid-write
        errno.ENXIO,  # device gone
        errno.ENODEV,  # node removed
        errno.EBADF,  # fd closed under us
        errno.ENOTTY,  # serial node became a non-terminal (uncommon)
        errno.EPIPE,  # broken pipe (rare on serial; included for completeness)
    }
)

if TYPE_CHECKING:
    from anyserial import SerialPort

    from watlowlib.transport.base import SerialSettings

__all__ = ["SerialTransport"]

# Per-call read chunk. Bigger is fine — anyserial returns whatever the
# kernel has ready and never blocks waiting to fill the buffer.
_RECEIVE_CHUNK: int = 4096


def _port_open_error_types() -> tuple[type[BaseException], ...]:
    """Build the ``except`` tuple used by :meth:`SerialTransport.open`.

    ``termios.error`` is a bare :class:`Exception` on CPython (not an
    :class:`OSError` subclass), so it has to be listed alongside
    :class:`OSError` explicitly for phantom ``/dev/ttyS*`` UARTs that
    fail ``tcgetattr`` with EIO.
    """
    try:
        import termios  # noqa: PLC0415 — platform-gated optional import
    except ImportError:  # pragma: no cover — Windows has no termios module
        return (OSError,)
    return (OSError, termios.error)


_PORT_OPEN_ERRORS: tuple[type[BaseException], ...] = _port_open_error_types()


class SerialTransport:
    """:class:`Transport` backed by a real serial port via ``anyserial``.

    Tests that don't need hardware should use
    :class:`watlowlib.transport.fake.FakeTransport`; the two conform to
    the same structural :class:`Transport` Protocol.
    """

    def __init__(self, settings: SerialSettings) -> None:
        self._settings = settings
        self._port: SerialPort | None = None
        # Bytes read past ``n`` in :meth:`read_exact` (e.g. when a
        # framing error makes the caller scan for a new preamble) are
        # held here so the next call sees them first. Serial I/O is
        # chunk-oriented; we can't ask the kernel "give me exactly n"
        # without buffering.
        self._pushback = bytearray()

    async def open(self) -> None:
        if self._port is not None:
            raise WatlowConnectionError(
                f"{self.label} is already open",
                context=ErrorContext(port=self.label),
            )
        config = SerialConfig(
            baudrate=self._settings.baudrate,
            byte_size=self._settings.bytesize,
            parity=self._settings.parity,
            stop_bits=self._settings.stopbits,
            flow_control=FlowControl(
                xon_xoff=self._settings.xonxoff,
                rts_cts=self._settings.rtscts,
            ),
            exclusive=self._settings.exclusive,
        )
        try:
            self._port = await open_serial_port(self._settings.port, config)
        except (PortBusyError, PortNotFoundError, SerialDisconnectedError) as exc:
            raise WatlowConnectionError(
                f"could not open {self.label}: {exc}",
                context=ErrorContext(port=self.label),
            ) from exc
        except SerialError as exc:
            raise WatlowTransportError(
                f"backend error opening {self.label}: {exc}",
                context=ErrorContext(port=self.label),
            ) from exc
        except _PORT_OPEN_ERRORS as exc:
            # Lower-level kernel errors (``termios.error``, EIO,
            # EACCES) can leak past the typed wrappers. Surface as
            # WatlowConnectionError so discovery (which promises to
            # never raise) can collect the failure.
            raise WatlowConnectionError(
                f"could not open {self.label}: {exc}",
                context=ErrorContext(port=self.label),
            ) from exc

    async def close(self) -> None:
        port = self._port
        if port is None:
            return
        self._port = None
        self._pushback.clear()
        # A wedged USB-485 dongle can hang ``aclose`` indefinitely
        # because the kernel-side write queue never drains. Bound the
        # close at 1s and swallow both the timeout and any backend /
        # kernel error — the caller already lost the port, blocking
        # them on cleanup helps no one.
        with contextlib.suppress(SerialError, OSError, TimeoutError):
            with anyio.fail_after(1.0):
                await port.aclose()

    async def write(self, data: bytes, *, timeout: float) -> None:
        port = self._require_port()
        try:
            with anyio.fail_after(timeout):
                await port.send(data)
        except TimeoutError as exc:
            raise WatlowTimeoutError(
                f"write on {self.label} timed out after {timeout}s",
                context=ErrorContext(port=self.label),
            ) from exc
        except (SerialClosedError, SerialDisconnectedError) as exc:
            raise WatlowConnectionError(
                f"write on {self.label} failed: {exc}",
                context=ErrorContext(port=self.label),
            ) from exc
        except SerialError as exc:
            raise WatlowTransportError(
                f"write on {self.label} failed: {exc}",
                context=ErrorContext(port=self.label),
            ) from exc
        except OSError as exc:
            # USB-485 yank surfaces as raw OSError(EIO) past the
            # backend's typed wrappers; map disconnect errnos to the
            # public connection-error type so callers can pattern-match.
            if exc.errno in _DISCONNECT_ERRNOS:
                raise WatlowConnectionError(
                    f"write on {self.label} disconnected: {exc}",
                    context=ErrorContext(port=self.label),
                ) from exc
            raise WatlowTransportError(
                f"write on {self.label} failed: {exc}",
                context=ErrorContext(port=self.label),
            ) from exc

    async def read_exact(self, n: int, *, timeout: float) -> bytes:
        port = self._require_port()
        if n <= 0:
            return b""
        buf = bytearray(self._pushback)
        self._pushback.clear()
        try:
            with anyio.fail_after(timeout):
                while len(buf) < n:
                    chunk = await port.receive(_RECEIVE_CHUNK)
                    if not chunk:
                        continue
                    buf.extend(chunk)
        except TimeoutError as exc:
            # Preserve whatever we did read — the next call may pick up
            # where this one left off once the device sends the rest.
            self._pushback.extend(buf)
            raise WatlowTimeoutError(
                f"read_exact({n}) on {self.label} timed out after {timeout}s",
                context=ErrorContext(port=self.label),
            ) from exc
        except (SerialClosedError, SerialDisconnectedError) as exc:
            raise WatlowConnectionError(
                f"read on {self.label} failed: {exc}",
                context=ErrorContext(port=self.label),
            ) from exc
        except SerialError as exc:
            raise WatlowTransportError(
                f"read on {self.label} failed: {exc}",
                context=ErrorContext(port=self.label),
            ) from exc
        except OSError as exc:
            if exc.errno in _DISCONNECT_ERRNOS:
                raise WatlowConnectionError(
                    f"read on {self.label} disconnected: {exc}",
                    context=ErrorContext(port=self.label),
                ) from exc
            raise WatlowTransportError(
                f"read on {self.label} failed: {exc}",
                context=ErrorContext(port=self.label),
            ) from exc

        result = bytes(buf[:n])
        leftover = bytes(buf[n:])
        if leftover:
            self._pushback.extend(leftover)
        return result

    async def read_available(
        self,
        *,
        idle_timeout: float,
        max_bytes: int | None = None,
    ) -> bytes:
        port = self._require_port()
        buf = bytearray(self._pushback)
        self._pushback.clear()
        cap = max_bytes if max_bytes and max_bytes > 0 else None
        while True:
            if cap is not None and len(buf) >= cap:
                break
            with anyio.move_on_after(idle_timeout) as scope:
                try:
                    chunk = await port.receive(_RECEIVE_CHUNK)
                except (SerialClosedError, SerialDisconnectedError) as exc:
                    raise WatlowConnectionError(
                        f"read on {self.label} disconnected: {exc}",
                        context=ErrorContext(port=self.label),
                    ) from exc
                except SerialError as exc:
                    raise WatlowTransportError(
                        f"read on {self.label} failed: {exc}",
                        context=ErrorContext(port=self.label),
                    ) from exc
                except OSError as exc:
                    if exc.errno in _DISCONNECT_ERRNOS:
                        raise WatlowConnectionError(
                            f"read on {self.label} disconnected: {exc}",
                            context=ErrorContext(port=self.label),
                        ) from exc
                    raise WatlowTransportError(
                        f"read on {self.label} failed: {exc}",
                        context=ErrorContext(port=self.label),
                    ) from exc
                buf.extend(chunk)
            if scope.cancelled_caught:
                break
        if cap is not None and len(buf) > cap:
            leftover = bytes(buf[cap:])
            self._pushback.extend(leftover)
            return bytes(buf[:cap])
        return bytes(buf)

    async def drain_input(self) -> None:
        self._pushback.clear()
        port = self._port
        if port is None:
            return
        with contextlib.suppress(SerialError, OSError):
            await port.reset_input_buffer()

    @property
    def is_open(self) -> bool:
        return self._port is not None and self._port.is_open

    @property
    def label(self) -> str:
        return self._settings.port

    def _require_port(self) -> SerialPort:
        port = self._port
        if port is None:
            raise WatlowConnectionError(
                f"{self.label} is not open",
                context=ErrorContext(port=self.label),
            )
        return port
