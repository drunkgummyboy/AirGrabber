"""The Downloads page and the header's ⬇ button: their texts (state, progress, where, when,
subtitles, the almost-full banner, the badge) and the widgets on a hidden Tk root."""

from __future__ import annotations

import asyncio
import gc
import tkinter as tk
import unittest
from datetime import UTC, datetime, timedelta
from typing import Any

import customtkinter as ctk

from app.models import (
    DownloadEntry,
    DownloadRecord,
    DownloadsOverview,
    DownloadState,
    FreeSpace,
    GrabOutcome,
    MediaType,
    Show,
)
from app.ui.downloads_button import DownloadsButton
from app.ui.views.downloads_view import (
    EMPTY_TITLE,
    PROGRESS_HINT,
    DownloadsView,
    badge_text,
    destination_text,
    imdb_find_url,
    meta_text,
    percent,
    same_rows,
    size_text,
    space_status_text,
    space_text,
    state_text,
    subtitles_text,
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
    subtitles: tuple[str, ...] = (),
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
        subtitles=subtitles,
    )


class TextTests(unittest.TestCase):
    def test_state_texts(self) -> None:
        program = "Download Station"
        self.assertEqual(
            state_text(DownloadState.DOWNLOADING, 0.4216, program), "Downloading 42 %"
        )
        self.assertEqual(state_text(DownloadState.WAITING, None, program), "Waiting")
        self.assertEqual(state_text(DownloadState.STOPPED, 0.1, program), "Stopped")
        self.assertEqual(state_text(DownloadState.READY, 1.0, program), "Ready")
        self.assertEqual(
            state_text(DownloadState.NOT_FOUND, None, program), "Not found in Download Station"
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

    def test_subtitles_in_the_settings_order(self) -> None:
        self.assertEqual(subtitles_text(("en", "nl"), ("nl", "en")), "Subtitles NL EN")
        self.assertEqual(subtitles_text(()), "")

    def test_almost_full(self) -> None:
        nas = FreeSpace(free_bytes=12 * GB, total_bytes=4000 * GB, on_nas=True, low=True)
        self.assertEqual(space_text(nas), "Your NAS is almost full: 12 GB free")
        self.assertEqual(space_status_text(nas), "⚠ Your NAS is almost full: 12 GB free")
        local = FreeSpace(free_bytes=int(4.5 * GB), on_nas=False, low=True)
        self.assertEqual(space_text(local), "Your download drive is almost full: 4.5 GB free")
        self.assertEqual(size_text(1200 * GB), "1.2 TB")

    def test_badge(self) -> None:
        self.assertEqual(badge_text(0), "")
        self.assertEqual(badge_text(3), "3")
        self.assertEqual(badge_text(120), "99+")

    def test_same_rows_and_imdb_search(self) -> None:
        self.assertTrue(same_rows([entry(1), entry(2)], [1, 2]))
        self.assertFalse(same_rows([entry(1), entry(2)], [2, 1]))
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

    def subscribe(self, listener: Any) -> Any:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)

    def subscribe_space(self, listener: Any) -> Any:
        self.space_listeners.append(listener)
        return lambda: self.space_listeners.remove(listener)

    def set_visible(self, visible: bool) -> None:
        self.visible.append(visible)

    async def check(self) -> DownloadsOverview:
        self.publish(self.next)
        return self.next

    def publish(self, overview: DownloadsOverview) -> None:
        self.latest = overview
        for listener in list(self.listeners):
            listener(overview)


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

    def view(self) -> DownloadsView:
        page = DownloadsView(
            self.root,
            downloads=self.status,  # type: ignore[arg-type]
            settings=FakeSettings(),  # type: ignore[arg-type]
            images=FakeImages(),  # type: ignore[arg-type]
            discovery=None,  # type: ignore[arg-type]
            on_open_show=self.opened.append,
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

    async def test_rows_update_in_place_then_rebuild(self) -> None:
        page = self.view()
        self.status.next = DownloadsOverview(
            entries=(entry(1, subtitles=("nl", "en")), entry(2, DownloadState.READY, 1.0)),
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
        self.assertIn("Ready", texts)
        self.assertIn("  ·  Subtitles EN NL", texts)
        self.assertEqual(rows[0]._bar.winfo_manager(), "pack")
        self.assertEqual(rows[1]._bar.winfo_manager(), "")

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

        # A new grab: rebuilt; a row opens its show.
        self.status.publish(
            DownloadsOverview(entries=(entry(3), entry(1), entry(2)), program="Download Station")
        )
        self.root.update()
        self.assertEqual(len(page._rows), 3)
        self.assertNotIn(rows[0], page._rows)
        page._rows[0]._clicked()
        self.assertEqual(self.opened, [FALLOUT])

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

    async def test_button_badge_and_space_notice(self) -> None:
        reports: list[tuple[str, int]] = []
        button = DownloadsButton(
            self.root,
            downloads=self.status,  # type: ignore[arg-type]
            command=lambda: None,
            report=lambda text, ms: reports.append((text, ms)),
        )
        button.pack()
        self.root.update()
        self.status.publish(DownloadsOverview(entries=(entry(1), entry(2, DownloadState.WAITING))))
        self.root.update()
        self.assertEqual(button._badge.cget("text"), "2")
        self.assertEqual(button._badge.winfo_manager(), "place")
        self.status.publish(DownloadsOverview(entries=(entry(1, DownloadState.READY, 1.0),)))
        self.root.update()
        self.assertEqual(button._badge.winfo_manager(), "")
        space = FreeSpace(free_bytes=12 * GB, low=True)
        for listener in self.status.space_listeners:
            listener(space)
        self.assertEqual(reports, [("⚠ Your NAS is almost full: 12 GB free", 15_000)])
        button.set_selected(True)
        button.destroy()
        self.assertEqual(self.status.listeners, [])
        self.assertEqual(self.status.space_listeners, [])


if __name__ == "__main__":
    unittest.main()
