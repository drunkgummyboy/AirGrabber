"""TransmissionClient against a fake Transmission RPC on httpx.MockTransport.

The fake answers as Transmission's RPC spec describes (see tests/fixtures/transmission/README.md):
409 with X-Transmission-Session-Id until the request carries it, Basic auth (401), the RPC
whitelist (403), `session-get` and `torrent-add` answers from the fixtures.
"""

from __future__ import annotations

import base64
import json
import logging
import unittest
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest import mock

import httpx

from app.database import SettingsRepository
from app.models import RemoteKind, Settings
from app.services.remote import RemoteError
from app.services.settings import SettingsService
from app.services.transmission import TransmissionClient, rpc_url

FIXTURES = Path(__file__).parent / "fixtures" / "transmission"
BASE = "http://192.168.1.30:9091"
HOST = "192.168.1.30:9091"
PASSWORD = "TRPASS123"
SESSION_ID = "pTXx9XMKbbKGbXbYQMQ8Dy3xHyAKt5JGnGrNNP8ewCUc2Usx"
MAGNET = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Some.Show.S01E01"
TORRENT = b"d8:announce30:udp://tracker.example.org:13374:infod4:name4:showee"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeTransmission:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict[str, Any]] = []  # the RPC requests that got past the checks
        self.auth: tuple[str, str] | None = ("beau", PASSWORD)
        self.whitelisted = True
        self.answers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "session-get": lambda body: fixture("session_get.json"),
            "torrent-add": lambda body: fixture("torrent_add_added.json"),
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.whitelisted:
            return httpx.Response(403, text="<h1>403: Forbidden</h1><p>Unauthorized IP Address.</p>")
        if self.auth is not None:
            expected = "Basic " + base64.b64encode(":".join(self.auth).encode()).decode()
            if request.headers.get("Authorization") != expected:
                return httpx.Response(401, text="<h1>401: Unauthorized</h1>")
        if request.headers.get("X-Transmission-Session-Id") != SESSION_ID:
            return httpx.Response(
                409, text="<h1>409: Conflict</h1>", headers={"X-Transmission-Session-Id": SESSION_ID}
            )
        body: dict[str, Any] = json.loads(request.content)
        self.bodies.append(body)
        answer = dict(self.answers[body["method"]](body))
        answer["tag"] = body.get("tag")
        return httpx.Response(200, json=answer)


def settings_service(**changes: Any) -> SettingsService:
    values: dict[str, Any] = {
        "remote_kind": RemoteKind.TRANSMISSION,
        "remote_url": BASE,
        "remote_username": "beau",
        "remote_password": PASSWORD,
    }
    values.update(changes)
    return SettingsService(mock.Mock(spec=SettingsRepository), Settings(**values))


class TransmissionTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.server = FakeTransmission()
        self.settings = settings_service()
        self.client = self.make_client(self.server)

    def make_client(self, handler: Callable[[httpx.Request], Any]) -> TransmissionClient:
        return TransmissionClient(self.settings, transport=httpx.MockTransport(handler))

    async def error_of(self, operation: Awaitable[object]) -> RemoteError:
        with self.assertRaises(RemoteError) as caught:
            await operation
        self.assertIsNone(caught.exception.__context__)
        return caught.exception


class RpcTests(TransmissionTestCase):
    def test_rpc_url(self) -> None:
        self.assertEqual(rpc_url(BASE), f"{BASE}/transmission/rpc")
        self.assertEqual(rpc_url(f"{BASE}/"), f"{BASE}/transmission/rpc")
        self.assertEqual(rpc_url("https://nas:9091/transmission/rpc"), "https://nas:9091/transmission/rpc")
        self.assertEqual(rpc_url("https://nas/my/rpc/"), "https://nas/my/rpc")

    async def test_session_id_handshake_then_version(self) -> None:
        self.assertEqual(await self.client.test(), "Transmission 4.0.6")
        first, second = self.server.requests
        self.assertEqual(str(first.url), f"{BASE}/transmission/rpc")
        self.assertEqual(first.method, "POST")
        self.assertNotIn("X-Transmission-Session-Id", first.headers)
        self.assertEqual(second.headers["X-Transmission-Session-Id"], SESSION_ID)
        self.assertEqual(self.server.bodies[0]["method"], "session-get")

    async def test_a_second_409_is_not_retried_forever(self) -> None:
        def always_conflict(request: httpx.Request) -> httpx.Response:
            return httpx.Response(409, headers={"X-Transmission-Session-Id": "again"})

        error = await self.error_of(self.make_client(always_conflict).test())
        self.assertIn(f"No Transmission answered at {HOST}", str(error))

    async def test_basic_auth_when_a_user_name_is_set(self) -> None:
        await self.client.test()
        expected = "Basic " + base64.b64encode(f"beau:{PASSWORD}".encode()).decode()
        self.assertTrue(all(r.headers["Authorization"] == expected for r in self.server.requests))
        for request in self.server.requests:
            self.assertNotIn(PASSWORD, str(request.url))
            self.assertNotIn(PASSWORD.encode(), request.content)

    async def test_no_auth_without_a_user_name(self) -> None:
        self.server.auth = None
        self.settings = settings_service(remote_username="", remote_password="")
        await self.make_client(self.server).test()
        self.assertTrue(all("Authorization" not in r.headers for r in self.server.requests))

    async def test_wrong_password_and_missing_account(self) -> None:
        self.server.auth = ("beau", "other")
        error = await self.error_of(self.client.test())
        self.assertEqual(str(error), "Transmission: wrong user name or password.")
        self.settings = settings_service(remote_username="", remote_password="")
        error = await self.error_of(self.make_client(self.server).test())
        self.assertIn("wants a user name and password", str(error))

    async def test_whitelist(self) -> None:
        self.server.whitelisted = False
        error = await self.error_of(self.client.test())
        self.assertIn("RPC whitelist", str(error))


class AddTests(TransmissionTestCase):
    async def test_magnet_as_filename(self) -> None:
        await self.client.add_magnet(MAGNET)
        [body] = self.server.bodies
        self.assertEqual(body["method"], "torrent-add")
        self.assertEqual(body["arguments"], {"filename": MAGNET})

    async def test_torrent_as_base64_metainfo(self) -> None:
        await self.client.add_torrent(TORRENT, "x.torrent")
        [body] = self.server.bodies
        self.assertEqual(base64.b64decode(body["arguments"]["metainfo"]), TORRENT)
        self.assertNotIn("filename", body["arguments"])

    async def test_destination_is_the_download_dir(self) -> None:
        self.settings = settings_service(remote_destination="/downloads/tv")
        client = self.make_client(self.server)
        await client.add_magnet(MAGNET)
        await client.add_torrent(TORRENT, "x.torrent")
        for body in self.server.bodies:
            self.assertEqual(body["arguments"]["download-dir"], "/downloads/tv")

    async def test_duplicate_is_fine(self) -> None:
        self.server.answers["torrent-add"] = lambda body: fixture("torrent_add_duplicate.json")
        await self.client.add_magnet(MAGNET)

    async def test_non_success_result(self) -> None:
        self.server.answers["torrent-add"] = lambda body: fixture("torrent_add_failure.json")
        error = await self.error_of(self.client.add_torrent(b"junk", "x.torrent"))
        self.assertEqual(str(error), "Transmission refused it: invalid or corrupt torrent file.")

    async def test_success_without_an_added_torrent(self) -> None:
        self.server.answers["torrent-add"] = lambda body: {"result": "success", "arguments": {}}
        error = await self.error_of(self.client.add_magnet(MAGNET))
        self.assertIn("doesn't understand", str(error))

    async def test_not_json(self) -> None:
        def html(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="<html>router</html>")

        self.settings = settings_service(remote_username="")
        error = await self.error_of(self.make_client(html).add_magnet(MAGNET))
        self.assertIn("doesn't understand", str(error))

    async def test_unreachable(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"failed for {request.url}", request=request)

        error = await self.error_of(self.make_client(refuse).add_magnet(MAGNET))
        self.assertEqual(
            str(error),
            f"Couldn't reach {HOST}. Check the address in Settings and that Transmission is on.",
        )

    async def test_not_set_up(self) -> None:
        self.settings = settings_service(remote_url="")
        error = await self.error_of(self.make_client(self.server).add_magnet(MAGNET))
        self.assertIn("isn't set up", str(error))
        self.assertEqual(self.server.requests, [])

    async def test_secrets_stay_out_of_messages_and_logs(self) -> None:
        messages: list[str] = []
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("tests").debug("capturing")
            await self.client.test()
            await self.client.add_magnet(MAGNET)
            await self.client.add_torrent(TORRENT, "x.torrent")
            self.server.auth = ("beau", "other")
            for operation in (self.client.test(), self.client.add_magnet(MAGNET)):
                try:
                    await operation
                except RemoteError as exc:
                    messages.append(repr(exc))
        everything = "\n".join(messages + logs.output)
        for secret in (PASSWORD, MAGNET, base64.b64encode(f"beau:{PASSWORD}".encode()).decode()):
            self.assertNotIn(secret, everything)
        self.assertEqual(len(messages), 2)


if __name__ == "__main__":
    unittest.main()
