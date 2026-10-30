"""The Downloads page in 2.0.7: the filter, "Clear finished" / "Remove from list" (a cleared mark
in the database, migration v7; never the history or the files), and a row's ⋯ menu ("Open the
folder" once the video is found, Download again…, Get subtitles, Remove from list)."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import os
import sqlite3
import tempfile
import time
import tkinter as tk
import unittest
from datetime import timedelta
from pathlib import Path
from typing import Any

import customtkinter as ctk

from app.database import CLEARED_MIGRATION, MIGRATIONS, Database
from app.models import DownloadEntry, DownloadsOverview, DownloadState, MediaType, Show, SubtitleOffer
from app.services.download_status import Located, video_folder
from app.ui.views.downloads_view import (
    EntryFilter,
    PageActions,
    entry_group,
    entry_key,
    filter_entries,
    filter_from_name,
    finished_ids,
    folder_tip,
    row_menu,
)
from app.ui.subtitle_control import SubtitleActions, SubtitleRuns
from tests.test_download_status import FALLOUT, HASH_A, NOW, ServiceTestCase, record
from tests.test_downloads_view import FakeImages, FakeSettings, FakeStatus


def entry(history_id: int, state: DownloadState = DownloadState.UNKNOWN, **fields: Any) -> DownloadEntry:
    return DownloadEntry(
        record=record(f"Fallout.S02E{history_id:02d}", episode=history_id, record_id=history_id),
        title=f"Fallout S02E{history_id:02d}",
        show=FALLOUT,
        state=state,
        **fields,
    )


ENTRIES = (
    entry(1, DownloadState.DOWNLOADING, progress=0.4),
    entry(2, DownloadState.WAITING),
    entry(3, DownloadState.STOPPED),
    entry(4, DownloadState.READY),
    entry(5, DownloadState.PROBLEM),
    entry(6, DownloadState.NOT_FOUND),
    entry(7, DownloadState.UNKNOWN),
    entry(8, DownloadState.READY),
)


class FilterTests(unittest.TestCase):
    def test_groups(self) -> None:
        self.assertIs(entry_group(DownloadState.DOWNLOADING), EntryFilter.DOWNLOADING)
        self.assertIs(entry_group(DownloadState.WAITING), EntryFilter.DOWNLOADING)
        self.assertIs(entry_group(DownloadState.STOPPED), EntryFilter.DOWNLOADING)
        self.assertIs(entry_group(DownloadState.READY), EntryFilter.FINISHED)
        self.assertIs(entry_group(DownloadState.PROBLEM), EntryFilter.PROBLEMS)
        self.assertIs(entry_group(DownloadState.NOT_FOUND), EntryFilter.PROBLEMS)
        self.assertIsNone(entry_group(DownloadState.UNKNOWN))

    def test_filter_keeps_the_order(self) -> None:
        def ids(chosen: EntryFilter) -> list[int | None]:
            return [e.record.id for e in filter_entries(ENTRIES, chosen)]

        self.assertEqual(ids(EntryFilter.ALL), [1, 2, 3, 4, 5, 6, 7, 8])
        self.assertEqual(ids(EntryFilter.DOWNLOADING), [1, 2, 3])
        self.assertEqual(ids(EntryFilter.FINISHED), [4, 8])
        self.assertEqual(ids(EntryFilter.PROBLEMS), [5, 6])

    def test_clear_finished_takes_the_ready_ones(self) -> None:
        self.assertEqual(finished_ids(ENTRIES), [4, 8])
        self.assertEqual(finished_ids(ENTRIES[:3]), [])

    def test_names(self) -> None:
        self.assertIs(filter_from_name("Finished"), EntryFilter.FINISHED)
        self.assertIs(filter_from_name("something else"), EntryFilter.ALL)

    def test_entry_key(self) -> None:
        self.assertEqual(entry_key(ENTRIES[0]), (MediaType.TV, FALLOUT.id, 2, 1))


class MenuTests(unittest.TestCase):
    def menu(self, located: Located, *, again: bool = True, subtitles: bool = True) -> list[Any]:
        return row_menu(
            ENTRIES[3],
            located,
            open_folder=lambda folder: None,
            again=(lambda: None) if again else None,
            subtitles=(lambda: None) if subtitles else None,
            subtitles_tip="no subtitles",
            remove=lambda: None,
        )

    def test_everything_available(self) -> None:
        items = self.menu(Located("C:/finished/Fallout"))
        self.assertEqual(
            [item.label for item in items],
            ["Open the folder", "Download again…", "Get subtitles", "Remove from list"],
        )
        self.assertTrue(all(item.action is not None for item in items))

    def test_greyed_with_the_reason(self) -> None:
        for problem, tip in (
            ("no_folder", "Choose where finished downloads end up first (Settings → Subtitles)"),
            ("not_found", "Not found in the folder where finished downloads end up"),
            ("unreachable", "The folder where finished downloads end up can't be reached"),
        ):
            located = Located(None, problem)  # type: ignore[arg-type]
            folder = self.menu(located)[0]
            self.assertIsNone(folder.action)
            self.assertEqual((folder.tip, folder_tip(located)), (tip, tip))
        items = self.menu(Located(None, "not_found"), again=False, subtitles=False)
        self.assertIsNone(items[1].action)
        self.assertEqual(items[2].tip, "no subtitles")
        self.assertIsNotNone(items[3].action)  # Remove from list always works

    def test_open_folder_calls_with_the_folder(self) -> None:
        opened: list[str] = []
        items = row_menu(
            ENTRIES[3],
            Located("C:/finished/Fallout"),
            open_folder=opened.append,
            again=None,
            subtitles=None,
            subtitles_tip="",
            remove=lambda: None,
        )
        assert items[0].action is not None
        items[0].action()
        self.assertEqual(opened, ["C:/finished/Fallout"])

    def test_video_folder(self) -> None:
        self.assertEqual(video_folder([os.path.join("C:/x", "Show", "a.mkv")]), os.path.join("C:/x", "Show"))
        self.assertIsNone(video_folder([]))


class ClearedTests(ServiceTestCase):
    async def test_cleared_grabs_leave_the_page_but_stay_in_the_history(self) -> None:
        first = await self.add(record("Fallout.S02E03.1080p.WEB", info_hash=HASH_A))
        second = await self.add(record("Fallout.S02E04.1080p.WEB", episode=4))
        self.assertEqual(len((await self.service.check()).entries), 2)
        assert first.id is not None
        self.assertEqual(await self.service.clear([first.id]), 1)
        # The listeners hear the shorter list at once, and every later check keeps it so.
        self.assertEqual([e.record.id for e in self.overviews[-1].entries], [second.id])
        overview = await self.service.check()
        self.assertEqual([e.record.id for e in overview.entries], [second.id])
        # Still in the history: downloaded marks, "Download missing" and subtitles see it.
        self.assertTrue(await self.db.history.has_episode(FALLOUT.id, 2, 3))
        self.assertEqual(len(await self.db.history.recent(10)), 2)
        self.assertEqual(await self.service.clear([first.id, 999]), 0)  # again; unknown id
        self.assertEqual(await self.service.clear([]), 0)

    async def test_a_new_service_reads_the_marks_from_the_database(self) -> None:
        grab = await self.add(record("Fallout.S02E03.1080p.WEB"))
        assert grab.id is not None
        await self.db.history.clear([grab.id], NOW)
        self.assertEqual(await self.db.history.listed(10), [])
        self.assertEqual((await self.service.check()).entries, ())


class LocateTests(ServiceTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.finished = Path(tmp.name)
        self.opened: list[str] = []
        self.service._opener = self.opened.append

    def video(self, relative: str) -> Path:
        path = self.finished / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\0" * 100)
        old = time.time() - 3600
        os.utime(path, (old, old))
        return path

    def entry_for(self, title: str, episode: int = 3) -> DownloadEntry:
        return DownloadEntry(
            record=record(title, episode=episode, record_id=1), title="Fallout", show=FALLOUT
        )

    async def test_no_folder_in_settings(self) -> None:
        located = await self.service.locate(self.entry_for("Fallout.S02E03.1080p.WEB"))
        self.assertEqual(located, Located(None, "no_folder"))

    async def test_found_and_not_found(self) -> None:
        await self.settings.update(subtitles_folder=str(self.finished))
        video = self.video("Fallout.S02E03.1080p.WEB/Fallout.S02E03.1080p.WEB.mkv")
        located = await self.service.locate(self.entry_for("Fallout.S02E03.1080p.WEB"))
        self.assertEqual(located, Located(str(video.parent), ""))
        self.assertTrue(self.service.open_folder(str(video.parent)))
        self.assertEqual(self.opened, [str(video.parent)])
        missing = await self.service.locate(self.entry_for("Fallout.S02E09.1080p.WEB", 9))
        self.assertEqual(missing, Located(None, "not_found"))

    async def test_out_of_reach(self) -> None:
        await self.settings.update(subtitles_folder=str(self.finished / "offline"))

        def scanner(folder: str) -> Any:
            raise OSError(53, "The network path was not found")

        self.service._folder_scanner = scanner
        located = await self.service.locate(self.entry_for("Fallout.S02E03.1080p.WEB"))
        self.assertEqual(located, Located(None, "unreachable"))

    async def test_a_failed_open_says_so(self) -> None:
        def fail(folder: str) -> None:
            raise OSError(2, "gone")

        self.service._opener = fail
        self.assertFalse(self.service.open_folder("C:/gone"))


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    def test_v7_is_the_cleared_marks_and_self_contained(self) -> None:
        self.assertEqual(MIGRATIONS[6], CLEARED_MIGRATION)
        self.assertNotIn("ALTER TABLE", CLEARED_MIGRATION)

    async def test_a_v6_database_is_upgraded_and_keeps_its_history(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "v6.db"
        with contextlib.closing(sqlite3.connect(path)) as raw:
            script = "\n".join(MIGRATIONS[:6])
            raw.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = 6;\nCOMMIT;")
            with raw:
                raw.execute(
                    "INSERT INTO download_history (media_type, media_id, season, episode, "
                    "release_title, downloaded_at) VALUES ('tv', 1, 1, 1, 'Kept', '2026-10-20')"
                )
        db = await Database.open(path)
        self.addAsyncCleanup(db.close)
        version = await db.fetch_one("PRAGMA user_version")
        assert version is not None
        self.assertEqual(version[0], 7)
        [kept] = await db.history.listed(5)
        assert kept.id is not None
        await db.history.clear([kept.id], NOW)
        self.assertEqual(await db.history.listed(5), [])
        await db.fetch_all("SELECT 1")
        async with db.transaction() as conn:
            await conn.execute("DELETE FROM download_history")
        row = await db.fetch_one("SELECT COUNT(*) FROM cleared_downloads")
        assert row is not None
        self.assertEqual(row[0], 0)  # a mark goes with its grab


class PageWidgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0
        self.status = FakeStatus()
        self.cleared: list[list[int]] = []
        self.again: list[tuple[int | None, bool]] = []

        async def clear(ids: list[int]) -> int:
            self.cleared.append(list(ids))
            return len(ids)

        self.status.clear = clear  # type: ignore[attr-defined]
        self.status.open_folder = lambda folder: True  # type: ignore[attr-defined]
        self.runs = SubtitleRuns()
        self.offer = SubtitleOffer.ON

    def view(self) -> Any:
        from app.ui.views.downloads_view import DownloadsView

        subtitles = SubtitleActions(
            offer=lambda: self.offer,
            languages=lambda: ("en",),
            fetch=lambda key, languages: None,
            runs=self.runs,
        )
        page = DownloadsView(
            self.root,
            downloads=self.status,  # type: ignore[arg-type]
            settings=FakeSettings(),  # type: ignore[arg-type]
            images=FakeImages(),  # type: ignore[arg-type]
            discovery=None,  # type: ignore[arg-type]
            on_open_show=lambda show: None,
            actions=PageActions(
                again=lambda e, auto: self.again.append((e.record.id, auto)), subtitles=subtitles
            ),
        )
        page.pack(fill="both", expand=True)
        return page

    async def settle(self) -> None:
        for _ in range(3):
            await asyncio.sleep(0.01)
            self.root.update()

    async def test_filter_clear_and_a_subtitle_request_in_the_row(self) -> None:
        page = self.view()
        self.status.publish(DownloadsOverview(entries=ENTRIES, program="Download Station", checked_at=NOW))
        await self.settle()
        self.assertEqual(len(page._rows), 8)
        self.assertEqual(str(page.clear_button.cget("state")), "normal")
        page.set_filter(EntryFilter.FINISHED)
        self.assertEqual([row.entry.record.id for row in page._rows], [4, 8])
        page.set_filter(EntryFilter.PROBLEMS)
        self.assertEqual([row.entry.record.id for row in page._rows], [5, 6])
        page._clear_finished()
        await self.settle()
        self.assertEqual(self.cleared, [[4, 8]])
        # Nothing finished: the button rests.
        self.status.publish(DownloadsOverview(entries=ENTRIES[:3], checked_at=NOW))
        page.set_filter(EntryFilter.FINISHED)
        await self.settle()
        self.assertEqual(page._rows, [])
        self.assertIsNotNone(page._no_match)
        self.assertEqual(str(page.clear_button.cget("state")), "disabled")
        page.set_filter(EntryFilter.ALL)
        # A subtitle request shows in its row.
        row = page._rows[0]
        from app.ui.subtitle_control import WORKING

        self.runs.set(entry_key(ENTRIES[0]), WORKING)
        self.assertEqual(str(row._subtitles.cget("text")), "  ·  Looking for subtitles…")
        self.runs.set(entry_key(ENTRIES[0]), None)
        self.assertFalse(row._subtitles.winfo_manager())

    async def test_the_row_menu_and_its_actions(self) -> None:
        page = self.view()
        self.status.publish(DownloadsOverview(entries=ENTRIES[3:4], checked_at=NOW))
        await self.settle()
        row = page._rows[0]
        items = page.menu_items(row, row.entry, Located(None, "not_found"))
        self.assertEqual(
            [item.action is not None for item in items], [False, True, True, True]
        )
        items[3].action()
        await self.settle()
        self.assertEqual(self.cleared, [[4]])
        items[1].action()  # Download again… opens its two choices
        await self.settle()
        from app.ui.missing_control import PopupMenu

        menu = PopupMenu._open
        assert menu is not None
        self.assertEqual(menu.labels, ("⬇ Download again automatically", "☰ Choose a copy…"))
        menu.buttons[0].invoke()
        self.assertEqual(self.again, [(4, True)])
        items[2].action()  # Get subtitles opens the language menu
        menu = PopupMenu._open
        assert menu is not None
        self.assertEqual(menu.labels, ("Get English subtitles",))
        menu.close()

    async def test_get_subtitles_is_greyed_while_subtitles_are_off(self) -> None:
        page = self.view()
        self.status.publish(DownloadsOverview(entries=ENTRIES[3:4], checked_at=NOW))
        await self.settle()
        row = page._rows[0]
        self.offer = SubtitleOffer.OFF
        items = page.menu_items(row, row.entry, Located(None, "not_found"))
        self.assertIsNone(items[2].action)
        self.assertEqual(items[2].tip, "Turn on subtitles in Settings → Subtitles")
        self.offer = SubtitleOffer.NONE  # no folder: the reason why
        items = page.menu_items(row, row.entry, Located(None, "not_found"))
        self.assertIsNone(items[2].action)
        self.assertIn("Settings → Subtitles", items[2].tip)
        self.assertNotIn("Turn on", items[2].tip)

    async def test_an_episode_of_a_show_no_longer_known_cannot_be_downloaded_again(self) -> None:
        page = self.view()
        orphan = entry(9, DownloadState.READY).model_copy(update={"show": None})
        self.status.publish(DownloadsOverview(entries=(orphan,), checked_at=NOW))
        await self.settle()
        row = page._rows[0]
        items = page.menu_items(row, orphan, Located(None, "no_folder"))
        self.assertIsNone(items[1].action)


if __name__ == "__main__":
    unittest.main()
