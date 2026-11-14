"""The user's presets, as the window and the renamer see them.

A thin wrapper over `app.forge.db.presets.PresetRepo`, so `app/forge/ui` never sees the database
package. `LastPresetError` lives in `app.forge.models.preset` and is re-exported here.

`subscribe` hears every change once it is stored (a save, a delete, Settings › Naming's
`replace_all`): the queue's preset bar and cards read the presets again (`ForgeHost`), so a
preset made in Settings can be chosen at once. A listener that fails is logged, never raised.
"""

from __future__ import annotations

import builtins
import logging
from collections.abc import Callable, Sequence

from app.forge.db.presets import PresetRepo
from app.forge.models.preset import LastPresetError, Preset

__all__ = ["LastPresetError", "PresetService"]

log = logging.getLogger(__name__)


class PresetService:
    def __init__(self, repository: PresetRepo) -> None:
        self._repository = repository
        self._listeners: builtins.list[Callable[[], None]] = []

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        """Call `listener` after every stored change; returns the function that unsubscribes."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def _changed(self) -> None:
        for listener in builtins.list(self._listeners):
            try:
                listener()
            except Exception:
                log.exception("A listener to changed presets failed")

    async def list(self) -> builtins.list[Preset]:
        """The presets, in the order they appear in the dropdown."""
        return await self._repository.list()

    async def save(self, preset: Preset, *, old_name: str | None = None) -> None:
        """Add a preset (last) or overwrite the one with its name; with `old_name`, rename that
        preset in place."""
        await self._repository.save(preset, old_name=old_name)
        self._changed()

    async def delete(self, name: str) -> None:
        """Delete a preset; raises `LastPresetError` rather than leave none."""
        await self._repository.delete(name)
        self._changed()

    async def replace_all(self, presets: Sequence[Preset]) -> None:
        await self._repository.replace_all(presets)
        self._changed()
