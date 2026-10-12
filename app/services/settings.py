"""Current preferences: held in memory for synchronous reads, persisted on every change."""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from typing import Any

from app.database import SettingsRepository
from app.models import Settings

log = logging.getLogger(__name__)

SettingsListener = Callable[[Settings], None]


class SettingsService:
    def __init__(self, repository: SettingsRepository, initial: Settings) -> None:
        self._repository = repository
        self._current = initial
        self._listeners: list[SettingsListener] = []

    @classmethod
    async def load(cls, repository: SettingsRepository) -> SettingsService:
        return cls(repository, await repository.load())

    @property
    def current(self) -> Settings:
        return self._current

    async def update(self, **changes: Any) -> Settings:
        """Validate, persist, then notify listeners. Raises pydantic.ValidationError on bad input."""
        updated = Settings.model_validate({**self._current.model_dump(), **changes})
        if updated == self._current:
            return updated
        await self._repository.save(updated)
        self._current = updated
        for listener in list(self._listeners):
            try:
                listener(updated)
            except Exception:
                log.exception("Settings listener %r failed", listener)
        return updated

    def subscribe(self, listener: SettingsListener) -> Callable[[], None]:
        """Call `listener` after every change. Returns a function that unsubscribes it."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(listener)

        return unsubscribe
