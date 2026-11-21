"""The video tools come with AirGrabber (app/services/media_tools.py): fetched and checked against
their published sums, once in the background when they're missing (tried again after a while), and
by `main.py --fetch-tools`. httpx.MockTransport only: never a real download."""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import tempfile
import unittest
import zipfile
from pathlib import Path
from typing import Any

import httpx

from app.config import AppPaths
from app.i18n import t
from app.services.media_tools import (
    FFMPEG_SUMS_URL,
    FFMPEG_URL,
    TOOLS_COMING_TEXT,
    TOOLS_NOT_YET_TEXT,
    YTDLP_SUMS_URL,
    YTDLP_URL,
    MediaTools,
    ToolInstallError,
    fetch_once,
    tools_folder,
)

WEB = "https://tools.example"
URLS: dict[str, Any] = {
    "ytdlp_url": f"{WEB}/y/yt-dlp.exe",
    "ytdlp_sums_url": f"{WEB}/y/SHA2-256SUMS",
    "ffmpeg_url": f"{WEB}/f/ff.zip",
    "ffmpeg_sums_url": f"{WEB}/f/checksums.sha256",
}
EXE = b"MZ fake yt-dlp"


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def _sums(name: str, data: bytes) -> bytes:
    return f"{hashlib.sha256(data).hexdigest()}  {name}\n".encode()


ZIPPED = _zip({"ff/bin/ffmpeg.exe": b"ff", "ff/bin/ffprobe.exe": b"fp", "ff/doc/x.txt": b"d"})


class FakeWeb:
    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, bytes]] = {}
        self.paths: list[str] = []
        self.add_good()

    def add_good(self) -> None:
        self.routes.update(
            {
                "/y/yt-dlp.exe": (200, EXE),
                "/y/SHA2-256SUMS": (200, _sums("yt-dlp.exe", EXE)),
                "/f/ff.zip": (200, ZIPPED),
                "/f/checksums.sha256": (200, _sums("ff.zip", ZIPPED)),
            }
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.paths.append(request.url.path)
        status, body = self.routes.get(request.url.path, (404, b""))
        return httpx.Response(status, content=body)


class MediaToolsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tools = Path(tmp.name) / "forge" / "tools"
        self.web = FakeWeb()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.web.handle))
        self.addAsyncCleanup(self.http.aclose)
        self.slept: list[float] = []

    def media(self, **extra: Any) -> MediaTools:
        async def sleep(seconds: float) -> None:
            self.slept.append(seconds)
            self.web.add_good()  # GitHub back again

        return MediaTools(self.tools, self.http, **URLS, sleep=sleep, **extra)

    async def test_fetched_checked_and_kept(self) -> None:
        media = self.media()
        self.assertFalse(media.present())
        self.assertTrue(await media.ensure())
        self.assertEqual((self.tools / "yt-dlp.exe").read_bytes(), EXE)
        bin_dir = self.tools / "ffmpeg" / "bin"
        self.assertEqual(sorted(p.name for p in bin_dir.iterdir()), ["ffmpeg.exe", "ffprobe.exe"])
        self.assertFalse((self.tools / "ffmpeg" / "doc").exists())  # only bin/
        self.assertEqual(sorted(p.name for p in self.tools.iterdir()), ["ffmpeg", "yt-dlp.exe"])
        self.assertTrue(media.present())
        asked = len(self.web.paths)
        self.assertFalse(await media.ensure())  # there: nothing happens
        self.assertEqual(len(self.web.paths), asked)

    async def test_a_bad_checksum_keeps_nothing_and_logs_no_address(self) -> None:
        self.web.routes["/f/ff.zip"] = (200, b"tampered")
        with self.assertLogs("app.services.media_tools", logging.INFO) as logs:
            with self.assertRaises(ToolInstallError):
                await self.media().ensure()
        self.assertEqual(list(self.tools.iterdir()), [])
        joined = "\n".join(logs.output)
        self.assertNotIn(WEB, joined)
        self.assertNotIn(str(self.tools), joined)

    async def test_the_background_fetch_tries_again_after_a_while(self) -> None:
        self.web.routes["/y/SHA2-256SUMS"] = (500, b"")
        media = self.media(retry_after_s=1800)
        told: list[str] = []

        async def arrived() -> None:
            told.append("there")

        media.when_fetched(arrived)
        self.assertEqual(media.missing_text(), t(TOOLS_COMING_TEXT))
        seen: list[str] = []
        real = media.ensure

        async def ensure(*args: Any) -> bool:
            seen.append(media.missing_text())  # before each try
            return await real(*args)

        media.ensure = ensure  # type: ignore[method-assign, assignment]
        with self.assertLogs("app.services.media_tools", logging.INFO) as logs:
            await media.run()
        self.assertEqual(self.slept, [1800])  # one failure, then a while, then again
        self.assertTrue(media.present())
        self.assertEqual(told, ["there"])
        # On their way at first; after a failure: couldn't yet, tried again by itself.
        self.assertEqual(seen, [t(TOOLS_COMING_TEXT), t(TOOLS_NOT_YET_TEXT)])
        self.assertNotIn(WEB, "\n".join(logs.output))

    async def test_nothing_happens_when_they_are_there(self) -> None:
        await self.media().ensure()
        asked = len(self.web.paths)
        told: list[str] = []

        async def arrived() -> None:
            told.append("there")

        media = self.media()
        media.when_fetched(arrived)
        await media.run()
        self.assertEqual((len(self.web.paths), told, self.slept), (asked, [], []))

    async def test_one_fetch_at_a_time_per_folder(self) -> None:
        # The trailers' fetch and the background one (two MediaTools) share the folder's lock.
        first, second = self.media(), self.media()
        started = asyncio.Event()
        real = first._install

        async def slow(on_progress: Any) -> None:
            started.set()
            await asyncio.sleep(0.05)
            await real(on_progress)

        first._install = slow  # type: ignore[method-assign]
        task = asyncio.create_task(first.ensure())
        await started.wait()
        self.assertTrue(second.busy)
        self.assertEqual(second.missing_text(), t(TOOLS_COMING_TEXT))
        self.assertEqual(await asyncio.gather(task, second.ensure()), [True, False])
        self.assertEqual(self.web.paths.count("/y/yt-dlp.exe"), 1)
        self.assertFalse(second.busy)

    async def test_a_listener_failing_never_stops_the_run(self) -> None:
        media = self.media()

        async def broken() -> None:
            raise RuntimeError(r"C:\Users\Alex")

        media.when_fetched(broken)
        with self.assertLogs("app.services.media_tools", logging.WARNING) as logs:
            await media.run()
        self.assertNotIn("Alex", "\n".join(logs.output))

    def test_only_the_published_https_sources(self) -> None:
        for url in (YTDLP_URL, YTDLP_SUMS_URL, FFMPEG_URL, FFMPEG_SUMS_URL):
            self.assertTrue(url.startswith("https://github.com/yt-dlp/"))


class WiringTests(unittest.IsolatedAsyncioTestCase):
    async def test_tools_that_came_later_make_the_waiting_copies(self) -> None:
        from app.services import _tools_arrived

        class Copies:
            def __init__(self, possible: bool) -> None:
                self.possible = possible

            async def tools_arrived(self) -> bool:
                return self.possible

        class Sends:
            resumed = 0

            def resume_copies(self) -> None:
                self.resumed += 1

        sends = Sends()
        await _tools_arrived(Copies(False), sends)  # type: ignore[arg-type]
        self.assertEqual(sends.resumed, 0)
        await _tools_arrived(Copies(True), sends)  # type: ignore[arg-type]
        self.assertEqual(sends.resumed, 1)

    def test_the_main_window_starts_the_background_fetch(self) -> None:
        import inspect

        from app.services import Services
        from app.ui.main_window import MainWindow

        self.assertIn("media_tools", Services.__slots__)
        # Spawned with the start's other background work, once the loop runs.
        self.assertIn("spawn(self._services.media_tools.run()", inspect.getsource(MainWindow._start))


class FetchOnceTests(unittest.IsolatedAsyncioTestCase):
    """`main.py --fetch-tools`: 0 when they're there now, 1 when not; never raises."""

    async def test_exit_codes(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        paths = AppPaths(Path(tmp.name))
        web = FakeWeb()
        web.routes = {}  # GitHub can't be reached
        http = httpx.AsyncClient(transport=httpx.MockTransport(web.handle))
        self.addAsyncCleanup(http.aclose)
        self.assertEqual(await fetch_once(paths, http=http), 1)

        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

        offline = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
        self.addAsyncCleanup(offline.aclose)
        self.assertEqual(await fetch_once(paths, http=offline), 1)
        tools = tools_folder(paths)
        self.assertEqual(tools, Path(tmp.name) / "forge" / "tools")
        (tools / "ffmpeg" / "bin").mkdir(parents=True)
        for name in ("yt-dlp.exe", "ffmpeg/bin/ffmpeg.exe", "ffmpeg/bin/ffprobe.exe"):
            (tools / name).write_bytes(b"")
        self.assertEqual(await fetch_once(paths, http=offline), 0)  # there: nothing asked


if __name__ == "__main__":
    unittest.main()
