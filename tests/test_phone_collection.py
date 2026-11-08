"""`GET /v1/collection/{tmdb_id}`, "In deze reeks": the real TMDBClient over a fake TMDB (and once
the real DiscoveryService's cache). Nothing here reaches a network."""

from __future__ import annotations

import json
import unittest
from datetime import UTC, datetime
from typing import Any
from unittest import mock

import httpx

from app.i18n import t
from app.models import PairedPhone
from app.services.discovery import DiscoveryService
from app.services.library import LibraryService
from app.services.phone.collection import FilmSeries
from app.services.phone.server import Reply, Request, _path_values
from app.services.tmdb import TMDBClient
from app.services.tvmaze import TVMazeClient

KEY = "0123456789abcdef0123456789abcdef"
ROUTE = ("GET", "/v1/collection/{tmdb_id}")
BACK_TO_THE_FUTURE = 264
FAILED = "AirGrabber couldn't look up the films in this series just now, so try again in a moment."
NOW = datetime(2026, 11, 8, 20, 0, tzinfo=UTC)


def part(id: int, title: str, day: str | None, poster: str | None = None) -> dict[str, Any]:
    return {
        "id": id,
        "title": title,
        "release_date": day or "",
        "poster_path": poster if poster is not None else f"/{id}.jpg",
        "overview": "",
    }


SAGA_PARTS = [
    part(165, "Back to the Future Part II", "1989-11-22"),
    part(999, "Back to the Future Part IV", None),
    part(105, "Back to the Future", "1985-07-03"),
    part(196, "Back to the Future Part III", "1990-05-25"),
]
SAGA = {"id": BACK_TO_THE_FUTURE, "name": "Back to the Future Collection", "parts": SAGA_PARTS}


def phone(manager: bool) -> PairedPhone:
    return PairedPhone(
        id="p", name="Phone", token_hash="h", profile_id=1, paired_at=NOW, manager=manager
    )


class FakeTMDB:
    """Answers by path; records each request."""

    def __init__(self, answers: dict[str, httpx.Response]) -> None:
        self.answers = answers
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/3")
        return self.answers.get(path, httpx.Response(404, json={"status_code": 34}))


def in_collection(collection: dict[str, Any] | None) -> dict[str, httpx.Response]:
    answers = {
        "/movie/105": httpx.Response(
            200,
            json={
                "id": 105,
                "title": "Back to the Future",
                "belongs_to_collection": (
                    None
                    if collection is None
                    else {"id": collection["id"], "name": collection["name"]}
                ),
            },
        )
    }
    if collection is not None:
        answers[f"/collection/{collection['id']}"] = httpx.Response(200, json=collection)
    return answers


class CollectionCase(unittest.IsolatedAsyncioTestCase):
    def tmdb(self, server: FakeTMDB, key: str = KEY) -> TMDBClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(server))
        self.addAsyncCleanup(http.aclose)
        return TMDBClient(http, lambda: key, retry_delay=0)

    def series(self, server: FakeTMDB, key: str = KEY) -> FilmSeries:
        return FilmSeries(self.tmdb(server, key).collection_of)

    async def ask(self, series: FilmSeries, id: str, *, manager: bool = True) -> Reply:
        path = f"/v1/collection/{id}"
        query = _path_values(ROUTE[1], path)
        assert query is not None
        reply = await series.routes()[ROUTE](Request("GET", path, query, {}, phone(manager)))
        json.dumps(reply.body)  # what the server sends
        return reply


class CollectionTests(CollectionCase):
    async def test_a_film_in_a_collection_lists_its_parts_in_order(self) -> None:
        server = FakeTMDB(in_collection(SAGA))
        reply = await self.ask(self.series(server), "105")
        self.assertEqual(reply.status, 200)
        self.assertEqual(
            reply.body["parts"],
            [
                {
                    "tmdb_id": 105,
                    "title": "Back to the Future",
                    "year": 1985,
                    "poster_url": "https://image.tmdb.org/t/p/w342/105.jpg",
                },
                {
                    "tmdb_id": 165,
                    "title": "Back to the Future Part II",
                    "year": 1989,
                    "poster_url": "https://image.tmdb.org/t/p/w342/165.jpg",
                },
                {
                    "tmdb_id": 196,
                    "title": "Back to the Future Part III",
                    "year": 1990,
                    "poster_url": "https://image.tmdb.org/t/p/w342/196.jpg",
                },
                {
                    "tmdb_id": 999,
                    "title": "Back to the Future Part IV",
                    "year": None,
                    "poster_url": "https://image.tmdb.org/t/p/w342/999.jpg",
                },
            ],
        )

    async def test_a_film_in_none_gives_an_empty_list(self) -> None:
        server = FakeTMDB(in_collection(None))
        reply = await self.ask(self.series(server), "105")
        self.assertEqual((reply.status, reply.body), (200, {"parts": []}))
        self.assertEqual([request.url.path for request in server.requests], ["/3/movie/105"])

    async def test_a_film_tmdb_does_not_know_gives_an_empty_list(self) -> None:
        reply = await self.ask(self.series(FakeTMDB({})), "424242")
        self.assertEqual((reply.status, reply.body), (200, {"parts": []}))

    async def test_the_tmdb_key_is_not_in_the_answer(self) -> None:
        sneaky = {
            **SAGA,
            "parts": [
                part(105, "Back to the Future", "1985-07-03", poster=f"/105.jpg?api_key={KEY}"),
                part(165, "Back to the Future Part II", "1989-11-22", poster=""),
            ],
        }
        server = FakeTMDB(in_collection(sneaky))
        reply = await self.ask(self.series(server), "105")
        self.assertEqual(reply.status, 200)
        self.assertTrue(server.requests)  # the key did travel to TMDB, as a parameter
        self.assertNotIn(KEY, json.dumps(reply.body))
        self.assertEqual([one["poster_url"] for one in reply.body["parts"]], [None, None])

    async def test_a_viewer_phone_may_ask(self) -> None:
        server = FakeTMDB(in_collection(SAGA))
        reply = await self.ask(self.series(server), "105", manager=False)
        self.assertEqual(reply.status, 200)
        self.assertEqual(len(reply.body["parts"]), 4)

    async def test_tmdb_out_of_reach_gives_a_plain_sentence_and_logs_nothing(self) -> None:
        server = FakeTMDB({"/movie/105": httpx.Response(503)})
        with (
            self.assertNoLogs("app.services.phone", level="DEBUG"),
            self.assertLogs("app.services.tmdb", level="WARNING"),  # its retries, by path only
        ):
            reply = await self.ask(self.series(server), "105")
        self.assertEqual(reply.status, 502)
        self.assertEqual(reply.body, {"error": "failed", "message": t(FAILED)})
        self.assertNotIn(KEY, json.dumps(reply.body))

    async def test_a_malformed_part_is_left_out(self) -> None:
        answers = in_collection({**SAGA, "parts": [{"id": 500}, *SAGA_PARTS]})
        with self.assertLogs("app.services.tmdb", level="WARNING"):
            reply = await self.ask(self.series(FakeTMDB(answers)), "105")
        self.assertEqual([one["tmdb_id"] for one in reply.body["parts"]], [105, 165, 196, 999])

    async def test_a_broken_collection_gives_a_plain_sentence(self) -> None:
        answers = in_collection(SAGA)
        answers[f"/collection/{BACK_TO_THE_FUTURE}"] = httpx.Response(200, json={"id": 1})
        reply = await self.ask(self.series(FakeTMDB(answers)), "105")
        self.assertEqual((reply.status, reply.body["error"]), (502, "failed"))

    async def test_without_a_key_it_says_so_plainly_and_asks_nothing(self) -> None:
        server = FakeTMDB(in_collection(SAGA))
        reply = await self.ask(self.series(server, key=""), "105")
        self.assertEqual((reply.status, reply.body["error"]), (502, "failed"))
        self.assertEqual(server.requests, [])

    async def test_an_id_that_is_not_a_film_id_is_a_bad_request(self) -> None:
        server = FakeTMDB(in_collection(SAGA))
        series = self.series(server)
        for id in ("abc", "0", "-5", "1.5", "１０５", "99999999999", "105%3Fx%3D1"):
            with self.subTest(id=id):
                reply = await self.ask(series, id)
                self.assertEqual((reply.status, reply.body["error"]), (400, "bad_request"))
        self.assertEqual(server.requests, [])


class CacheTests(CollectionCase):
    async def test_the_app_keeps_a_series_a_while(self) -> None:
        server = FakeTMDB(in_collection(SAGA))
        discovery = DiscoveryService(
            self.tmdb(server), mock.Mock(spec=TVMazeClient), mock.Mock(spec=LibraryService)
        )
        series = FilmSeries(discovery.film_series)
        first = await self.ask(series, "105")
        again = await self.ask(series, "105")
        self.assertEqual(first, again)
        self.assertEqual(len(server.requests), 2)  # the film, then its collection: once
        discovery.clear_cache()
        await self.ask(series, "105")
        self.assertEqual(len(server.requests), 4)
