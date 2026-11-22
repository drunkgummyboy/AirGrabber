"""The magnet guard: a magnet sent to the torrent program is watched until its files are known,
and a download that turns out to hold a program is removed (owner's decision 2026-11-22, "Watch
and remove").

A .torrent is looked into before it goes (app/services/torrent_files.py, ProgramInTorrent in
downloads.py); a magnet can't be: only the torrent program learns its files, once it has fetched
the metadata. On 2026-11-21 a magnet-only release ("… [MeGusta]") went to Download Station that
way and downloaded `… [MeGusta].exe`. So every magnet DownloadService sends to the remote program
(REMOTE mode's magnets, FOLDER mode's magnet fallback: `DownloadService.subscribe_magnets`, by
info hash) is watched here: checked every 2 s for its first 2 minutes (a program file is small:
seconds matter), every 10 s up to 30 minutes, every minute up to 24 hours, then no more. Each
check finds the task by its hash (`RemoteDownloads.status`), then asks for its files
(`file_names`, remote.py's RemoteTaskFiles):

- not known yet (a magnet still fetching its metadata): watched on;
- none of them a program: no longer watched;
- a program (torrent_files' `program_suffix`, the .torrent check's rule): the task is removed with
  what it downloaded (`remove_with_files`; Download Station only deletes an unfinished download,
  so a finished file may stay and the notice says so), its hash goes to `Settings.refused_hashes`
  (`DownloadService.remember_refused`: never sent again), the grab leaves the download history
  (`HistoryRepository.forget`: the episode or film doesn't count as fetched) and the listeners
  hear a CaughtProgram: the main window tells the user (tray + status bar, `caught_notice`) and
  downloads it again, the releases fetched for it before left out (`DownloadService.avoided`,
  as a stuck download's swap);
- the task gone (removed in the program; not listed 10 minutes after it was sent): no longer
  watched; the program can't be reached: tried again on the schedule.

One watch per info hash, any number at once, all checked by one loop (`run`, spawned from
MainWindow._start: no threads). After a restart `run` first picks up the grabs of the last 24
hours that went to the program in use and have an info hash (the history's `saved_path` is the
program's name for those; REMOTE mode's .torrent sends look the same and are checked once more,
harmlessly), so a restart while a magnet fetches its metadata is covered.

Logs: the release title as downloads.py logs it, counts and reasons; never a link, info hash,
task name, file name or path.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import NamedTuple

from app.database import HistoryRepository
from app.i18n import t
from app.models import (
    DownloadEntry,
    DownloadMode,
    DownloadRecord,
    DownloadState,
    MediaType,
    RemoteTask,
    Show,
)
from app.services.download_status import episode_title, movie_title
from app.services.downloads import DownloadService
from app.services.remote import FilesUnknown, RemoteError
from app.services.remote_downloads import RemoteDownloads
from app.services.settings import SettingsService
from app.services.torrent_files import program_suffix

log = logging.getLogger(__name__)

# (watched for less than this, seconds between checks): fast at first, then slower.
SCHEDULE: tuple[tuple[timedelta, float], ...] = (
    (timedelta(minutes=2), 2.0),
    (timedelta(minutes=30), 10.0),
    (timedelta(hours=24), 60.0),
)
WATCH_FOR = SCHEDULE[-1][0]  # then no longer watched
NOT_LISTED_GRACE = timedelta(minutes=10)  # a task the program never listed this long: gone
RESTART_LIMIT = 200  # the newest grabs looked at after a restart

ShowLookup = Callable[[int], Awaitable[Show | None]]
Sleep = Callable[[float | None], Awaitable[None]]  # None: until a new watch (tests: fakes)


def check_interval(age: timedelta) -> float | None:
    """Seconds until the next check of a magnet sent `age` ago; None past WATCH_FOR."""
    for until, seconds in SCHEDULE:
        if age < until:
            return seconds
    return None


class CaughtProgram(NamedTuple):
    """A magnet's download that held a program, removed from the torrent program."""

    title: str  # "Gold Rush S17E08", "Gladiator II (2024)"; the task's name without a grab
    file_name: str  # the program's file name, as the torrent names it (shown, never logged)
    file_may_remain: bool  # a file the program finished may still be where it put it
    entry: DownloadEntry | None  # the grab, to download again; None: nothing to fetch again
    avoid: tuple[str, ...]  # the releases fetched for it so far: left out of the next pick


class CaughtNotice(NamedTuple):
    """How a CaughtProgram is told: a tray notification (title, body) and the status bar."""

    title: str
    body: str
    status: str


def caught_notice(caught: CaughtProgram) -> CaughtNotice:
    title = t("Removed {title}", title=caught.title)
    parts = [t("It was a program, not a video.")]
    if caught.entry is not None:
        parts.append(t("AirGrabber is fetching another release."))
    if caught.file_may_remain:
        parts.append(
            t(
                "The file {name} may still be in your downloads folder: don't open it, "
                "delete it.",
                name=caught.file_name,
            )
        )
    body = " ".join(parts)
    return CaughtNotice(title=title, body=body, status=f"{title}. {body}")


@dataclass(slots=True)
class _Watch:
    info_hash: str  # lowercase hex
    sent_at: datetime  # when the magnet went (the grab's time after a restart)
    due: datetime  # the next check
    seen: bool = False  # the program has listed its task


class ProgramGuard:
    """`services.program_guard`: `watch` (DownloadService.subscribe_magnets), `run` (the
    loop, MainWindow._start), `subscribe` (CaughtProgram listeners, the main window)."""

    def __init__(
        self,
        remote: RemoteDownloads,
        settings: SettingsService,
        history: HistoryRepository,
        downloads: DownloadService,
        *,
        show: ShowLookup,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Sleep | None = None,
    ) -> None:
        self._remote = remote
        self._settings = settings
        self._history = history
        self._downloads = downloads
        self._show = show
        self._now = now
        self._sleep: Sleep = sleep if sleep is not None else self._wait
        self._watches: dict[str, _Watch] = {}
        self._listeners: list[Callable[[CaughtProgram], None]] = []
        self._wake = asyncio.Event()
        self._unreachable = False  # the program couldn't be reached at the last check

    # -- listeners ------------------------------------------------------------------------------

    def subscribe(self, listener: Callable[[CaughtProgram], None]) -> Callable[[], None]:
        """Call `listener` with every download removed for holding a program. Returns a
        function that unsubscribes it."""
        self._listeners.append(listener)
        subscribed = True

        def unsubscribe() -> None:
            nonlocal subscribed
            if subscribed:
                subscribed = False
                self._listeners.remove(listener)

        return unsubscribe

    def _tell(self, caught: CaughtProgram) -> None:
        for listener in list(self._listeners):
            try:
                listener(caught)
            except Exception:
                log.exception("Magnet guard listener %r failed", listener)

    # -- watching -------------------------------------------------------------------------------

    def watch(self, info_hash: str) -> None:
        """Watch the magnet with this info hash, sent to the torrent program just now (a
        magnet sent again starts its schedule over)."""
        key = info_hash.strip().casefold()
        if not key:
            return
        now = self._now()
        self._watches[key] = _Watch(key, now, now + timedelta(seconds=SCHEDULE[0][1]))
        self._wake.set()

    @property
    def watching(self) -> frozenset[str]:
        """The info hashes watched now."""
        return frozenset(self._watches)

    async def run(self) -> None:
        """Pick up the grabs from before the restart, then check what is due for as long as
        AirGrabber runs."""
        try:
            await self.pick_up()
        except Exception:
            log.exception("Magnet guard: the grabs from before the start couldn't be read")
        while True:
            self._wake.clear()
            try:
                await self.check_due()
            except Exception:
                log.exception("Magnet guard: a check failed")
            await self._sleep(self._next_wait())

    def _next_wait(self) -> float | None:
        if not self._watches:
            return None
        now = self._now()
        return max(0.0, min((watch.due - now).total_seconds() for watch in self._watches.values()))

    async def _wait(self, seconds: float | None) -> None:
        if seconds is None:
            await self._wake.wait()
            return
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                await self._wake.wait()

    async def pick_up(self) -> int:
        """Watch the grabs of the last 24 hours that went to the program in use and have an
        info hash (`saved_path` = its name). Returns how many."""
        settings = self._settings.current
        if settings.download_mode is DownloadMode.APP or not settings.remote_configured:
            return 0
        program = settings.remote_kind.program
        refused = set(settings.refused_hashes)
        now = self._now()
        count = 0
        for record in await self._history.recent(RESTART_LIMIT):
            if now - record.downloaded_at >= WATCH_FOR:
                break  # newest first: the rest is older still
            key = record.info_hash
            if record.saved_path != program or not key or key in refused or key in self._watches:
                continue
            self._watches[key] = _Watch(key, min(record.downloaded_at, now), now)
            count += 1
        if count:
            log.info("Magnet guard: watching %d grab(s) sent before this start", count)
            self._wake.set()
        return count

    async def check_due(self) -> None:
        """One round: every watch that is due, with one task list from the program."""
        now = self._now()
        due: list[_Watch] = []
        for watch in [watch for watch in self._watches.values() if watch.due <= now]:
            if now - watch.sent_at >= WATCH_FOR:
                self._drop(watch, "its files weren't known within 24 hours")
            else:
                due.append(watch)
        if not due:
            return
        try:
            status = await self._remote.status([watch.info_hash for watch in due])
        except RemoteError:
            self._reachable(False)
            for watch in due:
                self._later(watch, now)
            return
        self._reachable(True)
        tasks = {task.info_hash: task for task in status.tasks if task.info_hash}
        for watch in due:
            if self._watches.get(watch.info_hash) is not watch:
                continue  # sent again meanwhile: the new watch has its own schedule
            task = tasks.get(watch.info_hash)
            if task is None:
                if watch.seen or now - watch.sent_at >= NOT_LISTED_GRACE:
                    self._drop(watch, "its download is gone from the torrent program")
                else:
                    self._later(watch, now)  # not picked up yet
                continue
            watch.seen = True
            await self._check_task(watch, task, now)

    async def _check_task(self, watch: _Watch, task: RemoteTask, now: datetime) -> None:
        try:
            names = await self._remote.file_names(task)
        except FilesUnknown:
            self._drop(watch, "its torrent program can't tell a download's files")
            return
        except RemoteError:
            self._later(watch, now)
            return
        if names is None:
            self._later(watch, now)  # its metadata isn't there yet
            return
        found = next((name for name in names if program_suffix(name) is not None), None)
        if found is None:
            self._drop(watch, "no program among its files")
            return
        await self._remove(watch, task, found, now)

    async def _remove(self, watch: _Watch, task: RemoteTask, file_name: str, now: datetime) -> None:
        # Refused first: even while its removal fails, it is never sent again.
        await self._downloads.remember_refused(watch.info_hash)
        task_id = task.task_id or watch.info_hash
        try:
            deleted_all = await self._remote.remove_with_files([task_id])
        except RemoteError:
            log.warning("Magnet guard: a download holding a program couldn't be removed yet")
            self._later(watch, now)
            return
        self._watches.pop(watch.info_hash, None)
        record = await self._record(watch.info_hash)
        suffix = program_suffix(file_name)
        program = self._remote.kind.program
        if record is not None:
            log.warning(
                "Removed %r from %s: its torrent holds a %s file", record.release_title, program,
                suffix,
            )
        else:
            log.warning("Removed a magnet's download from %s: it holds a %s file", program, suffix)
        entry: DownloadEntry | None = None
        avoid: tuple[str, ...] = ()
        title = task.name or t("A download")
        if record is not None:
            avoid = await self._downloads.avoided(record)
            await self._forget(record)
            entry, title = await self._entry(record)
        finished = task.state is DownloadState.READY or task.progress > 0
        self._tell(
            CaughtProgram(
                title=title,
                file_name=file_name,
                file_may_remain=not deleted_all and finished,
                entry=entry,
                avoid=avoid,
            )
        )

    async def _record(self, info_hash: str) -> DownloadRecord | None:
        """The newest grab of this info hash in the history."""
        try:
            grabs = await self._history.every_grab()
        except Exception:
            log.exception("Magnet guard: the download history couldn't be read")
            return None
        return next((grab for grab in grabs if grab.info_hash == info_hash), None)

    async def _forget(self, record: DownloadRecord) -> None:
        if record.id is None:
            return
        try:
            await self._history.forget(record.id)
        except Exception:
            log.exception("Magnet guard: the grab couldn't be taken out of the download history")

    async def _entry(self, record: DownloadRecord) -> tuple[DownloadEntry | None, str]:
        """The grab as the Downloads page's "Download again" takes it, and its title; no entry
        for an episode whose show isn't known (nothing to search for)."""
        if record.media_type is MediaType.MOVIE:
            title = movie_title(record.release_title)
            return DownloadEntry(record=record, title=title), title
        show: Show | None = None
        try:
            show = await self._show(record.media_id)
        except Exception:
            log.exception("Magnet guard: the grab's show couldn't be read")
        if show is None or record.season is None:
            return None, record.release_title
        title = episode_title(show.name, record.season, record.episode)
        return DownloadEntry(record=record, title=title, show=show), title

    def _later(self, watch: _Watch, now: datetime) -> None:
        seconds = check_interval(now - watch.sent_at)
        watch.due = now + timedelta(seconds=seconds if seconds is not None else 0.0)

    def _drop(self, watch: _Watch, reason: str) -> None:
        if self._watches.get(watch.info_hash) is watch:
            del self._watches[watch.info_hash]
        log.info("Magnet guard: no longer watching a magnet (%s)", reason)

    def _reachable(self, reached: bool) -> None:
        if reached == (not self._unreachable):
            return
        self._unreachable = not reached
        if reached:
            log.info("Magnet guard: the torrent program answers again")
        else:
            log.info("Magnet guard: the torrent program can't be reached; trying again")
