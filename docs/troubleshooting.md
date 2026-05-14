# Troubleshooting

This page collects the failure modes that come up first in the field —
serial mis-config, factory protocol defaults, SKU-vs-protocol
mismatches, and the typed errors the library raises when something is
off. The CLI tools in [`watlowlib.cli`](api/watlowlib.md) cover most of
the investigation paths.

## "I just wired up a controller — what protocol is it speaking?"

**Every EZ-ZONE PM ships from the factory in Standard Bus** at
**38400 8-N-1**, address 1. Switching to Modbus RTU requires either an
SKU that includes the Modbus stack (comms position-8 ∈ `1`, `2`, `B`,
`D`, `E`, `F`) and a front-panel mode flip, *or* a protocol-mode
write through `change_protocol_mode(...)`.

First-contact paths:

```python
from watlowlib import ProtocolKind, open_device

# Standard Bus is the right first guess.
async with await open_device("/dev/ttyUSB0", protocol=ProtocolKind.STDBUS, address=1) as ctl:
    info = await ctl.identify()

# Modbus RTU on a controller you've already flipped.
async with await open_device(
    "/dev/ttyUSB0",
    protocol=ProtocolKind.MODBUS_RTU,
    address=1,
    serial_settings=SerialSettings(port="/dev/ttyUSB0", baudrate=9600),
) as ctl:
    info = await ctl.identify()

# Auto — Std Bus probe → Modbus probe → fail.
async with await open_device("/dev/ttyUSB0", protocol=ProtocolKind.AUTO) as ctl:
    print(ctl.session.protocol, ctl.info.part_number.raw)
```

`AUTO` is conservative: it never sweeps baud or parity. Wider port
discovery lives in `watlow-discover`.

## Per-protocol factory defaults

| Protocol      | Baud  | Framing | Address | Notes |
| ------------- | ----- | ------- | ------- | ----- |
| Standard Bus  | 38400 | 8-N-1   | 1       | Built-in on every EZ-ZONE PM. Address parameter is `[Ad;S]` (1–16). |
| Modbus RTU    | 9600  | 8-N-1   | 1       | Optional comms stack. Address parameter is `[Ad;m]` (1–247). |

Both share the same EIA-485 hardware port — they're a software-mode
flip, not separate connectors.

## `watlow-discover`

```bash
watlow-discover                                                # scan all ports, defaults
watlow-discover --port /dev/ttyUSB0                            # one port, defaults
watlow-discover --port /dev/ttyUSB0 --addresses 1-16           # multi-drop sweep
watlow-discover --port /dev/ttyUSB0 --protocol modbus_rtu --baud 9600
```

Probes the cartesian product of ports × bauds × protocols × addresses
and prints one [`FindResult`](api/devices.md) per probe attempt. With
no `--port` flag the CLI enumerates every visible serial port via
`anyserial`; defaults hit address `1` at `38400 / 19200 / 9600` on
both protocols, so a typical four-port rig is scanned in under 15
seconds. Pass `--responsive-only` to suppress silent rows.

If `watlow-discover` finds nothing:

1. Verify the port path. On Linux, `ls /dev/ttyUSB*` and `lsusb`
   confirm the adapter is attached.
2. Verify permission. The user needs read/write on the device, usually
   via `sudo usermod -aG dialout $USER`.
3. Verify the bus wiring — A/B polarity, common ground, end-of-line
   termination on long runs.
4. Try the other protocol's defaults (table above).
5. As a last resort, use `watlow-diag tap` to passively watch the line
   while toggling the front-panel protocol switch.

## `watlow-read`

```bash
watlow-read --port /dev/ttyUSB0 --address 1 -p process_value
```

Open, identify, read one parameter, exit. The fastest sanity check
that a controller is alive and decoded correctly.

## SKU-vs-protocol mismatches

EZ-ZONE PM SKUs encode the comms options as **position 8** of the
part-number string — the first character of the trailing options
block. `PM3R1CA-AAAAAAA` has comms code `A`: Standard Bus only, no
Modbus stack. Position-8 codes that *do* carry Modbus are `1`, `2`,
`B`, `D`, `E`, `F`.

| Symptom | Likely cause | Fix |
| ------- | ------------ | --- |
| `change_protocol_mode(MODBUS_RTU)` "succeeds" but the controller never answers Modbus frames afterwards | Std-Bus-only SKU (comms position-8 = `A`); the parameter write lands but the device has no Modbus stack to start | Flip back to Std Bus and use Modbus only on a comms-equipped SKU. The library now gates this on `Capability.HAS_MODBUS` and raises `WatlowConfigurationError` instead. |
| `identify()` reports `protocol_mismatch=True` | EEPROM parameter 17009 disagrees with the active wire protocol | Either flip the device to match (`change_protocol_mode`) or open with the protocol the device is actually serving. |
| `WatlowTimeoutError` on every read after a `change_protocol_mode` write | The controller did not answer at the target framing during verification | Re-open the controller with the target protocol's serial settings (38400 for Std Bus, 9600 for Modbus) and power-cycle if the firmware applies the change only after restart. |

## Common typed errors

| Error                                          | Meaning                                                                  | What to do |
| ---------------------------------------------- | ------------------------------------------------------------------------ | ---------- |
| `WatlowConnectionError`                        | Transport open failed                                                    | Check port path, permissions, cable. |
| `WatlowTimeoutError`                           | No reply within `timeout`                                                | Wrong protocol? Wrong baud? Wrong address? |
| `WatlowFrameError`                             | Std Bus framing or CRC invalid                                           | Line noise, electrical fault, or running Std Bus parser against a Modbus stream. |
| `WatlowProtocolUnsupportedError`               | Active protocol has no variant for this command                          | Switch protocol, or check the command's variant table. |
| `WatlowNoSuchObjectError` / `WatlowNoSuchAttributeError` / `WatlowNoSuchInstanceError` | Std Bus device returned a "no such" code      | Parameter or instance not present on this SKU; check `info.capabilities` and `info.loops`. |
| `WatlowModbusIllegalDataAddressError`          | Modbus device returned exception code 02                                 | Register not implemented for this SKU; same gap as the Std Bus "no such" codes. |
| `WatlowModbusIllegalDataValueError`            | Modbus device returned exception code 03                                 | Bad argument — clamp to the documented range. |
| `WatlowConfirmationRequiredError`              | `PERSISTENT` op without `confirm=True`                                   | Add `confirm=True` if the operation is intentional — see [Safety](safety.md). |
| `WatlowCapabilityError`                        | Reserved capability-gate error class; not currently emitted by the session | Treat as a future-compatible subclass of `WatlowError` if you catch it. |
| `WatlowFirmwareError`                          | Reserved firmware-gate error class; `min_firmware` is metadata in v1     | Treat as a future-compatible subclass of `WatlowCapabilityError` if you catch it. |
| `WatlowValidationError`                        | Pre-flight host-side validation refused the call                         | Bad parameter name, instance out of range, or value outside the parsed range. |
| `WatlowSinkDependencyError`                    | Sink module imported without its optional backend                        | `pip install 'watlowlib[parquet]'` / `[postgres]`. |

## Offline decode with `watlow-decode`

When a wire-trace dump shows up in a bug report or RE session, decode
it without hardware:

```bash
watlow-decode "55 FF 05 10 00 00 06 E8 01 03 01 04 01 01 E3 99"
```

`watlow-decode` understands the Std Bus BACnet MS/TP outer frame, the
Watlow attribute payload (read/write request/response shapes), and does
not open a serial port. Output is JSON-friendly; pipe to `jq` for
filtering.

## Diagnostics CLI (`watlow-diag`)

Reverse-engineering tools live under the `watlow-diag` namespace. They
are deliberately separate from the stable CLI because they can write
to the device:

| Subcommand              | Purpose |
| ----------------------- | ------- |
| `watlow-diag snapshot`  | Dump every read-only parameter the controller answers for a given identified family. |
| `watlow-diag stream`    | Continuous sample stream for jitter / framing analysis. |
| `watlow-diag sweep`     | Walk a parameter-id range and record responses. |
| `watlow-diag argfuzz`   | Probe a parameter's argument space. Destructive ops gated. |
| `watlow-diag tap`       | Passively watch the line and decode in real time. |
| `watlow-diag detect-framing` | Identify framing on an unknown-protocol line. |

Destructive subcommands require an explicit
`--i-understand-this-is-destructive` flag. Never invoked from normal
discovery or `open_device`.

## Forced protocol switch

If the controller is in the wrong mode and the front panel isn't
practical:

```bash
watlow-configure change-protocol-mode /dev/ttyUSB0 --address 1 --target modbus_rtu --confirm
```

The CLI thinly wraps
[`maintenance.change_protocol_mode(...)`](api/maintenance.md), gated
on the SKU's comms-code check and `confirm=True`. See
[Safety](safety.md).

## Capturing for a bug report

Useful artefacts:

- The raw command and reply bytes — `watlow-diag tap` for a passive
  capture, or the controller's `info` if `identify()` succeeds.
- `info` snapshot — part number, family, capabilities, configured vs
  active protocol.
- `watlow-diag snapshot` output — read-only parameter survey.
- The CLI version banner: `python -c "import watlowlib; print(watlowlib.__version__)"`.

## See also

- [Standard Bus protocol](protocol-stdbus.md) — frame layout, opcode
  tables, error codes.
- [Modbus RTU mapping](protocol-modbus.md) — register addressing,
  exception codes.
- [Safety](safety.md) — `confirm=True` and gate order.
- [Testing](testing.md) — `FakeTransport` for hardware-free repros.
