"""The tray icon: AirGrabber keeps running (and downloading) with its window closed.

TrayIcon wraps pystray. pystray needs a Win32 message loop of its own, so it runs on a daemon
thread: the one exception to "no threads" (CLAUDE.md). Its menu callbacks only hand work to the
asyncio loop with `loop.call_soon_threadsafe`; nothing on that thread touches Tk.

TrayHost ties the icon to the main window: closing the window hides it in the tray (the first
time after TrayNotice explains where it went), the menu opens it again or quits, and a second
start of AirGrabber (app/single_instance.py) brings it back. Without a working tray icon, or with
`keep_in_tray` off, closing the window quits, as before.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import tkinter as tk
from collections.abc import Callable
from enum import Enum
from typing import Any

import customtkinter as ctk
from PIL import Image

from app import APP_NAME
from app.i18n import N_, t
from app.services import SettingsService
from app.ui import theme
from app.ui.branding import load_logo, set_window_icon
from app.ui.dialog_kit import (
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

log = logging.getLogger(__name__)

# Shown through t().
OPEN_LABEL = N_("Open AirGrabber")
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


# -- the icon (pystray on its own thread) --------------------------------------------------------


class TrayIcon:
    """The tray icon and its menu: Open AirGrabber (default: clicking the icon), Check for new
    episodes now, Quit.

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
        on_failed: Callable[[], None] | None = None,
        backend: Any = None,
    ) -> None:
        self._loop = loop
        self._image = image
        self._on_open = on_open
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

    @property
    def running(self) -> bool:
        with self._lock:
            return self._thread is not None and not self._stopping and not self._failed

    def start(self) -> None:
        if self._thread is not None or self._stopping:
            return
        # pystray missing → ImportError: the caller falls back to close = quit.
        pystray = self._backend if self._backend is not None else _import_pystray()
        menu = pystray.Menu(
            pystray.MenuItem(t(OPEN_LABEL), self._menu_action(self._on_open), default=True),
            pystray.MenuItem(t(CHECK_NOW_LABEL), self._menu_action(self._on_check_now)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(t(QUIT_LABEL), self._menu_action(self._on_quit)),
        )
        self._icon = pystray.Icon(APP_NAME, self._image, APP_NAME, menu)
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
    ) -> None:
        self._window = window
        self._settings = settings
        self._on_check_now = on_check_now
        self._show_requests = show_requests
        self._icon: TrayIcon | None = None
        self._notice: TrayNotice | None = None

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

    def quit(self) -> None:
        """Close for good: main.py then runs the full shutdown (and installs a staged update)."""
        if _alive(self._window):
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
            on_open=self.show_window,
            on_check_now=self._on_check_now,
            on_quit=self.quit,
            on_failed=lambda: self._fall_back("its message loop failed"),
        )
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
                self.show_window()


def _alive(widget: tk.Misc) -> bool:
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:
        return False  # the application has been destroyed


# -- the first-time notice -----------------------------------------------------------------------


class TrayNotice(ctk.CTkToplevel):
    """Shown the first time the window closes to the tray: where AirGrabber went and how to quit
    it. Got it, Enter, Escape and the X all call `on_done` once."""

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
        heading_row(body, t(NOTICE_TITLE)).pack(fill="x")
        text_label(body, t(NOTICE_TEXT)).pack(anchor="w", pady=(LINE_GAP, 0))
        nav = ctk.CTkFrame(body, fg_color="transparent")
        nav.pack(fill="x", pady=(NAV_GAP, 0))
        primary_button(nav, t(NOTICE_BUTTON), self._done).pack(side="right")

        self.bind("<Return>", lambda _event: self._done())
        self.bind("<Escape>", lambda _event: self._done())
        place_over(self, master)
        activate_later(self)

    def _done(self) -> None:
        on_done, self._on_done = self._on_done, None
        self.destroy()
        if on_done is not None:
            on_done()
