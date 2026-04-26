r"""Diagnostics / reverse-engineering tools.

**Not on the default install path.** Destructive operations require
``--i-understand-this-is-destructive``. Never invoked from normal
discovery or open.

Six subcommands under the ``watlow-diag`` namespace:

- ``snapshot`` — read-only walk of every supported registry parameter;
  capability-discovery aid. Output is a JSON / text bundle suitable
  for bug reports.
- ``tap`` — passive line / byte capture; never writes.
- ``stream`` — raw-byte capture for protocol work.
- ``sweep`` — registry-parameter sweep across an ID range, recording
  :class:`Availability` outcomes. **Destructive** when ``--write`` is
  passed; reads only by default.
- ``argfuzz`` — boundary-value writes against one parameter.
  **Destructive** when ``--write`` is passed.
- ``detect-framing`` — read-only sweep of (protocol × baud × parity)
  combinations to recover the framing of a controller whose
  configuration has been lost (front panel broken, etc.).

Entry-point dispatcher::

    watlow-diag snapshot PORT [--out FILE]
    watlow-diag tap PORT [--duration 5]
    watlow-diag stream PORT [--duration 5]
    watlow-diag sweep PORT [--start 1000] [--end 2000]
    watlow-diag argfuzz PORT --parameter 7001 \\
        [--write --i-understand-this-is-destructive]
    watlow-diag detect-framing PORT [--address 1] [--json]
"""

from __future__ import annotations

import argparse

from watlowlib.cli.diagnostics._gate import (
    DESTRUCTIVE_FLAG,
    require_destructive_ack,
)

__all__ = ["DESTRUCTIVE_FLAG", "main", "require_destructive_ack"]


def main(argv: list[str] | None = None) -> int:
    """``watlow-diag`` dispatcher entry point.

    Parses the leading subcommand token (``snapshot``, ``tap``, ...)
    and forwards the remaining argv to the corresponding module's
    ``main``. Unknown subcommands print the available list and exit
    with code 2.
    """
    parser = argparse.ArgumentParser(
        prog="watlow-diag",
        description=(
            "Diagnostics namespace — RE tools, not on the default install path. "
            "Destructive operations require --i-understand-this-is-destructive."
        ),
        add_help=True,
    )
    parser.add_argument(
        "subcommand",
        choices=("snapshot", "tap", "stream", "sweep", "argfuzz", "detect-framing"),
        help="Diagnostic subcommand to run.",
    )
    parser.add_argument(
        "rest",
        nargs=argparse.REMAINDER,
        help="Arguments passed to the subcommand.",
    )
    ns = parser.parse_args(argv)
    # Lazy imports keep the gate-only path (e.g. ``watlow-diag --help``)
    # cheap and avoid the partially-initialised circle on first import.
    if ns.subcommand == "snapshot":
        from watlowlib.cli.diagnostics import snapshot  # noqa: PLC0415

        return snapshot.main(ns.rest)
    if ns.subcommand == "tap":
        from watlowlib.cli.diagnostics import tap  # noqa: PLC0415

        return tap.main(ns.rest)
    if ns.subcommand == "stream":
        from watlowlib.cli.diagnostics import stream  # noqa: PLC0415

        return stream.main(ns.rest)
    if ns.subcommand == "sweep":
        from watlowlib.cli.diagnostics import sweep  # noqa: PLC0415

        return sweep.main(ns.rest)
    if ns.subcommand == "argfuzz":
        from watlowlib.cli.diagnostics import argfuzz  # noqa: PLC0415

        return argfuzz.main(ns.rest)
    if ns.subcommand == "detect-framing":
        from watlowlib.cli.diagnostics import detect_framing  # noqa: PLC0415

        return detect_framing.main(ns.rest)
    raise AssertionError(f"unreachable: argparse choices guard {ns.subcommand!r}")
