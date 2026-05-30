---
description: Architectural design of watlowlib — a protocol-neutral Controller facade backed by a Session that dispatches through pluggable Standard Bus and Modbus RTU clients.
---

# watlowlib — package design

A Python driver for Watlow temperature controllers, modeled on `alicatlib`
and `sartoriuslib`. Multi-protocol from day one: Standard Bus and Modbus
RTU, with room for Modbus TCP, F4T WatBus, and others.

## 1. Recommendation in one paragraph

`watlowlib` is sartoriuslib-shaped: a protocol-neutral `Controller` facade
backed by a `Session` that dispatches through one of N pluggable
`ProtocolClient`s. The two initial protocols are **Standard Bus** (the
factory default on older controllers and the primary target — codec
ported from the `watlowtesting/` RE workspace and unit-tested against
live PM3 captures) and **Modbus RTU** (thin adapter over the in-house
`anymodbus` package, which is also AnyIO + anyserial-based). The single
biggest win unique to Watlow is that the existing `pm_parameters.json`
already carries the cross-protocol mapping per parameter (Standard Bus
`class/member/instance` *and* Modbus `relative_addr/absolute_addr`
*and* RWES persistence flag), so `read_parameter("setpoint")` lowers to
either protocol from one shared registry. That is the seam that earns
the multi-protocol abstraction; without it, layering Modbus over
Standard Bus would be ceremony.

## 2. Repo layout

This repo at `~/Documents/git/watlowlib/` is a sibling to alicatlib /
sartoriuslib, **separate from** `watlowtesting/` (which stays as the RE
/ capture workspace, like sartoriustesting → sartoriuslib).

Top-level files mirror sartoriuslib exactly: `pyproject.toml`
(hatchling + hatch-vcs, ruff, mypy strict, pyright, pytest+anyio, same
dep groups), `README.md`, `CHANGELOG.md`, `LICENSE`, `CONTRIBUTING.md`,
`SECURITY.md`, `zensical.toml`, `docs/`, `tests/`, `src/watlowlib/`.

## 3. Package tree

```
src/watlowlib/
  __init__.py, py.typed, errors.py, firmware.py, version.py, _logging.py, config.py

  transport/
    base.py        # Transport Protocol + SerialSettings (lifted from sartoriuslib)
    serial.py      # anyserial-backed
    fake.py        # scripted FakeTransport for tests / fixture replay

  protocol/
    base.py        # ProtocolClient Protocol, ProtocolKind StrEnum {AUTO, STDBUS, MODBUS_RTU}
    client.py      # make_protocol_client(kind, transport, ...) factory
    detect.py      # conservative auto-detect: Modbus RTU probe → Std Bus probe → fail
    stdbus/        # ported from watlowtesting/src/watlowlib/stdbus/
      framing.py   # current frame.py (BACnet MS/TP outer + CRCs)
      payload.py   # current payload.py (Watlow attribute service)
      tlv.py       # type-tag codec (split out of payload.py for clarity)
      tables.py    # ErrorCode, FrameType, type-tag table, address mapping
      client.py    # StdBusProtocolClient implementing ProtocolClient
      types.py     # StdBusFrame, StdBusReply, decoded value variants
    modbus/        # NEW — thin adapter over anymodbus (no codec / no framing of our own)
      client.py    # ModbusProtocolClient: holds an anymodbus.Bus + slave address.
                   #   execute(op: ModbusOp) -> tuple[int, ...] is the single
                   #   entry point; it selects the matching anymodbus.Slave
                   #   method from op.fn. Implements ProtocolClient (lock /
                   #   dispose) so the Session treats it identically to the
                   #   Std Bus client.
      ops.py       # ModbusFn StrEnum + ModbusOp dataclass (§5)
      tables.py    # data_type → register count + word/byte order defaults
                   #   (float = 2 regs HIGH_LOW, u16 = 1 reg, s32 = 2 regs, ...)
                   #   built on top of anymodbus.WordOrder / ByteOrder
      errors.py    # remap anymodbus exceptions into the WatlowError hierarchy

  registry/
    parameters.py  # Parameter spec table loaded from data/pm_parameters.json,
                   # keyed by canonical name AND parameter_id; carries:
                   #   - stdbus selector (cls, member, default instance)
                   #   - modbus relative_addr, absolute_addr, register count
                   #   - data_type → wire encoding per protocol
                   #   - RWES → SafetyTier
                   #   - range/default/enum metadata
    families.py    # ControllerFamily {PM, RM, ST, EZZONE_LIMIT, F4T, UNKNOWN}
                   # + classify_family(part_number)
    enumerations.py# load data/enumerations.json (Heat Algo, Sensor Type, ...)
    units.py       # Unit {CELSIUS, FAHRENHEIT, PERCENT} +
                   # UnitKind {temperature, percent, dimensionless,
                   # enumeration, string} declared per registry row
    aliases.py     # fuzzy-string resolvers ("setpoint" → 7001)
  data/
    pm_parameters.json     # already extracted
    enumerations.json      # already extracted
    rm_parameters.json     # later (RM family)
    f4t_parameters.json    # later

  commands/
    base.py        # Command[Req,Resp], StdBusVariant, ModbusVariant, CommandContext
    parameters.py  # READ_PARAMETER / WRITE_PARAMETER (the workhorse)
    identity.py    # READ_PART_NUMBER, READ_HARDWARE_ID, READ_FIRMWARE
    process.py     # READ_PV, READ_SETPOINT, WRITE_SETPOINT, READ_OUTPUT
    loop.py        # PID, autotune, hi/lo limits
    alarms.py
    profile.py     # ramp/soak (later — F4T-leaning)
    raw.py         # safe-list of read-only stdbus opcodes & modbus FCs

  devices/
    kind.py             # ControllerFamily re-export
    capability.py       # Capability bitmap, SafetyTier, Availability
    models.py           # Reading, ProcessValue, LoopState, AlarmState,
                        # DeviceInfo, ParameterEntry
    controller.py       # the Controller facade
    loop.py             # ControllerLoop sub-facade returned by controller.loop(n)
    session.py          # I/O lock, gates, availability cache
    factory.py          # open_device / _open_controller (private)
    discovery.py        # find_devices() — port-scan helper across protocols, bauds, addresses

  manager.py            # WatlowManager — multi-controller, port-aware serializer
  maintenance.py        # baud / address / protocol-mode change helpers (confirmed PERSISTENT writes)
  testing.py            # FakeTransport, canned frames, fixture loaders

  streaming/
    sample.py, recorder.py    # record(source, parameters=..., rate_hz=...)
                              # cadenced calls into poll_many()
                              # (Watlow has no autoprint/push-stream analog)

  sinks/
    base.py, _schema.py, memory.py, csv.py, jsonl.py, sqlite.py,
    parquet.py, postgres.py   # extras gated the same way

  sync/
    portal.py, controller.py, manager.py, recording.py, sinks.py

  cli/
    read.py        # watlow-read
    discover.py    # watlow-discover
    raw.py         # watlow-raw   (escape hatch per protocol)
    decode.py      # watlow-decode (offline frame decode)
    configure.py   # watlow-configure (baud / address / protocol-mode flips)
    diagnostics/
      snapshot.py, sweep.py, argfuzz.py, tap.py, stream.py, detect_framing.py
```

## 4. The protocol seam

```python
class ProtocolKind(StrEnum):
    AUTO = "auto"
    STDBUS = "stdbus"
    MODBUS_RTU = "modbus_rtu"

@runtime_checkable
class ProtocolClient[Request_contra, Reply_co](Protocol):
    @property
    def lock(self) -> anyio.Lock: ...
    @property
    def disposed(self) -> bool: ...
    def dispose(self) -> None: ...
    async def execute(self, request: Request_contra, *,
                      timeout: float | None = None,
                      command_name: str = "") -> Reply_co: ...
```

`StdBusProtocolClient` is `ProtocolClient[bytes, StdBusReply]` —
typed-bytes in, decoded reply out — because we own the codec.
`ModbusProtocolClient` is `ProtocolClient[ModbusOp, tuple[int, ...]]`
because `anymodbus` already owns the wire codec; the request shape is
a typed instruction (§5), not bytes. The `Session` holds an
`active: ProtocolKind` and dispatches; `make_protocol_client` is the
single factory. Adding a third protocol later (Modbus TCP, F4T's
WatBus, OPC UA) is one new subpackage + one new enum value + one new
factory branch — the `Transport` Protocol is wire-agnostic and a TCP
transport can sit alongside `serial.py` / `fake.py` without reshaping
the seam.

## 5. Command and variant shape

Commands are pure descriptors; the `Session` owns dispatch. Both
variants take a typed request and produce a typed response without
touching transport — this keeps the gate / log / availability machinery
in `Session.execute` (§5b) uniform across protocols and keeps variants
testable without a fake bus or a fake `Slave`.

```python
@dataclass(frozen=True, slots=True)
class Command[Req, Resp]:
    name: str
    stdbus: StdBusVariant[Req, Resp] | None
    modbus: ModbusVariant[Req, Resp] | None
    family_hints: frozenset[ControllerFamily]
    capability_hints: Capability
    safety: SafetyTier               # READ_ONLY / STATEFUL / PERSISTENT (§5b)
    min_firmware: FirmwareVersion | None = None
```

Std Bus variants emit raw payload bytes — we own that codec, so the
typed request goes in and bytes come out:

```python
@dataclass(frozen=True, slots=True)
class StdBusVariant[Req, Resp]:
    def encode(self, ctx: CommandContext, request: Req) -> bytes: ...
    def decode(self, reply: StdBusFrame, ctx: CommandContext) -> Resp: ...
```

Modbus variants emit a small typed instruction (`ModbusOp`) instead of
bytes — `anymodbus` already owns the wire codec, so handing it bytes
would be a layer violation:

```python
class ModbusFn(StrEnum):
    READ_HOLDING    = "read_holding"
    READ_INPUT      = "read_input"
    WRITE_REGISTER  = "write_register"
    WRITE_REGISTERS = "write_registers"
    # coil / discrete-input ops added if a registry parameter ever needs them

@dataclass(frozen=True, slots=True)
class ModbusOp:
    fn: ModbusFn
    address: int
    count: int = 1
    values: tuple[int, ...] | None = None     # set on writes

@dataclass(frozen=True, slots=True)
class ModbusVariant[Req, Resp]:
    def encode(self, ctx: CommandContext, request: Req) -> ModbusOp: ...
    def decode(self, words: tuple[int, ...], ctx: CommandContext) -> Resp: ...
```

`ModbusProtocolClient.execute(op)` is the only code that touches an
`anymodbus.Slave`; it picks the matching `Slave` method from `op.fn`
and returns the raw register tuple. Both protocol clients share the
`ProtocolClient` Protocol (§4) for lifecycle (`lock`, `dispose`); the
*request* shape differs between protocols, but variants on either side
remain pure functions of `(ctx, request)`.

The 80% case — read/write any registry parameter (§5a) — is one
`ReadParameter` / `WriteParameter` command pair whose variants both
pull selector + encoding from the spec. Specialized commands (autotune,
profile upload, identity) declare their own variants.

## 5a. Parameter registry

Each row of `data/pm_parameters.json` is loaded once into a
`ParameterSpec`, indexed by canonical name (with aliases) and by
`parameter_id`. The spec carries enough information to lower a
`read_parameter("setpoint")` call to either protocol with no
per-parameter bespoke code.

```python
@dataclass(frozen=True, slots=True)
class ParameterSpec:
    parameter_id: int                      # 4001, 7001, ...
    name: str                              # canonical "process_value"
    aliases: frozenset[str]                # "pv", "process_val", ...
    data_type: DataType                    # FLOAT / S32 / U16 / U32 / U8 / STRING / PACKED
    rwes: RwesFlag                         # R / RW / RWE / RWES
    safety: SafetyTier                     # derived from rwes (§5b)

    # Std Bus selector
    cls: int
    member: int
    default_instance: int
    max_instance: int

    # Modbus selector
    relative_addr: int
    absolute_addr: int
    register_count: int
    word_order: WordOrder | None = None    # None → client default (HIGH_LOW)

    # Decode metadata
    enum: type[IntEnum] | None = None      # bound from enumerations.json
    range_min: float | None = None
    range_max: float | None = None
    default: object | None = None

    # Family scope (advisory; empty = no prior, attempt anywhere)
    family_hints: frozenset[ControllerFamily] = frozenset()
```

Forward extension points kept off the spec deliberately: per-parameter
`min_firmware` / `max_firmware` and `capability_hints` are not wired
in today because the JSON registry doesn't carry the data — adding
empty fields would mislead. Adding them later is non-breaking because
the spec is built once at load.

## 5b. Capability, safety, and availability

Three small enums set the contract between the registry, the command
layer, and the session.

```python
class SafetyTier(IntEnum):
    READ_ONLY  = 0   # R                    — no state change
    STATEFUL   = 1   # (none in registry)   — runtime state change, no EEPROM
    PERSISTENT = 2   # RW / RWE / RWES      — EEPROM-backed; needs confirm=True

class Capability(Flag):
    NONE = 0
    PROFILE       = auto()   # ramp / soak engine (F4T / RM)
    LIMIT         = auto()   # over/under-temperature limit module
    HAS_COOLING   = auto()   # second control output present
    HAS_MODBUS    = auto()   # comms option includes Modbus RTU
    HAS_PROFILES  = auto()   # SKU supports profiles
    HAS_BLUETOOTH = auto()   # comms option includes Bluetooth
    HAS_ETHERNET  = auto()   # comms option includes Ethernet

class Availability(StrEnum):
    UNKNOWN     = "unknown"     # never tried this session
    SUPPORTED   = "supported"   # observed working this session
    UNSUPPORTED = "unsupported" # device rejected with a "no such" code
```

`SafetyTier` derives from `rwes`: `R` → `READ_ONLY`,
`RW` / `RWE` / `RWES` → `PERSISTENT`. `STATEFUL` is reserved for
operations like "start autotune" that don't write EEPROM but do change
runtime state; today no parameter is classified `STATEFUL`, but the
tier exists so commands like `loop.start_autotune()` have a place to
live without inventing it later.

The session keeps a per-command `Availability` cache for the lifetime
of the session. Mapping from device responses to availability
transitions:

| Response                                        | Transition |
|---                                              |---         |
| Success                                         | → `SUPPORTED` |
| Std Bus `0x81` (`NO_SUCH_OBJECT`)               | → `UNSUPPORTED` |
| Std Bus `0x83` (`NO_SUCH_ATTRIBUTE`)            | → `UNSUPPORTED` |
| Std Bus `0x84` (`NO_SUCH_INSTANCE`)             | unchanged — this *instance* is missing, not the parameter |
| Modbus `IllegalFunctionError`                   | → `UNSUPPORTED` |
| Modbus `IllegalDataAddressError`                | → `UNSUPPORTED` |
| Modbus `IllegalDataValueError`                  | unchanged — bad argument, not absence |
| Modbus `SlaveDeviceFailureError` / timeout / malformed | unchanged — non-response is not a refusal |

`UNSUPPORTED` is sticky for the session and short-circuits with a
typed error pre-I/O. `family_hints`, `capability_hints`, and firmware
metadata are advisory in v1; the session attempts and observes rather
than refusing from priors. A persistable, per-firmware probe report is
left open as future work; the in-memory `Availability` cache is the v1
mechanism.

## 6. Public API shape

```python
import anyio
from watlowlib import open_device, ProtocolKind, Controller

async def main() -> None:
    async with await open_device(
        "/dev/ttyUSB0",
        protocol=ProtocolKind.AUTO,    # try Std Bus then Modbus RTU
        address=1,                     # standard-bus address OR modbus slave
    ) as ctl:
        print(await ctl.identify())              # DeviceInfo
        pv = await ctl.read_pv()                 # Reading(value=72.4, unit=Unit.F)
        await ctl.set_setpoint(75.0, confirm=True)  # RWE → confirm gate
        loop1 = ctl.loop(1)
        print(await loop1.read_pid())
```

`ProtocolKind.AUTO` is conservative (Std Bus probe → Modbus probe →
fail clearly; rationale in §7), no opcode sweeps, no baud guessing.

Sync facade, manager, streaming, sinks, sync portal: aligned with the
family conventions — `Watlow.open` for sync, `WatlowManager`,
`record(...)`, `CsvSink`, `watlow-*` CLIs. Async is canonical; sync
wraps async via `anyio.from_thread.BlockingPortal`.

**Manager and ports.** The same RS-485 segment can only carry one
wire protocol at a time (every device on a bus must speak the same
framing). `WatlowManager` keys devices by canonical port identity, and
`add(name, port, ...)` locks the port to the protocol of the first
device added to it; subsequent `add` calls on that port must use the
same protocol or raise `WatlowConfigurationError`. Devices on
different ports remain independent and run concurrently.

## 6a. Public dataclasses

All frozen, `slots=True`. `py.typed` ships.

```python
@dataclass(frozen=True, slots=True)
class Reading:
    value: float | None              # None on overload / sensor-fail sentinel
    unit: Unit | None                # None when the source is unitless or unknown
    received_at: datetime
    monotonic_ns: int
    raw: bytes                       # original payload for debugging
    protocol: ProtocolKind           # which protocol decoded this

@dataclass(frozen=True, slots=True)
class LoopState:
    loop: int                        # 1-indexed (§7 / §11)
    pv: Reading
    setpoint: Reading
    output_pct: float | None
    mode: ControlMode | None         # OFF / AUTO / MANUAL / AUTOTUNE / UNKNOWN
    alarm_bits: int | None           # raw alarm word; AlarmState carries decoded view
    received_at: datetime
    raw: Mapping[str, bytes]         # one entry per parameter that fed the snapshot

@dataclass(frozen=True, slots=True)
class ParameterEntry:
    spec: ParameterSpec
    instance: int
    value: float | int | str | bool | None
    raw: bytes

@dataclass(frozen=True, slots=True)
class PartNumber:
    raw: str                         # "PM3R1CA-AAAAAAA"
    family: ControllerFamily         # parsed from leading characters
    # Per-family digit decoding (control type, output type, sensor input,
    # ...) is extensible — each family contributes its own decoder. Today
    # only the family discriminator is parsed; remaining digits stay in
    # `raw` until a family decoder lands.

@dataclass(frozen=True, slots=True)
class DeviceInfo:
    part_number: PartNumber
    hardware_id: int | None          # parameter 1001
    firmware_id: int | None          # parameter 1002
    serial_number: str | None
    family: ControllerFamily
    protocol: ProtocolKind           # the protocol this session is speaking
    address: int                     # bus address (Std Bus 1..16, Modbus 1..247)
    capabilities: Capability         # observed and/or seeded
    serial_settings: SerialSettings
    loops: int                       # discovered or family default

@dataclass(frozen=True, slots=True)
class AlarmState:
    loop: int
    high: bool | None
    low: bool | None
    silenced: bool | None
    raw_bits: int

@dataclass(frozen=True, slots=True)
class FindResult:
    port: str
    address: int
    baudrate: int
    protocol: ProtocolKind
    ok: bool
    info: DeviceInfo | None = None
    error: WatlowError | None = None
```

Forward extension: `Reading.status_flags: Mapping[str, bool]` is not
present today — the codec doesn't surface a per-value status word
distinct from the type tag. `raw` is sufficient for diagnostics in the
meantime; add `status_flags` when a captured parameter actually needs
it.

## 7. Auto-detection ordering

Older Watlow controllers (Series 96, F4, early EZ-ZONE) ship from the
factory in **Standard Bus**; on those models Modbus RTU is the
front-panel-opt-in mode. Newer EZ-ZONE firmware can ship in either mode
depending on configuration. Probe order — Std Bus first, then Modbus —
matches the older-fleet bias and aligns with the protocol we have the
deepest RE coverage of:

1. Drain → Standard Bus probe (read parameter 1001 / Hardware ID at MAC
   `0x10`, no opcode sweeps). A valid `55 FF`-framed reply with correct
   header + data CRC = Std Bus.
2. Drain → Modbus RTU probe via `anymodbus`: open a `Bus`, call
   `slave(N).read_holding_registers(addr_for_1001, count=2)`. A valid
   CRC-correct response = Modbus.
3. Fail with a clear `WatlowError` listing what we tried.

Auto-detect never tries multiple bauds; the user sets one. Both probes
are read-only by construction. The detector accepts an `address: int`
arg (default `1`); broader address sweeps live in the separate
`find_devices()` / `watlow-discover` discovery path, not the
open-device path.

`watlow-discover` (and the underlying `find_devices()`) accept
repeatable `--baud` flags for environments where the configured baud
is not known; `open_device` never sweeps baud. Watlow PM ships at
38400 8N1 on Std Bus per the manuals, and Modbus RTU on the same
controller can be configured at 9600 / 19200 / 38400 / 57600 /
115200 — discovery defaults to `(38400, 19200, 9600)` when `--baud`
is not supplied.

## 8. Errors

`WatlowError` root with the same `ErrorContext` pattern. Layered
subclasses: `WatlowTransportError`/`Timeout`/`Connection`,
`WatlowFrameError`, `WatlowProtocolError`,
`WatlowProtocolUnsupportedError`,
`WatlowCapabilityError`/`Warning`,
`WatlowConfirmationRequiredError`, plus protocol-specific:

- Std Bus: `WatlowNoSuchObjectError` (0x81),
  `WatlowNoSuchAttributeError` (0x83),
  `WatlowNoSuchInstanceError` (0x84) — already typed in the existing
  payload module, just split out.
- Modbus: re-raise `anymodbus` exceptions wrapped in `WatlowError`
  subclasses (`WatlowModbusIllegalFunctionError`,
  `WatlowModbusIllegalDataAddressError`,
  `WatlowModbusIllegalDataValueError`,
  `WatlowModbusSlaveFailureError`, `WatlowModbusTimeoutError`) so callers
  see one error hierarchy regardless of protocol. `__cause__` preserves
  the original `anymodbus` exception for callers that need it.

`ErrorContext` is the structured payload every `WatlowError` carries:

```python
@dataclass(frozen=True, slots=True)
class ErrorContext:
    command_name: str | None             # registry parameter or command name
    protocol: ProtocolKind | None
    port: str | None
    address: int | None                  # bus address
    parameter_id: int | None             # set when a parameter was the target
    # Std Bus selector — set when failure is at the inner-payload layer
    cls: int | None = None
    member: int | None = None
    instance: int | None = None
    # Modbus selector — set when failure is at the Modbus layer
    register_address: int | None = None
    function_code: int | None = None
    # Wire-level
    request: bytes | None = None
    response: bytes | None = None
    elapsed_s: float | None = None
```

Selector fields are populated per-protocol: Std Bus failures fill
`cls` / `member` / `instance`; Modbus failures fill `register_address`
/ `function_code`. Logging policy: `Session.execute` emits one
structured event per call at DEBUG with command name + selector, and
one at WARNING on every error; raw bytes go to TRACE-equivalent
DEBUG-with-`raw` rather than the default DEBUG channel to keep
day-to-day log noise low.

## 9. What ports vs what's new

**Port from `watlowtesting/src/watlowlib/`** (mostly mechanical — change
package paths, add docstrings, fold into Variant API):

- `_crc.py` → `protocol/stdbus/_crc.py` (or fold into framing)
- `stdbus/frame.py` → `protocol/stdbus/framing.py`
- `stdbus/payload.py` → split: codec into `protocol/stdbus/payload.py`,
  type-tags into `protocol/stdbus/tlv.py`, errors into `errors.py`
- `stdbus/transport.py` → `protocol/stdbus/client.py` (the read-loop
  becomes the `ProtocolClient.execute` body)
- `stdbus/client.py` → folded into `commands/parameters.py` +
  `devices/controller.py`
- `data/pm_parameters.json`, `data/enumerations.json` → `data/` (verbatim)
- `tests/test_codec.py`, `tests/test_crc.py` → `tests/` (verbatim,
  repath imports)

**New work**:

- Modbus adapter subpackage — thin wrapper over `anymodbus` (no codec or
  framing of our own); just the `ModbusProtocolClient`, the data-type ↔
  register-block lookup, and the exception remap into `WatlowError`.
- Parameter registry that unifies the two protocols on top of
  `pm_parameters.json`
- `Controller` facade + `Session` + gates + `factory.open_device` +
  multi-loop `loop(n)` view
- `WatlowManager`, streaming, sinks, sync facade, CLIs
- Capability/family taxonomy & classification from part number
- Discovery (MS/TP MAC sweep, Modbus address sweep)

**Dependencies** (lean core, mirrors sartoriuslib):

```
anyio>=4.13
anyserial>=0.1
anymodbus>=0.1   # in-house, AnyIO + anyserial-based, also pre-alpha
```

Optional extras: `parquet` (pyarrow), `postgres` (asyncpg), `trio`
(secondary AnyIO backend in tests).

## 10. Open design questions

### Resolved

1. **Repo location** — `~/Documents/git/watlowlib/`, sibling to alicatlib
   / sartoriuslib. ✓
2. **Modbus library** — use the in-house `anymodbus` package (pinned
   `>=0.1,<0.2`); revisit the upper bound when anymodbus tags 0.1.x. ✓
3. **RE provenance docs** — migrated. The literature survey lives at
   [`docs/protocol-stdbus.md`](protocol-stdbus.md); the empirical
   findings live at
   [`docs/protocol-stdbus-findings.md`](protocol-stdbus-findings.md).
   The originals stay in `watlowtesting/` as historical RE provenance. ✓

4. **`address` parameter on `open_device`** — surface a single
   `address: int`. Each protocol client interprets and validates: the
   Std Bus client maps `1..16` to MS/TP MAC `0x10..0x1F` and rejects
   anything out of range; the Modbus client passes `1..247` to
   `anymodbus.Bus.slave()` and lets it enforce the spec range. The
   Manager always knows which protocol is locked to a port (§6) so
   address validation happens at `add(...)` time. ✓
5. **Loop indexing** — `controller.loop(1)` is 1-indexed throughout the
   public API. It matches Watlow's manuals and is the same number used
   for Std Bus `instance` and the Modbus `next_inst_offset` math from
   the registry. ✓
6. **Word order on Modbus floats** — bake `WordOrder.HIGH_LOW` /
   `ByteOrder.BIG` into the `ModbusProtocolClient` as the default for
   the registry-driven path; `ParameterSpec.word_order` is `None` in
   day-one data and the client default applies. When RE turns up a
   parameter with a different layout, populate the spec's
   `word_order` for that row only — no client change. ✓

### Still open

1. **Cache strategy for EEPROM-backed parameters.** Writes
   self-invalidate the in-session cache for the affected parameter
   (and any known dependents). Whether to add a polled config-counter
   parameter for bulk invalidation depends on whether any Watlow
   parameter reliably ticks on every persistent write — captures so
   far don't prove it. Default for v1: writes self-invalidate;
   everything else re-reads on demand.
2. **Raw Modbus escape hatch shape.** `anymodbus.Slave` exposes
   typed-FC methods, not a raw FC dispatch, so `raw_modbus(fc, ...)`
   doesn't sit naturally on the public surface. Two shapes are
   plausible: (a) typed raw methods on the facade
   (`raw_read_holding_registers`, `raw_write_register`, …), or
   (b) one `raw_modbus(op: ModbusOp)` that reuses the variant-layer
   instruction. (b) keeps the public surface small and reuses the
   already-tested dispatch. Lean (b); revisit if a real raw use case
   resists it. Std Bus already has a clean fit:
   `raw_stdbus(service, cls, member, instance, value=None)`.
3. **Family classification rules beyond PM.** PM part numbers are
   well-documented; RM, ST, F4T, and EZ-ZONE Limit decoders are not
   in the design today. Each family lands a `PartNumber` decoder when
   its parameter table arrives, additive to the `ControllerFamily`
   enum without changing the data shape.
