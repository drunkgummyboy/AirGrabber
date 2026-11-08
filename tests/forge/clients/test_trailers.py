"""TrailerService (app/forge/clients/trailers.py): MediaForge's tests/api/test_trailers.py on
httpx.MockTransport and a fake yt-dlp (fake_ytdlp.py next to this file). Never a real download,
never the real yt-dlp."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path
from typing import Any
from unittest import mock

import httpx

from app.forge.clients.trailers import (
    FFMPEG_SUMS_URL,
    FFMPEG_URL,
    FORMAT,
    YTDLP_SUMS_URL,
    YTDLP_URL,
    ToolInstallError,
    TrailerService,
    sha256_for,
    youtube_search_url,
)
from app.forge.core.settings import ForgeSettingsService
from app.forge.db.settings import SettingsRepo
from app.forge.models.plan import PlannedTrailer
from app.services.tmdb import TMDBError
from tests.forge.base import ForgeTestCase
from tests.forge.db.base import ForgeDbTestCase

FAKE_YTDLP = str(Path(__file__).parent / "fake_ytdlp.py")
WEB = "https://tools.example"


class FakeWeb:
    """MockTransport handler: fixed replies by path (404 otherwise), every request recorded."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, bytes, dict[str, str]]] = {}
        self.requests: list[httpx.Request] = []

    def add(
        self, path: str, body: bytes = b"", status: int = 200, headers: dict[str, str] | None = None
    ) -> None:
        self.routes[path] = (status, body, headers or {})

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        reply = self.routes.get(request.url.path)
        if reply is None:
            return httpx.Response(404)
        status, body, headers = reply
        return httpx.Response(status, headers=headers, content=body)

    def requests_to(self, path: str) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.path == path]


def trailer(folder: Path, title: str = "Dune Part Two", year: int | None = 2024) -> PlannedTrailer:
    ny = f"{title} ({year})" if year else title
    return PlannedTrailer(
        query=f"ytsearch1:{title} {year} official trailer",
        title=title,
        year=year,
        target_stem=folder / f"{ny} - Trailer",
    )


def calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _zip(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def _sums(name: str, data: bytes) -> bytes:
    return f"{hashlib.sha256(data).hexdigest()}  {name}\nffff  other.exe\n".encode()


TOOL_URLS: dict[str, Any] = {
    "ytdlp_url": f"{WEB}/y/yt-dlp.exe",
    "ytdlp_sums_url": f"{WEB}/y/SHA2-256SUMS",
    "ffmpeg_url": f"{WEB}/f/ff.zip",
    "ffmpeg_sums_url": f"{WEB}/f/checksums.sha256",
}


class TrailerTestCase(ForgeDbTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.repo = SettingsRepo(self.db)
        self.settings = await ForgeSettingsService.load(self.repo)
        self.web = FakeWeb()
        # Like AirGrabber's shared client: GitHub's latest/download links redirect.
        self.http = httpx.AsyncClient(
            transport=httpx.MockTransport(self.web.handle), follow_redirects=True
        )
        self.addAsyncCleanup(self.http.aclose)
        self.argv = self.tmp / "argv.log"
        self.enterContext(mock.patch.dict(os.environ, {"FAKE_ARGV": str(self.argv)}))
        for name in ("FAKE_FAIL", "FAKE_SLEEP"):
            os.environ.pop(name, None)

    def service(self, **kw: Any) -> TrailerService:
        kw.setdefault("ytdlp_command", [sys.executable, FAKE_YTDLP])
        return TrailerService(self.tmp / "tools", self.http, self.settings, **kw)

    def serve_good_tools(self, zipped: bytes | None = None) -> bytes:
        exe = b"MZ fake yt-dlp"
        zipped = zipped if zipped is not None else _zip({"ff/bin/ffmpeg.exe": b"ff"})
        self.web.add("/y/yt-dlp.exe", exe)
        self.web.add("/y/SHA2-256SUMS", _sums("yt-dlp.exe", exe))
        self.web.add("/f/ff.zip", zipped)
        self.web.add("/f/checksums.sha256", _sums("ff.zip", zipped))
        return exe

    async def started(self) -> None:
        """Wait until the fake yt-dlp is running (it writes its arguments first)."""
        for _ in range(200):
            if self.argv.exists():
                return
            await asyncio.sleep(0.05)
        self.fail("the fake yt-dlp didn't start")


class DownloadTest(TrailerTestCase):
    async def test_download_success_returns_mp4(self) -> None:
        result = await self.service().download(trailer(self.tmp))
        assert result == self.tmp / "Dune Part Two (2024) - Trailer.mp4"
        assert result.read_bytes() == b"fake trailer"

    async def test_a_seasons_own_trailer_comes_before_a_search(self) -> None:
        asked: list[tuple[int, int]] = []

        async def season_trailer(tv_id: int, season: int) -> str | None:
            asked.append((tv_id, season))
            return "https://www.youtube.com/watch?v=abc" if season == 3 else None

        service = self.service(season_trailer=season_trailer)
        own = trailer(self.tmp).model_copy(update={"tmdb_id": 7, "season": 3})
        self.assertEqual(await service._source(own), "https://www.youtube.com/watch?v=abc")
        none = own.model_copy(update={"season": 2})
        self.assertEqual(await service._source(none), own.query)  # no trailer of its own: searched
        self.assertEqual(asked, [(7, 3), (7, 2)])
        film = trailer(self.tmp)
        self.assertEqual(await service._source(film), film.query)  # a film: never asked
        self.assertEqual(len(asked), 2)

    async def test_tmdb_failing_falls_back_on_the_search(self) -> None:
        async def season_trailer(tv_id: int, season: int) -> str | None:
            raise TMDBError("no")

        own = trailer(self.tmp).model_copy(update={"tmdb_id": 7, "season": 3})
        service = self.service(season_trailer=season_trailer)
        self.assertEqual(await service._source(own), own.query)

    async def test_download_failure_writes_url_shortcut(self) -> None:
        os.environ["FAKE_FAIL"] = "1"
        with self.assertLogs("app.forge.clients.trailers", logging.WARNING) as logs:
            result = await self.service().download(trailer(self.tmp))
        assert result == self.tmp / "Dune Part Two (2024) - Trailer.url"
        assert result is not None
        text = result.read_text(encoding="utf-8")
        assert "[InternetShortcut]" in text
        assert (
            "URL=https://www.youtube.com/results?search_query=Dune+Part+Two+2024+official+trailer"
            in text
        )
        # one failed try, one self-update, one retry
        assert [("-U" in c) for c in calls(self.argv)] == [False, True, False]
        # logs name neither addresses nor folders
        joined = "\n".join(logs.output)
        assert "http" not in joined and str(self.tmp) not in joined

    async def test_existing_trailer_is_not_redownloaded(self) -> None:
        (self.tmp / "Dune Part Two (2024) - Trailer.MP4").write_bytes(b"mine")
        assert await self.service().download(trailer(self.tmp)) is None
        assert (self.tmp / "Dune Part Two (2024) - Trailer.MP4").read_bytes() == b"mine"
        assert calls(self.argv) == []

    async def test_command_has_expected_args(self) -> None:
        bin_dir = self.tmp / "tools" / "ffmpeg" / "bin"
        svc = self.service()
        await svc.download(trailer(self.tmp))
        bin_dir.mkdir(parents=True)
        (bin_dir / "ffmpeg.exe").write_bytes(b"x")
        (self.tmp / "other").mkdir()
        await svc.download(trailer(self.tmp / "other"))
        without, with_ffmpeg = calls(self.argv)
        assert "--ffmpeg-location" not in without
        assert without[without.index("-f") + 1] == "best[ext=mp4]/best"
        assert with_ffmpeg[with_ffmpeg.index("-f") + 1] == FORMAT
        assert with_ffmpeg[with_ffmpeg.index("--merge-output-format") + 1] == "mp4"
        assert with_ffmpeg[with_ffmpeg.index("--ffmpeg-location") + 1] == str(bin_dir)
        assert with_ffmpeg[with_ffmpeg.index("-o") + 1].endswith(
            "Dune Part Two (2024) - Trailer.%(ext)s"
        )
        assert with_ffmpeg[-1] == "ytsearch1:Dune Part Two 2024 official trailer"
        assert "--no-playlist" in with_ffmpeg

    async def test_ytdlp_runs_without_a_console_window(self) -> None:
        real = asyncio.create_subprocess_exec
        seen: list[int] = []

        async def spy(*args: Any, **kw: Any) -> asyncio.subprocess.Process:
            seen.append(kw.get("creationflags", -1))
            return await real(*args, **kw)

        with mock.patch("asyncio.create_subprocess_exec", spy):
            await self.service().download(trailer(self.tmp))
        assert seen == [getattr(subprocess, "CREATE_NO_WINDOW", 0)]

    async def test_cancel_kills_ytdlp(self) -> None:
        # Review I4: a cancelled download must not keep writing into the user's folder.
        os.environ["FAKE_SLEEP"] = "2"
        task = asyncio.create_task(self.service().download(trailer(self.tmp)))
        await self.started()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(2.5)
        assert not (self.tmp / "Dune Part Two (2024) - Trailer.mp4").exists()

    async def test_cancel_removes_partial_pieces(self) -> None:
        stem = self.tmp / "Dune Part Two (2024) - Trailer"
        os.environ["FAKE_SLEEP"] = "5"
        task = asyncio.create_task(self.service().download(trailer(self.tmp)))
        await self.started()
        Path(f"{stem}.f137.mp4.part").write_bytes(b"half")
        Path(f"{stem}.mp4.ytdl").write_bytes(b"state")
        (self.tmp / "keep.mp4.part").write_bytes(b"someone else's")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        assert sorted(p.name for p in self.tmp.iterdir() if "Trailer" in p.name) == []
        assert (self.tmp / "keep.mp4.part").exists()


class UpdateTest(TrailerTestCase):
    async def test_maybe_update_respects_interval(self) -> None:
        svc = self.service()
        await self.settings.update(ytdlp_checked_at=time.time())
        await svc.maybe_update()
        assert calls(self.argv) == []
        await self.settings.update(ytdlp_checked_at=0)
        await svc.maybe_update()
        assert calls(self.argv) == [["-U"]]
        assert (await self.repo.load()).ytdlp_checked_at > 0


class InstallTest(TrailerTestCase):
    async def test_install_verifies_checksum_and_extracts_only_bin(self) -> None:
        zipped = _zip(
            {
                "ffmpeg-master-latest-win64-gpl-shared/bin/ffmpeg.exe": b"ff",
                "ffmpeg-master-latest-win64-gpl-shared/bin/avcodec-61.dll": b"dll",
                "ffmpeg-master-latest-win64-gpl-shared/doc/readme.txt": b"doc",
            }
        )
        exe = self.serve_good_tools(zipped)
        svc = self.service(**TOOL_URLS)
        progress: list[tuple[str, float]] = []
        await svc.install_tools(lambda step, frac: progress.append((step, frac)))
        tools = self.tmp / "tools"
        assert (tools / "yt-dlp.exe").read_bytes() == exe
        assert (tools / "ffmpeg" / "bin" / "ffmpeg.exe").read_bytes() == b"ff"
        assert (tools / "ffmpeg" / "bin" / "avcodec-61.dll").exists()
        assert not (tools / "ffmpeg" / "doc").exists()
        assert not list(tools.glob("*.part")) and not list(tools.glob("*.zip"))
        assert svc.tools_present()
        assert progress and progress[-1] == ("done", 1.0)
        assert {step for step, _ in progress} == {"yt-dlp", "ffmpeg", "done"}
        stored = await self.repo.load()
        assert stored.trailer_tools_ok and stored.ytdlp_checked_at > 0

    async def test_install_follows_redirects(self) -> None:
        # GitHub's latest/download links answer with a redirect to the real file.
        exe = self.serve_good_tools()
        self.web.add(
            "/y/yt-dlp.exe", status=302, headers={"Location": f"{WEB}/cdn/yt-dlp.exe"}
        )
        self.web.add("/cdn/yt-dlp.exe", exe)
        svc = self.service(**TOOL_URLS)
        await svc.install_tools(lambda step, frac: None)
        assert (self.tmp / "tools" / "yt-dlp.exe").read_bytes() == exe

    async def test_install_rejects_bad_checksum(self) -> None:
        self.web.add("/y/yt-dlp.exe", b"tampered")
        self.web.add("/y/SHA2-256SUMS", _sums("yt-dlp.exe", b"original"))
        svc = self.service(**TOOL_URLS)
        with self.assertRaises(ToolInstallError):
            await svc.install_tools(lambda step, frac: None)
        assert not (self.tmp / "tools" / "yt-dlp.exe").exists()
        assert not list((self.tmp / "tools").glob("*"))
        assert not (await self.repo.load()).trailer_tools_ok

    async def test_bad_ffmpeg_checksum_removes_ytdlp_too(self) -> None:
        self.serve_good_tools()
        self.web.add("/f/ff.zip", b"tampered zip")
        svc = self.service(**TOOL_URLS)
        with self.assertRaises(ToolInstallError):
            await svc.install_tools(lambda step, frac: None)
        assert not list((self.tmp / "tools").glob("*"))

    async def test_install_needs_a_published_checksum(self) -> None:
        self.serve_good_tools()
        self.web.add("/y/SHA2-256SUMS", b"ffff  something-else.exe\n")
        with self.assertRaises(ToolInstallError):
            await self.service(**TOOL_URLS).install_tools(lambda step, frac: None)
        assert self.web.requests_to("/y/yt-dlp.exe") == []

    async def test_http_errors_become_tool_errors_without_the_address(self) -> None:
        self.serve_good_tools()
        self.web.add("/y/yt-dlp.exe", status=500)
        with self.assertRaises(ToolInstallError) as caught:
            await self.service(**TOOL_URLS).install_tools(lambda step, frac: None)
        assert "500" in str(caught.exception) and WEB not in str(caught.exception)
        self.web.add("/y/SHA2-256SUMS", status=404)
        with self.assertRaises(ToolInstallError) as caught:
            await self.service(**TOOL_URLS).install_tools(lambda step, frac: None)
        assert "404" in str(caught.exception)

    async def test_connection_failure_hides_the_exception_text(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError(f"cannot reach {request.url}", request=request)

        http = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
        self.addAsyncCleanup(http.aclose)
        svc = TrailerService(self.tmp / "tools", http, self.settings, **TOOL_URLS)
        with self.assertLogs("app.forge.clients.trailers", logging.WARNING) as logs:
            with self.assertRaises(ToolInstallError) as caught:
                await svc.install_tools(lambda step, frac: None)
        assert WEB not in str(caught.exception)
        assert caught.exception.__cause__ is None and caught.exception.__suppress_context__
        assert all(WEB not in line for line in logs.output)
        assert not list((self.tmp / "tools").glob("*"))

    async def test_install_twice_at_once_downloads_once(self) -> None:
        self.serve_good_tools()
        svc = self.service(ytdlp_command=None, **TOOL_URLS)
        await asyncio.gather(
            svc.install_tools(lambda s, f: None), svc.install_tools(lambda s, f: None)
        )
        assert len(self.web.requests_to("/y/yt-dlp.exe")) == 1
        assert svc.tools_present()


class PureTest(ForgeTestCase):
    def test_sha256_for_parses_sums(self) -> None:
        text = "abc123  yt-dlp.exe\ndef456 *yt-dlp_x86.exe\n"
        assert sha256_for(text, "yt-dlp.exe") == "abc123"
        assert sha256_for(text, "yt-dlp_x86.exe") == "def456"
        assert sha256_for(text, "missing") is None

    def test_youtube_search_url(self) -> None:
        assert youtube_search_url("The Boys", 2019, 4) == (
            "https://www.youtube.com/results?search_query=The+Boys+2019+season+4+official+trailer"
        )
        assert youtube_search_url("Fast & Furious", None) == (
            "https://www.youtube.com/results?search_query=Fast+%26+Furious+official+trailer"
        )

    def test_only_the_two_published_https_sources(self) -> None:
        for url in (YTDLP_URL, YTDLP_SUMS_URL, FFMPEG_URL, FFMPEG_SUMS_URL):
            assert url.startswith("https://github.com/yt-dlp/")
