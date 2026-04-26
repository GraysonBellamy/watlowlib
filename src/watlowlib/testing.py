"""Public testing surface for downstream packages.

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

Fixture file format is one JSON object per line. The first line may
be a header recording the protocol and serial framing; everything
else is a round.

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
    "Fixture",
    "ModbusRound",
    "StdBusRound",
    "controller_from_fixture",
    "load_fixture",
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
        slave_provider=lambda: slave,
        address=fixture.address,
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
