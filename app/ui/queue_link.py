"""The download queue as the buttons see it (3.0.1).

The one-click downloads run in the app's download queue (app/services/download_queue.py),
which no window owns. `QueueLink` (owned by the main window) hands it the downloads the
buttons ask for and mirrors its events into the main window's DownloadStates, so every Download
button, row and stack card of a title shows where it is, also one opened later: "Queued" while
it waits (`Phase.QUEUED`), the working label while it searches or sends, then the outcome as
before ("✓ Sent" with what it picked, amber "No good release found", or back to ⬇ with the
reason in the status bar when the grab failed). A title found in the download history already
goes back to its own look too (no "✓ Grabbed": since 3.1.0 only Plex says what you have); the
status bar says it was downloaded before. The status bar hears each outcome as it did.

A batch (a calendar stack's "Download all", the show window's "All missing", My shows'
"Download missing", and the episodes a season pack queued instead when none was found) keeps
its progress in DownloadStates as well (`BatchState`: "Queued" until one of them starts, then
"Downloading 2 of 3…"); whoever queued it may follow it too (`on_progress`, and `on_done` with
each title's last event: "Download missing" builds its run's report from them).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.i18n import N_, t
from app.models import QueueEvent, QueueState
from app.services.download_queue import DownloadQueue, QueuedDownload
from app.ui.download_control import (
    BatchKey,
    BatchState,
    DownloadKey,
    DownloadStates,
    Phase,
    RunState,
    already_downloaded_text,
    download_status_text,
    grabbed_state,
    nothing_found_text,
)

# Shown through t().
NO_PACK_TEXT = N_("No season pack found; the episodes are queued.")

Report = Callable[[str], None]  # a text for the status bar
BatchProgress = Callable[[BatchState], None]
BatchDone = Callable[[tuple[QueueEvent, ...]], None]  # each title's last event, in order


@dataclass(frozen=True, slots=True)
class _Batch:
    keys: tuple[DownloadKey, ...]
    on_progress: BatchProgress | None = None
    on_done: BatchDone | None = None


class QueueLink:
    """Queues the buttons' downloads and shows the queue's events on them."""

    def __init__(self, queue: DownloadQueue, states: DownloadStates, report: Report) -> None:
        self._queue = queue
        self._states = states
        self._report = report
        self._batches: dict[BatchKey, _Batch] = {}
        self._unsubscribe = queue.subscribe(self._on_event)

    def download(self, item: QueuedDownload) -> None:
        """Queue one download (nothing happens when its title is queued already)."""
        self._queue.add(item)

    def download_batch(
        self,
        batch: BatchKey,
        items: Sequence[QueuedDownload],
        *,
        on_progress: BatchProgress | None = None,
        on_done: BatchDone | None = None,
    ) -> bool:
        """Queue `items` as one batch whose progress shows under `batch`'s key (a calendar
        stack's titles) and goes to `on_progress`; once none of them is left in the queue,
        `on_done` hears each one's last event. False (nothing queued, `on_done` not called)
        while that batch is queued already; with nothing to queue `on_done` hears () at once."""
        if batch in self._batches:
            return False
        self._queue.add_batch(items)
        if not self._follow(batch, tuple(item.key for item in items), on_progress, on_done):
            if on_done is not None:
                on_done(())
        return True

    def close(self) -> None:
        """Stop listening (the main window is closing)."""
        self._unsubscribe()

    def _follow(
        self,
        batch: BatchKey,
        keys: tuple[DownloadKey, ...],
        on_progress: BatchProgress | None = None,
        on_done: BatchDone | None = None,
    ) -> bool:
        active = tuple(key for key in keys if self._queue.is_active(key))
        if not active:
            return False
        self._batches[batch] = _Batch(active, on_progress, on_done)
        self._paint_batch(batch)
        return True

    def _on_event(self, event: QueueEvent) -> None:
        key, state, result = event.key, event.state, event.result
        states = self._states
        if state is QueueState.WAITING:
            states.set(key, RunState(Phase.QUEUED))
        elif state is QueueState.RUNNING:
            states.set(key, RunState(Phase.WORKING))
        elif state is QueueState.DONE and result is not None and result.grab is not None:
            states.set(key, grabbed_state(result))
            self._report(download_status_text(event.label, result))
        elif state is QueueState.DONE:  # downloaded already, e.g. meanwhile from elsewhere
            states.set(key, None)
            self._report(already_downloaded_text(event.label))
        elif state is QueueState.NOTHING and event.fallback:
            states.set(key, None)  # the season's control follows its episodes' batch now
            self._follow(event.fallback, event.fallback)
            self._report(t(NO_PACK_TEXT))
        elif state is QueueState.NOTHING:
            states.set(key, RunState(Phase.NOTHING))
            self._report(nothing_found_text(event.label))
        elif state is QueueState.FAILED:
            states.set(key, None)
            self._report(event.message)
        else:  # DROPPED: back to its own look
            states.set(key, None)
        for batch, followed in list(self._batches.items()):
            if key in followed.keys:
                self._paint_batch(batch)

    def _paint_batch(self, batch: BatchKey) -> None:
        """The batch's progress: "Queued" until one starts, then which one it fetches; gone
        once none is left in the queue."""
        followed = self._batches[batch]
        keys = followed.keys
        found = [self._queue.state(key) for key in keys]
        finished = sum(state is not None and not state.active for state in found)
        if finished >= len(keys):
            del self._batches[batch]
            self._states.set_batch(batch, None)
            if followed.on_done is not None:
                events = (self._queue.event(key) for key in keys)
                followed.on_done(tuple(event for event in events if event is not None))
            return
        started = finished or any(state is QueueState.RUNNING for state in found)
        state = BatchState(finished + 1, len(keys), queued=not started)
        self._states.set_batch(batch, state)
        if followed.on_progress is not None:
            followed.on_progress(state)
