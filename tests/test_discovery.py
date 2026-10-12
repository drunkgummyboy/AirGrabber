"""DiscoveryService with real TMDB/TVMaze clients on httpx.MockTransport and a real library."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock

import httpx

from app.config import AppPaths
from app.database import Database
from app.models import DiscoverShow, DiscoverShowEntry, MovieDetails, Show, TVDetails
from app.services import Services
from app.services.discovery import DiscoveryService, NoTVMazeMatch, normalize_title
from app.services.library import LibraryService
from app.services.tmdb import (
    TMDBAuthError,
    TMDBClient,
    TMDBError,
    TMDBNotConfigured,
    TMDBNotFound,
)
from app.services.tvmaze import TVMazeClient, TVMazeError

FIXTURES = Path(__file__).parent / "fixtures"
LOGGER = "app.services"  # the discovery service and both API clients
V3_KEY = "0123456789abcdef0123456789abcdef"

LINK_CLICK_TMDB = 123542  # tv_details.json: imdb tt14976292, tvdb 402033
LINK_CLICK_TVMAZE = 55050  # tvmaze/lookup_imdb.json
LINK_CLICK_IMDB = "tt14976292"
LINK_CLICK_TVDB = 402033
SPIDER_MAN = 969681  # movie_details.json

TV_TRENDING = "/3/trending/tv/week"
TV_ON_AIR = "/3/tv/on_the_air"
TV_DISCOVER = "/3/discover/tv"
MOVIE_TRENDING = "/3/trending/movie/week"
MOVIE_DISCOVER = "/3/discover/movie"
TV_PATHS = (TV_TRENDING, TV_ON_AIR, TV_DISCOVER)
MOVIE_PATHS = (MOVIE_TRENDING, MOVIE_DISCOVER)


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def ids(items: Any) -> list[int]:
    return [item["id"] for item in items]


def results_page(items: list[Any], number: int = 1) -> dict[str, Any]:
    return {"page": number, "results": items, "total_pages": 2, "total_results": 40}


def digital_details(movie_id: int, *days: date) -> dict[str, Any]:
    """Movie details whose US digital (type 4) releases fall on `days`."""
    releases = [
        {
            "certification": "",
            "iso_639_1": "",
            "note": "",
            "release_date": f"{day.isoformat()}T00:00:00.000Z",
            "type": 4,
        }
        for day in days
    ]
    return {
        "id": movie_id,
        "release_dates": {"results": [{"iso_3166_1": "US", "release_dates": releases}]},
    }


LINK_CLICK = DiscoverShow(id=LINK_CLICK_TMDB, name="LINK CLICK", first_air_date=date(2021, 4, 30))


class FakeAPIs:
    """Serves TMDB and TVMaze from fixtures and records every request.

    TVMaze lookups whose (param, value) is in `lookups` answer 301 → /shows/<id>, others 404.
    A path in `failures` answers with that status; a path in `gates` blocks until it is set.
    """

    def __init__(self, today: Callable[[], date]) -> None:
        self.today = today
        self.tmdb: dict[str, Any] = {
            TV_TRENDING: load("tmdb/tv_trending.json"),
            TV_ON_AIR: load("tmdb/tv_on_the_air.json"),
            TV_DISCOVER: load("tmdb/tv_premieres.json"),
            MOVIE_TRENDING: load("tmdb/movie_trending.json"),
            "/3/search/movie": load("tmdb/movie_search.json"),
            f"/3/tv/{LINK_CLICK_TMDB}": load("tmdb/tv_details.json"),
        }
        self.just_dropped = load("tmdb/movie_just_dropped.json")
        self.upcoming = load("tmdb/movie_upcoming_digital.json")
        # /discover/movie pages by (shelf, page); a missing page is empty, an int is a status.
        self.digital: dict[tuple[str, int], Any] = {
            ("just_dropped", 1): self.just_dropped,
            ("upcoming", 1): self.upcoming,
        }
        # /movie/<id> payloads; any other film has no release data (digital date unknown).
        self.movie_details: dict[int, Any] = {SPIDER_MAN: load("tmdb/movie_details.json")}
        self.shows: dict[int, Any] = {LINK_CLICK_TVMAZE: load("tvmaze/lookup_imdb.json")}
        self.lookups: dict[tuple[str, str], int] = {
            ("imdb", LINK_CLICK_IMDB): LINK_CLICK_TVMAZE,
            ("thetvdb", str(LINK_CLICK_TVDB)): LINK_CLICK_TVMAZE,
        }
        self.searches: dict[str, Any] = {}
        self.failures: dict[str, int] = {}
        self.gates: dict[str, asyncio.Event] = {}
        self.requests: list[httpx.Request] = []

    def count(self, *paths: str) -> int:
        return sum(1 for request in self.requests if request.url.path in paths)

    def of(self, path: str) -> list[httpx.Request]:
        return [request for request in self.requests if request.url.path == path]

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        if (gate := self.gates.get(path)) is not None:
            await gate.wait()
        if (status := self.failures.get(path)) is not None:
            return httpx.Response(status)
        if request.url.host == "api.themoviedb.org":
            return self._tmdb(request)
        return self._tvmaze(request)

    def _tmdb(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        params = request.url.params
        if path == MOVIE_DISCOVER:
            start = date.fromisoformat(params["release_date.gte"])
            shelf = "upcoming" if start > self.today() else "just_dropped"
            number = int(params.get("page", "1"))
            payload = self.digital.get((shelf, number), results_page([], number))
            if isinstance(payload, int):
                return httpx.Response(payload)
        elif path.startswith("/3/movie/"):
            movie_id = int(path.removeprefix("/3/movie/"))
            payload = self.movie_details.get(movie_id, {"id": movie_id})
        else:
            payload = self.tmdb.get(path)
        if payload is None:
            return httpx.Response(404, json={"status_code": 34, "success": False})
        return httpx.Response(200, json=payload)

    def _tvmaze(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/lookup/shows":
            [(param, value)] = request.url.params.multi_items()
            show_id = self.lookups.get((param, value))
            if show_id is None:
                return httpx.Response(404, json={"name": "Not Found", "status": 404})
            location = f"https://api.tvmaze.com/shows/{show_id}"
            return httpx.Response(301, headers={"Location": location})
        if path == "/search/shows":
            return httpx.Response(200, json=self.searches.get(request.url.params["q"], []))
        if path.startswith("/shows/"):
            show = self.shows.get(int(path.removeprefix("/shows/")))
            return httpx.Response(404) if show is None else httpx.Response(200, json=show)
        return httpx.Response(404)


async def until(condition: Callable[[], bool]) -> None:
    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), timeout=5)


class NormalizeTitleTests(unittest.TestCase):
    def test_leading_article(self) -> None:
        self.assertEqual(normalize_title("The Office"), "office")
        self.assertEqual(normalize_title("The Office"), normalize_title("Office"))

    def test_ampersand_and_apostrophe_variants(self) -> None:
        variants = [
            "Georgie & Mandy's First Marriage",
            "Georgie and Mandy's First Marriage",
            "Georgie & Mandys First Marriage",
            "georgie &  mandy’s first marriage",
            "GEORGIE AND MANDY'S FIRST MARRIAGE",
        ]
        for name in variants:
            with self.subTest(name=name):
                self.assertEqual(normalize_title(name), "georgie mandys first marriage")

    def test_punctuation_and_whitespace(self) -> None:
        self.assertEqual(
            normalize_title("Law & Order: Special Victims Unit"), "law order special victims unit"
        )
        self.assertEqual(normalize_title("  The   Last\tof Us \n"), "last of us")
        self.assertEqual(normalize_title("S.W.A.T."), normalize_title("SWAT"))

    def test_only_whole_words_are_dropped(self) -> None:
        self.assertEqual(normalize_title("Theatre Band"), "theatre band")
        self.assertNotEqual(normalize_title("The Office"), normalize_title("The Office (UK)"))


class DiscoveryTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = await Database.open(Path(self.tmp.name) / "discovery.db")
        self.now = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
        self.server = FakeAPIs(lambda: self.today)
        # Like the app's shared client, which follows TVMaze's lookup redirects.
        self.http = httpx.AsyncClient(
            transport=httpx.MockTransport(self.server), follow_redirects=True
        )
        self.key = V3_KEY
        tmdb = TMDBClient(self.http, lambda: self.key, retry_delay=0)
        tvmaze = TVMazeClient(self.http, retry_delay=0)
        self.library = LibraryService(self.db.library, self.db.history, tvmaze)
        self.service = DiscoveryService(tmdb, tvmaze, self.library, clock=lambda: self.now)

    async def asyncTearDown(self) -> None:
        await self.http.aclose()
        await self.db.close()
        self.tmp.cleanup()

    @property
    def today(self) -> date:
        return self.now.astimezone().date()

    async def add_tracked(self, show_id: int, name: str) -> None:
        await self.db.library.save_show(Show(id=show_id, name=name))
        await self.db.library.track(show_id)

    async def tracked_ids(self) -> list[int]:
        return [entry.show.id for entry in await self.library.tracked()]


class TVDashboardTests(DiscoveryTestCase):
    async def test_shelves_are_capped_and_flagged(self) -> None:
        self.server.tmdb[TV_TRENDING]["results"][1]["name"] = "The Office"
        await self.add_tracked(1, "Office")
        await self.add_tracked(2, "Law and Order - Special Victims Unit")
        dashboard = await self.service.tv_dashboard()

        trending = self.server.tmdb[TV_TRENDING]["results"]
        on_air = self.server.tmdb[TV_ON_AIR]["results"]
        premieres = self.server.tmdb[TV_DISCOVER]["results"]
        self.assertEqual([e.show.id for e in dashboard.trending], ids(trending[:18]))
        self.assertEqual([e.show.id for e in dashboard.on_air], ids(on_air[:18]))
        self.assertEqual([e.show.id for e in dashboard.premieres], ids(premieres[:18]))
        self.assertEqual(dashboard.failed, ())

        flagged = {e.show.name for e in dashboard.trending + dashboard.on_air if e.is_tracked}
        self.assertEqual(flagged, {"The Office", "Law & Order: Special Victims Unit"})
        self.assertFalse(any(e.is_tracked for e in dashboard.premieres))

    async def test_requests_and_date_window(self) -> None:
        await self.service.tv_dashboard()
        self.assertEqual([self.server.count(path) for path in TV_PATHS], [1, 1, 1])
        [premieres] = self.server.of(TV_DISCOVER)
        params = premieres.url.params
        self.assertEqual(params["first_air_date.gte"], self.today.isoformat())
        self.assertEqual(
            params["first_air_date.lte"], (self.today + timedelta(days=90)).isoformat()
        )

    async def test_cached_for_30_minutes(self) -> None:
        first = await self.service.tv_dashboard()
        self.now += timedelta(minutes=29, seconds=59)
        self.assertEqual(await self.service.tv_dashboard(), first)
        self.assertEqual(self.server.count(*TV_PATHS), 3)
        self.now += timedelta(seconds=1)
        self.assertEqual(await self.service.tv_dashboard(), first)
        self.assertEqual(self.server.count(*TV_PATHS), 6)

    async def test_force_refetches(self) -> None:
        await self.service.tv_dashboard()
        await self.service.tv_dashboard(force=True)
        self.assertEqual(self.server.count(*TV_PATHS), 6)
        await self.service.tv_dashboard()  # the forced fetch was cached
        self.assertEqual(self.server.count(*TV_PATHS), 6)

    async def test_new_day_refetches(self) -> None:
        await self.service.tv_dashboard()
        self.now += timedelta(days=1)
        await self.service.tv_dashboard()
        self.assertEqual(self.server.count(*TV_PATHS), 6)

    async def test_tracked_flags_follow_the_library_while_cached(self) -> None:
        def flagged(entries: tuple[DiscoverShowEntry, ...]) -> list[str]:
            return [entry.show.name for entry in entries if entry.is_tracked]

        dashboard = await self.service.tv_dashboard()
        self.assertEqual(flagged(dashboard.trending), [])
        await self.add_tracked(7, "Link Click")
        dashboard = await self.service.tv_dashboard()
        self.assertEqual(flagged(dashboard.trending), ["LINK CLICK"])
        await self.library.untrack(7)
        dashboard = await self.service.tv_dashboard()
        self.assertEqual(flagged(dashboard.trending), [])
        self.assertEqual(self.server.count(*TV_PATHS), 3)  # all from the cache

    async def test_failed_shelf_is_empty_and_not_cached(self) -> None:
        self.server.failures[TV_ON_AIR] = 503
        with self.assertLogs(LOGGER, "WARNING") as logs:
            dashboard = await self.service.tv_dashboard()
        self.assertEqual(dashboard.on_air, ())
        self.assertEqual(dashboard.failed, ("on_air",))
        self.assertEqual(len(dashboard.trending), 18)
        self.assertEqual(len(dashboard.premieres), 18)
        self.assertTrue(any("on_air shelf" in line for line in logs.output))

        del self.server.failures[TV_ON_AIR]
        before = self.server.count(*TV_PATHS)
        dashboard = await self.service.tv_dashboard()
        self.assertEqual(self.server.count(*TV_PATHS), before + 3)  # nothing was cached
        self.assertEqual((len(dashboard.on_air), dashboard.failed), (18, ()))
        await self.service.tv_dashboard()
        self.assertEqual(self.server.count(*TV_PATHS), before + 3)  # now it is

    async def test_every_shelf_failing_raises_the_first_error(self) -> None:
        for path in TV_PATHS:
            self.server.failures[path] = 422
        with self.assertLogs(LOGGER, "WARNING"), self.assertRaises(TMDBError) as caught:
            await self.service.tv_dashboard()
        self.assertIn("/trending/tv/week", str(caught.exception))
        self.server.failures.clear()
        await self.service.tv_dashboard()
        self.assertEqual(self.server.count(*TV_PATHS), 6)

    async def test_missing_key_propagates(self) -> None:
        self.key = ""
        with self.assertRaises(TMDBNotConfigured):
            await self.service.tv_dashboard()
        with self.assertRaises(TMDBNotConfigured):
            await self.service.movie_dashboard()
        self.assertEqual(self.server.requests, [])

    async def test_rejected_key_propagates(self) -> None:
        self.server.failures[TV_ON_AIR] = 401
        with self.assertRaises(TMDBAuthError):
            await self.service.tv_dashboard()
        self.server.failures = {MOVIE_TRENDING: 401}
        with self.assertRaises(TMDBAuthError):
            await self.service.movie_dashboard()


class MovieDashboardTests(DiscoveryTestCase):
    async def test_shelves_requests_and_windows(self) -> None:
        dashboard = await self.service.movie_dashboard()
        trending = self.server.tmdb[MOVIE_TRENDING]["results"]
        just_dropped = self.server.just_dropped["results"]
        upcoming = self.server.upcoming["results"]
        self.assertEqual([m.id for m in dashboard.just_dropped], ids(just_dropped[:18]))
        self.assertEqual([m.id for m in dashboard.trending], ids(trending[:18]))
        self.assertEqual([m.id for m in dashboard.upcoming], ids(upcoming[:18]))
        self.assertEqual(dashboard.failed, ())

        windows = [
            (
                r.url.params["release_date.gte"],
                r.url.params["release_date.lte"],
                r.url.params.get("page", "1"),
            )
            for r in self.server.of(MOVIE_DISCOVER)
        ]
        dropped_since = (self.today - timedelta(days=14)).isoformat()
        tomorrow = (self.today + timedelta(days=1)).isoformat()
        upcoming_until = (self.today + timedelta(days=21)).isoformat()
        today = self.today.isoformat()
        self.assertCountEqual(
            windows,
            [
                (dropped_since, today, "1"),
                (dropped_since, today, "2"),
                (tomorrow, upcoming_until, "1"),
                (tomorrow, upcoming_until, "2"),
            ],
        )
        self.assertEqual(self.server.count(MOVIE_TRENDING), 1)

    async def test_cached_for_30_minutes_and_force(self) -> None:
        # Each fetch: trending plus two pages of each digital shelf.
        await self.service.movie_dashboard()
        self.now += timedelta(minutes=29)
        await self.service.movie_dashboard()
        self.assertEqual(self.server.count(*MOVIE_PATHS), 5)
        await self.service.movie_dashboard(force=True)
        self.assertEqual(self.server.count(*MOVIE_PATHS), 10)
        self.now += timedelta(minutes=30)
        await self.service.movie_dashboard()
        self.assertEqual(self.server.count(*MOVIE_PATHS), 15)

    async def test_failed_shelf(self) -> None:
        self.server.failures[MOVIE_TRENDING] = 500
        with self.assertLogs(LOGGER, "WARNING"):
            dashboard = await self.service.movie_dashboard()
        self.assertEqual((dashboard.trending, dashboard.failed), ((), ("trending",)))
        self.assertEqual(len(dashboard.just_dropped), 18)
        self.assertEqual(len(dashboard.upcoming), 18)

    async def test_every_shelf_failing_raises(self) -> None:
        self.server.failures = {MOVIE_DISCOVER: 500, MOVIE_TRENDING: 500}
        with self.assertLogs(LOGGER, "WARNING"), self.assertRaises(TMDBError) as caught:
            await self.service.movie_dashboard()
        self.assertIn("/discover/movie", str(caught.exception))


class DigitalShelfTests(DiscoveryTestCase):
    """The digital shelves keep only films whose FIRST digital release is in their window."""

    def set_first_digital(self, movie_id: int, *offsets: int) -> None:
        """Digital releases `offsets` days from today."""
        days = (self.today + timedelta(days=offset) for offset in offsets)
        self.server.movie_details[movie_id] = digital_details(movie_id, *days)

    async def test_films_first_released_outside_the_window_are_dropped(self) -> None:
        # Just dropped: [today - 14, today].
        self.server.movie_details[933260] = digital_details(933260, date(2024, 10, 31))
        self.set_first_digital(1311031, -76, -3)  # Demon Slayer: re-released in the window
        self.set_first_digital(1236045, -14)  # first day of the window: kept
        self.set_first_digital(1204680, -15)  # a day too early
        self.set_first_digital(1291595, 0)  # today: kept
        # Upcoming: (today, today + 21].
        self.server.movie_details[1072790] = digital_details(1072790, date(2024, 2, 20))
        self.set_first_digital(1377237, 1)  # kept
        self.set_first_digital(1058424, 0)  # already out today
        self.set_first_digital(1514026, 21)  # last day of the window: kept
        self.set_first_digital(1302904, 22)  # a day too late

        dashboard = await self.service.movie_dashboard()

        just_dropped = [m.id for m in dashboard.just_dropped]
        dropped = {933260, 1311031, 1204680}
        expected = [i for i in ids(self.server.just_dropped["results"]) if i not in dropped]
        self.assertEqual(just_dropped, expected)  # unknown dates (most films here) are kept
        self.assertIn(SPIDER_MAN, just_dropped)  # first digital 2026-10-06, per its details
        upcoming = [m.id for m in dashboard.upcoming]
        dropped = {1072790, 1058424, 1302904}
        expected = [i for i in ids(self.server.upcoming["results"]) if i not in dropped]
        self.assertEqual(upcoming, expected)
        self.assertEqual(dashboard.failed, ())

        # The filtered dashboard is what gets cached.
        requests = len(self.server.requests)
        self.assertEqual(await self.service.movie_dashboard(), dashboard)
        self.assertEqual(len(self.server.requests), requests)

    async def test_page_2_fills_the_shelves_up_to_18(self) -> None:
        page_1 = self.server.just_dropped["results"]
        for movie in page_1[:5]:
            self.set_first_digital(movie["id"], -400)
        extra = [{"id": 9000 + n, "title": f"Extra {n}"} for n in range(20)]
        # page_1[5] again: popularity can move a film between the two page requests.
        self.server.digital[("just_dropped", 2)] = results_page([page_1[5], *extra], 2)
        self.server.digital[("upcoming", 2)] = results_page(extra, 2)

        dashboard = await self.service.movie_dashboard()

        self.assertEqual(
            [m.id for m in dashboard.just_dropped], ids(page_1[5:]) + [9000, 9001, 9002]
        )
        self.assertEqual(
            [m.id for m in dashboard.upcoming], ids(self.server.upcoming["results"][:18])
        )
        pages = sorted(r.url.params.get("page", "1") for r in self.server.of(MOVIE_DISCOVER))
        self.assertEqual(pages, ["1", "1", "2", "2"])

    async def test_films_whose_details_fail_are_kept(self) -> None:
        for movie_id, status in ((933260, 404), (1311031, 422)):
            self.server.movie_details[movie_id] = digital_details(movie_id, date(2024, 1, 1))
            self.server.failures[f"/3/movie/{movie_id}"] = status
        with self.assertLogs("app.services.discovery", "DEBUG") as logs:
            dashboard = await self.service.movie_dashboard()
        just_dropped = [m.id for m in dashboard.just_dropped]
        self.assertIn(933260, just_dropped)
        self.assertIn(1311031, just_dropped)
        self.assertEqual(dashboard.failed, ())
        self.assertTrue(any("TMDB id 933260" in line for line in logs.output))
        self.assertTrue(any("TMDB id 1311031" in line for line in logs.output))
        self.assertTrue(all(line.startswith("DEBUG") for line in logs.output))

    async def test_rejected_key_on_details_propagates(self) -> None:
        self.server.failures[f"/3/movie/{SPIDER_MAN}"] = 401
        with self.assertRaises(TMDBAuthError):
            await self.service.movie_dashboard()

    async def test_a_failing_page_fails_its_shelf(self) -> None:
        self.server.digital[("upcoming", 2)] = 422
        with self.assertLogs(LOGGER, "WARNING") as logs:
            dashboard = await self.service.movie_dashboard()
        self.assertEqual((dashboard.upcoming, dashboard.failed), ((), ("upcoming",)))
        self.assertEqual(len(dashboard.just_dropped), 18)
        self.assertTrue(any("upcoming shelf" in line for line in logs.output))

    async def test_details_land_in_the_shared_cache(self) -> None:
        dashboard = await self.service.movie_dashboard()
        first = dashboard.just_dropped[0].id
        upcoming = dashboard.upcoming[0].id
        self.assertEqual(self.server.count(f"/3/movie/{first}", f"/3/movie/{upcoming}"), 2)
        requests = len(self.server.requests)
        self.assertEqual((await self.service.movie_details(first)).id, first)
        self.assertEqual((await self.service.movie_details(upcoming)).id, upcoming)
        self.assertEqual(len(self.server.requests), requests)
        await self.service.movie_dashboard(force=True)  # lists again, details from the cache
        self.assertEqual(len(self.server.requests), requests + 5)


class DetailsTests(DiscoveryTestCase):
    MOVIE_PATH = f"/3/movie/{SPIDER_MAN}"

    async def test_movie_details_cached_for_24_hours(self) -> None:
        details = await self.service.movie_details(SPIDER_MAN)
        self.assertIsInstance(details, MovieDetails)
        self.assertEqual(details.imdb_id, "tt22084616")
        self.now += timedelta(hours=23, minutes=59)
        self.assertEqual(await self.service.movie_details(SPIDER_MAN), details)
        self.assertEqual(self.server.count(self.MOVIE_PATH), 1)
        self.now += timedelta(minutes=1)
        await self.service.movie_details(SPIDER_MAN)
        self.assertEqual(self.server.count(self.MOVIE_PATH), 2)

    async def test_tv_details_cached(self) -> None:
        details = await self.service.tv_details(LINK_CLICK_TMDB)
        self.assertIsInstance(details, TVDetails)
        self.assertEqual((details.imdb_id, details.tvdb_id), (LINK_CLICK_IMDB, LINK_CLICK_TVDB))
        await self.service.tv_details(LINK_CLICK_TMDB)
        self.assertEqual(self.server.count(f"/3/tv/{LINK_CLICK_TMDB}"), 1)

    async def test_concurrent_calls_share_one_request(self) -> None:
        gate = asyncio.Event()
        self.server.gates[self.MOVIE_PATH] = gate
        first = asyncio.create_task(self.service.movie_details(SPIDER_MAN))
        second = asyncio.create_task(self.service.movie_details(SPIDER_MAN))
        await until(lambda: self.server.count(self.MOVIE_PATH) == 1)
        gate.set()
        a, b = await asyncio.gather(first, second)
        self.assertEqual(a, b)
        self.assertEqual(self.server.count(self.MOVIE_PATH), 1)

    async def test_errors_are_not_cached(self) -> None:
        self.server.failures[self.MOVIE_PATH] = 404
        with self.assertRaises(TMDBNotFound):
            await self.service.movie_details(SPIDER_MAN)
        del self.server.failures[self.MOVIE_PATH]
        await self.service.movie_details(SPIDER_MAN)
        self.assertEqual(self.server.count(self.MOVIE_PATH), 2)

    async def test_a_shared_fetch_survives_one_cancelled_caller(self) -> None:
        gate = asyncio.Event()
        self.server.gates[self.MOVIE_PATH] = gate
        leaving = asyncio.create_task(self.service.movie_details(SPIDER_MAN))
        staying = asyncio.create_task(self.service.movie_details(SPIDER_MAN))
        await until(lambda: self.server.count(self.MOVIE_PATH) == 1)
        leaving.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await leaving
        gate.set()
        self.assertEqual((await staying).id, SPIDER_MAN)
        self.assertEqual(self.server.count(self.MOVIE_PATH), 1)

    async def test_the_last_caller_leaving_cancels_the_fetch(self) -> None:
        gate = asyncio.Event()
        self.server.gates[self.MOVIE_PATH] = gate
        task = asyncio.create_task(self.service.movie_details(SPIDER_MAN))
        await until(lambda: self.server.count(self.MOVIE_PATH) == 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        gate.set()
        # The abandoned fetch is gone: this call starts (and caches) a new one.
        await self.service.movie_details(SPIDER_MAN)
        await self.service.movie_details(SPIDER_MAN)
        self.assertEqual(self.server.count(self.MOVIE_PATH), 2)

    async def test_search_movies_passes_through(self) -> None:
        movies = await self.service.search_movies("dune", limit=3)
        self.assertEqual([m.id for m in movies], [438631, 693134, 841])
        await self.service.search_movies("dune")  # not cached
        requests = self.server.of("/3/search/movie")
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].url.params["query"], "dune")
        self.assertEqual(len(await self.service.search_movies("  ")), 0)


class ClearCacheTests(DiscoveryTestCase):
    async def test_clear_cache_forgets_lists_and_details(self) -> None:
        await self.service.tv_dashboard()
        await self.service.movie_dashboard()
        await self.service.movie_details(SPIDER_MAN)
        await self.service.tv_details(LINK_CLICK_TMDB)
        self.service.clear_cache()
        await self.service.tv_dashboard()
        await self.service.movie_dashboard()
        await self.service.movie_details(SPIDER_MAN)
        await self.service.tv_details(LINK_CLICK_TMDB)
        self.assertEqual(self.server.count(*TV_PATHS), 6)
        self.assertEqual(self.server.count(*MOVIE_PATHS), 10)
        # Fetched by each movie dashboard (Spider-Man is just dropped); the calls hit the cache.
        self.assertEqual(self.server.count(f"/3/movie/{SPIDER_MAN}"), 2)
        self.assertEqual(self.server.count(f"/3/tv/{LINK_CLICK_TMDB}"), 2)

    async def test_fetches_in_flight_during_a_clear_are_not_cached(self) -> None:
        details_path = f"/3/movie/{SPIDER_MAN}"
        gates = {path: asyncio.Event() for path in (details_path, TV_TRENDING)}
        self.server.gates.update(gates)
        details = asyncio.create_task(self.service.movie_details(SPIDER_MAN))
        dashboard = asyncio.create_task(self.service.tv_dashboard())
        await until(lambda: self.server.count(details_path, TV_TRENDING) == 2)
        self.service.clear_cache()
        for gate in gates.values():
            gate.set()
        self.assertEqual((await details).id, SPIDER_MAN)  # the callers still get their data
        self.assertEqual(len((await dashboard).trending), 18)
        await self.service.movie_details(SPIDER_MAN)
        await self.service.tv_dashboard()
        self.assertEqual(self.server.count(details_path), 2)
        self.assertEqual(self.server.count(*TV_PATHS), 6)


class TrackTests(DiscoveryTestCase):
    def lookups(self) -> list[tuple[str, str]]:
        return [
            item for request in self.server.of("/lookup/shows")
            for item in request.url.params.multi_items()
        ]

    async def test_track_by_imdb_id(self) -> None:
        show = await self.service.track(LINK_CLICK)
        self.assertEqual((show.id, show.name), (LINK_CLICK_TVMAZE, "LINK CLICK"))
        self.assertEqual(await self.tracked_ids(), [LINK_CLICK_TVMAZE])
        self.assertEqual(self.lookups(), [("imdb", LINK_CLICK_IMDB)])
        self.assertEqual(self.server.count("/search/shows"), 0)

    async def test_falls_back_to_the_tvdb_id(self) -> None:
        del self.server.lookups[("imdb", LINK_CLICK_IMDB)]
        show = await self.service.track(LINK_CLICK)
        self.assertEqual(show.id, LINK_CLICK_TVMAZE)
        self.assertEqual(
            self.lookups(), [("imdb", LINK_CLICK_IMDB), ("thetvdb", str(LINK_CLICK_TVDB))]
        )
        self.assertEqual(await self.tracked_ids(), [LINK_CLICK_TVMAZE])

    async def test_without_an_imdb_id_looks_up_the_tvdb_id_only(self) -> None:
        self.server.tmdb[f"/3/tv/{LINK_CLICK_TMDB}"]["external_ids"]["imdb_id"] = None
        await self.service.track(LINK_CLICK)
        self.assertEqual(self.lookups(), [("thetvdb", str(LINK_CLICK_TVDB))])

    def search_hits(self) -> list[dict[str, Any]]:
        namesake = {"id": 999, "name": "Link Click", "premiered": "2019-03-01"}
        return [
            {"score": 0.9, "show": namesake},
            {"score": 0.8, "show": load("tvmaze/lookup_imdb.json")},
        ]

    async def test_falls_back_to_name_and_year(self) -> None:
        self.server.lookups.clear()
        self.server.searches["LINK CLICK"] = self.search_hits()
        show = await self.service.track(LINK_CLICK)
        self.assertEqual(show.id, LINK_CLICK_TVMAZE)  # the 2021 hit, not the first one
        self.assertEqual(await self.tracked_ids(), [LINK_CLICK_TVMAZE])
        self.assertEqual(len(self.lookups()), 2)

    async def test_no_match_raises(self) -> None:
        self.server.lookups.clear()
        self.server.searches["LINK CLICK"] = self.search_hits()[:1]  # only the 2019 namesake
        with self.assertRaises(NoTVMazeMatch) as caught:
            await self.service.track(LINK_CLICK)
        self.assertIn("LINK CLICK", str(caught.exception))
        self.assertEqual(await self.tracked_ids(), [])
        self.assertEqual(self.server.count(f"/shows/{LINK_CLICK_TVMAZE}", "/shows/999"), 0)

    async def test_no_name_search_without_a_year(self) -> None:
        self.server.lookups.clear()
        self.server.searches["LINK CLICK"] = self.search_hits()
        undated = LINK_CLICK.model_copy(update={"first_air_date": None})
        with self.assertRaises(NoTVMazeMatch):
            await self.service.track(undated)
        self.assertEqual(self.server.count("/search/shows"), 0)

    async def test_errors_propagate(self) -> None:
        self.server.failures[f"/3/tv/{LINK_CLICK_TMDB}"] = 404
        with self.assertRaises(TMDBNotFound):
            await self.service.track(LINK_CLICK)
        del self.server.failures[f"/3/tv/{LINK_CLICK_TMDB}"]
        self.server.failures["/lookup/shows"] = 500
        with self.assertLogs(LOGGER, "WARNING"), self.assertRaises(TVMazeError):
            await self.service.track(LINK_CLICK)
        self.assertEqual(await self.tracked_ids(), [])

    async def test_tracked_show_is_flagged_despite_a_different_name(self) -> None:
        self.server.shows[LINK_CLICK_TVMAZE]["name"] = "Shiguang Dailiren"

        def link_click_flag(entries: tuple[DiscoverShowEntry, ...]) -> bool:
            return next(e.is_tracked for e in entries if e.show.id == LINK_CLICK_TMDB)

        self.assertFalse(link_click_flag((await self.service.tv_dashboard()).trending))
        show = await self.service.track(LINK_CLICK)
        self.assertEqual(show.name, "Shiguang Dailiren")
        self.assertTrue(link_click_flag((await self.service.tv_dashboard()).trending))
        await self.library.untrack(LINK_CLICK_TVMAZE)
        self.assertFalse(link_click_flag((await self.service.tv_dashboard()).trending))
        self.assertEqual(self.server.count(*TV_PATHS), 3)  # flags changed on cached lists


class ServicesWiringTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.paths = AppPaths(Path(self.tmp.name))
        # A real httpx client loads its CA bundle synchronously (~0.2 s): not worth a debug warning.
        asyncio.get_running_loop().slow_callback_duration = 1.0

    async def test_create_exposes_discovery_and_aclose_still_works(self) -> None:
        network = AssertionError("no network calls")
        with mock.patch.object(httpx.AsyncClient, "send", side_effect=network):
            services = await Services.create(self.paths)
            try:
                self.assertIsInstance(services.discovery, DiscoveryService)
                # One TVMaze client, shared by the library and discovery.
                self.assertIs(services.discovery._tvmaze, services.library._tvmaze)
                with self.assertRaises(TMDBNotConfigured):  # a fresh profile has no key
                    await services.discovery.tv_dashboard()
            finally:
                await services.aclose()
        self.assertTrue(services._http.is_closed)

    async def test_tmdb_uses_the_current_key_and_a_new_key_clears_the_caches(self) -> None:
        services = await Services.create(self.paths)
        self.addAsyncCleanup(services.aclose)
        sent: list[httpx.Request] = []

        async def fake_send(
            client: httpx.AsyncClient, request: httpx.Request, **kwargs: Any
        ) -> httpx.Response:
            sent.append(request)
            return httpx.Response(200, json={"results": []}, request=request)

        with (
            mock.patch.object(httpx.AsyncClient, "send", fake_send),
            mock.patch.object(services.discovery, "clear_cache") as clear,
        ):
            await services.settings.update(weeks_to_show=4)
            clear.assert_not_called()
            await services.settings.update(tmdb_api_key="firstkey")
            clear.assert_called_once()
            await services.discovery.search_movies("dune")
            self.assertEqual(sent[-1].url.params["api_key"], "firstkey")
            await services.settings.update(weeks_to_show=2)
            await services.settings.update(tmdb_api_key=" firstkey ")  # the same key once stripped
            clear.assert_called_once()
            await services.settings.update(tmdb_api_key="secondkey")
            self.assertEqual(clear.call_count, 2)
            await services.discovery.search_movies("dune")
            self.assertEqual(sent[-1].url.params["api_key"], "secondkey")

    async def test_a_new_key_really_empties_the_cache(self) -> None:
        services = await Services.create(self.paths)
        self.addAsyncCleanup(services.aclose)
        payload = load("tmdb/movie_details.json")
        sent: list[httpx.Request] = []

        async def fake_send(
            client: httpx.AsyncClient, request: httpx.Request, **kwargs: Any
        ) -> httpx.Response:
            sent.append(request)
            return httpx.Response(200, json=payload, request=request)

        with mock.patch.object(httpx.AsyncClient, "send", fake_send):
            await services.settings.update(tmdb_api_key="firstkey")
            await services.discovery.movie_details(SPIDER_MAN)
            await services.discovery.movie_details(SPIDER_MAN)
            self.assertEqual(len(sent), 1)
            await services.settings.update(tmdb_api_key="secondkey")
            await services.discovery.movie_details(SPIDER_MAN)
            self.assertEqual(len(sent), 2)


if __name__ == "__main__":
    unittest.main()
