"""A window opened from another secondary window (a search result's info window) comes to the
front and gets the focus.

Two things kept such a window behind the one it was opened from. CTkToplevel hides a new window
while it recolours its title bar and shows it again a little later, then hands the focus back
to the widget that had it (in the window the click came from); a window that raises itself a
fixed time after it opens (the info window's own `_activate`) does that while it is still
hidden when building it took longer, and Windows shows it again behind. And CTk binds every
click to "focus the clicked widget" (`bind_all`), which runs after the click's own handler: the
window the click was in takes the focus back from a window that was raised there.

`open_in_front` runs the opening and then waits until the window shows, plus a moment for
CTk's focus hand-back, before it raises and focuses it.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

Window = tk.Tk | tk.Toplevel

SETTLE_MS = 120  # after the window shows: past CTk's re-show (5 ms) and focus hand-back (10 ms)
POLL_MS = 50  # how often a hidden window is looked at again
POLL_TRIES = 80  # ~4 s: a window that hasn't shown by then is left alone


def open_in_front(opener: tk.Misc, open_window: Callable[[], object]) -> None:
    """Run `open_window` (it opens a window, or raises one already open) from the secondary
    window `opener`, then bring that window to the front: a new toplevel of the application,
    else the one that took the focus. Nothing more when neither is found."""
    root = opener.winfo_toplevel().master or opener.winfo_toplevel()
    before = {str(window) for window in _toplevels(root)}
    open_window()
    new = [window for window in _toplevels(root) if str(window) not in before]
    target: Window | None = new[-1] if new else _focused_window(root, opener)
    if target is not None:
        bring_to_front(target)


def bring_to_front(window: Window) -> None:
    """Raise and focus `window` once it shows (and CTk has settled), polling while it is
    hidden; never if it goes away meanwhile."""

    def look(tries_left: int) -> None:
        if not _alive(window):
            return
        if _showing(window):
            window.after(SETTLE_MS, raise_now)
        elif tries_left > 0:
            window.after(POLL_MS, lambda: look(tries_left - 1))

    def raise_now() -> None:
        if _alive(window) and _showing(window):
            window.lift()
            window.focus_force()

    window.after_idle(lambda: look(POLL_TRIES))


def _toplevels(root: tk.Misc) -> list[tk.Toplevel]:
    try:
        return [child for child in root.winfo_children() if isinstance(child, tk.Toplevel)]
    except tk.TclError:
        return []


def _focused_window(root: tk.Misc, opener: tk.Misc) -> Window | None:
    """The toplevel that has the focus, unless it is `opener`'s (or the main window)."""
    try:
        focused = root.focus_get()
    except (tk.TclError, KeyError):  # KeyError: focus on a widget tkinter doesn't know
        return None
    if focused is None:
        return None
    window = focused.winfo_toplevel()
    if str(window) in (str(opener.winfo_toplevel()), str(root)):
        return None
    return window


def _showing(window: Window) -> bool:
    try:
        return str(window.wm_state()) == "normal" and bool(window.winfo_viewable())
    except tk.TclError:
        return False


def _alive(window: tk.Misc) -> bool:
    try:
        return bool(window.winfo_exists())
    except tk.TclError:
        return False
