"""What the phone may suggest beyond Plex: the person's watchlist, then Discover. The real
DiscoveryService over a fake TMDB and TVMaze; a tap goes through the real PhoneDownloads and search,
with fake sources and a fake DownloadService. Nothing here reaches a network or a torrent program."""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any
from unittest import mock

import httpx

from app.i18n import t
from app.models import (
    DiscoverShow,
    Episode,
    GrabTarget,
    MediaType,
    Movie,
    MovieDetails,
    PairedPhone,
    SearchKind,
    Show,
    TVDetails,
)
from app.models.plex import PlexScan
from app.services.discovery import DiscoveryService
from app.services.library import LibraryService
from app.services.phone.server import Reply, Request
from app.services.phone.shortlist import Shortlist
from app.services.phone.suggestions import MAX_ITEMS, Suggestions
from app.services.tmdb import (
    TMDBAuthError,
    TMDBClient,
    TMDBError,
    TMDBNotConfigured,
    TMDBNotFound,
)
from app.services.tvmaze import TVMazeError, TVMazeNotFound
from tests.plex_link import TOKEN
from tests.test_phone_coming_up import NOW, TODAY
from tests.test_phone_downloads import DownloadsCase

OUT_LAST_YEAR = date(2025, 11, 7)
TMDB_SEVERANCE = 95396
TVMAZE_SEVERANCE = 5555  # not the show the phone follows
PACK = "Severance.S01.1080p.WEB.x264-GROUP"
FILM = "Arrival.2025.1080p.WEB.x264-GROUP"
SERIES_BODY = {"kind": "series", "id": TMDB_SEVERANCE}
FILM_BODY = {"kind": "movie", "id": 2}
SEASON_ONE = GrabTarget(media_type=MediaType.TV, media_id=TVMAZE_SEVERANCE, season=1)
NOT_OUT = "{label} isn't out yet, so try again once it is."
NO_MATCH = "AirGrabber can't find {title} to download, so choose something else."
NO_LOOKUP = "AirGrabber couldn't look up {title} just now, so try again in a moment."


def film(id: int, title: str = "", **fields: Any) -> Movie:
    fields.setdefault("release_date", OUT_LAST_YEAR)
    return Movie(id=id, title=title or f"Film {id}", poster_path=f"/{id}.jpg", **fields)


def tmdb_show(id: int = TMDB_SEVERANCE, name: str = "Severance", **fields: Any) -> DiscoverShow:
    fields.setdefault("first_air_date", date(2022, 2, 18))
    return DiscoverShow(id=id, name=name, poster_path=f"/{id}.jpg", **fields)


def tvmaze_show() -> Show:
    return Show(
        id=TVMAZE_SEVERANCE, name="Severance", premiered=date(2022, 2, 18), imdb_id="tt11280740"
    )


def season_one(id: int, number: int | None, day: date) -> Episode:
    return Episode(id=id, show_id=TVMAZE_SEVERANCE, season=1, number=number, air_date=day)


class SuggestionsCase(DownloadsCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.tmdb = mock.Mock(spec=TMDBClient, configured=True)
        for name in (
            "trending_movies",
            "digital_releases",
            "trending_tv",
            "tv_on_the_air",
            "tv_premieres",
        ):
            setattr(self.tmdb, name, mock.AsyncMock(return_value=[]))
        self.tmdb.movie_details = mock.AsyncMock(
            side_effect=lambda id: MovieDetails(id=id, digital_release=OUT_LAST_YEAR)
        )
        self.tmdb.tv_details = mock.AsyncMock(side_effect=lambda id: TVDetails(id=id, imdb_id="tt1"))
        self.tmdb.movie = mock.AsyncMock(return_value=film(2, "Arrival"))
        self.tmdb.tv = mock.AsyncMock(return_value=tmdb_show())
        self.tmdb.genres = mock.AsyncMock(return_value={18: "Drama", 878: "Science Fiction"})
        self.tvmaze = mock.Mock()
        self.tvmaze.lookup_show = mock.AsyncMock(return_value=tvmaze_show())
        self.tvmaze.search_shows = mock.AsyncMock(return_value=[])
        self.tvmaze.get_show_with_episodes = mock.AsyncMock(
            return_value=(tvmaze_show(), [season_one(1, 1, TODAY - timedelta(days=9))])
        )
        self.library = LibraryService(
            self.db.library, self.db.history, self.tvmaze, settings=self.settings
        )
        self.discovery = DiscoveryService(self.tmdb, self.tvmaze, self.library, clock=lambda: NOW)
        self.suggestions = self.new_suggestions()
        self.use_sources(self.good(PACK))

    def new_suggestions(self) -> Suggestions:
        return Suggestions(
            self.library,
            self.watchlist,
            self.discovery,
            self.profiles,
            self.settings,
            now=lambda: NOW,
        )

    async def ask(self, kind: str | None, phone: PairedPhone | None = None) -> Reply:
        query = {} if kind is None else {"kind": kind}
        request = Request("GET", "/v1/suggestions", query, {}, phone or self.phone)
        route = self.suggestions.routes()[("GET", "/v1/suggestions")]
        reply: Reply = await route(request)
        json.dumps(reply.body)  # what the server sends
        return reply

    async def put_on_shortlist(
        self, kind: str, id: int, title: str = "Typed by a phone", year: int = 1999
    ) -> dict[str, Any]:
        """Through the real route, as a phone would; the row as stored."""
        shortlist = Shortlist(self.settings, self.profiles, self.suggestions, now=lambda: NOW)
        key = f"{kind}-{id}"
        body = {"kind": kind, "title": title, "year": year, "suggestion_id": id}
        request = Request("PUT", "/v1/shortlist", {"key": key}, body, self.phone)
        reply = await shortlist.routes()[("PUT", "/v1/shortlist/{key}")](request)
        self.assertEqual(reply.status, 200, reply.body)
        row: dict[str, Any] = next(item for item in reply.body["items"] if item["key"] == key)
        return row

    def restart(self) -> None:
        """What this run knew is gone; Discover has moved on as well."""
        self.tmdb.trending_movies.return_value = []
        self.tmdb.trending_tv.return_value = []
        self.discovery.clear_cache()
        self.suggestions = self.new_suggestions()

    async def ids(self, kind: str) -> list[int]:
        reply = await self.ask(kind)
        self.assertEqual(reply.status, 200, reply.body)
        return [item["id"] for item in reply.body["items"]]

    async def plex_has(
        self,
        *,
        episodes: frozenset[tuple[int, int, int]] = frozenset(),
        films: frozenset[tuple[int | None, str | None]] = frozenset(),
    ) -> None:
        await self.settings.update(plex_token=TOKEN)
        await self.db.plex.replace(PlexScan(episodes=episodes, films=films), NOW)

    async def cached_show(self, id: int, **fields: Any) -> None:
        await self.db.library.save_show(
            Show(id=id, name=f"Show {id}", **fields),
            [Episode(id=id, show_id=id, season=1, number=1)],
        )


class WhatIsSuggestedTests(SuggestionsCase):
    async def test_watchlist_first_then_discover_without_repeats(self) -> None:
        await self.want(film(1), digital=OUT_LAST_YEAR)

        async def just_dropped(start: date, end: date, *, page: int = 1) -> list[Movie]:
            return [film(2)] if end == TODAY and page == 1 else []

        self.tmdb.digital_releases.side_effect = just_dropped
        self.tmdb.movie_details.side_effect = lambda id: MovieDetails(
            id=id, digital_release=TODAY if id == 2 else OUT_LAST_YEAR
        )
        self.tmdb.trending_movies.return_value = [film(1), film(3), film(2)]
        self.assertEqual(await self.ids("movie"), [1, 2, 3])

    async def test_an_item_has_what_the_app_reads(self) -> None:
        self.tmdb.trending_movies.return_value = [
            film(2, "Arrival", genre_ids=(18, 878, 99999), overview="A linguist.")
        ]
        self.tmdb.trending_tv.return_value = [tmdb_show(genre_ids=(18,), overview="Work.")]
        self.assertEqual(
            (await self.ask("movie")).body,
            {
                "items": [
                    {
                        "kind": "movie",
                        "id": 2,
                        "title": "Arrival",
                        "year": 2025,
                        "poster_url": "https://image.tmdb.org/t/p/w342/2.jpg",
                        "genres": ["Drama", "Science Fiction"],
                        "summary": "A linguist.",
                    }
                ]
            },
        )
        self.assertEqual(
            (await self.ask("series")).body["items"],
            [
                {
                    "kind": "series",
                    "id": TMDB_SEVERANCE,
                    "title": "Severance",
                    "year": 2022,
                    "poster_url": f"https://image.tmdb.org/t/p/w342/{TMDB_SEVERANCE}.jpg",
                    "genres": ["Drama"],
                    "summary": "Work.",
                }
            ],
        )

    async def test_the_lists_are_the_phones_persons_not_the_pcs(self) -> None:
        other = await self.profiles.add("Sam", self.profiles.current.colour)
        await self.want(film(1), digital=OUT_LAST_YEAR, person=other.id)
        await self.want(film(2), digital=OUT_LAST_YEAR)
        self.assertEqual(await self.ids("movie"), [2])
        reply = await self.ask("movie", self.phone_of(other.id))
        self.assertEqual([item["id"] for item in reply.body["items"]], [1])

    async def test_what_cant_be_downloaded_yet_isnt_suggested(self) -> None:
        await self.want(film(1), digital=TODAY + timedelta(days=30))
        self.tmdb.trending_movies.return_value = [film(2), film(3)]
        self.tmdb.movie_details.side_effect = lambda id: MovieDetails(
            id=id, digital_release=TODAY + timedelta(days=1) if id == 3 else TODAY
        )
        self.tmdb.trending_tv.return_value = [
            tmdb_show(7, "Airing"),
            tmdb_show(8, "Next month", first_air_date=TODAY + timedelta(days=30)),
        ]
        self.tmdb.tv_premieres.return_value = [tmdb_show(9, "Premiere")]
        self.assertEqual(await self.ids("movie"), [2])
        self.assertEqual(await self.ids("series"), [7])

    async def test_a_film_whose_details_fail_goes_by_its_list_date(self) -> None:
        self.tmdb.trending_movies.return_value = [film(2), film(3, release_date=TODAY)]
        self.tmdb.movie_details.side_effect = TMDBError("no details")
        self.assertEqual(await self.ids("movie"), [2])

    async def test_a_kind_it_doesnt_know_is_a_bad_request(self) -> None:
        for kind in (None, "song", ""):
            with self.subTest(kind):
                reply = await self.ask(kind)
                self.assertEqual(reply.status, 400)
                self.assertEqual(reply.body["error"], "bad_request")

    async def test_a_deleted_person_gets_a_plain_answer(self) -> None:
        reply = await self.ask("movie", self.phone_of(12345))
        self.assertEqual(reply.status, 401)
        self.assertEqual(reply.body["error"], "not_paired")

    async def test_a_viewers_phone_may_ask_and_download(self) -> None:
        viewer = self.phone.model_copy(update={"manager": False})
        self.tmdb.trending_tv.return_value = [tmdb_show()]
        self.assertEqual((await self.ask("series", viewer)).status, 200)
        reply = await self.post("/v1/download", SERIES_BODY, viewer)
        self.assertEqual(reply.body["outcome"], "grabbed", reply.body)

    async def test_at_most_60(self) -> None:
        self.assertEqual(MAX_ITEMS, 60)
        for id in range(1, 71):
            await self.want(film(id), digital=OUT_LAST_YEAR)
        self.assertEqual(len(await self.ids("movie")), 60)

    async def test_no_links_or_keys_in_the_answer(self) -> None:
        secret = "/x.jpg?api_key=SECRET-KEY"
        self.tmdb.trending_movies.return_value = [
            Movie(id=2, title="Two", poster_path=secret, release_date=OUT_LAST_YEAR)
        ]
        self.tmdb.trending_tv.return_value = [DiscoverShow(id=3, name="Three", poster_path=secret)]
        for kind, id in (("movie", 2), ("series", 3)):
            items = (await self.ask(kind)).body["items"]
            self.assertEqual([item["id"] for item in items], [id])
            self.assertIsNone(items[0]["poster_url"])
            self.assertEqual(
                set(items[0]), {"kind", "id", "title", "year", "poster_url", "genres", "summary"}
            )
            self.assertNotIn("SECRET-KEY", json.dumps(items))


class WhatPlexHasTests(SuggestionsCase):
    async def test_suggestions_leave_out_what_plex_has(self) -> None:
        await self.want(film(1), digital=OUT_LAST_YEAR)
        await self.want(film(2), digital=OUT_LAST_YEAR)
        self.tmdb.trending_movies.return_value = [film(3), film(4), film(5)]
        self.tmdb.movie_details.side_effect = lambda id: MovieDetails(
            id=id, digital_release=OUT_LAST_YEAR, imdb_id="tt0000004" if id == 4 else None
        )
        await self.plex_has(films=frozenset({(1, None), (3, None), (None, "tt0000004")}))
        self.assertEqual(await self.ids("movie"), [2, 5])

    async def test_a_series_in_plex_is_found_by_either_id(self) -> None:
        await self.cached_show(501, tvdb_id=9001)
        await self.cached_show(502, imdb_id="tt0000502")
        await self.cached_show(503, tvdb_id=9003)  # known here, but not in Plex
        self.tmdb.trending_tv.return_value = [tmdb_show(1, "One"), tmdb_show(2, "Two")]
        self.tmdb.tv_on_the_air.return_value = [tmdb_show(3, "Three"), tmdb_show(4, "Four")]
        details = {
            1: TVDetails(id=1, tvdb_id=9001),
            2: TVDetails(id=2, imdb_id="tt0000502"),
            3: TVDetails(id=3, tvdb_id=9003),
        }
        self.tmdb.tv_details.side_effect = lambda id: details.get(id, TVDetails(id=id))
        await self.plex_has(episodes=frozenset({(501, 1, 1), (502, 3, 7)}))
        self.assertEqual(await self.ids("series"), [3, 4])

    async def test_without_plex_connected_nothing_counts_as_had(self) -> None:
        await self.cached_show(501, tvdb_id=9001)
        await self.db.plex.replace(
            PlexScan(films=frozenset({(2, None)}), episodes=frozenset({(501, 1, 1)})), NOW
        )
        self.tmdb.trending_movies.return_value = [film(2)]
        self.tmdb.trending_tv.return_value = [tmdb_show(1, "One")]
        self.tmdb.tv_details.side_effect = lambda id: TVDetails(id=id, tvdb_id=9001)
        self.assertEqual(await self.ids("movie"), [2])
        self.assertEqual(await self.ids("series"), [1])
        self.tmdb.tv_details.assert_not_awaited()  # nothing to compare with: no calls for it

    async def test_a_series_whose_details_fail_stays(self) -> None:
        await self.cached_show(501, tvdb_id=9001)
        await self.plex_has(episodes=frozenset({(501, 1, 1)}))
        self.tmdb.trending_tv.return_value = [tmdb_show(1, "One")]
        self.tmdb.tv_details.side_effect = TMDBError("no details")
        self.assertEqual(await self.ids("series"), [1])


class WithoutTMDBTests(SuggestionsCase):
    async def test_no_tmdb_key_gives_watchlist_only_not_an_error(self) -> None:
        await self.want(film(1, genre_ids=(18,)), digital=OUT_LAST_YEAR)
        for name in (
            "trending_movies",
            "digital_releases",
            "trending_tv",
            "tv_on_the_air",
            "tv_premieres",
            "genres",
            "movie_details",
            "tv_details",
        ):
            getattr(self.tmdb, name).side_effect = TMDBNotConfigured("TMDB API key is not set")
        reply = await self.ask("movie")
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual([item["id"] for item in reply.body["items"]], [1])
        self.assertEqual(reply.body["items"][0]["genres"], [])
        reply = await self.ask("series")
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(reply.body["items"], [])

    async def test_a_refused_key_or_tmdb_being_down_is_no_error_either(self) -> None:
        await self.want(film(1), digital=OUT_LAST_YEAR)
        for problem in (TMDBAuthError("refused"), TMDBError("down")):
            with self.subTest(problem=type(problem).__name__):
                self.discovery.clear_cache()
                for name in ("trending_movies", "digital_releases", "genres"):
                    getattr(self.tmdb, name).side_effect = problem
                with mock.patch("app.services.discovery.log"):  # it says so itself
                    self.assertEqual(await self.ids("movie"), [1])


class FilmTapTests(SuggestionsCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.use_sources(self.good(FILM))
        self.tmdb.trending_movies.return_value = [film(2, "Arrival")]

    async def test_a_tap_downloads_a_discover_film_nobody_has_listed(self) -> None:
        await self.ask("movie")
        reply = await self.post("/v1/download", FILM_BODY)
        self.assertEqual(reply.body["outcome"], "grabbed", reply.body)
        self.assertEqual(
            self.grabs.grab.await_args.args[1], GrabTarget(media_type=MediaType.MOVIE, media_id=2)
        )

    async def test_after_a_restart_it_looks_at_discover_again(self) -> None:
        reply = await self.post("/v1/download", FILM_BODY)
        self.assertEqual(reply.body["outcome"], "grabbed", reply.body)

    async def test_a_film_offered_earlier_stays_downloadable_after_leaving_discover(self) -> None:
        await self.ask("movie")
        self.tmdb.trending_movies.return_value = []
        self.discovery.clear_cache()
        reply = await self.post("/v1/download", FILM_BODY)
        self.assertEqual(reply.body["outcome"], "grabbed", reply.body)

    async def test_it_searches_with_the_films_imdb_id(self) -> None:
        self.tmdb.movie_details.side_effect = lambda id: MovieDetails(
            id=id, digital_release=OUT_LAST_YEAR, imdb_id="tt2543164"
        )
        real = self.service._search.search
        requests: list[Any] = []

        def recording(request: Any) -> Any:
            requests.append(request)
            return real(request)

        with mock.patch.object(self.service._search, "search", recording):
            await self.post("/v1/download", FILM_BODY)
        self.assertEqual(requests[0].imdb_id, "tt2543164")

    async def test_it_says_when_it_isnt_out(self) -> None:
        await self.ask("movie")
        self.discovery.clear_cache()
        self.tmdb.movie_details.side_effect = lambda id: MovieDetails(
            id=id, digital_release=TODAY + timedelta(days=5)
        )
        reply = await self.post("/v1/download", FILM_BODY)
        self.assertEqual(reply.body["outcome"], "nothing_found")
        self.assertEqual(reply.body["message"], t(NOT_OUT, label="Arrival (2025)"))
        self.grabs.grab.assert_not_awaited()

    async def test_a_film_that_was_never_offered_is_no_longer_there(self) -> None:
        reply = await self.post("/v1/download", {"kind": "movie", "id": 777})
        self.assertEqual(reply.status, 410)
        self.assertEqual(reply.body["error"], "no_longer_there")

    async def test_a_film_on_the_shortlist_still_downloads_after_a_restart(self) -> None:
        await self.ask("movie")
        row = await self.put_on_shortlist("movie", 2)
        self.assertEqual(row["suggestion_id"], 2)
        self.restart()
        self.use_sources(self.good(FILM))
        reply = await self.post("/v1/download", FILM_BODY)
        self.assertEqual(reply.body["outcome"], "grabbed", reply.body)
        # TMDB's name, not the one the phone sent.
        self.assertEqual(reply.body["message"], "Arrival (2025) · Sent to qBittorrent")

    async def test_a_film_on_the_persons_list_may_go_on_the_shortlist(self) -> None:
        await self.want(film(5), digital=OUT_LAST_YEAR)
        self.assertEqual((await self.put_on_shortlist("movie", 5))["suggestion_id"], 5)

    async def test_a_forged_row_downloads_nothing(self) -> None:
        self.use_sources(self.good("Typed.By.A.Phone.1999.1080p.WEB.x264-GROUP"))
        row = await self.put_on_shortlist("movie", 777)
        self.assertIsNone(row["suggestion_id"])
        for path in ("/v1/download", "/v1/releases"):
            with self.subTest(path):
                reply = await self.post(path, {"kind": "movie", "id": 777})
                self.assertEqual(reply.status, 410, reply.body)
                self.assertEqual(reply.body["error"], "no_longer_there")
        self.grabs.grab.assert_not_awaited()
        self.tmdb.movie.assert_not_awaited()

    async def test_a_row_tmdb_cant_be_asked_about_gets_a_plain_answer_and_no_search(self) -> None:
        await self.ask("movie")
        await self.put_on_shortlist("movie", 2, "Arrival", 2025)
        self.restart()
        self.tmdb.movie.side_effect = TMDBError("SECRET")
        self.use_sources(self.good(FILM))
        with mock.patch.object(self.service._search, "search") as search:
            reply = await self.post("/v1/download", FILM_BODY)
        self.assertEqual(reply.status, 502)
        self.assertEqual(reply.body["message"], t(NO_LOOKUP, title="Arrival"))
        search.assert_not_called()
        self.grabs.grab.assert_not_awaited()

    async def test_a_row_tmdb_no_longer_knows_is_no_longer_there(self) -> None:
        await self.ask("movie")
        await self.put_on_shortlist("movie", 2)
        self.restart()
        self.tmdb.movie.side_effect = TMDBNotFound("gone")
        self.use_sources(self.good(FILM))
        self.assertEqual((await self.post("/v1/download", FILM_BODY)).status, 410)

    async def test_the_second_tap_says_already(self) -> None:
        await self.ask("movie")
        self.grabs.has_grabbed.return_value = True
        reply = await self.post("/v1/download", FILM_BODY)
        self.assertEqual(reply.body["outcome"], "already")
        self.grabs.grab.assert_not_awaited()


class SeriesTapTests(SuggestionsCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.tmdb.trending_tv.return_value = [tmdb_show()]
        await self.ask("series")

    async def tap_and_see_the_search(self) -> tuple[Reply, Any]:
        real = self.service._search.search
        requests: list[Any] = []

        def recording(request: Any) -> Any:
            requests.append(request)
            return real(request)

        with mock.patch.object(self.service._search, "search", recording):
            reply = await self.post("/v1/download", SERIES_BODY)
        self.assertEqual(reply.status, 200, reply.body)
        return reply, requests[0]

    async def test_series_download_asks_for_season_1(self) -> None:
        reply, request = await self.tap_and_see_the_search()
        self.assertEqual(reply.body["outcome"], "grabbed", reply.body)
        self.assertEqual(reply.body["message"], "Severance S01 · Sent to qBittorrent")
        self.assertEqual(request.kind, SearchKind.SEASON)
        self.assertEqual((request.title, request.season, request.episode), ("Severance", 1, None))
        self.assertEqual(request.episode_count, 1)
        self.assertEqual(request.imdb_id, "tt11280740")
        self.assertEqual(self.grabs.grab.await_args.args[1], SEASON_ONE)

    async def test_the_answers_id_is_tmdbs_the_grabs_is_tvmazes(self) -> None:
        self.assertEqual(await self.ids("series"), [TMDB_SEVERANCE])
        await self.post("/v1/download", SERIES_BODY)
        self.assertEqual(self.grabs.grab.await_args.args[1].media_id, TVMAZE_SEVERANCE)

    async def test_only_episodes_of_season_1_that_aired_count(self) -> None:
        self.tvmaze.get_show_with_episodes.return_value = (
            tvmaze_show(),
            [
                season_one(1, 1, TODAY - timedelta(days=9)),
                season_one(2, 2, TODAY - timedelta(days=2)),
                season_one(3, 3, TODAY + timedelta(days=5)),
                season_one(5, None, TODAY - timedelta(days=2)),  # a special
                Episode(id=4, show_id=TVMAZE_SEVERANCE, season=2, number=1, air_date=TODAY),
            ],
        )
        _, request = await self.tap_and_see_the_search()
        self.assertEqual(request.episode_count, 2)

    async def test_an_unknown_episode_count_still_works(self) -> None:
        self.tvmaze.get_show_with_episodes.return_value = (tvmaze_show(), [])
        reply, request = await self.tap_and_see_the_search()
        self.assertIsNone(request.episode_count)
        self.assertEqual(reply.body["outcome"], "grabbed", reply.body)

    async def test_tvmaze_failing_for_the_episodes_still_works(self) -> None:
        self.tvmaze.get_show_with_episodes.side_effect = TVMazeError("down")
        reply, request = await self.tap_and_see_the_search()
        self.assertIsNone(request.episode_count)
        self.assertEqual(reply.body["outcome"], "grabbed", reply.body)

    async def test_it_says_when_season_1_isnt_out(self) -> None:
        self.tvmaze.get_show_with_episodes.return_value = (
            tvmaze_show(),
            [season_one(1, 1, TODAY + timedelta(days=5))],
        )
        reply = await self.post("/v1/download", SERIES_BODY)
        self.assertEqual(reply.body["outcome"], "nothing_found")
        self.assertEqual(reply.body["message"], t(NOT_OUT, label="Severance S01"))
        self.grabs.grab.assert_not_awaited()

    async def test_downloaded_before_says_already(self) -> None:
        self.grabs.has_grabbed.return_value = True
        reply = await self.post("/v1/download", SERIES_BODY)
        self.assertEqual(reply.body["outcome"], "already")
        self.grabs.has_grabbed.assert_awaited_with(SEASON_ONE)

    async def test_choosing_by_hand_works_too(self) -> None:
        reply = await self.post("/v1/releases", SERIES_BODY)
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual([one["title"] for one in reply.body["releases"]], [PACK])
        chosen = await self.post("/v1/grab", {"ref": reply.body["releases"][0]["ref"]})
        self.assertEqual(chosen.body["outcome"], "grabbed", chosen.body)
        self.assertEqual(self.grabs.grab.await_args.args[1], SEASON_ONE)

    async def test_a_series_tvmaze_doesnt_know_gets_a_plain_answer(self) -> None:
        self.tvmaze.lookup_show.return_value = None
        reply = await self.post("/v1/download", SERIES_BODY)
        self.assertEqual(reply.status, 502)
        self.assertEqual(reply.body["error"], "failed")
        self.assertEqual(reply.body["message"], t(NO_MATCH, title="Severance"))
        self.grabs.grab.assert_not_awaited()

    async def test_tmdb_or_tvmaze_being_down_gets_a_plain_answer(self) -> None:
        for problem in (TMDBError("SECRET"), TVMazeError("SECRET"), TVMazeNotFound("SECRET")):
            with self.subTest(problem=type(problem).__name__):
                self.discovery.clear_cache()
                self.tmdb.tv_details.side_effect = problem
                reply = await self.post("/v1/download", SERIES_BODY)
                self.assertEqual(reply.status, 502)
                self.assertEqual(reply.body["message"], t(NO_LOOKUP, title="Severance"))

    async def test_a_series_never_offered_is_no_longer_there(self) -> None:
        reply = await self.post("/v1/download", {"kind": "series", "id": 777})
        self.assertEqual(reply.status, 410)
        self.assertEqual(reply.body["error"], "no_longer_there")
        self.tvmaze.lookup_show.assert_not_awaited()

    async def test_a_series_on_the_shortlist_still_downloads_after_a_restart(self) -> None:
        row = await self.put_on_shortlist("series", TMDB_SEVERANCE)
        self.assertEqual(row["suggestion_id"], TMDB_SEVERANCE)
        self.restart()
        self.use_sources(self.good(PACK))
        reply = await self.post("/v1/download", SERIES_BODY)
        self.assertEqual(reply.body["outcome"], "grabbed", reply.body)
        self.assertEqual(self.grabs.grab.await_args.args[1], SEASON_ONE)
        self.tmdb.tv.assert_awaited_once_with(TMDB_SEVERANCE)

    async def test_a_forged_series_row_downloads_nothing(self) -> None:
        row = await self.put_on_shortlist("series", 777)
        self.assertIsNone(row["suggestion_id"])
        reply = await self.post("/v1/download", {"kind": "series", "id": 777})
        self.assertEqual(reply.status, 410)
        self.assertEqual(reply.body["error"], "no_longer_there")
        self.tvmaze.search_shows.assert_not_awaited()
        self.grabs.grab.assert_not_awaited()

    async def test_a_series_row_tmdb_cant_be_asked_about_gets_a_plain_answer(self) -> None:
        await self.put_on_shortlist("series", TMDB_SEVERANCE, "Severance", 2022)
        self.restart()
        self.tmdb.tv.side_effect = TMDBError("SECRET")
        self.use_sources(self.good(PACK))
        reply = await self.post("/v1/download", SERIES_BODY)
        self.assertEqual(reply.status, 502)
        self.assertEqual(reply.body["message"], t(NO_LOOKUP, title="Severance"))
        self.tvmaze.lookup_show.assert_not_awaited()
        self.grabs.grab.assert_not_awaited()

    async def test_bad_ids_are_a_bad_request(self) -> None:
        for body in (
            {"kind": "series"},
            {"kind": "series", "id": "1"},
            {"kind": "series", "id": True},
        ):
            with self.subTest(body):
                self.assertEqual((await self.post("/v1/download", body)).status, 400)


class GenreNamesTests(SuggestionsCase):
    async def test_the_names_are_asked_once_per_kind(self) -> None:
        self.tmdb.trending_movies.return_value = [film(2, genre_ids=(18,))]
        self.tmdb.trending_tv.return_value = [tmdb_show(genre_ids=(18,))]
        for _ in range(2):
            await self.ask("movie")
            await self.ask("series")
        self.assertEqual(
            [call.args[0] for call in self.tmdb.genres.await_args_list],
            [MediaType.MOVIE, MediaType.TV],
        )

    async def test_the_names_are_in_english_whatever_the_apps_language(self) -> None:
        self.tmdb.trending_movies.return_value = [film(2, genre_ids=(18,))]
        with mock.patch("app.services.discovery.tmdb_language", return_value="nl-NL"):
            await self.ask("movie")
        self.assertEqual(self.tmdb.genres.await_args.kwargs["language"], "en-US")

    async def test_a_name_for_two_genres_becomes_both_and_each_once(self) -> None:
        self.tmdb.genres.return_value = {
            10759: "Action & Adventure",
            10765: "Sci-Fi & Fantasy",
            10768: "War & Politics",
            28: "Action",
            18: "Drama",
        }
        self.tmdb.trending_tv.return_value = [
            tmdb_show(genre_ids=(10759, 28, 18, 10765, 10768))
        ]
        self.assertEqual(
            (await self.ask("series")).body["items"][0]["genres"],
            ["Action", "Adventure", "Drama", "Science Fiction", "Fantasy", "War", "Politics"],
        )

    async def test_a_failure_isnt_remembered(self) -> None:
        self.tmdb.trending_movies.return_value = [film(2, genre_ids=(18,))]
        self.tmdb.genres.side_effect = TMDBError("down")
        self.assertEqual((await self.ask("movie")).body["items"][0]["genres"], [])
        self.tmdb.genres.side_effect = None
        self.assertEqual((await self.ask("movie")).body["items"][0]["genres"], ["Drama"])

    async def test_the_client_reads_tmdbs_list(self) -> None:
        asked: list[httpx.URL] = []

        def answer(request: httpx.Request) -> httpx.Response:
            asked.append(request.url)
            return httpx.Response(
                200, json={"genres": [{"id": 18, "name": "Drama"}, {"id": 35, "name": " "}]}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(answer)) as http:
            client = TMDBClient(http, api_key=lambda: "0123456789abcdef0123456789abcdef")
            self.assertEqual(await client.genres(MediaType.TV, language="nl-NL"), {18: "Drama"})
            await client.genres(MediaType.MOVIE, language="en-US")
        self.assertEqual(asked[0].path, "/3/genre/tv/list")
        self.assertEqual(asked[0].params["language"], "nl-NL")
        self.assertEqual(asked[1].path, "/3/genre/movie/list")
