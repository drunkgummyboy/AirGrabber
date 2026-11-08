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

The queue is kept in memory only: at shutdown `close()` drops what is still waiting, and the
log says how many. The automatic downloads (app/services/auto_download.py) keep their own
run, one download at a time.

Log hygiene: only labels ("Severance S02E01"), counts and outcomes are logged, never a release,
a link or a URL.
"""

from __future__ import annotations

import asyncio
import itertools
import logging
import traceback
from collections import deque
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

from app.i18n import t
from app.models import (
    GrabTarget,
    QueuedItem,
    QueueEvent,
    QueueKey,
    QueueState,
    QuickDownloadResult,
    SearchRequest,
)
from app.services.downloads import GrabError
from app.services.quick_download import QuickDownloadService
from app.services.remote import RemoteError

log = logging.getLogger(__name__)

WORKERS = 2  # one-click downloads searching at the same time

# What a queued download searches for, or a coroutine function that makes it when its turn
# comes (a film's request waits for its TMDB details).
RequestSource = SearchRequest | Callable[[], Awaitable[SearchRequest]]
QueueListener = Callable[[QueueEvent], None]


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


class DownloadQueue:
    """The app's one-click downloads, WORKERS at a time (`services.download_queue`)."""

    def __init__(self, quick_download: QuickDownloadService, *, workers: int = WORKERS) -> None:
        if workers < 1:
            raise ValueError("workers must be at least 1")
        self._quick = quick_download
        self._limit = workers
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
        """Shutdown: drop the downloads still waiting (logged: how many) and stop the
        running ones. Returns how many were dropped."""
        self._closed = True
        dropped = len(self._waiting)
        self._waiting.clear()
        for task in list(self._workers):
            task.cancel()
        log.info("Download queue closed: %d waiting download(s) dropped", dropped)
        return dropped

    def _add(self, item: QueuedDownload, batch: int | None) -> bool:
        if self._closed or self.is_active(item.key):
            return False
        self._waiting.append(_Entry(item, batch))
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

    def _emit(self, event: QueueEvent) -> None:
        self._events[event.key] = event
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception:
                log.exception("Download queue listener %r failed", listener)
