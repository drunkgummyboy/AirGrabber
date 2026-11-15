"""The download queue: every one-click download the user asks for, a couple at a time.

Clicking ⬇ on many episodes in a row used to start that many one-click downloads at once
(3.0.1's report: "some downloads seem to get cancelled along the way"). Each one runs a full
search of every source, and every source takes at most two requests at a time
(`BaseIndexer`'s slots; EZTV reads up to five pages per search), so ten at once queue up behind
each other's requests until the sources' time limits (25 s each, 30 s for a one-click download
in all) run out: the later ones came back with nothing and showed "No good release found"
although the releases were there. The queue runs at most WORKERS of them at a time, so every
search gets its sources' slots in time.

The queue belongs to Services and lives as long as the app: closing a window never cancels
what it queued. A download already waiting or running is not queued twice (`add` returns
False). A queued download searches through QuickDownloadService (which picks with `pick_best`
and skips a title that is in the download history by then), so the one-click path is the same
as before. Listeners (`subscribe`) hear every change of state as a QueueEvent: WAITING, RUNNING
(searching or sending), then DONE (grabbed, or downloaded already), NOTHING (nothing good
enough), FAILED (the grab failed: no torrent program, an unwritable folder, the remote program
refusing; `message` says why) or DROPPED (taken out before its turn). `event(key)` keeps each
title's last one, so a window opened later shows where its titles are.

A batch (`add_batch`: a calendar stack's "Download all", the show window's "All missing", My
shows' "Download missing", a season's episodes) ends at a failed grab, as before: its other
waiting downloads are dropped.
A season pack (`QueuedDownload.fallback`) that finds nothing queues the season's episodes
still needed instead (a batch of their own) and says so in its NOTHING event.

What waits survives a quit (item 25; an update restarts the app the same way): with a `file`
(Services: `<data>/download-queue.json`, QUEUE_FILE) every change of what waits or runs is
written there as plain JSON (`"version": 1`; each download's label, GrabTarget, SearchRequest,
`again`, `avoid`, `fallback`, batch and when it was queued; a film's request made at its turn
is kept as none and made again through `film_request`), atomically, through
`asyncio.to_thread`. `aclose()` at the quit writes what waited and what was running (that
counts as waiting; a worker cancelled by the quit's `cancel_all` first puts its download back
at the front). A "download again" (`again`) that was running at the quit may have grabbed
already: after the restart it is grabbed again. The next start's `restore()`
(MainWindow._start, once the loop runs) reads it and queues it again in its order, batches as
batches, behind whatever was clicked while it read; entries older than KEEP_FOR (7 days) are
dropped; the file is then replaced by what waits now, or removed when nothing does. A file
that can't be read (also one entry that doesn't validate), or of another format version, is
renamed to `download-queue.json.bad` and ignored as a whole, with a log line. Until `restore()` has run,
nothing is written (it would overwrite what the last run kept). Without a file (tests) the
queue is kept in memory only: `close()` drops what waits, and the log says how many. The
automatic downloads (app/services/auto_download.py) keep their own run, one download at a
time.

Log hygiene: only labels ("Severance S02E01"), counts and outcomes are logged, never a release,
a link or a URL.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import logging
import os
import tempfile
import traceback
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.i18n import t
from app.models import (
    GrabTarget,
    MediaType,
    QueuedItem,
    QueueEvent,
    QueueKey,
    QueueState,
    QuickDownloadResult,
    SearchRequest,
)
from app.models.types import UtcDatetime
from app.services.downloads import GrabError
from app.services.quick_download import QuickDownloadService
from app.services.remote import RemoteError

log = logging.getLogger(__name__)

WORKERS = 2  # one-click downloads searching at the same time
QUEUE_FILE = "download-queue.json"  # in the data folder: what waited at the last quit
QUEUE_FILE_VERSION = 1
KEEP_FOR = timedelta(days=7)  # a download queued longer ago than this isn't queued again

# What a queued download searches for, or a coroutine function that makes it when its turn
# comes (a film's request waits for its TMDB details).
RequestSource = SearchRequest | Callable[[], Awaitable[SearchRequest]]
QueueListener = Callable[[QueueEvent], None]
# A film's request by its TMDB id (and its label, for a message), made again at its turn for a
# film queued before a restart.
FilmRequest = Callable[[int, str], Awaitable[SearchRequest]]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def target_key(target: GrabTarget) -> QueueKey:
    """The queue's key of a download's title."""
    return (target.media_type, target.media_id, target.season, target.episode)


@dataclass(frozen=True, slots=True)
class QueuedDownload:
    """One one-click download: its `label` ("Severance S02E01"; the status bar and the
    Downloads page show it, the log too), what it is for (`target`), what it searches for
    (`request`); `again`: also when it was downloaded before (a click by hand on one title); `fallback`: a
    season pack's episodes still needed, queued instead when no pack is good enough."""

    label: str
    target: GrabTarget
    request: RequestSource
    again: bool = False
    fallback: tuple[QueuedDownload, ...] = ()
    avoid: tuple[str, ...] = ()  # releases never picked (a stuck download's swap)

    @property
    def key(self) -> QueueKey:
        return target_key(self.target)


@dataclass(slots=True)
class _Entry:
    item: QueuedDownload
    batch: int | None  # the batch it was queued with, if any
    queued_at: datetime = field(default_factory=_utc_now)


# -- the file (download-queue.json) ------------------------------------------------------------


class _StoredDownload(BaseModel):
    """A QueuedDownload as the file keeps it; `request` None: a film's, made at its turn."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    target: GrabTarget
    request: SearchRequest | None = None
    again: bool = False
    avoid: tuple[str, ...] = ()
    fallback: tuple[_StoredDownload, ...] = ()


class _StoredEntry(_StoredDownload):
    batch: int | None = None
    queued_at: UtcDatetime


class _QueueFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    version: Literal[1]
    entries: tuple[_StoredEntry, ...] = ()


def _stored(item: QueuedDownload) -> _StoredDownload | None:
    """`item` as the file keeps it; None when it can't be (a request made at its turn that
    isn't a film's)."""
    request = item.request if isinstance(item.request, SearchRequest) else None
    if request is None and item.target.media_type is not MediaType.MOVIE:
        return None
    fallback = tuple(kept for one in item.fallback if (kept := _stored(one)) is not None)
    return _StoredDownload(
        label=item.label,
        target=item.target,
        request=request,
        again=item.again,
        avoid=item.avoid,
        fallback=fallback,
    )


class DownloadQueue:
    """The app's one-click downloads, WORKERS at a time (`services.download_queue`)."""

    def __init__(
        self,
        quick_download: QuickDownloadService,
        *,
        workers: int = WORKERS,
        file: Path | None = None,
        film_request: FilmRequest | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """`file`: where what waits is kept across a quit (None: in memory only).
        `film_request`: a film's request by its TMDB id, for films kept there. `clock`: the
        current UTC instant (tests move it)."""
        if workers < 1:
            raise ValueError("workers must be at least 1")
        self._quick = quick_download
        self._limit = workers
        self._file = file
        self._film_request = film_request
        self._clock = clock
        self._restored = file is None  # nothing is written before restore() has read it
        self._dirty = False
        self._saver: asyncio.Task[None] | None = None
        self._final: tuple[_Entry, ...] | None = None  # what waited and ran at the quit
        self._waiting: deque[_Entry] = deque()
        self._running: dict[QueueKey, _Entry] = {}
        self._events: dict[QueueKey, QueueEvent] = {}
        self._listeners: list[QueueListener] = []
        self._workers: set[asyncio.Task[None]] = set()
        self._batch_ids = itertools.count(1)
        self._closed = False

    # -- what is in it -----------------------------------------------------------------------

    def subscribe(self, listener: QueueListener) -> Callable[[], None]:
        """Call `listener` with every QueueEvent. Returns a function that unsubscribes it."""
        self._listeners.append(listener)
        subscribed = True

        def unsubscribe() -> None:
            nonlocal subscribed
            if subscribed:
                subscribed = False
                self._listeners.remove(listener)

        return unsubscribe

    def event(self, key: QueueKey) -> QueueEvent | None:
        """The title's last event in this run of the app (None: never queued)."""
        return self._events.get(key)

    def state(self, key: QueueKey) -> QueueState | None:
        event = self._events.get(key)
        return event.state if event is not None else None

    def is_active(self, key: QueueKey) -> bool:
        """Whether the title is waiting or running."""
        return key in self._running or any(entry.item.key == key for entry in self._waiting)

    @property
    def waiting(self) -> tuple[QueuedItem, ...]:
        """The downloads still waiting for their turn, first first."""
        return tuple(
            QueuedItem(key=entry.item.key, label=entry.item.label) for entry in self._waiting
        )

    @property
    def running(self) -> tuple[QueuedItem, ...]:
        """The downloads searching or sending right now."""
        return tuple(
            QueuedItem(key=key, label=entry.item.label, state=QueueState.RUNNING)
            for key, entry in self._running.items()
        )

    # -- adding and taking out -----------------------------------------------------------------

    def add(self, item: QueuedDownload) -> bool:
        """Queue `item`; False (nothing changes) when its title is waiting or running already,
        or the queue is closed."""
        return self._add(item, None)

    def add_batch(self, items: Iterable[QueuedDownload]) -> tuple[QueueKey, ...]:
        """Queue `items` in order as one batch (a failed grab drops the batch's downloads
        still waiting); returns the keys queued now (the others were queued already)."""
        batch = next(self._batch_ids)
        return tuple(item.key for item in items if self._add(item, batch))

    def remove(self, key: QueueKey) -> bool:
        """Take a waiting download out of the queue (the Downloads page's ✕); False when it
        isn't waiting (running, finished or never queued)."""
        for entry in self._waiting:
            if entry.item.key == key:
                self._waiting.remove(entry)
                log.info("Download queue: %s taken out before its turn", entry.item.label)
                self._emit(QueueEvent(key=key, label=entry.item.label, state=QueueState.DROPPED))
                return True
        return False

    def close(self) -> int:
        """Shutdown: take the downloads still waiting out (logged: how many) and stop the
        running ones; with a file, both are written to it for the next start (`aclose` waits
        for that). Returns how many were waiting. A second call does nothing."""
        if self._closed:
            return 0
        self._closed = True
        waiting = len(self._waiting)
        if self._file is not None and self._restored:
            self._final = (*self._running.values(), *self._waiting)
        self._waiting.clear()
        for task in list(self._workers):
            task.cancel()
        if self._final is not None:
            log.info(
                "Download queue closed: %d download(s) kept for the next start", len(self._final)
            )
            self._changed()
        else:
            log.info("Download queue closed: %d waiting download(s) dropped", waiting)
        return waiting

    async def aclose(self) -> None:
        """`close()`, then wait until the file holds what was kept."""
        self.close()
        saver = self._saver
        if saver is not None:
            await saver

    async def restore(self) -> int:
        """Queue again what the last run kept in the file, in its order but behind what was
        queued while it was read (see the module docstring), and delete the file; returns how
        many were queued. Never raises over the file."""
        if self._restored:
            return 0
        file = self._file
        assert file is not None  # restored from the start without one
        problem: Exception | None = None
        kept: _QueueFile | None = None
        try:
            text = await asyncio.to_thread(_read, file)
            if text is not None:
                kept = _QueueFile.model_validate_json(text)
        except (OSError, ValueError) as exc:  # ValidationError and UnicodeDecodeError too
            problem = exc
        if self._closed:
            return 0  # the quit came first: the file stays as it is, for the next start
        if problem is not None:
            moved = await asyncio.to_thread(_set_aside, file)
            log.warning(
                "Download queue: the kept downloads couldn't be read (%s); file %s",
                type(problem).__name__,
                f"set aside as {file.name}.bad" if moved else "could not be set aside",
            )
        self._restored = True
        queued = 0 if kept is None or self._closed else self._queue_kept(kept.entries)
        # The file goes: replaced by what waits now (them, and what was queued meanwhile), or
        # removed when nothing does.
        self._changed()
        saver = self._saver
        if saver is not None:
            await asyncio.shield(saver)  # a cancelled restore leaves the write to finish
        return queued

    def _queue_kept(self, entries: Iterable[_StoredEntry]) -> int:
        now = self._clock()
        batches: dict[int, int] = {}  # the file's batch -> this run's
        queued = old = skipped = 0
        for stored in entries:
            if now - stored.queued_at > KEEP_FOR:
                old += 1
                continue
            item = self._from_stored(stored)
            if item is None:
                skipped += 1
                continue
            batch = None
            if stored.batch is not None:
                batch = batches.setdefault(stored.batch, next(self._batch_ids))
            if self._add(item, batch, stored.queued_at):
                queued += 1
        log.info(
            "Download queue: %d kept download(s) queued again, %d older than %d days dropped%s",
            queued,
            old,
            KEEP_FOR.days,
            f", {skipped} without a way to search left out" if skipped else "",
        )
        return queued

    def _from_stored(self, stored: _StoredDownload) -> QueuedDownload | None:
        request: RequestSource
        if stored.request is not None:
            request = stored.request
        elif stored.target.media_type is MediaType.MOVIE and self._film_request is not None:
            make, movie_id, label = self._film_request, stored.target.media_id, stored.label

            async def film() -> SearchRequest:
                return await make(movie_id, label)

            request = film
        else:
            return None
        fallback = tuple(
            one for kept in stored.fallback if (one := self._from_stored(kept)) is not None
        )
        return QueuedDownload(
            label=stored.label,
            target=stored.target,
            request=request,
            again=stored.again,
            fallback=fallback,
            avoid=stored.avoid,
        )

    def _add(
        self, item: QueuedDownload, batch: int | None, queued_at: datetime | None = None
    ) -> bool:
        if self._closed or self.is_active(item.key):
            return False
        self._waiting.append(_Entry(item, batch, queued_at or self._clock()))
        log.info("Download queue: %s queued (%d waiting)", item.label, len(self._waiting))
        self._emit(QueueEvent(key=item.key, label=item.label, state=QueueState.WAITING))
        self._start_workers()
        return True

    # -- working through it --------------------------------------------------------------------

    def _start_workers(self) -> None:
        live = [task for task in self._workers if not task.done()]
        wanted = min(self._limit, len(self._running) + len(self._waiting))
        loop = asyncio.get_running_loop()
        for _ in range(wanted - len(live)):
            task = loop.create_task(self._work(), name="download queue")
            self._workers.add(task)
            task.add_done_callback(self._workers.discard)

    async def _work(self) -> None:
        while self._waiting and not self._closed:
            await self._run(self._waiting.popleft())

    async def _run(self, entry: _Entry) -> None:
        item = entry.item
        key = item.key
        self._running[key] = entry
        self._emit(QueueEvent(key=key, label=item.label, state=QueueState.RUNNING))
        try:
            result = await self._download(item)
        except (GrabError, RemoteError) as exc:
            message = str(exc)  # user-facing text, free of URLs and keys
            log.warning("Download queue: %s, the grab failed: %s", item.label, message)
            self._fail(entry, message)
            return
        except asyncio.CancelledError:
            self._running.pop(key, None)
            if self._file is not None and not self._closed:
                # A quit cancels every task (main.py's cancel_all) before Services.aclose:
                # back to the front, so close() keeps it for the next start.
                self._waiting.appendleft(entry)
            log.info("Download queue: %s stopped before it finished", item.label)
            raise
        except Exception as exc:
            # Where it went wrong, but never the error's text: it may hold a URL or a release.
            error = type(exc).__name__
            where = "".join(traceback.format_tb(exc.__traceback__))
            log.error("Download queue: %s failed unexpectedly (%s)\n%s", item.label, error, where)
            text = t("Something went wrong with {label} ({error})", label=item.label, error=error)
            self._fail(entry, text)
            return
        self._running.pop(key, None)
        if result.already_downloaded or result.grab is not None:
            self._emit(QueueEvent(key=key, label=item.label, state=QueueState.DONE, result=result))
            return
        fallback: tuple[QueueKey, ...] = ()
        if item.fallback:
            log.info(
                "Download queue: %s, no season pack found; queuing its %d episode(s) instead",
                item.label,
                len(item.fallback),
            )
            self.add_batch(item.fallback)
            fallback = tuple(episode.key for episode in item.fallback)
        nothing = QueueState.NOTHING
        self._emit(
            QueueEvent(key=key, label=item.label, state=nothing, result=result, fallback=fallback)
        )

    async def _download(self, item: QueuedDownload) -> QuickDownloadResult:
        source = item.request
        request = source if isinstance(source, SearchRequest) else await source()
        if item.avoid:
            return await self._quick.download(
                request, item.target, again=item.again, avoid=item.avoid
            )
        return await self._quick.download(request, item.target, again=item.again)

    def _fail(self, entry: _Entry, message: str) -> None:
        item = entry.item
        self._running.pop(item.key, None)
        self._emit(
            QueueEvent(key=item.key, label=item.label, state=QueueState.FAILED, message=message)
        )
        if entry.batch is None:
            return
        dropped = [other for other in self._waiting if other.batch == entry.batch]
        for other in dropped:
            self._waiting.remove(other)
        if dropped:
            log.info(
                "Download queue: dropped the %d other waiting download(s) of its batch",
                len(dropped),
            )
        for other in dropped:
            self._emit(
                QueueEvent(key=other.item.key, label=other.item.label, state=QueueState.DROPPED)
            )

    # -- the file ----------------------------------------------------------------------------

    def _changed(self) -> None:
        """What waits or runs changed: write it to the file soon (one write at a time; the
        last state wins)."""
        if self._file is None or not self._restored:
            return
        self._dirty = True
        if self._saver is not None and not self._saver.done():
            return  # the running writer writes again
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no loop any more (a test's cleanup): write it here
            self._dirty = False
            _write(self._file, self._text())
            return
        self._saver = loop.create_task(self._save(), name="download queue file")

    async def _save(self) -> None:
        file = self._file
        assert file is not None
        while self._dirty:
            self._dirty = False
            text = self._text()
            if not await asyncio.to_thread(_write, file, text):
                log.warning("Download queue: what waits couldn't be kept for the next start")

    def _text(self) -> str | None:
        """The file's text for what waits and runs now (running first); None: nothing."""
        entries = (
            self._final
            if self._final is not None
            else (*self._running.values(), *self._waiting)
        )
        stored: list[_StoredEntry] = []
        for entry in entries:
            kept = _stored(entry.item)
            if kept is not None:
                stored.append(
                    _StoredEntry(**dict(kept), batch=entry.batch, queued_at=entry.queued_at)
                )
        if not stored:
            return None
        content = _QueueFile(version=QUEUE_FILE_VERSION, entries=tuple(stored))
        return json.dumps(content.model_dump(mode="json"), ensure_ascii=False, indent=1)

    def _emit(self, event: QueueEvent) -> None:
        self._changed()
        self._events[event.key] = event
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                log.exception("Download queue listener %r failed", listener)


def _read(file: Path) -> str | None:
    """The file's text; None when there is none. Blocking."""
    try:
        return file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def _write(file: Path, text: str | None) -> bool:
    """Write `text` to `file` atomically (None: remove it); False when it can't be. Blocking."""
    if text is None:
        return _remove(file)
    spare: Path | None = None
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        # Its own name: a write cancelled at a quit may still run on its thread.
        handle, name = tempfile.mkstemp(dir=file.parent, prefix=file.name + ".", suffix=".tmp")
        spare = Path(name)
        with os.fdopen(handle, "w", encoding="utf-8") as out:
            out.write(text)
        os.replace(spare, file)
    except OSError:
        if spare is not None:
            try:
                spare.unlink(missing_ok=True)
            except OSError:
                pass
        return False
    return True


def _remove(file: Path) -> bool:
    """Remove `file` if it is there; False when it can't be. Blocking."""
    try:
        file.unlink(missing_ok=True)
    except OSError:
        return False
    return True


def _set_aside(file: Path) -> bool:
    """Rename an unreadable `file` to `<name>.bad` (an older one replaced). Blocking."""
    try:
        os.replace(file, file.with_name(file.name + ".bad"))
    except OSError:
        return False
    return True
