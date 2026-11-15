"""Download new episodes automatically, for the shows whose switch is on, on everyone's lists
(whoever is selected: an episode is fetched once when any person follows its show with the
switch on, from the earliest moment one of them switched it on; app/services/profiles.py).

A run (at startup, then every 30 minutes; one at a time) first gives up on the episodes tried
at least once that were not found within GIVE_UP_AFTER of airing, then fetches the due ones
through the one-click Download (`QuickDownloadService`), at most MAX_PER_RUN: least-tried first
(so episodes that keep coming back empty can't crowd newer ones out), then oldest first. An
episode is due once it aired Settings' `auto_wait_hours` ago (releases need time to appear; the
first ones are more often fakes), after its show's switch went on, and while it is neither
downloaded nor given up; one never tried stays due past GIVE_UP_AFTER (the PC was off) until it
has had its try. Nothing found: the try is counted and the next run tries again. Downloaded
meanwhile by hand: skipped, not counted. A release that can't be grabbed (`ReleaseError`: no
link, no .torrent and nowhere to send its magnet; or `ReleaseRefused`: the remote program
turned this one down) counts as a try, is logged (no AutoError: those are kept for what stops a
run) and the run moves on; but when the program turned down every grab of a run (at least 2,
nothing else tried: its save folder gone, say) none is counted, their last tries are stamped
and one AutoError names the last reason. A failed grab of the program or folder (no torrent program,
unwritable folder, the remote torrent program unreachable or failing: RemoteError or any other
GrabError, also every source resting) is not the episode's fault: no try is counted, only its
last try stamped (so the next run starts with the others), it is reported as one AutoError and
ends the run. At most MAX_BACKLOG_PER_RUN of a run's places go to episodes past the window
never tried.

Season packs (item 22): when PACK_MIN_EPISODES or more due episodes belong to one season that
is over (`pack_size`: all its episodes aired, or the show ended or a later season began), the
run asks for that season's pack first, where its first due episode stands, as one of the
MAX_PER_RUN places. Grabbed: those episodes count as downloaded (one AutoDownloaded, "Lost
S02"). Nothing found, downloaded before, or no release of it grabbable: no try is counted for
the pack, and its episodes are searched one by one in the same run, as far as places are left.
A pack turned down by the program counts among the run's refusals (above); the program or
folder failing stamps its episodes' last tries and ends the run. A pack holds only what had
aired when it was grabbed (the download history), so an episode it didn't bring is fetched on
its own later.

Nothing is fetched twice: an episode grabbed before (the download history, also its season
pack) isn't due, nor, while Plex is connected, one in Plex (app/services/plex.py; the SQL of
LibraryRepository.auto_due leaves both out), and a film in Plex is skipped (its switch stays as
it is). None of it is shown: without Plex AirGrabber marks nothing as had.

Then, unless a grab failed, the watchlist films whose "Download when it's out" switch is on
(on anyone's list, each film once):
their release dates are refreshed from TMDB when stale, and the films that are out and not
searched within the last few hours (`app.services.watchlist.is_due`) are fetched the same way,
at most MAX_MOVIES_PER_RUN. Nothing found: the try is counted, and a film searched for long
enough is given up (its switch goes off). A downloaded film leaves the watchlist. A release
that can't be grabbed counts as a try, and a failed grab ends the run, as above. A film found downloaded already (from before it was put on the list)
has its switch turned off instead of being searched again and again.

Log hygiene: only episode labels ("Severance S02E04"), film titles and outcomes are logged,
never a release or a URL.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Sequence
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
    Episode,
    GrabTarget,
    MediaType,
    MovieDetails,
    QuickDownloadResult,
    Show,
    WatchlistEntry,
)
from app.services.downloads import RELEASE_FAILURES, GrabError
from app.services.quick_download import QuickDownloadService
from app.services.remote import ReleaseRefused, RemoteError
from app.services.search import episode_request, movie_request, season_request
from app.services.settings import SettingsService
from app.services.watchlist import GIVE_UP_AFTER as MOVIE_GIVE_UP_AFTER
from app.services.watchlist import WatchlistService

log = logging.getLogger(__name__)

AutoListener = Callable[[AutoEvent], None]

DUE_DELAY = timedelta(hours=2)  # the default wait after airing (Settings' `auto_wait_hours`)
GIVE_UP_AFTER = timedelta(days=3)  # stop looking this long after airing
MAX_PER_RUN = 5  # episodes (or season packs) fetched per run, one after another
# Of those, at most this many from the backlog: episodes aired before the GIVE_UP_AFTER window
# and never tried (the PC was off); current episodes come first.
MAX_BACKLOG_PER_RUN = 2
# A season over with this many due episodes or more is asked for as its pack first.
PACK_MIN_EPISODES = 3
# Due episodes read per run: the MAX_PER_RUN places, and enough after them to see how many of a
# season are due (a pack takes one place for them all).
DUE_LOOKAHEAD = 50
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

    def paused(self) -> bool:
        """The tray menu's pause is on (Settings' `auto_paused_until` is still to come)."""
        until = self._settings.current.auto_paused_until
        return until is not None and self._clock() < until

    async def run_once(self) -> AutoDownloadReport:
        """Give up on overdue episodes, then fetch the due ones (see the module docstring).

        Runs never overlap: a call made during a run starts once that run has finished.
        Unexpected errors propagate; events already emitted stand.
        """
        if self.paused():
            log.info("Automatic downloads are paused (the tray menu)")
            return AutoDownloadReport()
        async with self._run_lock:
            return await self._run()

    async def _run(self) -> AutoDownloadReport:
        now = self._clock()
        gave_up: list[str] = []
        for entry in await self._library.auto_give_up(aired_before=now - GIVE_UP_AFTER):
            label = episode_label(entry)
            log.info(
                "Auto-download %s: not found within %d days of airing, giving up",
                label,
                GIVE_UP_AFTER.days,
            )
            gave_up.append(label)
            self._emit(AutoGaveUp(label=label))

        due = await self._library.auto_due(
            aired_from=now - GIVE_UP_AFTER,
            aired_until=now - timedelta(hours=self._settings.current.auto_wait_hours),
            limit=DUE_LOOKAHEAD,
            backlog=MAX_BACKLOG_PER_RUN,
        )
        packs = await self._packs(due, now)
        run = _EpisodeRun()
        places = 0  # of MAX_PER_RUN: an episode or a season pack each
        asked: set[_SeasonKey] = set()  # the seasons whose pack this run asked for
        covered: set[int] = set()  # episodes a pack grabbed this run holds
        for entry in due:
            if run.error is not None or places >= MAX_PER_RUN:
                break
            if entry.episode.number is None or entry.episode.id in covered:
                continue  # (specials are never due; keeps mypy sure)
            season_key = (entry.show.id, entry.episode.season)
            pack = packs.get(season_key)
            if pack is not None and season_key not in asked:
                asked.add(season_key)
                places += 1
                if await self._grab_pack(run, entry.show, entry.episode.season, *pack):
                    covered.update(member.episode.id for member in pack[0])
                    continue
                if run.error is not None or places >= MAX_PER_RUN:
                    break  # no pack: its episodes one by one, as far as places are left
            places += 1
            await self._grab_episode(run, entry)

        refused = run.refused
        not_found = run.not_found
        error = run.error
        downloaded = run.downloaded
        if len(refused) >= 2 and len(refused) == run.attempted:
            # The program turned down every release it was sent ("Fails." with its save folder
            # gone, say): its fault, not the episodes'. Their last tries are stamped, none
            # counted (nothing is given up for it), and it is reported once.
            for episode_ids, _, _ in refused:
                for episode_id in episode_ids:
                    await self._library.stamp_auto_attempt(episode_id, self._clock())
            refused_labels = {label for _, label, _ in refused}
            not_found = [label for label in not_found if label not in refused_labels]
            error = refused[-1][2]  # user-facing text, free of URLs and keys
            log.warning(
                "Auto-download: the torrent program turned down all %d grabs: %s",
                len(refused),
                error,
            )
            self._emit(AutoError(message=error))
        else:
            for episode_ids, _, _ in refused:  # beside other outcomes: these releases' fault
                for episode_id in episode_ids:
                    await self._library.record_auto_attempt(episode_id, self._clock())

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
                "Auto-download run: %d tried, %d downloaded, %d not found, %d given up%s",
                places,
                len(downloaded),
                len(not_found),
                len(gave_up),
                ", stopped by a failed grab" if error is not None else "",
            )
        return report

    async def _packs(
        self, due: Sequence[CalendarEntry], now: datetime
    ) -> dict[_SeasonKey, tuple[list[CalendarEntry], int]]:
        """The seasons whose pack the run asks for first: PACK_MIN_EPISODES or more of their
        episodes due, and the season over (`pack_size`); each with its due episodes and how
        many of its episodes have aired."""
        groups: dict[_SeasonKey, list[CalendarEntry]] = {}
        for entry in due:
            if entry.episode.number is not None:
                groups.setdefault((entry.show.id, entry.episode.season), []).append(entry)
        packs: dict[_SeasonKey, tuple[list[CalendarEntry], int]] = {}
        for (show_id, season), group in groups.items():
            if len(group) < PACK_MIN_EPISODES:
                continue
            size = pack_size(group[0].show, await self._library.episodes(show_id), season, now)
            if size is not None:
                packs[(show_id, season)] = (group, size)
        return packs

    async def _grab_pack(
        self, run: _EpisodeRun, show: Show, season: int, group: list[CalendarEntry], size: int
    ) -> bool:
        """Ask for the season's pack instead of its due episodes in `group`; True when it was
        grabbed (they count as downloaded). Nothing found, downloaded before, or no release of
        it grabbable: False, and the episodes are searched one by one. The program or folder
        failing: False with `run.error` set (their last tries stamped), as for an episode.

        Only a refused pack stays among the run's grabs (`run.attempted`): one that falls back
        to its episodes otherwise is taken back, so "every grab turned down by the program"
        still holds when they all are."""
        label = f"{show.name} S{season:02d}"
        request = season_request(show, season, episodes=size)
        target = GrabTarget(media_type=MediaType.TV, media_id=show.id, season=season)
        log.info(
            "Auto-download %s: %d episodes due, searching for the season pack", label, len(group)
        )
        run.attempted += 1
        try:
            result = await self._quick_download.download(request, target)
        except ReleaseRefused as exc:  # its episodes' own tries count, one by one
            log.warning("Auto-download %s: the torrent program turned it down: %s", label, exc)
            run.refused.append(((), label, str(exc)))
            return False
        except RELEASE_FAILURES as exc:
            run.attempted -= 1
            log.warning(
                "Auto-download %s: no pack could be grabbed, trying the episodes: %s", label, exc
            )
            return False
        except (GrabError, RemoteError) as exc:
            for member in group:
                await self._library.stamp_auto_attempt(member.episode.id, self._clock())
            self._stop(run, label, str(exc))
            return False
        if result.already_downloaded:  # an earlier pack, before these episodes aired
            run.attempted -= 1
            log.info("Auto-download %s: pack downloaded before, trying the episodes", label)
            return False
        if result.grab is None:
            run.attempted -= 1
            log.info("Auto-download %s: no season pack found, trying the episodes", label)
            return False
        event = AutoDownloaded(label=label, quality=result.quality)
        log.info("Auto-download %s: downloaded (%s)", label, event.quality or "unknown quality")
        run.downloaded.append(event)
        self._emit(event)
        return True

    async def _grab_episode(self, run: _EpisodeRun, entry: CalendarEntry) -> None:
        """Fetch one due episode (see the module docstring)."""
        label = episode_label(entry)
        show, episode = entry.show, entry.episode
        assert episode.number is not None  # the caller skips specials
        request = episode_request(show, episode.season, episode.number)
        target = GrabTarget(
            media_type=MediaType.TV,
            media_id=show.id,
            season=episode.season,
            episode=episode.number,
        )
        log.info("Auto-download %s: searching", label)
        run.attempted += 1
        try:
            result = await self._quick_download.download(request, target)
        except ReleaseRefused as exc:  # its try is counted after the run (see _run)
            log.warning("Auto-download %s: the torrent program turned it down: %s", label, exc)
            run.refused.append(((episode.id,), label, str(exc)))
            run.not_found.append(label)
            return
        except RELEASE_FAILURES as exc:  # this episode's releases: a try, on to the next
            await self._library.record_auto_attempt(episode.id, self._clock())
            # Logged only: an AutoError (shown once an hour) is kept for what stops a run.
            log.warning("Auto-download %s: no release could be grabbed: %s", label, exc)
            run.not_found.append(label)
            return
        except (GrabError, RemoteError) as exc:
            # The torrent program or the folder: not the episode's fault, so no try is
            # counted (never given up for it), and the rest would fail the same way. Its
            # last try is stamped, so the next run starts with the others.
            await self._library.stamp_auto_attempt(episode.id, self._clock())
            self._stop(run, label, str(exc))
            return
        if result.already_downloaded:  # grabbed by hand since this run listed it
            run.attempted -= 1
            log.info("Auto-download %s: already downloaded", label)
            return
        if result.grab is None:
            await self._library.record_auto_attempt(episode.id, self._clock())
            log.info("Auto-download %s: nothing suitable yet, trying again later", label)
            run.not_found.append(label)
            return
        event = AutoDownloaded(label=label, quality=result.quality)
        log.info("Auto-download %s: downloaded (%s)", label, event.quality or "unknown quality")
        run.downloaded.append(event)
        self._emit(event)

    def _stop(self, run: _EpisodeRun, label: str, error: str) -> None:
        """A failed grab of the program or folder ends the run: one AutoError. `error` is
        user-facing text, free of URLs and keys."""
        run.error = error
        log.warning("Auto-download %s: the grab failed, stopping this run: %s", label, error)
        self._emit(AutoError(message=error))

    async def _run_watchlist(self, watchlist: WatchlistService) -> _MovieRun:
        """Fetch the watchlist films that are due (see the module docstring)."""
        run = _MovieRun()
        await watchlist.refresh_release_dates()
        now = self._clock()
        plex = await self._library.plex_films()
        skipped: list[int] = []

        def in_plex(entry: WatchlistEntry) -> bool:
            if plex.has(entry.movie_id, entry.imdb_id):
                skipped.append(entry.movie_id)
                return True
            return False

        due = await watchlist.due(now, MAX_MOVIES_PER_RUN, skip=in_plex)
        if skipped:
            log.info("Auto-download watchlist: %d film(s) in Plex skipped", len(skipped))
        for entry in due:
            movie = entry.movie
            title = movie.title
            request = movie_request(movie, MovieDetails(id=movie.id, imdb_id=entry.imdb_id))
            target = GrabTarget(media_type=MediaType.MOVIE, media_id=movie.id)
            log.info("Auto-download %s: searching", title)
            try:
                result = await self._quick_download.download(request, target)
            except RELEASE_FAILURES as exc:  # the film's releases: a try, on to the next
                log.warning("Auto-download %s: no release could be grabbed: %s", title, exc)
                result = QuickDownloadResult(found=False)
            except (GrabError, RemoteError) as exc:  # the program or folder: no try counted
                run.error = str(exc)  # user-facing text, free of URLs and keys
                log.warning(
                    "Auto-download %s: the grab failed, stopping this run: %s", title, run.error
                )
                self._emit(AutoError(message=run.error))
                break
            if result.already_downloaded:  # in the history from before it was put on the list
                log.info("Auto-download %s: downloaded before, switching it off", title)
                await watchlist.switch_off(movie.id)
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


def pack_size(show: Show, episodes: Iterable[Episode], season: int, now: datetime) -> int | None:
    """How many of `season`'s numbered episodes have aired, when the season is over: every one
    of them has aired, or (one not dated or still to come) the show has ended or a later season
    has begun airing. None while it may still be airing (no pack: it would miss the rest)."""
    listed = [e for e in episodes if e.number is not None and e.season > 0]
    own = [e for e in listed if e.season == season]
    aired = sum(e.has_aired(now) for e in own)
    if not own or not aired:
        return None
    if aired == len(own) or show.is_ended:
        return aired
    if any(e.season > season and e.has_aired(now) for e in listed):
        return aired
    return None


_SeasonKey = tuple[int, int]  # (TVMaze show id, season)


@dataclass(slots=True)
class _EpisodeRun:
    """What the episodes part of a run did."""

    downloaded: list[AutoDownloaded] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)
    error: str | None = None  # the AutoError that stopped the run
    attempted: int = 0  # grabs tried (searched, not found downloaded meanwhile)
    # (episode ids, label, reason) of each ReleaseRefused; a pack's has no episodes of its own.
    refused: list[tuple[tuple[int, ...], str, str]] = field(default_factory=list)


@dataclass(slots=True)
class _MovieRun:
    """What the watchlist part of a run did."""

    downloaded: list[AutoMovieDownloaded] = field(default_factory=list)
    not_found: list[str] = field(default_factory=list)
    gave_up: list[str] = field(default_factory=list)
    error: str | None = None
