"""A small key/value cache for TMDB responses. Callers build the keys: never with the TMDB key
(or anything else secret) in them."""

from __future__ import annotations

import json
import time
from typing import Any

from app.forge.db.database import Database


class CacheRepo:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get_json(self, key: str, max_age_s: float) -> Any | None:
        row = await self._db.fetch_one(
            "SELECT payload, fetched_at FROM tmdb_cache WHERE key = ?", (key,)
        )
        if row is None or time.time() - float(row[1]) > max_age_s:
            return None
        return json.loads(row[0])

    async def put_json(self, key: str, value: Any) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "INSERT INTO tmdb_cache (key, payload, fetched_at) VALUES (?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET payload = excluded.payload, "
                "fetched_at = excluded.fetched_at",
                (key, json.dumps(value), time.time()),
            )

    async def purge(self, older_than_s: float) -> int:
        """Drop the entries older than `older_than_s`; returns how many went."""
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "DELETE FROM tmdb_cache WHERE fetched_at < ?", (time.time() - older_than_s,)
            )
            return cursor.rowcount
