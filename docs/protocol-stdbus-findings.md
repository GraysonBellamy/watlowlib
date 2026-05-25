---
description: Empirical Watlow Standard Bus findings reverse-engineered against a live EZ-ZONE PM3 — frame builder/parser, payload codec, and type-tag table verified on the wire.
---

# Watlow Standard Bus — empirical findings

Companion to [`protocol-stdbus.md`](protocol-stdbus.md) (literature
survey). This document records what was reverse-engineered against a
live **EZ-ZONE PM3** (part number `PM3R1CA-AAAAAAA`) on 2026-04-25 over
a B&B Electronics 485USBTB-2W USB↔EIA-485 adapter at 38400 8N1.

The library implementation — frame builder/parser, payload codec, and
type-tag table — is in `src/watlowlib/protocol/stdbus/` and every claim
below is pinned by a unit test against captured wire bytes
(see `tests/test_crc.py` and `tests/test_codec.py`).

## Wire format summary

```
+---------+-----+-----+-----+--------+------+----------+----------+
| 55 FF   | FT  | DST | SRC | LEN_BE | HCRC | PAYLOAD  | DCRC_LE  |
+---------+-----+-----+-----+--------+------+----------+----------+
                BACnet MS/TP outer frame                Watlow inner

Header CRC: BACnet MS/TP CRC-8 over [FT,DST,SRC,LEN_HI,LEN_LO]
Data CRC:   BACnet MS/TP CRC-16  over PAYLOAD, transmitted little-endian
```

### Outer (BACnet MS/TP) — fully confirmed

| Field        | Observed                                          | Notes |
|---           |---                                                |---|
| Preamble     | `55 FF`                                           | exactly per BACnet MS/TP |
| Frame type   | `0x05` request → `0x06` response                  | **only these two are honored on the wire** — see "Frame-type space" below |
| Dst MAC      | `0x10`..`0x1F` for controllers                    | `MAC = 0x0F + standard_bus_address` (1..16) |
| Src MAC      | `0x00` for the host                               | controller responds with its own MAC as src |
| Length       | 16-bit big-endian payload byte count              | |
| Header CRC   | one byte                                          | confirmed against 6 canonical sample frames + every live capture |
| Data CRC     | 16-bit, transmitted little-endian                 | confirmed |

### Inner (Watlow attribute service) — confirmed

The payload is a small tagged-attribute protocol that resembles CIP common
services in spirit, with Watlow-specific service codes.

```
request   ::= 01 SERVICE [MODE] CLASS MEMBER INSTANCE [VALUE]
response  ::= 02 SERVICE_OR_ERROR ...

SERVICE = 0x03 (read) | 0x04 (write)
ERROR   = SERVICE_OR_ERROR with high bit set; payload = 02 ERROR  (no echo)

VALUE   = TYPE_TAG [LENGTH] DATA
```

`MODE` only appears in **read requests** as the byte after `0x03`. In every
in-the-wild capture it is `0x01`. We confirmed it selects a service mode
(see "Block / multi-parameter reads" below). For all normal use,
`MODE = 0x01` (single-attribute read).

### Type tags — confirmed

| Tag    | Type                  | Wire format                                | Confirmed via                                |
|---     |---                    |---                                         |---                                           |
| `0x01` | unsigned 8-bit        | tag + 1 byte (no length)                   | param 3002 (Operations Page) = `01 02`       |
| `0x03` | unsigned 16-bit       | tag + 2 bytes BE (no length)               | param 3010 (Read Lock) = `03 00 05`          |
| `0x05` | unsigned 32-bit       | tag + 4 bytes BE (no length)               | param 16006 (Tick Counter) = `05 FB 9D 48 F7`|
| `0x06` | signed 32-bit         | tag + 4 bytes BE (no length)               | param 1001 (Hardware ID) = `06 00 00 00 1C` (= 28 = "ARM CPU") |
| `0x08` | IEEE-754 float32      | tag + 4 bytes BE (no length)               | param 4001 (PV)                              |
| `0x09` | short string          | tag + length byte + ASCII (NUL-terminable) | param 1009 (Part Number) = `09 10 PM3R1CA-AAAAAAA\0` |
| `0x0F` | packed integer / enum | tag + count-byte (= N words) + N×2 bytes BE| param 8003 (Heat Algo) = `0F 01 00 47` (= 71 = PID) |

The "Wide Enumeration" data type from the EZ-ZONE register list shares tag
`0x0F`. In every live capture so far the count byte is `1` (single 16-bit
word). We have not yet observed a Wide Enum that exceeds 16 bits, so the
multi-word case is implemented in the codec but unverified on the wire.

The "2 - unsigned 16 bit" data type from the spreadsheet (used on
parameters in the 20000 range — CIP pointers) is **not** a distinct wire
type. When read, it returns whatever type the pointed-to parameter uses.

### Error responses — confirmed

| Code   | Name                | Trigger                                                | Confirmed via                                     |
|---     |---                  |---                                                     |---                                                |
| `0x81` | `NO_SUCH_OBJECT`    | invalid class                                          | `read(99001)`, `read(0)`                          |
| `0x83` | `NO_SUCH_ATTRIBUTE` | valid class, invalid member                            | `read(4099)` (class 4, member 99)                 |
| `0x84` | `NO_SUCH_INSTANCE`  | valid class+member, invalid instance                   | `read(4001, instance=99)`, `read(7001, instance=2)` |

Error response payload is exactly two bytes: `02 <code>`. No echo of the
failing selector.

### Address mapping — confirmed

| Standard Bus address | MS/TP MAC |
|---:                  |---:       |
| host (PC)            | `0x00`    |
| controller 1         | `0x10`    |
| controller 2         | `0x11`    |
| ...                  | ...       |
| controller 16        | `0x1F`    |

Confirmed by reading the live PM3 at `0x10` and observing source MAC `0x10` in
every reply.

### "Parameter ID" encoding — corrected from research doc

**The literature-survey hypothesis (decimal-split: 4001 → digits 04, 01) was
wrong**. The wire bytes are `(Standard_Class, Standard_Member, Standard_Instance)`
read directly from the EZ-ZONE register list columns. The *Parameter ID*
shown in user manuals is just `Class * 1000 + Member`. Examples confirmed
on the wire:

| Param | Class | Member | Wire bytes (selector) |
|---:   |---:   |---:    |---                    |
| 4001  | 4     | 1      | `04 01`               |
| 7001  | 7     | 1      | `07 01`               |
| 8003  | 8     | 3      | `08 03`               |
| 1009  | 1     | 9      | `01 09`               |
| 16006 | 16    | 6      | `10 06`               |

The encoding is therefore one byte for class and one byte for member. No
edge cases yet found that break this (parameters above 25500 would overflow
member; the highest member observed in the PM register list is in the 80s).

## Reverse-engineering results

### CRCs (offline)

BACnet MS/TP CRC-8 (header) and CRC-16/CCITT (data) verified against all six
canonical frames from the literature survey. 12/12 CRC unit tests pass; the
frame round-trips byte-for-byte.

### Baseline replay

Three known-good reads against the live PM3:

| Param | Type   | Live value           | Notes |
|---:   |---     |---                   |---|
| 4001  | float  | ~65 °F (drifts)      | matches "Process Value" |
| 7001  | float  | 32.0                 | factory-default-ish setpoint |
| 8003  | enum   | 71 ("PID")           | matches Heat Algorithm enum in manual |

### Instance edges

The PM3 exposes only **instance 1** of every parameter probed.
`read(4001, instance=2..3)` and `read(7001, instance=2..3)` all return
`NO_SUCH_INSTANCE` (`0x84`). The "PM3" model designation does not refer to
loop count; the unit has a single control loop.

### Type-tag catalog

Seven distinct wire data-type tags discovered (table above). Live captures
for each are stored as fixtures in `tests/test_codec.py`. The catalog accounts
for every type seen in the EZ-ZONE register list except:

- `unsigned 32-bit` was confirmed via param 16006 (tag `0x05`).
- `Member Based` is a software construct (parameter is a pointer; type comes
  from the target).
- `2 - unsigned 16 bit` resolves to whatever target the CIP pointer points to.
- `Wide Enumeration` shares tag `0x0F` with `Enumeration`; only the count
  byte differs in principle. Multi-word (count ≥ 2) packed-int responses
  remain unobserved.

### Error responses

All three error classes seen exactly. No timeouts, no malformed responses.
The error envelope is the simplest possible: `02 <code>`.

### Frame-type space

Sent four non-`0x05` frames at the live PM3. **All four returned silence.**

| Probe                                        | Wire                                | Reply |
|---                                           |---                                  |---|
| Poll-For-Master (frame type 0x01)            | `55 FF 01 10 00 00 00 F4`           | none |
| Test-Request (0x03) with payload "Watlow"    | `55 FF 03 10 00 00 06 F9 ...`       | none |
| Data-Not-Expecting-Reply (0x06) with read    | `55 FF 06 10 00 00 06 61 ...`       | none |
| `0x05` with corrupted header CRC             | `55 FF 05 10 00 00 06 17 ...`       | none |

Conclusion: **the PM3 is not a participating MS/TP node**. It only handles
frame type `0x05`. Bad header CRC is silently dropped. EZ-ZONE Configurator's
"autodiscovery" therefore must be brute-force address polling with `0x05`
read frames.

### Block / multi-parameter reads (partial)

The byte after function `0x03` in read requests is **not a count of
parameters**, contrary to the natural intuition. Empirically:

| Mode byte | Behavior                                                                 |
|---:       |---                                                                       |
| `0x00`    | Returns one read response (echoing the first/implicit selector)         |
| `0x01`    | Canonical single-attribute read (used everywhere in the wild)            |
| `0x02`    | Returns **multiple typed values** for the addressed instance — looks like a CIP-style "Get_Attributes_All" service. Response shape includes a sub-header (`04 46 08 02 01 01 32`) that is **not yet decoded**, followed by several typed values. For class 4 (AIN) instance 1 we receive at least: PV (member 1), Range Low (member 17), Range High (member 18). |
| `0x03`+   | Echo single-attribute response of the first selector; subsequent selectors ignored. |

For library v1 we expose only mode `0x01`. Mode `0x02` is documented as a
future feature pending more capture work.

## Open items

- **Service mode `0x02` (Get_Attributes_All)** payload decoding: identify
  the sub-header, the per-attribute envelope, and the attribute-ID encoding.
  Likely requires capturing `EZ-ZONE Configurator` doing a full settings dump
  (it almost certainly uses this service).
- **Wide-enum count ≥ 2** behavior: have not yet found a Wide Enum value that
  exceeds 16 bits.
- **Save-to-EEPROM / non-volatile commit semantics**: empirically
  confirmed on a lab PM3 (wire captures under
  `captures/2026-04-26/`). Writes to RWE parameters (we tested 17009
  Protocol) **persist across power cycles automatically** when 17051
  (Non-Volatile Save) is set to 106 (Yes), which is the factory
  default. 17051 itself is a sticky mode, not a one-shot trigger —
  it stayed at 106 across multiple unrelated writes. The "two-phase
  commit" hypothesis was wrong: RWE writes are single-step persistent
  on this firmware.
- **Profile / recipe upload-download**: the F4T family supports profile
  upload via Standard Bus; the PM has its own profile-step parameters
  (class 19+). Not yet exercised.
- **Other function codes**: only `0x03` (read) and `0x04` (write) confirmed.
  We deliberately did not probe arbitrary function codes against the live
  load. This should be done on a sacrificial controller before opening up
  exotic services.
- **F4T Ethernet Watbus (port 44819)**: out of scope for this library
  iteration.
