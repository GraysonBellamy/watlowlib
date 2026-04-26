"""Tests for :class:`watlowlib.WatlowManager` — port-protocol locking, multi-device polling."""

from __future__ import annotations

import pytest

from watlowlib import (
    FakeTransport,
    ProtocolKind,
    SerialSettings,
    WatlowConfigurationError,
    WatlowConnectionError,
    WatlowManager,
    WatlowValidationError,
    open_controller,
)

# Same captured PM3 PV (4001) round-trip used by ``test_integration.py``.
REQ_READ_PV = bytes.fromhex("55 FF 05 10 00 00 06 E8 01 03 01 04 01 01 E3 99")
RSP_READ_PV = bytes.fromhex(
    "55 FF 06 00 10 00 0B 88 02 03 01 04 01 01 08 45 1E 3C D4 A7 28",
)


def _pv_script() -> dict[bytes, bytes]:
    return {REQ_READ_PV: RSP_READ_PV}


# ---------------------------------------------------------------------------
# Pre-built Controller registration (manager doesn't own lifecycle)
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_manager_add_prebuilt_controller(anyio_backend: object) -> None:
    _ = anyio_backend
    transport = FakeTransport(_pv_script())
    settings = SerialSettings(port="fake://test")
    controller = await open_controller(
        transport,
        protocol=ProtocolKind.STDBUS,
        address=1,
        serial_settings=settings,
    )
    async with controller, WatlowManager() as mgr:
        await mgr.add("ctl1", controller)
        assert mgr.names == ("ctl1",)
        assert mgr.get("ctl1") is controller

        samples = await mgr.poll(["process_value"])
        assert len(samples) == 1
        assert samples[0].device == "ctl1"
        assert samples[0].parameter == "process_value"
        assert samples[0].value is not None


@pytest.mark.anyio
async def test_manager_add_with_transport_source(anyio_backend: object) -> None:
    """``Transport`` source — manager builds the session/client around it."""
    _ = anyio_backend
    transport = FakeTransport(_pv_script())
    await transport.open()
    try:
        async with WatlowManager() as mgr:
            await mgr.add("ctl1", transport, protocol=ProtocolKind.STDBUS, address=1)
            samples = await mgr.poll(["process_value"])
        assert len(samples) == 1
    finally:
        await transport.close()


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_manager_duplicate_name_raises(anyio_backend: object) -> None:
    _ = anyio_backend
    t1 = FakeTransport(_pv_script())
    t2 = FakeTransport(_pv_script())
    await t1.open()
    await t2.open()
    try:
        async with WatlowManager() as mgr:
            await mgr.add("ctl", t1, protocol=ProtocolKind.STDBUS, address=1)
            with pytest.raises(WatlowValidationError, match="already in use"):
                await mgr.add("ctl", t2, protocol=ProtocolKind.STDBUS, address=2)
    finally:
        await t1.close()
        await t2.close()


@pytest.mark.anyio
async def test_manager_unknown_name_in_get(anyio_backend: object) -> None:
    _ = anyio_backend
    async with WatlowManager() as mgr:
        with pytest.raises(WatlowValidationError, match="no controller"):
            mgr.get("nope")


@pytest.mark.anyio
async def test_manager_unknown_name_in_poll(anyio_backend: object) -> None:
    _ = anyio_backend
    async with WatlowManager() as mgr:
        with pytest.raises(WatlowValidationError, match="unknown controller"):
            await mgr.poll(["process_value"], names=["nope"])


@pytest.mark.anyio
async def test_manager_serial_settings_only_with_string_source(
    anyio_backend: object,
) -> None:
    _ = anyio_backend
    transport = FakeTransport(_pv_script())
    await transport.open()
    try:
        async with WatlowManager() as mgr:
            with pytest.raises(WatlowValidationError, match="serial_settings"):
                await mgr.add(
                    "ctl",
                    transport,
                    protocol=ProtocolKind.STDBUS,
                    address=1,
                    serial_settings=SerialSettings(port="fake://other"),
                )
    finally:
        await transport.close()


@pytest.mark.anyio
async def test_manager_close_blocks_subsequent_adds(anyio_backend: object) -> None:
    _ = anyio_backend
    mgr = WatlowManager()
    await mgr.close()
    transport = FakeTransport(_pv_script())
    await transport.open()
    try:
        with pytest.raises(WatlowConnectionError, match="closed"):
            await mgr.add("ctl", transport, protocol=ProtocolKind.STDBUS, address=1)
    finally:
        await transport.close()


# ---------------------------------------------------------------------------
# Per-port protocol lock
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_port_locks_protocol_after_first_add(anyio_backend: object) -> None:
    """Adding a Modbus device to a port already holding a Std Bus device raises.

    Uses the same FakeTransport object as the second source (so the port
    key is identical via ``id``-based keying for non-string sources).
    """
    _ = anyio_backend
    transport = FakeTransport(_pv_script())
    await transport.open()
    try:
        async with WatlowManager() as mgr:
            await mgr.add("ctl1", transport, protocol=ProtocolKind.STDBUS, address=1)
            with pytest.raises(
                WatlowConfigurationError,
                match="cannot mix Std Bus and Modbus",
            ):
                await mgr.add(
                    "ctl2",
                    transport,
                    protocol=ProtocolKind.MODBUS_RTU,
                    address=2,
                )
    finally:
        await transport.close()


@pytest.mark.anyio
async def test_port_lock_allows_same_protocol(anyio_backend: object) -> None:
    """Two Std Bus devices on the same port are fine — same protocol, shared client."""
    _ = anyio_backend
    transport = FakeTransport(_pv_script())
    await transport.open()
    try:
        async with WatlowManager() as mgr:
            ctl1 = await mgr.add(
                "ctl1",
                transport,
                protocol=ProtocolKind.STDBUS,
                address=1,
            )
            ctl2 = await mgr.add(
                "ctl2",
                transport,
                protocol=ProtocolKind.STDBUS,
                address=2,
            )
            # Both controllers share one protocol client (one lock per
            # port). They're distinct Controller / Session objects but
            # the underlying client is the same instance.
            assert ctl1.session.client is ctl2.session.client
    finally:
        await transport.close()


@pytest.mark.anyio
async def test_manager_rejects_auto_protocol(anyio_backend: object) -> None:
    _ = anyio_backend
    transport = FakeTransport(_pv_script())
    await transport.open()
    try:
        async with WatlowManager() as mgr:
            with pytest.raises(WatlowConfigurationError, match="AUTO"):
                await mgr.add(
                    "ctl",
                    transport,
                    protocol=ProtocolKind.AUTO,
                    address=1,
                )
    finally:
        await transport.close()


# ---------------------------------------------------------------------------
# Cross-port concurrency
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_cross_port_polls_dont_serialise(anyio_backend: object) -> None:
    """Two managed controllers on different ports run their polls concurrently.

    Smoke check: with two transports, ``poll()`` returns one sample per
    device, both populated. The actual concurrency contract is delivered
    by the task group; this asserts the result shape and that
    cross-port keys both make it into the result set.

    Both controllers use address=1 because each :class:`FakeTransport`
    models an independent physical bus, so MAC 0x10 (the captured
    request prefix) refers to a different physical device on each
    transport.
    """
    _ = anyio_backend
    t1 = FakeTransport(_pv_script(), label="fake://port1")
    t2 = FakeTransport(_pv_script(), label="fake://port2")
    await t1.open()
    await t2.open()
    try:
        async with WatlowManager() as mgr:
            await mgr.add("ctl1", t1, protocol=ProtocolKind.STDBUS, address=1)
            await mgr.add("ctl2", t2, protocol=ProtocolKind.STDBUS, address=1)
            samples = await mgr.poll(["process_value"])
        names = {s.device for s in samples}
        assert names == {"ctl1", "ctl2"}
    finally:
        await t1.close()
        await t2.close()


@pytest.mark.anyio
async def test_remove_then_close_caller_owned_transport(anyio_backend: object) -> None:
    """Remove + close on a caller-owned transport leaves the transport open.

    Manager only owns transports it built from string sources; for
    duck-typed :class:`Transport` sources the caller keeps lifecycle
    ownership. After the last ``remove()`` the manager disposes the
    shared client but does **not** close the transport.
    """
    _ = anyio_backend
    t1 = FakeTransport(_pv_script())
    await t1.open()
    try:
        async with WatlowManager() as mgr:
            await mgr.add("ctl1", t1, protocol=ProtocolKind.STDBUS, address=1)
            await mgr.add("ctl2", t1, protocol=ProtocolKind.STDBUS, address=2)
            assert sorted(mgr.names) == ["ctl1", "ctl2"]
            await mgr.remove("ctl1")
            assert list(mgr.names) == ["ctl2"]
            assert t1.is_open  # still in use by ctl2
            await mgr.remove("ctl2")
            assert list(mgr.names) == []
        # Manager closed; caller-owned transport stays open.
        assert t1.is_open
    finally:
        await t1.close()


# ---------------------------------------------------------------------------
# execute_each
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_execute_each_collects_per_device_results(anyio_backend: object) -> None:
    _ = anyio_backend
    # Independent FakeTransports — each models a separate physical bus,
    # so address=1 (MAC 0x10) is fine on both.
    t1 = FakeTransport(_pv_script(), label="fake://port1")
    t2 = FakeTransport(_pv_script(), label="fake://port2")
    await t1.open()
    await t2.open()
    try:
        async with WatlowManager() as mgr:
            await mgr.add("ctl1", t1, protocol=ProtocolKind.STDBUS, address=1)
            await mgr.add("ctl2", t2, protocol=ProtocolKind.STDBUS, address=1)
            results = await mgr.execute_each(lambda c: c.read_pv())
        assert set(results.keys()) == {"ctl1", "ctl2"}
        for name, result in results.items():
            assert result.ok, f"{name}: {result.error!r}"
            assert result.protocol is ProtocolKind.STDBUS
    finally:
        await t1.close()
        await t2.close()
