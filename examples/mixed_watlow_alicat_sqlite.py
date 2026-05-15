"""Mixed-vendor SQLite recording — Watlow + Alicat in one queryable file.

Demonstrates the cross-vendor row schema. Both libraries emit
long-format rows ``(device, address, protocol, parameter, parameter_id,
instance, value, unit, t_mono_ns, t_utc, requested_at, received_at,
latency_s)``, so a single SQLite table holds rows from both vendors and
ordinary SQL aggregates work across them.

Runs against ``FakeTransport`` fixture data — no hardware required.

This file is referenced from ``docs/streaming.md``.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import anyio

from watlowlib import (
    FakeTransport,
    ProtocolKind,
    Sample,
    SerialSettings,
    SqliteSink,
    WatlowManager,
    pipe,
    record,
)
from watlowlib.testing import open_test_controller

# --- Captured PM3 PV (4001) round-trip — same fixture as the test suite. ----
_REQ_READ_PV = bytes.fromhex("55 FF 05 10 00 00 06 E8 01 03 01 04 01 01 E3 99")
_RSP_READ_PV = bytes.fromhex(
    "55 FF 06 00 10 00 0B 88 02 03 01 04 01 01 08 45 1E 3C D4 A7 28",
)


def _fake_alicat_samples() -> list[Sample]:
    """Synthesise a few rows that look like an Alicat MFC poll.

    Real Alicat code would explode each frame field (pressure,
    temperature, mass flow, ...) into one row per field. We construct
    them directly here to keep the example self-contained.
    """
    now = datetime.now(UTC)

    def _row(parameter: str, parameter_id: int, value: float, unit: str) -> Sample:
        return Sample(
            device="mfc1",
            address=1,
            protocol=ProtocolKind.STDBUS,  # placeholder; alicat would use its own enum
            parameter=parameter,
            parameter_id=parameter_id,
            instance=1,
            value=value,
            unit=unit,
            t_mono_ns=0,
            t_utc=now,
            t_midpoint_mono_ns=None,
            requested_at=now,
            received_at=now,
            latency_s=0.001,
            raw=b"",
        )

    return [
        _row("pressure", 9001, 14.7, "psia"),
        _row("temperature", 9002, 23.4, "C"),
        _row("mass_flow", 9003, 12.5, "sccm"),
    ]


async def main(out_path: Path) -> None:
    transport = FakeTransport({_REQ_READ_PV: _RSP_READ_PV})

    async with SqliteSink(out_path, table="samples") as sink:
        # --- Watlow side: record at 5 Hz for ~0.4 s through a manager. -----
        controller = await open_test_controller(
            transport,
            protocol=ProtocolKind.STDBUS,
            address=1,
            serial_settings=SerialSettings(port="fake://watlow1"),
        )
        async with controller, WatlowManager() as mgr:
            await mgr.add("watlow1", controller)
            async with record(
                mgr,
                parameters=["process_value"],
                rate_hz=5.0,
                duration=0.4,
            ) as recording:
                summary = await pipe(recording.stream, sink, batch_size=4, flush_interval=0.1)
        print(
            f"watlow recorded {summary.samples_emitted} samples "
            f"in {(summary.finished_at - summary.started_at).total_seconds():.2f}s",
        )

        # --- Fake-Alicat side: write three synthetic rows directly. -------
        await sink.write_many(_fake_alicat_samples())

    # --- Query: cross-vendor aggregation in plain SQL. -------------------
    conn = sqlite3.connect(str(out_path))
    try:
        per_device = conn.execute(
            "SELECT device, COUNT(*) FROM samples GROUP BY device ORDER BY device",
        ).fetchall()
        per_parameter = conn.execute(
            "SELECT parameter, COUNT(*) FROM samples GROUP BY parameter ORDER BY parameter",
        ).fetchall()
    finally:
        conn.close()

    print("per-device counts:", per_device)
    print("per-parameter counts:", per_parameter)


if __name__ == "__main__":
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(tempfile.gettempdir()) / "mixed.sqlite"
    if out.exists():
        out.unlink()
    anyio.run(main, out)
