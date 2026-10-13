"""First-run welcome: what AirGrabber does, how downloads should open, and the TMDB key.

MainWindow opens it on the first start of a new library (see `first_run_action`). Choices are
saved as they are made: step 2's Next stores how to download; Finish, Skip for now and closing
the window all mark the setup as done (with the TMDB key, when one checked OK). Finish checks a
pasted key nobody checked yet, and stays on the step when TMDB doesn't accept it.
"""

from __future__ import annotations

import asyncio
import math
import tkinter as tk
from collections.abc import Callable, Coroutine
from enum import Enum
from pathlib import Path
from tkinter import filedialog
from typing import Any

import customtkinter as ctk

from app import APP_NAME
from app.models import DownloadMode, Settings
from app.services import DiscoveryService, SettingsService, TorrentAppService
from app.ui import theme
from app.ui.branding import load_logo, set_window_icon
from app.ui.dialog_kit import (
    BLOCK_GAP,
    BODY_SIZE,
    BUTTON_HEIGHT,
    CONTENT_WIDTH,
    DETAIL_SIZE,
    HEADING_SIZE,
    LINE_GAP,
    NAV_GAP,
    PAD_X,
    PAD_Y,
    activate_later,
    heading_row,
    place_over,
    primary_button,
    secondary_button,
    text_label,
)
from app.ui.tasks import spawn
from app.ui.text_fit import ELLIPSIS, SLACK, Measure, elide_to_width
from app.ui.tmdb_key_guide import TMDB_HEADING, TMDB_INTRO, KeyAction, TmdbKeyGuide
from app.ui.torrent_app_status import TorrentAppStatus, TorrentStatus

STEP_COUNT = 3
WELCOME_TEXT = (
    "Follow your favourite shows, see what's on each day, and download episodes and movies "
    "with one click."
)

_LOGO_HEIGHT = 96
_CARD_PAD_X = 20
_CARD_PAD_Y = 18
_CARD_GAP = 14
_DOT_SIZE = 20
_DOT_GAP = 14
_CARD_TEXT_WIDTH = CONTENT_WIDTH - 2 * _CARD_PAD_X - _DOT_SIZE - _DOT_GAP
_FOLDER_BUTTON_WIDTH = 150
_FOLDER_TEXT_WIDTH = _CARD_TEXT_WIDTH - _FOLDER_BUTTON_WIDTH - 16


# -- decisions (pure, tested) --------------------------------------------------------------------


class FirstRun(Enum):
    NOTHING = "nothing"  # set up already
    WELCOME = "welcome"  # a new library: open the welcome
    MARK_DONE = "mark_done"  # a library from before the welcome: record the setup as done


def first_run_action(setup_done: bool, tracked_shows: int) -> FirstRun:
    if setup_done:
        return FirstRun.NOTHING
    return FirstRun.MARK_DONE if tracked_shows > 0 else FirstRun.WELCOME


def step_label(step: int) -> str | None:
    """'Step 2 of 3' at the top right of steps 2 and 3; the welcome itself has none."""
    return f"Step {step} of {STEP_COUNT}" if step >= 2 else None


def nav_buttons(step: int) -> tuple[str | None, str | None]:
    """(left, right) texts of the bottom row; the welcome step has its button in the content."""
    if step == 2:
        return "Back", "Next"
    if step == 3:
        return "Skip for now", "Finish"
    return None, None


def download_changes(current: Settings, mode: DownloadMode, folder: Path) -> dict[str, Any]:
    """What step 2's Next saves: the mode, and the folder when it was changed."""
    changes: dict[str, Any] = {"download_mode": mode}
    if folder != current.download_dir:
        changes["download_dir"] = folder
    return changes


def elide_path(path: str, max_width: float, measure: Measure) -> str:
    r"""`path` if it fits in `max_width`, else its drive (or UNC share) and as many trailing
    folders as fit, "…" in between: "C:\…\Videos\watch". When not even the last folder fits
    that way, the path elided at its end."""
    if measure(path) <= max_width:
        return path
    sep = "\\" if "\\" in path else "/"
    parts = path.split(sep)
    head_count = 4 if path.startswith(sep * 2) else 1  # "", "", server, share
    head, tail = sep.join(parts[:head_count]), parts[head_count:]
    best = ""
    for count in range(1, len(tail)):  # at least one folder is left out
        candidate = sep.join((head, ELLIPSIS, *tail[-count:]))
        if measure(candidate) > max_width:
            break
        best = candidate
    return best or elide_to_width(path, max_width, measure)


def finish_changes(checked_key: str | None) -> dict[str, Any]:
    """What closing the welcome saves: setup done, plus the TMDB key when one checked OK."""
    changes: dict[str, Any] = {"setup_done": True}
    if checked_key:
        changes["tmdb_api_key"] = checked_key
    return changes


# -- widgets -------------------------------------------------------------------------------------


class _OptionCard(ctk.CTkFrame):
    """A selectable option: radio-style dot, title, muted description and an optional extra row.

    The whole card is clickable; call `bind_select()` once everything inside it exists.
    """

    def __init__(self, master: Any, title: str, description: str) -> None:
        super().__init__(master, corner_radius=10, cursor="hand2")
        inner = ctk.CTkFrame(self, fg_color="transparent")
        inner.pack(fill="x", padx=_CARD_PAD_X, pady=_CARD_PAD_Y)
        inner.grid_columnconfigure(1, weight=1)
        self._dot = ctk.CTkFrame(
            inner,
            width=_DOT_SIZE,
            height=_DOT_SIZE,
            corner_radius=_DOT_SIZE // 2,
            fg_color="transparent",
        )
        self._dot.grid(row=0, column=0, sticky="w", padx=(0, _DOT_GAP))
        ctk.CTkLabel(
            inner,
            text=title,
            font=theme.font(17, bold=True),
            text_color=theme.TEXT,
            anchor="w",
        ).grid(row=0, column=1, sticky="w")
        text_label(
            inner, description, size=DETAIL_SIZE, color=theme.TEXT_MUTED, wraplength=_CARD_TEXT_WIDTH
        ).grid(row=1, column=1, sticky="w", pady=(4, 0))
        self.extra = ctk.CTkFrame(inner, fg_color="transparent")
        self.set_selected(False)

    def show_extra(self, show: bool) -> None:
        if show:
            self.extra.grid(row=2, column=1, sticky="ew", pady=(12, 0))
        else:
            self.extra.grid_remove()

    def set_selected(self, selected: bool) -> None:
        self.configure(
            border_width=2 if selected else 1,
            border_color=theme.ACCENT if selected else theme.CELL_BORDER,
            fg_color=theme.TODAY_BG if selected else theme.CELL_BG,
        )
        self._dot.configure(
            border_width=6 if selected else 2,
            border_color=theme.ACCENT if selected else theme.EDGE,
        )

    def bind_select(self, command: Callable[[], None]) -> None:
        stack: list[tk.Misc] = [self]
        while stack:
            widget = stack.pop()
            widget.bind("<Button-1>", lambda _event: command(), add="+")
            stack.extend(widget.winfo_children())


class WelcomeDialog(ctk.CTkToplevel):
    """Three steps: welcome; open downloads in the torrent program or save them to a folder;
    the TMDB key. Modal over the main window."""

    def __init__(
        self,
        master: ctk.CTk,
        *,
        settings: SettingsService,
        torrent_app: TorrentAppService,
        discovery: DiscoveryService,
    ) -> None:
        super().__init__(master, fg_color=theme.BG)
        self._settings = settings
        self._torrent_app = torrent_app
        self._tasks: set[asyncio.Task[Any]] = set()
        self._logo_image: ctk.CTkImage | None = None
        self._step = 1
        self._mode = DownloadMode.APP  # the welcome's suggestion, whatever is stored
        self._folder = settings.current.download_dir

        self.title(f"Welcome to {APP_NAME}")
        set_window_icon(self)
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self._close)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)
        # The steps share one cell (the one on top shows); this frame underneath holds the
        # content width.
        ctk.CTkFrame(body, width=CONTENT_WIDTH, height=1, fg_color="transparent").grid(
            row=0, column=0, sticky="n"
        )
        self._pages = {
            1: self._build_welcome(body),
            2: self._build_download(body),
            3: self._build_tmdb(body, discovery),
        }
        for page in self._pages.values():
            page.grid(row=0, column=0, sticky="nsew")

        nav = ctk.CTkFrame(body, fg_color="transparent", height=BUTTON_HEIGHT)
        nav.grid(row=1, column=0, sticky="ew", pady=(NAV_GAP, 0))
        nav.pack_propagate(False)  # keeps its height on the welcome step, which has no buttons
        self._left = secondary_button(nav, "", self._on_left)
        self._right = primary_button(nav, "", self._on_right)

        self._show_step(1)
        self._keep_tallest_size()
        place_over(self, master)
        activate_later(self)
        self._spawn(self._load_logo(), "welcome-logo")

    # -- steps ---------------------------------------------------------------------------------

    def _build_welcome(self, master: Any) -> ctk.CTkFrame:
        page = ctk.CTkFrame(master, fg_color="transparent")
        inner = ctk.CTkFrame(page, fg_color="transparent")
        inner.pack(expand=True)
        self._logo = ctk.CTkLabel(inner, text="", height=_LOGO_HEIGHT)
        self._logo.pack()
        ctk.CTkLabel(
            inner,
            text=f"Welcome to {APP_NAME}",
            font=theme.font(HEADING_SIZE, bold=True),
            text_color=theme.TEXT,
        ).pack(pady=(BLOCK_GAP, 0))
        ctk.CTkLabel(
            inner,
            text=WELCOME_TEXT,
            font=theme.font(BODY_SIZE),
            text_color=theme.TEXT,
            justify="center",
            wraplength=480,
        ).pack(pady=(12, 0))
        primary_button(inner, "Get started", lambda: self._show_step(2), width=180).pack(
            pady=(NAV_GAP, 0)
        )
        return page

    def _build_download(self, master: Any) -> ctk.CTkFrame:
        page = ctk.CTkFrame(master, fg_color="transparent")
        heading_row(page, "How do you want to download?", step_label(2)).pack(fill="x")
        text_label(
            page, "You can change this later in Settings.", size=DETAIL_SIZE, color=theme.TEXT_MUTED
        ).pack(anchor="w", pady=(LINE_GAP, 0))

        self._app_card = _OptionCard(
            page,
            "Open in my torrent program",
            "Each download opens in your torrent program by itself. Best for most people.",
        )
        self._app_card.pack(fill="x", pady=(BLOCK_GAP, 0))
        self._torrent = TorrentAppStatus(
            self._app_card.extra, torrent_app=self._torrent_app, wraplength=_CARD_TEXT_WIDTH
        )
        self._torrent.pack(fill="x")
        self._app_card.show_extra(True)
        self._app_card.bind_select(lambda: self._select_mode(DownloadMode.APP))

        self._folder_card = _OptionCard(
            page, "Save to a folder", "For a watch folder, for example on a NAS."
        )
        self._folder_card.pack(fill="x", pady=(_CARD_GAP, 0))
        folder_row = self._folder_card.extra
        # One line, whatever the path: the dialog keeps the size it had with one line.
        self._folder_label = text_label(folder_row, "", size=DETAIL_SIZE, wraplength=0)
        self._folder_label.pack(side="left")
        self._show_folder()
        secondary_button(
            folder_row, "Choose folder…", self._choose_folder, width=_FOLDER_BUTTON_WIDTH
        ).pack(side="right")
        self._folder_card.bind_select(lambda: self._select_mode(DownloadMode.FOLDER))

        self._select_mode(self._mode)
        return page

    def _build_tmdb(self, master: Any, discovery: DiscoveryService) -> ctk.CTkFrame:
        page = ctk.CTkFrame(master, fg_color="transparent")
        heading_row(page, TMDB_HEADING, step_label(3)).pack(fill="x")
        text_label(
            page,
            f"{TMDB_INTRO} You can skip this and do it later.",
            size=DETAIL_SIZE,
            color=theme.TEXT_MUTED,
        ).pack(anchor="w", pady=(LINE_GAP, 0))
        self._guide = TmdbKeyGuide(page, discovery=discovery)
        self._guide.pack(fill="x", pady=(BLOCK_GAP, 0))
        return page

    def _keep_tallest_size(self) -> None:
        """Fix the dialog at its tallest (step 2 with both the install button and the folder
        row), so it doesn't change size between steps or while step 2 changes."""
        self._torrent.show(TorrentStatus.MISSING)
        self._folder_card.show_extra(True)
        self.update_idletasks()
        scaling = ctk.ScalingTracker.get_window_scaling(self)
        width = math.ceil(self.winfo_reqwidth() / scaling)
        height = math.ceil(self.winfo_reqheight() / scaling)
        self._torrent.show(TorrentStatus.LOOKING)
        self._select_mode(self._mode)
        self.geometry(f"{width}x{height}")  # CTk scales the size (not the position)

    def _show_step(self, step: int) -> None:
        self._step = step
        self._pages[step].tkraise()
        left, right = nav_buttons(step)
        for button, text, side in ((self._left, left, "left"), (self._right, right, "right")):
            if text is None:
                button.pack_forget()
            else:
                button.configure(text=text, state="normal")
                button.pack(side=side)
        if step == 2:
            self._torrent.refresh()  # again each time: a program may have been installed

    def _on_left(self) -> None:
        if self._step == 2:
            self._show_step(1)
        elif self._step == 3:
            self._finish(None)  # Skip for now

    def _on_right(self) -> None:
        if self._step == 2:
            self._spawn(self._save_download_choice(), "welcome-download-choice")
        elif self._step == 3:
            if self._guide.key_action() is KeyAction.CHECK:
                self._spawn(self._check_then_finish(), "welcome-finish")
            else:
                self._finish(self._guide.checked_key)

    # -- step 2: how to download -----------------------------------------------------------------

    def _select_mode(self, mode: DownloadMode) -> None:
        self._mode = mode
        self._app_card.set_selected(mode is DownloadMode.APP)
        self._folder_card.set_selected(mode is DownloadMode.FOLDER)
        self._folder_card.show_extra(mode is DownloadMode.FOLDER)

    def _choose_folder(self) -> None:
        # Native modal dialog: the event loop (and background tasks) pause until it closes.
        chosen = filedialog.askdirectory(parent=self, initialdir=str(self._folder), mustexist=True)
        if chosen:
            self._folder = Path(chosen)
            self._show_folder()

    def _show_folder(self) -> None:
        measure = theme.font(DETAIL_SIZE).measure
        text = elide_path(str(self._folder), _FOLDER_TEXT_WIDTH - SLACK, measure)
        self._folder_label.configure(text=text)

    async def _save_download_choice(self) -> None:
        self._set_nav_state("disabled")
        saved = self._save(download_changes(self._settings.current, self._mode, self._folder))
        await asyncio.wait([saved])  # not `await saved`: cancelling this must not cancel the save
        if not self.winfo_exists():
            return
        self._set_nav_state("normal")
        if saved.cancelled() or saved.exception() is not None:
            return  # logged and reported by the task runner
        self._show_step(3)

    # -- finishing -------------------------------------------------------------------------------

    async def _check_then_finish(self) -> None:
        """Finish with a key nobody checked yet: check it first. When TMDB doesn't accept it (or
        can't be reached) the step stays, showing why; Skip for now still finishes without it."""
        self._right.configure(state="disabled")
        key = await self._guide.check_now()
        if not self.winfo_exists():
            return
        self._right.configure(state="normal")
        if key is not None:
            self._finish(key)

    def _close(self) -> None:
        """The window's X: done, with whatever was chosen so far."""
        self._finish(self._guide.checked_key)

    def _finish(self, checked_key: str | None) -> None:
        self._save(finish_changes(checked_key))
        self.destroy()

    def _save(self, changes: dict[str, Any]) -> asyncio.Task[Settings]:
        """Save `changes`; not cancelled with the dialog. SettingsService applies updates one at
        a time, in order, so step 2's save and Finish's can't undo each other."""
        return spawn(self._settings.update(**changes), name="welcome-save")

    # -- helpers ---------------------------------------------------------------------------------

    def _set_nav_state(self, state: str) -> None:
        self._left.configure(state=state)
        self._right.configure(state=state)

    async def _load_logo(self) -> None:
        scaling = ctk.ScalingTracker.get_widget_scaling(self)
        logo = await asyncio.to_thread(load_logo, round(_LOGO_HEIGHT * scaling))
        if logo is None or not self.winfo_exists():
            return
        width = round(logo.width / scaling)
        self._logo_image = ctk.CTkImage(light_image=logo, dark_image=logo, size=(width, _LOGO_HEIGHT))
        self._logo.configure(image=self._logo_image)

    def _spawn(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task = spawn(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def destroy(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        super().destroy()
