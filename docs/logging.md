# Logging and acquisition

For continuous data capture, `watlowlib` provides a recorder that
samples a controller (or a `WatlowManager` of controllers) at a fixed
rate, plus first-party sinks that persist the resulting `Sample`
stream. The two halves connect through `pipe()`. See
[Design](design.md) §6.

## Recorder

[`record(...)`](../src/watlowlib/streaming/recorder.py) is an async
context manager. It schedules ticks on absolute targets (drift-free)
and yields a stream of `Sample` batches.

```python
import anyio
from watlowlib import WatlowManager, record

async def main() -> None:
    async with WatlowManager() as mgr:
        await mgr.add("oven_top", "/dev/ttyUSB0", address=1)
        await mgr.add("oven_bot", "/dev/ttyUSB0", address=2)
        async with record(
            mgr,
            parameters=["process_value", "setpoint"],
            rate_hz=2.0,
            duration=60.0,
        ) as stream:
            async for batch in stream:
                for sample in batch:
                    print(sample.device, sample.parameter, sample.value)

anyio.run(main)
```

`record(...)` accepts:

- A `Controller` for a single device.
- A `WatlowManager` for multi-device acquisition.
- A custom [`PollSource`](api/streaming.md) — the protocol the recorder
  drives for adapters and tests.

Required: `parameters` (list of canonical names or aliases) and
`rate_hz`. Optional: `duration` (seconds; `None` = unbounded),
`overflow_policy`. See [Design](design.md) §6 for the full scheduling
model.

Watlow polls a *small group* of parameters per device per tick, so a
recorder tick produces N×M samples — one per (device, parameter) pair
that succeeded. The shape is intentionally long-format so a SQLite
file can carry mixed-vendor recordings (Watlow rows union with
[alicatlib](https://github.com/GraysonBellamy/alicatlib) rows under
the same schema).

## `Sample`

[`Sample`](../src/watlowlib/streaming/sample.py) wraps a single
parameter read with timing provenance.

| Field            | Notes |
| ---------------- | ----- |
| `device`         | Manager-assigned name (or controller label for solo recordings). |
| `address`        | Bus address (Std Bus 1..16, Modbus 1..247). |
| `protocol`       | `ProtocolKind` of the active session. |
| `parameter`      | Canonical parameter name (e.g. `process_value`). |
| `parameter_id`   | Registry parameter id. |
| `instance`       | 1-indexed loop / channel selector. |
| `value`          | Decoded scalar, or `None` on sensor-fail / overload. |
| `unit`           | Display string for the unit, or `None` (v1 leaves this `None` for every PM parameter — the registry doesn't carry per-row units yet). |
| `monotonic_ns`   | `time.monotonic_ns` at the read site (drift analysis only). |
| `requested_at`   | UTC `datetime` just before the read leaves the host. |
| `received_at`    | UTC `datetime` just after the reply is decoded. |
| `midpoint_at`    | `(requested_at + received_at) / 2` — preferred timestamp for plots. |
| `latency_s`      | Poll round-trip seconds (precomputed). |
| `raw`            | Wire payload that produced the value. Tabular sinks drop it. |

## `AcquisitionSummary`

[`AcquisitionSummary`](api/streaming.md) is yielded by `record()` on
exit and returned by `pipe()`.

| Field                  | Notes |
| ---------------------- | ----- |
| `started_at`           | Wall-clock at the first scheduled tick. |
| `finished_at`          | Wall-clock at producer shutdown. |
| `samples_emitted`      | Total samples pushed onto the stream (= ticks × parameters × devices on a clean run). |
| `samples_late`         | Samples that missed their target slot or were dropped under the overflow policy. |
| `max_drift_ms`         | Largest observed positive drift of an emitted batch. |
| `target_total_samples` | Scheduled sample count for finite-duration runs, or `None` for open-ended runs. |

## Sinks

A sink persists `Sample`s. The protocol is small:

```python
class SampleSink(Protocol):
    async def open(self) -> None: ...
    async def write_many(self, samples: Sequence[Sample]) -> None: ...
    async def close(self) -> None: ...
    # plus async context-manager dunder methods
```

First-party sinks all conform:

| Sink            | Module                                                              | Backing                  | Needs extra            |
| --------------- | ------------------------------------------------------------------- | ------------------------ | ---------------------- |
| `InMemorySink`  | [sinks/memory.py](../src/watlowlib/sinks/memory.py)                | list (test/debug)        | —                      |
| `CsvSink`       | [sinks/csv.py](../src/watlowlib/sinks/csv.py)                      | stdlib `csv`             | —                      |
| `JsonlSink`     | [sinks/jsonl.py](../src/watlowlib/sinks/jsonl.py)                  | stdlib `json`            | —                      |
| `SqliteSink`    | [sinks/sqlite.py](../src/watlowlib/sinks/sqlite.py)                | stdlib `sqlite3` (WAL)   | —                      |
| `ParquetSink`   | [sinks/parquet.py](../src/watlowlib/sinks/parquet.py)              | `pyarrow`                | `watlowlib[parquet]`   |
| `PostgresSink`  | [sinks/postgres.py](../src/watlowlib/sinks/postgres.py)            | `asyncpg`                | `watlowlib[postgres]`  |

Optional-extra sinks lazy-import their backend; importing
`watlowlib.sinks.parquet` without `pyarrow` installed raises
`WatlowSinkDependencyError` with the install hint, not an opaque
`ImportError`.

## `pipe(stream, sink)`

[`pipe(...)`](../src/watlowlib/sinks/base.py) is the v1 acquisition
glue. It reads sample batches from the recorder, buffers up to
`batch_size` (or `flush_interval` seconds, whichever comes first), and
calls `sink.write_many` to flush. On stream exhaustion it drains any
remaining buffer and returns the `AcquisitionSummary`.

```python
async with (
    record(mgr, parameters=["process_value"], rate_hz=2.0, duration=60.0) as stream,
    SqliteSink("run.db") as sink,
):
    summary = await pipe(stream, sink, batch_size=64, flush_interval=1.0)
print(f"{summary.samples_emitted} samples, {summary.samples_late} late")
```

## Row schema

[`sample_to_row(sample)`](../src/watlowlib/sinks/base.py) is the
canonical flatten. Schema is locked on the first batch so mixed
success / error rows don't reshape the file.

| Column         | Type                | Notes |
| -------------- | ------------------- | ----- |
| `device`       | str                 | Manager-assigned name. |
| `address`      | int                 | Bus address. |
| `protocol`     | str                 | `stdbus` / `modbus_rtu`. |
| `parameter`    | str                 | Canonical parameter name. |
| `parameter_id` | int                 | Registry parameter id. |
| `instance`     | int                 | 1-indexed loop / channel selector. |
| `value`        | float \| int \| str \| null | Coerced from `Sample.value`. Bools become `"true"`/`"false"` strings so SQLite type inference doesn't pin the column to INTEGER. |
| `unit`         | str \| null         | Display unit; v1 emits `None` for every PM parameter. |
| `requested_at` / `received_at` / `midpoint_at` | str (ISO 8601) | Wall-clock timing per read. |
| `latency_s`    | float               | Poll round-trip seconds. |

The `Sample.raw` payload is intentionally **not** in the row — bytes
don't fit cleanly into CSV / JSONL / SQLite affinities. Callers that
need raw consume `Sample` directly via
[`InMemorySink`](api/sinks.md).

The schema is intentionally compatible with
[alicatlib](https://github.com/GraysonBellamy/alicatlib)'s sink schema
where the columns make sense across both libraries — see
[`examples/mixed_watlow_alicat_sqlite.py`](../examples/mixed_watlow_alicat_sqlite.py).

## Backpressure

The recorder has a bounded internal queue. If a slow sink can't keep
up, the recorder drops batches per the configured `OverflowPolicy`:

- `OverflowPolicy.BLOCK` — await the slow consumer (default). Silent
  drops are surprising in a data-acquisition setting, so the recorder
  blocks the producer rather than discarding samples. Effective sample
  rate drops to the consumer's drain rate; `samples_late` accrues
  once the consumer catches up.
- `OverflowPolicy.DROP_NEWEST` — drop the batch about to be enqueued.
  Counted on `samples_late`.

Drops are counted on `AcquisitionSummary.samples_late` and emitted as
structured log events through `_logging.get_logger("streaming")`.

## SQLite specifics

`SqliteSink` defaults to journal mode WAL, `synchronous=NORMAL`, and
one `BEGIN IMMEDIATE` … `COMMIT` per `write_many` call. Schema is
locked on the first batch — later samples carrying an unknown column
are dropped with a one-shot WARN log rather than reshaping the file.

```sql
SELECT device, parameter, AVG(value) FROM samples
GROUP BY device, parameter
ORDER BY device, parameter;
```

The same query works on a file that mixes Watlow and Alicat rows.

## See also

- [Streaming](streaming.md) — solo `Controller` and manager recordings.
- [Controllers](devices.md) — `Controller` and `Reading` shape.
- [Sinks API](api/sinks.md) — full reference.
- [Testing](testing.md) — `FakeTransport` for sink unit tests.
- [Design](design.md) §6 — scheduling, overflow, schema rationale.
