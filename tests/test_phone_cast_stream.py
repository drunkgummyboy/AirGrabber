"""Cast sound streams (app/services/phone/cast_stream.py): the ffmpeg recipe, `POST /v1/cast-stream`
with a fake Plex Media Server (httpx.MockTransport, tests/fixtures/plex/) and the real ▶ Play lookup
(no real folder looked at), the folder library's files, and the stream on a real play link server on
127.0.0.1 with fake ffmpegs (no real program runs). Nothing reaches the network."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import tempfile
import unittest
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import urlsplit

import httpx

from app.i18n import t
from app.models.plex import LibraryKind, PlexTitle, PlexVideo
from app.services.phone import cast_stream as cast_module
from app.services.phone.cast_stream import (
    MAX_STREAMS,
    NO_FFMPEG_TEXT,
    NOT_FOUND_TEXT,
    CastStreams,
    Probed,
    read_probe,
    stream_args,
)
from app.services.phone.play_links import PLAY_LINK_HOURS, PLAY_PORT, PlayLinks
from app.services.phone.server import Reply, Request
from app.services.phone.service import PhoneService
from app.services.playback import LibraryFolders, Located, PlaybackService, PlayOutcome, PlayProblem
from tests.test_phone_service import HOME, PLEX, ServiceCase
from tests.test_play_links import _free_port, fetch
from tests.test_plex import LOCAL_HTTPS, FakePlex, PlexTestCase, fixture

NOW = datetime(2026, 11, 21, 20, 0, tzinfo=UTC)
ROUTE = ("POST", "/v1/cast-stream")
SERIES = r"\\Alex-NAS\video\TV"
MOVIES = r"\\Alex-NAS\video\Movies"
DUNE_HERE = MOVIES + r"\Dune Part Two (2024)\Dune Part Two (2024).mkv"
DUNE_1080_HERE = MOVIES + r"\Dune Part Two (2024)\Dune Part Two (2024) 1080p.mkv"
ARK_HERE = SERIES + r"\The Ark (2023)\Season 02\The Ark - S02E02 - Woe's Hollow.mkv"
FOLDER_FILM = r"\\Alex-NAS\films\Oppenheimer (2023)\Oppenheimer (2023).mkv"
FOLDER_EPISODE = r"\\Alex-NAS\series\Severance\Season 1\Severance S01E02.mkv"
FOLDER_FILM_KEY = "movie:872585"
FOLDER_EPISODE_KEY = "episode:95396:1:2"
EPISODE_PART = "/library/parts/2122/1700000000/file.mkv"  # the Ark episode's, not Dune's
DUNE_1080_PART = "/library/parts/52/1700000000/file.mkv"
FFMPEG_START = b"\x00\x00\x00\x18ftypiso6-moov-and-the-first-fragment"
PRIVATE = (
    "Dune", "Ark", "Oppenheimer", "Severance", "Alex-NAS", "volume1", ".mkv", "872585",
    "95396", HOME, "file.mkv",
)  # fmt: skip


def probe_json(*, codec: str = "hevc", sounds: Sequence[int] = (8, 2)) -> dict[str, Any]:
    return {
        "streams": [
            {"codec_type": "video", "codec_name": "mjpeg", "disposition": {"attached_pic": 1}},
            {"codec_type": "video", "codec_name": codec, "width": 3840, "height": 2160},
            *({"codec_type": "audio", "codec_name": "truehd", "channels": n} for n in sounds),
            {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle"},
        ],
        "format": {"duration": "9960.25"},
    }


class FakeProcess:
    """A running ffmpeg as the stream sees it: its stdout fed by the test."""

    def __init__(self, args: Sequence[str]) -> None:
        self.args = list(args)
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode: int | None = None
        self.killed = False
        self._ended = asyncio.Event()

    def feed(self, data: bytes) -> None:
        self.stdout.feed_data(data)

    def finish(self, code: int = 0) -> None:
        if self.returncode is not None:
            return
        self.returncode = code
        self.stderr.feed_data(rb"C:\secret\path\Dune.mkv: some ffmpeg words")
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._ended.set()

    def kill(self) -> None:
        self.killed = True
        self.finish(-9)

    async def wait(self) -> int:
        await self._ended.wait()
        assert self.returncode is not None
        return self.returncode

    @property
    def joined(self) -> str:
        return " ".join(self.args)


def feeds_and_ends(code: int) -> Callable[[FakeProcess], None]:
    """An ffmpeg that gives the stream's start, then ends with `code`."""

    def script(process: FakeProcess) -> None:
        process.feed(FFMPEG_START)
        process.finish(code)

    return script


class Programs:
    """The fake ffprobe (`run`) and ffmpeg (`start`)."""

    def __init__(self) -> None:
        self.probe: dict[str, Any] | None = probe_json()
        self.probe_code = 0
        self.probed: list[list[str]] = []
        self.started: list[FakeProcess] = []
        # What each ffmpeg does once started (default: the stream's start, then it runs on).
        self.script: Callable[[FakeProcess], None] = lambda process: process.feed(FFMPEG_START)
        self.start_error: OSError | None = None

    async def run(self, args: Sequence[str], timeout_s: float) -> tuple[int, bytes]:
        self.probed.append(list(args))
        return self.probe_code, json.dumps(self.probe).encode() if self.probe else b"garbage"

    async def start(self, args: Sequence[str]) -> FakeProcess:
        if self.start_error is not None:
            raise self.start_error
        process = FakeProcess(args)
        self.started.append(process)
        asyncio.get_running_loop().call_soon(self.script, process)
        return process


def tools_in(root: Path) -> Path:
    tools = root / "tools" / "ffmpeg" / "bin"
    tools.mkdir(parents=True)
    (tools / "ffmpeg.exe").touch()
    (tools / "ffprobe.exe").touch()
    return tools


def request(body: dict[str, Any], local_address: str = HOME) -> Request:
    return Request("POST", "/v1/cast-stream", {}, body, None, {}, local_address=local_address)


def token_of(reply: Reply) -> str:
    return urlsplit(str(reply.body["url"])).path.split("/")[2]


def chunks_of(body: bytes) -> tuple[bytes, bool]:
    """A chunked body's bytes, and whether it ended with the last chunk."""
    data = b""
    while body:
        size_line, _, body = body.partition(b"\r\n")
        size = int(size_line, 16)
        if size == 0:
            return data, True
        data, body = data + body[:size], body[size + 2 :]
    return data, False


# -- the recipe ----------------------------------------------------------------------------------


class RecipeTests(unittest.TestCase):
    def test_stereo_from_the_start(self) -> None:
        args = stream_args(Path("bin") / "ffmpeg.exe", Path("film.mkv"))
        self.assertEqual(
            args,
            [
                str(Path("bin") / "ffmpeg.exe"), "-nostdin", "-hide_banner", "-loglevel", "error",
                "-i", "film.mkv", "-map", "0:V:0", "-map", "0:a:0", "-c:v", "copy",
                "-c:a", "aac", "-b:a", "256k", "-ac", "2",
                "-avoid_negative_ts", "make_zero", "-max_muxing_queue_size", "9999",
                "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof", "pipe:1",
            ],
        )  # fmt: skip

    def test_surround_hevc_from_a_place(self) -> None:
        joined = " ".join(
            stream_args(Path("ffmpeg"), Path("f"), start_s=90.5, audio=3, channels=6, hevc=True)
        )
        self.assertIn("-ss 90.500 -i f", joined)  # before -i: on the keyframe at or before it
        self.assertIn("-map 0:a:3", joined)
        self.assertIn("-c:v copy -tag:v hvc1", joined)
        self.assertIn("-c:a aac -b:a 384k -ac 6", joined)

    def test_without_sound(self) -> None:
        joined = " ".join(stream_args(Path("ffmpeg"), Path("f"), audio=None))
        self.assertNotIn("0:a", joined)
        self.assertNotIn("-c:a", joined)
        self.assertNotIn("-tag:v", joined)  # not HEVC: no hvc1

    def test_read_probe(self) -> None:
        self.assertEqual(read_probe(probe_json()), Probed("hevc", (8, 2), 9960.25))
        self.assertEqual(read_probe({"streams": [{"codec_type": "video", "codec_name": "h264",
                                                  "duration": "12"}]}),
                         Probed("h264", (), 12.0))  # fmt: skip
        self.assertIsNone(read_probe({"streams": [{"codec_type": "audio"}]}))
        self.assertIsNone(read_probe({}))


# -- making a link: Plex's files through ▶ Play's lookup -----------------------------------------


class CastingPlex(FakePlex):
    """FakePlex that also answers a film's (3001), an episode's (42) and a show's (1001) metadata."""

    def _server(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/library/metadata/") and self._authorised(request):
            if path == "/library/metadata/3001":
                return httpx.Response(200, json=fixture("metadata_movie.json"))
            if path == "/library/metadata/42":
                return httpx.Response(200, json=fixture("metadata_episode_markers.json"))
            if path == "/library/metadata/1001":
                show = fixture("shows.json")["MediaContainer"]["Metadata"][0]
                return httpx.Response(200, json={"MediaContainer": {"Metadata": [show]}})
            return httpx.Response(404)
        return super()._server(request)


class PlexLinkTests(PlexTestCase):
    async def asyncSetUp(self) -> None:
        with mock.patch("tests.test_plex.FakePlex", CastingPlex):
            await super().asyncSetUp()
        await self.settings.update(plex_server_uri=LOCAL_HTTPS)
        self.folders: LibraryFolders | None = LibraryFolders(series=SERIES, movies=MOVIES)
        self.here = {DUNE_HERE, DUNE_1080_HERE, ARK_HERE}

        async def folders() -> LibraryFolders | None:
            return self.folders

        self.playback = PlaybackService(self.plex, folders, exists=lambda path: path in self.here)
        self.programs = Programs()
        self.own: dict[str, Path] = {
            FOLDER_FILM_KEY: Path(FOLDER_FILM),
            FOLDER_EPISODE_KEY: Path(FOLDER_EPISODE),
        }
        self.asked_own: list[str] = []
        self.root = Path(self.tmp.name)
        self.tools = tools_in(self.root)
        self.cast = self.make()

    def make(self, tools: Path | None = None) -> CastStreams:
        async def own_file(key: str) -> Path | None:
            self.asked_own.append(key)
            return self.own.get(key)

        return CastStreams(
            self.plex,
            self.playback,
            tools or self.tools,
            own_file=own_file,
            start=self.programs.start,
            run=self.programs.run,
            now=lambda: NOW,
        )

    async def ask(self, body: dict[str, Any], cast: CastStreams | None = None) -> Reply:
        reply = await (cast or self.cast).routes()[ROUTE](request(body))
        assert isinstance(reply, Reply), reply
        return reply

    def probed_file(self) -> str:
        return self.programs.probed[-1][-1]

    async def test_a_plex_film_by_the_pcs_own_choice(self) -> None:
        reply = await self.ask({"video": "3001"})
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(self.probed_file(), DUNE_HERE)  # the 4K version, as ▶ Play
        token = token_of(reply)
        self.assertEqual(reply.body["url"], f"http://{HOME}:{PLAY_PORT}/c/{token}/stream.mp4")
        self.assertGreaterEqual(len(token), 32)
        self.assertEqual(reply.body["duration_s"], 9960.25)
        self.assertEqual(
            reply.body["expires_at"], (NOW + timedelta(hours=PLAY_LINK_HOURS)).isoformat()
        )
        self.assertEqual(set(reply.body), {"url", "duration_s", "expires_at"})
        asked = [one.url.path for one in self.fake.requests]
        self.assertIn("/library/metadata/3001", asked)
        self.assertFalse(any("/library/parts/" in path for path in asked))  # never read via Plex
        self.assertEqual(self.programs.started, [])  # ffmpeg only runs for a stream

    async def test_a_plex_film_by_the_phones_part(self) -> None:
        reply = await self.ask({"video": "3001", "part": DUNE_1080_PART})
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(self.probed_file(), DUNE_1080_HERE)

    async def test_a_part_of_another_title_is_ignored(self) -> None:
        for part in (EPISODE_PART, "/library/parts/999/1/file.mkv"):
            with self.subTest(part=part):
                reply = await self.ask({"video": "3001", "part": part})
                self.assertEqual(reply.status, 200, reply.body)
                self.assertEqual(self.probed_file(), DUNE_HERE)

    async def test_an_episode(self) -> None:
        reply = await self.ask({"video": "42", "part": DUNE_1080_PART})  # Dune's part: ignored
        self.assertEqual(reply.status, 200, reply.body)
        self.assertEqual(self.probed_file(), ARK_HERE)

    async def test_without_ffprobes_length_plexs(self) -> None:
        self.programs.probe = {"streams": [{"codec_type": "video", "codec_name": "h264"}]}
        reply = await self.ask({"video": "42"})
        self.assertEqual(reply.body["duration_s"], 2700.0)  # the episode's, from Plex

    async def test_folder_videos(self) -> None:
        for key, wanted, file in (
            ("movie:0872585", FOLDER_FILM_KEY, FOLDER_FILM),
            (FOLDER_EPISODE_KEY, FOLDER_EPISODE_KEY, FOLDER_EPISODE),
        ):
            with self.subTest(key=key):
                reply = await self.ask({"video": key, "part": DUNE_1080_PART})  # part ignored
                self.assertEqual(reply.status, 200, reply.body)
                self.assertEqual(self.asked_own[-1], wanted)
                self.assertEqual(self.probed_file(), file)
        self.assertFalse(any("/library/" in one.url.path for one in self.fake.requests))

    async def test_a_folder_video_works_without_plex(self) -> None:
        await self.settings.update(plex_token="")
        reply = await self.ask({"video": FOLDER_FILM_KEY})
        self.assertEqual(reply.status, 200, reply.body)

    async def test_not_found(self) -> None:
        cases: list[tuple[dict[str, Any], Callable[[], None]]] = [
            ({"video": "3999"}, lambda: None),  # Plex doesn't have it
            ({"video": "1001"}, lambda: None),  # a show: no video
            ({"video": "3001"}, lambda: self.here.clear()),  # not on this PC's shares
            ({"video": "42"}, lambda: setattr(self, "folders", None)),  # MediaForge off
            ({"video": "movie:1"}, lambda: None),  # not in the folders
        ]
        for body, before in cases:
            with self.subTest(body=body):
                before()
                reply = await self.ask(body)
                self.assertEqual(reply.status, 404, reply.body)
                self.assertEqual(reply.body, {"error": "not_found", "message": t(NOT_FOUND_TEXT)})

    async def test_a_file_ffprobe_cant_read_is_not_found(self) -> None:
        for code, probe in ((1, probe_json()), (0, None), (-1, probe_json())):
            with self.subTest(code=code):
                self.programs.probe_code, self.programs.probe = code, probe
                reply = await self.ask({"video": "3001"})
                self.assertEqual((reply.status, reply.body["error"]), (404, "not_found"))

    async def test_bad_requests(self) -> None:
        for body in (
            {},
            {"video": 3001},
            {"video": ""},
            {"video": "../3001"},
            {"video": "show:1"},
            {"video": "1234567890123"},
            {"video": "3001", "audio": -1},
            {"video": "3001", "audio": 64},
            {"video": "3001", "audio": "1"},
            {"video": "3001", "audio": True},
            {"video": "3001", "audio": 1.0},
            {"video": "3001", "channels": 1},
            {"video": "3001", "channels": 8},
            {"video": "3001", "channels": "6"},
            {"video": "3001", "part": 5},
            {"video": "3001", "part": ""},
            {"video": "3001", "part": "x" * 501},
            {"video": "3001", "audio": 2},  # the file has two sound tracks
        ):
            with self.subTest(body=body):
                reply = await self.ask(body)
                self.assertEqual((reply.status, reply.body["error"]), (400, "bad_request"))

    async def test_an_address_that_cant_be_one_is_a_bad_request(self) -> None:
        reply = await self.cast.routes()[ROUTE](request({"video": "3001"}, local_address="8.8.8.8"))
        assert isinstance(reply, Reply)
        self.assertEqual(reply.status, 400)

    async def test_no_ffmpeg(self) -> None:
        for missing in ("ffmpeg.exe", "ffprobe.exe"):
            with self.subTest(missing=missing):
                tools = self.root / missing / "bin"
                tools.mkdir(parents=True)
                for name in {"ffmpeg.exe", "ffprobe.exe"} - {missing}:
                    (tools / name).touch()
                cast = self.make(tools)
                self.assertFalse(cast.available)
                reply = await self.ask({"video": FOLDER_FILM_KEY}, cast)
                self.assertEqual(reply.status, 409)
                self.assertEqual(reply.body, {"error": "no_ffmpeg", "message": t(NO_FFMPEG_TEXT)})
        self.assertTrue(self.cast.available)

    async def test_no_plex(self) -> None:
        await self.settings.update(plex_token="")
        reply = await self.ask({"video": "3001"})
        self.assertEqual((reply.status, reply.body["error"]), (409, "no_plex"))
        self.assertEqual(self.fake.requests, [])

    async def test_plex_away(self) -> None:
        self.fake.answering = set()
        reply = await self.ask({"video": "3001"})
        self.assertEqual((reply.status, reply.body["error"]), (502, "failed"))
        self.assertEqual(self.programs.probed, [])

    async def test_audio_and_channels_are_kept_for_the_stream(self) -> None:
        links = PlayLinks(cast=self.cast)
        port = _free_port()
        await links.start(port)
        self.addAsyncCleanup(links.stop)
        self.programs.script = feeds_and_ends(0)
        cases = (
            ({"video": "3001", "channels": 6}, "-map 0:a:0", "-b:a 384k -ac 6"),  # 7.1: 5.1
            ({"video": "3001", "audio": 1, "channels": 6}, "-map 0:a:1", "-b:a 256k -ac 2"),
            ({"video": "3001", "audio": 1}, "-map 0:a:1", "-b:a 256k -ac 2"),
            ({"video": "3001"}, "-map 0:a:0", "-b:a 256k -ac 2"),
        )
        for body, mapped, sound in cases:
            with self.subTest(body=body):
                reply = await self.ask(body)
                answer = await fetch(port, "GET", f"/c/{token_of(reply)}/stream.mp4")
                self.assertEqual(answer.status, 200)
                joined = self.programs.started[-1].joined
                self.assertIn(mapped, joined)
                self.assertIn(sound, joined)
                self.assertIn("-c:v copy -tag:v hvc1", joined)
                self.assertIn(f"-i {DUNE_HERE}", joined)
        self.programs.probe = probe_json(codec="h264", sounds=())
        reply = await self.ask({"video": "3001", "audio": 5})  # no sound at all: none is mapped
        await fetch(port, "GET", f"/c/{token_of(reply)}/stream.mp4")
        joined = self.programs.started[-1].joined
        self.assertNotIn("-map 0:a", joined)
        self.assertNotIn("-tag:v", joined)

    async def test_nothing_private_reaches_the_log(self) -> None:
        links = PlayLinks(cast=self.cast)
        port = _free_port()
        await links.start(port)
        self.addAsyncCleanup(links.stop)
        self.programs.script = feeds_and_ends(1)
        with self.assertLogs("app", logging.DEBUG) as logs:
            tokens = []
            for body in (
                {"video": "3001", "part": DUNE_1080_PART},
                {"video": "42"},
                {"video": FOLDER_FILM_KEY},
                {"video": FOLDER_EPISODE_KEY},
            ):
                reply = await self.ask(body)
                tokens.append(token_of(reply))
                await fetch(port, "GET", f"/c/{tokens[-1]}/stream.mp4?t=12")
            await self.ask({"video": "movie:1"})
            self.here.clear()
            await self.ask({"video": "3001"})
        text = "\n".join(logs.output)
        for secret in (*PRIVATE, *tokens, "secret", "ffmpeg words", DUNE_1080_PART):
            self.assertNotIn(secret, text)
        self.assertIn("3001", text)  # a Plex ratingKey may be logged
        self.assertIn("ended with 1", text)


# -- the stream ----------------------------------------------------------------------------------


class StubPlex:
    connected = True

    async def cast_video(
        self, key: str, part_key: str | None = None
    ) -> tuple[LibraryKind, PlexVideo] | None:
        return None


class StubPlayback:
    async def locate(self, title: PlexTitle, video: PlexVideo) -> Located | PlayOutcome:
        return PlayOutcome("", "movie", problem=PlayProblem.NOT_FOUND)


class StreamCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.now = NOW
        self.programs = Programs()

        async def own_file(key: str) -> Path | None:
            return Path(FOLDER_FILM)

        self.cast = CastStreams(
            StubPlex(),
            StubPlayback(),
            tools_in(self.root),
            own_file=own_file,
            start=self.programs.start,
            run=self.programs.run,
            now=lambda: self.now,
        )
        self.links = PlayLinks(cast=self.cast)
        self.port = _free_port()
        await self.links.start(self.port)
        self.addAsyncCleanup(self.links.stop)

    async def link(self, **body: Any) -> str:
        reply = await self.cast.routes()[ROUTE](request({"video": FOLDER_FILM_KEY, **body}))
        assert isinstance(reply, Reply) and reply.status == 200, reply
        return f"/c/{token_of(reply)}/stream.mp4"

    async def open_stream(
        self, path: str
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, dict[str, str]]:
        """A GET whose answer's head and first chunk have come; the connection left open."""
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        self.addAsyncCleanup(_close, writer)
        writer.write(f"GET {path} HTTP/1.1\r\nHost: tv\r\n\r\n".encode())
        await writer.drain()
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
        lines = head.decode("latin-1").split("\r\n")
        self.assertEqual(lines[0], "HTTP/1.1 200 OK")
        size = int(await asyncio.wait_for(reader.readline(), 5), 16)
        self.assertEqual(await reader.readexactly(size + 2), FFMPEG_START + b"\r\n")
        headers = {
            name.lower(): value.strip()
            for name, _, value in (line.partition(":") for line in lines[1:] if line)
        }
        return reader, writer, headers

    async def until(self, condition: Callable[[], bool]) -> None:
        async with asyncio.timeout(5):
            while not condition():
                await asyncio.sleep(0.01)


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    with contextlib.suppress(OSError):
        await writer.wait_closed()


class StreamTests(StreamCase):
    async def test_the_stream_goes_in_chunks_and_ends_with_the_last(self) -> None:
        path = await self.link()

        def script(process: FakeProcess) -> None:
            process.feed(FFMPEG_START)
            process.feed(b"fragment-2" * 10_000)
            process.finish(0)

        self.programs.script = script
        answer = await fetch(self.port, "GET", path)
        self.assertEqual(answer.status, 200)
        self.assertEqual(answer.headers["content-type"], "video/mp4")
        self.assertEqual(answer.headers["transfer-encoding"], "chunked")
        self.assertNotIn("content-length", answer.headers)
        self.assertEqual(answer.headers["accept-ranges"], "none")
        self.assertEqual(answer.headers["access-control-allow-origin"], "*")
        self.assertEqual(answer.headers["transfermode.dlna.org"], "Streaming")
        self.assertIn("DLNA.ORG_OP=00", answer.headers["contentfeatures.dlna.org"])
        data, whole = chunks_of(answer.body)
        self.assertEqual(data, FFMPEG_START + b"fragment-2" * 10_000)
        self.assertTrue(whole)
        process = self.programs.started[0]
        self.assertNotIn("-ss", process.args)  # from the start
        self.assertIn(f"-i {FOLDER_FILM}", process.joined)
        self.assertEqual(process.args[0], str(self.root / "tools" / "ffmpeg" / "bin" / "ffmpeg.exe"))
        await self.until(lambda: self.cast.streams == 0)

    async def test_a_failing_ffmpeg_cuts_the_stream_short(self) -> None:
        path = await self.link()
        self.programs.script = feeds_and_ends(1)
        with self.assertLogs(cast_module.log, logging.WARNING) as logs:
            answer = await fetch(self.port, "GET", path)
        self.assertEqual(logs.output, ["WARNING:%s:Cast stream for movie: ffmpeg ended with 1"
                                       % cast_module.__name__])  # fmt: skip
        self.assertEqual(answer.status, 200)
        data, whole = chunks_of(answer.body)
        self.assertEqual(data, FFMPEG_START)
        self.assertFalse(whole)  # no last chunk: the TV sees it was cut

    async def test_an_ffmpeg_that_gives_nothing_is_502(self) -> None:
        path = await self.link()
        self.programs.script = lambda process: process.finish(1)
        answer = await fetch(self.port, "GET", path)
        self.assertEqual((answer.status, answer.body), (502, b""))
        self.programs.start_error = FileNotFoundError("ffmpeg.exe")
        with self.assertLogs(cast_module.log, logging.WARNING):
            answer = await fetch(self.port, "GET", path)
        self.assertEqual(answer.status, 502)
        self.assertEqual(self.cast.streams, 0)

    async def test_from_a_place(self) -> None:
        path = await self.link()
        self.programs.script = feeds_and_ends(0)
        for asked, said in (("90.5", "90.500"), ("3600", "3600.000"), ("0", None)):
            with self.subTest(t=asked):
                answer = await fetch(self.port, "GET", f"{path}?t={asked}")
                self.assertEqual(answer.status, 200)
                args = self.programs.started[-1].args
                if said is None:
                    self.assertNotIn("-ss", args)
                else:
                    self.assertEqual(args[args.index("-ss") + 1], said)
                    self.assertLess(args.index("-ss"), args.index("-i"))

    async def test_a_bad_place_is_400_and_starts_nothing(self) -> None:
        path = await self.link()
        for asked in ("abc", "-1", "nan", "inf", "", "1e30"):
            with self.subTest(t=asked):
                answer = await fetch(self.port, "GET", f"{path}?t={asked}")
                self.assertEqual((answer.status, answer.body), (400, b""))
        self.assertEqual(self.programs.started, [])

    async def test_a_range_gets_the_whole_stream(self) -> None:
        path = await self.link()
        self.programs.script = feeds_and_ends(0)
        answer = await fetch(self.port, "GET", path, "Range: bytes=1000-\r\n")
        self.assertEqual(answer.status, 200)
        self.assertEqual(chunks_of(answer.body), (FFMPEG_START, True))

    async def test_head_starts_nothing(self) -> None:
        path = await self.link()
        answer = await fetch(self.port, "HEAD", path)
        self.assertEqual((answer.status, answer.body), (200, b""))
        self.assertEqual(answer.headers["content-type"], "video/mp4")
        self.assertNotIn("content-length", answer.headers)
        self.assertEqual(self.programs.started, [])

    async def test_unknown_names_and_tokens_are_404(self) -> None:
        path = await self.link()
        token = path.split("/")[2]
        for asked in (
            f"/c/{token}/stream.mkv",
            f"/c/{token}/video.mp4",
            f"/c/{token}x/stream.mp4",
            f"/c/{token}/stream.mp4/more",
            f"/p/{token}/stream.mp4",  # a play link's path: another table
            "/c/stream.mp4",
        ):
            for method in ("GET", "HEAD"):
                with self.subTest(path=asked, method=method):
                    answer = await fetch(self.port, method, asked)
                    self.assertEqual(answer.status, 404)
        self.assertEqual(self.programs.started, [])

    async def test_links_expire_and_go_with_the_server(self) -> None:
        path = await self.link()
        self.now = NOW + timedelta(hours=PLAY_LINK_HOURS) - timedelta(seconds=1)
        self.assertEqual((await fetch(self.port, "HEAD", path)).status, 200)
        self.now = NOW + timedelta(hours=PLAY_LINK_HOURS)
        self.assertEqual((await fetch(self.port, "HEAD", path)).status, 404)
        self.now = NOW
        self.assertEqual((await fetch(self.port, "HEAD", path)).status, 404)  # dropped for good
        path = await self.link()
        await self.links.stop()
        await self.links.start(self.port)
        self.assertEqual((await fetch(self.port, "HEAD", path)).status, 404)

    async def test_a_new_request_on_the_token_ends_the_one_before(self) -> None:
        path = await self.link()
        first_reader, _, _ = await self.open_stream(path)
        first = self.programs.started[0]
        _, _, headers = await self.open_stream(f"{path}?t=60")
        self.assertEqual(headers["content-type"], "video/mp4")
        self.assertTrue(first.killed)
        self.assertEqual(await asyncio.wait_for(first_reader.read(), 5), b"")  # closed, no last chunk
        second = self.programs.started[1]
        self.assertFalse(second.killed)
        self.assertEqual(self.cast.streams, 1)
        other = await self.link()  # another link's stream leaves it be
        await self.open_stream(other)
        self.assertFalse(second.killed)
        self.assertEqual(self.cast.streams, 2)

    async def test_at_most_two_at_a_time_the_oldest_goes(self) -> None:
        self.assertEqual(MAX_STREAMS, 2)
        paths = [await self.link() for _ in range(3)]
        readers = []
        for path in paths:
            reader, _, _ = await self.open_stream(path)
            readers.append(reader)
        oldest, middle, newest = self.programs.started
        self.assertTrue(oldest.killed)
        self.assertFalse(middle.killed or newest.killed)
        self.assertEqual(self.cast.streams, 2)
        self.assertEqual(await asyncio.wait_for(readers[0].read(), 5), b"")
        await self.open_stream(paths[0])  # the first link again: the middle one goes now
        self.assertTrue(middle.killed)
        self.assertFalse(newest.killed)
        self.assertEqual(self.cast.streams, 2)

    async def test_a_tv_that_goes_away_ends_ffmpeg(self) -> None:
        path = await self.link()
        _, writer, _ = await self.open_stream(path)
        process = self.programs.started[0]
        await _close(writer)
        await self.until(lambda: process.killed and self.cast.streams == 0)

    async def test_a_tv_gone_while_ffmpeg_is_opening_the_file(self) -> None:
        path = await self.link()
        self.programs.script = lambda process: None  # nothing yet
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(f"GET {path} HTTP/1.1\r\n\r\n".encode())
        await writer.drain()
        await self.until(lambda: bool(self.programs.started))
        await _close(writer)
        process = self.programs.started[0]
        await self.until(lambda: process.killed and self.cast.streams == 0)

    async def test_stopping_the_server_ends_every_ffmpeg(self) -> None:
        for _ in range(2):
            await self.open_stream(await self.link())
        await self.links.stop()
        self.assertTrue(all(process.killed for process in self.programs.started))
        self.assertEqual(self.cast.streams, 0)

    async def test_ffmpeg_that_wont_say_anything_in_time(self) -> None:
        path = await self.link()
        self.programs.script = lambda process: None
        with mock.patch.object(cast_module, "FIRST_BYTES_S", 0.05):
            answer = await fetch(self.port, "GET", path)
        self.assertEqual(answer.status, 502)
        self.assertTrue(self.programs.started[0].killed)


# -- /v1/hello -----------------------------------------------------------------------------------


class HelloTests(ServiceCase):
    async def hello_says(self, tools: Path) -> bool:
        await self.settings.update(**PLEX)
        cast = CastStreams(StubPlex(), StubPlayback(), tools)
        self.service = PhoneService(
            self.settings,
            self.paths,
            self.profiles,
            routes_factory=lambda: cast.routes(),
            port=self.port,
            finder_port=self.finder_port,
            addresses=lambda: [HOME],
            cast=cast,
        )
        self.addAsyncCleanup(self.service.aclose)
        await self.service.set_on(True)
        status, body = await self.pair()
        self.assertEqual(status, 200)
        status, hello = await self.ask("GET", "/v1/hello", token=body["token"])
        self.assertEqual(status, 200)
        self.assertEqual(hello["cast_sound"], body["cast_sound"])
        await self.service.set_on(False)
        return bool(hello["cast_sound"])

    async def test_cast_sound_with_mediaforges_ffmpeg(self) -> None:
        self.assertTrue(await self.hello_says(tools_in(self.root)))

    async def test_no_cast_sound_without_it(self) -> None:
        self.assertFalse(await self.hello_says(self.root / "nothing"))


if __name__ == "__main__":
    unittest.main()
