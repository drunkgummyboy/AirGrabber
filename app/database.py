"""SQLite persistence via aiosqlite.

Owns the schema (versioned with PRAGMA user_version) and exposes repositories that accept and
return domain models only. All SQL in the application lives in this module.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sqlite3
from collections.abc import AsyncIterator, Iterable, Mapping, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import aiosqlite
from pydantic import BaseModel, ValidationError

from app.models import (
    CalendarEntry,
    DownloadRecord,
    Episode,
    LibraryEntry,
    LibraryStats,
    MediaType,
    Settings,
    Show,
)

log = logging.getLogger(__name__)

# Entry i upgrades the schema from version i to i + 1. Append only — never edit a shipped entry.
MIGRATIONS: tuple[str, ...] = (
    """
    CREATE TABLE shows (
        id                  INTEGER PRIMARY KEY,            -- TVMaze show id
        name                TEXT    NOT NULL,
        status              TEXT,
        type                TEXT,
        language            TEXT,
        genres              TEXT    NOT NULL DEFAULT '[]',  -- JSON array
        premiered           TEXT,                           -- YYYY-MM-DD
        ended               TEXT,
        network             TEXT,
        runtime             INTEGER,
        rating              REAL,
        weight              INTEGER NOT NULL DEFAULT 0,
        imdb_id             TEXT,
        poster_url          TEXT,
        poster_url_original TEXT,
        summary             TEXT    NOT NULL DEFAULT '',
        updated             INTEGER                         -- TVMaze change stamp
    ) STRICT;

    CREATE TABLE episodes (
        id         INTEGER PRIMARY KEY,                     -- TVMaze episode id
        show_id    INTEGER NOT NULL REFERENCES shows (id) ON DELETE CASCADE,
        season     INTEGER NOT NULL,
        number     INTEGER,                                 -- NULL for specials
        name       TEXT    NOT NULL DEFAULT '',
        type       TEXT    NOT NULL DEFAULT 'regular',
        air_date   TEXT,                                    -- network-local YYYY-MM-DD
        air_time   TEXT,                                    -- network-local HH:MM:SS
        airstamp   TEXT,                                    -- UTC instant, ISO-8601
        runtime    INTEGER,
        rating     REAL,
        image_url  TEXT,
        summary    TEXT    NOT NULL DEFAULT ''
    ) STRICT;
    CREATE INDEX ix_episodes_air_date ON episodes (air_date);
    CREATE INDEX ix_episodes_show ON episodes (show_id, season, number);

    CREATE TABLE tracked_shows (
        show_id       INTEGER PRIMARY KEY REFERENCES shows (id) ON DELETE CASCADE,
        tracked_at    TEXT    NOT NULL,
        auto_download INTEGER NOT NULL DEFAULT 0 CHECK (auto_download IN (0, 1)),
        synced_at     TEXT                                  -- NULL = never synced from TVMaze
    ) STRICT;

    CREATE TABLE download_history (
        id            INTEGER PRIMARY KEY,
        media_type    TEXT    NOT NULL CHECK (media_type IN ('tv', 'movie')),
        media_id      INTEGER NOT NULL,                     -- TVMaze show id / TMDB movie id
        season        INTEGER,
        episode       INTEGER,                              -- NULL with a season = season pack
        release_title TEXT    NOT NULL,
        info_hash     TEXT    UNIQUE,
        source        TEXT,
        size_bytes    INTEGER,
        saved_path    TEXT,
        downloaded_at TEXT    NOT NULL,
        CHECK (media_type = 'movie' OR season IS NOT NULL)
    ) STRICT;
    CREATE INDEX ix_history_media ON download_history (media_type, media_id, season, episode);

    CREATE TABLE settings (
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL                                 -- JSON-encoded
    ) STRICT;

    CREATE TABLE schedule_fetches (                         -- days pulled from TVMaze /schedule
        air_date   TEXT PRIMARY KEY,
        fetched_at TEXT NOT NULL
    ) STRICT;
    """,
)

SHOW_COLUMNS = tuple(Show.model_fields)
EPISODE_COLUMNS = tuple(Episode.model_fields)
HISTORY_COLUMNS = tuple(DownloadRecord.model_fields)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _upsert_sql(table: str, columns: Sequence[str], key: str = "id") -> str:
    names = ", ".join(columns)
    values = ", ".join(f":{column}" for column in columns)
    updates = ", ".join(f"{column} = excluded.{column}" for column in columns if column != key)
    return f"INSERT INTO {table} ({names}) VALUES ({values}) ON CONFLICT ({key}) DO UPDATE SET {updates}"


def _select(alias: str, columns: Sequence[str], prefix: str) -> str:
    """`s.id AS show__id, s.name AS show__name, ...` — disambiguates columns across a JOIN."""
    return ", ".join(f"{alias}.{column} AS {prefix}{column}" for column in columns)


def _section(row: sqlite3.Row, prefix: str) -> dict[str, Any]:
    return {key.removeprefix(prefix): row[key] for key in row.keys() if key.startswith(prefix)}


def _show_to_row(show: Show) -> dict[str, Any]:
    row = show.model_dump(mode="json")
    row["genres"] = json.dumps(row["genres"])
    return row


def _show_from_row(row: Mapping[str, Any]) -> Show:
    return Show.model_validate({**row, "genres": json.loads(row["genres"])})


def _episode_to_row(episode: Episode) -> dict[str, Any]:
    return episode.model_dump(mode="json")


_SHOW_UPSERT = _upsert_sql("shows", SHOW_COLUMNS)
_EPISODE_UPSERT = _upsert_sql("episodes", EPISODE_COLUMNS)
_SHOW_FIELDS = _select("s", SHOW_COLUMNS, "show__")
_EPISODE_FIELDS = _select("e", EPISODE_COLUMNS, "ep__")

_IS_DOWNLOADED = """
    EXISTS (
        SELECT 1 FROM download_history AS h
        WHERE h.media_type = 'tv' AND h.media_id = e.show_id AND h.season = e.season
          AND (h.episode IS NULL OR h.episode = e.number)
    )"""

_CALENDAR_SQL = f"""
    SELECT {_SHOW_FIELDS}, {_EPISODE_FIELDS}, {_IS_DOWNLOADED} AS is_downloaded
    FROM episodes AS e
    JOIN tracked_shows AS t ON t.show_id = e.show_id
    JOIN shows AS s ON s.id = e.show_id
    WHERE e.air_date BETWEEN :start AND :end
    ORDER BY e.air_date, e.airstamp, s.name COLLATE NOCASE, e.season, e.number
"""

# Popular untracked shows airing in the range: one episode per show per day, best `per_day` per day.
_SUGGESTIONS_SQL = f"""
    WITH candidates AS (
        SELECT e.id AS episode_id, e.show_id, e.air_date, s.weight,
               ROW_NUMBER() OVER (
                   PARTITION BY e.air_date, e.show_id ORDER BY e.season, e.number
               ) AS nth_of_show
        FROM episodes AS e
        JOIN shows AS s ON s.id = e.show_id
        WHERE e.air_date BETWEEN :start AND :end
          AND e.show_id NOT IN (SELECT show_id FROM tracked_shows)
          AND s.language = :language
          AND s.weight >= :min_weight
          AND s.type IN (SELECT value FROM json_each(:types))
    ),
    ranked AS (
        SELECT episode_id, air_date,
               ROW_NUMBER() OVER (PARTITION BY air_date ORDER BY weight DESC, show_id) AS day_rank
        FROM candidates
        WHERE nth_of_show = 1
    )
    SELECT {_SHOW_FIELDS}, {_EPISODE_FIELDS}, 0 AS is_downloaded
    FROM ranked AS r
    JOIN episodes AS e ON e.id = r.episode_id
    JOIN shows AS s ON s.id = e.show_id
    WHERE r.day_rank <= :per_day
    ORDER BY r.air_date, r.day_rank
"""


def _calendar_entry(row: sqlite3.Row, *, is_tracked: bool) -> CalendarEntry:
    return CalendarEntry(
        show=_show_from_row(_section(row, "show__")),
        episode=Episode.model_validate(_section(row, "ep__")),
        is_tracked=is_tracked,
        is_downloaded=bool(row["is_downloaded"]),
    )


class Database:
    """One aiosqlite connection shared by the whole app.

    Every statement goes through an asyncio lock, so a coroutine can never observe (or write
    into) another coroutine's half-finished transaction on the shared connection.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._lock = asyncio.Lock()
        self.library = LibraryRepository(self)
        self.history = HistoryRepository(self)
        self.settings = SettingsRepository(self)

    @classmethod
    async def open(cls, path: Path | str) -> Database:
        # Autocommit mode: transactions are opened explicitly by `transaction()`.
        conn = await aiosqlite.connect(path, isolation_level=None)
        try:
            conn.row_factory = aiosqlite.Row
            for pragma in ("foreign_keys = ON", "journal_mode = WAL", "synchronous = NORMAL"):
                await conn.execute(f"PRAGMA {pragma}")
            db = cls(conn)
            await db._migrate()
            await db._check_columns()
        except BaseException:
            await conn.close()
            raise
        return db

    async def close(self) -> None:
        async with self._lock:
            await self._conn.close()

    async def fetch_all(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> list[sqlite3.Row]:
        async with self._lock:
            return list(await self._conn.execute_fetchall(sql, params))

    async def fetch_one(
        self, sql: str, params: Sequence[Any] | Mapping[str, Any] = ()
    ) -> sqlite3.Row | None:
        rows = await self.fetch_all(sql, params)
        return rows[0] if rows else None

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Atomic unit of work. Use the yielded connection — not `fetch_*` — inside the block."""
        async with self._lock:
            await self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                await self._conn.execute("ROLLBACK")
                raise
            else:
                await self._conn.execute("COMMIT")

    async def _migrate(self) -> None:
        row = await self.fetch_one("PRAGMA user_version")
        current = row[0] if row else 0
        if current > len(MIGRATIONS):
            raise RuntimeError(
                f"Database schema v{current} is newer than this build supports (v{len(MIGRATIONS)})"
            )
        for version, script in enumerate(MIGRATIONS[current:], start=current + 1):
            log.info("Migrating database schema to v%d", version)
            async with self._lock:
                try:
                    await self._conn.executescript(
                        f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;"
                    )
                except BaseException:
                    with contextlib.suppress(sqlite3.OperationalError):
                        await self._conn.execute("ROLLBACK")
                    raise

    async def _check_columns(self) -> None:
        """Fail fast if a table drifts from the model it is mapped to."""
        mapped: tuple[tuple[str, type[BaseModel]], ...] = (
            ("shows", Show),
            ("episodes", Episode),
            ("download_history", DownloadRecord),
        )
        for table, model in mapped:
            rows = await self.fetch_all(f"PRAGMA table_info({table})")
            columns = {row["name"] for row in rows}
            if columns != set(model.model_fields):
                raise RuntimeError(
                    f"Table {table!r} columns {sorted(columns)} don't match "
                    f"{model.__name__} fields {sorted(model.model_fields)}"
                )


class LibraryRepository:
    """Shows, their episodes, and which of them the user tracks."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def save_show(self, show: Show, episodes: Iterable[Episode] | None = None) -> None:
        """Upsert a show; when `episodes` is given it becomes the show's complete episode list."""
        episode_rows = None if episodes is None else [_episode_to_row(e) for e in episodes]
        if episode_rows and any(row["show_id"] != show.id for row in episode_rows):
            raise ValueError(f"Episodes passed to save_show must all belong to show {show.id}")
        async with self._db.transaction() as conn:
            await conn.execute(_SHOW_UPSERT, _show_to_row(show))
            if episode_rows is not None:
                await conn.execute("DELETE FROM episodes WHERE show_id = ?", (show.id,))
                await conn.executemany(_EPISODE_UPSERT, episode_rows)

    async def save_schedule_day(
        self, day: date, shows: Iterable[Show], episodes: Iterable[Episode]
    ) -> None:
        """Cache one day of TVMaze's global schedule (feeds the calendar's suggestions)."""
        async with self._db.transaction() as conn:
            await conn.executemany(_SHOW_UPSERT, [_show_to_row(s) for s in shows])
            await conn.executemany(_EPISODE_UPSERT, [_episode_to_row(e) for e in episodes])
            await conn.execute(
                "INSERT INTO schedule_fetches (air_date, fetched_at) VALUES (?, ?) "
                "ON CONFLICT (air_date) DO UPDATE SET fetched_at = excluded.fetched_at",
                (day.isoformat(), _now()),
            )

    async def track(self, show_id: int, *, auto_download: bool = False) -> None:
        """Add a show to the library. The show must already be saved (`save_show`)."""
        async with self._db.transaction() as conn:
            await conn.execute(
                "INSERT INTO tracked_shows (show_id, tracked_at, auto_download) VALUES (?, ?, ?) "
                "ON CONFLICT (show_id) DO NOTHING",
                (show_id, _now(), auto_download),
            )

    async def untrack(self, show_id: int) -> None:
        async with self._db.transaction() as conn:
            await conn.execute("DELETE FROM tracked_shows WHERE show_id = ?", (show_id,))

    async def untrack_ended(self) -> int:
        """Drop every tracked show whose status is Ended. Returns how many were removed."""
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "DELETE FROM tracked_shows "
                "WHERE show_id IN (SELECT id FROM shows WHERE status = 'Ended')"
            )
            return cursor.rowcount

    async def mark_synced(self, show_id: int) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE tracked_shows SET synced_at = ? WHERE show_id = ?", (_now(), show_id)
            )

    async def mark_synced_many(self, show_ids: Iterable[int]) -> None:
        now = _now()
        rows = [(now, show_id) for show_id in show_ids]
        if not rows:
            return
        async with self._db.transaction() as conn:
            await conn.executemany("UPDATE tracked_shows SET synced_at = ? WHERE show_id = ?", rows)

    async def prune_schedule(self, before: date) -> int:
        """Forget cached schedule data from before `before` for shows the user doesn't track.

        Deletes those shows' older episodes, untracked shows left without any episode, and the
        matching `schedule_fetches` rows. Tracked shows keep every episode. Returns how many
        episodes were deleted.
        """
        cutoff = before.isoformat()
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "DELETE FROM episodes WHERE air_date < ? "
                "AND show_id NOT IN (SELECT show_id FROM tracked_shows)",
                (cutoff,),
            )
            deleted = cursor.rowcount
            await conn.execute(
                "DELETE FROM shows WHERE id NOT IN (SELECT show_id FROM tracked_shows) "
                "AND NOT EXISTS (SELECT 1 FROM episodes AS e WHERE e.show_id = shows.id)"
            )
            await conn.execute("DELETE FROM schedule_fetches WHERE air_date < ?", (cutoff,))
        return deleted

    async def get_show(self, show_id: int) -> Show | None:
        row = await self._db.fetch_one("SELECT * FROM shows WHERE id = ?", (show_id,))
        return _show_from_row(dict(row)) if row else None

    async def tracked(self) -> list[LibraryEntry]:
        rows = await self._db.fetch_all(
            f"SELECT {_SHOW_FIELDS}, t.tracked_at, t.auto_download, t.synced_at "
            "FROM tracked_shows AS t JOIN shows AS s ON s.id = t.show_id "
            "ORDER BY s.name COLLATE NOCASE"
        )
        return [
            LibraryEntry(
                show=_show_from_row(_section(row, "show__")),
                tracked_at=row["tracked_at"],
                auto_download=row["auto_download"],
                synced_at=row["synced_at"],
            )
            for row in rows
        ]

    async def tracked_ids(self) -> frozenset[int]:
        rows = await self._db.fetch_all("SELECT show_id FROM tracked_shows")
        return frozenset(row["show_id"] for row in rows)

    async def episodes(self, show_id: int) -> list[Episode]:
        rows = await self._db.fetch_all(
            "SELECT * FROM episodes WHERE show_id = ? ORDER BY season, number", (show_id,)
        )
        return [Episode.model_validate(dict(row)) for row in rows]

    async def calendar(self, start: date, end: date) -> list[CalendarEntry]:
        """Tracked episodes airing between `start` and `end` (inclusive), in airing order."""
        rows = await self._db.fetch_all(
            _CALENDAR_SQL, {"start": start.isoformat(), "end": end.isoformat()}
        )
        return [_calendar_entry(row, is_tracked=True) for row in rows]

    async def suggestions(
        self,
        start: date,
        end: date,
        *,
        per_day: int,
        language: str = "English",
        types: Sequence[str] = ("Scripted", "Animation"),
        min_weight: int = 40,
    ) -> list[CalendarEntry]:
        """The most popular untracked shows airing each day, from the cached TVMaze schedule."""
        rows = await self._db.fetch_all(
            _SUGGESTIONS_SQL,
            {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "language": language,
                "types": json.dumps(list(types)),
                "min_weight": min_weight,
                "per_day": per_day,
            },
        )
        return [_calendar_entry(row, is_tracked=False) for row in rows]

    async def schedule_fetches(self, start: date, end: date) -> dict[date, datetime]:
        """When each day in the range was last pulled from TVMaze's schedule."""
        rows = await self._db.fetch_all(
            "SELECT air_date, fetched_at FROM schedule_fetches WHERE air_date BETWEEN ? AND ?",
            (start.isoformat(), end.isoformat()),
        )
        return {
            date.fromisoformat(row["air_date"]): datetime.fromisoformat(row["fetched_at"])
            for row in rows
        }

    async def stats(self) -> LibraryStats:
        row = await self._db.fetch_one(
            """
            SELECT (SELECT COUNT(*) FROM tracked_shows) AS tracked_shows,
                   (SELECT COUNT(*) FROM episodes AS e
                      JOIN tracked_shows AS t ON t.show_id = e.show_id) AS episodes,
                   (SELECT MAX(synced_at) FROM tracked_shows) AS last_synced
            """
        )
        assert row is not None  # an aggregate-only SELECT always yields one row
        return LibraryStats.model_validate(dict(row))


class HistoryRepository:
    """Grabbed releases, used to flag what was already downloaded."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def add(self, record: DownloadRecord) -> bool:
        """Record a grab. Returns False if this exact torrent (info hash) was already recorded."""
        columns = [column for column in HISTORY_COLUMNS if column != "id"]
        sql = (
            f"INSERT INTO download_history ({', '.join(columns)}) "
            f"VALUES ({', '.join(':' + column for column in columns)}) "
            "ON CONFLICT (info_hash) DO NOTHING"
        )
        async with self._db.transaction() as conn:
            cursor = await conn.execute(sql, record.model_dump(mode="json", exclude={"id"}))
            return cursor.rowcount > 0

    async def has_episode(self, show_id: int, season: int, episode: int | None) -> bool:
        """True if the episode — or a pack of its whole season — was grabbed."""
        row = await self._db.fetch_one(
            "SELECT EXISTS (SELECT 1 FROM download_history WHERE media_type = ? AND media_id = ? "
            "AND season = ? AND (episode IS NULL OR episode IS ?))",
            (MediaType.TV, show_id, season, episode),
        )
        return bool(row and row[0])

    async def has_movie(self, movie_id: int) -> bool:
        row = await self._db.fetch_one(
            "SELECT EXISTS (SELECT 1 FROM download_history WHERE media_type = ? AND media_id = ?)",
            (MediaType.MOVIE, movie_id),
        )
        return bool(row and row[0])

    async def recent(self, limit: int = 50) -> list[DownloadRecord]:
        rows = await self._db.fetch_all(
            "SELECT * FROM download_history ORDER BY downloaded_at DESC LIMIT ?", (limit,)
        )
        return [DownloadRecord.model_validate(dict(row)) for row in rows]

    async def info_hashes(self) -> frozenset[str]:
        """Info hashes (lowercase hex) of every recorded grab that has one."""
        rows = await self._db.fetch_all(
            "SELECT info_hash FROM download_history WHERE info_hash IS NOT NULL"
        )
        return frozenset(row["info_hash"] for row in rows)


class SettingsRepository:
    """Preferences as key/value rows, validated as one `Settings` model on the way out."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def load(self) -> Settings:
        rows = await self._db.fetch_all("SELECT key, value FROM settings")
        stored = {row["key"]: json.loads(row["value"]) for row in rows}
        try:
            return Settings.model_validate(stored)
        except ValidationError as exc:
            # Keep the valid preferences rather than resetting everything over one bad value.
            invalid = {str(error["loc"][0]) for error in exc.errors() if error["loc"]}
            log.warning("Ignoring invalid stored settings: %s", ", ".join(sorted(invalid)) or "?")
            try:
                return Settings.model_validate({k: v for k, v in stored.items() if k not in invalid})
            except ValidationError:
                return Settings()

    async def save(self, settings: Settings) -> None:
        rows = [(key, json.dumps(value)) for key, value in settings.model_dump(mode="json").items()]
        async with self._db.transaction() as conn:
            await conn.executemany(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                rows,
            )
