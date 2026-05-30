"""Port-scan discovery tests.

Drive :func:`find_devices` through monkey-patched transport
constructors and a stubbed ``anyserial.list_serial_ports`` so the scan
never opens a real port. Each test stands up a fake bus that responds
to one (port, baudrate, protocol, address) combination and asserts the
cartesian-product expansion lands the right rows in the right order.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from anyserial import Parity, PortInfo

if TYPE_CHECKING:
    from collections.abc import Sequence

from watlowlib import (
    DEFAULT_DISCOVERY_ADDRESSES,
    DEFAULT_DISCOVERY_BAUDRATES,
    DEFAULT_DISCOVERY_PROTOCOLS,
    DiscoveryResult,
    FakeTransport,
    ProtocolKind,
    SerialSettings,
    WatlowConnectionError,
    find_devices,
)
from watlowlib.errors import ErrorContext
from watlowlib.protocol.stdbus.framing import Frame, encode_frame
from watlowlib.protocol.stdbus.payload import encode_read_request
from watlowlib.protocol.stdbus.tables import HOST_MAC, FrameType, addr_to_mac

# Captured PM3 round-trips, instance=1. Identify reads four params:
# part_number (1009), hardware_id (1001), firmware_id (1002),
# serial_number, plus parameter 17009 (configured protocol) because
# the scan passes ``query_configured_protocol=True``.
_REQ_HW = encode_read_request(1001, instance=1)
_REQ_PART = encode_read_request(1009, instance=1)
_REQ_FW = encode_read_request(1002, instance=1)


def _stdbus_request_for(payload: bytes, *, mac: int) -> bytes:
    return encode_frame(
        Frame(frame_type=FrameType.DATA_EXPECTING_REPLY, dst=mac, src=HOST_MAC, payload=payload),
    )


def _stdbus_reply(payload: bytes, *, mac: int) -> bytes:
    return encode_frame(
        Frame(
            frame_type=FrameType.DATA_NOT_EXPECTING_REPLY, dst=HOST_MAC, src=mac, payload=payload
        ),
    )


# Captured PM3 ReadResponse payloads.
_HW_PAYLOAD = bytes.fromhex("0203010101010600000018")  # hardware_id 24
_PART_PAYLOAD = bytes.fromhex("0203010109010910504D33523143412D414141414141410000")
_FW_PAYLOAD = bytes.fromhex("0203010102010600000018")  # firmware_id 24 (synthetic)


def _build_stdbus_script(*, mac: int) -> dict[bytes, bytes]:
    """A FakeTransport script that responds to identify() at one MAC."""
    return {
        _stdbus_request_for(_REQ_HW, mac=mac): _stdbus_reply(_HW_PAYLOAD, mac=mac),
        _stdbus_request_for(_REQ_PART, mac=mac): _stdbus_reply(_PART_PAYLOAD, mac=mac),
        _stdbus_request_for(_REQ_FW, mac=mac): _stdbus_reply(_FW_PAYLOAD, mac=mac),
    }


# --- Patched transport plumbing -------------------------------------


@dataclass
class _ScanFakes:
    """Bookkeeping for the patched transport factory.

    Keyed by ``(port, baudrate)`` so a multi-port × multi-baud scan can
    route each combo to a different scripted bus. Tests usually only
    populate one key; missing keys produce silent fakes (good — that
    surfaces as ``ok=False`` rows, the same shape a real silent bus
    would produce).
    """

    stdbus_scripts: dict[tuple[str, int], FakeTransport]
    modbus_scripts: dict[tuple[str, int], Any]
    open_failures: set[str]  # ports whose ``open()`` raises
    seen_combos: list[tuple[str, int, ProtocolKind]]


class _PatchedSerialTransport:
    """Stand-in for :class:`SerialTransport` keyed by (port, baudrate)."""

    def __init__(self, settings: SerialSettings, *, fakes: _ScanFakes) -> None:
        self._fakes = fakes
        self._settings = settings
        self._fake: FakeTransport | None = None
        self._closed = False

    def _resolve_fake(self) -> FakeTransport:
        if self._fake is None:
            key = (self._settings.port, self._settings.baudrate)
            scripted = self._fakes.stdbus_scripts.get(key)
            if scripted is None:
                self._fake = FakeTransport(
                    {}, label=f"fake://stdbus@{self._settings.port}@{self._settings.baudrate}"
                )
            else:
                # Reuse the scripted FakeTransport so its `_script` is
                # the one we expect. Each iteration through the scan
                # gets a fresh instance via this dict lookup — but a
                # single combo is opened once, walked, closed; a
                # FakeTransport reopen would be a problem. Wrap.
                self._fake = FakeTransport(
                    dict(scripted._script),  # pyright: ignore[reportPrivateUsage]
                    label=f"fake://stdbus@{self._settings.port}@{self._settings.baudrate}",
                )
        return self._fake

    @property
    def is_open(self) -> bool:
        return self._fake is not None and self._fake.is_open

    @property
    def label(self) -> str:
        return self._resolve_fake().label

    async def open(self) -> None:
        if self._settings.port in self._fakes.open_failures:
            raise WatlowConnectionError(
                f"open refused on {self._settings.port}",
                context=ErrorContext(port=self._settings.port),
            )
        await self._resolve_fake().open()

    async def close(self) -> None:
        if self._fake is not None:
            await self._fake.close()

    async def write(self, data: bytes, *, timeout: float) -> None:
        await self._resolve_fake().write(data, timeout=timeout)

    async def read_exact(self, n: int, *, timeout: float) -> bytes:
        return await self._resolve_fake().read_exact(n, timeout=timeout)

    async def read_available(
        self,
        *,
        idle_timeout: float,
        max_bytes: int | None = None,
    ) -> bytes:
        return await self._resolve_fake().read_available(
            idle_timeout=idle_timeout,
            max_bytes=max_bytes,
        )

    async def drain_input(self) -> None:
        await self._resolve_fake().drain_input()


class _PatchedBusTransport:
    """Stand-in for :class:`ModbusBusTransport` keyed by (port, baudrate)."""

    def __init__(self, settings: SerialSettings, *, fakes: _ScanFakes) -> None:
        self._fakes = fakes
        self._settings = settings
        self._open = False

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def label(self) -> str:
        return f"fake://modbus@{self._settings.port}@{self._settings.baudrate}"

    @property
    def bus(self) -> Any:
        return _BusFacade(
            fakes=self._fakes,
            port=self._settings.port,
            baudrate=self._settings.baudrate,
        )

    async def open(self) -> None:
        if self._settings.port in self._fakes.open_failures:
            raise WatlowConnectionError(
                f"open refused on {self._settings.port}",
                context=ErrorContext(port=self._settings.port),
            )
        self._open = True

    async def close(self) -> None:
        self._open = False


@dataclass
class _BusFacade:
    fakes: _ScanFakes
    port: str
    baudrate: int

    def slave(self, address: int) -> Any:
        slave = self.fakes.modbus_scripts.get((self.port, self.baudrate))
        if slave is None:
            return _SilentSlave()
        return slave


class _SilentSlave:
    async def read_holding_registers(self, address: int, *, count: int) -> tuple[int, ...]:
        from anymodbus import FrameTimeoutError

        raise FrameTimeoutError("scripted")

    async def read_input_registers(self, address: int, *, count: int) -> tuple[int, ...]:
        return await self.read_holding_registers(address, count=count)

    async def write_register(self, address: int, value: int) -> None:
        _ = address, value

    async def write_registers(self, address: int, values: Sequence[int]) -> None:
        _ = address, values


def _patch_factory(monkeypatch: pytest.MonkeyPatch, fakes: _ScanFakes) -> None:
    """Patch transport constructors + the ``anyserial`` enumerator."""
    from watlowlib.devices import discovery

    def _make_serial(settings: SerialSettings) -> _PatchedSerialTransport:
        return _PatchedSerialTransport(settings, fakes=fakes)

    monkeypatch.setattr(discovery, "SerialTransport", _make_serial)

    fakes_ref = fakes

    class _PatchedBus(_PatchedBusTransport):
        def __init__(self, settings: SerialSettings) -> None:
            super().__init__(settings, fakes=fakes_ref)

    import watlowlib.protocol.modbus.transport as modbus_transport_mod

    monkeypatch.setattr(modbus_transport_mod, "ModbusBusTransport", _PatchedBus)

    # Wrap _probe_combo so tests can observe which (port, baudrate,
    # protocol) combos were actually attempted (per-port short-circuit
    # invariant).
    real_probe = getattr(discovery, "_probe_combo")  # noqa: B009

    async def _record_then_probe(*args: Any, **kwargs: Any) -> Any:
        fakes.seen_combos.append((kwargs["port"], kwargs["baudrate"], kwargs["protocol"]))
        return await real_probe(*args, **kwargs)

    monkeypatch.setattr(discovery, "_probe_combo", _record_then_probe)


def _patch_anyserial(monkeypatch: pytest.MonkeyPatch, ports: Sequence[str]) -> None:
    """Stub ``anyserial.list_serial_ports`` to return ``ports`` in order."""

    async def _fake_list() -> list[PortInfo]:
        return [PortInfo(device=p) for p in ports]

    import anyserial

    monkeypatch.setattr(anyserial, "list_serial_ports", _fake_list)


def _empty_fakes() -> _ScanFakes:
    return _ScanFakes(
        stdbus_scripts={},
        modbus_scripts={},
        open_failures=set(),
        seen_combos=[],
    )


# --- Defaults ------------------------------------------------------


def test_defaults_are_narrow() -> None:
    """Default scan should hit address 1 only — multi-port × multi-baud × multi-protocol
    already balloons probe count; the address dimension stays at 1."""
    assert DEFAULT_DISCOVERY_ADDRESSES == (1,)
    assert DEFAULT_DISCOVERY_BAUDRATES == (38400, 19200, 9600)
    assert DEFAULT_DISCOVERY_PROTOCOLS == (ProtocolKind.STDBUS, ProtocolKind.MODBUS_RTU)


# --- Std Bus happy path -------------------------------------------


@pytest.mark.anyio
async def test_find_devices_stdbus_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    anyio_backend: object,
) -> None:
    _ = anyio_backend
    fakes = _empty_fakes()
    fakes.stdbus_scripts[("/dev/fake", 38400)] = FakeTransport(
        _build_stdbus_script(mac=addr_to_mac(1))
    )
    _patch_factory(monkeypatch, fakes)

    rows = await find_devices(
        ports=["/dev/fake"],
        addresses=(1,),
        baudrates=(38400,),
        protocols=(ProtocolKind.STDBUS,),
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.ok is True
    assert row.port == "/dev/fake"
    assert row.address == 1
    assert row.baudrate == 38400
    assert row.protocol is ProtocolKind.STDBUS
    assert row.device_info is not None
    assert row.device_info.part_number.raw.startswith("PM3R1CA")
    assert row.error is None
    # `query_configured_protocol=True` is passed; identify() reads
    # parameter 17009 but our scripted fake doesn't include it, so the
    # field stays None (this is the expected silent-secondary-field
    # behaviour — health stays OK because the load-bearing reads landed).
    assert row.device_info.configured_protocol is None


# --- Modbus happy path --------------------------------------------


@pytest.mark.anyio
async def test_find_devices_modbus_happy_path(
    monkeypatch: pytest.MonkeyPatch,
    anyio_backend: object,
) -> None:
    _ = anyio_backend
    from watlowlib import PARAMETERS
    from watlowlib.transport.fake import FakeSlave

    hw_addr = PARAMETERS.resolve("hardware_id").relative_addr
    fw_addr = PARAMETERS.resolve("firmware_id").relative_addr
    part_addr = PARAMETERS.resolve("part_number").relative_addr

    pn = "PM3R1CA-AAAAAAA".encode("ascii").ljust(16, b"\x00")
    pn_words: tuple[int, ...] = tuple(
        int.from_bytes(pn[i : i + 2], "big") for i in range(0, len(pn), 2)
    )

    slave = FakeSlave(
        {
            ("read_holding_registers", hw_addr): (0x0000, 0x0018),
            ("read_holding_registers", fw_addr): (0x0000, 0x0018),
            ("read_holding_registers", part_addr): pn_words,
        }
    )

    fakes = _empty_fakes()
    fakes.modbus_scripts[("/dev/fake", 9600)] = slave
    _patch_factory(monkeypatch, fakes)

    rows = await find_devices(
        ports=["/dev/fake"],
        addresses=(1,),
        baudrates=(9600,),
        protocols=(ProtocolKind.MODBUS_RTU,),
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.ok is True
    assert row.baudrate == 9600
    assert row.protocol is ProtocolKind.MODBUS_RTU
    assert row.device_info is not None
    assert row.device_info.part_number.raw.startswith("PM3R1CA")


# --- Sad paths -----------------------------------------------------


@pytest.mark.anyio
async def test_find_devices_port_wont_open(
    monkeypatch: pytest.MonkeyPatch,
    anyio_backend: object,
) -> None:
    """A port that refuses to open emits one row per planned (baud, proto,
    addr) tuple — and is short-circuited for the rest of the scan."""
    _ = anyio_backend
    fakes = _empty_fakes()
    fakes.open_failures.add("/dev/dead")
    _patch_factory(monkeypatch, fakes)

    rows = await find_devices(
        ports=["/dev/dead"],
        addresses=(1, 2),
        baudrates=(38400, 9600),
        protocols=(ProtocolKind.STDBUS, ProtocolKind.MODBUS_RTU),
    )

    # 2 bauds × 2 protocols × 2 addresses = 8 rows, every one ok=False.
    assert len(rows) == 8
    assert all(not r.ok for r in rows)
    assert all(isinstance(r.error, WatlowConnectionError) for r in rows)

    # Per-port short-circuit: only the first (baud, protocol) combo
    # actually attempted the open. The rest were skipped at the outer
    # loop. (_probe_combo wrapper records combos that reached _probe_combo.)
    # The first combo emits 2 rows (one per address) before marking
    # the port dead; subsequent combos emit short-circuit rows from
    # the outer loop, bypassing _probe_combo.
    assert len(fakes.seen_combos) == 1
    assert fakes.seen_combos[0] == ("/dev/dead", 38400, ProtocolKind.STDBUS)


@pytest.mark.anyio
async def test_find_devices_silent_bus(
    monkeypatch: pytest.MonkeyPatch,
    anyio_backend: object,
) -> None:
    """No script for any combo — every row is ok=False with a populated error."""
    _ = anyio_backend
    fakes = _empty_fakes()
    _patch_factory(monkeypatch, fakes)

    rows = await find_devices(
        ports=["/dev/fake"],
        addresses=(1,),
        baudrates=(38400,),
        protocols=(ProtocolKind.STDBUS,),
    )

    assert len(rows) == 1
    assert rows[0].ok is False
    assert rows[0].device_info is None
    # Silent address → identify produced an empty DeviceInfo; the
    # demote synthesises a WatlowTimeoutError so absent rows always
    # carry a typed error.
    assert rows[0].error is not None


@pytest.mark.anyio
async def test_find_devices_address_out_of_range_does_not_abort_scan(
    monkeypatch: pytest.MonkeyPatch,
    anyio_backend: object,
) -> None:
    """A Std Bus address > 16 emits a typed ok=False row, not a scan abort.

    Regression guard for the latent ``addr_to_mac`` bug: it used to
    raise a bare ``ValueError`` mid-dispatch that no layer caught, so a
    single out-of-range address aborted the whole scan. Discovery now
    pre-validates the address per protocol and emits a structured
    ``WatlowConfigurationError`` row while continuing to probe the
    valid addresses.
    """
    from watlowlib import WatlowConfigurationError

    _ = anyio_backend
    fakes = _empty_fakes()
    fakes.stdbus_scripts[("/dev/fake", 38400)] = FakeTransport(
        _build_stdbus_script(mac=addr_to_mac(1))
    )
    _patch_factory(monkeypatch, fakes)

    rows = await find_devices(
        ports=["/dev/fake"],
        addresses=(1, 17),  # 17 is out of the Std Bus 1..16 range
        baudrates=(38400,),
        protocols=(ProtocolKind.STDBUS,),
    )

    assert len(rows) == 2
    by_addr = {r.address: r for r in rows}
    # The valid address still probed successfully — the scan did not abort.
    assert by_addr[1].ok is True
    # The out-of-range address is a typed config error, elapsed-free.
    assert by_addr[17].ok is False
    assert isinstance(by_addr[17].error, WatlowConfigurationError)
    assert by_addr[17].elapsed_s == 0.0


# --- Cartesian product ordering -----------------------------------


@pytest.mark.anyio
async def test_find_devices_cartesian_product_order(
    monkeypatch: pytest.MonkeyPatch,
    anyio_backend: object,
) -> None:
    """Output ordering = port → baudrate → protocol → address."""
    _ = anyio_backend
    fakes = _empty_fakes()
    _patch_factory(monkeypatch, fakes)

    rows = await find_devices(
        ports=["/dev/a", "/dev/b"],
        addresses=(1, 2),
        baudrates=(38400, 9600),
        protocols=(ProtocolKind.STDBUS, ProtocolKind.MODBUS_RTU),
    )

    expected_keys = [
        (port, baud, proto, addr)
        for port in ("/dev/a", "/dev/b")
        for baud in (38400, 9600)
        for proto in (ProtocolKind.STDBUS, ProtocolKind.MODBUS_RTU)
        for addr in (1, 2)
    ]

    actual_keys = [(r.port, r.baudrate, r.protocol, r.address) for r in rows]
    assert actual_keys == expected_keys


# --- Port enumeration via anyserial -------------------------------


@pytest.mark.anyio
async def test_find_devices_enumerates_when_ports_is_none(
    monkeypatch: pytest.MonkeyPatch,
    anyio_backend: object,
) -> None:
    _ = anyio_backend
    fakes = _empty_fakes()
    _patch_factory(monkeypatch, fakes)
    _patch_anyserial(monkeypatch, ["/dev/x", "/dev/y"])

    rows = await find_devices(
        addresses=(1,),
        baudrates=(38400,),
        protocols=(ProtocolKind.STDBUS,),
    )

    assert [r.port for r in rows] == ["/dev/x", "/dev/y"]


@pytest.mark.anyio
async def test_find_devices_empty_ports_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
    anyio_backend: object,
) -> None:
    """``ports=[]`` is distinct from ``ports=None`` — no enumeration, no rows."""
    _ = anyio_backend
    fakes = _empty_fakes()
    _patch_factory(monkeypatch, fakes)

    rows = await find_devices(ports=[])
    assert rows == []


# --- Per-protocol framing -----------------------------------------


@pytest.mark.anyio
async def test_find_devices_uses_factory_framing_per_protocol(
    monkeypatch: pytest.MonkeyPatch,
    anyio_backend: object,
) -> None:
    """Modbus probes must inherit 8-E-1 parity even when bauds are user-supplied.

    Without per-protocol framing reset, a Modbus probe at 9600 would
    use the Std Bus factory parity (NONE) and miss every default
    parity Modbus device on the bus.
    """
    _ = anyio_backend

    seen_settings: list[tuple[ProtocolKind, Parity]] = []

    from watlowlib.devices import discovery

    real_probe = getattr(discovery, "_probe_combo")  # noqa: B009

    async def _capture(*args: Any, **kwargs: Any) -> Any:
        seen_settings.append((kwargs["protocol"], kwargs["serial_settings"].parity))
        return await real_probe(*args, **kwargs)

    monkeypatch.setattr(discovery, "_probe_combo", _capture)

    fakes = _empty_fakes()

    # Patch transport classes after wrapping _probe_combo (the patcher
    # also wraps _probe_combo; do them in the right order).
    def _make_serial(s: SerialSettings) -> _PatchedSerialTransport:
        return _PatchedSerialTransport(s, fakes=fakes)

    monkeypatch.setattr(discovery, "SerialTransport", _make_serial)

    class _PatchedBus(_PatchedBusTransport):
        def __init__(self, settings: SerialSettings) -> None:
            super().__init__(settings, fakes=fakes)

    import watlowlib.protocol.modbus.transport as modbus_transport_mod

    monkeypatch.setattr(modbus_transport_mod, "ModbusBusTransport", _PatchedBus)

    await find_devices(
        ports=["/dev/fake"],
        addresses=(1,),
        baudrates=(9600,),
        protocols=(ProtocolKind.STDBUS, ProtocolKind.MODBUS_RTU),
    )

    # Std Bus → Parity.NONE (8-N-1 factory). Modbus → Parity.EVEN (8-E-1 factory).
    assert (ProtocolKind.STDBUS, Parity.NONE) in seen_settings
    assert (ProtocolKind.MODBUS_RTU, Parity.EVEN) in seen_settings


# --- Validation ----------------------------------------------------


@pytest.mark.anyio
async def test_find_devices_rejects_auto_protocol(anyio_backend: object) -> None:
    _ = anyio_backend
    from watlowlib import WatlowConfigurationError

    with pytest.raises(WatlowConfigurationError, match="AUTO"):
        await find_devices(
            ports=["/dev/fake"],
            protocols=(ProtocolKind.AUTO,),
        )


@pytest.mark.anyio
async def test_find_devices_rejects_nonpositive_timeout(anyio_backend: object) -> None:
    _ = anyio_backend
    from watlowlib import WatlowConfigurationError

    with pytest.raises(WatlowConfigurationError, match="per_probe_timeout_s"):
        await find_devices(ports=["/dev/fake"], per_probe_timeout_s=0)


# --- Read-only invariant ------------------------------------------


@pytest.mark.anyio
async def test_find_devices_never_writes(
    monkeypatch: pytest.MonkeyPatch,
    anyio_backend: object,
) -> None:
    """A discovery scan must not issue any WRITE_PARAMETER calls.

    The handoff is explicit: scans run on rigs already talking to the
    controller; a setpoint write during discovery is a runaway-temperature
    incident waiting to happen.
    """
    _ = anyio_backend
    fakes = _empty_fakes()
    fakes.stdbus_scripts[("/dev/fake", 38400)] = FakeTransport(
        _build_stdbus_script(mac=addr_to_mac(1))
    )
    _patch_factory(monkeypatch, fakes)

    from watlowlib.commands.parameters import WRITE_PARAMETER
    from watlowlib.devices import session as session_mod

    write_calls: list[Any] = []
    real_execute = session_mod.Session.execute

    async def _spy_execute(
        self: session_mod.Session,
        command: Any,
        request: Any,
        **kwargs: Any,
    ) -> Any:
        if command is WRITE_PARAMETER:
            write_calls.append(request)
        return await real_execute(self, command, request, **kwargs)

    monkeypatch.setattr(session_mod.Session, "execute", _spy_execute)

    await find_devices(
        ports=["/dev/fake"],
        addresses=(1,),
        baudrates=(38400,),
        protocols=(ProtocolKind.STDBUS,),
    )

    assert write_calls == []


# --- ok flag semantics --------------------------------------------


@pytest.mark.anyio
async def test_find_devices_ok_flag_distinguishes_responsive_from_silent(
    monkeypatch: pytest.MonkeyPatch,
    anyio_backend: object,
) -> None:
    _ = anyio_backend
    fakes = _empty_fakes()
    # /dev/live responds at address 1; /dev/silent has no script.
    fakes.stdbus_scripts[("/dev/live", 38400)] = FakeTransport(
        _build_stdbus_script(mac=addr_to_mac(1))
    )
    _patch_factory(monkeypatch, fakes)

    rows = await find_devices(
        ports=["/dev/live", "/dev/silent"],
        addresses=(1,),
        baudrates=(38400,),
        protocols=(ProtocolKind.STDBUS,),
    )

    by_port = {r.port: r for r in rows}
    assert by_port["/dev/live"].ok is True
    assert by_port["/dev/silent"].ok is False

    # Single-attribute filter for GUI consumers.
    responsive = [r for r in rows if r.ok]
    assert [r.port for r in responsive] == ["/dev/live"]


# --- DiscoveryResult shape ---------------------------------------------


@pytest.mark.anyio
async def test_find_result_carries_flat_baudrate_and_health(
    monkeypatch: pytest.MonkeyPatch,
    anyio_backend: object,
) -> None:
    _ = anyio_backend
    fakes = _empty_fakes()
    fakes.stdbus_scripts[("/dev/fake", 19200)] = FakeTransport(
        _build_stdbus_script(mac=addr_to_mac(1))
    )
    _patch_factory(monkeypatch, fakes)

    rows = await find_devices(
        ports=["/dev/fake"],
        addresses=(1,),
        baudrates=(19200,),
        protocols=(ProtocolKind.STDBUS,),
    )

    from watlowlib.devices.models import DeviceHealth

    assert isinstance(rows[0], DiscoveryResult)
    assert rows[0].baudrate == 19200
    assert rows[0].device_info is not None
    assert rows[0].device_info.health is DeviceHealth.OK
