"""Settings › MediaForge: the sections of AirGrabber's Settings that belong to Forge
(docs/forge/INTEGRATION.md 3.7).

`app/ui/settings_dialog.py` only has a hook: its sidebar's third group, FORGE_GROUP, lists
FORGE_SECTIONS in order, and it builds each one with its `build(page, forge)`. A later Forge
section is one more line in FORGE_SECTIONS (its own module, imported lazily by its builder);
the dialog doesn't change.

A builder gets the dialog's page for its section (`SettingsPage`: heading and muted line done;
`box()` / `add()` below them) and AirGrabber's ForgeRuntime, and returns a ForgeSectionPart. The
sections apply on Settings' Save, like the rest of the dialog (D9): `problem()` is asked before
anything is stored, `save()` after AirGrabber's own settings were stored; Cancel and ✕ change
nothing. A section waits for `forge.start()` (Forge may not have started yet) and says so while
Forge is NOT_READY or FAILED.

The first section, **Library & watching**, is MediaForge's Settings › Library tab
(`ui/library_tab.py`): the download folders to watch (add / remove), "Use AirGrabber's downloads"
with AirGrabber's folders and why each is (not) watched, a suggested one's Add, and what to choose
(B-01, app/forge/core/airgrabber_folders.py), the Series and Movies library
folders (Browse), renaming on its own Off / Trial / On (On straight from Off becomes Trial first),
cleaning up the download folder, the "needs you" notification and the evening summary. Which
preset Series and Movies use is the Naming section's; Start with Windows stays AirGrabber's
(Automatic). Its foot (`LibrarySection.foot`, hidden while empty) holds "Bring over…" while the
old MediaForge's data can still be imported (app/forge/ui/import_step.py's BringOverBox, F-26)
and, while the old MediaForge is still installed, the guard's notice with "Remove the old
MediaForge" (RemoveOldBox, F-27).
"""

from __future__ import annotations

import functools
import logging
import os
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, Protocol

import customtkinter as ctk

from app.forge.core.library import NESTED, OVERLAP, library_problems
from app.forge.models.settings import ForgeSettings
from app.forge.ui.settings_menu import MENU_ICON, MENU_NOTE, SECTION_MENU, build_menu
from app.forge.ui.settings_naming import NAMING_ICON, NAMING_NOTE, SECTION_NAMING, build_naming
from app.i18n import N_, t
from app.ui import dialog_kit, theme
from app.ui.tasks import spawn

if TYPE_CHECKING:
    import asyncio

    from app.forge.runtime import ForgeRuntime
    from app.forge.ui.library_page import Mode, SettingsSource
    from app.ui.settings_dialog import SettingsBox, SettingsPage

log = logging.getLogger(__name__)

# This module is imported with the Settings dialog, at AirGrabber's start: it imports nothing of
# Forge's beyond its settings model and folder rules (the Library page's texts are imported
# where they are used), so Forge's core, forge.db and guessit stay unloaded (INTEGRATION.md 3.6).

# -- the registry -------------------------------------------------------------------------------

FORGE_GROUP = N_("MediaForge")  # the sidebar group's label
SECTION_LIBRARY = N_("Library & watching")


class ForgeSectionPart(Protocol):
    """What the dialog asks of a built Forge section when Save is clicked."""

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

LIBRARY_NOTE = N_("Where MediaForge finds finished downloads, and where it moves them.")
GETTING_READY = N_("Getting MediaForge ready…")
SAVE_FAILED = N_("MediaForge's settings couldn't be saved. The log has the details.")
ADD_FOLDER = N_("Add folder…")
REMOVE = N_("Remove")
BROWSE = N_("Browse")
CHOOSE_WATCH = N_("Choose a download folder to watch")
CHOOSE_SERIES = N_("Choose the Series library folder")
CHOOSE_MOVIES = N_("Choose the Movies library folder")
OPTIONS_TITLE = N_("Options")
CLEAN_TEXT = N_("Clean up downloads after moving (samples, .nfo, .txt, .url, empty folders)")
NEEDS_TEXT = N_("Tell me when files need me")
SUMMARY_TEXT = N_("A summary in the evening")
AIRGRABBER_TITLE = N_("AirGrabber's downloads")
AIRGRABBER_HINT = N_(
    "Watch the folders where AirGrabber's downloads end up. They follow AirGrabber's settings; "
    "a suggested folder is only watched once you add it."
)
USE_AIRGRABBER_TEXT = N_("Use AirGrabber's downloads")
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
)

_BUTTON_WIDTH = 110  # the dialog's Browse buttons
_ROW_GAP = 8
_LABEL_WIDTH = 90  # "Series" / "Movies"
_PATH_WRAP = 330  # a folder's path next to its label and button (CTk units)
_LIST_WRAP = 420  # a watched folder's path next to its Remove button
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


AskFolder = Callable[[str, str], str]  # (title, start folder) → the chosen folder, or ""


def _same_folder(a: str, b: str) -> bool:
    return os.path.normcase(a.rstrip("\\/")) == os.path.normcase(b.rstrip("\\/"))


# -- Library & watching ---------------------------------------------------------------------------


class LibrarySection:
    """The Library & watching page's contents. Changes stay in the form until Save
    (`problem` / `save`); a change saved elsewhere meanwhile (the Forge window's Off / Trial /
    On) shows here unless the same setting was changed in the form."""

    def __init__(
        self, page: SettingsPage, forge: _Starter, *, ask_folder: AskFolder | None = None
    ) -> None:
        self._page = page
        self._forge = forge
        self.ask_folder: AskFolder = ask_folder if ask_folder is not None else self._ask_folder
        self._settings: SettingsSource | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._baseline: dict[str, Any] = {}  # what the form showed of the stored settings
        self._watch: list[str] = []
        self._folders: dict[str, str] = {"series": "", "movies": ""}
        self._mode: Mode = "off"
        from app.forge.ui.library_page import MODES  # not at import time: see the top

        self._labels: dict[Mode, str] = {mode: t(label) for mode, label in MODES}
        frame = page.frame

        # Shown until Forge has started (or why it can't).
        self.waiting = page.box()
        self.waiting_label = self.waiting.text(t(GETTING_READY))
        self.waiting.add(self.waiting_label, gap=0, sticky="w")

        # The download folders to watch.
        watch = page.box(
            t("Download folders to watch"),
            t("MediaForge looks here every few minutes for downloads that have finished."),
        )
        self.watch_list = ctk.CTkFrame(watch.body, fg_color="transparent")
        self.watch_list.grid_columnconfigure(0, weight=1)
        watch.add(self.watch_list)
        self.add_button = self._button(watch.body, t(ADD_FOLDER), self.add_watch_folder)
        watch.add(self.add_button, gap=_ROW_GAP + 2, sticky="w")

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
        library = page.box(
            t("Library folders"), t("Shows and movies are moved here, each named with its preset.")
        )
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

        # Renaming on its own: Off / Trial / On.
        rename = page.box(
            t("Rename on its own"),
            t(
                "Sure matches are renamed and moved by themselves; anything else waits in the "
                "queue. Trial shows what would happen without moving anything."
            ),
        )
        self.mode = ctk.CTkSegmentedButton(
            rename.body,
            values=list(self._labels.values()),
            width=260,
            height=dialog_kit.BUTTON_HEIGHT,
            corner_radius=dialog_kit.BUTTON_RADIUS,
            font=theme.font(dialog_kit.DETAIL_SIZE, bold=True),
            fg_color=theme.SURFACE,
            selected_color=theme.SELECTED,
            selected_hover_color=theme.SELECTED_HOVER,
            unselected_color=theme.SURFACE,
            unselected_hover_color=theme.CHIP_HOVER,
            text_color=theme.BUTTON_TEXT,
            dynamic_resizing=False,
            command=self._mode_clicked,
        )
        rename.add(self.mode, sticky="w")
        self.note_label = rename.text("")
        self._note_row = rename.take_row()
        self.problems_label = rename.text("")
        self.problems_label.configure(text_color=theme.WARNING)
        self._problems_row = rename.take_row()

        # Options.
        options = page.box(t(OPTIONS_TITLE))
        self._clean = tk.BooleanVar(master=frame, value=True)
        self._needs = tk.BooleanVar(master=frame, value=True)
        self._summary = tk.BooleanVar(master=frame, value=True)
        for text, variable in (
            (CLEAN_TEXT, self._clean),
            (NEEDS_TEXT, self._needs),
            (SUMMARY_TEXT, self._summary),
        ):
            options.add(self._checkbox(options.body, t(text), variable), gap=_ROW_GAP, sticky="w")

        self._content: list[SettingsBox] = [watch, airgrabber, library, rename, options]

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

    @staticmethod
    def _button(master: Any, text: str, command: Callable[[], None]) -> ctk.CTkButton:
        button = dialog_kit.secondary_button(master, text, command, width=_BUTTON_WIDTH)
        button.configure(text_color=theme.BUTTON_TEXT)
        return button

    @staticmethod
    def _checkbox(master: Any, text: str, variable: tk.BooleanVar) -> ctk.CTkCheckBox:
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
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            border_color=theme.EDGE,
        )

    def _ask_folder(self, title: str, start: str) -> str:
        # Native modal dialog: the event loop pauses until it closes (as the dialog's Browse).
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
        self._load(settings.current)
        self._unsubscribe = settings.subscribe(self._saved_elsewhere)
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
        self._show_line(self.note_label, self._note_row, str(self.note_label.cget("text")))
        self.problems_label.configure(text="\n".join(problems))
        self._show_line(self.problems_label, self._problems_row, "\n".join(problems))

    @staticmethod
    def _show_line(label: ctk.CTkLabel, row: int, text: str) -> None:
        if text:
            label.grid(row=row, column=0, sticky="w", pady=(6, 0))
        else:
            label.grid_remove()

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

    # --- changes (kept in the form until Save) ---------------------------------------------------

    def _use_toggled(self) -> None:
        """'Use AirGrabber's downloads' clicked. Turned on while On is chosen and it adds a
        folder to watch: Trial first, saying so (nothing moves from a new folder unseen)."""
        from app.forge.core.airgrabber_folders import newly_watched  # not at import time
        from app.forge.ui.library_page import TRIAL_FIRST

        if self._settings is None:
            return
        before = self._draft_settings()
        self._use_chosen = bool(self._use.get())
        if self._mode == "on" and newly_watched(before, self._draft_settings()):
            self._mode = "trial"
            self.note_label.configure(text=t(TRIAL_FIRST))
        self._refresh()

    def add_suggested(self, folder: str) -> None:
        """A suggested folder of AirGrabber's becomes one of the owner's own (until Save)."""
        if not any(_same_folder(folder, f) for f in self._watch):
            self._watch.append(folder)
        self._refresh()

    def add_watch_folder(self) -> None:
        chosen = self.ask_folder(t(CHOOSE_WATCH), "")
        if not chosen or not self.waiting_label.winfo_exists():
            return
        folder = str(Path(chosen))
        if not any(_same_folder(folder, f) for f in self._watch):
            self._watch.append(folder)
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
        """Choose Off / Trial / On; On while Off is stored becomes Trial first, saying so."""
        from app.forge.ui.library_page import TRIAL_FIRST  # not at import time: see the top

        note = ""
        stored = self._baseline.get("automatic", "off")
        if mode == "on" and stored == "off":
            mode, note = "trial", t(TRIAL_FIRST)  # see what it would do before it moves anything
        self._mode = mode
        self.note_label.configure(text=note)
        self._refresh()

    # --- Save -------------------------------------------------------------------------------------

    def problem(self) -> str | None:
        """A watched folder inside a library folder or another watched one never saves; while
        Trial or On is chosen, every folder must be chosen too. Unchanged settings never stop
        Save (AirGrabber's own settings can still be saved)."""
        if not self.changes():
            return None
        draft = self._draft_settings()
        problems = library_problems(draft)
        if draft.automatic == "off":
            problems = [p for p in problems if p in (t(OVERLAP), t(NESTED))]
        return problems[0] if problems else None

    async def save(self) -> str | None:
        changes = self.changes()
        if self._settings is None or not changes:
            return None
        try:
            await self._settings.update(**changes)
        except Exception as exc:  # a bad value, or forge.db couldn't be written
            # The type only: a message could name a folder (with the user's name in it).
            log.error("MediaForge's settings couldn't be saved (%s)", type(exc).__name__)
            return t(SAVE_FAILED)
        return None


def _build_library(page: SettingsPage, forge: ForgeRuntime) -> ForgeSectionPart:
    return LibrarySection(page, forge)


# The MediaForge group's sections, in the sidebar's order.
FORGE_SECTIONS: dict[str, ForgeSection] = {
    SECTION_LIBRARY: ForgeSection("📁", LIBRARY_NOTE, _build_library),
    SECTION_NAMING: ForgeSection(NAMING_ICON, NAMING_NOTE, build_naming),
    SECTION_MENU: ForgeSection(MENU_ICON, MENU_NOTE, build_menu),
}
