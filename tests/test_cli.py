"""CLI smoke tests.

Each CLI entry-point gets a smoke test that runs against fixture data
(no real serial port) and asserts on stdout. Golden-file matching
would be brittle as the registry grows; substring assertions on
stable fields keep the tests stable.

The CLIs are deliberately thin wrappers around the facade; the
heavy lifting lives in :mod:`watlowlib.devices.controller` and
:mod:`watlowlib.testing`. These tests therefore only exercise
argument parsing, output formatting, and the wiring between them.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from watlowlib.cli import decode as decode_cli
from watlowlib.cli import discover as discover_cli
from watlowlib.cli import raw as raw_cli
from watlowlib.cli import read as read_cli

FIXTURES_DIR = Path(__file__).parent / "fixtures"
PM3_STDBUS = FIXTURES_DIR / "pm3_stdbus_pv_setpoint.jsonl"
PM3_MODBUS = FIXTURES_DIR / "pm3_modbus_pv_setpoint.jsonl"


# --- watlow-decode --------------------------------------------------


def test_decode_stdbus_read_request_text(capsys: pytest.CaptureFixture[str]) -> None:
    """Captured hardware-id read request decodes cleanly."""
    rc = decode_cli.main(["55FF0510000006E80103010101015EA0"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "ReadRequest" in out
    assert "parameter_id=1001" in out
    assert "type=0x05" in out


def test_decode_stdbus_read_response_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = decode_cli.main(["--format", "json", "55FF060010000B88020301010101060000001C5666"])
    out = capsys.readouterr().out
    assert rc == 0
    parsed = json.loads(out)
    assert parsed["payload"]["kind"] == "ReadResponse"
    assert parsed["payload"]["parameter_id"] == 1001
    assert parsed["payload"]["value"] == 28


def test_decode_rejects_invalid_hex(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        decode_cli.main(["zz"])
    assert exc_info.value.code == 2  # argparse error
    err = capsys.readouterr().err
    assert "not valid hex" in err


def test_decode_handles_bad_crc(capsys: pytest.CaptureFixture[str]) -> None:
    """Bad header CRC surfaces as a frame error, not a crash."""
    rc = decode_cli.main(["55FF0510000006FF"])  # wrong HCRC
    out = capsys.readouterr().out
    assert rc == 0
    assert "frame error" in out


# --- watlow-read ----------------------------------------------------


def test_read_stdbus_fixture_text(capsys: pytest.CaptureFixture[str]) -> None:
    rc = read_cli.main(
        [
            "--fixture",
            str(PM3_STDBUS),
            "--parameter",
            "process_value",
            "--parameter",
            "setpoint",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    assert "process_value" in out
    assert "setpoint" in out
    assert "id=4001" in out
    assert "id=7001" in out


def test_read_modbus_fixture_json(capsys: pytest.CaptureFixture[str]) -> None:
    rc = read_cli.main(
        [
            "--fixture",
            str(PM3_MODBUS),
            "--parameter",
            "process_value",
            "--format",
            "json",
        ]
    )
    out = capsys.readouterr().out
    assert rc == 0
    parsed = json.loads(out)
    assert parsed[0]["name"] == "process_value"
    assert parsed[0]["parameter_id"] == 4001


def test_read_requires_parameter() -> None:
    with pytest.raises(SystemExit):
        read_cli.main(["--fixture", str(PM3_STDBUS)])


def test_read_rejects_port_and_fixture() -> None:
    with pytest.raises(SystemExit):
        read_cli.main(
            [
                "--fixture",
                str(PM3_STDBUS),
                "--port",
                "/dev/null",
                "--parameter",
                "pv",
            ]
        )


# --- watlow-discover ------------------------------------------------


def test_discover_parse_addresses_range() -> None:
    assert discover_cli.parse_addresses(["1-3"]) == (1, 2, 3)


def test_discover_parse_addresses_csv() -> None:
    assert discover_cli.parse_addresses(["1,2,5,10"]) == (1, 2, 5, 10)


def test_discover_parse_addresses_mixed() -> None:
    assert discover_cli.parse_addresses(["1-2", "5"]) == (1, 2, 5)


def test_discover_parse_addresses_rejects_inverted_range() -> None:
    with pytest.raises(ValueError, match="high < low"):
        discover_cli.parse_addresses(["5-1"])


def test_discover_help_lists_protocols(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        discover_cli.main(["--help"])
    out = capsys.readouterr().out
    assert "stdbus" in out
    assert "modbus_rtu" in out


# --- watlow-raw -----------------------------------------------------


def test_raw_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        raw_cli.main(["--help"])
    out = capsys.readouterr().out
    assert "stdbus" in out
    assert "modbus" in out


def test_raw_stdbus_subparser_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        raw_cli.main(["--port", "/dev/null", "stdbus", "--help"])
    out = capsys.readouterr().out
    assert "--service" in out
    assert "--class" in out
    assert "--member" in out


def test_raw_modbus_subparser_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        raw_cli.main(["--port", "/dev/null", "modbus", "--help"])
    out = capsys.readouterr().out
    assert "--fn" in out
    assert "--register" in out
