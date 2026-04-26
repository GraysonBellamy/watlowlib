# watlowlib

Async-first Python driver for [Watlow](https://www.watlow.com/)
temperature controllers over RS-232 / EIA-485. Speaks both wire
protocols Watlow controllers expose — **Standard Bus** (BACnet MS/TP
outer framing + a small Watlow attribute service) and **Modbus RTU**
(via the in-house [`anymodbus`](https://github.com/GraysonBellamy/anymodbus))
— behind a single semantic `Controller` API that decodes to the same
typed `Reading` either way.

This site is the reference for the **v1** design. The authoritative
architectural document lives at [Design](design.md); every design
decision in the library should be traceable to a section there. Built
as a sibling to [`alicatlib`](https://github.com/GraysonBellamy/alicatlib)
and [`sartoriuslib`](https://github.com/GraysonBellamy/sartoriuslib):
the same async core, sync facade, multi-device manager, fake
transport, acquisition helpers, typed models, pluggable sinks, and
explicit safety gates.

## Where to start

- [Installation](installation.md)
- [Async quickstart](quickstart-async.md) — the canonical surface
- [Sync quickstart](quickstart-sync.md) — for scripts and notebooks
- [Controllers](devices.md) — `Controller`, families, capability flags
- [Commands](commands.md) — the command surface, safety tiers, protocol-variant dispatch
- [Parameters](parameters.md) — the cross-protocol parameter registry
- [Streaming](streaming.md) — `record(...)`, sinks, backpressure
- [Logging and acquisition](logging.md) — recorder, sinks, structured log events
- [Safety](safety.md) — destructive operations and `confirm=True`
- [Testing](testing.md) — `FakeTransport`, fixtures, hardware tiers
- [Troubleshooting](troubleshooting.md) — first-contact, typed errors, diagnostics
- Wire protocols: [Standard Bus](protocol-stdbus.md) /
  [Standard Bus findings](protocol-stdbus-findings.md) /
  [Modbus RTU](protocol-modbus.md)

## Status

Alpha. The library ships the full transport (real + fake), both
protocol clients (Standard Bus and Modbus RTU), the `Controller`
facade, multi-port `WatlowManager`, recorder and `record(...)` helper,
all first-party sinks (CSV, JSONL, SQLite, InMemory in the base
install; Parquet, Postgres behind extras), the sync facade,
fixture-based testing utilities, and the stable `watlow-*` CLI plus the
`watlow-diag` reverse-engineering namespace. The public architecture
is frozen; documentation completion and broader hardware coverage are
in progress. See [Design](design.md) for forward work.

## License

MIT. See [LICENSE](https://github.com/GraysonBellamy/watlowlib/blob/main/LICENSE).
