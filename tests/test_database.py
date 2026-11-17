"""The database's schema steps that aren't any one feature's, and the folder library's tables
(migration 12: `library_files`, `watch_places`; LibraryFileRepository; migration 13: the
folders' own `folder_*` tables, 3.17.0's folder rows moved out of `plex_*`)."""

from __future__ import annotations

import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from datetime import UTC, datetime
from unittest import mock

from app import database
from app.database import (
    FOLDER_LIBRARY_MIGRATION,
    FOLDER_TABLES_MIGRATION,
    MIGRATIONS,
    Database,
)
from app.models import Show
from app.models.folder_library import LibraryFile
from app.models.plex import PlexScan, PlexTitle


def schema(path: Path) -> dict[str, str]:
    """Every table's and index's CREATE statement, by name."""
    with contextlib.closing(sqlite3.connect(path)) as raw:
        rows = raw.execute(
            "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
        ).fetchall()
    return {name: sql for name, sql in rows}


def make_v11(path: Path) -> None:
    make_version(path, 11)


def make_version(path: Path, version: int) -> None:
    with contextlib.closing(sqlite3.connect(path)) as raw:
        script = "\n".join(MIGRATIONS[:version])
        raw.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")


class FolderLibraryMigrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    async def test_v12_adds_the_folder_library_tables(self) -> None:
        self.assertEqual(MIGRATIONS[11], FOLDER_LIBRARY_MIGRATION)
        self.assertNotIn("ALTER TABLE", FOLDER_LIBRARY_MIGRATION)
        self.assertNotIn("DROP", FOLDER_LIBRARY_MIGRATION)
        path = self.dir / "v11.db"
        make_v11(path)
        before = schema(path)
        with mock.patch.object(database, "MIGRATIONS", MIGRATIONS[:12]):
            db = await Database.open(path)
        self.addAsyncCleanup(db.close)
        version = await db.fetch_one("PRAGMA user_version")
        assert version is not None
        self.assertEqual(version[0], 12)
        after = schema(path)
        # Nothing that was there changed; only the two tables are new.
        self.assertEqual({name: after[name] for name in before}, before)
        self.assertEqual(set(after) - set(before), {"library_files", "watch_places"})
        tables = {row["name"]: row["strict"] for row in await db.fetch_all("PRAGMA table_list")}
        self.assertEqual((tables["library_files"], tables["watch_places"]), (1, 1))
        columns = {
            table: [row["name"] for row in await db.fetch_all(f"PRAGMA table_info({table})")]
            for table in ("library_files", "watch_places")
        }
        self.assertEqual(
            columns["library_files"],
            ["path", "size", "mtime", "kind", "tmdb_id", "season", "episode", "episode_end",
             "quality", "recognised"],
        )
        self.assertEqual(
            columns["watch_places"],
            ["profile_id", "item", "position_ms", "duration_ms", "watched", "updated_at"],
        )
        self.assertEqual(await db.library_files.all(), {})

    async def test_v11_code_still_reads_a_v12_file(self) -> None:
        """The update guard puts the old database back when going back (a newer user_version);
        beyond user_version, a v12 file holds nothing v11's code can't read."""
        path = self.dir / "v12.db"
        db = await Database.open(path)
        await db.library_files.replace_folder(
            "D:\\Films", [LibraryFile(path="D:\\Films\\a.mkv", size=1, mtime=2.0, recognised=False)]
        )
        await db.close()
        with contextlib.closing(sqlite3.connect(path)) as raw:
            raw.execute("PRAGMA user_version = 11")
        with mock.patch.object(database, "MIGRATIONS", MIGRATIONS[:11]):
            old = await Database.open(path)  # v11's migrations: nothing to do, columns checked
            self.addAsyncCleanup(old.close)
            self.assertEqual(await old.plex.titles(), [])
            self.assertEqual(await old.library.followed(), [])
            version = await old.fetch_one("PRAGMA user_version")
            assert version is not None
            self.assertEqual(version[0], 11)


FOLDER_TABLES = {
    "folder_titles", "folder_episodes", "folder_movies", "folder_state", "folder_gone",
    "ix_folder_movies_tmdb", "ix_folder_movies_imdb", "ix_folder_gone_episode",
}


def _scan(**values: object) -> PlexScan:
    return PlexScan.model_validate(values)


class FolderTablesMigrationTests(unittest.IsolatedAsyncioTestCase):
    """v13: the folders' own tables beside Plex's (docs/folder-library/
    2026-11-16-alongside-plex-design.md)."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    async def test_v13_adds_tables_only(self) -> None:
        self.assertEqual(len(MIGRATIONS), 13)
        self.assertEqual(MIGRATIONS[12], FOLDER_TABLES_MIGRATION)
        self.assertNotIn("ALTER", FOLDER_TABLES_MIGRATION.upper())
        self.assertNotIn("DROP", FOLDER_TABLES_MIGRATION.upper())
        path = self.dir / "v12.db"
        make_version(path, 12)
        before = schema(path)
        db = await Database.open(path)
        self.addAsyncCleanup(db.close)
        version = await db.fetch_one("PRAGMA user_version")
        assert version is not None
        self.assertEqual(version[0], 13)
        after = schema(path)
        # Nothing that was there changed; only the folders' tables (and their indexes) are new.
        self.assertEqual({name: after[name] for name in before}, before)
        self.assertEqual(set(after) - set(before), FOLDER_TABLES)
        tables = {row["name"]: row["strict"] for row in await db.fetch_all("PRAGMA table_list")}
        self.assertEqual(
            {name: tables.get(name) for name in FOLDER_TABLES if not name.startswith("ix_")},
            {name: 1 for name in FOLDER_TABLES if not name.startswith("ix_")},
        )

        async def columns(table: str) -> list[str]:
            return [row["name"] for row in await db.fetch_all(f"PRAGMA table_info({table})")]

        for table in ("titles", "episodes", "movies"):
            self.assertEqual(await columns(f"folder_{table}"), await columns(f"plex_{table}"))
        self.assertEqual(
            await columns("folder_state"),
            ["id", "checked_at", "shows", "unmatched_shows", "unmatched_films"],
        )
        self.assertEqual(
            await columns("folder_gone"),
            ["kind", "show_id", "season", "episode", "tmdb_id", "imdb_id", "left_at"],
        )
        # The same cascade from the shows cache as plex_episodes.
        keys = await db.fetch_all("PRAGMA foreign_key_list(folder_episodes)")
        self.assertEqual(
            [(row["table"], row["from"], row["on_delete"]) for row in keys],
            [("shows", "show_id", "CASCADE")],
        )

    def v12_with(self, path: Path, *, folder_rows: bool) -> None:
        """A 3.17.0 database whose plex_* tables hold the folders' rows (no Plex token) or
        Plex's."""
        make_version(path, 12)
        key = "folder:show:95396" if folder_rows else "4711"
        film_key = "folder:movie:438631" if folder_rows else "4712"
        libraries = "[]" if folder_rows else '[{"key": "1", "title": "TV", "kind": "show"}]'
        with contextlib.closing(sqlite3.connect(path)) as raw, raw:
            raw.execute("INSERT INTO shows (id, name, tvdb_id) VALUES (1, 'Severance', 371980)")
            raw.execute(
                "INSERT INTO plex_titles (kind, rating_key, title, year, show_id, tmdb_id) "
                "VALUES ('show', ?, 'Severance', 2022, 1, 95396), "
                "('movie', ?, 'Dune', 2021, NULL, 438631)",
                (key, film_key),
            )
            raw.execute(
                "INSERT INTO plex_episodes (show_id, season, episode) VALUES (1, 1, 1), (1, 1, 2)"
            )
            raw.execute("INSERT INTO plex_movies (tmdb_id, imdb_id) VALUES (438631, 'tt1160419')")
            raw.execute(
                "INSERT INTO plex_state (id, checked_at, shows, unmatched_shows, "
                "unmatched_films, libraries) VALUES (1, '2026-11-15T10:00:00+00:00', 1, 2, 3, ?)",
                (libraries,),
            )

    async def count(self, db: Database, table: str) -> int:
        row = await db.fetch_one(f"SELECT COUNT(*) FROM {table}")
        assert row is not None
        return int(row[0])

    async def test_v13_moves_the_folder_rows_out_of_plex(self) -> None:
        path = self.dir / "folders.db"
        self.v12_with(path, folder_rows=True)
        db = await Database.open(path)
        self.addAsyncCleanup(db.close)
        for table in ("plex_titles", "plex_episodes", "plex_movies", "plex_state"):
            self.assertEqual(await self.count(db, table), 0, table)
        self.assertEqual(await db.plex.titles(), [])
        self.assertEqual(
            [(title.kind, title.key, title.title, title.show_id)
             for title in await db.folders.titles()],
            [("movie", "folder:movie:438631", "Dune", None),
             ("show", "folder:show:95396", "Severance", 1)],
        )
        self.assertEqual(
            await db.folders.contents(),
            (frozenset({(1, 1, 1), (1, 1, 2)}), frozenset({(438631, "tt1160419")})),
        )
        checked = await db.folders.checked_at()
        assert checked is not None
        self.assertEqual(checked.isoformat(), "2026-11-15T10:00:00+00:00")
        state = await db.fetch_one("SELECT * FROM folder_state")
        assert state is not None
        self.assertEqual(
            (state["shows"], state["unmatched_shows"], state["unmatched_films"]), (1, 2, 3)
        )
        self.assertEqual(await self.count(db, "folder_gone"), 0)
        # The same marks, now from the folders' tables.
        self.assertTrue((await db.library.plex_films()).has(438631))
        self.assertTrue((await db.library.plex_shows()).has(371980))
        self.assertEqual(await db.library.plex_ids(1), frozenset())

    async def test_v13_leaves_plex_rows_where_they_are(self) -> None:
        path = self.dir / "plex.db"
        self.v12_with(path, folder_rows=False)
        db = await Database.open(path)
        self.addAsyncCleanup(db.close)
        for table in ("folder_titles", "folder_episodes", "folder_movies", "folder_state"):
            self.assertEqual(await self.count(db, table), 0, table)
        self.assertEqual([title.key for title in await db.plex.titles()], ["4712", "4711"])
        self.assertEqual(
            await db.plex.contents(),
            (frozenset({(1, 1, 1), (1, 1, 2)}), frozenset({(438631, "tt1160419")})),
        )
        self.assertEqual([library.key for library in await db.plex.libraries()], ["1"])
        self.assertIsNone(await db.folders.checked_at())

    async def test_v12_code_still_reads_a_v13_file(self) -> None:
        """Going back puts the copy from before the update in place (update_guard); even so,
        a v13 file holds nothing v12's code can't read."""
        path = self.dir / "v13.db"
        db = await Database.open(path)
        await db.library.save_show(Show(id=1, name="Severance"), [])
        await db.folders.replace(
            _scan(
                titles=(PlexTitle(kind="show", key="folder:show:1", title="Severance"),),
                episodes=frozenset({(1, 1, 1)}),
            ),
            datetime.now(UTC),
        )
        await db.close()
        with contextlib.closing(sqlite3.connect(path)) as raw:
            raw.execute("PRAGMA user_version = 12")
        with mock.patch.object(database, "MIGRATIONS", MIGRATIONS[:12]):
            old = await Database.open(path)
            self.addAsyncCleanup(old.close)
            self.assertEqual(await old.plex.titles(), [])
            self.assertEqual(await old.library.followed(), [])


class LibraryFileRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.db = await Database.open(Path(tmp.name) / "airgrabber.db")
        self.addAsyncCleanup(self.db.close)
        self.files = self.db.library_files

    def row(self, path: str, **values: object) -> LibraryFile:
        fields: dict[str, object] = {"size": 10, "mtime": 1_800_000_000.5, "recognised": True}
        fields.update(values)
        return LibraryFile.model_validate({"path": path, **fields})

    async def test_rows_round_trip(self) -> None:
        film = self.row(
            "D:\\Films\\Dune (2021)\\Dune (2021).mkv", kind="movie", tmdb_id=438631,
            quality="4K",
        )
        episode = self.row(
            "\\\\NAS\\Series\\Severance\\S01E02-E03.mkv", kind="episode", tmdb_id=95396,
            season=1, episode=2, episode_end=3, quality="1080p",
        )
        odd = self.row("D:\\Films\\zzz.mkv", recognised=False)
        await self.files.replace_folder("D:\\Films", [film, odd])
        await self.files.replace_folder("\\\\NAS\\Series", [episode])
        self.assertEqual(
            await self.files.all(), {row.path: row for row in (film, odd, episode)}
        )

    async def test_replace_folder_touches_that_folder_only(self) -> None:
        await self.files.replace_folder(
            "D:\\Films", [self.row("D:\\Films\\a.mkv"), self.row("D:\\Films\\b\\b.mkv")]
        )
        await self.files.replace_folder("D:\\Films2", [self.row("D:\\Films2\\c.mkv")])
        await self.files.replace_folder("M:\\", [self.row("M:\\d.mkv")])
        await self.files.replace_folder("D:\\Films", [self.row("D:\\Films\\b\\b.mkv", size=11)])
        stored = await self.files.all()
        self.assertEqual(set(stored), {"D:\\Films\\b\\b.mkv", "D:\\Films2\\c.mkv", "M:\\d.mkv"})
        self.assertEqual(stored["D:\\Films\\b\\b.mkv"].size, 11)
        await self.files.replace_folder("M:\\", [])
        self.assertEqual(set(await self.files.all()), {"D:\\Films\\b\\b.mkv", "D:\\Films2\\c.mkv"})
        # A folder with a trailing separator is the same folder; % and _ are no wildcards.
        await self.files.replace_folder("D:\\Films\\", [])
        await self.files.replace_folder("D:\\Film_", [])
        await self.files.replace_folder("D:\\%", [])
        self.assertEqual(set(await self.files.all()), {"D:\\Films2\\c.mkv"})

    async def test_clear(self) -> None:
        await self.files.replace_folder("D:\\Films", [self.row("D:\\Films\\a.mkv")])
        await self.files.clear()
        self.assertEqual(await self.files.all(), {})

    async def test_a_row_that_doesnt_read_is_left_out(self) -> None:
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO library_files (path, size, mtime, kind, recognised) "
                "VALUES ('D:\\Films\\x.mkv', 1, 2.0, 'trailer', 1)"
            )
        self.assertEqual(await self.files.all(), {})


if __name__ == "__main__":
    unittest.main()
