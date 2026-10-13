"""Tracked-library use cases.

Reads come straight from SQLite. Tracking, search, import, calendar suggestions and the
incremental background sync go through TVMaze and cache what they fetch.
"""

from __future__ import annotations

import asyncio
import functools
import logging
from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any, ParamSpec

from app.database import HistoryRepository, LibraryRepository
from app.models import (
    CalendarEntry,
    Episode,
    ImportResult,
    LibraryEntry,
    LibraryStats,
    Show,
    ShowSearchResult,
    SyncProgress,
    SyncReport,
    Weekday,
)
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


def week_start(day: date, first_day: Weekday) -> date:
    """The first day of the week (starting on `first_day`) that contains `day`."""
    return day - timedelta(days=(day.weekday() - first_day.weekday) % 7)


def calendar_range(
    today: date, *, first_day: Weekday, weeks: int, offset: int = 0
) -> tuple[date, date]:
    """First and last day of the calendar grid: `weeks` whole weeks.

    The grid starts at the week containing `today` moved by `offset` weeks (earlier weeks when
    negative).
    """
    start = week_start(today, first_day) + timedelta(weeks=offset)
    return start, start + timedelta(weeks=weeks, days=-1)


class LibraryService:
    def __init__(
        self, library: LibraryRepository, history: HistoryRepository, tvmaze: TVMazeClient
    ) -> None:
        self._library = library
        self._history = history
        self._tvmaze = tvmaze
        self._listeners: list[LibraryListener] = []
        # Held while saving-then-tracking a show and while pruning (which deletes untracked
        # shows), so a show can't be pruned between being saved and being tracked.
        self._tracking = asyncio.Lock()
        self._schedule_fetches: dict[date, asyncio.Task[None]] = {}
        self._sync_run: asyncio.Task[SyncReport] | None = None
        self._waiters: Counter[asyncio.Task[Any]] = Counter()

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

    # -- reads ---------------------------------------------------------------------------------

    async def tracked(self) -> list[LibraryEntry]:
        return await self._library.tracked()

    async def stats(self) -> LibraryStats:
        return await self._library.stats()

    async def calendar(self, start: date, end: date) -> list[CalendarEntry]:
        return await self._library.calendar(start, end)

    async def episodes(self, show_id: int) -> list[Episode]:
        """The stored episodes of a show, by season and number (specials have no number)."""
        return await self._library.episodes(show_id)

    async def search(self, query: str, *, limit: int = 12) -> list[ShowSearchResult]:
        """TVMaze shows matching `query`, best match first, flagged when already tracked."""
        shows = await self._tvmaze.search_shows(query, limit=limit)
        tracked = await self._library.tracked_ids() if shows else frozenset[int]()
        return [ShowSearchResult(show=show, is_tracked=show.id in tracked) for show in shows]

    # -- library changes -----------------------------------------------------------------------

    async def untrack(self, show_id: int) -> None:
        await self._library.untrack(show_id)
        self._notify()

    async def remove_ended(self) -> int:
        removed = await self._library.untrack_ended()
        if removed:
            self._notify()
        return removed

    async def track(self, show_id: int) -> Show:
        """Fetch a show with all its episodes from TVMaze and add it to the library.

        Tracking a show that is already tracked refreshes it. TVMaze errors propagate.
        """
        show = await self._fetch_and_track(show_id)
        self._notify()
        return show

    async def import_shows(
        self, entries: Sequence[str], progress: Callable[[int, int], None] | None = None
    ) -> ImportResult:
        """Track the shows listed in `entries`: TVMaze ids, or names (the best search hit wins).

        Entries are handled one by one to stay within TVMaze's rate limit; `progress(done, total)`
        follows each. Listeners are notified once, at the end, if anything was added.
        """
        pending = list(dict.fromkeys(entry.strip() for entry in entries if entry.strip()))
        tracked = set(await self._library.tracked_ids())
        added: list[str] = []
        already_tracked: list[str] = []
        not_found: list[str] = []
        failed: list[str] = []
        try:
            for done, entry in enumerate(pending, start=1):
                try:
                    outcome = await self._import_entry(entry, tracked)
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

    async def _import_entry(self, entry: str, tracked: set[int]) -> tuple[str, bool] | None:
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
            hits = await self._tvmaze.search_shows(entry, limit=1)
            if not hits:
                return None
            show_id = hits[0].id
            if show_id in tracked:
                return hits[0].name, False
        show = await self._fetch_and_track(show_id)
        tracked.add(show.id)
        return show.name, True

    async def _fetch_and_track(self, show_id: int) -> Show:
        show, episodes = await self._tvmaze.get_show_with_episodes(show_id)
        async with self._tracking:
            await self._library.save_show(show, episodes)
            await self._library.track(show.id)
        await self._library.mark_synced(show.id)
        return show

    # -- calendar suggestions ------------------------------------------------------------------

    async def suggestions(self, start: date, end: date, *, per_day: int) -> list[CalendarEntry]:
        """The most popular untracked shows airing each day, from TVMaze's cached US schedule."""
        await self._ensure_schedule(start, end)
        return await self._library.suggestions(start, end, per_day=per_day)

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
            # Tracked shows' rows are left to sync: storing their change stamp from here would
            # make sync believe their (unrefreshed) episode lists are current.
            tracked = await self._library.tracked_ids()
            shows = {show.id: show for show, _ in items if show.id not in tracked}
            await self._library.save_schedule_day(
                day, shows.values(), [episode for _, episode in items]
            )

    # -- sync ----------------------------------------------------------------------------------

    async def sync(
        self, progress: Callable[[SyncProgress], None] | None = None, *, force: bool = False
    ) -> SyncReport:
        """Refresh tracked shows from TVMaze, refetching only those that may have changed.

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
        entries = await self._library.tracked()
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
