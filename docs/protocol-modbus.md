# Modbus RTU mapping

`watlowlib` speaks Modbus RTU through the in-house
[`anymodbus`](https://github.com/GraysonBellamy/anymodbus) package.
Modbus variants emit a typed
[`ModbusOp`](../src/watlowlib/protocol/modbus/ops.py) (function code +
address + register words), not raw wire bytes — `anymodbus` builds
the PDU and CRC. This page documents how Watlow parameter ids map to
Modbus register addresses, the encoding rules, and the Watlow-specific
quirks that the codec abstracts away.

For framing, see the [Standard Bus protocol reference](protocol-stdbus.md)
for the alternate wire (Modbus RTU itself is a fully published
protocol).

## When to use Modbus over Standard Bus

Modbus RTU is **opt-in**: the SKU must include the comms stack (PM
position-8 ∈ `1`, `2`, `B`, `D`, `E`, `F`) and the device must be
flipped to Modbus mode at parameter 17009. Standard Bus is the factory
default and is always available — see
[Troubleshooting](troubleshooting.md). Reasons you might pick Modbus:

- You're integrating with a PLC or HMI that already speaks Modbus.
- You want a published wire spec rather than a partially-RE'd one.
- Your bus is shared with non-Watlow Modbus devices.

The [`Controller`](api/devices.md) facade is identical either way — a
parameter read against `setpoint` returns the same `Reading` whether
the session opened in Std Bus or Modbus.

## Address mapping

Each [`ParameterSpec`](../src/watlowlib/registry/parameters.py) carries
two Modbus addresses:

| Field            | Notes |
| ---------------- | ----- |
| `relative_addr`  | Per-block offset from the EZ-ZONE PM register list (e.g. process value = 360 = 0x168). |
| `absolute_addr`  | Full register address on the wire. |
| `register_count` | Number of 16-bit registers the parameter occupies (1 for u16, 2 for float / s32 / u32, more for short strings). |
| `word_order`     | `HIGH_LOW` (default) or `LOW_HIGH` for multi-register values. |

The Std Bus selector (`cls`, `member`, `default_instance`,
`max_instance`) and the Modbus selector are *both* populated for every
registry row. The session uses whichever matches the active protocol.

For multi-loop SKUs, each loop occupies the next register block — the
spec's `relative_addr` is loop 1; loop 2 reads from `relative_addr +
loop_block_size`. The codec handles the offset; callers pass
`instance=N` and the variant computes the right address.

## Function-code dispatch

Watlow uses the standard Modbus function codes:

| Function code | Name                       | Used for |
| ------------- | -------------------------- | -------- |
| `0x03`        | Read Holding Registers     | reading any parameter |
| `0x06`        | Write Single Register      | u16 / single-register writes |
| `0x10`        | Write Multiple Registers   | float / s32 / u32 / multi-register writes |

`anymodbus` selects the right code from the parameter's `register_count`
and the operation. Watlow does not use coil or input-register spaces.

## Data type encodings

| `DataType` | Registers | Encoding                                           |
| ---------- | --------- | -------------------------------------------------- |
| `FLOAT`    | 2         | IEEE 754 32-bit, default `HIGH_LOW` word order. EZ-ZONE PM has historically shipped both orderings depending on configuration; the registry's `word_order` field overrides. |
| `S32`      | 2         | Signed two's-complement 32-bit, `HIGH_LOW`.        |
| `U32`      | 2         | Unsigned 32-bit, `HIGH_LOW`.                       |
| `U16`      | 1         | Unsigned 16-bit. Most enumerations live here.      |
| `U8`       | 1         | Unsigned 8-bit packed in the low byte; high byte zero. |
| `STRING`   | N         | ASCII, two characters per register; null-padded.   |
| `PACKED`   | 1         | Enumeration code (u16 wire encoding).              |

The encoding lookup is centralised in
[`protocol/modbus/tables.py`](../src/watlowlib/protocol/modbus/tables.py)
behind `encoding_for(data_type)`.

## Word order

Watlow EZ-ZONE PM exposes a *Data Map* parameter (id 16562, the
`COMM_DATA_MAP` register) that toggles word order between high-low
and low-low formats. Different controllers and firmwares default
differently. Symptom of a mismatch: read PV looks like 1.5e-12 or
1e34 instead of a reasonable temperature.

The library's defaults match the most common EZ-ZONE PM setting; if
your controller is configured for the other order, override at the
registry level (see
[`ParameterSpec.word_order`](../src/watlowlib/registry/parameters.py))
or write `COMM_DATA_MAP` to match.

## Exception codes

Modbus exception responses map to the typed error hierarchy:

| Exception code | Mnemonic                | Typed error                                   |
| -------------- | ----------------------- | --------------------------------------------- |
| `01`           | Illegal Function        | `WatlowModbusIllegalFunctionError` (also a `WatlowProtocolUnsupportedError`) |
| `02`           | Illegal Data Address    | `WatlowModbusIllegalDataAddressError` (also a `WatlowProtocolUnsupportedError`) |
| `03`           | Illegal Data Value      | `WatlowModbusIllegalDataValueError`           |
| `04`           | Slave Device Failure    | `WatlowModbusSlaveFailureError`               |
| (timeout)      | no reply within timeout | `WatlowModbusTimeoutError` (also a `WatlowTimeoutError`) |

Codes `01` and `02` flip the per-session availability cache to
`UNSUPPORTED` for the offending parameter — the same shape as the
Std Bus "no such object/attribute/instance" responses. Code `03` is
treated as a transient validation error and does **not** poison the
cache. See [Design](design.md) §5b for the availability transition
table.

## Lock model

[`ModbusProtocolClient`](../src/watlowlib/protocol/modbus/client.py)
holds one `anyio.Lock` per port. Multiple controllers on the same
RS-485 segment share the lock and serialise their I/O. The
[`WatlowManager`](api/manager.md) enforces one protocol per port —
mixing Std Bus and Modbus on the same RS-485 segment raises
`WatlowConfigurationError` at `add()` time.

## See also

- [Standard Bus protocol](protocol-stdbus.md) — the alternate wire.
- [Standard Bus findings](protocol-stdbus-findings.md) — RE survey for
  the Std Bus payload grammar.
- [Parameters](parameters.md) — registry rows and addressing.
- [Commands](commands.md) — per-protocol variants and gate order.
- [`anymodbus`](https://github.com/GraysonBellamy/anymodbus) — the
  underlying Modbus codec and bus transport.
