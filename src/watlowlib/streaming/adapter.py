"""Cross-library :class:`PollSourceAdapter` — wraps one :class:`Controller`.

Shipped at the same name as the equivalent class in :mod:`alicatlib`,
:mod:`sartoriuslib`, and :mod:`nidaqlib` so consumers (capa, etc.)
import the same shape from every device library.

The watlow recorder is **parameter-oriented** — its ``PollSource``
Protocol takes ``poll_many(parameters, names, instances) ->
Sequence[Sample]``. The other libraries pass ``poll(names) ->
Mapping[str, DeviceResult[<reading>]]``. The class **name** is
uniform across libraries; the **method signature** matches each lib's
recorder. See :file:`UNIFIED_API_HANDOFF.md` §1 / §E for the rationale.

A solo :class:`Controller` already satisfies the recorder's
:class:`PollSource` Protocol directly using its transport label as
the device name. :class:`PollSourceAdapter` is only useful when a
downstream consumer wants a stable caller-provided device name (so
the manager's name appears in :attr:`Sample.device` and feeds the
``names=`` filter) without spinning up a full
:class:`~watlowlib.manager.WatlowManager`.
"""

from __future__ import annotations

import dataclasses
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from watlowlib.devices.controller import Controller
    from watlowlib.streaming.sample import Sample

__all__ = ["PollSourceAdapter"]


class PollSourceAdapter:
    """Wrap one :class:`Controller` as a named :class:`PollSource`.

    Implements ``poll_many(parameters, *, names=None, instances=(1,))
    -> Sequence[Sample]`` — the watlow recorder Protocol. When
    ``names`` is supplied and does not contain this adapter's name,
    the call short-circuits to an empty list (cross-library Manager-
    style filter semantics).

    Sample relabeling: :meth:`Controller.poll_many` tags each
    :class:`Sample` with the transport label by default. This adapter
    rebuilds each sample via :func:`dataclasses.replace` to set
    :attr:`Sample.device` to the caller-provided ``name``. Cost is
    negligible at typical watlow rates (1–5 Hz × small parameter
    sets) and stays well under 1 ms/tick at high parameter counts.
    """

    __slots__ = ("_device", "_name")

    def __init__(self, name: str, device: Controller) -> None:
        self._name = name
        self._device = device

    @property
    def name(self) -> str:
        """The caller-provided device name attached to every emitted sample."""
        return self._name

    @property
    def device(self) -> Controller:
        """The wrapped :class:`Controller`."""
        return self._device

    async def poll_many(
        self,
        parameters: Sequence[str | int],
        *,
        names: Sequence[str] | None = None,
        instances: Sequence[int] = (1,),
    ) -> list[Sample]:
        """Poll the wrapped controller and relabel each emitted sample.

        Returns ``[]`` when ``names`` is supplied and this adapter's
        :attr:`name` is not in it — same filter semantics as
        :meth:`WatlowManager.poll_many`.
        """
        if names is not None and self._name not in set(names):
            return []
        samples = await self._device.poll_many(parameters, instances=instances)
        return [dataclasses.replace(sample, device=self._name) for sample in samples]
