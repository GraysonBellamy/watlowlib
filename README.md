# watlowlib

Async-first Python driver for [Watlow](https://www.watlow.com/) temperature
controllers over RS-232 / EIA-485. Speaks both wire protocols Watlow
controllers expose — **Standard Bus** (BACnet MS/TP outer framing + a
small Watlow attribute service) and **Modbus RTU** (via the in-house
[`anymodbus`](https://github.com/GraysonBellamy/anymodbus)) — behind a
single semantic `Controller` API that decodes to the same typed
`Reading` either way.

Built as a sibling to
[`alicatlib`](https://github.com/GraysonBellamy/alicatlib) and
[`sartoriuslib`](https://github.com/GraysonBellamy/sartoriuslib): the
same async core, sync facade, multi-device manager, fake transport,
acquisition helpers, and pluggable sinks.

> **Status: alpha.** Both wire protocols, the `Controller` facade,
> `WatlowManager`, streaming, all sinks, the sync facade, and every CLI
> are implemented and covered by 494 tests across `asyncio`,
> `asyncio+uvloop`, and `trio`. The core has been exercised against a
> live EZ-ZONE PM3 (Standard Bus); the Modbus RTU side has full
> codec / client / integration coverage but limited bench mileage.
> Expect API churn until 1.0. See [docs/design.md](docs/design.md) for
> the architectural reference.

## Goals

- **One protocol-neutral API.** `Controller.read_pv()`,
  `read_setpoint()`, `set_setpoint()`, `read_parameter()` — the same
  calls work over Standard Bus and Modbus RTU, decoding to identical
  `Reading` / `LoopState` / `DeviceInfo` models.
- **Auto-detect.** `open_device(..., protocol=ProtocolKind.AUTO)` does
  Standard Bus probe → Modbus RTU probe → fail clearly. Read-only by
  construction; never sweeps opcodes or guesses bauds.
- **Cross-protocol parameter registry.** The EZ-ZONE register list
  carries both the Standard Bus selector (`class/member/instance`) and
  the Modbus register addresses for every parameter — so
  `read_parameter("setpoint")` lowers to either protocol from one
  shared registry.
- **Typed end to end.** `Unit.FAHRENHEIT`,
  `Capability.HAS_COOLING`, frozen-dataclass responses, `py.typed`,
  `mypy --strict` clean.
- **Typed errors.** `WatlowError` root with structured `ErrorContext`;
  every Standard Bus error code (`0x81` / `0x83` / `0x84`) and every
  Modbus exception (`0x01`–`0x0B`) maps to a distinct exception.
- **Safety gates.** Every `PERSISTENT` write — including setpoint,
  PID, baud, address, and protocol-mode changes — requires
  `confirm=True`.
- **Multi-device.** `WatlowManager` runs many controllers concurrently
  — same-port requests serialize, different ports run in parallel.
- **Acquisition built in.** `record(...)` drives one or many devices on
  an absolute-target cadence into pluggable sinks: `InMemorySink`,
  `CsvSink`, `JsonlSink`, `SqliteSink` in core; `ParquetSink` and
  `PostgresSink` behind extras.
- **Swappable transports.** `SerialTransport` for hardware,
  `FakeTransport` for tests, fixture-backed transports for regression
  goldens.
- **Sync or async.** Async core on `anyio`; complete sync facade at
  `watlowlib.sync` via a blocking portal — every async method has a
  sync parity.
- **CLI tooling.** `watlow-read`, `watlow-discover`, `watlow-raw`,
  `watlow-decode`, `watlow-configure`, and the `watlow-diag`
  reverse-engineering namespace.
- **Lean core.** `pip install watlowlib` pulls in `anyio`, `anyserial`,
  and `anymodbus` — nothing else.

## Install

```bash
pip install watlowlib

# optional sinks
pip install 'watlowlib[parquet]'   # ParquetSink (pyarrow)
pip install 'watlowlib[postgres]'  # PostgresSink (asyncpg)
```

Requires **Python 3.13+**. Linux, macOS, BSD, and Windows are supported
via [`anyserial`](https://pypi.org/project/anyserial/). On Linux, the
user running `watlow-*` needs read/write access to the serial device —
usually by joining the `dialout` group.

## Quickstart (async)

```python
import anyio
from watlowlib import open_device, ProtocolKind

async def main() -> None:
    async with await open_device(
        "/dev/ttyUSB0",
        protocol=ProtocolKind.AUTO,
        address=1,
    ) as ctl:
        info = await ctl.identify()
        print(info.model, info.part_number, info.firmware)
        pv = await ctl.read_pv()
        print(pv.value, pv.unit)
        await ctl.set_setpoint(75.0, confirm=True)

anyio.run(main)
```

Older Watlow controllers (Series 96, F4, early EZ-ZONE) ship from the
factory in **Standard Bus**; on those models Modbus RTU is the
front-panel-opt-in mode. Newer EZ-ZONE firmware can ship in either mode
depending on configuration. The `ProtocolKind.AUTO` detector tries
Standard Bus first then Modbus RTU.

## Quickstart (sync)

```python
from watlowlib.sync import Watlow

with Watlow.open("/dev/ttyUSB0", address=1) as ctl:
    print(ctl.read_pv())
    ctl.set_setpoint(75.0, confirm=True)
```

## Multi-device acquisition

```python
import anyio
from watlowlib import WatlowManager
from watlowlib.streaming import record
from watlowlib.sinks import CsvSink, pipe

async def main() -> None:
    async with WatlowManager() as mgr:
        await mgr.add("oven", "/dev/ttyUSB0", address=1)
        await mgr.add("furnace", "/dev/ttyUSB0", address=2)
        async with (
            record(
                mgr,
                parameters=["process_value", "setpoint"],
                rate_hz=2,
                duration=3600,
            ) as stream,
            CsvSink("run.csv") as sink,
        ):
            await pipe(stream, sink)

anyio.run(main)
```

The recorder runs on an absolute target cadence (drift-free), batches
samples per tick, and reports send/receive timing on every `Sample`.

## Command-line tools

```bash
watlow-discover --port /dev/ttyUSB0 --protocol both # probe + identify
watlow-read --port /dev/ttyUSB0 --protocol auto -p process_value
watlow-decode "55 FF 06 00 10 ..."                  # offline Std Bus decode
watlow-raw --port /dev/ttyUSB0 stdbus --service read --class 4 --member 1
watlow-configure change-protocol-mode /dev/ttyUSB0 --target modbus_rtu --confirm
watlow-diag snapshot /dev/ttyUSB0 --out diag.json  # reverse-engineering aids
```

All CLIs accept `--fixture FILE` to drive a scripted `FakeTransport`, so
end-to-end tests and demos work without hardware.

## Documentation

Full docs live at <https://GraysonBellamy.github.io/watlowlib/>. Useful
entry points:

- [Async quickstart](docs/quickstart-async.md) /
  [Sync quickstart](docs/quickstart-sync.md)
- [Controllers and capabilities](docs/devices.md)
- [Commands and safety tiers](docs/commands.md) /
  [Safety](docs/safety.md)
- [Parameter registry](docs/parameters.md)
- [Streaming and acquisition](docs/streaming.md) /
  [Logging](docs/logging.md)
- [Standard Bus reference](docs/protocol-stdbus.md) /
  [Standard Bus findings](docs/protocol-stdbus-findings.md) /
  [Modbus RTU mapping](docs/protocol-modbus.md)
- [Testing](docs/testing.md) — `FakeTransport`, fixtures, hardware tiers
- [Architecture (design doc)](docs/design.md)

## Development

[`uv`](https://docs.astral.sh/uv/) for env and lock management,
`hatchling` + `hatch-vcs` for builds, `ruff` for format and lint, `mypy
--strict` and `pyright` for types, AnyIO's pytest plugin for the test
suite (parametrised across `asyncio`, `asyncio+uvloop`, and `trio`).

```bash
uv sync --all-extras --dev
uv run pre-commit install
uv run pytest
uv run ruff format --check .
uv run ruff check .
uv run mypy
```

Hardware tests are gated behind `WATLOWLIB_ENABLE_STATEFUL_TESTS=1` and
`WATLOWLIB_ENABLE_DESTRUCTIVE_TESTS=1` and require a connected
controller.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the workflow and
[SECURITY.md](SECURITY.md) for the disclosure policy.

## License

MIT. See [LICENSE](LICENSE).
