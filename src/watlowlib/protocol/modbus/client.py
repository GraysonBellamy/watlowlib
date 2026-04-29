"""Modbus protocol client.

:class:`ModbusProtocolClient` implements the
:class:`watlowlib.protocol.base.ProtocolClient` Protocol over
:mod:`anymodbus`. The client takes a *slave provider* — a small
callable returning a live :class:`anymodbus.Slave` — so tests can
hand in a stub without spinning up a real bus.

The client is the only place that touches a :class:`Slave`. It picks
the matching method from :attr:`ModbusOp.fn`, awaits it under the
per-port :class:`anyio.Lock`, and remaps every :mod:`anymodbus`
exception into a typed :class:`WatlowError` (see
:mod:`watlowlib.protocol.modbus.errors`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

import anyio

from watlowlib._logging import get_logger
from watlowlib.config import DEFAULTS
from watlowlib.errors import (
    ErrorContext,
    WatlowConnectionError,
    WatlowError,
)
from watlowlib.protocol.base import ProtocolKind
from watlowlib.protocol.modbus.errors import remap_modbus_exception
from watlowlib.protocol.modbus.ops import ModbusFn, ModbusOp

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

__all__ = ["ModbusProtocolClient", "SlaveLike", "SlaveProvider"]


class SlaveLike(Protocol):
    """Structural shape of an :class:`anymodbus.Slave` used by this client.

    The :class:`ModbusProtocolClient` only invokes the four
    register-level methods listed below. Declaring a structural
    Protocol — rather than tying the client to a concrete
    :class:`anymodbus.Slave` — keeps test stubs (``FakeSlave``,
    per-test ``StubSlave``) type-compatible without subclassing
    :mod:`anymodbus`. Production callers still pass an
    :class:`anymodbus.Slave`; the structural type is wider only.
    """

    async def read_holding_registers(  # noqa: D102 — Slave docs the contract
        self, address: int, *, count: int
    ) -> tuple[int, ...]: ...

    async def read_input_registers(  # noqa: D102
        self, address: int, *, count: int
    ) -> tuple[int, ...]: ...

    async def write_register(self, address: int, value: int) -> None: ...  # noqa: D102

    async def write_registers(  # noqa: D102
        self, address: int, values: Sequence[int]
    ) -> None: ...


#: Callable that produces a live :class:`anymodbus.Slave` (or any
#: structurally-compatible stand-in — see :class:`SlaveLike`) for a
#: given bus address. The provider is invoked on every
#: :meth:`ModbusProtocolClient.execute` with that call's address, so
#: one client can serve every slave on a multi-drop bus. Lifetime of
#: the underlying :class:`anymodbus.Bus` is owned by the caller
#: (typically the :class:`ModbusBusTransport`), not the client.
type SlaveProvider = Callable[[int], SlaveLike]

_log = get_logger("modbus")


class ModbusProtocolClient:
    """:class:`ProtocolClient` for Modbus RTU.

    The client is **address-agnostic**: ``execute`` takes the slave
    address per-call so one client can serve every device on a
    multi-drop bus. The ``slave_provider`` receives that address and
    returns the live :class:`Slave`.

    Args:
        slave_provider: Callable mapping address → live :class:`Slave`.
            In production the provider closes over the
            :class:`ModbusBusTransport` and returns
            ``transport.bus.slave(address)``; in tests it returns a
            stub.
        port: Transport label, threaded into log events / error
            contexts.
    """

    def __init__(
        self,
        slave_provider: SlaveProvider,
        *,
        port: str = "",
    ) -> None:
        self._slave_provider = slave_provider
        self._port = port
        self._lock = anyio.Lock()
        self._disposed = False

    @property
    def lock(self) -> anyio.Lock:
        """Per-client lock acquired by :meth:`Session.execute`."""
        return self._lock

    @property
    def disposed(self) -> bool:
        """Whether :meth:`dispose` has been called."""
        return self._disposed

    @property
    def kind(self) -> ProtocolKind:
        """Wire protocol kind served by this client."""
        return ProtocolKind.MODBUS_RTU

    def dispose(self) -> None:
        """Mark this client unusable for future ``execute`` calls."""
        self._disposed = True

    async def execute(
        self,
        request: ModbusOp,
        *,
        address: int,
        timeout: float | None = None,
        command_name: str = "",
    ) -> tuple[int, ...]:
        """Run ``request`` against ``address`` and return the raw register tuple.

        Reads return the read words; writes return ``()`` so callers
        downstream can treat reads and writes uniformly.

        Args:
            request: The typed Modbus operation produced by a
                :class:`ModbusVariant`.
            address: Modbus slave address (``1..247``). Validated
                eagerly before any I/O.
            timeout: Per-call override of
                :attr:`watlowlib.config.DEFAULTS.io_timeout_s`. Bound
                via :func:`anyio.fail_after` around the dispatch so
                hung devices cannot stall the session lock
                indefinitely.
            command_name: Threaded into log events for traceability.

        Raises:
            WatlowConfigurationError: ``address`` is outside ``1..247``.
            WatlowConnectionError: client is disposed or the underlying
                :class:`anymodbus.Bus` has been closed.
            WatlowModbusError: a Modbus-layer exception (mapped via
                :func:`remap_modbus_exception`).
        """
        if address < 1 or address > 247:
            from watlowlib.errors import WatlowConfigurationError  # noqa: PLC0415

            msg = f"Modbus address {address} out of range (1..247)"
            raise WatlowConfigurationError(
                msg,
                context=self._error_context(command_name, request, address=address),
            )
        if self._disposed:
            raise WatlowConnectionError(
                "ModbusProtocolClient is disposed",
                context=self._error_context(command_name, request, address=address),
            )

        bound = timeout if timeout is not None else DEFAULTS.io_timeout_s

        try:
            slave = self._slave_provider(address)
        except WatlowError:
            # The provider itself surfaced a typed error (e.g. transport
            # not open). Let it propagate untouched.
            raise
        except Exception as exc:
            raise remap_modbus_exception(
                exc,
                context=self._error_context(command_name, request, address=address),
            ) from exc

        try:
            with anyio.fail_after(bound):
                result = await self._dispatch(slave, request)
        except TimeoutError as exc:
            from watlowlib.errors import WatlowTimeoutError  # noqa: PLC0415

            raise WatlowTimeoutError(
                f"Modbus {request.fn.value} on addr={address} timed out after {bound}s",
                context=self._error_context(command_name, request, address=address),
            ) from exc
        except WatlowError:
            raise
        except Exception as exc:
            raise remap_modbus_exception(
                exc,
                context=self._error_context(command_name, request, address=address),
            ) from exc

        _log.debug(
            "modbus exec ok cmd=%s addr=%d fn=%s reg=%d count=%d",
            command_name or "<anon>",
            address,
            request.fn.value,
            request.address,
            request.count,
        )
        return result

    async def _dispatch(self, slave: SlaveLike, op: ModbusOp) -> tuple[int, ...]:
        """Lower a :class:`ModbusOp` onto a :class:`Slave` method.

        Reads return the words verbatim. Writes return ``()`` because
        ``anymodbus.Slave.write_register`` / ``write_registers`` return
        ``None`` on success — the variant is expected to interpret a
        successful return as "the device accepted the value".
        """
        if op.fn is ModbusFn.READ_HOLDING:
            return await slave.read_holding_registers(op.address, count=op.count)
        if op.fn is ModbusFn.READ_INPUT:
            return await slave.read_input_registers(op.address, count=op.count)
        if op.fn is ModbusFn.WRITE_REGISTER:
            assert op.values is not None  # noqa: S101 — ModbusOp.__post_init__ guarantees
            await slave.write_register(op.address, op.values[0])
            return ()
        # ModbusFn is closed; the only remaining case is WRITE_REGISTERS.
        assert op.fn is ModbusFn.WRITE_REGISTERS  # noqa: S101
        assert op.values is not None  # noqa: S101
        await slave.write_registers(op.address, op.values)
        return ()

    def _error_context(
        self,
        command_name: str,
        op: ModbusOp,
        *,
        address: int,
    ) -> ErrorContext:
        # Map ModbusFn to its anymodbus FunctionCode integer for richer
        # error introspection. Done here (not in ModbusOp) so the op
        # stays a pure description and doesn't pull anymodbus.
        from anymodbus import FunctionCode  # noqa: PLC0415

        fn_code: int | None = None
        match op.fn:
            case ModbusFn.READ_HOLDING:
                fn_code = int(FunctionCode.READ_HOLDING_REGISTERS)
            case ModbusFn.READ_INPUT:
                fn_code = int(FunctionCode.READ_INPUT_REGISTERS)
            case ModbusFn.WRITE_REGISTER:
                fn_code = int(FunctionCode.WRITE_SINGLE_REGISTER)
            case ModbusFn.WRITE_REGISTERS:
                fn_code = int(FunctionCode.WRITE_MULTIPLE_REGISTERS)
        return ErrorContext(
            command_name=command_name or None,
            protocol=ProtocolKind.MODBUS_RTU,
            port=self._port or None,
            address=address,
            register_address=op.address,
            function_code=fn_code,
        )
