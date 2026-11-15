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
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass, field

from app.i18n import N_, t, tn
from app.models import (
    GrabTarget,
    IndexerState,
    IndexerStatus,
    MediaType,
    QuickDownloadResult,
    SearchKind,
    SearchRequest,
    TorrentRelease,
)
from app.services.downloads import RELEASE_FAILURES, DownloadService, GrabError, ProgramInTorrent
from app.services.picking import rank_releases
from app.services.search import TorrentSearchService
from app.services.settings import SettingsService

log = logging.getLogger(__name__)

Progress = Callable[[str], None]

# (media type, TVMaze show id or TMDB movie id, season, episode)
_TargetKey = tuple[MediaType, int, int | None, int | None]

SEARCH_TIMEOUT = 30.0  # seconds a one-click download waits for the sources, in all
GRACE_S = 8.0  # once a release it would pick has turned up: how long the slower sources still get
# How many releases it tries when the best ones hold a program or can't be grabbed
# (RELEASE_FAILURES: ReleaseError, or the remote program's ReleaseRefused).
MAX_PICKS = 5


SOURCES_RESTING = N_(
    "The sources that can search this are resting after repeated failures; "
    "try again in a few minutes."
)


class SourcesResting(GrabError):
    """No source searched: every one that could is resting after failing
    (`TorrentSearchService`, INDEXER_REST_S). Not the release's fault: the automatic downloads
    stop their run without counting a try."""


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
        grace: float = GRACE_S,
    ) -> None:
        self._search = search
        self._downloads = downloads
        self._settings = settings
        self._timeout = timeout
        self._grace = grace
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

    def is_running(self, target: GrabTarget) -> bool:
        """A download of exactly `target` is searching, grabbing or waiting for its turn here."""
        key = (target.media_type, target.media_id, target.season, target.episode)
        return key in self._target_locks

    async def download(
        self,
        request: SearchRequest,
        target: GrabTarget,
        progress: Progress | None = None,
        *,
        again: bool = False,
        avoid: Iterable[str] = (),
    ) -> QuickDownloadResult:
        """Search every source for `request`, pick the best release (`pick_best`) and grab it
        for `target`.

        One download per target at a time: a call for a target already being downloaded waits
        for that one. A target in the download history by then (or from before) is not searched
        again: `already_downloaded=True` and nothing is grabbed; unless `again` (the user asked
        to download it again: ↻ Again → Auto).

        The search runs to completion, but after `timeout` seconds in all the pick is made from
        whatever has arrived; once something it would pick has arrived, the sources still
        searching get `grace` seconds more, not the rest of `timeout` (one source that hardly
        ever answers made every download wait the whole time). Nothing suitable: `found=False` and nothing is grabbed. Grab
        failures of the program or folder propagate at once (GrabError, or RemoteError from the
        remote torrent program). A pick that fails for its own reasons (`RELEASE_FAILURES`: a
        program inside, no link, no .torrent and nowhere to send its magnet, the remote program
        turning it down) gives way to the next best, up to MAX_PICKS; when none can be grabbed
        the last such error propagates (only programs inside, or a season pack: `found=False`,
        so the queue falls back to the season's episodes). Every source resting after failing
        (`TorrentSearchService`): `SourcesResting`, at once.
        `progress` receives short status texts along the way. `avoid`: release titles and info
        hashes (casefolded) never picked: the ones a stuck download was swapped from.
        """
        label = request_label(request)
        async with self._target_lock(target):
            if not again and await self._downloads.has_grabbed(target):
                log.info("One-click %s: already downloaded, not searching again", label)
                return QuickDownloadResult(found=False, already_downloaded=True)
            if again:
                log.info("One-click %s: downloading again, as asked", label)
            return await self._search_and_grab(
                request, target, label, progress, frozenset(avoid)
            )

    async def _search_and_grab(
        self,
        request: SearchRequest,
        target: GrabTarget,
        label: str,
        progress: Progress | None,
        avoid: frozenset[str] = frozenset(),
    ) -> QuickDownloadResult:
        settings = self._settings.current
        releases = await self._collect(request, label, progress)
        ranked = [
            release
            for release in rank_releases(releases, request, settings)
            if release.title.casefold() not in avoid
            and not (release.info_hash and release.info_hash.casefold() in avoid)
        ]
        if not ranked:
            log.info("One-click %s: nothing suitable among %d results", label, len(releases))
            return QuickDownloadResult(found=False)
        unusable: Exception | None = None  # the last pick that failed for its own reasons
        for best in ranked[:MAX_PICKS]:
            quality = best.resolution.value if best.resolution else None
            log.info(
                "One-click %s: picked a %s release with %d seeders from %d results",
                label,
                quality or "unknown-quality",
                best.seeders,
                len(releases),
            )
            if progress is not None:
                progress(t("Grabbing the best one…"))
            try:
                grab = await self._downloads.grab(best, target)
            except ProgramInTorrent:
                continue  # malware dressed up as a video: on to the next best
            except RELEASE_FAILURES as exc:  # no link, turned down...: the next best may do
                log.info("One-click %s: that release can't be grabbed, trying the next", label)
                unusable = exc
                continue
            log.info("One-click %s: %s", label, grab.outcome.value)
            return QuickDownloadResult(
                found=True,
                release_title=best.title,
                quality=quality,
                seeders=best.seeders,
                size_bytes=best.size_bytes or None,  # 0 = the source didn't say
                grab=grab,
            )
        if unusable is not None and request.kind is not SearchKind.SEASON:
            log.warning("One-click %s: none of the picks could be grabbed", label)
            raise unusable
        if unusable is not None:  # a season pack: not found, so its episodes are fetched instead
            log.warning("One-click %s: none of the season packs could be grabbed", label)
            return QuickDownloadResult(found=False)
        log.warning("One-click %s: every pick held a program, none started", label)
        return QuickDownloadResult(found=False)

    async def _collect(
        self, request: SearchRequest, label: str, progress: Progress | None
    ) -> tuple[TorrentRelease, ...]:
        """The shown releases of the last search update that arrived within the timeout."""
        releases: tuple[TorrentRelease, ...] = ()
        loop = asyncio.get_running_loop()
        started = loop.time()
        settings = self._settings.current
        deadline = asyncio.timeout(self._timeout)
        pickable = False
        try:
            async with deadline:
                async with contextlib.aclosing(self._search.search(request)) as updates:
                    async for update in updates:
                        if update.done and _all_resting(update.statuses):
                            log.info("One-click %s: every source is resting after failing", label)
                            raise SourcesResting(t(SOURCES_RESTING))
                        releases = update.releases
                        if not pickable and rank_releases(releases, request, settings):
                            # Something good enough is in: the others get a little longer.
                            pickable = True
                            end = loop.time() + self._grace
                            when = deadline.when()
                            if when is None or end < when:
                                deadline.reschedule(end)
                        if progress is not None:
                            waiting = sum(
                                status.state is IndexerState.SEARCHING
                                for status in update.statuses
                            )
                            still = (
                                tn(
                                    ", {n} source still searching",
                                    ", {n} sources still searching",
                                    waiting,
                                )
                                if waiting
                                else ""
                            )
                            progress(t("Searching… {n} found", n=len(releases)) + still)
        except TimeoutError:
            if not deadline.expired():
                raise
            log.info(
                "One-click %s: stopped waiting for the sources after %.0f s",
                label,
                loop.time() - started,
            )
        return releases


def _all_resting(statuses: Iterable[IndexerStatus]) -> bool:
    """No source searched, and at least one sat out only because it rests after failing."""
    items = tuple(statuses)
    return any(s.resting for s in items) and all(s.state is IndexerState.SKIPPED for s in items)
