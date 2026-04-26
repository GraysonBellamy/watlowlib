"""Address sweeps for ``watlow-discover``.

Two sweep entry points, mirrored on the two protocols:

- :func:`sweep_stdbus` walks Standard Bus addresses ``1..16``
  (BACnet MS/TP MAC ``0x10..0x1F``).
- :func:`sweep_modbus` walks a configurable Modbus slave-address
  range (defaults to ``1..16`` to match the common bench setup;
  callers can extend to ``1..247``).

Each address probe issues one bounded read of parameter ``1001``
(Hardware ID — the auto-detect probe target) and returns a
:class:`DiscoveryResult` row. Successful probes promote into a full
:meth:`Controller.identify` call so the row carries a populated
:class:`DeviceInfo`.

The sweep opens the underlying transport **once** and reuses it across
every address — Standard Bus addresses differ only in the dst-MAC byte
of the BACnet MS/TP outer frame, and the Modbus bus driver multiplexes
slaves over a single open serial handle. Reopening per address would
add ~0.5s of cdc_acm re-init per probe on Linux; the open-once design
lands a 16-address sweep in well under a second of wall-clock above
the actual wire turnaround.

Discovery is **opt-in** — it is never run from ``open_device``. The
``watlow-discover`` CLI surfaces this module to the command line.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import anyio

from watlowlib._logging import get_logger
from watlowlib.devices.controller import Controller
from watlowlib.devices.models import DiscoveryResult
from watlowlib.devices.session import Session
from watlowlib.errors import (
    ErrorContext,
    WatlowError,
    WatlowProtocolUnsupportedError,
    WatlowTimeoutError,
    WatlowTransportError,
)
from watlowlib.protocol.base import ProtocolKind
from watlowlib.protocol.client import make_protocol_client
from watlowlib.registry.families import ControllerFamily
from watlowlib.registry.parameters import PARAMETERS
from watlowlib.transport.base import SerialSettings
from watlowlib.transport.serial import SerialTransport

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

    from watlowlib.transport.base import Transport

__all__ = ["DEFAULT_MODBUS_RANGE", "DEFAULT_STDBUS_RANGE", "sweep_modbus", "sweep_stdbus"]


#: Std Bus controllers occupy MAC ``0x10..0x1F`` → addresses ``1..16``.
DEFAULT_STDBUS_RANGE: tuple[int, ...] = tuple(range(1, 17))

#: Default Modbus sweep — narrow on purpose. The full ``1..247`` is
#: spec-allowed, but a single-segment RS-485 bus rarely has more than
#: a handful of devices; sweeping 247 slots takes ~5 minutes at the
#: probe budget. Callers that need the full sweep pass
#: ``addresses=range(1, 248)``.
DEFAULT_MODBUS_RANGE: tuple[int, ...] = tuple(range(1, 17))

_log = get_logger("discovery")


# Per-probe budget — generous enough to cover RS-485 turn-around at
# 9600 baud, tight enough that a 16-address silent sweep takes seconds
# rather than minutes. Overridable per call.
_DEFAULT_PROBE_TIMEOUT_S = 0.25


async def sweep_stdbus(
    port: str,
    *,
    addresses: Iterable[int] = DEFAULT_STDBUS_RANGE,
    serial_settings: SerialSettings | None = None,
    timeout_s: float = _DEFAULT_PROBE_TIMEOUT_S,
) -> AsyncIterator[DiscoveryResult]:
    """Yield one :class:`DiscoveryResult` per Std Bus address probed.

    Opens the serial transport once, walks addresses sequentially
    against the same open handle, then closes. Silent rows carry a
    populated :attr:`DiscoveryResult.error` of type
    :class:`watlowlib.errors.WatlowTimeoutError` (or
    :class:`watlowlib.errors.WatlowTransportError` for framing issues)
    so callers can distinguish "device absent" from "address never
    tried".

    ``timeout_s`` bounds every underlying parameter read; the default
    keeps a 16-address silent sweep under five seconds.
    """
    settings = _resolve_settings(port, serial_settings)
    async for result in _sweep(
        port=port,
        protocol=ProtocolKind.STDBUS,
        addresses=addresses,
        serial_settings=settings,
        timeout_s=timeout_s,
    ):
        yield result


async def sweep_modbus(
    port: str,
    *,
    addresses: Iterable[int] = DEFAULT_MODBUS_RANGE,
    serial_settings: SerialSettings | None = None,
    timeout_s: float = _DEFAULT_PROBE_TIMEOUT_S,
) -> AsyncIterator[DiscoveryResult]:
    """Yield one :class:`DiscoveryResult` per Modbus address probed.

    Same shape as :func:`sweep_stdbus` — sequential, with the bus
    transport opened once and reused across every slave address. The
    Modbus driver multiplexes slaves over a single open handle so no
    per-address transport churn is incurred.
    """
    settings = _resolve_settings(port, serial_settings)
    async for result in _sweep(
        port=port,
        protocol=ProtocolKind.MODBUS_RTU,
        addresses=addresses,
        serial_settings=settings,
        timeout_s=timeout_s,
    ):
        yield result


# --- Internals ------------------------------------------------------


async def _sweep(
    *,
    port: str,
    protocol: ProtocolKind,
    addresses: Iterable[int],
    serial_settings: SerialSettings,
    timeout_s: float,
) -> AsyncIterator[DiscoveryResult]:
    """Open the transport once and walk every address against the same handle.

    Yields a :class:`DiscoveryResult` per address. Transport-level
    failures during open propagate as a single error row keyed to the
    first address (the rest of the range is skipped — the bus is gone)
    so callers see a typed result instead of an exception bubbling
    out of the async iterator.
    """
    transport = _build_transport(protocol, serial_settings)
    try:
        await transport.open()
    except WatlowError as exc:
        # If the port itself can't be opened, every address probe
        # would fail the same way; emit one row keyed to the lowest
        # address and stop.
        first = next(iter(addresses), 1)
        yield DiscoveryResult(
            port=port,
            serial_settings=serial_settings,
            address=first,
            protocol=protocol,
            info=None,
            error=exc,
        )
        return

    try:
        for address in addresses:
            yield await _probe_address(
                transport=transport,
                port=port,
                protocol=protocol,
                address=address,
                serial_settings=serial_settings,
                timeout_s=timeout_s,
            )
    finally:
        await transport.close()


async def _probe_address(
    *,
    transport: Transport,
    port: str,
    protocol: ProtocolKind,
    address: int,
    serial_settings: SerialSettings,
    timeout_s: float,
) -> DiscoveryResult:
    """Build a session against ``transport`` for ``address``, run identify, yield row.

    Caps the entire identify exchange at ``timeout_s`` via
    :func:`anyio.fail_after`. ``identify`` issues four sub-reads at
    the per-call timeout and a silent bus would otherwise blow the
    documented per-address budget by 4×; the outer cap keeps a 16-
    address silent sweep within the documented budget.

    The transport is *not* closed here — the caller owns it for the
    lifetime of the sweep. Each probe disposes its own session +
    client on exit so the next address starts with a clean dispatch
    state.
    """
    client = make_protocol_client(protocol, transport, address=address)
    session = Session(
        client,
        registry=PARAMETERS,
        family=ControllerFamily.UNKNOWN,
        address=address,
        port=transport.label,
    )
    controller = Controller(session, transport, serial_settings=serial_settings)

    info = None
    error: WatlowError | None = None
    try:
        # ``identify`` issues four bounded reads. Wrap the whole
        # exchange in a single ``timeout_s`` budget so a silent
        # address bails after one bus turn-around rather than four.
        # We accept any protocol-unsupported / transport error as
        # "no device at this address," not as a sweep failure. Real
        # connection errors propagate.
        try:
            with anyio.fail_after(timeout_s):
                info = await controller.identify(timeout=timeout_s)
        except TimeoutError as exc:
            error = WatlowTimeoutError(
                f"identify on {protocol.value} address {address} "
                f"exceeded {timeout_s:.3f}s probe budget",
                context=ErrorContext(
                    port=port,
                    protocol=protocol,
                    address=address,
                ),
            )
            error.__cause__ = exc
        except (WatlowProtocolUnsupportedError, WatlowTransportError) as exc:
            error = exc
    finally:
        # Dispose only the per-address client; the shared transport
        # stays open for the next iteration.
        try:
            session.dispose()
        except WatlowError:
            # Disposal errors aren't load-bearing — log and continue.
            _log.debug("discovery: dispose failed for addr=%d", address, exc_info=True)

    # ``identify`` is tolerant by design: a silent device still yields
    # a :class:`DeviceInfo` with empty / ``None`` fields. Treat that
    # as "no device here" and demote to ``info=None`` so callers can
    # filter responsive vs absent rows on a single attribute. Surface
    # the absence as a typed timeout so silent rows have a structured
    # error rather than ``error=null``.
    if info is not None and not info.part_number.raw and info.hardware_id is None:
        info = None
        if error is None:
            error = WatlowTimeoutError(
                f"no device responded at {protocol.value} address {address}",
                context=ErrorContext(
                    port=port,
                    protocol=protocol,
                    address=address,
                ),
            )

    return DiscoveryResult(
        port=port,
        serial_settings=serial_settings,
        address=address,
        protocol=protocol,
        info=info,
        error=error,
    )


def _build_transport(
    protocol: ProtocolKind,
    serial_settings: SerialSettings,
) -> Transport:
    """Build a transport of the right shape for ``protocol``."""
    if protocol is ProtocolKind.MODBUS_RTU:
        # Lazy import — keep the Std-Bus path off the anymodbus dep
        # graph for users who never reach for Modbus.
        from watlowlib.protocol.modbus.transport import (  # noqa: PLC0415
            ModbusBusTransport,
        )

        return ModbusBusTransport(serial_settings)
    return SerialTransport(serial_settings)


def _resolve_settings(
    port: str,
    serial_settings: SerialSettings | None,
) -> SerialSettings:
    """Apply ``port`` to ``serial_settings`` (or build a default)."""
    if serial_settings is None:
        return SerialSettings(port=port)
    if serial_settings.port == port:
        return serial_settings
    from dataclasses import replace  # noqa: PLC0415 — cold path

    return replace(serial_settings, port=port)
