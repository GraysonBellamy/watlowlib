# Sync quickstart

The async core is canonical (see [Async quickstart](quickstart-async.md)).
The sync facade — [`watlowlib.sync`](../src/watlowlib/sync/__init__.py) —
wraps it through a per-context `BlockingPortal` for scripts, notebooks,
and REPL sessions. Every async method has a sync parity. See
[Design](design.md) §6.

## Single controller

```python
from watlowlib import ProtocolKind
from watlowlib.sync import Watlow

with Watlow.open(
    "/dev/ttyUSB0",
    protocol=ProtocolKind.STDBUS,
    address=1,
) as ctl:
    pv = ctl.read_pv()
    print(pv.value, pv.unit)
    ctl.set_setpoint(75.0, confirm=True)
```

[`Watlow.open`](../src/watlowlib/sync/controller.py) is a context
manager that returns a [`SyncController`](../src/watlowlib/sync/controller.py)
wrapping the async [`Controller`](../src/watlowlib/devices/controller.py).
Same parameters as [`open_device`](../src/watlowlib/devices/factory.py) —
`port`, `protocol`, `address`, `serial_settings` — plus an optional
`portal=` for sharing event loops.

## Multi-controller acquisition

```python
from watlowlib.sync import (
    SyncCsvSink,
    SyncWatlowManager,
    pipe,
    record,
)

with SyncWatlowManager() as mgr:
    mgr.add("oven_top", "/dev/ttyUSB0", address=1)
    mgr.add("oven_bot", "/dev/ttyUSB0", address=2)
    mgr.add("retort", "/dev/ttyUSB1", address=1)
    with (
        record(
            mgr,
            parameters=["process_value", "setpoint"],
            rate_hz=2.0,
            duration=300.0,
        ) as stream,
        SyncCsvSink("run.csv") as sink,
    ):
        summary = pipe(stream, sink, batch_size=64, flush_interval=1.0)
    print(summary.samples_emitted, "samples written")
```

[`SyncWatlowManager`](../src/watlowlib/sync/manager.py) is a plain
context manager that owns the shared portal and the wrapped async
[`WatlowManager`](../src/watlowlib/manager.py). Port canonicalisation,
per-port locking, and ref-counted client sharing are the manager's
job, not the caller's.

[`record()`](../src/watlowlib/sync/recording.py) and
[`pipe()`](../src/watlowlib/sync/recording.py) mirror their async
counterparts; the yielded `stream` is a blocking iterator of `Sample`
batches. Drift-free absolute-target scheduling works the same way as
the async recorder — see [Logging and acquisition](logging.md).

## Discovery

```python
from watlowlib import find_devices
from watlowlib.sync import SyncPortal

with SyncPortal() as portal:
    results = portal.call(find_devices, ports=["/dev/ttyUSB0"])
    for row in results:
        if row.ok:
            print(row.port, row.baudrate, row.address,
                  row.info.part_number.raw)
```

[`find_devices()`](../src/watlowlib/devices/discovery.py) probes
`ports × baudrates × protocols × addresses` and returns one
[`FindResult`](api/devices.md) per attempt. With no `ports` argument
it enumerates every visible serial port via `anyserial`; defaults
hit address `1` at bauds `38400 / 19200 / 9600` on both Watlow
protocols. The sync facade exposes discovery through a portal rather
than a dedicated wrapper because port scanning is rarely a tight
loop. See [Troubleshooting](troubleshooting.md).

## Persistent writes need `confirm=True`

```python
ctl.set_setpoint(75.0)               # WatlowConfirmationRequiredError
ctl.set_setpoint(75.0, confirm=True)  # writes; returns Reading echo
```

`SafetyTier.PERSISTENT` parameters require an explicit `confirm=True`
at the facade. The session raises `WatlowConfirmationRequiredError`
*before* any I/O if the gate is missing. See [Safety](safety.md).

## Using a shared portal

The throwaway-portal default is right for one-off scripts. For code
that holds both a manager and standalone sinks, share a portal so they
run on the same event loop:

```python
from watlowlib.sync import (
    SyncPortal,
    SyncSqliteSink,
    SyncWatlowManager,
    pipe,
    record,
)

with SyncPortal() as portal:
    with SyncWatlowManager(portal=portal) as mgr:
        mgr.add("oven_top", "/dev/ttyUSB0", address=1)
        with (
            record(mgr, parameters=["process_value"], rate_hz=2.0, duration=60.0, portal=portal) as stream,
            SyncSqliteSink("run.sqlite", portal=portal) as sink,
        ):
            pipe(stream, sink, portal=portal)
```

Mixing portals works but costs an extra event-loop hop per method
call. Share when performance matters; don't bother for one-off runs.

## See also

- [Installation](installation.md) — core install and extras.
- [Async quickstart](quickstart-async.md) — the canonical surface.
- [Controllers](devices.md) — `Controller`, families, capability flags.
- [Logging and acquisition](logging.md) — recorder, sinks, `pipe()`.
- [Safety](safety.md) — destructive commands and `confirm=True`.
