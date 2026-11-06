"""The way back after a bad update: what is kept, going back by itself and on request, the data."""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import update_guard
from app.update_guard import (
    ASIDE_SUFFIX,
    MAX_UNFINISHED_STARTS,
    PREVIOUS_DIR,
    SKIPPED_FILE,
    STAGING_DIR,
    WentBack,
    ask_to_go_back,
    before_start,
    confirm_started,
    going_back,
    keep_previous,
    previous_version,
    skipped_version,
    take_went_back,
)


def write_copy(folder: Path, version: str) -> None:
    (folder / "app").mkdir(parents=True)
    (folder / "main.py").write_text(f"# main {version}\n", encoding="utf-8")
    (folder / "requirements.txt").write_text(f"# packages {version}\n", encoding="utf-8")
    (folder / "app" / "__init__.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")


def make_database(path: Path, schema: int, rows: int = 1) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute("CREATE TABLE IF NOT EXISTS t (n INTEGER)")
        connection.execute("DELETE FROM t")
        connection.executemany("INSERT INTO t VALUES (?)", [(n,) for n in range(rows)])
        connection.execute(f"PRAGMA user_version = {schema}")
        connection.commit()
    finally:
        connection.close()


def row_count(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM t").fetchone()[0])
    finally:
        connection.close()


class GuardCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.dir = self.root / "AirGrabber"
        self.database = self.root / "data" / "airgrabber.db"
        self.databases: tuple[Path, ...] = (self.database,)
        write_copy(self.dir, "3.0.0")
        make_database(self.database, schema=5, rows=2)

    def update_to(self, version: str = "3.1.0") -> bool:
        """What an update does: old files moved aside, new ones in, the old ones kept."""
        old = self.dir / STAGING_DIR / "old"
        old.mkdir(parents=True)
        was = update_guard.read_version(self.dir / "app" / "__init__.py")
        for entry in update_guard.APP_ENTRIES:
            (self.dir / entry).rename(old / entry)
        write_copy(self.dir, version)
        kept = keep_previous(self.dir, old, was=was, now=version, databases=self.databases)
        update_guard.remove_tree(self.dir / STAGING_DIR)
        return kept

    def version(self) -> str | None:
        return update_guard.read_version(self.dir / "app" / "__init__.py")

    def start(self) -> bool:
        return before_start(self.dir, self.databases)


class KeepingTests(GuardCase):
    def test_an_update_keeps_the_old_files_and_data(self) -> None:
        self.assertTrue(self.update_to())
        self.assertEqual(previous_version(self.dir), "3.0.0")
        kept = self.dir / PREVIOUS_DIR
        self.assertEqual((kept / "files" / "main.py").read_text(encoding="utf-8"), "# main 3.0.0\n")
        self.assertEqual(row_count(kept / "data" / "airgrabber.db"), 2)

    def test_only_the_last_one_is_kept(self) -> None:
        self.update_to("3.1.0")
        self.update_to("3.2.0")
        self.assertEqual(previous_version(self.dir), "3.1.0")

    def test_a_database_that_is_open_copies_too(self) -> None:
        connection = sqlite3.connect(self.database)
        self.addCleanup(connection.close)
        self.assertTrue(self.update_to())

    def test_no_copy_of_the_data_means_no_way_back(self) -> None:
        with (
            mock.patch.object(update_guard, "_copy_databases", side_effect=OSError("disk full")),
            self.assertLogs(update_guard.log, "WARNING"),
        ):
            self.assertFalse(self.update_to())
        self.assertIsNone(previous_version(self.dir))
        self.assertFalse(self.start())

    def test_the_installer_putting_another_version_in_drops_it(self) -> None:
        self.update_to()
        (self.dir / "app" / "__init__.py").write_text('__version__ = "3.2.0"\n', encoding="utf-8")
        self.assertIsNone(previous_version(self.dir))
        self.assertFalse(self.start())
        self.assertFalse((self.dir / PREVIOUS_DIR).exists())
        self.assertEqual(self.version(), "3.2.0")

    def test_a_development_copy_has_no_way_back(self) -> None:
        self.update_to()
        (self.dir / "tests").mkdir()
        self.assertIsNone(previous_version(self.dir))
        for _ in range(MAX_UNFINISHED_STARTS + 2):
            self.assertFalse(self.start())
        self.assertEqual(self.version(), "3.1.0")
        self.assertFalse(ask_to_go_back(self.dir))

    def test_nothing_kept_nothing_happens(self) -> None:
        for _ in range(MAX_UNFINISHED_STARTS + 2):
            self.assertFalse(self.start())
        self.assertEqual(self.version(), "3.0.0")


class GoingBackByItselfTests(GuardCase):
    def test_starts_that_never_finish_go_back(self) -> None:
        self.update_to()
        for _ in range(MAX_UNFINISHED_STARTS):
            self.assertFalse(self.start())
            self.assertEqual(self.version(), "3.1.0")
        with self.assertLogs(update_guard.log, "WARNING"):
            self.assertTrue(self.start())
        self.assertEqual(self.version(), "3.0.0")
        self.assertEqual((self.dir / "main.py").read_text(encoding="utf-8"), "# main 3.0.0\n")
        self.assertEqual(skipped_version(self.dir), "3.1.0")
        self.assertFalse((self.dir / PREVIOUS_DIR).exists())
        self.assertEqual(take_went_back(self.dir), WentBack("3.1.0", "3.0.0", asked=False))
        self.assertIsNone(take_went_back(self.dir))  # once

    def test_a_start_that_worked_ends_the_counting(self) -> None:
        self.update_to()
        self.assertFalse(self.start())
        confirm_started(self.dir, "3.1.0")
        for _ in range(MAX_UNFINISHED_STARTS + 2):
            self.assertFalse(self.start())
        self.assertEqual(self.version(), "3.1.0")
        self.assertEqual(previous_version(self.dir), "3.0.0")  # Settings can still go back

    def test_the_old_version_closing_after_installing_confirms_nothing(self) -> None:
        self.update_to()  # Settings' "Update to …": version 3.0.0 is still the one running
        confirm_started(self.dir, "3.0.0")
        for _ in range(MAX_UNFINISHED_STARTS):
            self.start()
        with self.assertLogs(update_guard.log, "WARNING"):
            self.assertTrue(self.start())

    def test_a_failed_way_back_stays_and_does_not_try_again(self) -> None:
        self.update_to()
        real = update_guard.rename

        def failing(source: Path, destination: Path) -> None:
            if source == self.dir / "app":
                raise OSError("in use")
            real(source, destination)

        for _ in range(MAX_UNFINISHED_STARTS):
            self.start()
        with mock.patch.object(update_guard, "rename", failing), self.assertLogs(update_guard.log, "ERROR"):
            self.assertFalse(self.start())
        self.assertEqual(self.version(), "3.1.0")
        self.assertEqual((self.dir / "main.py").read_text(encoding="utf-8"), "# main 3.1.0\n")
        self.assertIsNone(skipped_version(self.dir))
        self.assertFalse(self.start())  # no more counting
        self.assertEqual(previous_version(self.dir), "3.0.0")

    def test_errors_never_escape(self) -> None:
        with (
            mock.patch.object(update_guard, "_before_start", side_effect=RuntimeError("x")),
            self.assertLogs(update_guard.log, "ERROR"),
        ):
            self.assertFalse(self.start())


class GoingBackOnRequestTests(GuardCase):
    def test_asked_goes_back_at_the_next_start(self) -> None:
        self.update_to()
        confirm_started(self.dir, "3.1.0")
        self.assertTrue(ask_to_go_back(self.dir))
        self.assertTrue(going_back(self.dir))
        self.assertEqual(self.version(), "3.1.0")  # nothing yet: the app is running
        with self.assertLogs(update_guard.log, "WARNING"):
            self.assertTrue(self.start())
        self.assertEqual(self.version(), "3.0.0")
        self.assertEqual(take_went_back(self.dir), WentBack("3.1.0", "3.0.0", asked=True))

    def test_a_downloaded_update_goes_too(self) -> None:
        self.update_to()
        (self.dir / STAGING_DIR / "new").mkdir(parents=True)
        self.assertTrue(ask_to_go_back(self.dir))
        self.assertFalse((self.dir / STAGING_DIR).exists())

    def test_nothing_to_go_back_to(self) -> None:
        self.assertFalse(ask_to_go_back(self.dir))
        self.assertFalse(going_back(self.dir))

    def test_a_newer_update_clears_the_skipped_version(self) -> None:
        self.update_to()
        self.assertTrue(ask_to_go_back(self.dir))
        with self.assertLogs(update_guard.log, "WARNING"):
            self.start()
        self.assertEqual((self.dir / SKIPPED_FILE).read_text(encoding="utf-8"), "3.1.0")
        self.update_to("3.2.0")
        self.assertIsNone(skipped_version(self.dir))


class DataTests(GuardCase):
    def go_back(self) -> None:
        self.assertTrue(ask_to_go_back(self.dir))
        with self.assertLogs(update_guard.log, "WARNING"):
            self.assertTrue(self.start())

    def test_data_the_old_version_can_read_stays_as_it_is(self) -> None:
        self.update_to()
        make_database(self.database, schema=5, rows=7)  # used since; the layout didn't change
        self.go_back()
        self.assertEqual(row_count(self.database), 7)
        self.assertFalse(self.database.with_name("airgrabber.db" + ASIDE_SUFFIX).exists())

    def test_a_newer_layout_goes_back_and_the_newer_file_stays_beside_it(self) -> None:
        self.update_to()
        make_database(self.database, schema=6, rows=7)
        self.go_back()
        self.assertEqual(row_count(self.database), 2)
        connection = sqlite3.connect(self.database)
        try:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 5)
        finally:
            connection.close()
        self.assertEqual(row_count(self.database.with_name("airgrabber.db" + ASIDE_SUFFIX)), 7)

    def test_a_database_the_old_version_never_had_is_set_aside(self) -> None:
        forge = self.root / "data" / "forge" / "forge.db"
        self.databases = (self.database, forge)
        self.update_to()
        make_database(forge, schema=9)
        self.go_back()
        self.assertFalse(forge.exists())  # the old version makes its own
        self.assertTrue(forge.with_name("forge.db" + ASIDE_SUFFIX).exists())

    def test_a_database_the_new_version_lost_comes_back(self) -> None:
        self.update_to()
        self.database.unlink()
        self.go_back()
        self.assertEqual(row_count(self.database), 2)
