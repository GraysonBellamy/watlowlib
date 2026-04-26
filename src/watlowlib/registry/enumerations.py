"""Loader for ``data/enumerations.json``.

The enumerations file groups the symbolic names Watlow uses for
parameter values (heat algorithms, sensor types, alarm states, ...). It
is shaped as a flat list of rows, where each row is one of:

- a 4-tuple ``[7Seg, PC label, text enumeration, value]`` — the actual
  symbol entry
- a 4-tuple where the last element is a string column header (e.g.
  ``[..., "Value"]``) — section header rows; skipped on load.

This module only loads the table. Binding specific symbol groups to
:class:`watlowlib.registry.parameters.ParameterSpec.enum` happens
elsewhere, once families and per-parameter enum metadata are wired
through.
"""

from __future__ import annotations

import json
from importlib.resources import files
from typing import Any

__all__ = ["EnumerationRow", "load_enumerations"]

#: One symbolic-value row from ``enumerations.json``.
type EnumerationRow = tuple[str | int, str, str | int, int]

_DATA_PACKAGE = "watlowlib.data"
_FILENAME = "enumerations.json"
_ROW_COLUMN_COUNT = 4


def load_enumerations() -> tuple[EnumerationRow, ...]:
    """Load and return all symbol rows from ``enumerations.json``.

    Section-header rows (where the value column is a string) are
    dropped; only ``(_, _, _, int)`` rows survive.
    """
    raw = files(_DATA_PACKAGE).joinpath(_FILENAME).read_text(encoding="utf-8")
    blob: list[dict[str, list[Any]]] = json.loads(raw)
    out: list[EnumerationRow] = []
    for entry in blob:
        row = entry.get("row")
        if row is None or len(row) != _ROW_COLUMN_COUNT:
            continue
        value = row[3]
        if not isinstance(value, int):
            # Header rows carry a string in the value column.
            continue
        out.append((row[0], str(row[1]), row[2], value))
    return tuple(out)
