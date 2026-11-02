"""The ignored files and folders (`ignored`, schema v4): what Auto-rename leaves alone.

Paths are matched by `ignore_key` (os.path.normcase, without a trailing slash): the way Windows
compares them. One row per path: ignoring it again only updates how it is shown.
"""

from __future__ import annotations

import builtins
import os
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from app.forge.db.database import Database
from app.forge.models.ignored import IgnoredPath


def ignore_key(path: Path | str) -> str:
    """How an ignored path is matched: Windows' own comparison (case and slashes)."""
    key = os.path.normcase(str(path))
    stripped = key.rstrip("\\/")
    return stripped if stripped and not stripped.endswith(":") else key


def _entry(row: Sequence[Any]) -> IgnoredPath:
    path, folder, added_at = row
    return IgnoredPath(
        path=Path(path), folder=bool(folder), added_at=datetime.fromtimestamp(added_at)
    )


class IgnoredRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def list(self) -> builtins.list[IgnoredPath]:
        """Every ignored file and folder, oldest first."""
        rows = await self._db.fetch_all(
            "SELECT path, folder, added_at FROM ignored ORDER BY added_at, path_key"
        )
        return [_entry(r) for r in rows]

    async def add(self, path: Path, *, folder: bool, when: float | None = None) -> IgnoredPath:
        """Ignore `path` (a folder: everything under it too)."""
        when = time.time() if when is None else when
        async with self._db.transaction() as conn:
            await conn.execute(
                "INSERT INTO ignored (path_key, path, folder, added_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(path_key) DO UPDATE SET path = excluded.path, "
                "folder = excluded.folder",
                (ignore_key(path), str(path), int(folder), when),
            )
        row = await self._db.fetch_one(
            "SELECT path, folder, added_at FROM ignored WHERE path_key = ?", (ignore_key(path),)
        )
        assert row is not None
        return _entry(row)

    async def remove(self, path: Path) -> bool:
        """Stop ignoring `path`. Returns whether it was ignored."""
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "DELETE FROM ignored WHERE path_key = ?", (ignore_key(path),)
            )
            return cursor.rowcount > 0
