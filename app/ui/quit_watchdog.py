"""Quitting always ends AirGrabber (owner, 2026-11-09: "right click quit does not work").

Once quitting really starts (MediaForge has finished its current file: `QuitGate`), `arm` starts
a timer. If AirGrabber is still closing when it runs out, the timer logs where every thread was
stuck and ends the process. Before, a database thread hung by the cycle collector (see
`gc_guard`) held closing up for good: Windows then said "not responding", and a new start
handed its request to the stuck copy and opened nothing.

Its thread is the one other exception to "no threads" besides the tray icon: it never touches
Tk or the asyncio loop (those may be the very thing that is stuck), it only logs and exits.
SQLite's journal keeps the databases whole; a downloaded update waits for the next close.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import traceback
from collections.abc import Callable

log = logging.getLogger(__name__)

QUIT_LIMIT_S = 60.0  # MediaForge's and the window's steps take 5 s each at most; closing is ~1 s
EXIT_CODE = 3

_timer: threading.Timer | None = None


def arm(limit_s: float = QUIT_LIMIT_S, *, end: Callable[[int], object] = os._exit) -> None:
    """From now on AirGrabber ends within `limit_s`, closed or not. Once (later calls do
    nothing until `disarm`)."""
    global _timer
    if _timer is not None:
        return
    timer = threading.Timer(limit_s, _expire, args=(limit_s, end))
    timer.name = "quit-watchdog"
    timer.daemon = True
    _timer = timer
    timer.start()


def disarm() -> None:
    """Closing got far enough (the update install that follows has its own safety)."""
    global _timer
    timer, _timer = _timer, None
    if timer is not None:
        timer.cancel()


def armed() -> bool:
    return _timer is not None


def _expire(limit_s: float, end: Callable[[int], object]) -> None:
    log.error(
        "AirGrabber didn't finish closing within %.0f s: ending it now. Where it was:\n%s",
        limit_s,
        thread_stacks(),
    )
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:  # noqa: BLE001 - ending matters more than the log
            pass
    end(EXIT_CODE)


def thread_stacks() -> str:
    """Every thread's stack (code locations only), for the log."""
    names = {thread.ident: thread.name for thread in threading.enumerate()}
    parts = []
    for ident, frame in sys._current_frames().items():
        if ident == threading.get_ident():
            continue  # this watchdog
        stack = "".join(traceback.format_stack(frame))
        parts.append(f"Thread {names.get(ident, ident)}:\n{stack}")
    return "\n".join(parts)
