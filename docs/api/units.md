---
description: watlowlib.units — to_pint(unit) lossy mapping from a watlowlib Unit (or alias string) to a pint-compatible unit string, without making pint a runtime dependency.
---

# `watlowlib.units`

`to_pint(unit)` — maps a `watlowlib.Unit` (or a recognised alias
string) to a pint-compatible unit string. `pint` is **not** a runtime
dependency of `watlowlib`; this helper returns plain strings so
downstream tools that use pint can feed them straight into
`pint.UnitRegistry.parse_expression`, while consumers that don't use
pint can ignore the output. Lossy by design: gauge/absolute and other
distinctions pint doesn't model are dropped.

## Public surface

::: watlowlib.units
