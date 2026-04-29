"""Conservative ``ProtocolKind.AUTO`` detector.

Order, per ``docs/design.md`` §7:

1. Drain → Standard Bus probe (read parameter ``1001`` / Hardware ID
   at MAC ``0x10..0x1F``). A valid ``55 FF`` framed reply with correct
   header + data CRCs = Std Bus. The reply payload may be a
   :class:`ReadResponse` *or* an :class:`ErrorResponse` — both confirm
   the wire protocol; only the *framing* is the probe target.
2. Drain → Modbus RTU probe (``read_holding_registers(0, count=2)`` —
   the Hardware ID register). A CRC-correct reply — including a Modbus
   exception response — confirms Modbus. A plain timeout / no reply
   does not.
3. Fail with :class:`WatlowProtocolUnsupportedError` listing both
   attempts in :class:`ErrorContext.response`.

Auto-detect never sweeps bauds; the user sets one (cross-cutting
invariant 5).

The module exposes three layers, in increasing testability:

- :func:`detect_protocol` — production entry point. Builds real
  serial / Modbus transports, runs both probes, returns the
  :class:`ResolvedProtocol` triple ``(kind, client, transport)`` so
  :func:`watlowlib.devices.factory.open_device` does **not** re-open
  the port after detection succeeds.
- :func:`probe_stdbus` / :func:`probe_modbus` — pure ``client → bool``
  functions tests use against a :class:`FakeTransport`-driven Std Bus
  client or a stub-:class:`Slave`-driven Modbus client.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from watlowlib._logging import get_logger
from watlowlib.errors import (
    ErrorContext,
    WatlowError,
    WatlowProtocolUnsupportedError,
    WatlowTimeoutError,
)
from watlowlib.protocol.base import ProtocolKind
from watlowlib.protocol.modbus.client import ModbusProtocolClient
from watlowlib.protocol.modbus.ops import ModbusFn, ModbusOp
from watlowlib.protocol.modbus.transport import ModbusBusTransport
from watlowlib.protocol.stdbus.client import StdBusProtocolClient
from watlowlib.protocol.stdbus.payload import encode_read_request
from watlowlib.transport.base import SerialSettings
from watlowlib.transport.serial import SerialTransport

if TYPE_CHECKING:
    from watlowlib.protocol.base import ProtocolClient
    from watlowlib.protocol.stdbus.types import StdBusReply
    from watlowlib.transport.base import Transport

__all__ = [
    "ResolvedProtocol",
    "detect_protocol",
    "probe_modbus",
    "probe_stdbus",
]

# Hardware ID is parameter 1001: cls=1, member=1, instance=1. Universal
# on EZ-ZONE PM and the canonical probe target per design §7.
_PROBE_PARAMETER_ID = 1001
# Modbus relative_addr for hardware_id from the registry; baked here so
# the detector never has to reach into the registry.
_PROBE_REGISTER_ADDRESS = 0
_PROBE_REGISTER_COUNT = 2

# Fall back to a tighter probe budget than the full I/O default — the
# detector should reject a wrong-protocol port quickly rather than
# stalling for the full 5 s read timeout twice in a row.
_DEFAULT_PROBE_TIMEOUT_S = 1.0

# Bus-address ranges for the two probes (see ``docs/design.md`` §6 +
# the open question 4 resolution). Values are spec ranges, not magic
# numbers — Std Bus maps ``1..16`` to MS/TP MAC ``0x10..0x1F``;
# Modbus RTU's slave-address space is ``1..247`` (``248..255`` reserved).
_STDBUS_MIN_ADDR, _STDBUS_MAX_ADDR = 1, 16
_MODBUS_MIN_ADDR, _MODBUS_MAX_ADDR = 1, 247

_log = get_logger("detect")


@dataclass(frozen=True, slots=True)
class ResolvedProtocol:
    """The protocol the detector confirmed on a port.

    Returned in lieu of just a :class:`ProtocolKind` so the caller can
    feed the already-built client + transport straight into the
    :class:`Controller` without re-opening the serial port.
    """

    kind: ProtocolKind
    client: ProtocolClient[Any, Any]
    transport: Transport


async def probe_stdbus(
    client: ProtocolClient[bytes, StdBusReply],
    *,
    address: int,
    timeout: float,
) -> bool:
    """Run the Std Bus probe through ``client`` against ``address``.

    Returns ``True`` if the device replies with any structurally valid
    Std Bus frame (read response, write response, or error response).
    Framing failures, timeouts, and connection errors return ``False``
    so the caller can fall through to the next probe.
    """
    payload = encode_read_request(_PROBE_PARAMETER_ID, instance=1)
    try:
        await client.execute(
            payload,
            address=address,
            timeout=timeout,
            command_name="auto_detect:stdbus",
        )
    except WatlowTimeoutError:
        return False
    except WatlowError as exc:
        # Frame errors / connection issues / unrecognised replies → not
        # Std Bus. We deliberately do *not* swallow non-Watlow
        # exceptions; programmer errors should propagate.
        _log.debug("stdbus probe rejected: %s", exc)
        return False
    return True


async def probe_modbus(
    client: ProtocolClient[ModbusOp, tuple[int, ...]],
    *,
    address: int,
    timeout: float,
) -> bool:
    """Run the Modbus probe through ``client`` against ``address``.

    Returns ``True`` if the slave replies — either with valid register
    words *or* with a Modbus exception response. A CRC-correct
    exception reply still confirms the wire protocol (the slave is
    speaking Modbus, just doesn't have the address we asked for).
    Returns ``False`` only when there was no reply at all (timeout,
    connection error) or the reply was malformed.
    """
    op = ModbusOp(
        fn=ModbusFn.READ_HOLDING,
        address=_PROBE_REGISTER_ADDRESS,
        count=_PROBE_REGISTER_COUNT,
    )
    try:
        await client.execute(
            op,
            address=address,
            timeout=timeout,
            command_name="auto_detect:modbus",
        )
    except WatlowProtocolUnsupportedError as exc:
        # IllegalFunction / IllegalDataAddress: a CRC-correct exception
        # response. The slave is speaking Modbus.
        _log.debug("modbus probe accepted via exception response: %s", exc)
        return True
    except WatlowTimeoutError:
        return False
    except WatlowError as exc:
        _log.debug("modbus probe rejected: %s", exc)
        return False
    return True


async def detect_protocol(
    port: str,
    *,
    address: int = 1,
    serial_settings: SerialSettings | None = None,
    timeout_s: float | None = None,
) -> ResolvedProtocol:
    """Probe ``port`` for Std Bus, then Modbus RTU.

    Args:
        port: Serial-port path. Must be openable by both
            :class:`SerialTransport` (Std Bus probe) and
            :class:`ModbusBusTransport` (Modbus probe). Bauds and
            framing come from ``serial_settings``; the same framing is
            used for both probes — auto-detect never sweeps bauds.
        address: Bus address. Std Bus accepts ``1..16``; Modbus
            accepts ``1..247``. The Std Bus probe rejects addresses
            outside its range early; if Std Bus is rejected, the
            Modbus probe runs against ``address`` so a single value
            covers the common case (``1``).
        serial_settings: Optional override. Default is **38400 8-N-1**
            (the EZ-ZONE PM Standard Bus factory framing). For
            Modbus-default fleets, pass an explicit
            :class:`SerialSettings`.
        timeout_s: Per-probe budget. Defaults to a tight 1.0 s — slow
            enough to cover RS-485 turn-around, fast enough to keep a
            wrong-port detection under ~2.5 s.

    Returns:
        A :class:`ResolvedProtocol` with the open transport + matching
        client. The caller owns the transport lifecycle from this
        point on (typically by handing it to
        :class:`watlowlib.devices.controller.Controller`).

    Raises:
        WatlowProtocolUnsupportedError: Both probes failed. The error
            context includes a short summary of each attempt.
    """
    settings = serial_settings or SerialSettings(port=port)
    if settings.port != port:
        settings = replace(settings, port=port)

    timeout = timeout_s if timeout_s is not None else _DEFAULT_PROBE_TIMEOUT_S

    attempts: list[str] = []

    resolved = await _try_stdbus(address, settings, timeout=timeout, attempts=attempts)
    if resolved is not None:
        return resolved

    resolved = await _try_modbus(address, settings, timeout=timeout, attempts=attempts)
    if resolved is not None:
        return resolved

    summary = "; ".join(attempts) or "no probes attempted"
    raise WatlowProtocolUnsupportedError(
        f"auto-detect failed on {port!r}: {summary}",
        context=ErrorContext(
            port=port,
            address=address,
            response=summary.encode("utf-8", errors="replace"),
        ),
    )


async def _try_stdbus(
    address: int,
    settings: SerialSettings,
    *,
    timeout: float,
    attempts: list[str],
) -> ResolvedProtocol | None:
    """Run the Std Bus probe; return a resolved triple or ``None``."""
    if not _STDBUS_MIN_ADDR <= address <= _STDBUS_MAX_ADDR:
        attempts.append(
            f"stdbus: address {address} out of range "
            f"({_STDBUS_MIN_ADDR}..{_STDBUS_MAX_ADDR}) — skipped",
        )
        return None
    transport = SerialTransport(settings)
    handed_off = False
    try:
        await transport.open()
        await transport.drain_input()
        client = StdBusProtocolClient(transport)
        if await probe_stdbus(client, address=address, timeout=timeout):
            _log.info("auto-detect: stdbus confirmed on %s", settings.port)
            handed_off = True
            return ResolvedProtocol(
                kind=ProtocolKind.STDBUS,
                client=client,
                transport=transport,
            )
        attempts.append("stdbus: no valid frame")
    except WatlowError as exc:
        attempts.append(f"stdbus: {type(exc).__name__}: {exc}")
    finally:
        if not handed_off:
            try:
                await transport.close()
            except WatlowError as exc:
                _log.debug("stdbus close after failed probe: %s", exc)
    return None


async def _try_modbus(
    address: int,
    settings: SerialSettings,
    *,
    timeout: float,
    attempts: list[str],
) -> ResolvedProtocol | None:
    """Run the Modbus probe; return a resolved triple or ``None``."""
    if not _MODBUS_MIN_ADDR <= address <= _MODBUS_MAX_ADDR:
        attempts.append(
            f"modbus: address {address} out of range "
            f"({_MODBUS_MIN_ADDR}..{_MODBUS_MAX_ADDR}) — skipped",
        )
        return None
    transport = ModbusBusTransport(settings)
    handed_off = False
    try:
        await transport.open()
        client = ModbusProtocolClient(
            slave_provider=transport.bus.slave,
            port=transport.label,
        )
        if await probe_modbus(client, address=address, timeout=timeout):
            _log.info("auto-detect: modbus confirmed on %s", settings.port)
            handed_off = True
            return ResolvedProtocol(
                kind=ProtocolKind.MODBUS_RTU,
                client=client,
                transport=transport,
            )
        attempts.append("modbus: no reply")
    except WatlowError as exc:
        attempts.append(f"modbus: {type(exc).__name__}: {exc}")
    finally:
        if not handed_off:
            try:
                await transport.close()
            except WatlowError as exc:
                _log.debug("modbus close after failed probe: %s", exc)
    return None
