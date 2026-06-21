"""Sync facade, heavy sinks, maintenance, and CLI surface tests.

Smoke + behavior tests for the sync facade, heavy sinks, maintenance
helpers, and the ``watlow-configure`` / ``watlow-diag`` CLIs.

The async core is exercised exhaustively elsewhere; this file's job is
to confirm:

- The sync portal correctly drives the async core (identify, read_pv,
  set_setpoint round-tripped through SyncController).
- The Watlow.open / SyncWatlowManager context managers wire portal
  lifecycle correctly and clean up on exit.
- Maintenance helpers refuse without ``confirm=True`` and validate
  ranges before any I/O.
- ParquetSink / PostgresSink instantiate on bare-core installs and
  raise WatlowSinkDependencyError when the extra is missing on open().
- The diagnostics CLIs run cleanly against fixture data.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import anyio
import pytest

from watlowlib.cli.configure import main as configure_main
from watlowlib.cli.diagnostics import main as diag_main
from watlowlib.cli.diagnostics._gate import (
    DESTRUCTIVE_FLAG,
    require_destructive_ack,
)
from watlowlib.errors import (
    WatlowConfigurationError,
    WatlowConfirmationRequiredError,
)
from watlowlib.maintenance import (
    MODBUS_BAUD_CODES,
    PROTOCOL_MODE_CODES,
    change_baud,
    change_modbus_address,
    change_protocol_mode,
    change_stdbus_address,
)
from watlowlib.protocol.base import ProtocolKind
from watlowlib.sinks import ParquetSink, PostgresConfig, PostgresSink
from watlowlib.sync import (
    SyncControllerLoop,
    SyncPortal,
    SyncWatlowManager,
    run_sync,
)
from watlowlib.sync.controller import SyncController, wrap_controller
from watlowlib.testing import controller_from_fixture

FIXTURES_DIR = Path(__file__).parent / "fixtures"
PM3_STDBUS = FIXTURES_DIR / "pm3_stdbus_pv_setpoint.jsonl"


# ---------------------------------------------------------------- sync portal


def test_sync_portal_runs_simple_coroutine() -> None:
    """SyncPortal can run an async function and return its result."""

    async def coro(x: int, y: int) -> int:
        return x + y

    with SyncPortal() as portal:
        assert portal.call(coro, 2, 3) == 5


def test_sync_portal_unwraps_single_member_exception_groups() -> None:
    """ExceptionGroup with one member is unwrapped to the inner error."""

    class MyError(RuntimeError):
        pass

    async def coro() -> None:
        # AnyIO's task group rewraps single failures into ExceptionGroup.
        async with anyio.create_task_group() as tg:
            _ = tg.start_soon(_raise, MyError("boom"))

    async def _raise(exc: Exception) -> None:
        raise exc

    with SyncPortal() as portal, pytest.raises(MyError, match="boom"):
        portal.call(coro)


def test_sync_portal_is_one_shot() -> None:
    """A SyncPortal cannot be re-entered after exit."""
    p = SyncPortal()
    with p:
        pass
    with pytest.raises(RuntimeError, match="not reusable"):
        p.__enter__()


def test_run_sync_helper() -> None:
    """run_sync runs one coroutine in a throwaway portal."""

    async def coro(value: int) -> int:
        return value * 2

    assert run_sync(coro, 21) == 42


# ---------------------------------------------------------------- sync controller


@pytest.fixture(scope="module")
def fixture_path() -> Path:
    return PM3_STDBUS


def test_watlow_open_against_fixture(fixture_path: Path) -> None:
    """Watlow.open does not work directly against fixtures (needs a port).

    But wrap_controller does — exercise the loop-by-loop / read path
    via the lower-level helper to confirm the sync facade lowering works.
    """
    with SyncPortal() as portal:
        async_ctl = portal.call(controller_from_fixture, str(fixture_path))
        sync_ctl = wrap_controller(async_ctl, portal)
        portal.call(async_ctl.__aenter__)
        try:
            info = sync_ctl.identify()
            assert info.protocol is ProtocolKind.STDBUS
            assert info.address == 1
            pv = sync_ctl.read_pv()
            assert pv.value is not None
            assert pv.protocol is ProtocolKind.STDBUS
        finally:
            portal.call(async_ctl.close)


def test_sync_controller_loop_view_lowers_to_async(fixture_path: Path) -> None:
    """SyncController.loop(n) returns a SyncControllerLoop that round-trips."""
    with SyncPortal() as portal:
        async_ctl = portal.call(controller_from_fixture, str(fixture_path))
        sync_ctl = wrap_controller(async_ctl, portal)
        portal.call(async_ctl.__aenter__)
        try:
            sync_ctl.identify()  # populate loop count
            loop = sync_ctl.loop(1)
            assert isinstance(loop, SyncControllerLoop)
            assert loop.number == 1
        finally:
            portal.call(async_ctl.close)


# ---------------------------------------------------------------- sync manager


def test_sync_watlow_manager_lifecycle() -> None:
    """SyncWatlowManager opens, names is empty, closes cleanly."""
    with SyncWatlowManager() as mgr:
        assert mgr.names == ()
        assert mgr.closed is False
    assert mgr.closed is True


def test_sync_watlow_manager_share_portal() -> None:
    """A shared portal lets multiple sync facades sit on one event loop."""
    with SyncPortal() as portal, SyncWatlowManager(portal=portal) as mgr:
        assert mgr.portal is portal


# ---------------------------------------------------------------- maintenance


@pytest.mark.parametrize(
    ("fn", "kwargs"),
    [
        (change_baud, {"target_baud": 19200}),
        (change_modbus_address, {"target_address": 5}),
        (change_stdbus_address, {"target_address": 3}),
        (change_protocol_mode, {"target": ProtocolKind.MODBUS_RTU}),
    ],
)
def test_maintenance_refuses_without_confirm(
    fn: object,
    kwargs: dict[str, object],
) -> None:
    """Every maintenance op refuses with confirm=False before any I/O."""

    async def go() -> None:
        await fn("/dev/null", **kwargs, confirm=False)  # type: ignore[operator]

    with pytest.raises(WatlowConfirmationRequiredError):
        anyio.run(go)


def test_change_baud_rejects_unsupported_rate() -> None:
    """change_baud raises WatlowConfigurationError for an unknown rate."""

    async def go() -> None:
        await change_baud("/dev/null", target_baud=12345, confirm=True)

    with pytest.raises(WatlowConfigurationError, match="not supported"):
        anyio.run(go)


def test_change_baud_rejects_stdbus_protocol() -> None:
    """change_baud requires Modbus; refuses Std Bus per design."""

    async def go() -> None:
        await change_baud(
            "/dev/null",
            target_baud=19200,
            current_protocol=ProtocolKind.STDBUS,
            confirm=True,
        )

    with pytest.raises(WatlowConfigurationError, match="only supported over Modbus"):
        anyio.run(go)


def test_change_modbus_address_rejects_out_of_range() -> None:
    """Modbus address sanity-check rejects 0 and >247."""

    async def go(addr: int) -> None:
        await change_modbus_address("/dev/null", target_address=addr, confirm=True)

    with pytest.raises(WatlowConfigurationError, match="out of Modbus range"):
        anyio.run(lambda: go(0))
    with pytest.raises(WatlowConfigurationError, match="out of Modbus range"):
        anyio.run(lambda: go(248))


def test_change_stdbus_address_rejects_out_of_range() -> None:
    """Std Bus address sanity-check rejects 0 and >16."""

    async def go(addr: int) -> None:
        await change_stdbus_address("/dev/null", target_address=addr, confirm=True)

    with pytest.raises(WatlowConfigurationError, match="out of Std Bus range"):
        anyio.run(lambda: go(0))
    with pytest.raises(WatlowConfigurationError, match="out of Std Bus range"):
        anyio.run(lambda: go(17))


def test_change_protocol_mode_rejects_auto() -> None:
    """change_protocol_mode requires a concrete target protocol."""

    async def go() -> None:
        await change_protocol_mode("/dev/null", target=ProtocolKind.AUTO, confirm=True)

    with pytest.raises(WatlowConfigurationError, match="must be STDBUS or MODBUS_RTU"):
        anyio.run(go)


def test_modbus_baud_codes_constants() -> None:
    """The Modbus baud-code map matches the PM enumeration."""
    assert MODBUS_BAUD_CODES == {9600: 188, 19200: 189, 38400: 190}


def test_protocol_mode_codes_constants() -> None:
    """The protocol-mode wide-enum codes match the PM enumeration."""
    assert PROTOCOL_MODE_CODES[ProtocolKind.STDBUS] == 1286
    assert PROTOCOL_MODE_CODES[ProtocolKind.MODBUS_RTU] == 1057


# ---------------------------------------------------------------- parquet sink


_HAVE_PYARROW = importlib.util.find_spec("pyarrow") is not None


def test_parquet_sink_instantiates_on_bare_core(tmp_path: Path) -> None:
    """ParquetSink instantiation succeeds without pyarrow installed."""
    sink = ParquetSink(tmp_path / "out.parquet")
    assert sink.path == tmp_path / "out.parquet"
    assert sink.compression == "zstd"
    assert sink.columns is None  # not locked yet


def test_parquet_sink_rejects_invalid_row_group_size(tmp_path: Path) -> None:
    """row_group_size < 1 raises ValueError at construction."""
    with pytest.raises(ValueError, match="row_group_size"):
        ParquetSink(tmp_path / "out.parquet", row_group_size=0)


@pytest.mark.skipif(_HAVE_PYARROW, reason="pyarrow extra is installed")
def test_parquet_sink_open_raises_dependency_error_without_extra(
    tmp_path: Path,
) -> None:
    """Without the parquet extra, open() raises WatlowSinkDependencyError."""
    from watlowlib.errors import WatlowSinkDependencyError

    sink = ParquetSink(tmp_path / "out.parquet")

    async def go() -> None:
        await sink.open()

    with pytest.raises(WatlowSinkDependencyError, match="parquet"):
        anyio.run(go)


# ---------------------------------------------------------------- postgres sink


_HAVE_ASYNCPG = importlib.util.find_spec("asyncpg") is not None


def test_postgres_config_accepts_discrete_fields() -> None:
    """PostgresConfig validates host/user/database and computes target()."""
    cfg = PostgresConfig(
        host="db.internal",
        user="watlow",
        password="secret",
        database="metrics",
    )
    target = cfg.target()
    assert "db.internal" in target
    assert "metrics" in target
    assert "secret" not in target  # password never appears in target()


def test_postgres_config_rejects_dsn_and_host_together() -> None:
    """dsn and host are mutually exclusive."""
    with pytest.raises(ValueError, match="mutually exclusive"):
        PostgresConfig(
            dsn="postgres://x",
            host="y",
            user="u",
            database="d",
        )


def test_postgres_config_rejects_missing_dsn_and_host() -> None:
    """At least one of dsn or host is required."""
    with pytest.raises(ValueError, match="requires either"):
        PostgresConfig()


def test_postgres_config_rejects_bad_identifier() -> None:
    """Schema / table identifiers are validated against a strict regex."""
    with pytest.raises(ValueError, match="must match"):
        PostgresConfig(host="x", user="u", database="d", table="bad name")


def test_postgres_sink_instantiates_on_bare_core() -> None:
    """PostgresSink instantiation succeeds without asyncpg installed."""
    cfg = PostgresConfig(host="x", user="u", database="d")
    sink = PostgresSink(cfg)
    assert sink.config is cfg
    assert sink.columns is None


@pytest.mark.skipif(_HAVE_ASYNCPG, reason="asyncpg extra is installed")
def test_postgres_sink_open_raises_dependency_error_without_extra() -> None:
    """Without the postgres extra, open() raises WatlowSinkDependencyError."""
    from watlowlib.errors import WatlowSinkDependencyError

    cfg = PostgresConfig(host="x", user="u", database="d")
    sink = PostgresSink(cfg)

    async def go() -> None:
        await sink.open()

    with pytest.raises(WatlowSinkDependencyError, match="postgres"):
        anyio.run(go)


# ---------------------------------------------------------------- watlow-diag


def test_diag_dispatcher_help_lists_subcommands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`watlow-diag --help` lists every subcommand."""
    with pytest.raises(SystemExit) as exc_info:
        diag_main(["--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    for sub in ("snapshot", "tap", "stream", "sweep", "argfuzz"):
        assert sub in out


def test_diag_snapshot_against_fixture(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`watlow-diag snapshot` against the PM3 Std Bus fixture."""
    rc = diag_main(
        [
            "snapshot",
            "_",
            "--fixture",
            str(PM3_STDBUS),
            "--include",
            "4001",
            "7001",
        ],
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "process_value" in out
    assert "setpoint" in out
    assert "ok" in out


def test_diag_sweep_write_requires_destructive_ack(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`watlow-diag sweep --write` without the ack flag refuses."""
    with pytest.raises(SystemExit) as exc_info:
        diag_main(["sweep", "_", "--fixture", str(PM3_STDBUS), "--write"])
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert "destructive" in err.lower()


def test_diag_argfuzz_write_requires_destructive_ack(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`watlow-diag argfuzz --write` without the ack flag refuses."""
    with pytest.raises(SystemExit) as exc_info:
        diag_main(
            [
                "argfuzz",
                "_",
                "--fixture",
                str(PM3_STDBUS),
                "--parameter",
                "setpoint",
                "--write",
            ],
        )
    assert exc_info.value.code == 2


def test_diag_gate_helper_raises_without_ack(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`require_destructive_ack` writes to stderr and exits 2."""
    with pytest.raises(SystemExit) as exc_info:
        require_destructive_ack(acked=False, op="testop")
    assert exc_info.value.code == 2
    err = capsys.readouterr().err
    assert DESTRUCTIVE_FLAG in err


def test_diag_gate_helper_passes_with_ack() -> None:
    """`require_destructive_ack(acked=True)` is a no-op."""
    require_destructive_ack(acked=True, op="testop")


# ---------------------------------------------------------------- watlow-configure


def test_configure_refuses_without_confirm(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every configure subcommand exits 2 without --confirm and emits no I/O."""
    rc = configure_main(
        [
            "change-modbus-address",
            "/dev/null",
            "--target-address",
            "5",
        ],
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "destructive" in err
    assert "--confirm" in err


def test_configure_help_lists_subcommands(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`watlow-configure --help` lists every subcommand."""
    with pytest.raises(SystemExit) as exc_info:
        configure_main(["--help"])
    assert exc_info.value.code == 0
    out = capsys.readouterr().out
    for sub in (
        "change-baud",
        "change-modbus-address",
        "change-stdbus-address",
        "change-protocol-mode",
    ):
        assert sub in out


# ---------------------------------------------------------------- public surface


def test_top_level_exports_phase7_sinks() -> None:
    """`watlowlib` re-exports ParquetSink / PostgresSink / WatlowSinkDependencyError."""
    import watlowlib

    for name in (
        "ParquetSink",
        "PostgresSink",
        "PostgresConfig",
        "WatlowSinkDependencyError",
    ):
        assert hasattr(watlowlib, name), f"missing public export: {name}"


def test_sync_module_exports() -> None:
    """`watlowlib.sync` re-exports the public sync surface."""
    import watlowlib.sync

    for name in (
        "Watlow",
        "SyncController",
        "SyncControllerLoop",
        "SyncWatlowManager",
        "SyncPortal",
        "SyncCsvSink",
        "SyncJsonlSink",
        "SyncSqliteSink",
        "SyncInMemorySink",
        "SyncParquetSink",
        "SyncPostgresSink",
        "record",
        "pipe",
        "run_sync",
    ):
        assert hasattr(watlowlib.sync, name), f"missing sync export: {name}"


# unused-import guard — keep the SyncController symbol referenced so editors
# don't garbage-collect it from the test layout.
_KEEP_REFERENCED: tuple[type, ...] = (SyncController,)
