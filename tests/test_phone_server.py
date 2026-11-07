"""The phone connection's small server, tried for real: TLS on a free port and a raw client."""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import ssl
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from app.config import AppPaths
from app.database import Database
from app.services import SettingsService
from app.services.phone import server as server_module
from app.services.phone.certificate import ensure_certificate
from app.services.phone.devices import DeviceStore
from app.services.phone.server import (
    MAX_BODY,
    Handler,
    PhoneServer,
    Reply,
    Request,
    is_home_address,
)

SURROGATE_UTF8 = b"\xed\xa0\x80"  # how a lone surrogate looks as bytes on the wire


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Answer:
    def __init__(self, raw: bytes) -> None:
        head, _, body = raw.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        self.status = int(lines[0].split(" ")[1])
        self.headers = {
            name.lower(): value.strip()
            for name, _, value in (line.partition(":") for line in lines[1:])
        }
        self.body: dict[str, Any] = json.loads(body)
        self.length = len(body)


def _is_whole(raw: bytes) -> bool:
    head, separator, body = raw.partition(b"\r\n\r\n")
    if not separator:
        return False
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            return len(body) >= int(line.partition(b":")[2])
    return False


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass


async def _assert_closed(case: unittest.TestCase, reader: asyncio.StreamReader) -> None:
    try:
        case.assertEqual(await asyncio.wait_for(reader.read(), 3), b"")
    except ConnectionError:
        pass  # closed hard: just as closed


class ServerCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.db = await Database.open(root / "library.db")
        self.addAsyncCleanup(self.db.close)
        self.settings = await SettingsService.load(self.db.settings)
        self.devices = DeviceStore(self.settings)
        self.phone, self.token = await self.devices.add("Living room phone", 2)
        self.cert, self.key, self.fingerprint = ensure_certificate(AppPaths(root=root))
        self.seen: list[Request] = []
        self.client_tls = ssl.create_default_context()
        self.client_tls.check_hostname = False
        self.client_tls.verify_mode = ssl.CERT_NONE
        self.port = _free_port()
        self.server = self._server({})
        self.addAsyncCleanup(self._stop_server)

    async def _stop_server(self) -> None:
        await self.server.stop()

    def _server(self, extra: dict[tuple[str, str], Handler]) -> PhoneServer:
        routes: dict[tuple[str, str], Handler] = {
            ("GET", "/v1/hello"): self._echo,
            ("POST", "/v1/echo"): self._echo,
            ("POST", "/v1/pair"): self._echo,
            ("GET", "/v1/forge/{id}/search"): self._echo,
            **extra,
        }
        return PhoneServer(routes, self.devices, self.cert, self.key)

    async def _start(self, extra: dict[tuple[str, str], Handler] | None = None) -> None:
        if extra:
            self.server = self._server(extra)
        await self.server.start(self.port)

    async def _echo(self, request: Request) -> Reply:
        self.seen.append(request)
        return Reply(200, {"ok": True})

    async def _connect(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection("127.0.0.1", self.port, ssl=self.client_tls)

    async def _exchange(self, raw: bytes) -> bytes:
        """The answer, read as a real client does: up to its Content-Length, then hang up."""
        reader, writer = await self._connect()
        received = b""
        try:
            writer.write(raw)
            await writer.drain()
            while not _is_whole(received):
                chunk = await asyncio.wait_for(reader.read(65536), 5)
                if not chunk:
                    break
                received += chunk
            return received
        finally:
            await _close(writer)

    async def _in_two_parts(self, head: bytes, body: bytes, delay: float) -> Answer:
        """Head, a pause, body, then read until the server closes: no reset may spoil the answer."""
        reader, writer = await self._connect()
        try:
            writer.write(head)
            await writer.drain()
            await asyncio.sleep(delay)
            writer.write(body)
            await writer.drain()
            return Answer(await asyncio.wait_for(reader.read(), 5))
        finally:
            await _close(writer)

    def _raw_outside(self) -> Any:
        """Every peer looks like it comes from outside the home network."""
        return mock.patch.object(
            server_module, "is_home_address", side_effect=lambda _ip: is_home_address("8.8.8.8")
        )

    async def _call(
        self, method: str, path: str, body: bytes = b"", token: str | None = None
    ) -> Answer:
        auth = f"Authorization: Bearer {token}\r\n".encode() if token else b""
        size = f"Content-Length: {len(body)}\r\n".encode() if body else b""
        head = f"{method} {path} HTTP/1.1\r\nHost: pc\r\n".encode()
        return Answer(await self._exchange(head + auth + size + b"\r\n" + body))


class PairedOnlyTests(ServerCase):
    async def test_no_token_gets_401_not_paired(self) -> None:
        await self._start()
        answer = await self._call("GET", "/v1/hello")
        self.assertEqual(answer.status, 401)
        self.assertEqual(answer.body["error"], "not_paired")
        self.assertTrue(answer.body["message"])
        self.assertEqual(self.seen, [])

    async def test_unknown_token_gets_401(self) -> None:
        await self._start()
        answer = await self._call("GET", "/v1/hello", token="not-a-real-token")
        self.assertEqual((answer.status, answer.body["error"]), (401, "not_paired"))

    async def test_removed_phone_gets_401_on_its_next_request(self) -> None:
        await self._start()
        self.assertEqual((await self._call("GET", "/v1/hello", token=self.token)).status, 200)
        await self.devices.remove(self.phone.id)
        answer = await self._call("GET", "/v1/hello", token=self.token)
        self.assertEqual((answer.status, answer.body["error"]), (401, "not_paired"))
        self.assertEqual(len(self.seen), 1)

    async def test_paired_phone_reaches_the_handler(self) -> None:
        await self._start()
        answer = await self._call(
            "POST", "/v1/echo?from=2026-11-06&days=21&q=a%20b&empty=", b'{"a": [1]}', self.token
        )
        self.assertEqual((answer.status, answer.body), (200, {"ok": True}))
        self.assertEqual(answer.headers["connection"], "close")
        self.assertEqual(answer.headers["content-type"], "application/json; charset=utf-8")
        self.assertEqual(int(answer.headers["content-length"]), answer.length)
        request = self.seen[0]
        self.assertEqual((request.method, request.path), ("POST", "/v1/echo"))
        self.assertEqual(
            request.query, {"from": "2026-11-06", "days": "21", "q": "a b", "empty": ""}
        )
        self.assertEqual(request.body, {"a": [1]})
        self.assertEqual(request.phone, self.phone)

    async def test_the_scheme_word_may_be_any_case(self) -> None:
        await self._start()
        raw = f"GET /v1/hello HTTP/1.1\r\nAUTHORIZATION: bearer {self.token}\r\n\r\n".encode()
        self.assertEqual(Answer(await self._exchange(raw)).status, 200)

    async def test_pairing_needs_no_token(self) -> None:
        await self._start()
        answer = await self._call("POST", "/v1/pair", b'{"code": "x"}')
        self.assertEqual(answer.status, 200)
        self.assertIsNone(self.seen[0].phone)
        self.assertEqual(self.seen[0].body, {"code": "x"})

    async def test_unknown_path_is_not_found_only_for_a_paired_phone(self) -> None:
        await self._start()
        self.assertEqual((await self._call("GET", "/v1/nope")).status, 401)
        answer = await self._call("GET", "/v1/nope", token=self.token)
        self.assertEqual((answer.status, answer.body["error"]), (404, "not_found"))
        wrong_method = await self._call("POST", "/v1/hello", token=self.token)
        self.assertEqual(wrong_method.status, 404)

    async def test_path_value_comes_in_the_query_and_beats_a_sent_one(self) -> None:
        await self._start()
        answer = await self._call(
            "GET", "/v1/forge/a%20b/search?q=dune&id=other", token=self.token
        )
        self.assertEqual(answer.status, 200)
        self.assertEqual(self.seen[0].query, {"id": "a b", "q": "dune"})
        self.assertEqual(self.seen[0].path, "/v1/forge/a%20b/search")

    async def test_an_empty_path_value_does_not_match(self) -> None:
        await self._start()
        answer = await self._call("GET", "/v1/forge//search", token=self.token)
        self.assertEqual(answer.status, 404)


class HomeOnlyTests(ServerCase):
    async def test_outside_address_refused(self) -> None:
        await self._start()
        with self._raw_outside() as seam, self.assertLogs(server_module.log, "WARNING"):
            # The door shuts before TLS: the handshake itself fails, so no certificate is shown.
            with self.assertRaises(OSError):
                await self._connect()
        seam.assert_called_once_with("127.0.0.1")
        self.assertEqual(self.seen, [])

    async def test_an_outside_peer_never_gets_a_byte(self) -> None:
        await self._start()
        with self._raw_outside(), self.assertLogs(server_module.log, "WARNING"):
            reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
            await _assert_closed(self, reader)
            await _close(writer)

    async def test_refusing_outside_peers_does_not_flood_the_log(self) -> None:
        await self._start()
        with self._raw_outside(), self.assertLogs(server_module.log, "WARNING") as logs:
            for _ in range(5):
                with self.assertRaises(OSError):
                    await self._connect()
        self.assertEqual(len(logs.output), 1)

    async def test_the_warning_comes_back_after_a_while(self) -> None:
        await self._start()
        with (
            self._raw_outside(),
            mock.patch.object(server_module, "_WARN_EVERY", 0.0),
            self.assertLogs(server_module.log, "WARNING") as logs,
        ):
            for _ in range(2):
                with self.assertRaises(OSError):
                    await self._connect()
        self.assertEqual(len(logs.output), 2)

    def test_home_addresses_pass(self) -> None:
        for ip in (
            "192.168.1.20",
            "10.0.0.5",
            "172.16.0.9",
            "172.31.255.1",
            "169.254.10.10",
            "127.0.0.1",
            "::1",
            "fe80::1",
            "fe80::1%12",
            "::ffff:192.168.1.20",
        ):
            with self.subTest(ip):
                self.assertTrue(is_home_address(ip))

    def test_other_addresses_do_not_pass(self) -> None:
        for ip in (
            "8.8.8.8",
            "172.32.0.1",
            "100.64.0.1",
            "192.0.2.1",
            "0.0.0.0",
            "::",
            "2001:4860:4860::8888",
            "::ffff:8.8.8.8",
            "not an address",
            "",
        ):
            with self.subTest(ip):
                self.assertFalse(is_home_address(ip))


class BadInputTests(ServerCase):
    async def test_body_over_64_kb_refused(self) -> None:
        await self._start()
        self.assertEqual(MAX_BODY, 64 * 1024)
        # Refused on what it announces, without waiting for it to arrive.
        answer = Answer(
            await self._exchange(
                f"POST /v1/echo HTTP/1.1\r\nAuthorization: Bearer {self.token}\r\n"
                f"Content-Length: {MAX_BODY + 1}\r\n\r\n".encode()
            )
        )
        self.assertEqual((answer.status, answer.body["error"]), (413, "bad_request"))
        self.assertEqual(self.seen, [])

    async def test_a_body_of_exactly_64_kb_is_read(self) -> None:
        await self._start()
        wrapper = b'{"a": ""}'
        body = wrapper[:-2] + b"x" * (MAX_BODY - len(wrapper)) + wrapper[-2:]
        self.assertEqual(len(body), MAX_BODY)
        answer = await self._call("POST", "/v1/echo", body, self.token)
        self.assertEqual(answer.status, 200)
        self.assertEqual(len(self.seen[0].body["a"]), MAX_BODY - len(wrapper))

    async def test_bodies_that_are_not_a_json_object_are_refused(self) -> None:
        await self._start()
        for body in (
            b"not json",
            b"[1, 2]",
            b'"text"',
            b"\xff\xfe\xfa",
            b"[" * 60000,
            b'{"a": ' * 10000,
        ):
            with self.subTest(body[:10]):
                answer = await self._call("POST", "/v1/echo", body, self.token)
                self.assertEqual((answer.status, answer.body["error"]), (400, "bad_request"))
        self.assertEqual(self.seen, [])

    async def test_broken_requests_are_refused(self) -> None:
        await self._start()
        auth = f"Authorization: Bearer {self.token}\r\n".encode()
        for raw in (
            b"nonsense\r\n\r\n",
            b"GET /v1/hello SPDY/3\r\n\r\n",
            b"GET /v1/hello HTTP/1.1\r\nno colon here\r\n\r\n",
            b"POST /v1/echo HTTP/1.1\r\n" + auth + b"Content-Length: -1\r\n\r\n",
            b"POST /v1/echo HTTP/1.1\r\n" + auth + b"Content-Length: 1_0\r\n\r\n",
            b"POST /v1/echo HTTP/1.1\r\n" + auth + b"Content-Length: 12x\r\n\r\n",
            b"POST /v1/echo HTTP/1.1\r\n" + auth + b"Transfer-Encoding: chunked\r\n\r\n",
            b"GET /v1/hello HTTP/1.1\r\nX-Long: " + b"x" * 40000 + b"\r\n\r\n",
        ):
            with self.subTest(raw[:40]):
                answer = Answer(await self._exchange(raw))
                self.assertEqual((answer.status, answer.body["error"]), (400, "bad_request"))
        self.assertEqual(self.seen, [])

    async def test_an_endless_content_length_is_too_big_not_a_crash(self) -> None:
        await self._start()
        raw = (
            f"POST /v1/echo HTTP/1.1\r\nAuthorization: Bearer {self.token}\r\n".encode()
            + b"Content-Length: "
            + b"9" * 5000
            + b"\r\n\r\n"
        )
        answer = Answer(await self._exchange(raw))
        self.assertEqual((answer.status, answer.body["error"]), (413, "bad_request"))

    async def test_a_bearer_token_with_a_lone_surrogate_is_not_paired(self) -> None:
        await self._start()
        raw = b"GET /v1/hello HTTP/1.1\r\nAuthorization: Bearer " + SURROGATE_UTF8 + b"\r\n\r\n"
        answer = Answer(await self._exchange(raw))
        self.assertEqual((answer.status, answer.body["error"]), (401, "not_paired"))

    def test_a_lone_surrogate_in_a_header_never_reaches_the_device_store(self) -> None:
        # DeviceStore.find would raise on it, however a header came to hold one.
        self.assertIsNone(server_module._bearer_token({"authorization": "Bearer \ud800"}))
        self.assertEqual(server_module._bearer_token({"authorization": "Bearer abc"}), "abc")
        self.assertIsNone(server_module._bearer_token({"authorization": "Basic abc"}))
        self.assertIsNone(server_module._bearer_token({"authorization": "Bearer "}))
        self.assertIsNone(server_module._bearer_token({}))

    async def test_a_json_body_with_a_lone_surrogate_is_refused(self) -> None:
        await self._start()
        for body in (
            b'{"code": "\\ud800"}',
            b'{"\\udc00": 1}',
            b'{"deep": [{"code": "\\ud800"}]}',
            b'{"code": "' + SURROGATE_UTF8 + b'"}',
        ):
            for path, token in (("/v1/pair", None), ("/v1/echo", self.token)):
                with self.subTest(body=body, path=path):
                    answer = await self._call("POST", path, body, token)
                    self.assertEqual((answer.status, answer.body["error"]), (400, "bad_request"))
        self.assertEqual(self.seen, [])

    async def test_a_query_or_path_with_a_lone_surrogate_gives_encodable_text(self) -> None:
        await self._start()
        answer = await self._call(
            "GET", "/v1/forge/%ED%A0%80/search?q=%ED%A0%80", token=self.token
        )
        self.assertEqual(answer.status, 200)
        self.assertEqual(sorted(self.seen[0].query), ["id", "q"])
        for value in self.seen[0].query.values():
            value.encode()

    async def test_a_proper_pair_of_surrogates_is_fine(self) -> None:
        await self._start()
        answer = await self._call("POST", "/v1/echo", b'{"t": "\\ud83c\\udfac"}', self.token)
        self.assertEqual(answer.status, 200)
        self.assertEqual(self.seen[0].body, {"t": "\U0001f3ac"})

    async def test_a_request_that_stalls_is_given_up_on(self) -> None:
        await self._start()
        with mock.patch.object(server_module, "READ_TIMEOUT", 0.1):
            for raw in (
                b"GET /v1/hel",
                f"POST /v1/echo HTTP/1.1\r\nAuthorization: Bearer {self.token}\r\n"
                "Content-Length: 10\r\n\r\n{".encode(),
            ):
                with self.subTest(raw[:10]):
                    answer = Answer(await self._exchange(raw))
                    self.assertEqual((answer.status, answer.body["error"]), (408, "bad_request"))
        self.assertEqual(self.seen, [])


class EarlyAnswerTests(ServerCase):
    """An answer before the body has arrived must still reach the phone."""

    DELAYS = (0.02, 0.1, 0.5)

    async def test_a_removed_phone_reads_its_401_when_the_body_comes_late(self) -> None:
        await self._start()
        await self.devices.remove(self.phone.id)
        body = b'{"a": 1}'
        head = (
            f"POST /v1/echo HTTP/1.1\r\nAuthorization: Bearer {self.token}\r\n"
            f"Content-Length: {len(body)}\r\n\r\n".encode()
        )
        for delay in self.DELAYS:
            with self.subTest(delay):
                answer = await self._in_two_parts(head, body, delay)
                self.assertEqual((answer.status, answer.body["error"]), (401, "not_paired"))
        self.assertEqual(self.seen, [])

    async def test_too_big_a_body_coming_late_still_reads_its_413(self) -> None:
        await self._start()
        body = b"x" * (MAX_BODY + 1000)
        head = (
            f"POST /v1/echo HTTP/1.1\r\nAuthorization: Bearer {self.token}\r\n"
            f"Content-Length: {len(body)}\r\n\r\n".encode()
        )
        for delay in self.DELAYS:
            with self.subTest(delay):
                answer = await self._in_two_parts(head, body, delay)
                self.assertEqual((answer.status, answer.body["error"]), (413, "bad_request"))
        self.assertEqual(self.seen, [])

    async def test_an_unknown_path_reads_its_404_when_the_body_comes_late(self) -> None:
        await self._start()
        head = (
            f"POST /v1/nope HTTP/1.1\r\nAuthorization: Bearer {self.token}\r\n"
            "Content-Length: 2\r\n\r\n".encode()
        )
        answer = await self._in_two_parts(head, b"{}", 0.1)
        self.assertEqual((answer.status, answer.body["error"]), (404, "not_found"))

    async def test_a_body_that_never_comes_is_waited_for_only_briefly(self) -> None:
        await self._start()
        head = b"POST /v1/echo HTTP/1.1\r\nContent-Length: 10\r\n\r\n"
        with mock.patch.object(server_module, "LINGER_TIME", 0.1):
            reader, writer = await self._connect()
            writer.write(head)
            await writer.drain()
            answer = Answer(await asyncio.wait_for(reader.read(), 3))
            await _close(writer)
        self.assertEqual(answer.status, 401)

    async def test_with_nothing_left_to_come_it_closes_at_once(self) -> None:
        await self._start()
        with mock.patch.object(server_module, "LINGER_TIME", 30.0):
            reader, writer = await self._connect()
            writer.write(b"GET /v1/hello HTTP/1.1\r\n\r\n")
            await writer.drain()
            answer = Answer(await asyncio.wait_for(reader.read(), 3))
            await _close(writer)
        self.assertEqual(answer.status, 401)

    async def test_stop_ends_the_wait_for_a_late_body(self) -> None:
        await self._start()
        with mock.patch.object(server_module, "LINGER_TIME", 30.0):
            reader, writer = await self._connect()
            writer.write(b"POST /v1/echo HTTP/1.1\r\nContent-Length: 10\r\n\r\n")
            await writer.drain()
            await asyncio.wait_for(reader.readuntil(b'"}'), 3)  # the 401 is here
            await asyncio.wait_for(self.server.stop(), 3)
        await _assert_closed(self, reader)
        await _close(writer)


class ConnectionLimitTests(ServerCase):
    async def _fill(self) -> list[asyncio.StreamWriter]:
        """Every place taken by a connection that never starts TLS."""
        writers = []
        for _ in range(server_module.MAX_CONNECTIONS):
            _, writer = await asyncio.open_connection("127.0.0.1", self.port)
            writers.append(writer)
            self.addAsyncCleanup(_close, writer)
        for _ in range(100):
            if len(self.server._connections) == server_module.MAX_CONNECTIONS:
                break
            await asyncio.sleep(0.02)
        self.assertEqual(len(self.server._connections), server_module.MAX_CONNECTIONS)
        return writers

    async def test_one_too_many_is_closed_at_once_before_tls(self) -> None:
        self.assertEqual(server_module.MAX_CONNECTIONS, 32)
        await self._start()
        await self._fill()
        with self.assertLogs(server_module.log, "WARNING"):
            with self.assertRaises(OSError):
                await asyncio.wait_for(self._connect(), 3)
        self.assertEqual(len(self.server._connections), server_module.MAX_CONNECTIONS)

    async def test_too_many_connections_do_not_flood_the_log(self) -> None:
        await self._start()
        await self._fill()
        with self.assertLogs(server_module.log, "WARNING") as logs:
            for _ in range(5):
                with self.assertRaises(OSError):
                    await asyncio.wait_for(self._connect(), 3)
        self.assertEqual(len(logs.output), 1)

    async def test_a_place_comes_free_when_a_connection_ends(self) -> None:
        await self._start()
        writers = await self._fill()
        await _close(writers[0])
        for _ in range(100):
            if len(self.server._connections) < server_module.MAX_CONNECTIONS:
                break
            await asyncio.sleep(0.02)
        answer = await self._call("GET", "/v1/hello", token=self.token)
        self.assertEqual(answer.status, 200)

    async def test_stop_closes_a_full_house(self) -> None:
        await self._start()
        await self._fill()
        await asyncio.wait_for(self.server.stop(), 5)
        self.assertEqual(len(self.server._connections), 0)


class HandlerTests(ServerCase):
    async def test_a_handler_error_returns_failed_and_logs_no_token(self) -> None:
        secret_path = r"D:\Films\Secret film.mkv"

        async def broken(_request: Request) -> Reply:
            raise RuntimeError(f"{self.token} {secret_path}")

        await self._start({("POST", "/v1/broken"): broken})
        with self.assertLogs(server_module.log, "ERROR") as logs:
            answer = await self._call("POST", "/v1/broken?q=secret-query", b"{}", self.token)
        self.assertEqual((answer.status, answer.body["error"]), (500, "failed"))
        text = "\n".join(logs.output)
        self.assertIn("RuntimeError", text)
        self.assertIn("/v1/broken", text)
        for secret in (self.token, secret_path, "secret-query"):
            self.assertNotIn(secret, text)
            self.assertNotIn(secret, json.dumps(answer.body))

    async def test_an_error_while_checking_the_phone_returns_failed(self) -> None:
        await self._start()
        secret = "secret-in-the-exception"
        with (
            mock.patch.object(self.devices, "find", side_effect=RuntimeError(secret)),
            self.assertLogs(server_module.log, "ERROR") as logs,
        ):
            answer = await self._call("GET", "/v1/hello", token=self.token)
        self.assertEqual((answer.status, answer.body["error"]), (500, "failed"))
        self.assertNotIn(secret, "\n".join(logs.output))

    async def test_a_reply_that_is_not_json_returns_failed(self) -> None:
        async def odd(_request: Request) -> Reply:
            return Reply(200, {"when": object()})

        await self._start({("GET", "/v1/odd"): odd})
        with self.assertLogs(server_module.log, "ERROR"):
            answer = await self._call("GET", "/v1/odd", token=self.token)
        self.assertEqual((answer.status, answer.body["error"]), (500, "failed"))

    async def test_a_handler_can_answer_with_its_own_error(self) -> None:
        async def gone(_request: Request) -> Reply:
            return server_module.error_reply(409, "no_longer_there", "Gone.")

        await self._start({("GET", "/v1/gone"): gone})
        answer = await self._call("GET", "/v1/gone", token=self.token)
        self.assertEqual(answer.status, 409)
        self.assertEqual(answer.body, {"error": "no_longer_there", "message": "Gone."})

    async def test_a_slow_handler_is_not_held_to_the_read_timeout(self) -> None:
        async def slow(_request: Request) -> Reply:
            await asyncio.sleep(0.3)
            return Reply(200, {"ok": True})

        await self._start({("GET", "/v1/slow"): slow})
        with mock.patch.object(server_module, "READ_TIMEOUT", 0.1):
            answer = await self._call("GET", "/v1/slow", token=self.token)
        self.assertEqual(answer.status, 200)


class LifetimeTests(ServerCase):
    async def test_running_follows_start_and_stop(self) -> None:
        self.assertFalse(self.server.running)
        await self.server.start(self.port)
        self.assertTrue(self.server.running)
        await self.server.stop()
        self.assertFalse(self.server.running)
        await self.server.stop()  # stopping twice is fine
        with self.assertRaises(OSError):
            await self._connect()

    async def test_it_starts_again_after_a_stop(self) -> None:
        await self.server.start(self.port)
        await self.server.stop()
        await self.server.start(self.port)
        self.assertEqual((await self._call("GET", "/v1/hello", token=self.token)).status, 200)

    async def test_port_in_use_raises_and_stays_off(self) -> None:
        with socket.socket() as taken:
            taken.bind(("0.0.0.0", self.port))
            taken.listen()
            with self.assertRaises(OSError):
                await self.server.start(self.port)
        self.assertFalse(self.server.running)

    async def test_the_server_closes_after_each_answer(self) -> None:
        await self._start()
        reader, writer = await self._connect()
        writer.write(
            f"GET /v1/hello HTTP/1.1\r\nAuthorization: Bearer {self.token}\r\n\r\n".encode()
        )
        await writer.drain()
        self.assertEqual(Answer(await asyncio.wait_for(reader.read(), 3)).status, 200)
        await _close(writer)

    async def test_it_speaks_tls_with_its_own_certificate(self) -> None:
        await self._start()
        reader, writer = await self._connect()
        der = writer.get_extra_info("ssl_object").getpeercert(binary_form=True)
        writer.write(b"GET /v1/hello HTTP/1.1\r\n\r\n")
        await writer.drain()
        await asyncio.wait_for(reader.read(), 3)
        await _close(writer)
        self.assertEqual(hashlib.sha256(der).hexdigest(), self.fingerprint)

    async def test_stop_closes_open_connections(self) -> None:
        await self._start()
        reader, writer = await self._connect()  # past TLS, sends nothing
        plain_reader, plain_writer = await asyncio.open_connection("127.0.0.1", self.port)
        await asyncio.sleep(0.05)  # plain_writer's connection is waiting for TLS by now
        await asyncio.wait_for(self.server.stop(), 3)
        await _assert_closed(self, reader)
        await _assert_closed(self, plain_reader)
        await _close(writer)
        await _close(plain_writer)

    async def test_stop_ends_a_request_still_being_worked_on(self) -> None:
        started = asyncio.Event()
        ended = asyncio.Event()

        async def endless(_request: Request) -> Reply:
            started.set()
            try:
                await asyncio.sleep(3600)
            finally:
                ended.set()
            return Reply(200, {})

        await self._start({("GET", "/v1/endless"): endless})
        reader, writer = await self._connect()
        writer.write(
            f"GET /v1/endless HTTP/1.1\r\nAuthorization: Bearer {self.token}\r\n\r\n".encode()
        )
        await writer.drain()
        await asyncio.wait_for(started.wait(), 3)
        await asyncio.wait_for(self.server.stop(), 3)
        self.assertTrue(ended.is_set())
        await _assert_closed(self, reader)
        await _close(writer)


if __name__ == "__main__":
    unittest.main()
