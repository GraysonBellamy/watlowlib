# Contributing to watlowlib

Thanks for your interest. Please read [docs/design.md](docs/design.md)
before making non-trivial changes — most design decisions are already
made and documented there.

## Dev setup

```bash
git clone https://github.com/GraysonBellamy/watlowlib
cd watlowlib
uv sync --all-extras --dev
uv run pre-commit install
```

## Core checks (must pass before merging)

```bash
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv run pytest
```

## Adding a new Watlow command

Per the design doc §5 and §6, a new command is:

1. One `Command` object in `src/watlowlib/commands/<group>.py` with its
   `StdBusVariant` and/or `ModbusVariant`, `safety` tier, and
   `family_hints` / `capability_hints` priors.
2. One request dataclass and one response dataclass (frozen, slotted).
3. One facade one-liner on `Controller` (or `ControllerLoop` for
   loop-scoped commands) — plus a sync-facade one-liner or
   `@sync_version` wrapper.
4. One fixture-backed unit test hitting each variant's `encode(...)` /
   `decode(...)` (or `apply(...)` for Modbus), plus one `FakeTransport`
   round-trip test.

**Nothing else.** No hand-written byte paths; no per-command branching
in `Session`.

## Adding a parameter

Most parameters do not need a custom command — the registry-driven
`ReadParameter` / `WriteParameter` pair handles them automatically once
the parameter is in the registry. To add one:

1. Add or update an entry in `src/watlowlib/data/<family>_parameters.json`
   carrying both the Standard Bus selector
   (`class_id`, `member_id`, default `instance_id`) and the Modbus
   register addresses (`relative_addr`, `absolute_addr`,
   `next_inst_offset`).
2. Set the data type and the RWES flag — RWES drives the safety tier
   (`R` → `READ_ONLY`, `RW` → `STATEFUL`, `RWE` / `RWES` →
   `PERSISTENT`).
3. If the parameter is enum-valued, add the enum table to
   `src/watlowlib/data/enumerations.json`.
4. Add a fixture-backed unit test asserting the wire bytes for both
   protocols.

## Safety

Any command that can damage hardware, lose data, or write EEPROM must
set `safety = SafetyTier.PERSISTENT` or `SafetyTier.DANGEROUS` on its
`Command` spec and accept `confirm=True` at the facade. The `Session`
rejects `confirm is not True` before any I/O. See design doc §6.1.

## Commits

Conventional-style short prefixes are helpful but not mandatory:

- `feat:` new user-visible behaviour
- `fix:` bugfix
- `refactor:` internal cleanup
- `docs:` docs only
- `ci:` pipeline changes
- `chore:` tooling/version bumps

## Tests that need hardware

Mark them with `hardware`, `hardware_stateful`, or
`hardware_destructive`. These are skipped in CI by default. Stateful
and destructive tiers also require opt-in env vars
(`WATLOWLIB_ENABLE_STATEFUL_TESTS=1`,
`WATLOWLIB_ENABLE_DESTRUCTIVE_TESTS=1`).
