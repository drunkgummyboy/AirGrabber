"""Forge's own database file, `forge.db`: settings, presets, remembered matches, the TMDB cache,
the undo history and (from v3) the move records.

MediaForge's schema v1 and v2 are kept exactly, so an old `mediaforge.db` copied in opens and
is migrated on; new migrations continue from v3. The connection follows AirGrabber's pattern
(`app/database.py`): one aiosqlite connection in autocommit mode with an asyncio lock around
every statement, and `transaction()` for writes, so the automatic renamer and the window can
never commit each other's half-done writes.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiosqlite

from app.config import AppPaths

log = logging.getLogger(__name__)

DB_NAME = "forge.db"

# MediaForge's schema v1, word for word (an old file must open unchanged).
_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS presets (
    name     TEXT PRIMARY KEY,
    position INTEGER NOT NULL,
    type     TEXT NOT NULL,
    format   TEXT NOT NULL,
    options  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS remembered_matches (
    kind       TEXT NOT NULL,
    norm_title TEXT NOT NULL,
    year       INTEGER NOT NULL DEFAULT 0,
    tmdb_id    INTEGER,
    label      TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (kind, norm_title, year)
);
CREATE TABLE IF NOT EXISTS tmdb_cache (
    key        TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    fetched_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    started_at  REAL NOT NULL,
    finished_at REAL,
    source      TEXT NOT NULL,
    preset_name TEXT
);
CREATE TABLE IF NOT EXISTS operations (
    id         INTEGER PRIMARY KEY,
    run_id     INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq        INTEGER NOT NULL,
    kind       TEXT NOT NULL,
    src        TEXT,
    dst        TEXT,
    size       INTEGER,
    mtime_ns   INTEGER,
    state      TEXT NOT NULL,
    error      TEXT,
    undo_state TEXT,
    undo_note  TEXT,
    undone_at  REAL
);
CREATE INDEX IF NOT EXISTS operations_run ON operations(run_id, seq);
"""

# v3 (AirGrabber 3.0.0): the move records (app/forge/db/moves.py). `src_key` holds
# os.path.normcase(src), so Windows paths compare as Windows does. `op_id` points at the journal
# row (operations.id) without a foreign key: move records outlive the 50 runs of undo history.
_SCHEMA_V3 = """
CREATE TABLE moves (
    id          INTEGER PRIMARY KEY,
    op_id       INTEGER,
    run_id      INTEGER,
    src         TEXT NOT NULL,
    src_key     TEXT NOT NULL,
    dst         TEXT NOT NULL,
    moved_at    REAL NOT NULL,
    kind        TEXT NOT NULL,
    tmdb_id     INTEGER
);
CREATE INDEX moves_src ON moves(src_key);
CREATE INDEX moves_op ON moves(op_id);
CREATE INDEX moves_moved_at ON moves(moved_at);
"""

# Entry i upgrades the schema from version i to i + 1. Append only — never edit a shipped entry.
MIGRATIONS: tuple[str, ...] = (
    _SCHEMA_V1,
    # v2 (MediaForge 1.3): which kinds a run renamed, for History's Series | Movies switch.
    "ALTER TABLE runs ADD COLUMN kinds TEXT NOT NULL DEFAULT '';",
    # v3 (AirGrabber 3.0.0): the move records.
    _SCHEMA_V3,
)
SCHEMA_VERSION = len(MIGRATIONS)


def db_path(paths: AppPaths) -> Path:
    """Where Forge keeps its database: `AppPaths.forge / "forge.db"`."""
    return paths.forge / DB_NAME


class Database:
    """One aiosqlite connection for all of Forge.

    Every statement goes through an asyncio lock, so a coroutine can never observe (or write
    into) another coroutine's half-finished transaction on the shared connection. Read with
    `fetch_all` / `fetch_one`; write inside `transaction()`.
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn
        self._lock = asyncio.Lock()

    @classmethod
    async def open(cls, path: Path) -> Database:
        """Open (and create or upgrade) the database file; its folder is made when missing."""
        path.parent.mkdir(parents=True, exist_ok=True)
        # Autocommit mode: transactions are opened explicitly by `transaction()`.
        conn = await aiosqlite.connect(path, isolation_level=None)
        try:
            conn.row_factory = aiosqlite.Row
            for pragma in ("journal_mode = WAL", "foreign_keys = ON", "synchronous = NORMAL"):
                await conn.execute(f"PRAGMA {pragma}")
            db = cls(conn)
            await db._migrate()
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
        current = int(row[0]) if row else 0
        if current > len(MIGRATIONS):
            raise RuntimeError(
                f"Forge database schema v{current} is newer than this build supports "
                f"(v{len(MIGRATIONS)})"
            )
        for version, script in enumerate(MIGRATIONS[current:], start=current + 1):
            log.info("Migrating the Forge database schema to v%d", version)
            async with self._lock:
                try:
                    await self._conn.executescript(
                        f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;"
                    )
                except BaseException:
                    with contextlib.suppress(sqlite3.OperationalError):
                        await self._conn.execute("ROLLBACK")
                    raise
