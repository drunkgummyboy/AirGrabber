"""The user's presets, in the order they appear in the dropdown."""

from __future__ import annotations

import builtins
from collections.abc import Sequence

import aiosqlite

from app.forge.db.database import Database
from app.forge.models.media import TypeHint
from app.forge.models.preset import DEFAULT_PRESETS, LastPresetError, Preset, PresetOptions

__all__ = ["LastPresetError", "PresetRepo"]


async def _count(conn: aiosqlite.Connection) -> int:
    rows = list(await conn.execute_fetchall("SELECT COUNT(*) FROM presets"))
    return int(rows[0][0]) if rows else 0


async def _replace(conn: aiosqlite.Connection, presets: Sequence[Preset]) -> None:
    await conn.execute("DELETE FROM presets")
    await conn.executemany(
        "INSERT INTO presets (name, position, type, format, options) VALUES (?, ?, ?, ?, ?)",
        [
            (p.name, i, p.type.value, p.format, p.options.model_dump_json())
            for i, p in enumerate(presets)
        ],
    )


class PresetRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def list(self) -> builtins.list[Preset]:
        rows = await self._db.fetch_all(
            "SELECT name, type, format, options FROM presets ORDER BY position"
        )
        return [
            Preset(
                name=name,
                type=TypeHint(type_),
                format=fmt,
                options=PresetOptions.model_validate_json(options),
            )
            for name, type_, fmt, options in rows
        ]

    async def save(self, preset: Preset, *, old_name: str | None = None) -> None:
        """Add a preset (last) or overwrite the one with its name (keeping its place). With
        `old_name`, the preset called that is renamed in place; a different preset already
        called `preset.name` is replaced by it."""
        values = (preset.type.value, preset.format, preset.options.model_dump_json())
        async with self._db.transaction() as conn:
            if old_name is not None and old_name != preset.name:
                await conn.execute("DELETE FROM presets WHERE name = ?", (preset.name,))
                await conn.execute(
                    "UPDATE presets SET name = ?, type = ?, format = ?, options = ? WHERE name = ?",
                    (preset.name, *values, old_name),
                )
            else:
                await conn.execute(
                    "INSERT INTO presets (name, position, type, format, options) "
                    "VALUES (?, (SELECT COALESCE(MAX(position), -1) + 1 FROM presets), ?, ?, ?) "
                    "ON CONFLICT(name) DO UPDATE SET type = excluded.type, "
                    "format = excluded.format, options = excluded.options",
                    (preset.name, *values),
                )

    async def delete(self, name: str) -> None:
        """Delete a preset; raises LastPresetError rather than leave none."""
        async with self._db.transaction() as conn:
            if await _count(conn) <= 1:
                raise LastPresetError(name)
            await conn.execute("DELETE FROM presets WHERE name = ?", (name,))

    async def replace_all(self, presets: Sequence[Preset]) -> None:
        async with self._db.transaction() as conn:
            await _replace(conn, presets)

    async def seed_defaults_if_empty(self) -> None:
        async with self._db.transaction() as conn:
            if await _count(conn) == 0:
                await _replace(conn, DEFAULT_PRESETS)
