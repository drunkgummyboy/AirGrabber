"""What MediaForge's runs are doing now, for the dock on every page of the main window (the owner,
2026-11-16: "display the same loading bar from the forge tab on every page when something is
going").

`RunProgress` (`ForgeServices.run_progress`) is the one place that knows: the page's run (its
queue view reports what its own dock shows, with its Cancel), the renamer's (an automatic run, or
the phone's tap: a run whose caller shows no progress of its own) and a right-click confirm
window's report here, each under its own `source`, and `idle(source)` at their end, whatever
happened. The newest report wins; idle comes only once no run is left. It is kept outside the
page because the page is built at its first visit, and a run may come before that.

`RunSteps` turns a run's `ProgressEvent`s into the dock's words and fraction, as the page's dock
has them ("Renaming 2 of 5 · Dune", a copy's "Copying 4.2 of 20 GB · Dune", a trailer's step).
Those words may name a file or a title: they are shown, never logged.

No tkinter here: app/forge/ui/run_dock.py shows it, on the main window's loop.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, NamedTuple

from app.i18n import t

if TYPE_CHECKING:
    from app.forge.core.executor import ProgressEvent
    from app.forge.core.queue import QueueItem

log = logging.getLogger(__name__)

PAGE = "page"  # MediaForge's page (its queue view)
AUTO = "renamer"  # the automatic renamer's runs (and the phone's tap)


class RunState(NamedTuple):
    running: bool
    text: str = ""  # translated; may name a file: never logged
    fraction: float = 0.0
    cancel: Callable[[], None] | None = None  # stops the run, as its dock's Cancel does
    source: str = ""


IDLE = RunState(False)

RunListener = Callable[[RunState], None]


def item_name(item: QueueItem) -> str:
    """How the dock names a file: the episode's or the film's title."""
    media = item.media
    return media.episode_title or media.title or item.path.name


class RunSteps:
    """One run's dock: `heard` each step (with the file's name, None when it has none), `now()`
    gives the words and the fraction."""

    def __init__(self, total: int = 0) -> None:
        self.total = total
        self.done: set[int] = set()
        self.name = ""
        self.step: tuple[str, float] | None = None  # after the renames: a trailer download
        self.copy: tuple[str, float] | None = None  # a file's copy between shares

    def heard(self, event: ProgressEvent, name: str | None) -> None:
        if event.item_id is not None and not event.copying:
            self.copy = None  # a file's own step: its copy (if any) is over
            if event.status == "working":
                self.step = None  # and a trailer step before it (the plan before's) too
        if event.copying:
            self.copy = (event.message, event.fraction)
        elif event.item_id is None and event.status == "working":
            self.step = (event.message, event.fraction)  # already in the interface language
        elif event.status == "working" and name is not None:
            self.name = name
        elif event.item_id is not None:
            self.done.add(event.item_id)

    def now(self) -> tuple[str, float]:
        if self.step is not None:
            return self.step
        if self.copy is not None:  # "Copying 4.2 of 20 GB · Dune (2021)"
            text, fraction = self.copy
            return (f"{text} · {self.name}" if self.name else text), fraction
        total = max(self.total, 1)
        done = len(self.done)
        n = min(done + 1, total)
        if self.name:
            text = t("Renaming {n} of {total} · {name}", n=n, total=total, name=self.name)
        else:
            text = t("Renaming {n} of {total}", n=n, total=total)
        return text, done / total


class RunProgress:
    """Whether MediaForge runs, and what its dock says (see the module's notes)."""

    def __init__(self) -> None:
        self._runs: dict[str, RunState] = {}  # by source, the newest report last
        self._listeners: list[RunListener] = []
        self.state = IDLE

    def busy(self, source: str) -> bool:
        """Whether the run of `source` is under way."""
        return source in self._runs

    def subscribe(self, listener: RunListener) -> Callable[[], None]:
        """Call `listener` with every change of `state`. Returns a function that unsubscribes."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(listener)

        return unsubscribe

    def report(
        self,
        text: str,
        fraction: float,
        *,
        source: str,
        cancel: Callable[[], None] | None = None,
    ) -> None:
        """The run of `source` is at `text` (translated), `fraction` of the way."""
        state = RunState(True, text, fraction, cancel, source)
        self._runs.pop(source, None)
        self._runs[source] = state
        self._tell(state)

    def idle(self, source: str) -> None:
        """The run of `source` has ended (done, failed or cancelled)."""
        if self._runs.pop(source, None) is None:
            return
        self._tell(next(reversed(self._runs.values())) if self._runs else IDLE)

    def follow(
        self, source: str, total: int, name_of: Callable[[int], str | None]
    ) -> Callable[[ProgressEvent], None]:
        """Report a run of `total` files now, and each of its steps through the returned
        callback (`name_of`: a file's name by its queue id, None when it has none). The caller
        calls `idle(source)` at its end."""
        steps = RunSteps(total)
        self.report(*steps.now(), source=source)

        def heard(event: ProgressEvent) -> None:
            name = name_of(event.item_id) if event.item_id is not None else None
            steps.heard(event, name)
            self.report(*steps.now(), source=source)

        return heard

    def close(self) -> None:
        self._listeners.clear()
        self._runs.clear()
        self.state = IDLE

    def _tell(self, state: RunState) -> None:
        if state == self.state:
            return
        self.state = state
        for listener in list(self._listeners):
            try:
                listener(state)
            except Exception:
                log.exception("A run progress listener failed")
