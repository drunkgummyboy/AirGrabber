"""Missing episodes: the latest-season rule, its SQL twin (missing counts), the v4
`have_episodes` table, LibraryService's marks and details, "Download missing" runs against a
fake one-click Download, and the UI's pure helpers."""

from __future__ import annotations

import asyncio
import contextlib
import gc
import sqlite3
import tempfile
import tkinter as tk
import unittest
from typing import Any
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import customtkinter as ctk
import httpx

from app.database import MIGRATIONS, Database
from app.models import (
    CalendarEntry,
    DownloadRecord,
    Episode,
    GrabOutcome,
    MediaType,
    MissingProgress,
    MissingRunReport,
    QuickDownloadResult,
    Show,
    ShowDetails,
)
from app.services.downloads import GrabError
from app.services.library import LibraryService, downloaded_ids
from app.services.missing import (
    MAX_PER_RUN,
    MissingDownloadService,
    MissingRunBusy,
    latest_season,
    missing_episodes,
)
from app.services.synology import DownloadStationError
from app.services.tvmaze import TVMazeClient
from app.ui.cards import ShowCard
from app.ui.missing_control import (
    FAILED_TEXT,
    NOTHING_MISSING_TEXT,
    MissingActions,
    MissingButton,
    MissingRuns,
    MissingRunState,
    download_missing_text,
    have_status_text,
    missing_look,
    menu_position,
    missing_text,
    outcome_status_text,
    popup_menu,
    outcome_texts,
    progress_texts,
)
from app.ui.show_details import row_mark, season_marks, season_menu_items
from tests.test_auto_download import FOUND, NOTHING, FakeQuickDownload

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
DAY = timedelta(days=1)


def ep(
    episode_id: int,
    season: int,
    number: int | None,
    *,
    aired: datetime | None = None,
    air_date: date | None = None,
    show_id: int = 1,
    kind: str = "regular",
) -> Episode:
    """An episode airing at `aired` (its air date that day), or only on `air_date`."""
    return Episode(
        id=episode_id,
        show_id=show_id,
        season=season,
        number=number,
        type=kind,
        airstamp=aired,
        air_date=air_date if air_date is not None else (aired.date() if aired else None),
    )


def ids(episodes: list[Episode]) -> list[int]:
    return [episode.id for episode in episodes]


def missing_ids(
    episodes: list[Episode],
    *,
    grabs: frozenset[tuple[int, int | None]] = frozenset(),
    have: frozenset[int] = frozenset(),
    now: datetime = NOW,
) -> list[int]:
    downloaded = downloaded_ids(episodes, grabs)
    return ids(missing_episodes(episodes, downloaded=downloaded, have=have, now=now))


# -- the rule ----------------------------------------------------------------------------------


class LatestSeasonTests(unittest.TestCase):
    def test_highest_season_with_an_aired_episode(self) -> None:
        episodes = [ep(1, 1, 1, aired=NOW - 400 * DAY), ep(2, 2, 1, aired=NOW - 10 * DAY)]
        self.assertEqual(latest_season(episodes, NOW), 2)

    def test_a_new_season_whose_first_episode_has_not_aired_leaves_the_previous_one(self) -> None:
        episodes = [
            ep(1, 1, 1, aired=NOW - 20 * DAY),
            ep(2, 1, 2, aired=NOW - 13 * DAY),
            ep(3, 2, 1, aired=NOW + 3 * DAY),
        ]
        self.assertEqual(latest_season(episodes, NOW), 1)
        self.assertEqual(missing_ids(episodes), [1, 2])

    def test_no_aired_episodes_no_latest_season_nothing_missing(self) -> None:
        episodes = [ep(1, 1, 1, aired=NOW + DAY), ep(2, 1, 2)]  # one upcoming, one without dates
        self.assertIsNone(latest_season(episodes, NOW))
        self.assertEqual(missing_ids(episodes), [])
        self.assertEqual(missing_ids([]), [])

    def test_specials_never_count(self) -> None:
        episodes = [
            ep(1, 1, 1, aired=NOW - 30 * DAY),
            ep(2, 2, None, aired=NOW - 5 * DAY),  # unnumbered special in season 2
            ep(3, 0, 1, aired=NOW - 4 * DAY),  # season 0
            ep(4, 3, 1, aired=NOW - 3 * DAY, kind="significant_special"),
        ]
        self.assertEqual(latest_season(episodes, NOW), 1)
        self.assertEqual(missing_ids(episodes), [1])

    def test_episodes_without_air_dates_are_not_aired(self) -> None:
        episodes = [ep(1, 1, 1, aired=NOW - 7 * DAY), ep(2, 1, 2)]
        self.assertEqual(missing_ids(episodes), [1])

    def test_air_date_without_airstamp_counts_from_that_day(self) -> None:
        episodes = [ep(1, 1, 1, air_date=NOW.date()), ep(2, 1, 2, air_date=NOW.date() + DAY)]
        self.assertEqual(missing_ids(episodes), [1])

    def test_the_airstamp_decides_to_the_minute(self) -> None:
        episodes = [ep(1, 1, 1, aired=NOW), ep(2, 1, 2, aired=NOW + timedelta(minutes=1))]
        self.assertEqual(missing_ids(episodes), [1])

    def test_nows_time_zone_decides_today_for_air_dates(self) -> None:
        # 01:00 UTC on the 19th is still the 18th in New York: an air date of the 19th waits.
        now = datetime(2026, 10, 19, 1, 0, tzinfo=UTC).astimezone(timezone(timedelta(hours=-4)))
        episodes = [ep(1, 1, 1, air_date=date(2026, 10, 19))]
        self.assertEqual(missing_ids(episodes, now=now), [])
        self.assertEqual(missing_ids(episodes, now=datetime(2026, 10, 19, 1, 0, tzinfo=UTC)), [1])


class MissingEpisodesTests(unittest.TestCase):
    def setUp(self) -> None:
        self.episodes = [
            ep(10, 1, 1, aired=NOW - 400 * DAY),
            ep(21, 2, 1, aired=NOW - 21 * DAY),
            ep(22, 2, 2, aired=NOW - 14 * DAY),
            ep(23, 2, 3, aired=NOW - 7 * DAY),
            ep(24, 2, 4, aired=NOW + 7 * DAY),
        ]

    def test_only_the_latest_season_aired_and_oldest_first(self) -> None:
        self.assertEqual(missing_ids(list(reversed(self.episodes))), [21, 22, 23])

    def test_downloaded_episodes_are_not_missing(self) -> None:
        self.assertEqual(missing_ids(self.episodes, grabs=frozenset({(2, 2)})), [21, 23])

    def test_a_season_pack_covers_the_season(self) -> None:
        self.assertEqual(missing_ids(self.episodes, grabs=frozenset({(2, None)})), [])
        # A pack of an older season doesn't help the latest one.
        self.assertEqual(missing_ids(self.episodes, grabs=frozenset({(1, None)})), [21, 22, 23])

    def test_marked_have_is_not_missing(self) -> None:
        self.assertEqual(missing_ids(self.episodes, have=frozenset({21, 23})), [22])


# -- database ----------------------------------------------------------------------------------


class DatabaseTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = await Database.open(Path(self.tmp.name) / "library.db")

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    async def add_show(
        self, show_id: int, episodes: list[Episode], *, tracked: bool = True, name: str = ""
    ) -> Show:
        show = Show(id=show_id, name=name or f"Show {show_id}")
        await self.db.library.save_show(show, episodes)
        if tracked:
            await self.db.library.track(show_id)
        return show

    async def grab(self, show_id: int, season: int, number: int | None) -> None:
        await self.db.history.add(
            DownloadRecord(
                media_type=MediaType.TV,
                media_id=show_id,
                season=season,
                episode=number,
                release_title=f"Show.{show_id}.S{season:02d}",
            )
        )


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    def test_the_have_migration_is_v4_and_self_contained(self) -> None:
        self.assertGreaterEqual(len(MIGRATIONS), 5)
        script = MIGRATIONS[3]
        self.assertIn("CREATE TABLE have_episodes", script)
        self.assertNotIn("ALTER TABLE", script)

    async def test_v3_database_is_upgraded_and_keeps_its_data(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "v3.db"
        with contextlib.closing(sqlite3.connect(path)) as raw:
            script = "\n".join(MIGRATIONS[:3])
            raw.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = 3;\nCOMMIT;")
            with raw:
                raw.execute("INSERT INTO shows (id, name) VALUES (1, 'Kept')")
        db = await Database.open(path)
        self.addAsyncCleanup(db.close)
        version = await db.fetch_one("PRAGMA user_version")
        assert version is not None
        self.assertEqual(version[0], len(MIGRATIONS))
        self.assertIsNotNone(await db.library.get_show(1))
        tables = {row["name"]: row["strict"] for row in await db.fetch_all("PRAGMA table_list")}
        self.assertEqual(tables["have_episodes"], 1)


    async def test_v5_counts_what_already_aired_as_had(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "v4.db"
        past = (datetime.now(UTC) - 3 * DAY).isoformat()
        future = (datetime.now(UTC) + 3 * DAY).isoformat()
        with contextlib.closing(sqlite3.connect(path)) as raw:
            script = "\n".join(MIGRATIONS[:4])
            raw.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = 4;\nCOMMIT;")
            with raw:
                raw.execute("INSERT INTO shows (id, name) VALUES (1, 'Tracked'), (2, 'Not')")
                raw.executemany(
                    "INSERT INTO episodes (id, show_id, season, number, type, airstamp) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (11, 1, 1, 1, "regular", past),  # aired: had
                        (12, 1, 1, 2, "regular", past),  # aired and downloaded
                        (13, 1, 1, 3, "regular", future),  # still to come
                        (14, 1, 0, None, "significant_special", past),  # a special
                        (21, 2, 1, 1, "regular", past),  # not tracked
                    ],
                )
                raw.execute(
                    "INSERT INTO tracked_shows (show_id, tracked_at) VALUES (1, ?)", (past,)
                )
                raw.execute(
                    "INSERT INTO download_history (media_type, media_id, season, episode, "
                    "release_title, downloaded_at) VALUES ('tv', 1, 1, 2, 'Tracked.S01E02', ?)",
                    (past,),
                )
        db = await Database.open(path)
        self.addAsyncCleanup(db.close)
        self.assertEqual(await db.library.have_ids(1), frozenset({11}))
        self.assertEqual(await db.library.have_ids(2), frozenset())


class HaveRepositoryTests(DatabaseTestCase):
    async def test_mark_aired_have_marks_only_aired_regular_episodes_not_downloaded(self) -> None:
        episodes = [
            ep(11, 1, 1, aired=NOW - 9 * DAY),
            ep(12, 1, 2, aired=NOW - 2 * DAY),
            ep(13, 1, 3, air_date=(NOW - DAY).date()),  # no airstamp: by the air date
            ep(14, 1, 4, aired=NOW + DAY),
            ep(15, 0, None, aired=NOW - DAY, kind="significant_special"),
        ]
        await self.add_show(1, episodes)
        await self.grab(1, 1, 2)
        marked = await self.db.library.mark_aired_have(1, now=NOW, today=NOW.date(), when=NOW)
        self.assertEqual(marked, 2)
        self.assertEqual(await self.db.library.have_ids(1), frozenset({11, 13}))
        self.assertEqual(await self.db.library.missing_counts(now=NOW, today=NOW.date()), {})

    async def test_mark_and_unmark(self) -> None:
        await self.add_show(1, [ep(1, 1, 1, aired=NOW), ep(2, 1, 2, aired=NOW)])
        self.assertEqual(await self.db.library.mark_have([1, 2, 1, 999], NOW), 2)  # 999 unknown
        self.assertEqual(await self.db.library.mark_have([1], NOW), 0)  # already marked
        self.assertEqual(await self.db.library.have_ids(1), frozenset({1, 2}))
        self.assertEqual(await self.db.library.unmark_have([2, 3]), 1)
        self.assertEqual(await self.db.library.have_ids(1), frozenset({1}))

    async def test_have_ids_are_per_show(self) -> None:
        await self.add_show(1, [ep(1, 1, 1, aired=NOW)])
        await self.add_show(2, [ep(5, 1, 1, aired=NOW, show_id=2)])
        await self.db.library.mark_have([1, 5], NOW)
        self.assertEqual(await self.db.library.have_ids(2), frozenset({5}))

    async def test_a_mark_goes_with_its_episode_and_survives_a_refresh(self) -> None:
        show = await self.add_show(1, [ep(1, 1, 1, aired=NOW), ep(2, 1, 2, aired=NOW)])
        await self.db.library.mark_have([1, 2], NOW)
        await self.db.library.save_show(show, [ep(1, 1, 1, aired=NOW)])  # episode 2 is gone
        self.assertEqual(await self.db.library.have_ids(1), frozenset({1}))
        row = await self.db.fetch_one("SELECT COUNT(*) FROM have_episodes")
        assert row is not None
        self.assertEqual(row[0], 1)


class MissingCountsTests(DatabaseTestCase):
    """The SQL counts agree with the Python rule, show by show."""

    async def counts(self, now: datetime = NOW) -> dict[int, int]:
        return await self.db.library.missing_counts(now=now, today=now.date())

    async def python_counts(self, now: datetime = NOW) -> dict[int, int]:
        result: dict[int, int] = {}
        for show_id in await self.db.library.tracked_ids():
            episodes = await self.db.library.episodes(show_id)
            downloaded = downloaded_ids(episodes, await self.db.history.show_grabs(show_id))
            have = await self.db.library.have_ids(show_id)
            count = len(missing_episodes(episodes, downloaded=downloaded, have=have, now=now))
            if count:
                result[show_id] = count
        return result

    async def test_counts_match_the_rule_on_every_edge_case(self) -> None:
        # 1: latest season 2 (season 3 not aired yet), one episode grabbed, one marked.
        await self.add_show(
            1,
            [
                ep(11, 1, 1, aired=NOW - 400 * DAY),
                ep(21, 2, 1, aired=NOW - 21 * DAY),
                ep(22, 2, 2, aired=NOW - 14 * DAY),
                ep(23, 2, 3, aired=NOW - 7 * DAY),
                ep(24, 2, 4, aired=NOW - 1 * DAY),
                ep(25, 2, None, aired=NOW - 2 * DAY),
                ep(31, 3, 1, aired=NOW + 30 * DAY),
            ],
        )
        await self.grab(1, 2, 2)
        await self.db.library.mark_have([23], NOW)
        # 2: a season pack of its latest season: nothing missing.
        await self.add_show(2, [ep(41, 1, 1, aired=NOW - DAY, show_id=2)])
        await self.grab(2, 1, None)
        # 3: nothing aired, one without dates.
        await self.add_show(3, [ep(51, 1, 1, aired=NOW + DAY, show_id=3), ep(52, 1, 2, show_id=3)])
        # 4: air dates only, one today and one tomorrow; a season-0 episode and a special type.
        await self.add_show(
            4,
            [
                ep(61, 1, 1, air_date=NOW.date(), show_id=4),
                ep(62, 1, 2, air_date=NOW.date() + DAY, show_id=4),
                ep(63, 0, 1, aired=NOW - DAY, show_id=4),
                ep(64, 2, 1, aired=NOW - DAY, show_id=4, kind="insignificant_special"),
            ],
        )
        # 5: untracked: never counted.
        await self.add_show(5, [ep(71, 1, 1, aired=NOW - DAY, show_id=5)], tracked=False)

        counts = await self.counts()
        self.assertEqual(counts, {1: 2, 4: 1})
        self.assertEqual(counts, await self.python_counts())
        later = NOW + 40 * DAY  # season 3 of show 1 has aired; show 3's first episode too
        self.assertEqual(await self.counts(later), await self.python_counts(later))
        self.assertEqual((await self.counts(later))[1], 1)

    async def test_the_airstamp_boundary_matches(self) -> None:
        await self.add_show(1, [ep(1, 1, 1, aired=NOW), ep(2, 1, 2, aired=NOW + timedelta(seconds=1))])
        self.assertEqual(await self.counts(), {1: 1})
        self.assertEqual(await self.counts(), await self.python_counts())


# -- the library service ---------------------------------------------------------------------


def _no_network(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404)


class ServiceTestCase(DatabaseTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(_no_network))
        self.library = LibraryService(
            self.db.library, self.db.history, TVMazeClient(self.http, retry_delay=0)
        )
        self.notifications = 0

        def changed() -> None:
            self.notifications += 1

        self.library.subscribe(changed)

    async def asyncTearDown(self) -> None:
        await self.http.aclose()
        await super().asyncTearDown()

    async def severance(self, count: int = 3) -> Show:
        """Season 1 aired long ago; season 2 has `count` aired episodes and one to come."""
        episodes = [ep(10, 1, 1, aired=NOW - 400 * DAY)]
        episodes += [
            ep(20 + n, 2, n, aired=NOW - (count - n + 1) * DAY) for n in range(1, count + 1)
        ]
        episodes.append(ep(99, 2, count + 1, aired=NOW + 7 * DAY))
        return await self.add_show(1, episodes, name="Severance")


class LibraryMissingTests(ServiceTestCase):
    async def test_show_details_of_a_tracked_show(self) -> None:
        await self.severance()
        await self.grab(1, 2, 1)
        await self.library.mark_have([22])
        details = await self.library.show_details(1, now=NOW)
        self.assertEqual(details.latest_season, 2)
        self.assertEqual(details.missing, frozenset({23}))
        self.assertEqual(details.have, frozenset({22}))
        self.assertTrue(details.has_it(details.episodes[2]))

    async def test_a_newly_followed_show_starts_with_nothing_missing(self) -> None:
        now = datetime.now(UTC)
        show = Show(id=7, name="New")
        episodes = [ep(71, 1, 1, aired=now - 2 * DAY, show_id=7)]
        episodes.append(ep(72, 1, 2, aired=now + 2 * DAY, show_id=7))

        class _TVMaze(TVMazeClient):
            async def get_show_with_episodes(self, show_id: int) -> tuple[Show, list[Episode]]:
                return show, episodes

        library = LibraryService(self.db.library, self.db.history, _TVMaze(self.http))
        await library.track(7)
        self.assertEqual(await self.db.library.have_ids(7), frozenset({71}))
        await library.unmark_have([71])
        await library.track(7)  # following it again (a refresh) doesn't mark it again
        self.assertEqual(await self.db.library.have_ids(7), frozenset())
        self.assertEqual(await library.missing_counts(now=now + 3 * DAY), {7: 2})

    async def test_an_untracked_show_has_nothing_missing(self) -> None:
        await self.severance()
        await self.db.library.untrack(1)
        self.assertEqual(await self.library.missing_episodes(1, now=NOW), [])
        self.assertEqual(await self.library.missing_counts(now=NOW), {})

    async def test_marks_notify_and_change_the_counts(self) -> None:
        await self.severance()
        self.assertEqual(await self.library.missing_counts(now=NOW), {1: 3})
        await self.library.mark_have([21, 22, 23])
        self.assertEqual(await self.library.missing_counts(now=NOW), {})
        await self.library.unmark_have([22])
        self.assertEqual(await self.library.missing_counts(now=NOW), {1: 1})
        self.assertEqual(self.notifications, 2)

    async def test_missing_episodes_oldest_first_with_the_show(self) -> None:
        await self.severance()
        entries = await self.library.missing_episodes(1, now=NOW)
        self.assertEqual([entry.episode.code for entry in entries], ["S02E01", "S02E02", "S02E03"])
        self.assertTrue(all(entry.show.name == "Severance" for entry in entries))

    async def test_an_episode_due_for_auto_download_is_missing_until_grabbed(self) -> None:
        await self.severance()
        await self.db.library.set_auto_download(1, True, since=NOW - 30 * DAY)
        due = await self.db.library.auto_due(
            aired_from=NOW - 10 * DAY, aired_until=NOW, limit=10
        )
        self.assertIn(23, [entry.episode.id for entry in due])
        self.assertIn(23, ids([e.episode for e in await self.library.missing_episodes(1, now=NOW)]))
        await self.grab(1, 2, 3)
        self.assertNotIn(
            23, ids([e.episode for e in await self.library.missing_episodes(1, now=NOW)])
        )


# -- "Download missing" ------------------------------------------------------------------------


class _PinnedLibrary(LibraryService):
    """`missing_episodes` as of NOW (the run asks without a `now`)."""

    async def missing_episodes(
        self, show_id: int, *, now: datetime | None = None
    ) -> list[CalendarEntry]:
        return await super().missing_episodes(show_id, now=NOW)


class DownloadMissingTests(ServiceTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.library = _PinnedLibrary(
            self.db.library, self.db.history, TVMazeClient(self.http, retry_delay=0)
        )
        self.quick = FakeQuickDownload(self.db.history)
        self.service = MissingDownloadService(self.library, self.quick)
        self.steps: list[MissingProgress] = []

    async def run_missing(self, **kwargs: int) -> MissingRunReport:
        return await self.service.download_missing(1, self.steps.append, **kwargs)

    async def test_fetches_oldest_first_and_carries_on_past_nothing_found(self) -> None:
        await self.severance()
        self.quick.outcomes["Severance S02E02"] = NOTHING
        report = await self.run_missing()
        self.assertEqual(self.quick.labels, ["Severance S02E01", "Severance S02E02", "Severance S02E03"])
        self.assertEqual(report.downloaded, ("Severance S02E01", "Severance S02E03"))
        self.assertEqual(report.not_found, ("Severance S02E02",))
        self.assertEqual((report.total, report.left_over, report.error), (3, 0, None))
        self.assertEqual(await self.library.missing_counts(now=NOW), {1: 1})
        # Each episode is announced, then finished.
        self.assertEqual(
            [(s.index, s.total, s.result is not None) for s in self.steps],
            [(1, 3, False), (1, 3, True), (2, 3, False), (2, 3, True), (3, 3, False), (3, 3, True)],
        )
        target = self.quick.calls[0][1]
        self.assertEqual((target.media_id, target.season, target.episode), (1, 2, 1))

    async def test_a_failed_grab_stops_the_run(self) -> None:
        await self.severance()
        for error in (GrabError("No torrent program"), DownloadStationError("NAS refused")):
            with self.subTest(error=type(error).__name__):
                self.quick.calls.clear()
                self.steps.clear()
                self.quick.outcomes = {"Severance S02E02": error}
                self.quick.default = NOTHING
                with self.assertLogs("app.services.missing", "WARNING"):
                    report = await self.run_missing()
                self.assertEqual(report.error, str(error))
                self.assertEqual(len(self.quick.calls), 2)
                self.assertTrue(self.steps[-1].failed)
                self.assertFalse(self.service.is_running(1))

    async def test_already_downloaded_meanwhile_is_counted_apart(self) -> None:
        await self.severance(1)
        self.quick.default = QuickDownloadResult(found=False, already_downloaded=True)
        report = await self.run_missing()
        self.assertEqual((report.already, report.downloaded), (("Severance S02E01",), ()))

    async def test_at_most_the_cap_per_run(self) -> None:
        await self.severance(MAX_PER_RUN + 3)
        report = await self.run_missing()
        self.assertEqual((report.total, report.left_over), (MAX_PER_RUN, 3))
        self.assertEqual(len(self.quick.calls), MAX_PER_RUN)
        report = await self.run_missing(limit=2)
        self.assertEqual((report.total, report.left_over, len(report.downloaded)), (2, 1, 2))

    async def test_one_run_per_show(self) -> None:
        await self.severance()
        self.quick.gate = gate = asyncio.Event()
        first = asyncio.create_task(self.run_missing())
        while not self.quick.calls:
            await asyncio.sleep(0.001)
        self.assertTrue(self.service.is_running(1))
        with self.assertRaises(MissingRunBusy):
            await self.service.download_missing(1)
        gate.set()
        report = await first
        self.assertEqual(len(report.downloaded), 3)
        self.assertFalse(self.service.is_running(1))

    async def test_nothing_missing_or_untracked_is_an_empty_run(self) -> None:
        report = await self.service.download_missing(42)
        self.assertEqual(report, MissingRunReport(show_id=42))
        self.assertEqual(self.quick.calls, [])

    async def test_a_failing_progress_listener_does_not_stop_the_run(self) -> None:
        await self.severance(2)

        def broken(step: MissingProgress) -> None:
            raise RuntimeError("boom")

        with self.assertLogs("app.services.missing", "ERROR"):
            report = await self.service.download_missing(1, broken)
        self.assertEqual(len(report.downloaded), 2)

    async def test_found_result_is_reported_in_the_step(self) -> None:
        await self.severance(1)
        await self.run_missing()
        result = self.steps[-1].result
        assert result is not None
        self.assertEqual(result, FOUND)
        assert result.grab is not None
        self.assertIs(result.grab.outcome, GrabOutcome.SAVED)


# -- UI helpers --------------------------------------------------------------------------------


def report(**fields: object) -> MissingRunReport:
    return MissingRunReport.model_validate({"show_id": 1, **fields})


class TextTests(unittest.TestCase):
    def test_counts_and_button(self) -> None:
        self.assertEqual(missing_text(3), "3 missing")
        self.assertEqual(download_missing_text(3), "⬇ Download missing (3)")
        self.assertEqual(download_missing_text(), "⬇ Download missing")

    def test_progress(self) -> None:
        self.assertEqual(progress_texts(2, 3)[0], "Downloading 2 of 3…")
        self.assertEqual(progress_texts(0, 0)[0], "Finding missing episodes…")

    def test_outcomes(self) -> None:
        mixed = report(total=3, downloaded=("a", "b"), not_found=("c",))
        self.assertEqual(outcome_texts(mixed)[0], "✓ 2 downloaded · 1 not found yet")
        self.assertEqual(outcome_texts(report(downloaded=("a",), already=("b",)))[0], "✓ 2 downloaded")
        self.assertEqual(outcome_texts(report(not_found=("a", "b")))[0], "2 not found yet")
        self.assertEqual(outcome_texts(report(error="x"))[0], FAILED_TEXT)
        self.assertEqual(outcome_texts(report(downloaded=("a",), error="x"))[0], "✓ 1 downloaded · stopped")
        self.assertEqual(outcome_texts(report()), (NOTHING_MISSING_TEXT,))

    def test_status_texts(self) -> None:
        text = outcome_status_text(
            "Severance", report(downloaded=("a",), not_found=("b",), left_over=4, error="No folder")
        )
        self.assertEqual(
            text,
            "Severance: 1 downloaded · 1 not found yet · 4 more missing: download missing again "
            "for those · stopped: No folder",
        )
        self.assertEqual(outcome_status_text("X", report()), "X: nothing missing")
        self.assertEqual(have_status_text("X", 1, have=True), "X: 1 episode marked as already had")
        self.assertEqual(
            have_status_text("X", 3, have=False), "X: 3 episodes marked as missing again"
        )


class LookTests(unittest.TestCase):
    def test_nothing_missing_and_no_run_is_hidden(self) -> None:
        self.assertIsNone(missing_look(0, None, with_count=True))

    def test_the_download_button(self) -> None:
        look = missing_look(3, None, with_count=True)
        assert look is not None
        self.assertEqual((look.layout, look.text, look.action), ("button", "⬇ Download missing (3)", "download"))
        card = missing_look(3, None, with_count=False)
        assert card is not None
        self.assertEqual(card.text, "⬇ Download missing")

    def test_a_working_run_wins(self) -> None:
        look = missing_look(0, MissingRunState(2, 3), with_count=True)
        assert look is not None
        self.assertEqual((look.layout, look.text, look.action), ("label", "Downloading 2 of 3…", None))

    def test_a_finished_run_starts_another_only_while_episodes_are_missing(self) -> None:
        done = MissingRunState(3, 3, report(total=3, downloaded=("a", "b", "c")))
        look = missing_look(0, done, with_count=True)
        assert look is not None
        self.assertEqual((look.text, look.action), ("✓ 3 downloaded", None))
        partly = MissingRunState(3, 3, report(total=3, downloaded=("a",), not_found=("b", "c")))
        look = missing_look(2, partly, with_count=True)
        assert look is not None
        self.assertEqual(look.action, "download")

    def test_run_states_expire_only_when_unchanged(self) -> None:
        runs = MissingRuns()
        done = MissingRunState(1, 1, report(total=1))
        runs.set(1, done)
        self.assertFalse(runs.is_working(1))
        newer = MissingRunState()
        runs.set(1, newer)
        runs.expire(1, done)
        self.assertIs(runs.get(1), newer)
        self.assertTrue(runs.is_working(1))
        runs.set(1, done)
        runs.expire(1, done)
        self.assertIsNone(runs.get(1))


class ShowWindowHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.episodes = (
            ep(10, 1, 1, aired=NOW - 400 * DAY),
            ep(21, 2, 1, aired=NOW - 14 * DAY),
            ep(22, 2, 2, aired=NOW - 7 * DAY),
            ep(23, 2, 3, aired=NOW - 1 * DAY),
            ep(24, 2, 4, aired=NOW + 7 * DAY),
        )

    def details(self, *, tracked: bool = True) -> ShowDetails:
        return ShowDetails(
            show=Show(id=1, name="Severance"),
            episodes=self.episodes,
            tracked=tracked,
            downloaded=frozenset({23}),
            latest_season=2 if tracked else None,
            missing=frozenset({21}) if tracked else frozenset(),
            have=frozenset({22, 10}),
        )

    def test_row_marks_on_the_latest_season_only(self) -> None:
        details = self.details()
        marks = {episode.id: row_mark(details, episode) for episode in self.episodes}
        self.assertEqual(marks, {10: None, 21: "missing", 22: "have", 23: None, 24: None})
        untracked = self.details(tracked=False)
        self.assertTrue(all(row_mark(untracked, e) is None for e in self.episodes))

    def test_season_marks_and_menu(self) -> None:
        details = self.details()
        self.assertEqual(season_marks(details, 2), ([21], [22]))
        self.assertEqual(season_marks(details, 1), ([], []))
        self.assertEqual(season_marks(details, None), ([], []))
        self.assertEqual(
            season_menu_items([21], [22]),
            [("I already have this season", "mark"), ("Mark as missing again", "unmark")],
        )
        self.assertEqual(season_menu_items([], []), [])


class MenuPositionTests(unittest.TestCase):
    WINDOW = (100, 50, 1300, 1000)  # x, y, width, height

    def test_below_the_anchor_right_edges_aligned(self) -> None:
        self.assertEqual(menu_position((500, 100, 40, 40), (200, 80), self.WINDOW), (340, 142))

    def test_kept_within_the_window(self) -> None:
        self.assertEqual(menu_position((110, 100, 40, 40), (200, 80), self.WINDOW), (100, 142))
        self.assertEqual(menu_position((1390, 100, 40, 40), (200, 80), self.WINDOW)[0], 1200)

    def test_above_when_there_is_no_room_below(self) -> None:
        self.assertEqual(menu_position((500, 1000, 40, 30), (200, 80), self.WINDOW), (340, 918))
        # No room above either: below it stays.
        self.assertEqual(menu_position((500, 60, 40, 30), (200, 2000), self.WINDOW), (340, 92))


class _FakeImages:
    async def get(self, *args: object, **kwargs: object) -> None:
        return None


class WidgetTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        self.started: list[int] = []
        self.marked: list[tuple[int, ...]] = []
        self.runs = MissingRuns()
        self.actions = MissingActions(
            download=lambda show: self.started.append(show.id),
            mark_have=lambda show, ids: self.marked.append(ids),
            unmark_have=lambda show, ids: None,
            runs=self.runs,
        )
        self.show = Show(id=7, name="Severance")

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    def button(self, count: int) -> MissingButton:
        button = MissingButton(
            self.root, show=self.show, count=count, actions=self.actions, with_count=True,
            height=36, font_size=14, width=250,
        )
        button.pack()
        self.root.update()
        return button

    def test_the_button_follows_the_run_and_survives_a_rebuild(self) -> None:
        button = self.button(3)
        assert button.look is not None
        self.assertEqual(button.look.text, "⬇ Download missing (3)")
        button._click()
        self.assertEqual(self.started, [7])
        self.runs.set(7, MissingRunState(1, 3))
        self.assertEqual(button.look.text, "Downloading 1 of 3…")
        button.destroy()  # the page reloads mid-run
        rebuilt = self.button(2)
        assert rebuilt.look is not None
        self.assertEqual(rebuilt.look.text, "Downloading 1 of 3…")
        rebuilt._click()  # working: no second run
        self.assertEqual(self.started, [7])
        done = MissingRunState(3, 3, report(total=3, downloaded=("a", "b", "c")))
        self.runs.set(7, done)
        self.assertEqual(rebuilt.look.text, "✓ 3 downloaded")
        rebuilt.set_count(0)
        self.runs.expire(7, done)
        self.assertIsNone(rebuilt.look)  # nothing missing, nothing to tell

    def test_a_tracked_card_shows_the_missing_row(self) -> None:
        images: Any = _FakeImages()
        card = ShowCard(
            self.root, self.show, images=images, on_auto_download=lambda s, on: None,
            missing=3, missing_actions=self.actions,
        )
        card.pack()
        empty = ShowCard(
            self.root, Show(id=8, name="Silo"), images=images, on_auto_download=lambda s, on: None,
            missing=0, missing_actions=self.actions,
        )
        empty.pack()
        plain = ShowCard(self.root, self.show, images=images, on_auto_download=lambda s, on: None)
        plain.pack()
        self.root.update()
        assert card.missing_button is not None and card.missing_button.look is not None
        self.assertEqual(card.missing_button.look.action, "download")
        self.assertTrue(card.missing_button.look.text.startswith("⬇"))
        assert empty.missing_button is not None
        self.assertIsNone(empty.missing_button.look)
        self.assertEqual(card.cget("height"), empty.cget("height"))  # equally tall
        self.assertGreater(card.cget("height"), plain.cget("height"))
        self.assertIsNone(plain.missing_button)

    def test_popup_menu_runs_the_choice_and_closes(self) -> None:
        anchor = ctk.CTkButton(self.root, text="⋯")
        anchor.pack()
        self.root.update()
        self.assertIsNone(popup_menu(anchor, []))
        menu = popup_menu(anchor, [("I already have this", lambda: self.marked.append((1,)))])
        assert menu is not None
        self.assertEqual(menu.labels, ("I already have this",))
        second = popup_menu(anchor, [("Mark as missing again", lambda: None)])
        assert second is not None
        self.assertFalse(menu.is_open)  # one at a time
        second.close()
        menu = popup_menu(anchor, [("I already have this", lambda: self.marked.append((1,)))])
        assert menu is not None
        menu.buttons[0].invoke()
        self.assertEqual(self.marked, [(1,)])
        self.assertFalse(menu.is_open)
        menu = popup_menu(anchor, [("x", lambda: None)])
        assert menu is not None
        anchor.destroy()
        self.assertFalse(menu.is_open)


if __name__ == "__main__":
    unittest.main()
