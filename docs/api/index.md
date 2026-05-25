---
description: Auto-generated API reference for watlowlib — every public name from the guide pages and its docstring, rendered via mkdocstrings-python.
---

# API reference

Auto-generated from source docstrings via
[mkdocstrings-python](https://mkdocstrings.github.io/python/). Every
public name on the guide pages ([Controllers](../devices.md),
[Commands](../commands.md), [Parameters](../parameters.md),
[Streaming](../streaming.md), …) links back to the relevant reference
section here.

## Top-level

- [`watlowlib`](watlowlib.md) — top-level re-exports
  (`open_device`, `find_devices`, `WatlowManager`, `record`,
  `Recording`, `PollSourceAdapter`, `DiscoveryResult`,
  `DeviceSnapshot`, `WatlowDeviceSnapshot`, `pipe`, the typed error
  hierarchy, the parameter registry, `ProtocolKind`,
  `ControllerFamily`, `Capability`, `SafetyTier`, …).
- [`watlowlib.units`](units.md) — `to_pint(unit)` lossy mapping to
  pint-compatible unit strings.

## Subpackages

- [`watlowlib.transport`](transport.md) — `Transport` Protocol,
  `SerialTransport`, `FakeTransport`, `SerialSettings`.
- [`watlowlib.protocol`](protocol.md) — `ProtocolKind`,
  `ProtocolClient`, Std Bus and Modbus RTU clients / codecs / tables.
- [`watlowlib.commands`](commands.md) — `Command[Req, Resp]`,
  `StdBusVariant`, `ModbusVariant`, the per-category command
  catalogue.
- [`watlowlib.devices`](devices.md) — `Controller`, `Session`, models
  (`Reading`, `DeviceInfo`, `PartNumber`, `AlarmState`, `LoopState`,
  `DiscoveryResult`, `DeviceSnapshot`, `WatlowDeviceSnapshot`, …),
  `ControllerFamily`, `Capability`, `SafetyTier`, `open_device`,
  discovery helpers.
- [`watlowlib.manager`](manager.md) — `WatlowManager`, `DeviceResult`,
  `ErrorPolicy`.
- [`watlowlib.streaming`](streaming.md) — `Sample`, `record()`,
  `OverflowPolicy`, `AcquisitionSummary`, `PollSource`.
- [`watlowlib.sinks`](sinks.md) — `SampleSink` Protocol, `pipe()`,
  first-party sinks (InMemory / CSV / JSONL / SQLite / Parquet /
  Postgres).
- [`watlowlib.sync`](sync.md) — sync facade over the async core.
- [`watlowlib.registry`](registry.md) — `PARAMETERS`,
  `ParameterRegistry`, `ParameterSpec`, `RwesFlag`, family table,
  enumerations, units.
- [`watlowlib.testing`](testing.md) — `FakeTransport`, `FakeSlave`,
  fixture parsers, `controller_from_fixture`.
- [`watlowlib.errors`](errors.md) — typed exception hierarchy and
  `ErrorContext`.
- [`watlowlib.firmware`](firmware.md) — `FirmwareVersion`.
- [`watlowlib.config`](config.md) — `Defaults`.
- [`watlowlib.maintenance`](maintenance.md) — port-level
  `change_baud`, `change_modbus_address`, `change_stdbus_address`,
  `change_protocol_mode` helpers.
