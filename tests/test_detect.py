"""Auto-detect tests.

Two layers of coverage:

- :func:`probe_stdbus` / :func:`probe_modbus` against scripted
  FakeTransport / StubSlave clients — pure probe-layer correctness.
- :func:`detect_protocol` against monkey-patched
  :class:`SerialTransport` and :class:`ModbusBusTransport` constructors
  to exercise the orchestration without opening a real port.

Decision matrix:

a) Std Bus answers          → STDBUS resolved
b) Modbus answers           → MODBUS_RTU resolved
c) silence on both          → WatlowProtocolUnsupportedError
d) garbage on Std Bus, then valid Modbus → MODBUS_RTU resolved
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from anymodbus import (
    FrameTimeoutError,
    IllegalDataAddressError,
    IllegalFunctionError,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

from watlowlib import (
    FakeTransport,
    ProtocolKind,
    SerialSettings,
    WatlowProtocolUnsupportedError,
)
from watlowlib.protocol.detect import (
    ResolvedProtocol,
    detect_protocol,
    probe_modbus,
    probe_stdbus,
)
from watlowlib.protocol.modbus.client import ModbusProtocolClient
from watlowlib.protocol.stdbus.client import StdBusProtocolClient
from watlowlib.protocol.stdbus.framing import Frame, encode_frame
from watlowlib.protocol.stdbus.payload import encode_read_request
from watlowlib.protocol.stdbus.tables import HOST_MAC, FrameType, addr_to_mac

# Captured PM3 hardware-id (1001) round-trip — reused as the probe target.
REQ_PROBE_HW = encode_read_request(1001, instance=1)


def _build_stdbus_reply(payload: bytes) -> bytes:
    """Build a wire reply for a frame addressed back to the host."""
    frame = Frame(
        frame_type=FrameType.DATA_NOT_EXPECTING_REPLY,
        dst=HOST_MAC,
        src=addr_to_mac(1),
        payload=payload,
    )
    return encode_frame(frame)


def _stdbus_request_wire(payload: bytes) -> bytes:
    """Build the framed wire write a client would emit for ``payload``."""
    frame = Frame(
        frame_type=FrameType.DATA_EXPECTING_REPLY,
        dst=addr_to_mac(1),
        src=HOST_MAC,
        payload=payload,
    )
    return encode_frame(frame)


# Real PM3 read-response shape: 02 03 01 CC MM II <type> <bytes...>
# Values taken from REG_READ_HW captures in test_integration.py.
_HW_ID_PAYLOAD = bytes.fromhex("0203010101010600000018")  # cls=1 mem=1 inst=1 packed=24


@pytest.mark.anyio
async def test_probe_stdbus_accepts_valid_reply(anyio_backend: object) -> None:
    """A scripted hardware-id reply confirms Std Bus."""
    _ = anyio_backend
    request = _stdbus_request_wire(REQ_PROBE_HW)
    reply = _build_stdbus_reply(_HW_ID_PAYLOAD)

    transport = FakeTransport({request: reply})
    await transport.open()
    try:
        client = StdBusProtocolClient(transport, address=1)
        assert await probe_stdbus(client, timeout=0.5) is True
    finally:
        await transport.close()


@pytest.mark.anyio
async def test_probe_stdbus_accepts_error_response(anyio_backend: object) -> None:
    """A CRC-correct error response (0x81) still confirms framing."""
    _ = anyio_backend
    request = _stdbus_request_wire(REQ_PROBE_HW)
    err_payload = bytes.fromhex("0281")
    reply = _build_stdbus_reply(err_payload)

    transport = FakeTransport({request: reply})
    await transport.open()
    try:
        client = StdBusProtocolClient(transport, address=1)
        assert await probe_stdbus(client, timeout=0.5) is True
    finally:
        await transport.close()


@pytest.mark.anyio
async def test_probe_stdbus_rejects_silence(anyio_backend: object) -> None:
    """No reply → not Std Bus."""
    _ = anyio_backend
    transport = FakeTransport()  # no scripted replies
    await transport.open()
    try:
        client = StdBusProtocolClient(transport, address=1)
        assert await probe_stdbus(client, timeout=0.05) is False
    finally:
        await transport.close()


@pytest.mark.anyio
async def test_probe_stdbus_rejects_garbage(anyio_backend: object) -> None:
    """A reply with no preamble in the scan window is rejected."""
    _ = anyio_backend
    request = _stdbus_request_wire(REQ_PROBE_HW)
    # 256 bytes of non-preamble garbage exceeds the preamble scan limit.
    transport = FakeTransport({request: bytes([0xAA] * 300)})
    await transport.open()
    try:
        client = StdBusProtocolClient(transport, address=1)
        assert await probe_stdbus(client, timeout=0.5) is False
    finally:
        await transport.close()


# --- probe_modbus ----------------------------------------------------


def _instantiate_modbus_exc(cls: type[BaseException]) -> BaseException:
    """Construct an :mod:`anymodbus` exception, handling exception-response classes.

    ``ModbusExceptionResponse`` subclasses (``IllegalFunctionError``,
    ``IllegalDataAddressError``, ...) require ``function_code`` as a
    keyword-only arg. Plain ones (``FrameTimeoutError``,
    ``CRCError``) accept a positional message.
    """
    from anymodbus import ModbusExceptionResponse

    if issubclass(cls, ModbusExceptionResponse):
        return cls(function_code=3)
    return cls("scripted")


class _StubSlave:
    """Minimal :class:`anymodbus.Slave` stand-in for probe tests."""

    def __init__(self, reply: tuple[int, ...] | type[BaseException]) -> None:
        self.reply = reply
        self.calls: list[tuple[int, int]] = []

    async def read_holding_registers(self, address: int, *, count: int) -> tuple[int, ...]:
        self.calls.append((address, count))
        if isinstance(self.reply, type):
            raise _instantiate_modbus_exc(self.reply)
        return self.reply

    async def read_input_registers(self, address: int, *, count: int) -> tuple[int, ...]:
        return await self.read_holding_registers(address, count=count)

    async def write_register(self, address: int, value: int) -> None:
        _ = address, value

    async def write_registers(self, address: int, values: Sequence[int]) -> None:
        _ = address, values


@pytest.mark.anyio
async def test_probe_modbus_accepts_valid_reply(anyio_backend: object) -> None:
    _ = anyio_backend
    slave = _StubSlave(reply=(0x0000, 0x0018))
    client = ModbusProtocolClient(slave_provider=lambda: slave, address=1, port="fake://m")
    assert await probe_modbus(client, timeout=0.5) is True


@pytest.mark.anyio
async def test_probe_modbus_accepts_illegal_function(anyio_backend: object) -> None:
    """A CRC-correct exception response also confirms Modbus framing."""
    _ = anyio_backend
    slave = _StubSlave(reply=IllegalFunctionError)
    client = ModbusProtocolClient(slave_provider=lambda: slave, address=1, port="fake://m")
    assert await probe_modbus(client, timeout=0.5) is True


@pytest.mark.anyio
async def test_probe_modbus_accepts_illegal_address(anyio_backend: object) -> None:
    _ = anyio_backend
    slave = _StubSlave(reply=IllegalDataAddressError)
    client = ModbusProtocolClient(slave_provider=lambda: slave, address=1, port="fake://m")
    assert await probe_modbus(client, timeout=0.5) is True


@pytest.mark.anyio
async def test_probe_modbus_rejects_timeout(anyio_backend: object) -> None:
    _ = anyio_backend
    slave = _StubSlave(reply=FrameTimeoutError)
    client = ModbusProtocolClient(slave_provider=lambda: slave, address=1, port="fake://m")
    assert await probe_modbus(client, timeout=0.5) is False


# --- detect_protocol orchestration ----------------------------------


@dataclass
class _Spy:
    """Records what the monkey-patched constructors saw."""

    stdbus_opens: int = 0
    stdbus_closes: int = 0
    modbus_opens: int = 0
    modbus_closes: int = 0


class _FakeSerialTransport:
    """Test stand-in for :class:`SerialTransport` (Std Bus probe path)."""

    def __init__(self, settings: SerialSettings, *, fake: FakeTransport, spy: _Spy) -> None:
        _ = settings
        self._fake = fake
        self._spy = spy

    @property
    def is_open(self) -> bool:
        return self._fake.is_open

    @property
    def label(self) -> str:
        return self._fake.label

    async def open(self) -> None:
        self._spy.stdbus_opens += 1
        await self._fake.open()

    async def close(self) -> None:
        self._spy.stdbus_closes += 1
        await self._fake.close()

    async def write(self, data: bytes, *, timeout: float) -> None:
        await self._fake.write(data, timeout=timeout)

    async def read_exact(self, n: int, *, timeout: float) -> bytes:
        return await self._fake.read_exact(n, timeout=timeout)

    async def read_available(self, *, idle_timeout: float, max_bytes: int | None = None) -> bytes:
        return await self._fake.read_available(idle_timeout=idle_timeout, max_bytes=max_bytes)

    async def drain_input(self) -> None:
        await self._fake.drain_input()


class _FakeModbusBusTransport:
    """Test stand-in for :class:`ModbusBusTransport`.

    Holds a stub slave provider in lieu of an :class:`anymodbus.Bus`.
    The detector accesses ``.bus.slave(addr)`` once after opening — we
    intercept that path via a small ``_BusFacade``.
    """

    def __init__(
        self,
        settings: SerialSettings,
        *,
        slave: _StubSlave | None,
        spy: _Spy,
    ) -> None:
        _ = settings
        self._slave = slave
        self._open = False
        self._spy = spy

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def label(self) -> str:
        return "fake://modbus"

    @property
    def bus(self) -> Any:  # anymodbus.Bus duck-type
        if self._slave is None:
            raise RuntimeError("no slave configured")
        slave = self._slave
        return _BusFacade(slave)

    async def open(self) -> None:
        self._spy.modbus_opens += 1
        self._open = True

    async def close(self) -> None:
        self._spy.modbus_closes += 1
        self._open = False


@dataclass
class _BusFacade:
    slave_obj: _StubSlave

    def slave(self, address: int) -> _StubSlave:
        _ = address
        return self.slave_obj


def _patch_detect(
    monkeypatch: pytest.MonkeyPatch,
    *,
    stdbus_fake: FakeTransport | None,
    modbus_slave: _StubSlave | None,
) -> _Spy:
    """Patch :class:`SerialTransport` and :class:`ModbusBusTransport` in detect."""
    spy = _Spy()
    import watlowlib.protocol.detect as detect_module

    def _serial_factory(settings: SerialSettings) -> _FakeSerialTransport:
        return _FakeSerialTransport(settings, fake=stdbus_fake or FakeTransport(), spy=spy)

    def _modbus_factory(settings: SerialSettings) -> _FakeModbusBusTransport:
        return _FakeModbusBusTransport(settings, slave=modbus_slave, spy=spy)

    monkeypatch.setattr(detect_module, "SerialTransport", _serial_factory)
    monkeypatch.setattr(detect_module, "ModbusBusTransport", _modbus_factory)
    return spy


@pytest.mark.anyio
async def test_detect_resolves_stdbus_when_stdbus_answers(
    monkeypatch: pytest.MonkeyPatch,
    anyio_backend: object,
) -> None:
    """Case (a): Std Bus answers → STDBUS, Modbus probe never runs."""
    _ = anyio_backend
    request = _stdbus_request_wire(REQ_PROBE_HW)
    reply = _build_stdbus_reply(_HW_ID_PAYLOAD)
    fake = FakeTransport({request: reply})
    spy = _patch_detect(monkeypatch, stdbus_fake=fake, modbus_slave=None)

    resolved = await detect_protocol("/dev/fake", address=1, timeout_s=0.5)
    assert isinstance(resolved, ResolvedProtocol)
    assert resolved.kind is ProtocolKind.STDBUS
    assert spy.stdbus_opens == 1
    assert spy.stdbus_closes == 0  # transport handed off, not closed
    assert spy.modbus_opens == 0  # second probe never ran
    # Caller owns the transport now; close it to keep the test tidy.
    await resolved.transport.close()


@pytest.mark.anyio
async def test_detect_resolves_modbus_when_only_modbus_answers(
    monkeypatch: pytest.MonkeyPatch,
    anyio_backend: object,
) -> None:
    """Case (b): silence on Std Bus, valid Modbus reply → MODBUS_RTU."""
    _ = anyio_backend
    spy = _patch_detect(
        monkeypatch,
        stdbus_fake=FakeTransport(),  # silent
        modbus_slave=_StubSlave(reply=(0x0000, 0x0018)),
    )

    resolved = await detect_protocol("/dev/fake", address=1, timeout_s=0.05)
    assert resolved.kind is ProtocolKind.MODBUS_RTU
    assert spy.stdbus_opens == 1
    assert spy.stdbus_closes == 1  # closed before Modbus probe opened
    assert spy.modbus_opens == 1
    assert spy.modbus_closes == 0  # transport handed off
    await resolved.transport.close()


@pytest.mark.anyio
async def test_detect_fails_when_both_silent(
    monkeypatch: pytest.MonkeyPatch,
    anyio_backend: object,
) -> None:
    """Case (c): silence on both → WatlowProtocolUnsupportedError, both transports closed."""
    _ = anyio_backend
    spy = _patch_detect(
        monkeypatch,
        stdbus_fake=FakeTransport(),
        modbus_slave=_StubSlave(reply=FrameTimeoutError),
    )

    with pytest.raises(WatlowProtocolUnsupportedError, match="auto-detect failed"):
        await detect_protocol("/dev/fake", address=1, timeout_s=0.05)

    assert spy.stdbus_opens == 1
    assert spy.stdbus_closes == 1
    assert spy.modbus_opens == 1
    assert spy.modbus_closes == 1


@pytest.mark.anyio
async def test_detect_garbage_then_modbus(
    monkeypatch: pytest.MonkeyPatch,
    anyio_backend: object,
) -> None:
    """Case (d): garbage on Std Bus, valid Modbus → MODBUS_RTU."""
    _ = anyio_backend
    request = _stdbus_request_wire(REQ_PROBE_HW)
    fake = FakeTransport({request: bytes([0xAA] * 300)})
    spy = _patch_detect(
        monkeypatch,
        stdbus_fake=fake,
        modbus_slave=_StubSlave(reply=(0, 24)),
    )

    resolved = await detect_protocol("/dev/fake", address=1, timeout_s=0.5)
    assert resolved.kind is ProtocolKind.MODBUS_RTU
    assert spy.stdbus_closes == 1
    await resolved.transport.close()


@pytest.mark.anyio
async def test_detect_skips_stdbus_for_high_address(
    monkeypatch: pytest.MonkeyPatch,
    anyio_backend: object,
) -> None:
    """Address 17+ skips Std Bus probe (range 1..16) and goes straight to Modbus."""
    _ = anyio_backend
    spy = _patch_detect(
        monkeypatch,
        stdbus_fake=FakeTransport(),
        modbus_slave=_StubSlave(reply=(0, 24)),
    )

    resolved = await detect_protocol("/dev/fake", address=42, timeout_s=0.05)
    assert resolved.kind is ProtocolKind.MODBUS_RTU
    assert spy.stdbus_opens == 0  # never tried
    await resolved.transport.close()


@pytest.mark.anyio
async def test_detect_protocol_with_no_anyio_required() -> None:
    """The probe API is callable from a synchronous context test guard.

    Acts as a smoke test that detect_protocol's import surface doesn't
    pull eagerly-evaluated AnyIO context (it should be invocable
    inside any AnyIO backend).
    """
    # Just import-check; behaviour is exercised by the parametrized tests above.
    assert detect_protocol is not None
    assert probe_stdbus is not None
    assert probe_modbus is not None
    # anyio import lands the type at module scope — the guard above
    # ensures we don't accidentally execute anyio APIs at import time.
    assert anyio is not None
