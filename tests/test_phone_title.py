"""The phone's title page (`GET /v1/title`, docs/companion/2026-11-20-title-page-design.md): any
TMDB film's or series' page, as find's result plus its details. The real DiscoveryService,
LibraryService, PhoneFind and PhoneDownloads over a fake TMDB and TVMaze (FindCase); one test goes
through the real TMDBClient over httpx.MockTransport. Nothing here reaches TMDB, TVMaze, Plex or a
torrent program."""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest import mock

import httpx

from app import i18n
from app.i18n import Language, t
from app.models import MediaType, MovieDetails, PairedPhone, TVDetails
from app.models.domain import CastMember, TitleExtras, TitleInfo, Video
from app.services.discovery import DiscoveryService
from app.services.phone.server import Reply, Request
from app.services.phone.title import TITLE_CAST, PhoneTitle
from app.services.tmdb import (
    FilmPage,
    TMDBAuthError,
    TMDBBadReply,
    TMDBClient,
    TMDBError,
    TMDBNotConfigured,
    TMDBNotFound,
)
from tests.test_phone_coming_up import NOW, SEVERANCE
from tests.test_phone_find import (
    ARRIVAL,
    ARRIVAL_RELEASE,
    COULDNT_SEARCH,
    KEY_REFUSED,
    NO_KEY,
    FindCase,
)
from tests.test_phone_person import grab, page, row, status_reading
from tests.test_phone_suggestions import OUT_LAST_YEAR, TMDB_SEVERANCE, film, tmdb_show
from tests.test_title_extras import MOVIE, TV, V3_KEY, FakeTMDB

NOT_FOUND = "AirGrabber couldn't find that title on TMDB."
PAGE_KEYS = (
    "backdrop_url", "tagline", "genres", "rating", "runtime", "seasons", "network",
    "certification", "trailer", "cast",
)


def trailer(key: str = "yt-arrival", site: str = "YouTube") -> Video:
    return Video(key=key, site=site, type="Trailer", official=True, language="en",
                 name="Official Trailer")


def actor(n: int, *, id: int | None = None, photo: bool = True) -> CastMember:
    return CastMember(
        name=f"Actor {n}",
        character=f"Part {n}",
        profile_path=f"/actor{n}.jpg" if photo else None,
        id=id,
    )


class TitleCase(FindCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.status = status_reading(None)
        self.find = self.new_find()
        self.use_sources(self.good(ARRIVAL_RELEASE))
        # The film: its card, its page (details, info, cast, videos) and its English info.
        # (Discovery holds on to the first mocks of `movie`, `movie_details` and `tv_details`:
        # theirs are changed, not replaced.)
        self.tmdb.movie.side_effect = self.film_of
        self.film_info = TitleInfo(
            id=ARRIVAL,
            overview="Louise is asked to talk to the visitors.",
            tagline="Why are they here?",
            genres=("Science Fiction", "Drama"),
            backdrop_path="/arrival-back.jpg",
            runtime=116,
            original_language="en",
            vote_average=7.63,
            vote_count=18000,
        )
        self.film_details = MovieDetails(
            id=ARRIVAL, runtime=116, certification="PG-13", digital_release=OUT_LAST_YEAR
        )
        self.film_cast: tuple[CastMember, ...] = (
            actor(1, id=501), actor(2, id=502, photo=False), actor(3)
        )
        self.film_videos: tuple[Video, ...] = (trailer(),)
        self.tmdb.film_page = mock.AsyncMock(side_effect=self.film_page)
        self.tmdb.movie_details.side_effect = self.details_of
        self.tmdb.movie_info = mock.AsyncMock(side_effect=self.film_info_in)
        self.tmdb.videos = mock.AsyncMock(return_value=[])
        # The series: its card, its info (the app's language is English: one answer for both),
        # its cast, its videos and its details.
        self.show_info = TitleInfo(
            id=TMDB_SEVERANCE,
            overview="Mark leads a team whose memories are split.",
            tagline="",
            genres=("Drama", "Sci-Fi & Fantasy"),
            backdrop_path="/severance-back.jpg",
            seasons=2,
            original_language="en",
            vote_average=8.4,
            vote_count=2500,
        )
        self.tmdb.tv = mock.AsyncMock(side_effect=self.show_of)
        self.tmdb.tv_info = mock.AsyncMock(side_effect=lambda id, language=None: self.show_info)
        self.tmdb.cast = mock.AsyncMock(return_value=[actor(7, id=707), actor(8)])
        self.tmdb.tv_details.side_effect = lambda id: TVDetails(
            id=id, number_of_seasons=2, imdb_id="tt1", networks=("Apple TV+", "Other")
        )
        self.title = PhoneTitle(self.find, self.discovery, self.tmdb, self.profiles)

    async def film_of(self, id: int) -> Any:
        if id != ARRIVAL:
            raise TMDBNotFound("TMDB /movie/{id}: not found (HTTP 404)")
        return film(ARRIVAL, "Arrival", overview="A linguist.")

    async def show_of(self, id: int) -> Any:
        if id != TMDB_SEVERANCE:
            raise TMDBNotFound("TMDB /tv/{id}: not found (HTTP 404)")
        return tmdb_show(overview="Work.")

    async def details_of(self, id: int) -> MovieDetails:
        return self.film_details

    async def film_page(self, id: int, **_: Any) -> FilmPage:
        return FilmPage(self.film_details, self.film_info, self.film_cast, self.film_videos)

    async def film_info_in(self, id: int, language: str | None = None) -> TitleInfo:
        return self.film_info

    async def open_page(self, query: dict[str, str], phone: PairedPhone | None = None) -> Reply:
        request = Request("GET", "/v1/title", query, {}, phone or self.phone)
        reply: Reply = await self.title.routes()[("GET", "/v1/title")](request)
        json.dumps(reply.body)  # what the server sends
        return reply

    async def page_of(self, kind: str, id: int) -> dict[str, Any]:
        reply = await self.open_page({"kind": kind, "id": str(id)})
        self.assertEqual(reply.status, 200, reply.body)
        return reply.body

    async def film_page_now(self) -> dict[str, Any]:
        return await self.page_of("movie", ARRIVAL)

    async def series_page_now(self) -> dict[str, Any]:
        return await self.page_of("series", TMDB_SEVERANCE)


class FilmPageTests(TitleCase):
    async def test_a_film_is_finds_item_plus_its_page(self) -> None:
        self.assertEqual(
            await self.film_page_now(),
            {
                "kind": "movie",
                "id": ARRIVAL,
                "title": "Arrival",
                "year": 2025,
                "poster_url": f"https://image.tmdb.org/t/p/w342/{ARRIVAL}.jpg",
                "overview": "Louise is asked to talk to the visitors.",  # the extras'
                "state": "new",
                "out": True,
                "backdrop_url": "https://image.tmdb.org/t/p/w1280/arrival-back.jpg",
                "tagline": "Why are they here?",
                "genres": ["Science Fiction", "Drama"],
                "rating": 7.6,
                "runtime": 116,
                "seasons": None,
                "network": None,
                "certification": "PG-13",
                "trailer": {"youtube_key": "yt-arrival", "name": "Official Trailer"},
                "cast": [
                    {"name": "Actor 1", "character": "Part 1",
                     "photo_url": "https://image.tmdb.org/t/p/w185/actor1.jpg", "tmdb_id": 501},
                    {"name": "Actor 2", "character": "Part 2", "photo_url": None, "tmdb_id": 502},
                    {"name": "Actor 3", "character": "Part 3",
                     "photo_url": "https://image.tmdb.org/t/p/w185/actor3.jpg", "tmdb_id": None},
                ],
            },
        )

    async def test_the_item_is_exactly_finds_result(self) -> None:
        self.films_found(film(ARRIVAL, "Arrival", overview="A linguist."))
        [found] = await self.found()
        title = await self.film_page_now()
        self.assertEqual(
            {key: value for key, value in title.items() if key not in PAGE_KEYS},
            found | {"overview": self.film_info.overview},
        )

    async def test_a_film_being_downloaded_says_how_far(self) -> None:
        self.grabs.has_grabbed.side_effect = lambda target: target.media_id == ARRIVAL
        self.status.latest = page(row(grab(MediaType.MOVIE, ARRIVAL), progress=0.42))
        title = await self.film_page_now()
        self.assertEqual((title["state"], title["progress"]), ("downloading", 42))
        self.assertEqual(self.status.mock_calls, [])

    async def test_without_votes_a_tagline_or_an_overview_in_the_extras(self) -> None:
        self.film_info = self.film_info.model_copy(
            update={"vote_count": 0, "tagline": "", "overview": "", "backdrop_path": None}
        )
        title = await self.film_page_now()
        self.assertIsNone(title["rating"])
        self.assertIsNone(title["tagline"])
        self.assertIsNone(title["backdrop_url"])
        self.assertEqual(title["overview"], "A linguist.")  # find's item's

    async def test_no_certification_or_runtime_is_null(self) -> None:
        self.film_details = self.film_details.model_copy(
            update={"certification": None, "runtime": None}
        )
        self.film_info = self.film_info.model_copy(update={"runtime": None})
        title = await self.film_page_now()
        self.assertEqual((title["certification"], title["runtime"]), (None, None))

    async def test_the_details_runtime_when_the_info_has_none(self) -> None:
        self.film_info = self.film_info.model_copy(update={"runtime": None})
        self.assertEqual((await self.film_page_now())["runtime"], 116)

    async def test_at_most_fifteen_actors_in_billing_order(self) -> None:
        self.assertEqual(TITLE_CAST, 15)
        self.film_cast = tuple(actor(n, id=n) for n in range(1, 21))
        cast = (await self.film_page_now())["cast"]
        self.assertEqual([member["tmdb_id"] for member in cast], list(range(1, 16)))

    async def test_genres_are_english_and_split_while_the_pc_speaks_dutch(self) -> None:
        before = i18n.current_language()
        i18n.set_language(Language.NL)
        self.addCleanup(i18n.set_language, before)
        dutch = self.film_info.model_copy(
            update={"overview": "Louise moet met de bezoekers praten.", "genres": ("Drama",)}
        )
        english = self.film_info.model_copy(update={"genres": ("Action & Adventure", "Drama")})

        async def info(id: int, language: str | None = None) -> TitleInfo:
            self.assertEqual(language, "en-US")
            return english

        async def page_in(id: int, **asked: Any) -> FilmPage:
            self.assertEqual(asked["language"], "nl-NL")
            return FilmPage(self.film_details, dutch, self.film_cast, self.film_videos)

        self.tmdb.movie_info.side_effect = info
        self.tmdb.film_page.side_effect = page_in
        title = await self.film_page_now()
        self.assertEqual(title["genres"], ["Action", "Adventure", "Drama"])
        self.assertEqual(title["overview"], "Louise moet met de bezoekers praten.")


class TrailerTests(TitleCase):
    async def test_youtubes_trailer_is_given_by_its_key(self) -> None:
        self.assertEqual(
            (await self.film_page_now())["trailer"],
            {"youtube_key": "yt-arrival", "name": "Official Trailer"},
        )

    async def test_no_trailer_is_null(self) -> None:
        self.film_videos = ()
        self.assertIsNone((await self.film_page_now())["trailer"])

    async def test_a_trailer_elsewhere_than_youtube_is_null(self) -> None:
        self.film_videos = (trailer("381999701", site="Vimeo"),)
        self.assertIsNone((await self.film_page_now())["trailer"])
        # Even when the extras hold one (pick_trailer takes YouTube's only).
        extras = TitleExtras(info=self.film_info, trailer=trailer("381999701", site="Vimeo"))
        with mock.patch.object(self.discovery, "movie_extras", mock.AsyncMock(return_value=extras)):
            self.assertIsNone((await self.film_page_now())["trailer"])


class SeriesPageTests(TitleCase):
    async def test_a_series_is_finds_item_plus_its_page(self) -> None:
        await self.library.untrack(SEVERANCE, profile_id=self.person)  # TVMaze's, by name
        self.assertEqual(
            await self.series_page_now(),
            {
                "kind": "series",
                "id": TMDB_SEVERANCE,
                "title": "Severance",
                "year": 2022,
                "poster_url": f"https://image.tmdb.org/t/p/w342/{TMDB_SEVERANCE}.jpg",
                "overview": "Mark leads a team whose memories are split.",
                "state": "new",
                "out": True,
                "backdrop_url": "https://image.tmdb.org/t/p/w1280/severance-back.jpg",
                "tagline": None,
                "genres": ["Drama", "Science Fiction", "Fantasy"],
                "rating": 8.4,
                "runtime": None,
                "seasons": 2,
                "network": "Apple TV+",
                "certification": None,
                "trailer": None,
                "cast": [
                    {"name": "Actor 7", "character": "Part 7",
                     "photo_url": "https://image.tmdb.org/t/p/w185/actor7.jpg", "tmdb_id": 707},
                    {"name": "Actor 8", "character": "Part 8",
                     "photo_url": "https://image.tmdb.org/t/p/w185/actor8.jpg", "tmdb_id": None},
                ],
            },
        )

    async def test_the_item_is_exactly_finds_result(self) -> None:
        self.shows_found(tmdb_show(overview="Work."))
        [found] = await self.found()
        title = await self.series_page_now()
        self.assertEqual(found["state"], "following")  # the phone's person follows Severance
        self.assertEqual(
            {key: value for key, value in title.items() if key not in PAGE_KEYS},
            found | {"overview": self.show_info.overview},
        )

    async def test_a_youtube_trailer_and_no_network(self) -> None:
        self.tmdb.videos.return_value = [trailer("yt-severance")]
        self.tmdb.tv_details.side_effect = lambda id: TVDetails(id=id, imdb_id="tt1")
        title = await self.series_page_now()
        self.assertEqual(title["trailer"], {"youtube_key": "yt-severance",
                                            "name": "Official Trailer"})
        self.assertIsNone(title["network"])

    async def test_the_details_seasons_when_the_info_has_none(self) -> None:
        self.show_info = self.show_info.model_copy(update={"seasons": None})
        self.tmdb.tv_details.side_effect = lambda id: TVDetails(id=id, number_of_seasons=3)
        self.assertEqual((await self.series_page_now())["seasons"], 3)


class OptionalPartsTests(TitleCase):
    async def test_no_extras_leaves_the_english_info_and_no_cast_or_trailer(self) -> None:
        self.tmdb.film_page.side_effect = TMDBBadReply("TMDB /movie/329865: unexpected payload")
        title = await self.film_page_now()
        self.assertEqual((title["cast"], title["trailer"]), ([], None))
        self.assertEqual(title["overview"], "A linguist.")  # no extras: find's
        self.assertEqual(title["tagline"], "Why are they here?")  # the English info's
        self.assertEqual(title["rating"], 7.6)
        self.assertEqual(title["runtime"], 116)

    async def test_no_english_info_leaves_no_genres(self) -> None:
        self.tmdb.movie_info.side_effect = TMDBError("out of reach")
        title = await self.film_page_now()
        self.assertEqual(title["genres"], [])
        self.assertEqual(title["tagline"], "Why are they here?")  # the extras'

    async def test_no_details_leaves_no_certification(self) -> None:
        self.discovery.clear_cache()
        with mock.patch.object(
            self.discovery, "movie_details", mock.AsyncMock(side_effect=TMDBError("down"))
        ):
            title = await self.film_page_now()
        self.assertIsNone(title["certification"])
        self.assertEqual(title["runtime"], 116)  # the info's
        self.assertTrue(title["out"])  # find's own: simply out without TMDB's details

    async def test_nothing_but_the_series_itself(self) -> None:
        self.tmdb.tv_info.side_effect = TMDBNotFound("TMDB /tv/{id}: not found (HTTP 404)")
        self.tmdb.tv_details.side_effect = TMDBError("down")
        title = await self.series_page_now()
        self.assertEqual(
            {key: title[key] for key in PAGE_KEYS},
            {"backdrop_url": None, "tagline": None, "genres": [], "rating": None,
             "runtime": None, "seasons": None, "network": None, "certification": None,
             "trailer": None, "cast": []},
        )
        self.assertEqual(title["overview"], "Work.")

    async def test_a_key_refused_by_an_optional_part_is_still_a_409(self) -> None:
        self.tmdb.movie_info.side_effect = TMDBAuthError("refused")
        reply = await self.open_page({"kind": "movie", "id": str(ARRIVAL)})
        self.assertEqual((reply.status, reply.body),
                         (409, {"error": "tmdb_key_refused", "message": t(KEY_REFUSED)}))


class ErrorTests(TitleCase):
    async def test_what_cant_be_read_is_a_bad_request(self) -> None:
        for query in (
            {},
            {"kind": "movie"},
            {"id": str(ARRIVAL)},
            {"kind": "film", "id": str(ARRIVAL)},
            {"kind": "tv", "id": str(ARRIVAL)},
            {"kind": "Movie", "id": str(ARRIVAL)},
            {"kind": "movie", "id": ""},
            {"kind": "movie", "id": "0"},
            {"kind": "movie", "id": "000"},
            {"kind": "movie", "id": "-5"},
            {"kind": "movie", "id": "1.5"},
            {"kind": "movie", "id": " 5"},
            {"kind": "movie", "id": "12345678901"},  # 11 digits
            {"kind": "movie", "id": "٣"},  # a digit, but not one TMDB writes
        ):
            with self.subTest(query):
                reply = await self.open_page(query)
                self.assertEqual(reply.status, 400)
                self.assertEqual(reply.body["error"], "bad_request")
        self.tmdb.movie.assert_not_awaited()
        self.assertEqual((await self.open_page({"kind": "movie", "id": "0329865"})).status, 200)

    async def test_a_title_tmdb_doesnt_know_is_not_found(self) -> None:
        for kind in ("movie", "series"):
            with self.subTest(kind):
                reply = await self.open_page({"kind": kind, "id": "4242"})
                self.assertEqual(
                    (reply.status, reply.body),
                    (404, {"error": "not_found", "message": t(NOT_FOUND)}),
                )

    async def test_without_a_tmdb_key_nothing_is_asked_and_it_says_so(self) -> None:
        self.tmdb.configured = False
        reply = await self.open_page({"kind": "movie", "id": str(ARRIVAL)})
        self.assertEqual(
            (reply.status, reply.body), (409, {"error": "no_tmdb_key", "message": t(NO_KEY)})
        )
        self.tmdb.movie.assert_not_awaited()
        self.tmdb.film_page.assert_not_awaited()

    async def test_a_key_that_disappears_or_is_refused_says_so_with_finds_sentences(self) -> None:
        for failure, code, said in (
            (TMDBNotConfigured("TMDB API key is not set"), "no_tmdb_key", NO_KEY),
            (TMDBAuthError("refused"), "tmdb_key_refused", KEY_REFUSED),
        ):
            for kind in ("movie", "series"):
                with self.subTest(code, kind=kind):
                    self.discovery.clear_cache()
                    self.tmdb.movie.side_effect = failure
                    self.tmdb.tv.side_effect = failure
                    reply = await self.open_page({"kind": kind, "id": "77"})
                    self.assertEqual(
                        (reply.status, reply.body), (409, {"error": code, "message": t(said)})
                    )

    async def test_tmdb_out_of_reach_is_a_plain_502(self) -> None:
        self.tmdb.movie.side_effect = TMDBError("SECRET")
        with self.assertLogs("app", "DEBUG") as logs:
            reply = await self.open_page({"kind": "movie", "id": str(ARRIVAL)})
        self.assertEqual(reply.status, 502)
        self.assertEqual(reply.body, {"error": "failed", "message": t(COULDNT_SEARCH)})
        text = "\n".join(logs.output)
        self.assertIn("Phone title: TMDB couldn't be asked: TMDBError", text)
        self.assertNotIn("SECRET", text)

    async def test_a_deleted_person_gets_a_plain_answer(self) -> None:
        reply = await self.open_page({"kind": "movie", "id": str(ARRIVAL)}, self.phone_of(12345))
        self.assertEqual(reply.status, 401)
        self.assertEqual(reply.body["error"], "not_paired")
        self.tmdb.movie.assert_not_awaited()

    async def test_the_route_is_get_v1_title(self) -> None:
        self.assertEqual(list(self.title.routes()), [("GET", "/v1/title")])


class FoundTitleTests(TitleCase):
    """A title page's title is remembered as found: the found routes take it as a search
    result."""

    async def test_a_titles_film_downloads_and_goes_on_the_list(self) -> None:
        await self.film_page_now()
        reply = await self.post("/v1/download", {"kind": "movie", "id": ARRIVAL, "found": True})
        self.assertEqual(reply.body["outcome"], "grabbed", reply.body)
        listed = [entry.movie_id for entry in await self.watchlist.by_release_of(self.person)]
        self.assertEqual(listed, [ARRIVAL])

    async def test_a_titles_series_has_its_episodes_and_may_be_followed(self) -> None:
        await self.series_page_now()
        reply = await self.episodes(TMDB_SEVERANCE)
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual([season["number"] for season in reply.body["seasons"]], [1, 2, 3])
        self.assertEqual((await self.follow_it({"id": TMDB_SEVERANCE})).status, 200)

    async def test_a_titles_series_episode_downloads(self) -> None:
        await self.series_page_now()
        self.use_sources(self.good("Severance.S01E02.1080p.WEB.x264-GROUP"))
        body = {"kind": "found_episode", "id": TMDB_SEVERANCE, "season": 1, "episode": 2}
        reply = await self.post("/v1/download", body)
        self.assertEqual(reply.body["outcome"], "grabbed", reply.body)

    async def test_without_the_page_the_film_isnt_found(self) -> None:
        reply = await self.post("/v1/download", {"kind": "movie", "id": ARRIVAL, "found": True})
        self.assertEqual(reply.status, 410)


class TitleLogTests(TitleCase):
    async def test_the_log_never_holds_a_title_id_name_or_character(self) -> None:
        self.tmdb.movie_info.side_effect = TMDBError("down")
        with self.assertLogs("app", "DEBUG") as logs:
            await self.film_page_now()
            await self.series_page_now()
            await self.open_page({"kind": "movie", "id": "4242"})
        text = "\n".join(logs.output)
        self.assertIn("Phone title: a movie page, 3 cast, a trailer", text)
        self.assertIn("Phone title: no English info from TMDB: TMDBError", text)
        for secret in ("Arrival", "Severance", "Actor", "Part 1", str(ARRIVAL),
                       str(TMDB_SEVERANCE), "4242", "501", "707"):
            self.assertNotIn(secret, text)

    async def test_through_the_real_client_nothing_identifying_is_logged(self) -> None:
        server = FakeTMDB()
        server.failures[f"/3/tv/{TV}/videos"] = 503  # a broken part: only it is left out
        async with httpx.AsyncClient(transport=httpx.MockTransport(server)) as http:
            client = TMDBClient(http, api_key=lambda: V3_KEY, retry_delay=0)
            self.discovery = DiscoveryService(client, self.tvmaze, self.library, clock=lambda: NOW)
            self.find = self.new_find()
            self.title = PhoneTitle(self.find, self.discovery, client, self.profiles)
            with self.assertLogs("app", "DEBUG") as logs:
                film_page = await self.page_of("movie", MOVIE)
                series_page = await self.page_of("series", TV)
        self.assertEqual(film_page["title"], "Harbour Lights")
        self.assertEqual(film_page["cast"][0]["tmdb_id"], 7000001)
        self.assertEqual(len(film_page["cast"]), 10)
        self.assertEqual(film_page["genres"], ["Drama", "Mystery"])
        self.assertEqual([one["tmdb_id"] for one in series_page["cast"]],
                         [7200001, 7200002, 7200003])
        self.assertIsNone(series_page["trailer"])
        text = "\n".join(logs.output)
        self.assertIn("TMDB tv: no videos", text)  # by its kind only
        for secret in (str(MOVIE), str(TV), "Harbour", "Orchard", "Maren", "Henrietta",
                       "Ida Marlowe", V3_KEY):
            self.assertNotIn(secret, text)


if __name__ == "__main__":
    unittest.main()
