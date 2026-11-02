"""Drive Tk and asyncio together in Forge's tests, like the real main loop does (ported from
MediaForge's tests/helpers/tk.py)."""

from __future__ import annotations

import asyncio
import time
import tkinter as tk
from collections.abc import Callable


async def pump(widget: tk.Misc, seconds: float = 0.2) -> None:
    """Let Tk and the loop run for `seconds`."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        widget.update()
        await asyncio.sleep(0.01)


async def pump_until(widget: tk.Misc, condition: Callable[[], object], within: float = 5.0) -> None:
    """Let Tk and the loop run until `condition()` is true; TimeoutError after `within` seconds."""
    deadline = time.monotonic() + within
    while not condition():
        if time.monotonic() > deadline:
            raise TimeoutError("condition not reached")
        widget.update()
        await asyncio.sleep(0.01)


def press(widget: tk.Misc, key: str) -> None:
    """Press `key` (a sequence such as "<Escape>") in `widget`. Tk hands a key only to the
    window with the keyboard focus and drops it while another window has that (another
    program's, or what the rest of a long test run left on screen), so `widget` takes the focus
    first, as the window being typed into has it, with nothing run in between to take it away."""
    widget.focus_force()
    widget.event_generate(key)
