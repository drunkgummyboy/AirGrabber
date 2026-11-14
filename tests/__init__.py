"""AirGrabber's tests (stdlib unittest: python -m unittest discover -s tests -t .).

No test changes a real window's frame: the pop-ups made borderless (app/ui/borderless.py) get
their close button and drag bindings, but the Win32 / DWM side is a stand-in that does
nothing (`InertNative`), so every window keeps the title bar Tk gave it. Likewise no test hooks a
real window for Explorer drops (app/forge/ui/file_drops.py) unless it asks for the real one.
No test starts a video player: ▶ Play's real starts (app/services/playback.py's `_start_process` /
`_open_with_windows`) raise; tests give PlaybackService a fake `launch` or patch those two. The
same for AirGrabber's own player (app/ui/media_player.py's `start_player_process`): tests give
MediaPlayerHost a fake `starter`.

No window reads its icon from an .ico file either (`wm iconbitmap`). The app reads each icon
once per root (app/ui/branding.py), but every test makes a root of its own, customtkinter reads
its own icon file for each one 200 ms after it opens, and Tk on Windows never gives back an
icon it read from a file (about 21 GDI and 7 USER objects for app/assets/airgrabber.ico), while
Windows allows a process 10,000 of each. `REAL_ICONBITMAP` is Tk's own, for a test that needs it.

customtkinter remembers every window it has seen in class-wide tables (ScalingTracker's,
AppearanceModeTracker's apps) and never forgets one. The app forgets each window it closes
(`install_window_release` in app/ui/widgets.py, from MainWindow), but a test's root is a
window too: a destroyed root leaves the tables together with every window of its interpreter
(`forget_root`). Otherwise each test's dead root, holding its Tcl interpreter and that
interpreter's GDI objects, stays until the run ends, and a whole run ran out of them ("Fail to
allocate bitmap")."""

from __future__ import annotations

import tkinter as tk
from typing import Any

import customtkinter as ctk

from app.forge.ui import file_drops
from app.services import open_with, playback
from app.ui import borderless, media_player


class InertNative:
    """Borderless windows' Win32 / DWM side, doing nothing."""

    def attach(self, window_id: int, *, dark: bool) -> bool:
        return True

    def start_drag(self, window_id: int) -> None:
        pass

    def set_dark(self, window_id: int, dark: bool) -> None:
        pass


borderless.set_native(InertNative())
file_drops.set_native(file_drops.InertNative())


def _refuse_player(*_args: Any, **_kwargs: Any) -> None:
    raise AssertionError("tests must never start a video player: fake PlaybackService's launch")


playback._start_process = _refuse_player
playback._open_with_windows = _refuse_player


async def _refuse_media_player(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError("tests must never start AirGrabber's player: give MediaPlayerHost a starter")


media_player.start_player_process = _refuse_media_player


def _refuse_open_with(*_args: Any, **_kwargs: Any) -> None:
    raise AssertionError("tests must never change Explorer's Open with: give open_with a registry")


# Explorer's "Open with" AirGrabber player (app/services/open_with.py): the real registry's
# writers raise and its reads find nothing, whatever a test forgets to fake.
open_with._write_key = _refuse_open_with
open_with._delete_tree = _refuse_open_with
open_with._delete_value = _refuse_open_with
open_with._assoc_changed = _refuse_open_with
open_with._read_value = lambda key, name: None


async def _no_open_with(on: bool) -> None:
    """MainWindow's start and Settings' tick: nothing is registered in tests."""


open_with.apply = _no_open_with


REAL_ICONBITMAP = tk.Wm.wm_iconbitmap


def _iconbitmap_unread(self: tk.Wm, bitmap: str | None = None, default: str | None = None) -> Any:
    """`wm iconbitmap` that never reads an .ico file: windows keep Tk's own icon in tests."""
    if bitmap is None and default is None:
        return REAL_ICONBITMAP(self)  # type: ignore[no-untyped-call]  # asking which one it is
    return ""


tk.Wm.wm_iconbitmap = tk.Wm.iconbitmap = _iconbitmap_unread  # type: ignore[method-assign]


def forget_root(root: tk.Tk) -> None:
    """Drop a destroyed root and every window of its interpreter from customtkinter's
    class-wide tables, so they (and the interpreter) can be freed."""
    interp = root.tk
    tracker = ctk.ScalingTracker
    for table in (tracker.window_widgets_dict, tracker.window_dpi_scaling_dict):
        for window in [window for window in table if window.tk is interp]:
            del table[window]
    apps: list[Any] = ctk.AppearanceModeTracker.app_list
    apps[:] = [app for app in apps if app.tk is not interp]


_tk_destroy = tk.Tk.destroy


def _destroy_and_forget(self: tk.Tk) -> None:
    """Tk's destroy, then `forget_root` (also when it failed); CTk's destroy() calls it too."""
    try:
        _tk_destroy(self)
    finally:
        forget_root(self)


tk.Tk.destroy = _destroy_and_forget  # type: ignore[method-assign]
