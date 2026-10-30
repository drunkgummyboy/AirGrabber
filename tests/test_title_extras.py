"""The info window's TMDB extras: the client's info / videos / cast / find requests and parsing,
`pick_trailer`, and DiscoveryService's `movie_extras` / `show_extras` (language, the English
fallback, the TVMaze → TMDB match, caching, what a failure leaves out). TMDB answers come from
tests/fixtures/tmdb/extras_*.json (built from TMDB's API reference, see its README.md) on
httpx.MockTransport; no live network."""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from app import i18n
from app.database import Database
from app.i18n import Language
from app.models import MediaType, Show, Video
from app.services.discovery import (
    CAST_LIMIT,
    DiscoveryService,
    pick_trailer,
    tmdb_language,
    trailer_languages,
)
from app.services.library import LibraryService
from app.services.tmdb import TMDBAuthError, TMDBClient, TMDBError, TMDBNotConfigured
from app.services.tvmaze import TVMazeClient

FIXTURES = Path(__file__).parent / "fixtures" / "tmdb"
V3_KEY = "0123456789abcdef0123456789abcdef"
MOVIE = 4242  # "Harbour Lights"
TV = 7171  # "The Quiet Orchard"
ORCHARD_IMDB = "tt9900071"
ENGLISH_OVERVIEW = "A harbourmaster finds that someone steers her ships the wrong way."


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def video(
    key: str,
    *,
    site: str = "YouTube",
    kind: str = "Trailer",
    official: bool = True,
    language: str | None = "en",
    published: str | None = "2026-05-01T00:00:00Z",
) -> Video:
    return Video.model_validate(
        {
            "key": key,
            "site": site,
            "type": kind,
            "official": official,
            "language": language,
            "published_at": published,
        }
    )


class DutchMixin(unittest.TestCase):
    def use(self, language: Language) -> None:
        before = i18n.current_language()
        i18n.set_language(language)
        self.addCleanup(i18n.set_language, before)


class LanguageTests(DutchMixin):
    def test_english(self) -> None:
        self.use(Language.EN)
        self.assertEqual(tmdb_language(), "en-US")
        self.assertEqual(trailer_languages(), ("en",))

    def test_dutch(self) -> None:
        self.use(Language.NL)
        self.assertEqual(tmdb_language(), "nl-NL")
        self.assertEqual(trailer_languages(), ("nl", "en"))


class PickTrailerTests(unittest.TestCase):
    def test_only_youtube_trailers(self) -> None:
        videos = [video("t", kind="Teaser"), video("v", site="Vimeo"), video("f", kind="Featurette")]
        self.assertIsNone(pick_trailer(videos, ("en",)))
        self.assertIsNone(pick_trailer([], ("en",)))
        self.assertEqual(pick_trailer([*videos, video("y", site="youtube")], ("en",)).key, "y")  # type: ignore[union-attr]

    def test_official_first(self) -> None:
        fan = video("fan", official=False, language="nl")
        official = video("official", official=True, language="en")
        self.assertEqual(pick_trailer([fan, official], ("nl", "en")).key, "official")  # type: ignore[union-attr]

    def test_then_the_preferred_language(self) -> None:
        english = video("en", language="en", published="2026-06-01T00:00:00Z")
        dutch = video("nl", language="nl", published="2026-01-01T00:00:00Z")
        french = video("fr", language="fr", published="2026-09-01T00:00:00Z")
        self.assertEqual(pick_trailer([english, dutch, french], ("nl", "en")).key, "nl")  # type: ignore[union-attr]
        self.assertEqual(pick_trailer([french, english, dutch], ("en",)).key, "en")  # type: ignore[union-attr]
        # Neither preferred: any language will do (the newest).
        self.assertEqual(pick_trailer([french, video("none", language=None)], ("nl", "en")).key, "fr")  # type: ignore[union-attr]

    def test_then_the_newest(self) -> None:
        old = video("old", published="2025-01-01T00:00:00Z")
        new = video("new", published="2026-01-01T00:00:00Z")
        undated = video("undated", published=None)
        self.assertEqual(pick_trailer([old, undated, new], ("en",)).key, "new")  # type: ignore[union-attr]

    def test_the_watch_url(self) -> None:
        self.assertEqual(video("abc_DEF-1").watch_url, "https://www.youtube.com/watch?v=abc_DEF-1")
        self.assertIsNone(video("381999701", site="Vimeo").watch_url)


class FakeTMDB:
    """Serves the extras fixtures by path, records every request; a path in `failures` answers
    with that status."""

    def __init__(self) -> None:
        english = copy.deepcopy(load("extras_movie_info_nl.json"))
        english["overview"] = ENGLISH_OVERVIEW
        english["genres"] = [{"id": 18, "name": "Drama"}, {"id": 9648, "name": "Mystery"}]
        tv_english = copy.deepcopy(load("extras_tv_info_nl.json"))
        tv_english["overview"] = "A judge reopens the orchard case that made her career."
        self.payloads: dict[tuple[str, str | None], Any] = {
            (f"/3/movie/{MOVIE}", "nl-NL"): load("extras_movie_info_nl.json"),
            (f"/3/movie/{MOVIE}", "en-US"): english,
            (f"/3/movie/{MOVIE}/videos", None): load("extras_movie_videos.json"),
            (f"/3/movie/{MOVIE}/credits", None): load("extras_movie_credits.json"),
            (f"/3/tv/{TV}", "nl-NL"): load("extras_tv_info_nl.json"),
            (f"/3/tv/{TV}", "en-US"): tv_english,
            (f"/3/tv/{TV}/videos", None): {"id": TV, "results": []},
            (f"/3/tv/{TV}/aggregate_credits", None): load("extras_tv_aggregate_credits.json"),
            (f"/3/find/{ORCHARD_IMDB}", None): load("extras_find_tv.json"),
        }
        self.failures: dict[str, int] = {}
        self.requests: list[httpx.Request] = []

    def of(self, path: str) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.path == path]

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if (status := self.failures.get(path)) is not None:
            return httpx.Response(status)
        payload = self.payloads.get((path, request.url.params.get("language")))
        if payload is None:
            payload = self.payloads.get((path, None))
        if payload is None:
            if path.startswith("/3/find/"):
                payload = {"movie_results": [], "tv_results": []}
            else:
                return httpx.Response(404, json={"status_code": 34, "success": False})
        return httpx.Response(200, json=payload)


class ExtrasTestCase(DutchMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.db = await Database.open(Path(tmp.name) / "extras.db")
        self.addAsyncCleanup(self.db.close)
        self.server = FakeTMDB()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.server))
        self.addAsyncCleanup(self.http.aclose)
        self.key = V3_KEY
        self.now = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
        self.tmdb = TMDBClient(self.http, lambda: self.key, retry_delay=0)
        tvmaze = TVMazeClient(self.http, retry_delay=0)
        library = LibraryService(self.db.library, self.db.history, tvmaze)
        self.service = DiscoveryService(self.tmdb, tvmaze, library, clock=lambda: self.now)


class ClientTests(ExtrasTestCase):
    async def test_movie_info_in_a_language(self) -> None:
        info = await self.tmdb.movie_info(MOVIE, language="nl-NL")
        [request] = self.server.requests
        self.assertEqual(request.url.path, f"/3/movie/{MOVIE}")
        self.assertEqual(request.url.params["language"], "nl-NL")
        self.assertTrue(info.overview.startswith("Wanneer de vuurtoren"))
        self.assertEqual(info.genres, ("Drama", "Mysterie", "Thriller"))
        self.assertEqual((info.runtime, info.status, info.original_language), (128, "Released", "en"))
        self.assertEqual((info.vote_average, info.vote_count), (7.4, 312))
        self.assertEqual(
            info.backdrop_url("w780"), "https://image.tmdb.org/t/p/w780/madeup_movie_info_nl_1.jpg"
        )

    async def test_the_clients_own_language_by_default(self) -> None:
        await self.tmdb.movie_info(MOVIE)
        self.assertEqual(self.server.requests[0].url.params["language"], "en-US")

    async def test_tv_info(self) -> None:
        info = await self.tmdb.tv_info(TV, language="nl-NL")
        self.assertEqual(info.overview, "")  # TMDB has no Dutch text for it
        self.assertEqual((info.seasons, info.status), (2, "Returning Series"))
        self.assertEqual(info.genres, ("Drama", "Misdaad"))

    async def test_videos(self) -> None:
        videos = await self.tmdb.videos(MediaType.MOVIE, MOVIE, languages=["nl", "en"])
        [request] = self.server.requests
        self.assertEqual(request.url.params["include_video_language"], "nl,en,null")
        # The entry without a key is left out.
        self.assertEqual(len(videos), 6)
        dutch = next(video for video in videos if video.key == "officialNl4")
        self.assertEqual((dutch.language, dutch.official, dutch.type), ("nl", True, "Trailer"))
        self.assertEqual(dutch.published_at, datetime(2026, 5, 22, 8, 30, tzinfo=UTC))

    async def test_videos_without_languages(self) -> None:
        await self.tmdb.videos(MediaType.TV, TV)
        [request] = self.server.requests
        self.assertEqual(request.url.path, f"/3/tv/{TV}/videos")
        self.assertNotIn("include_video_language", request.url.params)

    async def test_movie_cast_in_billing_order(self) -> None:
        cast = await self.tmdb.cast(MediaType.MOVIE, MOVIE)
        self.assertEqual(self.server.requests[0].url.path, f"/3/movie/{MOVIE}/credits")
        self.assertEqual(len(cast), 10)  # the nameless entry is left out; crew isn't cast
        self.assertEqual((cast[0].name, cast[0].character), ("Maren Holloway", "Ida Marlowe"))
        self.assertEqual(
            cast[0].photo_url(), "https://image.tmdb.org/t/p/w185/madeup_movie_credits_1.jpg"
        )
        self.assertIsNone(cast[2].photo_url())
        self.assertEqual(cast[4].character, "")

    async def test_show_cast_takes_the_part_played_most(self) -> None:
        cast = await self.tmdb.cast(MediaType.TV, TV)
        self.assertEqual(self.server.requests[0].url.path, f"/3/tv/{TV}/aggregate_credits")
        self.assertEqual(
            [(member.name, member.character) for member in cast],
            [
                ("Henrietta Saal", "Judge Amsel"),
                ("Pieter van Loon", "Mattias Brede"),
                ("Ama Kingsley", ""),
            ],
        )

    async def test_find_a_show_by_imdb_id(self) -> None:
        self.assertEqual(await self.tmdb.find_tv(ORCHARD_IMDB), TV)
        [request] = self.server.requests
        self.assertEqual(request.url.params["external_source"], "imdb_id")
        self.assertIsNone(await self.tmdb.find_tv("tt0000001"))  # TMDB knows none

    async def test_find_asks_nothing_for_something_else_than_an_imdb_id(self) -> None:
        for odd in ("", "12345", "tt12/../movie", "tt123?x=1"):
            with self.subTest(odd=odd):
                self.assertIsNone(await self.tmdb.find_tv(odd))
        self.assertEqual(self.server.requests, [])

    async def test_configured(self) -> None:
        self.assertTrue(self.tmdb.configured)
        self.key = "  "
        self.assertFalse(self.tmdb.configured)


class MovieExtrasTests(ExtrasTestCase):
    async def test_english(self) -> None:
        self.use(Language.EN)
        extras = await self.service.movie_extras(MOVIE)
        info_requests = self.server.of(f"/3/movie/{MOVIE}")
        self.assertEqual([r.url.params["language"] for r in info_requests], ["en-US"])
        self.assertEqual(extras.info.overview, ENGLISH_OVERVIEW)
        self.assertIsNotNone(extras.trailer)
        assert extras.trailer is not None
        self.assertEqual(extras.trailer.key, "officialEn3")
        self.assertEqual(len(extras.cast), CAST_LIMIT)
        self.assertEqual(extras.cast[0].name, "Maren Holloway")
        [videos] = self.server.of(f"/3/movie/{MOVIE}/videos")
        self.assertEqual(videos.url.params["include_video_language"], "en,null")

    async def test_dutch(self) -> None:
        self.use(Language.NL)
        extras = await self.service.movie_extras(MOVIE)
        self.assertTrue(extras.info.overview.startswith("Wanneer de vuurtoren"))
        self.assertEqual(extras.info.genres, ("Drama", "Mysterie", "Thriller"))
        assert extras.trailer is not None
        self.assertEqual(extras.trailer.key, "officialNl4")  # official and Dutch
        [videos] = self.server.of(f"/3/movie/{MOVIE}/videos")
        self.assertEqual(videos.url.params["include_video_language"], "nl,en,null")
        # The Dutch text was there: no English request.
        self.assertEqual(len(self.server.of(f"/3/movie/{MOVIE}")), 1)

    async def test_dutch_without_a_dutch_overview_takes_the_english_one(self) -> None:
        self.use(Language.NL)
        self.server.payloads[(f"/3/movie/{MOVIE}", "nl-NL")]["overview"] = ""
        extras = await self.service.movie_extras(MOVIE)
        self.assertEqual(extras.info.overview, ENGLISH_OVERVIEW)
        self.assertEqual(extras.info.genres, ("Drama", "Mysterie", "Thriller"))  # still Dutch

    async def test_the_titles_own_language_is_asked_for_trailers_too(self) -> None:
        self.use(Language.NL)
        self.server.payloads[(f"/3/movie/{MOVIE}", "nl-NL")]["original_language"] = "ko"
        await self.service.movie_extras(MOVIE)
        [videos] = self.server.of(f"/3/movie/{MOVIE}/videos")
        self.assertEqual(videos.url.params["include_video_language"], "nl,en,ko,null")

    async def test_cached_for_24_hours(self) -> None:
        first = await self.service.movie_extras(MOVIE)
        self.now += timedelta(hours=23)
        self.assertEqual(await self.service.movie_extras(MOVIE), first)
        self.assertEqual(len(self.server.of(f"/3/movie/{MOVIE}/credits")), 1)
        self.now += timedelta(hours=2)
        await self.service.movie_extras(MOVIE)
        self.assertEqual(len(self.server.of(f"/3/movie/{MOVIE}/credits")), 2)

    async def test_failing_videos_or_cast_only_leave_those_out(self) -> None:
        self.server.failures[f"/3/movie/{MOVIE}/videos"] = 500
        self.server.failures[f"/3/movie/{MOVIE}/credits"] = 404
        with self.assertLogs("app.services", "INFO"):  # the retries and what was left out
            extras = await self.service.movie_extras(MOVIE)
        self.assertIsNone(extras.trailer)
        self.assertEqual(extras.cast, ())
        self.assertEqual(extras.info.runtime, 128)

    async def test_failing_details_raise_and_are_not_cached(self) -> None:
        self.server.failures[f"/3/movie/{MOVIE}"] = 404
        with self.assertRaises(TMDBError):
            await self.service.movie_extras(MOVIE)
        del self.server.failures[f"/3/movie/{MOVIE}"]
        self.assertEqual((await self.service.movie_extras(MOVIE)).info.id, MOVIE)

    async def test_a_rejected_key_raises(self) -> None:
        self.server.failures[f"/3/movie/{MOVIE}/credits"] = 401
        with self.assertRaises(TMDBAuthError):
            await self.service.movie_extras(MOVIE)

    async def test_without_a_key(self) -> None:
        self.key = ""
        with self.assertRaises(TMDBNotConfigured):
            await self.service.movie_extras(MOVIE)
        self.assertEqual(self.server.requests, [])

    async def test_clear_cache_forgets_them(self) -> None:
        await self.service.movie_extras(MOVIE)
        self.service.clear_cache()
        await self.service.movie_extras(MOVIE)
        self.assertEqual(len(self.server.of(f"/3/movie/{MOVIE}/credits")), 2)


class ShowExtrasTests(ExtrasTestCase):
    ORCHARD = Show(id=55001, name="The Quiet Orchard", imdb_id=ORCHARD_IMDB)

    async def test_found_by_imdb_id(self) -> None:
        self.use(Language.NL)
        extras = await self.service.show_extras(self.ORCHARD)
        assert extras is not None
        self.assertEqual(extras.info.id, TV)
        self.assertEqual(extras.info.genres, ("Drama", "Misdaad"))
        # No Dutch text: TMDB's English one.
        self.assertEqual(
            extras.info.overview, "A judge reopens the orchard case that made her career."
        )
        self.assertIsNone(extras.trailer)
        self.assertEqual([member.name for member in extras.cast][:1], ["Henrietta Saal"])

    async def test_the_match_is_cached(self) -> None:
        await self.service.show_extras(self.ORCHARD)
        await self.service.show_extras(self.ORCHARD)
        self.assertEqual(len(self.server.of(f"/3/find/{ORCHARD_IMDB}")), 1)
        self.assertEqual(len(self.server.of(f"/3/tv/{TV}/aggregate_credits")), 1)

    async def test_no_counterpart(self) -> None:
        unknown = Show(id=55002, name="Nowhere Lane", imdb_id="tt0000002")
        self.assertIsNone(await self.service.show_extras(unknown))
        self.assertIsNone(await self.service.show_extras(Show(id=55003, name="No IMDb")))
        self.assertEqual(len(self.server.of("/3/find/tt0000002")), 1)

    async def test_a_show_matched_from_discover_needs_no_lookup(self) -> None:
        self.service._tracked_via_tmdb[TV] = 55004  # what resolve() remembers
        show = Show(id=55004, name="The Quiet Orchard")  # no IMDb id needed
        extras = await self.service.show_extras(show)
        assert extras is not None
        self.assertEqual(extras.info.id, TV)
        self.assertEqual([r.url.path for r in self.server.requests if "/find/" in r.url.path], [])

    async def test_without_a_key(self) -> None:
        self.key = ""
        with self.assertRaises(TMDBNotConfigured):
            await self.service.show_extras(Show(id=55003, name="No IMDb"))
        self.assertEqual(self.server.requests, [])


if __name__ == "__main__":
    unittest.main()
