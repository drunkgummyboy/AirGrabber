"""Ignored files and folders (app/forge/core/ignored.py, app/forge/db/ignored.py, forge.db v4):
the repository and the migration from v3, the snapshot's matching, "the whole folder" right
under the watched folder, the service (the Activity line names the file only, the log no path),
watching that skips what is ignored, and a file added by hand that is never refused."""

from __future__ import annotations

import dataclasses
import logging
import sqlite3
from pathlib import Path

from app.forge.core.activity import ActivityFeed
from app.forge.core.ignored import (
    IGNORED_LINE,
    IgnoreList,
    IgnoreService,
    gone_entries,
    top_folder,
)
from app.forge.core.queue import ItemStatus
from app.forge.core.watch import candidates
from app.forge.db.database import MIGRATIONS, SCHEMA_VERSION, Database
from app.forge.db.ignored import IgnoredRepo, ignore_key
from app.forge.models.ignored import IgnoredPath
from app.i18n import Language, set_language, t
from tests.forge.base import ForgeAsyncTestCase, ForgeTestCase
from tests.forge.core.test_auto_renamer import Clock, RenamerTestCase
from tests.forge.db.base import ForgeDbTestCase

DL = Path(r"D:\Downloads")


def entry(path: Path, folder: bool = False) -> IgnoredPath:
    return IgnoredPath.model_validate({"path": path, "folder": folder, "added_at": 0})


class RepoTest(ForgeDbTestCase):
    async def test_add_list_remove(self) -> None:
        repo = IgnoredRepo(self.db)
        assert await repo.list() == []
        course = await repo.add(DL / "Python Course", folder=True, when=100)
        clip = await repo.add(DL / "holiday.mp4", folder=False, when=200)
        assert (course.folder, clip.folder) == (True, False)
        listed = await repo.list()
        assert [(e.path, e.folder) for e in listed] == [
            (DL / "Python Course", True),
            (DL / "holiday.mp4", False),
        ]
        # The same path written otherwise is one entry (Windows compares without case).
        await repo.add(Path(r"d:\downloads\HOLIDAY.MP4"), folder=False, when=300)
        assert len(await repo.list()) == 2
        assert await repo.remove(Path(r"D:\DOWNLOADS\holiday.mp4"))
        assert not await repo.remove(DL / "holiday.mp4")
        assert [e.path for e in await repo.list()] == [DL / "Python Course"]

    def test_ignore_key(self) -> None:
        assert ignore_key(r"D:\Downloads\Course\\") == ignore_key("d:/downloads/course")
        assert ignore_key("D:\\") == "d:\\"


class MigrationTest(ForgeAsyncTestCase):
    async def test_a_v3_forge_db_upgrades_and_keeps_its_rows(self) -> None:
        path = self.tmp / "forge.db"
        conn = sqlite3.connect(path)
        for version, script in enumerate(MIGRATIONS[:3], start=1):
            conn.executescript(f"{script}\nPRAGMA user_version = {version};")
        conn.execute(
            "INSERT INTO moves (op_id, run_id, src, src_key, dst, moved_at, kind, tmdb_id) "
            "VALUES (1, 1, 'D:\\a.mkv', 'd:\\a.mkv', 'E:\\a.mkv', 1.0, 'movie', 7)"
        )
        conn.commit()
        conn.close()
        db = await Database.open(path)
        try:
            row = await db.fetch_one("PRAGMA user_version")
            assert row is not None and row[0] == SCHEMA_VERSION == 4
            assert await IgnoredRepo(db).list() == []
            count = await db.fetch_one("SELECT COUNT(*) FROM moves")
            assert count is not None and count[0] == 1
            await IgnoredRepo(db).add(DL / "x.mp4", folder=False)
            assert len(await IgnoredRepo(db).list()) == 1
        finally:
            await db.close()


class MatchingTest(ForgeTestCase):
    def test_files_and_folders(self) -> None:
        ignored = IgnoreList([entry(DL / "clip.mp4"), entry(DL / "Course", folder=True)])
        assert ignored
        assert ignored.ignored(DL / "clip.mp4")
        assert ignored.ignored(Path(r"d:\downloads\CLIP.mp4"))
        assert not ignored.ignored(DL / "clip2.mp4")
        assert ignored.ignored(DL / "Course" / "01 Intro.mp4")
        assert ignored.ignored(DL / "Course" / "Part 2" / "05.mp4")
        assert not ignored.ignored(DL / "Course 2" / "01.mp4")  # a name that starts the same
        assert ignored.folder_ignored(DL / "course")
        assert ignored.folder_ignored(DL / "Course" / "Part 2")
        assert not ignored.folder_ignored(DL)
        assert not IgnoreList()

    def test_the_whole_folder_is_the_one_right_under_the_watched_folder(self) -> None:
        assert top_folder(DL / "Course" / "Part 2" / "05.mp4", DL) == DL / "Course"
        assert top_folder(DL / "Season.Pack" / "e01.mkv", DL) == DL / "Season.Pack"
        assert top_folder(Path(r"d:\downloads\Course\01.mp4"), DL) == DL / "Course"
        assert top_folder(DL / "clip.mp4", DL) is None  # directly in the watched folder
        assert top_folder(Path(r"E:\Other\Course\01.mp4"), DL) is None  # not under it


class ServiceTest(ForgeDbTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(set_language, Language.EN)
        set_language(Language.EN)

    async def test_ignore_says_so_without_the_path_and_stop_ignoring(self) -> None:
        feed = ActivityFeed()
        service = IgnoreService(IgnoredRepo(self.db), activity=feed)
        heard: list[tuple[IgnoredPath, ...]] = []
        service.subscribe(heard.append)
        folder = Path(r"C:\Users\Alex\Downloads\Python Course")
        with self.assertLogs("app.forge.core.ignored", logging.INFO) as logged:
            await service.ignore(folder, folder=True)
        assert "Alex" not in "\n".join(logged.output)  # the last name only
        assert [line.text for line in feed.lines()] == [
            "Ignored: Python Course. MediaForge won't pick it up again."
        ]
        assert t(IGNORED_LINE, name="x") == "Ignored: x. MediaForge won't pick it up again."
        assert [e.path for e in service.entries] == [folder]
        assert service.snapshot().ignored(folder / "01.mp4")
        assert heard[-1] == service.entries
        # A fresh service reads it back.
        again = IgnoreService(IgnoredRepo(self.db))
        await again.load()
        assert [e.path for e in again.entries] == [folder]
        assert await service.stop_ignoring(folder)
        assert service.entries == () and not service.snapshot()
        assert heard[-1] == ()

    async def test_dutch_line(self) -> None:
        set_language(Language.NL)
        feed = ActivityFeed()
        service = IgnoreService(IgnoredRepo(self.db), activity=feed)
        await service.ignore(DL / "clip.mp4", folder=False)
        assert feed.lines()[-1].text == "Genegeerd: clip.mp4. MediaForge pakt het niet meer op."


class GoneTest(ForgeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.watched = self.tmp / "watched"
        self.watched.mkdir()

    def test_only_what_is_gone_from_a_folder_that_can_be_reached(self) -> None:
        here = self.watched / "here.mkv"
        here.touch()
        gone, gone_folder = self.watched / "gone.mkv", self.watched / "Gone Course"
        entries = [entry(here), entry(gone), entry(gone_folder, folder=True)]
        assert gone_entries(entries, [self.watched]) == entries[1:]

    def test_a_folder_that_cant_be_reached_says_nothing(self) -> None:
        offline = self.tmp / "nas-off"  # a NAS that's off: everything on it looks gone
        assert gone_entries([entry(offline / "clip.mkv")], [offline]) == []

    def test_what_isnt_under_a_watched_folder_any_more_goes(self) -> None:
        folder = self.tmp / "stopped watching"
        folder.mkdir()
        (folder / "clip.mkv").touch()
        elsewhere = entry(folder / "clip.mkv")  # still on the disk
        assert gone_entries([elsewhere], [self.watched]) == [elsewhere]

    def test_nothing_watched_at_all_says_nothing(self) -> None:
        assert gone_entries([entry(self.tmp / "clip.mkv")], []) == []


class CandidatesTest(ForgeTestCase):
    def test_ignored_files_and_folders_are_no_candidates(self) -> None:
        root = self.tmp / "dl"
        for name in ("Dune.2021.mkv", "clip.mp4", "Course/01.mp4", "Course/Part 2/02.mp4",
                     "Heat.1995/Heat.1995.mkv"):  # fmt: skip
            (root / name).parent.mkdir(parents=True, exist_ok=True)
            (root / name).write_bytes(b"video")
        everything = {p.name for p in candidates(root)}
        assert everything == {"Dune.2021.mkv", "clip.mp4", "01.mp4", "02.mp4", "Heat.1995.mkv"}
        ignored = IgnoreList([entry(root / "clip.mp4"), entry(root / "course", folder=True)])
        assert {p.name for p in candidates(root, ignored)} == {"Dune.2021.mkv", "Heat.1995.mkv"}


class WatchingTest(RenamerTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.ignored = IgnoreService(IgnoredRepo(self.db))
        self.parts = dataclasses.replace(self.parts, ignored=self.ignored)

    async def test_an_ignored_download_is_left_alone(self) -> None:
        await self.use()
        clip = self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        await self.ignored.ignore(clip.parent, folder=True)  # its whole release folder
        self.folders.download("Encanto.2021.1080p.mkv")
        clock = Clock()
        await self.two_looks(self.renamer(clock), clock)
        assert clip.exists()  # not touched
        [item] = self.queue.items  # Encanto only, renamed
        assert item.status is ItemStatus.DONE
        assert (self.folders.movies / "Encanto (2021)" / "Encanto (2021).mkv").exists()

    async def test_what_has_left_the_watched_folder_comes_off_the_list(self) -> None:
        await self.use()
        clip = self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        folder = self.folders.download("Encanto.2021.1080p.mkv").parent
        await self.ignored.ignore(clip, folder=False)
        await self.ignored.ignore(folder, folder=True)
        seen: list[int] = []
        self.ignored.subscribe(lambda entries: seen.append(len(entries)))
        clip.unlink()
        clock = Clock()
        await self.renamer(clock).look()
        assert [e.path for e in self.ignored.entries] == [folder]  # the folder is still there
        assert seen == [1]  # the Settings box follows
        assert not await IgnoredRepo(self.db).remove(clip)  # stored too

    async def test_a_file_added_by_hand_is_never_refused(self) -> None:
        clip = self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        await self.ignored.ignore(clip, folder=False)
        assert await self.queue.add_paths([clip], recursive=False) == 1
        assert [item.path for item in self.queue.items] == [clip]
