"""Backup & move in the UI (app/services/backup.py): SaveBackupDialog (whether keys and
passwords go in, then where the file goes) and LoadBackupDialog (the chosen file read and
checked, what it holds, Replace / Add to what I have / Cancel, then what changed and whether a
restart is needed). Settings → Backup opens them; the welcome's "Moving from another PC?" link
opens the load (`load_backup`: the file picker first).

Small borderless dialogs in the setup dialogs' look (app/ui/dialog_kit.py), modal over the
window that opened them, which gets the grab back when they close (`on_done` then hears the
load's result, None when nothing was loaded)."""

from __future__ import annotations

import asyncio
import logging
import tkinter as tk
from collections.abc import Awaitable, Callable, Coroutine
from datetime import date
from pathlib import Path
from tkinter import filedialog
from typing import Any

import customtkinter as ctk

from app.i18n import N_, Language, current_language, month_short, t, tn
from app.models import Accent
from app.models.backup import (
    FILE_SUFFIX,
    BackupFile,
    BackupLoadResult,
    BackupSummary,
    KeptDownloads,
    LoadMode,
)
from app.services.backup import BackupError, BackupService, default_file_name, summarize
from app.ui import theme
from app.ui.borderless import make_borderless
from app.ui.branding import set_window_icon
from app.ui.dialog_kit import (
    BODY_SIZE,
    BUTTON_HEIGHT,
    CLOSE_CLEARANCE,
    DETAIL_SIZE,
    HEADING_SIZE,
    LINE_GAP,
    NAV_GAP,
    PAD_X,
    PAD_Y,
    activate_later,
    place_over,
    primary_button,
    secondary_button,
    text_label,
)
from app.ui.quick_setup import restart_text
from app.ui.tasks import spawn

log = logging.getLogger(__name__)

WIDTH = 520  # the content's width (logical)
_INDENT = 30  # under a checkbox's text
_GAP = 16  # between a step's blocks
_RESTART_BUTTON_WIDTH = 150
_WIDE_BUTTON_WIDTH = 210  # "Toevoegen aan wat ik heb"

# Shown through t().
SAVE_HEADING = N_("Save a backup")
SAVE_INTRO = N_(
    "Your followed shows, film watchlist, download history and settings go into one file. "
    "Load it on another PC in Settings → Backup."
)
SECRETS_CHECK = N_("Include keys and passwords")
SECRETS_NOTE = N_("Anyone with the file can then use them. Keep it somewhere safe.")
SAVE_BUTTON = N_("Save…")
SAVING_TEXT = N_("Saving…")
SAVED_HEADING = N_("Backup saved")
SAVED_SECRETS = N_("The keys and passwords are in it: keep it somewhere safe.")
LOAD_HEADING = N_("Load a backup")
READING_TEXT = N_("Reading the backup…")
PROBLEM_HEADING = N_("Can't load this file")
NOTHING_CHANGED = N_("Nothing was changed.")
LOAD_FAILED = N_("Loading the backup didn't work.")
ASK_HEADING = N_("Load this backup?")
REPLACE_BUTTON = N_("Replace")
REPLACE_TEXT = N_(
    "Your shows, watchlist and settings become the backup's. Keys and passwords it doesn't "
    "have stay as they are."
)
ADD_BUTTON = N_("Add to what I have")
ADD_TEXT = N_(
    "The backup's shows and films join yours; where you have one already, yours stays. Your "
    "settings don't change."
)
LOADING_TEXT = N_("Loading the backup…")
LOADED_HEADING = N_("Backup loaded")
SETTINGS_REPLACED = N_("Your settings are now the backup's.")
KEPT_LOCAL = N_("Your downloads keep using qBittorrent on this PC.")
KEPT_THIS_PC = N_(
    "Where downloads go stays as it was on this PC: the backup's qBittorrent was on the other PC."
)
CHECK_FOLDERS = N_("Check your folders in Settings: they may be different on this PC.")
NO_SECRETS = N_("Keys and passwords weren't in the backup. Fill in any you need in Settings.")
FILE_TYPE = N_("AirGrabber backup")
ALL_FILES = N_("All files")


# -- texts (pure) --------------------------------------------------------------------------------


def summary_line(summary: BackupSummary) -> str:
    """What a backup holds, in one line: "12 shows, 5 films on the watchlist, settings"."""
    parts = [
        tn("{n} show", "{n} shows", summary.shows),
        tn("{n} film on the watchlist", "{n} films on the watchlist", summary.films),
    ]
    if summary.settings:
        parts.append(t("settings"))
    return ", ".join(parts)


def date_text(day: date) -> str:
    """"31 Oct 2026" (in the interface language)."""
    return f"{day.day} {month_short(day)} {day.year}"


def detail_line(summary: BackupSummary) -> str:
    """The muted line under it: when and by which version it was saved, how many downloads its
    history has, and whether keys and passwords are in it."""
    saved = date_text(summary.created.astimezone().date())
    parts = [
        t("Saved {date} by AirGrabber {version}", date=saved, version=summary.app_version)
        if summary.app_version
        else t("Saved {date}", date=saved),
        tn("{n} download in the history", "{n} downloads in the history", summary.grabs),
        t("with keys and passwords") if summary.secrets else t("without keys and passwords"),
    ]
    return " · ".join(parts)


def result_lines(result: BackupLoadResult) -> list[str]:
    """What a load changed, a line each (nothing for what it left alone)."""
    counts = result.counts
    lines = [
        t(
            "Added: {shows}, {films}.",
            shows=tn("{n} show", "{n} shows", counts.shows_added),
            films=tn("{n} film", "{n} films", counts.films_added),
        )
    ]
    if counts.shows_removed or counts.films_removed:
        lines.append(
            t(
                "Removed, as they aren't in the backup: {shows}, {films}.",
                shows=tn("{n} show", "{n} shows", counts.shows_removed),
                films=tn("{n} film", "{n} films", counts.films_removed),
            )
        )
    if counts.grabs_added:
        lines.append(
            tn(
                "{n} download added to the history.",
                "{n} downloads added to the history.",
                counts.grabs_added,
            )
        )
    if result.settings_replaced:
        lines.append(t(SETTINGS_REPLACED))
    if result.downloads_kept is KeptDownloads.LOCAL:
        lines.append(t(KEPT_LOCAL))
    elif result.downloads_kept is KeptDownloads.THIS_PC:
        lines.append(t(KEPT_THIS_PC))
    return lines


def result_notes(result: BackupLoadResult) -> list[str]:
    """The muted notes after a load that replaced the settings: check the folders, and fill in
    the keys and passwords the backup didn't have."""
    if not result.settings_replaced:
        return []
    notes = [t(CHECK_FOLDERS)]
    if not result.secrets_loaded:
        notes.append(t(NO_SECRETS))
    return notes


def restart_note(result: BackupLoadResult, language: Language, accent: Accent) -> str | None:
    """The restart note (shown through t()) when the loaded settings have another language or
    accent colour than the running ones (`language`, `accent`); None when neither changed."""
    return restart_text(language=result.language != language, accent=result.accent != accent)


# -- file pickers (native, modal: the event loop pauses until they close) ---------------------------


def _file_types() -> list[tuple[str, str]]:
    return [(t(FILE_TYPE), f"*{FILE_SUFFIX}"), (t(ALL_FILES), "*.*")]


def _start_folder() -> str:
    documents = Path.home() / "Documents"
    return str(documents if documents.is_dir() else Path.home())


def ask_save_path(parent: Any, suggested: str) -> Path | None:
    """Where to save the backup (`suggested`: the file name offered); None: cancelled."""
    chosen = filedialog.asksaveasfilename(
        parent=parent,
        title=t(SAVE_HEADING),
        initialdir=_start_folder(),
        initialfile=suggested,
        defaultextension=FILE_SUFFIX,
        filetypes=_file_types(),
    )
    return Path(chosen) if chosen else None


def ask_open_path(parent: Any) -> Path | None:
    """The backup file to load; None: cancelled."""
    chosen = filedialog.askopenfilename(
        parent=parent, title=t(LOAD_HEADING), initialdir=_start_folder(), filetypes=_file_types()
    )
    return Path(chosen) if chosen else None


# -- the dialogs -----------------------------------------------------------------------------------


class _BackupDialog(ctk.CTkToplevel):
    """A heading, the step's content and its buttons (right-aligned, the primary one first);
    each step replaces the content and the buttons. Esc and the round ✕ close it, except while
    it is busy."""

    def __init__(self, master: Any, title: str) -> None:
        super().__init__(master, fg_color=theme.BG)
        self.title(title)
        set_window_icon(self)
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.busy = False
        self._tasks: set[asyncio.Task[None]] = set()

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)
        ctk.CTkFrame(body, width=WIDTH, height=1, fg_color="transparent").pack()  # the width
        top = ctk.CTkFrame(body, fg_color="transparent")
        top.pack(fill="x")
        top.grid_columnconfigure(0, weight=1)
        self.heading = ctk.CTkLabel(
            top,
            text=title,
            font=theme.font(HEADING_SIZE, bold=True),
            text_color=theme.TEXT,
            anchor="w",
            justify="left",
            wraplength=WIDTH - CLOSE_CLEARANCE,
        )
        self.heading.grid(row=0, column=0, sticky="w")
        # The close button's corner stays free.
        ctk.CTkFrame(top, width=CLOSE_CLEARANCE, height=1, fg_color="transparent").grid(
            row=0, column=1
        )
        self.content = ctk.CTkFrame(body, fg_color="transparent")
        self.content.pack(fill="x")
        self.buttons = ctk.CTkFrame(body, fg_color="transparent", height=BUTTON_HEIGHT)
        self.buttons.pack(fill="x", pady=(NAV_GAP, 0))

        self.bind("<Escape>", lambda _event: self.close())
        self.borderless = make_borderless(
            self, drag_widgets=(self, body, top, self.heading, self.content, self.buttons)
        )

    # -- building a step ---------------------------------------------------------------------------

    def step(self, heading: str) -> None:
        """Start a step: its heading; the previous step's content and buttons go."""
        self.heading.configure(text=heading)
        for child in (*self.content.winfo_children(), *self.buttons.winfo_children()):
            child.destroy()

    def line(
        self,
        text: str,
        *,
        size: int = BODY_SIZE,
        color: theme.Color = theme.TEXT,
        bold: bool = False,
        gap: int = LINE_GAP,
        indent: int = 0,
    ) -> ctk.CTkLabel:
        """A wrapping text line under the previous one (`gap` above it; the first one sits
        LINE_GAP under the heading)."""
        label = text_label(
            self.content, text, size=size, color=color, bold=bold, wraplength=WIDTH - indent
        )
        first = len(self.content.winfo_children()) == 1
        label.pack(anchor="w", padx=(indent, 0), pady=(LINE_GAP if first else gap, 0))
        return label

    def button(
        self, text: str, command: Callable[[], None], *, primary: bool = False, width: int = 140
    ) -> ctk.CTkButton:
        """A button at the right of the row (add the rightmost first)."""
        make = primary_button if primary else secondary_button
        button = make(self.buttons, text, command, width=width)
        button.pack(side="right", padx=(12, 0))
        return button

    def set_buttons(self, state: str) -> None:
        for child in self.buttons.winfo_children():
            if isinstance(child, ctk.CTkButton):
                child.configure(state=state)

    def run(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        """Start `coro`; cancelled when the dialog goes."""
        task: asyncio.Task[None] = spawn(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # -- closing -----------------------------------------------------------------------------------

    def close(self) -> None:
        if not self.busy:
            self.finish()

    def finish(self) -> None:
        """Close, give the window under it the grab back, then `closed()`."""
        master = self.master
        if self.winfo_exists():
            self.destroy()
        try:  # Settings or the welcome, modal again
            if master.winfo_exists():
                master.grab_set()
                master.focus_force()
        except tk.TclError:  # gone meanwhile: only the focus is lost
            pass
        self.closed()

    def closed(self) -> None:
        """After the dialog closed (subclasses tell their owner)."""

    def destroy(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        super().destroy()


class SaveBackupDialog(_BackupDialog):
    """Settings → Backup → Save a backup…: the "Include keys and passwords" choice (off), then
    the file dialog (`ask_save_path`, today's name offered), then where it was saved."""

    def __init__(self, master: Any, *, backup: BackupService, today: date | None = None) -> None:
        super().__init__(master, t(SAVE_HEADING))
        self._backup = backup
        self._today = today if today is not None else date.today()
        self.include_secrets = ctk.BooleanVar(value=False)
        self._error: ctk.CTkLabel | None = None
        self.show_options()
        place_over(self, master)
        activate_later(self)

    def show_options(self) -> None:
        self.step(t(SAVE_HEADING))
        self.line(t(SAVE_INTRO), size=DETAIL_SIZE, color=theme.TEXT_MUTED)
        ctk.CTkCheckBox(
            self.content,
            text=t(SECRETS_CHECK),
            variable=self.include_secrets,
            width=0,
            height=BUTTON_HEIGHT,
            checkbox_width=22,
            checkbox_height=22,
            corner_radius=6,
            font=theme.font(DETAIL_SIZE),
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            border_color=theme.EDGE,
        ).pack(anchor="w", pady=(_GAP, 0))
        self.line(t(SECRETS_NOTE), size=DETAIL_SIZE, color=theme.TEXT_MUTED, gap=0, indent=_INDENT)
        self._error = self.line("", size=DETAIL_SIZE, color=theme.DANGER, gap=0)
        self._error.pack_forget()
        self.button(t("Cancel"), self.close)
        self.button(t(SAVE_BUTTON), self.choose_file, primary=True)

    def choose_file(self) -> None:
        path = ask_save_path(self, default_file_name(self._today))
        if path is not None and self.winfo_exists():
            self.run(self.save(path), "backup-save")

    async def save(self, path: Path) -> None:
        """Save the backup to `path`: the saved step, or the error line when that failed."""
        self.busy = True
        self.set_buttons("disabled")
        self._show_error(t(SAVING_TEXT), theme.TEXT_MUTED)
        try:
            summary = await self._backup.save(path, include_secrets=self.include_secrets.get())
        except BackupError as exc:
            if self.winfo_exists():
                self._show_error(str(exc), theme.DANGER)
                self.set_buttons("normal")
            return
        finally:
            self.busy = False
        if self.winfo_exists():
            self.show_saved(summary, path)

    def _show_error(self, text: str, color: theme.Color) -> None:
        if self._error is None:
            return
        self._error.configure(text=text, text_color=color)
        self._error.pack(anchor="w", pady=(_GAP, 0))

    def show_saved(self, summary: BackupSummary, path: Path) -> None:
        self.step(t(SAVED_HEADING))
        self._error = None
        self.line(summary_line(summary), gap=LINE_GAP)
        self.line(str(path), size=DETAIL_SIZE, color=theme.TEXT_MUTED)
        if summary.secrets:
            self.line(t(SAVED_SECRETS), size=DETAIL_SIZE, color=theme.WARNING)
        self.button(t("OK"), self.close, primary=True)


LoadedCallback = Callable[[BackupLoadResult], Awaitable[None]]
DoneCallback = Callable[[BackupLoadResult | None], None]


class LoadBackupDialog(_BackupDialog):
    """A chosen backup file: read and checked (a problem: why, and nothing changed), then what
    it holds with Replace / Add to what I have / Cancel, then what the load changed.

    `on_loaded`: awaited right after a load, before its result shows (the welcome marks its
    setup as done). `on_done`: after the dialog closed, with the load's result (None: nothing
    loaded). `on_restart`: the result's "Restart now" when the language or accent colour
    changed (without it only the note shows)."""

    def __init__(
        self,
        master: Any,
        *,
        backup: BackupService,
        path: Path,
        on_restart: Callable[[], None] | None = None,
        on_loaded: LoadedCallback | None = None,
        on_done: DoneCallback | None = None,
    ) -> None:
        super().__init__(master, t(LOAD_HEADING))
        self._backup = backup
        self._on_restart = on_restart
        self._on_loaded = on_loaded
        self._on_done = on_done
        self._file: BackupFile | None = None
        self.result: BackupLoadResult | None = None
        self.step(t(LOAD_HEADING))
        self.line(t(READING_TEXT), size=DETAIL_SIZE, color=theme.TEXT_MUTED)
        self.button(t("Cancel"), self.close)
        place_over(self, master)
        activate_later(self)
        self.run(self.read(path), "backup-read")

    async def read(self, path: Path) -> None:
        try:
            backup = await self._backup.open(path)
        except BackupError as exc:
            if self.winfo_exists():
                self.show_problem(str(exc), nothing_changed=True)
            return
        if self.winfo_exists():
            self._file = backup
            self.show_question(summarize(backup))

    def show_problem(self, message: str, *, nothing_changed: bool) -> None:
        self.step(t(PROBLEM_HEADING))
        self.line(message)
        if nothing_changed:
            self.line(t(NOTHING_CHANGED), size=DETAIL_SIZE, color=theme.TEXT_MUTED)
        self.button(t("OK"), self.close, primary=True)

    def show_question(self, summary: BackupSummary) -> None:
        self.step(t(ASK_HEADING))
        self.line(summary_line(summary), bold=True)
        self.line(detail_line(summary), size=DETAIL_SIZE, color=theme.TEXT_MUTED, gap=4)
        self.line(t(REPLACE_BUTTON), size=DETAIL_SIZE, bold=True, gap=_GAP + 4)
        self.line(t(REPLACE_TEXT), size=DETAIL_SIZE, color=theme.TEXT_MUTED, gap=2)
        self.line(t(ADD_BUTTON), size=DETAIL_SIZE, bold=True, gap=_GAP)
        self.line(t(ADD_TEXT), size=DETAIL_SIZE, color=theme.TEXT_MUTED, gap=2)
        self.button(t("Cancel"), self.close)
        self.button(
            t(ADD_BUTTON), lambda: self.choose(LoadMode.ADD), width=_WIDE_BUTTON_WIDTH
        )
        self.button(t(REPLACE_BUTTON), lambda: self.choose(LoadMode.REPLACE), primary=True)

    def choose(self, mode: LoadMode) -> None:
        if self._file is not None and not self.busy:
            self.busy = True
            self.set_buttons("disabled")
            self.run(self.load(self._file, mode), f"backup-load-{mode.value}")

    async def load(self, backup: BackupFile, mode: LoadMode) -> None:
        """Load it (Replace / Add to what I have), then show what changed."""
        self.busy = True
        if self.winfo_exists():
            self.step(t(LOADING_TEXT))
        try:
            result = await self._backup.load(backup, mode)
            self.result = result
            if self._on_loaded is not None:
                await self._on_loaded(result)
        except Exception:
            log.exception("Loading a backup failed")
            if self.winfo_exists():
                self.show_problem(t(LOAD_FAILED), nothing_changed=False)
            return
        finally:
            self.busy = False
        if self.winfo_exists():
            self.show_loaded(result)

    def show_loaded(self, result: BackupLoadResult) -> None:
        self.step(t(LOADED_HEADING))
        for text in result_lines(result):
            self.line(text)
        for index, text in enumerate(result_notes(result)):
            self.line(text, size=DETAIL_SIZE, color=theme.TEXT_MUTED, gap=_GAP if index == 0 else 4)
        note = restart_note(result, current_language(), theme.current_accent())
        if note is not None:
            row = ctk.CTkFrame(self.content, fg_color="transparent")
            row.pack(fill="x", pady=(_GAP, 0))
            room = WIDTH - (_RESTART_BUTTON_WIDTH + 12 if self._on_restart is not None else 0)
            text_label(row, t(note), size=DETAIL_SIZE, color=theme.WARNING, wraplength=room).pack(
                side="left", fill="x", expand=True
            )
            if self._on_restart is not None:
                primary_button(
                    row, t("Restart now"), self._on_restart, width=_RESTART_BUTTON_WIDTH
                ).pack(side="right", padx=(12, 0))
        self.button(t("OK"), self.close, primary=note is None or self._on_restart is None)

    def closed(self) -> None:
        if self._on_done is not None:
            self._on_done(self.result)


def load_backup(
    parent: Any,
    *,
    backup: BackupService,
    on_restart: Callable[[], None] | None = None,
    on_loaded: LoadedCallback | None = None,
    on_done: DoneCallback | None = None,
) -> LoadBackupDialog | None:
    """"Load a backup…": pick the file (`ask_open_path`), then the dialog over `parent`; None
    when no file was picked."""
    path = ask_open_path(parent)
    if path is None or not parent.winfo_exists():
        return None
    return LoadBackupDialog(
        parent,
        backup=backup,
        path=path,
        on_restart=on_restart,
        on_loaded=on_loaded,
        on_done=on_done,
    )
