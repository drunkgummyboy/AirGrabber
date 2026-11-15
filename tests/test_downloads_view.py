"""The Downloads page: its texts (state, progress, where, when, the almost-full
banner, the badge) and its widgets on a hidden Tk root (the band's ⬇ badge: test_spotlight)."""

from __future__ import annotations

import asyncio
import gc
import tkinter as tk
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any

import customtkinter as ctk

from app.models import (
    CalendarEntry,
    DownloadEntry,
    DownloadRecord,
    DownloadsOverview,
    DownloadState,
    Episode,
    FreeSpace,
    GivenUp,
    GrabOutcome,
    MediaType,
    Movie,
    RemoteTask,
    Show,
)
from app.models.downloads import MovedTo
from app.ui.views.downloads_view import (
    CHECKING_TEXT,
    EMPTY_TITLE,
    FIRST_ROWS,
    GIVEN_UP_TITLE,
    PROGRESS_HINT,
    DownloadsView,
    PageActions,
    badge_text,
    destination_text,
    imdb_find_url,
    meta_text,
    moved_text,
    percent,
    row_look,
    size_text,
    space_status_text,
    speed_text,
    space_text,
    state_text,
    time_left_text,
    transfer_text,
    when_text,
)

GB = 1024**3
NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
FALLOUT = Show(id=101, name="Fallout")


def entry(
    record_id: int,
    state: DownloadState = DownloadState.DOWNLOADING,
    progress: float | None = 0.42,
    *,
    media_type: MediaType = MediaType.TV,
    moved_to: MovedTo | None = None,
) -> DownloadEntry:
    tv = media_type is MediaType.TV
    return DownloadEntry(
        record=DownloadRecord(
            id=record_id,
            media_type=media_type,
            media_id=FALLOUT.id if tv else 558449,
            season=2 if tv else None,
            episode=record_id if tv else None,
            release_title=f"Release.{record_id}",
            downloaded_at=NOW - timedelta(hours=record_id),
            saved_path="Download Station",
        ),
        title=f"Fallout S02E0{record_id}" if tv else "Gladiator II (2024)",
        show=FALLOUT if tv else None,
        outcome=GrabOutcome.SENT,
        program="Download Station",
        state=state,
        progress=progress,
        moved_to=moved_to,
    )


GOLD_RUSH = MovedTo(
    folder="D:/Library/Series/Gold Rush/Season 17", shown=("Series", "Gold Rush", "Season 17")
)


class TextTests(unittest.TestCase):
    def test_state_texts(self) -> None:
        program = "Download Station"
        self.assertEqual(
            state_text(DownloadState.DOWNLOADING, 0.4216, program), "Downloading 42 %"
        )
        self.assertEqual(state_text(DownloadState.WAITING, None, program), "Waiting")
        # 3.0.2, as the mockup has them.
        self.assertEqual(state_text(DownloadState.STOPPED, 0.634, program), "Paused at 63 %")
        self.assertEqual(state_text(DownloadState.READY, 1.0, program), "✓ Finished")
        self.assertEqual(
            state_text(DownloadState.PENDING, None, program),
            "Waiting for Download Station to pick it up…",
        )
        self.assertEqual(
            state_text(DownloadState.NOT_FOUND, None, program),
            "Download Station never picked this up",
        )
        self.assertEqual(
            state_text(DownloadState.PROBLEM, 0.3, "qBittorrent"), "Problem in qBittorrent"
        )
        self.assertEqual(state_text(DownloadState.UNKNOWN, None, ""), "Status unknown")

    def test_percent_rounds_down(self) -> None:
        self.assertEqual(percent(0.999), 99)
        self.assertEqual(percent(1.0), 100)
        self.assertEqual(percent(0.0), 0)
        self.assertEqual(percent(None), 0)

    def test_where_it_went(self) -> None:
        self.assertEqual(
            destination_text(GrabOutcome.SENT, "Download Station"), "Sent to Download Station"
        )
        self.assertEqual(destination_text(GrabOutcome.SAVED, ""), "Saved to folder")
        self.assertEqual(
            destination_text(GrabOutcome.OPENED, "qBittorrent"), "Opened in qBittorrent"
        )
        self.assertEqual(destination_text(None, ""), "")

    def test_when(self) -> None:
        now = datetime(2026, 10, 20, 18, 0).astimezone()
        self.assertEqual(when_text(now.replace(hour=14, minute=32), now), "Today 14:32")
        self.assertEqual(when_text(now - timedelta(days=1), now), "Yesterday 18:00")
        self.assertEqual(when_text(now - timedelta(days=8), now), "12 Oct 18:00")
        self.assertEqual(when_text(now - timedelta(days=365), now), "20 Oct 2025")

    def test_meta_line(self) -> None:
        text = meta_text(entry(1), NOW)
        self.assertTrue(text.endswith(" · Sent to Download Station"), text)

    def test_where_mediaforge_moved_it(self) -> None:
        self.assertEqual(moved_text(GOLD_RUSH), "Renamed → Series › Gold Rush › Season 17")
        self.assertEqual(moved_text(None), "")
        self.assertEqual(moved_text(MovedTo(folder="D:/x", shown=())), "")
        text = meta_text(entry(1, DownloadState.READY, 1.0, moved_to=GOLD_RUSH), NOW)
        self.assertTrue(
            text.endswith(" · Sent to Download Station · Renamed → Series › Gold Rush › Season 17"),
            text,
        )
        self.assertNotIn("D:", text)  # never the path before the library folder

    def test_almost_full(self) -> None:
        nas = FreeSpace(free_bytes=12 * GB, total_bytes=4000 * GB, on_nas=True, low=True)
        self.assertEqual(space_text(nas), "Your NAS is almost full: 12 GB free")
        self.assertEqual(space_status_text(nas), "⚠ Your NAS is almost full: 12 GB free")
        local = FreeSpace(free_bytes=int(4.5 * GB), on_nas=False, low=True)
        self.assertEqual(space_text(local), "Your download drive is almost full: 4.5 GB free")
        self.assertEqual(size_text(1200 * GB), "1.2 TB")

    def test_speed_and_time_left_texts(self) -> None:
        mb = 1024**2
        self.assertEqual(
            transfer_text(int(4.2 * mb), 12 * 60), "4.2 MB/s · 12 min left"
        )
        self.assertEqual(transfer_text(850 * 1024, None), "850 KB/s")
        self.assertEqual(transfer_text(25 * mb, 30), "25 MB/s · under a minute left")
        self.assertEqual(transfer_text(None, 90), "2 min left")
        self.assertEqual(transfer_text(0, None), "")  # no speed to tell
        self.assertEqual(time_left_text(59), "under a minute left")
        self.assertEqual(time_left_text(2 * 3600 + 5 * 60), "2 h 5 min left")
        self.assertEqual(time_left_text(3 * 3600), "3 h left")
        self.assertEqual(time_left_text(86400 + 7200), "1 day left")
        self.assertEqual(time_left_text(3 * 86400), "3 days left")

    def test_the_edges_of_speed_and_time_left(self) -> None:
        # "1 h" only from the hour on; never "24 h".
        self.assertEqual(time_left_text(3599), "59 min left")
        self.assertEqual(time_left_text(3600), "1 h left")
        self.assertEqual(time_left_text(3629), "1 h left")
        self.assertEqual(time_left_text(86399), "23 h 59 min left")
        self.assertEqual(time_left_text(86400), "1 day left")
        # By what it would show: no "1024 KB/s", no "10.0 MB/s".
        self.assertEqual(speed_text(round(1023.4 * 1024)), "1023 KB/s")
        self.assertEqual(speed_text(round(1023.6 * 1024)), "1.0 MB/s")
        self.assertEqual(speed_text(round(9.96 * 1024**2)), "10 MB/s")
        self.assertEqual(speed_text(round(9.94 * 1024**2)), "9.9 MB/s")

    def test_a_downloading_row_shows_speed_and_time_left(self) -> None:
        task = RemoteTask(
            name="Fallout.S02E01", state=DownloadState.DOWNLOADING, progress=0.42,
            speed_bps=int(4.2 * 1024**2), eta_s=12 * 60,
        )
        overview = DownloadsOverview(program="Download Station", synced=True)
        downloading = entry(1).model_copy(update={"task": task})
        self.assertEqual(row_look(downloading, overview, NOW).transfer, "4.2 MB/s · 12 min left")
        # Paused or done: neither (whatever the program still says).
        paused = entry(1, DownloadState.STOPPED).model_copy(update={"task": task})
        done = entry(1, DownloadState.READY, 1.0).model_copy(update={"task": task})
        self.assertEqual(row_look(paused, overview, NOW).transfer, "")
        self.assertEqual(row_look(done, overview, NOW).transfer, "")
        self.assertEqual(row_look(entry(1), overview, NOW).transfer, "")  # no task: not known

    def test_badge(self) -> None:
        self.assertEqual(badge_text(0), "")
        self.assertEqual(badge_text(3), "3")
        self.assertEqual(badge_text(120), "99+")

    def test_same_rows_and_imdb_search(self) -> None:
        self.assertEqual(
            imdb_find_url("Gladiator II (2024)"),
            "https://www.imdb.com/find/?q=Gladiator%20II%20%282024%29",
        )


class FakeStatus:
    """DownloadStatusService's surface the widgets use."""

    def __init__(self) -> None:
        self.latest: DownloadsOverview | None = None
        self.listeners: list[Any] = []
        self.space_listeners: list[Any] = []
        self.visible: list[bool] = []
        self.next = DownloadsOverview()
        self.first = DownloadsOverview(checking=True)  # what `from_history` answers
        self.check_gate: asyncio.Event | None = None  # holds `check` back (a slow program)

    def subscribe(self, listener: Any) -> Any:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def subscribe_space(self, listener: Any) -> Any:
        self.space_listeners.append(listener)
        return lambda: self.space_listeners.remove(listener)

    def set_visible(self, visible: bool) -> None:
        self.visible.append(visible)

    async def from_history(self) -> DownloadsOverview:
        return self.first

    async def check(self) -> DownloadsOverview:
        if self.check_gate is not None:
            await self.check_gate.wait()
        self.publish(self.next)
        return self.next

    def publish(self, overview: DownloadsOverview) -> None:
        self.latest = overview
        for listener in list(self.listeners):
            listener(overview)


class FakeGivenUp:
    """GivenUpService's surface the page uses: `items` listed, `try_again` heard."""

    def __init__(self, items: tuple[GivenUp, ...] = ()) -> None:
        self.items = items
        self.tried: list[GivenUp] = []

    async def listing(self) -> tuple[GivenUp, ...]:
        return self.items

    async def try_again(self, item: GivenUp) -> bool:
        self.tried.append(item)
        self.items = tuple(other for other in self.items if other is not item)
        return True


LOST_S01E05 = GivenUp(
    label="Lost S01E05",
    tried_at=NOW - timedelta(days=1),
    episode=CalendarEntry(
        show=Show(id=7, name="Lost"),
        episode=Episode(id=75, show_id=7, season=1, number=5),
        is_tracked=True,
    ),
)
DUNE_GIVEN_UP = GivenUp(
    label="Dune (2021)", tried_at=NOW - timedelta(days=2), movie=Movie(id=438631, title="Dune")
)


class FakeImages:
    async def get(self, url: str, size: tuple[int, int]) -> None:
        return None


class FakeSettings:
    class current:  # noqa: N801 - mimics SettingsService.current
        subtitle_languages = ("en", "nl")


class WidgetTests(unittest.IsolatedAsyncioTestCase):
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
        asyncio.get_running_loop().slow_callback_duration = 2.0  # building widgets is slow
        self.status = FakeStatus()
        self.opened: list[Show] = []

    def view(
        self, given_up: FakeGivenUp | None = None, actions: PageActions | None = None
    ) -> DownloadsView:
        page = DownloadsView(
            self.root,
            downloads=self.status,  # type: ignore[arg-type]
            settings=FakeSettings(),  # type: ignore[arg-type]
            images=FakeImages(),  # type: ignore[arg-type]
            discovery=None,  # type: ignore[arg-type]
            on_open_show=self.opened.append,
            given_up=given_up,  # type: ignore[arg-type]
            actions=actions,
        )
        page.pack(fill="both", expand=True)
        return page

    @staticmethod
    def texts(widget: Any) -> list[str]:
        """The texts of the labels laid out (with every container above them laid out)."""
        found: list[str] = []
        for child in widget.winfo_children():
            if not child.winfo_manager():
                continue
            if isinstance(child, ctk.CTkLabel):
                found.append(str(child.cget("text")))
            found += WidgetTests.texts(child)
        return found

    async def test_rows_update_in_place_and_are_kept_by_grab(self) -> None:
        page = self.view()
        self.status.next = DownloadsOverview(
            entries=(entry(1), entry(2, DownloadState.READY, 1.0)),
            program="Download Station",
            checked_at=NOW,
        )
        await page.load()
        self.root.update()
        rows = list(page._rows)
        self.assertEqual(len(rows), 2)
        texts = self.texts(page)
        self.assertIn("Fallout S02E01", texts)
        self.assertIn("Downloading 42 %", texts)
        self.assertIn("✓ Finished", texts)
        self.assertFalse(any("Subtitles" in text for text in texts))
        assert rows[0]._bar is not None
        self.assertEqual(rows[0]._bar.winfo_manager(), "pack")
        self.assertIsNone(rows[1]._bar)  # made only when needed

        # Same grabs: the rows stay, their states change.
        self.status.publish(
            DownloadsOverview(
                entries=(entry(1, DownloadState.READY, 1.0), entry(2, DownloadState.READY, 1.0)),
                program="Download Station",
                checked_at=NOW,
            )
        )
        self.root.update()
        self.assertEqual(page._rows, rows)
        self.assertEqual(rows[0]._bar.winfo_manager(), "")

        # A new grab: one new row on top, the others kept (3.0.2); a row opens its show.
        self.status.publish(
            DownloadsOverview(entries=(entry(0), entry(1), entry(2)), program="Download Station")
        )
        self.root.update()
        self.assertEqual(len(page._rows), 3)
        self.assertEqual(page._rows[1:], rows)
        self.assertEqual(int(rows[0].grid_info()["row"]), 2)  # moved down one
        page._rows[0]._clicked()
        self.assertEqual(self.opened, [FALLOUT])

    async def test_the_speed_shows_under_the_progress_bar(self) -> None:
        page = self.view()
        task = RemoteTask(
            name="Fallout.S02E01", state=DownloadState.DOWNLOADING, progress=0.42,
            speed_bps=int(4.2 * 1024**2), eta_s=12 * 60,
        )
        self.status.next = DownloadsOverview(
            entries=(entry(1).model_copy(update={"task": task}),),
            program="Download Station",
            synced=True,
            checked_at=NOW,
        )
        await page.load()
        self.root.update()
        self.assertIn("4.2 MB/s · 12 min left", self.texts(page))
        self.status.publish(
            DownloadsOverview(
                entries=(entry(1, DownloadState.READY, 1.0).model_copy(update={"task": task}),),
                program="Download Station",
                synced=True,
                checked_at=NOW,
            )
        )
        self.root.update()
        self.assertNotIn("4.2 MB/s · 12 min left", self.texts(page))

    async def test_a_moved_grab_says_where_it_ended_up(self) -> None:
        page = self.view()
        self.status.next = DownloadsOverview(
            entries=(entry(1, DownloadState.READY, 1.0, moved_to=GOLD_RUSH), entry(2)),
            program="Download Station",
            checked_at=NOW,
        )
        await page.load()
        self.root.update()
        texts = self.texts(page)
        moved = [text for text in texts if "Renamed → " in text]
        self.assertEqual(len(moved), 1, texts)
        self.assertTrue(moved[0].endswith("Renamed → Series › Gold Rush › Season 17"), moved)

    async def test_empty_hint_problem_and_banner(self) -> None:
        page = self.view()
        await page.load()
        self.root.update()
        self.assertIn(EMPTY_TITLE, self.texts(page))

        self.status.publish(DownloadsOverview(entries=(entry(1, DownloadState.UNKNOWN, None),)))
        self.root.update()
        texts = self.texts(page)
        self.assertIn(PROGRESS_HINT, texts)
        self.assertIn("Status unknown", texts)
        self.assertNotIn(EMPTY_TITLE, texts)

        space = FreeSpace(free_bytes=12 * GB, total_bytes=4000 * GB, low=True)
        self.status.publish(
            DownloadsOverview(
                entries=(entry(1, DownloadState.UNKNOWN, None),),
                program="Download Station",
                problem="Can't reach the NAS at 192.168.1.20:5000.",
                space=space,
            )
        )
        self.root.update()
        texts = self.texts(page)
        self.assertIn("⚠ Can't reach the NAS at 192.168.1.20:5000.", texts)
        self.assertIn("⚠ Your NAS is almost full: 12 GB free", texts)
        self.assertNotIn(PROGRESS_HINT, texts)

    async def test_the_page_lists_history_before_the_program_answers(self) -> None:
        page = self.view()
        self.status.first = DownloadsOverview(
            entries=(
                entry(1, DownloadState.UNKNOWN, None),
                entry(2, DownloadState.UNKNOWN, None),
            ),
            program="Download Station",
            checking=True,
        )
        self.status.next = DownloadsOverview(
            entries=(entry(1), entry(2, DownloadState.READY, 1.0)),
            program="Download Station",
            synced=True,
            checked_at=NOW,
        )
        self.status.check_gate = asyncio.Event()
        load = asyncio.create_task(page.load())
        for _ in range(5):
            await asyncio.sleep(0)
        self.root.update()
        self.assertFalse(load.done())  # the program hasn't answered yet
        texts = self.texts(page)
        self.assertIn("Fallout S02E01", texts)
        self.assertIn("Fallout S02E02", texts)
        self.assertIn(CHECKING_TEXT, texts)
        self.assertNotIn("Status unknown", texts)  # no verdict before the program's
        self.assertEqual(str(page.clear_button.cget("state")), "disabled")
        rows = list(page._rows)
        self.status.check_gate.set()
        await load
        self.root.update()
        texts = self.texts(page)
        self.assertNotIn(CHECKING_TEXT, texts)
        self.assertIn("Downloading 42 %", texts)
        self.assertIn("✓ Finished", texts)
        self.assertEqual(page._rows, rows)  # the same rows, filled in

    async def test_while_checking_without_grabs_the_page_waits(self) -> None:
        page = self.view()
        self.status.check_gate = asyncio.Event()
        load = asyncio.create_task(page.load())
        for _ in range(5):
            await asyncio.sleep(0)
        self.root.update()
        self.assertNotIn(EMPTY_TITLE, self.texts(page))  # the program may still list some
        self.assertIsNotNone(page._loading)
        self.status.check_gate.set()
        await load
        self.root.update()
        self.assertIn(EMPTY_TITLE, self.texts(page))

    @staticmethod
    async def section_read(page: DownloadsView) -> None:
        """Until the section's own read (a task of its own) is done."""
        task = page._read_task
        if task is not None:
            await task

    async def test_the_couldnt_find_section_lists_what_was_given_up(self) -> None:
        given_up = FakeGivenUp((LOST_S01E05, DUNE_GIVEN_UP))
        page = self.view(given_up)
        self.status.next = DownloadsOverview(
            entries=(entry(1),), program="Download Station", checked_at=NOW
        )
        await page.load()
        await self.section_read(page)
        self.root.update()
        texts = self.texts(page)
        self.assertIn(GIVEN_UP_TITLE, texts)
        self.assertIn("Lost S01E05", texts)
        self.assertIn("Dune (2021)", texts)
        group = page.given_up_group
        assert group is not None
        self.assertEqual(group.labels, ("Lost S01E05", "Dune (2021)"))
        self.assertEqual(
            [str(button.cget("text")) for button in group.buttons[0]], ["Try again", "Choose…"]
        )
        # Below the grabs.
        self.assertGreater(int(group.grid_info()["row"]), int(page._rows[0].grid_info()["row"]))
        # Nothing given up: no section.
        given_up.items = ()
        await page.load()
        await self.section_read(page)
        self.root.update()
        self.assertNotIn(GIVEN_UP_TITLE, self.texts(page))

    async def test_try_again_and_choose_in_the_section(self) -> None:
        given_up = FakeGivenUp((LOST_S01E05, DUNE_GIVEN_UP))
        chosen: list[GivenUp] = []
        actions = PageActions(again=lambda _entry, _auto: None, choose=chosen.append)
        page = self.view(given_up, actions)
        await page.load()
        await self.section_read(page)
        self.root.update()
        self.assertIn(GIVEN_UP_TITLE, self.texts(page))  # also without any grab
        group = page.given_up_group
        assert group is not None
        group.buttons[1][1].invoke()  # Dune's Choose…
        self.assertEqual(chosen, [DUNE_GIVEN_UP])
        group.buttons[0][0].invoke()  # Lost's Try again
        for _ in range(10):
            await asyncio.sleep(0)
        self.root.update()
        self.assertEqual(given_up.tried, [LOST_S01E05])
        self.assertEqual(group.labels, ("Dune (2021)",))  # read again: it left the list

    async def test_a_give_up_after_the_first_visit_shows_on_the_next(self) -> None:
        given_up = FakeGivenUp()
        page = self.view(given_up)
        page.refresh_if_stale()  # the first visit: the page loads
        assert page._load_task is not None
        await page._load_task
        await self.section_read(page)
        self.root.update()
        self.assertNotIn(GIVEN_UP_TITLE, self.texts(page))
        given_up.items = (LOST_S01E05,)  # given up meanwhile
        self.assertFalse(page.is_stale())
        page.refresh_if_stale()  # the next visit: not stale, the section read again
        await self.section_read(page)
        self.root.update()
        self.assertIn("Lost S01E05", self.texts(page))

    async def test_show_scrolls_to_the_section_below_many_grabs(self) -> None:
        given_up = FakeGivenUp((LOST_S01E05,))
        page = self.view(given_up)
        self.root.geometry("900x400")
        self.root.deiconify()
        many = tuple(entry(index) for index in range(1, FIRST_ROWS + 15))
        self.status.next = DownloadsOverview(
            entries=many, program="Download Station", checked_at=NOW
        )
        # A first visit: Show before the program answered and the rows are made.
        page.refresh_if_stale()
        page.show_given_up()
        task = page._scroll_task
        assert task is not None
        await asyncio.wait_for(task, timeout=15)
        self.root.update()
        self.assertFalse(page.building)
        group = page.given_up_group
        assert group is not None
        self.assertEqual(len(page._rows), len(many))
        top, bottom = page._body._parent_canvas.yview()
        place = group.winfo_y() / page._body.winfo_height()
        self.assertGreater(top, 0.5)  # scrolled well down the long list
        self.assertTrue(top - 0.01 <= place <= bottom, (top, place, bottom))

    async def test_visibility_follows_the_page(self) -> None:
        page = self.view()
        self.root.deiconify()
        self.root.update()
        page.pack_forget()
        self.root.update()
        page.destroy()
        self.assertIn(True, self.status.visible)
        self.assertEqual(self.status.visible[-1], False)
        self.assertEqual(self.status.listeners, [])


if __name__ == "__main__":
    unittest.main()
