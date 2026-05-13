"""Tests for :mod:`watlowlib.sinks` — Sink Protocol contract + per-sink behavior."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest

from watlowlib import (
    CsvSink,
    InMemorySink,
    JsonlSink,
    ProtocolKind,
    Sample,
    SampleSink,
    SqliteSink,
    Unit,
    WatlowSinkSchemaError,
    pipe,
    sample_to_row,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence
    from pathlib import Path


def _sample(
    *,
    device: str = "ctl1",
    parameter: str = "process_value",
    parameter_id: int = 4001,
    value: float | int | str | bool | None = 72.4,
    address: int = 1,
    protocol: ProtocolKind = ProtocolKind.STDBUS,
) -> Sample:
    now = datetime.now(UTC)
    return Sample(
        device=device,
        address=address,
        protocol=protocol,
        parameter=parameter,
        parameter_id=parameter_id,
        instance=1,
        value=value,
        unit=None,
        monotonic_ns=12345,
        requested_at=now,
        received_at=now,
        midpoint_at=now,
        latency_s=0.001,
        raw=b"\x00\x01",
    )


# ---------------------------------------------------------------------------
# sample_to_row
# ---------------------------------------------------------------------------


def test_sample_to_row_includes_long_format_keys() -> None:
    row = sample_to_row(_sample())
    expected_keys = {
        "device",
        "address",
        "protocol",
        "parameter",
        "parameter_id",
        "instance",
        "value",
        "unit",
        "requested_at",
        "received_at",
        "midpoint_at",
        "latency_s",
    }
    assert set(row.keys()) == expected_keys
    # ``raw`` is intentionally absent from tabular rows — bytes don't
    # round-trip cleanly through CSV / SQLite affinities.
    assert "raw" not in row
    # Protocol stored as the string value, not the enum.
    assert row["protocol"] == "stdbus"


def test_sample_to_row_coerces_bools_to_strings() -> None:
    """Bools render as ``"true"``/``"false"`` so SQLite doesn't pin the column to INTEGER."""
    row = sample_to_row(_sample(value=True))
    assert row["value"] == "true"
    row = sample_to_row(_sample(value=False))
    assert row["value"] == "false"


def test_sample_to_row_passes_none_through() -> None:
    row = sample_to_row(_sample(value=None))
    assert row["value"] is None


def _sample_with_unit(unit: Unit | str | None) -> Sample:
    now = datetime.now(UTC)
    return Sample(
        device="ctl1",
        address=1,
        protocol=ProtocolKind.STDBUS,
        parameter="process_value",
        parameter_id=4001,
        instance=1,
        value=72.4,
        unit=unit,
        monotonic_ns=0,
        requested_at=now,
        received_at=now,
        midpoint_at=now,
        latency_s=0.0,
        raw=b"",
    )


def test_sample_to_row_serialises_unit_enum_as_value() -> None:
    """``Unit.FAHRENHEIT`` becomes the string ``"F"`` in the flat row."""
    assert sample_to_row(_sample_with_unit(Unit.FAHRENHEIT))["unit"] == "F"
    assert sample_to_row(_sample_with_unit(Unit.CELSIUS))["unit"] == "C"
    assert sample_to_row(_sample_with_unit(Unit.PERCENT))["unit"] == "%"


def test_sample_to_row_passes_string_unit_through() -> None:
    """Cross-vendor rows (Alicat ``"psia"``, ``"sccm"``) stay as-is."""
    assert sample_to_row(_sample_with_unit("psia"))["unit"] == "psia"
    assert sample_to_row(_sample_with_unit("sccm"))["unit"] == "sccm"


def test_sample_to_row_passes_none_unit_through() -> None:
    assert sample_to_row(_sample_with_unit(None))["unit"] is None


# ---------------------------------------------------------------------------
# Sink contract test — parametrized
# ---------------------------------------------------------------------------


@pytest.fixture
def sample_batch() -> list[Sample]:
    return [
        _sample(device="ctl1", parameter="process_value", value=72.4),
        _sample(device="ctl1", parameter="setpoint", parameter_id=7001, value=75.0),
    ]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "kind",
    [
        "memory",
        "jsonl",
        "csv",
        "sqlite",
    ],
)
async def test_sink_contract(
    anyio_backend: object,
    tmp_path: Path,
    sample_batch: list[Sample],
    kind: str,
) -> None:
    """Every sink: open → write_many → close round-trip; idempotent close."""
    _ = anyio_backend
    sink: SampleSink
    if kind == "memory":
        sink = InMemorySink()
    elif kind == "jsonl":
        sink = JsonlSink(tmp_path / "out.jsonl")
    elif kind == "csv":
        sink = CsvSink(tmp_path / "out.csv")
    elif kind == "sqlite":
        sink = SqliteSink(tmp_path / "out.sqlite")
    else:  # pragma: no cover — exhaustive parametrize above
        raise AssertionError(kind)

    async with sink:
        await sink.write_many(sample_batch)
        # Second batch — exercises the schema-locked path on tabular sinks.
        await sink.write_many([_sample(parameter="output_power", value=42.0)])

    # Idempotent close (called by __aexit__, then again).
    await sink.close()


# ---------------------------------------------------------------------------
# Per-sink integrations
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_in_memory_sink_collects(
    anyio_backend: object,
    sample_batch: list[Sample],
) -> None:
    _ = anyio_backend
    sink = InMemorySink()
    async with sink:
        await sink.write_many(sample_batch)
    assert sink.samples == sample_batch


@pytest.mark.anyio
async def test_jsonl_sink_round_trip(
    anyio_backend: object,
    tmp_path: Path,
    sample_batch: list[Sample],
) -> None:
    _ = anyio_backend
    out = tmp_path / "out.jsonl"
    async with JsonlSink(out) as sink:
        await sink.write_many(sample_batch)
    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == len(sample_batch)
    parsed = [json.loads(line) for line in lines]
    assert parsed[0]["parameter"] == "process_value"
    assert parsed[1]["parameter"] == "setpoint"


@pytest.mark.anyio
async def test_jsonl_sink_appends_across_opens(
    anyio_backend: object,
    tmp_path: Path,
    sample_batch: list[Sample],
) -> None:
    """Re-opening an existing JSONL file extends it; pre-existing content survives."""
    _ = anyio_backend
    out = tmp_path / "out.jsonl"
    out.write_text('{"prior": "row"}\n', encoding="utf-8")

    async with JsonlSink(out) as sink:
        await sink.write_many(sample_batch[:1])
    async with JsonlSink(out) as sink:
        await sink.write_many(sample_batch[1:])

    lines = out.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 + len(sample_batch)
    assert json.loads(lines[0]) == {"prior": "row"}
    assert json.loads(lines[1])["parameter"] == "process_value"
    assert json.loads(lines[2])["parameter"] == "setpoint"


@pytest.mark.anyio
async def test_csv_sink_truncates_on_open(
    anyio_backend: object,
    tmp_path: Path,
    sample_batch: list[Sample],
) -> None:
    """CsvSink overwrites: the schema is locked per-run, cross-run append isn't supported."""
    _ = anyio_backend
    out = tmp_path / "out.csv"
    out.write_text("PRE-EXISTING-CONTENT\n", encoding="utf-8")

    async with CsvSink(out) as sink:
        await sink.write_many(sample_batch)

    text = out.read_text(encoding="utf-8")
    assert "PRE-EXISTING-CONTENT" not in text
    # Fresh header + len(sample_batch) rows.
    assert text.count("\n") == len(sample_batch) + 1


@pytest.mark.anyio
async def test_csv_sink_locks_columns(
    anyio_backend: object,
    tmp_path: Path,
    sample_batch: list[Sample],
) -> None:
    _ = anyio_backend
    out = tmp_path / "out.csv"
    sink = CsvSink(out)
    async with sink:
        await sink.write_many(sample_batch)
        assert sink.columns is not None
        assert "parameter" in sink.columns
    text = out.read_text(encoding="utf-8")
    # Header + 2 rows.
    assert text.count("\n") == 3


@pytest.mark.anyio
async def test_sqlite_sink_creates_table_and_inserts(
    anyio_backend: object,
    tmp_path: Path,
    sample_batch: list[Sample],
) -> None:
    _ = anyio_backend
    out = tmp_path / "out.sqlite"
    async with SqliteSink(out, table="samples") as sink:
        await sink.write_many(sample_batch)
        assert sink.columns is not None

    conn = sqlite3.connect(str(out))
    try:
        rows = conn.execute(
            "SELECT device, parameter, value FROM samples ORDER BY parameter",
        ).fetchall()
    finally:
        conn.close()
    # Sorted by parameter: process_value < setpoint
    assert rows == [("ctl1", "process_value", 72.4), ("ctl1", "setpoint", 75.0)]


@pytest.mark.anyio
async def test_sqlite_sink_create_table_false_missing_table_raises(
    anyio_backend: object,
    tmp_path: Path,
) -> None:
    _ = anyio_backend
    out = tmp_path / "out.sqlite"
    sink = SqliteSink(out, table="missing", create_table=False)
    with pytest.raises(WatlowSinkSchemaError):
        await sink.open()


# ---------------------------------------------------------------------------
# pipe() driver
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_pipe_drains_stream_into_sink(
    anyio_backend: object,
    sample_batch: list[Sample],
) -> None:
    _ = anyio_backend

    async def _gen() -> AsyncIterator[Sequence[Sample]]:
        yield sample_batch
        yield [_sample(parameter="output_power", value=42.0)]

    sink = InMemorySink()
    await sink.open()
    summary = await pipe(_gen(), sink, batch_size=10)
    assert summary.samples_emitted == 3
    assert len(sink.samples) == 3
    await sink.close()


@pytest.mark.anyio
async def test_pipe_rejects_bad_args(anyio_backend: object) -> None:
    _ = anyio_backend

    async def _empty() -> AsyncIterator[Sequence[Sample]]:
        # An empty async iterator — yields nothing.
        return
        yield  # unreachable; satisfies the AsyncIterator return type

    sink = InMemorySink()
    await sink.open()
    with pytest.raises(ValueError, match="batch_size"):
        await pipe(_empty(), sink, batch_size=0)
    with pytest.raises(ValueError, match="flush_interval"):
        await pipe(_empty(), sink, flush_interval=0)
    await sink.close()


# ---------------------------------------------------------------------------
# Cross-vendor schema test
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_cross_vendor_schema_in_one_sqlite_file(
    anyio_backend: object,
    tmp_path: Path,
) -> None:
    """A row from Watlow and a row from a fake-Alicat-shaped sink fit the same query.

    Mixed-vendor recordings must unify under one schema. This test
    exercises the long-format key set: the Watlow sink emits one row
    per parameter; a synthetic Alicat-shaped sink emits one row per
    *frame field* with the same column names. Both end up in one
    queryable SQLite file.
    """
    _ = anyio_backend
    out = tmp_path / "mixed.sqlite"

    # Watlow side — one parameter, one row.
    watlow_sample = _sample(device="watlow1", parameter="process_value", value=72.4)
    # Fake "Alicat" rows — same long-format schema, different vendor name.
    # In practice the alicat sink would explode each frame field into a
    # row; here we just construct two rows directly to prove they share
    # the schema.
    alicat_p = _sample(device="mfc1", parameter="pressure", parameter_id=9001, value=14.7)
    alicat_t = _sample(
        device="mfc1",
        parameter="temperature",
        parameter_id=9002,
        value=23.4,
    )

    async with SqliteSink(out, table="samples") as sink:
        await sink.write_many([watlow_sample, alicat_p, alicat_t])

    conn = sqlite3.connect(str(out))
    try:
        # Cross-vendor query: distinct devices in one table.
        devices = conn.execute(
            "SELECT DISTINCT device FROM samples ORDER BY device",
        ).fetchall()
        # Per-device parameter count.
        per_device = conn.execute(
            "SELECT device, COUNT(*) FROM samples GROUP BY device ORDER BY device",
        ).fetchall()
    finally:
        conn.close()

    assert devices == [("mfc1",), ("watlow1",)]
    assert per_device == [("mfc1", 2), ("watlow1", 1)]
