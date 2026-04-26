"""Module logger setup + structured-log helpers.

Every public-API event flows through :func:`get_logger`; the session
``execute`` path emits one DEBUG event per call with command name +
selector and one WARNING on every error. Raw bytes go to a separate
``watlowlib.wire`` logger so day-to-day DEBUG noise stays readable.

See ``docs/design.md`` §8.
"""

from __future__ import annotations

import logging

_ROOT = "watlowlib"


def get_logger(suffix: str | None = None) -> logging.Logger:
    """Return ``watlowlib`` (or ``watlowlib.<suffix>``) logger.

    Suffixes are dotted paths under the package root, e.g.
    ``"session"`` → ``watlowlib.session``, ``"wire"`` →
    ``watlowlib.wire``.
    """
    return logging.getLogger(_ROOT if suffix is None else f"{_ROOT}.{suffix}")


__all__ = ["get_logger"]
