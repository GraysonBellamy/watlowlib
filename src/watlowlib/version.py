"""Public ``__version__`` accessor.

Resolves the version from the hatch-vcs-generated ``_version.py`` at
build time, falling back to ``"0.0.0+unknown"`` for editable installs
without VCS metadata.
"""

from __future__ import annotations

try:
    from watlowlib._version import __version__ as __version__  # noqa: PLC0414
except ImportError:  # pragma: no cover — only hit before the build hook runs
    __version__ = "0.0.0+unknown"

__all__ = ["__version__"]
