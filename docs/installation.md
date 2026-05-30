---
description: Install watlowlib from PyPI for async Watlow temperature controller I/O on Python 3.13+, with optional Parquet and Postgres sink extras.
---

# Installation

```bash
pip install watlowlib
```

Requires **Python 3.13 or newer**. Core dependencies are
[`anyio`](https://pypi.org/project/anyio/) (async runtime),
[`anyserial`](https://pypi.org/project/anyserial/) (cross-platform
serial I/O), and
[`anymodbus`](https://github.com/GraysonBellamy/anymodbus) (Modbus RTU
codec).

## Optional extras

```bash
pip install 'watlowlib[parquet]'    # ParquetSink (pyarrow)
pip install 'watlowlib[postgres]'   # PostgresSink (asyncpg)
pip install 'watlowlib[trio]'       # run the async core on the trio backend
pip install 'watlowlib[docs]'       # build the docs locally
```

CSV, JSONL, SQLite, and InMemory sinks need no extras — they use only
the standard library. See [Logging and acquisition](logging.md) for the
sink surface and [Sinks API](api/sinks.md) for the reference.

## Platform support

Linux, macOS, BSD, and Windows are supported via
[`anyserial`](https://pypi.org/project/anyserial/) (readiness-driven
I/O on POSIX, IOCP on Windows). Serial-port enumeration uses
`anyserial.list_serial_ports()` natively.

On Linux, the user running `watlow-*` commands must have read/write
access to the serial device — usually achieved by adding the user to
the `dialout` group (`sudo usermod -aG dialout $USER`) and re-logging.

## Wiring quick reference

EZ-ZONE PM and similar controllers expose EIA-485 half-duplex on a
two-wire bus. Default settings:

| Protocol      | Baud  | Framing | Address range |
| ------------- | ----- | ------- | ------------- |
| Standard Bus  | 38400 | 8-N-1   | 1–16          |
| Modbus RTU    | 9600  | 8-N-1   | 1–247         |

A USB-to-RS-485 adapter is the usual host-side connection. Daisy-chain
up to 16 EZ-ZONE PM controllers on one Standard Bus segment (4000 ft
maximum cable length); terminate one end with a 120 Ω resistor. See
[Standard Bus protocol reference](protocol-stdbus.md) and
[Modbus RTU mapping](protocol-modbus.md).
