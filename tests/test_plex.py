"""Plex (app/services/plex.py): the PIN sign-in, the servers and the order their connections are
tried in, paging, guids of the new and the old agents, the mapping to TVMaze / TMDB ids, the v10
migration, the missing rule with what is in Plex (calendar, counts, the show window, the season
paths), the automatic downloads skipping it, Disconnect, the backup leaving the token out, and
the token never in a log line or URL. A fake plex.tv and Plex Media Server answer through
httpx.MockTransport from tests/fixtures/plex/; nothing reaches the network."""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import logging
import sqlite3
import tempfile
import unittest
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from app import __version__
from app.database import MIGRATIONS, Database
from app.models import DownloadRecord, Episode, MediaType, Settings, Show
from app.models.plex import (
    PlexCounts,
    PlexItemReply,
    PlexLibrary,
    PlexPin,
    PlexProblem,
    PlexScan,
    PlexServer,
)
from app.services import SettingsService
from app.services.auto_download import AutoDownloadService
from app.services.backup import replace_changes, settings_for_backup
from app.services.library import LibraryService
from app.services.missing import missing_cutoffs, missing_episodes
from app.services.plex import (
    PAGE_SIZE,
    PIN_POLL_S,
    PlexClient,
    PlexService,
    connection_order,
    external_ids,
    match_scan,
    parse_guid,
)
from app.services.tvmaze import TVMazeClient
from app.ui.calendar_stack import needed_entries, row_state, row_text
from app.ui.show_details import row_mark, season_marks, season_needed
from app.models import MovieDetails
from tests.plex_link import have_marks, mark_have, plex_settings
from tests.test_auto_download import FakeQuickDownload
from tests.test_watchlist import FakeQuickDownload as FakeFilmDownload
from tests.test_watchlist import WatchlistTestCase
from tests.test_watchlist import movie as watch_movie

FIXTURES = Path(__file__).parent / "fixtures" / "plex"
TOKEN = "fake-plex-token-7f3a9c"
CLIENT_ID = "00000000-0000-4000-8000-000000000001"
SERVER_ID = "a1b2c3d4e5f6a7b8c9d0a1b2c3d4e5f6a7b8c9d0"
LAPTOP_ID = "b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2"
LOCAL_HTTPS = "https://192-168-1-20.a1b2c3d4e5f6.plex.direct:32400"
LOCAL_HTTP = "http://192.168.1.20:32400"
REMOTE = "https://203-0-113-7.a1b2c3d4e5f6.plex.direct:32400"
RELAY = "https://198-51-100-9.a1b2c3d4e5f6.plex.direct:8443"
NOW = datetime(2026, 11, 3, 10, 0, tzinfo=UTC)
DAY = timedelta(days=1)


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _base(url: httpx.URL) -> str:
    default = {"http": 80, "https": 443}[url.scheme]
    port = f":{url.port}" if url.port not in (None, default) else ""
    return f"{url.scheme}://{url.host}{port}"


class FakePlex:
    """plex.tv and the server Alex-NAS behind httpx.MockTransport."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.answering: set[str] = {LOCAL_HTTPS, LOCAL_HTTP, REMOTE}  # the server's addresses
        self.plex_tv_down = False
        self.refuse = False  # 401 for the token
        self.resources = "resources.json"
        self.resources_data: list[dict[str, Any]] | None = None  # instead of the file
        self.polls_before_linked = 2
        self.pin_gone = False
        self.items: dict[tuple[str, int], list[dict[str, Any]]] = {
            ("1", 2): fixture("shows.json")["MediaContainer"]["Metadata"],
            ("1", 4): fixture("episodes.json")["MediaContainer"]["Metadata"],
            ("2", 1): fixture("movies.json")["MediaContainer"]["Metadata"],
            ("3", 1): [],
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "plex.tv":
            return self._plex_tv(request)
        if _base(request.url) not in self.answering:
            raise httpx.ConnectError("no route to host", request=request)
        return self._server(request)

    def _authorised(self, request: httpx.Request) -> bool:
        return not self.refuse and request.headers.get("X-Plex-Token") == TOKEN

    def _plex_tv(self, request: httpx.Request) -> httpx.Response:
        if self.plex_tv_down:
            raise httpx.ConnectTimeout("timed out", request=request)
        path = request.url.path
        if path == "/api/v2/pins" and request.method == "POST":
            return httpx.Response(201, json=fixture("pin_new.json"))
        if path.startswith("/api/v2/pins/"):
            if self.pin_gone:
                return httpx.Response(404, json={"errors": [{"code": 1020}]})
            if self.polls_before_linked > 0:
                self.polls_before_linked -= 1
                return httpx.Response(200, json=fixture("pin_new.json"))
            return httpx.Response(200, json=fixture("pin_linked.json"))
        if path == "/api/v2/resources":
            if not self._authorised(request):
                return httpx.Response(401, json={"errors": [{"code": 1001}]})
            if self.resources_data is not None:
                return httpx.Response(200, json=self.resources_data)
            return httpx.Response(200, json=fixture(self.resources))
        return httpx.Response(404)

    def _server(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/identity":
            return httpx.Response(200, json=fixture("identity.json"))
        if not self._authorised(request):
            return httpx.Response(401, text="Unauthorized")
        if path == "/library/sections":
            return httpx.Response(200, json=fixture("sections.json"))
        parts = path.strip("/").split("/")  # library, sections, key, all
        if len(parts) == 4 and parts[3] == "all":
            items = self.items.get((parts[2], int(request.url.params["type"])), [])
            start = int(request.headers.get("X-Plex-Container-Start", "0"))
            size = int(request.headers.get("X-Plex-Container-Size", str(len(items) or 1)))
            page = items[start : start + size]
            return httpx.Response(
                200,
                json={
                    "MediaContainer": {
                        "size": len(page),
                        "totalSize": len(items),
                        "offset": start,
                        "Metadata": page,
                    }
                },
            )
        return httpx.Response(404)

    def urls(self) -> list[str]:
        return [str(request.url) for request in self.requests]

    def identities(self) -> list[str]:
        return [_base(r.url) for r in self.requests if r.url.path == "/identity"]


def show(show_id: int, name: str, *, tvdb: int | None = None, imdb: str | None = None) -> Show:
    return Show(id=show_id, name=name, tvdb_id=tvdb, imdb_id=imdb)


def ep(
    episode_id: int, show_id: int, season: int, number: int | None, aired: datetime
) -> Episode:
    return Episode(
        id=episode_id,
        show_id=show_id,
        season=season,
        number=number,
        airstamp=aired,
        air_date=aired.date(),
    )


# -- guids and the mapping (pure) ---------------------------------------------------------------


class GuidTests(unittest.TestCase):
    def test_the_new_agents(self) -> None:
        self.assertEqual(parse_guid("tvdb://412345"), ("tvdb", "412345"))
        self.assertEqual(parse_guid("imdb://tt13194362"), ("imdb", "tt13194362"))
        self.assertEqual(parse_guid("tmdb://202411"), ("tmdb", "202411"))

    def test_the_old_agents(self) -> None:
        self.assertEqual(
            parse_guid("com.plexapp.agents.thetvdb://399999?lang=en"), ("tvdb", "399999")
        )
        # An episode's old guid names its show first.
        self.assertEqual(
            parse_guid("com.plexapp.agents.thetvdb://399999/4/1?lang=en"), ("tvdb", "399999")
        )
        self.assertEqual(
            parse_guid("com.plexapp.agents.imdb://tt0111161?lang=en"), ("imdb", "tt0111161")
        )
        self.assertEqual(
            parse_guid("com.plexapp.agents.themoviedb://1022789?lang=en"), ("tmdb", "1022789")
        )
        self.assertEqual(
            parse_guid("com.plexapp.agents.hama://tvdb-424242?lang=en"), ("tvdb", "424242")
        )
        self.assertEqual(
            parse_guid("com.plexapp.agents.hama://tvdb2-424242?lang=en"), ("tvdb", "424242")
        )

    def test_what_names_no_outside_id(self) -> None:
        for text in (
            "plex://show/5d9c086c46115600200aa2fe",
            "local://3004",
            "com.plexapp.agents.none://3004",
            "com.plexapp.agents.hama://anidb-1234?lang=en",
            "imdb://nm0000001",  # a person, not a title
            "tvdb://0",
            "tvdb://abc",
            "",
            "not a guid",
        ):
            with self.subTest(text=text):
                self.assertIsNone(parse_guid(text))

    def test_an_items_ids_in_order_without_repeats(self) -> None:
        item = PlexItemReply.model_validate(
            {
                "ratingKey": 7,
                "guid": "com.plexapp.agents.thetvdb://1?lang=en",
                "Guid": [{"id": "tvdb://1"}, {"id": "imdb://tt0000001"}, {"id": "tvdb://2"}],
            }
        )
        self.assertEqual(item.rating_key, "7")
        self.assertEqual(external_ids(item), {"tvdb": ["1", "2"], "imdb": ["tt0000001"]})


def items(name: str) -> list[PlexItemReply]:
    return [
        PlexItemReply.model_validate(item) for item in fixture(name)["MediaContainer"]["Metadata"]
    ]


KNOWN = [
    (101, 412345, "tt99999901"),  # The Ark: TheTVDB id (its IMDb id differs from Plex's)
    (102, None, "tt11280740"),  # Severance: only an IMDb id in the cache
    (103, 399999, None),  # Slow Horses (Plex's old TheTVDB agent)
    (104, 424242, None),  # Frieren (Hama)
]


class MatchTests(unittest.TestCase):
    def test_shows_by_tvdb_else_imdb_and_their_episodes(self) -> None:
        scan = match_scan(
            shows=items("shows.json"),
            episodes=items("episodes.json"),
            films=[],
            known=KNOWN,
        )
        self.assertEqual(
            scan.episodes,
            {(101, 2, 8), (101, 2, 9), (102, 2, 4), (103, 4, 1), (104, 1, 1)},
        )
        self.assertEqual(scan.shows, 6)
        # Not known to AirGrabber, and a show with only a TMDB id (TVMaze has none).
        self.assertEqual(scan.unmatched_shows, 2)

    def test_tvdb_wins_over_imdb(self) -> None:
        item = PlexItemReply.model_validate(
            {"ratingKey": "1", "Guid": [{"id": "imdb://tt0000002"}, {"id": "tvdb://5"}]}
        )
        episode = PlexItemReply.model_validate(
            {"grandparentRatingKey": "1", "parentIndex": 1, "index": 1}
        )
        scan = match_scan(
            shows=[item], episodes=[episode], films=[], known=[(1, 5, None), (2, None, "tt0000002")]
        )
        self.assertEqual(scan.episodes, {(1, 1, 1)})

    def test_films_by_tmdb_and_imdb(self) -> None:
        scan = match_scan(shows=[], episodes=[], films=items("movies.json"), known=[])
        self.assertEqual(
            scan.films,
            {(693134, "tt15239678"), (1022789, None), (None, "tt0111161")},
        )
        self.assertEqual(scan.unmatched_films, 1)  # a home video without any outside id


class ConnectionOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_owned_servers_only_and_local_https_first(self) -> None:
        fake = FakePlex()
        async with httpx.AsyncClient(transport=httpx.MockTransport(fake)) as http:
            client = PlexClient(http, client_id=lambda: CLIENT_ID)
            servers = await client.servers(TOKEN)
        [server] = servers  # not the player, not the friend's shared server
        self.assertEqual((server.machine_id, server.name), (SERVER_ID, "Alex-NAS"))
        self.assertEqual(
            [c.uri for c in connection_order(server)], [LOCAL_HTTPS, LOCAL_HTTP, REMOTE]
        )
        self.assertNotIn(RELAY, [c.uri for c in server.connections])
        [request] = fake.requests
        self.assertEqual(request.url.params["includeHttps"], "1")
        self.assertEqual(request.url.params["includeRelay"], "0")


# -- the service ---------------------------------------------------------------------------------


async def until(condition: Any, timeout: float = 5.0) -> None:
    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), timeout=timeout)


@contextlib.contextmanager
def capture_logs(*names: str) -> Iterator[list[str]]:
    """Every record of these loggers, DEBUG and up, formatted (exception text included)."""
    lines: list[str] = []

    class Keep(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(self.format(record))

    handler = Keep(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    loggers = [logging.getLogger(name) for name in names]
    levels = [logger.level for logger in loggers]
    for logger in loggers:
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
    try:
        yield lines
    finally:
        for logger, level in zip(loggers, levels, strict=True):
            logger.removeHandler(handler)
            logger.setLevel(level)


class Clock:
    def __init__(self, start: datetime = NOW) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)
        await asyncio.sleep(0)


class PlexTestCase(unittest.IsolatedAsyncioTestCase):
    """A database with four shows in the cache (three followed), a fake Plex and the service."""

    connected = True

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.db = await Database.open(Path(self.tmp.name) / "library.db")
        self.addAsyncCleanup(self.db.close)
        aired = NOW - 10 * DAY
        await self.db.library.save_show(
            show(101, "The Ark", tvdb=412345, imdb="tt99999901"),
            [ep(1208, 101, 2, 8, aired), ep(1209, 101, 2, 9, aired), ep(1210, 101, 2, 10, aired)],
        )
        await self.db.library.save_show(
            show(102, "Severance", imdb="tt11280740"), [ep(1404, 102, 2, 4, aired)]
        )
        await self.db.library.save_show(
            show(103, "Slow Horses", tvdb=399999), [ep(1501, 103, 4, 1, aired)]
        )
        await self.db.library.save_show(
            show(104, "Frieren", tvdb=424242), [ep(1601, 104, 1, 1, aired)]
        )
        for show_id in (101, 102, 103):
            await self.db.library.track(show_id, profile_id=1)
        values: dict[str, Any] = {"plex_client_id": CLIENT_ID}
        if self.connected:
            values["plex_token"] = TOKEN
        self.settings = SettingsService(self.db.settings, Settings(**values))
        self.fake = FakePlex()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.fake))
        self.addAsyncCleanup(self.http.aclose)
        self.clock = Clock()
        self.changes = 0
        self.plex = PlexService(
            PlexClient(self.http, client_id=lambda: self.settings.current.plex_client_id),
            self.db.plex,
            self.settings,
            on_change=self.changed,
            clock=self.clock,
            sleep=self.clock.sleep,
        )

    def changed(self) -> None:
        self.changes += 1


class PinTests(PlexTestCase):
    connected = False

    async def test_signing_in(self) -> None:
        pin = await self.plex.new_pin()
        self.assertEqual(pin.code, "K7QX")
        self.assertEqual(pin.expires_at, datetime(2026, 11, 3, 10, 15, tzinfo=UTC))
        self.assertTrue(await self.plex.wait_for_pin(pin))
        self.assertEqual(self.settings.current.plex_token.get_secret_value(), TOKEN)
        self.assertEqual(self.clock.sleeps, [PIN_POLL_S, PIN_POLL_S])  # every 2 s
        self.assertTrue(self.plex.connected)
        self.assertEqual(self.changes, 1)  # connected: the pages show what you have now
        create = self.fake.requests[0]
        self.assertEqual((create.method, create.url.path), ("POST", "/api/v2/pins"))
        self.assertEqual(create.url.params["strong"], "false")  # a short code
        for request in self.fake.requests:
            self.assertEqual(request.headers["X-Plex-Client-Identifier"], CLIENT_ID)
            self.assertEqual(request.headers["X-Plex-Product"], "AirGrabber")
            self.assertEqual(request.headers["X-Plex-Version"], __version__)
            self.assertEqual(request.headers["Accept"], "application/json")
            self.assertNotIn("X-Plex-Token", request.headers)

    async def test_the_pages_hear_plex_connected_and_disconnected(self) -> None:
        # As Services wires it: a change of Plex reaches the library's listeners (the pages
        # reload), and the library reads whether Plex is connected from the same settings.
        library = LibraryService(
            self.db.library,
            self.db.history,
            TVMazeClient(self.http, retry_delay=0),
            settings=self.settings,
        )
        plex = PlexService(
            PlexClient(self.http, client_id=lambda: self.settings.current.plex_client_id),
            self.db.plex,
            self.settings,
            on_change=library.notify_changed,
            clock=self.clock,
            sleep=self.clock.sleep,
        )
        heard: list[bool] = []
        library.subscribe(lambda: heard.append(library.plex_connected))
        self.assertFalse(library.plex_connected)
        self.assertTrue(await plex.wait_for_pin(await plex.new_pin()))
        self.assertEqual(heard, [True])
        await plex.disconnect()
        self.assertEqual(heard, [True, False])
        self.assertFalse(library.plex_connected)

    async def test_a_client_identifier_is_made_once(self) -> None:
        settings = SettingsService(self.db.settings, Settings())
        plex = PlexService(
            PlexClient(self.http, client_id=lambda: settings.current.plex_client_id),
            self.db.plex,
            settings,
            clock=self.clock,
        )
        await plex.new_pin()
        made = settings.current.plex_client_id
        self.assertRegex(made, r"^[0-9a-f-]{36}$")
        await plex.new_pin()
        self.assertEqual(settings.current.plex_client_id, made)
        self.assertEqual(self.fake.requests[-1].headers["X-Plex-Client-Identifier"], made)

    async def test_the_code_expires(self) -> None:
        self.fake.polls_before_linked = 10_000
        pin = PlexPin(id=4242001, code="K7QX", expires_at=NOW + timedelta(seconds=7))
        self.assertFalse(await self.plex.wait_for_pin(pin))
        self.assertEqual(len(self.clock.sleeps), 4)  # asked at 0, 2, 4 and 6 s
        self.assertFalse(self.plex.connected)

    async def test_a_code_plex_forgot(self) -> None:
        self.fake.pin_gone = True
        pin = PlexPin(id=4242001, code="K7QX", expires_at=NOW + timedelta(minutes=10))
        self.assertFalse(await self.plex.wait_for_pin(pin))
        self.assertFalse(self.plex.connected)

    async def test_cancelling_stops_waiting(self) -> None:
        self.fake.polls_before_linked = 10_000
        pin = PlexPin(id=4242001, code="K7QX", expires_at=NOW + timedelta(minutes=15))
        task = asyncio.create_task(self.plex.wait_for_pin(pin))
        for _ in range(5):
            await asyncio.sleep(0)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.plex.connected)

    async def test_plex_tv_away_for_a_moment_is_waited_out(self) -> None:
        pin = PlexPin(id=4242001, code="K7QX", expires_at=NOW + timedelta(minutes=15))
        self.fake.plex_tv_down = True
        task = asyncio.create_task(self.plex.wait_for_pin(pin))
        for _ in range(6):
            await asyncio.sleep(0)
        self.fake.plex_tv_down = False
        self.assertTrue(await task)

    async def test_no_check_without_a_token(self) -> None:
        await self.plex.check()
        self.assertEqual(self.fake.requests, [])


class CheckTests(PlexTestCase):
    async def test_a_check_reads_and_maps_everything(self) -> None:
        await self.plex.check()
        episodes, films = await self.db.plex.contents()
        self.assertEqual(
            episodes, {(101, 2, 8), (101, 2, 9), (102, 2, 4), (103, 4, 1), (104, 1, 1)}
        )
        self.assertEqual(films, {(693134, "tt15239678"), (1022789, None), (None, "tt0111161")})
        status = await self.plex.status()
        self.assertIsNone(status.problem)
        assert status.server is not None
        self.assertEqual((status.server.machine_id, status.server.name), (SERVER_ID, "Alex-NAS"))
        # The followed shows' episodes (not Frieren's: nobody follows it).
        self.assertEqual(
            status.counts,
            PlexCounts(episodes=4, shows=3, films=3, unmatched_shows=2, checked_at=NOW),
        )
        self.assertEqual(
            [(lib.key, lib.title, lib.kind, lib.on) for lib in status.libraries],
            [("1", "TV Shows", "show", True), ("2", "Movies", "movie", True),
             ("3", "Home videos", "movie", True)],
        )
        self.assertNotIn("/library/sections/4/all", [r.url.path for r in self.fake.requests])
        self.assertEqual(self.changes, 1)
        await self.plex.check()  # the same again: nothing changed
        self.assertEqual(self.changes, 1)

    async def test_local_https_first_then_the_others_and_the_one_that_answered_is_kept(
        self,
    ) -> None:
        self.fake.answering = {LOCAL_HTTP, REMOTE}
        await self.plex.check()
        self.assertEqual(self.fake.identities(), [LOCAL_HTTPS, LOCAL_HTTP])
        self.assertEqual(self.settings.current.plex_server_uri, LOCAL_HTTP)
        library = [r for r in self.fake.requests if r.url.path.startswith("/library")]
        self.assertTrue(all(_base(r.url) == LOCAL_HTTP for r in library))

        self.fake.requests.clear()
        await self.plex.check()
        self.assertEqual(self.fake.identities(), [LOCAL_HTTP])  # tried first next time

        self.fake.requests.clear()
        self.fake.answering = {REMOTE}  # it stops answering: the others again
        await self.plex.check()
        self.assertEqual(self.fake.identities(), [LOCAL_HTTP, LOCAL_HTTPS, REMOTE])
        self.assertEqual(self.settings.current.plex_server_uri, REMOTE)

    async def test_paging(self) -> None:
        count = 2 * PAGE_SIZE + 3
        template = fixture("episodes.json")["MediaContainer"]["Metadata"][0]
        self.fake.items[("1", 4)] = [
            {**copy.deepcopy(template), "ratingKey": str(n), "parentIndex": 1 + n // 100,
             "index": 1 + n % 100}
            for n in range(count)
        ]
        await self.plex.check()
        pages = [
            (r.headers["X-Plex-Container-Start"], r.headers["X-Plex-Container-Size"])
            for r in self.fake.requests
            if r.url.path == "/library/sections/1/all" and r.url.params["type"] == "4"
        ]
        self.assertEqual(pages, [("0", "500"), ("500", "500"), ("1000", "500")])
        episodes, _ = await self.db.plex.contents()
        self.assertEqual(len(episodes), count)  # all The Ark's

    async def test_a_server_that_ignores_the_page_size_is_read_once(self) -> None:
        many = [{"ratingKey": str(n)} for n in range(PAGE_SIZE + 20)]

        def everything(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"MediaContainer": {"size": len(many), "Metadata": many}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(everything)) as http:
            client = PlexClient(http, client_id=lambda: CLIENT_ID)
            found = await client.items(LOCAL_HTTP, TOKEN, "1", 2, guids=True)
        self.assertEqual(len(found), PAGE_SIZE + 20)

    async def test_a_library_switched_off(self) -> None:
        await self.plex.check()
        await self.plex.set_library("2", False)
        _, films = await self.db.plex.contents()
        self.assertEqual(films, frozenset())
        status = await self.plex.status()
        self.assertEqual({lib.key: lib.on for lib in status.libraries}, {"1": True, "2": False, "3": True})
        self.assertEqual(self.settings.current.plex_sections_off, ("2",))
        await self.plex.set_library("2", True)
        _, films = await self.db.plex.contents()
        self.assertEqual(len(films), 3)

    async def test_unreachable_keeps_the_last_list(self) -> None:
        await self.plex.check()
        before = await self.db.plex.contents()
        self.fake.answering = set()
        self.fake.plex_tv_down = True
        self.clock.now += timedelta(hours=2)
        with self.assertLogs("app.services.plex", logging.INFO):
            await self.plex.check()
        self.assertEqual(await self.db.plex.contents(), before)
        status = await self.plex.status()
        self.assertIs(status.problem, PlexProblem.UNREACHABLE)
        self.assertEqual(status.counts.checked_at, NOW)  # the last list's

    async def test_plex_tv_away_but_the_server_answers(self) -> None:
        await self.plex.check()
        self.fake.plex_tv_down = True
        self.fake.items[("2", 1)] = []
        await self.plex.check()
        self.assertIsNone((await self.plex.status()).problem)
        _, films = await self.db.plex.contents()
        self.assertEqual(films, frozenset())

    async def test_a_refused_token_keeps_the_last_list(self) -> None:
        await self.plex.check()
        before = await self.db.plex.contents()
        self.fake.refuse = True
        with self.assertLogs("app.services.plex", logging.WARNING):
            await self.plex.check()
        self.assertEqual(await self.db.plex.contents(), before)
        self.assertIs((await self.plex.status()).problem, PlexProblem.SIGNED_OUT)

    async def test_several_servers_wait_for_a_choice(self) -> None:
        self.fake.resources = "resources_two.json"
        self.fake.answering = {LOCAL_HTTP}
        await self.plex.check()
        status = await self.plex.status()
        self.assertIs(status.problem, PlexProblem.CHOOSE_SERVER)
        self.assertEqual([s.name for s in status.servers], ["Alex-NAS", "Alex-Laptop"])
        self.assertEqual(await self.db.plex.contents(), (frozenset(), frozenset()))
        await self.plex.choose_server(SERVER_ID)
        status = await self.plex.status()
        self.assertIsNone(status.problem)
        assert status.server is not None
        self.assertEqual(status.server.name, "Alex-NAS")
        self.assertEqual(len((await self.db.plex.contents())[0]), 5)

    async def test_no_server_on_the_account(self) -> None:
        # Only the player and a friend's shared server.
        self.fake.resources_data = [
            r for r in fixture("resources.json") if r["clientIdentifier"] != SERVER_ID
        ]
        await self.plex.check()
        self.assertIs((await self.plex.status()).problem, PlexProblem.NO_SERVER)
        self.assertEqual(await self.db.plex.contents(), (frozenset(), frozenset()))

    async def test_one_check_at_a_time(self) -> None:
        await asyncio.gather(self.plex.check(), self.plex.check(), self.plex.check())
        sections = [r for r in self.fake.requests if r.url.path == "/library/sections"]
        self.assertEqual(len(sections), 1)

    async def test_disconnect_forgets_everything(self) -> None:
        await self.plex.check()
        await self.plex.set_library("3", False)
        changes = self.changes
        await self.plex.disconnect()
        current = self.settings.current
        self.assertEqual(current.plex_token.get_secret_value(), "")
        self.assertEqual(
            (current.plex_server_id, current.plex_server_name, current.plex_server_uri),
            ("", "", ""),
        )
        self.assertEqual(current.plex_sections_off, ())
        self.assertEqual(await self.db.plex.contents(), (frozenset(), frozenset()))
        status = await self.plex.status()
        self.assertFalse(status.connected)
        self.assertEqual(status.counts, PlexCounts())
        self.assertEqual(status.libraries, ())
        self.assertEqual(self.changes, changes + 1)
        self.assertEqual(await self.db.library.plex_ids(101), frozenset())

    async def test_periodic_checks(self) -> None:
        task = asyncio.create_task(self.plex.run_periodic(interval_s=1800))
        await until(lambda: 1800 in self.clock.sleeps)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self.assertIn(1800, self.clock.sleeps)
        self.assertEqual(len((await self.db.plex.contents())[0]), 5)


class SecretTests(PlexTestCase):
    connected = False

    async def test_the_token_never_appears_in_a_log_line_or_url(self) -> None:
        # AirGrabber's own loggers and httpx's (whose request lines name the URL).
        with capture_logs("app", "httpx", "httpcore") as lines:
            pin = await self.plex.new_pin()
            self.assertTrue(await self.plex.wait_for_pin(pin))
            await self.plex.check()
            self.fake.refuse = True
            await self.plex.check()
            self.fake.refuse = False
            self.fake.answering = set()
            self.fake.plex_tv_down = True
            await self.plex.check()
        self.assertTrue(any("Plex check" in line for line in lines))
        for line in lines:
            self.assertNotIn(TOKEN, line)
        for url in self.fake.urls():
            self.assertNotIn(TOKEN, url)
        with_token = [r for r in self.fake.requests if "X-Plex-Token" in r.headers]
        self.assertTrue(with_token)
        self.assertTrue(all(r.headers["X-Plex-Token"] == TOKEN for r in with_token))
        self.assertNotIn(TOKEN, repr(self.settings.current))
        self.assertNotIn(TOKEN, repr(await self.plex.status()))

    async def test_redirects_are_never_followed(self) -> None:
        moved: list[str] = []

        def redirect(request: httpx.Request) -> httpx.Response:
            moved.append(str(request.url))
            return httpx.Response(302, headers={"Location": "https://elsewhere.example/x"})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(redirect), follow_redirects=True
        ) as http:
            client = PlexClient(http, client_id=lambda: CLIENT_ID)
            with self.assertRaises(Exception):
                await client.servers(TOKEN)
        self.assertEqual(len(moved), 1)

    async def test_the_backup_never_holds_plex(self) -> None:
        settings = Settings(
            plex_client_id=CLIENT_ID,
            plex_token=TOKEN,
            plex_server_id=SERVER_ID,
            plex_server_name="Alex-NAS",
            plex_server_uri=LOCAL_HTTP,
            plex_sections_off=("3",),
            tmdb_api_key="a-tmdb-key",
        )
        stored = settings_for_backup(settings, include_secrets=True)
        self.assertNotIn(TOKEN, json.dumps(stored))
        self.assertFalse({name for name in stored if name.startswith("plex_")})
        self.assertIn("tmdb_api_key", stored)  # the other secrets do go in when asked
        # A load keeps this PC's Plex link.
        self.assertFalse(
            {name for name in replace_changes(Settings(), settings) if name.startswith("plex_")}
        )


# -- the migration --------------------------------------------------------------------------------


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_v9_database_is_upgraded_with_its_data(self) -> None:
        self.assertIn("CREATE TABLE plex_episodes", MIGRATIONS[9])
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "v9.db"
        synced = "2026-11-01T10:00:00+00:00"
        with contextlib.closing(sqlite3.connect(path)) as raw:
            script = "\n".join(MIGRATIONS[:9])
            raw.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = 9;\nCOMMIT;")
            with raw:
                raw.execute(
                    "INSERT INTO shows (id, name, imdb_id) VALUES (1, 'Kept', 'tt0000001')"
                )
                raw.execute(
                    "INSERT INTO episodes (id, show_id, season, number, air_date) "
                    "VALUES (11, 1, 1, 1, '2026-10-01')"
                )
                raw.execute(
                    "INSERT INTO tracked_shows (profile_id, show_id, tracked_at, synced_at) "
                    "VALUES (1, 1, ?, ?)",
                    (synced, synced),
                )
                raw.execute(
                    "INSERT INTO have_episodes (episode_id, marked_at) VALUES (11, ?)", (synced,)
                )
        db = await Database.open(path)
        self.addAsyncCleanup(db.close)
        version = await db.fetch_one("PRAGMA user_version")
        assert version is not None
        self.assertEqual(version[0], 10)
        kept = await db.library.get_show(1)
        assert kept is not None
        self.assertEqual((kept.name, kept.imdb_id, kept.tvdb_id), ("Kept", "tt0000001", None))
        self.assertEqual(await have_marks(db, 1), frozenset({11}))  # kept, no longer read
        # Followed shows are read from TVMaze again at the next sync (for their TheTVDB id).
        [entry] = await db.library.followed()
        self.assertIsNone(entry.synced_at)
        tables = {row["name"]: row["strict"] for row in await db.fetch_all("PRAGMA table_list")}
        for table in ("plex_episodes", "plex_movies", "plex_state"):
            self.assertEqual(tables[table], 1, table)
        self.assertEqual(await db.plex.contents(), (frozenset(), frozenset()))
        self.assertEqual(await db.plex.counts(), PlexCounts())
        # The show's TheTVDB id is kept from now on.
        await db.library.save_show(Show(id=1, name="Kept", tvdb_id=4242), [])
        refreshed = await db.library.get_show(1)
        assert refreshed is not None
        self.assertEqual(refreshed.tvdb_id, 4242)

    async def test_plex_rows_go_with_their_show(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        db = await Database.open(Path(tmp.name) / "x.db")
        self.addAsyncCleanup(db.close)
        await db.library.save_show(Show(id=5, name="Gone"), [])
        await db.plex.replace(
            PlexScan(episodes=frozenset({(5, 1, 1), (6, 1, 1)})), NOW  # 6 isn't in the cache
        )
        self.assertEqual((await db.plex.contents())[0], {(5, 1, 1)})
        await db.library.prune_schedule(date(2100, 1, 1))  # nobody follows it, no episodes
        self.assertEqual((await db.plex.contents())[0], frozenset())


# -- the missing rule with Plex --------------------------------------------------------------------


class MissingRuleTests(unittest.IsolatedAsyncioTestCase):
    """Severance: season 2 aired E01–E03 (missing unless in Plex or grabbed), E04 is still to
    air. Plex is connected (without it nothing is missing: tests/test_missing.py)."""

    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.db = await Database.open(Path(tmp.name) / "library.db")
        self.addAsyncCleanup(self.db.close)
        self.http = httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(500))
        )
        self.addAsyncCleanup(self.http.aclose)
        self.library = LibraryService(
            self.db.library,
            self.db.history,
            TVMazeClient(self.http, retry_delay=0),
            settings=plex_settings(self.db),
        )
        self.episodes = [ep(21, 1, 2, 1, NOW - 4 * DAY), ep(22, 1, 2, 2, NOW - 3 * DAY)]
        # E04 airs long after any day these tests run on.
        self.episodes += [ep(23, 1, 2, 3, NOW - 2 * DAY), ep(24, 1, 2, 4, NOW + 3650 * DAY)]
        await self.db.library.save_show(Show(id=1, name="Severance"), self.episodes)
        await self.db.library.track(1, profile_id=1)

    async def in_plex(self, *numbers: int) -> None:
        await self.db.plex.replace(
            PlexScan(episodes=frozenset((1, 2, number) for number in numbers)), NOW
        )

    async def test_counts_calendar_and_show_window(self) -> None:
        self.assertEqual(await self.library.missing_counts(now=NOW, profile_id=1), {1: 3})
        await self.in_plex(1, 2)
        self.assertEqual(await self.library.missing_counts(now=NOW, profile_id=1), {1: 1})
        start, end = NOW.date() - 7 * DAY, NOW.date() + 7 * DAY
        self.assertEqual(
            await self.library.missing_between(start, end, now=NOW, profile_id=1),
            frozenset({23}),
        )
        self.assertEqual(await self.library.plex_between(start, end), frozenset({21, 22}))
        details = await self.library.show_details(1, now=NOW, profile_id=1)
        self.assertEqual(details.in_plex, frozenset({21, 22}))
        self.assertEqual(details.missing, frozenset({23}))
        marks = [row_mark(details, e) for e in details.episodes]
        self.assertEqual(marks, ["plex", "plex", "missing", None])
        self.assertEqual(season_marks(details, 2), ([23], [21, 22]))
        self.assertEqual(
            [entry.episode.id for entry in season_needed(details, 2, lambda _key: None)], [23]
        )
        # "Download missing" fetches only what isn't in Plex.
        entries = await self.library.missing_episodes(1, now=NOW, profile_id=1)
        self.assertEqual([entry.episode.id for entry in entries], [23])

    async def test_new_episodes_in_plex_are_not_new(self) -> None:
        now = NOW - 2 * DAY + timedelta(hours=3)  # E03 aired 3 hours ago: new
        start, end = now.date() - 7 * DAY, now.date() + 7 * DAY
        self.assertEqual(
            await self.library.new_between(start, end, now=now, profile_id=1), frozenset({23})
        )
        await self.in_plex(3)
        self.assertEqual(
            await self.library.new_between(start, end, now=now, profile_id=1), frozenset()
        )

    async def test_the_sql_and_the_python_rule_agree(self) -> None:
        await self.in_plex(2)
        await mark_have(self.db, [21], NOW.isoformat())  # an old mark: no longer counts
        sql = await self.library.missing_between(
            date(2026, 1, 1), date(2027, 1, 1), now=NOW, profile_id=1
        )
        plex = await self.db.library.plex_ids(1)
        python = missing_episodes(self.episodes, downloaded=set(), in_plex=plex, now=NOW)
        self.assertEqual(sql, frozenset(e.id for e in python))
        self.assertEqual(sql, frozenset({21, 23}))

    async def test_a_grabbed_episode_plex_lacks_is_neither_had_nor_missing(self) -> None:
        await self.in_plex(1)
        await self.db.history.add(
            DownloadRecord(
                media_type=MediaType.TV,
                media_id=1,
                season=2,
                episode=2,
                release_title="Severance.S02E02",
            )
        )
        details = await self.library.show_details(1, now=NOW, profile_id=1)
        self.assertEqual(details.missing, frozenset({23}))
        self.assertNotIn(22, details.in_plex)
        self.assertEqual([row_mark(details, e) for e in details.episodes][1], None)
        self.assertEqual(await self.library.missing_counts(now=NOW, profile_id=1), {1: 1})
        # "All missing" fetches only E03: E02 is on its way.
        self.assertEqual(
            [entry.episode.id for entry in season_needed(details, 2, lambda _key: None)], [23]
        )
        entries = await self.library.missing_episodes(1, now=NOW, profile_id=1)
        self.assertEqual([entry.episode.id for entry in entries], [23])

    async def test_the_calendar_stack_rows(self) -> None:
        await self.in_plex(1)
        await self.db.history.add(
            DownloadRecord(
                media_type=MediaType.TV,
                media_id=1,
                season=2,
                episode=2,
                release_title="Severance.S02E02",
            )
        )
        entries = await self.library.calendar(
            NOW.date() - 7 * DAY, NOW.date() + 7 * DAY, profile_id=1
        )
        plex = await self.library.plex_between(NOW.date() - 7 * DAY, NOW.date())
        states = [row_state(entry, None, plex, marks=True) for entry in entries]
        self.assertEqual(states, ["plex", "quiet", "needed"])  # E02 grabbed: on its way
        self.assertEqual(row_text("plex"), "✓ in Plex")
        self.assertEqual(row_text("quiet"), "")
        self.assertEqual(
            [e.episode.id for e in needed_entries(entries, lambda _key: None, plex)], [23]
        )
        # Without Plex the rows say nothing.
        states = [row_state(entry, None, frozenset(), marks=False) for entry in entries]
        self.assertEqual(states, ["quiet", "quiet", "quiet"])


class AutoDownloadTests(unittest.IsolatedAsyncioTestCase):
    """The Ark's E01 and E02 aired 10 and 5 hours ago; its switch has been on for 3 days."""

    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.db = await Database.open(Path(tmp.name) / "library.db")
        self.addAsyncCleanup(self.db.close)
        hour = timedelta(hours=1)
        episodes = [ep(31, 3, 1, 1, NOW - 10 * hour), ep(32, 3, 1, 2, NOW - 5 * hour)]
        await self.db.library.save_show(Show(id=3, name="The Ark"), episodes)
        await self.db.library.track(3, profile_id=1)
        await self.db.library.set_auto_download(3, True, since=NOW - 3 * DAY, profile_id=1)

    async def test_an_episode_in_plex_is_never_due_nor_given_up(self) -> None:
        await self.db.plex.replace(PlexScan(episodes=frozenset({(3, 1, 1)})), NOW)
        due = await self.db.library.auto_due(
            aired_from=NOW - 3 * DAY, aired_until=NOW - timedelta(hours=2), limit=10
        )
        self.assertEqual([entry.episode.id for entry in due], [32])
        given_up = await self.db.library.auto_give_up(aired_before=NOW, when=NOW)
        self.assertEqual([entry.episode.id for entry in given_up], [32])

    async def test_a_run_skips_what_is_in_plex(self) -> None:
        await self.db.plex.replace(PlexScan(episodes=frozenset({(3, 1, 1), (3, 1, 2)})), NOW)
        quick = FakeQuickDownload(self.db.history)
        auto = AutoDownloadService(
            self.db.library,
            quick,
            SettingsService(self.db.settings, Settings()),
            clock=lambda: NOW,
        )
        report = await auto.run_once()
        self.assertEqual(quick.calls, [])
        self.assertEqual(report.downloaded, ())
        await self.db.plex.replace(PlexScan(episodes=frozenset({(3, 1, 1)})), NOW)
        await auto.run_once()
        self.assertEqual([target.episode for _request, target in quick.calls], [2])


    async def test_an_old_manual_mark_no_longer_stops_it(self) -> None:
        await mark_have(self.db, [31], NOW.isoformat())  # "I already have this", before 3.1.0
        due = await self.db.library.auto_due(
            aired_from=NOW - 3 * DAY, aired_until=NOW - timedelta(hours=2), limit=10
        )
        self.assertEqual([entry.episode.id for entry in due], [31, 32])

    async def test_without_plex_nothing_is_downloaded_twice(self) -> None:
        quick = FakeQuickDownload(self.db.history)
        auto = AutoDownloadService(
            self.db.library,
            quick,
            SettingsService(self.db.settings, Settings()),  # Plex not connected
            clock=lambda: NOW,
        )
        await auto.run_once()
        self.assertEqual([target.episode for _request, target in quick.calls], [1, 2])
        await auto.run_once()  # both are in the download history now
        self.assertEqual([target.episode for _request, target in quick.calls], [1, 2])


class WatchlistFilmTests(WatchlistTestCase):
    """A watchlist film in Plex isn't searched for, and stays on the list."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.quick = FakeFilmDownload(self.downloads)
        self.auto = AutoDownloadService(
            self.db.library,
            self.quick,
            SettingsService(self.db.settings, Settings()),
            watchlist=self.service,
            clock=lambda: self.now,
        )

    async def add(self, movie_id: int, title: str, imdb_id: str | None = None) -> None:
        film = watch_movie(movie_id, title, release_date=None)
        self.discovery.details[movie_id] = MovieDetails(
            id=movie_id, imdb_id=imdb_id, digital_release=self.now.date()
        )
        await self.service.add(film, auto_download=True, profile_id=1)

    async def test_films_in_plex_are_skipped(self) -> None:
        await self.add(1, "Dune: Part Two", imdb_id="tt15239678")  # in Plex by TMDB id
        await self.add(2, "Old Favourite", imdb_id="tt0111161")  # by IMDb id only
        await self.add(3, "Weapons")
        await self.db.plex.replace(
            PlexScan(films=frozenset({(1, "tt15239678"), (None, "tt0111161")})), self.now
        )
        with self.assertLogs("app.services.auto_download", logging.INFO) as logs:
            await self.auto.run_once()
        self.assertEqual(self.quick.titles, ["Weapons"])
        self.assertTrue(self.service.contains(1))  # never taken off a watchlist
        self.assertTrue(self.service.contains(2))
        self.assertTrue(any("2 film(s) in Plex skipped" in line for line in logs.output))


# -- the film side ----------------------------------------------------------------------------------


class FilmTests(unittest.IsolatedAsyncioTestCase):
    async def test_films_by_tmdb_or_imdb(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        db = await Database.open(Path(tmp.name) / "x.db")
        self.addAsyncCleanup(db.close)
        await db.plex.replace(
            PlexScan(films=frozenset({(693134, "tt15239678"), (None, "tt0111161")})), NOW
        )
        films = await db.library.plex_films()
        self.assertTrue(films.has(693134))
        self.assertTrue(films.has(278, "tt0111161"))  # by its IMDb id
        self.assertFalse(films.has(278))
        self.assertFalse(films.has(None, None))


if __name__ == "__main__":
    unittest.main()
