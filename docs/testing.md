# Testing

`watlowlib` ships first-class testing utilities so callers can build
deterministic unit tests against the full controller facade — no
hardware required. Everything in this guide lives in
[`watlowlib.testing`](api/testing.md), a public module re-exported
from the top-level package as `from watlowlib.testing import ...`.

See [Design](design.md) §6 (testing seam).

## Running the test suite

```bash
# Fast unit tests — no hardware, no slow markers.
pytest

# With coverage.
pytest --cov=watlowlib --cov-report=term-missing
```

Async tests use [AnyIO's pytest plugin](https://anyio.readthedocs.io/en/stable/testing.html);
mark coroutine tests with `@pytest.mark.anyio`. The suite parametrises
`anyio_backend` to cover both `asyncio` and `trio` (install with
`pip install 'watlowlib[trio]'`).

## Hardware test tiers

The suite uses three pytest markers for hardware-dependent tests. All
three are excluded from the default run; opt in with the matching env
vars.

| Marker                  | What it does                                                  | Opt-in |
| ----------------------- | ------------------------------------------------------------- | ------ |
| `hardware`              | Read-only against a connected controller (identify, read PV, sweep). | `WATLOWLIB_TEST_PORT=/dev/ttyUSB0` |
| `hardware_stateful`     | Changes device state (parameter writes).                      | `WATLOWLIB_ENABLE_STATEFUL_TESTS=1` |
| `hardware_destructive`  | Destructive ops (baud / address change, protocol switch).     | `WATLOWLIB_ENABLE_DESTRUCTIVE_TESTS=1` |

## `FakeTransport` — the canonical test double

[`FakeTransport`](../src/watlowlib/transport/fake.py) implements the
full [`Transport`](api/transport.md) protocol against an in-process
script. The script maps write payloads to scripted replies; every
write is recorded so tests can assert exactly what bytes the session
produced.

```python
from watlowlib import FakeTransport, ProtocolKind, open_controller, SerialSettings

REQ_READ_PV = bytes.fromhex("55 FF 05 10 00 00 06 E8 01 03 01 04 01 01 E3 99")
RSP_READ_PV = bytes.fromhex(
    "55 FF 06 00 10 00 0B 88 02 03 01 04 01 01 08 45 1E 3C D4 A7 28"
)

async def test_read_pv_returns_reading() -> None:
    transport = FakeTransport({REQ_READ_PV: RSP_READ_PV})
    controller = await open_controller(
        transport,
        protocol=ProtocolKind.STDBUS,
        address=1,
        serial_settings=SerialSettings(port="fake://demo"),
    )
    async with controller as ctl:
        reading = await ctl.read_pv()
    assert reading.value == pytest.approx(2531.78, rel=1e-3)
    assert transport.writes == [REQ_READ_PV]
```

`FakeTransport` is constructed with:

- `script: Mapping[bytes, bytes | Sequence[bytes] | Callable[[bytes], ...]]` —
  the write→reply table. A script entry can be a static reply, a list
  of frames, or a callable that inspects the actual write.
- `label: str` — identifier used in error contexts.
- `latency_s: float` — per-op artificial delay for simulating a slow
  device.

Pass the transport directly to `open_controller(transport, ...)` —
when the first argument is a `Transport`, the factory skips serial
setup and uses it as-is.

## `FakeSlave` — the Modbus equivalent

The Modbus path lowers to an `anymodbus.Slave` rather than raw bytes,
so the test double for Modbus is [`FakeSlave`](api/testing.md). It
records the lowered method calls (`read_holding_registers`,
`write_registers`, …) and answers reads from a script keyed by
`(method, address)`:

```python
from watlowlib.testing import FakeSlave

slave = FakeSlave()
slave.add_script("read_holding_registers", 360, (17299, 29054))  # PV @ 4001
# ... wire FakeSlave into a ModbusProtocolClient and open_controller ...
```

Writes are silent successes by default and recorded on `slave.writes`
for assertions.

## Fixture files (JSONL)

[`load_fixture(path)`](api/testing.md) parses a JSONL capture into a
[`Fixture`](api/testing.md) object holding both Std Bus and Modbus
rounds:

```jsonl
{"kind": "header", "protocol": "stdbus", "address": 1, "baudrate": 38400, "parity": "none"}
{"protocol": "stdbus", "label": "read_pv", "request_hex": "55FF051000...", "response_hex": "55FF0600..."}
{"protocol": "modbus_rtu", "label": "read_setpoint", "method": "read_holding_registers", "address": 2160, "count": 2, "response_words": [17348, 0]}
```

Rules:

- Lines are JSON objects; one round per line.
- The optional first line (`"kind": "header"`) sets address, baudrate,
  and parity for the whole capture.
- `"protocol": "stdbus"` rounds carry hex strings for request and
  response (whitespace tolerant).
- `"protocol": "modbus_rtu"` rounds carry method + address + count +
  response_words (reads) or method + address + values (writes).
- Malformed input raises `WatlowValidationError` with the file path
  and line number.

The captures shipped under [`captures/`](../captures/) use this format.
Convert a real wire-trace capture by hand or via `watlow-decode` (see
[Troubleshooting](troubleshooting.md)).

## `controller_from_fixture`

For the common case of "open a controller wired to one captured
scenario," use the one-liner:

```python
from watlowlib.testing import controller_from_fixture

async def test_pv_round_trip() -> None:
    async with controller_from_fixture("captures/2026-04-26/02-pv-sp.json") as ctl:
        reading = await ctl.read_pv()
    assert reading.value > 20.0
```

`controller_from_fixture` reads the header, builds the right transport
(`FakeTransport` for Std Bus, `FakeSlave` for Modbus), wires it
through `open_controller`, and returns an opened `Controller` ready
for facade-level assertions.

## See also

- [Testing API](api/testing.md) — full reference.
- [Transport API](api/transport.md) — `Transport` protocol shape.
- [Standard Bus protocol](protocol-stdbus.md) and [Modbus RTU](protocol-modbus.md) — wire layers behind the captures.
- [Design](design.md) §6 — fixture format and test layout.
