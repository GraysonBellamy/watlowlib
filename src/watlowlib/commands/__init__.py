"""Command descriptors — pure ``(ctx, request) → response`` variants.

The workhorse pair is :data:`READ_PARAMETER` / :data:`WRITE_PARAMETER`.
Every public-API method on
:class:`watlowlib.devices.controller.Controller` lowers to these two
commands; specialised commands (PID, alarms, profile upload) live
alongside.
"""

from __future__ import annotations

from watlowlib.commands.base import (
    Command,
    CommandContext,
    ModbusVariant,
    StdBusVariant,
)
from watlowlib.commands.loop import (
    PidGains,
    read_output,
    read_pid,
    write_pid,
)
from watlowlib.commands.parameters import (
    READ_PARAMETER,
    WRITE_PARAMETER,
    ReadParameterRequest,
    WriteParameterRequest,
)

__all__ = [
    "READ_PARAMETER",
    "WRITE_PARAMETER",
    "Command",
    "CommandContext",
    "ModbusVariant",
    "PidGains",
    "ReadParameterRequest",
    "StdBusVariant",
    "WriteParameterRequest",
    "read_output",
    "read_pid",
    "write_pid",
]
