"""Tracked-library use cases.

Reads come straight from SQLite. Tracking, search, import, calendar suggestions and the
incremental background sync go through TVMaze and cache what they fetch.

Each person follows their own shows (app/services/profiles.py): every call about what someone
follows names them (`profile_id`), read by the caller when it started, so a call that finishes
after a switch still belongs to the person it was made for. The shows' cache, their episodes
and the downloads are shared; the sync refreshes every show anyone follows.

What you have and miss (owner's decision for 3.1.0): only while Plex is connected
(`plex_connected`, app/services/plex.py) does anything count as had, and then only what is in
Plex; missing is what aired and is neither in Plex nor grabbed by AirGrabber (app.services.
missing). Without Plex the calls about it answer "nothing" (no missing, no new, nothing in
Plex): the pages show no have / missing cues at all. The download history is still read where
it keeps an episode from being fetched twice, but is never shown as "have it".
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, NamedTuple, ParamSpec

from app.database import HistoryRepository, LibraryRepository
from app.models import (
    CalendarEntry,
    Episode,
    ImportMatch,
    ImportResult,
    LibraryEntry,
    LibraryStats,
    Show,
    ShowActivity,
    ShowDetails,
    ShowSearchResult,
    SyncProgress,
    SyncReport,
    Weekday,
)
from app.models.plex import PlexFilms
from app.services.missing import latest_season, missing_cutoffs, missing_episodes, new_episodes
from app.services.plex import plex_connected
from app.services.settings import SettingsService
from app.services.tvmaze import TVMazeClient, TVMazeError, TVMazeNotFound

log = logging.getLogger(__name__)

P = ParamSpec("P")

LibraryListener = Callable[[], None]

SCHEDULE_COUNTRY = "US"
# Schedules from yesterday on can still change (new listings, reschedules); older days are final.
SCHEDULE_TTL = timedelta(hours=12)
# Shows synced longer ago than this may have changed before TVMaze's "month" updates feed starts.
UPDATES_WINDOW = timedelta(days=28)
SYNC_CONCURRENCY = 3
# After each sync, untracked shows' schedule episodes older than this are dropped.
SCHEDULE_RETENTION = timedelta(days=60)
# Untracked shows opened in the details window are fetched from TVMaze and never stored: they are
# kept in memory this long (at most DETAILS_CACHE_SIZE of them), so reopening one, or the window
# reloading after a library change, needs no new request.
DETAILS_TTL = timedelta(minutes=15)
DETAILS_CACHE_SIZE = 20
# The calendar always shows this many days before today (on a Monday, the weekend's episodes).
RECENT_DAYS = 3


def week_start(day: date, first_day: Weekday) -> date:
    """The first day of the week (starting on `first_day`) that contains `day`."""
    return day - timedelta(days=(day.weekday() - first_day.weekday) % 7)


def calendar_range(
    today: date, *, first_day: Weekday, weeks: int, offset: int = 0
) -> tuple[date, date]:
    """First and last day of the calendar grid: whole weeks.

    The grid shows `weeks` weeks from the week containing `today` moved by `offset` weeks
    (earlier weeks when negative). At the current position (offset 0) it starts a week earlier
    while today is fewer than RECENT_DAYS days into its week, so the last RECENT_DAYS days are
    always on it and the chosen number of weeks still lies ahead (`weeks` + 1 weeks).
    """
    this_week = week_start(today, first_day)
    start = this_week + timedelta(weeks=offset)
    end = start + timedelta(weeks=weeks, days=-1)
    if offset == 0 and (today - this_week).days < RECENT_DAYS:
        start -= timedelta(weeks=1)
    return start, end


class LibraryService:
    def __init__(
        self,
        library: LibraryRepository,
        history: HistoryRepository,
        tvmaze: TVMazeClient,
        *,
        settings: SettingsService | None = None,
    ) -> None:
        """`settings`: read for `auto_download_new_shows` whenever a show is newly followed
        (without it, new shows start with automatic downloads off), and for whether Plex is
        connected (`plex_connected`; without settings it never is)."""
        self._library = library
        self._history = history
        self._tvmaze = tvmaze
        self._settings = settings
        self._listeners: list[LibraryListener] = []
        # Held while saving-then-tracking a show and while pruning (which deletes untracked
        # shows), so a show can't be pruned between being saved and being tracked.
        self._tracking = asyncio.Lock()
        self._schedule_fetches: dict[date, asyncio.Task[None]] = {}
        self._sync_run: asyncio.Task[SyncReport] | None = None
        self._waiters: Counter[asyncio.Task[Any]] = Counter()
        # Untracked shows fetched for `show_details`, oldest first.
        self._fetched: dict[int, _Fetched] = {}

    def subscribe(self, listener: LibraryListener) -> Callable[[], None]:
        """Call `listener` after every library change. Returns a function that unsubscribes it."""
        self._listeners.append(listener)
        subscribed = True

        def unsubscribe() -> None:
            nonlocal subscribed
            if subscribed:
                subscribed = False
                self._listeners.remove(listener)

        return unsubscribe

    def _notify(self) -> None:
        for listener in list(self._listeners):
            _invoke(listener)

    def notify_changed(self) -> None:
        """Tell the listeners the library changed from elsewhere (a Plex check that changed
        what counts as had, Plex connected or disconnected)."""
        self._notify()

    @property
    def plex_connected(self) -> bool:
        """Plex is connected (app/services/plex.py): only then do the pages show what you
        have and miss (see the module docstring)."""
        settings = self._settings
        return settings is not None and plex_connected(settings.current)

    # -- reads ---------------------------------------------------------------------------------

    async def tracked(self, *, profile_id: int) -> list[LibraryEntry]:
        """The shows a person follows, by name."""
        return await self._library.tracked(profile_id=profile_id)

    async def stats(self, *, profile_id: int) -> LibraryStats:
        return await self._library.stats(profile_id=profile_id)

    async def calendar(self, start: date, end: date, *, profile_id: int) -> list[CalendarEntry]:
        """The episodes of the shows a person follows airing between `start` and `end`."""
        return await self._library.calendar(start, end, profile_id=profile_id)

    async def episodes(self, show_id: int) -> list[Episode]:
        """The stored episodes of a show, by season and number (specials have no number)."""
        return await self._library.episodes(show_id)

    async def plex_films(self) -> PlexFilms:
        """The films in Plex (Movies' "Hide what I already have", the cards' ✓ In Plex,
        the automatic downloads); none while Plex isn't connected."""
        if not self.plex_connected:
            return PlexFilms()
        return await self._library.plex_films()

    async def film_downloaded(self, movie_id: int) -> bool:
        """Whether a film (TMDB id) is in the download history: its window offers its
        subtitles (CC ▾). Never shown as "have it"."""
        return await self._history.has_movie(movie_id)

    async def activity(
        self, *, profile_id: int, now: datetime | None = None
    ) -> dict[int, ShowActivity]:
        """Per id of a show the person follows: its next episode (see ShowActivity). Shows
        without one are left out. `now`: an aware instant (tests); its time zone decides which
        day "today" is."""
        now = now if now is not None else datetime.now().astimezone()
        upcoming = await self._library.next_episodes(
            profile_id=profile_id, now=now, today=now.date()
        )
        following = {episode.show_id: episode for episode in upcoming}
        tracked = await self._library.tracked_ids(profile_id=profile_id)
        return {
            show_id: ShowActivity(show_id=show_id, next_episode=following[show_id])
            for show_id in sorted(following.keys() & tracked)
        }

    async def last_downloads(self) -> dict[int, datetime]:
        """Per show id: when its latest episode or season pack was downloaded, however long ago
        (My shows' "Recently downloaded" order). Shows never downloaded are left out."""
        grabs = await self._history.latest_tv_grabs(datetime(1970, 1, 1, tzinfo=UTC))
        return {record.media_id: record.downloaded_at for record in grabs}

    async def show_details(
        self, show_id: int, *, profile_id: int, now: datetime | None = None
    ) -> ShowDetails:
        """A TVMaze show with its episodes, whether the person follows it, downloads and next
        episode, and while Plex is connected what of it is in Plex and missing (see
        ShowDetails), for the show details window.

        A show the person follows comes from the library's cache. Any other show (or a followed
        one whose cache holds no episodes yet) is fetched from TVMaze and kept in memory for
        DETAILS_TTL; nothing is written to the database. TVMaze errors propagate. `now`: an
        aware instant (tests); its time zone decides which day "today" is.
        """
        now = now if now is not None else datetime.now().astimezone()
        entry = await self._library.tracked_entry(show_id, profile_id=profile_id)
        episodes = await self._library.episodes(show_id) if entry is not None else []
        if entry is not None and episodes:
            show = entry.show
        else:
            show, episodes = await self._fetch_show(show_id)
        grabs = await self._history.show_grabs(show.id)
        downloaded = downloaded_ids(episodes, grabs)
        tracked = entry is not None
        plex = self.plex_connected
        marked = tracked and plex  # what you have and miss shows only with Plex
        in_plex = await self._library.plex_ids(show.id) if marked else frozenset[int]()
        missing = (
            missing_episodes(episodes, downloaded=downloaded, in_plex=in_plex, now=now)
            if marked
            else []
        )
        new = (
            new_episodes(episodes, downloaded=downloaded, in_plex=in_plex, now=now)
            if marked
            else []
        )
        return ShowDetails(
            show=show,
            episodes=tuple(episodes),
            tracked=tracked,
            auto_download=entry is not None and entry.auto_download,
            downloaded=downloaded,
            next_episode=next_episode(episodes, now),
            plex_connected=plex,
            latest_season=latest_season(episodes, now) if marked else None,
            missing=frozenset(episode.id for episode in missing),
            new=frozenset(episode.id for episode in new),
            in_plex=in_plex,
        )

    async def missing_episodes(
        self, show_id: int, *, profile_id: int, now: datetime | None = None
    ) -> list[CalendarEntry]:
        """The missing episodes (app.services.missing) of a show the person follows, oldest
        first, from the library alone; [] for a show they don't follow, and without Plex.
        `now`: an aware instant (tests)."""
        if not self.plex_connected:
            return []
        now = now if now is not None else datetime.now().astimezone()
        entry = await self._library.tracked_entry(show_id, profile_id=profile_id)
        if entry is None:
            return []
        episodes = await self._library.episodes(show_id)
        downloaded = downloaded_ids(episodes, await self._history.show_grabs(show_id))
        in_plex = await self._library.plex_ids(show_id)
        return [
            CalendarEntry(show=entry.show, episode=episode, is_tracked=True)
            for episode in missing_episodes(
                episodes, downloaded=downloaded, in_plex=in_plex, now=now
            )
        ]

    async def missing_counts(
        self, *, profile_id: int, now: datetime | None = None
    ) -> dict[int, int]:
        """Per id of a show the person follows, how many of its latest season's episodes are
        missing (one query for every show); shows with none are left out, and every show
        without Plex. `now`: an aware instant (tests); its time zone decides which day "today"
        is."""
        if not self.plex_connected:
            return {}
        now = now if now is not None else datetime.now().astimezone()
        missing_by, missing_day = missing_cutoffs(now)
        return await self._library.missing_counts(
            profile_id=profile_id,
            now=now,
            today=now.date(),
            missing_by=missing_by,
            missing_day=missing_day,
        )

    async def missing_between(
        self, start: date, end: date, *, profile_id: int, now: datetime | None = None
    ) -> frozenset[int]:
        """Ids of the missing episodes (app.services.missing's rule) airing between `start`
        and `end`, inclusive: the calendar's amber "Not downloaded yet" marks; none without
        Plex. `now`: an aware instant (tests); its time zone decides which day "today" is."""
        if not self.plex_connected:
            return frozenset()
        now = now if now is not None else datetime.now().astimezone()
        missing_by, missing_day = missing_cutoffs(now)
        return await self._library.missing_between(
            start,
            end,
            profile_id=profile_id,
            now=now,
            today=now.date(),
            missing_by=missing_by,
            missing_day=missing_day,
        )

    async def new_between(
        self, start: date, end: date, *, profile_id: int, now: datetime | None = None
    ) -> frozenset[int]:
        """Ids of the new episodes (aired less than a day ago, neither in Plex nor downloaded;
        see app.services.missing) airing between `start` and `end`, inclusive: the calendar's
        blue "New" notes; none without Plex. `now`: an aware instant (tests)."""
        if not self.plex_connected:
            return frozenset()
        now = now if now is not None else datetime.now().astimezone()
        missing_by, missing_day = missing_cutoffs(now)
        return await self._library.new_between(
            start,
            end,
            profile_id=profile_id,
            now=now,
            today=now.date(),
            missing_by=missing_by,
            missing_day=missing_day,
        )

    async def plex_between(self, start: date, end: date) -> frozenset[int]:
        """Ids of the episodes airing between `start` and `end` (inclusive) that are in Plex,
        the only ones that count as had (a calendar stack card marks them "in Plex"); none
        without Plex."""
        if not self.plex_connected:
            return frozenset()
        return await self._library.plex_between(start, end)

    async def _fetch_show(self, show_id: int) -> tuple[Show, list[Episode]]:
        """The show and its episodes from TVMaze, or from memory when fetched recently."""
        fetched = self._fetched.get(show_id)
        stored_at = datetime.now(UTC)
        if fetched is not None and timedelta(0) <= stored_at - fetched.stored_at < DETAILS_TTL:
            return fetched.show, list(fetched.episodes)
        show, episodes = await self._tvmaze.get_show_with_episodes(show_id)
        episodes.sort(key=_episode_order)  # as the library lists them
        self._fetched.pop(show_id, None)  # re-inserted last: the newest
        self._fetched[show_id] = _Fetched(show, tuple(episodes), stored_at)
        while len(self._fetched) > DETAILS_CACHE_SIZE:
            del self._fetched[next(iter(self._fetched))]
        return show, episodes

    async def search(
        self, query: str, *, profile_id: int, limit: int = 12
    ) -> list[ShowSearchResult]:
        """TVMaze shows matching `query`, best match first, flagged when the person follows
        them already."""
        shows = await self._tvmaze.search_shows(query, limit=limit)
        tracked = (
            await self._library.tracked_ids(profile_id=profile_id) if shows else frozenset[int]()
        )
        return [ShowSearchResult(show=show, is_tracked=show.id in tracked) for show in shows]

    # -- library changes -----------------------------------------------------------------------

    async def untrack(self, show_id: int, *, profile_id: int) -> None:
        """Stop following a show (the person's own follow only: the show's cache, episodes and
        marks stay while anyone follows it)."""
        await self._library.untrack(show_id, profile_id=profile_id)
        self._notify()

    async def set_auto_download(self, show_id: int, on: bool, *, profile_id: int) -> None:
        """Switch a person's automatic downloads of new episodes of a show they follow on (from
        now) or off."""
        await self._library.set_auto_download(show_id, on, profile_id=profile_id)
        log.info(
            "Automatic downloads %s for TVMaze show %d (person %d)",
            "on" if on else "off", show_id, profile_id,
        )
        self._notify()

    async def remove_ended(self, *, profile_id: int) -> int:
        """Stop following every ended show the person follows (others keep theirs)."""
        removed = await self._library.untrack_ended(profile_id=profile_id)
        if removed:
            self._notify()
        return removed

    async def track(self, show_id: int, *, profile_id: int) -> Show:
        """Fetch a show with all its episodes from TVMaze and add it to a person's library.

        A newly followed show starts with automatic downloads on when the
        `auto_download_new_shows` setting says so. Tracking a show the person follows already
        refreshes it (and keeps their switch). TVMaze errors propagate.
        """
        show = await self._fetch_and_track(show_id, profile_id=profile_id)
        self._notify()
        return show

    async def import_shows(
        self,
        entries: Sequence[str],
        progress: Callable[[int, int], None] | None = None,
        *,
        profile_id: int,
    ) -> ImportResult:
        """Add the shows listed in `entries` to a person's library: TVMaze ids, or names (the
        best search hit wins).

        Entries are handled one by one to stay within TVMaze's rate limit; `progress(done, total)`
        follows each. Listeners are notified once, at the end, if anything was added.
        """
        pending = list(dict.fromkeys(entry.strip() for entry in entries if entry.strip()))
        tracked = set(await self._library.tracked_ids(profile_id=profile_id))
        added: list[str] = []
        already_tracked: list[str] = []
        not_found: list[str] = []
        failed: list[str] = []
        try:
            for done, entry in enumerate(pending, start=1):
                try:
                    outcome = await self._import_entry(entry, tracked, profile_id=profile_id)
                except TVMazeNotFound:
                    not_found.append(entry)
                except TVMazeError as exc:
                    log.warning("Import of %r failed: %s", entry, exc)
                    failed.append(entry)
                else:
                    if outcome is None:
                        not_found.append(entry)
                    else:
                        name, is_new = outcome
                        (added if is_new else already_tracked).append(name)
                if progress is not None:
                    _invoke(progress, done, len(pending))
        finally:
            if added:
                self._notify()
        return ImportResult(
            added=tuple(added),
            already_tracked=tuple(already_tracked),
            not_found=tuple(not_found),
            failed=tuple(failed),
        )

    async def _import_entry(
        self, entry: str, tracked: set[int], *, profile_id: int
    ) -> tuple[str, bool] | None:
        """Resolve and track one import line.

        Returns (display name, newly added), or None when nothing matched. Adds newly tracked
        ids to `tracked`, so later entries naming the same show count as already tracked.
        """
        if entry.isascii() and entry.isdigit():
            show_id = int(entry)
            if show_id in tracked:
                known = await self._library.get_show(show_id)
                return (known.name if known else entry), False
        else:
            hit = await self._best_hit(entry)
            if hit is None:
                return None
            show_id = hit.id
            if show_id in tracked:
                return hit.name, False
        show = await self._fetch_and_track(show_id, profile_id=profile_id)
        tracked.add(show.id)
        return show.name, True

    async def find_matches(
        self,
        names: Sequence[str],
        progress: Callable[[int, int], None] | None = None,
        *,
        profile_id: int,
    ) -> tuple[ImportMatch, ...]:
        """Match show names (from another app) to TVMaze shows without tracking anything
        (`already_tracked`: the person follows the show).

        Uses the same best-hit rule as `import_shows`. Blank and repeated names are dropped;
        the rest are looked up one by one (TVMaze's rate limit), `progress(done, total)` after
        each. A failed lookup gives a match with `failed` set instead of raising.
        """
        pending = list(dict.fromkeys(name.strip() for name in names if name.strip()))
        tracked = (
            await self._library.tracked_ids(profile_id=profile_id) if pending else frozenset[int]()
        )
        matches: list[ImportMatch] = []
        for done, name in enumerate(pending, start=1):
            try:
                show = await self._best_hit(name)
            except TVMazeNotFound:
                matches.append(ImportMatch(source_name=name))
            except TVMazeError as exc:
                log.warning("Matching a show name on TVMaze failed: %s", exc)
                matches.append(ImportMatch(source_name=name, failed=True))
            else:
                matches.append(_match(name, show, tracked))
            if progress is not None:
                _invoke(progress, done, len(pending))
        found = sum(match.found for match in matches)
        log.info(
            "Matched %d of %d show names on TVMaze (%d failed)",
            found, len(matches), sum(match.failed for match in matches),
        )
        return tuple(matches)

    async def _best_hit(self, name: str) -> Show | None:
        """The TVMaze show a name stands for: the best search hit, or None.

        The name is searched normalised (`search_name`: Unicode dashes, minus signs and curly
        quotes as their ASCII forms), then as `search_queries` says until one finds something:
        a trailing "(YYYY)" or "(US)" is searched without it first; a bare year or country at
        the end ("MacGyver 2016", "Shameless US") after the whole name; letters written apart
        ("S H I E L D") also as "S.H.I.E.L.D." and "SHIELD". With a year or country, the first
        hit premiering in that year / from that country wins (a hit named exactly like the
        whole name wins at once: the year is part of "Space: 1999"); without one, the first
        query's best hit does. A bracketed name stops at the first query with hits, as before.
        """
        name = search_name(name)
        hint = name_hint(name)
        fallback: Show | None = None
        for query in search_queries(name, hint):
            candidates = await self._tvmaze.search_shows_with_country(query)
            if not candidates:
                continue
            if hint is None:
                return candidates[0][0]
            for show, country in candidates:
                if same_name(show.name, name) or hint.fits(show, country):
                    return show
            if fallback is None:
                fallback = candidates[0][0]
            if hint.bracketed:
                break
        return fallback

    async def _fetch_and_track(self, show_id: int, *, profile_id: int) -> Show:
        show, episodes = await self._tvmaze.get_show_with_episodes(show_id)
        settings = self._settings
        auto_download = settings is not None and settings.current.auto_download_new_shows
        async with self._tracking:
            await self._library.save_show(show, episodes)
            await self._library.track(show.id, profile_id=profile_id, auto_download=auto_download)
        await self._library.mark_synced(show.id)
        self._fetched.pop(show.id, None)  # the library's copy is the current one now
        return show

    # -- calendar suggestions ------------------------------------------------------------------

    async def suggestions(
        self, start: date, end: date, *, profile_id: int, per_day: int
    ) -> list[CalendarEntry]:
        """The most popular shows the person doesn't follow airing each day, from TVMaze's
        cached US schedule,
        best first: scripted and animated series, then (filling quiet days such as Saturdays,
        when the networks air hardly any new ones) popular reality, variety and documentary
        shows (`LibraryRepository.suggestions`)."""
        await self._ensure_schedule(start, end)
        return await self._library.suggestions(
            start, end, profile_id=profile_id, per_day=per_day
        )

    async def _ensure_schedule(self, start: date, end: date) -> None:
        """Fetch every day in [start, end] whose cached schedule is missing or may be outdated.

        Days never fetched are fetched. Days from yesterday on are fetched again once their copy
        is older than SCHEDULE_TTL; earlier days are final once fetched. A day TVMaze fails to
        deliver is logged and skipped.
        """
        if end < start:
            return
        fetched = await self._library.schedule_fetches(start, end)
        now, today = datetime.now(UTC), date.today()
        due = [
            day
            for day in _days(start, end)
            if (last := fetched.get(day)) is None
            or (day >= today - timedelta(days=1) and now - last > SCHEDULE_TTL)
        ]
        if due:
            await self._join(*(self._schedule_fetch(day) for day in due))

    def _schedule_fetch(self, day: date) -> asyncio.Task[None]:
        """The fetch of `day`'s schedule already in flight, or a newly started one."""
        task = self._schedule_fetches.get(day)
        if task is None or task.cancelling():
            task = asyncio.create_task(self._fetch_schedule_day(day), name=f"schedule-{day}")
            self._schedule_fetches[day] = task
            task.add_done_callback(functools.partial(self._forget_schedule_fetch, day))
        return task

    def _forget_schedule_fetch(self, day: date, task: asyncio.Task[None]) -> None:
        if self._schedule_fetches.get(day) is task:
            del self._schedule_fetches[day]

    async def _fetch_schedule_day(self, day: date) -> None:
        try:
            items = await self._tvmaze.schedule(day, country=SCHEDULE_COUNTRY)
        except TVMazeError as exc:
            log.warning("Skipping the TVMaze schedule of %s: %s", day, exc)
            return
        async with self._tracking:
            # Followed shows' rows (anyone's) are left to sync: storing their change stamp from
            # here would make sync believe their (unrefreshed) episode lists are current.
            tracked = await self._library.followed_ids()
            shows = {show.id: show for show, _ in items if show.id not in tracked}
            await self._library.save_schedule_day(
                day, shows.values(), [episode for _, episode in items]
            )

    # -- sync ----------------------------------------------------------------------------------

    async def sync(
        self, progress: Callable[[SyncProgress], None] | None = None, *, force: bool = False
    ) -> SyncReport:
        """Refresh the shows anyone follows from TVMaze, refetching only those that may have
        changed.

        A show is refetched when `force` is set, when it was never synced or not within
        UPDATES_WINDOW, or when TVMaze's updates feed stamps it newer than the stored copy
        (TVMaze bumps a show's stamp whenever one of its episodes is created, changed or
        deleted). Only one sync runs at a time: a call made during a run waits for that run
        and returns its report.
        """
        run = self._sync_run
        if run is None or run.done() or run.cancelling():
            run = asyncio.create_task(self._run_sync(progress, force=force), name="library-sync")
            self._sync_run = run
            run.add_done_callback(self._forget_sync_run)
        await self._join(run)
        return run.result()

    def _forget_sync_run(self, run: asyncio.Task[SyncReport]) -> None:
        if self._sync_run is run:
            self._sync_run = None

    async def _run_sync(
        self, progress: Callable[[SyncProgress], None] | None, *, force: bool
    ) -> SyncReport:
        started_at = datetime.now(UTC)
        entries = await self._library.followed()
        stale = await self._stale_shows(entries, force=force, now=started_at)
        await self._library.mark_synced_many(
            entry.show.id for entry in entries if entry.show.id not in stale
        )

        to_refresh = [entry.show for entry in entries if entry.show.id in stale]
        refreshed = failed = 0
        slots = asyncio.Semaphore(SYNC_CONCURRENCY)

        async def refresh(show: Show) -> None:
            nonlocal refreshed, failed
            async with slots:
                try:
                    latest, episodes = await self._tvmaze.get_show_with_episodes(show.id)
                except TVMazeError as exc:
                    log.warning(
                        "Sync could not refresh %s (TVMaze id %d): %s", show.name, show.id, exc
                    )
                    failed += 1
                else:
                    await self._library.save_show(latest, episodes)
                    await self._library.mark_synced(latest.id)
                    refreshed += 1
            if progress is not None:
                done = refreshed + failed
                _invoke(progress, SyncProgress(done=done, total=len(to_refresh), current=show.name))

        if to_refresh:
            await self._join(*(asyncio.create_task(refresh(show)) for show in to_refresh))

        async with self._tracking:
            pruned = await self._library.prune_schedule(date.today() - SCHEDULE_RETENTION)
        report = SyncReport(
            checked=len(entries),
            refreshed=refreshed,
            failed=failed,
            started_at=started_at,
            finished_at=datetime.now(UTC),
        )
        log.info(
            "Library sync: %d shows checked, %d refreshed, %d failed; %d old episodes pruned",
            report.checked, report.refreshed, report.failed, pruned,
        )
        self._notify()
        return report

    async def _stale_shows(
        self, entries: Sequence[LibraryEntry], *, force: bool, now: datetime
    ) -> set[int]:
        """Ids of the tracked shows that need refetching."""
        if force or not entries:
            return {entry.show.id for entry in entries}
        try:
            updates = await self._tvmaze.updated_shows("month")
        except TVMazeError as exc:
            log.warning("TVMaze updates feed unavailable, refreshing every show: %s", exc)
            return {entry.show.id for entry in entries}
        return {
            entry.show.id for entry in entries if _is_stale(entry, updates.get(entry.show.id), now)
        }

    async def run_periodic_sync(
        self,
        *,
        interval: timedelta = timedelta(hours=6),
        initial_delay: float = 5.0,
        progress: Callable[[SyncProgress], None] | None = None,
    ) -> None:
        """Sync after `initial_delay` seconds, then every `interval`, until cancelled.

        A failed run is logged and the loop carries on.
        """
        await asyncio.sleep(initial_delay)
        while True:
            try:
                await self.sync(progress)
            except Exception:
                log.exception("Library sync failed")
            await asyncio.sleep(interval.total_seconds())

    # -- shared tasks --------------------------------------------------------------------------

    async def _join(self, *tasks: asyncio.Task[Any]) -> None:
        """Wait for tasks that other calls may be awaiting too; re-raise the first failure.

        Returns once every task is done, or raises as soon as one fails. When this call stops
        waiting early (a failure, or its own cancellation), the tasks no other call is waiting
        for are cancelled and awaited; the others keep running.
        """
        self._waiters.update(tasks)
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
            for task in tasks:
                if task in done and not task.cancelled() and (exc := task.exception()) is not None:
                    raise exc
        finally:
            orphans: list[asyncio.Task[Any]] = []
            for task in tasks:
                self._waiters[task] -= 1
                if self._waiters[task] <= 0:
                    del self._waiters[task]
                    if not task.done():
                        task.cancel()
                        orphans.append(task)
            if orphans:
                await asyncio.wait(orphans)
            for task in tasks:  # mark every failure as retrieved; the first one is raised above
                if task.done() and not task.cancelled():
                    task.exception()


class _Fetched(NamedTuple):
    """An untracked show fetched for `show_details`."""

    show: Show
    episodes: tuple[Episode, ...]
    stored_at: datetime


_FAR_FUTURE = datetime.max.replace(tzinfo=UTC)


def _episode_order(episode: Episode) -> tuple[int, int]:
    """By season, then number; unnumbered episodes (specials) first, as SQLite sorts NULL."""
    return episode.season, -1 if episode.number is None else episode.number


def next_episode(episodes: Iterable[Episode], now: datetime) -> Episode | None:
    """The first numbered, non-special episode still to air: an airstamp after `now`, or
    (without one) an air date from `now`'s day on. The Tracked cards' rule
    (LibraryRepository.next_episodes); `now` is aware, its time zone decides the day."""
    today = now.date()
    upcoming = [
        episode
        for episode in episodes
        if episode.season > 0
        and episode.number is not None
        and (
            episode.airstamp > now
            if episode.airstamp is not None
            else episode.air_date is not None and episode.air_date >= today
        )
    ]
    return min(
        upcoming,
        key=lambda episode: (
            episode.air_date or date.max,
            episode.airstamp or _FAR_FUTURE,
            episode.season,
            episode.number or 0,
        ),
        default=None,
    )


def downloaded_ids(
    episodes: Iterable[Episode], grabs: Iterable[tuple[int, int | None]]
) -> frozenset[int]:
    """Ids of the episodes `grabs` ((season, episode) pairs; episode None = a season pack)
    cover: the episode itself, or its season's pack (as the calendar flags them)."""
    grabbed = set(grabs)
    packs = {season for season, number in grabbed if number is None}
    return frozenset(
        episode.id
        for episode in episodes
        if episode.season in packs
        or (episode.number is not None and (episode.season, episode.number) in grabbed)
    )


class NameHint(NamedTuple):
    """The year or country a show name ends with, and the name without it."""

    query: str  # the name without its suffix
    year: int | None
    country: str | None  # ISO code as TVMaze gives it
    bracketed: bool = True  # "Ghosts (2021)"; False for a bare "MacGyver 2016"

    def fits(self, show: Show, country: str | None) -> bool:
        """`show` (from `country`) premiered in the year / comes from the country named."""
        return (self.year is None or show.year == self.year) and (
            self.country is None or country == self.country
        )


_SUFFIX = re.compile(r"^(?P<base>.*\S)\s*\((?P<tag>\d{4}|[A-Z]{2})\)$")
_BARE_SUFFIX = re.compile(r"^(?P<base>.*\S)\s+(?P<tag>\d{4}|[A-Z]{2})$")
# Countries a name may end with without brackets ("Shameless US"); other capitals at the end
# ("House MD") stay part of the name.
_BARE_COUNTRIES = frozenset({"US", "UK", "GB", "AU", "NZ", "CA", "IE", "ZA"})
# Names other sites use for a country, mapped to TVMaze's ISO 3166-1 code.
_COUNTRY_ALIASES = {"UK": "GB"}
# One letter ("S", "S.") and letters with dots ("S.H.I.E.L.D.", "A.P"): parts of an initialism.
_LETTER = re.compile(r"[A-Za-z]\.?")
_DOTTED = re.compile(r"(?:[A-Za-z]\.){2,}[A-Za-z]?\.?|(?:[A-Za-z]\.)+[A-Za-z]")


_DASHES = re.compile("[‐-―−]")  # hyphens, dashes, the minus sign
_QUOTES = str.maketrans(
    {
        "‘": "'", "’": "'", "‚": "'", "‛": "'",
        "“": '"', "”": '"', "„": '"', "‟": '"',
    }
)


def search_name(name: str) -> str:
    """`name` as searched on TVMaze: NFKC, every Unicode dash or minus sign as "-", curly
    quotes as straight ones, whitespace collapsed. (airdates.tv writes "Re:Zero − Starting
    Life in Another World" with a minus sign; TVMaze knows it with "-".)"""
    text = _DASHES.sub("-", unicodedata.normalize("NFKC", name)).translate(_QUOTES)
    return " ".join(text.split())


def name_hint(name: str) -> NameHint | None:
    """The year or country a show name ends with: in brackets, "Ghosts (2021)" or "The Office
    (US)" (any two capitals); bare, "MacGyver 2016" or "Shameless US" (_BARE_COUNTRIES only).
    A year counts from 1900 to 2100."""
    text = name.strip()
    bracketed = True
    match = _SUFFIX.match(text)
    if match is None:
        bracketed = False
        match = _BARE_SUFFIX.match(text)
        if match is None or not (match["tag"].isdigit() or match["tag"] in _BARE_COUNTRIES):
            return None
    base, tag = match["base"], match["tag"]
    if tag.isdigit():
        year = int(tag)
        return NameHint(base, year, None, bracketed) if 1900 <= year <= 2100 else None
    return NameHint(base, None, _COUNTRY_ALIASES.get(tag, tag), bracketed)


def initialism_variants(name: str) -> list[str]:
    """Other spellings of an initialism in `name`: letters written apart ("Agents of S H I E L
    D", "S. H. I. E. L. D.") or with dots ("S.H.I.E.L.D.") as "Agents of S.H.I.E.L.D." and
    "Agents of SHIELD" (TVMaze finds the show by either, not by spaced letters). Empty when
    `name` has none: one letter alone ("I Love Lucy", "Malcolm X") is no initialism."""
    tokens = name.split()
    dotted: list[str] = []
    joined: list[str] = []
    found = False
    index = 0
    while index < len(tokens):
        token = tokens[index]
        end = index
        while end < len(tokens) and _LETTER.fullmatch(tokens[end]):
            end += 1
        if end - index >= 2:
            letters = "".join(part.rstrip(".") for part in tokens[index:end])
        elif _DOTTED.fullmatch(token):
            letters, end = token.replace(".", ""), index + 1
        else:
            dotted.append(token)
            joined.append(token)
            index += 1
            continue
        dotted.append(".".join(letters) + ".")
        joined.append(letters)
        found = True
        index = end
    if not found:
        return []
    variants = (" ".join(dotted), " ".join(joined))
    return [variant for variant in dict.fromkeys(variants) if variant != name]


def search_queries(name: str, hint: NameHint | None) -> list[str]:
    """What LibraryService searches for a (normalised) name, in order, each once (ignoring
    case): the name, or with a year / country `hint` the name without it first when that was in
    brackets and second when it was bare; then the initialism variants (`initialism_variants`)
    of the name without the hint and of the whole name."""
    if hint is None:
        bases = [name]
    elif hint.bracketed:
        bases = [hint.query, name]
    else:
        bases = [name, hint.query]
    sources = [hint.query, name] if hint is not None else [name]
    variants = [variant for source in sources for variant in initialism_variants(source)]
    queries: dict[str, str] = {}
    for query in (*bases, *variants):
        queries.setdefault(query.casefold(), query)
    return list(queries.values())


def same_name(a: str, b: str) -> bool:
    """The same name beyond case, spaces and punctuation ("Space: 1999" / "space 1999")."""
    return _plain_name(a) == _plain_name(b)


def _plain_name(name: str) -> str:
    return "".join(char for char in name.casefold() if char.isalnum())


def _match(name: str, show: Show | None, tracked: frozenset[int]) -> ImportMatch:
    if show is None:
        return ImportMatch(source_name=name)
    return ImportMatch(
        source_name=name,
        show_id=show.id,
        show_name=show.name,
        year=show.year,
        network=show.network,
        already_tracked=show.id in tracked,
    )


def _is_stale(entry: LibraryEntry, changed_at: int | None, now: datetime) -> bool:
    """`changed_at`: the show's stamp in TVMaze's updates feed, None if it isn't listed."""
    if entry.synced_at is None or now - entry.synced_at > UPDATES_WINDOW:
        return True
    stored = entry.show.updated
    return changed_at is not None and (stored is None or changed_at > stored)


def _days(start: date, end: date) -> Iterator[date]:
    for offset in range((end - start).days + 1):
        yield start + timedelta(days=offset)


def _invoke(callback: Callable[P, None], *args: P.args, **kwargs: P.kwargs) -> None:
    """Run a listener or progress callback: its failure is logged, never propagated."""
    try:
        callback(*args, **kwargs)
    except Exception:
        log.exception("Callback %r failed", callback)
