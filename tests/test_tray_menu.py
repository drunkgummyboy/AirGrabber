"""The tray menu's lines: today's episodes, the downloads, the automatic downloads' pause."""

from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta

from app.models import (
    CalendarEntry,
    DownloadEntry,
    DownloadRecord,
    DownloadsOverview,
    DownloadState,
    Episode,
    MediaType,
    Show,
)
from app.ui.tray_menu import downloads_line, pause_until, resume_text, today_line


def entry(name: str, season: int, number: int) -> CalendarEntry:
    return CalendarEntry(
        show=Show(id=number, name=name),
        episode=Episode(id=number, show_id=number, season=season, number=number, air_date=date.today()),
        is_tracked=True,
    )


def grab(state: DownloadState, progress: float | None) -> DownloadEntry:
    record = DownloadRecord(media_type=MediaType.TV, media_id=1, season=1, episode=1, release_title="x")
    return DownloadEntry(record=record, title="x", state=state, progress=progress)


class LineTests(unittest.TestCase):
    def test_today(self) -> None:
        self.assertEqual(today_line([]), "Nothing airs today")
        self.assertEqual(today_line([entry("Reacher", 4, 8)]), "Today: Reacher S04E08")
        many = [entry(name, 1, n) for n, name in enumerate(["A", "B", "C", "D", "E"], 1)]
        self.assertEqual(today_line(many), "Today: A S01E01, B S01E02, C S01E03 +2 more")

    def test_downloads(self) -> None:
        self.assertEqual(downloads_line(None), "")
        idle = DownloadsOverview(entries=(grab(DownloadState.READY, 1.0),))
        self.assertEqual(downloads_line(idle), "")
        busy = DownloadsOverview(
            entries=(grab(DownloadState.DOWNLOADING, 0.3), grab(DownloadState.DOWNLOADING, 0.6))
        )
        self.assertEqual(downloads_line(busy), "Downloading 2 · 45%")
        unknown = DownloadsOverview(entries=(grab(DownloadState.DOWNLOADING, None),))
        self.assertEqual(downloads_line(unknown), "Downloading 1")

    def test_the_pause(self) -> None:
        now = datetime(2026, 11, 8, 13, 30, tzinfo=UTC)
        self.assertEqual(pause_until(now, 3), now + timedelta(hours=3))
        tomorrow = pause_until(now, None).astimezone()
        self.assertEqual((tomorrow.hour, tomorrow.minute), (0, 0))
        self.assertEqual(tomorrow.date(), (now.astimezone() + timedelta(days=1)).date())
        self.assertTrue(resume_text(now).startswith("Resume automatic downloads (paused until "))
