"""Automatic Prowlarr setup against recorded Prowlarr payloads (httpx.MockTransport, no network)."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any
from unittest import mock

import httpx

from app.database import SettingsRepository
from app.models import ProwlarrSetupResult, ProwlarrState, ProwlarrStatus, Settings
from app.services.prowlarr_setup import (
    CURATED,
    WINGET_COMMAND,
    LocalProwlarr,
    ProwlarrSetupService,
    WingetRunner,
    on_this_pc,
    parse_config,
    read_local_config,
    run_winget,
)
from app.services.settings import SettingsService

FIXTURES = Path(__file__).parent / "fixtures" / "prowlarr_setup"
LOGGER = "app.services.prowlarr_setup"

KEY = "0123456789abcdef0123456789abcdef"  # the key in the fixture configs
BASE = "http://127.0.0.1:9696"  # what fixtures/prowlarr_setup/config.xml points at
STATUS = {"appName": "Prowlarr", "instanceName": "Prowlarr", "version": "2.3.0.5236"}
CURATED_NAMES = ("Knaben", "LimeTorrents", "TorrentDownload", "showRSS", "Nyaa.si")

Reply = (
    httpx.Response
    | Exception
    | Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]]
)


def load(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def ok(payload: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


def config(**elements: str) -> str:
    """A config.xml with the fixture key unless ApiKey is given."""
    elements.setdefault("ApiKey", KEY)
    body = "".join(f"  <{tag}>{value}</{tag}>\r\n" for tag, value in elements.items())
    return f"<Config>\r\n{body}</Config>\r\n"


def body(request: httpx.Request) -> Any:
    return json.loads(request.content)


def created(request: httpx.Request) -> httpx.Response:
    """POST /api/v1/indexer when Prowlarr's test passes: 201 with the saved indexer."""
    return ok({**body(request), "id": 15}, 201)


class FakeProwlarr:
    """Answers by (method, path below the base) with queued replies (the last one repeats);
    anything unrouted is a 404. Records every request."""

    def __init__(self, prefix: str = "") -> None:
        self.prefix = prefix  # Prowlarr's UrlBase
        self.routes: dict[tuple[str, str], list[Reply]] = {
            ("GET", "/ping"): [ok({"status": "OK"})],
            ("GET", "/api/v1/system/status"): [ok(STATUS)],
            ("GET", "/api/v1/appprofile"): [ok(load("appprofile.json"))],
            ("GET", "/api/v1/indexer"): [ok(load("indexer_list_empty.json"))],
            ("GET", "/api/v1/indexer/schema"): [ok(load("indexer_schema.json"))],
            ("POST", "/api/v1/indexer"): [created],
        }
        self.requests: list[httpx.Request] = []

    def on(self, method: str, path: str, *replies: Reply) -> None:
        self.routes[(method, path)] = list(replies)

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix(self.prefix)
        replies = self.routes.get((request.method, path))
        if not replies:
            return httpx.Response(404)
        reply = replies.pop(0) if len(replies) > 1 else replies[0]
        if isinstance(reply, Exception):
            raise reply
        if isinstance(reply, httpx.Response):
            return reply
        answer = reply(request)
        return await answer if inspect.isawaitable(answer) else answer

    def paths(self, method: str | None = None) -> list[str]:
        return [r.url.path for r in self.requests if method is None or r.method == method]

    def posted(self, path: str = "/api/v1/indexer") -> list[dict[str, Any]]:
        return [body(r) for r in self.requests if r.method == "POST" and r.url.path == path]


class FakeTime:
    """A clock that only moves when the service sleeps."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


class FakeWinget:
    """Stands in for winget: records the arguments and answers `result` (never runs winget)."""

    def __init__(self, result: bool) -> None:
        self.result = result
        self.calls: list[tuple[str, ...]] = []

    async def __call__(self, arguments: Sequence[str]) -> bool:
        self.calls.append(tuple(arguments))
        return self.result


def settings_service(**values: Any) -> SettingsService:
    return SettingsService(mock.Mock(spec=SettingsRepository), Settings(**values))


# --- config.xml -------------------------------------------------------------------------------


class ConfigParsingTests(unittest.TestCase):
    def test_config_as_prowlarr_generates_it(self) -> None:
        local = read_local_config(FIXTURES / "config.xml")
        self.assertEqual(local, LocalProwlarr(BASE, KEY))
        self.assertNotIn(KEY, repr(local))

    def test_rewritten_config_with_url_base(self) -> None:
        # Elements missing and reordered; BindAddress 127.0.0.1, Port 9697, UrlBase /prowlarr/.
        local = read_local_config(FIXTURES / "config_urlbase.xml")
        self.assertEqual(local, LocalProwlarr("http://127.0.0.1:9697/prowlarr", KEY))

    def test_the_base_url_is_a_valid_prowlarr_url_setting(self) -> None:
        for name in ("config.xml", "config_urlbase.xml"):
            local = read_local_config(FIXTURES / name)
            assert local is not None
            self.assertEqual(Settings(prowlarr_url=local.base_url).prowlarr_url, local.base_url)

    def test_malformed_xml(self) -> None:
        for text in ("", "   ", "<Config><ApiKey>x</Config>", "not xml", f"<Config>{KEY}"):
            with self.subTest(text=text):
                self.assertIsNone(parse_config(text))

    def test_root_must_be_config(self) -> None:
        self.assertIsNone(parse_config(f"<Settings><ApiKey>{KEY}</ApiKey></Settings>"))

    def test_doctype_is_rejected(self) -> None:
        entity = f'<!DOCTYPE Config [<!ENTITY k "{KEY}">]><Config><ApiKey>&k;</ApiKey></Config>'
        self.assertIsNone(parse_config(entity))
        plain = f"<!doctype Config><Config><ApiKey>{KEY}</ApiKey></Config>"
        self.assertIsNone(parse_config(plain))
        self.assertIsNotNone(parse_config(f"<Config><ApiKey>{KEY}</ApiKey></Config>"))

    def test_api_key_must_be_32_hex_digits(self) -> None:
        self.assertIsNone(parse_config("<Config><Port>9696</Port></Config>"))
        for key in ("", KEY[:31], KEY + "0", KEY[:31] + "g", "{" + KEY[:30] + "}", KEY[:16]):
            with self.subTest(key=key):
                self.assertIsNone(parse_config(config(ApiKey=key)))
        for key, expected in ((f"  {KEY}\r\n ", KEY), (KEY.upper(), KEY.upper())):
            with self.subTest(key=key):
                local = parse_config(config(ApiKey=key))
                assert local is not None
                self.assertEqual(local.api_key, expected)

    def test_port(self) -> None:
        cases = {"9696": 9696, " 9697 ": 9697, "": 9696, "1": 1, "65535": 65535}
        for text, port in cases.items():
            with self.subTest(port=text):
                self.assertEqual(
                    parse_config(config(Port=text)), LocalProwlarr(f"http://127.0.0.1:{port}", KEY)
                )
        self.assertEqual(parse_config(config()), LocalProwlarr(BASE, KEY))  # no <Port>: default
        for text in ("0", "65536", "99999", "-1", "+9696", "9_696", "abc", "96.96", "٩٦٩٦"):
            with self.subTest(port=text):
                self.assertIsNone(parse_config(config(Port=text)))

    def test_bind_address(self) -> None:
        cases = {
            "*": "127.0.0.1",
            "": "127.0.0.1",
            "0.0.0.0": "127.0.0.1",
            "::": "127.0.0.1",
            "[::]": "127.0.0.1",
            "0:0:0:0:0:0:0:0": "127.0.0.1",
            "127.0.0.1": "127.0.0.1",
            "localhost": "127.0.0.1",
            "LocalHost": "127.0.0.1",
            "prowlarr.lan": "127.0.0.1",
            " 192.168.1.20 ": "192.168.1.20",
            "::1": "[::1]",
            "[fe80::1]": "[fe80::1]",
            "fe80::1%12": "127.0.0.1",  # a scoped address cannot go in a URL as is
        }
        for bind, host in cases.items():
            with self.subTest(bind=bind):
                local = parse_config(config(BindAddress=bind, Port="9696"))
                self.assertEqual(local, LocalProwlarr(f"http://{host}:9696", KEY))

    def test_url_base(self) -> None:
        cases = {
            "/prowlarr/": "/prowlarr",
            "prowlarr": "/prowlarr",
            " /a/b/ ": "/a/b",
            "/": "",
            "": "",
        }
        for url_base, prefix in cases.items():
            with self.subTest(url_base=url_base):
                local = parse_config(config(UrlBase=url_base, Port="9696"))
                self.assertEqual(local, LocalProwlarr(f"{BASE}{prefix}", KEY))

    def test_ssl_settings_are_ignored(self) -> None:
        local = parse_config(config(EnableSsl="True", SslPort="6969", SslCertPath="C:\\c.pfx"))
        self.assertEqual(local, LocalProwlarr(BASE, KEY))

    def test_file_problems(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        folder = Path(tmp.name)
        self.assertIsNone(read_local_config(folder / "missing.xml"))
        self.assertIsNone(read_local_config(folder))  # a folder, not a file

        bom = folder / "bom.xml"
        bom.write_bytes(b"\xef\xbb\xbf" + config().encode())  # bytes: keep the CRLFs
        self.assertEqual(read_local_config(bom), LocalProwlarr(BASE, KEY))

        latin = folder / "latin.xml"
        latin.write_bytes(config(InstanceName="Pr\xf6wlarr").encode("latin-1"))
        self.assertIsNone(read_local_config(latin))

        big = folder / "big.xml"
        big.write_bytes(config(Padding="x" * 64 * 1024).encode())
        self.assertIsNone(read_local_config(big))
        just_fits = folder / "fits.xml"
        text = config(Padding="")
        just_fits.write_bytes(config(Padding="x" * (64 * 1024 - len(text))).encode())
        self.assertEqual(just_fits.stat().st_size, 64 * 1024)
        self.assertEqual(read_local_config(just_fits), LocalProwlarr(BASE, KEY))


# --- the service ------------------------------------------------------------------------------


class SetupTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.server = FakeProwlarr()
        self.time = FakeTime()

    def service(
        self,
        settings: SettingsService,
        *,
        config_path: Path = FIXTURES / "config.xml",
        winget: WingetRunner | None = None,
    ) -> ProwlarrSetupService:
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(self.server), timeout=httpx.Timeout(10.0, connect=5.0)
        )
        self.addAsyncCleanup(http.aclose)
        return ProwlarrSetupService(
            http,
            settings,
            config_path=config_path,
            sleep=self.time.sleep,
            clock=self.time.clock,
            winget=winget if winget is not None else FakeWinget(False),
        )

    async def run_setup(self, settings: SettingsService, **options: Any) -> ProwlarrSetupResult:
        return await self.service(settings, **options).run()

    def missing_config(self) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return Path(tmp.name) / "config.xml"

    def assert_secret_hygiene(self) -> None:
        for request in self.server.requests:
            self.assertNotIn("apikey", str(request.url).lower())
            if request.url.path == "/ping":
                self.assertNotIn("X-Api-Key", request.headers)
            else:
                self.assertEqual(request.headers["X-Api-Key"], KEY)


class DiscoveryTests(SetupTestCase):
    async def test_no_config_file_means_no_requests(self) -> None:
        settings = settings_service()
        result = await self.run_setup(settings, config_path=self.missing_config())
        self.assertEqual(result, ProwlarrSetupResult())
        self.assertEqual(self.server.requests, [])
        self.assertEqual(settings.current, Settings())

    async def test_unusable_config_means_no_requests(self) -> None:
        path = self.missing_config()
        path.write_bytes(config(ApiKey="not-a-key").encode())
        settings = settings_service()
        self.assertEqual(await self.run_setup(settings, config_path=path), ProwlarrSetupResult())
        self.assertEqual(self.server.requests, [])

    async def test_a_configured_prowlarr_is_left_alone(self) -> None:
        cases: dict[str, dict[str, str]] = {
            "nas": {"prowlarr_url": "http://nas.local:9696", "prowlarr_api_key": "nas-key"},
            "url only": {"prowlarr_url": BASE},
            "key only": {"prowlarr_api_key": KEY},
        }
        for case, values in cases.items():
            with self.subTest(case):
                settings = settings_service(**values)
                before = settings.current
                self.assertEqual(await self.run_setup(settings), ProwlarrSetupResult())
                self.assertEqual(self.server.requests, [])
                self.assertIs(settings.current, before)

    async def test_waits_for_prowlarr_to_answer(self) -> None:
        self.server.on(
            "GET",
            "/ping",
            httpx.ConnectError("refused"),
            httpx.Response(503),
            ok({"status": "starting"}),
            ok({"status": "OK"}),
        )
        repository = mock.Mock(spec=SettingsRepository)
        # The sites were added on an earlier launch.
        settings = SettingsService(repository, Settings(prowlarr_seeded_url=BASE))
        result = await self.run_setup(settings)
        self.assertEqual(result, ProwlarrSetupResult(connected=True))
        self.assertEqual(self.time.sleeps, [1.0, 1.0, 1.0])
        self.assertEqual(self.server.paths(), ["/ping"] * 4 + ["/api/v1/system/status"])
        self.assertEqual(settings.current.prowlarr_url, BASE)
        self.assertEqual(settings.current.prowlarr_api_key.get_secret_value(), KEY)
        repository.save.assert_awaited_once()  # url and key saved in one update
        self.assertEqual(
            self.server.requests[0].extensions["timeout"],
            {"connect": 3.0, "read": 5.0, "write": 5.0, "pool": 5.0},
        )
        self.assert_secret_hygiene()

    async def test_gives_up_after_a_minute(self) -> None:
        self.server.on("GET", "/ping", httpx.ConnectError("refused"))
        settings = settings_service()
        with self.assertLogs(LOGGER, "INFO") as logs:
            self.assertEqual(await self.run_setup(settings), ProwlarrSetupResult())
        self.assertEqual(self.time.now, 60.0)
        self.assertEqual(self.server.paths(), ["/ping"] * 61)
        self.assertEqual(settings.current, Settings())
        self.assertIn("did not answer", "\n".join(logs.output))

    async def test_a_rejected_key_saves_nothing(self) -> None:
        self.server.on("GET", "/api/v1/system/status", httpx.Response(401))
        settings = settings_service()
        self.assertEqual(await self.run_setup(settings), ProwlarrSetupResult())
        self.assertEqual(self.server.paths(), ["/ping", "/api/v1/system/status"])
        self.assertEqual(settings.current, Settings())

    async def test_something_else_than_prowlarr_saves_nothing(self) -> None:
        replies: list[Reply] = [
            ok({"appName": "Sonarr", "version": "4.0"}),
            ok(["not", "a", "status"]),
            httpx.Response(200, text="<html>"),
            httpx.Response(307, headers={"Location": "/prowlarr/api/v1/system/status"}),
            httpx.ReadTimeout("slow"),
        ]
        for reply in replies:
            with self.subTest(reply=reply):
                self.server.on("GET", "/api/v1/system/status", reply)
                settings = settings_service()
                with self.assertLogs(LOGGER, "INFO"):
                    self.assertEqual(await self.run_setup(settings), ProwlarrSetupResult())
                self.assertEqual(settings.current, Settings())
                self.assertNotIn("/api/v1/indexer", self.server.paths())

    async def test_settings_entered_meanwhile_win(self) -> None:
        settings = settings_service()

        async def user_saves_settings(request: httpx.Request) -> httpx.Response:
            await settings.update(prowlarr_url="http://nas.local:9696", prowlarr_api_key="nas")
            return ok(STATUS)

        self.server.on("GET", "/api/v1/system/status", user_saves_settings)
        self.assertEqual(await self.run_setup(settings), ProwlarrSetupResult())
        self.assertEqual(settings.current.prowlarr_url, "http://nas.local:9696")
        self.assertEqual(settings.current.prowlarr_api_key.get_secret_value(), "nas")
        self.assertEqual(settings.current.prowlarr_seeded_url, "")

    async def test_connects_and_adds_the_sites(self) -> None:
        settings = settings_service()
        result = await self.run_setup(settings)
        self.assertEqual(result, ProwlarrSetupResult(connected=True, added=CURATED_NAMES))
        self.assertEqual(
            self.server.paths(),
            [
                "/ping",
                "/api/v1/system/status",
                "/api/v1/appprofile",
                "/api/v1/indexer",
                "/api/v1/indexer/schema",
                *["/api/v1/indexer"] * 5,
            ],
        )
        current = settings.current
        self.assertEqual(
            (current.prowlarr_url, current.prowlarr_api_key.get_secret_value()), (BASE, KEY)
        )
        self.assertEqual(current.prowlarr_seeded_url, BASE)
        self.assert_secret_hygiene()

    async def test_url_base_is_kept(self) -> None:
        self.server.prefix = "/prowlarr"
        settings = settings_service(prowlarr_seeded_url="http://127.0.0.1:9697/prowlarr")
        result = await self.run_setup(settings, config_path=FIXTURES / "config_urlbase.xml")
        self.assertEqual(result, ProwlarrSetupResult(connected=True))
        self.assertEqual(
            [str(r.url) for r in self.server.requests],
            [
                "http://127.0.0.1:9697/prowlarr/ping",
                "http://127.0.0.1:9697/prowlarr/api/v1/system/status",
            ],
        )
        self.assertEqual(settings.current.prowlarr_url, "http://127.0.0.1:9697/prowlarr")


class SeedingTests(SetupTestCase):
    def configured(self, **values: Any) -> SettingsService:
        """Settings pointing at the Prowlarr on this PC (as if discovered on an earlier launch)."""
        values.setdefault("prowlarr_url", BASE)
        values.setdefault("prowlarr_api_key", KEY)
        return settings_service(**values)

    def schema_entry(self, definition_name: str) -> dict[str, Any]:
        [entry] = [e for e in load("indexer_schema.json") if e["definitionName"] == definition_name]
        return dict(entry)

    async def test_adds_the_curated_sites_in_order(self) -> None:
        settings = self.configured()
        result = await self.run_setup(settings)
        self.assertEqual(result, ProwlarrSetupResult(added=CURATED_NAMES))
        self.assertEqual(settings.current.prowlarr_seeded_url, BASE)
        self.assertEqual(
            self.server.paths("GET"),
            [
                "/api/v1/system/status",
                "/api/v1/appprofile",
                "/api/v1/indexer",
                "/api/v1/indexer/schema",
            ],
        )
        posted = self.server.posted()
        self.assertEqual([(p["definitionName"], p["priority"]) for p in posted], list(CURATED))
        self.assertEqual(
            posted[0],
            {
                "name": "Knaben",
                "definitionName": "Knaben",
                "implementation": "Knaben",
                "configContract": "NoAuthTorrentBaseSettings",
                "protocol": "torrent",
                "enable": True,
                "appProfileId": 1,
                "priority": 10,
                "tags": [],
                "fields": [
                    {"name": "baseUrl", "value": None},
                    {"name": "baseSettings.queryLimit", "value": None},
                    {"name": "baseSettings.grabLimit", "value": None},
                    {"name": "baseSettings.limitsUnit", "value": 0},
                    {"name": "torrentBaseSettings.appMinimumSeeders", "value": None},
                    {"name": "torrentBaseSettings.seedRatio", "value": None},
                    {"name": "torrentBaseSettings.seedTime", "value": None},
                    {"name": "torrentBaseSettings.packSeedTime", "value": None},
                    {"name": "torrentBaseSettings.preferMagnetUrl", "value": False},
                ],
            },
        )
        lime = posted[1]
        self.assertEqual(
            {k: v for k, v in lime.items() if k != "fields"},
            {
                "name": "LimeTorrents",
                "definitionName": "limetorrents",
                "implementation": "Cardigann",
                "configContract": "CardigannSettings",
                "protocol": "torrent",
                "enable": True,
                "appProfileId": 1,
                "priority": 15,
                "tags": [],
            },
        )
        lime_fields = {f["name"]: f["value"] for f in lime["fields"]}
        self.assertEqual(lime_fields["definitionFile"], "limetorrents")
        self.assertIsNone(lime_fields["baseUrl"])
        self.assertEqual(lime_fields["primarydownloadlink"], 1)
        # Every field of the definition but the info texts, with the schema's values.
        for payload in posted:
            entry = self.schema_entry(payload["definitionName"])
            expected = [
                {"name": f["name"], "value": f.get("value")}
                for f in entry["fields"]
                if f.get("type") != "info"
            ]
            self.assertEqual(payload["fields"], expected)
            self.assertFalse(any(f["name"].startswith("info_") for f in payload["fields"]))
        self.assert_secret_hygiene()

    async def test_timeouts(self) -> None:
        await self.run_setup(self.configured())
        timeouts = {
            (r.method, r.url.path): r.extensions["timeout"]["read"] for r in self.server.requests
        }
        self.assertEqual(
            timeouts,
            {
                ("GET", "/api/v1/system/status"): 10.0,
                ("GET", "/api/v1/appprofile"): 10.0,
                ("GET", "/api/v1/indexer"): 10.0,
                ("GET", "/api/v1/indexer/schema"): 60.0,
                ("POST", "/api/v1/indexer"): 90.0,
            },
        )

    async def test_sites_already_there_are_skipped(self) -> None:
        self.server.on(
            "GET",
            "/api/v1/indexer",
            ok(
                [
                    {"id": 1, "name": "Knaben", "definitionName": "Knaben"},
                    # The same name from another definition: Prowlarr wants unique names.
                    {"id": 2, "name": "limeTORRENTS", "definitionName": "limetorrents-clone"},
                    # The same definition under the user's own name.
                    {"id": 3, "name": "My TD", "definitionName": "TorrentDownload"},
                    {"id": 4, "name": "Odd", "definitionName": None},
                    "garbage",
                ]
            ),
        )
        settings = self.configured()
        result = await self.run_setup(settings)
        self.assertEqual(result, ProwlarrSetupResult(added=("showRSS", "Nyaa.si")))
        self.assertEqual([p["name"] for p in self.server.posted()], ["showRSS", "Nyaa.si"])
        self.assertEqual(settings.current.prowlarr_seeded_url, BASE)

    async def test_nothing_to_add_completes(self) -> None:
        self.server.on("GET", "/api/v1/indexer", ok(load("indexer_list.json")))
        settings = self.configured()
        self.assertEqual(await self.run_setup(settings), ProwlarrSetupResult())
        self.assertEqual(self.server.posted(), [])
        self.assertEqual(settings.current.prowlarr_seeded_url, BASE)

    async def test_sites_failing_their_test_are_skipped(self) -> None:
        refusals = {
            "LimeTorrents": load("indexer_post_failed_cloudflare.json"),
            "TorrentDownload": load("indexer_post_failed_unreachable.json"),
            "showRSS": load("indexer_post_duplicate_name.json"),
            "Nyaa.si": load("indexer_post_invalid_profile.json"),
        }

        def post(request: httpx.Request) -> httpx.Response:
            name = body(request)["name"]
            return ok(refusals[name], 400) if name in refusals else created(request)

        self.server.on("POST", "/api/v1/indexer", post)
        settings = self.configured()
        with self.assertLogs(LOGGER, "INFO") as logs:
            result = await self.run_setup(settings)
        self.assertEqual(result, ProwlarrSetupResult(added=("Knaben",)))
        self.assertEqual(len(self.server.posted()), 5)
        self.assertEqual(settings.current.prowlarr_seeded_url, BASE)  # final: not retried
        output = "\n".join(logs.output)
        self.assertIn("LimeTorrents not added (its test was blocked by Cloudflare)", output)
        self.assertIn("TorrentDownload not added (its test failed)", output)
        self.assertIn("showRSS not added (rejected: PredicateValidator on Name)", output)
        self.assertIn(
            "Nyaa.si not added (rejected: PredicateValidator on Name; "
            "GreaterThanValidator on AppProfileId)",
            output,
        )
        for text in ("http", "moet", "Unable", "Should be unique", "1337x"):
            self.assertNotIn(text, output)

    async def test_prowlarr_failures_leave_the_run_unfinished(self) -> None:
        failures: dict[str, Reply] = {
            "401": httpx.Response(401),
            "503": httpx.Response(503),
            "404": httpx.Response(404),
            "connect": httpx.ConnectError("refused"),
            "timeout": httpx.ReadTimeout("slow"),
        }
        for case, failure in failures.items():
            with self.subTest(case):
                self.server = FakeProwlarr()
                self.server.on("POST", "/api/v1/indexer", created, failure)
                settings = self.configured()
                with self.assertLogs(LOGGER, "INFO") as logs:
                    result = await self.run_setup(settings)
                self.assertEqual(result, ProwlarrSetupResult(added=("Knaben",)))
                self.assertEqual(len(self.server.posted()), 2)  # stopped at the failure
                self.assertEqual(settings.current.prowlarr_seeded_url, "")
                self.assertIn("trying again next launch", "\n".join(logs.output))

    async def test_failing_reads_leave_the_run_unfinished(self) -> None:
        for path in ("/api/v1/appprofile", "/api/v1/indexer", "/api/v1/indexer/schema"):
            for failure in (httpx.Response(500), httpx.ReadTimeout("slow"), ok({"not": "a list"})):
                with self.subTest(path=path, failure=failure):
                    self.server = FakeProwlarr()
                    self.server.on("GET", path, failure)
                    settings = self.configured()
                    with self.assertLogs(LOGGER, "WARNING"):
                        self.assertEqual(await self.run_setup(settings), ProwlarrSetupResult())
                    self.assertEqual(self.server.posted(), [])
                    self.assertEqual(settings.current.prowlarr_seeded_url, "")

    async def test_site_definitions_not_downloaded_yet(self) -> None:
        native = [e for e in load("indexer_schema.json") if e["implementation"] != "Cardigann"]
        self.server.on("GET", "/api/v1/indexer/schema", ok(native))
        settings = self.configured()
        with self.assertLogs(LOGGER, "INFO") as logs:
            result = await self.run_setup(settings)
        self.assertEqual(result, ProwlarrSetupResult(added=("Knaben",)))
        self.assertEqual([p["name"] for p in self.server.posted()], ["Knaben"])
        self.assertEqual(settings.current.prowlarr_seeded_url, "")  # tried again next launch
        self.assertIn("not downloaded", "\n".join(logs.output))

    async def test_a_site_prowlarr_no_longer_offers(self) -> None:
        schema = [e for e in load("indexer_schema.json") if e["definitionName"] != "showrss"]
        self.server.on("GET", "/api/v1/indexer/schema", ok(schema))
        settings = self.configured()
        result = await self.run_setup(settings)
        added = ("Knaben", "LimeTorrents", "TorrentDownload", "Nyaa.si")
        self.assertEqual(result, ProwlarrSetupResult(added=added))
        self.assertEqual(settings.current.prowlarr_seeded_url, BASE)

    async def test_unsuitable_sites_are_skipped(self) -> None:
        schema = load("indexer_schema.json")
        for entry in schema:
            if entry["definitionName"] == "showrss":  # "Other" only: never asked by AirGrabber
                entry["capabilities"]["categories"] = [{"id": 8000, "name": "Other"}]
            if entry["definitionName"] == "nyaasi":
                entry["privacy"] = "private"
            if entry["definitionName"] == "torrentdownload":
                entry["protocol"] = "usenet"
        self.server.on("GET", "/api/v1/indexer/schema", ok(schema))
        settings = self.configured()
        result = await self.run_setup(settings)
        self.assertEqual(result, ProwlarrSetupResult(added=("Knaben", "LimeTorrents")))
        self.assertEqual([p["name"] for p in self.server.posted()], ["Knaben", "LimeTorrents"])
        self.assertEqual(settings.current.prowlarr_seeded_url, BASE)

    async def test_category_lookup_uses_top_level_ids(self) -> None:
        schema = load("indexer_schema.json")
        for entry in schema:
            if entry["definitionName"] == "Knaben":
                entry["capabilities"]["categories"] = [
                    {"id": 8000, "name": "Other", "subCategories": [{"id": 5040, "name": "HD"}]},
                    {"id": 5000, "name": "TV"},
                ]
        self.server.on("GET", "/api/v1/indexer/schema", ok(schema))
        result = await self.run_setup(self.configured())
        self.assertEqual(result.added, CURATED_NAMES)

    async def test_already_done_means_no_requests(self) -> None:
        settings = self.configured(prowlarr_seeded_url=BASE)
        self.assertEqual(await self.run_setup(settings), ProwlarrSetupResult())
        self.assertEqual(self.server.requests, [])

    async def test_a_prowlarr_elsewhere_is_never_seeded(self) -> None:
        for url in ("http://nas.local:9696", "http://localhost:9696", "http://127.0.0.1:9697"):
            with self.subTest(url=url):
                settings = self.configured(prowlarr_url=url)
                self.assertEqual(await self.run_setup(settings), ProwlarrSetupResult())
                self.assertEqual(self.server.requests, [])
                self.assertEqual(settings.current.prowlarr_seeded_url, "")

    async def test_no_local_config_means_no_seeding(self) -> None:
        settings = self.configured()
        result = await self.run_setup(settings, config_path=self.missing_config())
        self.assertEqual(result, ProwlarrSetupResult())
        self.assertEqual(self.server.requests, [])

    async def test_a_stored_key_that_fails_means_no_seeding(self) -> None:
        self.server.on("GET", "/api/v1/system/status", httpx.Response(401))
        settings = self.configured(prowlarr_api_key="ffffffffffffffffffffffffffffffff")
        self.assertEqual(await self.run_setup(settings), ProwlarrSetupResult())
        self.assertEqual(self.server.paths(), ["/api/v1/system/status"])
        self.assertEqual(settings.current.prowlarr_seeded_url, "")

    async def test_the_stored_key_is_used(self) -> None:
        other = "ffffffffffffffffffffffffffffffff"  # e.g. an environment override; it works
        await self.run_setup(self.configured(prowlarr_api_key=other))
        self.assertEqual({r.headers["X-Api-Key"] for r in self.server.requests}, {other})

    async def test_uses_the_lowest_app_profile(self) -> None:
        profiles = [{"name": "B", "id": 3}, {"name": "A", "id": 2}, {"name": "bad", "id": 0}]
        self.server.on("GET", "/api/v1/appprofile", ok(profiles))
        await self.run_setup(self.configured())
        self.assertEqual({p["appProfileId"] for p in self.server.posted()}, {2})

    async def test_creates_an_app_profile_when_there_is_none(self) -> None:
        self.server.on("GET", "/api/v1/appprofile", ok([]))
        self.server.on(
            "POST", "/api/v1/appprofile", lambda request: ok({**body(request), "id": 7}, 201)
        )
        settings = self.configured()
        result = await self.run_setup(settings)
        self.assertEqual(result.added, CURATED_NAMES)
        [create] = self.server.posted("/api/v1/appprofile")
        self.assertEqual(
            create,
            {
                "name": "AirGrabber",
                "enableRss": True,
                "enableAutomaticSearch": True,
                "enableInteractiveSearch": True,
                "minimumSeeders": 1,
            },
        )
        self.assertEqual({p["appProfileId"] for p in self.server.posted()}, {7})
        self.assertEqual(settings.current.prowlarr_seeded_url, BASE)

    async def test_app_profile_problems_stop_the_run(self) -> None:
        cases: dict[str, tuple[Reply, Reply | None]] = {
            "unreadable profiles": (ok([{"name": "no id"}]), None),
            "create refused": (ok([]), httpx.Response(400)),
            "create answer without id": (ok([]), ok({"name": "AirGrabber"}, 201)),
        }
        for case, (listed, create) in cases.items():
            with self.subTest(case):
                self.server = FakeProwlarr()
                self.server.on("GET", "/api/v1/appprofile", listed)
                if create is not None:
                    self.server.on("POST", "/api/v1/appprofile", create)
                settings = self.configured()
                with self.assertLogs(LOGGER, "WARNING"):
                    self.assertEqual(await self.run_setup(settings), ProwlarrSetupResult())
                self.assertEqual(self.server.posted(), [])
                self.assertEqual(settings.current.prowlarr_seeded_url, "")

    async def test_logs_never_show_the_key_or_urls(self) -> None:
        refusal = ok(load("indexer_post_failed_unreachable.json"), 400)
        leaky = httpx.ConnectError(f"{BASE}/api/v1/indexer?apikey={KEY}")
        self.server.on("POST", "/api/v1/indexer", created, refusal, leaky)
        root = logging.getLogger("app")
        with self.assertLogs(root, "DEBUG") as logs:
            await self.run_setup(settings_service())
        output = "\n".join(logs.output)
        self.assertIn("Prowlarr setup", output)
        for secret in (KEY, "http", "apikey", "127.0.0.1"):
            self.assertNotIn(secret, output)

    async def test_marker_survives_saving_the_settings_dialog(self) -> None:
        settings = self.configured(prowlarr_seeded_url=BASE)
        await settings.update(
            quality_tv="720p",
            prowlarr_url="http://127.0.0.1:9696/",
            prowlarr_api_key=KEY,
            remote_url="https://nas.local:5001",
        )
        self.assertEqual(settings.current.prowlarr_seeded_url, BASE)
        stored = settings.current.model_dump(mode="json")
        self.assertEqual(Settings.model_validate(stored).prowlarr_seeded_url, BASE)

    async def test_runs_one_at_a_time(self) -> None:
        settings = self.configured()
        service = self.service(settings)
        first, second = await asyncio.gather(service.run(), service.run())
        self.assertEqual(first.added, CURATED_NAMES)
        self.assertEqual(second, ProwlarrSetupResult())  # saw the marker the first one set
        self.assertEqual(len(self.server.posted()), 5)


class OnThisPcTests(unittest.TestCase):
    def test_loopback_addresses(self) -> None:
        for url in (
            "http://localhost:9696",
            "http://LocalHost:9696/prowlarr",
            "http://127.0.0.1:9696",
            "http://127.0.0.2:1",
            "http://[::1]:9696",
            " http://127.0.0.1:9696/ ",
        ):
            with self.subTest(url=url):
                self.assertTrue(on_this_pc(url))

    def test_other_computers(self) -> None:
        for url in ("http://nas.local:9696", "http://192.168.1.20:9696", "", "not a url", "http://"):
            with self.subTest(url=url):
                self.assertFalse(on_this_pc(url))

    def test_the_address_in_config_xml(self) -> None:
        local = LocalProwlarr("http://192.168.1.20:9696", KEY)
        self.assertTrue(on_this_pc("http://192.168.1.20:9696", local))
        self.assertTrue(on_this_pc("http://192.168.1.20:9696/", local))
        self.assertFalse(on_this_pc("http://192.168.1.21:9696", local))


class StatusTests(SetupTestCase):
    def configured(self, **values: Any) -> SettingsService:
        values.setdefault("prowlarr_url", BASE)
        values.setdefault("prowlarr_api_key", KEY)
        values.setdefault("prowlarr_seeded_url", BASE)
        return settings_service(**values)

    async def status(self, settings: SettingsService, **options: Any) -> ProwlarrStatus:
        return await self.service(settings, **options).status()

    async def test_not_installed(self) -> None:
        settings = settings_service()
        status = await self.status(settings, config_path=self.missing_config())
        self.assertEqual(status, ProwlarrStatus(state=ProwlarrState.NOT_INSTALLED))
        self.assertEqual(self.server.requests, [])

    async def test_installed_but_not_answering(self) -> None:
        for failure in (httpx.ConnectError("refused"), httpx.Response(503)):
            with self.subTest(failure=failure):
                self.server = FakeProwlarr()
                self.server.on("GET", "/ping", failure)
                settings = settings_service()
                status = await self.status(settings)
                self.assertEqual(
                    status, ProwlarrStatus(state=ProwlarrState.NOT_RUNNING, address=BASE)
                )
                self.assertEqual(self.server.paths(), ["/ping"])  # one look, no waiting
                self.assertEqual(self.time.sleeps, [])
                self.assertEqual(settings.current, Settings())

    async def test_the_connected_prowlarr_on_this_pc_stopped_answering(self) -> None:
        self.server.on("GET", "/ping", httpx.ConnectError("refused"))
        self.server.on("GET", "/api/v1/system/status", httpx.ConnectError("refused"))
        with self.assertLogs(LOGGER, "WARNING"):
            status = await self.status(self.configured())
        self.assertEqual(status, ProwlarrStatus(state=ProwlarrState.NOT_RUNNING, address=BASE))
        self.assertEqual(self.server.paths(), ["/api/v1/system/status", "/ping"])

    async def test_connected_on_this_pc_counts_its_sites(self) -> None:
        self.server.on("GET", "/api/v1/indexer", ok(load("indexer_list.json")))
        status = await self.status(self.configured())
        self.assertEqual(
            status, ProwlarrStatus(state=ProwlarrState.CONNECTED_LOCAL, sites=5, address=BASE)
        )
        self.assertEqual(self.server.paths(), ["/api/v1/system/status", "/api/v1/indexer"])
        self.assertEqual(self.server.posted(), [])
        self.assert_secret_hygiene()

    async def test_only_switched_on_sites_count(self) -> None:
        sites = load("indexer_list.json")
        sites[0]["enable"] = False
        del sites[1]["enable"]
        sites[2]["enable"] = "maybe"
        self.server.on("GET", "/api/v1/indexer", ok([*sites, "garbage"]))
        status = await self.status(self.configured())
        self.assertEqual(status.sites, 2)

    async def test_sites_unknown_when_the_list_fails(self) -> None:
        self.server.on("GET", "/api/v1/indexer", httpx.Response(500))
        with self.assertLogs(LOGGER, "WARNING"):
            status = await self.status(self.configured())
        self.assertEqual(
            status, ProwlarrStatus(state=ProwlarrState.CONNECTED_LOCAL, sites=None, address=BASE)
        )

    async def test_a_loopback_address_is_this_pc(self) -> None:
        url = "http://localhost:9696"
        settings = self.configured(prowlarr_url=url)
        status = await self.status(settings, config_path=self.missing_config())
        self.assertEqual(
            status, ProwlarrStatus(state=ProwlarrState.CONNECTED_LOCAL, sites=0, address=url)
        )

    async def test_connected_elsewhere(self) -> None:
        url = "http://nas.local:9696"
        status = await self.status(self.configured(prowlarr_url=url, prowlarr_api_key="nas"))
        self.assertEqual(status, ProwlarrStatus(state=ProwlarrState.CONNECTED_REMOTE, address=url))
        self.assertEqual([str(r.url) for r in self.server.requests], [f"{url}/api/v1/system/status"])
        self.assertEqual(self.server.requests[0].headers["X-Api-Key"], "nas")

    async def test_not_connected(self) -> None:
        nas = "http://nas.local:9696"
        cases: dict[str, tuple[dict[str, str], Path | None, str]] = {
            "key rejected here": ({}, None, BASE),
            "key rejected elsewhere": ({"prowlarr_url": nas}, None, BASE),
            "elsewhere, none here": ({"prowlarr_url": nas}, self.missing_config(), ""),
            "url only": ({"prowlarr_api_key": ""}, None, BASE),
            "key only": ({"prowlarr_url": ""}, self.missing_config(), ""),
        }
        for case, (values, config_path, address) in cases.items():
            with self.subTest(case):
                self.server = FakeProwlarr()
                self.server.on("GET", "/api/v1/system/status", httpx.Response(401))
                settings = self.configured(**values)
                before = settings.current
                options = {} if config_path is None else {"config_path": config_path}
                status = await self.status(settings, **options)
                self.assertEqual(
                    status, ProwlarrStatus(state=ProwlarrState.NOT_CONNECTED, address=address)
                )
                self.assertIs(settings.current, before)
                self.assertNotIn("/api/v1/indexer", self.server.paths())

    async def test_connects_a_prowlarr_started_since_launch(self) -> None:
        self.server.on(
            "GET",
            "/api/v1/indexer",
            ok(load("indexer_list_empty.json")),  # before adding the sites
            ok(load("indexer_list.json")),
        )
        settings = settings_service()
        status = await self.status(settings)
        self.assertEqual(
            status, ProwlarrStatus(state=ProwlarrState.CONNECTED_LOCAL, sites=5, address=BASE)
        )
        current = settings.current
        self.assertEqual(
            (current.prowlarr_url, current.prowlarr_api_key.get_secret_value()), (BASE, KEY)
        )
        self.assertEqual(current.prowlarr_seeded_url, BASE)
        self.assertEqual(len(self.server.posted()), 5)
        self.assert_secret_hygiene()

    async def test_a_prowlarr_here_with_a_rejected_key_is_not_connected(self) -> None:
        self.server.on("GET", "/api/v1/system/status", httpx.Response(401))
        settings = settings_service()
        status = await self.status(settings)
        self.assertEqual(status, ProwlarrStatus(state=ProwlarrState.NOT_CONNECTED, address=BASE))
        self.assertEqual(settings.current, Settings())

    async def test_logs_never_show_the_key_or_urls(self) -> None:
        self.server.on("GET", "/api/v1/system/status", httpx.ConnectError(f"{BASE}?apikey={KEY}"))
        self.server.on("GET", "/ping", httpx.ConnectError(f"{BASE}/ping"))
        with self.assertLogs(logging.getLogger("app"), "DEBUG") as logs:
            await self.status(self.configured())
        output = "\n".join(logs.output)
        for secret in (KEY, "http", "apikey", "127.0.0.1"):
            self.assertNotIn(secret, output)


class InstallTests(SetupTestCase):
    async def asyncSetUp(self) -> None:
        # The test loop runs in debug mode, which logs every step over 0.1 s; 90 fake seconds of
        # polling (a file read in a thread each) can take longer than that on a busy machine.
        asyncio.get_running_loop().slow_callback_duration = 2.0

    async def install(
        self, settings: SettingsService, winget: FakeWinget, **options: Any
    ) -> tuple[bool, list[str]]:
        steps: list[str] = []
        installed = await self.service(settings, winget=winget, **options).install(steps.append)
        return installed, steps

    def test_the_winget_command(self) -> None:
        self.assertEqual(
            WINGET_COMMAND,
            (
                "install",
                "--id",
                "TeamProwlarr.Prowlarr",
                "--exact",
                "--silent",
                "--accept-package-agreements",
                "--accept-source-agreements",
            ),
        )

    async def test_installs_connects_and_adds_the_sites(self) -> None:
        winget = FakeWinget(True)
        settings = settings_service()
        installed, steps = await self.install(settings, winget)
        self.assertTrue(installed)
        self.assertEqual(winget.calls, [WINGET_COMMAND])
        self.assertEqual(
            steps, ["Installing… Windows may ask for permission", "Setting up Prowlarr…"]
        )
        current = settings.current
        self.assertEqual(
            (current.prowlarr_url, current.prowlarr_api_key.get_secret_value()), (BASE, KEY)
        )
        self.assertEqual(current.prowlarr_seeded_url, BASE)
        self.assertEqual(len(self.server.posted()), 5)
        self.assert_secret_hygiene()

    async def test_waits_for_the_new_prowlarr(self) -> None:
        path = self.missing_config()

        async def sleep(seconds: float) -> None:  # Prowlarr writes its config after 3 s
            await self.time.sleep(seconds)
            if self.time.now == 3.0:
                path.write_bytes((FIXTURES / "config.xml").read_bytes())

        http = httpx.AsyncClient(transport=httpx.MockTransport(self.server))
        self.addAsyncCleanup(http.aclose)
        settings = settings_service()
        service = ProwlarrSetupService(
            http,
            settings,
            config_path=path,
            sleep=sleep,
            clock=self.time.clock,
            winget=FakeWinget(True),
        )
        # ... and answers after 5 s.
        self.server.on("GET", "/ping", *[httpx.ConnectError("refused")] * 2, ok({"status": "OK"}))
        self.assertTrue(await service.install())
        self.assertEqual(self.time.now, 5.0)
        self.assertEqual(settings.current.prowlarr_url, BASE)
        self.assertEqual(settings.current.prowlarr_seeded_url, BASE)

    async def test_winget_failing(self) -> None:
        settings = settings_service()
        installed, steps = await self.install(settings, FakeWinget(False))
        self.assertFalse(installed)
        self.assertEqual(steps, ["Installing… Windows may ask for permission"])
        self.assertEqual(self.server.requests, [])
        self.assertEqual(settings.current, Settings())

    async def test_prowlarr_not_answering(self) -> None:
        self.server.on("GET", "/ping", httpx.ConnectError("refused"))
        settings = settings_service()
        service = self.service(settings, winget=FakeWinget(True))
        with self.assertLogs(LOGGER, "INFO") as logs:
            installed = await service.install()
        self.assertTrue(installed)  # installed; `status` tells it isn't answering
        self.assertEqual(self.time.now, 90.0)
        self.assertEqual(self.server.paths(), ["/ping"] * 91)
        self.assertEqual(settings.current, Settings())
        self.assertIn("did not answer within 90 s", "\n".join(logs.output))
        status = await service.status()
        self.assertEqual(status, ProwlarrStatus(state=ProwlarrState.NOT_RUNNING, address=BASE))

    async def test_no_prowlarr_config_appears(self) -> None:
        settings = settings_service()
        with self.assertLogs(LOGGER, "INFO"):
            installed, _steps = await self.install(
                settings, FakeWinget(True), config_path=self.missing_config()
            )
        self.assertFalse(installed)
        self.assertEqual(self.time.now, 90.0)
        self.assertEqual(self.server.requests, [])
        self.assertEqual(settings.current, Settings())


class WingetProcess:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, None]:
        return b"winget output", None

    def kill(self) -> None:
        pass


class RunWingetTests(unittest.IsolatedAsyncioTestCase):
    """run_winget with the process start patched out: winget never runs."""

    def patch_winget(self, returncode: int | None, path: str | None = r"C:\winget.exe") -> Any:
        self.enterContext(mock.patch.object(sys, "platform", "win32"))
        self.enterContext(mock.patch.object(shutil, "which", return_value=path))
        if returncode is None:
            spawn = mock.AsyncMock(side_effect=FileNotFoundError(2, "missing"))
        else:
            spawn = mock.AsyncMock(return_value=WingetProcess(returncode))
        self.enterContext(mock.patch.object(asyncio, "create_subprocess_exec", spawn))
        return spawn

    async def test_success(self) -> None:
        spawn = self.patch_winget(0)
        self.assertTrue(await run_winget(WINGET_COMMAND))
        args = spawn.await_args
        assert args is not None
        self.assertEqual(args.args, (r"C:\winget.exe", *WINGET_COMMAND))
        self.assertEqual(args.kwargs["creationflags"], getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(args.kwargs["stdin"], subprocess.DEVNULL)

    async def test_non_zero_exit_logs_the_code_only(self) -> None:
        self.patch_winget(-1978335212)
        with self.assertLogs(LOGGER, "INFO") as logs:
            self.assertFalse(await run_winget(WINGET_COMMAND))
        self.assertIn("-1978335212", "\n".join(logs.output))

    async def test_winget_missing(self) -> None:
        spawn = self.patch_winget(0, path=None)
        self.assertFalse(await run_winget(WINGET_COMMAND))
        spawn.assert_not_awaited()

    async def test_winget_cannot_start(self) -> None:
        self.patch_winget(None)
        self.assertFalse(await run_winget(WINGET_COMMAND))


if __name__ == "__main__":
    unittest.main()
