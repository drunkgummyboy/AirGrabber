"""TMDBClient against recorded payloads served by httpx.MockTransport (no live network)."""

from __future__ import annotations

import asyncio
import copy
import io
import json
import logging
import unittest
from collections.abc import Callable, Coroutine, Iterator
from datetime import date
from pathlib import Path
from typing import Any
from unittest import mock

import httpx

from app.models import DiscoverShow, Movie, MovieDetails
from app.services.tmdb import (
    TMDBAuthError,
    TMDBClient,
    TMDBError,
    TMDBNotConfigured,
    TMDBNotFound,
)

FIXTURES = Path(__file__).parent / "fixtures" / "tmdb"
LOGGER = "app.services.tmdb"
BASE = "https://api.themoviedb.org/3"
V3_KEY = "0123456789abcdef0123456789abcdef"
V4_TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJhdWQiOiJ0ZXN0In0.c2lnbmF0dXJl"

Handler = Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]]


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def ok(payload: Any) -> httpx.Response:
    return httpx.Response(200, json=payload)


def page(*results: Any) -> dict[str, Any]:
    return {"page": 1, "results": list(results), "total_pages": 1, "total_results": len(results)}


def release(kind: int, day: str, certification: str = "") -> dict[str, Any]:
    return {
        "certification": certification,
        "descriptors": [],
        "iso_639_1": "",
        "note": "",
        "release_date": f"{day}T00:00:00.000Z" if day else "",
        "type": kind,
    }


def movie_with_releases(region: str, *releases: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": 1,
        "imdb_id": "tt0000001",
        "runtime": 100,
        "release_dates": {"results": [{"iso_3166_1": region, "release_dates": list(releases)}]},
    }


class FakeTMDB:
    """Answers with the queued replies in order (the last one repeats) and records each request."""

    def __init__(self, *replies: httpx.Response | Exception) -> None:
        self.replies = list(replies)
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


class TMDBTestCase(unittest.IsolatedAsyncioTestCase):
    def client(
        self, handler: Handler, *, key: str | Callable[[], str] = V3_KEY, **options: Any
    ) -> TMDBClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(http.aclose)
        options.setdefault("retry_delay", 0)
        api_key = key if callable(key) else (lambda: key)
        return TMDBClient(http, api_key, **options)

    def assert_request(
        self,
        request: httpx.Request,
        path: str,
        params: dict[str, str],
        *,
        language: str = "en-US",
        key: str = V3_KEY,
    ) -> None:
        self.assertEqual(request.method, "GET")
        self.assertEqual(str(request.url.copy_with(query=None)), BASE + path)
        expected = {**params, "language": language, "api_key": key}
        self.assertEqual(sorted(request.url.params.multi_items()), sorted(expected.items()))


class RequestShapeTests(TMDBTestCase):
    async def test_trending_tv(self) -> None:
        server = FakeTMDB(ok(page()))
        await self.client(server).trending_tv()
        [request] = server.requests
        self.assert_request(request, "/trending/tv/week", {})

    async def test_tv_on_the_air(self) -> None:
        server = FakeTMDB(ok(page()))
        await self.client(server).tv_on_the_air()
        [request] = server.requests
        self.assert_request(request, "/tv/on_the_air", {})

    async def test_tv_premieres(self) -> None:
        server = FakeTMDB(ok(page()))
        await self.client(server).tv_premieres(date(2026, 10, 12), date(2026, 11, 11))
        [request] = server.requests
        self.assert_request(
            request,
            "/discover/tv",
            {
                "sort_by": "popularity.desc",
                "with_original_language": "en",
                "first_air_date.gte": "2026-10-12",
                "first_air_date.lte": "2026-11-11",
            },
        )

    async def test_trending_movies(self) -> None:
        server = FakeTMDB(ok(page()))
        await self.client(server).trending_movies()
        [request] = server.requests
        self.assert_request(request, "/trending/movie/week", {})

    async def test_digital_releases(self) -> None:
        server = FakeTMDB(ok(page()))
        await self.client(server).digital_releases(date(2026, 9, 28), date(2026, 10, 12))
        [request] = server.requests
        self.assert_request(
            request,
            "/discover/movie",
            {
                "sort_by": "popularity.desc",
                "with_release_type": "4",
                "region": "US",
                "release_date.gte": "2026-09-28",
                "release_date.lte": "2026-10-12",
            },
        )

    async def test_digital_releases_page(self) -> None:
        server = FakeTMDB(ok(page()))
        client = self.client(server)
        await client.digital_releases(date(2026, 9, 28), date(2026, 10, 12), page=2)
        [request] = server.requests
        self.assert_request(
            request,
            "/discover/movie",
            {
                "sort_by": "popularity.desc",
                "with_release_type": "4",
                "region": "US",
                "release_date.gte": "2026-09-28",
                "release_date.lte": "2026-10-12",
                "page": "2",
            },
        )
        with self.assertRaises(ValueError):
            await client.digital_releases(date(2026, 9, 28), date(2026, 10, 12), page=0)
        self.assertEqual(len(server.requests), 1)

    async def test_search_movies_strips_query(self) -> None:
        server = FakeTMDB(ok(page()))
        await self.client(server).search_movies("  dune \n")
        [request] = server.requests
        self.assert_request(request, "/search/movie", {"query": "dune", "include_adult": "false"})

    async def test_movie_details(self) -> None:
        server = FakeTMDB(ok({"id": 969681}))
        await self.client(server).movie_details(969681)
        [request] = server.requests
        self.assert_request(
            request, "/movie/969681", {"append_to_response": "release_dates,external_ids"}
        )

    async def test_tv_details(self) -> None:
        server = FakeTMDB(ok({"id": 123542}))
        await self.client(server).tv_details(123542)
        [request] = server.requests
        self.assert_request(request, "/tv/123542", {"append_to_response": "external_ids"})

    async def test_language_and_region_options(self) -> None:
        server = FakeTMDB(ok(page()))
        client = self.client(server, language="nl-NL", region="NL")
        await client.digital_releases(date(2026, 10, 1), date(2026, 10, 2))
        await client.trending_tv()
        self.assert_request(
            server.requests[0],
            "/discover/movie",
            {
                "sort_by": "popularity.desc",
                "with_release_type": "4",
                "region": "NL",
                "release_date.gte": "2026-10-01",
                "release_date.lte": "2026-10-02",
            },
            language="nl-NL",
        )
        self.assert_request(server.requests[1], "/trending/tv/week", {}, language="nl-NL")

    async def test_empty_search_makes_no_request(self) -> None:
        server = FakeTMDB(ok(load("movie_search.json")))
        client = self.client(server)
        self.assertEqual(await client.search_movies(""), [])
        self.assertEqual(await client.search_movies(" \t\n"), [])
        self.assertEqual(server.requests, [])

    async def test_leaves_the_shared_client_open(self) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(FakeTMDB(ok(page())))) as http:
            await TMDBClient(http, lambda: V3_KEY, retry_delay=0).trending_tv()
            self.assertFalse(http.is_closed)

    def test_rejects_zero_concurrency(self) -> None:
        with self.assertRaises(ValueError):
            TMDBClient(mock.Mock(spec=httpx.AsyncClient), lambda: V3_KEY, max_concurrent=0)


class AuthTests(TMDBTestCase):
    async def test_v3_key_goes_in_the_query(self) -> None:
        server = FakeTMDB(ok(page()))
        await self.client(server, key=V3_KEY).trending_tv()
        [request] = server.requests
        self.assertEqual(request.url.params.get_list("api_key"), [V3_KEY])
        self.assertNotIn("Authorization", request.headers)

    async def test_v4_token_goes_in_a_bearer_header(self) -> None:
        server = FakeTMDB(ok(page()))
        await self.client(server, key=V4_TOKEN).trending_tv()
        [request] = server.requests
        self.assertEqual(request.headers["Authorization"], f"Bearer {V4_TOKEN}")
        self.assertNotIn("api_key", request.url.params)
        self.assertEqual(request.url.params["language"], "en-US")

    async def test_key_is_stripped(self) -> None:
        server = FakeTMDB(ok(page()))
        client = self.client(server, key=f"  {V3_KEY}\n")
        await client.trending_movies()
        await self.client(server, key=f"\t{V4_TOKEN} ").trending_movies()
        self.assertEqual(server.requests[0].url.params["api_key"], V3_KEY)
        self.assertEqual(server.requests[1].headers["Authorization"], f"Bearer {V4_TOKEN}")

    async def test_missing_key_raises_before_any_request(self) -> None:
        server = FakeTMDB(ok(page()))
        for key in ("", "   ", "\n\t"):
            client = self.client(server, key=key)
            calls: list[Callable[[], Coroutine[Any, Any, object]]] = [
                client.trending_tv,
                client.tv_on_the_air,
                lambda: client.tv_premieres(date(2026, 1, 1), date(2026, 1, 2)),
                client.trending_movies,
                lambda: client.digital_releases(date(2026, 1, 1), date(2026, 1, 2)),
                lambda: client.search_movies("dune"),
                lambda: client.movie_details(1),
                lambda: client.tv_details(1),
            ]
            for call in calls:
                with self.subTest(key=key, call=call), self.assertRaises(TMDBNotConfigured):
                    await call()
        self.assertEqual(server.requests, [])
        self.assertTrue(issubclass(TMDBNotConfigured, TMDBError))

    async def test_invalid_characters_in_key_raise_before_any_request(self) -> None:
        server = FakeTMDB(ok(page()))
        for key in (f"{V4_TOKEN}​", "abc\tdef", "clé"):
            with self.subTest(key=key), self.assertRaises(TMDBAuthError):
                await self.client(server, key=key).trending_tv()
        self.assertEqual(server.requests, [])

    async def test_key_is_read_on_every_request(self) -> None:
        current = [""]
        server = FakeTMDB(ok(page()))
        client = self.client(server, key=lambda: current[0])
        with self.assertRaises(TMDBNotConfigured):
            await client.trending_tv()
        current[0] = "firstkey"
        await client.trending_tv()
        current[0] = "secondkey"
        await client.trending_tv()
        current[0] = V4_TOKEN
        await client.trending_tv()
        first, second, third = server.requests
        self.assertEqual(first.url.params["api_key"], "firstkey")
        self.assertEqual(second.url.params["api_key"], "secondkey")
        self.assertNotIn("api_key", third.url.params)
        self.assertEqual(third.headers["Authorization"], f"Bearer {V4_TOKEN}")


class ListParsingTests(TMDBTestCase):
    async def test_trending_tv(self) -> None:
        raw = load("tv_trending.json")
        shows = await self.client(FakeTMDB(ok(raw))).trending_tv()
        self.assertEqual([show.id for show in shows], [item["id"] for item in raw["results"]])
        link_click = shows[0]
        self.assertIsInstance(link_click, DiscoverShow)
        self.assertEqual(link_click.id, 123542)
        self.assertEqual(link_click.name, "LINK CLICK")
        self.assertEqual(link_click.original_name, "时光代理人")
        self.assertEqual(link_click.original_language, "zh")
        self.assertEqual(link_click.first_air_date, date(2021, 4, 30))
        self.assertEqual(link_click.year, 2021)
        self.assertEqual(link_click.origin_country, ("CN",))
        self.assertEqual((link_click.vote_average, link_click.vote_count), (8.2, 134))
        self.assertEqual(link_click.popularity, 54.3771)
        self.assertTrue(link_click.overview.startswith("Using superpowers"))
        self.assertEqual(
            link_click.poster_url(),
            "https://image.tmdb.org/t/p/w185/wo9jwu5qMvXzhW2cyy5E6hOVs92.jpg",
        )
        self.assertEqual(
            link_click.poster_url("w342"),
            "https://image.tmdb.org/t/p/w342/wo9jwu5qMvXzhW2cyy5E6hOVs92.jpg",
        )
        self.assertEqual(shows[1].origin_country, ("CA", "US"))

    async def test_tv_on_the_air(self) -> None:
        raw = load("tv_on_the_air.json")
        shows = await self.client(FakeTMDB(ok(raw))).tv_on_the_air()
        self.assertEqual(len(shows), 20)
        wwhl = shows[0]
        self.assertEqual((wwhl.id, wwhl.name), (22980, "Watch What Happens Live with Andy Cohen"))
        self.assertEqual(wwhl.first_air_date, date(2009, 7, 16))
        self.assertEqual(wwhl.origin_country, ("US",))
        self.assertEqual(shows[-1].origin_country, ("DE", "AT", "CH"))

    async def test_tv_premieres(self) -> None:
        raw = load("tv_premieres.json")
        shows = await self.client(FakeTMDB(ok(raw))).tv_premieres(
            date(2026, 10, 12), date(2026, 11, 11)
        )
        self.assertEqual(len(shows), 20)
        vision = shows[0]
        self.assertEqual((vision.id, vision.name), (213375, "VisionQuest"))
        self.assertEqual(vision.first_air_date, date(2026, 10, 14))
        self.assertEqual((vision.vote_average, vision.vote_count), (0.0, 0))
        by_id = {show.id: show for show in shows}
        self.assertIsNone(by_id[270596].poster_path)  # null poster in the payload
        self.assertIsNone(by_id[270596].poster_url())

    async def test_trending_movies(self) -> None:
        raw = load("movie_trending.json")
        movies = await self.client(FakeTMDB(ok(raw))).trending_movies()
        self.assertEqual([movie.id for movie in movies], [item["id"] for item in raw["results"]])
        spider = movies[0]
        self.assertIsInstance(spider, Movie)
        self.assertEqual((spider.id, spider.title), (969681, "Spider-Man: Brand New Day"))
        self.assertEqual(spider.release_date, date(2026, 7, 29))
        self.assertEqual(spider.year, 2026)
        self.assertEqual(spider.genre_ids, (878, 28, 12))
        self.assertEqual(spider.vote_count, 5131)
        self.assertIsNone(spider.imdb_id)  # list items carry no IMDb id
        self.assertEqual(
            spider.poster_url(), "https://image.tmdb.org/t/p/w342/bjiS5ipwxb9JFy3XRRN4OAilSeX.jpg"
        )

    async def test_digital_releases_just_dropped(self) -> None:
        raw = load("movie_just_dropped.json")
        movies = await self.client(FakeTMDB(ok(raw))).digital_releases(
            date(2026, 9, 28), date(2026, 10, 12)
        )
        self.assertEqual(len(movies), 20)
        spider = movies[0]
        self.assertEqual(spider.id, 969681)
        self.assertEqual(spider.release_date, date(2026, 10, 6))  # the US digital date

    async def test_digital_releases_upcoming(self) -> None:
        raw = load("movie_upcoming_digital.json")
        movies = await self.client(FakeTMDB(ok(raw))).digital_releases(
            date(2026, 10, 13), date(2026, 11, 12)
        )
        self.assertEqual(len(movies), 20)
        self.assertEqual((movies[0].id, movies[0].title), (1377237, "Runner"))
        self.assertEqual(movies[0].release_date, date(2026, 10, 13))

    async def test_search_movies(self) -> None:
        raw = load("movie_search.json")
        client = self.client(FakeTMDB(ok(raw)))
        movies = await client.search_movies("dune")
        self.assertEqual(len(movies), 12)  # default limit
        self.assertEqual([movie.id for movie in movies[:3]], [438631, 693134, 841])
        dune = movies[0]
        self.assertEqual((dune.title, dune.release_date), ("Dune", date(2021, 9, 15)))
        self.assertEqual(len(await client.search_movies("dune", limit=3)), 3)
        self.assertEqual(len(await client.search_movies("dune", limit=50)), 20)

    async def test_null_poster_gives_no_url(self) -> None:
        movies = await self.client(FakeTMDB(ok(load("movie_search.json")))).search_movies(
            "dune", limit=20
        )
        no_poster = next(movie for movie in movies if movie.id == 539644)
        self.assertIsNone(no_poster.poster_path)
        self.assertIsNone(no_poster.poster_url())

    async def test_empty_dates_become_none(self) -> None:
        movies = load("movie_search.json")
        movies["results"][0]["release_date"] = ""
        shows = load("tv_premieres.json")
        shows["results"][0]["first_air_date"] = ""
        server = FakeTMDB(ok(movies), ok(shows))
        client = self.client(server)
        [movie, *_] = await client.search_movies("dune")
        [show, *_] = await client.tv_premieres(date(2026, 10, 12), date(2026, 11, 11))
        self.assertIsNone(movie.release_date)
        self.assertIsNone(movie.year)
        self.assertIsNone(show.first_air_date)
        self.assertIsNone(show.year)

    async def test_minimal_items_get_defaults(self) -> None:
        server = FakeTMDB(
            ok(page({"id": 1, "title": "T", "overview": None, "vote_average": None})),
            ok(page({"id": 2, "name": "N", "origin_country": []})),
        )
        client = self.client(server)
        [movie] = await client.trending_movies()
        [show] = await client.trending_tv()
        self.assertEqual((movie.overview, movie.vote_average, movie.genre_ids), ("", 0.0, ()))
        self.assertEqual((show.overview, show.vote_count, show.origin_country), ("", 0, ()))


class DetailsParsingTests(TMDBTestCase):
    async def details(self, payload: Any, region: str = "US") -> MovieDetails:
        return await self.client(FakeTMDB(ok(payload)), region=region).movie_details(1)

    async def test_movie_details_fixture(self) -> None:
        details = await self.details(load("movie_details.json"))
        self.assertEqual(details.id, 969681)
        self.assertEqual(details.imdb_id, "tt22084616")
        self.assertEqual(details.imdb_url, "https://www.imdb.com/title/tt22084616/")
        self.assertEqual(details.runtime, 145)
        self.assertEqual(details.certification, "PG-13")
        self.assertEqual(details.digital_release, date(2026, 10, 6))
        self.assertEqual(details.theatrical_release, date(2026, 7, 31))  # type 3, not the IMAX type 2

    async def test_other_regions(self) -> None:
        raw = load("movie_details.json")
        gb = await self.details(raw, "GB")
        # GB rates the digital release "12" and the theatrical one "12A": digital wins.
        self.assertEqual(
            (gb.certification, gb.digital_release, gb.theatrical_release),
            ("12", date(2026, 10, 6), date(2026, 7, 29)),
        )
        nl = await self.details(raw, "nl")
        self.assertEqual(
            (nl.certification, nl.digital_release, nl.theatrical_release),
            ("12", None, date(2026, 7, 30)),
        )

    async def test_region_without_entry(self) -> None:
        details = await self.details(load("movie_details.json"), "ZZ")
        self.assertEqual(
            (details.certification, details.digital_release, details.theatrical_release),
            (None, None, None),
        )
        self.assertEqual(details.imdb_id, "tt22084616")
        self.assertEqual(details.runtime, 145)

    async def test_without_release_dates(self) -> None:
        details = await self.details({"id": 5, "imdb_id": None, "runtime": 0})
        self.assertEqual(details, MovieDetails(id=5))

    async def test_imdb_id_falls_back_to_external_ids(self) -> None:
        raw = load("movie_details.json")
        raw["imdb_id"] = None
        self.assertEqual((await self.details(raw)).imdb_id, "tt22084616")
        raw["imdb_id"] = ""
        self.assertEqual((await self.details(raw)).imdb_id, "tt22084616")
        raw["external_ids"]["imdb_id"] = None
        details = await self.details(raw)
        self.assertIsNone(details.imdb_id)
        self.assertIsNone(details.imdb_url)

    async def test_earliest_dates_win(self) -> None:
        details = await self.details(
            movie_with_releases(
                "US",
                release(4, "2026-03-10"),
                release(4, "2026-02-01"),
                release(3, "2026-01-20"),
                release(3, "2026-01-05"),
            )
        )
        self.assertEqual(details.digital_release, date(2026, 2, 1))
        self.assertEqual(details.theatrical_release, date(2026, 1, 5))

    async def test_limited_theatrical_is_the_fallback(self) -> None:
        details = await self.details(
            movie_with_releases("US", release(1, "2026-01-01"), release(2, "2026-01-09"))
        )
        self.assertEqual(details.theatrical_release, date(2026, 1, 9))
        self.assertIsNone(details.digital_release)

    async def test_empty_release_date_is_ignored(self) -> None:
        details = await self.details(
            movie_with_releases("US", release(4, ""), release(4, "2026-05-05"))
        )
        self.assertEqual(details.digital_release, date(2026, 5, 5))

    async def test_certification_preference(self) -> None:
        cases = [
            # (releases, expected): digital first, then theatrical, then the first rated one.
            ([release(3, "2026-01-01", "R"), release(4, "2026-02-01", "NR")], "NR"),
            ([release(1, "2026-01-01", "PG"), release(4, "2026-02-01", " ")], "PG"),
            ([release(1, "2026-01-01", "PG"), release(3, "2026-02-01", "R")], "R"),
            ([release(5, "2026-01-01", "G"), release(1, "2026-02-01", "PG")], "G"),
            ([release(3, "2026-01-01"), release(4, "2026-02-01")], None),
        ]
        for releases, expected in cases:
            with self.subTest(expected=expected):
                details = await self.details(movie_with_releases("US", *releases))
                self.assertEqual(details.certification, expected)

    async def test_tv_details_fixture(self) -> None:
        details = await self.client(FakeTMDB(ok(load("tv_details.json")))).tv_details(123542)
        self.assertEqual(details.id, 123542)
        self.assertEqual(details.status, "Returning Series")
        self.assertEqual(details.number_of_seasons, 5)
        self.assertEqual(details.imdb_id, "tt14976292")
        self.assertEqual(details.tvdb_id, 402033)

    async def test_tv_details_without_external_ids(self) -> None:
        details = await self.client(FakeTMDB(ok({"id": 7}))).tv_details(7)
        self.assertEqual((details.imdb_id, details.tvdb_id, details.status), (None, None, None))


class MalformedPayloadTests(TMDBTestCase):
    async def test_malformed_list_items_are_skipped(self) -> None:
        raw = load("movie_trending.json")
        del raw["results"][2]["title"]
        broken_id = raw["results"][2]["id"]
        raw["results"].append("not a movie")
        with self.assertLogs(LOGGER, "WARNING") as logs:
            movies = await self.client(FakeTMDB(ok(raw))).trending_movies()
        self.assertEqual(len(movies), 19)
        self.assertNotIn(broken_id, [movie.id for movie in movies])
        self.assertEqual(len(logs.output), 2)
        self.assertIn(f"id {broken_id}", logs.output[0])
        self.assertIn("title", logs.output[0])
        self.assertIn("item 20", logs.output[1])

    async def test_malformed_tv_item_is_skipped(self) -> None:
        raw = load("tv_on_the_air.json")
        raw["results"][0]["id"] = "not-an-id"
        with self.assertLogs(LOGGER, "WARNING"):
            shows = await self.client(FakeTMDB(ok(raw))).tv_on_the_air()
        self.assertEqual(len(shows), 19)

    async def test_list_endpoint_rejects_wrong_shape(self) -> None:
        for payload in ([1, 2], {"status_code": 34}, {"results": {"id": 1}}, "text"):
            with self.subTest(payload=payload), self.assertRaises(TMDBError):
                await self.client(FakeTMDB(ok(payload))).trending_tv()

    async def test_invalid_details_raise(self) -> None:
        client = self.client(FakeTMDB(ok({"title": "no id"})))
        with self.assertRaises(TMDBError) as caught:
            await client.movie_details(1)
        self.assertIn("unexpected payload (id: Field required)", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)  # its str() would quote the payload
        self.assertIsNone(caught.exception.__context__)
        with self.assertRaises(TMDBError):
            await client.tv_details(1)

    async def test_invalid_json_raises(self) -> None:
        client = self.client(FakeTMDB(httpx.Response(200, text="<html>oops</html>")))
        with self.assertRaises(TMDBError) as caught:
            await client.trending_tv()
        self.assertIn("/trending/tv/week", str(caught.exception))
        self.assertIsNone(caught.exception.__context__)


class HttpBehaviourTests(TMDBTestCase):
    async def test_401_raises_auth_error_at_once(self) -> None:
        server = FakeTMDB(httpx.Response(401, json=load("error_401.json")))
        with self.assertNoLogs(LOGGER), self.assertRaises(TMDBAuthError) as caught:
            await self.client(server).movie_details(1)
        self.assertEqual(len(server.requests), 1)
        self.assertIn("TMDB rejected the API key", str(caught.exception))

    async def test_404_raises_not_found_at_once(self) -> None:
        server = FakeTMDB(httpx.Response(404, json={"status_code": 34, "success": False}))
        with self.assertNoLogs(LOGGER), self.assertRaises(TMDBNotFound) as caught:
            await self.client(server).tv_details(99999999)
        self.assertEqual(len(server.requests), 1)
        self.assertIn("/tv/99999999", str(caught.exception))

    async def test_other_4xx_raises_at_once(self) -> None:
        server = FakeTMDB(httpx.Response(422))
        with self.assertRaises(TMDBError) as caught:
            await self.client(server).trending_tv()
        self.assertNotIsInstance(caught.exception, (TMDBNotFound, TMDBAuthError))
        self.assertIn("422", str(caught.exception))
        self.assertEqual(len(server.requests), 1)

    async def test_429_then_success(self) -> None:
        server = FakeTMDB(httpx.Response(429, headers={"Retry-After": "0"}), ok(page()))
        with self.assertLogs(LOGGER, "WARNING") as logs:
            self.assertEqual(await self.client(server).trending_movies(), [])
        self.assertEqual(len(server.requests), 2)
        [message] = logs.output
        self.assertIn("/trending/movie/week", message)
        self.assertIn("HTTP 429", message)

    async def test_persistent_429_gives_up_after_4_attempts(self) -> None:
        server = FakeTMDB(httpx.Response(429))
        with self.assertLogs(LOGGER, "WARNING") as logs, self.assertRaises(TMDBError) as caught:
            await self.client(server).trending_tv()
        self.assertEqual(len(server.requests), 4)
        self.assertEqual(len(logs.output), 3)
        self.assertIn("429", str(caught.exception))

    async def test_persistent_500_gives_up_after_3_attempts(self) -> None:
        server = FakeTMDB(httpx.Response(500))
        with self.assertLogs(LOGGER, "WARNING") as logs, self.assertRaises(TMDBError) as caught:
            await self.client(server).movie_details(1)
        self.assertNotIsInstance(caught.exception, (TMDBNotFound, TMDBAuthError))
        self.assertEqual(len(server.requests), 3)
        self.assertEqual(len(logs.output), 2)
        self.assertIn("/movie/1", str(caught.exception))
        self.assertIn("500", str(caught.exception))

    async def test_5xx_then_success(self) -> None:
        server = FakeTMDB(httpx.Response(503), ok(page()))
        with self.assertLogs(LOGGER, "WARNING"):
            await self.client(server).tv_on_the_air()
        self.assertEqual(len(server.requests), 2)

    async def test_transport_errors_are_retried(self) -> None:
        server = FakeTMDB(httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), ok(page()))
        with self.assertLogs(LOGGER, "WARNING") as logs:
            await self.client(server).trending_tv()
        self.assertEqual(len(server.requests), 3)
        self.assertIn("ConnectError", logs.output[0])
        self.assertIn("ReadTimeout", logs.output[1])

    async def test_persistent_transport_error_gives_up_after_3_attempts(self) -> None:
        server = FakeTMDB(httpx.ConnectTimeout("timed out"))
        with self.assertLogs(LOGGER, "WARNING"), self.assertRaises(TMDBError) as caught:
            await self.client(server).trending_tv()
        self.assertEqual(len(server.requests), 3)
        self.assertEqual(
            str(caught.exception),
            "TMDB /trending/tv/week: ConnectTimeout (gave up after 3 attempts)",
        )
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    async def test_non_transport_request_error_is_not_retried(self) -> None:
        server = FakeTMDB(httpx.DecodingError("bad gzip"))
        with self.assertRaises(TMDBError) as caught:
            await self.client(server).trending_tv()
        self.assertEqual(len(server.requests), 1)
        self.assertIn("DecodingError", str(caught.exception))
        self.assertIsNone(caught.exception.__context__)


class BackoffTests(TMDBTestCase):
    """Retry waits, recorded by a stand-in for asyncio.sleep instead of really sleeping."""

    async def waits(self, *replies: httpx.Response) -> list[float]:
        delays: list[float] = []
        real_sleep = asyncio.sleep

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)
            await real_sleep(0)

        client = self.client(FakeTMDB(*replies), retry_delay=1.0)
        with mock.patch("asyncio.sleep", fake_sleep), self.assertLogs(LOGGER, "WARNING"):
            try:
                await client.trending_tv()
            except TMDBError:
                pass
        return delays

    async def test_exponential_backoff_for_5xx(self) -> None:
        self.assertEqual(await self.waits(httpx.Response(502)), [1.0, 2.0])

    async def test_exponential_backoff_for_429_without_retry_after(self) -> None:
        self.assertEqual(await self.waits(httpx.Response(429)), [1.0, 2.0, 4.0])

    async def test_numeric_retry_after_is_honoured(self) -> None:
        replies = (httpx.Response(429, headers={"Retry-After": "7"}), ok(page()))
        self.assertEqual(await self.waits(*replies), [7.0])


class ConcurrencyTests(TMDBTestCase):
    async def test_semaphore_caps_in_flight_requests(self) -> None:
        in_flight = peak = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            for _ in range(5):
                await asyncio.sleep(0)
            in_flight -= 1
            return ok({"id": int(request.url.path.rsplit("/", 1)[1])})

        client = self.client(handler, max_concurrent=2)
        details = await asyncio.gather(*(client.tv_details(tv_id) for tv_id in range(1, 7)))
        self.assertEqual([d.id for d in details], [1, 2, 3, 4, 5, 6])
        self.assertEqual(peak, 2)

    async def test_slot_is_free_while_waiting_to_retry(self) -> None:
        paths: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if paths == ["/3/tv/1"]:
                return httpx.Response(503)
            return ok({"id": int(request.url.path.rsplit("/", 1)[1])})

        client = self.client(handler, max_concurrent=1)
        with self.assertLogs(LOGGER, "WARNING"):
            await asyncio.gather(client.tv_details(1), client.tv_details(2))
        self.assertEqual(paths, ["/3/tv/1", "/3/tv/2", "/3/tv/1"])


SECRET = "SECRETKEY123"
SECRET_TOKEN = "eyJSECRETTOKEN456"


def exception_chain(exc: BaseException) -> Iterator[BaseException]:
    """The exception plus everything reachable through __cause__ and __context__."""
    seen: set[int] = set()
    pending: list[BaseException] = [exc]
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        pending.extend(e for e in (current.__cause__, current.__context__) if e is not None)


class SecretHygieneTests(TMDBTestCase):
    """The key must never surface in exception messages (chained ones included) or logs."""

    def setUp(self) -> None:
        # Capture everything that reaches the root logger. httpx's own INFO line ("HTTP Request:
        # GET <full url>") is silenced by app.config.configure_logging; mirror that here.
        self.log_output = io.StringIO()
        handler = logging.StreamHandler(self.log_output)
        handler.setLevel(logging.DEBUG)
        root = logging.getLogger()
        saved_level = root.level
        root.addHandler(handler)
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, saved_level)
        self.addCleanup(root.removeHandler, handler)
        for noisy in ("httpx", "httpcore"):
            noisy_logger = logging.getLogger(noisy)
            self.addCleanup(noisy_logger.setLevel, noisy_logger.level)
            noisy_logger.setLevel(logging.WARNING)

    @staticmethod
    def leaky_transport_error(request: httpx.Request) -> httpx.Response:
        # Worst case: an httpx error whose message embeds the full URL (query included).
        raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

    @staticmethod
    def leaky_redirects_error(request: httpx.Request) -> httpx.Response:
        raise httpx.TooManyRedirects(f"too many redirects for {request.url}", request=request)

    @staticmethod
    def echoing_html(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=f"<html>proxy error for {request.url}</html>")

    async def test_key_never_leaks(self) -> None:
        async def run(handler: Callable[[httpx.Request], httpx.Response], key: str) -> TMDBError:
            async def async_handler(request: httpx.Request) -> httpx.Response:
                return handler(request)

            client = self.client(async_handler, key=key)
            with self.assertRaises(TMDBError) as caught:
                await client.movie_details(1)
            return caught.exception

        scenarios: dict[str, Callable[[httpx.Request], httpx.Response]] = {
            "401": lambda request: httpx.Response(401, json=load("error_401.json")),
            "500 exhausted": lambda request: httpx.Response(500, text=str(request.url)),
            "429 exhausted": lambda request: httpx.Response(429, text=str(request.url)),
            "transport error": self.leaky_transport_error,
            "non-transport request error": self.leaky_redirects_error,
            "invalid JSON": self.echoing_html,
            "unexpected payload": lambda request: ok({"echo": str(request.url)}),
        }
        for key in (SECRET, SECRET_TOKEN):
            for name, handler in scenarios.items():
                with self.subTest(key=key, scenario=name):
                    error = await run(handler, key)
                    for exc in exception_chain(error):
                        self.assertNotIn(key, str(exc))
                        self.assertNotIn(key, repr(exc))
                        self.assertNotIn(key, " ".join(map(str, exc.args)))

        logged = self.log_output.getvalue()
        self.assertIn("/movie/1", logged)  # the retry warnings were captured...
        self.assertNotIn(SECRET, logged)  # ...without the key
        self.assertNotIn(SECRET_TOKEN, logged)

    async def test_malformed_item_warning_has_no_key(self) -> None:
        raw = copy.deepcopy(load("movie_trending.json"))
        raw["results"][0]["title"] = None

        async def handler(request: httpx.Request) -> httpx.Response:
            return ok(raw)

        await self.client(handler, key=SECRET).trending_movies()
        logged = self.log_output.getvalue()
        self.assertIn("skipping malformed", logged)
        self.assertNotIn(SECRET, logged)


if __name__ == "__main__":
    unittest.main()
