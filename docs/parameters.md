---
description: The cross-protocol parameter registry in watlowlib — canonical names for EZ-ZONE PM parameters mapped to Standard Bus (class/member/instance) and Modbus registers.
---

# Parameters

Watlow controllers expose every settable / readable value as a
**parameter** identified by a numeric id (e.g. `4001` = process value,
`7001` = setpoint, `8003` = heat algorithm). The same parameter is
addressable on both wire protocols — Standard Bus reads it as
`(class, member, instance)`, Modbus RTU reads it as a register
address. `watlowlib` carries a registry of every published EZ-ZONE PM
parameter so callers can use a canonical name and the library does
the protocol-specific addressing.

See [Design](design.md) §5a for the registry rationale.

## The registry at a glance

```python
from watlowlib import PARAMETERS

spec = PARAMETERS.resolve("setpoint")
print(spec.parameter_id, spec.data_type, spec.rwes, spec.safety)
# 7001 DataType.FLOAT RwesFlag.RWES SafetyTier.PERSISTENT

spec = PARAMETERS.resolve(4001)
print(spec.name, spec.relative_addr, spec.cls, spec.member)
# process_value 360 0x68 0x01
```

[`PARAMETERS`](api/registry.md) is the default `ParameterRegistry`,
loaded eagerly from the bundled JSON on import. `resolve()` accepts a
canonical name, an alias, or a parameter id; unknown lookups raise
[`WatlowValidationError`](api/errors.md).

## `ParameterSpec`

Every row carries enough metadata to address the parameter on either
protocol and validate user input before sending.

| Field                | Notes |
| -------------------- | ----- |
| `parameter_id`       | Watlow parameter id (e.g. 4001). |
| `name`               | Canonical lowercase identifier (e.g. `process_value`). |
| `aliases`            | Frozenset of alternate names (e.g. `pv`, `process_temp`). |
| `data_type`          | `FLOAT`, `S32`, `U32`, `U16`, `U8`, `STRING`, or `PACKED` (enumeration). |
| `rwes`               | `R`, `W`, `RW`, `RWE`, or `RWES`. |
| `safety`             | Derived from `rwes`: `R` → `READ_ONLY`, anything else → `PERSISTENT`. |
| `cls` / `member`     | Std Bus selector (class id, member id). |
| `default_instance` / `max_instance` | Std Bus instance bounds (1..N for multi-loop SKUs). |
| `relative_addr` / `absolute_addr` | Modbus register addresses (relative is the per-block offset; absolute is the full address). |
| `register_count`     | How many 16-bit Modbus registers the parameter occupies. |
| `word_order`         | `HIGH_LOW` or `LOW_HIGH` for multi-register values; `None` defers to the client default. |
| `range_min` / `range_max` | Soft bounds parsed from the EZ-ZONE register list (when machine-readable). |
| `family_hints`       | Advisory `ControllerFamily` priors. |

The Std Bus selector and Modbus selector are *both* populated for
every parameter. Which one is used at call time depends on the
session's active protocol.

## RWES → SafetyTier

The EZ-ZONE register list tags every parameter with an RWES flag that
encodes both access and persistence:

| Flag    | Meaning                                                       | Tier        |
| ------- | ------------------------------------------------------------- | ----------- |
| `R`     | read-only                                                     | `READ_ONLY` |
| `W`     | write-only (rare; e.g. action triggers)                       | `PERSISTENT` |
| `RW`    | runtime read/write, **not** EEPROM-backed                     | `PERSISTENT` |
| `RWE`   | RW + persisted to EEPROM                                      | `PERSISTENT` |
| `RWES`  | RWE + included in the user save / restore set                 | `PERSISTENT` |

Anything that writes — even `RW` ("runtime only") — needs
`confirm=True` at the facade. The library treats every write as a
state change worth confirming. See [Safety](safety.md) for the gate
order and the rationale.

## Resolving by alias

The bundled JSON ships with one canonical name per parameter, but
common alternate names resolve through the alias table:

```python
PARAMETERS.resolve("pv")              # → parameter 4001 (process_value)
PARAMETERS.resolve("set_point")       # → parameter 7001 (setpoint)
PARAMETERS.resolve("heat_algorithm")  # → parameter 8003 (heat_algorithm)
```

Aliases are case-insensitive. The full alias table lives in
[`registry/aliases.py`](../src/watlowlib/registry/aliases.py).

## Multi-instance parameters

Per-loop parameters carry an instance selector — instance 1 is loop 1,
instance 2 is loop 2, etc. The default is always 1; the registry's
`max_instance` caps the upper bound:

```python
async with await open_device("/dev/ttyUSB0", address=1) as ctl:
    pv1 = await ctl.read_pv(instance=1)            # loop 1
    pv2 = await ctl.read_pv(instance=2)            # loop 2 (dual-loop SKUs)
    out = await ctl.read_parameter("output_power", instance=2)
```

`Controller.read_parameter` and `write_parameter` validate the
instance against `spec.max_instance` before encoding. Out-of-range
instances raise [`WatlowValidationError`](api/errors.md).

## Range validation

When the EZ-ZONE register list publishes a machine-readable range (e.g.
`-1999.0 to 9999.0`), the registry parses it into `range_min` /
`range_max` and the facade soft-validates writes:

```python
await ctl.write_parameter("setpoint", 1e6, confirm=True)
# WatlowValidationError: value 1000000 out of range for 'setpoint' (-1999.0..9999.0)
```

Ranges parsed as enumeration strings (`"Off (0), On (1)"`) are left as
`None` and skipped by the validator. Out-of-range values still trip
the device's own bound check on the wire if the host validator
missed them.

## Enumerations

Enumerated parameters (`DataType.PACKED`) decode to integer codes; the
[`enumerations`](../src/watlowlib/registry/enumerations.py) table
documents the human meanings. Common ones:

| Parameter         | Codes |
| ----------------- | ----- |
| `heat_algorithm`  | `62` Off, `63` PID, `71` On-Off |
| `cool_algorithm`  | `62` Off, `63` PID, `71` On-Off |
| `tc_type`         | `J`, `K`, `T`, `S`, `R`, `B`, `N`, `E`, `C` |
| `units`           | `15` °C, `30` °F — parameter 3005, front-panel display |
| `display_units`   | `15` °C, `30` °F — parameter 17050, comms display label. **Not** an authoritative source for the wire scale on every firmware; see [`docs/devices.md`](devices.md) §Units. |

Reading an enumerated parameter returns the integer; the facade does
not auto-decode to a label (so callers can write back round-trip safe
values without an extra mapping). See
[`registry/enumerations.py`](../src/watlowlib/registry/enumerations.py)
for the full table.

## Reading and writing

```python
async with await open_device("/dev/ttyUSB0", address=1) as ctl:
    # Generic read — returns ParameterEntry; the facade unwraps to Reading
    # for typed-shortcut parameters (process_value, setpoint, ...).
    entry = await ctl.read_parameter("heat_algorithm", instance=1)
    print(entry.value)                # → 63 (PID)

    # Write — confirm=True is mandatory.
    await ctl.write_parameter("heat_algorithm", 71, instance=1, confirm=True)
```

`Controller.read_parameter` returns a [`ParameterEntry`](api/devices.md)
that carries the spec, instance, decoded value, and raw bytes.
[`Controller.read_pv`](api/devices.md) and friends wrap that in a
[`Reading`](api/devices.md) for the small set of parameters where the
facade guarantees a richer shape.

## See also

- [Controllers](devices.md) — `Controller` facade and capability flags.
- [Commands](commands.md) — `READ_PARAMETER` / `WRITE_PARAMETER` dispatch.
- [Safety](safety.md) — gate order and `confirm=True`.
- [Registry API](api/registry.md) — full reference.
- [Design](design.md) §5a — registry shape and JSON-load model.
