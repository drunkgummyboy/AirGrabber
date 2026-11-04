"""MediaForge's Settings sections: the MediaForge group of AirGrabber's one Settings window
(app/ui/settings_dialog.py; docs/forge/INTEGRATION.md 3.7, docs/forge/PAGE.md).

The window's sidebar lists FORGE_SECTIONS in order under FORGE_GROUP, and it builds each one with
its `build(page, forge)` when its page is first shown. A later Forge section is one more line in
FORGE_SECTIONS (its own module, imported lazily by its builder); the window doesn't change.

A builder gets the window's page for its section (app/ui/settings_layout.py's `SettingsPage`:
heading and muted line done; `box()` / `add()` below them) and AirGrabber's ForgeRuntime, and
returns a ForgeSectionPart. The sections built so far apply on the window's Save (D9): `problem()`
is asked of each before anything is stored, then each `save()`; Cancel and ✕ change nothing. A
section waits for `forge.start()` (Forge may not have started yet) and says so while Forge is
NOT_READY or FAILED.

The first section, **Auto-rename**, is everything about renaming on its own (the owner's decision:
no page of its own beside it; the queue's status line opens this section). Its first box:
the renamer's light and status, live (`AutoRenamer.watch_status`), Off / Manual / On, the line saying
what the chosen one means, and what Manual and On still need. Then the download folders to watch
(add / remove), what is ignored (the queue's Ignore: each entry's name, its path as a tooltip, with
"Stop ignoring", applied on Save like the rest; app/forge/core/ignored.py's IgnoreService,
`services.ignored`), "Use AirGrabber's downloads" with AirGrabber's folders and why each is (not)
watched, a suggested one's Add, and what to choose (B-01, app/forge/core/airgrabber_folders.py), the
Series and Movies library folders (Browse), and the options: cleaning up the download folder, the
"needs you" notification, the evening summary and whether forged files leave the queue
(`remove_after_forge`). Which preset Series and Movies use is the Naming section's (the library box
says so); Start with Windows stays AirGrabber's (Automatic).

Its safety rules: everything waits for Save; On straight from Off becomes Manual first, and so does
On when a folder starts being watched (nothing moves from a new folder unseen), each saying so; a
watched folder inside a library folder or another watched one is never saved; while Manual or On is
chosen every folder must be chosen too. While the old MediaForge is installed the renamer's guard
keeps it idle and the status says so. Its foot (`LibrarySection.foot`, hidden while empty) holds
"Bring over…" while the old MediaForge's data can still be imported (app/forge/ui/import_step.py's
BringOverBox, F-26) and, while the old MediaForge is still installed, the guard's notice with
"Remove the old MediaForge" (RemoveOldBox, F-27).
"""

from __future__ import annotations

import functools
import logging
import os
import tkinter as tk
from collections.abc import Callable, Sequence
from pathlib import Path
from tkinter import filedialog
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, Protocol

import customtkinter as ctk

from app.forge.core.library import NESTED, OVERLAP, library_problems
from app.forge.models.settings import ForgeSettings
from app.forge.ui.look import app_look, light_colour
from app.forge.ui.settings_menu import MENU_ICON, MENU_NOTE, SECTION_MENU, build_menu
from app.forge.ui.settings_naming import NAMING_ICON, NAMING_NOTE, SECTION_NAMING, build_naming
from app.forge.ui.settings_subtitles import (
    SECTION_SUBTITLES,
    SUBTITLES_ICON,
    SUBTITLES_NOTE,
    build_subtitles,
)
from app.i18n import N_, t
from app.ui import dialog_kit, theme
from app.ui.tasks import spawn
from app.ui.tooltip import add_tooltip

if TYPE_CHECKING:
    import asyncio

    from app.forge.core.auto import WatchStatus
    from app.forge.models.ignored import IgnoredPath
    from app.forge.runtime import ForgeRuntime
    from app.ui.settings_layout import SettingsBox, SettingsPage

log = logging.getLogger(__name__)

# Light at import (Settings imports it when it opens; AirGrabber's start doesn't): nothing
# of Forge's beyond its settings model and folder rules, so Forge's core, forge.db and guessit
# stay unloaded (INTEGRATION.md 3.6).

# -- the registry -------------------------------------------------------------------------------

type Mode = Literal["off", "trial", "on"]

FORGE_GROUP = N_("MediaForge")  # the sidebar group's label
SECTION_LIBRARY = N_("Auto-rename")


class ForgeSectionPart(Protocol):
    """What the Settings window asks of a built Forge section when Save is clicked."""

    def problem(self) -> str | None:
        """Why Save can't store this section's changes (translated), or None. Asked before
        anything is stored."""
        ...

    async def save(self) -> str | None:
        """Store this section's changes; a translated reason when that failed, else None."""
        ...


ForgeSectionBuilder = Callable[["SettingsPage", "ForgeRuntime"], ForgeSectionPart]


class ForgeSection(NamedTuple):
    icon: str  # the sidebar's icon
    note: str  # the page's muted line, an N_() text
    build: ForgeSectionBuilder


# -- texts ----------------------------------------------------------------------------------------

LIBRARY_NOTE = N_(
    "MediaForge can rename finished downloads by itself and move them into your Series and "
    "Movies folders."
)
GETTING_READY = N_("Getting MediaForge ready…")
SAVE_FAILED = N_("MediaForge's settings couldn't be saved. The log has the details.")
ADD_FOLDER = N_("Add folder…")
REMOVE = N_("Remove")
BROWSE = N_("Browse")
CHOOSE_WATCH = N_("Choose a download folder to watch")
CHOOSE_SERIES = N_("Choose the Series library folder")
CHOOSE_MOVIES = N_("Choose the Movies library folder")
LIBRARY_TEXT = N_(
    "Downloads MediaForge finds always move here. Files you add yourself only when their preset "
    "has Move to library folder on."
)
LIBRARY_POINTER = N_("Choose the preset for each folder under Naming › Library presets.")
TRIAL_FIRST = N_(
    "Manual first: downloads wait in the queue until you forge them; switch to On when you're happy."
)
MODES: tuple[tuple[Mode, str], ...] = (("off", N_("Off")), ("trial", N_("Manual")), ("on", N_("On")))
MODE_LINES: dict[Mode, str] = {
    "off": N_("Off: nothing happens by itself; you rename files in the queue."),
    "trial": N_("Manual: downloads it finds wait in the queue; nothing moves until you forge them."),
    "on": N_("On: sure matches are renamed and moved; the rest waits in the queue."),
}
NEEDS_FIRST = N_("Manual and On need these first:")
OPTIONS_TITLE = N_("Options")
CLEAN_TEXT = N_("Clean up downloads after moving (samples, .nfo, .txt, .url, empty folders)")
NEEDS_TEXT = N_("Tell me when files need me")
SUMMARY_TEXT = N_("A summary in the evening")
REMOVE_TEXT = N_("Remove files from the queue after they're forged (Undo stays in History)")
AIRGRABBER_TITLE = N_("AirGrabber's downloads")
AIRGRABBER_HINT = N_(
    "Watch the folders where AirGrabber's downloads end up. They follow AirGrabber's settings; "
    "a suggested folder is only watched once you add it."
)
USE_AIRGRABBER_TEXT = N_("Use AirGrabber's downloads")
IGNORED_TITLE = N_("Ignored")
IGNORED_TEXT = N_("Files and folders Auto-rename leaves alone.")
NOTHING_IGNORED = N_("Nothing ignored.")
STOP_IGNORING = N_("Stop ignoring")
FOLDER_MARK = "📁"  # before an ignored folder
ADD_SUGGESTED = N_("Add")

# What this section edits; Save stores only the ones changed here.
FIELDS: tuple[str, ...] = (
    "use_airgrabber_downloads",
    "watch_folders",
    "series_folder",
    "movies_folder",
    "automatic",
    "clean_downloads",
    "notify_needs",
    "daily_summary",
    "remove_after_forge",
)

_BUTTON_WIDTH = 110  # the dialog's Browse buttons
_ROW_GAP = 8
_LABEL_WIDTH = 90  # "Series" / "Movies"
_PATH_WRAP = 330  # a folder's path next to its label and button (CTk units)
_LIST_WRAP = 420  # a watched folder's path next to its Remove button
_STATUS_WRAP = 500  # the renamer's status next to its light
_STOP_WIDTH = 150  # "Stop ignoring"
_IGNORED_WRAP = 380  # an ignored name next to its Stop ignoring button
_TEXT_SIZE = dialog_kit.SMALL_SIZE + 1  # as the dialog's boxes' explanations


class _Starter(Protocol):
    """What the section needs of the ForgeRuntime (tests pass a fake)."""

    @property
    def reason(self) -> str: ...

    @property
    def services(self) -> _Parts | None: ...

    async def start(self) -> _Parts | None: ...


class _Parts(Protocol):
    @property
    def settings(self) -> SettingsSource: ...


class IgnoredSource(Protocol):
    """What the section needs of IgnoreService (app/forge/core/ignored.py)."""

    @property
    def entries(self) -> tuple[IgnoredPath, ...]: ...

    def subscribe(
        self, listener: Callable[[tuple[IgnoredPath, ...]], None]
    ) -> Callable[[], None]: ...

    async def stop_ignoring(self, path: Path) -> bool: ...


AskFolder = Callable[[str, str], str]  # (title, start folder) → the chosen folder, or ""


class SettingsSource(Protocol):
    """What a section needs of ForgeSettingsService."""

    @property
    def current(self) -> ForgeSettings: ...

    async def update(self, **changes: Any) -> ForgeSettings: ...

    def subscribe(self, listener: Callable[[ForgeSettings], None]) -> Callable[[], None]: ...


class StatusSource(Protocol):
    """What the section needs of the AutoRenamer: its status now and every later one."""

    @property
    def last_status(self) -> WatchStatus | None: ...

    def status(self, unreachable: Sequence[Path]) -> WatchStatus: ...

    def watch_status(self, listener: Callable[[WatchStatus], None]) -> Callable[[], None]: ...


def _same_folder(a: str, b: str) -> bool:
    return os.path.normcase(a.rstrip("\\/")) == os.path.normcase(b.rstrip("\\/"))


def ignored_text(entry: IgnoredPath) -> str:
    """An ignored entry as the list shows it: its name (the whole path is its tooltip), a folder
    with 📁 before it."""
    return f"{FOLDER_MARK}  {entry.name}" if entry.folder else entry.name


# -- Auto-rename ----------------------------------------------------------------------------------


class LibrarySection:
    """The Auto-rename section's contents. Changes stay in the form until Save (`problem` / `save`);
    a change saved elsewhere meanwhile (a folder AirGrabber's settings brought) shows here unless
    the same setting was changed in the form."""

    def __init__(
        self, page: SettingsPage, forge: _Starter, *, ask_folder: AskFolder | None = None
    ) -> None:
        self._page = page
        self._forge = forge
        self._look = app_look()
        self.ask_folder: AskFolder = ask_folder if ask_folder is not None else self._ask_folder
        self._settings: SettingsSource | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._baseline: dict[str, Any] = {}  # what the form showed of the stored settings
        self._watch: list[str] = []
        self._folders: dict[str, str] = {"series": "", "movies": ""}
        self._mode: Mode = "off"
        self._renamer: StatusSource | None = None
        self._unwatch: Callable[[], None] | None = None
        self._labels: dict[Mode, str] = {mode: t(label) for mode, label in MODES}
        frame = page.frame

        # Shown until Forge has started (or why it can't).
        self.waiting = page.box()
        self.waiting_label = self.waiting.text(t(GETTING_READY))
        self.waiting.add(self.waiting_label, gap=0, sticky="w")

        # Renaming on its own: the light and status, Off / Manual / On, what it means, what's needed.
        rename = page.box()
        status_row = rename.frame_row(gap=0)
        self.light = ctk.CTkLabel(
            status_row, text="●", font=theme.font(14), text_color=theme.TEXT_MUTED
        )
        self.light.pack(side="left", padx=(0, 6))
        self.status_label = ctk.CTkLabel(
            status_row,
            text="",
            font=theme.font(dialog_kit.DETAIL_SIZE),
            text_color=theme.TEXT,
            anchor="w",
            justify="left",
            wraplength=_STATUS_WRAP,
        )
        self.status_label.pack(side="left", fill="x", expand=True)
        self.mode = ctk.CTkSegmentedButton(
            rename.body,
            values=list(self._labels.values()),
            width=260,
            height=dialog_kit.BUTTON_HEIGHT,
            corner_radius=dialog_kit.BUTTON_RADIUS,
            font=theme.font(dialog_kit.DETAIL_SIZE, bold=True),
            fg_color=self._look.SURFACE,
            selected_color=self._look.SELECTED,
            selected_hover_color=self._look.SELECTED_HOVER,
            unselected_color=self._look.SURFACE,
            unselected_hover_color=self._look.CHIP_HOVER,
            text_color=theme.BUTTON_TEXT,
            dynamic_resizing=False,
            command=self._mode_clicked,
        )
        rename.add(self.mode, gap=_ROW_GAP, sticky="w")
        self.mode_line = rename.text("")
        rename.add(self.mode_line, gap=_ROW_GAP, sticky="w")
        self.note_label = rename.text("")
        self._note_row = rename.take_row()
        self.problems_label = rename.text("")
        self.problems_label.configure(text_color=theme.WARNING)
        self._problems_row = rename.take_row()

        # The download folders to watch.
        watch = page.box(
            t("Download folders to watch"),
            t("MediaForge looks here when AirGrabber opens and every few minutes for finished downloads."),
        )
        self.watch_list = ctk.CTkFrame(watch.body, fg_color="transparent")
        self.watch_list.grid_columnconfigure(0, weight=1)
        watch.add(self.watch_list)
        self.add_button = self._button(watch.body, t(ADD_FOLDER), self.add_watch_folder)
        watch.add(self.add_button, gap=_ROW_GAP + 2, sticky="w")

        # What Auto-rename leaves alone (the queue's Ignore); Stop ignoring waits for Save.
        self._ignored: IgnoredSource | None = None
        self._ignored_entries: tuple[IgnoredPath, ...] = ()
        self._unignore: list[Path] = []  # stopped ignoring in the form (stored on Save)
        self._unsubscribe_ignored: Callable[[], None] | None = None
        ignored = page.box(t(IGNORED_TITLE), t(IGNORED_TEXT))
        # Never empty (an empty CTkFrame keeps its default 200 units): "Nothing ignored." then.
        self.ignored_list = ctk.CTkFrame(ignored.body, fg_color="transparent")
        self.ignored_list.grid_columnconfigure(0, weight=1)
        ignored.add(self.ignored_list)

        # AirGrabber's downloads (B-01): the switch, AirGrabber's folders and why, what to choose.
        self._use_chosen: bool | None = None  # None: not chosen (on unless brought over)
        airgrabber = page.box(t(AIRGRABBER_TITLE), t(AIRGRABBER_HINT))
        self._use = tk.BooleanVar(master=frame, value=True)
        self.use_box = self._checkbox(airgrabber.body, t(USE_AIRGRABBER_TEXT), self._use)
        self.use_box.configure(command=self._use_toggled)
        airgrabber.add(self.use_box, sticky="w")
        self.airgrabber_list = ctk.CTkFrame(airgrabber.body, fg_color="transparent")
        self.airgrabber_list.grid_columnconfigure(0, weight=1)
        airgrabber.add(self.airgrabber_list, gap=_ROW_GAP)

        # The library folders.
        library = page.box(t("Library folders"), t(LIBRARY_TEXT))
        rows = library.frame_row()
        rows.grid_columnconfigure(1, weight=1)
        self.folder_labels: dict[str, ctk.CTkLabel] = {}
        self.browse_buttons: dict[str, ctk.CTkButton] = {}
        kinds: tuple[Literal["series", "movies"], ...] = ("series", "movies")
        for row_no, kind in enumerate(kinds):
            ctk.CTkLabel(
                rows,
                text=t("Series") if kind == "series" else t("Movies"),
                width=_LABEL_WIDTH,
                anchor="w",
                font=theme.font(dialog_kit.DETAIL_SIZE, bold=True),
                text_color=theme.TEXT,
            ).grid(row=row_no, column=0, sticky="w", pady=(0 if row_no == 0 else _ROW_GAP, 0))
            label = ctk.CTkLabel(
                rows,
                text="",
                anchor="w",
                justify="left",
                wraplength=_PATH_WRAP,
                font=theme.font(_TEXT_SIZE),
            )
            label.grid(
                row=row_no, column=1, sticky="ew", padx=(8, 10),
                pady=(0 if row_no == 0 else _ROW_GAP, 0),
            )
            button = self._button(rows, t(BROWSE), functools.partial(self.choose_library, kind))
            button.grid(row=row_no, column=2, sticky="e", pady=(0 if row_no == 0 else _ROW_GAP, 0))
            self.folder_labels[kind] = label
            self.browse_buttons[kind] = button
        library.add(library.text(t(LIBRARY_POINTER)), gap=_ROW_GAP, sticky="w")

        # Options.
        options = page.box(t(OPTIONS_TITLE))
        self._clean = tk.BooleanVar(master=frame, value=True)
        self._needs = tk.BooleanVar(master=frame, value=True)
        self._summary = tk.BooleanVar(master=frame, value=True)
        self._remove = tk.BooleanVar(master=frame, value=False)
        for text, variable in (
            (CLEAN_TEXT, self._clean),
            (NEEDS_TEXT, self._needs),
            (SUMMARY_TEXT, self._summary),
            (REMOVE_TEXT, self._remove),
        ):
            options.add(self._checkbox(options.body, t(text), variable), gap=_ROW_GAP, sticky="w")

        self._content: list[SettingsBox] = [rename, watch, ignored, airgrabber, library, options]

        # The foot: hidden until something is put in it (F-27's "Remove the old MediaForge").
        self.foot = ctk.CTkFrame(frame, fg_color="transparent")
        page.add(self.foot)
        self.foot.grid_remove()

        tk.Misc.bind(frame, "<Destroy>", self._destroyed, "+")
        services = forge.services
        if services is not None:
            self._ready(services.settings)
        else:
            for box in self._content:
                box.frame.grid_remove()
            self._task = spawn(self._wait(), name="forge-settings-start")

    # --- widgets -------------------------------------------------------------------------------

    def _button(self, master: Any, text: str, command: Callable[[], None]) -> ctk.CTkButton:
        button = dialog_kit.secondary_button(
            master, text, command, width=_BUTTON_WIDTH, look=self._look
        )
        button.configure(text_color=theme.BUTTON_TEXT)
        return button

    def _checkbox(self, master: Any, text: str, variable: tk.BooleanVar) -> ctk.CTkCheckBox:
        return ctk.CTkCheckBox(
            master,
            text=text,
            variable=variable,
            width=0,
            height=dialog_kit.BUTTON_HEIGHT,
            checkbox_width=22,
            checkbox_height=22,
            corner_radius=6,
            font=theme.font(dialog_kit.DETAIL_SIZE),
            fg_color=self._look.ACCENT,
            hover_color=self._look.ACCENT_HOVER,
            border_color=self._look.EDGE,
        )

    def _ask_folder(self, title: str, start: str) -> str:
        # Native modal dialog: the event loop pauses until it closes (as Settings' Browse).
        return filedialog.askdirectory(
            title=title,
            parent=self._page.frame.winfo_toplevel(),
            initialdir=start or None,
            mustexist=True,
        )

    # --- starting --------------------------------------------------------------------------------

    @property
    def ready(self) -> bool:
        """Whether Forge's settings are showing (Forge has started)."""
        return self._settings is not None

    async def _wait(self) -> None:
        services = await self._forge.start()
        if not self.waiting_label.winfo_exists():
            return
        if services is None:
            # NOT_READY (guessit missing) or FAILED: say why; nothing here can be changed.
            self.waiting_label.configure(text=self._forge.reason or t(GETTING_READY))
            return
        self._ready(services.settings)

    def _ready(self, settings: SettingsSource) -> None:
        self._settings = settings
        source: IgnoredSource | None = getattr(self._forge.services, "ignored", None)
        if source is not None:
            self._ignored = source
            self._ignored_entries = source.entries
            self._unsubscribe_ignored = source.subscribe(self._ignored_changed)
        self._load(settings.current)
        self._unsubscribe = settings.subscribe(self._saved_elsewhere)
        renamer: StatusSource | None = getattr(self._forge.services, "auto", None)
        if renamer is not None:
            self._renamer = renamer
            self.show_status(renamer.last_status or renamer.status([]))
            self._unwatch = renamer.watch_status(self.show_status)
        first_gap = self.waiting.frame.grid_info().get("pady")
        self.waiting.frame.grid_remove()
        for box in self._content:
            box.frame.grid()
        if first_gap is not None:  # the first box sits where the waiting box sat
            self._content[0].frame.grid_configure(pady=first_gap)
        # "Bring over…" in the foot while the old MediaForge's data exists (F-26, D13).
        from app.forge.ui.import_step import BringOverBox, RemoveOldBox  # not at import time

        self.bring_over = BringOverBox(self.foot, self._forge.services)
        # The guard's notice and "Remove the old MediaForge" while it is installed (F-27, D4).
        self.remove_old = RemoveOldBox(self.foot, self._forge)

    def _destroyed(self, event: tk.Event[Any]) -> None:
        if event.widget is not self._page.frame:
            return
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._unsubscribe_ignored is not None:
            self._unsubscribe_ignored()
            self._unsubscribe_ignored = None
        if self._unwatch is not None:
            self._unwatch()
            self._unwatch = None

    def show_status(self, status: WatchStatus) -> None:
        """The renamer's status, live: the light's colour and its words."""
        if self.status_label.winfo_exists():
            self.light.configure(text_color=light_colour(status.state))
            self.status_label.configure(text=status.text)

    # --- the form --------------------------------------------------------------------------------

    def _load(self, settings: ForgeSettings) -> None:
        """Show `settings` and take them as what is stored."""
        self._baseline = {name: getattr(settings, name) for name in FIELDS}
        for name in FIELDS:
            self._show(name, self._baseline[name])
        self._refresh()

    def _show(self, name: str, value: Any) -> None:
        if name == "use_airgrabber_downloads":
            self._use_chosen = value
            self._use.set(self._use_on())
        elif name == "watch_folders":
            self._watch = list(value)
        elif name in ("series_folder", "movies_folder"):
            self._folders[name.removesuffix("_folder")] = value
        elif name == "automatic":
            self._mode = value
            self.note_label.configure(text="")
        elif name == "clean_downloads":
            self._clean.set(value)
        elif name == "notify_needs":
            self._needs.set(value)
        elif name == "daily_summary":
            self._summary.set(value)
        elif name == "remove_after_forge":
            self._remove.set(value)

    def draft(self) -> dict[str, Any]:
        """What the form holds, field by field."""
        return {
            "use_airgrabber_downloads": self._use_chosen,
            "watch_folders": list(self._watch),
            "series_folder": self._folders["series"],
            "movies_folder": self._folders["movies"],
            "automatic": self._mode,
            "clean_downloads": bool(self._clean.get()),
            "notify_needs": bool(self._needs.get()),
            "daily_summary": bool(self._summary.get()),
            "remove_after_forge": bool(self._remove.get()),
        }

    def changes(self) -> dict[str, Any]:
        """The fields changed in the form (what Save stores)."""
        if self._settings is None:
            return {}
        return {
            name: value for name, value in self.draft().items() if value != self._baseline[name]
        }

    def _draft_settings(self) -> ForgeSettings:
        assert self._settings is not None
        return self._settings.current.model_copy(update=self.draft())

    def _saved_elsewhere(self, updated: ForgeSettings) -> None:
        """Settings stored meanwhile (or by this form's Save): show each one the form hasn't
        changed; a field changed here keeps the form's value."""
        if not self.waiting_label.winfo_exists():
            return
        draft = self.draft()
        for name in FIELDS:
            value = getattr(updated, name)
            if draft[name] == self._baseline[name] and value != draft[name]:
                self._show(name, value)
            self._baseline[name] = value
        self._refresh()

    def _refresh(self) -> None:
        """The folder lists and labels, the switch and the problems under it."""
        for child in self.watch_list.winfo_children():
            child.destroy()
        if not self._watch:
            ctk.CTkLabel(
                self.watch_list,
                text=t("None yet"),
                font=theme.font(_TEXT_SIZE),
                text_color=theme.TEXT_MUTED,
                anchor="w",
            ).grid(row=0, column=0, sticky="w")
        for row_no, folder in enumerate(self._watch):
            pady = (0 if row_no == 0 else _ROW_GAP // 2, 0)
            ctk.CTkLabel(
                self.watch_list,
                text=folder,
                font=theme.font(_TEXT_SIZE),
                text_color=theme.TEXT,
                anchor="w",
                justify="left",
                wraplength=_LIST_WRAP,
            ).grid(row=row_no, column=0, sticky="w", pady=pady)
            self._button(
                self.watch_list, t(REMOVE), functools.partial(self.remove_watch_folder, folder)
            ).grid(row=row_no, column=1, sticky="e", padx=(10, 0), pady=pady)
        self._refresh_ignored()
        self._refresh_airgrabber()
        for kind, folder in self._folders.items():
            self.folder_labels[kind].configure(
                text=folder or t("Not chosen yet"),
                text_color=theme.TEXT if folder else theme.TEXT_MUTED,
            )
        self.mode.set(self._labels[self._mode])
        problems = library_problems(self._draft_settings()) if self._settings else []
        # The switch can't leave Off while something is missing; it can always go back to Off.
        enabled = not problems or self._mode != "off"
        self.mode.configure(state="normal" if enabled else "disabled")
        self.mode_line.configure(text=t(MODE_LINES[self._mode]))
        self._show_line(self.note_label, self._note_row, str(self.note_label.cget("text")))
        needs = "\n".join([t(NEEDS_FIRST), *(f"•  {p}" for p in problems)]) if problems else ""
        self.problems_label.configure(text=needs)
        self._show_line(self.problems_label, self._problems_row, needs)

    @staticmethod
    def _show_line(label: ctk.CTkLabel, row: int, text: str) -> None:
        if text:
            label.grid(row=row, column=0, sticky="w", pady=(6, 0))
        else:
            label.grid_remove()

    # --- ignored files and folders ------------------------------------------------------------

    @property
    def ignored_shown(self) -> list[IgnoredPath]:
        """The ignored entries the box lists (without those stopped in the form)."""
        stopped = {os.path.normcase(str(path)) for path in self._unignore}
        return [
            e for e in self._ignored_entries if os.path.normcase(str(e.path)) not in stopped
        ]

    @property
    def unignored(self) -> list[Path]:
        """What Save stops ignoring."""
        return list(self._unignore)

    def stop_ignoring(self, path: Path) -> None:
        """Stop ignoring `path` (stored on Save, like the rest of the section)."""
        if all(os.path.normcase(str(path)) != os.path.normcase(str(p)) for p in self._unignore):
            self._unignore.append(path)
        self._refresh_ignored()

    def _ignored_changed(self, entries: tuple[IgnoredPath, ...]) -> None:
        """Ignored (or stopped) elsewhere meanwhile: the list follows."""
        if not self.waiting_label.winfo_exists():
            return
        self._ignored_entries = entries
        keys = {os.path.normcase(str(e.path)) for e in entries}
        self._unignore = [p for p in self._unignore if os.path.normcase(str(p)) in keys]
        self._refresh_ignored()

    def _refresh_ignored(self) -> None:
        for child in self.ignored_list.winfo_children():
            child.destroy()
        shown = self.ignored_shown
        if not shown:
            ctk.CTkLabel(
                self.ignored_list,
                text=t(NOTHING_IGNORED),
                font=theme.font(_TEXT_SIZE),
                text_color=theme.TEXT_MUTED,
                anchor="w",
            ).grid(row=0, column=0, sticky="w")
        for row_no, entry in enumerate(shown):
            pady = (0 if row_no == 0 else _ROW_GAP // 2, 0)
            label = ctk.CTkLabel(
                self.ignored_list,
                text=ignored_text(entry),
                font=theme.font(_TEXT_SIZE),
                text_color=theme.TEXT,
                anchor="w",
                justify="left",
                wraplength=_IGNORED_WRAP,
            )
            label.grid(row=row_no, column=0, sticky="w", pady=pady)
            add_tooltip(label, str(entry.path))
            button = self._button(
                self.ignored_list,
                t(STOP_IGNORING),
                functools.partial(self.stop_ignoring, entry.path),
            )
            button.configure(width=_STOP_WIDTH)
            button.grid(row=row_no, column=1, sticky="e", padx=(10, 0), pady=pady)

    def _use_on(self) -> bool:
        """Whether the form uses AirGrabber's downloads (as `ForgeSettings.airgrabber_on`)."""
        if self._use_chosen is not None:
            return self._use_chosen
        return self._settings is None or not self._settings.current.imported_from

    def _refresh_airgrabber(self) -> None:
        """AirGrabber's folders, each with why (or why not) it is watched, an Add button on a
        suggestion, and what to choose when AirGrabber names no folder on this PC."""
        from app.forge.core.airgrabber_folders import (  # not at import time: see the top
            airgrabber_in_use,
            ask_text,
            use_text,
        )

        for child in self.airgrabber_list.winfo_children():
            child.destroy()
        on = self._use_on()
        self._use.set(on)
        if self._settings is None:
            self.airgrabber_list.grid_remove()
            return
        draft = self._draft_settings()
        row_no = 0
        for use in airgrabber_in_use(draft):
            pady = (0 if row_no == 0 else _ROW_GAP, 0)
            ctk.CTkLabel(
                self.airgrabber_list,
                text=use.folder.path,
                font=theme.font(_TEXT_SIZE),
                text_color=theme.TEXT if on and use.use == "watched" else theme.TEXT_MUTED,
                anchor="w",
                justify="left",
                wraplength=_LIST_WRAP,
            ).grid(row=row_no, column=0, sticky="w", pady=pady)
            if use.use == "suggested":
                self._button(
                    self.airgrabber_list,
                    t(ADD_SUGGESTED),
                    functools.partial(self.add_suggested, use.folder.path),
                ).grid(row=row_no, column=1, rowspan=2, sticky="e", padx=(10, 0), pady=pady)
            ctk.CTkLabel(
                self.airgrabber_list,
                text=use_text(use),
                font=theme.font(dialog_kit.SMALL_SIZE),
                text_color=theme.TEXT_MUTED,
                anchor="w",
                justify="left",
                wraplength=_LIST_WRAP,
            ).grid(row=row_no + 1, column=0, sticky="w")
            row_no += 2
        ask = ask_text(draft.airgrabber) if on else ""
        if ask:
            ctk.CTkLabel(
                self.airgrabber_list,
                text=ask,
                font=theme.font(_TEXT_SIZE),
                text_color=theme.WARNING,
                anchor="w",
                justify="left",
                wraplength=_LIST_WRAP,
            ).grid(
                row=row_no,
                column=0,
                columnspan=2,
                sticky="w",
                pady=(0 if row_no == 0 else _ROW_GAP, 0),
            )
            row_no += 1
        # An empty CTkFrame keeps its default size (200 units tall): without rows the list is
        # taken out, so the box ends under the tick box.
        if row_no:
            self.airgrabber_list.grid()
        else:
            self.airgrabber_list.grid_remove()

    # --- changes (kept in the form until Save) ---------------------------------------------------

    def _use_toggled(self) -> None:
        """'Use AirGrabber's downloads' clicked. Turned on while On is chosen and it adds a
        folder to watch: Manual first, saying so (nothing moves from a new folder unseen)."""
        if self._settings is None:
            return
        before = self._draft_settings()
        self._use_chosen = bool(self._use.get())
        self._trial_for_new_folder(before)
        self._refresh()

    def _trial_for_new_folder(self, before: ForgeSettings) -> None:
        """On is chosen and the form now watches a folder `before` didn't: Manual first."""
        from app.forge.core.airgrabber_folders import newly_watched  # not at import time

        if self._settings is None or self._mode != "on":
            return
        if newly_watched(before, self._draft_settings()):
            self._mode = "trial"
            self.note_label.configure(text=t(TRIAL_FIRST))

    def _add_folder(self, folder: str) -> None:
        if self._settings is None or any(_same_folder(folder, f) for f in self._watch):
            return
        before = self._draft_settings()
        self._watch.append(folder)
        self._trial_for_new_folder(before)

    def add_suggested(self, folder: str) -> None:
        """A suggested folder of AirGrabber's becomes one of the owner's own (until Save)."""
        self._add_folder(folder)
        self._refresh()

    def add_watch_folder(self) -> None:
        chosen = self.ask_folder(t(CHOOSE_WATCH), "")
        if not chosen or not self.waiting_label.winfo_exists():
            return
        self._add_folder(str(Path(chosen)))
        self._refresh()

    def remove_watch_folder(self, folder: str) -> None:
        self._watch = [f for f in self._watch if f != folder]
        self._refresh()

    def choose_library(self, kind: Literal["series", "movies"]) -> None:
        title = CHOOSE_SERIES if kind == "series" else CHOOSE_MOVIES
        chosen = self.ask_folder(t(title), self._folders[kind])
        if not chosen or not self.waiting_label.winfo_exists():
            return
        self._folders[kind] = str(Path(chosen))
        self._refresh()

    def _mode_clicked(self, label: str) -> None:
        self.set_mode(next(m for m, text in self._labels.items() if text == label))

    def set_mode(self, mode: Mode) -> None:
        """Choose Off / Manual / On; On while Off is stored, or with a folder not watched yet,
        becomes Manual first, saying so (see what it would do before it moves anything)."""
        from app.forge.core.airgrabber_folders import newly_watched  # not at import time

        note = ""
        stored = self._baseline.get("automatic", "off")
        if mode == "on" and self._settings is not None:
            new_folder = newly_watched(self._settings.current, self._draft_settings())
            if stored == "off" or new_folder:
                mode, note = "trial", t(TRIAL_FIRST)
        self._mode = mode
        self.note_label.configure(text=note)
        self._refresh()

    # --- Save -------------------------------------------------------------------------------------

    def problem(self) -> str | None:
        """A watched folder inside a library folder or another watched one never saves; while
        Manual or On is chosen, every folder must be chosen too. Unchanged settings never stop
        Save (AirGrabber's own settings can still be saved)."""
        if not self.changes():
            return None
        draft = self._draft_settings()
        problems = library_problems(draft)
        if draft.automatic == "off":
            problems = [p for p in problems if p in (t(OVERLAP), t(NESTED))]
        return problems[0] if problems else None

    async def save(self) -> str | None:
        from app.forge.core.airgrabber_folders import newly_watched  # not at import time

        changes = self.changes()
        stopped = list(self._unignore)
        if self._settings is None or not (changes or stopped):
            return None
        draft = self._draft_settings()
        if draft.automatic == "on" and newly_watched(self._settings.current, draft):
            changes["automatic"] = "trial"  # a folder came in meanwhile: nothing moves unseen
        try:
            if changes:
                await self._settings.update(**changes)
            if stopped and self._ignored is not None:
                for path in stopped:
                    await self._ignored.stop_ignoring(path)
                self._unignore = [p for p in self._unignore if p not in stopped]
        except Exception as exc:  # a bad value, or forge.db couldn't be written
            # The type only: a message could name a folder (with the user's name in it).
            log.error("MediaForge's settings couldn't be saved (%s)", type(exc).__name__)
            return t(SAVE_FAILED)
        return None


def _build_library(page: SettingsPage, forge: ForgeRuntime) -> ForgeSectionPart:
    return LibrarySection(page, forge)


# Settings' MediaForge group, in the sidebar's order.
FORGE_SECTIONS: dict[str, ForgeSection] = {
    SECTION_LIBRARY: ForgeSection("📁", LIBRARY_NOTE, _build_library),
    SECTION_NAMING: ForgeSection(NAMING_ICON, NAMING_NOTE, build_naming),
    SECTION_SUBTITLES: ForgeSection(SUBTITLES_ICON, SUBTITLES_NOTE, build_subtitles),
    SECTION_MENU: ForgeSection(MENU_ICON, MENU_NOTE, build_menu),
}
