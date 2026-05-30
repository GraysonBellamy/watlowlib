"""Std Bus enumerations and MAC ↔ address mapping.

Centralizes the constants the framing, payload, TLV, and client modules
need so a future PR can grow the family / firmware tables here without
edits scattered across the subpackage. Values are verified against
captured EZ-ZONE PM3 traffic.
"""

from __future__ import annotations

from enum import IntEnum

from watlowlib.errors import WatlowValidationError

__all__ = [
    "ADDR_OFFSET",
    "DIR_REQUEST",
    "DIR_RESPONSE",
    "FN_READ",
    "FN_WRITE",
    "HOST_MAC",
    "ErrorCode",
    "FrameType",
    "addr_to_mac",
    "mac_to_addr",
]


class FrameType(IntEnum):
    """BACnet MS/TP frame types observed on Standard Bus traffic.

    BACnet MS/TP defines ``0..7`` plus ``0x80..0xFF`` as proprietary.
    Only ``0x05`` (request) and ``0x06`` (response) are honoured on the
    wire by EZ-ZONE PM controllers — see
    ``docs/protocol-stdbus-findings.md`` ("Frame-type space"). The rest
    are documented for probing by reverse-engineering tooling.
    """

    TOKEN = 0x00
    POLL_FOR_MASTER = 0x01
    REPLY_TO_POLL_FOR_MASTER = 0x02
    TEST_REQUEST = 0x03
    TEST_RESPONSE = 0x04
    DATA_EXPECTING_REPLY = 0x05
    DATA_NOT_EXPECTING_REPLY = 0x06
    REPLY_POSTPONED = 0x07


HOST_MAC = 0x00
ADDR_OFFSET = 0x0F


def addr_to_mac(addr: int) -> int:
    """Map a Standard Bus address (``1..16``) to its MS/TP MAC.

    Raises:
        WatlowValidationError: ``addr`` is outside ``1..16``. Typed
            (not a bare :class:`ValueError`) so callers in the dispatch
            / discovery path can catch it as a :class:`WatlowError` and
            surface a structured ``ok=False`` result rather than
            aborting a whole scan.
    """
    if not 1 <= addr <= 16:
        raise WatlowValidationError(f"Standard Bus address out of range 1..16: {addr}")
    return ADDR_OFFSET + addr


def mac_to_addr(mac: int) -> int:
    """Map an MS/TP MAC (``0x10..0x1F``) back to its Standard Bus address.

    Raises:
        WatlowValidationError: ``mac`` is outside the controller range.
    """
    if not 0x10 <= mac <= 0x1F:
        raise WatlowValidationError(f"MAC out of expected controller range: 0x{mac:02X}")
    return mac - ADDR_OFFSET


# Inner-payload direction / function bytes.
DIR_REQUEST = 0x01
DIR_RESPONSE = 0x02

FN_READ = 0x03
FN_WRITE = 0x04


class ErrorCode(IntEnum):
    """Error response codes observed when the request selector is invalid.

    The error response payload is two bytes: ``0x02`` (response
    direction) followed by the code. There is no echo of the failing
    class/member/instance.

    Mapping to :class:`watlowlib.devices.capability.Availability`
    (per ``docs/design.md`` §5b):

    - ``NO_SUCH_OBJECT`` / ``NO_SUCH_ATTRIBUTE`` → ``UNSUPPORTED``
    - ``NO_SUCH_INSTANCE`` → unchanged (the parameter exists; this loop
      / channel does not)
    """

    NO_SUCH_OBJECT = 0x81
    NO_SUCH_ATTRIBUTE = 0x83
    NO_SUCH_INSTANCE = 0x84
