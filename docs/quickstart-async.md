# Quickstart — async

The async core is `open_device`, `Controller`, parameter read / write,
and identify. The example below uses `FakeTransport` so it runs without
hardware. Replace `open_controller(transport, ...)` with
`open_device("/dev/ttyUSB0", protocol=ProtocolKind.STDBUS, address=1)`
to talk to a live PM3.

## Read PV via `FakeTransport`

```python
import anyio

from watlowlib import (
    FakeTransport,
    ProtocolKind,
    SerialSettings,
    open_controller,
)

# Captured PM3 round-trip: read PV (parameter 4001) at MAC 0x10
REQ_READ_PV = bytes.fromhex("55 FF 05 10 00 00 06 E8 01 03 01 04 01 01 E3 99")
RSP_READ_PV = bytes.fromhex(
    "55 FF 06 00 10 00 0B 88 02 03 01 04 01 01 08 45 1E 3C D4 A7 28"
)


async def main() -> None:
    transport = FakeTransport({REQ_READ_PV: RSP_READ_PV})
    controller = await open_controller(
        transport,
        protocol=ProtocolKind.STDBUS,
        address=1,
        serial_settings=SerialSettings(port="fake://demo"),
    )
    async with controller as ctl:
        reading = await ctl.read_pv()
        print(reading)
        # → Reading(value=2531.78..., unit=None, ..., protocol=ProtocolKind.STDBUS)


anyio.run(main)
```

## Live serial — replace one line

```python
from watlowlib import ProtocolKind, open_device

controller = await open_device(
    "/dev/ttyUSB0",
    protocol=ProtocolKind.STDBUS,
    address=1,
)
async with controller as ctl:
    info = await ctl.identify()
    pv = await ctl.read_pv()
    await ctl.set_setpoint(75.0, confirm=True)
```

## Persistent writes need `confirm=True`

`SafetyTier.PERSISTENT` parameters (RW / RWE / RWES) require an
explicit `confirm=True` at the facade. The session raises
`WatlowConfirmationRequiredError` *before* any I/O if the gate is
missing — so a forgotten `confirm` never reaches the controller.

```python
await ctl.set_setpoint(75.0)              # WatlowConfirmationRequiredError
await ctl.set_setpoint(75.0, confirm=True)  # writes; returns Reading echo
```

## Modbus RTU

The same `read_parameter("setpoint")` works against a controller
switched to Modbus front-panel mode with no user-facing code changes —
just pass `protocol=ProtocolKind.MODBUS_RTU` at open time.
