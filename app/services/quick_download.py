"""One-click download: search every source, pick the best release by itself, grab it.

UI-independent, so an automatic "download new episodes" job can use it as well as the cards'
Download buttons. Downloads of the same target run one after another, and one whose target is in
the download history by then is skipped, so two windows (or a window and the automatic job)
never grab the same thing twice.

Log hygiene: release links may embed an API key, so only the request's show / film and episode
and the outcome are logged, never a release or a URL.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

from app.models import (
    GrabTarget,
    IndexerState,
    MediaType,
    QuickDownloadResult,
    SearchKind,
    SearchRequest,
    TorrentRelease,
)
from app.services.downloads import DownloadService
from app.services.picking import pick_best
from app.services.search import TorrentSearchService
from app.services.settings import SettingsService

log = logging.getLogger(__name__)

Progress = Callable[[str], None]

# (media type, TVMaze show id or TMDB movie id, season, episode)
_TargetKey = tuple[MediaType, int, int | None, int | None]

SEARCH_TIMEOUT = 30.0  # seconds a one-click download waits for the sources, in all


@dataclass(slots=True)
class _TargetLock:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0  # holding or waiting


def request_label(request: SearchRequest) -> str:
    """"Severance S02E01", "Severance S02" or "Dune (2021)": what the request is for."""
    if request.kind is SearchKind.MOVIE:
        return f"{request.title} ({request.year})" if request.year else request.title
    code = f"S{request.season or 0:02d}"
    if request.kind is SearchKind.EPISODE:
        code += f"E{request.episode or 0:02d}"
    return f"{request.title} {code}"


class QuickDownloadService:
    """Finds the best release for a request and grabs it."""

    def __init__(
        self,
        search: TorrentSearchService,
        downloads: DownloadService,
        settings: SettingsService,
        *,
        timeout: float = SEARCH_TIMEOUT,
    ) -> None:
        self._search = search
        self._downloads = downloads
        self._settings = settings
        self._timeout = timeout
        self._target_locks: dict[_TargetKey, _TargetLock] = {}

    @contextlib.asynccontextmanager
    async def _target_lock(self, target: GrabTarget) -> AsyncIterator[None]:
        """Hold `target`'s lock; the entry is dropped once nobody holds or waits for it."""
        key = (target.media_type, target.media_id, target.season, target.episode)
        entry = self._target_locks.get(key)
        if entry is None:
            entry = self._target_locks[key] = _TargetLock()
        entry.users += 1
        try:
            async with entry.lock:
                yield
        finally:
            entry.users -= 1
            if entry.users == 0:
                del self._target_locks[key]

    async def download(
        self, request: SearchRequest, target: GrabTarget, progress: Progress | None = None
    ) -> QuickDownloadResult:
        """Search every source for `request`, pick the best release (`pick_best`) and grab it
        for `target`.

        One download per target at a time: a call for a target already being downloaded waits
        for that one. A target in the download history by then (or from before) is not searched
        again: `already_downloaded=True` and nothing is grabbed.

        The search runs to completion, but after `timeout` seconds in all the pick is made from
        whatever has arrived. Nothing suitable: `found=False` and nothing is grabbed. Grab
        failures propagate (GrabError, or RemoteError from the remote torrent program).
        `progress` receives short status texts along the way.
        """
        label = request_label(request)
        async with self._target_lock(target):
            if await self._downloads.has_grabbed(target):
                log.info("One-click %s: already downloaded, not searching again", label)
                return QuickDownloadResult(found=False, already_downloaded=True)
            return await self._search_and_grab(request, target, label, progress)

    async def _search_and_grab(
        self, request: SearchRequest, target: GrabTarget, label: str, progress: Progress | None
    ) -> QuickDownloadResult:
        settings = self._settings.current
        releases = await self._collect(request, label, progress)
        best = pick_best(releases, request, settings)
        if best is None:
            log.info("One-click %s: nothing suitable among %d results", label, len(releases))
            return QuickDownloadResult(found=False)
        quality = best.resolution.value if best.resolution else None
        log.info(
            "One-click %s: picked a %s release with %d seeders from %d results",
            label,
            quality or "unknown-quality",
            best.seeders,
            len(releases),
        )
        if progress is not None:
            progress("Grabbing the best one…")
        grab = await self._downloads.grab(best, target)
        log.info("One-click %s: %s", label, grab.outcome.value)
        return QuickDownloadResult(
            found=True,
            release_title=best.title,
            quality=quality,
            seeders=best.seeders,
            grab=grab,
        )

    async def _collect(
        self, request: SearchRequest, label: str, progress: Progress | None
    ) -> tuple[TorrentRelease, ...]:
        """The shown releases of the last search update that arrived within the timeout."""
        releases: tuple[TorrentRelease, ...] = ()
        deadline = asyncio.timeout(self._timeout)
        try:
            async with deadline:
                async with contextlib.aclosing(self._search.search(request)) as updates:
                    async for update in updates:
                        releases = update.releases
                        if progress is not None:
                            waiting = sum(
                                status.state is IndexerState.SEARCHING
                                for status in update.statuses
                            )
                            sources = "source" if waiting == 1 else "sources"
                            progress(
                                f"Searching… {len(releases)} found"
                                + (f", {waiting} {sources} still searching" if waiting else "")
                            )
        except TimeoutError:
            if not deadline.expired():
                raise
            log.info(
                "One-click %s: stopped waiting for the sources after %g s", label, self._timeout
            )
        return releases
