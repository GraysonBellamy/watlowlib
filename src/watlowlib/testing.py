r"""Public testing surface for downstream packages.

The test seam is a contract: anyone consuming :mod:`watlowlib` can
``import watlowlib.testing`` and write facade-level tests against
captured PM3 round-trips without touching a serial port.

What's here:

- :class:`FakeTransport` — re-export of the in-process scripted
  transport (fixture-replay grade: ordered queue + unmatched-write
  capture).
- :class:`FakeSlave` — stub :class:`anymodbus.Slave` for the Modbus
  facade path (the equivalent shape for variants that emit a
  :class:`ModbusOp` rather than wire bytes).
- :class:`StdBusRound` / :class:`ModbusRound` / :class:`Fixture` —
  typed records for one captured round-trip and a captured scenario.
- :func:`load_fixture` — JSONL file → :class:`Fixture`.
- :func:`controller_from_fixture` — JSONL file → opened
  :class:`Controller` ready for facade-level assertions.
- :func:`parse_arrow_fixture` — plaintext arrow file → ``dict[bytes,
  bytes]`` script map for :class:`FakeTransport`. The arrow format
  matches :mod:`alicatlib.testing` and :mod:`sartoriuslib.testing`
  for cross-package consistency.
- :func:`FakeTransportFromArrowFixture` — plaintext arrow file →
  built :class:`FakeTransport`.

Two fixture formats are supported:

**Plaintext arrow** (Std Bus only — recommended for code review)::

    # scenario: read_pv
    > 55 FF 05 10 00 00 06 E8 01 03 01 04 01 01 E3 99
    < 55 FF 06 00 10 00 0B 88 02 03 01 04 01 01 08 45 1E 3C D4 A7 28

Bytes are space-separated hex. ``#`` introduces comments; blank lines
are ignored. Each ``>`` line names a request; one or more following
``<`` lines name the reply (concatenated into one scripted reply).

**JSONL** (rich — required for Modbus, optional for Std Bus). One
JSON object per line. The first line may be a header recording the
protocol and serial framing; everything else is a round.

Std Bus round::

    {
        "protocol": "stdbus",
        "label": "read_pv",
        "request_hex": "55FF051000...",
        "response_hex": "55FF0600...",
    }

Modbus round::

    {
        "protocol": "modbus_rtu",
        "label": "read_pv",
        "method": "read_holding_registers",
        "address": 360,
        "count": 2,
        "response_words": [17299, 29054],
    }

    {
        "protocol": "modbus_rtu",
        "label": "set_setpoint",
        "method": "write_registers",
        "address": 2160,
        "values": [17348, 0],
    }

Optional header (``"kind": "header"``) sets address, baudrate, and
parity for the whole capture::

    {
        "kind": "header",
        "protocol": "stdbus",
        "address": 1,
        "baudrate": 38400,
        "parity": "none",
    }
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from anyserial import Parity

from watlowlib.devices.controller import Controller
from watlowlib.devices.factory import open_controller
from watlowlib.devices.session import Session
from watlowlib.protocol.base import ProtocolKind
from watlowlib.protocol.modbus.client import ModbusProtocolClient
from watlowlib.registry.families import ControllerFamily
from watlowlib.registry.parameters import PARAMETERS
from watlowlib.transport.base import SerialSettings
from watlowlib.transport.fake import FakeSlave, FakeTransport

if TYPE_CHECKING:
    from collections.abc import Sequence

    from watlowlib.transport.base import Transport

__all__ = [
    "FakeSlave",
    "FakeTransport",
    "FakeTransportFromArrowFixture",
    "Fixture",
    "ModbusRound",
    "StdBusRound",
    "controller_from_fixture",
    "load_fixture",
    "parse_arrow_fixture",
]


@dataclass(frozen=True, slots=True)
class StdBusRound:
    """One captured Std Bus request/response pair."""

    label: str
    request: bytes
    response: bytes


@dataclass(frozen=True, slots=True)
class ModbusRound:
    """One captured Modbus call.

    For reads ``response_words`` carries the register tuple the slave
    returned; ``values`` is empty. For writes ``values`` carries the
    register words the controller sent and ``response_words`` is empty
    (a successful write returns no payload).
    """

    label: str
    method: str
    address: int
    count: int
    response_words: tuple[int, ...] = ()
    values: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class Fixture:
    """A captured scenario — one wire protocol, many rounds."""

    protocol: ProtocolKind
    address: int = 1
    serial_settings: SerialSettings = field(
        default_factory=lambda: SerialSettings(port="fake://fixture")
    )
    stdbus_rounds: tuple[StdBusRound, ...] = ()
    modbus_rounds: tuple[ModbusRound, ...] = ()

    def fake_transport(self, *, label: str = "fake://fixture") -> FakeTransport:
        """Build a :class:`FakeTransport` scripted with the Std Bus rounds.

        Each captured request maps to its captured response in the
        dict-based script, so a test that issues the same parameter
        read twice gets the same response both times — appropriate for
        facade smoke tests where ordering doesn't matter.
        """
        script = {round_.request: round_.response for round_ in self.stdbus_rounds}
        return FakeTransport(script, label=label)

    def fake_slave(self) -> FakeSlave:
        """Build a :class:`FakeSlave` scripted with the Modbus rounds.

        Read rounds populate the ``(method, address) → response_words``
        script entry; write rounds need no entry — :class:`FakeSlave`
        treats unscripted writes as silent successes and records them
        on :attr:`FakeSlave.writes` so tests can assert on the lowered
        register words.
        """
        slave = FakeSlave()
        for round_ in self.modbus_rounds:
            if round_.response_words:
                slave.add_script(round_.method, round_.address, round_.response_words)
        return slave


def load_fixture(path: str | Path) -> Fixture:
    """Parse ``path`` as a JSONL fixture and return a :class:`Fixture`.

    The first line may be a header (``{"kind": "header", ...}``) that
    sets ``address`` and the serial framing for the capture. Subsequent
    lines are rounds; their ``protocol`` field must agree with the
    header (or with the first round, if the header is omitted).

    Raises:
        ValueError: malformed JSONL, missing fields, or a row whose
            ``protocol`` field disagrees with the rest of the file.
    """
    p = Path(path)
    rows: list[dict[str, Any]] = []
    for line_no, raw in enumerate(p.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{p}:{line_no}: not valid JSON: {exc}") from exc
    if not rows:
        raise ValueError(f"{p}: fixture is empty")

    header: dict[str, Any] = {}
    body: list[dict[str, Any]] = list(rows)
    if rows[0].get("kind") == "header":
        header = rows[0]
        body = rows[1:]

    protocol = _resolve_protocol(header, body, source=p)

    address = int(header.get("address", 1) or 1)
    serial_settings = _settings_from_header(header)

    if protocol is ProtocolKind.STDBUS:
        stdbus_rounds = tuple(_parse_stdbus_row(row, p, idx) for idx, row in enumerate(body, 2))
        return Fixture(
            protocol=protocol,
            address=address,
            serial_settings=serial_settings,
            stdbus_rounds=stdbus_rounds,
        )

    modbus_rounds = tuple(_parse_modbus_row(row, p, idx) for idx, row in enumerate(body, 2))
    return Fixture(
        protocol=protocol,
        address=address,
        serial_settings=serial_settings,
        modbus_rounds=modbus_rounds,
    )


async def controller_from_fixture(
    path: str | Path,
    *,
    family: ControllerFamily = ControllerFamily.PM,
) -> Controller:
    """Build an unopened :class:`Controller` scripted by ``path``.

    Returned in unopened form so the caller drives lifecycle through
    ``async with``. Std Bus fixtures wire through :class:`FakeTransport`;
    Modbus fixtures wire through :class:`FakeSlave` (the
    :class:`Transport` shim only carries lifecycle, since the Modbus
    protocol client talks to its slave provider directly).
    """
    fixture = load_fixture(path)
    if fixture.protocol is ProtocolKind.STDBUS:
        transport: Transport = fixture.fake_transport()
        return await open_controller(
            transport,
            protocol=ProtocolKind.STDBUS,
            address=fixture.address,
            serial_settings=fixture.serial_settings,
            family=family,
        )

    # Modbus path: build the protocol client over a FakeSlave directly,
    # and hand the controller a lifecycle-only :class:`FakeTransport`.
    slave = fixture.fake_slave()
    transport = FakeTransport(label="fake://fixture-modbus")
    # FakeSlave is structurally compatible with the
    # :class:`SlaveLike` Protocol the Modbus client expects.
    client = ModbusProtocolClient(
        slave_provider=lambda _addr: slave,
        port=transport.label,
    )
    session = Session(
        client,
        registry=PARAMETERS,
        family=family,
        address=fixture.address,
        port=transport.label,
    )
    return Controller(session, transport, serial_settings=fixture.serial_settings)


# --- internals -------------------------------------------------------


def _resolve_protocol(
    header: dict[str, object],
    body: Sequence[dict[str, object]],
    *,
    source: Path,
) -> ProtocolKind:
    declared = header.get("protocol") or (body[0].get("protocol") if body else None)
    if declared is None:
        raise ValueError(f"{source}: missing 'protocol' field")
    try:
        protocol = ProtocolKind(str(declared))
    except ValueError as exc:
        raise ValueError(f"{source}: unknown protocol {declared!r}") from exc
    if protocol is ProtocolKind.AUTO:
        raise ValueError(f"{source}: 'auto' is not a fixture protocol")
    for row in body:
        row_protocol = row.get("protocol")
        if row_protocol is not None and str(row_protocol) != protocol.value:
            raise ValueError(
                f"{source}: row protocol {row_protocol!r} disagrees with {protocol.value!r}"
            )
    return protocol


def _settings_from_header(header: dict[str, Any]) -> SerialSettings:
    port = str(header.get("port", "fake://fixture") or "fake://fixture")
    baudrate = int(header.get("baudrate", 38400) or 38400)
    parity_str = str(header.get("parity", "none") or "none").lower()
    return SerialSettings(port=port, baudrate=baudrate, parity=Parity(parity_str))


def _parse_stdbus_row(row: dict[str, Any], source: Path, line_no: int) -> StdBusRound:
    label = str(row.get("label", ""))
    try:
        request = bytes.fromhex(str(row["request_hex"]))
        response = bytes.fromhex(str(row["response_hex"]))
    except (KeyError, ValueError) as exc:
        raise ValueError(f"{source}:{line_no}: malformed stdbus row: {exc}") from exc
    return StdBusRound(label=label, request=request, response=response)


def _parse_modbus_row(row: dict[str, Any], source: Path, line_no: int) -> ModbusRound:
    label = str(row.get("label", ""))
    try:
        method = str(row["method"])
        address = int(row["address"])
    except (KeyError, ValueError) as exc:
        raise ValueError(f"{source}:{line_no}: malformed modbus row: {exc}") from exc
    response_words = tuple(int(w) for w in row.get("response_words", []) or ())
    values = tuple(int(w) for w in row.get("values", []) or ())
    count_field = row.get("count")
    if count_field is not None:
        count = int(count_field)
    elif response_words:
        count = len(response_words)
    elif values:
        count = len(values)
    else:
        count = 1
    return ModbusRound(
        label=label,
        method=method,
        address=address,
        count=count,
        response_words=response_words,
        values=values,
    )


# ---------------------------------------------------------------------------
# Plaintext-arrow fixture loader (Std Bus). Mirrors alicatlib /
# sartoriuslib for cross-package consistency.
# ---------------------------------------------------------------------------


def _iter_semantic_lines(text: Sequence[str]) -> Sequence[tuple[int, str]]:
    """Yield ``(line_number, stripped_line)`` for non-comment, non-blank lines."""
    out: list[tuple[int, str]] = []
    for line_number, raw in enumerate(text, start=1):
        stripped = raw.rstrip("\r\n")
        lean = stripped.lstrip()
        if not lean or lean.startswith("#"):
            continue
        out.append((line_number, stripped))
    return out


def _hex_payload(line: str, marker: str, *, source: Path, line_no: int) -> bytes:
    """Decode ``"55 FF 05 ..."`` after ``marker`` into raw bytes."""
    without = line.lstrip()[len(marker) :]
    cleaned = without.replace(" ", "").replace("\t", "")
    if not cleaned:
        raise ValueError(
            f"{source}:{line_no}: empty hex payload after {marker!r}",
        )
    try:
        return bytes.fromhex(cleaned)
    except ValueError as exc:
        raise ValueError(
            f"{source}:{line_no}: invalid hex bytes after {marker!r}: {exc}",
        ) from exc


def parse_arrow_fixture(path: str | Path) -> dict[bytes, bytes]:
    r"""Parse a plaintext-arrow fixture into a :class:`FakeTransport` script.

    The fixture format is intentionally human-skimmable so captured
    Std Bus traffic round-trips through code review (cross-package
    convention shared with :mod:`alicatlib.testing` and
    :mod:`sartoriuslib.testing`)::

        # scenario: read_pv (PM3, parameter 4001)
        > 55 FF 05 10 00 00 06 E8 01 03 01 04 01 01 E3 99
        < 55 FF 06 00 10 00 0B 88 02 03 01 04 01 01 08 45 1E 3C D4 A7 28

    Parsing rules:

    - Lines starting with ``#`` are comments; ignored.
    - Blank lines are ignored.
    - ``>`` introduces a request — bytes after the marker are decoded
      as space-separated hex. Whitespace within a payload is ignored
      so callers can group bytes for readability.
    - ``<`` introduces one reply — same hex encoding. Multiple ``<``
      lines after a single ``>`` concatenate into one scripted reply
      (useful when a logical reply was captured across multiple
      reads).
    - Duplicate ``>`` entries are a fixture error rather than a
      silent overwrite.

    Returns:
        Mapping ``request_bytes → reply_bytes`` ready to feed
        :class:`FakeTransport`.

    Raises:
        ValueError: On malformed lines, a ``<`` before any ``>``, or
            a duplicate ``>`` entry. Every error message names the
            offending line number.
        FileNotFoundError: Via the underlying :meth:`Path.read_text`.
    """
    fixture_path = Path(path)
    script: dict[bytes, bytes] = {}
    current_send: bytes | None = None
    current_reply_chunks: list[bytes] = []

    def _flush() -> None:
        nonlocal current_send, current_reply_chunks
        if current_send is None:
            return
        if current_send in script:
            raise ValueError(
                f"{fixture_path}: duplicate send entry {current_send!r}",
            )
        script[current_send] = b"".join(current_reply_chunks)
        current_send = None
        current_reply_chunks = []

    text = fixture_path.read_text(encoding="utf-8")
    for line_number, line in _iter_semantic_lines(text.splitlines()):
        lean = line.lstrip()
        if lean.startswith(">"):
            _flush()
            current_send = _hex_payload(
                line,
                ">",
                source=fixture_path,
                line_no=line_number,
            )
        elif lean.startswith("<"):
            if current_send is None:
                raise ValueError(
                    f"{fixture_path}:{line_number}: '<' line without preceding '>'",
                )
            current_reply_chunks.append(
                _hex_payload(line, "<", source=fixture_path, line_no=line_number),
            )
        else:
            raise ValueError(
                f"{fixture_path}:{line_number}: unrecognized line {line!r}; "
                f"lines must start with '>', '<', or '#'",
            )
    _flush()
    return script


def FakeTransportFromArrowFixture(  # noqa: N802 — public factory, title-case matches the class it returns
    path: str | Path,
    *,
    label: str | None = None,
) -> FakeTransport:
    """Load a plaintext-arrow fixture into a built :class:`FakeTransport`.

    Convenience wrapper around :func:`parse_arrow_fixture` plus
    :class:`FakeTransport` construction. The returned transport is
    not opened — the caller awaits ``.open()`` as usual.

    Args:
        path: Path to the ``.txt`` fixture.
        label: Optional override for :attr:`FakeTransport.label`;
            defaults to ``"fixture://<basename>"`` so error contexts
            point at the actual fixture during failures.
    """
    script = parse_arrow_fixture(path)
    resolved_label = label if label is not None else f"fixture://{Path(path).name}"
    return FakeTransport(script, label=resolved_label)
