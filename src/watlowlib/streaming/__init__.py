"""Streaming primitives — :func:`record` + :class:`Sample`.

The streaming layer drives a :class:`PollSource` (a
:class:`~watlowlib.devices.controller.Controller` or
:class:`~watlowlib.manager.WatlowManager`) at an absolute-target
cadence and publishes :class:`Sample` batches into an async receive
stream. Pair with :func:`watlowlib.sinks.pipe` to drain into a
:class:`~watlowlib.sinks.SampleSink`.

Design reference: ``docs/design.md`` §6.
"""

from __future__ import annotations

from watlowlib.streaming.adapter import PollSourceAdapter
from watlowlib.streaming.recorder import (
    AcquisitionSummary,
    OverflowPolicy,
    PollSource,
    Recording,
    record,
)
from watlowlib.streaming.sample import Sample

__all__ = [
    "AcquisitionSummary",
    "OverflowPolicy",
    "PollSource",
    "PollSourceAdapter",
    "Recording",
    "Sample",
    "record",
]
