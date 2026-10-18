"""DownloadStationClient against a fake DSM Web API on httpx.MockTransport (magnets and
.torrent uploads)."""

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import unittest
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import parse_qsl

import httpx

from app.database import SettingsRepository
from app.models import Settings
from app.services.settings import SettingsService
from app.services.remote import RemoteError
from app.services.synology import DownloadStationClient, DownloadStationError
from tests.multipart import parse_multipart

FIXTURES = Path(__file__).parent / "fixtures" / "synology"
BASE = "http://192.168.1.20:5000"
HOST = "192.168.1.20:5000"
PASSWORD = "DSMPASS123"
SID = "SIDx7Kq93LmZp0"
MAGNET = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Some.Show.S01E01"
TORRENT = b"d8:announce30:udp://tracker.example.org:13374:infod4:name4:showee"

AUTH = "SYNO.API.Auth"
TASK2 = "SYNO.DownloadStation2.Task"
TASK = "SYNO.DownloadStation.Task"
DS_INFO = "SYNO.DownloadStation.Info"
DSM_INFO = "SYNO.DSM.Info"

# A DSM 6 box with Download Station 3.x but without the DownloadStation2 APIs.
DSM6_INFO: dict[str, Any] = {
    "data": {
        AUTH: {"maxVersion": 6, "minVersion": 1, "path": "auth.cgi"},
        DS_INFO: {"maxVersion": 2, "minVersion": 1, "path": "DownloadStation/info.cgi"},
        TASK: {"maxVersion": 3, "minVersion": 1, "path": "DownloadStation/task.cgi"},
    },
    "success": True,
}


def api_info() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / "api_info.json").read_text(encoding="utf-8"))
    return data


def ok(data: Any = None) -> httpx.Response:
    body: dict[str, Any] = {"success": True}
    if data is not None:
        body["data"] = data
    return httpx.Response(200, json=body)


def failure(code: int, *, nested: bool = True) -> httpx.Response:
    error: Any = {"code": code} if nested else code
    return httpx.Response(200, json={"success": False, "error": error})


class FakeDSM:
    """Answers query.cgi from `info` and records every API call as (api, method, form)."""

    def __init__(self) -> None:
        self.info: Any = api_info()
        self.requests: list[httpx.Request] = []
        self.calls: list[tuple[str, str, dict[str, str]]] = []
        self.files: list[dict[str, tuple[str, bytes]]] = []  # the file parts of each call
        self.overrides: dict[tuple[str, str], Callable[[], httpx.Response]] = {}
        self.gates: dict[tuple[str, str], asyncio.Event] = {}
        self.ds_info: dict[str, Any] = {"version": 4382, "version_string": "3.9.2-4382"}
        self.dsm_info: dict[str, Any] = {"version_string": "DSM 7.2.1-69057 Update 5"}

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/webapi/query.cgi":
            return httpx.Response(200, json=self.info)
        if request.headers.get("Content-Type", "").startswith("multipart/form-data"):
            form, files = parse_multipart(request)
        else:
            form, files = dict(parse_qsl(request.content.decode())), {}
        self.files.append(files)
        key = (form.get("api", ""), form.get("method", ""))
        self.calls.append((key[0], key[1], form))
        if (gate := self.gates.get(key)) is not None:
            await gate.wait()
        if (override := self.overrides.get(key)) is not None:
            return override()
        if key == (AUTH, "login"):
            return ok({"sid": SID, "did": "device"})
        if key in ((TASK2, "create"), (TASK, "create")):
            return ok({"list_id": [], "task_id": ["dbid_1"]})
        if key == (DS_INFO, "getinfo"):
            return ok(self.ds_info)
        if key == (DSM_INFO, "getinfo"):
            return ok(self.dsm_info)
        if key == (AUTH, "logout"):
            return ok()
        return failure(103)

    def methods(self) -> list[tuple[str, str]]:
        return [(api, method) for api, method, _ in self.calls]

    def form(self, api: str, method: str) -> dict[str, str]:
        return next(form for name, verb, form in self.calls if (name, verb) == (api, method))


def settings_service(**changes: Any) -> SettingsService:
    values: dict[str, Any] = {
        "remote_url": BASE,
        "remote_username": "beau",
        "remote_password": PASSWORD,
    }
    values.update(changes)
    return SettingsService(mock.Mock(spec=SettingsRepository), Settings(**values))


class SynologyTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.dsm = FakeDSM()
        self.settings = settings_service()
        self.client = self.make_client(self.dsm)

    def make_client(self, handler: Callable[[httpx.Request], Any]) -> DownloadStationClient:
        return DownloadStationClient(self.settings, transport=httpx.MockTransport(handler))

    async def configure(self, **changes: Any) -> None:
        await self.settings.update(**changes)

    async def error_of(self, operation: Awaitable[object]) -> DownloadStationError:
        with self.assertRaises(DownloadStationError) as caught:
            await operation
        return caught.exception


class DiscoveryAndTaskTests(SynologyTestCase):
    async def test_dsm_with_downloadstation2_creates_a_json_encoded_task(self) -> None:
        await self.configure(remote_destination="video/tv")
        await self.client.add_magnet(MAGNET)
        self.assertEqual(
            self.dsm.methods(), [(AUTH, "login"), (TASK2, "create"), (AUTH, "logout")]
        )
        create = self.dsm.form(TASK2, "create")
        self.assertEqual(create["version"], "2")
        self.assertEqual(create["type"], '"url"')
        self.assertEqual(json.loads(create["url"]), [MAGNET])
        self.assertEqual(create["create_list"], "false")
        self.assertEqual(create["destination"], '"video/tv"')
        self.assertEqual(create["_sid"], SID)
        request = next(r for r in self.dsm.requests if r.url.path == "/webapi/entry.cgi")
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.url.query, b"")  # the magnet and the sid stay out of the URL

    async def test_discovery_asks_for_the_apis_it_needs(self) -> None:
        await self.client.add_magnet(MAGNET)
        query = self.dsm.requests[0]
        self.assertEqual(query.method, "GET")
        self.assertEqual(query.url.path, "/webapi/query.cgi")
        self.assertEqual(query.url.params["api"], "SYNO.API.Info")
        self.assertEqual(query.url.params["method"], "query")
        asked = set(query.url.params["query"].split(","))
        self.assertLessEqual({AUTH, TASK2, TASK, DS_INFO}, asked)

    async def test_without_a_destination_download_station_picks_its_default(self) -> None:
        await self.client.add_magnet(MAGNET)
        self.assertNotIn("destination", self.dsm.form(TASK2, "create"))

    async def test_dsm6_falls_back_to_the_old_task_api(self) -> None:
        self.dsm.info = DSM6_INFO
        await self.configure(remote_destination="downloads/tv")
        await self.client.add_magnet(MAGNET)
        self.assertEqual(
            self.dsm.methods(), [(AUTH, "login"), (TASK, "create"), (AUTH, "logout")]
        )
        create = self.dsm.form(TASK, "create")
        self.assertEqual(create["uri"], MAGNET)  # plain values: no requestFormat JSON here
        self.assertEqual(create["destination"], "downloads/tv")
        self.assertEqual(create["version"], "3")
        request = next(r for r in self.dsm.requests if r.url.path.endswith("task.cgi"))
        self.assertEqual(request.method, "POST")

    async def test_downloadstation2_without_version_2_falls_back_too(self) -> None:
        self.dsm.info["data"][TASK2]["maxVersion"] = 1
        await self.client.add_magnet(MAGNET)
        self.assertIn((TASK, "create"), self.dsm.methods())

    async def test_login_posts_the_password_and_keeps_it_out_of_the_url(self) -> None:
        await self.client.add_magnet(MAGNET)
        login = self.dsm.form(AUTH, "login")
        self.assertEqual(login["passwd"], PASSWORD)
        self.assertEqual(login["account"], "beau")
        self.assertEqual(login["session"], "DownloadStation")
        self.assertEqual(login["format"], "sid")
        self.assertEqual(login["version"], "6")  # the fixture's Auth maxVersion
        request = next(r for r in self.dsm.requests if r.url.path == "/webapi/auth.cgi")
        self.assertEqual(request.method, "POST")
        for sent in self.dsm.requests:
            self.assertNotIn(PASSWORD, str(sent.url))
            self.assertNotIn(SID, str(sent.url))

    async def test_json_request_format_applies_to_auth_too(self) -> None:
        self.dsm.info["data"][AUTH] = {
            "maxVersion": 7, "minVersion": 1, "path": "entry.cgi", "requestFormat": "JSON"
        }
        await self.client.add_magnet(MAGNET)
        login = self.dsm.form(AUTH, "login")
        self.assertEqual(login["passwd"], json.dumps(PASSWORD))
        self.assertEqual(login["version"], "6")

    async def test_logout_happens_after_a_failed_create(self) -> None:
        self.dsm.overrides[(TASK2, "create")] = lambda: failure(403)
        await self.error_of(self.client.add_magnet(MAGNET))
        self.assertEqual(self.dsm.methods()[-1], (AUTH, "logout"))
        self.assertEqual(self.dsm.form(AUTH, "logout")["_sid"], SID)

    async def test_logout_happens_when_the_operation_is_cancelled(self) -> None:
        gate = asyncio.Event()
        self.dsm.gates[(TASK2, "create")] = gate
        task = asyncio.create_task(self.client.add_magnet(MAGNET))
        while (TASK2, "create") not in self.dsm.methods():
            await asyncio.sleep(0.001)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.dsm.methods()[-1], (AUTH, "logout"))

    async def test_no_logout_when_login_fails(self) -> None:
        self.dsm.overrides[(AUTH, "login")] = lambda: failure(400)
        await self.error_of(self.client.add_magnet(MAGNET))
        self.assertEqual(self.dsm.methods(), [(AUTH, "login")])

    async def test_a_failing_logout_is_logged_and_does_not_hide_the_result(self) -> None:
        self.dsm.overrides[(AUTH, "logout")] = lambda: failure(105)
        with self.assertLogs("app.services.synology", "WARNING") as logs:
            await self.client.add_magnet(MAGNET)  # the task was created: no error
        self.assertIn("logout", logs.output[0])

    async def test_not_configured_makes_no_request(self) -> None:
        for missing in ("remote_url", "remote_username", "remote_password"):
            with self.subTest(missing=missing):
                self.settings = settings_service(**{missing: ""})
                client = self.make_client(self.dsm)
                self.assertFalse(client.configured)
                error = await self.error_of(client.add_magnet(MAGNET))
                self.assertIn("isn't set up", str(error))
        self.assertEqual(self.dsm.requests, [])
        self.assertTrue(self.client.configured)

    async def test_settings_are_read_on_every_call(self) -> None:
        await self.configure(remote_url="https://nas.local:5001")
        await self.client.add_magnet(MAGNET)
        self.assertTrue(all(r.url.host == "nas.local" for r in self.dsm.requests))

    async def test_rejects_non_magnet_links(self) -> None:
        with self.assertRaises(ValueError):
            await self.client.add_magnet("https://example.com/file.torrent")


class TorrentUploadTests(SynologyTestCase):
    def call_index(self, api: str, method: str) -> int:
        return next(i for i, (name, verb, _) in enumerate(self.dsm.calls) if (name, verb) == (api, method))

    async def test_downloadstation2_uploads_the_file_as_multipart(self) -> None:
        await self.configure(remote_destination="video/tv")
        await self.client.add_torrent(TORRENT, "Some Show S01E01.torrent")
        self.assertEqual(
            self.dsm.methods(), [(AUTH, "login"), (TASK2, "create"), (AUTH, "logout")]
        )
        create = self.dsm.form(TASK2, "create")
        self.assertEqual(create["api"], TASK2)
        self.assertEqual(create["version"], "2")
        self.assertEqual(create["method"], "create")
        self.assertEqual(create["type"], '"file"')
        self.assertEqual(json.loads(create["file"]), ["torrent"])
        self.assertEqual(create["destination"], '"video/tv"')
        self.assertEqual(create["create_list"], "false")
        self.assertEqual(create["_sid"], SID)
        files = self.dsm.files[self.call_index(TASK2, "create")]
        self.assertEqual(files, {"torrent": ("Some Show S01E01.torrent", TORRENT)})
        request = next(r for r in self.dsm.requests if r.url.path == "/webapi/entry.cgi")
        self.assertTrue(request.headers["Content-Type"].startswith("multipart/form-data"))
        self.assertEqual(request.url.query, b"")
        # api, version and method lead the body (DSM rejects them later on).
        body = request.content
        self.assertLess(body.index(b'name="api"'), body.index(b'name="type"'))
        self.assertLess(body.index(b'name="method"'), body.index(b'name="torrent"'))

    async def test_downloadstation2_without_a_destination_sends_an_empty_one(self) -> None:
        await self.client.add_torrent(TORRENT, "x.torrent")
        self.assertEqual(self.dsm.form(TASK2, "create")["destination"], '""')

    async def test_dsm6_falls_back_to_the_old_task_api(self) -> None:
        self.dsm.info = DSM6_INFO
        await self.configure(remote_destination="/downloads/tv/")
        await self.client.add_torrent(TORRENT, "x.torrent")
        self.assertEqual(
            self.dsm.methods(), [(AUTH, "login"), (TASK, "create"), (AUTH, "logout")]
        )
        create = self.dsm.form(TASK, "create")
        self.assertEqual(create["destination"], "downloads/tv")  # slashes stripped for DSM
        self.assertNotIn("uri", create)
        files = self.dsm.files[self.call_index(TASK, "create")]
        self.assertEqual(files, {"file": ("x.torrent", TORRENT)})
        request = next(r for r in self.dsm.requests if r.url.path.endswith("task.cgi"))
        body = request.content
        self.assertGreater(body.index(b'name="file"'), body.index(b'name="_sid"'))  # file last

    async def test_upload_errors_and_logout(self) -> None:
        self.dsm.overrides[(TASK2, "create")] = lambda: failure(403)
        error = await self.error_of(self.client.add_torrent(TORRENT, "x.torrent"))
        self.assertIn("its default folder", str(error))
        self.assertIsInstance(error, RemoteError)
        self.assertEqual(self.dsm.methods()[-1], (AUTH, "logout"))

    async def test_the_kind(self) -> None:
        self.assertEqual(self.client.kind.program, "Download Station")


class ConnectionTestTests(SynologyTestCase):
    async def test_reports_the_download_station_version(self) -> None:
        self.assertEqual(await self.client.test(), "Download Station 3.9.2")
        self.assertEqual(
            self.dsm.methods(), [(AUTH, "login"), (DS_INFO, "getinfo"), (AUTH, "logout")]
        )
        request = next(r for r in self.dsm.requests if r.url.path.endswith("info.cgi"))
        self.assertEqual(request.url.query, b"")

    async def test_adds_the_dsm_version_when_the_nas_offers_it(self) -> None:
        self.dsm.info["data"][DSM_INFO] = {"maxVersion": 2, "minVersion": 1, "path": "entry.cgi"}
        self.assertEqual(await self.client.test(), "Download Station 3.9.2 on DSM 7.2.1")

    async def test_unreadable_dsm_version_is_left_out(self) -> None:
        self.dsm.info["data"][DSM_INFO] = {"maxVersion": 2, "minVersion": 1, "path": "entry.cgi"}
        self.dsm.overrides[(DSM_INFO, "getinfo")] = lambda: failure(105)
        self.assertEqual(await self.client.test(), "Download Station 3.9.2")

    async def test_account_without_download_station_rights(self) -> None:
        self.dsm.overrides[(DS_INFO, "getinfo")] = lambda: failure(105)
        error = await self.error_of(self.client.test())
        self.assertIn("isn't allowed to use Download Station", str(error))
        self.assertEqual(self.dsm.methods()[-1], (AUTH, "logout"))


class ErrorMappingTests(SynologyTestCase):
    async def test_auth_errors(self) -> None:
        expected = {
            400: "rejected the user name or password",
            401: "account is disabled",
            402: "permission denied",
            403: "Use a DSM account without 2-step verification",
            404: "Use a DSM account without 2-step verification",
            406: "Use a DSM account without 2-step verification",
            407: "blocked this computer's IP address",
        }
        for code, text in expected.items():
            for nested in (True, False):
                with self.subTest(code=code, nested=nested):
                    self.dsm.overrides[(AUTH, "login")] = partial(failure, code, nested=nested)
                    error = await self.error_of(self.client.add_magnet(MAGNET))
                    self.assertIn(text, str(error))

    async def test_task_errors(self) -> None:
        await self.configure(remote_destination="video/tv")
        expected = {
            401: "maximum number of tasks",
            402: 'isn\'t allowed to save to "video/tv"',
            403: 'destination "video/tv" doesn\'t exist',
            406: "Set one in Download Station or in AirGrabber's settings",
            105: "isn't allowed to use Download Station",
            999: "Download Station reported error 999",
        }
        for code, text in expected.items():
            with self.subTest(code=code):
                self.dsm.overrides[(TASK2, "create")] = partial(failure, code)
                error = await self.error_of(self.client.add_magnet(MAGNET))
                self.assertIn(text, str(error))

    async def test_task_errors_without_a_destination(self) -> None:
        self.dsm.overrides[(TASK2, "create")] = lambda: failure(403)
        error = await self.error_of(self.client.add_magnet(MAGNET))
        self.assertIn("its default folder", str(error))

    async def test_unreachable_host(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("[Errno 11001] getaddrinfo failed", request=request)

        error = await self.error_of(self.make_client(refuse).test())
        self.assertIn(f"Can't reach the NAS at {HOST}", str(error))
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)

    async def test_timeout(self) -> None:
        def slow(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        error = await self.error_of(self.make_client(slow).add_magnet(MAGNET))
        self.assertIn("didn't answer in time", str(error))

    async def test_untrusted_certificate(self) -> None:
        def self_signed(request: httpx.Request) -> httpx.Response:
            cause = ssl.SSLCertVerificationError(1, "certificate verify failed: self-signed")
            raise httpx.ConnectError("connection failed", request=request) from cause

        def text_only(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(
                "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed", request=request
            )

        for handler in (self_signed, text_only):
            with self.subTest(handler=handler.__name__):
                error = await self.error_of(self.make_client(handler).test())
                self.assertIn("Certificate not trusted", str(error))
                self.assertIn('Untick "Verify the certificate (https)"', str(error))
                self.assertIsNone(error.__context__)

    async def test_other_tls_failures(self) -> None:
        def wrong_port(request: httpx.Request) -> httpx.Response:
            cause = ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number")
            raise httpx.ConnectError("connection failed", request=request) from cause

        error = await self.error_of(self.make_client(wrong_port).test())
        self.assertIn("Couldn't make a secure connection", str(error))

    async def test_not_a_dsm(self) -> None:
        responses = {
            "404": lambda: httpx.Response(404, text="<html>nope</html>"),
            "html": lambda: httpx.Response(200, text="<html>router login</html>"),
            "failure": lambda: failure(102),
            "no auth api": lambda: httpx.Response(200, json={"success": True, "data": {}}),
        }
        for label, respond in responses.items():
            with self.subTest(label=label):

                def handler(request: httpx.Request, respond: Any = respond) -> httpx.Response:
                    response: httpx.Response = respond()
                    return response

                error = await self.error_of(self.make_client(handler).test())
                self.assertIn(f"No Synology DSM answered at {HOST}", str(error))

    async def test_download_station_not_installed(self) -> None:
        self.dsm.info = {"success": True, "data": {AUTH: DSM6_INFO["data"][AUTH]}}
        error = await self.error_of(self.client.add_magnet(MAGNET))
        self.assertIn("Download Station isn't installed", str(error))
        self.assertEqual(self.dsm.calls, [])  # no login attempted

    async def test_redirect_names_only_the_new_origin(self) -> None:
        def redirect(request: httpx.Request) -> httpx.Response:
            location = "https://nas.local:5001/webapi/query.cgi?api=SYNO.API.Info&query=all"
            return httpx.Response(301, headers={"Location": location})

        error = await self.error_of(self.make_client(redirect).test())
        self.assertIn("redirects to https://nas.local:5001:", str(error))
        self.assertNotIn("query", str(error))

    async def test_http_error_status(self) -> None:
        self.dsm.overrides[(AUTH, "login")] = lambda: httpx.Response(502)
        error = await self.error_of(self.client.test())
        self.assertIn("answered HTTP 502", str(error))

    async def test_login_without_sid(self) -> None:
        self.dsm.overrides[(AUTH, "login")] = lambda: ok({})
        error = await self.error_of(self.client.test())
        self.assertIn("returned no session", str(error))


class SecretHygieneTests(SynologyTestCase):
    async def test_password_and_sid_never_reach_messages_or_logs(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"failed for {request.url}", request=request)

        def tls(request: httpx.Request) -> httpx.Response:
            message = f"[SSL: CERTIFICATE_VERIFY_FAILED] {request.url}"
            raise httpx.ConnectError(message, request=request)

        messages: list[str] = []
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("tests").debug("capturing")
            await self.client.add_magnet(MAGNET)
            await self.client.test()
            scenarios: list[tuple[str, str, int]] = [
                (AUTH, "login", 400), (AUTH, "login", 403), (TASK2, "create", 402),
                (TASK2, "create", 406), (AUTH, "logout", 105), (DS_INFO, "getinfo", 105),
            ]
            for api, method, code in scenarios:
                self.dsm.overrides = {(api, method): partial(failure, code)}
                for operation in (self.client.add_magnet(MAGNET), self.client.test()):
                    try:
                        await operation
                    except DownloadStationError as exc:
                        messages.append(str(exc))
                        messages.append(repr(exc))
                        self.assertIsNone(exc.__context__)
            for handler in (refuse, tls):
                try:
                    await self.make_client(handler).test()
                except DownloadStationError as exc:
                    messages.append(repr(exc))
                    self.assertIsNone(exc.__context__)
        self.assertGreater(len(messages), 10)
        everything = "\n".join(messages + logs.output)
        self.assertNotIn(PASSWORD, everything)
        self.assertNotIn(SID, everything)
        self.assertNotIn(MAGNET, everything)
        shown = "\n".join(messages)
        self.assertNotIn("/webapi", shown)  # no request URL, let alone its query parameters
        self.assertNotIn("api=", shown)
        self.assertNotIn(PASSWORD, repr(self.settings.current))


if __name__ == "__main__":
    unittest.main()
