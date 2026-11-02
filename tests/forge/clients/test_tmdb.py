"""ForgeTmdb (MediaForge's tests/api/test_tmdb.py) on AirGrabber's TMDBClient, with
httpx.MockTransport instead of MediaForge's aiohttp fake server: never a live request."""

from __future__ import annotations

import functools
import io
import logging
import unittest
from collections.abc import Callable, Coroutine
from typing import Any

import httpx

from app.forge.clients.tmdb import (
    DEFAULT_LANGUAGE,
    JSON_TTL_S,
    ForgeTmdb,
    TmdbError,
    cache_key,
)
from app.forge.db.cache import CacheRepo
from app.forge.models.media import MediaKind
from app.models.tmdb import TMDBSeason, TMDBTitle
from app.services.tmdb import TMDBClient, TMDBNotConfigured
from tests.forge.db.base import ForgeDbTestCase

V3_KEY = "abcdef0123456789secret"
V4_TOKEN = "eyJhbGciOiJIUzI1NiJ9.payload.sig"
TMDB_LOGGER = "app.services.tmdb"


def movie(id_: int, title: str, date: str = "2021-10-22") -> dict[str, Any]:
    return {"id": id_, "title": title, "release_date": date, "poster_path": f"/{id_}.jpg"}


def results(*items: dict[str, Any]) -> httpx.Response:
    return httpx.Response(200, json={"page": 1, "results": list(items)})


class FakeTmdb:
    """MediaForge's FakeTmdb on httpx: replies queued per path (the last one repeats), 404 for
    a path without any, and every request recorded."""

    def __init__(self) -> None:
        self.replies: dict[str, list[httpx.Response | Exception]] = {}
        self.seen: list[httpx.Request] = []

    def add(self, path: str, *replies: httpx.Response | Exception) -> None:
        self.replies.setdefault(path, []).extend(replies)

    def requests_to(self, path: str) -> list[httpx.Request]:
        return [request for request in self.seen if request.url.path == path]

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        queue = self.replies.get(request.url.path)
        if not queue:
            return httpx.Response(404, json={"success": False, "status_code": 34})
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


class ForgeTmdbTestCase(ForgeDbTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.fake = FakeTmdb()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.fake))
        self.addAsyncCleanup(self.http.aclose)
        self.cache = CacheRepo(self.db)

    def forge(
        self,
        key: str | Callable[[], str] = V3_KEY,
        *,
        cache: bool = False,
        language: Callable[[], str] | None = None,
        client_language: str = "en-US",
    ) -> ForgeTmdb:
        api_key = key if callable(key) else (lambda: key)
        client = TMDBClient(self.http, api_key, retry_delay=0, language=client_language)
        options: dict[str, Any] = {"cache": self.cache if cache else None}
        if language is not None:
            options["language"] = language
        return ForgeTmdb(client, **options)


class RequestTests(ForgeTmdbTestCase):
    async def test_v3_key_sent_as_query(self) -> None:
        self.fake.add("/3/search/movie", results())
        await self.forge().search_movie("Dune", None)
        [seen] = self.fake.requests_to("/3/search/movie")
        self.assertEqual(seen.url.params["api_key"], V3_KEY)
        self.assertNotIn("Authorization", seen.headers)

    async def test_v4_token_sent_as_bearer(self) -> None:
        self.fake.add("/3/search/movie", results())
        await self.forge(V4_TOKEN).search_movie("Dune", None)
        [seen] = self.fake.requests_to("/3/search/movie")
        self.assertNotIn("api_key", seen.url.params)
        self.assertEqual(seen.headers["Authorization"], f"Bearer {V4_TOKEN}")

    async def test_movie_search_with_year_then_without_dedupes_in_order(self) -> None:
        self.fake.add(
            "/3/search/movie",
            results(movie(1, "Dune")),
            results(movie(2, "Dune", "1984-12-14"), movie(1, "Dune")),
        )
        found = await self.forge().search_movie("Dune", 2021)
        self.assertEqual([r.id for r in found], [1, 2])
        first, second = self.fake.requests_to("/3/search/movie")
        self.assertEqual(first.url.params["year"], "2021")
        self.assertNotIn("year", second.url.params)

    async def test_movie_search_without_year_asks_once(self) -> None:
        self.fake.add("/3/search/movie", results(movie(1, "Dune")))
        self.assertEqual([r.id for r in await self.forge().search_movie("Dune", None)], [1])
        self.assertEqual(len(self.fake.requests_to("/3/search/movie")), 1)

    async def test_tv_search_uses_first_air_date_year(self) -> None:
        self.fake.add(
            "/3/search/tv",
            results({"id": 9, "name": "The Office", "first_air_date": "2005-03-24"}),
        )
        [result] = await self.forge().search_tv("The Office", 2005)
        [seen] = self.fake.requests_to("/3/search/tv")
        self.assertEqual(seen.url.params["first_air_date_year"], "2005")
        self.assertNotIn("year", seen.url.params)
        self.assertEqual((result.title, result.year), ("The Office", 2005))

    async def test_include_adult_false_and_language_sent(self) -> None:
        self.fake.add("/3/search/movie", results())
        await self.forge(language=lambda: "nl-NL").search_movie("Dune", None)
        params = self.fake.requests_to("/3/search/movie")[0].url.params
        self.assertEqual(params["include_adult"], "false")
        self.assertEqual(params["language"], "nl-NL")

    async def test_names_language_is_not_the_interface_language(self) -> None:
        """AirGrabber's client asks in Dutch while Dutch is on; Forge's names stay en-US."""
        self.fake.add("/3/search/tv", results())
        self.fake.add("/3/tv/9", httpx.Response(200, json={"id": 9, "name": "The Office"}))
        forge = self.forge(client_language="nl-NL")
        self.assertEqual(forge.language, DEFAULT_LANGUAGE)
        await forge.search_tv("The Office", None)
        await forge.details(MediaKind.EPISODE, 9)
        await forge.season(9, 1)
        self.assertEqual(
            [request.url.params["language"] for request in self.fake.seen], ["en-US"] * 3
        )

    async def test_language_is_read_per_lookup(self) -> None:
        current = ["de-DE"]
        self.fake.add("/3/search/tv", results())
        forge = self.forge(language=lambda: current[0])
        await forge.search_tv("Dark", None)
        current[0] = "  "  # an empty setting falls back to en-US
        await forge.search_tv("Dark", None)
        self.assertEqual(
            [request.url.params["language"] for request in self.fake.seen], ["de-DE", "en-US"]
        )

    async def test_details(self) -> None:
        self.fake.add("/3/tv/9", httpx.Response(200, json={"id": 9, "name": "The Office"}))
        self.fake.add("/3/movie/1", httpx.Response(200, json=movie(1, "Dune")))
        forge = self.forge()
        show = await forge.details(MediaKind.EPISODE, 9)
        film = await forge.details(MediaKind.MOVIE, 1)
        assert show is not None and film is not None
        self.assertEqual(show.title, "The Office")
        self.assertEqual((film.title, film.year, film.poster_path), ("Dune", 2021, "/1.jpg"))
        self.assertEqual(self.fake.seen[0].url.params["include_adult"], "false")

    async def test_details_404_returns_none(self) -> None:
        self.assertIsNone(await self.forge().details(MediaKind.MOVIE, 5))

    async def test_season_404_returns_none(self) -> None:
        self.assertIsNone(await self.forge().season(9, 7))

    async def test_season_parsed(self) -> None:
        self.fake.add(
            "/3/tv/9/season/1",
            httpx.Response(
                200,
                json={
                    "season_number": 1,
                    "poster_path": "/s1.jpg",
                    "episodes": [{"episode_number": 2, "name": "Theseus", "extra": 1}],
                },
            ),
        )
        season = await self.forge().season(9, 1)
        assert season is not None
        self.assertEqual(season.poster_path, "/s1.jpg")
        self.assertEqual(season.episodes[0].name, "Theseus")

    async def test_bad_results_are_skipped(self) -> None:
        self.fake.add("/3/search/movie", results({"title": "no id"}, movie(3, "Ok")))
        with self.assertLogs(TMDB_LOGGER, "WARNING"):
            found = await self.forge().search_movie("x", None)
        self.assertEqual([r.id for r in found], [3])

    async def test_configured_follows_the_key(self) -> None:
        current = [""]
        forge = self.forge(lambda: current[0])
        self.assertFalse(forge.configured())
        current[0] = V3_KEY
        self.assertTrue(forge.configured())

    async def test_no_key_raises_tmdb_error_before_any_request(self) -> None:
        forge = self.forge("")
        with self.assertRaises(TmdbError) as caught:
            await forge.search_tv("Dark", None)
        self.assertIsInstance(caught.exception, TMDBNotConfigured)
        self.assertEqual(self.fake.seen, [])


class FailureTests(ForgeTmdbTestCase):
    async def test_retry_on_429_then_success(self) -> None:
        self.fake.add(
            "/3/search/movie",
            httpx.Response(429, json={}, headers={"Retry-After": "0"}),
            results(movie(1, "Dune")),
        )
        with self.assertLogs(TMDB_LOGGER, "WARNING"):
            found = await self.forge().search_movie("Dune", None)
        self.assertEqual([r.id for r in found], [1])

    async def test_5xx_three_times_raises_tmdb_error(self) -> None:
        self.fake.add("/3/search/movie", httpx.Response(503, json={}))
        with self.assertLogs(TMDB_LOGGER, "WARNING"), self.assertRaises(TmdbError):
            await self.forge().search_movie("Dune", None)
        self.assertEqual(len(self.fake.requests_to("/3/search/movie")), 3)

    async def test_network_error_becomes_tmdb_error(self) -> None:
        self.fake.add("/3/search/movie", httpx.ConnectError("unreachable"))
        with self.assertLogs(TMDB_LOGGER, "WARNING"), self.assertRaises(TmdbError):
            await self.forge().search_movie("Dune", None)

    async def test_key_never_in_exception_text_or_logs(self) -> None:
        def leaky(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/movie"):
                return httpx.Response(500, text=str(request.url))
            return leaky(request)

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(http.aclose)
        output = io.StringIO()
        capture = logging.StreamHandler(output)
        root = logging.getLogger()
        root.addHandler(capture)
        self.addCleanup(root.removeHandler, capture)
        for key in (V3_KEY, V4_TOKEN):
            forge = ForgeTmdb(TMDBClient(http, functools.partial(str, key), retry_delay=0))
            calls: list[Callable[[], Coroutine[Any, Any, object]]] = [
                lambda: forge.search_movie("Dune", 2021),
                lambda: forge.season(9, 1),
            ]
            for call in calls:
                with self.subTest(key=key), self.assertRaises(TmdbError) as caught:
                    await call()
                error: BaseException | None = caught.exception
                while error is not None:
                    self.assertNotIn(key, str(error))
                    self.assertNotIn(key, repr(error))
                    error = error.__cause__ or error.__context__
            self.assertNotIn(key, output.getvalue())


class CacheTests(ForgeTmdbTestCase):
    async def test_cache_hit_skips_network(self) -> None:
        self.fake.add("/3/search/movie", results(movie(1, "Dune")))
        forge = self.forge(cache=True)
        self.assertEqual([r.id for r in await forge.search_movie("Dune", None)], [1])
        self.assertEqual([r.id for r in await forge.search_movie("Dune", None)], [1])
        self.assertEqual(len(self.fake.requests_to("/3/search/movie")), 1)

    async def test_details_and_season_are_cached(self) -> None:
        self.fake.add("/3/movie/1", httpx.Response(200, json=movie(1, "Dune")))
        self.fake.add(
            "/3/tv/9/season/1",
            httpx.Response(200, json={"season_number": 1, "episodes": [{"episode_number": 1}]}),
        )
        forge = self.forge(cache=True)
        for _ in range(2):
            self.assertEqual(
                await forge.details(MediaKind.MOVIE, 1),
                TMDBTitle(id=1, title="Dune", date="2021-10-22", poster_path="/1.jpg"),
            )
            season = await forge.season(9, 1)
            self.assertIsInstance(season, TMDBSeason)
        self.assertEqual(len(self.fake.seen), 2)

    async def test_a_404_is_not_cached(self) -> None:
        forge = self.forge(cache=True)
        self.assertIsNone(await forge.season(9, 7))
        self.assertIsNone(await forge.season(9, 7))
        self.assertIsNone(await forge.details(MediaKind.MOVIE, 5))
        self.assertIsNone(await forge.details(MediaKind.MOVIE, 5))
        self.assertEqual(len(self.fake.seen), 4)
        self.assertEqual(await self.db.fetch_all("SELECT key FROM tmdb_cache"), [])

    async def test_cache_key_includes_language(self) -> None:
        self.fake.add(
            "/3/search/movie",
            results(movie(1, "Dune")),
            results(movie(1, "Duin")),
        )
        await self.forge(cache=True, language=lambda: "en-US").search_movie("Dune", None)
        [nl] = await self.forge(cache=True, language=lambda: "nl-NL").search_movie("Dune", None)
        self.assertEqual(nl.title, "Duin")

    async def test_cache_never_stores_the_key(self) -> None:
        self.fake.add("/3/search/movie", results(movie(1, "Dune")))
        self.fake.add("/3/movie/1", httpx.Response(200, json=movie(1, "Dune")))
        for key in (V3_KEY, V4_TOKEN):
            forge = self.forge(key, cache=True)
            await forge.search_movie("Dune", 2021)
            await forge.details(MediaKind.MOVIE, 1)
        rows = await self.db.fetch_all("SELECT key, payload FROM tmdb_cache")
        self.assertTrue(rows)
        for key, payload in rows:
            for secret in (V3_KEY, V4_TOKEN):
                self.assertNotIn(secret, key)
                self.assertNotIn(secret, payload)

    async def test_keys_are_mediaforges(self) -> None:
        """A cache brought over from MediaForge (raw TMDB JSON under its keys) is used as is."""
        self.assertEqual(
            cache_key("/search/movie", {"query": "Dune", "year": 2021}, "en-US"),
            "/search/movie?include_adult=false&language=en-US&query=Dune&year=2021",
        )
        await self.cache.put_json(
            "/search/tv?first_air_date_year=2005&include_adult=false&language=en-US"
            "&query=The Office",
            {"page": 1, "results": [{"id": 9, "name": "The Office", "extra": True}]},
        )
        await self.cache.put_json(
            "/tv/9?include_adult=false&language=en-US",
            {"id": 9, "name": "The Office", "first_air_date": "2005-03-24"},
        )
        await self.cache.put_json(
            "/tv/9/season/1?include_adult=false&language=en-US",
            {"season_number": 1, "episodes": [{"episode_number": 1, "name": "Pilot"}]},
        )
        forge = self.forge(cache=True)
        [show] = await forge.search_tv("The Office", 2005)
        details = await forge.details(MediaKind.EPISODE, 9)
        season = await forge.season(9, 1)
        self.assertEqual(show.title, "The Office")
        assert details is not None and season is not None
        self.assertEqual(details.year, 2005)
        self.assertEqual(season.episodes[0].name, "Pilot")
        self.assertEqual(self.fake.seen, [])

    async def test_stale_or_unreadable_entries_are_asked_again(self) -> None:
        self.fake.add("/3/search/movie", results(movie(1, "Dune")))
        self.fake.add("/3/movie/1", httpx.Response(200, json=movie(1, "Dune")))
        search_key = cache_key("/search/movie", {"query": "Dune"}, "en-US")
        details_key = cache_key("/movie/1", {}, "en-US")
        await self.cache.put_json(search_key, {"results": [movie(7, "Old")]})
        await self.cache.put_json(details_key, {"no": "title"})
        async with self.db.transaction() as conn:
            await conn.execute(
                "UPDATE tmdb_cache SET fetched_at = fetched_at - ? WHERE key = ?",
                (JSON_TTL_S + 60, search_key),
            )
        forge = self.forge(cache=True)
        self.assertEqual([r.id for r in await forge.search_movie("Dune", None)], [1])
        film = await forge.details(MediaKind.MOVIE, 1)
        assert film is not None
        self.assertEqual(film.title, "Dune")
        self.assertEqual(len(self.fake.seen), 2)
        # Both entries were stored afresh.
        self.assertEqual([r.id for r in await forge.search_movie("Dune", None)], [1])
        self.assertIsNotNone(await forge.details(MediaKind.MOVIE, 1))
        self.assertEqual(len(self.fake.seen), 2)

    async def test_without_a_cache_every_lookup_asks(self) -> None:
        self.fake.add("/3/search/tv", results())
        forge = self.forge()
        await forge.search_tv("Dark", None)
        await forge.search_tv("Dark", None)
        self.assertEqual(len(self.fake.seen), 2)
        self.assertEqual(await self.db.fetch_all("SELECT key FROM tmdb_cache"), [])


class ModelTests(ForgeTmdbTestCase):
    async def test_result_model_maps_tv_fields(self) -> None:
        result = TMDBTitle.model_validate(
            {"id": 1, "name": "Shogun", "original_name": "SHOGUN", "first_air_date": "2024-02-27"}
        )
        self.assertEqual(
            (result.title, result.original_title, result.year), ("Shogun", "SHOGUN", 2024)
        )
        self.assertIsNone(TMDBTitle.model_validate({"id": 2, "title": "X", "release_date": ""}).year)

    async def test_dump_validates_back(self) -> None:
        """What the cache stores (a dump) reads back as the same title."""
        title = TMDBTitle.model_validate(
            {"id": 1, "name": "Shogun", "first_air_date": "2024-02-27", "overview": None}
        )
        self.assertEqual(TMDBTitle.model_validate(title.model_dump(mode="json")), title)


if __name__ == "__main__":
    unittest.main()
