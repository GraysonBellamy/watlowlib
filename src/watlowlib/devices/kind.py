"""Re-export :class:`ControllerFamily` under :mod:`watlowlib.devices`.

Callers can import it from either location. The canonical home is
:mod:`watlowlib.registry.families` so the registry layer can construct
family enums without depending on :mod:`watlowlib.devices`.
"""

from __future__ import annotations

from watlowlib.registry.families import ControllerFamily, classify_family

__all__ = ["ControllerFamily", "classify_family"]
