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
observation**, not protocol guarantees. The generic session attempts
commands and updates the per-session availability cache on the
device's response. Pre-I/O refusal happens after an observed
`WatlowNoSuchObjectError` / `WatlowModbusIllegalDataAddressError` on
the current session, or in targeted helper paths that would otherwise
issue a known-bad write for a decoded SKU.

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

## Units

Watlow PM controllers carry **two** display-unit registers:

| ID    | Name                            | What it does                                                   |
| ----- | ------------------------------- | -------------------------------------------------------------- |
| 3005  | Display - Units                 | Front-panel temperature scale (visible on the device).         |
| 17050 | Communications - Display Units  | A label register that *claims* to drive the comms wire scale. |

Both registers are reachable through the parameter API, but **neither
is a reliable source of truth for the unit of temperature values on
the wire**. On at least one PM3 firmware revision (PM3C1AJ, firmware
id 5678), 17050 is **label-only**: writing it changes the enum the
device reports when 17050 is read back, but does not affect the scale
of temperature values exchanged over comms. The internal storage unit
on that device is °F regardless of what either register says; verified
empirically by writing a known setpoint and comparing the comms
readback to the front-panel display.

`watlowlib` therefore **does not** infer `Reading.unit` from either
register. The default is `unit=None` for temperature reads — an
honest "I don't know" rather than a confident lie. To get a
meaningful tag, verify the wire scale externally (the bundled
`watlow-diag probe-unit` diagnostic does this — see
[Diagnosing the wire scale](#diagnosing-the-wire-scale-watlow-diag-probe-unit)
below) and then declare it at open time:

```python
from watlowlib import Unit, open_device

async with await open_device(
    "/dev/ttyUSB0",
    address=1,
    assert_wire_temperature_unit=Unit.FAHRENHEIT,  # externally verified
) as ctl:
    pv = await ctl.read_pv()
    assert pv.unit is Unit.FAHRENHEIT  # tag matches value scale
```

The assertion is propagated to every `Reading` and `Sample` produced
by the session. Without it, temperature tags stay `None` — the
`Reading.value` is still the raw number off the wire; you just have
to know what scale it's in yourself.

### Inspection facade for parameter 17050

For diagnostics, the value of 17050 is reachable through a dedicated
inspection facade. It does **not** feed `Reading.unit`:

```python
# Read the cached label (one wire turn-around, then cached).
label = await ctl.read_comms_unit_label()  # Unit | None

# Flip the label (RWE; persists across power cycles).
await ctl.set_comms_unit_label(Unit.CELSIUS, confirm=True)
```

`set_comms_unit_label` accepts a `Unit` or a case-insensitive alias
(`"C"`, `"F"`, `"celsius"`, `"degF"`, `"°C"`). Raw device codes (15
for Celsius, 30 for Fahrenheit) belong on the lower-level
`write_parameter("display_units", code)` path. Note that writing
17050 has no documented effect on the wire scale on this firmware —
it only changes what the device reports back when 17050 is read.

### Diagnosing the wire scale (`watlow-diag probe-unit`)

`watlow-diag probe-unit` is a read-only diagnostic that infers the
wire scale by comparing a known front-panel reading against the
comms readback. Read-only by construction: it never writes anything
to the device.

Procedure:

1. Look at the front panel and note the **value** and **unit** of
   the parameter you'll compare against (default: setpoint). E.g.
   the panel shows `SP = 50` with the °C indicator lit.
2. Run:

   ```sh
   watlow-diag probe-unit COM6 --panel-shows 50 --panel-unit C
   ```

3. The probe reads the same parameter over comms and reports which
   scale matches. Sample output:

   ```text
   Reference comparison:
     panel shows : 50.0 C
     comms reads : 122.0 (setpoint instance=1)

   Inference: fahrenheit
     panel-as-°C = 50.0, delta vs comms = 72.0
     panel-as-°F = 122.0, delta vs comms = 0.0

     → open_device(..., assert_wire_temperature_unit=Unit.FAHRENHEIT)
   ```

4. Take the recommended `assert_wire_temperature_unit=` value into
   your `open_device` call. From then on, `Reading.unit` and
   `Sample.unit` will reflect that wire scale.

Useful flags:

- `--parameter setpoint|process_value|...` — choose which temperature
  parameter to compare. Setpoint is usually the easiest (you set it
  yourself on the panel; PV drifts under control).
- `--epsilon 0.2` — match tolerance. Bump up if your panel rounds to
  whole degrees.
- `--json` — emit the full report as JSON for downstream tooling.

The diagnostic is one-shot: hardware behaviour for a given SKU +
firmware doesn't change between runs, so the recommended kwarg can
be hard-coded once and reused.

## Discovery

[`find_devices()`](../src/watlowlib/devices/discovery.py) probes the
cartesian product of `ports × baudrates × protocols × addresses` and
returns one [`FindResult`](api/devices.md) per probe attempt. The
default scan is narrow — every visible serial port (via
`anyserial.list_serial_ports`), bauds `38400 / 19200 / 9600`, both
Watlow protocols, address `1` only — so a GUI Discover dialog can ask
"is anything plugged in?" in under 15 seconds on a four-port rig.

```python
from watlowlib import find_devices

rows = await find_devices()                # scan all ports, defaults
rows = await find_devices(ports=["/dev/ttyUSB0"])
rows = await find_devices(addresses=range(1, 17))  # multi-drop sweep

for row in rows:
    if row.ok:
        print(row.port, row.baudrate, row.protocol.value,
              row.address, row.info.part_number.raw)
```

A row's `ok` field is the single attribute callers filter on:
populated `info` and `error is None` ⇔ `ok is True`. Silent /
absent addresses surface as `ok=False` rows carrying a typed
`WatlowError` so callers can distinguish "port wouldn't open" from
"no reply at this address".

The [`watlow-discover`](troubleshooting.md#watlow-discover) CLI wraps
`find_devices()` with a JSON / table renderer.

## See also

- [Commands](commands.md) — the command surface and gate order.
- [Parameters](parameters.md) — registry, parameter ids, units.
- [Streaming](streaming.md) — `record()`, `Sample`, sinks.
- [Standard Bus protocol](protocol-stdbus.md) and [Modbus RTU mapping](protocol-modbus.md) — wire layer.
- [Design](design.md) §5 — full taxonomy and runtime-verification model.
