"""The phone's actor page (`GET /v1/person`) and the download progress on the phone's items
(docs/companion/2026-11-18-person-progress-design.md). The real DiscoveryService, LibraryService,
PhoneFind and PhoneDownloads over a fake TMDB and TVMaze (FindCase); the Downloads page is a fake
whose `latest` is set by hand. Nothing here reaches TMDB, TVMaze, Plex or a torrent program."""

from __future__ import annotations

import json
import unittest
from datetime import date, timedelta
from typing import Any, cast
from unittest import mock

import httpx

from app.i18n import t
from app.models import (
    DiscoverShow,
    DownloadEntry,
    DownloadRecord,
    DownloadsOverview,
    DownloadState,
    GrabTarget,
    MediaType,
    Movie,
    PairedPhone,
)
from app.models.downloads import MovedTo
from app.models.person import Person, PersonCredit, PersonMatch
from app.services.discovery import DiscoveryService
from app.services.download_status import DownloadStatusService
from app.services.phone.coming_up import ComingUp, GrabbedFilms
from app.services.phone.person import PERSON_CREDITS, PhonePerson
from app.services.phone.progress import Progress
from app.services.phone.server import Reply, Request
from app.services.tmdb import (
    TMDBAuthError,
    TMDBClient,
    TMDBError,
    TMDBNotConfigured,
    TMDBNotFound,
)
from tests.test_phone_coming_up import NOW, SEVERANCE, PhoneCase, episode
from tests.test_phone_find import (
    ARRIVAL,
    ARRIVAL_RELEASE,
    COULDNT_SEARCH,
    EPISODES,
    KEY_REFUSED,
    NO_KEY,
    FindCase,
)
from tests.test_phone_suggestions import (
    OUT_LAST_YEAR,
    TMDB_SEVERANCE,
    TVMAZE_SEVERANCE,
    film,
    tmdb_show,
    tvmaze_show,
)
from tests.test_tmdb import load

RENEE = 7001
NAME = "Renée Marlowe"
NOT_FOUND = "AirGrabber couldn't find {name} on TMDB."
KEY = "0123456789abcdef0123456789abcdef"


def part(title: Movie | DiscoverShow, character: str = "A part") -> PersonCredit:
    return PersonCredit(title=title, character=character)


def person(*credits: PersonCredit, id: int = RENEE, **fields: Any) -> Person:
    return Person(id=id, name=NAME, credits=credits, **fields)


def grab(
    media_type: MediaType,
    media_id: int,
    season: int | None = None,
    number: int | None = None,
    *,
    hours_ago: float = 1,
) -> DownloadRecord:
    return DownloadRecord(
        media_type=media_type,
        media_id=media_id,
        season=season,
        episode=number,
        release_title="Some.Release.1080p",
        downloaded_at=NOW - timedelta(hours=hours_ago),
    )


def row(
    record: DownloadRecord,
    state: DownloadState = DownloadState.DOWNLOADING,
    progress: float | None = None,
    *,
    moved: bool = False,
) -> DownloadEntry:
    place = MovedTo(folder=r"\\nas\films\X", shown=("Films", "X")) if moved else None
    return DownloadEntry(
        record=record, title="A title", state=state, progress=progress, moved_to=place
    )


def page(*rows: DownloadEntry) -> DownloadsOverview:
    return DownloadsOverview(entries=rows, program="qBittorrent")


# -- the progress itself ------------------------------------------------------------------------


class ProgressTests(unittest.TestCase):
    def test_unfinished_is_the_share_done_rounded_down_at_most_99(self) -> None:
        cases = [
            (DownloadState.DOWNLOADING, 0.0, 0),
            (DownloadState.DOWNLOADING, 0.426, 42),
            (DownloadState.DOWNLOADING, 0.999, 99),
            (DownloadState.DOWNLOADING, 1.0, 99),  # not finished until the program says so
            (DownloadState.WAITING, 0.5, 50),
            (DownloadState.STOPPED, 0.3, 30),  # paused
            (DownloadState.PROBLEM, 0.7, 70),
            (DownloadState.PENDING, None, None),  # the program hasn't got it yet
        ]
        for state, done, wanted in cases:
            with self.subTest(state=state, done=done):
                progress = Progress(page(row(grab(MediaType.MOVIE, 1), state, done)))
                self.assertEqual(progress.film(1), wanted)

    def test_finished_or_moved_by_mediaforge_is_100(self) -> None:
        for entry in (
            row(grab(MediaType.MOVIE, 1), DownloadState.READY, 1.0),
            row(grab(MediaType.MOVIE, 1), DownloadState.READY, None),
            row(grab(MediaType.MOVIE, 1), DownloadState.DOWNLOADING, 0.4, moved=True),
        ):
            with self.subTest(entry=entry.state):
                self.assertEqual(Progress(page(entry)).film(1), 100)

    def test_what_the_program_never_had_or_cant_tell_is_null(self) -> None:
        for state in (DownloadState.NOT_FOUND, DownloadState.UNKNOWN):
            with self.subTest(state):
                progress = Progress(page(row(grab(MediaType.MOVIE, 1), state, 0.5)))
                self.assertIsNone(progress.film(1))

    def test_no_check_yet_or_not_on_the_page_is_null(self) -> None:
        self.assertIsNone(Progress(None).film(1))
        self.assertIsNone(Progress(None).episode(SEVERANCE, 2, 1))
        self.assertIsNone(Progress(page(row(grab(MediaType.MOVIE, 2), progress=0.5))).film(1))
        # A film and a show may share a number: they never stand in for each other.
        show_row = row(grab(MediaType.TV, 1, 1, 1), progress=0.5)
        self.assertIsNone(Progress(page(show_row)).film(1))

    def test_a_season_pack_covers_its_episodes_only(self) -> None:
        progress = Progress(page(row(grab(MediaType.TV, SEVERANCE, 2), progress=0.25)))
        self.assertEqual(progress.episode(SEVERANCE, 2, 1), 25)
        self.assertEqual(progress.episode(SEVERANCE, 2, 7), 25)
        self.assertIsNone(progress.episode(SEVERANCE, 1, 1))
        self.assertIsNone(progress.episode(SEVERANCE + 1, 2, 1))
        other = Progress(page(row(grab(MediaType.TV, SEVERANCE, 2, 9), progress=0.25)))
        self.assertIsNone(other.episode(SEVERANCE, 2, 1))

    def test_the_newest_grab_of_a_title_wins(self) -> None:
        older = row(grab(MediaType.TV, SEVERANCE, 2, 1, hours_ago=5), DownloadState.READY)
        newer_pack = row(grab(MediaType.TV, SEVERANCE, 2, hours_ago=1), progress=0.1)
        self.assertEqual(Progress(page(older, newer_pack)).episode(SEVERANCE, 2, 1), 10)
        older_pack = row(grab(MediaType.TV, SEVERANCE, 2, hours_ago=9), DownloadState.READY)
        newer = row(grab(MediaType.TV, SEVERANCE, 2, 1, hours_ago=1), progress=0.6)
        self.assertEqual(Progress(page(older_pack, newer)).episode(SEVERANCE, 2, 1), 60)
        again = row(grab(MediaType.MOVIE, 1, hours_ago=1), progress=0.33)
        first = row(grab(MediaType.MOVIE, 1, hours_ago=48), DownloadState.READY)
        for order in ((again, first), (first, again)):
            with self.subTest(order=[one.progress for one in order]):
                self.assertEqual(Progress(page(*order)).film(1), 33)

    def test_a_grab_on_its_way_beats_a_newer_one_that_isnt(self) -> None:
        # Coming up says `downloading` while any grab of it is on its way: its share is said.
        stuck = row(grab(MediaType.MOVIE, 1, hours_ago=30), DownloadState.DOWNLOADING, 0.3)
        for newer_state in (DownloadState.READY, DownloadState.NOT_FOUND, DownloadState.UNKNOWN):
            with self.subTest(newer_state):
                newer = row(grab(MediaType.MOVIE, 1, hours_ago=1), newer_state, None)
                self.assertEqual(Progress(page(stuck, newer)).film(1), 30)
        older_pack = row(grab(MediaType.TV, SEVERANCE, 2, hours_ago=9), DownloadState.WAITING, 0.2)
        newer = row(grab(MediaType.TV, SEVERANCE, 2, 1, hours_ago=1), DownloadState.READY)
        self.assertEqual(Progress(page(older_pack, newer)).episode(SEVERANCE, 2, 1), 20)
        # Of several on their way, the newest; a moved one isn't on its way.
        going = row(grab(MediaType.MOVIE, 1, hours_ago=2), DownloadState.STOPPED, 0.9)
        moved = row(grab(MediaType.MOVIE, 1, hours_ago=1), DownloadState.DOWNLOADING, 0.1,
                    moved=True)
        self.assertEqual(Progress(page(stuck, going)).film(1), 90)
        self.assertEqual(Progress(page(going, moved)).film(1), 90)


def status_reading(overview: DownloadsOverview | None) -> Any:
    """A Downloads page whose `latest` is `overview`; a check asked of it would show."""
    status = mock.Mock(spec=DownloadStatusService)
    status.latest = overview
    return status


# -- on Coming up -------------------------------------------------------------------------------


class ComingUpProgressTests(PhoneCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.status = status_reading(None)
        self.films = GrabbedFilms(cast(DownloadStatusService, self.status))
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

    async def items(self) -> list[dict[str, Any]]:
        request = Request("GET", "/v1/coming-up", {"from": "2026-11-01"}, {}, self.phone)
        reply = await self.service.routes()[("GET", "/v1/coming-up")](request)
        self.assertEqual(reply.status, 200, reply.body)
        json.dumps(reply.body)
        return cast(list[dict[str, Any]], reply.body["items"])

    async def test_an_episode_on_its_way_says_how_far(self) -> None:
        await self.follow(episode(1, date(2026, 11, 1), number=1))
        self.status.latest = page(row(grab(MediaType.TV, SEVERANCE, 2, 1), progress=0.427))
        [item] = await self.items()
        self.assertEqual((item["state"], item["progress"]), ("downloading", 42))

    async def test_a_season_pack_on_its_way_says_it_for_its_episodes(self) -> None:
        await self.follow(episode(1, date(2026, 11, 1), number=1))
        self.status.latest = page(row(grab(MediaType.TV, SEVERANCE, 2), DownloadState.STOPPED, 0.8))
        [item] = await self.items()
        self.assertEqual((item["state"], item["progress"]), ("downloading", 80))

    async def test_a_film_on_its_way_says_how_far(self) -> None:
        await self.want(film(ARRIVAL), digital=OUT_LAST_YEAR)
        self.status.latest = page(row(grab(MediaType.MOVIE, ARRIVAL), progress=0.05))
        [item] = await self.items()
        self.assertEqual((item["state"], item["progress"]), ("downloading", 5))

    async def test_only_downloading_items_carry_progress(self) -> None:
        await self.follow(episode(1, date(2026, 11, 1), number=1))
        await self.want(film(ARRIVAL), digital=OUT_LAST_YEAR)
        self.status.latest = page(row(grab(MediaType.MOVIE, ARRIVAL), DownloadState.READY))
        items = await self.items()
        self.assertEqual(
            sorted(item["state"] for item in items), ["can_download", "downloaded"]
        )
        for item in items:
            self.assertNotIn("progress", item)

    async def test_just_grabbed_and_not_on_the_page_yet_is_null(self) -> None:
        await self.follow(episode(1, date(2026, 11, 1), number=1))
        target = grab(MediaType.TV, SEVERANCE, 2, 1)
        self.grabs.subscribe_grabs.call_args.args[0](
            GrabTarget(media_type=MediaType.TV, media_id=SEVERANCE, season=2, episode=1)
        )
        self.status.latest = page(row(target, DownloadState.UNKNOWN))
        [item] = await self.items()
        self.assertEqual(item["state"], "downloading")
        self.assertIsNone(item["progress"])

    async def test_a_phone_never_makes_the_pc_ask_the_torrent_program(self) -> None:
        await self.follow(episode(1, date(2026, 11, 1), number=1))
        self.status.latest = page(row(grab(MediaType.TV, SEVERANCE, 2, 1), progress=0.5))
        await self.items()
        self.assertEqual(self.status.mock_calls, [])  # `latest` read, nothing asked


# -- on find --------------------------------------------------------------------------------------


class FindProgressTests(FindCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.status = status_reading(None)
        self.find = self.new_find()
        self.use_sources(self.good(ARRIVAL_RELEASE))

    async def test_a_downloading_film_says_how_far_and_the_others_carry_none(self) -> None:
        self.grabs.has_grabbed.side_effect = lambda target: target.media_id in {1, 2}
        self.status.latest = page(row(grab(MediaType.MOVIE, 1), progress=0.5))
        self.films_found(film(1), film(2), film(3))
        self.shows_found(tmdb_show())
        results = await self.found()
        self.assertEqual(
            [(item["state"], item.get("progress", "none")) for item in results],
            [("downloading", 50), ("downloading", None), ("new", "none"),
             ("following", "none")],  # the phone's person follows Severance
        )
        self.assertIn("progress", results[1])  # null, not left out, when it's downloading
        self.assertEqual(self.status.mock_calls, [])

    async def test_a_series_episodes_say_how_far_theirs_are(self) -> None:
        await self.db.library.save_show(tvmaze_show(), EPISODES)  # known, followed by none
        await self.plex_has(episodes=frozenset({(TVMAZE_SEVERANCE, 1, 1)}))
        for season, number in ((1, 2), (2, None)):  # S01E02, and S02 as a pack
            await self.db.history.add(grab(MediaType.TV, TVMAZE_SEVERANCE, season, number))
        self.status.latest = page(
            row(grab(MediaType.TV, TVMAZE_SEVERANCE, 1, 2), DownloadState.READY),
            row(grab(MediaType.TV, TVMAZE_SEVERANCE, 2), progress=0.25),
        )
        self.shows_found(tmdb_show())
        await self.found()
        reply = await self.episodes(TMDB_SEVERANCE)
        self.assertEqual(reply.status, 200, reply.body)
        said = [
            (season["number"], one["number"], one["state"], one.get("progress", "none"))
            for season in reply.body["seasons"]
            for one in season["episodes"]
        ]
        self.assertEqual(
            said,
            [
                (1, 1, "in_library", "none"),
                (1, 2, "downloading", 100),
                (2, 1, "downloading", 25),
                (2, 2, "new", "none"),
                (3, 1, "new", "none"),
            ],
        )
        self.assertEqual(self.status.mock_calls, [])


# -- the actor page -------------------------------------------------------------------------------


class PersonCase(FindCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.status = status_reading(None)
        self.find = self.new_find()
        self.use_sources(self.good(ARRIVAL_RELEASE))
        self.renee = person(
            part(film(ARRIVAL, "Arrival", overview="A linguist.", popularity=12.5), "Louise"),
            part(tmdb_show(overview="Work.", popularity=40.0), "Helly"),
            biography="A made-up actor.",
            birthday=date(1971, 4, 2),
            place_of_birth="Ghent, Belgium",
            known_for="Acting",
            profile_path="/renee.jpg",
        )
        self.tmdb.person = mock.AsyncMock(side_effect=self.person_of)
        self.tmdb.search_people = mock.AsyncMock(return_value=[PersonMatch(id=RENEE, name=NAME)])
        self.actor = PhonePerson(self.find, self.discovery, self.profiles)

    async def person_of(self, id: int) -> Person:
        if id != RENEE:
            raise TMDBNotFound("TMDB /person/{id}: not found (HTTP 404)")
        return self.renee

    async def look_up(self, query: dict[str, str], phone: PairedPhone | None = None) -> Reply:
        request = Request("GET", "/v1/person", query, {}, phone or self.phone)
        reply: Reply = await self.actor.routes()[("GET", "/v1/person")](request)
        json.dumps(reply.body)  # what the server sends
        return reply

    async def answer(self, **query: str) -> dict[str, Any]:
        query.setdefault("name", NAME)
        reply = await self.look_up(query)
        self.assertEqual(reply.status, 200, reply.body)
        return reply.body

    async def credits(self, **query: str) -> list[dict[str, Any]]:
        return cast(list[dict[str, Any]], (await self.answer(**query))["credits"])


class PersonTests(PersonCase):
    async def test_by_tmdb_id_with_find_results_as_credits(self) -> None:
        await self.library.untrack(SEVERANCE, profile_id=self.person)  # TVMaze's, by name
        body = await self.answer(tmdb_id=str(RENEE))
        self.tmdb.person.assert_awaited_once_with(RENEE)
        self.tmdb.search_people.assert_not_awaited()
        self.assertEqual(
            body,
            {
                "id": RENEE,
                "name": NAME,
                "photo_url": "https://image.tmdb.org/t/p/w342/renee.jpg",
                "biography": "A made-up actor.",
                "birthday": "1971-04-02",
                "deathday": None,
                "place_of_birth": "Ghent, Belgium",
                "known_for": "Acting",
                "credits": [
                    {
                        "kind": "series",
                        "id": TMDB_SEVERANCE,
                        "title": "Severance",
                        "year": 2022,
                        "poster_url": f"https://image.tmdb.org/t/p/w342/{TMDB_SEVERANCE}.jpg",
                        "overview": "Work.",
                        "state": "new",
                        "out": True,
                        "character": "Helly",
                        "popularity": 40.0,
                    },
                    {
                        "kind": "movie",
                        "id": ARRIVAL,
                        "title": "Arrival",
                        "year": 2025,
                        "poster_url": f"https://image.tmdb.org/t/p/w342/{ARRIVAL}.jpg",
                        "overview": "A linguist.",
                        "state": "new",
                        "out": True,
                        "character": "Louise",
                        "popularity": 12.5,
                    },
                ],
            },
        )

    async def test_each_credit_is_exactly_a_find_result_plus_its_part(self) -> None:
        self.grabs.has_grabbed.side_effect = lambda target: target.media_id == ARRIVAL
        credits = await self.credits()
        self.films_found(film(ARRIVAL, "Arrival", overview="A linguist.", popularity=12.5))
        self.shows_found(tmdb_show(overview="Work.", popularity=40.0))
        found = {(item["kind"], item["id"]): item for item in await self.found()}
        for credit in credits:
            with self.subTest(credit["kind"]):
                bare = {k: v for k, v in credit.items() if k not in {"character", "popularity"}}
                self.assertEqual(bare, found[(credit["kind"], credit["id"])])

    async def test_a_missing_photo_text_and_dates_are_null_or_empty(self) -> None:
        self.renee = person()
        body = await self.answer()
        self.assertEqual(
            {key: body[key] for key in ("photo_url", "biography", "birthday", "deathday",
                                        "place_of_birth", "known_for", "credits")},
            {"photo_url": None, "biography": "", "birthday": None, "deathday": None,
             "place_of_birth": None, "known_for": None, "credits": []},
        )
        self.renee = person(deathday=date(2020, 1, 31), profile_path="/x.jpg?api_key=SECRET")
        body = await self.answer()
        self.assertEqual((body["deathday"], body["photo_url"]), ("2020-01-31", None))

    async def test_by_name_the_folded_name_beats_tmdbs_first(self) -> None:
        self.tmdb.search_people.return_value = [
            PersonMatch(id=7002, name="Rene Marlow"),
            PersonMatch(id=RENEE, name="Renée  MARLOWE"),
        ]
        body = await self.answer(name="  renee marlowe ")
        self.tmdb.search_people.assert_awaited_once_with("renee marlowe")
        self.tmdb.person.assert_awaited_once_with(RENEE)
        self.assertEqual(body["id"], RENEE)

    async def test_by_name_else_tmdbs_first(self) -> None:
        self.tmdb.search_people.return_value = [
            PersonMatch(id=RENEE, name="Renée Marlowe-Smith"),
            PersonMatch(id=7003, name="Someone Else"),
        ]
        await self.answer(name="Renee M.")
        self.tmdb.person.assert_awaited_once_with(RENEE)

    async def test_an_id_tmdb_doesnt_know_falls_back_to_the_name(self) -> None:
        body = await self.answer(tmdb_id="999")
        self.assertEqual(
            [call.args for call in self.tmdb.person.await_args_list], [(999,), (RENEE,)]
        )
        self.tmdb.search_people.assert_awaited_once_with(NAME)
        self.assertEqual(body["id"], RENEE)

    async def test_no_one_by_that_name_is_not_found(self) -> None:
        self.tmdb.search_people.return_value = []
        for query in ({}, {"tmdb_id": "999"}):
            with self.subTest(query):
                reply = await self.look_up({"name": NAME, **query})
                self.assertEqual(reply.status, 404)
                self.assertEqual(
                    reply.body, {"error": "not_found", "message": t(NOT_FOUND, name=NAME)}
                )

    async def test_a_name_or_id_that_cant_be_read_is_a_bad_request(self) -> None:
        self.assertEqual((await self.look_up({"name": "R"})).status, 200)
        self.assertEqual((await self.look_up({"name": "x" * 100})).status, 200)
        self.assertEqual((await self.look_up({"name": NAME, "tmdb_id": "9" * 10})).status, 200)
        # An empty id is none: by name.
        self.tmdb.person.reset_mock()
        self.assertEqual((await self.look_up({"name": NAME, "tmdb_id": ""})).status, 200)
        self.tmdb.search_people.assert_awaited_with(NAME)
        self.tmdb.person.assert_awaited_once_with(RENEE)
        for query in (
            {},
            {"name": ""},
            {"name": "   "},
            {"name": "x" * 101},
            {"tmdb_id": str(RENEE)},  # the name is needed too: it's what is said and the fallback
            {"name": NAME, "tmdb_id": "abc"},
            {"name": NAME, "tmdb_id": "-1"},
            {"name": NAME, "tmdb_id": "1.5"},
            {"name": NAME, "tmdb_id": "9" * 11},
            {"name": NAME, "tmdb_id": "\u0663"},  # a digit, but not one TMDB writes
        ):
            with self.subTest(query):
                reply = await self.look_up(query)
                self.assertEqual(reply.status, 400)
                self.assertEqual(reply.body["error"], "bad_request")

    async def test_without_a_tmdb_key_nothing_is_asked_and_it_says_so(self) -> None:
        self.tmdb.configured = False
        reply = await self.look_up({"name": NAME})
        self.assertEqual(
            (reply.status, reply.body), (409, {"error": "no_tmdb_key", "message": t(NO_KEY)})
        )
        self.tmdb.person.assert_not_awaited()
        self.tmdb.search_people.assert_not_awaited()

    async def test_a_key_that_disappears_or_is_refused_says_so_with_finds_sentences(self) -> None:
        for failure, code, said in (
            (TMDBNotConfigured("TMDB API key is not set"), "no_tmdb_key", NO_KEY),
            (TMDBAuthError("refused"), "tmdb_key_refused", KEY_REFUSED),
        ):
            with self.subTest(code):
                self.tmdb.person.side_effect = failure
                reply = await self.look_up({"name": NAME, "tmdb_id": str(RENEE)})
                self.assertEqual(
                    (reply.status, reply.body), (409, {"error": code, "message": t(said)})
                )

    async def test_tmdb_out_of_reach_is_a_plain_502_as_find(self) -> None:
        self.tmdb.search_people.side_effect = TMDBError("SECRET")
        with self.assertLogs("app", "DEBUG") as logs:
            reply = await self.look_up({"name": NAME})
        self.assertEqual(reply.status, 502)
        self.assertEqual(reply.body, {"error": "failed", "message": t(COULDNT_SEARCH)})
        text = "\n".join(logs.output)
        self.assertIn("Phone person: TMDB couldn't be asked: TMDBError", text)
        self.assertNotIn("Phone find", text)
        self.assertNotIn("SECRET", text)

    async def test_a_deleted_person_gets_a_plain_answer(self) -> None:
        reply = await self.look_up({"name": NAME}, self.phone_of(12345))
        self.assertEqual(reply.status, 401)
        self.assertEqual(reply.body["error"], "not_paired")
        self.tmdb.search_people.assert_not_awaited()


class CreditTests(PersonCase):
    async def test_talk_news_and_reality_series_parts_as_themself_and_no_title_are_left_out(
        self,
    ) -> None:
        self.renee = person(
            part(tmdb_show(1, "Late Talk", genre_ids=(35, 10767)), "Guest"),
            part(tmdb_show(2, "The News", genre_ids=(10763,)), "Anchor"),
            part(tmdb_show(3, "Island Love", genre_ids=(10764,)), "Contestant"),
            part(film(11), "Self"),
            part(film(12), "Himself (voice)"),
            part(film(13), "Self - Host"),
            part(film(14), "HERSELF (archive footage)"),
            part(tmdb_show(4, "Four"), "Themselves"),
            part(film(15, " "), "A part"),  # no title
            part(tmdb_show(5, ""), "A part"),
            part(film(16), "Selfridge"),  # not the word itself
            part(film(17), ""),
            part(tmdb_show(6, "Drama", genre_ids=(18,)), "Lead"),
            part(film(18), "Future Self"),  # parts named after one, not the person themself
            part(film(19), "Older Self"),
            part(film(20), "Ivy's Inner Self (voice)"),
        )
        credits = await self.credits()
        self.assertEqual(
            sorted((item["kind"], item["id"], item["character"]) for item in credits),
            [("movie", 16, "Selfridge"), ("movie", 17, ""), ("movie", 18, "Future Self"),
             ("movie", 19, "Older Self"), ("movie", 20, "Ivy's Inner Self (voice)"),
             ("series", 6, "Lead")],
        )

    async def test_each_title_once_with_its_first_part(self) -> None:
        self.renee = person(
            part(film(11), "Self"),  # left out: the next part of it counts
            part(film(11), "First"),
            part(film(11), "Second"),
            part(tmdb_show(11, "Same number"), "On TV"),  # a series is another title
        )
        credits = await self.credits()
        self.assertEqual(
            sorted((item["kind"], item["id"], item["character"]) for item in credits),
            [("movie", 11, "First"), ("series", 11, "On TV")],
        )

    async def test_the_most_popular_first_at_most_forty(self) -> None:
        self.assertEqual(PERSON_CREDITS, 40)
        parts = [part(film(100 + n, popularity=float(n))) for n in range(30)]
        parts += [part(tmdb_show(200 + n, f"Show {n}", popularity=n + 0.5)) for n in range(30)]
        self.renee = person(*parts)
        credits = await self.credits()
        self.assertEqual(len(credits), 40)
        popularity = [item["popularity"] for item in credits]
        self.assertEqual(popularity, sorted(popularity, reverse=True))
        self.assertEqual(popularity[0], 29.5)
        self.assertEqual(popularity[-1], 10.0)  # 29.5 down to 10.0 by halves: 40 credits

    async def test_a_credit_on_its_way_says_how_far(self) -> None:
        self.grabs.has_grabbed.side_effect = lambda target: target.media_id == ARRIVAL
        self.status.latest = page(row(grab(MediaType.MOVIE, ARRIVAL), progress=0.71))
        by_kind = {item["kind"]: item for item in await self.credits()}
        self.assertEqual((by_kind["movie"]["state"], by_kind["movie"]["progress"]),
                         ("downloading", 71))
        self.assertNotIn("progress", by_kind["series"])  # a series is never downloading
        self.assertEqual(self.status.mock_calls, [])


class FoundCreditTests(PersonCase):
    """Every credit is remembered as found: the found routes take it as a search result."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.credits()

    async def test_a_credited_film_downloads_and_goes_on_the_list(self) -> None:
        reply = await self.post("/v1/download", {"kind": "movie", "id": ARRIVAL, "found": True})
        self.assertEqual(reply.body["outcome"], "grabbed", reply.body)
        listed = [entry.movie_id for entry in await self.watchlist.by_release_of(self.person)]
        self.assertEqual(listed, [ARRIVAL])

    async def test_a_credited_series_has_its_episodes_and_may_be_followed(self) -> None:
        reply = await self.episodes(TMDB_SEVERANCE)
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual([season["number"] for season in reply.body["seasons"]], [1, 2, 3])
        self.assertEqual((await self.follow_it({"id": TMDB_SEVERANCE})).status, 200)

    async def test_a_credited_series_episode_downloads(self) -> None:
        self.use_sources(self.good("Severance.S01E02.1080p.WEB.x264-GROUP"))
        body = {"kind": "found_episode", "id": TMDB_SEVERANCE, "season": 1, "episode": 2}
        reply = await self.post("/v1/download", body)
        self.assertEqual(reply.body["outcome"], "grabbed", reply.body)


class PersonLogTests(PersonCase):
    async def test_the_log_never_holds_the_name_ids_titles_or_characters(self) -> None:
        self.tmdb.search_people.return_value = [PersonMatch(id=RENEE, name=NAME)]
        with self.assertLogs("app", "DEBUG") as logs:
            await self.answer(tmdb_id=str(RENEE))
            await self.answer(tmdb_id="4242")  # not on TMDB: by name
            self.tmdb.search_people.return_value = []
            await self.look_up({"name": "Nobody Named Thus"})
        text = "\n".join(logs.output)
        for secret in (NAME, "Renée", "Nobody Named Thus", str(RENEE), "4242", "Arrival",
                       "Severance", "Louise", "Helly", str(ARRIVAL), str(TMDB_SEVERANCE)):
            self.assertNotIn(secret, text)

    async def test_through_the_real_client_nothing_identifying_is_logged(self) -> None:
        def answer(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/3/search/person":
                return httpx.Response(200, json=load("extras_search_person.json"))
            if request.url.path == "/3/person/7001":
                return httpx.Response(200, json=load("extras_person.json"))
            return httpx.Response(503)  # a credit's details

        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
            client = TMDBClient(http, api_key=lambda: KEY, retry_delay=0)
            self.discovery = DiscoveryService(client, self.tvmaze, self.library, clock=lambda: NOW)
            self.find = self.new_find()
            self.actor = PhonePerson(self.find, self.discovery, self.profiles)
            with self.assertLogs("app", "DEBUG") as logs:
                credits = await self.credits(name="renee marlowe")
        self.assertEqual(
            [(item["kind"], item["id"], item["character"]) for item in credits],
            [("series", 9901, "Nell Quinn"), ("movie", 8801, "Dr. Ilse Varga"),
             ("series", 9903, "")],
        )
        text = "\n".join(logs.output)
        self.assertIn("/movie/{id}", text)  # a credit's details were retried, by its kind only
        for secret in ("Renée", "renee", "7001", "8801", "9901", "Quiet Orbit", "Harbour",
                       "Nell Quinn", "Ilse", KEY):
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
