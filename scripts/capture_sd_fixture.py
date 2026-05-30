"""Capture a read-only Series SD round-trip from COM11 into a JSONL fixture.

READS ONLY -- no writes to the live unit. Run once on the bench rig.
"""

from __future__ import annotations

import json
from pathlib import Path

import anyio

from watlowlib import SERIES_SD_PROFILE, open_device
from watlowlib.registry.parameters import SD_PARAMETERS

PORT = "COM11"
ADDRESS = 10
FIXTURE = Path("tests/fixtures/sd_modbus_pv_setpoint.jsonl")
# Read-only registers captured for the fixture, in order.
READS = ["process_value", "setpoint", "output_power", "units", "input_error"]


def _words_from_raw(raw: bytes) -> list[int]:
    return [int.from_bytes(raw[i : i + 2], "big") for i in range(0, len(raw), 2)]


async def main() -> None:
    """Capture the configured SD reads and rewrite the JSONL fixture."""
    rows: list[dict[str, object]] = [
        {
            "kind": "header",
            "protocol": "modbus_rtu",
            "address": ADDRESS,
            "baudrate": 9600,
            "parity": "none",
            "port": "fake://sd-modbus",
        }
    ]
    ctl = await open_device(
        PORT,
        profile=SERIES_SD_PROFILE,
        address=ADDRESS,
        identify=False,
    )
    async with ctl:
        for name in READS:
            spec = SD_PARAMETERS.resolve(name)
            entry = await ctl.read_parameter(name)
            words = _words_from_raw(entry.raw)
            rows.append(
                {
                    "protocol": "modbus_rtu",
                    "label": f"read_{name}",
                    "method": "read_holding_registers",
                    "address": spec.relative_addr,
                    "count": len(words),
                    "response_words": words,
                }
            )
            print(f"{name:16} regs={words} -> {entry.value!r}")  # noqa: T201

    await anyio.Path(FIXTURE).write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {FIXTURE}")  # noqa: T201


if __name__ == "__main__":
    anyio.run(main)
