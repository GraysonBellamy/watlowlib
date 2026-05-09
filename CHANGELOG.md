# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

### Added

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
