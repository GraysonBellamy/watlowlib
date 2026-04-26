# Controllers

Watlow controllers all do the same thing — read a process value, drive
a setpoint, run a PID loop — and differ in *how much extra* they can
do, not *what they are*. `watlowlib` exposes a single
[`Controller`](api/devices.md) class for every model. Family is a
discriminator on `DeviceInfo`, capabilities are a flag bitmap derived
from the part number, and family-specific behaviour is dispatched on
capabilities, not by class hierarchy. See [Design](design.md) §5b for
the full rationale.

## One class, many families

[`open_device(...)`](api/devices.md) always returns a `Controller`.
The controller's [`DeviceInfo`](api/devices.md) carries the active wire
protocol, the part-number string read from the device, the family
classification, the loop count, and the capability bitmap decoded from
the part number.

```python
async with await open_device(
    "/dev/ttyUSB0",
    protocol=ProtocolKind.STDBUS,
    address=1,
) as ctl:
    info = await ctl.identify()
    print(info.part_number.raw, info.family, info.protocol)
    print(info.capabilities)
```

## Family classification

Family is decided by the leading characters of the part-number string:

| Prefix  | Family                          | Notes |
| ------- | ------------------------------- | ----- |
| `PM*`   | `ControllerFamily.PM`           | EZ-ZONE PM. Reference family with full part-number decoder (case size, control type, power, output codes, comms options). |
| `RM*`   | `ControllerFamily.RM`           | EZ-ZONE RM. Discriminator only — no per-digit decoder yet. |
| `ST*`   | `ControllerFamily.ST`           | EZ-ZONE ST. Discriminator only. |
| `F4T*`  | `ControllerFamily.F4T`          | F4T. Discriminator only. |
| anything else | `ControllerFamily.UNKNOWN` | First-class case — no priors, every call becomes a live probe. |

[`classify_family(part_number)`](../src/watlowlib/registry/families.py)
is the helper. Classification is case-insensitive and
whitespace-tolerant. The PM decoder also populates a free-form
`PartNumber.details` map (case size, control type, output codes,
options string) — see
[`decode_part_number`](../src/watlowlib/registry/families.py).

!!! note "Standard Bus is the EZ-ZONE PM factory default"
    Every PM ships from the factory in **Standard Bus** at
    **38400 8-N-1**, address 1. Modbus RTU is opt-in and requires
    either a comms-option SKU that includes the Modbus stack or a
    front-panel mode flip on a dual-stack SKU. See
    [Troubleshooting](troubleshooting.md) for first-contact paths and
    SKU caveats.

## Capability flags

[`Capability`](api/devices.md) is a `Flag` enum derived from the parsed
part number — the bits encode SKU facts that don't depend on the
device responding to any particular query.

| Capability        | Source                       | Meaning |
| ----------------- | ---------------------------- | ------- |
| `HAS_MODBUS`      | comms code (position 8) ∈ {1,2,B,D,E,F} | Modbus RTU stack ordered with the SKU. |
| `HAS_BLUETOOTH`   | comms code ∈ {B,E,F,G,H,J,K} | Bluetooth comms ordered. |
| `HAS_ETHERNET`    | comms code ∈ {3,G}           | Ethernet comms ordered. |
| `HAS_COOLING`     | output_2 != 'A'              | Second control output present. |
| `HAS_PROFILES`    | control_type ∈ {R,B,E,N,T}   | Ramp / soak engine. |
| `PROFILE`         | (same as above)              | Family-level profile capability. |
| `LIMIT`           | family / control_type        | Over/under-temperature limit. |

Bits not derivable from the part-number string remain zero rather than
being guessed. See [`capabilities_for_part_number`](../src/watlowlib/registry/families.py).

## Capabilities are priors, not contracts

The reverse-engineering sample behind these tables is small. The
library treats the family table and capability bits as **priors from
observation**, not protocol guarantees. The session attempts commands
whose priors don't match and updates the per-session availability
cache on the device's response. Pre-I/O refusal happens only under
`strict=True` (when wired through the session) or after an observed
`WatlowNoSuchObjectError` / `WatlowModbusIllegalDataAddressError` on
the current session.

`DeviceInfo.health` carries the outcome of `identify()`:

- `DeviceHealth.OK` — every identity probe succeeded.
- `DeviceHealth.PARTIAL` — part number captured but a secondary field
  (firmware, serial) missed.
- `DeviceHealth.FAILED` — part number could not be read; capability
  decoding skipped.

See [Safety](safety.md) and [Design](design.md) §5b for the gate-order
rationale.

## Identifying a controller

```python
async with await open_device("/dev/ttyUSB0", address=1) as ctl:
    info = await ctl.identify()
    print(f"part:     {info.part_number.raw}")
    print(f"family:   {info.family}")
    print(f"firmware: {info.firmware_id}")
    print(f"serial:   {info.serial_number}")
    print(f"loops:    {info.loops}")
    print(f"protocol: {info.protocol} (configured: {info.configured_protocol})")
    print(f"caps:     {info.capabilities}")
    if info.protocol_mismatch:
        print("warning: EEPROM and active protocol disagree")
```

`identify()` runs the part-number / firmware / serial reads in one
shot, parses the part number, and ORs the family prior with the
SKU-decoded capability bits. Re-running it forces a refresh — useful
after a `change_protocol_mode(...)` or a parameter write that may
flip a capability. See [`Controller.identify`](api/devices.md).

`DeviceInfo.protocol_mismatch` flags the case where parameter 17009
(protocol mode) reports one wire protocol but the host is currently
talking another — common on Std-Bus-only SKUs where 17009 was written
to "Modbus" but the comms position-8 character means no Modbus stack
ever shipped.

## Multiple loops

Dual-loop SKUs (PM6/PM8/PM9 control type `U`) expose
[`Controller.loop(n)`](api/devices.md) for per-loop access:

```python
async with await open_device("/dev/ttyUSB0", address=1) as ctl:
    info = await ctl.identify()
    if info.loops >= 2:
        loop2 = ctl.loop(2)
        pv = await loop2.read_pv()
```

`loop(n)` validates `n` against `info.loops` and returns a
[`ControllerLoop`](api/devices.md) bound to the same session. All
single-loop SKUs default to `loops=1`; `Controller.read_pv()` is
shorthand for `Controller.loop(1).read_pv()`.

## Discovery

[`sweep_stdbus(port)`](../src/watlowlib/devices/discovery.py) walks
Standard Bus addresses 1–16 on a port; `sweep_modbus(port, range)`
walks a Modbus slave range. Both return one
[`DiscoveryResult`](api/devices.md) per probed address regardless of
outcome.

```python
from watlowlib import sweep_stdbus

async with anyio.from_thread.start_blocking_portal() as _:
    rows = await sweep_stdbus("/dev/ttyUSB0")
    for row in rows:
        if row.protocol is not None:
            print(row.address, row.info.part_number.raw)
```

The [`watlow-discover`](troubleshooting.md#watlow-discover) CLI wraps
both sweeps with a JSON / table renderer.

## See also

- [Commands](commands.md) — the command surface and gate order.
- [Parameters](parameters.md) — registry, parameter ids, units.
- [Streaming](streaming.md) — `record()`, `Sample`, sinks.
- [Standard Bus protocol](protocol-stdbus.md) and [Modbus RTU mapping](protocol-modbus.md) — wire layer.
- [Design](design.md) §5 — full taxonomy and runtime-verification model.
