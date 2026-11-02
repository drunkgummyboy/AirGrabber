"""Match choices the user made, so the same show isn't asked about twice."""

from __future__ import annotations

import builtins
import time

from app.forge.db.database import Database
from app.forge.models.match import RememberedMatch
from app.forge.models.media import MediaKind

__all__ = ["MatchRepo", "RememberedMatch"]


def _year_key(year: int | None) -> int:
    return year or 0


class MatchRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(
        self, kind: MediaKind, norm_title: str, year: int | None
    ) -> RememberedMatch | None:
        row = await self._db.fetch_one(
            "SELECT tmdb_id, label FROM remembered_matches "
            "WHERE kind = ? AND norm_title = ? AND year = ?",
            (kind.value, norm_title, _year_key(year)),
        )
        if row is None:
            return None
        return RememberedMatch(
            kind=kind, norm_title=norm_title, year=year, tmdb_id=row[0], label=row[1]
        )

    async def remember(self, match: RememberedMatch) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "INSERT INTO remembered_matches "
                "(kind, norm_title, year, tmdb_id, label, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(kind, norm_title, year) DO UPDATE SET "
                "tmdb_id = excluded.tmdb_id, label = excluded.label, "
                "created_at = excluded.created_at",
                (
                    match.kind.value,
                    match.norm_title,
                    _year_key(match.year),
                    match.tmdb_id,
                    match.label,
                    time.time(),
                ),
            )

    async def list(self) -> builtins.list[RememberedMatch]:
        rows = await self._db.fetch_all(
            "SELECT kind, norm_title, year, tmdb_id, label FROM remembered_matches "
            "ORDER BY norm_title, year"
        )
        return [
            RememberedMatch(
                kind=MediaKind(kind), norm_title=title, year=year or None, tmdb_id=tid, label=label
            )
            for kind, title, year, tid, label in rows
        ]

    async def forget(self, kind: MediaKind, norm_title: str, year: int | None) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "DELETE FROM remembered_matches WHERE kind = ? AND norm_title = ? AND year = ?",
                (kind.value, norm_title, _year_key(year)),
            )

    async def clear(self) -> None:
        async with self._db.transaction() as conn:
            await conn.execute("DELETE FROM remembered_matches")
