"""The AirGrabber logo and window icon. Both ship in app/assets, so an update brings new artwork."""

from __future__ import annotations

import logging
import sys
import tkinter as tk
from pathlib import Path

import customtkinter as ctk
from PIL import Image

log = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).resolve().parent.parent / "assets"
LOGO_FILE = ASSETS_DIR / "logo.png"
ICON_FILE = ASSETS_DIR / "airgrabber.ico"  # keep in step with install.ps1 (shortcut icon)


def set_window_icon(window: ctk.CTk | ctk.CTkToplevel) -> None:
    """Our icon in the title bar and taskbar. Call it from the window's __init__: customtkinter
    applies its own icon 200 ms after a window opens unless iconbitmap() was called first."""
    if sys.platform != "win32" or not ICON_FILE.is_file():
        return
    try:
        window.iconbitmap(str(ICON_FILE))
    except tk.TclError as exc:
        log.debug("Could not set the window icon: %r", exc)


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
