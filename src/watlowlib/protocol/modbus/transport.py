"""Transport-shaped adapter over an :class:`anymodbus.Bus`.

Modbus is the asymmetric protocol: :mod:`anymodbus` already owns the
serial handle, so the byte-level :class:`Transport` methods
(``write`` / ``read_exact`` / ``read_available``) are no-ops or
raise. The :class:`ModbusProtocolClient` reaches through this adapter
for the live :class:`anymodbus.Bus` and uses Modbus methods directly.

Why an adapter at all? :class:`Controller` is built around a single
:class:`Transport` lifecycle (``__aenter__`` opens, ``__aexit__``
closes). Wrapping the Bus in a Transport-shaped object keeps the
facade unchanged across protocols — opening the Modbus controller is
the same call as opening the Std Bus controller.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from watlowlib.errors import (
    ErrorContext,
    WatlowConfigurationError,
    WatlowConnectionError,
)

if TYPE_CHECKING:
    from anymodbus import Bus

    from watlowlib.transport.base import SerialSettings

__all__ = ["ModbusBusTransport"]


# anymodbus accepts these parity literals — they happen to match
# anyserial.Parity values 1:1 except for the case.
_ALLOWED_PARITY = ("none", "even", "odd", "mark", "space")


class ModbusBusTransport:
    """Holds an :class:`anymodbus.Bus` behind the :class:`Transport` API.

    Lifecycle:

    - ``open()`` calls :func:`anymodbus.open_modbus_rtu` and stores the
      resulting :class:`Bus`. Re-open raises :class:`WatlowConnectionError`.
    - ``close()`` awaits :meth:`Bus.aclose`. Safe on an unopened or
      already-closed instance.
    - ``write`` / ``read_exact`` / ``read_available`` raise
      :class:`NotImplementedError`. The :class:`ModbusProtocolClient`
      never calls them — it uses :attr:`bus` instead.
    """

    def __init__(self, settings: SerialSettings) -> None:
        # Validate the parity up front. anymodbus only accepts a small
        # literal set; better to fail at construction than deep inside
        # ``open_modbus_rtu``.
        parity = str(settings.parity.value).lower()
        if parity not in _ALLOWED_PARITY:
            msg = (
                f"unsupported parity for Modbus RTU: {parity!r}; expected one of {_ALLOWED_PARITY}"
            )
            raise WatlowConfigurationError(msg, context=ErrorContext(port=settings.port))
        self._settings = settings
        self._parity = parity
        self._bus: Bus | None = None

    @property
    def is_open(self) -> bool:
        return self._bus is not None

    @property
    def label(self) -> str:
        return self._settings.port

    @property
    def bus(self) -> Bus:
        """Return the live :class:`anymodbus.Bus`.

        Raises :class:`WatlowConnectionError` if :meth:`open` has not
        completed (or the transport has been closed).
        """
        if self._bus is None:
            raise WatlowConnectionError(
                f"ModbusBusTransport for {self._settings.port!r} is not open",
                context=ErrorContext(port=self._settings.port),
            )
        return self._bus

    async def open(self) -> None:
        if self._bus is not None:
            raise WatlowConnectionError(
                f"{self._settings.port!r} is already open",
                context=ErrorContext(port=self._settings.port),
            )
        # Imported lazily so importing :mod:`watlowlib.transport` from
        # a Std-Bus-only program does not pay the anymodbus + anyserial
        # cost up front.
        from anymodbus import open_modbus_rtu  # noqa: PLC0415

        self._bus = await open_modbus_rtu(
            self._settings.port,
            baudrate=self._settings.baudrate,
            parity=self._parity,  # type: ignore[arg-type]  # validated above
        )

    async def close(self) -> None:
        bus = self._bus
        if bus is None:
            return
        self._bus = None
        await bus.aclose()

    async def write(self, data: bytes, *, timeout: float) -> None:
        _ = data, timeout
        msg = "ModbusBusTransport does not support raw write — use ModbusProtocolClient"
        raise NotImplementedError(msg)

    async def read_exact(self, n: int, *, timeout: float) -> bytes:
        _ = n, timeout
        msg = "ModbusBusTransport does not support raw read — use ModbusProtocolClient"
        raise NotImplementedError(msg)

    async def read_available(
        self,
        *,
        idle_timeout: float,
        max_bytes: int | None = None,
    ) -> bytes:
        _ = idle_timeout, max_bytes
        return b""

    async def drain_input(self) -> None:
        # anymodbus drains the serial input buffer before each request
        # by default (``BusConfig.reset_input_buffer_before_request``).
        return None
