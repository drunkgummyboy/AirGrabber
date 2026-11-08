"""The tray icon: AirGrabber keeps running (and downloading) with its window closed.

TrayIcon wraps pystray. pystray needs a Win32 message loop of its own, so it runs on a daemon
thread: the one exception to "no threads" (CLAUDE.md). Its menu callbacks only hand work to the
asyncio loop with `loop.call_soon_threadsafe`; nothing on that thread touches Tk.

TrayHost ties the icon to the main window: closing the window hides it in the tray (the first
time after TrayNotice explains where it went), the menu opens it again or quits, and a second
start of AirGrabber (app/single_instance.py) brings it back. Without a working tray icon, or with
`keep_in_tray` off, closing the window quits, as before.

MediaForge (Forge inside AirGrabber) shares the icon: Open MediaForge in its menu, the
renamer's notifications through `notify`, and how many files wait there in the tooltip and as a
small badge on the icon (`set_forge_count`, MediaForge's `with_badge`), all on the loop.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import tkinter as tk
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import Any

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFont

from app import APP_NAME
from app.i18n import N_, t, tn
from app.services import SettingsService
from app.ui import theme
from app.ui.borderless import make_borderless
from app.ui.branding import load_logo, set_window_icon
from app.ui.dialog_kit import (
    CLOSE_CLEARANCE,
    CONTENT_WIDTH,
    LINE_GAP,
    NAV_GAP,
    PAD_X,
    PAD_Y,
    activate_later,
    heading_row,
    place_over,
    primary_button,
    text_label,
)
from app.ui.tasks import spawn
from app.ui.tray_menu import PAUSE_FOR, PAUSE_HOURS, PAUSE_MENU, PAUSE_UNTIL_TOMORROW

log = logging.getLogger(__name__)

# Shown through t().
OPEN_LABEL = N_("Open AirGrabber")
OPEN_FORGE_LABEL = N_("Open MediaForge")
CHECK_NOW_LABEL = N_("Check for new episodes now")
QUIT_LABEL = N_("Quit")

NOTICE_TITLE = N_("AirGrabber keeps running")
NOTICE_TEXT = N_(
    "It's in the tray next to the clock (click ^ if you don't see it), so new episodes still "
    "download. Right-click its icon and choose Quit to close it completely."
)
NOTICE_BUTTON = N_("Got it")

_IMAGE_PX = 128  # pystray hands Windows an .ico; it picks the size the tray needs from it
_SHOW_POLL_S = 0.5  # how often a second start's "show the window" is checked for
_STOP_TIMEOUT_S = 3.0
# The waiting files' badge (MediaForge's `with_badge`): amber (as the switch's badge) and
# AirGrabber's font (D10); one colour in both modes, read when it is painted.
_BADGE_FONT_FILE = Path(ctk.__file__).parent / "assets" / "fonts" / "Roboto" / "Roboto-Medium.ttf"


# -- decisions (pure, tested) --------------------------------------------------------------------


class CloseAction(Enum):
    QUIT = "quit"  # close = quit, as before the tray
    HIDE = "hide"  # into the tray
    NOTICE_THEN_HIDE = "notice"  # the first time: say where it went, then into the tray


def close_action(*, keep_in_tray: bool, tray_running: bool, notice_shown: bool) -> CloseAction:
    """What closing the main window does."""
    if not keep_in_tray or not tray_running:
        return CloseAction.QUIT
    return CloseAction.HIDE if notice_shown else CloseAction.NOTICE_THEN_HIDE


def tooltip(count: int) -> str:
    """The icon's tooltip: AirGrabber, and how many files wait in MediaForge."""
    if count <= 0:
        return APP_NAME
    waiting = tn("{n} file waiting", "{n} files waiting", count)
    return f"{APP_NAME} · MediaForge: {waiting}"


def with_badge(image: Image.Image, count: int) -> Image.Image:
    """The icon with a small amber count in its bottom-right corner (MediaForge's), "9+" from
    ten on. A small Pillow drawing (one 128 px picture): fine on the loop."""
    badged = image.convert("RGBA").copy()
    radius = min(badged.size) // 4
    right, bottom = badged.width - 1, badged.height - 1
    draw = ImageDraw.Draw(badged)
    draw.ellipse((right - 2 * radius, bottom - 2 * radius, right, bottom), fill=theme.pick(theme.WARNING, dark=True))
    text = str(count) if count < 10 else "9+"
    centre = (right - radius, bottom - radius)
    colour = theme.pick(theme.ON_WARNING, dark=True)
    draw.text(centre, text, fill=colour, font=_badge_font(int(radius * 1.3)), anchor="mm")
    return badged


def _badge_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(str(_BADGE_FONT_FILE), size)
    except OSError:
        return ImageFont.load_default(size)


# -- the icon (pystray on its own thread) --------------------------------------------------------


class TrayIcon:
    """The tray icon and its menu: Open AirGrabber (default: clicking the icon), Open MediaForge
    (with `on_open_forge`), Check for new episodes now, Quit; `set_count` puts MediaForge's
    waiting files' count in its tooltip and as a badge on it.

    Every callback runs on `loop`'s thread (handed over with call_soon_threadsafe), never on
    pystray's. `start` raises ImportError when pystray is missing; `on_failed` is called (on the
    loop) when its message loop dies later. `backend` stands in for the pystray module in tests.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        image: Image.Image,
        *,
        on_open: Callable[[], None],
        on_check_now: Callable[[], None],
        on_quit: Callable[[], None],
        on_open_forge: Callable[[], None] | None = None,
        on_failed: Callable[[], None] | None = None,
        on_pause: Callable[[int | None], None] | None = None,
        on_resume: Callable[[], None] | None = None,
        backend: Any = None,
    ) -> None:
        """`on_pause(hours)` (None: until tomorrow) / `on_resume`: the automatic downloads'
        pause and today's lines in the menu (none without them)."""
        self._on_pause = on_pause
        self._on_resume = on_resume
        # What the menu shows of AirGrabber: set on the loop, read on pystray's thread (one
        # assignment each, so either thread sees a whole value).
        self._lines: tuple[str, ...] = ()
        self._resume = ""  # "Resume … (paused until 14:30)" while paused
        self._loop = loop
        self._image = image
        self._on_open = on_open
        self._on_open_forge = on_open_forge
        self._on_check_now = on_check_now
        self._on_quit = on_quit
        self._on_failed = on_failed
        self._backend = backend
        self._icon: Any = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()  # guards the three flags below (two threads use them)
        self._ready = False  # pystray's loop runs and the icon was shown
        self._stopping = False
        self._failed = False
        self._count = 0  # MediaForge's waiting files (only the loop's thread uses it)

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and not self._stopping and not self._failed

    def start(self) -> None:
        if self._thread is not None or self._stopping:
            return
        # pystray missing → ImportError: the caller falls back to close = quit.
        pystray = self._backend if self._backend is not None else _import_pystray()
        items = [pystray.MenuItem(t(OPEN_LABEL), self._menu_action(self._on_open), default=True)]
        if self._on_open_forge is not None:
            items.append(pystray.MenuItem(t(OPEN_FORGE_LABEL), self._menu_action(self._on_open_forge)))
        if self._on_pause is not None:
            items += [pystray.Menu.SEPARATOR, *self._status_items(pystray), pystray.Menu.SEPARATOR]
        items.append(pystray.MenuItem(t(CHECK_NOW_LABEL), self._menu_action(self._on_check_now)))
        if self._on_pause is not None:
            items += self._pause_items(pystray)
        items += [
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(t(QUIT_LABEL), self._menu_action(self._on_quit)),
        ]
        menu = pystray.Menu(*items)
        count = self._count
        image = with_badge(self._image, count) if count > 0 else self._image
        self._icon = pystray.Icon(APP_NAME, image, tooltip(count), menu)
        # A daemon thread: pystray's own setup thread inherits that, so neither can keep the
        # process alive if stop() is never reached.
        thread = threading.Thread(target=self._run, name="tray-icon", daemon=True)
        self._thread = thread
        thread.start()

    def stop(self) -> None:
        """Remove the icon and end its thread (waits up to a few seconds). Never touches Tk or
        the loop, so main.py can call it after the loop has closed. Safe to call twice."""
        with self._lock:
            if self._stopping:
                return
            self._stopping = True
            ready = self._ready
        thread = self._thread
        if thread is None:
            return
        if ready:
            self._icon.stop()
        # Otherwise _setup sees _stopping once pystray's loop runs, and stops it from there.
        if thread is not threading.current_thread():
            thread.join(_STOP_TIMEOUT_S)
            if thread.is_alive():
                log.warning("The tray icon did not stop in time")

    def _status_items(self, pystray: Any) -> list[Any]:
        """Today's episodes and the downloads: greyed lines, hidden while empty."""

        def line(index: int) -> Any:
            return pystray.MenuItem(
                lambda _item: self._lines[index] if index < len(self._lines) else "",
                None,
                enabled=False,
                visible=lambda _item: index < len(self._lines) and bool(self._lines[index]),
            )

        return [line(0), line(1)]

    def _pause_items(self, pystray: Any) -> list[Any]:
        """Pause automatic downloads ▸ For 3 hours / Until tomorrow; while paused, Resume."""
        pause, resume = self._on_pause, self._on_resume
        assert pause is not None
        choices = pystray.Menu(
            pystray.MenuItem(
                t(PAUSE_FOR, n=PAUSE_HOURS), self._menu_action(lambda: pause(PAUSE_HOURS))
            ),
            pystray.MenuItem(t(PAUSE_UNTIL_TOMORROW), self._menu_action(lambda: pause(None))),
        )
        return [
            pystray.MenuItem(t(PAUSE_MENU), choices, visible=lambda _item: not self._resume),
            pystray.MenuItem(
                lambda _item: self._resume,
                self._menu_action(resume if resume is not None else lambda: None),
                visible=lambda _item: bool(self._resume),
            ),
        ]

    def set_lines(self, lines: tuple[str, ...], resume: str = "") -> None:
        """The menu's lines (today's episodes, the downloads) and, while paused, its Resume
        item's text. On the loop; the menu reads them when it opens."""
        if lines == self._lines and resume == self._resume:
            return
        self._lines, self._resume = lines, resume
        icon = self._icon
        if icon is not None:
            try:
                icon.update_menu()
            except Exception:
                log.debug("Could not refresh the tray menu", exc_info=True)

    def notify(self, title: str, message: str) -> None:
        """A Windows notification from the tray icon; nothing when the icon isn't showing."""
        with self._lock:
            showing = self._ready and not self._stopping and not self._failed
        if not showing:
            log.info("No tray icon to show the notification %r", title)
            return
        try:
            self._icon.notify(message, title)
        except Exception:
            log.warning("Could not show the notification %r", title, exc_info=True)

    def set_count(self, count: int) -> None:
        """How many files MediaForge needs the owner for: in the tooltip, and as a badge on the
        icon (none for 0). Before `start` it is kept for the icon to start with."""
        count = max(0, count)
        if count == self._count:
            return
        self._count = count
        icon = self._icon
        if icon is None:
            return
        try:
            icon.title = tooltip(count)
            icon.icon = with_badge(self._image, count) if count > 0 else self._image
        except Exception:
            log.warning("Could not show MediaForge's count on the tray icon", exc_info=True)

    # -- pystray's threads -----------------------------------------------------------------------

    def _run(self) -> None:
        try:
            self._icon.run(setup=self._setup)
        except Exception:
            log.exception("The tray icon stopped working")
            with self._lock:
                self._failed = True
            if self._on_failed is not None:
                self._post(self._on_failed)

    def _setup(self, icon: Any) -> None:
        """pystray's setup thread, once its message loop runs."""
        with self._lock:
            self._ready = True
            stopping = self._stopping
            if not stopping:
                icon.visible = True
        if stopping:
            icon.stop()

    def _menu_action(self, callback: Callable[[], None]) -> Callable[..., None]:
        # pystray calls an action without arguments when its signature takes none.
        def action(*_args: object) -> None:
            self._post(callback)

        return action

    def _post(self, callback: Callable[[], None]) -> None:
        """Hand `callback` to the asyncio loop: the only thing the tray thread may do."""
        try:
            self._loop.call_soon_threadsafe(_run_logged, callback)
        except RuntimeError:
            pass  # the loop has closed: AirGrabber is shutting down


def _import_pystray() -> Any:
    import pystray

    return pystray


def _run_logged(callback: Callable[[], None]) -> None:
    try:
        callback()
    except Exception:
        log.exception("Tray action failed")


# -- the main window's side ----------------------------------------------------------------------


class TrayHost:
    """The main window in the tray: close-to-tray, show again, quit, a second start's request.

    Create it in the window's __init__ (it is the WM_DELETE_WINDOW handler), call `start` once
    the asyncio loop runs, and `stop` at exit (main.py, after the shutdown).
    """

    def __init__(
        self,
        window: ctk.CTk,
        *,
        settings: SettingsService,
        on_check_now: Callable[[], None],
        show_requests: Callable[[], bool] | None = None,
        on_open_forge: Callable[[], None] | None = None,
        on_quit: Callable[[], None] | None = None,
        on_reopen: Callable[[], None] | None = None,
        on_pause: Callable[[int | None], None] | None = None,
        on_resume: Callable[[], None] | None = None,
    ) -> None:
        """`on_open_forge`: the menu's Open MediaForge (no such item without it). `on_quit`:
        what quitting does (the main window's, which first waits for MediaForge); without it
        the window is destroyed at once. `on_reopen`: called when a plain Open (the menu's
        Open, a second start) brings the window back from the tray (the main window's start
        page: Series › Calendar)."""
        self._window = window
        self._settings = settings
        self._on_check_now = on_check_now
        self._show_requests = show_requests
        self._on_open_forge = on_open_forge
        self._on_quit = on_quit
        self._on_reopen = on_reopen
        self._icon: TrayIcon | None = None
        self._notice: TrayNotice | None = None
        self._forge_count = 0
        self._on_pause = on_pause
        self._on_resume = on_resume
        self._lines: tuple[tuple[str, ...], str] = ((), "")

    @property
    def running(self) -> bool:
        return self._icon is not None and self._icon.running

    def start(self) -> None:
        spawn(self._start_icon(), name="tray-icon")
        if self._show_requests is not None:
            spawn(self._watch_show_requests(self._show_requests), name="show-requests")

    def stop(self) -> None:
        icon, self._icon = self._icon, None
        if icon is not None:
            icon.stop()

    def notify(self, title: str, message: str) -> None:
        if self._icon is not None:
            self._icon.notify(title, message)

    def set_forge_count(self, count: int) -> None:
        """MediaForge's waiting files on the icon (kept for an icon that starts later)."""
        self._forge_count = count
        if self._icon is not None:
            self._icon.set_count(count)

    def set_lines(self, lines: tuple[str, ...], resume: str = "") -> None:
        """The menu's lines and its Resume text (kept for an icon that starts later)."""
        self._lines = (lines, resume)
        if self._icon is not None:
            self._icon.set_lines(lines, resume)

    def close_window(self) -> None:
        """WM_DELETE_WINDOW: into the tray, or quit (see close_action)."""
        if self._notice is not None and _alive(self._notice):
            self._notice.lift()
            self._notice.focus_force()
            return
        current = self._settings.current
        action = close_action(
            keep_in_tray=current.keep_in_tray,
            tray_running=self.running,
            notice_shown=current.tray_notice_shown,
        )
        if action is CloseAction.QUIT:
            self.quit()
        elif action is CloseAction.HIDE:
            self._window.withdraw()
        else:
            self._notice = TrayNotice(self._window, on_done=self._notice_done)

    def show_window(self) -> None:
        window = self._window
        if not _alive(window):
            return
        window.deiconify()
        window.lift()
        window.focus_force()

    def open_window(self) -> None:
        """A plain Open (the tray menu's Open, a second start of AirGrabber): the window back
        from the tray at its start page (`on_reopen`), then shown and raised. A window already
        on screen (or minimised) keeps its page. `show_window` is for a window shown for a
        purpose: it keeps its page."""
        window = self._window
        if not _alive(window):
            return
        if self._on_reopen is not None and window.state() == "withdrawn":
            self._on_reopen()
        self.show_window()

    def quit(self) -> None:
        """Close for good: main.py then runs the full shutdown (and installs a staged update).
        With `on_quit`, that decides when (MediaForge first finishes its current file)."""
        if self._on_quit is not None:
            self._on_quit()
        elif _alive(self._window):
            self._window.destroy()

    def _notice_done(self) -> None:
        self._notice = None
        spawn(self._settings.update(tray_notice_shown=True), name="tray-notice-shown")
        if self.running:
            self._window.withdraw()
        else:
            self.quit()  # the icon went away meanwhile: nothing to come back from

    async def _start_icon(self) -> None:
        image = await asyncio.to_thread(load_logo, _IMAGE_PX)
        if not _alive(self._window):
            return
        if image is None:
            self._fall_back("the logo could not be loaded")
            return
        icon = TrayIcon(
            asyncio.get_running_loop(),
            image,
            on_open=self.open_window,
            on_open_forge=self._on_open_forge,
            on_check_now=self._on_check_now,
            on_quit=self.quit,
            on_failed=lambda: self._fall_back("its message loop failed"),
            on_pause=self._on_pause,
            on_resume=self._on_resume,
        )
        icon.set_count(self._forge_count)  # a count that came before the icon
        icon.set_lines(*self._lines)
        try:
            icon.start()
        except Exception as exc:  # ImportError: pystray is missing
            self._fall_back(repr(exc))
            return
        self._icon = icon
        log.info("Tray icon started")

    def _fall_back(self, reason: str) -> None:
        log.warning("No tray icon (%s): closing the window quits AirGrabber", reason)
        self._icon = None
        if _alive(self._window) and self._window.state() == "withdrawn":
            self.show_window()  # hidden (or started with --tray): the window is the only way in

    async def _watch_show_requests(self, requested: Callable[[], bool]) -> None:
        while True:
            await asyncio.sleep(_SHOW_POLL_S)
            if requested() and _alive(self._window):
                log.info("AirGrabber was started again: showing the window")
                self.open_window()


def _alive(widget: tk.Misc) -> bool:
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:
        return False  # the application has been destroyed


# -- the first-time notice -----------------------------------------------------------------------


class TrayNotice(ctk.CTkToplevel):
    """Shown the first time the window closes to the tray: where AirGrabber went and how to quit
    it. Got it, Enter, Escape and the round ✕ all call `on_done` once."""

    def __init__(self, master: ctk.CTk, *, on_done: Callable[[], None]) -> None:
        super().__init__(master, fg_color=theme.BG)
        self._on_done: Callable[[], None] | None = on_done

        self.title(APP_NAME)
        set_window_icon(self)
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._done)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)
        # Holds the content width.
        ctk.CTkFrame(body, width=CONTENT_WIDTH, height=1, fg_color="transparent").pack()
        heading = heading_row(body, t(NOTICE_TITLE), right_room=CLOSE_CLEARANCE)
        heading.pack(fill="x")
        text_label(body, t(NOTICE_TEXT)).pack(anchor="w", pady=(LINE_GAP, 0))
        nav = ctk.CTkFrame(body, fg_color="transparent")
        nav.pack(fill="x", pady=(NAV_GAP, 0))
        primary_button(nav, t(NOTICE_BUTTON), self._done).pack(side="right")

        self.bind("<Return>", lambda _event: self._done())
        self.bind("<Escape>", lambda _event: self._done())
        # Its ✕ (and Alt+F4) call `_done`.
        self.borderless = make_borderless(self, drag_widgets=(self, body, heading))
        place_over(self, master)
        activate_later(self)

    def _done(self) -> None:
        on_done, self._on_done = self._on_done, None
        self.destroy()
        if on_done is not None:
            on_done()
