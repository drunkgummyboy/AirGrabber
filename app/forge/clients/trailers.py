"""Trailer downloads through yt-dlp, plus fetching yt-dlp and ffmpeg safely on first use.

MediaForge's `api/trailers.py` on AirGrabber's shared httpx client. The tools live in
`AppPaths.forge / "tools"`; fetching them is AirGrabber's (app/services/media_tools.py: their two
published HTTPS sources, each checked against its published SHA-256 sums, INTEGRATION.md D12),
which also fetches them by itself after an install or update. This is not AirGrabber's trailer
player: it saves a trailer file next to a renamed movie.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
import time
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from urllib.parse import quote_plus

import httpx

from app.forge.core import fsops
from app.forge.core.activity import ActivityFeed, say_to
from app.forge.core.parser import VIDEO_EXTENSIONS
from app.forge.core.planner import trailer_words
from app.forge.core.settings import ForgeSettingsService
from app.forge.models.plan import PlannedTrailer
from app.i18n import t
from app.services.media_tools import (
    FFMPEG_SUMS_URL,
    FFMPEG_URL,
    YTDLP_SUMS_URL,
    YTDLP_URL,
    MediaTools,
    Progress,
    ToolInstallError,
    sha256_for,
)
from app.services.tmdb import TMDBError

__all__ = [
    "FFMPEG_SUMS_URL",
    "FFMPEG_URL",
    "FORMAT",
    "YTDLP_SUMS_URL",
    "YTDLP_URL",
    "ToolInstallError",
    "TrailerService",
    "sha256_for",
    "youtube_search_url",
]

log = logging.getLogger(__name__)

FORMAT = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
SINGLE_FILE_FORMAT = "best[ext=mp4]/best"
UPDATE_INTERVAL_S = 7 * 24 * 3600
DOWNLOAD_TIMEOUT_S = 15 * 60


def youtube_search_url(title: str, year: int | None, season: int | None = None) -> str:
    return "https://www.youtube.com/results?search_query=" + quote_plus(
        trailer_words(title, year, season)
    )


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
        self._fetcher = MediaTools(
            tools_dir,
            http,
            ytdlp_url=ytdlp_url,
            ytdlp_sums_url=ytdlp_sums_url,
            ffmpeg_url=ffmpeg_url,
            ffmpeg_sums_url=ffmpeg_sums_url,
        )

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

    @property
    def installing(self) -> bool:
        """The tools are being downloaded right now (Forge's start or AirGrabber's own fetch,
        app/services/media_tools.py)."""
        return self._fetcher.busy

    async def install_tools(self, on_progress: Progress) -> None:
        try:
            fetched = await self._fetcher.ensure(on_progress)
        except ToolInstallError:
            log.warning("Trailer tools couldn't be installed")
            raise
        if fetched:
            await self._settings.update(trailer_tools_ok=True, ytdlp_checked_at=time.time())
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
        url = youtube_search_url(trailer.title, trailer.year, trailer.season)
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
