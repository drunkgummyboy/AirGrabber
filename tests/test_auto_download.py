"""Automatic downloads of new episodes: schema v2, the due-episode SQL, the per-show switch,
`auto_download_new_shows`, and AutoDownloadService runs against a fake one-click Download."""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import tempfile
import unittest
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock

import httpx

from app.config import AppPaths
from app.database import MIGRATIONS, Database, HistoryRepository
from app.models import (
    AutoDownloaded,
    AutoDownloadReport,
    AutoError,
    AutoEvent,
    AutoGaveUp,
    DownloadRecord,
    Episode,
    GrabOutcome,
    GrabResult,
    GrabTarget,
    MediaType,
    QuickDownloadResult,
    SearchKind,
    SearchRequest,
    Settings,
    Show,
)
from app.services import Services, SettingsService
from app.services.auto_download import (
    DUE_DELAY,
    GIVE_UP_AFTER,
    MAX_PER_RUN,
    AutoDownloadService,
)
from app.i18n import t
from app.services.downloads import NO_LINK, GrabError, ReleaseError
from app.services.remote import ReleaseRefused, RemoteError
from app.services.library import LibraryService
from app.services.quick_download import (
    SOURCES_RESTING,
    Progress,
    QuickDownloadService,
    SourcesResting,
    request_label,
)
from app.services.search import episode_request
from app.services.synology import DownloadStationError
from app.services.tvmaze import TVMazeClient
from tests.test_library import DRAGON, DUNE, FakeTVMaze

LOGGER = "app.services.auto_download"
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)
WEEK_AGO = NOW - timedelta(days=7)

SAVED = GrabResult(outcome=GrabOutcome.SAVED, path="C:/watch/x.torrent", message="Saved x.torrent")
FOUND = QuickDownloadResult(
    found=True, release_title="Severance S02E04 1080p WEB", quality="1080p", seeders=50, grab=SAVED
)
NOTHING = QuickDownloadResult(found=False)

Outcome = QuickDownloadResult | Exception


def show(show_id: int, name: str | None = None) -> Show:
    return Show(id=show_id, name=name or f"Show {show_id}", premiered="2022-02-18")


def episode(
    episode_id: int, show_id: int, aired: datetime | None, *, season: int = 1, number: int | None
) -> Episode:
    return Episode(
        id=episode_id, show_id=show_id, season=season, number=number, airstamp=aired
    )


def still_airing(show_id: int, *, season: int = 1) -> Episode:
    """An episode of `season` airing next week: the season isn't over, so the automatic
    downloads ask for no season pack (item 22) and fetch its episodes one by one."""
    return episode(900 + show_id, show_id, NOW + timedelta(days=7), season=season, number=99)


async def until(condition: Callable[[], bool]) -> None:
    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), timeout=5)


class FakeQuickDownload(QuickDownloadService):
    """Answers from `outcomes` (by request label, else `default`) without searching; a found
    release is recorded in the download history, as the real grab does."""

    def __init__(self, history: HistoryRepository) -> None:  # no search, no grabs
        self._history = history
        self.outcomes: dict[str, Outcome] = {}
        self.default: Outcome = FOUND
        self.calls: list[tuple[SearchRequest, GrabTarget]] = []
        self.gate: asyncio.Event | None = None
        self.active = 0
        self.max_active = 0

    @property
    def labels(self) -> list[str]:
        return [request_label(request) for request, _ in self.calls]

    async def download(
        self,
        request: SearchRequest,
        target: GrabTarget,
        progress: Progress | None = None,
        *,
        again: bool = False,
        avoid: object = (),  # a stuck download's swap
    ) -> QuickDownloadResult:
        self.calls.append((request, target))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.gate is not None:
                await self.gate.wait()
            outcome = self.outcomes.get(request_label(request), self.default)
            if isinstance(outcome, Exception):
                raise outcome
            if outcome.grab is not None:
                await self._history.add(
                    DownloadRecord(
                        media_type=MediaType.TV,
                        media_id=target.media_id,
                        season=target.season,
                        episode=target.episode,
                        release_title=outcome.release_title or "?",
                    )
                )
            return outcome
        finally:
            self.active -= 1


class DatabaseTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.path = Path(self.tmp.name) / "library.db"
        self.db = await Database.open(self.path)

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    async def add_show(
        self,
        show_id: int,
        episodes: Iterable[Episode],
        *,
        name: str | None = None,
        since: datetime | None = WEEK_AGO,
        tracked: bool = True,
    ) -> Show:
        """Save a show with `episodes`; track it with its switch on from `since` (None: off)."""
        saved = show(show_id, name)
        await self.db.library.save_show(saved, list(episodes))
        if tracked:
            await self.db.library.track(show_id, profile_id=1)
            if since is not None:
                await self.db.library.set_auto_download(show_id, True, since=since, profile_id=1)
        return saved

    async def due(
        self, now: datetime = NOW, limit: int = 100, backlog: int | None = None
    ) -> list[int]:
        entries = await self.db.library.auto_due(
            aired_from=now - GIVE_UP_AFTER, aired_until=now - DUE_DELAY, limit=limit,
            backlog=backlog,
        )
        return [entry.episode.id for entry in entries]

    async def attempt(self, episode_id: int) -> tuple[int, str, int] | None:
        row = await self.db.fetch_one(
            "SELECT tries, last_try, gave_up FROM auto_attempts WHERE episode_id = ?",
            (episode_id,),
        )
        return None if row is None else (row["tries"], row["last_try"], row["gave_up"])

    async def auto_since(self, show_id: int) -> str | None:
        row = await self.db.fetch_one(
            "SELECT auto_since FROM tracked_shows WHERE show_id = ?", (show_id,)
        )
        assert row is not None
        value: str | None = row["auto_since"]
        return value


class DueEpisodeTests(DatabaseTestCase):
    async def test_due_from_two_hours_after_airing(self) -> None:
        await self.add_show(
            1,
            [
                episode(11, 1, NOW - DUE_DELAY, number=1),
                episode(12, 1, NOW - DUE_DELAY + timedelta(seconds=1), number=2),
                episode(13, 1, NOW + timedelta(days=1), number=3),
            ],
        )
        self.assertEqual(await self.due(), [11])

    async def test_due_until_three_days_after_airing(self) -> None:
        await self.add_show(
            1,
            [
                episode(11, 1, NOW - GIVE_UP_AFTER, number=1),
                episode(12, 1, NOW - GIVE_UP_AFTER - timedelta(seconds=1), number=2),
            ],
            since=NOW - timedelta(days=30),
        )
        # Past the window only while never tried (the PC was off), behind those within it.
        self.assertEqual(await self.due(), [11, 12])
        await self.db.library.record_auto_attempt(11, NOW)
        await self.db.library.record_auto_attempt(12, NOW)
        self.assertEqual(await self.due(), [11])

    async def test_only_episodes_aired_after_the_switch_went_on(self) -> None:
        since = NOW - timedelta(days=1)
        await self.add_show(
            1,
            [
                episode(11, 1, since - timedelta(hours=1), number=1),
                episode(12, 1, since, number=2),
                episode(13, 1, since + timedelta(seconds=1), number=3),
            ],
            since=since,
        )
        self.assertEqual(await self.due(), [13])

    async def test_grabbed_episodes_and_season_packs_are_not_due(self) -> None:
        aired = NOW - timedelta(hours=5)
        await self.add_show(1, [episode(11, 1, aired, number=1), episode(12, 1, aired, number=2)])
        await self.add_show(2, [episode(21, 2, aired, season=3, number=1)])
        self.assertEqual(await self.due(), [11, 12, 21])
        await self.db.history.add(
            DownloadRecord(
                media_type=MediaType.TV, media_id=1, season=1, episode=2, release_title="e"
            )
        )
        await self.db.history.add(
            DownloadRecord(media_type=MediaType.TV, media_id=2, season=3, release_title="pack")
        )
        self.assertEqual(await self.due(), [11])

    async def test_given_up_episodes_are_not_due(self) -> None:
        await self.add_show(1, [episode(11, 1, NOW - timedelta(days=2), number=1)])
        self.assertEqual(await self.due(), [11])
        await self.db.library.record_auto_attempt(11, NOW)
        # Two days later it is overdue and given up; even a window reaching back is now empty.
        later = NOW + timedelta(days=2)
        given_up = await self.db.library.auto_give_up(aired_before=later - GIVE_UP_AFTER)
        self.assertEqual([entry.episode.id for entry in given_up], [11])
        self.assertEqual(await self.due(), [])
        self.assertEqual(await self.due(later), [])

    async def test_an_episode_never_tried_is_not_given_up(self) -> None:
        # The PC was off: it aired four days ago and was never searched.
        await self.add_show(1, [episode(11, 1, NOW - timedelta(days=4), number=1)])
        given_up = await self.db.library.auto_give_up(aired_before=NOW - GIVE_UP_AFTER)
        self.assertEqual(given_up, [])
        self.assertIsNone(await self.attempt(11))
        self.assertEqual(await self.due(), [11])  # still due, past the window, until it is tried

    async def test_a_tried_overdue_episode_is_given_up(self) -> None:
        await self.add_show(1, [episode(11, 1, NOW - timedelta(days=4), number=1)])
        await self.db.library.record_auto_attempt(11, NOW - timedelta(hours=1))
        given_up = await self.db.library.auto_give_up(aired_before=NOW - GIVE_UP_AFTER)
        self.assertEqual([entry.episode.id for entry in given_up], [11])
        self.assertEqual(await self.attempt(11), (1, (NOW - timedelta(hours=1)).isoformat(), 1))
        self.assertEqual(await self.due(), [])
        # Given up once: a later call leaves it out.
        self.assertEqual(
            await self.db.library.auto_give_up(aired_before=NOW - GIVE_UP_AFTER), []
        )

    async def test_an_old_give_up_without_a_try_is_released(self) -> None:
        # Before 2026-11-15 an episode was given up untried: a row with no tries.
        await self.add_show(1, [episode(11, 1, NOW - timedelta(days=4), number=1)])
        async with self.db.transaction() as conn:
            await conn.execute(
                "INSERT INTO auto_attempts (episode_id, tries, last_try, gave_up) "
                "VALUES (?, 0, ?, 1)",
                (11, (NOW - timedelta(days=1)).isoformat()),
            )
        self.assertEqual(await self.due(), [11])
        self.assertEqual(
            await self.db.library.auto_give_up(aired_before=NOW - GIVE_UP_AFTER), []
        )
        await self.db.library.record_auto_attempt(11, NOW)
        self.assertEqual(await self.attempt(11), (1, NOW.isoformat(), 0))
        given_up = await self.db.library.auto_give_up(aired_before=NOW - GIVE_UP_AFTER)
        self.assertEqual([entry.episode.id for entry in given_up], [11])

    async def test_new_episodes_come_before_a_backlog_never_tried(self) -> None:
        await self.add_show(
            1,
            [
                episode(11, 1, NOW - timedelta(days=6), number=1),
                episode(12, 1, NOW - timedelta(days=5), number=2),
                episode(13, 1, NOW - timedelta(days=1), number=3),
            ],
            since=NOW - timedelta(days=30),
        )
        self.assertEqual(await self.due(), [13, 11, 12])

    async def test_a_backlog_takes_at_most_its_places(self) -> None:
        old = [episode(10 + n, 1, NOW - timedelta(days=9 - n), number=n) for n in range(1, 5)]
        current = [episode(21, 1, NOW - timedelta(days=1), number=5)]
        await self.add_show(1, old + current, since=NOW - timedelta(days=30))
        self.assertEqual(await self.due(limit=5, backlog=2), [21, 11, 12])
        await self.db.library.record_auto_attempt(21, NOW)  # tried: still after the backlog
        self.assertEqual(await self.due(limit=5, backlog=2), [11, 12, 21])

    async def test_a_stopped_run_moves_its_episode_back_without_a_try(self) -> None:
        aired = NOW - timedelta(hours=5)
        await self.add_show(1, [episode(11, 1, aired, number=1), episode(12, 1, aired, number=2)])
        await self.db.library.stamp_auto_attempt(11, NOW)
        self.assertEqual(await self.attempt(11), (0, NOW.isoformat(), 0))
        self.assertEqual(await self.due(), [12, 11])
        self.assertEqual(
            await self.db.library.auto_give_up(aired_before=NOW + timedelta(days=9)), []
        )
        await self.db.library.stamp_auto_attempt(999, NOW)  # a vanished episode: ignored
        self.assertIsNone(await self.attempt(999))

    async def test_specials_and_episodes_without_an_airstamp_are_never_due(self) -> None:
        aired = NOW - timedelta(hours=5)
        await self.add_show(
            1,
            [
                episode(11, 1, aired, season=0, number=1),  # season 0
                episode(12, 1, aired, number=None),  # special inside a season
                episode(13, 1, None, number=3),  # no airstamp
                episode(14, 1, aired, number=4),
            ],
        )
        self.assertEqual(await self.due(), [14])
        for episode_id in (11, 12, 13, 14):
            await self.db.library.record_auto_attempt(episode_id, NOW)
        given_up = await self.db.library.auto_give_up(aired_before=NOW + timedelta(days=30))
        self.assertEqual([entry.episode.id for entry in given_up], [14])

    async def test_only_tracked_shows_with_the_switch_on(self) -> None:
        aired = NOW - timedelta(hours=5)
        await self.add_show(1, [episode(11, 1, aired, number=1)])
        await self.add_show(2, [episode(21, 2, aired, number=1)], since=None)  # switch off
        await self.add_show(3, [episode(31, 3, aired, number=1)], tracked=False)
        self.assertEqual(await self.due(), [11])

    async def test_oldest_first_and_limited(self) -> None:
        await self.add_show(
            1,
            [episode(10 + n, 1, NOW - timedelta(hours=10 - n), number=n) for n in range(1, 5)],
            name="B show",
        )
        await self.add_show(2, [episode(21, 2, NOW - timedelta(hours=9), number=1)], name="A show")
        # 11 and 21 aired at the same moment: by show name.
        self.assertEqual(await self.due(), [21, 11, 12, 13, 14])
        self.assertEqual(await self.due(limit=2), [21, 11])

    async def test_least_tried_first_so_new_episodes_are_not_crowded_out(self) -> None:
        aired = NOW - timedelta(days=2)
        await self.add_show(
            1, [episode(10 + n, 1, aired + timedelta(hours=n), number=n) for n in range(1, 7)]
        )
        for n in range(1, 6):  # episodes 11-15: three tries each, 15 tried longest ago
            for minutes in (90, 60, 50 + n):
                await self.db.library.record_auto_attempt(10 + n, NOW - timedelta(minutes=minutes))
            self.assertEqual((await self.attempt(10 + n) or (0,))[0], 3)
        self.assertEqual(await self.due(limit=5), [16, 15, 14, 13, 12])

    async def test_attempts_survive_a_refresh_and_go_with_their_episode(self) -> None:
        aired = NOW - timedelta(hours=5)
        saved = await self.add_show(
            1, [episode(11, 1, aired, number=1), episode(12, 1, aired, number=2)]
        )
        await self.db.library.record_auto_attempt(11, NOW)
        await self.db.library.record_auto_attempt(12, NOW)
        await self.db.library.record_auto_attempt(12, NOW + timedelta(minutes=30))
        self.assertEqual(await self.attempt(12), (2, (NOW + timedelta(minutes=30)).isoformat(), 0))
        # A sync stores the show's episode list again: episode 12 is gone from TVMaze.
        await self.db.library.save_show(saved, [episode(11, 1, aired, number=1)])
        self.assertEqual(await self.attempt(11), (1, NOW.isoformat(), 0))
        self.assertIsNone(await self.attempt(12))
        self.assertEqual([e.id for e in await self.db.library.episodes(1)], [11])

    async def test_attempt_at_a_vanished_episode_is_ignored(self) -> None:
        await self.db.library.record_auto_attempt(999, NOW)
        self.assertIsNone(await self.attempt(999))


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_v1_database_is_upgraded_with_its_data(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "v1.db"
        with contextlib.closing(sqlite3.connect(path)) as raw:
            raw.executescript(f"BEGIN;\n{MIGRATIONS[0]}\nPRAGMA user_version = 1;\nCOMMIT;")
            with raw:
                raw.executemany(
                    "INSERT INTO shows (id, name, status, genres) VALUES (?, ?, ?, ?)",
                    [(1, "Severance", "Running", '["Drama"]'), (2, "Andor", "Ended", "[]")],
                )
                raw.executemany(
                    "INSERT INTO episodes (id, show_id, season, number, airstamp) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [
                        (11, 1, 2, 1, "2025-01-17T02:00:00Z"),
                        (12, 1, 2, 2, "2025-01-24T02:00:00Z"),
                        (21, 2, 1, 1, "2022-09-21T01:00:00Z"),
                    ],
                )
                raw.executemany(
                    "INSERT INTO tracked_shows (show_id, tracked_at, auto_download, synced_at) "
                    "VALUES (?, ?, ?, ?)",
                    [
                        (1, "2025-01-01T10:00:00+00:00", 1, "2025-02-01T10:00:00+00:00"),
                        (2, "2024-06-01T10:00:00+00:00", 0, None),
                    ],
                )
                raw.execute(
                    "INSERT INTO download_history (media_type, media_id, season, episode, "
                    "release_title, info_hash, downloaded_at) VALUES ('tv', 1, 2, 1, "
                    "'Severance.S02E01.1080p', 'abcdef0123456789abcdef0123456789abcdef01', "
                    "'2025-01-17T09:00:00+00:00')"
                )
                raw.execute("INSERT INTO settings (key, value) VALUES ('weeks_to_show', '4')")

        before = datetime.now(UTC)
        db = await Database.open(path)
        self.addAsyncCleanup(db.close)
        after = datetime.now(UTC)

        version = await db.fetch_one("PRAGMA user_version")
        assert version is not None
        self.assertEqual(version[0], len(MIGRATIONS))
        self.assertGreaterEqual(len(MIGRATIONS), 2)
        entries = {entry.show.id: entry for entry in await db.library.tracked(profile_id=1)}
        self.assertEqual(sorted(entries), [1, 2])
        self.assertEqual(entries[1].show.genres, ("Drama",))
        # Every switch starts off after the upgrade, also those the 1.x import had on.
        self.assertFalse(entries[1].auto_download)
        self.assertFalse(entries[2].auto_download)
        # v10 (3.1.0) clears it: the next sync reads every followed show again (TheTVDB ids).
        self.assertIsNone(entries[1].synced_at)
        self.assertEqual([e.id for e in await db.library.episodes(1)], [11, 12])
        self.assertTrue(await db.history.has_episode(1, 2, 1))
        self.assertFalse(await db.history.has_episode(1, 2, 2))
        self.assertEqual((await db.settings.load()).weeks_to_show, 4)

        # No switch has a start: nothing is due until the user turns a show on again.
        rows = {
            row["show_id"]: (row["auto_download"], row["auto_since"])
            for row in await db.fetch_all(
                "SELECT show_id, auto_download, auto_since FROM tracked_shows"
            )
        }
        self.assertEqual(rows, {1: (0, None), 2: (0, None)})
        since_2000 = datetime(2000, 1, 1, tzinfo=UTC)
        self.assertEqual(
            await db.library.auto_due(aired_from=since_2000, aired_until=after, limit=10), []
        )

        # The new table works, cascades from episodes, and the foreign keys hold.
        await db.library.record_auto_attempt(12, before)
        self.assertEqual(len(await db.fetch_all("SELECT * FROM auto_attempts")), 1)
        await db.library.save_show(show(1, "Severance"), [episode(11, 1, None, season=2, number=1)])
        self.assertEqual(await db.fetch_all("SELECT * FROM auto_attempts"), [])
        self.assertEqual(await db.fetch_all("PRAGMA foreign_key_check"), [])
        tables = {
            row["name"]: row["strict"] for row in await db.fetch_all("PRAGMA table_list")
        }
        self.assertEqual(tables["auto_attempts"], 1)

    async def test_fresh_database_gets_the_whole_schema(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        db = await Database.open(Path(tmp.name) / "new.db")
        self.addAsyncCleanup(db.close)
        version = await db.fetch_one("PRAGMA user_version")
        assert version is not None
        self.assertEqual(version[0], len(MIGRATIONS))
        columns = {row["name"] for row in await db.fetch_all("PRAGMA table_info(tracked_shows)")}
        self.assertIn("auto_since", columns)


class SwitchTests(DatabaseTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.server = FakeTVMaze()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.server))
        self.settings = SettingsService(self.db.settings, Settings())
        self.library = LibraryService(
            self.db.library,
            self.db.history,
            TVMazeClient(self.http, retry_delay=0),
            settings=self.settings,
        )
        self.notifications = 0

        def count() -> None:
            self.notifications += 1

        self.library.subscribe(count)

    async def asyncTearDown(self) -> None:
        await self.http.aclose()
        await super().asyncTearDown()

    async def switch(self, show_id: int) -> bool:
        entries = {entry.show.id: entry for entry in await self.library.tracked(profile_id=1)}
        return entries[show_id].auto_download

    async def test_switch_on_and_off(self) -> None:
        await self.add_show(1, [], since=None)
        self.assertFalse(await self.switch(1))
        self.assertIsNone(await self.auto_since(1))

        before = datetime.now(UTC)
        await self.library.set_auto_download(1, True, profile_id=1)
        self.assertTrue(await self.switch(1))
        since = await self.auto_since(1)
        assert since is not None
        self.assertLessEqual(before, datetime.fromisoformat(since))
        self.assertEqual(self.notifications, 1)

        await self.library.set_auto_download(1, True, profile_id=1)  # already on: keeps its start
        self.assertEqual(await self.auto_since(1), since)

        await self.library.set_auto_download(1, False, profile_id=1)
        self.assertFalse(await self.switch(1))
        self.assertIsNone(await self.auto_since(1))
        self.assertEqual(self.notifications, 3)

    async def test_switching_an_untracked_show_does_nothing(self) -> None:
        await self.library.set_auto_download(12345, True, profile_id=1)
        self.assertEqual(await self.library.tracked(profile_id=1), [])

    async def test_new_shows_follow_the_setting(self) -> None:
        await self.library.track(DUNE, profile_id=1)
        self.assertFalse(await self.switch(DUNE))
        self.assertIsNone(await self.auto_since(DUNE))

        await self.settings.update(auto_download_new_shows=True)
        before = datetime.now(UTC)
        await self.library.track(DRAGON, profile_id=1)
        self.assertTrue(await self.switch(DRAGON))
        since = await self.auto_since(DRAGON)
        assert since is not None
        self.assertLessEqual(before, datetime.fromisoformat(since))

        await self.library.track(DUNE, profile_id=1)  # already followed: refreshed, switch untouched
        self.assertFalse(await self.switch(DUNE))

    async def test_imported_shows_follow_the_setting(self) -> None:
        await self.settings.update(auto_download_new_shows=True)
        result = await self.library.import_shows([str(DUNE)], profile_id=1)
        self.assertEqual(len(result.added), 1)
        self.assertTrue(await self.switch(DUNE))

    async def test_without_settings_new_shows_start_switched_off(self) -> None:
        library = LibraryService(
            self.db.library, self.db.history, TVMazeClient(self.http, retry_delay=0)
        )
        await library.track(DUNE, profile_id=1)
        self.assertFalse(await self.switch(DUNE))


class ActivityTests(DatabaseTestCase):
    """LibraryService.activity: the Tracked cards' latest grab and next episode."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(FakeTVMaze()))
        self.library = LibraryService(
            self.db.library, self.db.history, TVMazeClient(self.http, retry_delay=0)
        )

    async def asyncTearDown(self) -> None:
        await self.http.aclose()
        await super().asyncTearDown()

    async def grab(self, show_id: int, season: int, number: int | None, when: datetime) -> None:
        await self.db.history.add(
            DownloadRecord(
                media_type=MediaType.TV,
                media_id=show_id,
                season=season,
                episode=number,
                release_title=f"Show {show_id} S{season}E{number}",
                downloaded_at=when,
            )
        )

    async def test_the_next_episode_per_show_and_no_grabs(self) -> None:
        await self.add_show(
            1,
            [
                episode(11, 1, NOW - timedelta(days=1), number=1),
                episode(12, 1, NOW + timedelta(days=2), number=2),
                episode(13, 1, NOW + timedelta(days=9), number=3),
                episode(10, 1, NOW + timedelta(days=1), season=0, number=1),  # a special
            ],
            since=None,
        )
        await self.add_show(2, [episode(21, 2, NOW - timedelta(days=30), number=1)], since=None)
        await self.add_show(3, [episode(31, 3, NOW + timedelta(hours=1), number=1)], tracked=False)
        await self.add_show(4, [], since=None)
        await self.grab(1, 1, 1, NOW - timedelta(days=1))
        await self.grab(1, 1, 2, NOW - timedelta(hours=2))  # the latest one wins
        await self.grab(2, 1, 1, NOW - timedelta(days=8))  # too old
        await self.grab(4, 1, None, NOW - timedelta(days=3))  # a season pack

        activity = await self.library.activity(now=NOW, profile_id=1)

        # A grab is never mentioned (only Plex says what you have, 3.1.0): 2 and 4 have
        # nothing upcoming; 3 is untracked.
        self.assertEqual(sorted(activity), [1])
        first = activity[1]
        assert first.next_episode is not None
        self.assertEqual(first.next_episode.id, 12)

    async def test_an_episode_without_airstamp_counts_from_its_air_date(self) -> None:
        await self.db.library.save_show(
            show(1),
            [
                Episode(id=11, show_id=1, season=1, number=1, air_date=NOW.date()),
                Episode(
                    id=12, show_id=1, season=1, number=2, air_date=NOW.date() + timedelta(days=7)
                ),
            ],
        )
        await self.db.library.track(1, profile_id=1)
        activity = await self.library.activity(now=NOW, profile_id=1)
        upcoming = activity[1].next_episode
        assert upcoming is not None
        self.assertEqual(upcoming.id, 11)  # airs today: still "next"

    async def test_last_downloads_however_long_ago(self) -> None:
        """My shows' "Recently downloaded" order: every show's latest grab, old ones too."""
        await self.add_show(1, [], since=None)
        await self.add_show(2, [], since=None)
        await self.add_show(3, [], since=None)
        await self.grab(1, 1, 1, NOW - timedelta(days=400))
        await self.grab(1, 1, 2, NOW - timedelta(days=200))  # the latest one wins
        await self.grab(2, 3, None, NOW - timedelta(hours=1))  # a season pack

        last = await self.library.last_downloads()

        self.assertEqual(last, {1: NOW - timedelta(days=200), 2: NOW - timedelta(hours=1)})


class RunTests(DatabaseTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.now = NOW
        self.quick = FakeQuickDownload(self.db.history)
        self.service = AutoDownloadService(
            self.db.library,
            self.quick,
            SettingsService(self.db.settings, Settings()),
            clock=lambda: self.now,
        )
        self.events: list[AutoEvent] = []
        self.service.subscribe(self.events.append)

    async def test_found_episode_is_downloaded_once(self) -> None:
        severance = await self.add_show(
            44933, [episode(1, 44933, NOW - timedelta(hours=3), season=2, number=4)],
            name="Severance",
        )
        report = await self.service.run_once()
        expected = AutoDownloaded(label="Severance S02E04", quality="1080p")
        self.assertEqual(report, AutoDownloadReport(downloaded=(expected,)))
        self.assertEqual(self.events, [expected])
        [(request, target)] = self.quick.calls
        self.assertEqual(request, episode_request(severance, 2, 4))
        self.assertEqual(
            target, GrabTarget(media_type=MediaType.TV, media_id=44933, season=2, episode=4)
        )
        # Downloaded: in the history, so never due again.
        self.now += timedelta(minutes=30)
        self.assertEqual(await self.service.run_once(), AutoDownloadReport())
        self.assertEqual(len(self.quick.calls), 1)
        self.assertEqual(self.events, [expected])

    async def test_the_wait_after_airing_follows_the_setting(self) -> None:
        await self.add_show(1, [episode(11, 1, NOW - timedelta(hours=3), number=1)], name="Lost")
        await self.service._settings.update(auto_wait_hours=4)
        self.assertEqual(await self.service.run_once(), AutoDownloadReport())  # 3 h: too early
        self.now += timedelta(hours=1, seconds=1)
        report = await self.service.run_once()
        self.assertEqual([d.label for d in report.downloaded], ["Lost S01E01"])

    async def test_a_pause_from_the_tray_holds_them_until_it_ends(self) -> None:
        await self.add_show(1, [episode(11, 1, NOW - timedelta(hours=3), number=1)], name="Lost")
        await self.service._settings.update(auto_paused_until=NOW + timedelta(hours=3))
        self.assertTrue(self.service.paused())
        self.assertEqual(await self.service.run_once(), AutoDownloadReport())
        self.assertEqual(self.quick.calls, [])
        self.now += timedelta(hours=3)
        self.assertFalse(self.service.paused())
        report = await self.service.run_once()
        self.assertEqual([d.label for d in report.downloaded], ["Lost S01E01"])

    async def test_nothing_found_counts_a_try_and_retries_next_run(self) -> None:
        await self.add_show(1, [episode(11, 1, NOW - timedelta(hours=3), number=1)], name="Lost")
        self.quick.default = NOTHING
        report = await self.service.run_once()
        self.assertEqual(report, AutoDownloadReport(not_found=("Lost S01E01",)))
        self.assertEqual(self.events, [])
        self.assertEqual(await self.attempt(11), (1, NOW.isoformat(), 0))

        self.now += timedelta(minutes=30)
        await self.service.run_once()
        self.assertEqual(await self.attempt(11), (2, self.now.isoformat(), 0))
        self.assertEqual(self.quick.labels, ["Lost S01E01", "Lost S01E01"])

    async def test_an_unreachable_nas_stops_the_run_without_counting_tries(self) -> None:
        aired = NOW - timedelta(hours=3)
        await self.add_show(
            1,
            [
                *(episode(10 + n, 1, aired + timedelta(minutes=n), number=n) for n in (1, 2, 3)),
                still_airing(1),
            ],
            name="Lost",
        )
        problem = "Can't write to the download folder \\\\NAS\\Media: The network path was not found"
        self.quick.default = GrabError(problem)
        with self.assertLogs(LOGGER, "WARNING"):
            report = await self.service.run_once()
        self.assertEqual(report.error, problem)
        self.assertEqual(report.downloaded, ())
        self.assertEqual(self.events, [AutoError(message=problem)])
        self.assertEqual(self.quick.labels, ["Lost S01E01"])
        # Not the episode's fault: no try counted, only moved back in the order.
        self.assertEqual(await self.attempt(11), (0, NOW.isoformat(), 0))
        self.assertIsNone(await self.attempt(12))

        # Days later the NAS is back: never tried, so never given up. All three are a backlog
        # now: two places a run, E01 (moved back) last.
        self.quick.default = FOUND
        self.now = aired + GIVE_UP_AFTER + timedelta(hours=1)
        report = await self.service.run_once()
        self.assertEqual(report.gave_up, ())
        self.assertEqual(
            [event.label for event in report.downloaded], ["Lost S01E02", "Lost S01E03"]
        )
        self.assertIsNone(report.error)
        self.now += timedelta(minutes=30)
        report = await self.service.run_once()
        self.assertEqual([event.label for event in report.downloaded], ["Lost S01E01"])

    async def test_a_failed_release_moves_on_to_the_next_episode(self) -> None:
        aired = NOW - timedelta(hours=3)
        await self.add_show(
            1,
            [
                *(episode(10 + n, 1, aired + timedelta(minutes=n), number=n) for n in (1, 2, 3)),
                still_airing(1),
            ],
            name="Lost",
        )
        self.quick.outcomes["Lost S01E01"] = ReleaseError(t(NO_LINK))
        with self.assertLogs(LOGGER, "WARNING"):
            report = await self.service.run_once()
        self.assertEqual(self.quick.labels, ["Lost S01E01", "Lost S01E02", "Lost S01E03"])
        self.assertEqual([event.label for event in report.downloaded], ["Lost S01E02", "Lost S01E03"])
        self.assertEqual(report.not_found, ("Lost S01E01",))
        self.assertIsNone(report.error)  # the run went on
        self.assertEqual(await self.attempt(11), (1, NOW.isoformat(), 0))  # its try counts
        # No AutoError: the UI shows one an hour, and it must stay free for a NAS out of reach.
        self.assertFalse(any(isinstance(event, AutoError) for event in self.events))

    async def test_a_repeated_refusal_lets_the_next_episode_through(self) -> None:
        aired = NOW - timedelta(hours=3)
        await self.add_show(
            1, [episode(11, 1, aired, number=1), episode(12, 1, aired + timedelta(minutes=1), number=2)],
            name="Lost",
        )
        refused = ReleaseRefused("qBittorrent didn't add the torrent. It may have it already.")
        self.quick.outcomes["Lost S01E01"] = refused
        self.quick.outcomes["Lost S01E02"] = NOTHING  # not out yet in the first run
        with self.assertLogs(LOGGER, "WARNING"):
            report = await self.service.run_once()
        self.assertEqual(report.not_found, ("Lost S01E01", "Lost S01E02"))
        self.assertIsNone(report.error)
        del self.quick.outcomes["Lost S01E02"]
        self.now += timedelta(minutes=30)
        with self.assertLogs(LOGGER, "WARNING"):
            report = await self.service.run_once()
        self.assertEqual([event.label for event in report.downloaded], ["Lost S01E02"])
        self.assertEqual(await self.attempt(11), (2, self.now.isoformat(), 0))

    async def test_a_program_refusing_every_release_is_reported_and_counts_no_tries(self) -> None:
        # E.g. qBittorrent's save folder is gone: "Fails." for everything it is sent.
        aired = NOW - timedelta(hours=3)
        await self.add_show(
            1, [episode(11, 1, aired, number=1), episode(12, 1, aired + timedelta(minutes=1), number=2)],
            name="Lost",
        )
        refused = "qBittorrent didn't add the torrent. It may have it already, or the save folder may not be usable."
        self.quick.default = ReleaseRefused(refused)
        with self.assertLogs(LOGGER, "WARNING"):
            report = await self.service.run_once()
        self.assertEqual(self.quick.labels, ["Lost S01E01", "Lost S01E02"])
        self.assertEqual(self.events, [AutoError(message=refused)])  # once for the run
        self.assertEqual(report.error, refused)
        self.assertEqual(report.not_found, ())
        for episode_id in (11, 12):  # never given up while the program refuses everything
            self.assertEqual(await self.attempt(episode_id), (0, NOW.isoformat(), 0))

    async def test_one_refusal_beside_a_success_counts_a_try(self) -> None:
        aired = NOW - timedelta(hours=3)
        await self.add_show(
            1, [episode(11, 1, aired, number=1), episode(12, 1, aired + timedelta(minutes=1), number=2)],
            name="Lost",
        )
        self.quick.outcomes["Lost S01E01"] = ReleaseRefused("qBittorrent says this .torrent file isn't valid.")
        with self.assertLogs(LOGGER, "WARNING"):
            report = await self.service.run_once()
        self.assertEqual([event.label for event in report.downloaded], ["Lost S01E02"])
        self.assertFalse(any(isinstance(event, AutoError) for event in self.events))
        self.assertIsNone(report.error)
        self.assertEqual(report.not_found, ("Lost S01E01",))
        self.assertEqual(await self.attempt(11), (1, NOW.isoformat(), 0))

    async def test_a_repeated_program_failure_on_one_episode_lets_the_next_through(self) -> None:
        aired = NOW - timedelta(hours=3)
        await self.add_show(
            1, [episode(11, 1, aired, number=1), episode(12, 1, aired + timedelta(minutes=1), number=2)],
            name="Lost",
        )
        self.quick.outcomes["Lost S01E01"] = RemoteError("qBittorrent at 192.168.1.20:8080 answered HTTP 500.")
        with self.assertLogs(LOGGER, "WARNING"):
            first = await self.service.run_once()
        self.assertEqual(self.quick.labels, ["Lost S01E01"])  # the run stopped there
        self.assertIsNotNone(first.error)
        self.now += timedelta(minutes=30)
        with self.assertLogs(LOGGER, "WARNING"):
            second = await self.service.run_once()
        # E01 was moved back (its last try stamped, no try counted): E02 goes first.
        self.assertEqual([event.label for event in second.downloaded], ["Lost S01E02"])
        self.assertEqual(await self.attempt(11), (0, self.now.isoformat(), 0))

    async def test_every_source_resting_stops_the_run_without_counting_tries(self) -> None:
        aired = NOW - timedelta(hours=3)
        await self.add_show(
            1, [episode(11, 1, aired, number=1), episode(12, 1, aired, number=2)], name="Lost"
        )
        self.quick.default = SourcesResting(t(SOURCES_RESTING))
        with self.assertLogs(LOGGER, "WARNING"):
            report = await self.service.run_once()
        self.assertEqual(report.error, t(SOURCES_RESTING))
        self.assertEqual(self.quick.labels, ["Lost S01E01"])
        self.assertEqual(await self.attempt(11), (0, NOW.isoformat(), 0))  # no try counted

    async def test_download_station_refusal_counts_as_a_failed_grab(self) -> None:
        await self.add_show(1, [episode(11, 1, NOW - timedelta(hours=3), number=1)], name="Lost")
        self.quick.default = DownloadStationError("Download Station refused the magnet link.")
        with self.assertLogs(LOGGER, "WARNING"):
            report = await self.service.run_once()
        self.assertEqual(report.error, "Download Station refused the magnet link.")
        self.assertEqual(
            self.events, [AutoError(message="Download Station refused the magnet link.")]
        )
        self.assertEqual(await self.attempt(11), (0, NOW.isoformat(), 0))  # no try counted

    async def test_gives_up_after_three_days_once(self) -> None:
        aired = NOW - timedelta(hours=3)
        await self.add_show(
            1,
            [episode(11, 1, aired, number=1), episode(12, 1, aired + timedelta(hours=1), number=2)],
            name="Lost",
        )
        self.quick.outcomes["Lost S01E01"] = NOTHING
        await self.service.run_once()
        self.assertEqual(self.events, [AutoDownloaded(label="Lost S01E02", quality="1080p")])

        self.now = aired + GIVE_UP_AFTER + timedelta(seconds=1)
        self.events.clear()
        calls = len(self.quick.calls)
        report = await self.service.run_once()
        self.assertEqual(report, AutoDownloadReport(gave_up=("Lost S01E01",)))
        self.assertEqual(self.events, [AutoGaveUp(label="Lost S01E01")])
        self.assertEqual(len(self.quick.calls), calls)  # not searched again
        self.assertEqual(await self.attempt(11), (1, NOW.isoformat(), 1))

        self.now += timedelta(minutes=30)
        self.assertEqual(await self.service.run_once(), AutoDownloadReport())
        self.assertEqual(self.events, [AutoGaveUp(label="Lost S01E01")])

    async def test_an_episode_missed_while_closed_is_searched_before_it_is_given_up(self) -> None:
        await self.add_show(1, [episode(11, 1, NOW - timedelta(days=4), number=1)], name="Lost")
        self.quick.default = NOTHING
        report = await self.service.run_once()
        self.assertEqual(report, AutoDownloadReport(not_found=("Lost S01E01",)))
        self.assertEqual(self.quick.labels, ["Lost S01E01"])
        self.assertEqual(await self.attempt(11), (1, NOW.isoformat(), 0))

        self.now += timedelta(minutes=30)
        report = await self.service.run_once()
        self.assertEqual(report, AutoDownloadReport(gave_up=("Lost S01E01",)))
        self.assertEqual(self.events, [AutoGaveUp(label="Lost S01E01")])  # only the new give-up
        self.assertEqual(len(self.quick.calls), 1)

    async def test_an_episode_missed_while_closed_can_still_be_downloaded(self) -> None:
        await self.add_show(1, [episode(11, 1, NOW - timedelta(days=4), number=1)], name="Lost")
        report = await self.service.run_once()
        self.assertEqual([event.label for event in report.downloaded], ["Lost S01E01"])
        self.assertEqual(report.gave_up, ())

    async def test_at_most_five_per_run_oldest_first(self) -> None:
        aired = NOW - timedelta(days=2)
        await self.add_show(
            1,
            [
                *(episode(10 + n, 1, aired + timedelta(hours=n), number=n) for n in range(1, 8)),
                still_airing(1),
            ],
            name="Lost",
        )
        report = await self.service.run_once()
        expected = [f"Lost S01E0{n}" for n in range(1, MAX_PER_RUN + 1)]
        self.assertEqual(self.quick.labels, expected)
        self.assertEqual([event.label for event in report.downloaded], expected)
        self.now += timedelta(minutes=30)
        await self.service.run_once()
        self.assertEqual(self.quick.labels, [f"Lost S01E0{n}" for n in range(1, 8)])

    async def test_episodes_that_keep_coming_back_empty_dont_block_newer_ones(self) -> None:
        aired = NOW - timedelta(days=1)
        await self.add_show(
            1,
            [
                *(episode(10 + n, 1, aired + timedelta(hours=n), number=n) for n in range(1, 7)),
                still_airing(1),
            ],
            name="Lost",
        )
        self.quick.default = NOTHING
        await self.service.run_once()  # all untried: the five oldest
        self.assertEqual(self.quick.labels, [f"Lost S01E0{n}" for n in range(1, 6)])
        self.now += timedelta(minutes=30)
        self.quick.calls.clear()
        await self.service.run_once()  # the untried one first, then the least recently tried
        self.assertEqual(self.quick.labels, [f"Lost S01E0{n}" for n in (6, 1, 2, 3, 4)])

    async def test_episode_downloaded_meanwhile_is_skipped_without_a_try(self) -> None:
        await self.add_show(1, [episode(11, 1, NOW - timedelta(hours=3), number=1)], name="Lost")
        self.quick.default = QuickDownloadResult(found=False, already_downloaded=True)
        report = await self.service.run_once()
        self.assertEqual(report, AutoDownloadReport())
        self.assertEqual(self.events, [])
        self.assertIsNone(await self.attempt(11))

    async def test_runs_never_overlap(self) -> None:
        aired = NOW - timedelta(hours=3)
        await self.add_show(
            1, [episode(11, 1, aired, number=1), episode(12, 1, aired, number=2)], name="Lost"
        )
        self.quick.default = NOTHING
        self.quick.gate = asyncio.Event()
        first = asyncio.create_task(self.service.run_once())
        second = asyncio.create_task(self.service.run_once())
        await until(lambda: len(self.quick.calls) == 1)
        for _ in range(20):
            await asyncio.sleep(0)
        self.assertEqual(len(self.quick.calls), 1)  # the second run waits for the first
        self.quick.gate.set()
        reports = await asyncio.gather(first, second)
        self.assertEqual(self.quick.max_active, 1)
        self.assertEqual(self.quick.labels, ["Lost S01E01", "Lost S01E02"] * 2)
        self.assertEqual([report.not_found for report in reports], [("Lost S01E01", "Lost S01E02")] * 2)
        self.assertEqual((await self.attempt(11) or (0,))[0], 2)

    async def test_failing_listener_is_logged_and_others_still_hear(self) -> None:
        await self.add_show(1, [episode(11, 1, NOW - timedelta(hours=3), number=1)], name="Lost")
        heard: list[AutoEvent] = []

        def broken(event: AutoEvent) -> None:
            raise RuntimeError("boom")

        self.service.subscribe(broken)
        unsubscribe = self.service.subscribe(heard.append)
        with self.assertLogs(LOGGER, "ERROR"):
            await self.service.run_once()
        self.assertEqual(heard, self.events)
        self.assertEqual(len(heard), 1)
        unsubscribe()
        unsubscribe()

    async def test_periodic_runs_start_at_once_and_survive_failures(self) -> None:
        calls = 0

        async def run_once() -> AutoDownloadReport:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("boom")
            return AutoDownloadReport()

        with (
            mock.patch.object(self.service, "run_once", side_effect=run_once),
            self.assertLogs(LOGGER, "ERROR") as logs,
        ):
            task = asyncio.create_task(self.service.run_periodic(interval_s=0))
            await until(lambda: calls >= 3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIn("Auto-download run failed", logs.output[0])

    async def test_logs_name_episodes_and_outcomes_only(self) -> None:
        await self.add_show(1, [episode(11, 1, NOW - timedelta(hours=3), number=1)], name="Lost")
        with self.assertLogs(LOGGER, "INFO") as logs:
            await self.service.run_once()
        text = "\n".join(logs.output)
        self.assertIn("Lost S01E01", text)
        self.assertNotIn("http", text)
        self.assertNotIn(FOUND.release_title or "?", text)


class SeasonPackTests(DatabaseTestCase):
    """Item 22: three or more due episodes of one season that is over are asked for as the
    season's pack first; nothing (or nothing grabbable) falls back to the episodes."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.now = NOW
        self.quick = FakeQuickDownload(self.db.history)
        self.service = AutoDownloadService(
            self.db.library,
            self.quick,
            SettingsService(self.db.settings, Settings()),
            clock=lambda: self.now,
        )
        self.events: list[AutoEvent] = []
        self.service.subscribe(self.events.append)

    async def add_season(
        self, count: int, *, season: int = 2, upcoming: int = 0, ended: bool = False
    ) -> Show:
        """"Lost": `count` episodes of `season` dropped at once 3 h ago, then `upcoming` more
        airing next week."""
        aired = NOW - timedelta(hours=3)
        episodes = [
            episode(100 + n, 1, aired + timedelta(minutes=n), season=season, number=n)
            for n in range(1, count + 1)
        ]
        episodes += [
            episode(100 + n, 1, NOW + timedelta(days=7), season=season, number=n)
            for n in range(count + 1, count + upcoming + 1)
        ]
        lost = Show(id=1, name="Lost", premiered="2004-09-22", status="Ended" if ended else None)
        await self.db.library.save_show(lost, episodes)
        await self.db.library.track(1, profile_id=1)
        await self.db.library.set_auto_download(1, True, since=WEEK_AGO, profile_id=1)
        return lost

    async def test_three_due_episodes_of_an_ended_season_get_a_pack(self) -> None:
        await self.add_season(3)
        await self.add_show(2, [episode(21, 2, NOW - timedelta(hours=4), number=1)], name="Dark")
        report = await self.service.run_once()
        # The pack is one place; the run's other places go on as before.
        self.assertEqual(self.quick.labels, ["Dark S01E01", "Lost S02"])
        request, target = self.quick.calls[1]
        self.assertEqual(request.kind, SearchKind.SEASON)
        self.assertEqual(request.episode_count, 3)
        self.assertEqual(
            target, GrabTarget(media_type=MediaType.TV, media_id=1, season=2, episode=None)
        )
        self.assertEqual(
            [event.label for event in report.downloaded], ["Dark S01E01", "Lost S02"]
        )
        self.assertEqual(report.not_found, ())
        # The pack holds them: never due again, nothing counted against them.
        self.now += timedelta(minutes=30)
        self.assertEqual(await self.service.run_once(), AutoDownloadReport())
        self.assertEqual(len(self.quick.calls), 2)
        self.assertIsNone(await self.attempt(101))

    async def test_no_pack_falls_back_to_episodes(self) -> None:
        await self.add_season(5)
        self.quick.outcomes["Lost S02"] = NOTHING
        report = await self.service.run_once()
        # The pack took one of the MAX_PER_RUN places; the episodes follow in the same run.
        self.assertEqual(
            self.quick.labels, ["Lost S02", *(f"Lost S02E0{n}" for n in range(1, MAX_PER_RUN))]
        )
        self.assertEqual(
            [event.label for event in report.downloaded],
            [f"Lost S02E0{n}" for n in range(1, MAX_PER_RUN)],
        )
        self.assertEqual(report.not_found, ())
        # The one left over: fetched on its own by the next run (no pack for one episode).
        self.now += timedelta(minutes=30)
        report = await self.service.run_once()
        self.assertEqual([event.label for event in report.downloaded], ["Lost S02E05"])
        self.assertEqual(self.quick.labels[-1], "Lost S02E05")

    async def test_a_pack_that_cant_be_grabbed_falls_back_to_episodes(self) -> None:
        await self.add_season(3)
        self.quick.outcomes["Lost S02"] = ReleaseError(t(NO_LINK))
        with self.assertLogs(LOGGER, "WARNING"):
            report = await self.service.run_once()
        self.assertEqual(
            self.quick.labels, ["Lost S02", "Lost S02E01", "Lost S02E02", "Lost S02E03"]
        )
        self.assertEqual(len(report.downloaded), 3)
        self.assertIsNone(report.error)
        self.assertFalse(any(isinstance(event, AutoError) for event in self.events))

    async def test_a_pack_and_its_episodes_all_refused_is_one_error(self) -> None:
        await self.add_season(3)
        refused = "qBittorrent didn't add the torrent. The save folder may not be usable."
        self.quick.default = ReleaseRefused(refused)
        with self.assertLogs(LOGGER, "WARNING"):
            report = await self.service.run_once()
        self.assertEqual(len(self.quick.calls), 4)
        self.assertEqual(self.events, [AutoError(message=refused)])
        self.assertEqual(report.not_found, ())
        for episode_id in (101, 102, 103):
            self.assertEqual(await self.attempt(episode_id), (0, NOW.isoformat(), 0))

    async def test_no_pack_then_every_episode_refused_is_one_error(self) -> None:
        # The save folder is gone: no pack out there, and every episode turned down.
        await self.add_season(3)
        refused = "qBittorrent didn't add the torrent. The save folder may not be usable."
        self.quick.default = ReleaseRefused(refused)
        self.quick.outcomes["Lost S02"] = NOTHING
        with self.assertLogs(LOGGER, "WARNING"):
            report = await self.service.run_once()
        self.assertEqual(self.quick.labels[0], "Lost S02")
        self.assertEqual(self.events, [AutoError(message=refused)])
        self.assertEqual(report.error, refused)
        for episode_id in (101, 102, 103):  # stamped, no try counted
            self.assertEqual(await self.attempt(episode_id), (0, NOW.isoformat(), 0))

    async def test_a_program_failure_on_a_pack_stops_the_run(self) -> None:
        await self.add_season(3)
        problem = "Can't write to the download folder \\\\NAS\\Media"
        self.quick.default = GrabError(problem)
        with self.assertLogs(LOGGER, "WARNING"):
            report = await self.service.run_once()
        self.assertEqual(self.quick.labels, ["Lost S02"])
        self.assertEqual(report.error, problem)
        self.assertEqual(self.events, [AutoError(message=problem)])
        for episode_id in (101, 102, 103):  # stamped, no try counted
            self.assertEqual(await self.attempt(episode_id), (0, NOW.isoformat(), 0))

    async def test_a_season_still_airing_gets_no_pack(self) -> None:
        await self.add_season(3, upcoming=1)
        report = await self.service.run_once()
        self.assertEqual(self.quick.labels, ["Lost S02E01", "Lost S02E02", "Lost S02E03"])
        self.assertEqual(len(report.downloaded), 3)

    async def test_two_due_episodes_get_no_pack(self) -> None:
        await self.add_season(2)
        await self.service.run_once()
        self.assertEqual(self.quick.labels, ["Lost S02E01", "Lost S02E02"])

    async def test_an_ended_show_gets_a_pack_despite_an_undated_episode(self) -> None:
        lost = await self.add_season(3, ended=True)
        undated = Episode(id=199, show_id=1, season=2, number=4)  # TVMaze never dated it
        await self.db.library.save_show(lost, [*await self.db.library.episodes(1), undated])
        await self.service.run_once()
        self.assertEqual(self.quick.labels, ["Lost S02"])

    async def test_a_later_season_on_air_means_this_one_ended(self) -> None:
        lost = await self.add_season(3)
        undated = Episode(id=199, show_id=1, season=2, number=4)
        later = episode(201, 1, NOW + timedelta(days=30), season=3, number=1)
        aired_later = episode(202, 1, NOW - timedelta(days=30), season=3, number=2)
        await self.db.library.save_show(
            lost, [*await self.db.library.episodes(1), undated, later, aired_later]
        )
        await self.service.run_once()
        self.assertEqual(self.quick.labels[0], "Lost S02")


class ServicesWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_services_offer_the_automatic_downloads(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        asyncio.get_running_loop().slow_callback_duration = 1.0  # httpx loads its CA bundle
        network = AssertionError("no network calls")
        with mock.patch.object(httpx.AsyncClient, "send", side_effect=network):
            services = await Services.create(AppPaths(Path(tmp.name)))
        try:
            self.assertIsInstance(services.auto_download, AutoDownloadService)
            self.assertEqual(await services.auto_download.run_once(), AutoDownloadReport())
        finally:
            await services.aclose()


if __name__ == "__main__":
    unittest.main()
