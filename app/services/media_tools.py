"""The video tools (ffmpeg, ffprobe and yt-dlp) come with AirGrabber (the owner, 2026-11-21).

install.ps1 gets them during the install (`main.py --fetch-tools`, `fetch_once`); a copy that
lacks them (one updated from before, or an install whose fetch failed) fetches them once in the
background after its start (`MediaTools.run`, spawned from the main window), trying again every
RETRY_AFTER_S while it runs, and at the next start. When they are there, nothing happens.

They live in `AppPaths.forge / "tools"` (never under `app/`): `yt-dlp.exe`, and `ffmpeg/bin/`
(ffmpeg.exe, ffprobe.exe and their DLLs). Each comes from its published HTTPS source on GitHub
(yt-dlp's own releases; yt-dlp's FFmpeg-Builds, the shared GPL build), about 110 MB together, and
is kept only when it matches its published SHA-256 sum (INTEGRATION.md D12). MediaForge's trailers
(app/forge/clients/trailers.py), the phone-sized copies and the Cast streams use them. The
trailers' fetch is this one: one lock per tools folder keeps two from fetching at once.

Logs never give a URL, a path or an exception's text.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import zipfile
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import httpx

from app import APP_NAME, __version__
from app.i18n import N_, t

if TYPE_CHECKING:
    from app.config import AppPaths

log = logging.getLogger(__name__)

TOOLS_FOLDER = "tools"  # under AppPaths.forge
YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
YTDLP_SUMS_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/SHA2-256SUMS"
FFMPEG_URL = (
    "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-master-latest-win64-gpl-shared.zip"
)
FFMPEG_SUMS_URL = (
    "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/checksums.sha256"
)
CHUNK = 1024 * 1024
# MediaForge waited up to 60 s per read for the tools (the shared client's own is 10 s).
TOOL_TIMEOUT = httpx.Timeout(10.0, connect=5.0, read=60.0)
RETRY_AFTER_S = 3600.0  # a failed background fetch, again while AirGrabber runs

# What the phone's features say while the tools aren't there (cast_stream.py, copies.py).
TOOLS_COMING_TEXT = N_(
    "AirGrabber is still getting its video tools (ffmpeg) for this PC, so try again in a few "
    "minutes."
)
TOOLS_NOT_YET_TEXT = N_(
    "AirGrabber couldn't get its video tools (ffmpeg) for this PC yet and tries again by itself, "
    "so try again later."
)

type Progress = Callable[[str, float], None]


class ToolInstallError(Exception):
    """yt-dlp or ffmpeg couldn't be downloaded or didn't match its published checksum (its text
    is translated and names no address)."""


def tools_folder(paths: AppPaths) -> Path:
    return paths.forge / TOOLS_FOLDER


def ffmpeg_bin(tools: Path) -> Path:
    """ffmpeg's bin folder (ffmpeg.exe, ffprobe.exe) in a tools folder."""
    return tools / "ffmpeg" / "bin"


def sha256_for(sums_text: str, filename: str) -> str | None:
    for line in sums_text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == filename:
            return parts[0].lower()
    return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _extract_bin(zip_path: Path, target: Path) -> None:
    """Keep only <top>/bin/* from the ffmpeg build."""
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            parts = PurePosixPath(info.filename).parts
            if len(parts) == 3 and parts[1] == "bin" and not info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                part = target / f"{parts[2]}.part"  # a start cut short leaves no half a program
                with z.open(info) as src, part.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
                os.replace(part, target / parts[2])


# One lock per tools folder (and loop): the trailers' fetch and the background one never at once.
_LOCKS: dict[str, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}


def _key(folder: Path) -> str:
    return os.path.normcase(os.path.abspath(folder))


def _lock_for(folder: Path) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    for key in [key for key, (old, _lock) in _LOCKS.items() if old.is_closed()]:
        del _LOCKS[key]  # a closed loop's (tests run many) is let go
    held = _LOCKS.get(_key(folder))
    if held is None or held[0] is not loop:
        held = _LOCKS[_key(folder)] = (loop, asyncio.Lock())
    return held[1]


def _quiet(_step: str, _fraction: float) -> None:
    pass


class MediaTools:
    def __init__(
        self,
        tools_dir: Path,
        http: httpx.AsyncClient,
        *,
        ytdlp_url: str = YTDLP_URL,
        ytdlp_sums_url: str = YTDLP_SUMS_URL,
        ffmpeg_url: str = FFMPEG_URL,
        ffmpeg_sums_url: str = FFMPEG_SUMS_URL,
        retry_after_s: float = RETRY_AFTER_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        """`tools_dir`: `AppPaths.forge / "tools"`. The rest: for tests."""
        self._tools = tools_dir
        self._http = http
        self._urls = (ytdlp_url, ytdlp_sums_url, ffmpeg_url, ffmpeg_sums_url)
        self._retry_after_s = retry_after_s
        self._sleep = sleep
        self._failed = False  # the last background try failed (until the next one)
        self._listeners: list[Callable[[], Awaitable[None]]] = []

    @property
    def tools_dir(self) -> Path:
        return self._tools

    @property
    def ytdlp(self) -> Path:
        return self._tools / "yt-dlp.exe"

    @property
    def ffmpeg_bin(self) -> Path:
        return ffmpeg_bin(self._tools)

    def present(self) -> bool:
        """yt-dlp, ffmpeg and ffprobe are all there."""
        return (
            self.ytdlp.is_file()
            and (self.ffmpeg_bin / "ffmpeg.exe").is_file()
            and (self.ffmpeg_bin / "ffprobe.exe").is_file()
        )

    @property
    def busy(self) -> bool:
        """Being fetched right now (here, or by the trailers' fetch)."""
        held = _LOCKS.get(_key(self._tools))
        return held is not None and held[1].locked()

    def missing_text(self) -> str:
        """What a feature that needs them says while they aren't there (translated)."""
        return t(TOOLS_NOT_YET_TEXT if self._failed and not self.busy else TOOLS_COMING_TEXT)

    def when_fetched(self, listener: Callable[[], Awaitable[None]]) -> None:
        """`listener()` once the background fetch (`run`) finds them there after they were
        missing (whoever fetched them)."""
        self._listeners.append(listener)

    # -- fetching ----------------------------------------------------------------------------

    async def ensure(self, on_progress: Progress = _quiet) -> bool:
        """Fetch them unless they are all there: True when this call fetched them. Raises
        ToolInstallError (nothing half-fetched left)."""
        async with _lock_for(self._tools):
            if await asyncio.to_thread(self.present):
                on_progress("done", 1.0)
                return False
            await self._install(on_progress)
            on_progress("done", 1.0)
            return True

    async def run(self) -> None:
        """After AirGrabber's start (spawned on the loop): fetch them when they're missing; a
        failure tries again every RETRY_AFTER_S while AirGrabber runs. Never raises."""
        if await asyncio.to_thread(self.present):
            return
        log.info("Video tools missing: fetching them")
        while True:
            try:
                await self.ensure()
            except ToolInstallError:  # its own warning said why, without the address
                self._failed = True
            except Exception as exc:  # the type only: a message could hold a path
                log.warning("Video tools: fetching failed (%s)", type(exc).__name__)
                self._failed = True
            else:
                self._failed = False
                log.info("Video tools are there")
                await self._tell()
                return
            log.info("Video tools: trying again in %.0f min", self._retry_after_s / 60)
            await self._sleep(self._retry_after_s)

    async def _tell(self) -> None:
        for listener in list(self._listeners):
            try:
                await listener()
            except Exception as exc:
                log.warning("Video tools: a listener failed (%s)", type(exc).__name__)

    async def _download(self, url: str, dest: Path, step: str, on_progress: Progress) -> None:
        async with self._http.stream("GET", url, timeout=TOOL_TIMEOUT) as resp:
            if resp.status_code != 200:
                raise ToolInstallError(
                    t("{step}: download failed (HTTP {status})", step=step, status=resp.status_code)
                )
            try:
                total = int(resp.headers.get("content-length", "0"))
            except ValueError:
                total = 0
            done = 0
            with dest.open("wb") as f:
                async for chunk in resp.aiter_bytes(CHUNK):
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        on_progress(step, min(done / total, 0.99))

    async def _sums(self, url: str, step: str) -> str:
        resp = await self._http.get(url, timeout=TOOL_TIMEOUT)
        if resp.status_code != 200:
            raise ToolInstallError(
                t("{step}: checksum list unavailable (HTTP {status})", step=step, status=resp.status_code)
            )
        return resp.text

    async def _fetch_verified(
        self, url: str, sums_url: str, dest: Path, step: str, on_progress: Progress
    ) -> None:
        name = url.rsplit("/", 1)[-1]
        expected = sha256_for(await self._sums(sums_url, step), name)
        if expected is None:
            raise ToolInstallError(t("{step}: no published checksum for {name}", step=step, name=name))
        part = dest.with_name(dest.name + ".part")
        try:
            await self._download(url, part, step, on_progress)
            if await asyncio.to_thread(_sha256, part) != expected:
                raise ToolInstallError(t("{step}: the download doesn't match its checksum", step=step))
            os.replace(part, dest)
        finally:
            part.unlink(missing_ok=True)

    async def _install(self, on_progress: Progress) -> None:
        ytdlp_url, ytdlp_sums, ffmpeg_url, ffmpeg_sums = self._urls
        self._tools.mkdir(parents=True, exist_ok=True)
        try:
            try:
                await self._fetch_verified(ytdlp_url, ytdlp_sums, self.ytdlp, "yt-dlp", on_progress)
                zip_path = self._tools / "ffmpeg.zip"
                try:
                    await self._fetch_verified(
                        ffmpeg_url, ffmpeg_sums, zip_path, "ffmpeg", on_progress
                    )
                    await asyncio.to_thread(_extract_bin, zip_path, self.ffmpeg_bin)
                finally:
                    zip_path.unlink(missing_ok=True)
            except httpx.HTTPError as exc:
                # Never the exception's text: it may hold the address.
                log.warning("Video tools: download failed (%s)", type(exc).__name__)
                raise ToolInstallError(t("Download failed: could not reach GitHub.")) from None
        except ToolInstallError:
            log.warning("Video tools couldn't be fetched")
            self.ytdlp.unlink(missing_ok=True)
            shutil.rmtree(self._tools / "ffmpeg", ignore_errors=True)
            raise
        log.info("Video tools fetched")


async def fetch_once(paths: AppPaths, *, http: httpx.AsyncClient | None = None) -> int:
    """`main.py --fetch-tools` (install.ps1): fetch them unless they are there. 0 when they are
    there now, 1 when they couldn't be fetched (the app tries again after its start). Never
    raises."""
    client = http or httpx.AsyncClient(
        timeout=httpx.Timeout(10.0, connect=5.0),
        follow_redirects=True,  # GitHub's latest/download links redirect
        headers={"User-Agent": f"{APP_NAME}/{__version__}"},
    )
    try:
        await MediaTools(tools_folder(paths), client).ensure()
    except ToolInstallError:
        return 1
    except Exception as exc:  # the type only: a message could hold a path
        log.warning("Video tools: fetching failed (%s)", type(exc).__name__)
        return 1
    finally:
        if http is None:
            await client.aclose()
    return 0
