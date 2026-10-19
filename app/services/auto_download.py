"""Download new episodes automatically, for the tracked shows whose switch is on.

A run (at startup, then every 30 minutes; one at a time) first gives up on the episodes that
were not found within GIVE_UP_AFTER of airing, then fetches the due ones through the one-click
Download (`QuickDownloadService`), at most MAX_PER_RUN: least-tried first (so episodes that keep
coming back empty can't crowd newer ones out), then oldest first. An episode is due once it
aired DUE_DELAY ago (releases need time to appear), after its show's switch went on, and while
it is neither downloaded nor given up. Nothing found: the try is counted and the next run tries
again. Downloaded meanwhile by hand: skipped, not counted. A failed grab (no torrent program, unwritable folder, the remote torrent program refusing) is
counted too, reported as one AutoError and ends the run.

Then, unless a grab failed, the watchlist films whose "Download when it's out" switch is on:
their release dates are refreshed from TMDB when stale, and the films that are out and not
searched within the last few hours (`app.services.watchlist.is_due`) are fetched the same way,
at most MAX_MOVIES_PER_RUN. Nothing found: the try is counted, and a film searched for long
enough is given up (its switch goes off). A downloaded film leaves the watchlist. A failed grab
ends the run as above. A film found downloaded already (from before it was put on the list)
has its switch turned off instead of being searched again and again.

Log hygiene: only episode labels ("Severance S02E04"), film titles and outcomes are logged,
never a release or a URL.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.database import LibraryRepository
from app.models import (
    AutoDownloaded,
    AutoDownloadReport,
    AutoError,
    AutoEvent,
    AutoGaveUp,
    AutoMovieDownloaded,
    AutoMovieGaveUp,
    CalendarEntry,
    GrabTarget,
    MediaType,
    MovieDetails,
)
from app.services.downloads import GrabError
from app.services.quick_download import QuickDownloadService
from app.services.remote import RemoteError
from app.services.search import episode_request, movie_request
from app.services.settings import SettingsService
from app.services.watchlist import GIVE_UP_AFTER as MOVIE_GIVE_UP_AFTER
from app.services.watchlist import WatchlistService

log = logging.getLogger(__name__)

AutoListener = Callable[[AutoEvent], None]

DUE_DELAY = timedelta(hours=2)  # wait this long after airing: releases need time to appear
GIVE_UP_AFTER = timedelta(days=3)  # stop looking this long after airing
MAX_PER_RUN = 5  # episodes fetched per run, one after another
MAX_MOVIES_PER_RUN = 5  # watchlist films fetched per run, after the episodes
RUN_INTERVAL_S = 1800.0  # seconds between runs


def _utc_now() -> datetime:
    return datetime.now(UTC)


def episode_label(entry: CalendarEntry) -> str:
    """"Severance S02E04": the episode as events, reports and logs name it."""
    return f"{entry.show.name} {entry.episode.code}"


class AutoDownloadService:
    """Runs the automatic downloads of new episodes and reports what they do to listeners."""

    def __init__(
        self,
        library: LibraryRepository,
        quick_download: QuickDownloadService,
        settings: SettingsService,
        *,
        watchlist: WatchlistService | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """`watchlist`: its films are fetched too. `clock` returns the current UTC instant
        (tests move it)."""
        self._library = library
        self._quick_download = quick_download
        self._watchlist = watchlist
        # Not read yet: the grabs read the download preferences through QuickDownloadService.
        self._settings = settings
        self._clock = clock
        self._listeners: list[AutoListener] = []
        self._run_lock = asyncio.Lock()  # one run at a time; a second caller waits its turn

    def subscribe(self, listener: AutoListener) -> Callable[[], None]:
        """Call `listener` with every event (AutoDownloaded / AutoGaveUp / AutoError) as it
        happens. Returns a function that unsubscribes it."""
        self._listeners.append(listener)
        subscribed = True

        def unsubscribe() -> None:
            nonlocal subscribed
            if subscribed:
                subscribed = False
                self._listeners.remove(listener)

        return unsubscribe

    def _emit(self, event: AutoEvent) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                log.exception("Auto-download listener %r failed", listener)

    async def run_periodic(self, interval_s: float = RUN_INTERVAL_S) -> None:
        """Run now, then every `interval_s` seconds, until cancelled. A failed run is logged and
        the loop carries on."""
        while True:
            try:
                await self.run_once()
            except Exception:
                log.exception("Auto-download run failed")
            await asyncio.sleep(interval_s)

    async def run_once(self) -> AutoDownloadReport:
        """Give up on overdue episodes, then fetch the due ones (see the module docstring).

        Runs never overlap: a call made during a run starts once that run has finished.
        Unexpected errors propagate; events already emitted stand.
        """
        async with self._run_lock:
            return await self._run()

    async def _run(self) -> AutoDownloadReport:
        now = self._clock()
        gave_up: list[str] = []
        for entry in await self._library.auto_give_up(aired_before=now - GIVE_UP_AFTER, when=now):
            label = episode_label(entry)
            log.info(
                "Auto-download %s: not found within %d days of airing, giving up",
                label,
                GIVE_UP_AFTER.days,
            )
            gave_up.append(label)
            self._emit(AutoGaveUp(label=label))

        due = await self._library.auto_due(
            aired_from=now - GIVE_UP_AFTER, aired_until=now - DUE_DELAY, limit=MAX_PER_RUN
        )
        downloaded: list[AutoDownloaded] = []
        not_found: list[str] = []
        error: str | None = None
        for entry in due:
            label = episode_label(entry)
            show, episode = entry.show, entry.episode
            if episode.number is None:  # never due (the query skips specials); keeps mypy sure
                continue
            request = episode_request(show, episode.season, episode.number)
            target = GrabTarget(
                media_type=MediaType.TV,
                media_id=show.id,
                season=episode.season,
                episode=episode.number,
            )
            log.info("Auto-download %s: searching", label)
            try:
                result = await self._quick_download.download(request, target)
            except (GrabError, RemoteError) as exc:
                await self._library.record_auto_attempt(episode.id, self._clock())
                error = str(exc)  # user-facing text, free of URLs and keys
                log.warning(
                    "Auto-download %s: the grab failed, stopping this run: %s", label, error
                )
                self._emit(AutoError(message=error))
                break
            if result.already_downloaded:  # grabbed by hand since this run listed it
                log.info("Auto-download %s: already downloaded", label)
                continue
            if result.grab is None:
                await self._library.record_auto_attempt(episode.id, self._clock())
                log.info("Auto-download %s: nothing suitable yet, trying again later", label)
                not_found.append(label)
                continue
            event = AutoDownloaded(label=label, quality=result.quality)
            log.info("Auto-download %s: downloaded (%s)", label, event.quality or "unknown quality")
            downloaded.append(event)
            self._emit(event)

        movies = _MovieRun()
        if error is None and self._watchlist is not None:
            movies = await self._run_watchlist(self._watchlist)
            error = movies.error

        report = AutoDownloadReport(
            downloaded=tuple(downloaded),
            not_found=tuple(not_found),
            gave_up=tuple(gave_up),
            error=error,
            movies_downloaded=tuple(movies.downloaded),
            movies_not_found=tuple(movies.not_found),
            movies_gave_up=tuple(movies.gave_up),
        )
        if due or gave_up:
            log.info(
                "Auto-download run: %d due, %d downloaded, %d not found, %d given up%s",
                len(due),
                len(downloaded),
                len(not_found),
                len(gave_up),
                ", stopped by a failed grab" if error is not None else "",
            )
        return report

    async def _run_watchlist(self, watchlist: WatchlistService) -> _MovieRun:
        """Fetch the watchlist films that are due (see the module docstring)."""
        run = _MovieRun()
        await watchlist.refresh_release_dates()
        now = self._clock()
        due = watchlist.due(now, MAX_MOVIES_PER_RUN)
        for entry in due:
            movie = entry.movie
            title = movie.title
            request = movie_request(movie, MovieDetails(id=movie.id, imdb_id=entry.imdb_id))
            target = GrabTarget(media_type=MediaType.MOVIE, media_id=movie.id)
            log.info("Auto-download %s: searching", title)
            try:
                result = await self._quick_download.download(request, target)
            except (GrabError, RemoteError) as exc:
                await watchlist.record_try(movie.id, when=self._clock())
                run.error = str(exc)  # user-facing text, free of URLs and keys
                log.warning(
                    "Auto-download %s: the grab failed, stopping this run: %s", title, run.error
                )
                self._emit(AutoError(message=run.error))
                break
            if result.already_downloaded:  # in the history from before it was put on the list
                log.info("Auto-download %s: downloaded before, switching it off", title)
                await watchlist.set_auto_download(movie.id, False)
                continue
            if result.grab is None:
                if await watchlist.record_try(movie.id, when=self._clock()):
                    log.info(
                        "Auto-download %s: not found within %d days, giving up",
                        title,
                        MOVIE_GIVE_UP_AFTER.days,
                    )
                    run.gave_up.append(title)
                    self._emit(AutoMovieGaveUp(title=title))
                else:
                    log.info("Auto-download %s: nothing suitable yet, trying again later", title)
                    run.not_found.append(title)
                continue
            event = AutoMovieDownloaded(title=title, quality=result.quality)
            log.info("Auto-download %s: downloaded (%s)", title, event.quality or "unknown quality")
            run.downloaded.append(event)
            self._emit(event)
        if due:
            log.info(
                "Auto-download watchlist run: %d due, %d downloaded, %d not found, %d given up%s",
                len(due),
                len(run.downloaded),
                len(run.not_found),
                len(run.gave_up),
                ", stopped by a failed grab" if run.error is not None else "",
            )
        return run


@dataclass(slots=True)
class _MovieRun:
    """What the watchlist part of a run did."""

    downloaded: list[AutoMovieDownloaded] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)
    gave_up: list[str] = field(default_factory=list)
    error: str | None = None
