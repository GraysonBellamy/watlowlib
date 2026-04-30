# Streaming and sinks

`watlowlib` records device polls into time-series sinks through three
small primitives:

- `WatlowManager` — owns a fleet of `Controller`s; keys them by name
  and serialises I/O on each port's shared client lock.
- `record(...)` — an async context manager that drives a `PollSource`
  (a `Controller` or a `WatlowManager`) at an absolute-target cadence
  and emits per-tick `Sample` batches into a receive stream.
- `pipe(stream, sink)` — drains the receive stream into any
  `SampleSink`. In-tree sinks: `InMemorySink`, `JsonlSink`, `CsvSink`,
  `SqliteSink`. Heavier backends (parquet, postgres) sit behind extras.

The row schema is **long-format**: one row per (device, parameter,
instance) read. A `Sample` carries:

```
device, address, protocol, parameter, parameter_id, instance,
value, unit, requested_at, received_at, midpoint_at, latency_s, raw
```

Tabular sinks drop `raw` (binary; awkward in CSV / SQLite) and stringify
`protocol`. The same key set fits an Alicat-shaped row, so a single
SQLite file can hold mixed-vendor recordings — see the
[mixed-vendor example](../examples/mixed_watlow_alicat_sqlite.py).

## Solo controller

```python
import anyio

from watlowlib import (
    InMemorySink,
    ProtocolKind,
    open_device,
    pipe,
    record,
)


async def main() -> None:
    async with await open_device(
        "/dev/ttyUSB0",
        protocol=ProtocolKind.STDBUS,
        address=1,
    ) as ctl:
        async with InMemorySink() as sink:
            async with record(
                ctl,
                parameters=["process_value", "setpoint"],
                rate_hz=2.0,
                duration=10.0,
            ) as stream:
                await pipe(stream, sink, batch_size=8)
        for sample in sink.samples:
            print(sample.parameter, sample.value)


anyio.run(main)
```

A `Controller` satisfies `PollSource` directly via
`Controller.poll_many(...)`. `Controller.poll()` is the no-argument PV
snapshot helper; use `poll_many()` for acquisition batches.

## Multi-controller manager

```python
from watlowlib import SqliteSink, WatlowManager, pipe, record


async def main() -> None:
    async with WatlowManager() as mgr:
        await mgr.add("oven_top", "/dev/ttyUSB0", address=1)
        await mgr.add("oven_bot", "/dev/ttyUSB0", address=2)
        await mgr.add("retort", "/dev/ttyUSB1", address=1)

        async with SqliteSink("run.sqlite") as sink:
            async with record(
                mgr,
                parameters=["process_value", "setpoint"],
                rate_hz=2.0,
                duration=300.0,
            ) as stream:
                await pipe(stream, sink, batch_size=64, flush_interval=1.0)
```

The manager:

- locks each port to one wire protocol (mixing Std Bus and Modbus on
  one RS-485 segment raises `WatlowConfigurationError`),
- shares one `ProtocolClient` per port (one lock = serialised I/O on
  that bus, regardless of how many controllers are addressed there),
- runs cross-port polls concurrently via `anyio.create_task_group()`,
- ref-counts shared clients so `remove(name)` only closes the transport
  on the last controller using it.

## Backpressure

`record(..., overflow=...)` selects what happens when the consumer
falls behind:

- `BLOCK` (default) — producer awaits the consumer. Effective rate
  drops to the consumer's drain rate; nothing is silently dropped.
- `DROP_NEWEST` — drop the batch about to be enqueued; counted as
  `samples_late` in the run summary.
- `DROP_OLDEST` — evict the oldest queued batch and enqueue the newest;
  useful for live dashboards where freshness matters more than keeping
  every historical tick.

The recorder logs its run summary on context-manager exit. `pipe()`
returns an `AcquisitionSummary` from the sink side with
`samples_emitted` set to the number of samples written.

## Cross-vendor SQLite

`SqliteSink` defaults to journal mode WAL, `synchronous=NORMAL`, and
one `BEGIN IMMEDIATE` … `COMMIT` per `write_many` call. Schema is
locked on the first batch — later samples carrying an unknown column
are dropped with a one-shot WARN log rather than reshaping the file.

The Watlow row schema is shared with the alicat library: see
[`examples/mixed_watlow_alicat_sqlite.py`](../examples/mixed_watlow_alicat_sqlite.py)
for a runnable demonstration. Plain SQL aggregates work across
vendors:

```sql
SELECT device, parameter, AVG(value) FROM samples
GROUP BY device, parameter
ORDER BY device, parameter;
```

## Pre-built controllers

`WatlowManager.add(name, controller)` accepts a pre-built `Controller`
(opened via `open_device(...)`). The manager only tracks the name
mapping in that case — the caller retains lifecycle ownership of the
transport and the controller.
