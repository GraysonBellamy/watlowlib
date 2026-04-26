"""Discovery sweep tests.

Drive :func:`sweep_stdbus` and :func:`sweep_modbus` through
monkey-patched transport constructors so the sweep never opens a
real port. Asserts that every probed address yields a
:class:`DiscoveryResult` with the right :attr:`protocol` field, and
that responsive addresses get a populated :class:`DeviceInfo`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from anyserial import Parity

if TYPE_CHECKING:
    from collections.abc import Sequence

from watlowlib import (
    DiscoveryResult,
    FakeTransport,
    ProtocolKind,
    SerialSettings,
    sweep_modbus,
    sweep_stdbus,
)
from watlowlib.protocol.stdbus.framing import Frame, encode_frame
from watlowlib.protocol.stdbus.payload import encode_read_request
from watlowlib.protocol.stdbus.tables import HOST_MAC, FrameType, addr_to_mac

# Captured PM3 round-trips, instance=1.
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


# Captured PM3 ReadResponse payloads (parsed from test_integration.py).
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


# --- Discovery uses ``open_device`` under the hood, which builds new
#     SerialTransport / ModbusBusTransport instances per address. We
#     patch those constructors at the call site so we can route each
#     probe to a per-address fake.


@dataclass
class _SweepFakes:
    """Bookkeeping for the patched transport factory."""

    stdbus_scripts: dict[int, FakeTransport]
    modbus_scripts: dict[int, Any]
    seen_addresses: list[int]


class _PatchedSerialTransport:
    """Stand-in for :class:`SerialTransport` that multiplexes per-address scripts.

    The post-2026-04-26 discovery refactor opens the transport once
    per sweep and walks addresses against the same handle (Std Bus
    addresses live in the BACnet MS/TP dst-MAC byte, not in the
    transport configuration). Each per-address ``FakeTransport``
    script keys on different MAC bytes, so unioning them produces a
    single combined script that routes correctly on exact-bytes
    match.
    """

    def __init__(self, settings: SerialSettings, *, fakes: _SweepFakes) -> None:
        self._fakes = fakes
        self._fake: FakeTransport | None = None
        self._settings = settings

    def _resolve_fake(self) -> FakeTransport:
        if self._fake is None:
            combined_script: dict[bytes, Any] = {}
            for per_address_fake in self._fakes.stdbus_scripts.values():
                # Each per-address FakeTransport carries its own
                # script dict. Reach into it (test-only seam) so we
                # can union without rebuilding the whole script
                # parameter pipeline.
                combined_script.update(per_address_fake._script)  # pyright: ignore[reportPrivateUsage]
            self._fake = FakeTransport(
                combined_script, label=f"fake://stdbus@{self._settings.port}"
            )
        return self._fake

    @property
    def is_open(self) -> bool:
        return self._fake is not None and self._fake.is_open

    @property
    def label(self) -> str:
        return self._resolve_fake().label

    async def open(self) -> None:
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
    """Stand-in for :class:`ModbusBusTransport` routed per address."""

    def __init__(self, settings: SerialSettings, *, fakes: _SweepFakes) -> None:
        self._fakes = fakes
        self._open = False
        self._settings = settings

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def label(self) -> str:
        return f"fake://modbus@{self._settings.port}"

    @property
    def bus(self) -> Any:
        return _BusFacade(fakes=self._fakes)

    async def open(self) -> None:
        self._open = True

    async def close(self) -> None:
        self._open = False


@dataclass
class _BusFacade:
    fakes: _SweepFakes

    def slave(self, address: int) -> Any:
        slave = self.fakes.modbus_scripts.get(address)
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


def _patch_factory(monkeypatch: pytest.MonkeyPatch, fakes: _SweepFakes) -> None:
    """Patch the constructors used by discovery sweeps + ``open_device``.

    The Modbus client does ``isinstance(transport, ModbusBusTransport)``
    after construction (cross-protocol guard), so we replace the
    *class* rather than the constructor — the patched class is what
    both the factory **and** the isinstance check see.

    Discovery imports :class:`SerialTransport` and
    :func:`make_protocol_client` directly (not through the factory)
    after the 2026-04-26 open-once-per-protocol refactor, so the
    patcher mirrors the bindings on both modules.
    """
    from watlowlib.devices import discovery, factory

    def _make_serial(settings: SerialSettings) -> _PatchedSerialTransport:
        return _PatchedSerialTransport(settings, fakes=fakes)

    monkeypatch.setattr(factory, "SerialTransport", _make_serial)
    monkeypatch.setattr(discovery, "SerialTransport", _make_serial)

    # Build a per-test ``ModbusBusTransport`` class bound to ``fakes``
    # so the constructor signature stays single-arg (``settings``) and
    # ``isinstance`` checks pass against the same class object.
    fakes_ref = fakes

    class _PatchedBus(_PatchedBusTransport):
        def __init__(self, settings: SerialSettings) -> None:
            super().__init__(settings, fakes=fakes_ref)

    import watlowlib.protocol.modbus.transport as modbus_transport_mod

    monkeypatch.setattr(modbus_transport_mod, "ModbusBusTransport", _PatchedBus)

    # Wrap make_protocol_client to record which address was opened so
    # tests can assert the sweep walked the expected address range.
    import watlowlib.protocol.client as client_mod

    real_make = client_mod.make_protocol_client

    def _record_then_make(kind: ProtocolKind, transport: Any, *, address: int) -> Any:
        fakes.seen_addresses.append(address)
        return real_make(kind, transport, address=address)

    monkeypatch.setattr(factory, "make_protocol_client", _record_then_make)
    monkeypatch.setattr(discovery, "make_protocol_client", _record_then_make)


# --- Std Bus sweep ---------------------------------------------------


@pytest.mark.anyio
async def test_sweep_stdbus_yields_one_row_per_address(
    monkeypatch: pytest.MonkeyPatch,
    anyio_backend: object,
) -> None:
    _ = anyio_backend
    # Only address 1 (MAC 0x10) responds.
    fakes = _SweepFakes(
        stdbus_scripts={1: FakeTransport(_build_stdbus_script(mac=addr_to_mac(1)))},
        modbus_scripts={},
        seen_addresses=[],
    )
    _patch_factory(monkeypatch, fakes)

    rows: list[DiscoveryResult] = [
        row async for row in sweep_stdbus("/dev/fake", addresses=[1, 2, 3])
    ]

    assert len(rows) == 3
    # All rows tagged STDBUS, in input order.
    assert [r.address for r in rows] == [1, 2, 3]
    assert all(r.protocol is ProtocolKind.STDBUS for r in rows)

    # Address 1 successful — info populated, error None.
    assert rows[0].info is not None
    assert rows[0].info.part_number.raw.startswith("PM3")
    assert rows[0].error is None

    # Addresses 2 / 3 silent — info demoted to None. No error is
    # surfaced because :meth:`Controller.identify` is tolerant of
    # absent identity parameters; callers distinguish responsive vs
    # absent rows by ``info is None``.
    for r in rows[1:]:
        assert r.info is None


# --- Modbus sweep ---------------------------------------------------


@pytest.mark.anyio
async def test_sweep_modbus_yields_one_row_per_address(
    monkeypatch: pytest.MonkeyPatch,
    anyio_backend: object,
) -> None:
    _ = anyio_backend
    # Build a slave that responds to the identify() reads on Modbus.
    # PM hardware_id is 2 regs at relative_addr=0; firmware_id is 2
    # regs at relative_addr=2; part_number is 8 regs at the registry
    # value.
    from watlowlib import PARAMETERS
    from watlowlib.transport.fake import FakeSlave

    hw_addr = PARAMETERS.resolve("hardware_id").relative_addr
    fw_addr = PARAMETERS.resolve("firmware_id").relative_addr
    part_addr = PARAMETERS.resolve("part_number").relative_addr

    # Build a slave answering at address 5; addresses 4 and 6 stay
    # silent. Part-number string is "PM3R1CA-AAAAAAA" packed big-endian.
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

    fakes = _SweepFakes(
        stdbus_scripts={},
        modbus_scripts={5: slave},
        seen_addresses=[],
    )
    _patch_factory(monkeypatch, fakes)

    settings = SerialSettings(port="/dev/fake", baudrate=9600, parity=Parity.EVEN)
    rows: list[DiscoveryResult] = [
        row
        async for row in sweep_modbus(
            "/dev/fake",
            addresses=[4, 5, 6],
            serial_settings=settings,
        )
    ]

    assert len(rows) == 3
    assert [r.address for r in rows] == [4, 5, 6]
    assert all(r.protocol is ProtocolKind.MODBUS_RTU for r in rows)

    # Address 5 succeeded — part-number decoded.
    hit = rows[1]
    assert hit.info is not None
    assert hit.info.part_number.raw.startswith("PM3R1CA")
    assert hit.info.protocol is ProtocolKind.MODBUS_RTU

    # 4 and 6 silent.
    assert rows[0].info is None
    assert rows[2].info is None


@pytest.mark.anyio
async def test_sweep_stdbus_default_range_is_one_through_sixteen(
    monkeypatch: pytest.MonkeyPatch,
    anyio_backend: object,
) -> None:
    _ = anyio_backend
    fakes = _SweepFakes(stdbus_scripts={}, modbus_scripts={}, seen_addresses=[])
    _patch_factory(monkeypatch, fakes)

    addresses_seen = [row.address async for row in sweep_stdbus("/dev/fake")]
    assert addresses_seen == list(range(1, 17))


@pytest.mark.anyio
async def test_sweep_modbus_custom_range(
    monkeypatch: pytest.MonkeyPatch,
    anyio_backend: object,
) -> None:
    _ = anyio_backend
    fakes = _SweepFakes(stdbus_scripts={}, modbus_scripts={}, seen_addresses=[])
    _patch_factory(monkeypatch, fakes)

    rows = [row async for row in sweep_modbus("/dev/fake", addresses=range(100, 103))]
    assert [r.address for r in rows] == [100, 101, 102]
    # Silent addresses → info=None (identify is tolerant; the demotion
    # at the discovery layer turns "every-field-None DeviceInfo" into
    # ``info=None``).
    assert all(r.info is None for r in rows)
