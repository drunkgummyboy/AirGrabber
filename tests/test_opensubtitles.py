"""OpenSubtitles client: headers, login and its host, search parameters, download and quota,
errors in plain words, and no key, password or token in URLs, messages or logs (a fake server
answering with tests/fixtures/opensubtitles; no live calls). Also the moviehash."""

from __future__ import annotations

import json
import logging
import os
import random
import struct
import tempfile
import unittest
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.services.opensubtitles import (
    APP_API_KEY,
    NO_KEY_TEXT,
    KEY_REJECTED_TEXT,
    LOGIN_REJECTED_TEXT,
    OWN_KEY_REJECTED_TEXT,
    QUOTA_TEXT,
    SESSION_TTL_S,
    UNREACHABLE_TEXT,
    USER_AGENT,
    KeyRejected,
    effective_api_key,
    LoginRejected,
    OpenSubtitlesClient,
    OpenSubtitlesError,
    QuotaExceeded,
    Unreachable,
    imdb_number,
    movie_hash,
    sorted_params,
)

FIXTURES = Path(__file__).parent / "fixtures" / "opensubtitles"
KEY = "TESTAPIKEY0123456789abcdefABCDEF"
USERNAME = "beau"
PASSWORD = "S3cret-pass!"
TOKEN = json.loads((FIXTURES / "login.json").read_text(encoding="utf-8"))["token"]
LINK = json.loads((FIXTURES / "download.json").read_text(encoding="utf-8"))["link"]
SRT = b"1\r\n00:00:01,000 --> 00:00:02,000\r\nHello\r\n"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeOpenSubtitles:
    """Answers like api.opensubtitles.com; `routes` maps "METHOD /path" to (status, body)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.routes: dict[str, Callable[[httpx.Request], httpx.Response]] = {
            "GET /api/v1/infos/formats": self.json(200, fixture("infos_formats.json")),
            "POST /api/v1/login": self.login,
            "GET /api/v1/infos/user": self.json(200, fixture("infos_user.json")),
            "GET /api/v1/subtitles": self.json(200, fixture("search_episode.json")),
            "POST /api/v1/download": self.json(200, fixture("download.json")),
        }
        self.login_answer = fixture("login.json")

    @staticmethod
    def json(status: int, body: Any, headers: dict[str, str] | None = None) -> Callable[[httpx.Request], httpx.Response]:
        return lambda request: httpx.Response(status, json=body, headers=headers)

    def login(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body != {"username": USERNAME, "password": PASSWORD}:
            return httpx.Response(401, json={"message": "Error, invalid username/password", "status": 401})
        return httpx.Response(200, json=self.login_answer)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "www.opensubtitles.com":
            return httpx.Response(200, content=SRT)
        if request.headers.get("Api-Key") != KEY:
            return httpx.Response(401, json={"message": "Invalid authentication credentials"})
        route = self.routes.get(f"{request.method} {request.url.path}")
        if route is None:
            return httpx.Response(404, json={"message": "no route"})
        return route(request)

    def paths(self) -> list[str]:
        return [f"{r.method} {r.url.host}{r.url.path}" for r in self.requests]


class ClientTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.server = FakeOpenSubtitles()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.server), follow_redirects=True)
        self.addAsyncCleanup(self.http.aclose)
        self.key = KEY
        self.now = 1000.0
        self.slept: list[float] = []
        self.client = self.make_client()

    def make_client(self) -> OpenSubtitlesClient:
        async def sleep(seconds: float) -> None:
            self.slept.append(seconds)
            self.now += seconds

        return OpenSubtitlesClient(
            self.http, api_key=lambda: self.key, clock=lambda: self.now, sleep=sleep, retry_delay=0.5
        )

    async def error_of(self, awaitable: Any) -> OpenSubtitlesError:
        try:
            await awaitable
        except OpenSubtitlesError as exc:
            return exc
        raise AssertionError("no OpenSubtitlesError")


class HeaderTests(ClientTestCase):
    async def test_every_request_names_the_app_and_carries_the_key_in_a_header(self) -> None:
        await self.client.search({"query": "x"})
        request = self.server.requests[0]
        self.assertEqual(request.headers["Api-Key"], KEY)
        self.assertEqual(request.headers["User-Agent"], USER_AGENT)
        self.assertRegex(USER_AGENT, r"^AirGrabber v\d+\.\d+")
        self.assertEqual(request.headers["Accept"], "application/json")
        self.assertNotIn("Authorization", request.headers)
        self.assertNotIn(KEY, str(request.url))
        self.assertEqual(request.url.host, "api.opensubtitles.com")

    async def test_search_parameters_are_sorted_lowercase_and_without_empties(self) -> None:
        await self.client.search(
            {"Query": "Desperate Housewives", "languages": "pt-BR,en", "season_number": 1, "year": ""}
        )
        query = self.server.requests[0].url.params.multi_items()
        self.assertEqual(
            query,
            [("languages", "pt-br,en"), ("query", "desperate housewives"), ("season_number", "1")],
        )

    async def test_no_key_means_no_request(self) -> None:
        self.key = "  "
        self.assertFalse(self.client.configured)
        error = await self.error_of(self.client.search({"query": "x"}))
        self.assertIsInstance(error, KeyRejected)
        self.assertEqual(str(error), NO_KEY_TEXT)
        self.assertEqual(self.server.requests, [])

    async def test_requests_are_spaced(self) -> None:
        for _ in range(3):
            await self.client.search({"query": "x"})
        self.assertEqual(self.slept, [0.25, 0.25])  # at most four a second


class AppKeyTests(ClientTestCase):
    def test_the_key_ships_with_the_app_in_one_place(self) -> None:
        self.assertRegex(APP_API_KEY, r"^[A-Za-z0-9]{32}$")  # AirGrabber's consumer key
        self.assertTrue(self.client.configured)

    async def test_a_refused_app_key(self) -> None:
        self.key = "revoked"
        error = await self.error_of(self.client.search({"query": "x"}))
        self.assertIsInstance(error, KeyRejected)
        self.assertEqual(str(error), KEY_REJECTED_TEXT)
        self.assertNotIn("revoked", str(error))

    async def test_no_answer(self) -> None:
        self.server.routes["GET /api/v1/subtitles"] = self.server.json(503, {})
        error = await self.error_of(self.client.search({"query": "x"}))
        self.assertIsInstance(error, Unreachable)
        self.assertEqual(str(error), UNREACHABLE_TEXT)
        self.assertEqual(len(self.server.requests), 3)


class OwnKeyTests(ClientTestCase):
    def test_the_own_key_wins_when_filled_in(self) -> None:
        self.assertEqual(effective_api_key("  OWN  ", "SHIPPED"), "OWN")
        self.assertEqual(effective_api_key("", "SHIPPED"), "SHIPPED")
        self.assertEqual(effective_api_key("   ", " SHIPPED "), "SHIPPED")
        self.assertEqual(effective_api_key("", ""), "")

    async def test_check_key_asks_with_that_key_only(self) -> None:
        self.key = "SOMETHING-ELSE"  # the key in use doesn't matter for the check
        await self.client.check_key(f"  {KEY} ")
        (request,) = self.server.requests
        self.assertEqual(f"{request.method} {request.url.path}", "GET /api/v1/infos/formats")
        self.assertEqual(request.headers["Api-Key"], KEY)
        self.assertNotIn("Authorization", request.headers)
        self.assertNotIn(KEY, str(request.url))

    async def test_a_refused_own_key_is_named_without_the_key(self) -> None:
        with self.assertLogs("app.services.opensubtitles", logging.DEBUG) as logs:
            logging.getLogger("app.services.opensubtitles").debug("start")
            error = await self.error_of(self.client.check_key("WRONG-OWN-KEY"))
        self.assertIsInstance(error, KeyRejected)
        self.assertEqual(str(error), OWN_KEY_REJECTED_TEXT)
        self.assertNotIn("WRONG-OWN-KEY", str(error))
        self.assertNotIn("WRONG-OWN-KEY", " ".join(logs.output))

    async def test_check_key_unreachable(self) -> None:
        self.server.routes["GET /api/v1/infos/formats"] = self.server.json(503, {})
        error = await self.error_of(self.client.check_key(KEY))
        self.assertIsInstance(error, Unreachable)

    async def test_requests_with_a_refused_own_key_say_so(self) -> None:
        own = OpenSubtitlesClient(self.http, api_key=lambda: "WRONG-OWN-KEY", own_key=lambda: True)
        error = await self.error_of(own.search({"query": "x"}))
        self.assertIsInstance(error, KeyRejected)
        self.assertEqual(str(error), OWN_KEY_REJECTED_TEXT)
        shipped = OpenSubtitlesClient(self.http, api_key=lambda: "REVOKED")
        error = await self.error_of(shipped.search({"query": "x"}))
        self.assertEqual(str(error), KEY_REJECTED_TEXT)


class LoginTests(ClientTestCase):
    async def test_sign_in_posts_the_password_in_the_body_and_reads_the_downloads_left(self) -> None:
        account = await self.client.sign_in(USERNAME, PASSWORD)
        self.assertEqual(account.allowed_downloads, 20)
        self.assertEqual(account.remaining_downloads, 18)
        self.assertEqual(account.level, "Sub leecher")
        login, user = self.server.requests
        self.assertEqual(login.method, "POST")
        self.assertEqual(json.loads(login.content), {"username": USERNAME, "password": PASSWORD})
        self.assertNotIn(PASSWORD, str(login.url))
        self.assertEqual(user.headers["Authorization"], f"Bearer {TOKEN}")
        self.assertTrue(self.client.signed_in)

    async def test_later_requests_carry_the_token(self) -> None:
        await self.client.sign_in(USERNAME, PASSWORD)
        await self.client.download_link(123)
        download = self.server.requests[-1]
        self.assertEqual(download.headers["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(json.loads(download.content), {"file_id": 123})

    async def test_a_vip_account_moves_to_its_host(self) -> None:
        self.server.login_answer = fixture("login_vip.json")
        await self.client.sign_in(USERNAME, PASSWORD)
        await self.client.search({"query": "x"})
        self.assertEqual(self.server.requests[-1].url.host, "vip-api.opensubtitles.com")

    async def test_an_unknown_host_is_never_used(self) -> None:
        self.server.login_answer = {**fixture("login.json"), "base_url": "evil.example.com"}
        await self.client.sign_in(USERNAME, PASSWORD)
        await self.client.search({"query": "x"})
        self.assertEqual({r.url.host for r in self.server.requests}, {"api.opensubtitles.com"})

    async def test_wrong_password(self) -> None:
        error = await self.error_of(self.client.sign_in(USERNAME, "nope"))
        self.assertIsInstance(error, LoginRejected)
        self.assertEqual(str(error), LOGIN_REJECTED_TEXT)
        self.assertFalse(self.client.signed_in)

    async def test_refused_credentials_are_not_sent_again_by_the_runs(self) -> None:
        await self.error_of(self.client.sign_in(USERNAME, "nope"))
        error = await self.error_of(self.client.ensure_session(USERNAME, "nope"))
        self.assertIsInstance(error, LoginRejected)
        self.assertEqual(len(self.server.requests), 1)  # the first login only
        # Settings' Sign in (the user asks again) does ask.
        await self.error_of(self.client.sign_in(USERNAME, "nope"))
        self.assertEqual(len(self.server.requests), 2)

    async def test_logins_are_a_second_apart(self) -> None:
        await self.error_of(self.client.sign_in(USERNAME, "nope"))
        self.slept.clear()
        await self.error_of(self.client.sign_in(USERNAME, "nope"))
        self.assertEqual([round(s, 2) for s in self.slept], [1.0])

    async def test_a_refused_key_on_login_is_a_key_problem(self) -> None:
        self.server.routes["POST /api/v1/login"] = self.server.json(403, {"message": "forbidden"})
        error = await self.error_of(self.client.sign_in(USERNAME, PASSWORD))
        self.assertIsInstance(error, KeyRejected)
        self.assertEqual(str(error), KEY_REJECTED_TEXT)

    async def test_the_session_is_kept_then_renewed(self) -> None:
        self.assertFalse(await self.client.ensure_session("", ""))
        self.assertTrue(await self.client.ensure_session(USERNAME, PASSWORD))
        self.assertTrue(await self.client.ensure_session(USERNAME, PASSWORD))

        def logins() -> int:
            return sum(r.url.path.endswith("/login") for r in self.server.requests)

        self.assertEqual(logins(), 1)
        self.now += SESSION_TTL_S + 1
        await self.client.ensure_session(USERNAME, PASSWORD)
        self.assertEqual(logins(), 2)
        self.key = "OTHERKEY"  # a changed key needs a new session (and is refused here)
        await self.error_of(self.client.ensure_session(USERNAME, PASSWORD))
        self.assertEqual(logins(), 3)

    async def test_an_expired_token_is_dropped_and_the_call_made_again(self) -> None:
        await self.client.sign_in(USERNAME, PASSWORD)
        calls = []

        def search(request: httpx.Request) -> httpx.Response:
            calls.append(request.headers.get("Authorization"))
            if "Authorization" in request.headers:
                return httpx.Response(500, json={"message": "invalid token"})
            return httpx.Response(200, json=fixture("search_empty.json"))

        self.server.routes["GET /api/v1/subtitles"] = search
        self.assertEqual(await self.client.search({"query": "x"}), [])
        self.assertEqual(calls, [f"Bearer {TOKEN}", None])
        self.assertFalse(self.client.signed_in)


class SearchTests(ClientTestCase):
    async def test_usable_results_only(self) -> None:
        found = await self.client.search({"query": "x"})
        # Not the machine- or AI-translated ones, nor the one in two parts; hearing-impaired ones stay.
        self.assertEqual([c.file_id for c in found], [10041, 10051, 10061, 10071, 10081])
        hashed = next(c for c in found if c.file_id == 10061)
        self.assertTrue(hashed.moviehash_match)
        self.assertTrue(hashed.hearing_impaired)
        self.assertEqual({c.language for c in found}, {"en", "nl"})

    async def test_an_odd_answer(self) -> None:
        self.server.routes["GET /api/v1/subtitles"] = lambda r: httpx.Response(200, content=b"<html>")
        error = await self.error_of(self.client.search({"query": "x"}))
        self.assertEqual(str(error), "OpenSubtitles /subtitles: the answer wasn't readable")

    async def test_rate_limited_then_answered(self) -> None:
        answers = [
            httpx.Response(429, json={"message": "API rate limit exceeded"}, headers={"Retry-After": "2"}),
            httpx.Response(200, json=fixture("search_empty.json")),
        ]
        self.server.routes["GET /api/v1/subtitles"] = lambda r: answers.pop(0)
        self.assertEqual(await self.client.search({"query": "x"}), [])
        self.assertIn(2.0, self.slept)

    async def test_rate_limit_waits_are_capped(self) -> None:
        self.server.routes["GET /api/v1/subtitles"] = self.server.json(
            429, {"message": "API rate limit exceeded"}, {"ratelimit-reset": "3600"}
        )
        error = await self.error_of(self.client.search({"query": "x"}))
        self.assertIsInstance(error, Unreachable)
        self.assertLessEqual(max(self.slept), 10.0)

    async def test_network_failure(self) -> None:
        def broken(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

        self.server.routes["GET /api/v1/subtitles"] = broken
        error = await self.error_of(self.client.search({"query": "x"}))
        self.assertIsInstance(error, Unreachable)
        self.assertIsNone(error.__context__)
        self.assertIsNone(error.__cause__)


class DownloadTests(ClientTestCase):
    async def test_link_and_quota(self) -> None:
        link = await self.client.download_link(9443515)
        self.assertEqual(link.link, LINK)
        self.assertEqual(link.remaining, 17)
        self.assertEqual(link.reset_at, datetime(2022, 4, 8, 13, 3, 15, tzinfo=UTC))
        self.assertNotIn(LINK, repr(link))

    async def test_used_up(self) -> None:
        self.server.routes["POST /api/v1/download"] = self.server.json(406, fixture("download_quota.json"))
        error = await self.error_of(self.client.download_link(1))
        self.assertIsInstance(error, QuotaExceeded)
        assert isinstance(error, QuotaExceeded)
        self.assertEqual(str(error), QUOTA_TEXT)
        self.assertEqual(error.reset_at, datetime(2022, 4, 8, 13, 3, 15, tzinfo=UTC))

    async def test_used_up_without_a_body(self) -> None:
        self.server.routes["POST /api/v1/download"] = lambda r: httpx.Response(406, content=b"")
        error = await self.error_of(self.client.download_link(1))
        assert isinstance(error, QuotaExceeded)
        self.assertIsNone(error.reset_at)

    async def test_the_file_comes_without_the_key_or_token(self) -> None:
        await self.client.sign_in(USERNAME, PASSWORD)
        self.assertEqual(await self.client.fetch(LINK), SRT)
        request = self.server.requests[-1]
        self.assertEqual(request.url.host, "www.opensubtitles.com")
        self.assertNotIn("Api-Key", request.headers)
        self.assertNotIn("Authorization", request.headers)
        self.assertEqual(request.headers["User-Agent"], USER_AGENT)

    async def test_only_opensubtitles_links_are_followed(self) -> None:
        for link in ("http://www.opensubtitles.com/x.srt", "https://evil.example.com/x.srt",
                     "https://opensubtitles.com.evil.example/x.srt"):
            with self.subTest(link=link):
                await self.error_of(self.client.fetch(link))
        self.assertEqual(self.server.requests, [])

    async def test_an_empty_or_huge_file_is_refused(self) -> None:
        for content in (b"", b"x" * 5_000_001):

            def answer(request: httpx.Request, content: bytes = content) -> httpx.Response:
                return httpx.Response(200, content=content)

            http = httpx.AsyncClient(transport=httpx.MockTransport(answer))
            self.addAsyncCleanup(http.aclose)
            client = OpenSubtitlesClient(http, api_key=lambda: KEY)
            with self.subTest(size=len(content)):
                await self.error_of(client.fetch(LINK))


class SecretHygieneTests(ClientTestCase):
    async def test_secrets_stay_out_of_messages_and_logs(self) -> None:
        # As app/config.py sets them: httpx logs request URLs at INFO.
        for name in ("httpx", "httpcore"):
            logger = logging.getLogger(name)
            self.addCleanup(logger.setLevel, logger.level)
            logger.setLevel(logging.WARNING)
        messages: list[str] = []
        with self.assertLogs(level=logging.DEBUG) as logs:
            logging.getLogger("tests").debug("capturing")
            await self.client.sign_in(USERNAME, PASSWORD)
            await self.client.search({"query": "x"})
            link = await self.client.download_link(1)
            await self.client.fetch(link.link)
            failures: list[Callable[[httpx.Request], httpx.Response]] = [
                self.server.json(406, fixture("download_quota.json")),
                self.server.json(503, {}),
                self.server.json(418, {"message": KEY}),
            ]
            for failure in failures:
                self.server.routes["POST /api/v1/download"] = failure
                try:
                    await self.client.download_link(1)
                except OpenSubtitlesError as exc:
                    messages.append(repr(exc))
            for operation in (self.client.sign_in(USERNAME, "wrong-" + PASSWORD),):
                try:
                    await operation
                except OpenSubtitlesError as exc:
                    messages.append(repr(exc))
        everything = "\n".join(messages + logs.output)
        for secret in (KEY, PASSWORD, TOKEN, LINK, "A184A5EA6302F2CA"):
            self.assertNotIn(secret, everything)
        self.assertGreaterEqual(len(messages), 4)


class HelperTests(unittest.TestCase):
    def test_imdb_number(self) -> None:
        self.assertEqual(imdb_number("tt0903747"), 903747)
        self.assertEqual(imdb_number("TT0410975"), 410975)
        for value in (None, "", "tt", "tt0000000", "nm123"):
            self.assertIsNone(imdb_number(value))

    def test_sorted_params(self) -> None:
        self.assertEqual(
            sorted_params({"b": "X", "a": 2, "c": " "}), [("a", "2"), ("b", "x")]
        )


def reference_hash(name: str) -> str:
    """The Python implementation on OpenSubtitles' "Moviehash source codes" page, as printed
    there (signed '<q' words, masked to 64 bits)."""
    longlongformat = "<q"
    bytesize = struct.calcsize(longlongformat)
    with open(name, "rb") as f:
        filesize = os.path.getsize(name)
        hash = filesize
        for _x in range(int(65536 / bytesize)):
            buffer = f.read(bytesize)
            (l_value,) = struct.unpack(longlongformat, buffer)
            hash += l_value
            hash = hash & 0xFFFFFFFFFFFFFFFF
        f.seek(max(0, filesize - 65536), 0)
        for _x in range(int(65536 / bytesize)):
            buffer = f.read(bytesize)
            (l_value,) = struct.unpack(longlongformat, buffer)
            hash += l_value
            hash = hash & 0xFFFFFFFFFFFFFFFF
    return "%016x" % hash


class MovieHashTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)

    def file(self, content: bytes) -> Path:
        path = self.folder / f"video{len(list(self.folder.iterdir()))}.avi"
        path.write_bytes(content)
        return path

    def test_zeros_hash_to_the_size(self) -> None:
        size = 200_000
        self.assertEqual(movie_hash(self.file(bytes(size))), f"{size:016x}")

    def test_the_sum_wraps_at_64_bits(self) -> None:
        content = b"\xff" * 300_000  # every word is 2**64 - 1
        expected = (300_000 - 2 * 8192) % 2**64
        self.assertEqual(movie_hash(self.file(content)), f"{expected:016x}")

    def test_leading_zeros_are_kept(self) -> None:
        digest = movie_hash(self.file(bytes(140_000)))
        self.assertEqual(digest, "00000000000222e0")
        self.assertEqual(len(digest or ""), 16)

    def test_matches_the_documented_implementation(self) -> None:
        rng = random.Random(20261019)
        for size in (131_073, 131_080, 200_000, 1_048_583, 3_000_000):
            with self.subTest(size=size):
                path = self.file(rng.randbytes(size))
                self.assertEqual(movie_hash(path), reference_hash(str(path)))

    def test_files_outside_the_hashed_sizes(self) -> None:
        self.assertIsNone(movie_hash(self.file(bytes(131_072))))
        self.assertIsNone(movie_hash(self.file(b"tiny")))


if __name__ == "__main__":
    unittest.main()
