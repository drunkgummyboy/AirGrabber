"""Forge's settings: held in memory for synchronous reads, persisted on every change.

Replaces MediaForge's shared mutable `services.app_settings` (every `app_settings = …model_copy(…)`
+ `settings.save(…)` pair is one `update(...)` here). Modelled on AirGrabber's
`app/services/settings.py`: `current` is a frozen `ForgeSettings`, `update(**changes)` validates and
persists one change at a time, `subscribe(listener)` hears about every change.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import Any

from app.forge.db.settings import SettingsRepo
from app.forge.models.settings import ForgeSettings

log = logging.getLogger(__name__)

ForgeSettingsListener = Callable[[ForgeSettings], None]


class ForgeSettingsService:
    def __init__(self, repository: SettingsRepo, initial: ForgeSettings) -> None:
        self._repository = repository
        self._current = initial
        self._listeners: list[ForgeSettingsListener] = []
        # One update at a time: each merges its changes into the settings the previous one
        # stored, so the renamer, the trailer tools and Settings can't undo each other.
        self._lock = asyncio.Lock()

    @classmethod
    async def load(cls, repository: SettingsRepo) -> ForgeSettingsService:
        return cls(repository, await repository.load())

    @property
    def current(self) -> ForgeSettings:
        return self._current

    async def update(self, **changes: Any) -> ForgeSettings:
        """Validate, persist, then notify listeners. Raises pydantic.ValidationError on a bad
        value and TypeError on a name ForgeSettings doesn't have (the model ignores unknown keys
        when loading, so a misspelt name would otherwise be dropped without a word).

        Updates are serialized: `changes` are merged into the settings as they are once every
        earlier update has been saved.
        """
        unknown = sorted(set(changes) - set(ForgeSettings.model_fields))
        if unknown:
            raise TypeError(f"ForgeSettings has no field(s): {', '.join(unknown)}")
        async with self._lock:
            updated = ForgeSettings.model_validate({**self._current.model_dump(), **changes})
            if updated == self._current:
                return updated
            await self._repository.save(updated)
            self._current = updated
            for listener in list(self._listeners):
                try:
                    listener(updated)
                except Exception:
                    log.exception("Forge settings listener %r failed", listener)
            return updated

    def subscribe(self, listener: ForgeSettingsListener) -> Callable[[], None]:
        """Call `listener` after every change. Returns a function that unsubscribes it."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(listener)

        return unsubscribe
