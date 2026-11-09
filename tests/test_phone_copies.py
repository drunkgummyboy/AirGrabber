"""Phone-sized copies (app/services/phone/copies.py): the recipe, and the jobs with a fake Plex and
fake programs (nothing real is fetched or converted)."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import Any

from app.models.plex import PlexFilmFile
from app.services.phone.copies import (
    CONVERT_FAILED_TEXT,
    DOLBY_VISION_TEXT,
    FETCH_SHARE,
    NO_ROOM_TEXT,
    SMALL_ALREADY_TEXT,
    STALE_DAYS,
    PhoneCopies,
    SourceInfo,
    ffmpeg_args,
    fit,
    progress_seconds,
    read_probe,
)
from app.services.phone.server import FileReply, Reply, Request
from app.services.plex import PlexUnreachable

FFMPEG = Path("tools") / "ffmpeg.exe"
FILM_BYTES = b"x" * 5000


def probe_json(**video: Any) -> dict[str, Any]:
    stream = {
        "codec_type": "video", "codec_name": "hevc", "width": 3840, "height": 2160,
        "pix_fmt": "yuv420p10le", "color_primaries": "bt2020", "color_transfer": "smpte2084",
        "color_space": "bt2020nc", **video,
    }  # fmt: skip
    return {
        "streams": [
            stream,
            {"codec_type": "audio", "codec_name": "truehd", "channels": 8},
            {"codec_type": "audio", "codec_name": "ac3", "channels": 6},
            {"codec_type": "subtitle", "codec_name": "hdmv_pgs_subtitle"},
            {"codec_type": "subtitle", "codec_name": "mov_text"},
        ],
        "format": {"duration": "7200.5"},
    }


class RecipeTests(unittest.TestCase):
    def test_fit(self) -> None:
        self.assertEqual(fit(3840, 2160), (1920, 1080))
        self.assertEqual(fit(3840, 1600), (1920, 800))  # a wide film keeps its shape
        self.assertEqual(fit(4096, 2160), (1920, 1012))
        self.assertEqual(fit(2880, 2160), (1440, 1080))
        self.assertEqual(fit(1280, 720), (1280, 720))  # smaller stays

    def test_read_probe(self) -> None:
        info = read_probe(probe_json())
        assert info is not None
        self.assertEqual((info.width, info.height, info.ten_bit), (3840, 2160, True))
        self.assertEqual((info.primaries, info.transfer, info.matrix), ("bt2020", "smpte2084", "bt2020nc"))
        self.assertEqual(info.duration_s, 7200.5)
        self.assertEqual(info.sounds, (("truehd", 8), ("ac3", 6)))
        self.assertEqual(info.subtitles, ("hdmv_pgs_subtitle", "mov_text"))
        self.assertFalse(info.dolby_vision_only)
        self.assertIsNone(read_probe({"streams": [{"codec_type": "audio"}]}))

    def test_dolby_vision(self) -> None:
        only = [{"side_data_type": "DOVI configuration record", "dv_profile": 5,
                 "dv_bl_signal_compatibility_id": 0}]  # fmt: skip
        under = [{"side_data_type": "DOVI configuration record", "dv_profile": 8,
                  "dv_bl_signal_compatibility_id": 1}]  # fmt: skip
        dv5 = read_probe(probe_json(side_data_list=only))
        dv8 = read_probe(probe_json(side_data_list=under))
        assert dv5 is not None and dv8 is not None
        self.assertTrue(dv5.dolby_vision_only)
        self.assertFalse(dv8.dolby_vision_only)  # HDR10 under it: that's what the copy gets

    def test_the_card_does_the_picture(self) -> None:
        info = read_probe(probe_json())
        assert info is not None
        args = ffmpeg_args(FFMPEG, Path("in.mkv"), Path("out.mkv.part"), info)
        self.assertEqual(args[0], str(FFMPEG))
        joined = " ".join(args)
        self.assertIn("-hwaccel cuda -hwaccel_output_format cuda", joined)
        self.assertIn("scale_cuda=w=1920:h=1080", joined)
        self.assertIn("-c:v hevc_nvenc", joined)
        self.assertIn("-b:v 6M -maxrate 10M", joined)
        self.assertIn("-profile:v main10", joined)
        self.assertIn("-color_primaries bt2020 -color_trc smpte2084 -colorspace bt2020nc", joined)
        self.assertIn("-c:a copy -c:a:0 eac3 -b:a:0 640k -ac:a:0 6", joined)  # TrueHD 7.1
        self.assertNotIn("-c:a:1", joined)  # AC-3 copied
        self.assertIn("-c:s copy -c:s:1 srt", joined)  # PGS copied, MP4 text as SRT
        self.assertEqual(args[-3:], ["-f", "matroska", "out.mkv.part"])
        self.assertIn("-progress pipe:1", joined)

    def test_the_processor_decodes_when_the_card_wont(self) -> None:
        info = SourceInfo(width=3840, height=2160, ten_bit=False, duration_s=60)
        joined = " ".join(ffmpeg_args(FFMPEG, Path("in"), Path("out"), info, card_decodes=False))
        self.assertNotIn("-hwaccel", joined)
        self.assertIn("scale=w=1920:h=1080:flags=lanczos,format=yuv420p", joined)
        self.assertIn("-profile:v main ", joined)
        self.assertNotIn("-color_primaries", joined)  # none known: none written

    def test_progress(self) -> None:
        self.assertEqual(progress_seconds("out_time_us=1500000\n"), 1.5)
        self.assertEqual(progress_seconds("out_time_ms=3000000"), 3.0)  # microseconds too
        self.assertIsNone(progress_seconds("out_time_us=N/A"))
        self.assertIsNone(progress_seconds("frame=12"))


class FakePlex:
    def __init__(self) -> None:
        self.film: PlexFilmFile | None = PlexFilmFile(
            part_key="/library/parts/9/1/file.mkv", size=len(FILM_BYTES), height=2160,
            duration_s=7200,
        )  # fmt: skip
        self.broken = False

    async def film_file(self, key: str) -> PlexFilmFile | None:
        return self.film

    async def fetch_film(self, film: PlexFilmFile, write: Callable[[bytes], Awaitable[None]]) -> None:
        if self.broken:
            raise PlexUnreachable("broke off")
        for start in range(0, len(FILM_BYTES), 1000):
            await write(FILM_BYTES[start : start + 1000])


class JobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.tools = self.root / "tools"
        self.tools.mkdir()
        for name in ("ffmpeg.exe", "ffprobe.exe"):
            (self.tools / name).write_bytes(b"")
        self.folder = self.root / "phone-copies"
        self.plex = FakePlex()
        self.probe = probe_json()
        self.card_encodes = True
        self.card_decodes = True
        self.conversions: list[list[str]] = []
        self.now = 1_000_000.0
        self.free = 10**12

    def copies(self) -> PhoneCopies:
        copies = PhoneCopies(
            self.plex,  # type: ignore[arg-type]
            self.folder,
            self.tools,
            run=self.fake_run,
            convert=self.fake_convert,
            clock=lambda: self.now,
            free_space=lambda _path: self.free,
        )
        self.addAsyncCleanup(copies.aclose)
        return copies

    async def fake_run(self, args: Sequence[str], _timeout: float) -> tuple[int, bytes]:
        if Path(args[0]).name == "ffprobe.exe":
            return 0, json.dumps(self.probe).encode()
        return (0 if self.card_encodes else 1), b""  # the card's try

    async def fake_convert(
        self, args: Sequence[str], duration: float, progress: Callable[[float], None]
    ) -> int:
        self.conversions.append(list(args))
        if "-hwaccel" in args and not self.card_decodes:
            return 1
        progress(0.5)
        Path(args[-1]).write_bytes(b"small copy")
        progress(1.0)
        return 0

    async def ask(self, copies: PhoneCopies, method: str, path: str, **body: Any) -> Any:
        routes = copies.routes()
        key = path.split("/")[3] if path.count("/") >= 3 else ""
        pattern = path.replace(key, "{key}") if key else path
        headers = body.pop("headers", {})
        request = Request(method, path, {"key": key} if key else {}, body, None, headers)
        return await routes[method, pattern](request)

    async def made(self, copies: PhoneCopies, key: str = "501") -> dict[str, Any]:
        for _ in range(200):
            reply = await self.ask(copies, "GET", f"/v1/phone-copies/{key}")
            if reply.body.get("state") in ("ready", "failed"):
                return dict(reply.body)
            await asyncio.sleep(0.01)
        self.fail("the copy never finished")

    async def test_a_copy_from_asking_to_dropping(self) -> None:
        copies = self.copies()
        await copies.start()
        self.assertTrue(copies.available)
        first = await self.ask(copies, "POST", "/v1/phone-copies", key="501", kind="movie", title="Dune")
        self.assertEqual((first.status, first.body["key"]), (200, "501"))
        again = await self.ask(copies, "POST", "/v1/phone-copies", key="501", kind="movie", title="Dune")
        self.assertEqual(again.status, 200)  # the same job, not a second one
        body = await self.made(copies)
        self.assertEqual(body, {"key": "501", "state": "ready", "progress": 100, "size": 10})
        self.assertEqual(len(self.conversions), 1)
        self.assertFalse((self.folder / "501.source").exists())  # the 4K file went
        whole = await self.ask(copies, "GET", "/v1/phone-copies/501/file")
        self.assertIsInstance(whole, FileReply)
        self.assertEqual((whole.size, whole.start, whole.partial), (10, 0, False))
        part = await self.ask(copies, "GET", "/v1/phone-copies/501/file", headers={"range": "bytes=4-"})
        self.assertEqual((part.start, part.length, part.partial), (4, 6, True))
        past = await self.ask(copies, "GET", "/v1/phone-copies/501/file", headers={"range": "bytes=10-"})
        self.assertEqual(past.status, 416)
        dropped = await self.ask(copies, "DELETE", "/v1/phone-copies/501")
        self.assertEqual(dropped.status, 204)
        self.assertEqual(list(self.folder.iterdir()), [])
        gone = await self.ask(copies, "GET", "/v1/phone-copies/501")
        self.assertEqual(gone.status, 404)
        self.assertEqual((await self.ask(copies, "DELETE", "/v1/phone-copies/77")).status, 204)

    async def test_progress_fetching_then_converting(self) -> None:
        copies = self.copies()
        await copies.start()
        seen: list[int] = []
        real = self.fake_convert

        async def watched(args: Sequence[str], duration: float, progress: Callable[[float], None]) -> int:
            def told(share: float) -> None:
                progress(share)
                seen.append(copies._jobs["501"].progress)

            self.assertEqual(copies._jobs["501"].state, "converting")
            self.assertEqual(copies._jobs["501"].progress, FETCH_SHARE)
            return await real(args, duration, told)

        copies._convert = watched
        await self.ask(copies, "POST", "/v1/phone-copies", key="501", kind="movie")
        await self.made(copies)
        self.assertEqual(seen, [FETCH_SHARE + (100 - FETCH_SHARE) // 2 + 1, 100])

    async def test_without_a_card_nothing_is_offered(self) -> None:
        self.card_encodes = False
        copies = self.copies()
        await copies.start()
        self.assertFalse(copies.available)
        reply = await self.ask(copies, "POST", "/v1/phone-copies", key="501", kind="movie")
        self.assertEqual((reply.status, reply.body["error"]), (409, "failed"))

    async def test_without_ffmpeg_nothing_is_offered(self) -> None:
        (self.tools / "ffprobe.exe").unlink()
        copies = self.copies()
        await copies.start()
        self.assertFalse(copies.available)

    async def test_bad_requests(self) -> None:
        copies = self.copies()
        await copies.start()
        for body in ({"key": "5/1", "kind": "movie"}, {"key": "501", "kind": "episode"}, {}):
            reply = await self.ask(copies, "POST", "/v1/phone-copies", **body)
            self.assertEqual(reply.status, 400, body)

    async def test_failures_say_why(self) -> None:
        cases: list[tuple[str, Callable[[], None], str]] = [
            ("small", lambda: setattr(self.plex, "film", self.plex.film.model_copy(update={"height": 1080})), SMALL_ALREADY_TEXT),  # type: ignore[union-attr]
            ("room", lambda: setattr(self, "free", 1000), NO_ROOM_TEXT),
            ("dolby", lambda: setattr(self, "probe", probe_json(side_data_list=[{"side_data_type": "DOVI configuration record", "dv_bl_signal_compatibility_id": 0}])), DOLBY_VISION_TEXT),
        ]  # fmt: skip
        for name, make_it_fail, text in cases:
            with self.subTest(name):
                await self.asyncSetUp()
                make_it_fail()
                copies = self.copies()
                await copies.start()
                await self.ask(copies, "POST", "/v1/phone-copies", key="501", kind="movie")
                body = await self.made(copies)
                self.assertEqual((body["state"], body["message"]), ("failed", text))
                self.assertNotIn("size", body)
                self.assertEqual(list(self.folder.iterdir()), [])  # nothing half-made left
                file = await self.ask(copies, "GET", "/v1/phone-copies/501/file")
                self.assertEqual(file.status, 409)

    async def test_plex_breaking_off(self) -> None:
        self.plex.broken = True
        copies = self.copies()
        await copies.start()
        await self.ask(copies, "POST", "/v1/phone-copies", key="501", kind="movie")
        body = await self.made(copies)
        self.assertEqual(body["state"], "failed")
        self.assertTrue(body["message"])

    async def test_the_processor_decodes_when_the_card_wont(self) -> None:
        self.card_decodes = False
        copies = self.copies()
        await copies.start()
        await self.ask(copies, "POST", "/v1/phone-copies", key="501", kind="movie")
        self.assertEqual((await self.made(copies))["state"], "ready")
        self.assertEqual(len(self.conversions), 2)
        self.assertNotIn("-hwaccel", self.conversions[1])

    async def test_a_conversion_that_fails(self) -> None:
        copies = self.copies()

        async def broken(*_args: Any) -> int:
            return 1

        copies._convert = broken
        await copies.start()
        await self.ask(copies, "POST", "/v1/phone-copies", key="501", kind="movie")
        body = await self.made(copies)
        self.assertEqual((body["state"], body["message"]), ("failed", CONVERT_FAILED_TEXT))

    async def test_one_at_a_time_and_a_drop_while_it_runs(self) -> None:
        copies = self.copies()
        gate = asyncio.Event()
        real = self.fake_convert

        async def slow(args: Sequence[str], duration: float, progress: Callable[[float], None]) -> int:
            await gate.wait()
            return await real(args, duration, progress)

        copies._convert = slow
        await copies.start()
        await self.ask(copies, "POST", "/v1/phone-copies", key="501", kind="movie")
        await self.ask(copies, "POST", "/v1/phone-copies", key="502", kind="movie")
        for _ in range(50):
            await asyncio.sleep(0.01)
        states = {key: copies._jobs[key].state for key in ("501", "502")}
        self.assertEqual(states, {"501": "converting", "502": "waiting"})
        await self.ask(copies, "DELETE", "/v1/phone-copies/501")  # stops the one running
        self.assertNotIn("501", copies._jobs)
        gate.set()
        self.assertEqual((await self.made(copies, "502"))["state"], "ready")
        self.assertEqual(sorted(p.name for p in self.folder.iterdir()), ["502.mkv"])

    async def test_copies_from_before_and_stale_ones(self) -> None:
        self.folder.mkdir()
        (self.folder / "501.mkv").write_bytes(b"made before")
        (self.folder / "502.mkv.part").write_bytes(b"half")
        (self.folder / "502.source").write_bytes(b"4k")
        copies = self.copies()
        self.now = (self.folder / "501.mkv").stat().st_mtime + 60
        await copies.start()
        body = (await self.ask(copies, "GET", "/v1/phone-copies/501")).body
        self.assertEqual((body["state"], body["size"]), ("ready", 11))
        self.assertEqual([p.name for p in self.folder.iterdir()], ["501.mkv"])  # half-made gone
        self.now += STALE_DAYS * 86400 + 61
        await copies._clean()
        self.assertEqual(list(self.folder.iterdir()), [])
        self.assertEqual((await self.ask(copies, "GET", "/v1/phone-copies/501")).status, 404)


if __name__ == "__main__":
    unittest.main()
