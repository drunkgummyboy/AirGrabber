"""Downloads that stopped moving: found by StuckWatch, swapped while leaving out what was fetched."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

from app.models import (
    DownloadEntry,
    DownloadRecord,
    DownloadsOverview,
    DownloadState,
    MediaType,
    RemoteTask,
    Settings,
)
from app.services.stuck_downloads import MAX_SWAPS, StuckWatch, avoided

START = datetime(2026, 11, 8, 12, tzinfo=UTC)


def record(grab_id: int, episode: int = 1, title: str = "Show.S01E01.1080p-GRP") -> DownloadRecord:
    return DownloadRecord(
        id=grab_id,
        media_type=MediaType.TV,
        media_id=7,
        season=1,
        episode=episode,
        release_title=title,
        info_hash=f"{grab_id:040x}",
        downloaded_at=START,
    )


def entry(
    grab_id: int,
    progress: float,
    *,
    state: DownloadState = DownloadState.DOWNLOADING,
    added: datetime | None = None,
    task_id: str | None = "abc",
    episode: int = 1,
) -> DownloadEntry:
    task = RemoteTask(
        name="Show", progress=progress, state=state, task_id=task_id, added_at=added
    )
    return DownloadEntry(
        record=record(grab_id, episode),
        title="Show S01E01",
        state=state,
        progress=progress,
        task=task,
    )


def overview(*entries: DownloadEntry, synced: bool = True) -> DownloadsOverview:
    return DownloadsOverview(entries=entries, synced=synced)


class StuckWatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = START
        self.watch = StuckWatch(now=lambda: self.now)

    def later(self, hours: float) -> None:
        self.now += timedelta(hours=hours)

    def test_a_download_standing_still_is_swapped_once(self) -> None:
        self.assertEqual(self.watch.stuck(overview(entry(1, 0.40)), 6), [])
        self.later(5)
        self.assertEqual(self.watch.stuck(overview(entry(1, 0.4005)), 6), [])  # not moving
        self.later(1)
        [due] = self.watch.stuck(overview(entry(1, 0.4005)), 6)
        self.assertEqual(due.record.id, 1)
        self.later(1)
        self.assertEqual(self.watch.stuck(overview(entry(1, 0.4005)), 6), [])  # once

    def test_moving_starts_the_clock_again(self) -> None:
        self.watch.stuck(overview(entry(1, 0.10)), 6)
        self.later(5)
        self.watch.stuck(overview(entry(1, 0.20)), 6)
        self.later(5)
        self.assertEqual(self.watch.stuck(overview(entry(1, 0.20)), 6), [])
        self.later(1)
        self.assertEqual(len(self.watch.stuck(overview(entry(1, 0.20)), 6)), 1)

    def test_a_task_still_at_nothing_counts_from_when_the_program_got_it(self) -> None:
        added = START - timedelta(hours=7)
        self.assertEqual(len(self.watch.stuck(overview(entry(1, 0.0, added=added)), 6)), 1)

    def test_never_swapped(self) -> None:
        added = START - timedelta(hours=30)
        self.assertEqual(self.watch.stuck(overview(entry(1, 0.0, added=added)), 0), [])
        paused = entry(2, 0.0, added=added, state=DownloadState.STOPPED)
        queued = entry(3, 0.0, added=added, state=DownloadState.WAITING)
        no_task = entry(4, 0.0, added=added, task_id=None)
        self.assertEqual(self.watch.stuck(overview(paused, queued, no_task), 6), [])
        unread = overview(entry(5, 0.0, added=added), synced=False)
        self.assertEqual(self.watch.stuck(unread, 6), [])

    def test_a_title_is_swapped_at_most_max_swaps_times(self) -> None:
        added = START - timedelta(hours=7)
        swapped = 0
        for grab_id in range(1, MAX_SWAPS + 3):
            swapped += len(self.watch.stuck(overview(entry(grab_id, 0.0, added=added)), 6))
        self.assertEqual(swapped, MAX_SWAPS)
        other = entry(99, 0.0, added=added, episode=2)
        self.assertEqual(len(self.watch.stuck(overview(other), 6)), 1)


class AvoidedTests(unittest.TestCase):
    def test_every_release_of_the_same_episode(self) -> None:
        stuck = record(3, title="Show.S01E01.1080p-NEW")
        history = [
            record(1, title="Show.S01E01.720p-OLD"),
            record(2, episode=2, title="Show.S01E02.1080p-GRP"),
            stuck,
        ]
        keys = avoided(history, stuck)
        self.assertIn("show.s01e01.1080p-new", keys)
        self.assertIn("show.s01e01.720p-old", keys)
        self.assertIn(f"{3:040x}", keys)
        self.assertNotIn("show.s01e02.1080p-grp", keys)


class SettingTests(unittest.TestCase):
    def test_the_nearest_choice(self) -> None:
        self.assertEqual(Settings().stuck_swap_hours, 6)
        self.assertEqual(Settings(stuck_swap_hours=0).stuck_swap_hours, 0)
        self.assertEqual(Settings(stuck_swap_hours=5).stuck_swap_hours, 6)
        self.assertEqual(Settings(stuck_swap_hours=100).stuck_swap_hours, 24)
        self.assertEqual(Settings(stuck_swap_hours="x").stuck_swap_hours, 6)


if __name__ == "__main__":
    unittest.main()
