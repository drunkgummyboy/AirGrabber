"""People (accounts, 3.0.1): the v8 migration from a full v7 database (lossless, person 1 gets
everything), each person's own followed shows, watchlist, calendar and missing episodes, the
automatic downloads covering everyone's lists once, a downloaded film leaving every list,
unfollowing that leaves what another person follows alone, ProfileService, the backup with
people and the 1.x import into the selected person."""

from __future__ import annotations

import contextlib
import importlib
import json
import sqlite3
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

import httpx

from app.database import MIGRATIONS, BackupRepository, Database, match_people
from app.models import (
    DownloadRecord,
    Episode,
    GrabTarget,
    MediaType,
    Movie,
    MovieDetails,
    Profile,
    ProfileColour,
    QuickDownloadResult,
    SearchRequest,
    Show,
)
from app.models.backup import (
    FORMAT_NAME,
    FORMAT_VERSION,
    BackupFile,
    BackupProfile,
    BackupShow,
    LoadMode,
)
from app.models.profiles import NAME_MAX
from app.services import SettingsService
from app.services.auto_download import DUE_DELAY, GIVE_UP_AFTER, AutoDownloadService
from app.services.backup import MACHINE_FIELDS, BackupService
from app.services.library import LibraryService
from app.services.profiles import (
    ProfileChange,
    ProfileError,
    ProfileService,
    clean_name,
    name_problem,
    next_colour,
)
from app.services.quick_download import Progress
from app.services.tmdb import TMDBNotConfigured
from app.services.tvmaze import TVMazeClient
from app.services.watchlist import WatchlistService
from tests.plex_link import have_marks, mark_have, plex_settings
from tests.test_auto_download import FOUND, FakeQuickDownload
from tests.test_library import DRAGON, DUNE, FakeTVMaze

# tools/ is not a package; importing by name keeps mypy from seeing the module twice.
import_legacy = importlib.import_module("tools.import_legacy").import_legacy

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=UTC)
CREATED = datetime(2026, 11, 1, tzinfo=UTC)
ME, SANNE = 1, 2


def show(show_id: int, name: str = "", *, status: str = "Running") -> Show:
    return Show(
        id=show_id,
        name=name or f"Show {show_id}",
        status=status,
        type="Scripted",
        language="English",
        weight=90,
    )


def episode(episode_id: int, show_id: int, aired: datetime, *, number: int = 1) -> Episode:
    return Episode(
        id=episode_id,
        show_id=show_id,
        season=1,
        number=number,
        air_date=aired.date(),
        airstamp=aired,
    )


def film(movie_id: int, title: str = "") -> Movie:
    return Movie.model_validate(
        {"id": movie_id, "title": title or f"Film {movie_id}", "release_date": "2026-01-10"}
    )


def person(
    profile_id: int, name: str | None, colour: ProfileColour = ProfileColour.BLUE
) -> Profile:
    return Profile(id=profile_id, name=name, colour=colour, created_at=CREATED)


class Quick(FakeQuickDownload):
    """FakeQuickDownload that records a film's grab as a film's (a downloaded film leaves the
    watchlists)."""

    async def download(
        self,
        request: SearchRequest,
        target: GrabTarget,
        progress: Progress | None = None,
        *,
        again: bool = False,
    ) -> QuickDownloadResult:
        if target.media_type is not MediaType.MOVIE:
            return await super().download(request, target, progress, again=again)
        self.calls.append((request, target))
        await self._history.add(
            DownloadRecord(media_type=MediaType.MOVIE, media_id=target.media_id, release_title="F")
        )
        return FOUND


def no_tmdb() -> mock.MagicMock:
    discovery = mock.MagicMock()
    discovery.movie_details = mock.AsyncMock(side_effect=TMDBNotConfigured("no key"))
    return discovery


class DatabaseCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.db = await Database.open(self.folder / "airgrabber.db")
        self.addAsyncCleanup(self.db.close)
        self.sanne = await self.db.profiles.add("Sanne", ProfileColour.PINK, CREATED)

    async def save(self, show_id: int, *episodes: Episode, status: str = "Running") -> None:
        await self.db.library.save_show(show(show_id, status=status), list(episodes))


# -- the v8 migration ------------------------------------------------------------------------------


def _v7(path: Path) -> None:
    """A v7 database (what 3.0.0 leaves) with a row in every table."""
    with contextlib.closing(sqlite3.connect(path)) as raw:
        raw.execute("PRAGMA foreign_keys = ON")
        for version, script in enumerate(MIGRATIONS[:7], start=1):
            raw.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
        raw.executescript(
            """
            INSERT INTO shows (id, name, status, genres) VALUES
                (1, 'Severance', 'Running', '["Drama"]'), (2, 'Fargo', 'Ended', '[]'),
                (3, 'Untracked', 'Running', '[]');
            INSERT INTO episodes (id, show_id, season, number, name, air_date, airstamp) VALUES
                (11, 1, 1, 1, 'Good News', '2026-10-01', '2026-10-01T02:00:00+00:00'),
                (12, 1, 1, 2, 'Half Loop', '2026-10-08', '2026-10-08T02:00:00+00:00'),
                (21, 2, 1, 1, 'Blood', '2024-01-01', NULL),
                (31, 3, 1, 1, 'Pilot', '2026-10-20', NULL);
            INSERT INTO tracked_shows (show_id, tracked_at, auto_download, synced_at, auto_since)
            VALUES (1, '2026-01-01T00:00:00+00:00', 1, '2026-10-31T00:00:00+00:00',
                    '2026-09-01T00:00:00+00:00'),
                   (2, '2025-05-05T00:00:00+00:00', 0, NULL, NULL);
            INSERT INTO auto_attempts (episode_id, tries, last_try, gave_up)
            VALUES (12, 2, '2026-10-09T00:00:00+00:00', 0);
            INSERT INTO have_episodes (episode_id, marked_at)
            VALUES (11, '2026-10-02T00:00:00+00:00');
            INSERT INTO download_history (id, media_type, media_id, season, episode, release_title,
                                          info_hash, downloaded_at)
            VALUES (5, 'tv', 1, 1, 1, 'Severance.S01E01', 'aa', '2026-10-01T08:00:00+00:00');
            INSERT INTO cleared_downloads (history_id, cleared_at)
            VALUES (5, '2026-10-02T00:00:00+00:00');
            INSERT INTO settings (key, value) VALUES ('weeks_to_show', '2');
            INSERT INTO schedule_fetches (air_date, fetched_at)
            VALUES ('2026-10-20', '2026-10-20T00:00:00+00:00');
            INSERT INTO subtitle_files (video_path, language, history_id, media_type, media_id,
                                        tries)
            VALUES ('C:/v.mkv', 'en', 5, 'tv', 1, 1);
            """
        )
        movie = json.dumps({"id": 77, "title": "Dune: Part Three", "release_date": "2026-12-18"})
        raw.execute(
            "INSERT INTO watchlist (movie_id, title, movie, added_at, auto_download, imdb_id, "
            "digital_release, details_at, tries, first_try, last_try, gave_up) "
            "VALUES (77, 'Dune: Part Three', ?, '2026-10-01T00:00:00+00:00', 1, 'tt0000077', "
            "'2027-03-01', '2026-10-31T00:00:00+00:00', 3, '2026-10-20T00:00:00+00:00', "
            "'2026-10-30T00:00:00+00:00', 0)",
            (movie,),
        )
        raw.commit()


def _dump(path: Path, table: str, order: str) -> list[tuple[Any, ...]]:
    with contextlib.closing(sqlite3.connect(path)) as raw:
        return list(raw.execute(f"SELECT * FROM {table} ORDER BY {order}"))


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "airgrabber.db"
        _v7(self.path)

    async def test_v7_upgrades_losslessly_and_person_1_gets_everything(self) -> None:
        shared = {
            "shows": "id",
            "episodes": "id",
            "auto_attempts": "episode_id",
            "have_episodes": "episode_id",
            "download_history": "id",
            "cleared_downloads": "history_id",
            "settings": "key",
            "schedule_fetches": "air_date",
            "subtitle_files": "video_path",
        }
        before = {table: _dump(self.path, table, order) for table, order in shared.items()}
        tracked = _dump(self.path, "tracked_shows", "show_id")
        watchlist = _dump(self.path, "watchlist", "movie_id")

        db = await Database.open(self.path)
        try:
            version = await db.fetch_one("PRAGMA user_version")
            assert version is not None
            self.assertEqual(version[0], len(MIGRATIONS))
            self.assertGreaterEqual(len(MIGRATIONS), 8)  # v9 (3.0.2): the seen marks
            people = await db.profiles.all()
            self.assertEqual(
                [(p.id, p.name, p.colour) for p in people], [(1, None, ProfileColour.BLUE)]
            )
            for table, order in shared.items():  # untouched
                after = _dump(self.path, table, order)
                if table == "shows":  # v10 (3.1.0) adds an empty tvdb_id at the end
                    self.assertTrue(all(row[-1] is None for row in after))
                    after = [row[:-1] for row in after]
                self.assertEqual(after, before[table], table)
            rows = await db.fetch_all(
                "SELECT show_id, tracked_at, auto_download, synced_at, auto_since, profile_id "
                "FROM tracked_shows ORDER BY show_id"
            )
            # v10 clears synced_at: the next sync reads every followed show (TheTVDB ids).
            self.assertEqual(
                [tuple(row) for row in rows], [(*row[:3], None, *row[4:], 1) for row in tracked]
            )
            columns = (
                "movie_id, title, movie, added_at, auto_download, imdb_id, digital_release, "
                "theatrical_release, details_at, tries, first_try, last_try, gave_up"
            )
            rows = await db.fetch_all(f"SELECT {columns}, profile_id FROM watchlist")
            self.assertEqual([tuple(row) for row in rows], [(*row, 1) for row in watchlist])
            # Person 1's library reads as before.
            entries = await db.library.tracked(profile_id=1)
            self.assertEqual([(e.show.name, e.auto_download) for e in entries],
                             [("Fargo", False), ("Severance", True)])  # fmt: skip
            [movie] = await db.watchlist.entries(profile_id=1)
            self.assertEqual((movie.movie_id, movie.tries, movie.imdb_id), (77, 3, "tt0000077"))
            # The keys, indexes and foreign keys of the rebuilt tables.
            indexes = {
                row["name"]
                for row in await db.fetch_all("SELECT name FROM sqlite_master WHERE type = 'index'")
            }
            self.assertLessEqual({"ix_tracked_shows_show", "ix_watchlist_movie"}, indexes)
            parents_of = {"tracked_shows": {"profiles", "shows"}, "watchlist": {"profiles"}}
            for table, parents in parents_of.items():
                keys = await db.fetch_all(f"PRAGMA foreign_key_list({table})")
                self.assertEqual({row["table"] for row in keys}, parents, table)
            self.assertEqual(await db.fetch_all("PRAGMA foreign_key_check"), [])
            check = await db.fetch_one("PRAGMA integrity_check")
            assert check is not None
            self.assertEqual(check[0], "ok")
            with self.assertRaises(sqlite3.IntegrityError):  # the same follow twice
                async with db.transaction() as conn:
                    await conn.execute(
                        "INSERT INTO tracked_shows (profile_id, show_id, tracked_at) "
                        "VALUES (1, 1, 'now')"
                    )
        finally:
            await db.close()

    async def test_removing_a_person_takes_their_lists_only(self) -> None:
        db = await Database.open(self.path)
        try:
            sanne = await db.profiles.add("Sanne", ProfileColour.PINK, NOW)
            await db.library.track(1, profile_id=sanne.id)
            await db.watchlist.add(film(78), profile_id=sanne.id, auto_download=True, when=NOW)
            self.assertTrue(await db.profiles.remove(sanne.id))
            self.assertEqual(await db.library.tracked_ids(profile_id=sanne.id), frozenset())
            self.assertEqual(await db.watchlist.entries(profile_id=sanne.id), [])
            self.assertEqual(await db.library.tracked_ids(profile_id=1), {1, 2})
            self.assertEqual(len(await db.library.episodes(1)), 2)
            self.assertFalse(await db.profiles.remove(1))  # never the last one
        finally:
            await db.close()


# -- each person's lists ---------------------------------------------------------------------------


class PerPersonTests(DatabaseCase):
    async def test_followed_shows_and_switches_are_a_persons_own(self) -> None:
        await self.save(1)
        await self.save(2)
        await self.db.library.track(1, profile_id=ME, auto_download=True)
        await self.db.library.track(2, profile_id=SANNE)
        self.assertEqual(await self.db.library.tracked_ids(profile_id=ME), {1})
        self.assertEqual(await self.db.library.tracked_ids(profile_id=SANNE), {2})
        self.assertEqual(await self.db.library.followed_ids(), {1, 2})
        await self.db.library.set_auto_download(2, True, profile_id=ME)  # not Me's show
        entry = await self.db.library.tracked_entry(2, profile_id=SANNE)
        assert entry is not None
        self.assertFalse(entry.auto_download)
        self.assertEqual((await self.db.library.stats(profile_id=SANNE)).tracked_shows, 1)

    async def test_calendar_missing_and_suggestions_per_person(self) -> None:
        aired = NOW - timedelta(days=3)
        await self.save(1, episode(11, 1, aired))
        await self.save(2, episode(21, 2, aired))
        await self.db.library.track(1, profile_id=ME)
        await self.db.library.track(2, profile_id=SANNE)
        library = LibraryService(
            self.db.library, self.db.history, mock.MagicMock(), settings=plex_settings(self.db)
        )  # Plex connected: what is missing shows
        day = aired.date()
        mine = await library.calendar(day, day, profile_id=ME)
        hers = await library.calendar(day, day, profile_id=SANNE)
        self.assertEqual([e.show.id for e in mine], [1])
        self.assertEqual([e.show.id for e in hers], [2])
        self.assertEqual(await library.missing_counts(profile_id=ME, now=NOW), {1: 1})
        self.assertEqual(await library.missing_counts(profile_id=SANNE, now=NOW), {2: 1})
        self.assertEqual(await library.missing_between(day, day, profile_id=SANNE, now=NOW), {21})
        self.assertEqual(
            [e.episode.id for e in await library.missing_episodes(2, profile_id=ME, now=NOW)], []
        )
        # A show someone else follows can be suggested; one's own never.
        await self.db.library.save_schedule_day(day, [], [])
        suggested = await self.db.library.suggestions(day, day, profile_id=ME, per_day=5)
        self.assertEqual([e.show.id for e in suggested], [2])
        activity = await library.activity(profile_id=SANNE, now=NOW)
        self.assertEqual(set(activity), set())  # nothing grabbed, nothing upcoming

    async def test_a_watchlist_is_a_persons_own_and_its_film_bookkeeping_is_shared(self) -> None:
        await self.db.watchlist.add(film(7), profile_id=ME, auto_download=True, when=NOW)
        await self.db.watchlist.record_try(7, NOW, give_up=False)
        await self.db.watchlist.set_release_dates(
            7, imdb_id="tt7", digital=date(2026, 1, 1), theatrical=None, when=NOW
        )
        await self.db.watchlist.add(film(7), profile_id=SANNE, auto_download=True, when=NOW)
        hers = await self.db.watchlist.entry(7, profile_id=SANNE)
        assert hers is not None
        self.assertEqual(
            (hers.tries, hers.imdb_id, hers.digital_release), (1, "tt7", date(2026, 1, 1))
        )
        await self.db.watchlist.record_try(7, NOW, give_up=True)  # given up: every switch off
        for profile_id in (ME, SANNE):
            entry = await self.db.watchlist.entry(7, profile_id=profile_id)
            assert entry is not None
            self.assertEqual((entry.tries, entry.gave_up, entry.auto_download), (2, True, False))
        await self.db.watchlist.set_auto_download(7, True, profile_id=SANNE)  # starts afresh
        mine = await self.db.watchlist.entry(7, profile_id=ME)
        hers = await self.db.watchlist.entry(7, profile_id=SANNE)
        assert mine is not None and hers is not None
        self.assertEqual((mine.tries, mine.auto_download), (0, False))
        self.assertEqual((hers.tries, hers.auto_download), (0, True))
        self.assertTrue(await self.db.watchlist.remove(7, profile_id=SANNE))
        self.assertIsNotNone(await self.db.watchlist.entry(7, profile_id=ME))

    async def test_a_downloaded_film_leaves_every_list(self) -> None:
        for profile_id in (ME, SANNE):
            await self.db.watchlist.add(
                film(7), profile_id=profile_id, auto_download=True, when=NOW
            )
        await self.db.history.add(
            DownloadRecord(media_type=MediaType.MOVIE, media_id=7, release_title="Film.7.1080p")
        )
        for profile_id in (ME, SANNE):
            self.assertEqual(await self.db.watchlist.entries(profile_id=profile_id), [])

    async def test_unfollowing_leaves_what_another_person_follows(self) -> None:
        old = NOW - timedelta(days=200)
        await self.save(1, episode(11, 1, old), status="Ended")
        await self.db.library.track(1, profile_id=ME)
        await self.db.library.track(1, profile_id=SANNE)
        await mark_have(self.db, [11], NOW.isoformat())  # an old mark (kept, unread since 3.1.0)
        await self.db.library.untrack(1, profile_id=ME)
        self.assertEqual(await self.db.library.untrack_ended(profile_id=ME), 0)
        await self.db.library.prune_schedule(NOW.date())
        self.assertIsNotNone(await self.db.library.get_show(1))
        self.assertEqual([e.id for e in await self.db.library.episodes(1)], [11])
        self.assertEqual(await have_marks(self.db, 1), {11})
        # "Cleanup Ended" is the person's own: the last follow goes, then the cache with it.
        self.assertEqual(await self.db.library.untrack_ended(profile_id=SANNE), 1)
        await self.db.library.prune_schedule(NOW.date())
        self.assertIsNone(await self.db.library.get_show(1))

    async def test_sync_covers_everyones_shows_once(self) -> None:
        await self.save(1)
        await self.save(2)
        await self.db.library.track(1, profile_id=ME)
        await self.db.library.track(1, profile_id=SANNE)
        await self.db.library.track(2, profile_id=SANNE)
        followed = await self.db.library.followed()
        self.assertEqual([entry.show.id for entry in followed], [1, 2])
        self.assertTrue(all(entry.synced_at is None for entry in followed))
        await self.db.library.mark_synced(1)
        [first, _] = await self.db.library.followed()
        self.assertIsNotNone(first.synced_at)  # every person's row of the show was stamped


class LibraryServiceTests(DatabaseCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.server = FakeTVMaze()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.server))
        self.addAsyncCleanup(self.http.aclose)
        tvmaze = TVMazeClient(self.http, retry_delay=0)
        self.library = LibraryService(self.db.library, self.db.history, tvmaze)

    async def test_a_second_follower_shares_the_show(self) -> None:
        await self.library.track(DUNE, profile_id=ME)
        self.assertEqual(await have_marks(self.db, DUNE), frozenset())  # no marks since 3.1.0
        await self.library.track(DUNE, profile_id=SANNE)
        details = await self.library.show_details(DUNE, profile_id=SANNE)
        self.assertTrue(details.tracked)
        self.assertFalse((await self.library.show_details(DRAGON, profile_id=SANNE)).tracked)
        await self.library.untrack(DUNE, profile_id=ME)
        self.assertTrue((await self.library.show_details(DUNE, profile_id=SANNE)).tracked)

    async def test_import_goes_to_the_person_it_was_started_for(self) -> None:
        result = await self.library.import_shows([str(DUNE), str(DRAGON)], profile_id=SANNE)
        self.assertEqual(len(result.added), 2)
        self.assertEqual(await self.db.library.tracked_ids(profile_id=SANNE), {DUNE, DRAGON})
        self.assertEqual(await self.db.library.tracked_ids(profile_id=ME), frozenset())


# -- the automatic downloads: everyone's lists, each title once ----------------------------------


class AutoDownloadUnionTests(DatabaseCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.quick = Quick(self.db.history)
        self.quick.default = FOUND
        settings = await SettingsService.load(self.db.settings)
        self.discovery = mock.MagicMock()
        self.discovery.movie_details = mock.AsyncMock(
            return_value=MovieDetails(id=7, digital_release=date(2026, 1, 1))
        )
        self.watchlist = WatchlistService(
            self.db.watchlist, self.discovery, mock.MagicMock(), clock=lambda: NOW
        )
        self.auto = AutoDownloadService(
            self.db.library, self.quick, settings, watchlist=self.watchlist, clock=lambda: NOW
        )

    async def test_an_episode_two_people_follow_is_fetched_once(self) -> None:
        aired = NOW - DUE_DELAY - timedelta(hours=1)
        await self.save(1, episode(11, 1, aired))
        await self.db.library.track(1, profile_id=ME)
        await self.db.library.track(1, profile_id=SANNE)
        # Me switched on after it aired; Sanne before: the earliest switch counts.
        await self.db.library.set_auto_download(1, True, profile_id=ME, since=NOW)
        before = aired - timedelta(days=1)
        await self.db.library.set_auto_download(1, True, profile_id=SANNE, since=before)
        due = await self.db.library.auto_due(
            aired_from=NOW - GIVE_UP_AFTER, aired_until=NOW - DUE_DELAY, limit=10
        )
        self.assertEqual([entry.episode.id for entry in due], [11])
        report = await self.auto.run_once()
        self.assertEqual(len(report.downloaded), 1)
        self.assertEqual(len(self.quick.calls), 1)
        report = await self.auto.run_once()  # downloaded: never again
        self.assertEqual(len(self.quick.calls), 1)

    async def test_whoever_is_selected_others_shows_are_fetched(self) -> None:
        aired = NOW - DUE_DELAY - timedelta(hours=1)
        await self.save(2, episode(21, 2, aired))
        await self.db.library.track(2, profile_id=SANNE)
        before = aired - timedelta(days=1)
        await self.db.library.set_auto_download(2, True, profile_id=SANNE, since=before)
        await self.watchlist.load(ME)  # Me is selected
        await self.auto.run_once()
        self.assertEqual([target.media_id for _request, target in self.quick.calls], [2])

    async def test_a_film_on_two_lists_is_searched_once_and_leaves_both(self) -> None:
        for profile_id in (ME, SANNE):
            await self.db.watchlist.add(
                film(7), profile_id=profile_id, auto_download=True, when=NOW
            )
        await self.watchlist.load(ME)
        self.assertEqual([entry.movie_id for entry in await self.watchlist.due(NOW, 5)], [7])
        report = await self.auto.run_once()
        self.assertEqual(len(report.movies_downloaded), 1)
        self.assertEqual([target.media_id for _request, target in self.quick.calls], [7])
        for profile_id in (ME, SANNE):
            self.assertEqual(await self.db.watchlist.entries(profile_id=profile_id), [])

    async def test_a_film_on_someone_elses_list_only(self) -> None:
        await self.db.watchlist.add(film(7), profile_id=SANNE, auto_download=True, when=NOW)
        await self.watchlist.load(ME)
        await self.auto.run_once()
        self.assertEqual([target.media_id for _request, target in self.quick.calls], [7])


# -- ProfileService --------------------------------------------------------------------------------


class NameTests(unittest.TestCase):
    def test_clean_name(self) -> None:
        self.assertEqual(clean_name("  Sanne   de  Vries "), "Sanne de Vries")
        self.assertEqual(clean_name(None), "")

    def test_name_problems(self) -> None:
        people = [person(1, None), person(2, "Sanne")]
        self.assertEqual(name_problem("", people), "Type a name")
        self.assertIsNone(name_problem("", people, required=False))
        too_long = name_problem("x" * (NAME_MAX + 1), people)
        self.assertEqual(too_long, f"Use at most {NAME_MAX} characters")
        self.assertIsNone(name_problem("x" * NAME_MAX, people))
        self.assertEqual(name_problem(" SANNE ", people), "Someone is called Sanne already")
        self.assertIsNone(name_problem("sanne", people, own=2))  # her own name, other case
        self.assertIsNone(name_problem("Beau", people))

    def test_next_colour(self) -> None:
        self.assertIs(next_colour([]), ProfileColour.BLUE)
        self.assertIs(next_colour([person(1, None)]), ProfileColour.PINK)
        everyone = [person(i, f"P{i}", colour) for i, colour in enumerate(ProfileColour)]
        self.assertIs(next_colour(everyone), ProfileColour.BLUE)


class ProfileServiceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.db = await Database.open(Path(tmp.name) / "airgrabber.db")
        self.addAsyncCleanup(self.db.close)
        self.settings = await SettingsService.load(self.db.settings)
        self.watchlist = WatchlistService(self.db.watchlist, no_tmdb(), mock.MagicMock())
        self.profiles = ProfileService(
            self.db.profiles, self.settings, self.watchlist, clock=lambda: NOW
        )
        await self.profiles.load()
        self.changes: list[ProfileChange] = []
        self.profiles.subscribe(self.changes.append)

    async def test_starts_as_me(self) -> None:
        self.assertEqual([p.id for p in self.profiles.people], [1])
        self.assertEqual((self.profiles.current_id, self.profiles.current.name), (1, None))

    async def test_add_rename_and_recolour(self) -> None:
        sanne = await self.profiles.add("  Sanne ", ProfileColour.PINK)
        self.assertEqual(
            (sanne.name, sanne.colour, sanne.created_at), ("Sanne", ProfileColour.PINK, NOW)
        )
        self.assertEqual(self.profiles.current_id, 1)  # adding doesn't switch
        self.assertEqual(self.changes, [ProfileChange.PEOPLE])
        with self.assertRaises(ProfileError):
            await self.profiles.add("sanne", ProfileColour.GREEN)
        with self.assertRaises(ProfileError):
            await self.profiles.add("", ProfileColour.GREEN)
        renamed = await self.profiles.update(1, name="Beau", colour=ProfileColour.GREEN)
        self.assertEqual((renamed.name, renamed.colour), ("Beau", ProfileColour.GREEN))
        self.assertEqual(self.changes, [ProfileChange.PEOPLE, ProfileChange.PEOPLE])
        await self.profiles.update(1, name="Beau", colour=ProfileColour.GREEN)  # no change
        self.assertEqual(len(self.changes), 2)
        with self.assertRaises(ProfileError):
            await self.profiles.update(1, name="SANNE", colour=ProfileColour.GREEN)
        await self.profiles.update(1, name="", colour=ProfileColour.GREEN)  # "Me" again
        self.assertIsNone(self.profiles.current.name)

    async def test_switch_saves_loads_the_watchlist_and_tells(self) -> None:
        sanne = await self.profiles.add("Sanne", ProfileColour.PINK)
        await self.db.watchlist.add(film(7), profile_id=sanne.id, auto_download=True, when=NOW)
        await self.profiles.switch(sanne.id)
        self.assertEqual(self.settings.current.current_profile, sanne.id)
        self.assertEqual((self.watchlist.profile_id, self.watchlist.contains(7)), (sanne.id, True))
        self.assertEqual(self.changes[-1], ProfileChange.SWITCHED)
        await self.profiles.switch(sanne.id)  # already: nothing
        self.assertEqual(self.changes.count(ProfileChange.SWITCHED), 1)
        with self.assertRaises(ProfileError):
            await self.profiles.switch(99)
        # Opening again: as the last one chosen.
        again = ProfileService(self.db.profiles, self.settings, self.watchlist)
        await again.load()
        self.assertEqual(again.current_id, sanne.id)

    async def test_remove_refuses_the_last_and_switches_away_from_the_selected(self) -> None:
        with self.assertRaises(ProfileError):
            await self.profiles.remove(1)
        sanne = await self.profiles.add("Sanne", ProfileColour.PINK)
        await self.profiles.switch(sanne.id)
        self.changes.clear()
        await self.profiles.remove(sanne.id)
        self.assertEqual(self.profiles.current_id, 1)
        self.assertEqual(self.settings.current.current_profile, 1)
        self.assertEqual([p.id for p in self.profiles.people], [1])
        self.assertEqual(self.changes, [ProfileChange.PEOPLE, ProfileChange.SWITCHED])

    async def test_a_selection_naming_nobody_is_the_first_person(self) -> None:
        await self.settings.update(current_profile=42)
        await self.profiles.load()
        self.assertEqual((self.profiles.current_id, self.settings.current.current_profile), (1, 1))

    async def test_unsubscribe(self) -> None:
        heard: list[ProfileChange] = []
        stop = self.profiles.subscribe(heard.append)
        stop()
        stop()
        await self.profiles.add("Sanne", ProfileColour.PINK)
        self.assertEqual(heard, [])

    async def test_a_click_for_the_person_before_a_switch_stays_theirs(self) -> None:
        sanne = await self.profiles.add("Sanne", ProfileColour.PINK)
        await self.watchlist.add(film(7), profile_id=1)  # clicked while Me was showing
        await self.profiles.switch(sanne.id)
        await self.watchlist.add(film(8), profile_id=1)  # a click that lands after the switch
        self.assertFalse(self.watchlist.contains(8))  # Sanne's list in memory stays hers
        mine = await self.db.watchlist.entries(profile_id=1)
        self.assertEqual([e.movie_id for e in mine], [8, 7])
        self.assertEqual(await self.db.watchlist.entries(profile_id=sanne.id), [])


# -- the backup ------------------------------------------------------------------------------------


class MatchPeopleTests(unittest.TestCase):
    def test_by_name_then_in_order(self) -> None:
        here = [person(1, None), person(2, "Sanne"), person(3, "Kid")]
        backup = [
            BackupProfile(id=1, name="Beau"),
            BackupProfile(id=2, name="sanne"),
            BackupProfile(id=3, name="Oma"),
            BackupProfile(id=4, name="Opa"),
        ]
        mapping, extra, unmatched = match_people(backup, here)
        self.assertEqual(mapping, {2: 2, 1: 1, 3: 3})
        self.assertEqual([p.name for p in extra], ["Opa"])
        self.assertEqual(unmatched, [])
        mapping, extra, unmatched = match_people(backup[:1], here)
        self.assertEqual((mapping, extra, [p.id for p in unmatched]), ({1: 1}, [], [2, 3]))


class BackupPeopleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)

    async def place(self, name: str) -> tuple[Database, ProfileService, BackupService]:
        db = await Database.open(self.folder / f"{name}.db")
        self.addAsyncCleanup(db.close)
        settings = await SettingsService.load(db.settings)
        watchlist = WatchlistService(db.watchlist, mock.MagicMock(), mock.MagicMock())
        profiles = ProfileService(db.profiles, settings, watchlist, clock=lambda: NOW)
        await profiles.load()
        backup = BackupService(BackupRepository(db), settings, profiles, clock=lambda: NOW)
        return db, profiles, backup

    async def two_people(self) -> tuple[Database, ProfileService, BackupService]:
        db, profiles, backup = await self.place("a")
        await profiles.update(1, name="Beau", colour=ProfileColour.BLUE)
        sanne = await profiles.add("Sanne", ProfileColour.PINK)
        await db.library.save_show(show(1), [episode(11, 1, NOW - timedelta(days=9))])
        await db.library.save_show(show(2), [episode(21, 2, NOW - timedelta(days=9))])
        await db.library.track(1, profile_id=1, auto_download=True)
        await db.library.track(1, profile_id=sanne.id)
        await db.library.track(2, profile_id=sanne.id)
        await mark_have(db, [11], NOW.isoformat())  # an old mark: backups still carry it
        await db.watchlist.add(film(7), profile_id=sanne.id, auto_download=False, when=NOW)
        return db, profiles, backup

    async def test_people_go_in_and_a_shared_show_carries_its_episodes_once(self) -> None:
        _db, _profiles, backup = await self.two_people()
        made = await backup.build(include_secrets=False)
        self.assertEqual(made.version, FORMAT_VERSION)
        self.assertEqual([(p.id, p.name, p.colour) for p in made.profiles],
                         [(1, "Beau", "blue"), (2, "Sanne", "pink")])  # fmt: skip
        self.assertEqual([(s.profile, s.show.id, len(s.episodes), s.have) for s in made.shows],
                         [(1, 1, 1, (11,)), (2, 1, 0, ()), (2, 2, 1, ())])  # fmt: skip
        self.assertEqual([(f.profile, f.movie.id) for f in made.watchlist], [(2, 7)])
        self.assertIn("current_profile", MACHINE_FIELDS)
        self.assertNotIn("current_profile", made.settings)

    async def test_replace_into_a_new_pc_brings_the_people_and_their_lists(self) -> None:
        _a_db, _a, a_backup = await self.two_people()
        made = await a_backup.build(include_secrets=False)
        db, profiles, backup = await self.place("b")
        result = await backup.load(made, LoadMode.REPLACE)
        self.assertEqual((result.counts.people_added, result.counts.people_removed), (1, 0))
        self.assertEqual([(p.name, p.colour) for p in profiles.people],
                         [("Beau", ProfileColour.BLUE), ("Sanne", ProfileColour.PINK)])  # fmt: skip
        sanne = profiles.people[1].id
        self.assertEqual(await db.library.tracked_ids(profile_id=1), {1})
        self.assertEqual(await db.library.tracked_ids(profile_id=sanne), {1, 2})
        self.assertEqual(await have_marks(db, 1), {11})
        self.assertEqual([e.movie_id for e in await db.watchlist.entries(profile_id=sanne)], [7])
        self.assertEqual(await BackupRepository(db).read(), await BackupRepository(_a_db).read())

    async def test_replace_removes_the_people_the_backup_lacks(self) -> None:
        _db, _a, a_backup = await self.two_people()
        made = await a_backup.build(include_secrets=False)
        one = made.model_copy(
            update={"profiles": made.profiles[:1], "shows": made.shows[:1], "watchlist": ()}
        )
        db, profiles, backup = await self.place("b")
        await profiles.add("Kid", ProfileColour.GREEN)
        await profiles.switch(2)
        result = await backup.load(one, LoadMode.REPLACE)
        self.assertEqual(result.counts.people_removed, 1)
        self.assertEqual([p.name for p in profiles.people], ["Beau"])
        self.assertEqual(profiles.current_id, 1)  # the selected one went: the first again

    async def test_add_keeps_the_people_here_and_joins_their_lists(self) -> None:
        _db, _a, a_backup = await self.two_people()
        made = await a_backup.build(include_secrets=False)
        db, profiles, backup = await self.place("b")
        sanne_here = await profiles.add("SANNE", ProfileColour.PURPLE)
        await db.library.save_show(show(3), [])
        await db.library.track(3, profile_id=sanne_here.id)
        result = await backup.load(made, LoadMode.ADD)
        self.assertEqual(result.counts.people_added, 0)
        self.assertEqual([(p.name, p.colour) for p in profiles.people],
                         [(None, ProfileColour.BLUE), ("SANNE", ProfileColour.PURPLE)])  # fmt: skip
        self.assertEqual(await db.library.tracked_ids(profile_id=sanne_here.id), {1, 2, 3})
        self.assertEqual(await db.library.tracked_ids(profile_id=1), {1})

    async def test_a_backup_from_before_people_goes_to_the_selected_person(self) -> None:
        db, profiles, backup = await self.place("b")
        sanne = await profiles.add("Sanne", ProfileColour.PINK)
        await profiles.switch(sanne.id)
        old = BackupFile(
            format=FORMAT_NAME,
            version=1,
            created=NOW,
            shows=(BackupShow(show=show(1), tracked_at=NOW, episodes=(episode(11, 1, NOW),)),),
        )
        result = await backup.load(old, LoadMode.ADD)
        self.assertEqual(result.counts.shows_added, 1)
        self.assertEqual(await db.library.tracked_ids(profile_id=sanne.id), {1})
        self.assertEqual(await db.library.tracked_ids(profile_id=1), frozenset())
        self.assertEqual(len(profiles.people), 2)


# -- the 1.x import --------------------------------------------------------------------------------


class ImportLegacyTests(DatabaseCase):
    async def select(self, profile_id: int) -> None:
        stored = await self.db.settings.load()
        await self.db.settings.save(stored.model_copy(update={"current_profile": profile_id}))

    async def test_into_the_selected_person(self) -> None:
        await self.select(SANNE)
        (self.folder / "followed_shows.json").write_text(
            json.dumps({"5": {"name": "Alex Show"}}), encoding="utf-8"
        )
        report = await import_legacy(self.folder, self.db)
        self.assertEqual((report.profile_id, report.shows), (SANNE, 1))
        self.assertEqual(await self.db.library.tracked_ids(profile_id=SANNE), {5})
        self.assertEqual(await self.db.library.tracked_ids(profile_id=ME), frozenset())

    async def test_a_selection_naming_nobody_imports_into_person_1(self) -> None:
        await self.select(9)
        (self.folder / "followed_shows.json").write_text(
            json.dumps({"5": {"name": "Alex Show"}}), encoding="utf-8"
        )
        report = await import_legacy(self.folder, self.db)
        self.assertEqual(report.profile_id, ME)


if __name__ == "__main__":
    unittest.main()
