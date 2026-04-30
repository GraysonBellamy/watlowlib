# Safety

`watlowlib` drives physical hardware — heaters, ovens, retort
controllers, calibration baths. Safety rules are binding; see the
[Design doc](design.md) §5b for the authoritative list.

## Per-command safety tier

Every [`Command`](api/commands.md) carries a [`SafetyTier`](../src/watlowlib/devices/capability.py):

| Tier         | Examples                                                          | Gate |
| ------------ | ----------------------------------------------------------------- | ---- |
| `READ_ONLY`  | `process_value`, `setpoint` reads, `output_power`, alarm status, identity, part number | runs freely |
| `STATEFUL`   | reserved (no library command currently uses this tier)            | runs freely |
| `PERSISTENT` | every parameter write — including `RW` runtime-only parameters; PID writes; protocol-mode flip; baud / address change | requires `confirm=True` |

Calling a `PERSISTENT` operation without `confirm=True` raises
`WatlowConfirmationRequiredError` *before* any I/O — no bytes go out.

The library treats every write as a state change worth confirming,
including `RW` "runtime only" parameters that don't write EEPROM.
That's slightly stricter than the Watlow RWES classification; see
[Design](design.md) §5b for the rationale.

## Gate order

[`Session.execute()`](../src/watlowlib/devices/session.py) applies
gates in this fixed order:

1. **Protocol** (hard) — the active-protocol variant must not be
   `None`, else `WatlowProtocolUnsupportedError`.
2. **Known-denied** (hard once observed) — if the per-session
   availability cache records `Availability.UNSUPPORTED` for this
   command, raise pre-I/O without re-probing.
3. **Safety tier** (hard) — `PERSISTENT` operations need `confirm=True`.
4. **Execute**, then map the device response into an availability
   transition (Std Bus "no such object/attribute/instance" or Modbus
   `IllegalDataAddress` flips the cache to `UNSUPPORTED`).

## Why not hard-gate on capability priors?

The reverse-engineering sample behind the family / capability tables
is small. The cost of a wrong denial (user blocked from a parameter
their controller actually supports) is worse than the cost of a failed
attempt (one round-trip and a clean typed error). The device remains
the source of truth for what's available; the library's prior is just
a hint. See [Controllers](devices.md) for the "capabilities are
priors, not contracts" framing.

## Persistent-write helpers

The following maintenance operations are gated as `PERSISTENT` because
they mutate device EEPROM and may force a transport rebind:

| Function                                                                            | Notes |
| ----------------------------------------------------------------------------------- | ----- |
| [`change_baud(...)`](api/maintenance.md)                                            | Updates the baud parameter, saves, re-opens the transport at the new rate. |
| [`change_modbus_address(...)`](api/maintenance.md)                                  | Updates the Modbus slave address; rebinds the session. |
| [`change_stdbus_address(...)`](api/maintenance.md)                                  | Updates the Standard Bus address (1..16); rebinds the session. |
| [`change_protocol_mode(...)`](api/maintenance.md)                                   | Flips the controller between Std Bus and Modbus RTU at parameter 17009; verifies post-write at the new framing. |

All four require `confirm=True`. They're exposed both as one-shot
port-level helpers (no full `Controller` lifecycle needed) and via the
[`watlow-configure`](troubleshooting.md) CLI.

!!! warning "SKU gate on `change_protocol_mode`"
    On EZ-ZONE PM SKUs without a Modbus stack (comms position-8 = `A`,
    `0`, `5`, `6`, etc.), writing parameter 17009 = "Modbus" *succeeds*
    on the wire but the device never starts answering Modbus frames.
    `change_protocol_mode` checks the part number's comms code before
    issuing the write and raises `WatlowConfigurationError` if the SKU
    does not include the requested protocol. See
    [Troubleshooting](troubleshooting.md).

## Pre-flight validation

Before a write reaches the wire, the registry validates the value
against the parameter's range (when `range_min` / `range_max` parsed
cleanly from the EZ-ZONE register list) and the instance against
`spec.max_instance`. Both checks raise `WatlowValidationError`
pre-I/O — bytes never go out for an obviously-malformed write.

The device performs its own bound check on the wire and answers with
`WatlowNoSuchObjectError` (Std Bus) or
`WatlowModbusIllegalDataValueError` (Modbus) if the host validator
missed the violation.

## Hardware test tiers

| Marker                  | What it does                                                  | Opt-in env var |
| ----------------------- | ------------------------------------------------------------- | -------------- |
| `hardware`              | Read-only against a connected controller                      | `WATLOWLIB_TEST_PORT=/dev/ttyUSB0` |
| `hardware_stateful`     | Changes device state (parameter writes)                       | `WATLOWLIB_ENABLE_STATEFUL_TESTS=1` |
| `hardware_destructive`  | Baud / address change, protocol switch                        | `WATLOWLIB_ENABLE_DESTRUCTIVE_TESTS=1` |

Default `pytest` runs exclude all three. See [Testing](testing.md).

## See also

- [Commands](commands.md) — gate order on every command surface.
- [Controllers](devices.md) — capability flags as priors not contracts.
- [Parameters](parameters.md) — RWES → `SafetyTier` mapping.
- [Maintenance API](api/maintenance.md) — persistent-write helpers.
- [Testing](testing.md) — hardware tiers and `FakeTransport`.
- [Design](design.md) §5b — full gate-order rationale.
