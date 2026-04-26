"""Command-line entry points for watlowlib.

Each CLI is a thin wrapper over the public facade — there is no
behaviour reachable from a CLI that isn't reachable from
:class:`watlowlib.Controller` or one of the typed helpers in
:mod:`watlowlib.testing`. That keeps the CLIs trivially testable
against fixtures (no real serial port required) and keeps the
library / CLI surfaces in lockstep.

Core entry points:

- :mod:`watlowlib.cli.read`     — ``watlow-read``
- :mod:`watlowlib.cli.discover` — ``watlow-discover``
- :mod:`watlowlib.cli.decode`   — ``watlow-decode`` (offline)
- :mod:`watlowlib.cli.raw`      — ``watlow-raw``

Configuration and diagnostics:

- :mod:`watlowlib.cli.configure`         — ``watlow-configure``
- :mod:`watlowlib.cli.diagnostics`       — ``watlow-diag`` dispatcher
- :mod:`watlowlib.cli.diagnostics.snapshot` / ``sweep`` / ``argfuzz`` /
  ``tap`` / ``stream`` — RE / capability-discovery tools.
"""

from __future__ import annotations

__all__: list[str] = []
