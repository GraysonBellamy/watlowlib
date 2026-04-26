"""Sample sinks — drop-in destinations for :func:`watlowlib.sinks.pipe`.

Every sink satisfies the :class:`SampleSink` Protocol so the recorder
glue (:func:`pipe`) can drain into any of them. Stdlib-only sinks
(:class:`InMemorySink`, :class:`JsonlSink`, :class:`CsvSink`,
:class:`SqliteSink`) ship in the core install. Heavier backends
(:class:`ParquetSink`, :class:`PostgresSink`) ship behind the
``watlowlib[parquet]`` / ``watlowlib[postgres]`` extras — the modules
import on bare-core installs (so ``from watlowlib.sinks import
ParquetSink`` works) and the dependency check is deferred to
:meth:`open`.

Design reference: ``docs/design.md`` §6.
"""

from __future__ import annotations

from watlowlib.sinks.base import SampleSink, pipe, sample_to_row
from watlowlib.sinks.csv import CsvSink
from watlowlib.sinks.jsonl import JsonlSink
from watlowlib.sinks.memory import InMemorySink
from watlowlib.sinks.parquet import ParquetSink
from watlowlib.sinks.postgres import PostgresConfig, PostgresSink
from watlowlib.sinks.sqlite import SqliteSink

__all__ = [
    "CsvSink",
    "InMemorySink",
    "JsonlSink",
    "ParquetSink",
    "PostgresConfig",
    "PostgresSink",
    "SampleSink",
    "SqliteSink",
    "pipe",
    "sample_to_row",
]
