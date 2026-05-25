---
description: The Command[Req, Resp] surface in watlowlib — per-protocol variants, safety tiers, firmware metadata, and how the session dispatches Standard Bus vs Modbus RTU.
---

# Commands

Every Watlow command is one [`Command[Req, Resp]`](../src/watlowlib/commands/base.py)
spec — a frozen dataclass carrying metadata (name, family hints,
capability hints, safety tier, optional firmware bounds) plus at most
one variant per protocol. Per-protocol work lives on
[`StdBusVariant`](../src/watlowlib/commands/base.py) /
[`ModbusVariant`](../src/watlowlib/commands/base.py) objects, not as
methods bolted onto `Command`. The session selects the variant matching
the active protocol; if the selected variant is `None`, the call fails
pre-I/O with `WatlowProtocolUnsupportedError`. See [Design](design.md)
§5.

This page is a catalogue by module. Full API reference is at
[api/commands.md](api/commands.md); the
[`Controller`](api/devices.md) facade methods are thin wrappers over
`session.execute(COMMAND, request)`.

## Anatomy of a command

```python
from watlowlib.commands import Command, CommandContext
from watlowlib.commands.base import ModbusVariant, StdBusVariant
from watlowlib.devices.capability import Capability, SafetyTier
from watlowlib.registry.families import ControllerFamily
```

Key [`Command`](../src/watlowlib/commands/base.py) fields:

| Field               | Purpose |
| ------------------- | ------- |
| `name`              | Python-friendly identifier; used in error context and log events. |
| `stdbus`            | [`StdBusVariant`](../src/watlowlib/commands/base.py) carrying class/member/instance + encode/decode, or `None` if no Std Bus binding. |
| `modbus`            | [`ModbusVariant`](../src/watlowlib/commands/base.py) carrying register address + register count + encode/decode, or `None` if no Modbus binding. |
| `family_hints`      | Advisory `frozenset[ControllerFamily]` prior; empty = no prior. |
| `capability_hints`  | Advisory metadata reserved for stricter future gates; `Capability.NONE` = no hint. |
| `safety`            | [`SafetyTier`](api/devices.md): `READ_ONLY`, `STATEFUL`, `PERSISTENT`. |
| `min_firmware`      | Optional firmware-version floor metadata; not enforced by the v1 session. |

Every command is dispatched via `session.execute(spec, request)`. The
request is a typed per-command dataclass; the response is the typed
result the variant decodes. The session never juggles raw bytes at the
public surface.

## Gate order

`Session.execute()` applies gates in a specific order before any I/O.
See [Safety](safety.md) for the full table and [Design](design.md)
§5b for the rationale.

1. **Protocol** (hard) — active-protocol variant must not be `None`,
   else `WatlowProtocolUnsupportedError`.
2. **Known-denied** (hard once observed) — per-session availability
   cache short-circuits commands the device has already returned a
   "no such object/attribute/instance" code for.
3. **Safety tier** (hard) — `PERSISTENT` requires `confirm=True`.
4. **Execute**, then update the availability cache from the device's
   response (see [Design](design.md) §5b).

## Commands by module

### Parameters — [commands/parameters.py](../src/watlowlib/commands/parameters.py)

The two workhorse commands. Every public-API method on
[`Controller`](api/devices.md) lowers to one of these.

| Command           | Std Bus              | Modbus               | Safety       | Surface |
| ----------------- | -------------------- | -------------------- | ------------ | ------- |
| `READ_PARAMETER`  | class 03 / member 01 | input/holding registers (per spec) | derived from RWES | `Controller.read_parameter(name, instance=1)` |
| `WRITE_PARAMETER` | class 04 / member 01 | preset multiple regs | derived from RWES | `Controller.write_parameter(name, value, confirm=True)` |

Both are parameterised by [`ParameterSpec`](../src/watlowlib/registry/parameters.py)
from the [registry](parameters.md). The spec carries the canonical
name, parameter id, data type (float / uint / enum), RWES flag (read /
write / EEPROM-saved / setpoint-bound), unit, and per-protocol
addressing (Std Bus class/member/instance, Modbus register address).
The RWES flag determines `SafetyTier`: anything that writes EEPROM
(W or E) is `PERSISTENT`.

The [`Controller`](api/devices.md) facade exposes typed shortcuts that
compose `READ_PARAMETER` / `WRITE_PARAMETER`:

| Method                            | Underlying parameter     | Tier        |
| --------------------------------- | ------------------------ | ----------- |
| `Controller.read_pv(instance=1)`  | `process_value` (4001)   | READ_ONLY   |
| `Controller.read_setpoint(instance=1)` | `setpoint` (7001)   | READ_ONLY   |
| `Controller.set_setpoint(value, confirm=True, instance=1)` | `setpoint` (7001) | PERSISTENT |
| `Controller.read_parameter(name)` | any registered parameter | per-spec    |
| `Controller.write_parameter(name, value, confirm=True)` | any registered parameter | per-spec |

`Controller.poll()` is the no-argument process-value snapshot helper.
`Controller.poll_many(parameters, instances=...)` issues one read per
requested parameter/instance and returns `Sample` rows for the
recorder.

### Loop — [commands/loop.py](../src/watlowlib/commands/loop.py)

Composite operations against a single control loop.

| Helper                          | What it does | Safety |
| ------------------------------- | ------------ | ------ |
| `read_pid(session, instance=1)` | Reads heat / cool gains as one [`PidGains`](api/commands.md) snapshot. | READ_ONLY |
| `write_pid(session, gains, confirm=True, instance=1)` | Writes a [`PidGains`](api/commands.md) — heat P/I/D and (when present) cool P/I/D. | PERSISTENT |
| `read_output(session, instance=1)` | Reads `output_power` as a [`Reading`](api/devices.md). | READ_ONLY |

Cool gains are skipped on single-output SKUs (`Capability.HAS_COOLING`
absent). PID round-trips on dual-output SKUs are subject to known
registry-range issues — see [Troubleshooting](troubleshooting.md).

### Alarms — [commands/alarms.py](../src/watlowlib/commands/alarms.py)

| Helper                          | What it does | Safety |
| ------------------------------- | ------------ | ------ |
| `read_alarms(session, instance=1)` | Reads alarm-status bits and decodes into an [`AlarmState`](api/devices.md) (high / low / silenced). | READ_ONLY |

## Protocol-variant dispatch

The session selects the variant for the active protocol:

```python
# Std Bus active: uses spec.stdbus
# Modbus active:  uses spec.modbus
await ctl.write_parameter("setpoint", 75.0, confirm=True)
```

If the active variant is `None` for the active protocol, the call
fails pre-I/O with `WatlowProtocolUnsupportedError`. Switching
protocols is via [`maintenance.change_protocol_mode`](api/maintenance.md);
`open_device(...)` never flips protocol mode as a side effect.

## Adding a command

1. Define a request dataclass and (where useful) a response type in
   the appropriate `commands/<module>.py`.
2. Implement `StdBusVariant.encode/decode` and / or
   `ModbusVariant.encode_registers/decode` for the variants you can
   support.
3. Construct a `Command(name=..., stdbus=..., modbus=..., safety=...,
   capability_hints=...)` at module top level.
4. Add a thin facade method on [`Controller`](api/devices.md) (or a
   composite helper in `commands/<module>.py`) that calls
   `await session.execute(SPEC, request)`.
5. Add unit tests against [`FakeTransport`](api/testing.md) and the
   relevant captured fixture.

See [Design](design.md) §5 for the spec / variant separation rationale.

## See also

- [Controllers](devices.md) — `Controller` facade and capability flags.
- [Parameters](parameters.md) — registry, parameter ids, units, RWES.
- [Safety](safety.md) — gate order and `confirm=True` rules.
- [Standard Bus protocol](protocol-stdbus.md) and [Modbus RTU](protocol-modbus.md) — wire layers.
- [Design](design.md) §5 — full command surface and variant rationale.
