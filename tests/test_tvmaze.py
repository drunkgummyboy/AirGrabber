"""TVMazeClient against recorded payloads served by httpx.MockTransport (no live network)."""

from __future__ import annotations

import asyncio
import json
import unittest
from collections.abc import Callable, Coroutine
from datetime import UTC, date, datetime, time
from pathlib import Path
from typing import Any
from unittest import mock

import httpx
from pydantic import ValidationError

from app.services.tvmaze import TVMazeClient, TVMazeError, TVMazeNotFound

FIXTURES = Path(__file__).parent / "fixtures" / "tvmaze"
LOGGER = "app.services.tvmaze"

Handler = Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]]


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def ok(payload: Any) -> httpx.Response:
    return httpx.Response(200, json=payload)


def minimal_show(show_id: int) -> dict[str, Any]:
    return {"id": show_id, "name": f"Show {show_id}"}


class FakeTVMaze:
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


class TVMazeTestCase(unittest.IsolatedAsyncioTestCase):
    def client(self, handler: Handler, **options: Any) -> TVMazeClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(http.aclose)
        options.setdefault("retry_delay", 0)
        return TVMazeClient(http, **options)

    def assert_request(
        self, request: httpx.Request, path: str, params: list[tuple[str, str]]
    ) -> None:
        self.assertEqual(request.method, "GET")
        self.assertEqual(str(request.url.copy_with(query=None)), f"https://api.tvmaze.com{path}")
        self.assertEqual(request.url.params.multi_items(), params)


class RequestShapeTests(TVMazeTestCase):
    async def test_search_strips_query(self) -> None:
        server = FakeTVMaze(ok(load("search.json")))
        await self.client(server).search_shows("  severance \n")
        [request] = server.requests
        self.assert_request(request, "/search/shows", [("q", "severance")])

    async def test_get_show(self) -> None:
        server = FakeTVMaze(ok(minimal_show(42846)))
        await self.client(server).get_show(42846)
        [request] = server.requests
        self.assert_request(request, "/shows/42846", [])

    async def test_get_show_with_episodes_embeds_episodes(self) -> None:
        server = FakeTVMaze(ok(load("show_embed.json")))
        await self.client(server).get_show_with_episodes(42846)
        [request] = server.requests
        self.assert_request(request, "/shows/42846", [("embed[]", "episodes")])

    async def test_schedule(self) -> None:
        server = FakeTVMaze(ok([]))
        client = self.client(server)
        await client.schedule(date(2026, 10, 12))
        await client.schedule(date(2026, 1, 2), country="GB")
        self.assert_request(
            server.requests[0], "/schedule", [("country", "US"), ("date", "2026-10-12")]
        )
        self.assert_request(
            server.requests[1], "/schedule", [("country", "GB"), ("date", "2026-01-02")]
        )

    async def test_updated_shows(self) -> None:
        server = FakeTVMaze(ok({}))
        await self.client(server).updated_shows("week")
        [request] = server.requests
        self.assert_request(request, "/updates/shows", [("since", "week")])

    async def test_empty_query_makes_no_request(self) -> None:
        server = FakeTVMaze(ok(load("search.json")))
        client = self.client(server)
        self.assertEqual(await client.search_shows(""), [])
        self.assertEqual(await client.search_shows("   "), [])
        self.assertEqual(server.requests, [])

    async def test_leaves_the_shared_client_open(self) -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(FakeTVMaze(ok({})))) as http:
            await TVMazeClient(http, retry_delay=0).updated_shows("day")
            self.assertFalse(http.is_closed)


class ParsingTests(TVMazeTestCase):
    async def test_search_keeps_api_order(self) -> None:
        raw = load("search.json")
        shows = await self.client(FakeTVMaze(ok(raw))).search_shows("severance")
        self.assertEqual([show.id for show in shows], [44933, 85532, 70294])
        severance = shows[0]
        self.assertEqual(severance.name, "Severance")
        self.assertEqual(severance.network, "Apple TV")  # a webChannel, not a network
        self.assertEqual(severance.imdb_id, "tt11280740")
        self.assertEqual(severance.genres, ("Drama", "Science-Fiction", "Mystery"))
        self.assertEqual(severance.premiered, date(2022, 2, 18))
        self.assertEqual(shows[2].name, raw[2]["show"]["name"])
        self.assertEqual(shows[2].network, "France 2")

    async def test_search_applies_limit(self) -> None:
        client = self.client(FakeTVMaze(ok(load("search.json"))))
        shows = await client.search_shows("severance", limit=2)
        self.assertEqual([show.id for show in shows], [44933, 85532])

    async def test_get_show(self) -> None:
        show = await self.client(FakeTVMaze(ok(load("show_embed.json")))).get_show(42846)
        self.assertEqual(show.id, 42846)
        self.assertEqual(show.name, "Dune: Prophecy")
        self.assertEqual(show.network, "HBO")
        self.assertEqual(show.runtime, 68)  # averageRuntime wins over runtime
        self.assertEqual(show.updated, 1765814630)
        self.assertNotIn("<p>", show.summary)

    async def test_get_show_with_episodes(self) -> None:
        raw = load("show_embed.json")
        client = self.client(FakeTVMaze(ok(raw)))
        show, episodes = await client.get_show_with_episodes(42846)
        self.assertEqual(show.name, "Dune: Prophecy")
        self.assertEqual(
            [episode.id for episode in episodes], [ep["id"] for ep in raw["_embedded"]["episodes"]]
        )
        self.assertTrue(all(episode.show_id == 42846 for episode in episodes))
        first = episodes[0]
        self.assertEqual(first.code, "S01E01")
        self.assertEqual(first.name, "The Hidden Hand")
        self.assertEqual(first.air_date, date(2024, 11, 17))
        self.assertEqual(first.air_time, time(21, 0))
        self.assertEqual(first.airstamp, datetime(2024, 11, 18, 2, 0, tzinfo=UTC))
        self.assertEqual(first.rating, 7.8)
        self.assertTrue(first.summary.startswith("On Wallach IX"))

    async def test_get_show_with_many_episodes(self) -> None:
        raw = load("web_show.json")
        client = self.client(FakeTVMaze(ok(raw)))
        show, episodes = await client.get_show_with_episodes(44778)
        self.assertEqual((show.id, show.name), (44778, "House of the Dragon"))
        self.assertEqual(len(episodes), len(raw["_embedded"]["episodes"]))
        self.assertEqual(episodes[0].name, "The Heirs of the Dragon")
        self.assertEqual({episode.show_id for episode in episodes}, {44778})

    async def test_schedule(self) -> None:
        raw = load("schedule.json")
        entries = await self.client(FakeTVMaze(ok(raw))).schedule(date(2026, 10, 12))
        self.assertEqual(len(entries), len(raw))
        show, episode = entries[0]
        self.assertEqual((show.id, show.name, show.network), (15308, "Way Too Early", "MS NOW"))
        self.assertEqual((episode.id, episode.show_id), (3774529, 15308))
        self.assertEqual((episode.season, episode.number), (2026, 198))
        self.assertEqual(episode.air_date, date(2026, 10, 12))
        self.assertEqual(episode.airstamp, datetime(2026, 10, 12, 9, 0, tzinfo=UTC))
        self.assertTrue(all(show.id == episode.show_id for show, episode in entries))

    async def test_updated_shows_converts_keys(self) -> None:
        payload = {"1": 1700000000, "42846": 1765814630}
        updates = await self.client(FakeTVMaze(ok(payload))).updated_shows("day")
        self.assertEqual(updates, {1: 1700000000, 42846: 1765814630})


class LookupTests(TVMazeTestCase):
    """/lookup/shows: a hit is a 301 to the show, a miss a 404."""

    def redirecting_client(self, handler: Handler) -> TVMazeClient:
        # Like the app's shared client, which follows redirects.
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
        self.addAsyncCleanup(http.aclose)
        return TVMazeClient(http, retry_delay=0)

    async def test_hit_by_imdb_follows_the_redirect(self) -> None:
        requests: list[httpx.Request] = []
        raw = load("lookup_imdb.json")

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/lookup/shows":
                return httpx.Response(
                    301, headers={"Location": "https://api.tvmaze.com/shows/55050"}
                )
            return ok(raw)

        show = await self.redirecting_client(handler).lookup_show(imdb="tt14976292")
        assert show is not None
        self.assertEqual((show.id, show.name), (55050, "LINK CLICK"))
        self.assertEqual(show.imdb_id, "tt14976292")
        self.assertEqual(show.premiered, date(2021, 4, 30))
        first, second = requests
        self.assert_request(first, "/lookup/shows", [("imdb", "tt14976292")])
        self.assert_request(second, "/shows/55050", [])

    async def test_hit_by_thetvdb(self) -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.url.path == "/lookup/shows":
                return httpx.Response(
                    301, headers={"Location": "https://api.tvmaze.com/shows/55050"}
                )
            return ok(load("lookup_imdb.json"))

        show = await self.redirecting_client(handler).lookup_show(thetvdb=402033)
        assert show is not None
        self.assertEqual(show.id, 55050)
        self.assert_request(requests[0], "/lookup/shows", [("thetvdb", "402033")])

    async def test_miss_returns_none(self) -> None:
        server = FakeTVMaze(httpx.Response(404, json={"name": "Not Found", "status": 404}))
        client = self.redirecting_client(server)
        with self.assertNoLogs(LOGGER):
            self.assertIsNone(await client.lookup_show(imdb="tt0000000"))
            self.assertIsNone(await client.lookup_show(thetvdb=1))
        self.assertEqual(len(server.requests), 2)

    async def test_other_errors_propagate(self) -> None:
        server = FakeTVMaze(httpx.Response(400))
        with self.assertRaises(TVMazeError) as caught:
            await self.redirecting_client(server).lookup_show(imdb="tt14976292")
        self.assertNotIsInstance(caught.exception, TVMazeNotFound)

    async def test_needs_exactly_one_id(self) -> None:
        server = FakeTVMaze(ok(load("lookup_imdb.json")))
        client = self.redirecting_client(server)
        with self.assertRaises(ValueError):
            await client.lookup_show()
        with self.assertRaises(ValueError):
            await client.lookup_show(imdb="tt14976292", thetvdb=402033)
        self.assertEqual(server.requests, [])


class MalformedPayloadTests(TVMazeTestCase):
    async def test_malformed_search_hit_is_skipped(self) -> None:
        raw = load("search.json")
        raw.insert(1, {"score": 0.5, "show": {"id": 999}})  # no name
        client = self.client(FakeTVMaze(ok(raw)))
        with self.assertLogs(LOGGER, "WARNING") as logs:
            shows = await client.search_shows("severance")
        self.assertEqual([show.id for show in shows], [44933, 85532, 70294])
        [message] = logs.output
        self.assertIn("show 999", message)

    async def test_malformed_schedule_items_are_skipped(self) -> None:
        raw = load("schedule.json")
        broken_id = raw[3]["id"]
        del raw[3]["season"]
        raw.append("not an episode")
        client = self.client(FakeTVMaze(ok(raw)))
        with self.assertLogs(LOGGER, "WARNING") as logs:
            entries = await client.schedule(date(2026, 10, 12))
        self.assertEqual(len(entries), len(raw) - 2)
        self.assertNotIn(broken_id, [episode.id for _, episode in entries])
        self.assertEqual(len(logs.output), 2)
        self.assertIn(f"episode {broken_id}", logs.output[0])

    async def test_list_endpoint_rejects_non_list(self) -> None:
        client = self.client(FakeTVMaze(ok({"name": "Not Found"})))
        with self.assertRaises(TVMazeError):
            await client.search_shows("severance")
        with self.assertRaises(TVMazeError):
            await client.schedule(date(2026, 10, 12))

    async def test_invalid_show_raises(self) -> None:
        client = self.client(FakeTVMaze(ok({"id": 1})))
        with self.assertRaises(TVMazeError) as caught:
            await client.get_show(1)
        self.assertIsInstance(caught.exception.__cause__, ValidationError)
        with self.assertRaises(TVMazeError):
            await client.get_show_with_episodes(1)

    async def test_updated_shows_rejects_non_object(self) -> None:
        client = self.client(FakeTVMaze(ok([1, 2, 3])))
        with self.assertRaises(TVMazeError):
            await client.updated_shows("month")

    async def test_invalid_json_raises(self) -> None:
        client = self.client(FakeTVMaze(httpx.Response(200, text="<html>oops</html>")))
        with self.assertRaises(TVMazeError) as caught:
            await client.get_show(1)
        self.assertIsInstance(caught.exception.__cause__, ValueError)


class HttpBehaviourTests(TVMazeTestCase):
    async def test_404_raises_not_found_at_once(self) -> None:
        server = FakeTVMaze(httpx.Response(404))
        with self.assertNoLogs(LOGGER), self.assertRaises(TVMazeNotFound):
            await self.client(server).get_show(1)
        self.assertEqual(len(server.requests), 1)

    async def test_other_4xx_raises_at_once(self) -> None:
        server = FakeTVMaze(httpx.Response(400))
        with self.assertRaises(TVMazeError) as caught:
            await self.client(server).get_show(1)
        self.assertNotIsInstance(caught.exception, TVMazeNotFound)
        self.assertEqual(len(server.requests), 1)

    async def test_429_then_success(self) -> None:
        server = FakeTVMaze(
            httpx.Response(429, headers={"Retry-After": "0"}), ok(minimal_show(1))
        )
        with self.assertLogs(LOGGER, "WARNING") as logs:
            show = await self.client(server).get_show(1)
        self.assertEqual(show.id, 1)
        self.assertEqual(len(server.requests), 2)
        [message] = logs.output
        self.assertIn("/shows/1", message)
        self.assertIn("HTTP 429", message)

    async def test_persistent_429_gives_up_after_4_attempts(self) -> None:
        server = FakeTVMaze(httpx.Response(429))
        with self.assertLogs(LOGGER, "WARNING") as logs, self.assertRaises(TVMazeError) as caught:
            await self.client(server).get_show(1)
        self.assertEqual(len(server.requests), 4)
        self.assertEqual(len(logs.output), 3)
        self.assertIn("429", str(caught.exception))

    async def test_persistent_500_gives_up_after_3_attempts(self) -> None:
        server = FakeTVMaze(httpx.Response(500))
        with self.assertLogs(LOGGER, "WARNING") as logs, self.assertRaises(TVMazeError) as caught:
            await self.client(server).get_show(1)
        self.assertNotIsInstance(caught.exception, TVMazeNotFound)
        self.assertEqual(len(server.requests), 3)
        self.assertEqual(len(logs.output), 2)
        self.assertIn("/shows/1", str(caught.exception))
        self.assertIn("500", str(caught.exception))

    async def test_5xx_then_success(self) -> None:
        server = FakeTVMaze(httpx.Response(503), ok(minimal_show(1)))
        with self.assertLogs(LOGGER, "WARNING"):
            await self.client(server).get_show(1)
        self.assertEqual(len(server.requests), 2)

    async def test_transport_errors_are_retried(self) -> None:
        server = FakeTVMaze(
            httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), ok(minimal_show(1))
        )
        with self.assertLogs(LOGGER, "WARNING") as logs:
            await self.client(server).get_show(1)
        self.assertEqual(len(server.requests), 3)
        self.assertIn("ConnectError", logs.output[0])
        self.assertIn("ReadTimeout", logs.output[1])

    async def test_persistent_transport_error_gives_up_after_3_attempts(self) -> None:
        server = FakeTVMaze(httpx.ConnectTimeout("timed out"))
        with self.assertLogs(LOGGER, "WARNING"), self.assertRaises(TVMazeError) as caught:
            await self.client(server).get_show(1)
        self.assertEqual(len(server.requests), 3)
        self.assertIsInstance(caught.exception.__cause__, httpx.ConnectTimeout)


class BackoffTests(TVMazeTestCase):
    """Retry waits, recorded by a stand-in for asyncio.sleep instead of really sleeping."""

    async def waits(self, *replies: httpx.Response) -> list[float]:
        delays: list[float] = []
        real_sleep = asyncio.sleep

        async def fake_sleep(delay: float) -> None:
            delays.append(delay)
            await real_sleep(0)

        client = self.client(FakeTVMaze(*replies), retry_delay=1.0)
        with mock.patch("asyncio.sleep", fake_sleep), self.assertLogs(LOGGER, "WARNING"):
            try:
                await client.get_show(1)
            except TVMazeError:
                pass
        return delays

    async def test_exponential_backoff_for_5xx(self) -> None:
        self.assertEqual(await self.waits(httpx.Response(502)), [1.0, 2.0])

    async def test_exponential_backoff_for_429_without_retry_after(self) -> None:
        self.assertEqual(await self.waits(httpx.Response(429)), [1.0, 2.0, 4.0])

    async def test_numeric_retry_after_is_honoured(self) -> None:
        replies = (httpx.Response(429, headers={"Retry-After": "7"}), ok(minimal_show(1)))
        self.assertEqual(await self.waits(*replies), [7.0])

    async def test_http_date_retry_after_falls_back_to_backoff(self) -> None:
        retry_after = {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}
        replies = (httpx.Response(429, headers=retry_after), ok(minimal_show(1)))
        self.assertEqual(await self.waits(*replies), [1.0])


class ConcurrencyTests(TVMazeTestCase):
    async def test_semaphore_caps_in_flight_requests(self) -> None:
        in_flight = peak = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            for _ in range(5):
                await asyncio.sleep(0)
            in_flight -= 1
            return ok(minimal_show(int(request.url.path.rsplit("/", 1)[1])))

        client = self.client(handler, max_concurrent=2)
        shows = await asyncio.gather(*(client.get_show(show_id) for show_id in range(1, 7)))
        self.assertEqual([show.id for show in shows], [1, 2, 3, 4, 5, 6])
        self.assertEqual(peak, 2)

    async def test_slot_is_free_while_waiting_to_retry(self) -> None:
        paths: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            paths.append(request.url.path)
            if paths == ["/shows/1"]:
                return httpx.Response(503)
            return ok(minimal_show(int(request.url.path.rsplit("/", 1)[1])))

        client = self.client(handler, max_concurrent=1)
        with self.assertLogs(LOGGER, "WARNING"):
            await asyncio.gather(client.get_show(1), client.get_show(2))
        # Show 2 used the only slot while show 1 waited to retry.
        self.assertEqual(paths, ["/shows/1", "/shows/2", "/shows/1"])

    async def test_cancellation_propagates_and_frees_the_slot(self) -> None:
        entered = asyncio.Event()

        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/shows/1":
                entered.set()
                await asyncio.Event().wait()  # never answers
            return ok(minimal_show(2))

        client = self.client(handler, max_concurrent=1)
        task = asyncio.create_task(client.get_show(1))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual((await client.get_show(2)).id, 2)


if __name__ == "__main__":
    unittest.main()
