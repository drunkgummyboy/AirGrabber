"""The Forge window's Auto-rename page (was "Library"): renaming on its own, set up right here
(docs/forge/INTEGRATION.md 3.7, D3), as a short guided page.

At the top what it does in one sentence, the watching light with the renamer's status
(`AutoRenamer.status`, colours as MediaForge's watching dot) and Off / Trial / On, each with a
line saying what it means; while a step below is missing, why Trial and On can't be chosen yet.
Then three steps, each marked done once it is:
1. Watch these folders — AirGrabber's download folders (B-01: why each is or isn't watched, a
   suggestion's Add) and the owner's own (Remove), plus "Add folder…".
2. Put renamed files here — the Series and the Movies library folder, each with Browse.
3. How files are named — the preset Series and Movies are moved with (`series_preset` /
   `movies_preset`).
And "More in Settings…" as a small link (AirGrabber's Settings at MediaForge › Library &
watching, the window's callback).

Every change is saved at once, with the safety of Settings' Library & watching: a watched folder
inside a library folder or another watched one is never saved; while Trial or On is chosen a
change that leaves a step missing isn't either (it says why); On straight from Off becomes Trial
first, and so does On when a folder starts being watched (nothing moves from a new folder
unseen). While the old MediaForge is installed the renamer's guard keeps it idle and the status
says so. Sections are MediaForge's cards, in the Forge look.
"""

from __future__ import annotations

import functools
import logging
import os
from collections.abc import Callable, Sequence
from pathlib import Path
from tkinter import filedialog
from typing import Any, Literal, Protocol

import customtkinter as ctk

from app.forge.core.airgrabber_folders import (
    airgrabber_in_use,
    ask_text,
    newly_watched,
    use_text,
    watched_folders,
)
from app.forge.core.auto import WatchState, WatchStatus
from app.forge.core.library import NESTED, OVERLAP, library_problems
from app.forge.models.media import TypeHint
from app.forge.models.preset import Preset
from app.forge.models.settings import ForgeSettings
from app.forge.ui.look import forge_look
from app.i18n import N_, t
from app.ui import dialog_kit, tasks, theme

log = logging.getLogger(__name__)

Mode = Literal["off", "trial", "on"]
Kind = Literal["series", "movies"]
MODES: tuple[tuple[Mode, str], ...] = (("off", N_("Off")), ("trial", N_("Trial")), ("on", N_("On")))
TRIAL_FIRST = N_("Trial first: MediaForge shows what it would do; switch to On when you're happy.")
INTRO = N_(
    "MediaForge can rename finished downloads by itself and move them into your Series and "
    "Movies folders."
)
MODE_LINES: dict[Mode, str] = {
    "off": N_("Off: nothing happens by itself; you rename files in the queue."),
    "trial": N_("Trial: the queue shows what it would do; nothing is moved."),
    "on": N_("On: sure matches are renamed and moved; the rest waits in the queue."),
}
NEEDS_FIRST = N_("Trial and On need these first:")
STEP_WATCH = N_("Watch these folders")
STEP_WATCH_HINT = N_("MediaForge looks here every few minutes for downloads that have finished.")
STEP_LIBRARY = N_("Put renamed files here")
STEP_LIBRARY_HINT = N_("Shows and movies are moved here, each named with its preset.")
STEP_NAMING = N_("How files are named")
STEP_NAMING_HINT = N_("The preset each kind is named with when it moves.")
AIRGRABBER_FOLDERS = N_("AirGrabber's downloads")
YOUR_FOLDERS = N_("Your folders")
SAVE_FAILED = N_("That couldn't be saved. The log has the details.")
LIGHT_COLOURS: dict[WatchState, theme.Pair] = {
    "watching": theme.SUCCESS,
    "trial": theme.WARNING,
    "unreachable": theme.DANGER,
}
WRAP = 640  # CTk units
_PATH_WRAP = 460
_BUTTON_WIDTH = 110
_KINDS: tuple[Kind, ...] = ("series", "movies")

AskFolder = Callable[[str, str], str]  # (title, start folder) → the chosen folder, or ""


class SettingsSource(Protocol):
    """What the page needs of ForgeSettingsService."""

    @property
    def current(self) -> ForgeSettings: ...

    async def update(self, **changes: Any) -> ForgeSettings: ...

    def subscribe(self, listener: Callable[[ForgeSettings], None]) -> Callable[[], None]: ...


class StatusSource(Protocol):
    """What the page needs of the AutoRenamer."""

    def status(self, unreachable: Sequence[Path]) -> WatchStatus: ...


class PresetSource(Protocol):
    """What the page needs of PresetService."""

    async def list(self) -> list[Preset]: ...


def light_colour(state: WatchState) -> theme.Pair:
    """The watching light's colour: green watching, amber trial, red unreachable, else muted."""
    return LIGHT_COLOURS.get(state, theme.TEXT_MUTED)


def _same_folder(a: str, b: str) -> bool:
    return os.path.normcase(a.rstrip("\\/")) == os.path.normcase(b.rstrip("\\/"))


def save_problem(draft: ForgeSettings) -> str | None:
    """Why `draft` can't be saved (translated), as Settings' Library & watching says it: a
    watched folder inside a library folder or another watched one, never; while Trial or On is
    chosen, a step that is missing."""
    problems = library_problems(draft)
    if draft.automatic == "off":
        problems = [p for p in problems if p in (t(OVERLAP), t(NESTED))]
    return problems[0] if problems else None


def fits(preset: Preset, kind: Kind) -> bool:
    """A preset that can name `kind`: made for it, or for both (Auto)."""
    wanted = TypeHint.TV if kind == "series" else TypeHint.MOVIE
    return preset.type in (TypeHint.AUTO, wanted)


class LibraryPage(ctk.CTkFrame):
    """The Auto-rename page. `set_watch_status` takes the renamer's `on_status` (the window wires
    it); a change to the watched folders or the mode shows the status the renamer gives for the
    new settings at once; any other settings change keeps the last status. `ask_folder`
    (the folder chooser) is replaceable in tests."""

    def __init__(
        self,
        master: Any,
        settings: SettingsSource,
        renamer: StatusSource,
        on_more_settings: Callable[[], None],
        *,
        look: theme.Look | None = None,
        presets: PresetSource | None = None,
        ask_folder: AskFolder | None = None,
    ) -> None:
        super().__init__(master, fg_color="transparent")
        self._look: theme.Look = forge_look() if look is None else look
        self._settings = settings
        self._renamer = renamer
        self._presets_source = presets
        self.presets: list[Preset] = []
        self.ask_folder: AskFolder = ask_folder if ask_folder is not None else self._ask_folder
        self._labels = {mode: t(label) for mode, label in MODES}
        self._task_running = False
        self._note = ""  # "Trial first: …" after On became Trial
        self._numbers: dict[ctk.CTkLabel, int] = {}  # each step's mark → its number
        look = self._look

        # What it does, the light, Off / Trial / On and what each means.
        box = self._card()
        ctk.CTkLabel(
            box,
            text=t(INTRO),
            font=theme.font(14, bold=True),
            text_color=theme.TEXT,
            justify="left",
            anchor="w",
            wraplength=WRAP,
        ).pack(anchor="w", fill="x")
        row = ctk.CTkFrame(box, fg_color="transparent")
        row.pack(fill="x", pady=(10, 8))
        self.mode = ctk.CTkSegmentedButton(
            row,
            values=list(self._labels.values()),
            height=34,
            corner_radius=dialog_kit.BUTTON_RADIUS,
            font=theme.font(13, bold=True),
            fg_color=look.CHIP,
            selected_color=look.SELECTED,
            selected_hover_color=look.SELECTED_HOVER,
            unselected_color=look.CHIP,
            unselected_hover_color=look.CHIP_HOVER,
            text_color=theme.TEXT,  # as the queue's kind switch (BUTTON_TEXT: 3.9:1 in dark)
            command=self._mode_clicked,
        )
        self.mode.pack(side="left")
        self.light = ctk.CTkLabel(row, text="●", font=theme.font(14), text_color=theme.TEXT_MUTED)
        self.light.pack(side="left", padx=(18, 6))
        self.status_label = ctk.CTkLabel(
            row, text="", font=theme.font(13), text_color=theme.TEXT, anchor="w"
        )
        self.status_label.pack(side="left", fill="x", expand=True)
        self.state: WatchState = "off"
        self.mode_lines: dict[Mode, ctk.CTkLabel] = {}
        for mode, line in MODE_LINES.items():
            label = ctk.CTkLabel(
                box, text=t(line), font=theme.font(12), anchor="w", justify="left",
                wraplength=WRAP, height=20,
            )  # fmt: skip
            label.pack(anchor="w")
            self.mode_lines[mode] = label
        self.note_label = self._line(box, theme.TEXT_MUTED)
        self.problems_label = self._line(box, theme.WARNING)

        # Step 1: the folders to watch.
        box, self.watch_mark, self.watch_problem = self._step(1, STEP_WATCH, STEP_WATCH_HINT)
        self.watch_list = ctk.CTkFrame(box, fg_color="transparent")
        self.watch_list.pack(fill="x")
        self.watch_list.grid_columnconfigure(0, weight=1)
        self.add_button = self._button(box, t("Add folder…"), self._add_clicked)
        self.add_button.pack(anchor="w", pady=(8, 0))

        # Step 2: the library folders.
        box, self.library_mark, self.library_problem = self._step(
            2, STEP_LIBRARY, STEP_LIBRARY_HINT
        )
        rows = ctk.CTkFrame(box, fg_color="transparent")
        rows.pack(fill="x")
        rows.grid_columnconfigure(1, weight=1)
        self.folder_labels: dict[Kind, ctk.CTkLabel] = {}
        self.browse_buttons: dict[Kind, ctk.CTkButton] = {}
        for row_no, kind in enumerate(_KINDS):
            pady = (0 if row_no == 0 else 6, 0)
            self._kind_label(rows, kind).grid(row=row_no, column=0, sticky="w", pady=pady)
            label = ctk.CTkLabel(
                rows, text="", font=theme.font(12), anchor="w", justify="left",
                wraplength=_PATH_WRAP,
            )  # fmt: skip
            label.grid(row=row_no, column=1, sticky="ew", padx=(8, 10), pady=pady)
            button = self._button(
                rows, t("Browse"), functools.partial(self._browse_clicked, kind)
            )
            button.grid(row=row_no, column=2, sticky="e", pady=pady)
            self.folder_labels[kind] = label
            self.browse_buttons[kind] = button

        # Step 3: the presets.
        box, self.naming_mark, _naming_problem = self._step(3, STEP_NAMING, STEP_NAMING_HINT)
        rows = ctk.CTkFrame(box, fg_color="transparent")
        rows.pack(fill="x")
        self.preset_menus: dict[Kind, ctk.CTkOptionMenu] = {}
        for row_no, kind in enumerate(_KINDS):
            pady = (0 if row_no == 0 else 6, 0)
            self._kind_label(rows, kind).grid(row=row_no, column=0, sticky="w", pady=pady)
            menu = ctk.CTkOptionMenu(
                rows,
                values=["…"],
                width=220,
                height=32,
                corner_radius=8,
                fg_color=look.CHIP,
                button_color=look.CHIP_HOVER,
                button_hover_color=look.HOVER,
                text_color=theme.TEXT,
                dropdown_fg_color=look.SURFACE,
                dropdown_hover_color=look.MENU_HOVER,
                dropdown_text_color=theme.TEXT,
                font=theme.font(12),
                dropdown_font=theme.font(12),
                command=functools.partial(self._preset_clicked, kind),
            )
            menu.grid(row=row_no, column=1, sticky="w", padx=(8, 0), pady=pady)
            self.preset_menus[kind] = menu

        # "More in Settings…": a small link at the bottom.
        self.more_button = ctk.CTkButton(
            self,
            text=t("More in Settings…"),
            width=10,
            height=26,
            fg_color="transparent",
            hover_color=look.CHIP,
            text_color=look.LINK,
            font=theme.font(12, underline=True),
            command=on_more_settings,
        )
        self.more_button.pack(anchor="w", padx=6, pady=(4, 8))

        self._shown: ForgeSettings = settings.current
        self.fill(settings.current)
        self.set_watch_status(renamer.status([]))
        self._unsubscribe: Callable[[], None] | None = settings.subscribe(self._settings_saved)
        self.reload_presets()

    # --- building ----------------------------------------------------------------------------

    def _card(self) -> ctk.CTkFrame:
        card = ctk.CTkFrame(self, fg_color=self._look.SURFACE, corner_radius=10)
        card.pack(fill="x", padx=6, pady=6)
        inner = ctk.CTkFrame(card, fg_color="transparent")
        inner.pack(fill="x", padx=16, pady=14)
        return inner

    def _step(
        self, number: int, title: str, hint: str
    ) -> tuple[ctk.CTkFrame, ctk.CTkLabel, ctk.CTkLabel]:
        """A step's card: its mark (the number, a ✓ once done), title and hint; returns the
        body, the mark and the step's warning line."""
        box = self._card()
        head = ctk.CTkFrame(box, fg_color="transparent")
        head.pack(fill="x")
        mark = ctk.CTkLabel(
            head,
            text=str(number),
            width=26,
            height=26,
            corner_radius=13,
            fg_color=self._look.ACCENT,
            text_color=theme.ON_COLOR,
            font=theme.font(12, bold=True),
        )
        mark.pack(side="left", padx=(0, 10))
        self._numbers[mark] = number
        ctk.CTkLabel(
            head, text=t(title), font=theme.font(15, bold=True), text_color=theme.TEXT
        ).pack(side="left")
        ctk.CTkLabel(
            box,
            text=t(hint),
            font=theme.font(12),
            text_color=theme.TEXT_MUTED,
            justify="left",
            anchor="w",
            wraplength=WRAP,
        ).pack(anchor="w", pady=(2, 8))
        body = ctk.CTkFrame(box, fg_color="transparent")
        body.pack(fill="x")
        problem = self._line(box, theme.WARNING)
        return body, mark, problem

    def _line(self, master: Any, colour: theme.Pair) -> ctk.CTkLabel:
        """A line shown only while it has something to say."""
        return ctk.CTkLabel(
            master, text="", font=theme.font(12), text_color=colour, justify="left", anchor="w",
            wraplength=WRAP,
        )  # fmt: skip

    def _button(self, master: Any, text: str, command: Callable[[], None]) -> ctk.CTkButton:
        button = dialog_kit.secondary_button(
            master, text, command, width=_BUTTON_WIDTH, look=self._look
        )
        button.configure(text_color=theme.BUTTON_TEXT, height=32)
        return button

    @staticmethod
    def _kind_label(master: Any, kind: Kind) -> ctk.CTkLabel:
        return ctk.CTkLabel(
            master,
            text=t("Series") if kind == "series" else t("Movies"),
            width=80,
            anchor="w",
            font=theme.font(12, bold=True),
            text_color=theme.TEXT,
        )

    def _ask_folder(self, title: str, start: str) -> str:
        # Native modal dialog: the event loop pauses until it closes (as Settings' Browse).
        return filedialog.askdirectory(
            title=title, parent=self.winfo_toplevel(), initialdir=start or None, mustexist=True
        )

    # --- showing --------------------------------------------------------------------------------

    def set_watch_status(self, status: WatchStatus) -> None:
        """The renamer's status: the light's colour and its words."""
        if not self.winfo_exists():
            return
        self.state = status.state
        self.light.configure(text_color=light_colour(status.state))
        self.status_label.configure(text=status.text)

    def fill(self, settings: ForgeSettings) -> None:
        """Show `settings`: the mode and why Trial / On can't be chosen yet, the three steps."""
        problems = library_problems(settings)
        self.mode.set(self._labels[settings.automatic])
        # Trial and On while a step is missing are refused (`set_mode`), saying why below; the
        # switch itself stays readable, greyed out only while a change is being saved.
        self.mode.configure(state="disabled" if self._task_running else "normal")
        for mode, label in self.mode_lines.items():
            label.configure(
                text_color=theme.TEXT if mode == settings.automatic else theme.TEXT_MUTED
            )
        if settings.automatic != "trial":
            self._note = ""
        self._show_line(self.note_label, self._note)
        needs = "\n".join([t(NEEDS_FIRST), *(f"•  {p}" for p in problems)]) if problems else ""
        self._show_line(self.problems_label, needs)
        self._fill_watch(settings)
        for kind in _KINDS:
            folder = settings.series_folder if kind == "series" else settings.movies_folder
            self.folder_labels[kind].configure(
                text=folder or t("Not chosen yet"),
                text_color=theme.TEXT if folder else theme.TEXT_MUTED,
            )
        self._fill_presets(settings)
        self._mark(self.watch_mark, bool(watched_folders(settings)))
        self._mark(self.library_mark, bool(settings.series_folder and settings.movies_folder))
        self._mark(self.naming_mark, True)

    def _mark(self, mark: ctk.CTkLabel, done: bool) -> None:
        mark.configure(
            text="✓" if done else str(self._numbers[mark]),
            fg_color=theme.SUCCESS if done else self._look.ACCENT,
        )

    @staticmethod
    def _show_line(label: ctk.CTkLabel, text: str) -> None:
        if label.cget("text") != text:
            label.configure(text=text)
        if text and not label.winfo_manager():
            label.pack(anchor="w", pady=(6, 0))
        elif not text and label.winfo_manager():
            label.pack_forget()

    def _heading(self, text: str, row: int) -> int:
        ctk.CTkLabel(
            self.watch_list,
            text=text.upper(),
            font=theme.font(11, bold=True),
            text_color=theme.TEXT_MUTED,
            anchor="w",
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0 if row == 0 else 8, 2))
        return row + 1

    def _fill_watch(self, settings: ForgeSettings) -> None:
        """AirGrabber's folders (why each is or isn't watched, a suggestion's Add, what to
        choose), then the owner's own (Remove)."""
        for child in self.watch_list.winfo_children():
            child.destroy()
        row = 0
        if settings.airgrabber_on:
            uses = airgrabber_in_use(settings)
            ask = ask_text(settings.airgrabber)
            if uses or ask:
                row = self._heading(t(AIRGRABBER_FOLDERS), row)
            for use in uses:
                ctk.CTkLabel(
                    self.watch_list,
                    text=use.folder.path,
                    font=theme.font(12),
                    text_color=theme.TEXT if use.use == "watched" else theme.TEXT_MUTED,
                    anchor="w",
                    justify="left",
                    wraplength=_PATH_WRAP,
                ).grid(row=row, column=0, sticky="w", pady=(2, 0))
                if use.use == "suggested":
                    self._button(
                        self.watch_list,
                        t("Add"),
                        functools.partial(self._suggested_clicked, use.folder.path),
                    ).grid(row=row, column=1, rowspan=2, sticky="e", padx=(10, 0))
                ctk.CTkLabel(
                    self.watch_list,
                    text=use_text(use),
                    font=theme.font(11),
                    text_color=theme.TEXT_MUTED,
                    anchor="w",
                    justify="left",
                    wraplength=_PATH_WRAP,
                ).grid(row=row + 1, column=0, sticky="w")
                row += 2
            if ask:
                ctk.CTkLabel(
                    self.watch_list,
                    text=ask,
                    font=theme.font(12),
                    text_color=theme.WARNING,
                    anchor="w",
                    justify="left",
                    wraplength=_PATH_WRAP,
                ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))
                row += 1
        if settings.watch_folders or row:
            row = self._heading(t(YOUR_FOLDERS), row)
        if not settings.watch_folders:
            ctk.CTkLabel(
                self.watch_list, text=t("None yet"), font=theme.font(12),
                text_color=theme.TEXT_MUTED, anchor="w",
            ).grid(row=row, column=0, sticky="w")  # fmt: skip
        for folder in settings.watch_folders:
            ctk.CTkLabel(
                self.watch_list,
                text=folder,
                font=theme.font(12),
                text_color=theme.TEXT,
                anchor="w",
                justify="left",
                wraplength=_PATH_WRAP,
            ).grid(row=row, column=0, sticky="w", pady=(2, 0))
            self._button(
                self.watch_list, t("Remove"), functools.partial(self._remove_clicked, folder)
            ).grid(row=row, column=1, sticky="e", padx=(10, 0), pady=(2, 0))
            row += 1

    def _fill_presets(self, settings: ForgeSettings) -> None:
        for kind in _KINDS:
            menu = self.preset_menus[kind]
            current = settings.series_preset if kind == "series" else settings.movies_preset
            names = [p.name for p in self.presets if fits(p, kind)]
            if current not in names:
                names.insert(0, current)  # what is stored, even before the presets are read
            if list(menu.cget("values")) != names:
                menu.configure(values=names)
            if menu.get() != current:
                menu.set(current)

    def reload_presets(self) -> None:
        """Read the presets (again): the page shows, Settings › Naming may have changed them."""
        if self._presets_source is not None:
            tasks.spawn(self._load_presets(), name="forge-auto-presets")

    async def _load_presets(self) -> None:
        source = self._presets_source
        if source is None:
            return
        presets = await source.list()
        if self.winfo_exists():
            self.presets = presets
            self._fill_presets(self._settings.current)

    def _settings_saved(self, settings: ForgeSettings) -> None:
        if not self.winfo_exists():
            return
        before, self._shown = self._shown, settings
        self.fill(settings)
        # Only a change the status depends on recomputes it; otherwise the last status the window
        # passed in (e.g. "Can't reach") stays, matching the window's band.
        if (watched_folders(before), before.automatic) != (
            watched_folders(settings),
            settings.automatic,
        ):
            self.set_watch_status(self._renamer.status([]))

    # --- saving -----------------------------------------------------------------------------

    async def save(self, problem_label: ctk.CTkLabel | None = None, **changes: Any) -> bool:
        """Save `changes` at once, as Settings would: refused (saying why under
        `problem_label`, or under the mode) when `save_problem` finds one; On becomes Trial
        when a folder starts being watched. True when saved."""
        label = problem_label or self.problems_label
        current = self._settings.current
        draft = current.model_copy(update=changes)
        problem = save_problem(draft)
        if problem is not None:
            self._show_line(label, problem)
            return False
        if draft.automatic == "on" and newly_watched(current, draft):
            changes["automatic"] = "trial"  # nothing moves from a new folder unseen
            self._note = t(TRIAL_FIRST)
        try:
            await self._settings.update(**changes)
        except Exception as exc:  # a bad value, or forge.db couldn't be written
            # The type only: a message could name a folder (with the user's name in it).
            log.error("The Auto-rename page's change couldn't be saved (%s)", type(exc).__name__)
            if self.winfo_exists():
                self._show_line(label, t(SAVE_FAILED))
            return False
        if self.winfo_exists():
            for line in (self.watch_problem, self.library_problem):
                if line is not label:
                    self._show_line(line, "")
            if label is not self.problems_label:
                self._show_line(label, "")
            self.fill(self._settings.current)
        return True

    async def add_watch_folder(self) -> bool:
        chosen = self.ask_folder(t("Choose a download folder to watch"), "")
        if not chosen or not self.winfo_exists():
            return False
        folder = str(Path(chosen))
        current = self._settings.current.watch_folders
        if any(_same_folder(folder, f) for f in current):
            return False
        return await self.save(self.watch_problem, watch_folders=[*current, folder])

    async def add_suggested(self, folder: str) -> bool:
        """A suggested folder of AirGrabber's becomes one of the owner's own."""
        current = self._settings.current.watch_folders
        if any(_same_folder(folder, f) for f in current):
            return False
        return await self.save(self.watch_problem, watch_folders=[*current, folder])

    async def remove_watch_folder(self, folder: str) -> bool:
        current = self._settings.current.watch_folders
        return await self.save(
            self.watch_problem, watch_folders=[f for f in current if f != folder]
        )

    async def choose_library(self, kind: Kind) -> bool:
        current = self._settings.current
        start = current.series_folder if kind == "series" else current.movies_folder
        title = (
            t("Choose the Series library folder")
            if kind == "series"
            else t("Choose the Movies library folder")
        )
        chosen = self.ask_folder(title, start)
        if not chosen or not self.winfo_exists():
            return False
        return await self.save(self.library_problem, **{f"{kind}_folder": str(Path(chosen))})

    async def set_preset(self, kind: Kind, name: str) -> bool:
        return await self.save(None, **{f"{kind}_preset": name})

    # --- clicks -------------------------------------------------------------------------------

    def _spawn(self, coro: Any) -> None:
        tasks.spawn(coro, name="forge-auto-rename")

    def _add_clicked(self) -> None:
        self._spawn(self.add_watch_folder())

    def _suggested_clicked(self, folder: str) -> None:
        self._spawn(self.add_suggested(folder))

    def _remove_clicked(self, folder: str) -> None:
        self._spawn(self.remove_watch_folder(folder))

    def _browse_clicked(self, kind: Kind) -> None:
        self._spawn(self.choose_library(kind))

    def _preset_clicked(self, kind: Kind, name: str) -> None:
        self._spawn(self.set_preset(kind, name))

    # --- the switch -----------------------------------------------------------------------------

    def _mode_clicked(self, label: str) -> None:
        mode = next(m for m, text in self._labels.items() if text == label)
        tasks.spawn(self.set_mode(mode), name="forge-library-mode")

    async def set_mode(self, mode: Mode) -> None:
        """Save the mode; On straight from Off becomes Trial first, with a note saying so;
        Trial and On only once every step is done."""
        note = ""
        if mode == "on" and self._settings.current.automatic == "off":
            mode, note = "trial", t(TRIAL_FIRST)  # see what it would do before it moves anything
        if mode != "off" and library_problems(self._settings.current):
            self.fill(self._settings.current)  # the switch shows what is still in use
            return
        self._task_running = True
        self.mode.configure(state="disabled")
        try:
            settings = await self._settings.update(automatic=mode)
        except BaseException:
            self._task_running = False
            if self.winfo_exists():
                self.fill(self._settings.current)  # the switch shows what is still in use
            raise
        self._task_running = False
        if not self.winfo_exists():
            return
        self._note = note
        self.fill(settings)

    def destroy(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        super().destroy()
