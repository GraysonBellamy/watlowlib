"""Targeted regression tests for :mod:`watlowlib.transport.serial`.

Covers the OSError disconnect-errno → WatlowConnectionError mapping
that lets application code pattern-match USB-485 cable yanks
reliably. The full integration coverage of SerialTransport flows
through the protocol-client and detector test suites; this file
exists for the narrow errno-mapping path that doesn't surface
anywhere else.
"""

from __future__ import annotations

import errno

import pytest

from watlowlib.errors import WatlowConnectionError, WatlowTransportError
from watlowlib.transport.base import SerialSettings
from watlowlib.transport.serial import SerialTransport


class _RaisingPort:
    """Stub :mod:`anyserial` port that raises a chosen :class:`OSError` on send/receive."""

    def __init__(self, exc: OSError) -> None:
        self._exc = exc
        self.is_open = True

    async def send(self, data: bytes) -> None:
        del data
        raise self._exc

    async def receive(self, n: int) -> bytes:
        del n
        raise self._exc

    async def aclose(self) -> None:
        self.is_open = False

    async def reset_input_buffer(self) -> None:
        pass


@pytest.mark.parametrize(
    "errno_value",
    [errno.EIO, errno.ENXIO, errno.ENODEV, errno.EBADF],
)
@pytest.mark.anyio
async def test_write_raises_connection_error_on_disconnect_errno(
    errno_value: int,
    anyio_backend: object,
) -> None:
    """``OSError`` carrying a disconnect errno surfaces as ``WatlowConnectionError``."""
    _ = anyio_backend
    transport = SerialTransport(SerialSettings(port="/dev/null"))
    stub = _RaisingPort(OSError(errno_value, "scripted"))
    transport._port = stub  # type: ignore[assignment]

    with pytest.raises(WatlowConnectionError, match="disconnected"):
        await transport.write(b"x", timeout=1.0)


@pytest.mark.anyio
async def test_write_raises_transport_error_on_unrelated_errno(anyio_backend: object) -> None:
    """A non-disconnect ``OSError`` still maps to ``WatlowTransportError``."""
    _ = anyio_backend
    transport = SerialTransport(SerialSettings(port="/dev/null"))
    stub = _RaisingPort(OSError(errno.EINVAL, "scripted"))
    transport._port = stub  # type: ignore[assignment]

    with pytest.raises(WatlowTransportError):
        await transport.write(b"x", timeout=1.0)


@pytest.mark.parametrize(
    "errno_value",
    [errno.EIO, errno.ENXIO, errno.ENODEV],
)
@pytest.mark.anyio
async def test_read_raises_connection_error_on_disconnect_errno(
    errno_value: int,
    anyio_backend: object,
) -> None:
    """Symmetric mapping on the read path."""
    _ = anyio_backend
    transport = SerialTransport(SerialSettings(port="/dev/null"))
    stub = _RaisingPort(OSError(errno_value, "scripted"))
    transport._port = stub  # type: ignore[assignment]

    with pytest.raises(WatlowConnectionError, match="disconnected"):
        await transport.read_exact(4, timeout=1.0)
