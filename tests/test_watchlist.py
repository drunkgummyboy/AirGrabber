"""The movie watchlist: schema v3, WatchlistRepository, the pure release / due rules,
WatchlistService (cache, listeners, TMDB release dates, leaving the list on a grab) and the
watchlist part of AutoDownloadService's runs."""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import tempfile
import unittest
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from unittest import mock

import httpx

from app.config import AppPaths
from app.database import MIGRATIONS, Database
from app.models import (
    AutoDownloadReport,
    AutoError,
    AutoEvent,
    AutoMovieDownloaded,
    AutoMovieGaveUp,
    DownloadRecord,
    GrabOutcome,
    GrabResult,
    GrabTarget,
    MediaType,
    Movie,
    MovieDetails,
    QuickDownloadResult,
    SearchKind,
    SearchRequest,
    Settings,
    TorrentApp,
    TorrentRelease,
    WatchlistEntry,
)
from app.services import Services, SettingsService
from app.services.auto_download import MAX_MOVIES_PER_RUN, AutoDownloadService
from app.services.discovery import DiscoveryService
from app.services.downloads import DownloadService, GrabError
from app.services.quick_download import Progress, QuickDownloadService
from app.services.remote_downloads import RemoteDownloads
from app.services.search import movie_request
from app.services.tmdb import TMDBError, TMDBNotConfigured
from app.services.watchlist import (
    DIGITAL_AFTER_THEATRICAL,
    GIVE_UP_AFTER,
    REFRESH_AFTER,
    RETRY_AFTER,
    WatchlistService,
    available_on,
    due_order,
    is_due,
    is_out,
    needs_refresh,
    should_give_up,
)
from tests.test_auto_download import episode, show

NOW = datetime(2026, 11, 20, 12, 0, tzinfo=UTC)
TODAY = NOW.date()
AUTO_LOGGER = "app.services.auto_download"
LOGGER = "app.services.watchlist"

SAVED = GrabResult(outcome=GrabOutcome.SAVED, path="C:/watch/x.torrent", message="Saved x.torrent")
FOUND = QuickDownloadResult(
    found=True, release_title="Dune.Part.Three.2026.1080p.WEB", quality="1080p", seeders=80,
    grab=SAVED,
)
NOTHING = QuickDownloadResult(found=False)

Outcome = QuickDownloadResult | Exception


def movie(movie_id: int = 1, title: str = "Dune: Part Three", **fields: object) -> Movie:
    return Movie.model_validate(
        {"id": movie_id, "title": title, "release_date": "2026-12-18", **fields}
    )


def entry(**fields: object) -> WatchlistEntry:
    data: dict[str, object] = {"movie": movie(), "added_at": NOW - timedelta(days=10)}
    data.update(fields)
    return WatchlistEntry.model_validate(data)


class FakeDiscovery(DiscoveryService):
    """TMDB details by id from `details`; an id missing there raises `error`."""

    def __init__(self) -> None:  # no TMDB, no TVMaze
        self.details: dict[int, MovieDetails] = {}
        self.error: TMDBError = TMDBError("TMDB is down")
        self.calls: list[int] = []

    async def movie_details(self, movie_id: int) -> MovieDetails:
        self.calls.append(movie_id)
        if movie_id in self.details:
            return self.details[movie_id]
        raise self.error


def downloads_service(db: Database, opened: list[str], cache: Path) -> DownloadService:
    """A real DownloadService in APP mode that opens magnets into `opened` (no web)."""
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(404)))
    settings = SettingsService(db.settings, Settings(download_mode="app"))
    return DownloadService(
        http,
        db.history,
        settings,
        RemoteDownloads(settings),
        cache,
        find_app=lambda: TorrentApp(name="qBittorrent"),
        opener=opened.append,
    )


class WatchlistTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.db = await Database.open(Path(self.tmp.name) / "library.db")
        self.addAsyncCleanup(self.db.close)
        self.now = NOW
        self.discovery = FakeDiscovery()
        self.opened: list[str] = []
        self.downloads = downloads_service(self.db, self.opened, Path(self.tmp.name) / "torrents")
        self.addAsyncCleanup(self.downloads._http.aclose)
        self.service = WatchlistService(
            self.db.watchlist, self.discovery, self.downloads, clock=lambda: self.now
        )
        self.changes = 0
        self.service.subscribe(self.on_change)

    def on_change(self) -> None:
        self.changes += 1

    async def row(self, movie_id: int) -> WatchlistEntry | None:
        return await self.db.watchlist.entry(movie_id)


# -- schema ------------------------------------------------------------------------------------


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_v2_database_gets_an_empty_watchlist_and_keeps_its_data(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "v2.db"
        with contextlib.closing(sqlite3.connect(path)) as raw:
            script = "\n".join(MIGRATIONS[:2])
            raw.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = 2;\nCOMMIT;")
            with raw:
                raw.execute(
                    "INSERT INTO download_history (media_type, media_id, release_title, "
                    "downloaded_at) VALUES ('movie', 7, 'Old.Film.1080p', '2025-01-01T00:00:00Z')"
                )
        db = await Database.open(path)
        self.addAsyncCleanup(db.close)
        version = await db.fetch_one("PRAGMA user_version")
        assert version is not None
        self.assertGreaterEqual(version[0], 3)
        self.assertEqual(await db.watchlist.entries(), [])
        self.assertTrue(await db.history.has_movie(7))
        tables = {row["name"]: row["strict"] for row in await db.fetch_all("PRAGMA table_list")}
        self.assertEqual(tables["watchlist"], 1)

    def test_the_watchlist_migration_is_v3_and_self_contained(self) -> None:
        script = MIGRATIONS[2]
        self.assertIn("CREATE TABLE watchlist", script)
        self.assertNotIn("ALTER TABLE", script)  # touches nothing that came before


# -- repository --------------------------------------------------------------------------------


class RepositoryTests(WatchlistTestCase):
    async def test_add_keeps_the_film_and_an_existing_row(self) -> None:
        film = movie(5, "Sinners", poster_path="/p.jpg", vote_average=7.6, vote_count=900)
        self.assertTrue(await self.db.watchlist.add(film, auto_download=True, when=NOW))
        self.assertFalse(
            await self.db.watchlist.add(movie(5, "Renamed"), auto_download=False, when=NOW)
        )
        stored = await self.row(5)
        assert stored is not None
        self.assertEqual(stored.movie, film)
        self.assertEqual(stored.added_at, NOW)
        self.assertTrue(stored.auto_download)
        self.assertEqual((stored.tries, stored.gave_up, stored.details_at), (0, False, None))

    async def test_newest_first(self) -> None:
        for movie_id, days in ((1, 3), (2, 1), (3, 2)):
            await self.db.watchlist.add(
                movie(movie_id), auto_download=True, when=NOW - timedelta(days=days)
            )
        self.assertEqual([e.movie_id for e in await self.db.watchlist.entries()], [2, 3, 1])

    async def test_release_dates_and_imdb_id(self) -> None:
        await self.db.watchlist.add(movie(1, imdb_id="tt0000001"), auto_download=True, when=NOW)
        await self.db.watchlist.set_release_dates(
            1, imdb_id=None, digital=date(2026, 12, 1), theatrical=date(2026, 10, 2), when=NOW
        )
        stored = await self.row(1)
        assert stored is not None
        self.assertEqual(stored.imdb_id, "tt0000001")  # kept: the details named none
        self.assertEqual(
            (stored.digital_release, stored.theatrical_release, stored.details_at),
            (date(2026, 12, 1), date(2026, 10, 2), NOW),
        )
        await self.db.watchlist.set_release_dates(
            1, imdb_id="tt0000002", digital=None, theatrical=None, when=NOW
        )
        stored = await self.row(1)
        assert stored is not None
        self.assertEqual((stored.imdb_id, stored.digital_release), ("tt0000002", None))

    async def test_tries_give_up_and_switching_on_again_starts_afresh(self) -> None:
        await self.db.watchlist.add(movie(1), auto_download=True, when=NOW)
        await self.db.watchlist.record_try(1, NOW, give_up=False)
        later = NOW + timedelta(hours=5)
        await self.db.watchlist.record_try(1, later, give_up=True)
        stored = await self.row(1)
        assert stored is not None
        self.assertEqual(
            (stored.tries, stored.first_try, stored.last_try, stored.gave_up, stored.auto_download),
            (2, NOW, later, True, False),
        )
        await self.db.watchlist.set_auto_download(1, True)
        stored = await self.row(1)
        assert stored is not None
        self.assertEqual(
            (stored.tries, stored.first_try, stored.last_try, stored.gave_up, stored.auto_download),
            (0, None, None, False, True),
        )

    async def test_switching_on_a_switch_that_is_on_keeps_the_tries(self) -> None:
        await self.db.watchlist.add(movie(1), auto_download=True, when=NOW)
        await self.db.watchlist.record_try(1, NOW, give_up=False)
        await self.db.watchlist.set_auto_download(1, True)
        stored = await self.row(1)
        assert stored is not None
        self.assertEqual(stored.tries, 1)

    async def test_remove_and_unknown_films(self) -> None:
        await self.db.watchlist.add(movie(1), auto_download=True, when=NOW)
        self.assertTrue(await self.db.watchlist.remove(1))
        self.assertFalse(await self.db.watchlist.remove(1))
        await self.db.watchlist.record_try(1, NOW, give_up=False)  # ignored
        await self.db.watchlist.set_auto_download(1, True)
        self.assertEqual(await self.db.watchlist.entries(), [])

    async def test_a_film_grab_takes_it_off_the_list_an_episode_grab_does_not(self) -> None:
        await self.db.watchlist.add(movie(1), auto_download=True, when=NOW)
        await self.db.watchlist.add(movie(2), auto_download=True, when=NOW)
        await self.db.history.add(
            DownloadRecord(media_type=MediaType.TV, media_id=1, season=1, release_title="x")
        )
        await self.db.history.add(
            DownloadRecord(media_type=MediaType.MOVIE, media_id=2, release_title="y")
        )
        self.assertEqual([e.movie_id for e in await self.db.watchlist.entries()], [1])

    async def test_an_unreadable_row_is_skipped_not_fatal(self) -> None:
        await self.db.watchlist.add(movie(1), auto_download=True, when=NOW)
        await self.db.watchlist.add(movie(2), auto_download=True, when=NOW)
        async with self.db.transaction() as conn:
            await conn.execute("UPDATE watchlist SET movie = '{\"id\": 2}' WHERE movie_id = 2")
        with self.assertLogs("app.database", "WARNING"):
            entries = await self.db.watchlist.entries()
        self.assertEqual([e.movie_id for e in entries], [1])


# -- rules -------------------------------------------------------------------------------------


class AvailableOnTests(unittest.TestCase):
    def test_digital_release_wins(self) -> None:
        film = entry(digital_release="2026-11-12", theatrical_release="2026-09-01")
        self.assertEqual(available_on(film), date(2026, 11, 12))

    def test_theatrical_plus_sixty_days_without_a_digital_date(self) -> None:
        film = entry(theatrical_release="2026-09-01")
        self.assertEqual(DIGITAL_AFTER_THEATRICAL, timedelta(days=60))
        self.assertEqual(available_on(film), date(2026, 10, 31))

    def test_tmdb_release_date_without_any_details(self) -> None:
        film = entry(movie=movie(release_date="2026-08-01"))
        self.assertEqual(available_on(film), date(2026, 9, 30))

    def test_unknown_without_any_date(self) -> None:
        film = entry(movie=movie(release_date=None))
        self.assertIsNone(available_on(film))
        self.assertFalse(is_out(film, TODAY))

    def test_out_from_that_day(self) -> None:
        film = entry(digital_release=TODAY.isoformat())
        self.assertTrue(is_out(film, TODAY))
        self.assertFalse(is_out(film, TODAY - timedelta(days=1)))


class DueTests(unittest.TestCase):
    OUT = TODAY.isoformat()

    def test_due_once_out_with_the_switch_on(self) -> None:
        self.assertTrue(is_due(entry(digital_release=self.OUT), NOW))
        self.assertFalse(is_due(entry(digital_release="2026-11-21"), NOW))
        self.assertFalse(is_due(entry(digital_release=self.OUT, auto_download=False), NOW))
        self.assertFalse(is_due(entry(digital_release=self.OUT, gave_up=True), NOW))

    def test_searched_at_most_every_four_hours(self) -> None:
        self.assertEqual(RETRY_AFTER, timedelta(hours=4))
        recent = entry(digital_release=self.OUT, tries=1, last_try=NOW - RETRY_AFTER + timedelta(seconds=1))
        self.assertFalse(is_due(recent, NOW))
        self.assertTrue(is_due(recent, NOW + timedelta(seconds=1)))
        self.assertTrue(is_due(entry(digital_release=self.OUT, last_try=NOW - RETRY_AFTER), NOW))

    def test_order_least_tried_then_longest_ago_then_earliest_out(self) -> None:
        films = [
            entry(movie=movie(1), tries=2, last_try=NOW - timedelta(days=2)),
            entry(movie=movie(2), tries=1, last_try=NOW - timedelta(hours=5)),
            entry(movie=movie(3), tries=1, last_try=NOW - timedelta(hours=9)),
            entry(movie=movie(4), digital_release="2026-11-01"),
            entry(movie=movie(5), digital_release="2026-10-01"),
        ]
        self.assertEqual([f.movie_id for f in sorted(films, key=due_order)], [5, 4, 3, 2, 1])

    def test_give_up_thirty_days_after_the_first_search(self) -> None:
        self.assertEqual(GIVE_UP_AFTER, timedelta(days=30))
        self.assertFalse(should_give_up(entry(), NOW))  # the first search
        first = NOW - GIVE_UP_AFTER
        self.assertTrue(should_give_up(entry(tries=40, first_try=first), NOW))
        self.assertFalse(
            should_give_up(entry(tries=40, first_try=first + timedelta(seconds=1)), NOW)
        )

    def test_refresh_when_never_read_or_a_day_old(self) -> None:
        self.assertTrue(needs_refresh(entry(), NOW))
        self.assertFalse(needs_refresh(entry(details_at=NOW - timedelta(hours=23)), NOW))
        self.assertTrue(needs_refresh(entry(details_at=NOW - REFRESH_AFTER), NOW))
        self.assertTrue(needs_refresh(entry(details_at=NOW + timedelta(hours=1)), NOW))


# -- service -----------------------------------------------------------------------------------


class ServiceTests(WatchlistTestCase):
    async def test_add_reads_the_release_dates_and_tells_listeners(self) -> None:
        self.discovery.details[1] = MovieDetails(
            id=1, imdb_id="tt1234567", digital_release="2026-12-01", theatrical_release="2026-10-30"
        )
        with self.assertLogs(LOGGER, "INFO"):
            await self.service.add(movie(1))
        self.assertTrue(self.service.contains(1))
        stored = self.service.entry(1)
        assert stored is not None
        self.assertEqual(stored.imdb_id, "tt1234567")
        self.assertEqual(stored.digital_release, date(2026, 12, 1))
        self.assertEqual(stored.details_at, NOW)
        self.assertTrue(stored.auto_download)
        self.assertEqual(stored, await self.row(1))
        self.assertEqual(self.changes, 2)  # on the list, then its dates

    async def test_add_without_a_tmdb_key_keeps_the_stored_data(self) -> None:
        self.discovery.error = TMDBNotConfigured("no key")
        await self.service.add(movie(1), auto_download=False)
        stored = self.service.entry(1)
        assert stored is not None
        self.assertEqual((stored.auto_download, stored.details_at), (False, None))
        self.assertEqual(self.changes, 1)

    async def test_adding_twice_changes_nothing(self) -> None:
        await self.service.add(movie(1))
        changes = self.changes
        await self.service.add(movie(1), auto_download=False)
        self.assertEqual(self.changes, changes)
        self.assertEqual(self.discovery.calls, [1])

    async def test_remove(self) -> None:
        await self.service.add(movie(1))
        changes = self.changes
        await self.service.remove(1)
        await self.service.remove(1)
        self.assertFalse(self.service.contains(1))
        self.assertIsNone(await self.row(1))
        self.assertEqual(self.changes, changes + 1)

    async def test_entries_newest_first_and_load_from_the_database(self) -> None:
        for movie_id in (1, 2, 3):
            await self.service.add(movie(movie_id))
            self.now += timedelta(minutes=1)
        self.assertEqual([e.movie_id for e in self.service.entries()], [3, 2, 1])
        fresh = WatchlistService(self.db.watchlist, self.discovery, self.downloads)
        self.assertFalse(fresh.contains(1))
        await fresh.load()
        self.assertEqual(fresh.entries(), self.service.entries())

    async def test_switch(self) -> None:
        await self.service.add(movie(1))
        await self.service.set_auto_download(1, False)
        stored = self.service.entry(1)
        assert stored is not None
        self.assertFalse(stored.auto_download)

    async def test_record_try_gives_up_after_thirty_days(self) -> None:
        await self.service.add(movie(1))
        self.assertFalse(await self.service.record_try(1))
        self.now += GIVE_UP_AFTER - timedelta(seconds=1)
        self.assertFalse(await self.service.record_try(1))
        self.now += timedelta(seconds=1)
        self.assertTrue(await self.service.record_try(1))
        stored = self.service.entry(1)
        assert stored is not None
        self.assertEqual((stored.tries, stored.gave_up, stored.auto_download), (3, True, False))
        self.assertFalse(await self.service.record_try(99))  # not on the list

    async def test_refresh_reads_stale_films_only_and_stops_without_a_key(self) -> None:
        self.discovery.details = {n: MovieDetails(id=n, digital_release="2026-12-01") for n in (1, 2)}
        await self.service.add(movie(1))
        await self.service.add(movie(2))
        self.discovery.calls.clear()
        await self.service.refresh_release_dates()
        self.assertEqual(self.discovery.calls, [])  # read on adding
        self.now += REFRESH_AFTER
        await self.service.refresh_release_dates()
        self.assertEqual(sorted(self.discovery.calls), [1, 2])

        self.discovery.calls.clear()
        self.discovery.details.clear()
        self.discovery.error = TMDBNotConfigured("no key")
        await self.service.refresh_release_dates(force=True)
        self.assertEqual(len(self.discovery.calls), 1)  # no key: stop asking

    async def test_refresh_skips_a_film_tmdb_fails_on(self) -> None:
        self.discovery.error = TMDBError("timeout")
        await self.service.add(movie(1))
        await self.service.add(movie(2))
        self.discovery.calls.clear()
        self.discovery.details[2] = MovieDetails(id=2, digital_release="2026-12-01")
        with self.assertLogs(LOGGER, "INFO"):
            await self.service.refresh_release_dates(force=True)
        self.assertEqual(sorted(self.discovery.calls), [1, 2])
        stored = self.service.entry(2)
        assert stored is not None
        self.assertEqual(stored.digital_release, date(2026, 12, 1))

    async def test_a_grab_from_anywhere_takes_the_film_off_the_list(self) -> None:
        await self.service.add(movie(1))
        await self.service.add(movie(2))
        changes = self.changes
        heard: list[str] = []
        self.downloads.subscribe(lambda: heard.append("pages"))
        release = TorrentRelease(
            title="Dune.Part.Three.2026.1080p", source="test", info_hash="a" * 40, seeders=5
        )
        await self.downloads.grab(release, GrabTarget(media_type=MediaType.MOVIE, media_id=1))
        self.assertEqual(len(self.opened), 1)  # the magnet went to the torrent program
        self.assertFalse(self.service.contains(1))
        self.assertIsNone(await self.row(1))
        self.assertTrue(self.service.contains(2))
        self.assertEqual(self.changes, changes + 1)
        self.assertEqual(heard, ["pages"])
        # An episode of a show with the same id leaves the list alone.
        await self.downloads.grab(
            release.model_copy(update={"info_hash": "b" * 40}),
            GrabTarget(media_type=MediaType.TV, media_id=2, season=1, episode=1),
        )
        self.assertTrue(self.service.contains(2))

    async def test_failing_listener_is_logged_and_others_still_hear(self) -> None:
        def broken() -> None:
            raise RuntimeError("boom")

        self.service.subscribe(broken)
        with self.assertLogs(LOGGER, "ERROR"):
            await self.service.add(movie(1))
        self.assertGreater(self.changes, 0)

    async def test_unsubscribe(self) -> None:
        unsubscribe = self.service.subscribe(self.on_change)
        unsubscribe()
        unsubscribe()
        await self.service.add(movie(1))
        self.assertEqual(self.changes, 1)  # the first subscription only (TMDB is down)


# -- automatic downloads -----------------------------------------------------------------------


class FakeQuickDownload(QuickDownloadService):
    """Answers from `outcomes` (by title, else `default`) without searching; a found release is
    grabbed through the real DownloadService (history + grab listeners), as the real one does."""

    def __init__(self, downloads: DownloadService) -> None:  # no search
        self._downloads = downloads
        self.outcomes: dict[str, Outcome] = {}
        self.default: Outcome = FOUND
        self.calls: list[tuple[SearchRequest, GrabTarget]] = []

    @property
    def titles(self) -> list[str]:
        return [request.title for request, _ in self.calls]

    async def download(
        self,
        request: SearchRequest,
        target: GrabTarget,
        progress: Progress | None = None,
        *,
        again: bool = False,
    ) -> QuickDownloadResult:
        self.calls.append((request, target))
        if await self._downloads.has_grabbed(target):
            return QuickDownloadResult(found=False, already_downloaded=True)
        outcome = self.outcomes.get(request.title, self.default)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome.grab is not None:
            release = TorrentRelease(
                title=outcome.release_title or "?", source="test",
                info_hash=f"{target.media_id:040d}",
            )
            await self._downloads.grab(release, target)
        return outcome


class AutoRunTests(WatchlistTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.quick = FakeQuickDownload(self.downloads)
        self.auto = AutoDownloadService(
            self.db.library,
            self.quick,
            SettingsService(self.db.settings, Settings()),
            watchlist=self.service,
            clock=lambda: self.now,
        )
        self.events: list[AutoEvent] = []
        self.auto.subscribe(self.events.append)

    async def add(
        self, movie_id: int, title: str, *, digital: date | None = TODAY, auto: bool = True,
        imdb_id: str | None = None,
    ) -> Movie:
        film = movie(movie_id, title, release_date=None)
        self.discovery.details[movie_id] = MovieDetails(
            id=movie_id, imdb_id=imdb_id, digital_release=digital
        )
        await self.service.add(film, auto_download=auto)
        return film

    async def test_an_out_film_is_downloaded_and_leaves_the_list(self) -> None:
        film = await self.add(1, "Dune: Part Three", imdb_id="tt15239678")
        report = await self.auto.run_once()
        expected = AutoMovieDownloaded(title="Dune: Part Three", quality="1080p")
        self.assertEqual(report, AutoDownloadReport(movies_downloaded=(expected,)))
        self.assertEqual(self.events, [expected])
        [(request, target)] = self.quick.calls
        self.assertEqual(
            request, movie_request(film, MovieDetails(id=1, imdb_id="tt15239678"))
        )
        self.assertIs(request.kind, SearchKind.MOVIE)
        self.assertEqual(request.imdb_id, "tt15239678")
        self.assertEqual(target, GrabTarget(media_type=MediaType.MOVIE, media_id=1))
        self.assertFalse(self.service.contains(1))
        self.now += timedelta(hours=5)
        self.assertEqual(await self.auto.run_once(), AutoDownloadReport())
        self.assertEqual(len(self.quick.calls), 1)

    async def test_not_out_yet_switched_off_or_without_dates_is_not_searched(self) -> None:
        await self.add(1, "Soon", digital=TODAY + timedelta(days=1))
        await self.add(2, "Off", auto=False)
        await self.add(3, "Unknown", digital=None)
        self.assertEqual(await self.auto.run_once(), AutoDownloadReport())
        self.assertEqual(self.quick.calls, [])

    async def test_nothing_found_retries_every_four_hours_then_gives_up(self) -> None:
        await self.add(1, "Weapons")
        self.quick.default = NOTHING
        report = await self.auto.run_once()
        self.assertEqual(report, AutoDownloadReport(movies_not_found=("Weapons",)))
        stored = self.service.entry(1)
        assert stored is not None
        self.assertEqual((stored.tries, stored.last_try), (1, NOW))

        self.now += timedelta(minutes=30)  # the next run: too soon for this film
        self.assertEqual(await self.auto.run_once(), AutoDownloadReport())
        self.now = NOW + RETRY_AFTER
        await self.auto.run_once()
        self.assertEqual(self.quick.titles, ["Weapons", "Weapons"])

        self.now = NOW + GIVE_UP_AFTER
        report = await self.auto.run_once()
        self.assertEqual(report, AutoDownloadReport(movies_gave_up=("Weapons",)))
        self.assertEqual(self.events, [AutoMovieGaveUp(title="Weapons")])
        stored = self.service.entry(1)
        assert stored is not None
        self.assertEqual((stored.gave_up, stored.auto_download), (True, False))
        self.now += RETRY_AFTER
        self.assertEqual(await self.auto.run_once(), AutoDownloadReport())
        self.assertEqual(len(self.quick.calls), 3)

    async def test_failed_grab_reports_one_error_and_ends_the_run(self) -> None:
        await self.add(1, "A")
        await self.add(2, "B")
        self.quick.default = GrabError("No torrent program is set up on this PC.")
        with self.assertLogs(AUTO_LOGGER, "WARNING"):
            report = await self.auto.run_once()
        self.assertEqual(report.error, "No torrent program is set up on this PC.")
        self.assertEqual(
            self.events, [AutoError(message="No torrent program is set up on this PC.")]
        )
        self.assertEqual(len(self.quick.calls), 1)
        tried = [e for e in self.service.entries() if e.tries]
        self.assertEqual(len(tried), 1)

    async def test_a_failed_episode_grab_skips_the_films(self) -> None:
        await self.db.library.save_show(
            show(9, "Lost"), [episode(91, 9, NOW - timedelta(hours=3), number=1)]
        )
        await self.db.library.track(9)
        await self.db.library.set_auto_download(9, True, since=NOW - timedelta(days=1))
        await self.add(1, "A")
        self.quick.default = GrabError("Broken")
        with self.assertLogs(AUTO_LOGGER, "WARNING"):
            report = await self.auto.run_once()
        self.assertEqual(report.error, "Broken")
        self.assertEqual(self.quick.titles, ["Lost"])  # the film wasn't tried
        self.assertEqual(self.events, [AutoError(message="Broken")])

    async def test_at_most_five_films_per_run(self) -> None:
        for n in range(1, MAX_MOVIES_PER_RUN + 3):
            await self.add(n, f"Film {n}", digital=TODAY - timedelta(days=n))
        self.quick.default = NOTHING
        await self.auto.run_once()
        self.assertEqual(len(self.quick.calls), MAX_MOVIES_PER_RUN)
        # The longest-out first; the rest come first in the next run.
        self.assertEqual(self.quick.titles[0], f"Film {MAX_MOVIES_PER_RUN + 2}")
        self.quick.calls.clear()
        self.now += timedelta(minutes=30)
        await self.auto.run_once()
        self.assertEqual(sorted(self.quick.titles), ["Film 1", "Film 2"])

    async def test_a_film_downloaded_before_it_was_listed_is_switched_off(self) -> None:
        await self.db.history.add(
            DownloadRecord(media_type=MediaType.MOVIE, media_id=1, release_title="Old.1080p")
        )
        await self.add(1, "Old Favourite")
        report = await self.auto.run_once()
        self.assertEqual(report, AutoDownloadReport())
        stored = self.service.entry(1)
        assert stored is not None
        self.assertFalse(stored.auto_download)
        self.assertEqual(stored.tries, 0)
        self.now += RETRY_AFTER
        await self.auto.run_once()
        self.assertEqual(len(self.quick.calls), 1)

    async def test_release_dates_are_refreshed_by_the_run(self) -> None:
        await self.add(1, "Delayed", digital=TODAY + timedelta(days=3))
        self.discovery.details[1] = MovieDetails(id=1, digital_release=TODAY)
        self.discovery.calls.clear()
        await self.auto.run_once()
        self.assertEqual(self.discovery.calls, [])  # read on adding: still fresh
        self.now += REFRESH_AFTER
        report = await self.auto.run_once()
        self.assertEqual(self.discovery.calls, [1])
        self.assertEqual([e.title for e in report.movies_downloaded], ["Delayed"])

    async def test_logs_name_films_and_outcomes_only(self) -> None:
        await self.add(1, "Dune: Part Three")
        with self.assertLogs(AUTO_LOGGER, "INFO") as logs:
            await self.auto.run_once()
        text = "\n".join(logs.output)
        self.assertIn("Dune: Part Three", text)
        self.assertNotIn("http", text)
        self.assertNotIn(FOUND.release_title or "?", text)


class ServicesWiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_services_load_the_watchlist_and_hand_it_to_the_automatic_downloads(
        self,
    ) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        paths = AppPaths(Path(tmp.name))
        paths.ensure()
        db = await Database.open(paths.database)
        await db.watchlist.add(movie(1), auto_download=True, when=NOW)
        await db.close()
        asyncio.get_running_loop().slow_callback_duration = 1.0  # httpx loads its CA bundle
        network = AssertionError("no network calls")
        with mock.patch.object(httpx.AsyncClient, "send", side_effect=network):
            services = await Services.create(paths)
        try:
            self.assertIsInstance(services.watchlist, WatchlistService)
            self.assertTrue(services.watchlist.contains(1))
            self.assertIs(services.auto_download._watchlist, services.watchlist)
        finally:
            await services.aclose()



if __name__ == "__main__":
    unittest.main()
