"""Missing episodes: the latest-season rule, its SQL twin (missing counts), the v4
`have_episodes` table (kept, no longer read), LibraryService's details with and without Plex
(only a connected Plex counts as had, 3.1.0), and the UI's pure helpers. ("Download missing"
goes through the download queue: tests/test_download_queue.py, test_queue_link.py.)"""

from __future__ import annotations

import contextlib
import gc
import sqlite3
import tempfile
import tkinter as tk
import unittest
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import customtkinter as ctk
import httpx

from app.database import MIGRATIONS, Database
from app.models import (
    DownloadRecord,
    Episode,
    MediaType,
    MissingRunReport,
    Show,
    ShowDetails,
)
from app.models.plex import PlexScan
from app.services.library import LibraryService, downloaded_ids
from app.services.missing import (
    MISSING_AFTER,
    is_new,
    is_overdue,
    latest_season,
    missing_cutoffs,
    missing_episodes,
    new_episodes,
)
from app.services.tvmaze import TVMazeClient
from app.ui.missing_control import (
    FAILED_TEXT,
    NOTHING_MISSING_TEXT,
    MissingRuns,
    MissingRunState,
    download_missing_text,
    menu_position,
    missing_text,
    outcome_status_text,
    popup_menu,
    outcome_texts,
    progress_texts,
)
from app.ui.show_details import row_mark, season_marks
from tests.plex_link import have_marks, mark_have, plex_settings

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


def cutoffs(now: datetime) -> dict[str, Any]:
    """The SQL twins' `missing_by` / `missing_day` for `now` (app.services.missing), for
    person 1."""
    missing_by, missing_day = missing_cutoffs(now)
    return {"profile_id": 1, "missing_by": missing_by, "missing_day": missing_day}


def missing_ids(
    episodes: list[Episode],
    *,
    grabs: frozenset[tuple[int, int | None]] = frozenset(),
    in_plex: frozenset[int] = frozenset(),
    now: datetime = NOW,
) -> list[int]:
    downloaded = downloaded_ids(episodes, grabs)
    return ids(missing_episodes(episodes, downloaded=downloaded, in_plex=in_plex, now=now))


def new_ids(
    episodes: list[Episode],
    *,
    grabs: frozenset[tuple[int, int | None]] = frozenset(),
    in_plex: frozenset[int] = frozenset(),
    now: datetime = NOW,
) -> list[int]:
    downloaded = downloaded_ids(episodes, grabs)
    return ids(new_episodes(episodes, downloaded=downloaded, in_plex=in_plex, now=now))


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

    def test_air_date_without_airstamp_counts_from_the_day_after(self) -> None:
        episodes = [
            ep(1, 1, 1, air_date=NOW.date() - DAY),
            ep(2, 1, 2, air_date=NOW.date()),  # aired today: new, not missing yet
            ep(3, 1, 3, air_date=NOW.date() + DAY),
        ]
        self.assertEqual(missing_ids(episodes), [1])
        self.assertEqual(new_ids(episodes), [2])

    def test_the_airstamp_decides_to_the_minute_a_day_later(self) -> None:
        episodes = [
            ep(1, 1, 1, aired=NOW - MISSING_AFTER),
            ep(2, 1, 2, aired=NOW - MISSING_AFTER + timedelta(minutes=1)),
            ep(3, 1, 3, aired=NOW),
            ep(4, 1, 4, aired=NOW + timedelta(minutes=1)),
        ]
        self.assertEqual(missing_ids(episodes), [1])
        self.assertEqual(new_ids(episodes), [2, 3])
        self.assertEqual(missing_ids(episodes, now=NOW + DAY), [1, 2, 3])

    def test_nows_time_zone_decides_today_for_air_dates(self) -> None:
        # 01:00 UTC on the 20th is still the 19th in New York: an air date of the 19th is new
        # there, missing in UTC.
        utc = datetime(2026, 10, 20, 1, 0, tzinfo=UTC)
        new_york = utc.astimezone(timezone(timedelta(hours=-4)))
        episodes = [ep(1, 1, 1, air_date=date(2026, 10, 19))]
        self.assertEqual(missing_ids(episodes, now=new_york), [])
        self.assertEqual(new_ids(episodes, now=new_york), [1])
        self.assertEqual(missing_ids(episodes, now=utc), [1])
        self.assertEqual(new_ids(episodes, now=utc), [])

    def test_an_airstamp_counts_the_same_in_every_time_zone(self) -> None:
        episodes = [ep(1, 1, 1, aired=NOW - DAY - timedelta(minutes=5))]
        for hours in (-10, -4, 0, 2, 13):
            now = NOW.astimezone(timezone(timedelta(hours=hours)))
            self.assertEqual(missing_ids(episodes, now=now), [1], hours)

    def test_cutoffs_and_overdue(self) -> None:
        self.assertEqual(MISSING_AFTER, timedelta(hours=24))
        self.assertEqual(missing_cutoffs(NOW), (NOW - DAY, NOW.date() - DAY))
        self.assertTrue(is_overdue(ep(1, 1, 1, aired=NOW - DAY), NOW))
        self.assertFalse(is_overdue(ep(1, 1, 1, aired=NOW - DAY + timedelta(seconds=1)), NOW))
        self.assertTrue(is_new(ep(1, 1, 1, aired=NOW - timedelta(hours=3)), NOW))
        self.assertFalse(is_new(ep(1, 1, 1, aired=NOW + timedelta(hours=3)), NOW))  # not aired
        self.assertFalse(is_new(ep(1, 1, 1), NOW))  # no date at all: neither
        self.assertFalse(is_overdue(ep(1, 1, 1), NOW))

    def test_a_new_episode_is_neither_missing_nor_new_once_downloaded_or_in_plex(self) -> None:
        episodes = [ep(1, 1, 1, aired=NOW - timedelta(hours=2))]
        self.assertEqual(new_ids(episodes), [1])
        self.assertEqual(new_ids(episodes, grabs=frozenset({(1, 1)})), [])
        self.assertEqual(new_ids(episodes, in_plex=frozenset({1})), [])

    def test_a_new_seasons_first_episode_makes_it_the_latest_at_once(self) -> None:
        episodes = [
            ep(1, 1, 1, aired=NOW - 20 * DAY),
            ep(2, 2, 1, aired=NOW - timedelta(hours=1)),
        ]
        self.assertEqual(latest_season(episodes, NOW), 2)
        self.assertEqual(missing_ids(episodes), [])  # season 1 no longer counts; 2 is new
        self.assertEqual(new_ids(episodes), [2])


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

    def test_in_plex_is_not_missing(self) -> None:
        self.assertEqual(missing_ids(self.episodes, in_plex=frozenset({21, 23})), [22])


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
            await self.db.library.track(show_id, profile_id=1)
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
        self.assertEqual(await have_marks(db, 1), frozenset({11}))
        self.assertEqual(await have_marks(db, 2), frozenset())


class OldMarksTests(DatabaseTestCase):
    """"I already have this" (v4 / v5) is gone: its marks are kept as they are, unread."""

    async def test_old_marks_no_longer_count(self) -> None:
        await self.add_show(1, [ep(1, 1, 1, aired=NOW - 3 * DAY), ep(2, 1, 2, aired=NOW - 2 * DAY)])
        await mark_have(self.db, [1], NOW.isoformat())
        counts = await self.db.library.missing_counts(now=NOW, today=NOW.date(), **cutoffs(NOW))
        self.assertEqual(counts, {1: 2})
        await self.db.library.set_auto_download(1, True, since=NOW - 10 * DAY, profile_id=1)
        due = await self.db.library.auto_due(aired_from=NOW - 9 * DAY, aired_until=NOW, limit=9)
        self.assertEqual([entry.episode.id for entry in due], [1, 2])
        self.assertEqual(await have_marks(self.db, 1), frozenset({1}))  # kept as it was

    async def test_a_mark_still_goes_with_its_episode(self) -> None:
        show = await self.add_show(1, [ep(1, 1, 1, aired=NOW), ep(2, 1, 2, aired=NOW)])
        await mark_have(self.db, [1, 2], NOW.isoformat())
        await self.db.library.save_show(show, [ep(1, 1, 1, aired=NOW)])  # episode 2 is gone
        self.assertEqual(await have_marks(self.db, 1), frozenset({1}))


class MissingCountsTests(DatabaseTestCase):
    """The SQL counts agree with the Python rule, show by show."""

    async def counts(self, now: datetime = NOW) -> dict[int, int]:
        return await self.db.library.missing_counts(now=now, today=now.date(), **cutoffs(now))

    async def python_counts(self, now: datetime = NOW) -> dict[int, int]:
        result: dict[int, int] = {}
        for show_id in await self.db.library.tracked_ids(profile_id=1):
            episodes = await self.db.library.episodes(show_id)
            downloaded = downloaded_ids(episodes, await self.db.history.show_grabs(show_id))
            in_plex = await self.db.library.plex_ids(show_id)
            count = len(
                missing_episodes(episodes, downloaded=downloaded, in_plex=in_plex, now=now)
            )
            if count:
                result[show_id] = count
        return result

    async def test_counts_match_the_rule_on_every_edge_case(self) -> None:
        # 1: latest season 2 (season 3 not aired yet), one episode grabbed, one in Plex.
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
        await self.db.plex.replace(PlexScan(episodes=frozenset({(1, 2, 3)})), NOW)
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
        self.assertEqual(counts, {1: 2})  # 4's episode aired today: new, not missing yet
        self.assertEqual(counts, await self.python_counts())
        tomorrow = NOW + DAY  # 4's first is missing now; its second aired today: new
        self.assertEqual(await self.counts(tomorrow), {1: 2, 4: 1})
        self.assertEqual(await self.counts(tomorrow), await self.python_counts(tomorrow))
        later = NOW + 40 * DAY  # season 3 of show 1 has aired; show 3's first episode too
        self.assertEqual(await self.counts(later), await self.python_counts(later))
        self.assertEqual((await self.counts(later))[1], 1)

    async def test_the_airstamp_boundary_matches(self) -> None:
        await self.add_show(
            1,
            [
                ep(1, 1, 1, aired=NOW - MISSING_AFTER),
                ep(2, 1, 2, aired=NOW - MISSING_AFTER + timedelta(seconds=1)),
            ],
        )
        self.assertEqual(await self.counts(), {1: 1})
        self.assertEqual(await self.counts(), await self.python_counts())

    async def test_time_zones_match(self) -> None:
        # Air dates only: which day "today" is depends on `now`'s time zone, in SQL as in Python.
        await self.add_show(
            1,
            [
                ep(1, 1, 1, air_date=date(2026, 10, 18)),
                ep(2, 1, 2, air_date=date(2026, 10, 19)),
            ],
        )
        utc = datetime(2026, 10, 20, 1, 0, tzinfo=UTC)
        for hours in (-10, -4, 0, 5, 13):
            now = utc.astimezone(timezone(timedelta(hours=hours)))
            self.assertEqual(await self.counts(now), await self.python_counts(now), hours)
        self.assertEqual(await self.counts(utc), {1: 2})
        self.assertEqual(await self.counts(utc.astimezone(timezone(timedelta(hours=-4)))), {1: 1})


# -- the library service ---------------------------------------------------------------------


def _no_network(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404)


class ServiceTestCase(DatabaseTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(_no_network))
        self.settings = plex_settings(self.db)  # Plex connected: what is missing shows
        self.library = LibraryService(
            self.db.library,
            self.db.history,
            TVMazeClient(self.http, retry_delay=0),
            settings=self.settings,
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

    async def in_plex(self, *episodes: tuple[int, int]) -> None:
        """Severance's (season, number) episodes are in Plex."""
        found = frozenset((1, season, number) for season, number in episodes)
        await self.db.plex.replace(PlexScan(episodes=found), NOW)


class LibraryMissingTests(ServiceTestCase):
    async def test_show_details_of_a_tracked_show(self) -> None:
        await self.severance()
        await self.grab(1, 2, 1)
        await self.in_plex((2, 2))
        details = await self.library.show_details(1, now=NOW, profile_id=1)
        self.assertTrue(details.plex_connected)
        self.assertEqual(details.latest_season, 2)
        self.assertEqual(details.missing, frozenset({23}))
        self.assertEqual(details.in_plex, frozenset({22}))
        self.assertTrue(details.is_in_plex(details.episodes[2]))
        self.assertTrue(details.is_downloaded(details.episodes[1]))  # kept, never shown

    async def test_a_newly_followed_show_gets_no_marks(self) -> None:
        # With Plex, what aired and isn't in Plex or grabbed is missing from the start.
        now = datetime.now(UTC)
        show = Show(id=7, name="New")
        episodes = [ep(71, 1, 1, aired=now - 2 * DAY, show_id=7)]
        episodes.append(ep(72, 1, 2, aired=now + 2 * DAY, show_id=7))

        class _TVMaze(TVMazeClient):
            async def get_show_with_episodes(self, show_id: int) -> tuple[Show, list[Episode]]:
                return show, episodes

        library = LibraryService(
            self.db.library, self.db.history, _TVMaze(self.http), settings=self.settings
        )
        await library.track(7, profile_id=1)
        self.assertEqual(await have_marks(self.db, 7), frozenset())
        self.assertEqual(await library.missing_counts(now=now, profile_id=1), {7: 1})
        self.assertEqual(await library.missing_counts(now=now + 3 * DAY, profile_id=1), {7: 2})

    async def test_an_untracked_show_has_nothing_missing(self) -> None:
        await self.severance()
        await self.db.library.untrack(1, profile_id=1)
        self.assertEqual(await self.library.missing_episodes(1, now=NOW, profile_id=1), [])
        self.assertEqual(await self.library.missing_counts(now=NOW, profile_id=1), {})

    async def test_plex_changes_the_counts(self) -> None:
        await self.severance()
        self.assertEqual(await self.library.missing_counts(now=NOW, profile_id=1), {1: 3})
        await self.in_plex((2, 1), (2, 2), (2, 3))
        self.assertEqual(await self.library.missing_counts(now=NOW, profile_id=1), {})
        await self.in_plex((2, 1), (2, 3))
        self.assertEqual(await self.library.missing_counts(now=NOW, profile_id=1), {1: 1})

    async def test_without_plex_nothing_is_had_or_missing(self) -> None:
        await self.severance()
        await self.in_plex((2, 1))  # left from before (Disconnect empties it)
        await self.grab(1, 2, 2)
        await self.settings.update(plex_token="")
        self.assertFalse(self.library.plex_connected)
        library = self.library
        start, end = NOW.date() - 30 * DAY, NOW.date() + 30 * DAY
        self.assertEqual(await library.missing_counts(now=NOW, profile_id=1), {})
        self.assertEqual(await library.missing_episodes(1, now=NOW, profile_id=1), [])
        self.assertEqual(await library.missing_between(start, end, now=NOW, profile_id=1), set())
        self.assertEqual(await library.new_between(start, end, now=NOW, profile_id=1), set())
        self.assertEqual(await library.plex_between(start, end), set())
        self.assertEqual((await library.plex_films()).tmdb, set())
        details = await library.show_details(1, now=NOW, profile_id=1)
        self.assertFalse(details.plex_connected)
        self.assertIsNone(details.latest_season)
        self.assertEqual((details.missing, details.new, details.in_plex), (set(), set(), set()))
        self.assertEqual(details.downloaded, frozenset({22}))  # still known: nothing twice
        self.assertTrue(all(row_mark(details, e) is None for e in details.episodes))

    async def test_missing_episodes_oldest_first_with_the_show(self) -> None:
        await self.severance()
        entries = await self.library.missing_episodes(1, now=NOW, profile_id=1)
        self.assertEqual([entry.episode.code for entry in entries], ["S02E01", "S02E02", "S02E03"])
        self.assertTrue(all(entry.show.name == "Severance" for entry in entries))

    async def test_an_episode_due_for_auto_download_is_missing_until_grabbed(self) -> None:
        await self.severance()
        await self.db.library.set_auto_download(1, True, since=NOW - 30 * DAY, profile_id=1)
        due = await self.db.library.auto_due(
            aired_from=NOW - 10 * DAY, aired_until=NOW, limit=10
        )
        self.assertIn(23, [entry.episode.id for entry in due])
        self.assertIn(23, ids([e.episode for e in await self.library.missing_episodes(1, now=NOW, profile_id=1)]))
        await self.grab(1, 2, 3)
        self.assertNotIn(
            23, ids([e.episode for e in await self.library.missing_episodes(1, now=NOW, profile_id=1)])
        )


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


class RunStateTests(unittest.TestCase):
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

    def details(self, *, tracked: bool = True, plex: bool = True) -> ShowDetails:
        return ShowDetails(
            show=Show(id=1, name="Severance"),
            episodes=self.episodes,
            tracked=tracked,
            downloaded=frozenset({23}),
            plex_connected=plex,
            latest_season=2 if tracked else None,
            missing=frozenset({21}) if tracked else frozenset(),
            in_plex=frozenset({22, 10}),
        )

    def test_row_marks_on_the_latest_season_only(self) -> None:
        details = self.details()
        marks = {episode.id: row_mark(details, episode) for episode in self.episodes}
        # 23 was grabbed and isn't in Plex yet: on its way, no mark at all.
        self.assertEqual(marks, {10: None, 21: "missing", 22: "plex", 23: None, 24: None})
        untracked = self.details(tracked=False)
        self.assertTrue(all(row_mark(untracked, e) is None for e in self.episodes))

    def test_no_marks_without_plex(self) -> None:
        details = self.details(plex=False)
        self.assertTrue(all(row_mark(details, e) is None for e in self.episodes))
        self.assertEqual(season_marks(details, 2), ([], []))

    def test_season_marks(self) -> None:
        details = self.details()
        self.assertEqual(season_marks(details, 2), ([21], [22]))
        self.assertEqual(season_marks(details, 1), ([], []))
        self.assertEqual(season_marks(details, None), ([], []))


class MenuPositionTests(unittest.TestCase):
    WINDOW = (100, 50, 1300, 1000)  # x, y, width, height

    def test_below_the_anchor_right_edges_aligned(self) -> None:
        self.assertEqual(menu_position((500, 100, 40, 40), (200, 80), self.WINDOW), (340, 142))

    def test_kept_within_the_window(self) -> None:
        self.assertEqual(menu_position((110, 100, 40, 40), (200, 80), self.WINDOW), (100, 142))
        self.assertEqual(menu_position((1390, 100, 40, 40), (200, 80), self.WINDOW)[0], 1200)

    def test_left_edges_aligned_and_clamped(self) -> None:
        self.assertEqual(
            menu_position((500, 100, 40, 40), (200, 80), self.WINDOW, align="left"), (500, 142)
        )
        # Past the window's right edge: moved left until it fits.
        self.assertEqual(
            menu_position((1300, 100, 40, 40), (200, 80), self.WINDOW, align="left")[0], 1200
        )

    def test_above_when_there_is_no_room_below(self) -> None:
        self.assertEqual(menu_position((500, 1000, 40, 30), (200, 80), self.WINDOW), (340, 918))
        # No room above either: below it stays.
        self.assertEqual(menu_position((500, 60, 40, 30), (200, 2000), self.WINDOW), (340, 92))


class WidgetTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        self.marked: list[tuple[int, ...]] = []

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    def test_popup_menu_runs_the_choice_and_closes(self) -> None:
        anchor = ctk.CTkButton(self.root, text="⋯")
        anchor.pack()
        self.root.update()
        self.assertIsNone(popup_menu(anchor, []))
        menu = popup_menu(anchor, [("Open on IMDb", lambda: self.marked.append((1,)))])
        assert menu is not None
        self.assertEqual(menu.labels, ("Open on IMDb",))
        second = popup_menu(anchor, [("Remove show…", lambda: None)])
        assert second is not None
        self.assertFalse(menu.is_open)  # one at a time
        second.close()
        menu = popup_menu(anchor, [("Open on IMDb", lambda: self.marked.append((1,)))])
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
