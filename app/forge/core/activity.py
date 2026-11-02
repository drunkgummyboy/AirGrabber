"""The Activity strip's lines: what Forge did, in the interface language.

MediaForge fed its Activity strip from the log (`UiLogBridge`, a logging handler). AirGrabber's
log stays English (CLAUDE.md), so the strip can't be a log handler any more: the places that
logged for it now also `say(t(...))` here, next to their English log line.

Everything runs on the one asyncio loop: `say` is called from coroutines and listeners are
called right away, on the loop (the Tk loop), so a view can update its widgets directly.
"""

from __future__ import annotations

import contextlib
import logging
from collections import deque
from collections.abc import Callable
from typing import Protocol

from pydantic import BaseModel

log = logging.getLogger(__name__)

MAX_LINES = 1000  # the strip kept as many (MediaForge's log_strip.MAX_LINES)


class ActivityLine(BaseModel, frozen=True):
    """One line: its text (translated), a logging level and whether it reports a success."""

    level: int = logging.INFO
    text: str
    success: bool = False


ActivityListener = Callable[[ActivityLine], None]


class Say(Protocol):
    """What the core takes to report activity: `ActivityFeed.say`, or `quiet`."""

    def __call__(self, text: str, *, level: int = ..., success: bool = ...) -> None: ...


def quiet(text: str, *, level: int = logging.INFO, success: bool = False) -> None:
    """A `Say` that drops every line (for code built without a feed, such as tests)."""


def say_to(feed: ActivityFeed | None) -> Say:
    """`feed.say`, or `quiet` when there is no feed."""
    return feed.say if feed is not None else quiet


class ActivityFeed:
    """A bounded buffer of activity lines with listeners."""

    def __init__(self, max_lines: int = MAX_LINES) -> None:
        self._lines: deque[ActivityLine] = deque(maxlen=max_lines)
        self._listeners: list[ActivityListener] = []

    def say(self, text: str, *, level: int = logging.INFO, success: bool = False) -> None:
        """Add a line (`text` already translated) and hand it to every listener."""
        line = ActivityLine(level=level, text=text, success=success)
        self._lines.append(line)
        for listener in list(self._listeners):
            try:
                listener(line)
            except Exception:
                log.exception("Activity listener %r failed", listener)

    def lines(self) -> tuple[ActivityLine, ...]:
        """The lines kept so far, oldest first (a view opened later shows these first)."""
        return tuple(self._lines)

    def subscribe(self, listener: ActivityListener) -> Callable[[], None]:
        """Call `listener` with every new line. Returns a function that unsubscribes it."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(listener)

        return unsubscribe

    def close(self) -> None:
        """Forget every listener (Forge is shutting down)."""
        self._listeners.clear()
