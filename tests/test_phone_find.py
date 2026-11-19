"""Find on the phone: films and series not in the library, by name. The real DiscoveryService and
LibraryService over a fake TMDB and TVMaze; downloads go through the real PhoneDownloads and
search, with fake sources and a fake DownloadService. Nothing here reaches TMDB, TVMaze, Plex or
a torrent program."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, cast
from unittest import mock

import httpx

from app.i18n import t
from app.models import (
    DiscoverShow,
    DownloadRecord,
    DownloadState,
    Episode,
    GrabTarget,
    MediaType,
    Movie,
    MovieDetails,
    PairedPhone,
    SearchKind,
    TVDetails,
)
from app.models.folder_library import LibraryFile
from app.models.plex import PlexEpisode, PlexTitle
from app.services.discovery import DiscoveryService
from app.services.download_status import DownloadStatusService
from app.services.downloads import GrabError
from app.services.phone.find import FIND_LIMIT, FOUND_KEEP, PhoneFind
from app.services.phone.server import Reply, Request
from app.services.plex import PlexService, PlexUnreachable
from app.services.tmdb import TMDBAuthError, TMDBClient, TMDBError, TMDBNotConfigured
from app.services.tvmaze import TVMazeError
from app.services.watchlist import WatchlistService
from tests.test_phone_coming_up import NOW, SEVERANCE, TODAY
from tests.test_phone_suggestions import (
    NO_LOOKUP,
    NO_MATCH,
    NOT_OUT,
    OUT_LAST_YEAR,
    PACK,
    TMDB_SEVERANCE,
    TVMAZE_SEVERANCE,
    SuggestionsCase,
    film,
    tmdb_show,
    tvmaze_show,
)

ARRIVAL = 329865
ARRIVAL_RELEASE = "Arrival.2025.1080p.WEB.x264-GROUP"
TYPED = "arrival of the severance"
NO_KEY = "Searching needs a TMDB key: add one in AirGrabber's Settings."
KEY_REFUSED = "TMDB refused the key in AirGrabber's Settings, so check it there."
SERIES_FOLDER = r"\\nas\tv"
COULDNT_SEARCH = "AirGrabber couldn't search just now, so try again in a moment."
NOTHING_FOUND = "Nothing was found by that name, so try other words."
SEARCH_AGAIN = "That search isn't there any more, so search again."
FOLLOWING = "You follow {title} now."
ALREADY = "You already follow {title}."
CANT_FOLLOW = "AirGrabber can't find {title} to follow, so choose something else."
SEASON_TWO = GrabTarget(media_type=MediaType.TV, media_id=TVMAZE_SEVERANCE, season=2)


def aired(id: int, season: int, number: int | None, day: date, name: str = "") -> Episode:
    return Episode(
        id=id, show_id=TVMAZE_SEVERANCE, season=season, number=number, air_date=day, name=name
    )


EPISODES = [
    aired(11, 1, 1, TODAY - timedelta(days=400), "Good News About Hell"),
    aired(12, 1, 2, TODAY - timedelta(days=393), "Half Loop"),
    aired(13, 1, None, TODAY - timedelta(days=390), "Inside Lumon"),  # a special
    aired(21, 2, 1, TODAY - timedelta(days=3), "Hello, Ms. Cobel"),
    aired(22, 2, 2, TODAY + timedelta(days=4), "Goodbye, Mrs. Selvig"),
    aired(31, 3, 1, TODAY + timedelta(days=300), "Unwritten"),
]


class FindCase(SuggestionsCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.tmdb.search_movies = mock.AsyncMock(return_value=[])
        self.tmdb.search_tv = mock.AsyncMock(return_value=[])
        self.tvmaze.get_show_with_episodes.return_value = (tvmaze_show(), list(EPISODES))
        # A watchlist that reads release dates through Discovery, as the app's does.
        self.watchlist = WatchlistService(
            self.db.watchlist, self.discovery, self.grabs, settings=self.settings, clock=lambda: NOW
        )
        self.suggestions = self.new_suggestions()
        # Plex's side: the last check's titles and a show's episodes as Plex has them now.
        self.plex: Any = mock.Mock(spec=PlexService)
        self.plex.titles = mock.AsyncMock(return_value=[])
        self.plex.episodes = mock.AsyncMock(return_value=[])
        self.find = self.new_find()
        self.use_sources(self.good(PACK))

    def new_find(self) -> PhoneFind:
        return PhoneFind(
            self.library,
            self.watchlist,
            self.discovery,
            self.grabs,
            self.profiles,
            self.suggestions,
            settings=self.settings,
            plex=self.plex,
            files=self.db.library_files,
            status=cast(DownloadStatusService, self.status),
            now=lambda: NOW,
        )

    async def in_the_folders(self, tmdb_id: int, *episodes: tuple[int, int, int | None]) -> None:
        """The folder library has these files of a show: (season, episode, last episode)."""
        await self.settings.update(folder_library_on=True, series_folders=(SERIES_FOLDER,))
        await self.db.library_files.replace_folder(
            SERIES_FOLDER,
            [
                LibraryFile(
                    path=rf"{SERIES_FOLDER}\Show\S{season:02d}E{number:02d}.mkv",
                    size=1,
                    mtime=1.0,
                    kind="episode",
                    tmdb_id=tmdb_id,
                    season=season,
                    episode=number,
                    episode_end=last,
                    recognised=True,
                )
                for season, number, last in episodes
            ],
        )

    def plex_show(self, **ids: Any) -> PlexTitle:
        title = PlexTitle(kind="show", key="777", title="Severance on Plex", **ids)
        self.plex.titles.return_value = [
            PlexTitle(kind="movie", key="1", title="A film", tmdb_id=TMDB_SEVERANCE),
            title,
        ]
        return title

    async def look_for(self, text: str | None, phone: PairedPhone | None = None) -> Reply:
        query = {} if text is None else {"q": text}
        request = Request("GET", "/v1/find", query, {}, phone or self.phone)
        reply: Reply = await self.find.routes()[("GET", "/v1/find")](request)
        json.dumps(reply.body)  # what the server sends
        return reply

    async def found(self, text: str = TYPED) -> list[dict[str, Any]]:
        reply = await self.look_for(text)
        self.assertEqual(reply.status, 200, reply.body)
        results: list[dict[str, Any]] = reply.body["results"]
        return results

    async def episodes(self, id: object, phone: PairedPhone | None = None) -> Reply:
        path = "/v1/find/series/{id}"
        request = Request("GET", f"/v1/find/series/{id}", {"id": str(id)}, {}, phone or self.phone)
        reply: Reply = await self.find.routes()[("GET", path)](request)
        json.dumps(reply.body)
        return reply

    async def follow_it(self, body: dict[str, Any], phone: PairedPhone | None = None) -> Reply:
        request = Request("POST", "/v1/follow", {}, body, phone or self.phone)
        reply: Reply = await self.find.routes()[("POST", "/v1/follow")](request)
        json.dumps(reply.body)
        return reply

    def films_found(self, *films: Any) -> None:
        self.tmdb.search_movies.return_value = list(films)

    def shows_found(self, *shows: DiscoverShow) -> None:
        self.tmdb.search_tv.return_value = list(shows)

    def restart(self) -> None:
        super().restart()
        self.find = self.new_find()
        self.use_sources(self.good(PACK))

    def searched(self) -> list[Any]:
        """Records what the torrent search is asked for from now on."""
        real = self.service._search.search
        requests: list[Any] = []

        def recording(request: Any) -> Any:
            requests.append(request)
            return real(request)

        patcher = mock.patch.object(self.service._search, "search", recording)
        patcher.start()
        self.addCleanup(patcher.stop)
        return requests


class WhatIsFoundTests(FindCase):
    async def test_films_first_then_series_with_what_the_app_reads(self) -> None:
        await self.library.untrack(SEVERANCE, profile_id=self.person)  # TVMaze's, by name
        self.films_found(film(ARRIVAL, "Arrival", overview="A linguist."))
        self.shows_found(tmdb_show(overview="Work."))
        reply = await self.look_for(TYPED)
        self.assertEqual(
            reply.body,
            {
                "results": [
                    {
                        "kind": "movie",
                        "id": ARRIVAL,
                        "title": "Arrival",
                        "year": 2025,
                        "poster_url": f"https://image.tmdb.org/t/p/w342/{ARRIVAL}.jpg",
                        "overview": "A linguist.",
                        "state": "new",
                        "out": True,
                    },
                    {
                        "kind": "series",
                        "id": TMDB_SEVERANCE,
                        "title": "Severance",
                        "year": 2022,
                        "poster_url": f"https://image.tmdb.org/t/p/w342/{TMDB_SEVERANCE}.jpg",
                        "overview": "Work.",
                        "state": "new",
                        "out": True,
                    },
                ]
            },
        )

    async def test_each_kind_in_tmdbs_order_at_most_ten(self) -> None:
        self.assertEqual(FIND_LIMIT, 10)
        self.films_found(*(film(id) for id in range(100, 112)))
        self.shows_found(*(tmdb_show(id, f"Show {id}") for id in range(200, 212)))
        results = await self.found()
        self.assertEqual(
            [(item["kind"], item["id"]) for item in results],
            [("movie", id) for id in range(100, 110)] + [("series", id) for id in range(200, 210)],
        )
        self.tmdb.search_movies.assert_awaited_once_with(TYPED, limit=10)
        self.tmdb.search_tv.assert_awaited_once_with(TYPED, limit=10)

    async def test_year_may_be_null_and_overview_empty(self) -> None:
        self.films_found(film(ARRIVAL, release_date=None))
        self.shows_found(tmdb_show(first_air_date=None))
        for item in await self.found():
            self.assertIsNone(item["year"])
            self.assertEqual(item["overview"], "")

    async def test_the_text_is_stripped_and_two_to_a_hundred_characters(self) -> None:
        self.films_found(film(ARRIVAL))
        self.assertEqual(len(await self.found("  ab \t")), 1)
        self.tmdb.search_movies.assert_awaited_with("ab", limit=10)
        self.assertEqual((await self.look_for("x" * 100)).status, 200)
        for text in (None, "", "a", "  a  ", "x" * 101):
            with self.subTest(text=text):
                reply = await self.look_for(text)
                self.assertEqual(reply.status, 400)
                self.assertEqual(reply.body["error"], "bad_request")

    async def test_nothing_found_says_so(self) -> None:
        reply = await self.look_for(TYPED)
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body, {"results": [], "message": t(NOTHING_FOUND)})

    async def test_without_a_tmdb_key_nothing_is_searched_and_it_says_so(self) -> None:
        self.tmdb.configured = False
        reply = await self.look_for(TYPED)
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body, {"results": [], "message": t(NO_KEY)})
        self.tmdb.search_movies.assert_not_awaited()

    async def test_a_key_that_disappears_meanwhile_says_the_same(self) -> None:
        self.tmdb.search_tv.side_effect = TMDBNotConfigured("TMDB API key is not set")
        reply = await self.look_for(TYPED)
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body, {"results": [], "message": t(NO_KEY)})

    async def test_a_refused_key_says_so(self) -> None:
        self.tmdb.search_movies.side_effect = TMDBAuthError("refused")
        reply = await self.look_for(TYPED)
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.body, {"results": [], "message": t(KEY_REFUSED)})

    async def test_tmdb_ids_never_reach_the_log_when_tmdb_stumbles(self) -> None:
        def answer(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/3/search/movie":
                found = {"id": ARRIVAL, "title": "Arrival", "release_date": "2025-01-01"}
                return httpx.Response(200, json={"results": [found]})
            if request.url.path == "/3/search/tv":
                return httpx.Response(200, json={"results": []})
            return httpx.Response(503)  # its details

        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
            client = TMDBClient(http, api_key=lambda: "0123456789abcdef0123456789abcdef",
                                retry_delay=0)
            self.discovery = DiscoveryService(client, self.tvmaze, self.library, clock=lambda: NOW)
            self.find = self.new_find()
            with self.assertLogs("app", "DEBUG") as logs:
                results = await self.found()
        self.assertEqual([(item["id"], item["out"]) for item in results], [(ARRIVAL, True)])
        text = "\n".join(logs.output)
        self.assertIn("/movie/{id}", text)  # the retries were logged...
        self.assertNotIn(str(ARRIVAL), text)  # ...without the film's id

    async def test_tmdb_out_of_reach_is_a_plain_502(self) -> None:
        self.films_found(film(ARRIVAL))
        self.tmdb.search_tv.side_effect = TMDBError("SECRET")
        reply = await self.look_for(TYPED)
        self.assertEqual(reply.status, 502)
        self.assertEqual(reply.body, {"error": "failed", "message": t(COULDNT_SEARCH)})

    async def test_tmdb_out_of_reach_through_the_real_client(self) -> None:
        def answer(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
            client = TMDBClient(http, api_key=lambda: "0123456789abcdef0123456789abcdef",
                                retry_delay=0)
            self.discovery = DiscoveryService(client, self.tvmaze, self.library, clock=lambda: NOW)
            self.find = self.new_find()
            with self.assertLogs("app", "DEBUG") as logs:
                reply = await self.look_for(TYPED)
        self.assertEqual(reply.status, 502)
        self.assertEqual(reply.body["message"], t(COULDNT_SEARCH))
        self.assertNotIn(TYPED, "\n".join(logs.output))

    async def test_no_links_or_keys_in_the_answer(self) -> None:
        secret = "/x.jpg?api_key=SECRET-KEY"
        self.films_found(Movie(id=ARRIVAL, title="Arrival", poster_path=secret))
        self.shows_found(DiscoverShow(id=3, name="Three", poster_path=secret))
        results = await self.found()
        self.assertEqual([item["poster_url"] for item in results], [None, None])
        self.assertNotIn("SECRET-KEY", json.dumps(results))

    async def test_a_deleted_person_gets_a_plain_answer_everywhere(self) -> None:
        gone = self.phone_of(12345)
        for reply in (
            await self.look_for(TYPED, gone),
            await self.episodes(TMDB_SEVERANCE, gone),
            await self.follow_it({"id": TMDB_SEVERANCE}, gone),
        ):
            self.assertEqual(reply.status, 401)
            self.assertEqual(reply.body["error"], "not_paired")
        self.tmdb.search_movies.assert_not_awaited()

    async def test_the_log_never_holds_the_text_a_title_or_a_tmdb_id(self) -> None:
        self.films_found(film(ARRIVAL, "Arrival"))
        self.shows_found(tmdb_show())
        with self.assertLogs("app", "DEBUG") as logs:
            await self.found()
            self.assertEqual((await self.episodes(TMDB_SEVERANCE)).status, 200)
            self.assertEqual((await self.follow_it({"id": TMDB_SEVERANCE})).status, 200)
            await self.found("nothing like it")
        text = "\n".join(logs.output)
        for secret in (TYPED, "nothing like it", "Arrival", "Severance", "Half Loop",
                       str(ARRIVAL), str(TMDB_SEVERANCE)):
            self.assertNotIn(secret, text)


class FilmStateTests(FindCase):
    async def test_in_library_beats_downloading_beats_on_the_list_beats_new(self) -> None:
        for id in (1, 2, 3):
            await self.want(film(id), digital=OUT_LAST_YEAR)
        self.grabs.has_grabbed.side_effect = lambda target: target.media_id in {1, 2}
        await self.plex_has(films=frozenset({(1, None)}))
        self.films_found(film(1), film(2), film(3), film(4))
        self.assertEqual(
            [item["state"] for item in await self.found()],
            ["in_library", "downloading", "on_list", "new"],
        )
        self.assertEqual(
            self.grabs.has_grabbed.await_args_list[0].args[0],
            GrabTarget(media_type=MediaType.MOVIE, media_id=2),
        )

    async def test_had_by_its_imdb_id(self) -> None:
        self.tmdb.movie_details.side_effect = lambda id: MovieDetails(
            id=id, digital_release=OUT_LAST_YEAR, imdb_id="tt0000004" if id == 4 else None
        )
        await self.plex_has(films=frozenset({(None, "tt0000004")}))
        self.films_found(film(4), film(5))
        self.assertEqual([item["state"] for item in await self.found()], ["in_library", "new"])

    async def test_on_another_persons_list_is_new(self) -> None:
        other = await self.profiles.add("Sam", self.profiles.current.colour)
        await self.want(film(1), digital=OUT_LAST_YEAR, person=other.id)
        self.films_found(film(1))
        self.assertEqual((await self.found())[0]["state"], "new")

    async def test_out_goes_by_tmdbs_release_dates(self) -> None:
        self.tmdb.movie_details.side_effect = lambda id: MovieDetails(
            id=id, digital_release=TODAY if id == 1 else TODAY + timedelta(days=1)
        )
        self.films_found(film(1), film(2))
        self.assertEqual([item["out"] for item in await self.found()], [True, False])

    async def test_a_film_tmdb_cant_detail_is_out_and_not_an_error(self) -> None:
        self.tmdb.movie_details.side_effect = TMDBError("no details")
        self.films_found(film(1, release_date=TODAY + timedelta(days=30)))
        results = await self.found()
        self.assertEqual([(item["out"], item["state"]) for item in results], [(True, "new")])


class SeriesStateTests(FindCase):
    async def test_in_library_beats_following_beats_new(self) -> None:
        # The phone's person follows TVMaze's Severance (DownloadsCase): known by its name here.
        await self.cached_show(501, tvdb_id=9001)
        await self.plex_has(episodes=frozenset({(501, 1, 1)}))
        details = {1: TVDetails(id=1, tvdb_id=9001), 4: TVDetails(id=4, tvdb_id=9001)}
        self.tmdb.tv_details.side_effect = lambda id: details.get(id, TVDetails(id=id))
        self.shows_found(
            tmdb_show(1, "One"),
            tmdb_show(TMDB_SEVERANCE, "Severance"),
            tmdb_show(3, "Three"),
            tmdb_show(4, "Severance"),  # followed, and in Plex
        )
        self.assertEqual(
            [item["state"] for item in await self.found()],
            ["in_library", "following", "new", "in_library"],
        )

    async def test_a_show_the_cache_doesnt_know_is_had_from_plex_or_the_folders(self) -> None:
        await self.plex_has()  # Plex connected, nothing it had matched to a cached show
        self.plex_show(tvdb_id=9001)
        await self.in_the_folders(3, (1, 1, None))
        details = {1: TVDetails(id=1, tvdb_id=9001), 2: TVDetails(id=2, tvdb_id=9002)}
        self.tmdb.tv_details.side_effect = lambda id: details.get(id, TVDetails(id=id))
        self.plex.titles.return_value.append(PlexTitle(kind="show", key="8", title="x", tmdb_id=4))
        self.shows_found(
            tmdb_show(1, "By TheTVDB"),
            tmdb_show(2, "Elsewhere"),
            tmdb_show(3, "In the folders"),
            tmdb_show(4, "By TMDB"),
        )
        self.assertEqual(
            [item["state"] for item in await self.found()],
            ["in_library", "new", "in_library", "in_library"],
        )

    async def test_without_the_folders_on_their_files_dont_count(self) -> None:
        await self.in_the_folders(3, (1, 1, None))
        await self.settings.update(folder_library_on=False)
        self.shows_found(tmdb_show(3, "In the folders"))
        self.assertEqual((await self.found())[0]["state"], "new")

    async def test_someone_elses_follow_is_new(self) -> None:
        other = await self.profiles.add("Sam", self.profiles.current.colour)
        await self.library.untrack(SEVERANCE, profile_id=self.person)
        await self.follow(person=other.id)
        self.shows_found(tmdb_show(TMDB_SEVERANCE, "Severance"))
        self.assertEqual((await self.found())[0]["state"], "new")

    async def test_a_series_whose_details_fail_is_not_had(self) -> None:
        await self.cached_show(501, tvdb_id=9001)
        await self.plex_has(episodes=frozenset({(501, 1, 1)}))
        self.tmdb.tv_details.side_effect = TMDBError("no details")
        self.shows_found(tmdb_show(1, "One"))
        self.assertEqual((await self.found())[0]["state"], "new")

    async def test_out_from_its_first_air_date(self) -> None:
        self.shows_found(
            tmdb_show(1, "Today", first_air_date=TODAY),
            tmdb_show(2, "Tomorrow", first_air_date=TODAY + timedelta(days=1)),
            tmdb_show(3, "Unknown", first_air_date=None),
        )
        self.assertEqual([item["out"] for item in await self.found()], [True, False, True])


class EpisodeListTests(FindCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.shows_found(tmdb_show())
        await self.found()

    async def test_seasons_and_their_numbered_episodes(self) -> None:
        reply = await self.episodes(TMDB_SEVERANCE)
        self.assertEqual(reply.status, 200, reply.body)

        def new(number: int, name: str, out: bool) -> dict[str, Any]:
            return {"number": number, "name": name, "out": out, "state": "new"}

        self.assertEqual(
            reply.body,
            {
                "following": False,
                "seasons": [
                    {
                        "number": 1,
                        "out": True,
                        "episodes": [
                            new(1, "Good News About Hell", True),
                            new(2, "Half Loop", True),
                        ],
                    },
                    {
                        "number": 2,
                        "out": True,
                        "episodes": [
                            new(1, "Hello, Ms. Cobel", True),
                            new(2, "Goodbye, Mrs. Selvig", False),
                        ],
                    },
                    {"number": 3, "out": False, "episodes": [new(1, "Unwritten", False)]},
                ],
            },
        )

    async def test_in_library_beats_downloading_beats_new_for_a_show_nobody_follows(self) -> None:
        await self.db.library.save_show(tvmaze_show(), EPISODES)  # known here, followed by none
        await self.plex_has(episodes=frozenset({(TVMAZE_SEVERANCE, 1, 1)}))
        for season, number in ((1, 1), (1, 2), (2, None)):  # S02 as a pack
            await self.db.history.add(
                DownloadRecord(
                    media_type=MediaType.TV,
                    media_id=TVMAZE_SEVERANCE,
                    season=season,
                    episode=number,
                    release_title="x",
                    downloaded_at=NOW,
                )
            )
        reply = await self.episodes(TMDB_SEVERANCE)
        states = [
            (season["number"], one["number"], one["state"])
            for season in reply.body["seasons"]
            for one in season["episodes"]
        ]
        self.assertEqual(
            states,
            [
                (1, 1, "in_library"),
                (1, 2, "downloading"),
                (2, 1, "downloading"),
                (2, 2, "new"),  # aired after the pack was grabbed
                (3, 1, "new"),
            ],
        )

    async def test_a_show_nobody_follows_and_the_cache_doesnt_know_is_had_from_plex(self) -> None:
        await self.plex_has()  # Plex connected; its check matched nothing to this show
        title = self.plex_show(imdb_id="tt11280740")  # TVMaze's IMDb id for it
        self.plex.episodes.return_value = [
            PlexEpisode(key="1", season=1, episode=1),
            PlexEpisode(key="2", season=1, episode=None),
            PlexEpisode(key="3", season=None, episode=2),
        ]
        await self.in_the_folders(TMDB_SEVERANCE, (2, 1, 2))  # "S02E01-E02"
        await self.db.history.add(
            DownloadRecord(
                media_type=MediaType.TV,
                media_id=TVMAZE_SEVERANCE,
                season=1,
                episode=2,
                release_title="x",
                downloaded_at=NOW,
            )
        )
        reply = await self.episodes(TMDB_SEVERANCE)
        self.assertEqual(reply.status, 200, reply.body)
        states = [
            (season["number"], one["number"], one["state"])
            for season in reply.body["seasons"]
            for one in season["episodes"]
        ]
        self.assertEqual(
            states,
            [
                (1, 1, "in_library"),
                (1, 2, "downloading"),
                (2, 1, "in_library"),
                (2, 2, "in_library"),
                (3, 1, "new"),
            ],
        )
        self.plex.episodes.assert_awaited_once_with(title)

    async def test_plex_not_answering_counts_nothing_and_is_no_error(self) -> None:
        await self.plex_has()
        self.plex_show(tmdb_id=TMDB_SEVERANCE)
        self.plex.episodes.side_effect = PlexUnreachable("away")
        reply = await self.episodes(TMDB_SEVERANCE)
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(
            {one["state"] for season in reply.body["seasons"] for one in season["episodes"]},
            {"new"},
        )

    async def test_without_plex_connected_plex_isnt_asked(self) -> None:
        self.plex_show(tmdb_id=TMDB_SEVERANCE)
        self.assertEqual((await self.episodes(TMDB_SEVERANCE)).status, 200)
        self.plex.titles.assert_not_awaited()
        self.plex.episodes.assert_not_awaited()

    async def test_following_says_whether_the_person_follows_it(self) -> None:
        await self.db.library.save_show(tvmaze_show(), EPISODES)
        await self.db.library.track(TVMAZE_SEVERANCE, profile_id=self.person)
        reply = await self.episodes(TMDB_SEVERANCE)
        self.assertTrue(reply.body["following"])
        other = await self.profiles.add("Sam", self.profiles.current.colour)
        self.assertFalse((await self.episodes(TMDB_SEVERANCE, self.phone_of(other.id))).body[
            "following"
        ])

    async def test_a_suggested_series_has_its_episodes_too(self) -> None:
        self.restart()
        self.tmdb.trending_tv.return_value = [tmdb_show()]
        self.assertEqual((await self.episodes(TMDB_SEVERANCE)).status, 200)

    async def test_a_series_never_found_is_no_longer_there(self) -> None:
        self.tvmaze.lookup_show.reset_mock()
        for id in (777, SEVERANCE):  # SEVERANCE: the followed show's TVMaze id, not a TMDB one
            with self.subTest(id):
                reply = await self.episodes(id)
                self.assertEqual(reply.status, 410)
                self.assertEqual(reply.body, {"error": "no_longer_there",
                                              "message": t(SEARCH_AGAIN)})
        self.tvmaze.lookup_show.assert_not_awaited()

    async def test_after_a_restart_it_says_search_again(self) -> None:
        self.restart()
        self.assertEqual((await self.episodes(TMDB_SEVERANCE)).status, 410)

    async def test_a_series_tvmaze_doesnt_know_gets_a_plain_answer(self) -> None:
        self.tvmaze.lookup_show.return_value = None
        reply = await self.episodes(TMDB_SEVERANCE)
        self.assertEqual(reply.status, 502)
        self.assertEqual(reply.body, {"error": "failed",
                                      "message": t(NO_MATCH, title="Severance")})

    async def test_tvmaze_being_down_gets_a_plain_answer(self) -> None:
        self.tvmaze.get_show_with_episodes.side_effect = TVMazeError("SECRET")
        reply = await self.episodes(TMDB_SEVERANCE)
        self.assertEqual(reply.status, 502)
        self.assertEqual(reply.body["message"], t(NO_LOOKUP, title="Severance"))

    async def test_an_id_that_isnt_a_number_is_a_bad_request(self) -> None:
        for id in ("abc", "-1", "1.5", "", "1" * 11, "9" * 5000):
            with self.subTest(id):
                self.assertEqual((await self.episodes(id)).status, 400)


class FollowTests(FindCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.shows_found(tmdb_show())
        await self.found()

    async def test_following_a_found_series(self) -> None:
        reply = await self.follow_it({"id": TMDB_SEVERANCE})
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(
            reply.body, {"outcome": "following", "message": t(FOLLOWING, title="Severance")}
        )
        followed = await self.library.tracked_ids(profile_id=self.person)
        self.assertIn(TVMAZE_SEVERANCE, followed)
        self.assertEqual((await self.found())[0]["state"], "following")

    async def test_a_follow_looks_its_show_up_once(self) -> None:
        self.tvmaze.lookup_show.reset_mock()
        await self.follow_it({"id": TMDB_SEVERANCE})
        self.tvmaze.lookup_show.assert_awaited_once()

    async def test_following_again_says_already(self) -> None:
        await self.follow_it({"id": TMDB_SEVERANCE})
        reply = await self.follow_it({"id": TMDB_SEVERANCE})
        self.assertEqual(
            reply.body, {"outcome": "already", "message": t(ALREADY, title="Severance")}
        )

    async def test_a_phone_follows_for_its_own_person(self) -> None:
        other = await self.profiles.add("Sam", self.profiles.current.colour)
        viewer = self.phone_of(other.id).model_copy(update={"manager": False})
        self.assertEqual((await self.follow_it({"id": TMDB_SEVERANCE}, viewer)).status, 200)
        self.assertIn(TVMAZE_SEVERANCE, await self.library.tracked_ids(profile_id=other.id))
        self.assertNotIn(TVMAZE_SEVERANCE, await self.library.tracked_ids(profile_id=self.person))

    async def test_a_suggested_series_may_be_followed(self) -> None:
        self.restart()
        self.tmdb.trending_tv.return_value = [tmdb_show()]
        self.assertEqual((await self.follow_it({"id": TMDB_SEVERANCE})).body["outcome"],
                         "following")

    async def test_a_series_never_found_is_no_longer_there(self) -> None:
        reply = await self.follow_it({"id": 777})
        self.assertEqual(reply.status, 410)
        self.assertEqual(reply.body["error"], "no_longer_there")
        self.assertNotIn(TVMAZE_SEVERANCE, await self.library.tracked_ids(profile_id=self.person))

    async def test_a_series_tvmaze_doesnt_know_gets_a_plain_answer(self) -> None:
        self.tvmaze.lookup_show.return_value = None
        reply = await self.follow_it({"id": TMDB_SEVERANCE})
        self.assertEqual(reply.status, 502)
        self.assertEqual(reply.body, {"error": "failed",
                                      "message": t(CANT_FOLLOW, title="Severance")})

    async def test_tvmaze_being_down_gets_a_plain_answer(self) -> None:
        self.tvmaze.get_show_with_episodes.side_effect = TVMazeError("SECRET")
        reply = await self.follow_it({"id": TMDB_SEVERANCE})
        self.assertEqual(reply.status, 502)
        self.assertEqual(reply.body["message"], t(NO_LOOKUP, title="Severance"))

    async def test_what_cant_be_read_is_a_bad_request(self) -> None:
        for body in ({}, {"id": "1"}, {"id": True}, {"id": 1.5}):
            with self.subTest(body):
                self.assertEqual((await self.follow_it(body)).status, 400)


class FoundFilmTests(FindCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.use_sources(self.good(ARRIVAL_RELEASE))
        self.films_found(film(ARRIVAL, "Arrival"))
        await self.found()
        self.body = {"kind": "movie", "id": ARRIVAL, "found": True}

    async def listed(self) -> list[int]:
        return [entry.movie_id for entry in await self.watchlist.by_release_of(self.person)]

    async def test_a_tap_downloads_it_and_puts_it_on_the_list(self) -> None:
        await self.settings.update(auto_download_new_films=True)
        reply = await self.post("/v1/download", self.body)
        self.assertEqual(reply.body["outcome"], "grabbed", reply.body)
        self.assertEqual(
            self.grabs.grab.await_args.args[1],
            GrabTarget(media_type=MediaType.MOVIE, media_id=ARRIVAL),
        )
        self.assertEqual(await self.listed(), [ARRIVAL])
        entry = (await self.watchlist.by_release_of(self.person))[0]
        self.assertTrue(entry.auto_download)  # Settings' switch, as every add
        self.assertEqual(entry.digital_release, OUT_LAST_YEAR)  # its dates read on adding

    async def test_it_goes_on_the_list_whatever_the_tap_finds(self) -> None:
        self.use_sources()
        reply = await self.post("/v1/download", self.body)
        self.assertEqual(reply.body["outcome"], "nothing_found")
        self.assertEqual(await self.listed(), [ARRIVAL])
        self.tmdb.movie_details.side_effect = lambda id: MovieDetails(
            id=id, digital_release=TODAY + timedelta(days=5)
        )
        self.discovery.clear_cache()
        await self.watchlist.remove(ARRIVAL, profile_id=self.person)
        reply = await self.post("/v1/download", self.body)
        self.assertEqual(reply.body["message"], t(NOT_OUT, label="Arrival (2025)"))
        self.assertEqual(await self.listed(), [ARRIVAL])

    async def test_on_the_phones_persons_list_only(self) -> None:
        other = await self.profiles.add("Sam", self.profiles.current.colour)
        await self.post("/v1/download", self.body, self.phone_of(other.id))
        self.assertEqual(await self.listed(), [])
        self.assertEqual(
            [entry.movie_id for entry in await self.watchlist.by_release_of(other.id)], [ARRIVAL]
        )

    async def test_a_film_already_on_the_list_is_left_as_it_is(self) -> None:
        await self.want(film(ARRIVAL, "Arrival"), digital=OUT_LAST_YEAR)  # its switch on
        await self.post("/v1/download", self.body)
        entry = (await self.watchlist.by_release_of(self.person))[0]
        self.assertTrue(entry.auto_download)
        self.assertEqual(entry.added_at, NOW)

    async def test_looking_at_the_downloads_doesnt_put_it_on_the_list(self) -> None:
        reply = await self.post("/v1/releases", self.body)
        self.assertEqual([one["title"] for one in reply.body["releases"]], [ARRIVAL_RELEASE])
        self.assertEqual(await self.listed(), [])

    async def test_a_chosen_download_puts_it_on_the_list_once_the_grab_worked(self) -> None:
        ref = (await self.post("/v1/releases", self.body)).body["releases"][0]["ref"]
        self.grabs.grab.side_effect = GrabError("The folder is full.")
        self.assertEqual((await self.post("/v1/grab", {"ref": ref})).status, 502)
        self.assertEqual(await self.listed(), [])
        self.grabs.grab.side_effect = None
        reply = await self.post("/v1/grab", {"ref": ref})
        self.assertEqual(reply.body["outcome"], "grabbed", reply.body)
        self.assertEqual(await self.listed(), [ARRIVAL])

    async def test_a_film_tmdb_cant_detail_is_searched_for_anyway(self) -> None:
        self.discovery.clear_cache()
        self.tmdb.movie_details.side_effect = TMDBError("down")
        reply = await self.post("/v1/download", self.body)
        self.assertEqual(reply.body["outcome"], "grabbed", reply.body)

    async def test_after_a_restart_it_is_no_longer_there(self) -> None:
        self.restart()
        reply = await self.post("/v1/download", self.body)
        self.assertEqual(reply.status, 410)
        self.assertEqual(reply.body["message"], t(SEARCH_AGAIN))
        self.assertEqual(await self.listed(), [])

    async def test_found_true_asks_the_found_memory_only(self) -> None:
        await self.want(film(5), digital=OUT_LAST_YEAR)  # on the list, never found
        reply = await self.post("/v1/download", {"kind": "movie", "id": 5, "found": True})
        self.assertEqual(reply.status, 410)
        self.grabs.grab.assert_not_awaited()

    async def test_found_must_be_true_or_false(self) -> None:
        for found in ("yes", 1, None):
            with self.subTest(found):
                reply = await self.post("/v1/download", {**self.body, "found": found})
                self.assertEqual(reply.status, 400)

    async def test_an_older_companions_tap_on_a_found_film_is_as_before(self) -> None:
        # Found by someone's search, but a tap without "found" knows nothing of it: not on the
        # person's list, not grabbed, not suggested.
        old_style = {"kind": "movie", "id": ARRIVAL}
        for path in ("/v1/download", "/v1/releases"):
            with self.subTest(path):
                reply = await self.post(path, old_style)
                self.assertEqual(reply.status, 410, reply.body)
        self.assertEqual(await self.listed(), [])
        self.grabs.grab.assert_not_awaited()

    async def test_an_older_companions_tap_on_a_suggested_found_film_adds_nothing(self) -> None:
        self.tmdb.trending_movies.return_value = [film(ARRIVAL, "Arrival")]
        reply = await self.post("/v1/download", {"kind": "movie", "id": ARRIVAL})
        self.assertEqual(reply.body["outcome"], "grabbed", reply.body)
        self.assertEqual(await self.listed(), [])

    async def test_an_older_companions_again_on_a_grabbed_film_stays_as_before(self) -> None:
        # Grabbed, then taken off the list on purpose; Coming up still has it on the page.
        await self.want(film(ARRIVAL, "Arrival"), digital=OUT_LAST_YEAR)
        self.grabs.has_grabbed.return_value = True
        old_style = {"kind": "movie", "id": ARRIVAL}
        self.assertEqual((await self.post("/v1/download", old_style)).body["outcome"], "already")
        await self.watchlist.remove(ARRIVAL, profile_id=self.person)
        grab = DownloadRecord(media_type=MediaType.MOVIE, media_id=ARRIVAL, release_title="x")
        self.downloads_page((grab, DownloadState.DOWNLOADING), title="Arrival (2025)")
        self.tmdb.movie_details.side_effect = lambda id: MovieDetails(
            id=id, digital_release=TODAY + timedelta(days=5)  # TMDB's dates say not out
        )
        self.discovery.clear_cache()
        reply = await self.post("/v1/download", {**old_style, "again": True})
        self.assertEqual(reply.body["outcome"], "grabbed", reply.body)  # searched, as before
        self.assertEqual(await self.listed(), [])  # and not put back on the list

    async def test_a_list_that_cant_be_written_after_a_grab_still_says_grabbed(self) -> None:
        ref = (await self.post("/v1/releases", self.body)).body["releases"][0]["ref"]
        with mock.patch.object(self.watchlist, "add", side_effect=RuntimeError("disk")):
            with self.assertLogs("app.services.phone.downloads", "WARNING"):
                reply = await self.post("/v1/grab", {"ref": ref})
        self.assertEqual(reply.body["outcome"], "grabbed", reply.body)


class FoundSeasonTests(FindCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.shows_found(tmdb_show())
        await self.found()

    async def test_a_season_as_a_pack(self) -> None:
        self.use_sources(self.good("Severance.S02.1080p.WEB.x264-GROUP"))
        requests = self.searched()
        reply = await self.post("/v1/download", {"kind": "season", "id": TMDB_SEVERANCE,
                                                 "season": 2})
        self.assertEqual(reply.body["outcome"], "grabbed", reply.body)
        self.assertEqual(reply.body["message"], "Severance S02 · Sent to qBittorrent")
        self.assertEqual(requests[0].kind, SearchKind.SEASON)
        self.assertEqual((requests[0].season, requests[0].episode_count), (2, 1))  # one aired
        self.assertEqual(self.grabs.grab.await_args.args[1], SEASON_TWO)

    async def test_downloaded_before_says_already(self) -> None:
        self.grabs.has_grabbed.return_value = True
        reply = await self.post("/v1/download", {"kind": "season", "id": TMDB_SEVERANCE,
                                                 "season": 2})
        self.assertEqual(reply.body["outcome"], "already")
        self.grabs.has_grabbed.assert_awaited_with(SEASON_TWO)

    async def test_a_season_not_out_isnt_searched_for(self) -> None:
        reply = await self.post("/v1/download", {"kind": "season", "id": TMDB_SEVERANCE,
                                                 "season": 3})
        self.assertEqual(reply.body["outcome"], "nothing_found")
        self.assertEqual(reply.body["message"], t(NOT_OUT, label="Severance S03"))
        self.grabs.grab.assert_not_awaited()

    async def test_a_season_it_hasnt_is_no_longer_there(self) -> None:
        reply = await self.post("/v1/download", {"kind": "season", "id": TMDB_SEVERANCE,
                                                 "season": 9})
        self.assertEqual(reply.status, 410)

    async def test_choosing_by_hand(self) -> None:
        reply = await self.post("/v1/releases", {"kind": "season", "id": TMDB_SEVERANCE,
                                                 "season": 1})
        self.assertEqual([one["title"] for one in reply.body["releases"]], [PACK])
        chosen = await self.post("/v1/grab", {"ref": reply.body["releases"][0]["ref"]})
        self.assertEqual(chosen.body["outcome"], "grabbed", chosen.body)
        self.assertEqual(
            self.grabs.grab.await_args.args[1],
            GrabTarget(media_type=MediaType.TV, media_id=TVMAZE_SEVERANCE, season=1),
        )
        self.assertEqual(await self.watchlist.by_release_of(self.person), ())

    async def test_a_suggested_series_season_works_too(self) -> None:
        self.restart()
        self.tmdb.trending_tv.return_value = [tmdb_show()]
        reply = await self.post("/v1/download", {"kind": "season", "id": TMDB_SEVERANCE,
                                                 "season": 1})
        self.assertEqual(reply.body["outcome"], "grabbed", reply.body)

    async def test_never_found_is_no_longer_there(self) -> None:
        reply = await self.post("/v1/download", {"kind": "season", "id": 777, "season": 1})
        self.assertEqual(reply.status, 410)
        self.assertEqual(reply.body["message"], t(SEARCH_AGAIN))


class FoundEpisodeTests(FindCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.shows_found(tmdb_show())
        await self.found()

    def body(self, id: int = TMDB_SEVERANCE, season: int = 1, episode: int = 2) -> dict[str, Any]:
        return {"kind": "found_episode", "id": id, "season": season, "episode": episode}

    async def test_a_single_episode(self) -> None:
        self.use_sources(self.good("Severance.S01E02.1080p.WEB.x264-GROUP"))
        requests = self.searched()
        reply = await self.post("/v1/download", self.body())
        self.assertEqual(reply.body["outcome"], "grabbed", reply.body)
        self.assertEqual(reply.body["message"], "Severance S01E02 · Sent to qBittorrent")
        self.assertEqual(requests[0].kind, SearchKind.EPISODE)
        self.assertEqual((requests[0].season, requests[0].episode), (1, 2))
        self.assertEqual(
            self.grabs.grab.await_args.args[1],
            GrabTarget(media_type=MediaType.TV, media_id=TVMAZE_SEVERANCE, season=1, episode=2),
        )

    async def test_its_tmdb_id_never_names_a_followed_show_with_that_tvmaze_id(self) -> None:
        # The phone's person follows TVMaze show SEVERANCE, whose episode S02E01 exists.
        for path in ("/v1/download", "/v1/releases"):
            with self.subTest(path):
                reply = await self.post(path, self.body(SEVERANCE, 2, 1))
                self.assertEqual(reply.status, 410, reply.body)
                self.assertEqual(reply.body["error"], "no_longer_there")
        self.grabs.grab.assert_not_awaited()

    async def test_a_found_show_whose_tmdb_id_is_a_followed_tvmaze_id(self) -> None:
        self.shows_found(tmdb_show(SEVERANCE, "Severance"))  # TMDB's 44933, TVMaze's 5555
        await self.found()
        self.use_sources(self.good("Severance.S01E02.1080p.WEB.x264-GROUP"))
        reply = await self.post("/v1/download", self.body(SEVERANCE, 1, 2))
        self.assertEqual(reply.body["outcome"], "grabbed", reply.body)
        self.assertEqual(self.grabs.grab.await_args.args[1].media_id, TVMAZE_SEVERANCE)

    async def test_an_episode_not_out_isnt_searched_for(self) -> None:
        reply = await self.post("/v1/download", self.body(season=2, episode=2))
        self.assertEqual(reply.body["outcome"], "nothing_found")
        self.assertEqual(reply.body["message"], t(NOT_OUT, label="Severance S02E02"))

    async def test_an_episode_it_hasnt_is_no_longer_there(self) -> None:
        self.assertEqual((await self.post("/v1/download", self.body(episode=9))).status, 410)

    async def test_what_cant_be_read_is_a_bad_request(self) -> None:
        for body in (
            {"kind": "season", "id": TMDB_SEVERANCE},
            {"kind": "season", "id": TMDB_SEVERANCE, "season": "1"},
            {"kind": "season", "id": TMDB_SEVERANCE, "season": True},
            {"kind": "found_episode", "id": TMDB_SEVERANCE, "season": 1},
            {"kind": "found_episode", "id": "1", "season": 1, "episode": 1},
        ):
            with self.subTest(body):
                self.assertEqual((await self.post("/v1/download", body)).status, 400)


class MemoryTests(FindCase):
    async def test_the_newest_500_of_each_kind_are_remembered(self) -> None:
        self.assertEqual(FOUND_KEEP, 500)
        with mock.patch("app.services.phone.find.FOUND_KEEP", 3):
            for id in range(1, 6):
                self.films_found(film(id))
                self.shows_found(tmdb_show(id, f"Show {id}"))
                await self.found()
            self.films_found()
            self.shows_found()
            await self.found()
        found = {"kind": "movie", "found": True}
        self.assertEqual((await self.post("/v1/releases", {**found, "id": 2})).status, 410)
        self.assertEqual((await self.post("/v1/releases", {**found, "id": 3})).status, 200)
        self.assertEqual((await self.episodes(2)).status, 410)
        self.assertEqual((await self.episodes(3)).status, 200)


class ClientTests(FindCase):
    async def test_the_client_searches_tmdbs_series(self) -> None:
        asked: list[httpx.URL] = []

        def answer(request: httpx.Request) -> httpx.Response:
            asked.append(request.url)
            results = [
                {"id": 1, "name": "One", "first_air_date": "2022-02-18", "overview": "x"},
                {"id": "broken"},
                {"id": 2, "name": "Two"},
            ]
            return httpx.Response(200, json={"results": results})

        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
            client = TMDBClient(http, api_key=lambda: "0123456789abcdef0123456789abcdef")
            with self.assertLogs("app.services.tmdb", "WARNING"):  # the broken one, by its place
                shows = await client.search_tv("  one  ", limit=1)
            self.assertEqual(await client.search_tv("   "), [])
        self.assertEqual([(show.id, show.name, show.year) for show in shows], [(1, "One", 2022)])
        self.assertEqual(len(asked), 1)
        self.assertEqual(asked[0].path, "/3/search/tv")
        self.assertEqual(asked[0].params["query"], "one")
        self.assertEqual(asked[0].params["include_adult"], "false")
