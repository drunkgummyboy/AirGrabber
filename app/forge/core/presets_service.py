"""The user's presets, as the window and the renamer see them.

A thin wrapper over `app.forge.db.presets.PresetRepo`, so `app/forge/ui` never sees the database
package. `LastPresetError` lives in `app.forge.models.preset` and is re-exported here.
"""

from __future__ import annotations

import builtins
from collections.abc import Sequence

from app.forge.db.presets import PresetRepo
from app.forge.models.preset import LastPresetError, Preset

__all__ = ["LastPresetError", "PresetService"]


class PresetService:
    def __init__(self, repository: PresetRepo) -> None:
        self._repository = repository

    async def list(self) -> builtins.list[Preset]:
        """The presets, in the order they appear in the dropdown."""
        return await self._repository.list()

    async def save(self, preset: Preset, *, old_name: str | None = None) -> None:
        """Add a preset (last) or overwrite the one with its name; with `old_name`, rename that
        preset in place."""
        await self._repository.save(preset, old_name=old_name)

    async def delete(self, name: str) -> None:
        """Delete a preset; raises `LastPresetError` rather than leave none."""
        await self._repository.delete(name)

    async def replace_all(self, presets: Sequence[Preset]) -> None:
        await self._repository.replace_all(presets)
