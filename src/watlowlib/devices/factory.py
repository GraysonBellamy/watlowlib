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
from watlowlib.registry.units import Unit, coerce_unit
from watlowlib.transport.base import SerialSettings
from watlowlib.transport.serial import SerialTransport

if TYPE_CHECKING:
    from watlowlib.transport.base import Transport

__all__ = ["coerce_wire_temperature_unit", "open_device"]


async def open_device(
    port: str,
    *,
    protocol: ProtocolKind = ProtocolKind.STDBUS,
    address: int = 1,
    serial_settings: SerialSettings | None = None,
    assert_wire_temperature_unit: Unit | str | None = None,
    identify: bool = True,
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
        identify: When ``True`` (default), :meth:`Controller.identify`
            runs after the transport opens so :meth:`Controller.snapshot`
            renders without further wire I/O. Set ``False`` for the
            fast-path open scenarios where caller code drives identity
            itself or wants the open to return immediately.
        assert_wire_temperature_unit: User-asserted scale of
            temperature values on the wire. Sets
            :class:`Reading.unit` / :class:`Sample.unit` for
            temperature parameters. Accepts a :class:`Unit` or a
            case-insensitive string alias (``"C"``, ``"F"``,
            ``"celsius"``, ``"degF"``, ``"°C"``, ...).
            :attr:`Unit.PERCENT` is rejected. ``None`` (the default)
            means temperature readings carry ``unit=None``. The
            library does **not** infer this from parameter 17050 —
            on at least one PM3 firmware 17050 is a label-only
            register and would silently mis-tag. Verify the actual
            scale externally — the bundled
            ``watlow-diag probe-unit`` CLI automates the comparison
            against a known panel reading; see ``docs/devices.md``
            §Units — before asserting it here.

    Returns:
        An *opened* :class:`Controller` whose transport is ready for
        :meth:`Controller.poll` / :meth:`Controller.poll_many` calls.
        Every protocol (``STDBUS``, ``MODBUS_RTU``, ``AUTO``) returns
        an opened controller; ``__aenter__`` is a no-op and
        ``__aexit__`` closes the transport.

    Raises:
        WatlowConfigurationError: ``address`` is out of range or
            ``protocol`` is unsupported.
        WatlowValidationError: ``assert_wire_temperature_unit`` is
            :attr:`Unit.PERCENT` or an unrecognised alias.
        WatlowProtocolUnsupportedError: ``protocol=AUTO`` and both
            probes failed.
    """
    if protocol not in (ProtocolKind.STDBUS, ProtocolKind.MODBUS_RTU, ProtocolKind.AUTO):
        raise WatlowConfigurationError(
            f"unsupported protocol kind: {protocol!r}",
            context=ErrorContext(port=port),
        )

    wire_unit = coerce_wire_temperature_unit(assert_wire_temperature_unit)

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
            wire_temperature_unit=wire_unit,
        )
        controller = Controller(session, resolved.transport, serial_settings=settings)
        if identify:
            await controller.identify()
        return controller

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
    controller = await _open_controller(
        transport,
        protocol=protocol,
        address=address,
        serial_settings=settings,
        wire_temperature_unit=wire_unit,
    )
    if identify:
        await controller.identify()
    return controller


async def _open_controller(
    transport: Transport,
    *,
    protocol: ProtocolKind,
    address: int,
    serial_settings: SerialSettings,
    family: ControllerFamily = ControllerFamily.UNKNOWN,
    wire_temperature_unit: Unit | None = None,
) -> Controller:
    """Build an opened :class:`Controller` over an existing :class:`Transport`.

    Module-private — :func:`open_device` calls this for the concrete-
    protocol path. The testing seam at
    :mod:`watlowlib.testing` (``open_test_controller``,
    ``controller_from_fixture``) keeps its own fixture-friendly
    equivalent so the test surface doesn't depend on a private symbol.

    Opens the transport if not already open. ``wire_temperature_unit``
    is the already-coerced :class:`watlowlib.registry.units.Unit` (or
    ``None``) that drives :class:`Reading.unit` for temperature
    parameters.
    """
    if protocol is ProtocolKind.AUTO:
        raise WatlowConfigurationError(
            "_open_controller requires a concrete protocol; AUTO must be resolved by "
            "open_device (which runs the detector and returns a built Controller).",
            context=ErrorContext(port=transport.label),
        )
    if not transport.is_open:
        await transport.open()
    client = make_protocol_client(protocol, transport)
    session = Session(
        client,
        registry=PARAMETERS,
        family=family,
        address=address,
        port=transport.label,
        wire_temperature_unit=wire_temperature_unit,
    )
    return Controller(session, transport, serial_settings=serial_settings)


def coerce_wire_temperature_unit(value: Unit | str | None) -> Unit | None:
    """Normalise the ``assert_wire_temperature_unit`` kwarg.

    Accepts a :class:`Unit`, a case-insensitive alias, or ``None``.
    Rejects :attr:`Unit.PERCENT` pre-I/O — a temperature scale must
    be °C or °F.
    """
    from watlowlib.errors import WatlowValidationError  # noqa: PLC0415 — cold path

    if value is None:
        return None
    resolved = coerce_unit(value)
    if resolved is Unit.PERCENT:
        raise WatlowValidationError(
            "assert_wire_temperature_unit accepts CELSIUS / FAHRENHEIT only; "
            "PERCENT is not a temperature scale",
        )
    return resolved


_coerce_wire_temperature_unit = coerce_wire_temperature_unit
