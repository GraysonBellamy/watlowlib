"""Port-scan discovery for ``watlow-discover`` and ``capa``-style adapters.

:func:`find_devices` is the single discovery entry point. It walks the
cartesian product of ``ports × baudrates × protocols × addresses`` and
returns one :class:`FindResult` per probe attempt — mirroring the
``alicatlib.find_devices`` / ``sartoriuslib.discover_port`` ecosystem
shape so a GUI Discover dialog can filter on a single ``ok`` flag.

The scan is **read-only**: every probe is a bounded
:meth:`Controller.identify` call. No setpoint writes, no parameter
writes, no comms-unit-label probes — discovery routinely runs on rigs
that already have other software talking to the same controller.

Per-(port, baudrate, protocol) combination, the transport is opened
once and every address is probed against the same handle. Standard
Bus addresses live in the BACnet MS/TP outer-frame ``dst`` MAC byte;
the Modbus bus driver multiplexes slaves over a single open serial
handle. Reopening per address would add ~0.5 s of cdc_acm re-init per
probe on Linux for no benefit.

If a port fails to open at all, it is marked dead for the rest of the
scan: every subsequent (baudrate × protocol) combination for that
port short-circuits with a single :class:`FindResult` per planned
address carrying the open error. This avoids hammering a port that
``anyserial`` listed but the kernel won't let us touch.

The default scan is narrow on purpose:

- ``addresses`` defaults to ``(1,)`` — Modbus RTU allows 1..247, but
  a multi-port × multi-baud × multi-protocol scan with a 16-address
  default explodes the probe count and the wall-clock for a GUI scan
  past what operators tolerate. Callers that need a full address
  sweep pass ``addresses=range(1, 248)`` explicitly.
- ``baudrates`` defaults to ``(38400, 19200, 9600)`` — the EZ-ZONE PM
  ships at 38400 on Std Bus and 9600 on Modbus RTU; 19200 covers
  rigs that have been re-configured to the middle baud.
- ``protocols`` defaults to ``(STDBUS, MODBUS_RTU)`` — both Watlow
  wire protocols on serial. ``AUTO`` is not in the default set; it
  would double the open count per (port, baudrate) and the detector
  is intentionally a single-port API.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

import anyio

from watlowlib._logging import get_logger
from watlowlib.devices.controller import Controller
from watlowlib.devices.models import FindResult
from watlowlib.devices.session import Session
from watlowlib.errors import (
    ErrorContext,
    WatlowConnectionError,
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
    from collections.abc import Sequence

    from watlowlib.transport.base import Transport

__all__ = [
    "DEFAULT_DISCOVERY_ADDRESSES",
    "DEFAULT_DISCOVERY_BAUDRATES",
    "DEFAULT_DISCOVERY_PROTOCOLS",
    "find_devices",
]


#: Default address set probed per (port, baudrate, protocol) combo.
#: Narrow on purpose — see module docstring.
DEFAULT_DISCOVERY_ADDRESSES: tuple[int, ...] = (1,)

#: Default baudrate set. Covers the EZ-ZONE PM Std Bus factory (38400),
#: the Modbus RTU factory (9600), and the typical 19200 middle ground.
DEFAULT_DISCOVERY_BAUDRATES: tuple[int, ...] = (38400, 19200, 9600)

#: Default protocol set — both Watlow serial protocols.
DEFAULT_DISCOVERY_PROTOCOLS: tuple[ProtocolKind, ...] = (
    ProtocolKind.STDBUS,
    ProtocolKind.MODBUS_RTU,
)

#: Per-probe budget. Tight enough that a wrong-baud / wrong-protocol
#: combo bails after one bus turn-around; generous enough to cover a
#: 9600 baud RS-485 turn-around plus the four sub-reads
#: ``identify()`` issues.
_DEFAULT_PROBE_TIMEOUT_S: float = 0.5

_log = get_logger("discovery")


async def find_devices(
    *,
    ports: Sequence[str] | None = None,
    addresses: Sequence[int] | None = None,
    baudrates: Sequence[int] | None = None,
    protocols: Sequence[ProtocolKind] | None = None,
    serial_template: SerialSettings | None = None,
    per_probe_timeout_s: float = _DEFAULT_PROBE_TIMEOUT_S,
) -> list[FindResult]:
    """Probe local serial ports for Watlow controllers.

    Args:
        ports: Serial-port paths to scan. ``None`` enumerates every
            visible port via :func:`anyserial.list_serial_ports`. An
            empty sequence returns ``[]`` without enumeration.
        addresses: Bus addresses to probe per (port, baudrate, protocol)
            combination. Defaults to :data:`DEFAULT_DISCOVERY_ADDRESSES`
            (``(1,)``). Std Bus accepts ``1..16``; Modbus RTU accepts
            ``1..247``. Out-of-range addresses for a given protocol are
            still emitted as ``ok=False`` rows carrying a
            :class:`WatlowConfigurationError`.
        baudrates: Baud rates to try. Defaults to
            :data:`DEFAULT_DISCOVERY_BAUDRATES`.
        protocols: Wire protocols to probe. Defaults to
            :data:`DEFAULT_DISCOVERY_PROTOCOLS`. ``ProtocolKind.AUTO``
            is not accepted here (one row per concrete probe).
        serial_template: Optional :class:`SerialSettings` whose
            ``parity`` / ``bytesize`` / ``stopbits`` / ``rtscts`` /
            ``xonxoff`` / ``exclusive`` fields override the
            per-protocol factory framing for every probe. ``port``
            and ``baudrate`` are always overwritten per iteration.
        per_probe_timeout_s: Per-probe budget. Bounds the
            :meth:`Controller.identify` call (four bounded sub-reads)
            so a silent address bails after one round-trip rather than
            four. Defaults to ``0.5`` — a four-port × three-baud ×
            two-protocol scan with one address per combo lands in
            ~12 s of wall-clock.

    Returns:
        One :class:`FindResult` per (port × baudrate × protocol ×
        address) tuple, in input order. The cartesian product is
        iterated outermost-port, then baudrate, then protocol, then
        address — same input → same output ordering.

    Raises:
        WatlowConfigurationError: ``protocols`` contains
            :attr:`ProtocolKind.AUTO`, or ``per_probe_timeout_s`` is
            non-positive.

    Notes:
        - **Read-only.** Discovery never writes to the device; it
          only calls :meth:`Controller.identify` (four parameter
          reads). Safe to run on rigs that already have other
          software talking to the controller.
        - **Per-port short-circuit.** If a port fails to open with a
          :class:`WatlowConnectionError`, the rest of the scan for
          that port emits ``ok=False`` rows without re-attempting the
          open. This avoids hammering a port the kernel won't give us.
    """
    if per_probe_timeout_s <= 0:
        from watlowlib.errors import WatlowConfigurationError  # noqa: PLC0415 — cold path

        raise WatlowConfigurationError(
            f"per_probe_timeout_s must be positive; got {per_probe_timeout_s!r}",
        )

    resolved_ports = await _resolve_ports(ports)
    resolved_addresses = tuple(addresses) if addresses is not None else DEFAULT_DISCOVERY_ADDRESSES
    resolved_baudrates = tuple(baudrates) if baudrates is not None else DEFAULT_DISCOVERY_BAUDRATES
    resolved_protocols = tuple(protocols) if protocols is not None else DEFAULT_DISCOVERY_PROTOCOLS

    if ProtocolKind.AUTO in resolved_protocols:
        from watlowlib.errors import WatlowConfigurationError  # noqa: PLC0415 — cold path

        raise WatlowConfigurationError(
            "find_devices does not accept ProtocolKind.AUTO; pass concrete "
            "protocols (STDBUS, MODBUS_RTU). Auto-detection is a single-port "
            "API on open_device.",
        )

    results: list[FindResult] = []
    dead_ports: set[str] = set()
    for port in resolved_ports:
        for baud in resolved_baudrates:
            for protocol in resolved_protocols:
                if port in dead_ports:
                    # Emit one row per planned address so callers see
                    # the same cartesian-product shape regardless of
                    # whether the port opened.
                    error = WatlowConnectionError(
                        f"port {port!r} previously failed to open in this scan",
                        context=ErrorContext(
                            port=port,
                            protocol=protocol,
                        ),
                    )
                    results.extend(
                        FindResult(
                            port=port,
                            address=address,
                            baudrate=baud,
                            protocol=protocol,
                            ok=False,
                            info=None,
                            error=error,
                        )
                        for address in resolved_addresses
                    )
                    continue

                settings = _build_settings(
                    port=port,
                    baudrate=baud,
                    protocol=protocol,
                    template=serial_template,
                )
                rows, port_died = await _probe_combo(
                    port=port,
                    baudrate=baud,
                    protocol=protocol,
                    addresses=resolved_addresses,
                    serial_settings=settings,
                    timeout_s=per_probe_timeout_s,
                )
                results.extend(rows)
                if port_died:
                    dead_ports.add(port)
    return results


# --- Internals ------------------------------------------------------


async def _resolve_ports(ports: Sequence[str] | None) -> tuple[str, ...]:
    """Enumerate ports via ``anyserial`` when ``ports`` is ``None``."""
    if ports is None:
        # Lazy import — keep ``anyserial.list_serial_ports`` off the
        # cold-start path for callers that always pass explicit ports
        # (the common case for capa once a rig is configured).
        from anyserial import list_serial_ports  # noqa: PLC0415

        infos = await list_serial_ports()
        return tuple(info.device for info in infos)
    return tuple(ports)


def _build_settings(
    *,
    port: str,
    baudrate: int,
    protocol: ProtocolKind,
    template: SerialSettings | None,
) -> SerialSettings:
    """Build :class:`SerialSettings` for one (port, baudrate, protocol).

    Starts from the protocol's factory framing
    (:meth:`SerialSettings.factory_for`) so a Modbus probe inherits
    the EZ-ZONE PM Modbus default parity (8-E-1) and a Std Bus probe
    inherits 8-N-1. When the caller passes ``template``, its parity /
    bytesize / stopbits / flow-control fields override the factory
    framing; ``port`` and ``baudrate`` are always overwritten.
    """
    factory = SerialSettings.factory_for(protocol, port=port)
    if template is None:
        return replace(factory, baudrate=baudrate)
    return replace(
        factory,
        baudrate=baudrate,
        bytesize=template.bytesize,
        parity=template.parity,
        stopbits=template.stopbits,
        rtscts=template.rtscts,
        xonxoff=template.xonxoff,
        exclusive=template.exclusive,
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


async def _probe_combo(
    *,
    port: str,
    baudrate: int,
    protocol: ProtocolKind,
    addresses: Sequence[int],
    serial_settings: SerialSettings,
    timeout_s: float,
) -> tuple[list[FindResult], bool]:
    """Open one (port, baudrate, protocol) transport and probe every address.

    Returns ``(rows, port_died)`` where ``port_died`` is ``True`` when
    the transport failed to open — signalling the caller to skip the
    rest of the scan for this port.
    """
    transport = _build_transport(protocol, serial_settings)
    rows: list[FindResult] = []
    try:
        await transport.open()
    except WatlowConnectionError as exc:
        # The port itself isn't usable — every address probe would
        # fail the same way; emit a row per planned address keyed to
        # the open error and tell the caller this port is dead.
        rows.extend(
            FindResult(
                port=port,
                address=address,
                baudrate=baudrate,
                protocol=protocol,
                ok=False,
                info=None,
                error=exc,
            )
            for address in addresses
        )
        return rows, True
    except WatlowError as exc:
        # Non-connection transport / config error — still surface one
        # row per address but don't mark the port dead; a different
        # (baudrate, protocol) combo may succeed (e.g. wrong-parity
        # rejected by the kernel termios layer on a specific framing).
        rows.extend(
            FindResult(
                port=port,
                address=address,
                baudrate=baudrate,
                protocol=protocol,
                ok=False,
                info=None,
                error=exc,
            )
            for address in addresses
        )
        return rows, False

    try:
        for address in addresses:
            # Sequential await — a list comprehension can't capture an
            # ``await`` inside a regular ``for``, and we want
            # deterministic per-address ordering.
            rows.append(  # noqa: PERF401
                await _probe_address(
                    transport=transport,
                    port=port,
                    baudrate=baudrate,
                    protocol=protocol,
                    address=address,
                    serial_settings=serial_settings,
                    timeout_s=timeout_s,
                ),
            )
    finally:
        try:
            await transport.close()
        except WatlowError as exc:
            _log.debug(
                "discovery: transport close failed on %s (%s @ %d): %s",
                port,
                protocol.value,
                baudrate,
                exc,
            )
    return rows, False


async def _probe_address(
    *,
    transport: Transport,
    port: str,
    baudrate: int,
    protocol: ProtocolKind,
    address: int,
    serial_settings: SerialSettings,
    timeout_s: float,
) -> FindResult:
    """Identify the device at ``address`` over the open ``transport``.

    Caps the entire identify exchange at ``timeout_s`` via
    :func:`anyio.fail_after` — :meth:`Controller.identify` issues four
    sub-reads at the per-call timeout, and a silent bus would
    otherwise blow the documented per-address budget by 4×.

    The transport is *not* closed here — :func:`_probe_combo` owns it
    for the lifetime of the (port, baudrate, protocol) combo. Each
    probe disposes its own session + client on exit so the next
    address starts with a clean dispatch state.
    """
    client = make_protocol_client(protocol, transport)
    session = Session(
        client,
        registry=PARAMETERS,
        family=ControllerFamily.UNKNOWN,
        address=address,
        port=transport.label,
    )
    # Pass the real per-probe settings through so the responsive row's
    # ``info.serial_settings`` reflects what actually opened the port —
    # callers may want to feed it straight back into ``open_device``.
    controller = Controller(session, transport, serial_settings=serial_settings)

    info = None
    error: WatlowError | None = None
    try:
        try:
            with anyio.fail_after(timeout_s):
                # ``query_configured_protocol=True`` adds one round-trip
                # but populates ``DeviceInfo.configured_protocol`` so
                # GUI Discover dialogs can flag protocol mismatches
                # without a follow-up call. Silent addresses time out
                # on the first read, so this only costs on responsive
                # devices.
                info = await controller.identify(
                    timeout=timeout_s,
                    query_configured_protocol=True,
                )
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
        try:
            session.dispose()
        except WatlowError:
            _log.debug(
                "discovery: dispose failed on %s addr=%d",
                protocol.value,
                address,
                exc_info=True,
            )

    # ``identify`` is tolerant by design: a silent device still yields
    # a structurally-valid :class:`DeviceInfo` with empty / ``None``
    # fields. Treat that as "no device here" and demote to
    # ``info=None`` so callers can filter responsive vs absent rows on
    # a single attribute. Surface the absence as a typed timeout so
    # silent rows always carry a structured error.
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

    return FindResult(
        port=port,
        address=address,
        baudrate=baudrate,
        protocol=protocol,
        ok=info is not None,
        info=info,
        error=error,
    )
