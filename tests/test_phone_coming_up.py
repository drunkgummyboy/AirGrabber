"""The phone's Coming up: series from the calendar, films from its person's watchlist, in one
list, each with the state the PC shows. Real database, fake torrent program and no searches."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

from app.database import Database
from app.models import (
    DownloadEntry,
    DownloadRecord,
    DownloadsOverview,
    DownloadState,
    Episode,
    GrabTarget,
    MediaType,
    Movie,
    PairedPhone,
    ProfileColour,
    Show,
)
from app.models.plex import PlexFilms
from app.services.download_queue import DownloadQueue
from app.services.download_status import DownloadStatusService
from app.services.downloads import DownloadService
from app.services.library import LibraryService
from app.services.phone.coming_up import JUST_GRABBED_S, ComingUp, GrabbedFilms
from app.services.phone.server import Reply, Request
from app.services.profiles import ProfileService
from app.services.quick_download import QuickDownloadService
from app.services.search import TorrentSearchService
from app.services.settings import SettingsService
from app.services.watchlist import WatchlistService

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
TODAY = NOW.date()
SEVERANCE = 44933
DUNE = 438631


def show(id: int = SEVERANCE, name: str = "Severance", **fields: Any) -> Show:
    return Show(id=id, name=name, poster_url=f"https://static.tvmaze.com/{id}.jpg", **fields)


def episode(id: int, day: date, *, show_id: int = SEVERANCE, number: int | None = 1) -> Episode:
    return Episode(id=id, show_id=show_id, season=2, number=number, air_date=day)


def film(id: int = DUNE, title: str = "Dune", **fields: Any) -> Movie:
    return Movie(id=id, title=title, poster_path=f"/{id}.jpg", **fields)


def record(
    media_type: MediaType, media_id: int, season: int | None, number: int | None
) -> DownloadRecord:
    return DownloadRecord(
        media_type=media_type,
        media_id=media_id,
        season=season,
        episode=number,
        release_title="Some.Release.1080p",
    )


class PhoneCase(unittest.IsolatedAsyncioTestCase):
    """A real database and the real services a phone's request goes through; `self.status`'s
    `latest` is what the PC's Downloads page holds."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.database_file = Path(self.tmp.name) / "library.db"
        self.db = await Database.open(self.database_file)
        self.addAsyncCleanup(self.db.close)
        self.settings = await SettingsService.load(self.db.settings)
        self.library = LibraryService(
            self.db.library, self.db.history, mock.Mock(), settings=self.settings
        )
        self.grabs = mock.Mock(spec=DownloadService)
        self.grabs.has_grabbed = mock.AsyncMock(return_value=False)
        self.watchlist = WatchlistService(self.db.watchlist, mock.Mock(), self.grabs)
        self.profiles = ProfileService(self.db.profiles, self.settings, self.watchlist)
        await self.profiles.load()
        self.person = self.profiles.current_id
        self.status = SimpleNamespace(latest=None)
        self.films = GrabbedFilms(cast(DownloadStatusService, self.status))
        self.search: Any = mock.Mock(spec=TorrentSearchService)
        self.quick = QuickDownloadService(self.search, self.grabs, self.settings)
        self.queue = DownloadQueue(self.quick)
        self.addCleanup(self.queue.close)
        self.seconds = 1000.0
        self.phone = self.phone_of(self.person)

    def phone_of(self, person: int) -> PairedPhone:
        return PairedPhone(
            id="phone-1",
            name="Pixel",
            token_hash="0" * 64,
            profile_id=person,
            paired_at=NOW,
        )

    async def follow(self, *episodes: Episode, person: int | None = None) -> None:
        await self.db.library.save_show(show(), episodes)
        await self.db.library.track(SEVERANCE, profile_id=person or self.person)

    async def want(
        self, movie: Movie, *, person: int | None = None, digital: date | None = None
    ) -> None:
        await self.db.watchlist.add(
            movie, profile_id=person or self.person, auto_download=True, when=NOW
        )
        if digital is not None:
            await self.db.watchlist.set_release_dates(
                movie.id, imdb_id=None, digital=digital, theatrical=None, when=NOW
            )

    def downloads_page(
        self, *grabs: tuple[DownloadRecord, DownloadState], title: str = "A title"
    ) -> None:
        self.status.latest = DownloadsOverview(
            entries=tuple(
                DownloadEntry(record=grab, title=title, state=state) for grab, state in grabs
            )
        )


class ComingUpTests(PhoneCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.service = ComingUp(
            self.library,
            self.watchlist,
            cast(DownloadStatusService, self.status),
            self.grabs,
            self.quick,
            self.queue,
            self.profiles,
            self.films,
            now=lambda: NOW,
            clock=lambda: self.seconds,
        )

    async def items(self, **query: str) -> list[dict[str, Any]]:
        reply = await self.ask(**query)
        self.assertEqual(reply.status, 200, reply.body)
        return cast(list[dict[str, Any]], reply.body["items"])

    async def ask(self, phone: PairedPhone | None = None, /, **query: str) -> Reply:
        request = Request("GET", "/v1/coming-up", query, {}, phone or self.phone)
        reply = await self.service.routes()[("GET", "/v1/coming-up")](request)
        json.dumps(reply.body)  # what the server sends
        return reply

    async def test_episodes_and_movies_merged_nearest_day_first(self) -> None:
        await self.follow(
            episode(1, date(2026, 11, 20), number=3),
            episode(2, date(2026, 11, 8), number=1),
        )
        await self.want(film(), digital=date(2026, 11, 10))
        items = await self.items(**{"from": "2026-11-07", "days": "21"})
        self.assertEqual(
            [(item["kind"], item["date"]) for item in items],
            [
                ("episode", "2026-11-08"),
                ("movie", "2026-11-10"),
                ("episode", "2026-11-20"),
            ],
        )
        self.assertEqual(
            items[0],
            {
                "kind": "episode",
                "id": SEVERANCE,
                "season": 2,
                "episode": 1,
                "title": "Severance",
                "code": "S02E01",
                "date": "2026-11-08",
                "poster_url": f"https://static.tvmaze.com/{SEVERANCE}.jpg",
                "state": "not_out",
            },
        )
        self.assertEqual(
            items[1],
            {
                "kind": "movie",
                "id": DUNE,
                "season": None,
                "episode": None,
                "title": "Dune",
                "code": None,
                "date": "2026-11-10",
                "poster_url": f"https://image.tmdb.org/t/p/w342/{DUNE}.jpg",
                "state": "not_out",
            },
        )

    async def test_the_window_is_from_and_days_and_defaults_to_today_and_21(self) -> None:
        await self.follow(
            episode(1, date(2026, 11, 6), number=1),
            episode(2, date(2026, 11, 7), number=2),
            episode(3, date(2026, 11, 27), number=3),
            episode(4, date(2026, 11, 28), number=4),
        )
        self.assertEqual([item["episode"] for item in await self.items()], [2, 3])
        self.assertEqual(
            [item["episode"] for item in await self.items(**{"from": "2026-11-06", "days": "1"})],
            [1],
        )

    async def test_what_cant_be_read_is_a_bad_request(self) -> None:
        for query in (
            {"from": "someday"},
            {"days": "soon"},
            {"days": "0"},
            {"days": "100000"},
            {"from": "9999-12-31", "days": "21"},
        ):
            with self.subTest(query):
                reply = await self.ask(**query)
                self.assertEqual(reply.status, 400)
                self.assertEqual(reply.body["error"], "bad_request")

    async def test_movies_come_from_the_phones_profiles_watchlist_only(self) -> None:
        other = await self.profiles.add("Sam", ProfileColour.BLUE)
        await self.want(film(), digital=date(2026, 11, 10))
        await self.want(film(99, "Other"), person=other.id, digital=date(2026, 11, 10))
        await self.watchlist.load(self.person)  # the PC has the first person selected

        reply = await self.ask(self.phone_of(other.id))
        self.assertEqual([item["title"] for item in reply.body["items"]], ["Other"])
        self.assertEqual([item["title"] for item in await self.items()], ["Dune"])

    async def test_series_come_from_the_phones_profile_too(self) -> None:
        other = await self.profiles.add("Sam", ProfileColour.BLUE)
        await self.follow(episode(1, TODAY), person=other.id)
        self.assertEqual(await self.items(), [])
        reply = await self.ask(self.phone_of(other.id))
        self.assertEqual(len(reply.body["items"]), 1)

    async def test_a_deleted_person_gets_a_plain_answer_not_someone_elses_list(self) -> None:
        await self.want(film(), digital=date(2026, 11, 10))
        reply = await self.ask(self.phone_of(12345))
        self.assertEqual(reply.status, 401)
        self.assertEqual(reply.body["error"], "not_paired")
        self.assertIn("connect the phone again", reply.body["message"])
        self.assertNotIn("items", reply.body)

    async def test_aired_episode_can_download(self) -> None:
        aired = episode(1, TODAY).model_copy(update={"airstamp": NOW})
        later = episode(2, TODAY, number=2).model_copy(
            update={"airstamp": datetime(2026, 11, 7, 20, 0, tzinfo=UTC)}
        )
        await self.follow(aired, later)
        states = {item["episode"]: item["state"] for item in await self.items()}
        self.assertEqual(states, {1: "can_download", 2: "not_out"})

    async def test_a_special_has_no_number_to_download_by_so_it_stays_out(self) -> None:
        await self.follow(episode(1, TODAY, number=None), episode(2, TODAY, number=1))
        self.assertEqual([item["episode"] for item in await self.items()], [1])

    async def test_movie_out_uses_is_out(self) -> None:
        # No digital date: it counts as out 60 days after the cinemas, as on the PC.
        await self.want(film(1, "Out", release_date=date(2026, 9, 8)))
        await self.want(film(2, "Not yet", release_date=date(2026, 9, 9)))
        items = await self.items(days="10")
        self.assertEqual(
            [(item["title"], item["date"], item["state"]) for item in items],
            [("Out", "2026-11-07", "can_download"), ("Not yet", "2026-11-08", "not_out")],
        )

    async def test_a_film_out_before_the_window_stays_until_it_is_fetched(self) -> None:
        await self.want(film(1, "Old"), digital=date(2026, 1, 1))
        await self.want(film(2, "Old and in Plex"), digital=date(2026, 1, 1))
        with mock.patch.object(
            self.library, "plex_films", return_value=PlexFilms(tmdb=frozenset({2}))
        ):
            items = await self.items()
        self.assertEqual([item["title"] for item in items], ["Old"])
        self.assertEqual(items[0]["state"], "can_download")

    async def test_not_out_movie_has_its_date_not_a_button(self) -> None:
        await self.want(film(), digital=date(2026, 11, 20))
        items = await self.items()
        self.assertEqual(items[0]["date"], "2026-11-20")
        self.assertEqual(items[0]["state"], "not_out")

    async def test_a_film_past_the_window_waits_and_one_without_a_date_comes_last(self) -> None:
        await self.want(film(1, "Far off"), digital=date(2027, 6, 1))
        await self.want(film(2, "No date"))
        await self.want(film(3, "Soon"), digital=date(2026, 11, 9))
        items = await self.items()
        self.assertEqual(
            [(item["title"], item["date"], item["state"]) for item in items],
            [("Soon", "2026-11-09", "not_out"), ("No date", None, "not_out")],
        )

    async def grab_film(self) -> DownloadRecord:
        """A film on the list, seen there by a phone, then grabbed: its rows really go."""
        await self.want(film(), digital=date(2026, 11, 7))
        self.assertEqual([item["title"] for item in await self.items()], ["Dune"])
        grab = record(MediaType.MOVIE, DUNE, None, None)
        await self.db.history.add(grab)
        self.assertEqual(await self.db.watchlist.entries(profile_id=self.person), [])
        return grab

    async def test_downloaded_item_says_so(self) -> None:
        await self.follow(episode(1, date(2026, 11, 1), number=1))
        grab = await self.grab_film()
        self.downloads_page(
            (record(MediaType.TV, SEVERANCE, 2, 1), DownloadState.READY),
            (grab, DownloadState.READY),
            title="Dune (2021)",
        )
        items = await self.items(**{"from": "2026-11-01"})
        self.assertEqual(
            [(item["kind"], item["state"]) for item in items],
            [("episode", "downloaded"), ("movie", "downloaded")],
        )
        self.assertEqual(items[1]["title"], "Dune")
        self.assertEqual(items[1]["date"], "2026-11-07")
        self.assertEqual(items[1]["poster_url"], f"https://image.tmdb.org/t/p/w342/{DUNE}.jpg")

    async def test_a_grabbed_film_follows_the_same_state_rules(self) -> None:
        grab = await self.grab_film()
        for state, wanted in (
            (DownloadState.DOWNLOADING, "downloading"),
            (DownloadState.READY, "downloaded"),
            (DownloadState.UNKNOWN, "can_download"),
            (DownloadState.PROBLEM, "can_download"),
        ):
            with self.subTest(state):
                self.downloads_page((grab, state))
                self.assertEqual([item["state"] for item in await self.items()], [wanted])

    async def test_a_grabbed_film_shows_at_once_and_leaves_with_the_downloads_page(self) -> None:
        grab = await self.grab_film()
        listener = self.grabs.subscribe_grabs.call_args.args[0]
        listener(GrabTarget(media_type=MediaType.MOVIE, media_id=DUNE))
        self.assertEqual([item["state"] for item in await self.items()], ["downloading"])
        self.seconds += JUST_GRABBED_S + 1
        self.assertEqual(await self.items(), [])
        self.downloads_page((grab, DownloadState.READY))
        self.assertEqual([item["state"] for item in await self.items()], ["downloaded"])
        self.downloads_page()
        self.assertEqual(await self.items(), [])

    async def test_a_grabbed_film_not_seen_before_is_named_by_its_downloads_page_row(self) -> None:
        grab = record(MediaType.MOVIE, DUNE, None, None)
        self.downloads_page((grab, DownloadState.READY), title="Dune (2021)")
        items = await self.items()
        self.assertEqual(items[0]["title"], "Dune")
        self.assertEqual(items[0]["date"], grab.downloaded_at.date().isoformat())
        self.assertIsNone(items[0]["poster_url"])

    async def test_a_grabbed_film_of_someone_else_stays_off_this_phone(self) -> None:
        other = await self.profiles.add("Sam", ProfileColour.BLUE)
        await self.want(film(99, "Sams film"), person=other.id, digital=date(2026, 11, 7))
        await self.items()  # the lists as they are, seen by any phone
        grab = record(MediaType.MOVIE, 99, None, None)
        await self.db.history.add(grab)
        self.downloads_page((grab, DownloadState.READY))
        self.assertEqual(await self.items(), [])
        reply = await self.ask(self.phone_of(other.id))
        self.assertEqual([item["title"] for item in reply.body["items"]], ["Sams film"])

    async def test_what_the_downloads_page_says_is_the_state(self) -> None:
        await self.follow(episode(1, date(2026, 11, 1), number=1))
        expected = {
            DownloadState.DOWNLOADING: "downloading",
            DownloadState.WAITING: "downloading",
            DownloadState.PENDING: "downloading",
            DownloadState.STOPPED: "downloading",
            DownloadState.READY: "downloaded",
            DownloadState.UNKNOWN: "can_download",  # may be anything: the PC says "unknown"
            DownloadState.PROBLEM: "can_download",
            DownloadState.NOT_FOUND: "can_download",
        }
        self.assertEqual(set(expected), set(DownloadState))
        for state, wanted in expected.items():
            with self.subTest(state):
                self.downloads_page((record(MediaType.TV, SEVERANCE, 2, 1), state))
                items = await self.items(**{"from": "2026-11-01"})
                self.assertEqual(items[0]["state"], wanted)

    async def test_a_program_out_of_reach_never_makes_a_grab_downloaded(self) -> None:
        await self.follow(episode(1, date(2026, 11, 1), number=1))
        grab = record(MediaType.TV, SEVERANCE, 2, 1)
        self.downloads_page((grab, DownloadState.DOWNLOADING))
        items = await self.items(**{"from": "2026-11-01"})
        self.assertEqual(items[0]["state"], "downloading")
        self.downloads_page((grab, DownloadState.UNKNOWN))  # the next check couldn't ask
        items = await self.items(**{"from": "2026-11-01"})
        self.assertEqual(items[0]["state"], "can_download")

    async def test_a_grab_a_moment_ago_is_downloading_while_the_status_is_unknown(self) -> None:
        await self.follow(episode(1, date(2026, 11, 1), number=1))
        grab = record(MediaType.TV, SEVERANCE, 2, 1)
        target = GrabTarget(media_type=MediaType.TV, media_id=SEVERANCE, season=2, episode=1)
        self.grabs.subscribe_grabs.call_args.args[0](target)
        self.downloads_page((grab, DownloadState.UNKNOWN))
        with mock.patch.object(self.library, "downloaded_between", return_value=frozenset({1})):
            items = await self.items(**{"from": "2026-11-01"})
        self.assertEqual(items[0]["state"], "downloading")

    async def test_a_season_pack_on_the_downloads_page_counts_for_its_episodes(self) -> None:
        await self.follow(episode(1, date(2026, 11, 1), number=1))
        self.downloads_page((record(MediaType.TV, SEVERANCE, 2, None), DownloadState.DOWNLOADING))
        items = await self.items(**{"from": "2026-11-01"})
        self.assertEqual(items[0]["state"], "downloading")

    async def test_another_episode_on_the_downloads_page_changes_nothing(self) -> None:
        await self.follow(episode(1, date(2026, 11, 1), number=1))
        self.downloads_page((record(MediaType.TV, SEVERANCE, 2, 9), DownloadState.READY))
        items = await self.items(**{"from": "2026-11-01"})
        self.assertEqual(items[0]["state"], "can_download")

    async def test_a_grab_in_the_history_alone_shows_no_mark_as_on_the_pcs_cards(self) -> None:
        # Owner's decision for 3.1.0: without Plex the cards keep their Download button; a tap
        # says it was downloaded already.
        await self.follow(episode(1, date(2026, 11, 1), number=1))
        await self.db.history.add(record(MediaType.TV, SEVERANCE, 2, 1))
        items = await self.items(**{"from": "2026-11-01"})
        self.assertEqual(items[0]["state"], "can_download")

    async def test_in_plex_or_on_its_way_there_is_downloaded(self) -> None:
        await self.follow(
            episode(1, date(2026, 11, 1), number=1),
            episode(2, date(2026, 11, 1), number=2),
            episode(3, date(2026, 11, 1), number=3),
        )
        await self.want(film(), digital=date(2026, 11, 7))
        with (
            mock.patch.object(self.library, "plex_between", return_value=frozenset({1})),
            mock.patch.object(self.library, "downloaded_between", return_value=frozenset({2})),
            mock.patch.object(
                self.library, "plex_films", return_value=PlexFilms(tmdb=frozenset({DUNE}))
            ),
        ):
            items = await self.items(**{"from": "2026-11-01"})
        self.assertEqual(
            [item["state"] for item in items],
            ["downloaded", "downloaded", "can_download", "downloaded"],
        )

    async def test_still_on_its_way_beats_the_mark_from_the_history(self) -> None:
        await self.follow(episode(2, date(2026, 11, 1), number=1))
        self.downloads_page((record(MediaType.TV, SEVERANCE, 2, 1), DownloadState.DOWNLOADING))
        with mock.patch.object(self.library, "downloaded_between", return_value=frozenset({2})):
            items = await self.items(**{"from": "2026-11-01"})
        self.assertEqual(items[0]["state"], "downloading")

    async def test_a_run_at_work_is_downloading(self) -> None:
        await self.follow(episode(1, date(2026, 11, 1), number=1))
        target = GrabTarget(media_type=MediaType.TV, media_id=SEVERANCE, season=2, episode=1)
        with mock.patch.object(self.queue, "is_active", return_value=True):
            items = await self.items(**{"from": "2026-11-01"})
        self.assertEqual(items[0]["state"], "downloading")  # waiting in the PC's queue
        async with self.quick._target_lock(target):  # any one-click run: queue, phone, automatic
            items = await self.items(**{"from": "2026-11-01"})
            self.assertEqual(items[0]["state"], "downloading")
        items = await self.items(**{"from": "2026-11-01"})
        self.assertEqual(items[0]["state"], "can_download")

    async def test_just_grabbed_is_downloading_until_the_downloads_page_catches_up(self) -> None:
        await self.follow(episode(1, date(2026, 11, 1), number=1))
        target = GrabTarget(media_type=MediaType.TV, media_id=SEVERANCE, season=2, episode=1)
        listener = self.grabs.subscribe_grabs.call_args.args[0]
        listener(target)
        items = await self.items(**{"from": "2026-11-01"})
        self.assertEqual(items[0]["state"], "downloading")
        self.downloads_page((record(MediaType.TV, SEVERANCE, 2, 1), DownloadState.READY))
        items = await self.items(**{"from": "2026-11-01"})
        self.assertEqual(items[0]["state"], "downloaded")
        self.status.latest = None
        self.seconds += JUST_GRABBED_S + 1
        items = await self.items(**{"from": "2026-11-01"})
        self.assertEqual(items[0]["state"], "can_download")

    async def test_a_poster_address_that_could_hold_a_key_is_left_out(self) -> None:
        for bad in (
            "https://images.example/p.jpg?api_key=SECRET",
            "https://user:SECRET@images.example/p.jpg",
            "https://images.example/p.jpg#SECRET",
            "file:///C:/posters/p.jpg",
        ):
            with self.subTest(bad):
                shown = show().model_copy(update={"poster_url": bad})
                await self.db.library.save_show(shown, (episode(1, TODAY),))
                await self.db.library.track(SEVERANCE, profile_id=self.person)
                items = await self.items()
                self.assertIsNone(items[0]["poster_url"])
                self.assertNotIn("SECRET", json.dumps(items))
