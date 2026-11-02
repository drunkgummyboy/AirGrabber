"""Forge's settings, stored as one JSON document (MediaForge's `settings` row "app")."""

from __future__ import annotations

import json
from typing import Any

from app.forge.db.database import Database
from app.forge.models.settings import ForgeSettings

_KEY = "app"


class SettingsRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def load(self) -> ForgeSettings:
        """The stored settings, or the defaults when none are stored. Keys MediaForge kept
        that AirGrabber now owns (the TMDB key, theme, accent …) are ignored."""
        row = await self._db.fetch_one("SELECT value FROM settings WHERE key = ?", (_KEY,))
        return ForgeSettings.model_validate_json(row[0]) if row else ForgeSettings()

    async def load_document(self) -> dict[str, Any]:
        """The stored JSON document as it is, unknown keys included ({} when none is stored or
        it isn't an object): the import (app/forge/migration.py) reads the old MediaForge's TMDB
        key from it. Never log what it returns."""
        row = await self._db.fetch_one("SELECT value FROM settings WHERE key = ?", (_KEY,))
        if row is None:
            return {}
        try:
            document = json.loads(row[0])
        except ValueError:
            return {}
        return document if isinstance(document, dict) else {}

    async def save(self, settings: ForgeSettings) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (_KEY, settings.model_dump_json()),
            )
