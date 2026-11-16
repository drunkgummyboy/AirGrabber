"""The database's schema steps that aren't any one feature's, and the folder library's tables
(migration 12: `library_files`, `watch_places`; LibraryFileRepository)."""

from __future__ import annotations

import contextlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import database
from app.database import FOLDER_LIBRARY_MIGRATION, MIGRATIONS, Database
from app.models.folder_library import LibraryFile


def schema(path: Path) -> dict[str, str]:
    """Every table's and index's CREATE statement, by name."""
    with contextlib.closing(sqlite3.connect(path)) as raw:
        rows = raw.execute(
            "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY name"
        ).fetchall()
    return {name: sql for name, sql in rows}


def make_v11(path: Path) -> None:
    with contextlib.closing(sqlite3.connect(path)) as raw:
        script = "\n".join(MIGRATIONS[:11])
        raw.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = 11;\nCOMMIT;")


class FolderLibraryMigrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    async def test_v12_adds_the_folder_library_tables(self) -> None:
        self.assertEqual(len(MIGRATIONS), 12)
        self.assertEqual(MIGRATIONS[11], FOLDER_LIBRARY_MIGRATION)
        self.assertNotIn("ALTER TABLE", FOLDER_LIBRARY_MIGRATION)
        self.assertNotIn("DROP", FOLDER_LIBRARY_MIGRATION)
        path = self.dir / "v11.db"
        make_v11(path)
        before = schema(path)
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
