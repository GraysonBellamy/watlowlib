"""In-process fake transport for tests and fixture replay.

:class:`FakeTransport` implements the :class:`Transport` Protocol
without touching a serial port. Tests script the expected
write→response mapping; unscripted writes are recorded but produce no
reply, which surfaces as a real timeout on the next read (the intended
failure mode — tests notice when they forgot to script a command).

The transport is fixture-replay grade:

- The dict-based ``script`` matches by exact bytes.
- An optional ordered ``queue`` of ``(write, reply)`` pairs is consumed
  FIFO and is the right shape for capture-replay scenarios where the
  same request may legitimately appear more than once with a different
  reply (a recorder reading PV in a tight loop, say).
- :attr:`unmatched_writes` exposes the subset of :attr:`writes` that
  hit neither a dict entry nor the next queue entry — useful for
  tests that want to assert "no surprise traffic hit the wire".
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence

import anyio

from watlowlib.errors import (
    ErrorContext,
    WatlowConnectionError,
    WatlowTimeoutError,
)

__all__ = ["FakeSlave", "FakeTransport", "ScriptedReply", "ScriptedSlaveEntry"]

#: One entry in a :class:`FakeSlave` script.
#:
#: A read entry is a tuple of register words; a write entry is ``None``
#: (the slave just records the call); either kind can be replaced by
#: an ``anymodbus`` exception class to model an error response.
type ScriptedSlaveEntry = tuple[int, ...] | type[BaseException] | None

#: A scripted reply. Bytes are emitted verbatim; sequences are
#: concatenated in order; callables receive the exact write payload and
#: return bytes (or a sequence of bytes) — useful when the response
#: depends on the request.
type ScriptedReply = bytes | Sequence[bytes] | Callable[[bytes], bytes | Sequence[bytes]]


def _normalize_reply(reply: bytes | Sequence[bytes]) -> bytes:
    if isinstance(reply, bytes):
        return reply
    return b"".join(reply)


def _instantiate_modbus_exception(cls: type[BaseException]) -> BaseException:
    """Build an :mod:`anymodbus` exception, handling exception-response classes.

    ``ModbusExceptionResponse`` subclasses (``IllegalFunctionError``,
    ``IllegalDataAddressError``, ...) require ``function_code`` as a
    keyword-only arg. Plain ones (``FrameTimeoutError``, ``CRCError``)
    accept a positional message.
    """
    # Imported lazily so importing :mod:`watlowlib.transport.fake`
    # doesn't pull anymodbus on Std-Bus-only setups.
    from anymodbus import ModbusExceptionResponse  # noqa: PLC0415

    if issubclass(cls, ModbusExceptionResponse):
        return cls(function_code=3)
    return cls("scripted")


class FakeSlave:
    """Scripted :class:`anymodbus.Slave` stand-in for tests.

    Mirrors the surface :class:`watlowlib.protocol.modbus.client.ModbusProtocolClient`
    actually calls (``read_holding_registers``,
    ``read_input_registers``, ``write_register``, ``write_registers``)
    and records every call. Tests assert on :attr:`reads` and
    :attr:`writes` to verify the :class:`ModbusOp` lowered correctly.

    Args:
        script: ``(method, address) → reply`` map. ``method`` is one
            of the four call names above. The reply is a ``tuple``
            of register words, an ``anymodbus`` exception class
            (raised at call time, with the right constructor args),
            or ``None`` (treat the call as a no-op success). Missing
            entries surface a :class:`KeyError` so an unscripted call
            fails the test rather than returning empty results.
    """

    def __init__(self, script: Mapping[tuple[str, int], ScriptedSlaveEntry] | None = None) -> None:
        self._script: dict[tuple[str, int], ScriptedSlaveEntry] = dict(script or {})
        self.reads: list[tuple[str, int, int]] = []
        self.writes: list[tuple[str, int, tuple[int, ...]]] = []

    async def read_holding_registers(self, address: int, *, count: int) -> tuple[int, ...]:
        self.reads.append(("read_holding_registers", address, count))
        return self._dispatch_read("read_holding_registers", address, count=count)

    async def read_input_registers(self, address: int, *, count: int) -> tuple[int, ...]:
        self.reads.append(("read_input_registers", address, count))
        return self._dispatch_read("read_input_registers", address, count=count)

    async def write_register(self, address: int, value: int) -> None:
        self.writes.append(("write_register", address, (value,)))
        self._dispatch_write("write_register", address)

    async def write_registers(self, address: int, values: Sequence[int]) -> None:
        self.writes.append(("write_registers", address, tuple(values)))
        self._dispatch_write("write_registers", address)

    def _dispatch_read(self, method: str, address: int, *, count: int) -> tuple[int, ...]:
        key = (method, address)
        if key not in self._script:
            msg = f"FakeSlave: no scripted reply for {key} (count={count})"
            raise KeyError(msg)
        entry = self._script[key]
        if isinstance(entry, type):
            raise _instantiate_modbus_exception(entry)
        if entry is None:
            return ()
        result = tuple(entry)
        if len(result) != count:
            msg = (
                f"FakeSlave: scripted reply length {len(result)} does not match "
                f"requested count={count} for {key}"
            )
            raise AssertionError(msg)
        return result

    def _dispatch_write(self, method: str, address: int) -> None:
        key = (method, address)
        if key not in self._script:
            return  # writes default to a silent success when unscripted
        entry = self._script[key]
        if isinstance(entry, type):
            raise _instantiate_modbus_exception(entry)
        # Tuple/None entries on a write key mean "succeed silently"; the
        # call has already been recorded above.

    def add_script(
        self,
        method: str,
        address: int,
        reply: ScriptedSlaveEntry,
    ) -> None:
        """Register or overwrite a scripted reply for ``(method, address)``."""
        self._script[(method, address)] = reply


class FakeTransport:
    """Scripted :class:`Transport` for tests.

    Args:
        script: Mapping of ``write_bytes → reply``. Every scripted
            write queues the corresponding reply into the read buffer.
            Unknown writes are recorded but produce no reply; the next
            read times out.
        label: Identifier used in errors.
        latency_s: Per-operation artificial delay, useful for
            simulating a slow device.
    """

    def __init__(
        self,
        script: Mapping[bytes, ScriptedReply] | None = None,
        *,
        queue: Iterable[tuple[bytes, ScriptedReply]] | None = None,
        label: str = "fake://test",
        latency_s: float = 0.0,
    ) -> None:
        self._script: dict[bytes, ScriptedReply] = dict(script or {})
        self._queue: deque[tuple[bytes, ScriptedReply]] = deque(queue or [])
        self._writes: list[bytes] = []
        self._unmatched: list[bytes] = []
        self._read_buffer = bytearray()
        self._is_open = False
        self._label = label
        self._latency_s = latency_s
        self._force_read_timeout = False
        self._force_write_timeout = False

    async def open(self) -> None:
        if self._is_open:
            raise WatlowConnectionError(
                f"{self._label} is already open",
                context=ErrorContext(port=self._label),
            )
        self._is_open = True

    async def close(self) -> None:
        self._is_open = False

    async def write(self, data: bytes, *, timeout: float) -> None:
        self._ensure_open()
        if self._force_write_timeout:
            raise WatlowTimeoutError(
                f"write on {self._label} timed out after {timeout}s (forced)",
                context=ErrorContext(port=self._label),
            )
        if self._latency_s:
            await anyio.sleep(self._latency_s)
        payload = bytes(data)
        self._writes.append(payload)
        reply = self._resolve_reply(payload)
        if reply is None:
            self._unmatched.append(payload)
            return
        if callable(reply):
            produced = reply(payload)
            self._read_buffer.extend(_normalize_reply(produced))
        else:
            self._read_buffer.extend(_normalize_reply(reply))

    def _resolve_reply(self, payload: bytes) -> ScriptedReply | None:
        """Pick a scripted reply for ``payload``.

        Lookup order: dict first (exact match wins regardless of
        position), then the FIFO queue (next entry's request must match
        ``payload`` byte-for-byte; a mismatch leaves the queue alone so
        the test sees the misalignment as ``unmatched_writes``).
        """
        if payload in self._script:
            return self._script[payload]
        if self._queue and self._queue[0][0] == payload:
            return self._queue.popleft()[1]
        return None

    async def read_exact(self, n: int, *, timeout: float) -> bytes:
        self._ensure_open()
        if self._force_read_timeout:
            raise WatlowTimeoutError(
                f"read_exact({n}) on {self._label} timed out after {timeout}s (forced)",
                context=ErrorContext(port=self._label),
            )
        if self._latency_s:
            await anyio.sleep(self._latency_s)
        if len(self._read_buffer) < n:
            raise WatlowTimeoutError(
                f"read_exact({n}) on {self._label} timed out after {timeout}s",
                context=ErrorContext(port=self._label),
            )
        result = bytes(self._read_buffer[:n])
        del self._read_buffer[:n]
        return result

    async def read_available(
        self,
        *,
        idle_timeout: float,
        max_bytes: int | None = None,
    ) -> bytes:
        self._ensure_open()
        if self._latency_s:
            await anyio.sleep(self._latency_s)
        # ``idle_timeout`` is ignored — there is no real "idle" on a
        # fake transport; we hand back whatever is in the buffer
        # immediately. Tests that want to model an idle wait can set
        # ``latency_s`` instead.
        _ = idle_timeout
        if max_bytes is None or max_bytes >= len(self._read_buffer):
            result = bytes(self._read_buffer)
            self._read_buffer.clear()
        else:
            result = bytes(self._read_buffer[:max_bytes])
            del self._read_buffer[:max_bytes]
        return result

    async def drain_input(self) -> None:
        self._read_buffer.clear()

    @property
    def is_open(self) -> bool:
        return self._is_open

    @property
    def label(self) -> str:
        return self._label

    @property
    def writes(self) -> tuple[bytes, ...]:
        """Every write payload recorded since construction, in order."""
        return tuple(self._writes)

    @property
    def unmatched_writes(self) -> tuple[bytes, ...]:
        """Writes that didn't match any scripted reply, in order.

        A test can assert ``transport.unmatched_writes == ()`` to catch
        accidentally-unscripted traffic — the corresponding read would
        have timed out, but a precise assertion fails faster and points
        at the right call.
        """
        return tuple(self._unmatched)

    @property
    def remaining_queue(self) -> tuple[tuple[bytes, ScriptedReply], ...]:
        """Queue entries that have not been consumed yet."""
        return tuple(self._queue)

    def feed(self, data: bytes) -> None:
        """Push unsolicited bytes into the read buffer.

        Useful for simulating a device that left chatter on the line
        which the protocol client has to drain on recovery.
        """
        self._read_buffer.extend(data)

    def add_script(self, command: bytes, reply: ScriptedReply) -> None:
        """Register or overwrite a scripted reply for ``command``."""
        self._script[bytes(command)] = reply

    def extend_queue(self, rounds: Iterable[tuple[bytes, ScriptedReply]]) -> None:
        """Append more ordered ``(write, reply)`` pairs to the FIFO queue."""
        self._queue.extend(rounds)

    def force_read_timeout(self, enabled: bool = True) -> None:
        """Force the next read to raise :class:`WatlowTimeoutError`."""
        self._force_read_timeout = enabled

    def force_write_timeout(self, enabled: bool = True) -> None:
        """Force the next :meth:`write` to raise :class:`WatlowTimeoutError`."""
        self._force_write_timeout = enabled

    def _ensure_open(self) -> None:
        if not self._is_open:
            raise WatlowConnectionError(
                f"{self._label} is not open",
                context=ErrorContext(port=self._label),
            )
