"""Trailer downloads through yt-dlp, plus fetching yt-dlp and ffmpeg safely on first use.

MediaForge's `api/trailers.py` on AirGrabber's shared httpx client. The tools live in
`AppPaths.forge / "tools"` and are downloaded fresh from their two published HTTPS sources, each
checked against its published SHA-256 sums (INTEGRATION.md D12). This is not AirGrabber's trailer
player: it saves a trailer file next to a renamed movie.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import subprocess
import time
import zipfile
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path, PurePosixPath
from urllib.parse import quote_plus

import httpx

from app.forge.core import fsops
from app.forge.core.activity import ActivityFeed, say_to
from app.forge.core.parser import VIDEO_EXTENSIONS
from app.forge.core.settings import ForgeSettingsService
from app.forge.models.plan import PlannedTrailer
from app.i18n import t
from app.services.tmdb import TMDBError

log = logging.getLogger(__name__)

YTDLP_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
YTDLP_SUMS_URL = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/SHA2-256SUMS"
FFMPEG_URL = (
    "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/"
    "ffmpeg-master-latest-win64-gpl-shared.zip"
)
FFMPEG_SUMS_URL = (
    "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/checksums.sha256"
)
FORMAT = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
SINGLE_FILE_FORMAT = "best[ext=mp4]/best"
UPDATE_INTERVAL_S = 7 * 24 * 3600
DOWNLOAD_TIMEOUT_S = 15 * 60
CHUNK = 1024 * 1024
# MediaForge waited up to 60 s per read for the tools (the shared client's own is 10 s).
TOOL_TIMEOUT = httpx.Timeout(10.0, connect=5.0, read=60.0)

type Progress = Callable[[str, float], None]


class ToolInstallError(Exception):
    """yt-dlp or ffmpeg couldn't be downloaded or didn't match its published checksum."""


def youtube_search_url(title: str, year: int | None) -> str:
    words = f"{title} {year} official trailer" if year else f"{title} official trailer"
    return "https://www.youtube.com/results?search_query=" + quote_plus(words)


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
                with z.open(info) as src, (target / parts[2]).open("wb") as dst:
                    shutil.copyfileobj(src, dst)


def _remove_partials(stem: Path) -> None:
    """Delete half-downloaded pieces yt-dlp leaves behind when it is stopped."""
    for name in fsops.list_names(stem.parent):
        if name.startswith(stem.name) and (".part" in name or name.endswith(".ytdl")):
            try:
                fsops.remove_file(stem.parent / name)
            except OSError:
                pass


class TrailerService:
    def __init__(
        self,
        tools_dir: Path,
        http: httpx.AsyncClient,
        settings: ForgeSettingsService,
        *,
        ytdlp_command: Sequence[str] | None = None,
        ytdlp_url: str = YTDLP_URL,
        ytdlp_sums_url: str = YTDLP_SUMS_URL,
        ffmpeg_url: str = FFMPEG_URL,
        ffmpeg_sums_url: str = FFMPEG_SUMS_URL,
        activity: ActivityFeed | None = None,
        season_trailer: Callable[[int, int], Awaitable[str | None]] | None = None,
    ) -> None:
        """`season_trailer(tv_id, season)`: a season's own trailer, when TMDB lists one."""
        self._season_trailer = season_trailer
        self._tools = tools_dir
        self._say = say_to(activity)  # the Activity strip, next to each English log line
        self._http = http
        self._settings = settings
        self._command = list(ytdlp_command) if ytdlp_command else None
        self._urls = (ytdlp_url, ytdlp_sums_url, ffmpeg_url, ffmpeg_sums_url)
        self._install_lock = asyncio.Lock()

    @property
    def _ytdlp(self) -> Path:
        return self._tools / "yt-dlp.exe"

    @property
    def _ffmpeg_bin(self) -> Path:
        return self._tools / "ffmpeg" / "bin"

    def _base_command(self) -> list[str]:
        return self._command or [str(self._ytdlp)]

    def tools_present(self) -> bool:
        has_ytdlp = self._command is not None or self._ytdlp.exists()
        return has_ytdlp and (self._ffmpeg_bin / "ffmpeg.exe").exists()

    # --- first-use install ----------------------------------------------------------------

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

    async def install_tools(self, on_progress: Progress) -> None:
        async with self._install_lock:
            if self.tools_present():
                on_progress("done", 1.0)
                return
            await self._install(on_progress)

    async def _install(self, on_progress: Progress) -> None:
        ytdlp_url, ytdlp_sums, ffmpeg_url, ffmpeg_sums = self._urls
        self._tools.mkdir(parents=True, exist_ok=True)
        try:
            try:
                await self._fetch_verified(ytdlp_url, ytdlp_sums, self._ytdlp, "yt-dlp", on_progress)
                zip_path = self._tools / "ffmpeg.zip"
                try:
                    await self._fetch_verified(
                        ffmpeg_url, ffmpeg_sums, zip_path, "ffmpeg", on_progress
                    )
                    await asyncio.to_thread(_extract_bin, zip_path, self._ffmpeg_bin)
                finally:
                    zip_path.unlink(missing_ok=True)
            except httpx.HTTPError as exc:
                # Never the exception's text: it may hold the address.
                log.warning("Trailer tools: download failed (%s)", type(exc).__name__)
                raise ToolInstallError(t("Download failed: could not reach GitHub.")) from None
        except ToolInstallError:
            self._ytdlp.unlink(missing_ok=True)
            shutil.rmtree(self._tools / "ffmpeg", ignore_errors=True)
            raise
        await self._settings.update(trailer_tools_ok=True, ytdlp_checked_at=time.time())
        on_progress("done", 1.0)
        log.info("Trailer tools installed")
        self._say(t("Trailer tools installed"), success=True)

    # --- running yt-dlp -------------------------------------------------------------------

    async def _run(self, args: Sequence[str]) -> int:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = await asyncio.create_subprocess_exec(
            *self._base_command(),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            creationflags=flags,
        )
        try:
            await asyncio.wait_for(proc.communicate(), DOWNLOAD_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            log.warning("yt-dlp took too long and was stopped")
            return -1
        except asyncio.CancelledError:
            proc.kill()  # never leave it writing into the user's folder
            await asyncio.shield(proc.wait())
            raise
        if proc.returncode:
            # Only the exit code: yt-dlp's own output names addresses and the user's folders.
            log.warning("yt-dlp failed (exit code %s)", proc.returncode)
        return proc.returncode or 0

    async def maybe_update(self) -> None:
        if time.time() - self._settings.current.ytdlp_checked_at < UPDATE_INTERVAL_S:
            return
        await self._run(["-U"])
        await self._settings.update(ytdlp_checked_at=time.time())

    @staticmethod
    def _existing(stem: Path) -> Path | None:
        wanted = stem.name.casefold()
        for name in fsops.list_names(stem.parent):
            entry = stem.parent / name
            if entry.stem.casefold() == wanted and entry.suffix.lower() in VIDEO_EXTENSIONS:
                return entry
        return None

    async def _source(self, trailer: PlannedTrailer) -> str:
        """What yt-dlp fetches: the season's own trailer when TMDB lists one, else a search."""
        if self._season_trailer is None or trailer.tmdb_id is None or trailer.season is None:
            return trailer.query
        try:
            url = await self._season_trailer(trailer.tmdb_id, trailer.season)
        except TMDBError as exc:
            log.info("No season trailer from TMDB (%s); searching instead", type(exc).__name__)
            return trailer.query
        return url or trailer.query

    async def download(self, trailer: PlannedTrailer) -> Path | None:
        """Download the trailer; on failure leave a YouTube search shortcut instead."""
        stem = trailer.target_stem
        if await asyncio.to_thread(self._existing, stem):
            return None
        has_ffmpeg = (self._ffmpeg_bin / "ffmpeg.exe").exists()
        args = ["--no-playlist", "--newline"]
        if has_ffmpeg:
            args += ["-f", FORMAT, "--merge-output-format", "mp4"]
            args += ["--ffmpeg-location", str(self._ffmpeg_bin)]
        else:
            args += ["-f", SINGLE_FILE_FORMAT]
        args += ["-o", f"{stem}.%(ext)s", await self._source(trailer)]

        try:
            if await self._run(args) != 0:
                await self._run(["-U"])  # YouTube changes often; a fresh yt-dlp usually fixes it
                await self._run(args)
        except asyncio.CancelledError:
            await asyncio.to_thread(_remove_partials, stem)
            raise
        found = await asyncio.to_thread(self._existing, stem)
        if found is not None:
            return found

        shortcut = stem.with_name(stem.name + ".url")
        url = youtube_search_url(trailer.title, trailer.year)
        content = f"[InternetShortcut]\nURL={url}\n".encode()
        await asyncio.to_thread(Path(fsops.long_path(shortcut)).write_bytes, content)
        log.warning("Trailer for %s couldn't be downloaded; saved a search link", trailer.title)
        self._say(
            t(
                "Trailer for {title} couldn't be downloaded; saved a search link",
                title=trailer.title,
            ),
            level=logging.WARNING,
        )
        return shortcut
