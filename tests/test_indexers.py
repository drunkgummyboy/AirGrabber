"""Torrent indexers against recorded payloads served by httpx.MockTransport (no live network)."""

from __future__ import annotations

import asyncio
import copy
import io
import json
import logging
import unittest
from collections.abc import Callable, Coroutine, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TypeVar
from unittest import mock

import httpx

from app.indexers import (
    USER_AGENT,
    ApibayIndexer,
    BaseIndexer,
    BitsearchIndexer,
    EztvIndexer,
    IndexerError,
    ProwlarrIndexer,
    YtsIndexer,
    default_indexers,
)
from app.models import Resolution, SearchKind, SearchRequest, TorrentRelease

FIXTURES = Path(__file__).parent / "fixtures" / "indexers"
LOGGER = "app.indexers"

Handler = Callable[[httpx.Request], Coroutine[Any, Any, httpx.Response]]
IndexerT = TypeVar("IndexerT", bound=BaseIndexer)

EPISODE = SearchRequest(
    kind=SearchKind.EPISODE,
    title="Severance",
    year=2022,
    season=2,
    episode=1,
    imdb_id="tt11280740",
    queries=("Severance S02E01",),
)
SEASON = SearchRequest(
    kind=SearchKind.SEASON,
    title="Severance",
    season=2,
    imdb_id="tt11280740",
    queries=("Severance S02", "Severance Season 2"),
)
MOVIE = SearchRequest(
    kind=SearchKind.MOVIE,
    title="The Shawshank Redemption",
    year=1994,
    imdb_id="tt0111161",
    queries=("The Shawshank Redemption 1994",),
)

PROWLARR_URL = "http://localhost:9696"
PROWLARR_KEY = "PRWLKEY123"
FIXTURE_PROWLARR_KEY = "8c1f0e2a9b3d4c5e6f708192a3b4c5d6"  # the key embedded in the fixture


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def ok(payload: Any) -> httpx.Response:
    return httpx.Response(200, json=payload)


def with_queries(request: SearchRequest, *queries: str) -> SearchRequest:
    return request.model_copy(update={"queries": queries})


def unix(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, UTC)


class FakeServer:
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

    def params(self, name: str) -> list[str | None]:
        return [request.url.params.get(name) for request in self.requests]


class IndexerTestCase(unittest.IsolatedAsyncioTestCase):
    def http(self, handler: Handler) -> httpx.AsyncClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(http.aclose)
        return http

    def make(self, cls: type[IndexerT], handler: Handler, **options: Any) -> IndexerT:
        options.setdefault("retry_delay", 0)
        return cls(self.http(handler), **options)

    def prowlarr(
        self,
        handler: Handler,
        *,
        url: str = PROWLARR_URL,
        key: str = PROWLARR_KEY,
        **options: Any,
    ) -> ProwlarrIndexer:
        options.setdefault("retry_delay", 0)
        return ProwlarrIndexer(self.http(handler), url=lambda: url, api_key=lambda: key, **options)

    def assert_get(
        self, request: httpx.Request, url: str, params: list[tuple[str, str]]
    ) -> None:
        self.assertEqual(request.method, "GET")
        self.assertEqual(str(request.url.copy_with(query=None)), url)
        self.assertEqual(request.url.params.multi_items(), params)
        self.assertEqual(request.headers["User-Agent"], USER_AGENT)


# --- contract -------------------------------------------------------------------------------


class ContractTests(IndexerTestCase):
    async def test_default_indexers(self) -> None:
        indexers = default_indexers(
            self.http(FakeServer(ok([]))), prowlarr_url=lambda: "", prowlarr_api_key=lambda: ""
        )
        self.assertEqual(
            [type(indexer) for indexer in indexers],
            [ApibayIndexer, EztvIndexer, BitsearchIndexer, YtsIndexer, ProwlarrIndexer],
        )
        self.assertEqual(
            [indexer.name for indexer in indexers],
            ["apibay", "eztv", "bitsearch", "yts", "prowlarr"],
        )
        self.assertEqual(
            [indexer.label for indexer in indexers],
            ["APIBay", "EZTV", "Bitsearch", "YTS", "Prowlarr"],
        )

    async def test_supports(self) -> None:
        server = FakeServer(ok([]))
        no_imdb_episode = EPISODE.model_copy(update={"imdb_id": None})
        no_imdb_season = SEASON.model_copy(update={"imdb_id": None})
        cases: list[tuple[BaseIndexer, list[bool]]] = [
            (self.make(ApibayIndexer, server), [True, True, True, True]),
            (self.make(BitsearchIndexer, server), [True, True, True, True]),
            (self.make(EztvIndexer, server), [True, True, False, False]),
            (self.make(YtsIndexer, server), [False, False, True, False]),
            (self.prowlarr(server, url="", key=""), [False, False, False, False]),
            (self.prowlarr(server), [True, True, True, True]),
        ]
        for indexer, expected in cases:
            with self.subTest(indexer=indexer.name):
                supported = [
                    indexer.supports(request)
                    for request in (EPISODE, SEASON, MOVIE, no_imdb_episode)
                ]
                self.assertEqual(supported, expected)
        eztv = self.make(EztvIndexer, server)
        self.assertFalse(eztv.supports(no_imdb_season))
        self.assertFalse(eztv.supports(EPISODE.model_copy(update={"episode": None})))
        self.assertFalse(eztv.supports(SEASON.model_copy(update={"season": None})))

    async def test_unsupported_requests_make_no_request(self) -> None:
        server = FakeServer(ok([]))
        self.assertEqual(await self.make(EztvIndexer, server).search(MOVIE), [])
        no_imdb = EPISODE.model_copy(update={"imdb_id": None})
        self.assertEqual(await self.make(EztvIndexer, server).search(no_imdb), [])
        self.assertEqual(await self.make(YtsIndexer, server).search(EPISODE), [])
        self.assertEqual(await self.prowlarr(server, url="", key="").search(EPISODE), [])
        self.assertEqual(server.requests, [])

    async def test_without_queries_text_indexers_make_no_request(self) -> None:
        server = FakeServer(ok([]))
        for cls in (ApibayIndexer, BitsearchIndexer):
            self.assertEqual(await self.make(cls, server).search(with_queries(EPISODE)), [])
        no_imdb_movie = with_queries(MOVIE.model_copy(update={"imdb_id": None}))
        self.assertEqual(await self.make(YtsIndexer, server).search(no_imdb_movie), [])
        self.assertEqual(await self.prowlarr(server).search(with_queries(EPISODE, " ", "")), [])
        self.assertEqual(server.requests, [])

    async def test_rejects_bad_concurrency(self) -> None:
        with self.assertRaises(ValueError):
            ApibayIndexer(mock.Mock(spec=httpx.AsyncClient), max_concurrent=0)

    async def test_leaves_the_shared_client_open(self) -> None:
        handler = FakeServer(ok(load("apibay_none.json")))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await ApibayIndexer(http, retry_delay=0).search(EPISODE)
            self.assertFalse(http.is_closed)

    async def test_web_search_urls(self) -> None:
        server = FakeServer(ok([]))
        query = "Severance S02E01"
        expected: list[tuple[BaseIndexer, str]] = [
            (
                self.make(ApibayIndexer, server),
                "https://thepiratebay.org/search.php?q=Severance+S02E01",
            ),
            (self.make(EztvIndexer, server), "https://eztvx.to/search/Severance%20S02E01"),
            (
                self.make(BitsearchIndexer, server),
                "https://bitsearch.eu/search?q=Severance+S02E01",
            ),
            (
                self.make(YtsIndexer, server),
                "https://yts.gg/browse-movies/Severance%20S02E01/all/all/0/latest/0/all",
            ),
        ]
        for indexer, url in expected:
            self.assertEqual(indexer.web_search_url(query), url)
        self.assertEqual(
            self.prowlarr(server, url="http://nas:9696/").web_search_url(query),
            "http://nas:9696/search",
        )
        self.assertIsNone(self.prowlarr(server, url="").web_search_url(query))


# --- shared HTTP behaviour (exercised through APIBay) -------------------------------------


class HttpBehaviourTests(IndexerTestCase):
    async def search(self, server: FakeServer, **options: Any) -> list[TorrentRelease]:
        return await self.make(ApibayIndexer, server, **options).search(EPISODE)

    async def test_429_then_success(self) -> None:
        server = FakeServer(
            httpx.Response(429, headers={"Retry-After": "0"}), ok(load("apibay_episode.json"))
        )
        with self.assertLogs(LOGGER, "WARNING") as logs:
            releases = await self.search(server)
        self.assertEqual(len(releases), 15)
        self.assertEqual(len(server.requests), 2)
        [message] = logs.output
        self.assertIn("APIBay: /q.php answered HTTP 429", message)
        self.assertNotIn("https://", message)

    async def test_persistent_429_gives_up_after_4_attempts(self) -> None:
        server = FakeServer(httpx.Response(429))
        with self.assertLogs(LOGGER, "WARNING") as logs, self.assertRaises(IndexerError) as caught:
            await self.search(server)
        self.assertEqual(len(server.requests), 4)
        self.assertEqual(len(logs.output), 3)
        self.assertIn("429", str(caught.exception))

    async def test_persistent_500_gives_up_after_3_attempts(self) -> None:
        server = FakeServer(httpx.Response(500))
        with self.assertLogs(LOGGER, "WARNING") as logs, self.assertRaises(IndexerError) as caught:
            await self.search(server)
        self.assertEqual(len(server.requests), 3)
        self.assertEqual(len(logs.output), 2)
        self.assertEqual(
            str(caught.exception), "APIBay: /q.php answered HTTP 500; gave up after 3 attempts"
        )

    async def test_5xx_then_success(self) -> None:
        server = FakeServer(httpx.Response(503), ok(load("apibay_none.json")))
        with self.assertLogs(LOGGER, "WARNING"):
            self.assertEqual(await self.search(server), [])
        self.assertEqual(len(server.requests), 2)

    async def test_transport_errors_are_retried(self) -> None:
        server = FakeServer(
            httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), ok(load("apibay_none.json"))
        )
        with self.assertLogs(LOGGER, "WARNING") as logs:
            await self.search(server)
        self.assertEqual(len(server.requests), 3)
        self.assertIn("ConnectError", logs.output[0])
        self.assertIn("ReadTimeout", logs.output[1])

    async def test_persistent_transport_error_gives_up_after_3_attempts(self) -> None:
        server = FakeServer(httpx.ConnectTimeout("timed out"))
        with self.assertLogs(LOGGER, "WARNING"), self.assertRaises(IndexerError) as caught:
            await self.search(server)
        self.assertEqual(len(server.requests), 3)
        self.assertEqual(
            str(caught.exception),
            "APIBay: no response from /q.php (ConnectTimeout); gave up after 3 attempts",
        )
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    async def test_other_4xx_raises_at_once(self) -> None:
        server = FakeServer(httpx.Response(403))
        with self.assertNoLogs(LOGGER), self.assertRaises(IndexerError) as caught:
            await self.search(server)
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(str(caught.exception), "APIBay: /q.php answered HTTP 403")

    async def test_non_transport_request_error_is_not_retried(self) -> None:
        server = FakeServer(httpx.DecodingError("bad gzip"))
        with self.assertRaises(IndexerError) as caught:
            await self.search(server)
        self.assertEqual(len(server.requests), 1)
        self.assertIn("DecodingError", str(caught.exception))
        self.assertIsNone(caught.exception.__context__)

    async def test_invalid_json_raises(self) -> None:
        server = FakeServer(httpx.Response(200, text="<html>Cloudflare says no</html>"))
        with self.assertRaises(IndexerError) as caught:
            await self.search(server)
        self.assertEqual(str(caught.exception), "APIBay: /q.php did not answer with valid JSON")
        self.assertIsNone(caught.exception.__context__)

    async def test_wrong_shape_raises(self) -> None:
        for payload in ({"error": "maintenance"}, "text", 42):
            with self.subTest(payload=payload), self.assertRaises(IndexerError) as caught:
                await self.search(FakeServer(ok(payload)))
            self.assertTrue(str(caught.exception).startswith("APIBay: unexpected answer"))

    async def test_malformed_items_are_skipped(self) -> None:
        raw = load("apibay_episode.json")
        del raw[2]["name"]
        raw[3]["seeders"] = "lots"
        raw[4]["info_hash"] = "not-a-hash"
        raw.append("not a torrent")
        with self.assertLogs(LOGGER, "WARNING") as logs:
            releases = await self.search(FakeServer(ok(raw)))
        self.assertEqual(len(releases), 12)
        self.assertEqual(len(logs.output), 4)
        self.assertIn("skipping malformed item 2 from /q.php: name: Field required", logs.output[0])
        self.assertIn("item 3", logs.output[1])
        self.assertIn("seeders", logs.output[1])
        self.assertIn("item 15", logs.output[3])

    async def test_cancellation_propagates(self) -> None:
        started = asyncio.Event()

        async def hang(request: httpx.Request) -> httpx.Response:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("never answered")

        task = asyncio.create_task(self.make(ApibayIndexer, hang).search(EPISODE))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_semaphore_caps_in_flight_requests(self) -> None:
        in_flight = peak = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            for _ in range(5):
                await asyncio.sleep(0)
            in_flight -= 1
            return ok(load("apibay_none.json"))

        indexer = self.make(ApibayIndexer, handler, max_concurrent=2)
        await asyncio.gather(*(indexer.search(EPISODE) for _ in range(6)))
        self.assertEqual(peak, 2)

    async def test_backoff(self) -> None:
        async def waits(*replies: httpx.Response) -> list[float]:
            delays: list[float] = []
            real_sleep = asyncio.sleep

            async def fake_sleep(delay: float) -> None:
                delays.append(delay)
                await real_sleep(0)

            indexer = self.make(ApibayIndexer, FakeServer(*replies), retry_delay=1.0)
            with mock.patch("asyncio.sleep", fake_sleep), self.assertLogs(LOGGER, "WARNING"):
                try:
                    await indexer.search(EPISODE)
                except IndexerError:
                    pass
            return delays

        self.assertEqual(await waits(httpx.Response(502)), [1.0, 2.0])
        self.assertEqual(await waits(httpx.Response(429)), [1.0, 2.0, 4.0])
        retry_after = httpx.Response(429, headers={"Retry-After": "7"})
        self.assertEqual(await waits(retry_after, ok(load("apibay_none.json"))), [7.0])


# --- APIBay -----------------------------------------------------------------------------------


class ApibayTests(IndexerTestCase):
    async def test_request(self) -> None:
        server = FakeServer(ok(load("apibay_episode.json")))
        await self.make(ApibayIndexer, server).search(EPISODE)
        [request] = server.requests
        self.assert_get(request, "https://apibay.org/q.php", [("q", "Severance S02E01")])

    async def test_parses_fixture(self) -> None:
        server = FakeServer(ok(load("apibay_episode.json")))
        releases = await self.make(ApibayIndexer, server).search(EPISODE)
        self.assertEqual(len(releases), 15)
        first = releases[0]
        self.assertEqual(first.title, "Severance S02E01 1080p WEB H264-SuccessfulCrab")
        self.assertEqual(first.source, "apibay")
        self.assertEqual(first.info_hash, "a762217ea6a1d6e64fa297259dc14ab53f75d6c7")
        self.assertEqual(first.size_bytes, 3963487862)
        self.assertEqual((first.seeders, first.leechers), (162, 8))
        self.assertEqual(first.published_at, datetime(2025, 1, 17, 2, 35, 1, tzinfo=UTC))
        self.assertEqual(first.info_url, "https://thepiratebay.org/description.php?id=77879319")
        self.assertIsNone(first.download_url)
        self.assertEqual(first.resolution, Resolution.FULL_HD)
        assert first.magnet is not None
        self.assertTrue(first.magnet.startswith(f"magnet:?xt=urn:btih:{first.info_hash}&dn="))

    async def test_no_results(self) -> None:
        server = FakeServer(ok(load("apibay_none.json")))
        self.assertEqual(await self.make(ApibayIndexer, server).search(SEASON), [])
        self.assertEqual(server.params("q"), ["Severance S02", "Severance Season 2"])

    async def test_query_fall_through_stops_at_first_hit(self) -> None:
        server = FakeServer(
            ok(load("apibay_none.json")), ok(load("apibay_episode.json")), ok([])
        )
        request = with_queries(EPISODE, "Severance 2x01", "Severance S02E01", "Severance")
        releases = await self.make(ApibayIndexer, server).search(request)
        self.assertEqual(len(releases), 15)
        self.assertEqual(server.params("q"), ["Severance 2x01", "Severance S02E01"])

    async def test_first_hit_ends_the_search(self) -> None:
        server = FakeServer(ok(load("apibay_episode.json")))
        request = with_queries(EPISODE, "Severance S02E01", "Severance 2x01")
        await self.make(ApibayIndexer, server).search(request)
        self.assertEqual(server.params("q"), ["Severance S02E01"])

    async def test_category_filter_and_zero_seeds(self) -> None:
        raw = load("apibay_episode.json")
        raw[0]["category"] = "101"  # Audio > Music
        raw[1]["category"] = "301"  # Applications
        raw[2]["seeders"] = "0"
        releases = await self.make(ApibayIndexer, FakeServer(ok(raw))).search(EPISODE)
        self.assertEqual(len(releases), 12)
        titles = {release.title for release in releases}
        for item in raw[:3]:
            self.assertNotIn(item["name"], titles)
        self.assertTrue(all(release.seeders > 0 for release in releases))

    async def test_query_with_only_unwanted_hits_falls_through(self) -> None:
        unwanted = load("apibay_episode.json")[:2]
        for item in unwanted:
            item["category"] = "601"  # E-books
        server = FakeServer(ok(unwanted), ok(load("apibay_episode.json")))
        request = with_queries(EPISODE, "Severance S02E01", "Severance 2x01")
        self.assertEqual(len(await self.make(ApibayIndexer, server).search(request)), 15)
        self.assertEqual(len(server.requests), 2)


# --- EZTV -------------------------------------------------------------------------------------


def eztv_torrent(
    n: int, *, season: int = 2, episode: int = 1, seeds: int = 10, title: str | None = None
) -> dict[str, Any]:
    info_hash = f"{n:040x}"
    return {
        "id": n,
        "hash": info_hash,
        "title": title or f"Show S{season:02d}E{episode:02d} 1080p WEB-{n} EZTV",
        "magnet_url": f"magnet:?xt=urn:btih:{info_hash}&dn=show",
        "seeds": seeds,
        "peers": 1,
        "size_bytes": "1000",
        "season": str(season),
        "episode": str(episode),
        "date_released_unix": 1700000000,
    }


def eztv_page(torrents: list[dict[str, Any]], page: int = 1) -> dict[str, Any]:
    return {"imdb_id": "11280740", "torrents_count": 999, "limit": 100, "page": page,
            "torrents": torrents}


class EztvTests(IndexerTestCase):
    async def test_request(self) -> None:
        server = FakeServer(ok(load("eztv_show.json")))
        await self.make(EztvIndexer, server).search(EPISODE)
        [request] = server.requests  # the fixture's 30 torrents are fewer than a full page
        self.assert_get(
            request,
            "https://eztvx.to/api/get-torrents",
            [("imdb_id", "11280740"), ("limit", "100"), ("page", "1")],
        )

    async def test_imdb_id_keeps_leading_zeros(self) -> None:
        server = FakeServer(ok(load("eztv_none.json")))
        request = EPISODE.model_copy(update={"imdb_id": "tt0944947"})
        await self.make(EztvIndexer, server).search(request)
        self.assertEqual(server.params("imdb_id"), ["0944947"])

    async def test_episode_filter(self) -> None:
        raw = load("eztv_show.json")
        request = EPISODE.model_copy(update={"episode": 9})
        releases = await self.make(EztvIndexer, FakeServer(ok(raw))).search(request)
        self.assertEqual(len(releases), 6)
        self.assertTrue(all("S02E09" in release.title for release in releases))
        best = max(releases, key=lambda release: release.seeders)
        [item] = [item for item in raw["torrents"] if item["seeds"] == 327]
        self.assertEqual(best.title, item["title"])
        self.assertEqual(best.source, "eztv")
        self.assertEqual(best.info_hash, item["hash"].lower())
        self.assertEqual(best.magnet_uri, item["magnet_url"])
        self.assertEqual(best.size_bytes, int(item["size_bytes"]))
        self.assertEqual((best.seeders, best.leechers), (327, item["peers"]))
        self.assertEqual(best.published_at, unix(item["date_released_unix"]))
        self.assertIsNone(best.download_url)

    async def test_season_packs(self) -> None:
        releases = await self.make(EztvIndexer, FakeServer(ok(load("eztv_show.json")))).search(
            SEASON
        )
        self.assertEqual(
            [release.title for release in releases],
            ["Severance S02 1080p x265-ELiTE EZTV", "Severance S02 720p x265-TiPEX EZTV"],
        )

    async def test_season_pack_by_title_and_other_seasons(self) -> None:
        torrents = [
            eztv_torrent(1, episode=0, title="Show S02 1080p WEB EZTV"),
            eztv_torrent(2, episode=5, title="Show S02 COMPLETE 720p WEB EZTV"),
            eztv_torrent(3, episode=1, title="Show Full Season 2 480p EZTV"),
            eztv_torrent(4, season=1, episode=0, title="Show S01 1080p WEB EZTV"),
            eztv_torrent(5, episode=3),
        ]
        releases = await self.make(EztvIndexer, FakeServer(ok(eztv_page(torrents)))).search(SEASON)
        self.assertEqual(
            [release.title for release in releases],
            ["Show S02 1080p WEB EZTV", "Show S02 COMPLETE 720p WEB EZTV",
             "Show Full Season 2 480p EZTV"],
        )

    async def test_zero_seeds_and_torrent_urls(self) -> None:
        torrents = [eztv_torrent(1, seeds=0), eztv_torrent(2), eztv_torrent(3), eztv_torrent(4)]
        torrents[1]["torrent_url"] = "https://zoink.ch/torrent/show.torrent"
        torrents[2]["torrent_url"] = None
        torrents[3]["torrent_url"] = "ftp://example.org/show.torrent"
        releases = await self.make(EztvIndexer, FakeServer(ok(eztv_page(torrents)))).search(EPISODE)
        self.assertEqual(
            [release.download_url for release in releases],
            ["https://zoink.ch/torrent/show.torrent", None, None],
        )

    async def test_paging_stops_at_a_short_page(self) -> None:
        pages = [
            eztv_page([eztv_torrent(n) for n in range(1, 101)], 1),
            eztv_page([eztv_torrent(n, episode=2) for n in range(101, 201)], 2),
            eztv_page([eztv_torrent(n) for n in range(201, 208)], 3),
        ]
        server = FakeServer(*(ok(page) for page in pages), ok(eztv_page([], 4)))
        releases = await self.make(EztvIndexer, server).search(EPISODE)
        self.assertEqual(server.params("page"), ["1", "2", "3"])
        self.assertEqual(len(releases), 107)

    async def test_paging_stops_after_5_pages(self) -> None:
        server = FakeServer(ok(eztv_page([eztv_torrent(n) for n in range(1, 101)])))
        releases = await self.make(EztvIndexer, server).search(EPISODE)
        self.assertEqual(server.params("page"), ["1", "2", "3", "4", "5"])
        self.assertEqual(len(releases), 500)

    async def test_page_without_torrents_ends_paging(self) -> None:
        full = eztv_page([eztv_torrent(n) for n in range(1, 101)])
        server = FakeServer(ok(full), ok(load("eztv_none.json")))
        releases = await self.make(EztvIndexer, server).search(EPISODE)
        self.assertEqual(len(releases), 100)
        self.assertEqual(len(server.requests), 2)

    async def test_no_results(self) -> None:
        server = FakeServer(ok(load("eztv_none.json")))
        self.assertEqual(await self.make(EztvIndexer, server).search(EPISODE), [])
        self.assertEqual(len(server.requests), 1)


# --- Bitsearch --------------------------------------------------------------------------------


class BitsearchTests(IndexerTestCase):
    async def test_request(self) -> None:
        server = FakeServer(ok(load("bitsearch_search.json")))
        await self.make(BitsearchIndexer, server).search(EPISODE)
        [request] = server.requests
        self.assert_get(request, "https://bitsearch.eu/api/v1/search", [("q", "Severance S02E01")])

    async def test_parses_fixture(self) -> None:
        server = FakeServer(ok(load("bitsearch_search.json")))
        releases = await self.make(BitsearchIndexer, server).search(EPISODE)
        self.assertEqual(len(releases), 15)
        first = releases[0]
        self.assertEqual(
            first.title, "severance.s02e01.1080p.web.h264-successfulcrab[EZTVx.to].mkv"
        )
        self.assertEqual(first.source, "bitsearch")
        self.assertEqual(first.info_hash, "a762217ea6a1d6e64fa297259dc14ab53f75d6c7")
        self.assertEqual(first.size_bytes, 3963487862)
        self.assertEqual((first.seeders, first.leechers), (2980, 1631))
        self.assertEqual(
            first.published_at, datetime(2026, 10, 12, 6, 30, 3, 938000, tzinfo=UTC)
        )
        self.assertIsNone(first.magnet_uri)
        assert first.magnet is not None
        self.assertTrue(first.magnet.startswith(f"magnet:?xt=urn:btih:{first.info_hash}"))
        self.assertIn(
            "Severance.S02E01.Hello.Ms.Cobel.720p.ATVP.WEB-DL.DDPA5.1.H.264-NTb",
            [release.title for release in releases],  # trailing space stripped
        )

    async def test_no_results_falls_through_and_stops_at_first_hit(self) -> None:
        server = FakeServer(
            ok(load("bitsearch_none.json")), ok(load("bitsearch_search.json")), ok({})
        )
        request = with_queries(SEASON, "Severance S02", "Severance Season 2", "Severance")
        releases = await self.make(BitsearchIndexer, server).search(request)
        self.assertEqual(len(releases), 15)
        self.assertEqual(server.params("q"), ["Severance S02", "Severance Season 2"])

    async def test_no_results(self) -> None:
        server = FakeServer(ok(load("bitsearch_none.json")))
        self.assertEqual(await self.make(BitsearchIndexer, server).search(SEASON), [])
        self.assertEqual(len(server.requests), 2)

    async def test_zero_seeds_dropped(self) -> None:
        raw = load("bitsearch_search.json")
        raw["results"][0]["seeders"] = 0
        releases = await self.make(BitsearchIndexer, FakeServer(ok(raw))).search(EPISODE)
        self.assertEqual(len(releases), 14)
        self.assertNotIn(raw["results"][0]["title"], [release.title for release in releases])

    async def test_reported_failure_raises(self) -> None:
        server = FakeServer(ok({"success": False, "results": []}))
        with self.assertRaises(IndexerError) as caught:
            await self.make(BitsearchIndexer, server).search(EPISODE)
        self.assertTrue(str(caught.exception).startswith("Bitsearch: "))


# --- YTS --------------------------------------------------------------------------------------


def yts_movies(*movies: dict[str, Any]) -> dict[str, Any]:
    return {"status": "ok", "data": {"movie_count": len(movies), "movies": list(movies)}}


class YtsTests(IndexerTestCase):
    async def test_request_by_imdb_id(self) -> None:
        server = FakeServer(ok(load("yts_movie.json")))
        await self.make(YtsIndexer, server).search(MOVIE)
        [request] = server.requests
        self.assert_get(
            request,
            "https://movies-api.accel.li/api/v2/list_movies.json",
            [("query_term", "tt0111161")],
        )

    async def test_parses_fixture(self) -> None:
        server = FakeServer(ok(load("yts_movie.json")))
        releases = await self.make(YtsIndexer, server).search(MOVIE)
        self.assertEqual(
            [release.title for release in releases],
            [
                "The Shawshank Redemption (1994) [720p] [bluray] [x264] [YTS]",
                "The Shawshank Redemption (1994) [1080p] [bluray] [x264] [YTS]",
                "The Shawshank Redemption (1994) [2160p] [bluray] [x265] [YTS]",
            ],
        )
        self.assertEqual(
            [release.resolution for release in releases],
            [Resolution.HD, Resolution.FULL_HD, Resolution.UHD],
        )
        self.assertEqual([release.is_hevc for release in releases], [False, False, True])
        first = releases[0]
        self.assertEqual(first.source, "yts")
        self.assertEqual(first.info_hash, "ac418db33fa5cea4fab11bc58008fe08f291c9be")
        self.assertEqual(
            first.download_url,
            "https://yts.gg/torrent/download/AC418DB33FA5CEA4FAB11BC58008FE08F291C9BE",
        )
        self.assertEqual(first.size_bytes, 890199081)
        self.assertEqual((first.seeders, first.leechers), (100, 43))
        self.assertEqual(first.published_at, unix(1446344423))
        self.assertEqual(first.info_url, "https://yts.gg/movies/the-shawshank-redemption-1994")

    async def test_no_results(self) -> None:
        server = FakeServer(ok(load("yts_none.json")))
        self.assertEqual(await self.make(YtsIndexer, server).search(MOVIE), [])
        self.assertEqual(len(server.requests), 1)

    async def test_zero_seeds_dropped(self) -> None:
        raw = load("yts_movie.json")
        raw["data"]["movies"][0]["torrents"][1]["seeds"] = 0
        releases = await self.make(YtsIndexer, FakeServer(ok(raw))).search(MOVIE)
        self.assertEqual(len(releases), 2)

    async def fallback(self, first_base_reply: httpx.Response | Exception) -> None:
        hosts: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            hosts.append(request.url.host)
            if request.url.host == "movies-api.accel.li":
                if isinstance(first_base_reply, Exception):
                    raise first_base_reply
                return first_base_reply
            self.assertEqual(
                str(request.url.copy_with(query=None)), "https://yts.gg/api/v2/list_movies.json"
            )
            return ok(load("yts_movie.json"))

        with self.assertLogs(LOGGER, "WARNING") as logs:
            releases = await self.make(YtsIndexer, handler).search(MOVIE)
        self.assertEqual(len(releases), 3)
        self.assertEqual(hosts, ["movies-api.accel.li", "yts.gg"])  # one try, then fall back
        self.assertIn("trying the next YTS API base", logs.output[-1])

    async def test_falls_back_on_5xx(self) -> None:
        await self.fallback(httpx.Response(503))

    async def test_falls_back_on_transport_error(self) -> None:
        await self.fallback(httpx.ConnectError("no route"))

    async def test_falls_back_on_non_json(self) -> None:
        await self.fallback(httpx.Response(200, text="<html>moved</html>"))

    async def test_falls_back_on_unexpected_payload(self) -> None:
        await self.fallback(ok({"status": "error", "status_message": "gone"}))

    async def test_last_base_gets_full_retries_then_raises(self) -> None:
        server = FakeServer(httpx.Response(503))
        with self.assertLogs(LOGGER, "WARNING"), self.assertRaises(IndexerError) as caught:
            await self.make(YtsIndexer, server).search(MOVIE)
        self.assertEqual(
            [request.url.host for request in server.requests],
            ["movies-api.accel.li", "yts.gg", "yts.gg", "yts.gg"],
        )
        self.assertTrue(str(caught.exception).startswith("YTS: "))

    async def test_text_query_matches_year(self) -> None:
        shawshank = load("yts_movie.json")["data"]["movies"][0]
        other = copy.deepcopy(shawshank)
        other.update(title="Shawshank Again", title_long="Shawshank Again (2004)", year=2004)
        server = FakeServer(ok(yts_movies(other, shawshank)))
        request = MOVIE.model_copy(update={"imdb_id": None})
        releases = await self.make(YtsIndexer, server).search(request)
        self.assertEqual(server.params("query_term"), ["The Shawshank Redemption 1994"])
        self.assertEqual(len(releases), 3)
        self.assertTrue(all(release.title.startswith("The Shawshank") for release in releases))

        any_year = request.model_copy(update={"year": None})
        releases = await self.make(YtsIndexer, server).search(any_year)
        self.assertEqual(len(releases), 6)

    async def test_imdb_query_skips_other_films(self) -> None:
        shawshank = load("yts_movie.json")["data"]["movies"][0]
        other = copy.deepcopy(shawshank)
        other.update(imdb_code="tt9999999", title_long="Other (1994)")
        server = FakeServer(ok(yts_movies(other, shawshank)))
        releases = await self.make(YtsIndexer, server).search(MOVIE)
        self.assertEqual(len(releases), 3)
        self.assertTrue(all(release.title.startswith("The Shawshank") for release in releases))


# --- Prowlarr ---------------------------------------------------------------------------------


STATUS = {
    "appName": "Prowlarr",
    "instanceName": "Prowlarr",
    "version": "1.24.3.4754",
    "buildTime": "2024-09-20T13:34:25Z",
    "isDebug": False,
    "isProduction": True,
    "authentication": "forms",
}


class ProwlarrTests(IndexerTestCase):
    async def test_supports_reads_settings_on_every_call(self) -> None:
        settings = {"url": "", "key": ""}
        indexer = ProwlarrIndexer(
            self.http(FakeServer(ok([]))),
            url=lambda: settings["url"],
            api_key=lambda: settings["key"],
        )
        self.assertFalse(indexer.supports(EPISODE))
        settings["url"] = PROWLARR_URL
        self.assertFalse(indexer.supports(EPISODE))
        settings["key"] = "   "
        self.assertFalse(indexer.supports(EPISODE))
        settings["key"] = PROWLARR_KEY
        self.assertTrue(indexer.supports(EPISODE))
        settings["url"] = ""
        self.assertFalse(indexer.supports(MOVIE))

    async def test_request(self) -> None:
        server = FakeServer(ok(load("prowlarr_search.json")))
        await self.prowlarr(server).search(EPISODE)
        [request] = server.requests
        self.assert_get(
            request,
            "http://localhost:9696/api/v1/search",
            [("query", "Severance S02E01"), ("type", "search"), ("categories", "5000"),
             ("limit", "100")],
        )
        self.assertEqual(request.headers["X-Api-Key"], PROWLARR_KEY)
        self.assertNotIn(PROWLARR_KEY, str(request.url))

    async def test_movie_category(self) -> None:
        server = FakeServer(ok([]))
        await self.prowlarr(server).search(MOVIE)
        self.assertEqual(server.params("categories"), ["2000"])

    async def test_url_is_normalized(self) -> None:
        server = FakeServer(ok([]))
        await self.prowlarr(server, url=" nas.local:9696/prowlarr/ ").search(SEASON)
        self.assertEqual(
            str(server.requests[0].url.copy_with(query=None)),
            "http://nas.local:9696/prowlarr/api/v1/search",
        )

    async def test_parses_fixture(self) -> None:
        server = FakeServer(ok(load("prowlarr_search.json")))
        releases = await self.prowlarr(server).search(EPISODE)
        # The usenet hit and the unseeded torrent are dropped.
        self.assertEqual(
            [release.source for release in releases],
            ["prowlarr:1337x", "prowlarr:The Pirate Bay", "prowlarr:TorrentLeech"],
        )
        full, magnet_only, download_only = releases
        self.assertEqual(full.title, "Severance S02E01 1080p WEB H264-SuccessfulCrab")
        self.assertEqual(full.info_hash, "a762217ea6a1d6e64fa297259dc14ab53f75d6c7")
        self.assertEqual(full.size_bytes, 3963487862)
        self.assertEqual((full.seeders, full.leechers), (1203, 87))
        self.assertEqual(full.published_at, datetime(2025, 1, 17, 2, 35, 1, tzinfo=UTC))
        assert full.magnet_uri is not None and full.download_url is not None
        self.assertTrue(full.magnet_uri.startswith("magnet:?xt=urn:btih:A762217E"))
        self.assertTrue(full.download_url.startswith("http://localhost:9696/4/download?"))
        self.assertEqual(full.info_url, "https://1337x.to/torrent/6276713/"
                         "Severance-S02E01-1080p-WEB-H264-SuccessfulCrab/")

        self.assertIsNone(magnet_only.download_url)
        self.assertEqual(magnet_only.info_hash, "a7e0fd95ca0824783f3dfd05f095cf033db3eda4")
        self.assertTrue(magnet_only.is_hevc)

        self.assertIsNone(download_only.magnet_uri)
        self.assertIsNone(download_only.info_hash)
        self.assertIsNone(download_only.magnet)
        assert download_only.download_url is not None
        self.assertTrue(download_only.download_url.startswith("http://localhost:9696/9/download?"))
        self.assertEqual(download_only.resolution, Resolution.UHD)

    async def test_magnet_handed_out_as_download_url(self) -> None:
        raw = load("prowlarr_search.json")[3]
        raw["downloadUrl"] = "magnet:?xt=urn:btih:7904FF59D2A3DFF190B79DFA9D1BD5DF91A29E2A"
        [release] = await self.prowlarr(FakeServer(ok([raw]))).search(EPISODE)
        self.assertEqual(release.magnet_uri, raw["downloadUrl"])
        self.assertEqual(release.info_hash, "7904ff59d2a3dff190b79dfa9d1bd5df91a29e2a")
        self.assertIsNone(release.download_url)

    async def test_tries_only_the_first_two_queries(self) -> None:
        server = FakeServer(ok([]), ok([]), ok(load("prowlarr_search.json")))
        request = with_queries(EPISODE, "a", "b", "c")
        self.assertEqual(await self.prowlarr(server).search(request), [])
        self.assertEqual(server.params("query"), ["a", "b"])

    async def test_second_query_used_when_first_is_empty(self) -> None:
        server = FakeServer(ok([]), ok(load("prowlarr_search.json")))
        request = with_queries(EPISODE, "a", "b")
        self.assertEqual(len(await self.prowlarr(server).search(request)), 3)
        self.assertEqual(server.params("query"), ["a", "b"])

    async def test_401_rejects_the_key_at_once(self) -> None:
        server = FakeServer(httpx.Response(401))
        with self.assertNoLogs(LOGGER), self.assertRaises(IndexerError) as caught:
            await self.prowlarr(server).search(EPISODE)
        self.assertEqual(str(caught.exception), "Prowlarr rejected the API key")
        self.assertEqual(len(server.requests), 1)

    async def test_key_with_invalid_characters(self) -> None:
        server = FakeServer(ok([]))
        with self.assertRaises(IndexerError) as caught:
            await self.prowlarr(server, key=f"{PROWLARR_KEY}​").search(EPISODE)
        self.assertIn("invalid characters", str(caught.exception))
        self.assertEqual(server.requests, [])

    async def test_searches_wait_longer_than_the_client_default(self) -> None:
        # Prowlarr answers once every site it asks has answered; its status call stays quick.
        server = FakeServer(ok([]), ok(STATUS))
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(server), timeout=httpx.Timeout(10.0, connect=5.0)
        )
        self.addAsyncCleanup(http.aclose)
        indexer = ProwlarrIndexer(http, url=lambda: PROWLARR_URL, api_key=lambda: PROWLARR_KEY)
        await indexer.search(EPISODE)
        await indexer.test()
        search, status = server.requests
        self.assertEqual(
            search.extensions["timeout"], {"connect": 5.0, "read": 30.0, "write": 30.0, "pool": 30.0}
        )
        self.assertEqual(
            status.extensions["timeout"], {"connect": 5.0, "read": 10.0, "write": 10.0, "pool": 10.0}
        )
        self.assertEqual(ProwlarrIndexer.search_timeout, 40.0)
        self.assertIsNone(BaseIndexer.search_timeout)
        self.assertTrue(
            all(cls.search_timeout is None for cls in (ApibayIndexer, EztvIndexer, YtsIndexer))
        )

    async def test_test_reports_the_version(self) -> None:
        server = FakeServer(ok(STATUS))
        self.assertEqual(await self.prowlarr(server).test(), "Prowlarr 1.24.3.4754")
        [request] = server.requests
        self.assert_get(request, "http://localhost:9696/api/v1/system/status", [])
        self.assertEqual(request.headers["X-Api-Key"], PROWLARR_KEY)

    async def test_test_failures(self) -> None:
        cases: dict[str, tuple[httpx.Response, str]] = {
            "bad key": (httpx.Response(401), "Prowlarr rejected the API key"),
            "wrong path": (httpx.Response(404, text="Not Found"), "check the URL"),
            "not prowlarr": (ok({"appName": "Sonarr", "version": "4.0.9"}), "check the URL"),
            "no version": (ok({"appName": "Prowlarr"}), "check the URL"),
            "html": (httpx.Response(200, text="<html></html>"), "not answer with valid JSON"),
        }
        for name, (reply, expected) in cases.items():
            with self.subTest(name), self.assertRaises(IndexerError) as caught:
                await self.prowlarr(FakeServer(reply)).test()
            self.assertIn(expected, str(caught.exception))

    async def test_test_unconfigured_makes_no_request(self) -> None:
        server = FakeServer(ok(STATUS))
        for url, key in (("", PROWLARR_KEY), (PROWLARR_URL, ""), ("  ", "  ")):
            with self.subTest(url=url, key=key), self.assertRaises(IndexerError) as caught:
                await self.prowlarr(server, url=url, key=key).test()
            self.assertIn("not set up", str(caught.exception))
        self.assertEqual(server.requests, [])


# --- secrets ----------------------------------------------------------------------------------


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


class SecretHygieneTests(IndexerTestCase):
    """The Prowlarr key must never surface in exception messages (causes included) or logs."""

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
    def echo(request: httpx.Request) -> str:
        """Everything a careless server or proxy could echo back: the URL and the key header."""
        return f"{request.url} {request.headers.get('X-Api-Key')}"

    async def test_key_never_leaks(self) -> None:
        def leaky_transport_error(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"cannot reach {self.echo(request)}", request=request)

        def leaky_redirects_error(request: httpx.Request) -> httpx.Response:
            raise httpx.TooManyRedirects(f"redirect loop at {self.echo(request)}", request=request)

        scenarios: dict[str, Callable[[httpx.Request], httpx.Response]] = {
            "401": lambda request: httpx.Response(401, text=self.echo(request)),
            "500 exhausted": lambda request: httpx.Response(500, text=self.echo(request)),
            "429 exhausted": lambda request: httpx.Response(429, text=self.echo(request)),
            "transport error": leaky_transport_error,
            "non-transport request error": leaky_redirects_error,
            "invalid JSON": lambda request: httpx.Response(200, text=self.echo(request)),
        }
        for name, scenario in scenarios.items():
            for operation in ("search", "test"):
                with self.subTest(scenario=name, operation=operation):

                    async def handler(
                        request: httpx.Request,
                        scenario: Callable[[httpx.Request], httpx.Response] = scenario,
                    ) -> httpx.Response:
                        return scenario(request)

                    indexer = self.prowlarr(handler)
                    with self.assertRaises(IndexerError) as caught:
                        if operation == "search":
                            await indexer.search(EPISODE)
                        else:
                            await indexer.test()
                    for exc in exception_chain(caught.exception):
                        self.assertNotIn(PROWLARR_KEY, str(exc))
                        self.assertNotIn(PROWLARR_KEY, repr(exc))
                        self.assertNotIn(PROWLARR_KEY, " ".join(map(str, exc.args)))

        logged = self.log_output.getvalue()
        self.assertIn("/api/v1/search", logged)  # the retry warnings were captured...
        self.assertNotIn(PROWLARR_KEY, logged)  # ...without the key

    async def test_download_links_are_kept_but_never_logged(self) -> None:
        text = (FIXTURES / "prowlarr_search.json").read_text(encoding="utf-8")
        raw = json.loads(text.replace(FIXTURE_PROWLARR_KEY, PROWLARR_KEY))
        raw[0]["seeders"] = "lots"  # malformed, and its downloadUrl carries the key
        raw.append({"title": None, "downloadUrl": raw[3]["downloadUrl"], "protocol": "torrent"})

        async def handler(request: httpx.Request) -> httpx.Response:
            return ok(raw)

        releases = await self.prowlarr(handler).search(EPISODE)
        self.assertEqual(len(releases), 2)
        assert releases[1].download_url is not None
        self.assertIn(PROWLARR_KEY, releases[1].download_url)  # stored for the download...
        logged = self.log_output.getvalue()
        self.assertEqual(logged.count("skipping malformed"), 2)
        self.assertNotIn(PROWLARR_KEY, logged)  # ...but never logged


if __name__ == "__main__":
    unittest.main()
