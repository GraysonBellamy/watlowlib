# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.7.0] — 2026-05-30

### Added (Watlow Series SD support)

- **`watlowlib.DeviceProfile`** — first-class device type bundling a
  family, parameter registry, default protocol + serial framing, and an
  identity strategy. Two profiles ship: **`EZZONE_PROFILE`** (the
  reference EZ-ZONE PM; preserves all prior behaviour) and
  **`SERIES_SD_PROFILE`** (Watlow Series SD over Modbus RTU). The full
  set is **`DEVICE_PROFILES`**. `IdentifyStrategy` is the `Protocol`
  identity functions implement.
- **`watlowlib.SD_PARAMETERS`** — parameter registry for the Series SD,
  loaded from the new bundled `data/sd_parameters.json` (core Home /
  Operations registers; verified against `sd_manual.txt` rev F and the
  extracted parameter tables). Process value / setpoint scale ÷1000;
  power / percent ÷100; temperatures are °F over the wire by default.
- **`open_device(..., profile=EZZONE_PROFILE)`** — the device type is
  now selected by profile. `protocol=None` (the new default) adopts the
  profile's factory protocol (Std Bus for PM, Modbus RTU for SD), and
  `serial_settings=None` adopts its factory framing (PM Std Bus 38400
  8-N-1; SD Modbus 9600 8-N-1). `WatlowController.open`,
  `WatlowManager.add`, `SyncWatlowManager.add`, and the
  `watlowlib.testing` seam (`open_test_controller`,
  `controller_from_fixture`) all gain the same `profile=` argument.
- **`Controller.set_persistent_writes(enabled, *, confirm=True)`** —
  toggles Series SD register 17 (NV-memory write enable). Write `False`
  before a burst of setpoint writes to keep them in RAM and spare the
  EEPROM (the SD resets the register to `1` on every power cycle).
- **`find_devices(..., profiles=DEVICE_PROFILES)`** — profile-driven
  discovery. Each profile contributes its own protocol, factory framing
  (so the SD's 8-N-1 is probed, not the PM Modbus 8-E-1), registry, and
  identity strategy, so a scan can surface both PM and SD controllers.
- **`ControllerFamily.SD`** and `classify_family("SD…") → SD`.
- **`DataType.S16`** — signed 16-bit single-register wire type (Series
  SD power / percent registers), with a guarded engineering-unit
  `ParameterSpec.scale` applied on the Modbus read/write path only
  (Std Bus PM reads stay unscaled, integers stay integers).

### Changed

- **`Session(profile=…)`** replaces the separate `registry=` / `family=`
  constructor arguments; `session.registry` / `session.family` remain as
  read-only delegating properties, plus a new `session.profile`.
- **`Controller.identify()`** is now device-neutral and delegates to the
  bound profile's identity strategy (PM logic unchanged; SD reads its
  numeric identity registers 10/11/13 + serial 7-8 + units reg 18).
- **`find_devices` / `addr_to_mac`** — an out-of-range bus address now
  yields a typed `ok=False` `DiscoveryResult` (`WatlowConfigurationError`)
  instead of aborting the whole scan; `addr_to_mac` raises
  `WatlowValidationError` rather than a bare `ValueError`.

### Fixed

- The Modbus `STRING` decode path now wraps `anymodbus`'s strict-ASCII
  `UnicodeDecodeError` as a typed `WatlowProtocolError`, so a
  foreign / wrong-protocol device can no longer abort a discovery scan.
- Removed the dead `DataType.INT16 = 0x0F` alias of `PACKED` (it decoded
  *unsigned* — a trap for "signed 16-bit").
- `ParameterRegistry` now rebinds specs via `dataclasses.replace`, so a
  new `ParameterSpec` field can never be silently dropped.

## [0.6.0] — 2026-05-15

### Added (Unified Device-Library API)

- **`watlowlib.PollSourceAdapter`** — wraps one `Controller` as a
  named `PollSource`. Implements
  `poll_many(parameters, *, names=None, instances=(1,))
  -> Sequence[Sample]` and relabels each emitted `Sample.device` to
  the caller-provided name via `dataclasses.replace`. Shipped at the
  same name (different signature) as the equivalent classes in
  `alicatlib`, `sartoriuslib`, and `nidaqlib` so consumers (capa, etc.)
  import the same shape from every device library.
- **`watlowlib.Recording[T]`** — frozen container yielded by
  `record(...)`. Exposes `.stream` (async iterator of per-tick
  batches), `.summary` (live `AcquisitionSummary`), and `.rate_hz`.
  Cross-library shape; consumers branch on the payload type per lib
  (watlow: `Recording[Sequence[Sample]]`).
- **`watlowlib.DeviceSnapshot` / `watlowlib.WatlowDeviceSnapshot`** —
  I/O-free identity snapshot built from cached `DeviceInfo` + session
  counters. Returned by new `Controller.snapshot()` /
  `SyncController.snapshot()`. Includes `family`, `capabilities`,
  and an `availability_summary` mapping of UNSUPPORTED commands.
- **`watlowlib.units.to_pint(unit)`** — maps a `Unit` (or alias
  string) to a pint-compatible unit string (`"degC"` / `"degF"` /
  `"percent"`). `pint` is **not** a runtime dependency — `to_pint`
  returns plain strings. Moves the in-house mapping out of capa's
  adapter layer.
- **`DeviceResult.success(value)` / `DeviceResult.failure(error)`**
  classmethod factories (kwarg construction still works).
- **`open_device(..., identify=True)`** by default — runs
  `Controller.identify()` after the transport opens so
  `Controller.snapshot()` renders without further wire I/O. Pass
  `identify=False` for fast-path opens.
- **`watlowlib.testing.open_test_controller(transport, ...)`** —
  public testing helper that builds an opened `Controller` over a
  `FakeTransport` for downstream test suites. Replaces the now-
  private factory entry point.
- **`Session.recoverable_error_count`** — public counter incremented
  when the session swallows-and-retries a transient transport error.
  Wired but dormant — `WatlowTransientTransportError` is deferred per
  the spec; the field stays at 0 unless a future transient class
  surfaces.
- **`Session.last_error` / `Session.availability_summary()`** —
  accessors that feed `WatlowDeviceSnapshot`.
- **`AcquisitionSummary` is now mutable.** Counters update in place
  during a run; consumers (TUIs, dashboards) read live progress
  through `recording.summary`. `finished_at` is `None` while running
  and set on context-manager exit.

### Changed (BREAKING — Unified Device-Library API)

- **`open_controller` removed from the public API.** Use
  `open_device(...)` for production or
  `watlowlib.testing.open_test_controller(transport, ...)` for tests.
  The factory body lives at `watlowlib.devices.factory._open_controller`
  (module-private).
- **`FindResult` renamed to `DiscoveryResult`** with reshaped fields
  per the unified spec:
  - `info` → `device_info`
  - `error` typed as `WatlowError | None` (was `object | None`)
  - added `elapsed_s: float`
  - `address` widened to `str | int | None` for cross-library
    compatibility (watlow still passes `int` in practice).
- **`Sample` timestamp rename.** `monotonic_ns` → `t_mono_ns`;
  `midpoint_at` → `t_utc`; new optional `t_midpoint_mono_ns: int | None`
  for sensors with integration windows. `requested_at` /
  `received_at` / `latency_s` retained as I/O provenance.
- **`record()` yields a `Recording[Sequence[Sample]]`** instead of a
  bare receive stream. Migration: `as stream:` → `as recording:` and
  `async for batch in stream` → `async for batch in recording.stream`.
- **`AcquisitionSummary` is mutable** (was `frozen=True`). The
  recorder is the sole writer; consumers treat it as read-only.
- **`sample_to_row` row keys** updated to match the new field set:
  `monotonic_ns`/`midpoint_at` columns replaced by `t_mono_ns` /
  `t_utc`.
- **Sync facade**: `record(...)` now yields a `SyncRecording`
  (`.stream`, `.summary`, `.rate_hz`); `SyncController.snapshot()`
  added.

### Removed (BREAKING)

- `watlowlib.open_controller` — see `open_device` / `open_test_controller`.
- `watlowlib.FindResult` — see `DiscoveryResult`.
- `Sample.monotonic_ns` / `Sample.midpoint_at` — see `t_mono_ns` /
  `t_utc`.

### Notes

- `WatlowTransientTransportError` is **deferred** per the unified
  spec §F. No cold-open race has been observed in watlow today; the
  typed transient stays out of the public surface until evidence
  surfaces.
- `Controller.expected_rate_hz` is **not added** — the recorder rate
  lives on `Recording.rate_hz` per §I.

---

## [0.5.0] — 2026-05-14

### Added

- **`watlowlib.find_devices()`** — port-scan discovery helper that
  walks the cartesian product of `ports × baudrates × protocols ×
  addresses` and returns one `FindResult` per probe attempt. With no
  arguments, scans every visible serial port via
  `anyserial.list_serial_ports()` at bauds `(38400, 19200, 9600)` on
  both Standard Bus and Modbus RTU at address `1` — fast enough
  (~12 s on a four-port rig) for a GUI Discover dialog.
  Read-only by construction (calls only `Controller.identify()`),
  short-circuits per-port on open failure, surfaces
  `WatlowConnectionError` / `WatlowTimeoutError` on `FindResult.error`
  rather than raising. Mirrors the
  `alicatlib.find_devices` / `sartoriuslib.discover_port` ecosystem
  contract so the `capa` Setup-editor Discover dialog can wire
  Watlow alongside the other adapters.
- **`watlowlib.FindResult`** — frozen dataclass: `port`, `address`,
  `baudrate` (flat), `protocol`, `ok`, `info`, `error`. A single
  `ok` attribute is the canonical filter for "responsive vs silent /
  errored" rows; populated `info.health` and
  `info.configured_protocol` come along for free (the scan passes
  `query_configured_protocol=True` so GUI rows can show
  `health=ok`).
- Module-level `DEFAULT_DISCOVERY_ADDRESSES` (`(1,)`),
  `DEFAULT_DISCOVERY_BAUDRATES` (`(38400, 19200, 9600)`), and
  `DEFAULT_DISCOVERY_PROTOCOLS` (`(STDBUS, MODBUS_RTU)`) constants
  for callers that override per-rig without hard-coding policy.

### Changed (BREAKING)

- **`watlowlib.sweep_stdbus` / `watlowlib.sweep_modbus` removed.**
  Both single-protocol async-generator sweeps are replaced by the
  single multi-port / multi-baud / multi-protocol `find_devices()`
  API. Drop-in equivalents:
  - `sweep_stdbus(port, addresses=range(1, 17))` →
    `find_devices(ports=[port], protocols=(ProtocolKind.STDBUS,), addresses=range(1, 17))`
  - `sweep_modbus(port, addresses=range(1, 17))` →
    `find_devices(ports=[port], protocols=(ProtocolKind.MODBUS_RTU,), addresses=range(1, 17), baudrates=(9600,))`
- **`DiscoveryResult` replaced by `FindResult`.** The new shape
  flattens `baudrate` (was nested in `serial_settings`), adds an
  explicit `ok: bool` field, and defaults `info` / `error` to
  `None`. `protocol` is now always populated (never `None`) —
  every probe attempt names the protocol it tried.
- **`DEFAULT_STDBUS_RANGE` / `DEFAULT_MODBUS_RANGE` removed.** Both
  defaulted to `1..16`; the replacement default
  `DEFAULT_DISCOVERY_ADDRESSES = (1,)` is narrower on purpose
  because multi-port × multi-baud × multi-protocol scans balloon
  probe count.
- **`watlow-discover` CLI rewritten.** `--port` is now optional and
  repeatable (omit to scan every port via `anyserial`). `--baud` is
  repeatable; default is `(38400, 19200, 9600)`. `--addresses`
  defaults to `1` (was `1..16`). `--protocol` default is now `both`.
  New: `--responsive-only` to suppress silent rows, `--probe-timeout`
  for the per-probe budget. Output rows show port, baudrate, address,
  protocol, and `health=…` on responsive rows.

## [0.4.0]

### Changed (BREAKING)

- **`Reading.unit` / `Sample.unit` for temperature parameters no
  longer derive from parameter 17050.** On at least one PM3 firmware
  revision (id 5678, verified on a PM3C1AJ-AAAAAAA), 17050
  ("Communications - Display Units") is a **label-only** register:
  writing it changes the enum the device reports for 17050, but
  does not change the scale of values exchanged over comms. The
  library previously tagged readings from 17050's value and
  silently mis-tagged °F values as °C on this firmware. Default
  behaviour is now `Reading.unit = None` for temperature reads — an
  honest "I don't know" — unless the caller declares the wire scale
  via the new `assert_wire_temperature_unit=` kwarg (see Added).
  See `docs/devices.md` §Units for the new contract.
- **API renames** (no deprecation shims):
  - `Controller.read_display_units()` → `read_comms_unit_label()`
  - `Controller.set_display_units()` → `set_comms_unit_label()`
  - `Session.display_unit()` → `comms_unit_label()`
  - `Session.invalidate_display_unit()` →
    `invalidate_comms_unit_label()`
  - Sync mirrors (`SyncController.read_display_units` etc.) renamed
    in parity.

  The new names make explicit what the register actually is: a
  label-only inspection facade for parameter 17050. Writing it does
  not change `Reading.unit`.
- `watlowlib.registry.units.resolve_unit(kind, display_unit)`
  parameter renamed to `temperature_unit` for clarity. Behaviour is
  unchanged for the temperature branch.

### Added

- **`assert_wire_temperature_unit=`** kwarg on `open_device`,
  `open_controller`, `WatlowManager.add`,
  `SyncWatlowManager.add`, and `Watlow.open`. The user-supplied,
  externally-verified scale of temperature values on the wire.
  Drives `Reading.unit` / `Sample.unit` for all temperature
  parameters in that session. Accepts a `Unit` or a
  case-insensitive alias (`"C"`, `"F"`, `"celsius"`, `"degF"`,
  `"°C"`, ...); `Unit.PERCENT` is rejected pre-I/O. `None` (the
  default) leaves temperature tags as `None`. Logs a one-shot WARN
  the first time an asserted value feeds a `Reading` so the
  assertion appears plainly in capture logs.
- **`watlow-diag probe-unit`** — read-only diagnostic that infers
  the wire-side temperature scale by comparing a known
  front-panel reading against the comms readback. Emits a
  recommendation for the `assert_wire_temperature_unit=` kwarg.
  Usage: `watlow-diag probe-unit PORT --panel-shows 50
  --panel-unit C`. Supports `--json` for machine-readable output.
- `Session.wire_temperature_unit()` — pure accessor used by the
  reading / sample builders.

### Fixed

- **Recorder lock starvation under command-heavy load.** The per-tick
  `poll_many` batch now acquires the per-port lock once for the
  entire batch instead of N times (one per parameter × instance).
  Concurrent writers on the same port can no longer interleave
  between two reads of the same tick, and tick latency is bounded by
  one queue traversal regardless of the parameter count. The fix is
  unconditional — no opt-in flag — and reuses the existing
  `anyio.Lock` via an owner-check, so `Session.execute` and the
  manager's per-port group all compose under a single lock without
  deadlocking. `WatlowManager.poll_many` likewise acquires the
  shared port lock once around all devices in the same port group.

### Performance

- `AcquisitionSummary.tick_duration_ms_p50` and
  `AcquisitionSummary.tick_duration_ms_p99` — wall-clock per-tick
  duration percentiles, populated by the recorder and emitted in the
  `recorder.stop` log line. Compares directly to `1000 / rate_hz`;
  a large gap between p99 and p50 indicates another task is
  competing for the per-port lock during writes.
- `watlowlib._lock.maybe_acquire(lock)` — internal helper used by
  the session, the streaming poll loop, and the manager port-group
  loop to compose batched acquisition without a parallel `_locked`
  API surface.

### Migration

Downstream code that opened controllers and consumed
`Reading.unit` / `Sample.unit` for temperature parameters must
either:

1. **Recommended:** run `watlow-diag probe-unit PORT --panel-shows
   VALUE --panel-unit UNIT` against each SKU once to determine the
   actual wire scale, then pass
   `assert_wire_temperature_unit=Unit.FAHRENHEIT` (or
   `Unit.CELSIUS`) to `open_device` from then on. Tags now reflect
   the true scale.
2. Accept `Reading.unit = None` and rely on a separately-tracked
   wire-scale constant in downstream code. Safer than the old
   behaviour but pushes unit handling to every consumer.

Any code calling `read_display_units` / `set_display_units` must be
renamed to `read_comms_unit_label` / `set_comms_unit_label`.
Writing 17050 no longer affects `Reading.unit` on any firmware.

## [0.1.0] — initial alpha

Initial alpha release of `watlowlib` — an async-first Python driver
for Watlow temperature controllers over RS-232 / EIA-485, modeled on
the sibling `alicatlib` and `sartoriuslib` libraries. Both wire
protocols (Standard Bus and Modbus RTU) sit behind one
protocol-neutral `Controller` API. See
[docs/design.md](docs/design.md) for the architectural reference.

#### Infrastructure

- `pyproject.toml` (hatchling + hatch-vcs, ruff lint + format, mypy
  strict, pyright strict, pytest + AnyIO, `uv` dependency groups,
  PEP 735) mirroring sartoriuslib.
- `.pre-commit-config.yaml` — ruff, codespell, uv-lock, plus local
  mypy / pyright hooks pinned to the same dep groups CI uses.
- GitHub Actions: `ci.yml` (lint → typecheck → test → build matrix
  across Linux/macOS/Windows × Python 3.13/3.14), `docs.yml` (zensical
  build + GitHub Pages deploy), `release.yml` (PyPI OIDC trusted
  publishing).
- Issue / PR templates with watlow-specific fields (Standard Bus vs
  Modbus RTU, parameter ID, safety tier).
- `zensical.toml` — docs site nav, theme, mkdocstrings handler.
- `.gitignore`, `.gitattributes`, `.editorconfig`, `.python-version`.

#### Standard Bus protocol

- BACnet MS/TP outer framing — `55 FF` preamble, frame-type byte,
  destination/source MAC, 16-bit big-endian payload length, header
  CRC-8, payload, data CRC-16 little-endian. Encode + decode round-trip
  byte-for-byte against captured wire bytes.
- Watlow inner attribute-service payload codec — direction byte,
  function byte (read 0x03 / write 0x04), selector triple
  `(class, member, instance)`, type-tag value layer.
- Type-tag catalog confirmed against a live EZ-ZONE PM3:
  `U8` (0x01), `U16` (0x03), `U32` (0x05), `S32` (0x06),
  `FLOAT` (0x08), `STRING` (0x09), and `PACKED` enum/u16 (0x0F).
- Error-response codec: `NO_SUCH_OBJECT` (0x81),
  `NO_SUCH_ATTRIBUTE` (0x83), `NO_SUCH_INSTANCE` (0x84) → typed
  `WatlowNoSuchObjectError` / `WatlowNoSuchAttributeError` /
  `WatlowNoSuchInstanceError`.
- Address mapping: `addr_to_mac(1..16)` → MS/TP MAC `0x10..0x1F`;
  parameter helpers `split_param` / `join_param`.
- `StdBusProtocolClient` exposing read / write / identify behind the
  `ProtocolClient` Protocol.

#### Modbus RTU protocol

- `ModbusProtocolClient` over the in-house
  [`anymodbus`](https://github.com/GraysonBellamy/anymodbus) package.
- Per-`DataType` codec — `FLOAT`, `S32`, `U32`, `U16`, `U8`, `PACKED`,
  `STRING` — with configurable word order and 125-register PDU bound.
- Exception remap: every Modbus exception code (`0x01`–`0x0B`) lowers
  to a distinct `WatlowModbusError` subclass
  (`IllegalFunction` / `IllegalDataAddress` / `IllegalDataValue` /
  `SlaveFailure` / `Timeout`).

#### Parameter registry, families, capabilities

- `ParameterRegistry` + frozen `ParameterSpec` — every entry carries
  the Standard Bus selector (`class/member/instance`), the Modbus
  register address, data type, units, and RWES safety flag, so
  `read_parameter("setpoint")` lowers to either protocol from one
  shared table.
- `data/pm_parameters.json` — PM-family register list (extracted from
  Watlow's "EZ-ZONE ALL Register List" spreadsheet).
- `data/enumerations.json` — enum value tables (Heat Algorithm,
  Sensor Type, etc.) wired to the registry's `PACKED` decoder.
- `decode_part_number` / `classify_family` — PM/RM/ST/F4T/EZ-ZONE
  Limit part-number classification feeding `Capability` flags
  (`HAS_COOLING`, `HAS_MODBUS`, `HAS_PROFILES`, `HAS_BLUETOOTH`,
  `HAS_ETHERNET`, …) and per-SKU comms-code introspection
  (`pm_comms_supports_modbus`).
- Safety tiering — `RwesFlag` → `SafetyTier`
  (`READ_ONLY` / `STATEFUL` / `PERSISTENT`); persistent writes require
  `confirm=True`.

#### `Controller`, `Session`, `factory.open_device`

- `Controller` facade — `read_pv`, `read_setpoint`, `set_setpoint`,
  `read_parameter`, `write_parameter`, `identify`, `read_loop_state`,
  `read_alarm_state`, multi-loop variants. All methods are
  protocol-neutral and return frozen `Reading` / `LoopState` /
  `DeviceInfo` / `AlarmState` models.
- `Session` — single-flight per-device dispatch with availability
  cache, prior gating, and per-call timeout boundary.
- `open_device(...)` / `open_controller(...)` with
  `ProtocolKind.AUTO` (Standard Bus probe → Modbus RTU probe → fail
  cleanly). Read-only by construction; never sweeps opcodes or
  guesses bauds.
- `sweep_stdbus` / `sweep_modbus` — opt-in address sweeps that open
  the underlying transport once and walk address ranges.

#### Manager, streaming, sinks

- `WatlowManager` — concurrent multi-device manager with port-keyed
  serialization (same-port requests serialize, different ports run in
  parallel) and `ErrorPolicy.RAISE` / `RETURN`.
- `record(source, parameters=..., rate_hz=..., duration=...)` —
  absolute-target poll loop driving one `Controller` or a
  `WatlowManager`. Drift-free cadence, per-tick batches, send/receive
  timing on every `Sample`, and `BLOCK` / `DROP_NEWEST` /
  `DROP_OLDEST` overflow policies.
- Sinks (in-tree): `InMemorySink`, `CsvSink`, `JsonlSink`,
  `SqliteSink`. Optional extras: `ParquetSink` (`watlowlib[parquet]`,
  pyarrow), `PostgresSink` (`watlowlib[postgres]`, asyncpg).
  `SchemaLock` and `pipe(stream, sink)` helpers complete the
  acquisition surface.

#### Sync facade

- `watlowlib.sync.Watlow.open(...)`, `SyncController`,
  `SyncWatlowManager`, `record`, and per-sink synchronous adapters —
  each backed by a `SyncPortal` so every async method has a sync
  parity. Lets scripts, notebooks, and REPLs use the library without
  touching `anyio.run`.

#### Maintenance

- `change_baud`, `change_modbus_address`, `change_stdbus_address`,
  `change_protocol_mode` — parameter-write helpers gated behind
  `confirm=True`, with SKU-comms-code pre-flight checks where
  applicable.

#### CLIs

- `watlow-read` — one or more decoded parameter reads over either protocol.
- `watlow-discover` — address sweep + identify.
- `watlow-decode` — offline Standard Bus frame decode.
- `watlow-raw` — escape-hatch raw read / write of an arbitrary
  parameter ID.
- `watlow-configure` — parameter-write entry points
  (`change-baud`, `change-modbus-address`,
  `change-stdbus-address`, `change-protocol-mode`).
- `watlow-diag` — diagnostics namespace with `snapshot`, `tap`,
  `stream`, `sweep`, `argfuzz`, `detect-framing` subcommands.
  Destructive subcommands require
  `--i-understand-this-is-destructive`.
- All CLIs accept `--fixture FILE` to drive a scripted
  `FakeTransport`, so end-to-end tests and demos work without
  hardware.

#### Transport

- `Transport` Protocol with `SerialTransport` (over `anyserial`) for
  hardware and `FakeTransport` for tests.
- `SerialSettings` dataclass — port, baud, parity, byte-size,
  stop-bits, timeouts.

#### Tests

- 494 tests across `asyncio`, `asyncio+uvloop`, and `trio` covering
  CRCs, codecs, protocol clients, registry, families, transports,
  the Modbus integration path, controller / session / factory /
  discovery, manager, streaming, sinks, sync facade, and every CLI.
- `tests/fixtures/` — captured wire-byte fixtures from a live PM3
  (`pm3_stdbus_pv_setpoint.jsonl`, `pm3_modbus_pv_setpoint.jsonl`)
  used as goldens for round-trip tests.
- Hardware tests gated behind `WATLOWLIB_ENABLE_STATEFUL_TESTS=1`
  and `WATLOWLIB_ENABLE_DESTRUCTIVE_TESTS=1`.

#### Documentation

- Async + sync quickstarts, devices and capabilities, commands and
  safety tiers, parameter registry, streaming, logging, Standard Bus
  reference + bench findings, Modbus RTU mapping, testing,
  troubleshooting, and architecture notes.
- mkdocstrings-driven API reference for every public subpackage,
  built with zensical and deployed to GitHub Pages.
