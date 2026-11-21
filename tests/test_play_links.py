"""Play links (app/services/phone/play_links.py): short-lived, unguessable plain-http links to a
folder library video and its subtitles, for the phone's own player and the TVs. A real link server
on 127.0.0.1 and a free port, a fake clock, temporary folders; the route that makes a link through
PhoneLibrary, and the phone connection that starts and stops the server."""

from __future__ import annotations

import asyncio
import contextlib
import socket
import ssl
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from unittest import mock
from urllib.parse import urlsplit

from app.config import AppPaths
from app.i18n import t
from app.services.phone import library as library_module
from app.services.phone import play_links as play_links_module
from app.services.phone.cast_stream import Probed
from app.services.phone.certificate import ensure_certificate
from app.services.phone.devices import DeviceStore
from app.services.phone.library import PLEX_LIBRARY_TEXT, PhoneLibrary
from app.services.phone.play_links import (
    NOT_SERVING_TEXT,
    PLAY_LINK_HOURS,
    PLAY_PORT,
    PLAY_PORTS,
    TOKEN_BYTES,
    PlayLinks,
)
from app.services.phone.server import PhoneServer, Reply, Request, range_span
from app.services.phone.service import PORT_BUSY_TEXT, PhoneService
from tests.plex_link import TOKEN
from tests.test_phone_library import DUNE, DUNE_ID, GONE, LibraryCase, film
from tests.test_phone_service import HOME, PLEX, ServiceCase

NOW = datetime(2026, 11, 16, 20, 0, tzinfo=UTC)
DLNA_FEATURES = "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000"
PLAY_LINK = ("POST", "/v1/library/{key}/play-link")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


class Fetched:
    def __init__(self, raw: bytes) -> None:
        head, _, self.body = raw.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        self.status = int(lines[0].split(" ")[1])
        self.headers = {
            name.lower(): value.strip()
            for name, _, value in (line.partition(":") for line in lines[1:])
        }


async def fetch(port: int, method: str, path: str, headers: str = "") -> Fetched:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(f"{method} {path} HTTP/1.1\r\nHost: pc\r\n{headers}\r\n".encode())
        await writer.drain()
        return Fetched(await asyncio.wait_for(reader.read(), 5))
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


class LinksCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.video = self.root / "The Matrix (1999).mkv"
        self.data = bytes(range(10)) * 3  # 30 bytes
        self.video.write_bytes(self.data)
        self.now = NOW
        self.links = PlayLinks(now=lambda: self.now)
        self.port = _free_port()
        await self.links.start(self.port)
        self.addAsyncCleanup(self.links.stop)

    async def get(self, path: str, headers: str = "", method: str = "GET") -> Fetched:
        return await fetch(self.port, method, path, headers)


class ServingTests(LinksCase):
    async def test_ranges_as_tvs_ask_them(self) -> None:
        token, _ = self.links.make(self.video, [])
        path = f"/p/{token}/video.mkv"
        size = len(self.data)

        whole = await self.get(path)
        self.assertEqual(whole.status, 200)
        self.assertEqual(whole.body, self.data)
        self.assertEqual(whole.headers["accept-ranges"], "bytes")
        self.assertEqual(whole.headers["content-type"], "video/x-matroska")
        self.assertEqual(int(whole.headers["content-length"]), size)
        self.assertEqual(whole.headers["access-control-allow-origin"], "*")
        self.assertEqual(whole.headers["transfermode.dlna.org"], "Streaming")
        self.assertEqual(whole.headers["contentfeatures.dlna.org"], DLNA_FEATURES)

        head = await self.get(path, method="HEAD")
        self.assertEqual(head.status, 200)
        self.assertEqual(head.body, b"")
        self.assertEqual(head.headers, whole.headers)

        part = await self.get(path, "Range: bytes=2-4\r\n")
        self.assertEqual(part.status, 206)
        self.assertEqual(part.body, self.data[2:5])
        self.assertEqual(int(part.headers["content-length"]), 3)
        self.assertEqual(part.headers["content-range"], f"bytes 2-4/{size}")
        self.assertEqual(part.headers["transfermode.dlna.org"], "Streaming")

        rest = await self.get(path, "Range: bytes=5-\r\n")
        self.assertEqual(rest.status, 206)
        self.assertEqual(rest.body, self.data[5:])
        self.assertEqual(rest.headers["content-range"], f"bytes 5-{size - 1}/{size}")

        past_the_end = await self.get(path, "Range: bytes=999-\r\n")
        self.assertEqual(past_the_end.status, 416)
        self.assertEqual(past_the_end.headers["content-range"], f"bytes */{size}")
        self.assertEqual(past_the_end.body, b"")

        too_far = await self.get(path, "Range: bytes=20-999\r\n")  # the end cut to the file's
        self.assertEqual((too_far.status, too_far.body), (206, self.data[20:]))

        head_part = await self.get(path, "Range: bytes=2-4\r\n", method="HEAD")
        self.assertEqual((head_part.status, head_part.body), (206, b""))
        self.assertEqual(head_part.headers["content-range"], f"bytes 2-4/{size}")

    async def test_only_get_and_head(self) -> None:
        token, _ = self.links.make(self.video, [])
        answer = await self.get(f"/p/{token}/video.mkv", method="POST")
        self.assertEqual(answer.status, 405)
        self.assertEqual(answer.body, b"")

    async def test_a_link_is_for_its_own_paths_only(self) -> None:
        token, _ = self.links.make(self.video, [])
        for path in (
            f"/p/{token}/video.mp4",
            f"/p/{token}/other.mkv",
            f"/p/{token}/sub/0.vtt",
            f"/p/{token}",
            f"/p/{token}/video.mkv/more",
            "/",
            "/p//video.mkv",
        ):
            with self.subTest(path):
                self.assertEqual((await self.get(path)).status, 404)
        self.assertEqual((await self.get(f"/p/{token}/VIDEO.MKV?t=1")).status, 200)

    async def test_a_file_gone_since_is_404(self) -> None:
        token, _ = self.links.make(self.video, [])
        self.video.unlink()
        self.assertEqual((await self.get(f"/p/{token}/video.mkv")).status, 404)

    async def test_a_new_link_doesnt_end_the_old_one(self) -> None:
        first, _ = self.links.make(self.video, [])
        second, _ = self.links.make(self.video, [])
        self.assertNotEqual(first, second)
        for token in (first, second):
            self.assertEqual((await self.get(f"/p/{token}/video.mkv")).status, 200)

    async def test_broken_requests_get_an_answer(self) -> None:
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        try:
            writer.write(b"nonsense\r\n\r\n")
            await writer.drain()
            answer = Fetched(await asyncio.wait_for(reader.read(), 5))
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
        self.assertEqual(answer.status, 400)


class LinkTests(LinksCase):
    async def test_links_expire_and_are_never_logged(self) -> None:
        with self.assertLogs("app.services.phone", "DEBUG") as logs:
            token, expires = self.links.make(self.video, [])
            self.assertEqual(expires, NOW + timedelta(hours=PLAY_LINK_HOURS))
            self.assertGreaterEqual(len(token), TOKEN_BYTES * 4 // 3)  # token_urlsafe(24)
            guessed = "A" * len(token)
            self.assertEqual((await self.get(f"/p/{guessed}/video.mkv")).status, 404)
            self.assertEqual((await self.get(f"/p/{token}/video.mkv")).status, 200)
            self.now = NOW + timedelta(hours=PLAY_LINK_HOURS) - timedelta(seconds=1)
            self.assertEqual((await self.get(f"/p/{token}/video.mkv")).status, 200)
            self.now = NOW + timedelta(hours=PLAY_LINK_HOURS)
            self.assertEqual((await self.get(f"/p/{token}/video.mkv")).status, 404)
            self.assertEqual((await self.get(f"/p/{token}/video.mkv", method="HEAD")).status, 404)
            await self.links.stop()
            await self.links.start(self.port)
            play_links_module.log.debug("(something logged)")
        text = "\n".join(logs.output)
        self.assertIn("Play link server listening on port", text)
        self.assertIn("Play link server stopped", text)
        for secret in (token, "/p/", "Matrix", self.root.name, str(self.video), "127.0.0.1"):
            self.assertNotIn(secret, text)

    async def test_expired_links_are_dropped_when_a_new_one_is_made(self) -> None:
        old, _ = self.links.make(self.video, [])
        self.now = NOW + timedelta(hours=PLAY_LINK_HOURS, minutes=1)
        new, _ = self.links.make(self.video, [])
        self.assertEqual(set(self.links._links), {new})
        self.assertNotIn(old, self.links._links)

    async def test_links_are_gone_with_the_server(self) -> None:
        token, _ = self.links.make(self.video, [])
        await self.links.stop()
        self.assertFalse(self.links.running)
        await self.links.start(self.port)
        self.assertTrue(self.links.running)
        self.assertEqual((await self.get(f"/p/{token}/video.mkv")).status, 404)

    async def test_outside_addresses_are_refused(self) -> None:
        reader = mock.Mock(spec=asyncio.StreamReader)
        writer = mock.Mock(spec=asyncio.StreamWriter)
        writer.get_extra_info.side_effect = lambda name, default=None: (
            ("8.8.8.8", 51000) if name == "peername" else default
        )
        with self.assertLogs(play_links_module.log, "WARNING") as logs:
            for _ in range(3):
                self.links._accept(reader, writer)
        self.assertEqual(writer.close.call_count, 3)
        self.assertEqual(reader.method_calls, [])  # nothing read
        self.assertEqual(writer.write.call_count, 0)  # nothing said
        self.assertEqual(self.links._connections, set())
        self.assertEqual(len(logs.output), 1)  # at most once a minute
        self.assertNotIn("8.8.8.8", "\n".join(logs.output))

    async def test_too_many_connections_are_refused(self) -> None:
        reader = mock.Mock(spec=asyncio.StreamReader)
        writer = mock.Mock(spec=asyncio.StreamWriter)
        writer.get_extra_info.side_effect = lambda name, default=None: (
            ("127.0.0.1", 51000) if name == "peername" else default
        )
        with mock.patch.object(play_links_module, "MAX_CONNECTIONS", 0):
            with self.assertLogs(play_links_module.log, "WARNING"):
                self.links._accept(reader, writer)
        writer.close.assert_called_once()
        self.assertEqual(self.links._connections, set())

    async def test_subtitles_go_as_vtt(self) -> None:
        srt = self.root / "The Matrix (1999).nl.srt"
        srt.write_bytes("1\r\n00:00:01,500 --> 00:00:03,000\r\nHallo.\r\n".encode("utf-8-sig"))
        vtt = self.root / "The Matrix (1999).en.vtt"
        vtt.write_bytes(b"WEBVTT\n\n00:01.000 --> 00:02.000\nHello.\n")
        ass = self.root / "The Matrix (1999).de.ass"
        ass.write_bytes(b"[Script Info]\r\n")
        token, _ = self.links.make(self.video, [srt, vtt, ass])

        converted = await self.get(f"/p/{token}/sub/0.vtt")
        self.assertEqual(converted.status, 200)
        self.assertTrue(converted.headers["content-type"].startswith("text/vtt"))
        self.assertTrue(converted.body.startswith(b"WEBVTT"))
        self.assertIn(b"00:00:01.500 --> 00:00:03.000", converted.body)
        self.assertEqual(converted.headers["access-control-allow-origin"], "*")
        self.assertEqual(int(converted.headers["content-length"]), len(converted.body))

        as_it_is = await self.get(f"/p/{token}/sub/1.vtt")
        self.assertEqual((as_it_is.status, as_it_is.body), (200, vtt.read_bytes()))
        head = await self.get(f"/p/{token}/sub/1.vtt", method="HEAD")
        self.assertEqual((head.status, head.body), (200, b""))
        self.assertEqual(int(head.headers["content-length"]), len(vtt.read_bytes()))

        for path in ("sub/2.vtt", "sub/3.vtt", "sub/01.vtt", "sub/x.vtt", "sub/0.srt"):
            with self.subTest(path):
                self.assertEqual((await self.get(f"/p/{token}/{path}")).status, 404)
        srt.unlink()
        self.assertEqual((await self.get(f"/p/{token}/sub/0.vtt")).status, 404)


class RangeSpanTests(unittest.TestCase):
    def test_range_span(self) -> None:
        """None: the whole file; False: past the end (416); else (first, last), both included."""
        for wanted, span in (
            ("", None),
            ("bytes=5-", (5, 99)),
            ("bytes=0-", (0, 99)),
            ("bytes=5-9", (5, 9)),
            ("bytes=5-5", (5, 5)),
            ("bytes=5-500", (5, 99)),
            ("bytes=9-5", None),  # not understood: the whole file
            ("items=5-", None),
            ("bytes=-5", None),
            ("bytes=²-", None),
            ("bytes=5-²", None),
            ("bytes=99-", (99, 99)),
            ("bytes=100-", False),
            ("bytes=100-200", False),
            ("bytes=" + "9" * 5000 + "-", None),  # too long a number: not understood, no error
            ("bytes=5-" + "9" * 5000, None),
        ):
            with self.subTest(wanted):
                found = range_span({"range": wanted} if wanted else {}, 100)
                self.assertEqual(found, span)


class LocalAddressTests(unittest.IsolatedAsyncioTestCase):
    async def test_the_phone_server_says_which_address_it_was_reached_on(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        seen: list[Request] = []

        async def handler(request: Request) -> Reply:
            seen.append(request)
            return Reply(200, {})

        from app.database import Database
        from app.models import Settings
        from app.services.settings import SettingsService

        db = await Database.open(root / "library.db")
        self.addAsyncCleanup(db.close)
        devices = DeviceStore(SettingsService(db.settings, Settings()))
        _, token = await devices.add("Pixel", 1)
        cert, key, _ = await asyncio.to_thread(ensure_certificate, AppPaths(root=root))
        port = _free_port()
        server = PhoneServer({("GET", "/v1/thing"): handler}, devices, cert, key)
        await server.start(port)
        self.addAsyncCleanup(server.stop)
        client = ssl.create_default_context()
        client.check_hostname = False
        client.verify_mode = ssl.CERT_NONE
        reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=client)
        try:
            writer.write(
                f"GET /v1/thing HTTP/1.1\r\nAuthorization: Bearer {token}\r\n\r\n".encode()
            )
            await writer.drain()
            await asyncio.wait_for(reader.read(), 5)
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()
        self.assertEqual([one.local_address for one in seen], ["127.0.0.1"])


class RouteTests(LibraryCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.links = PlayLinks(now=lambda: NOW)
        self.play_port = _free_port()
        await self.links.start(self.play_port)
        self.addAsyncCleanup(self.links.stop)
        self.probed: list[Path] = []
        self.probe_answer: Probed | None = None

        async def probe(path: Path) -> Probed | None:
            self.probed.append(path)
            return self.probe_answer

        self.library = PhoneLibrary(
            self.settings,
            self.playing,
            self.db.library_files,
            self.db.watch_places,
            cast(Any, self.discovery),
            cast(Any, self.tmdb),
            play_links=self.links,
            probe=probe,
        )
        self.video = self.root / "Dune.2160p.MKV"
        self.data = b"dune" * 10
        self.video.write_bytes(self.data)
        (self.root / "Dune.2160p.nl.srt").write_bytes(b"1\n00:00:01,000 --> 00:00:02,000\nHoi\n")
        (self.root / "Dune.2160p.eng.ass").write_bytes(b"[Script Info]\n")
        (self.root / "Dune.2160p.en.forced.vtt").write_bytes(b"WEBVTT\n")
        await self.store((DUNE,), [film(str(self.video), DUNE_ID, "4K", len(self.data))])

    async def play_link(self, local_address: str, key: str = "movie:438631") -> Reply:
        request = Request(
            "POST", "/v1/library/movie:438631/play-link", {"key": key}, {}, self.phone, {},
            local_address=local_address,
        )
        reply = await self.library.routes()[PLAY_LINK](request)
        assert isinstance(reply, Reply), reply
        return reply

    async def test_the_route_gives_a_link_on_the_address_the_phone_used(self) -> None:
        reply = await self.play_link("192.168.1.20")
        self.assertEqual(reply.status, 200, reply.body)
        url = reply.body["url"]
        # The port the link server got (PLAY_PORTS: 58762 first, then 47762, then a free one).
        self.assertTrue(url.startswith(f"http://192.168.1.20:{self.play_port}/p/"), url)
        self.assertTrue(url.endswith("/video.mkv"), url)
        self.assertEqual(PLAY_PORT, 58762)
        self.assertEqual(PLAY_PORTS, (58762, 47762, 0))
        # Without ffprobe's word the codecs are null.
        self.assertEqual(
            (reply.body["video_codec"], reply.body["height"], reply.body["audio_codec"]),
            (None, None, None),
        )
        self.assertEqual(self.probed, [self.video])
        self.assertEqual(
            datetime.fromisoformat(reply.body["expires_at"]), NOW + timedelta(hours=6)
        )
        # Only SRT and VTT, each under the id `subtitles` gives it, as a link of its own.
        listed = await self.ask(("GET", "/v1/library/{key}/subtitles"), key="movie:438631")
        ids = {one["format"]: one["id"] for one in listed.body["subtitles"]}
        base = url.rsplit("/", 1)[0]
        self.assertEqual(
            sorted(reply.body["subtitles"], key=lambda one: one["id"]),
            sorted(
                [
                    {"id": ids["srt"], "language": "nl", "forced": False,
                     "url": f"{base}/sub/{ids['srt']}.vtt"},
                    {"id": ids["vtt"], "language": "en", "forced": True,
                     "url": f"{base}/sub/{ids['vtt']}.vtt"},
                ],
                key=lambda one: one["id"],
            ),
        )
        # And the link works, on the link server.
        port = self.play_port
        path = urlsplit(url).path
        self.assertEqual((await fetch(port, "GET", path)).body, self.data)
        srt = next(one for one in reply.body["subtitles"] if one["id"] == ids["srt"])
        subtitle = await fetch(port, "GET", urlsplit(srt["url"]).path)
        self.assertTrue(subtitle.body.startswith(b"WEBVTT"))
        self.assertIn(b"Hoi", subtitle.body)

    async def test_an_ipv6_address_goes_in_brackets(self) -> None:
        for local, host in (
            ("fe80::1", "[fe80::1]"),
            ("::ffff:192.168.1.20", "192.168.1.20"),
            ("fe80::1%12", "[fe80::1%2512]"),
        ):
            with self.subTest(local):
                reply = await self.play_link(local)
                self.assertEqual(reply.status, 200, reply.body)
                self.assertTrue(
                    reply.body["url"].startswith(f"http://{host}:{self.play_port}/p/"),
                    reply.body["url"],
                )

    async def test_the_codecs_for_the_phones_choice(self) -> None:
        self.probe_answer = Probed(
            "hevc", (2, 6), 100.0, 2160, True, ("aac", "eac3"), default_sound=1
        )
        reply = await self.play_link("192.168.1.20")
        self.assertEqual(
            (reply.body["video_codec"], reply.body["height"], reply.body["audio_codec"]),
            ("hevc", 2160, "eac3"),  # the default sound track's, not the first's
        )
        self.probe_answer = Probed("h264", (), 100.0, None, False, (), None)  # no sound
        reply = await self.play_link("192.168.1.20")
        self.assertEqual(
            (reply.body["video_codec"], reply.body["height"], reply.body["audio_codec"]),
            ("h264", None, None),
        )

    async def test_a_link_names_the_port_it_got_and_none_without_one(self) -> None:
        await self.links.stop()
        taken = socket.socket()
        self.addCleanup(taken.close)
        taken.bind(("0.0.0.0", 0))
        taken.listen()
        busy = int(taken.getsockname()[1])
        await self.links.start((busy, 0))  # as 58762 kept by Windows: a free one then
        self.assertNotIn(self.links.port, (None, 0, busy))
        reply = await self.play_link("192.168.1.20")
        self.assertEqual(urlsplit(reply.body["url"]).port, self.links.port)
        fetched = await fetch(self.links.port or 0, "GET", urlsplit(reply.body["url"]).path)
        self.assertEqual(fetched.body, self.data)
        await self.links.stop()
        self.assertIsNone(self.links.port)
        reply = await self.play_link("192.168.1.20")
        self.assertEqual(
            (reply.status, reply.body),
            (503, {"error": "failed", "message": t(NOT_SERVING_TEXT)}),
        )

    async def test_a_share_asleep_is_folder_timeout(self) -> None:
        def asleep(_path: Path) -> int:
            time.sleep(1.0)
            return 0

        with (
            mock.patch.object(library_module, "READ_TIMEOUT_S", 0.2),
            mock.patch.object(library_module, "_size_of", asleep),
        ):
            reply = await self.play_link("192.168.1.20")
        self.assertEqual((reply.status, reply.body["error"]), (504, "folder_timeout"))
        self.assertEqual(self.links._links, {})

    async def test_an_address_that_cant_be_one_is_a_bad_request(self) -> None:
        for local in ("", "8.8.8.8", "not an address"):
            with self.subTest(local):
                reply = await self.play_link(local)
                self.assertEqual((reply.status, reply.body["error"]), (400, "bad_request"))
        self.assertEqual(self.links._links, {})

    async def test_no_reachable_copy_is_no_longer_there(self) -> None:
        self.video.unlink()
        reply = await self.play_link("192.168.1.20")
        self.assertEqual(
            (reply.status, reply.body), (404, {"error": "no_longer_there", "message": t(GONE)})
        )
        for key in ("movie:1", "folder:movie:438631", ""):
            reply = await self.play_link("192.168.1.20", key)
            self.assertEqual((reply.status, reply.body["error"]), (404, "no_longer_there"))
        self.assertEqual(self.links._links, {})

    async def test_beside_plex_it_answers_and_off_it_answers_plex_library(self) -> None:
        await self.settings.update(plex_token=TOKEN)  # the folders stay on: two shelves
        reply = await self.play_link("192.168.1.20")
        self.assertEqual(reply.status, 200)
        await self.settings.update(folder_library_on=False)  # Plex alone
        reply = await self.play_link("192.168.1.20")
        self.assertEqual(
            (reply.status, reply.body),
            (409, {"error": "plex_library", "message": t(PLEX_LIBRARY_TEXT)}),
        )

    async def test_the_log_holds_no_link_or_address(self) -> None:
        with self.assertLogs("app.services.phone", "DEBUG") as logs:
            reply = await self.play_link("192.168.1.20")
            play_links_module.log.debug("(something logged)")
        text = "\n".join(logs.output)
        token = reply.body["url"].split("/")[4]
        for secret in (token, "192.168.1.20", "Dune", self.root.name, "438631"):
            self.assertNotIn(secret, text)


class WithThePhoneConnectionTests(ServiceCase):
    def service_with_links(
        self, play_ports: int | tuple[int, ...] | None = None
    ) -> tuple[PhoneService, PlayLinks, int]:
        links = PlayLinks()
        play_port = _free_port()
        service = PhoneService(
            self.settings,
            self.paths,
            self.profiles,
            routes_factory=lambda: {},
            port=self.port,
            finder_port=self.finder_port,
            addresses=lambda: [HOME],
            play_links=links,
            play_port=play_ports if play_ports is not None else play_port,
        )
        self.addAsyncCleanup(service.aclose)
        return service, links, play_port

    async def test_a_kept_play_port_falls_back_and_hello_says_the_one_it_got(self) -> None:
        await self.settings.update(**PLEX)
        busy = socket.socket()
        self.addCleanup(busy.close)
        busy.bind(("0.0.0.0", 0))
        busy.listen()
        kept = int(busy.getsockname()[1])  # as 58762 inside a range Hyper-V keeps
        second = _free_port()  # as 47762
        service, links, _ = self.service_with_links((kept, second, 0))
        self.service = service
        await service.set_on(True)
        self.assertIsNone(service.problem)
        self.assertEqual(links.port, second)
        status, paired = await self.pair()
        self.assertEqual((status, paired["play_port"]), (200, second))
        _, hello = await self.ask("GET", "/v1/hello", token=paired["token"])
        self.assertEqual(hello["play_port"], second)
        await service.set_on(False)
        self.assertIsNone(links.port)

    async def plain_port_is_open(self, port: int) -> bool:
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            return False
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        return True

    async def test_it_runs_with_the_phone_connection(self) -> None:
        service, links, play_port = self.service_with_links()
        order: list[bool] = []
        real_start = links.start

        async def start(port: int = PLAY_PORT) -> None:
            order.append(service.running)  # the phone server is up by then
            await real_start(port)

        with mock.patch.object(links, "start", start):
            await service.set_on(True)
        self.assertEqual(order, [True])
        self.assertTrue(links.running)
        self.assertIsNone(service.problem)
        self.assertTrue(await self.plain_port_is_open(play_port))
        await service.set_on(False)
        self.assertFalse(links.running)
        self.assertFalse(await self.plain_port_is_open(play_port))
        await service.set_on(True)
        self.assertTrue(links.running)
        await service.aclose()
        self.assertFalse(links.running)
        self.assertFalse(service.running)

    async def test_a_busy_play_port_leaves_the_phone_server_running(self) -> None:
        service, links, play_port = self.service_with_links()
        with socket.socket() as taken:
            taken.bind(("0.0.0.0", play_port))
            taken.listen()
            with self.assertLogs("app.services.phone.service", "WARNING"):
                await service.set_on(True)  # doesn't raise
            self.assertEqual(service.problem, PORT_BUSY_TEXT)
            self.assertTrue(service.running)
            self.assertFalse(links.running)
            self.assertTrue(await self.port_is_open())  # the phone server answers
        # `run` takes it up once the port is free again.
        task = asyncio.create_task(service.run(retry_s=0.01))
        self.addCleanup(task.cancel)
        for _ in range(300):
            if links.running and service.problem is None:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(links.running)
        self.assertIsNone(service.problem)


if __name__ == "__main__":
    unittest.main()
