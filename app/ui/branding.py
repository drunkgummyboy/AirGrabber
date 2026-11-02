"""The AirGrabber logo and window icon. Both ship in app/assets, so an update brings new artwork.

Window icons are read from their .ico once, never per window: Tk on Windows doesn't give back
an icon it read from a file (`wm iconbitmap <file>`: about 21 GDI and 7 USER objects for
airgrabber.ico, every time, even after the window closed), and a process gets 10,000 of each,
so a long session that opened window after window ended in Tk's "Fail to allocate bitmap".
AirGrabber's icon is the root's default for every window (read once per root); a window with
an icon of its own (MediaForge's) gets it from Tk images made once per root
(`wm iconphoto`, whose icons go with the window)."""

from __future__ import annotations

import functools
import logging
import sys
import tkinter as tk
import weakref
from pathlib import Path

import customtkinter as ctk
from PIL import Image, ImageTk

log = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
LOGO_FILE = ASSETS_DIR / "logo.png"
ICON_FILE = ASSETS_DIR / "airgrabber.ico"  # keep in step with install.ps1 (shortcut icon)
OWN_ICON_MAX = 64  # the largest size Windows shows a window's icon at (taskbar, 200 %)

# Roots whose default icon is AirGrabber's; per root, the Tk images of each icon of its own.
_defaulted: weakref.WeakSet[tk.Misc] = weakref.WeakSet()
_own_images: weakref.WeakKeyDictionary[tk.Misc, dict[Path, list[ImageTk.PhotoImage]]] = (
    weakref.WeakKeyDictionary()
)


def set_window_icon(window: ctk.CTk | ctk.CTkToplevel) -> None:
    """Our icon in the title bar and taskbar. Call it from the window's __init__: customtkinter
    applies its own icon 200 ms after a window opens unless iconbitmap() was called first.
    The root (MainWindow) reads the .ico once, as the default of all its windows; any other
    window takes that default (an empty `wm iconbitmap`: nothing read)."""
    if sys.platform != "win32" or not ICON_FILE.is_file():
        return
    try:
        root = window.nametowidget(".")
        if root not in _defaulted:
            _defaulted.add(root)
            root.iconbitmap(default=str(ICON_FILE))
        if window is not root:
            window.iconbitmap("")
    except tk.TclError as exc:
        log.debug("Could not set the window icon: %r", exc)


def set_own_icon(window: ctk.CTk | ctk.CTkToplevel, icon: Path) -> None:
    """`icon` (an .ico) in the window's title bar and taskbar instead of AirGrabber's
    (MediaForge's window). Again and again is fine: the .ico is read once, its Tk images made
    once per root. From the window's __init__ it also stops customtkinter's own icon."""
    if sys.platform != "win32" or not icon.is_file():
        return
    try:
        window.iconbitmap("")  # nothing read; customtkinter leaves the icon alone from now on
        root = window.nametowidget(".")
        per_root = _own_images.setdefault(root, {})
        images = per_root.get(icon)
        if images is None:
            images = per_root[icon] = [
                ImageTk.PhotoImage(frame, master=root) for frame in _icon_frames(icon)
            ]
        if images:
            window.wm_iconphoto(False, *(str(image) for image in images))
    except tk.TclError as exc:
        log.debug("Could not set the window's own icon: %r", exc)


@functools.lru_cache(maxsize=4)
def _icon_frames(icon: Path) -> tuple[Image.Image, ...]:
    """The pictures in `icon` up to OWN_ICON_MAX pixels (the smallest one if all are larger),
    once per run: a few small images, about a millisecond."""
    try:
        with Image.open(icon) as image:
            sizes = sorted(image.info.get("sizes") or {image.size})
            wanted = [size for size in sizes if max(size) <= OWN_ICON_MAX] or sizes[:1]
            frames: list[Image.Image] = []
            for size in wanted:
                image.size = size  # type: ignore[misc]  # picks the .ico's picture of that size
                image.load()
                frames.append(image.convert("RGBA"))
            return tuple(frames)
    except (OSError, ValueError) as exc:
        log.warning("Could not read the icon %s: %r", icon.name, exc)
        return ()


def load_logo(height: int) -> Image.Image | None:
    """The logo resized to `height` pixels tall. Pillow work: call through asyncio.to_thread."""
    try:
        with Image.open(LOGO_FILE) as image:
            image.load()
            width = max(1, round(image.width * height / image.height))
            return image.convert("RGBA").resize((width, height), Image.Resampling.LANCZOS)
    except OSError as exc:
        log.warning("Could not load the logo: %r", exc)
        return None
