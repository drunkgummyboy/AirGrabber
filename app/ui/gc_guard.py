"""Keep Python's cycle collector on the Tk thread (D6, adopted 2026-11-08; ported from MediaForge).

Python runs its cycle collector on whichever thread happens to allocate when the counts run over,
so also on aiosqlite's database thread or a `to_thread` worker. It then frees Tk fonts and images
there, and their `__del__` calls into Tk from the wrong thread. That call waits for the Tk thread,
which never answers it: on 2026-11-08 the database thread hung for good in `Font.__del__`, and
every search, grab and page that needed the database waited with it (the owner: "AirGrabber is
kind of freezing"). The soaks of F-28 had never caught one.

So automatic collection is off and the Tk thread collects on a timer: the young generation often
(cheap, as Python itself would), the older ones rarely. Objects alive at the start are frozen so a
full collection stays quick. Plain reference counting is untouched: what isn't in a cycle is freed
where it is dropped, as before.
"""

from __future__ import annotations

import gc
import tkinter as tk
from typing import Any

TICK_MS = 100
MIDDLE_EVERY = 20  # ticks: about every 2 s
FULL_EVERY = 300  # ticks: about every 30 s


def start(root: Any, tick_ms: int = TICK_MS) -> None:
    """Collect on `root`'s timer from now on, never elsewhere. Stops with `root`."""
    gc.collect()
    gc.freeze()
    gc.disable()
    ticks = 0

    def tick() -> None:
        nonlocal ticks
        ticks += 1
        if ticks % FULL_EVERY == 0:
            gc.collect(2)
        elif ticks % MIDDLE_EVERY == 0:
            gc.collect(1)
        else:
            gc.collect(0)
        try:
            if root.winfo_exists():
                root.after(tick_ms, tick)
        except tk.TclError:  # the window is gone
            pass

    root.after(tick_ms, tick)


def stop() -> None:
    """Back to Python's own collecting (at exit)."""
    gc.unfreeze()
    gc.collect()
    gc.enable()
