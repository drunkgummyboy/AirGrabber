from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import aiosqlite

from app.config import AppPaths
from app.forge.db.cache import CacheRepo
from app.forge.db.database import SCHEMA_VERSION, Database, db_path
from app.forge.db.history import HistoryRepo
from app.forge.db.matches import MatchRepo
from app.forge.db.presets import PresetRepo
from app.forge.db.settings import SettingsRepo
from app.forge.models.history import OpState, RunSource, UndoState
from app.forge.models.media import MediaKind, TypeHint
from tests.forge.db.base import ForgeDbTestCase

# MediaForge 1.3's database as its `Database._migrate` left it (schema v2): v1's tables, then
# the `kinds` column. A frozen copy, so a change to app/forge/db/database.py can't move it.
MEDIAFORGE_1_3_SCHEMA = """
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
ALTER TABLE runs ADD COLUMN kinds TEXT NOT NULL DEFAULT '';
PRAGMA user_version=2;
"""

# What a MediaForge 1.3 user's file holds: made-up rows in every table.
MEDIAFORGE_1_3_ROWS = """
INSERT INTO settings (key, value) VALUES
    ('app', '{"tmdb_key": "made-up-key", "theme": "Light", "tmdb_language": "nl-NL",
              "series_folder": "D:\\\\Series", "automatic": "trial"}');
INSERT INTO presets (name, position, type, format, options) VALUES
    ('Mine', 0, 'tv', '{n} - {s00e00}', '{"series_poster": true}'),
    ('Auto', 1, 'auto', '{ny}', '{}');
INSERT INTO remembered_matches (kind, norm_title, year, tmdb_id, label, created_at) VALUES
    ('episode', 'the office', 2005, 2316, 'The Office (2005)', 1700000000.0),
    ('movie', 'home video', 0, NULL, 'Don''t use TMDB', 1700000001.0);
INSERT INTO tmdb_cache (key, payload, fetched_at) VALUES ('tv:the office', '[1, 2]', 4e9);
INSERT INTO runs (id, started_at, finished_at, source, preset_name, kinds) VALUES
    (7, 1700000000.0, 1700000005.0, 'automatic', 'Mine', 'episode');
INSERT INTO operations (id, run_id, seq, kind, src, dst, size, mtime_ns, state, error,
                        undo_state, undo_note, undone_at) VALUES
    (1, 7, 1, 'rename', 'C:\\m\\a.mkv', 'D:\\Series\\a.mkv', 5, 6, 'done', NULL,
     'done', NULL, 1700000010.0),
    (2, 7, 2, 'rename', 'C:\\m\\b.mkv', 'D:\\Series\\b.mkv', NULL, NULL, 'pending', NULL,
     NULL, NULL, NULL);
"""

_SCHEMA_SQL = "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"


async def _mediaforge_1_3_file(path: Path) -> None:
    async with aiosqlite.connect(path) as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.executescript(MEDIAFORGE_1_3_SCHEMA)
        await conn.executescript(MEDIAFORGE_1_3_ROWS)
        await conn.commit()


async def _schema(path: Path) -> list[tuple[object, ...]]:
    async with aiosqlite.connect(path) as conn:
        return [tuple(row) for row in await conn.execute_fetchall(_SCHEMA_SQL)]


class DatabaseTest(ForgeDbTestCase):
    async def test_open_sets_wal_and_version(self) -> None:
        assert await self.scalar("PRAGMA journal_mode") == "wal"
        assert await self.scalar("PRAGMA user_version") == SCHEMA_VERSION == 4
        assert await self.scalar("PRAGMA foreign_keys") == 1

    async def test_open_twice_is_idempotent(self) -> None:
        first = await Database.open(self.tmp / "x.db")
        await first.close()
        second = await Database.open(self.tmp / "x.db")
        try:
            row = await second.fetch_one("PRAGMA user_version")
            assert row is not None and row[0] == SCHEMA_VERSION
        finally:
            await second.close()

    async def test_open_creates_parent_folder(self) -> None:
        database = await Database.open(self.tmp / "a" / "b" / "x.db")
        await database.close()
        assert (self.tmp / "a" / "b" / "x.db").exists()

    def test_the_file_lives_in_forges_data_folder(self) -> None:
        paths = AppPaths(self.data_dir)
        assert db_path(paths) == self.data_dir / "forge" / "forge.db"

    async def test_a_newer_schema_is_refused(self) -> None:
        path = self.tmp / "future.db"
        async with aiosqlite.connect(path) as conn:
            await conn.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
            await conn.commit()
        with self.assertRaisesRegex(RuntimeError, "newer"):
            await Database.open(path)
        path.unlink()  # the connection was closed: Windows lets the file go

    async def test_a_failed_migration_changes_nothing(self) -> None:
        # A v1 file whose `runs` already has `kinds`: v2's ALTER fails, the file stays v1.
        path = self.tmp / "odd.db"
        async with aiosqlite.connect(path) as conn:
            await conn.executescript(MEDIAFORGE_1_3_SCHEMA)
            await conn.execute("PRAGMA user_version=1")
            await conn.commit()
        with self.assertRaises(sqlite3.OperationalError):
            await Database.open(path)
        async with aiosqlite.connect(path) as conn:
            [(version,)] = list(await conn.execute_fetchall("PRAGMA user_version"))
        assert version == 1

    async def test_a_failed_transaction_is_rolled_back(self) -> None:
        with self.assertRaises(ZeroDivisionError):
            async with self.db.transaction() as conn:
                await conn.execute("INSERT INTO settings (key, value) VALUES ('x', '1')")
                _ = 1 / 0
        assert await self.db.fetch_all("SELECT * FROM settings") == []
        async with self.db.transaction() as conn:  # the lock and the connection still work
            await conn.execute("INSERT INTO settings (key, value) VALUES ('x', '1')")
        assert await self.scalar("SELECT value FROM settings WHERE key = 'x'") == "1"

    async def test_a_reader_waits_for_a_transaction(self) -> None:
        inside = asyncio.Event()
        release = asyncio.Event()

        async def write() -> None:
            async with self.db.transaction() as conn:
                await conn.execute("INSERT INTO settings (key, value) VALUES ('a', '1')")
                inside.set()
                await release.wait()
                await conn.execute("INSERT INTO settings (key, value) VALUES ('b', '2')")

        writer = asyncio.create_task(write())
        await inside.wait()
        reader = asyncio.create_task(self.db.fetch_all("SELECT key FROM settings ORDER BY key"))
        await asyncio.sleep(0.05)
        assert not reader.done()  # it would otherwise see 'a' without 'b'
        release.set()
        await writer
        assert [row[0] for row in await reader] == ["a", "b"]


class MediaForgeFileTest(ForgeDbTestCase):
    """A database written by MediaForge 1.3 (schema v2), copied in, opens with its rows and its
    own tables unchanged; v3 and v4 only add the move records' and the ignored paths' tables."""

    async def test_opens_and_keeps_its_rows(self) -> None:
        path = self.tmp / "mediaforge.db"
        await _mediaforge_1_3_file(path)
        before = await _schema(path)

        db = await Database.open(path)
        try:
            assert (await db.fetch_one("PRAGMA user_version") or [None])[0] == SCHEMA_VERSION == 4
            assert (await db.fetch_one("SELECT COUNT(*) FROM moves") or [None])[0] == 0

            settings = await SettingsRepo(db).load()
            assert (settings.tmdb_language, settings.series_folder) == ("nl-NL", r"D:\Series")
            assert settings.automatic == "trial"

            presets = await PresetRepo(db).list()
            assert [(p.name, p.type) for p in presets] == [
                ("Mine", TypeHint.TV),
                ("Auto", TypeHint.AUTO),
            ]
            assert presets[0].options.series_poster

            matches = MatchRepo(db)
            office = await matches.get(MediaKind.EPISODE, "the office", 2005)
            assert office is not None and office.tmdb_id == 2316
            skipped = await matches.get(MediaKind.MOVIE, "home video", None)
            assert skipped is not None and skipped.tmdb_id is None
            assert len(await matches.list()) == 2

            assert await CacheRepo(db).get_json("tv:the office", max_age_s=60) == [1, 2]

            history = HistoryRepo(db)
            [run] = await history.list_runs()
            assert (run.id, run.source, run.preset_name) == (7, RunSource.AUTOMATIC, "Mine")
            assert run.kinds == {MediaKind.EPISODE}
            assert (run.file_count, run.undone_count) == (1, 1)
            [pending] = await history.pending_operations()
            assert (pending.id, pending.state) == (2, OpState.PENDING)
            [done, _] = await history.operations(7)
            assert done.undo_state is UndoState.DONE and done.undone_at is not None
            assert await history.was_undone(Path(r"C:\m\a.mkv"))
        finally:
            await db.close()
        after = await _schema(path)
        assert set(before) <= set(after)  # MediaForge's own tables and index are untouched
        added = {(kind, name) for kind, name, _, _ in set(after) - set(before)}
        assert added == {
            ("table", "moves"),
            ("index", "moves_src"),
            ("index", "moves_op"),
            ("index", "moves_moved_at"),
            ("table", "ignored"),  # v4
            ("index", "sqlite_autoindex_ignored_1"),  # its primary key
        }

    async def test_a_new_forge_db_has_the_same_schema_as_a_migrated_one(self) -> None:
        old = self.tmp / "mediaforge.db"
        await _mediaforge_1_3_file(old)
        await (await Database.open(old)).close()  # v2 → v3
        new = self.tmp / "forge.db"
        await (await Database.open(new)).close()
        assert await _schema(new) == await _schema(old)
