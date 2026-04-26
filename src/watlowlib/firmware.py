"""Firmware version parsing + comparison.

Watlow exposes firmware identity as parameter 1002 (``S32``); the user-
facing version is derived from the build / branch / prototype triple
(parameters 1004 / 1006 / 1002 in PM Map 1) but a simple ``major.minor``
form is enough for command gating.

:class:`FirmwareVersion` is the type stored on
:attr:`watlowlib.commands.base.Command.min_firmware`. The session
compares the device-reported version against the command's minimum
before dispatch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Self

_PARSE_RE = re.compile(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?\s*$")


@dataclass(frozen=True, slots=True, order=True)
class FirmwareVersion:
    """Semantic firmware version (``major.minor.patch``).

    ``order=True`` makes the dataclass orderable on its tuple of fields,
    which matches the ``major.minor.patch`` precedence callers expect.
    """

    major: int
    minor: int = 0
    patch: int = 0

    @classmethod
    def parse(cls, text: str) -> Self:
        """Parse a string like ``"1"``, ``"1.2"``, ``"1.2.3"``, or ``"v1.2"``."""
        m = _PARSE_RE.match(text)
        if m is None:
            raise ValueError(f"invalid firmware version: {text!r}")
        major = int(m.group(1))
        minor = int(m.group(2) or 0)
        patch = int(m.group(3) or 0)
        return cls(major, minor, patch)

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


__all__ = ["FirmwareVersion"]
