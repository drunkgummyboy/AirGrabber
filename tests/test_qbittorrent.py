"""QBittorrentClient against a fake qBittorrent Web API v2 on httpx.MockTransport.

The fake answers as qBittorrent 4.1 – 5.1 do (see tests/fixtures/qbittorrent/README.md):
login "Ok." + a SID cookie or "Fails.", 403 for a banned IP and for requests without a session,
the CSRF check on Origin / Referer, version text, and torrents/add "Ok." / "Fails." / 415.
"""

from __future__ import annotations

import json
import logging
import unittest
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import parse_qsl, urlsplit

import httpx

from app.database import SettingsRepository
from app.models import RemoteKind, Settings
from app.services.qbittorrent import QBittorrentClient
from app.services.remote import RemoteError
from app.services.settings import SettingsService
from tests.multipart import parse_multipart

FIXTURES = Path(__file__).parent / "fixtures" / "qbittorrent"
BASE = "http://192.168.1.20:8080"
HOST = "192.168.1.20:8080"
PASSWORD = "QBTPASS123"
SID = "hBc7TxF76ERhvIw0jQQ4LZ7Z1jQUV0tQ"
MAGNET = "magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567&dn=Some.Show.S01E01"
TORRENT = b"d8:announce30:udp://tracker.example.org:13374:infod4:name4:showee"


def fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


class FakeQBittorrent:
    """qBittorrent's Web API: sessions by SID cookie, the CSRF check, recorded calls."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.calls: list[str] = []  # API paths, e.g. "auth/login"
        self.forms: dict[str, dict[str, str]] = {}
        self.files: dict[str, dict[str, tuple[str, bytes]]] = {}
        self.username, self.password = "admin", PASSWORD
        self.banned = False
        self.bypass_auth = False  # "Bypass authentication for clients in whitelisted subnets"
        self.add_answer: Callable[[], httpx.Response] = lambda: httpx.Response(200, text="Ok.")
        self.version = fixture("app_version.txt")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.partition("/api/v2/")[2]
        self.calls.append(path)
        if self._cross_site(request):
            return httpx.Response(401, text="Unauthorized")
        content_type = request.headers.get("Content-Type", "")
        if content_type.startswith("multipart/form-data"):
            form, files = parse_multipart(request)
        else:
            form, files = dict(parse_qsl(request.content.decode())), {}
        self.forms[path], self.files[path] = form, files
        if path == "auth/login":
            if self.banned:
                return httpx.Response(403, text="Your IP address has been banned")
            if (form.get("username"), form.get("password")) == (self.username, self.password):
                return httpx.Response(200, text="Ok.", headers={"Set-Cookie": f"SID={SID}; path=/"})
            return httpx.Response(200, text="Fails.")
        if not self.bypass_auth and request.headers.get("Cookie") != f"SID={SID}":
            return httpx.Response(403, text="Forbidden")
        if path == "app/version":
            return httpx.Response(200, text=self.version)
        if path == "torrents/add":
            return self.add_answer()
        if path == "auth/logout":
            return httpx.Response(200)
        return httpx.Response(404, text="Not Found")

    @staticmethod
    def _cross_site(request: httpx.Request) -> bool:
        """qBittorrent's isCrossSiteRequest: Origin, else Referer, must match the Host."""
        host = request.headers["Host"]
        for header in ("Origin", "Referer"):
            value = request.headers.get(header)
            if value:
                return bool(urlsplit(value).netloc != host)
        return False


def settings_service(**changes: Any) -> SettingsService:
    values: dict[str, Any] = {
        "remote_kind": RemoteKind.QBITTORRENT,
        "remote_url": BASE,
        "remote_username": "admin",
        "remote_password": PASSWORD,
    }
    values.update(changes)
    return SettingsService(mock.Mock(spec=SettingsRepository), Settings(**values))


class QBittorrentTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.qbt = FakeQBittorrent()
        self.settings = settings_service()
        self.client = self.make_client(self.qbt)

    def make_client(self, handler: Callable[[httpx.Request], Any]) -> QBittorrentClient:
        return QBittorrentClient(self.settings, transport=httpx.MockTransport(handler))

    async def error_of(self, operation: Awaitable[object]) -> RemoteError:
        with self.assertRaises(RemoteError) as caught:
            await operation
        self.assertIsNone(caught.exception.__context__)
        return caught.exception


class LoginTests(QBittorrentTestCase):
    async def test_ok_signs_in_tests_and_signs_out(self) -> None:
        self.assertEqual(await self.client.test(), "qBittorrent v5.0.2")
        self.assertEqual(self.qbt.calls, ["auth/login", "app/version", "auth/logout"])
        login = self.qbt.requests[0]
        self.assertEqual(login.method, "POST")
        self.assertEqual(self.qbt.forms["auth/login"], {"username": "admin", "password": PASSWORD})
        self.assertNotIn(PASSWORD, str(login.url))
        self.assertEqual(self.qbt.requests[2].method, "POST")  # logout

    async def test_referer_and_origin_match_the_web_ui(self) -> None:
        await self.client.test()
        for request in self.qbt.requests:
            self.assertEqual(request.headers["Referer"], f"{BASE}/")
            self.assertEqual(request.headers["Origin"], BASE)

    async def test_origin_leaves_out_a_default_port_and_the_path(self) -> None:
        self.settings = settings_service(remote_url="https://nas.example:443/qbt")
        client = self.make_client(self.qbt)
        await client.test()
        request = self.qbt.requests[0]
        self.assertEqual(request.url.path, "/qbt/api/v2/auth/login")
        self.assertEqual(request.headers["Origin"], "https://nas.example")
        self.assertEqual(request.headers["Referer"], "https://nas.example/qbt/")
        self.assertEqual(self.qbt.calls, ["auth/login", "app/version", "auth/logout"])

    async def test_fails_means_a_wrong_password(self) -> None:
        self.qbt.password = "other"
        error = await self.error_of(self.client.test())
        self.assertEqual(str(error), "qBittorrent: wrong user name or password.")
        self.assertEqual(self.qbt.calls, ["auth/login"])  # no logout without a session

    async def test_401_from_newer_builds_is_a_wrong_password_too(self) -> None:
        def unauthorized(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401)

        error = await self.error_of(self.make_client(unauthorized).test())
        self.assertIn("wrong user name or password", str(error))

    async def test_403_on_login_is_a_ban(self) -> None:
        self.qbt.banned = True
        error = await self.error_of(self.client.test())
        self.assertIn("blocked this computer after too many failed sign-ins", str(error))

    async def test_without_a_user_name_it_does_not_sign_in(self) -> None:
        self.qbt.bypass_auth = True
        self.settings = settings_service(remote_username="", remote_password="")
        self.assertEqual(await self.make_client(self.qbt).test(), "qBittorrent v5.0.2")
        self.assertEqual(self.qbt.calls, ["app/version"])

    async def test_without_a_user_name_when_qbittorrent_wants_one(self) -> None:
        self.settings = settings_service(remote_username="", remote_password="")
        error = await self.error_of(self.make_client(self.qbt).test())
        self.assertIn("wants a user name and password", str(error))

    async def test_unusual_version_text_is_left_out(self) -> None:
        self.qbt.version = "<html>surprise</html>"
        self.assertEqual(await self.client.test(), "qBittorrent")

    async def test_not_set_up_makes_no_request(self) -> None:
        self.settings = settings_service(remote_url="")
        error = await self.error_of(self.make_client(self.qbt).test())
        self.assertIn("isn't set up", str(error))
        self.assertEqual(self.qbt.requests, [])


class AddTests(QBittorrentTestCase):
    async def test_magnet_goes_in_urls_as_multipart(self) -> None:
        await self.client.add_magnet(MAGNET)
        self.assertEqual(self.qbt.calls, ["auth/login", "torrents/add", "auth/logout"])
        add = next(r for r in self.qbt.requests if r.url.path.endswith("torrents/add"))
        self.assertEqual(add.method, "POST")
        self.assertTrue(add.headers["Content-Type"].startswith("multipart/form-data"))
        self.assertEqual(self.qbt.forms["torrents/add"], {"urls": MAGNET})
        self.assertEqual(add.url.query, b"")

    async def test_torrent_file_goes_in_torrents(self) -> None:
        await self.client.add_torrent(TORRENT, "Some Show S01E01.torrent")
        self.assertEqual(
            self.qbt.files["torrents/add"], {"torrents": ("Some Show S01E01.torrent", TORRENT)}
        )
        self.assertEqual(self.qbt.forms["torrents/add"], {})

    async def test_destination_is_the_savepath(self) -> None:
        self.settings = settings_service(remote_destination="/downloads/tv")
        client = self.make_client(self.qbt)
        await client.add_magnet(MAGNET)
        self.assertEqual(
            self.qbt.forms["torrents/add"], {"urls": MAGNET, "savepath": "/downloads/tv"}
        )
        await client.add_torrent(TORRENT, "x.torrent")
        self.assertEqual(self.qbt.forms["torrents/add"], {"savepath": "/downloads/tv"})

    async def test_fails_is_an_error_and_still_signs_out(self) -> None:
        self.qbt.add_answer = lambda: httpx.Response(200, text="Fails.")
        error = await self.error_of(self.client.add_magnet(MAGNET))
        self.assertIn("qBittorrent didn't add the torrent", str(error))
        self.assertEqual(self.qbt.calls[-1], "auth/logout")

    async def test_invalid_torrent_file(self) -> None:
        self.qbt.add_answer = lambda: httpx.Response(415, text="'x' is not a valid torrent file.")
        error = await self.error_of(self.client.add_torrent(b"junk", "x.torrent"))
        self.assertEqual(str(error), "qBittorrent says this .torrent file isn't valid.")

    async def test_newer_json_answers(self) -> None:
        added = json.loads(fixture("torrents_add_json.json"))
        self.qbt.add_answer = lambda: httpx.Response(200, json=added)
        await self.client.add_magnet(MAGNET)
        nothing = {**added, "success_count": 0, "failure_count": 1, "added_torrent_ids": []}
        self.qbt.add_answer = lambda: httpx.Response(200, json=nothing)
        await self.error_of(self.client.add_magnet(MAGNET))
        self.qbt.add_answer = lambda: httpx.Response(409)
        error = await self.error_of(self.client.add_magnet(MAGNET))
        self.assertIn("It may have it already", str(error))

    async def test_rejects_non_magnet_links(self) -> None:
        with self.assertRaises(ValueError):
            await self.client.add_magnet("https://example.com/file.torrent")


class ErrorTests(QBittorrentTestCase):
    async def test_unreachable(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"failed for {request.url}", request=request)

        error = await self.error_of(self.make_client(refuse).test())
        self.assertEqual(
            str(error),
            f"Couldn't reach {HOST}. Check the address in Settings and that qBittorrent is on.",
        )

    async def test_self_signed_certificate(self) -> None:
        def tls(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED]", request=request)

        error = await self.error_of(self.make_client(tls).test())
        self.assertIn('Untick "Verify the certificate (https)"', str(error))

    async def test_timeout(self) -> None:
        def slow(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timed out", request=request)

        error = await self.error_of(self.make_client(slow).test())
        self.assertIn("didn't answer in time", str(error))

    async def test_not_a_qbittorrent(self) -> None:
        def router(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, text="<html>router</html>")

        self.settings = settings_service(remote_username="")
        error = await self.error_of(self.make_client(router).test())
        self.assertIn(f"No qBittorrent Web UI answered at {HOST}", str(error))

    async def test_redirect(self) -> None:
        def redirect(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": "https://192.168.1.20:8443/login?x=1"})

        error = await self.error_of(self.make_client(redirect).test())
        self.assertIn("redirects to https://192.168.1.20:8443:", str(error))

    async def test_secrets_stay_out_of_messages_and_logs(self) -> None:
        messages: list[str] = []
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("tests").debug("capturing")
            await self.client.test()
            await self.client.add_magnet(MAGNET)
            await self.client.add_torrent(TORRENT, "x.torrent")
            self.qbt.password = "other"
            for operation in (self.client.test(), self.client.add_magnet(MAGNET)):
                try:
                    await operation
                except RemoteError as exc:
                    messages.append(repr(exc))
        everything = "\n".join(messages + logs.output)
        for secret in (PASSWORD, SID, MAGNET):
            self.assertNotIn(secret, everything)
        self.assertGreaterEqual(len(messages), 2)


if __name__ == "__main__":
    unittest.main()
