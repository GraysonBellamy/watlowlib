"""Tests for the plaintext-arrow fixture loader in :mod:`watlowlib.testing`."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from watlowlib.testing import (
    FakeTransport,
    FakeTransportFromArrowFixture,
    parse_arrow_fixture,
)

if TYPE_CHECKING:
    from pathlib import Path


def _write(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")


class TestParseArrowFixture:
    def test_simple_request_reply(self, tmp_path: Path) -> None:
        fixture = tmp_path / "round.txt"
        _write(
            fixture,
            "# read_pv\n"
            "> 55 FF 05 10 00 00 06 E8 01 03 01 04 01 01 E3 99\n"
            "< 55 FF 06 00 10 00 0B 88 02 03 01 04 01 01 08 45 1E 3C D4 A7 28\n",
        )
        script = parse_arrow_fixture(fixture)
        assert len(script) == 1
        request = bytes.fromhex("55FF051000000 6E8010301040101E399".replace(" ", ""))
        assert request in script
        assert script[request].startswith(b"\x55\xff\x06\x00")

    def test_concatenated_replies(self, tmp_path: Path) -> None:
        """Two ``<`` lines after one ``>`` concatenate into one reply."""
        fixture = tmp_path / "multi.txt"
        _write(
            fixture,
            "> 01 02\n< AA BB\n< CC DD\n",
        )
        script = parse_arrow_fixture(fixture)
        assert script == {b"\x01\x02": b"\xaa\xbb\xcc\xdd"}

    def test_blank_lines_and_comments_ignored(self, tmp_path: Path) -> None:
        fixture = tmp_path / "comments.txt"
        _write(
            fixture,
            "# scenario\n\n> 01 02\n# inline comment\n\n< AA BB\n",
        )
        assert parse_arrow_fixture(fixture) == {b"\x01\x02": b"\xaa\xbb"}

    def test_reply_before_request_raises(self, tmp_path: Path) -> None:
        fixture = tmp_path / "bad.txt"
        _write(fixture, "< AA BB\n")
        with pytest.raises(ValueError, match="without preceding"):
            parse_arrow_fixture(fixture)

    def test_duplicate_request_raises(self, tmp_path: Path) -> None:
        fixture = tmp_path / "dup.txt"
        _write(
            fixture,
            "> 01 02\n< AA\n> 01 02\n< BB\n",
        )
        with pytest.raises(ValueError, match="duplicate send"):
            parse_arrow_fixture(fixture)

    def test_empty_payload_raises(self, tmp_path: Path) -> None:
        fixture = tmp_path / "empty.txt"
        _write(fixture, ">\n< AA\n")
        with pytest.raises(ValueError, match="empty hex"):
            parse_arrow_fixture(fixture)

    def test_invalid_hex_raises(self, tmp_path: Path) -> None:
        fixture = tmp_path / "bad_hex.txt"
        _write(fixture, "> ZZ ZZ\n< AA\n")
        with pytest.raises(ValueError, match="invalid hex"):
            parse_arrow_fixture(fixture)


class TestFakeTransportFromArrowFixture:
    def test_returns_unopened_transport(self, tmp_path: Path) -> None:
        fixture = tmp_path / "round.txt"
        _write(fixture, "> 01 02\n< AA BB\n")
        transport = FakeTransportFromArrowFixture(fixture)
        assert isinstance(transport, FakeTransport)
        assert transport.label == f"fixture://{fixture.name}"

    def test_label_override(self, tmp_path: Path) -> None:
        fixture = tmp_path / "round.txt"
        _write(fixture, "> 01 02\n< AA BB\n")
        transport = FakeTransportFromArrowFixture(fixture, label="custom://label")
        assert transport.label == "custom://label"
