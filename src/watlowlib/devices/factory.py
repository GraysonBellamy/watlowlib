"""``open_device`` — single entry point for opening a controller.

Honours :attr:`ProtocolKind.STDBUS`, :attr:`ProtocolKind.MODBUS_RTU`,
and :attr:`ProtocolKind.AUTO` (Std Bus probe → Modbus probe → fail).
The detector itself lives in :mod:`watlowlib.protocol.detect`; the
factory only orchestrates.

The factory does **not** sweep bauds — the user sets one. See
``docs/design.md`` §7 for why baud sweeping is opt-in via the
``watlow-discover`` CLI rather than the open path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from watlowlib.devices.controller import Controller
from watlowlib.devices.session import Session
from watlowlib.errors import ErrorContext, WatlowConfigurationError
from watlowlib.protocol.base import ProtocolKind
from watlowlib.protocol.client import make_protocol_client
from watlowlib.registry.families import ControllerFamily
from watlowlib.registry.parameters import PARAMETERS
from watlowlib.transport.base import SerialSettings
from watlowlib.transport.serial import SerialTransport

if TYPE_CHECKING:
    from watlowlib.transport.base import Transport

__all__ = ["open_controller", "open_device"]


async def open_device(
    port: str,
    *,
    protocol: ProtocolKind = ProtocolKind.STDBUS,
    address: int = 1,
    serial_settings: SerialSettings | None = None,
) -> Controller:
    """Open a controller on a serial port.

    Args:
        port: Serial-port path (``/dev/ttyUSB0``, ``COM3``, ...).
        protocol: Wire protocol. ``STDBUS`` and ``MODBUS_RTU`` open
            directly; ``AUTO`` runs the conservative detector
            (Std Bus → Modbus → fail) per ``docs/design.md`` §7.
        address: Bus address. Std Bus accepts ``1..16``; Modbus RTU
            accepts ``1..247``. Under ``AUTO`` the same address is
            tried against both probes.
        serial_settings: Optional override. Default is **38400 8-N-1**,
            the EZ-ZONE PM Standard Bus factory setting; ``port`` from
            the positional arg is applied if ``serial_settings`` is
            ``None``. For Modbus RTU, the typical PM factory framing
            is **9600 8-E-1** — pass an explicit
            :class:`SerialSettings` to override the default. Auto-
            detect uses the same framing for both probes — there is
            no baud sweeping in the open path (cross-cutting
            invariant 5).

    Returns:
        An *opened* :class:`Controller` when ``protocol=AUTO`` (the
        detector held the transport open after a successful probe),
        otherwise an *unopened* :class:`Controller` to be used as an
        async context manager.

    Raises:
        WatlowConfigurationError: ``address`` is out of range or
            ``protocol`` is unsupported.
        WatlowProtocolUnsupportedError: ``protocol=AUTO`` and both
            probes failed.
    """
    if protocol not in (ProtocolKind.STDBUS, ProtocolKind.MODBUS_RTU, ProtocolKind.AUTO):
        raise WatlowConfigurationError(
            f"unsupported protocol kind: {protocol!r}",
            context=ErrorContext(port=port),
        )

    settings = serial_settings or SerialSettings(port=port)
    if settings.port != port:
        # User passed both — we honour the explicit ``port`` arg over
        # the settings dataclass to avoid silent surprise.
        from dataclasses import replace  # noqa: PLC0415 — cold path

        settings = replace(settings, port=port)

    if protocol is ProtocolKind.AUTO:
        # Lazy import — keep the Std-Bus-only callers off the anymodbus
        # dep graph until they actually opt in to AUTO.
        from watlowlib.protocol.detect import detect_protocol  # noqa: PLC0415

        resolved = await detect_protocol(
            port,
            address=address,
            serial_settings=settings,
        )
        # Detector returned an *open* transport already paired with the
        # right client; build the controller around them and skip
        # ``Controller.__aenter__``'s open() (it short-circuits when
        # ``transport.is_open`` is already True).
        session = Session(
            resolved.client,
            registry=PARAMETERS,
            family=ControllerFamily.UNKNOWN,
            address=address,
            port=resolved.transport.label,
        )
        return Controller(session, resolved.transport, serial_settings=settings)

    transport: Transport
    if protocol is ProtocolKind.MODBUS_RTU:
        # Lazy import — keep the Std-Bus path off the anymodbus dep
        # graph for users who never reach for Modbus.
        from watlowlib.protocol.modbus.transport import (  # noqa: PLC0415
            ModbusBusTransport,
        )

        transport = ModbusBusTransport(settings)
    else:
        transport = SerialTransport(settings)
    return await open_controller(
        transport,
        protocol=protocol,
        address=address,
        serial_settings=settings,
    )


async def open_controller(
    transport: Transport,
    *,
    protocol: ProtocolKind,
    address: int,
    serial_settings: SerialSettings,
    family: ControllerFamily = ControllerFamily.UNKNOWN,
) -> Controller:
    """Build a :class:`Controller` over an existing :class:`Transport`.

    Tests use this to drive the facade through a
    :class:`watlowlib.transport.fake.FakeTransport`. Production code
    uses :func:`open_device`.
    """
    if protocol is ProtocolKind.AUTO:
        raise WatlowConfigurationError(
            "open_controller requires a concrete protocol; AUTO must be resolved by "
            "open_device (which runs the detector and returns a built Controller).",
            context=ErrorContext(port=transport.label),
        )
    client = make_protocol_client(protocol, transport)
    session = Session(
        client,
        registry=PARAMETERS,
        family=family,
        address=address,
        port=transport.label,
    )
    return Controller(session, transport, serial_settings=serial_settings)
