"""Settings › Sorting's rule editor: one rule in a small window of its own, modal over Settings,
one question per line (docs/forge/2026-11-18-rules-design.md, the mockup's section 2): name, the
folder it watches (and its subfolders), what it takes, the preset and extras per kind, then
(move / sort inside the folder) and how (Automatic / Manual). Save hands `draft()` to `on_save`,
which stores it (Settings' instant saving) or says why it can't: then the editor stays open with
that reason in red under the questions. Delete calls `on_delete`, Cancel changes nothing.

- The extras are ticked as in the chosen preset with this rule's changes on top; `draft()` keeps
  only the ticks that differ from the preset (RuleTarget.extras), so a preset edited later still
  reaches the ticks the rule never touched. A preset gone shows PRESET_GONE under its menu and
  keeps the rule's stored extras. Each kind's menu lists the presets that can name it
  (`queue_parts.fits`), and the rule's own preset even when it can't or is gone.
- "Anything" has a preset (and with a move a destination) per kind; the kind not taken keeps
  what it had, hidden.
- At its foot, the live check: the folder read as a look reads it (`peek`, app/forge/core/
  rule_check.py's: a thread under the look's time limit, None when it can't be reached) and
  explained (`rule_check.explain`, words from app/forge/ui/rule_rows.py). It runs again 300 ms
  after a change of the folder (read again) or of subfolders, take or quality (what was read
  explained again: a NAS asleep isn't asked again for each click); an answer arriving after a
  newer look started or after the window closed is dropped. AirGrabber's rule: one check per
  folder.
- Under the questions, this rule's notes (`rules.rule_notes` against the other rules, live).

No title bar (app/ui/borderless.py), never `transient` (see app/forge/ui/widgets/dialogs.py); it
hands the grab back to Settings when it closes, as app/forge/ui/matches_window.py does.
"""

from __future__ import annotations

import asyncio
import tkinter as tk
from collections.abc import Awaitable, Callable, Sequence
from functools import partial
from pathlib import Path
from typing import Any, Literal

import customtkinter as ctk

from app.forge.core.formatter import name_file
from app.forge.core.rule_check import explain
from app.forge.core.rules import PRESET_GONE, rule_notes
from app.forge.models.media import MediaKind, ParsedMedia, ResolvedMedia
from app.forge.models.preset import Preset, PresetOptions, with_options
from app.forge.models.rules import Rule, RuleQuality, RuleTake, RuleTarget
from app.forge.models.settings import ForgeSettings
from app.forge.ui.look import app_look
from app.forge.ui.queue_parts import OPTION_LABELS, fits
from app.forge.ui.queue_rows import Kind
from app.forge.ui.rule_rows import check_lines, last_name, made_name, name_of
from app.forge.ui.settings_naming import (
    SAMPLE_EPISODE,
    SAMPLE_EXTENSION,
    SAMPLE_MOVIE,
    quiet_line,
)
from app.i18n import N_, t
from app.ui import theme
from app.ui.borderless import clearance, make_borderless
from app.ui.branding import set_window_icon
from app.ui.dialog_kit import (
    BUTTON_HEIGHT,
    BUTTON_RADIUS,
    DETAIL_SIZE,
    FitScroll,
    LINE_GAP,
    activate_later,
    heading_row,
    max_dialog_height,
    place_over,
    primary_button,
    secondary_button,
)
from app.ui.tasks import spawn

WIDTH = 620
PAD_X = 25
PAD_Y = 20
CAPTION_WIDTH = 150
CONTENT_WIDTH = WIDTH - CAPTION_WIDTH - 14
TEXT_SIZE = 13
ROW_PAD = 10  # above and below each question's line
SMALL_GAP = 6
BUTTON_WIDTH = 100
BUTTONS_GAP = 16  # above the buttons
MENU_WIDTH = 260
RADIO_SIZE = 18
ROOM_AROUND = 160  # the heading and the buttons, when the questions get their height
CHECK_DELAY_MS = 300  # the live check waits this long after a change

EDIT_TITLE = N_("Edit rule")
NEW_TITLE = N_("New rule")
NAME_CAPTION = N_("Name")
NAME_HINT = N_(
    "Shown in the queue as “Rule: {name}”. Leave it empty to have one made from your choices."
)
FOLDER_CAPTION = N_("Watch this folder")
BROWSE = N_("Browse…")
CHOOSE_WATCHED = N_("Choose the folder this rule watches")
WITH_SUBFOLDERS = N_(
    "Also look in the folders inside it (a download that comes in its own folder, a season pack)"
)
TOP_ONLY = N_("Only the files directly in this folder")
AIRGRABBER_FOLDERS = N_("The folders AirGrabber downloads to")
TAKE_CAPTION = N_("Take")
TAKES: dict[RuleTake, str] = {
    "anything": N_("Anything"),
    "shows": N_("Shows"),
    "films": N_("Films"),
}
QUALITIES: dict[RuleQuality, str] = {
    "any": N_("Any quality"),
    "4k": N_("4K only"),
    "not_4k": N_("Not 4K"),
}
FORGE_CAPTION = N_("Forge them with")
KIND_CAPTIONS: dict[MediaKind, str] = {MediaKind.EPISODE: N_("Shows"), MediaKind.MOVIE: N_("Films")}
NAMES_LIKE = N_("This preset names them like: {example}")
TICKS_NOTE = N_("Ticked as in the preset. A change here is for this rule only.")
THEN_CAPTION = N_("Then")
MOVE_TO = N_("Move them to")
LIBRARY_LABELS: dict[MediaKind, str] = {
    MediaKind.EPISODE: N_("Your Series folder"),
    MediaKind.MOVIE: N_("Your Movies folder"),
}
CHOOSE_DESTINATION = N_("Choose the folder to move them to")
SORT_HERE = N_("Sort them inside this folder")
SORT_NOTE = N_("(the preset's folders are made here, e.g. {example})")
HOW_CAPTION = N_("How")
AUTOMATIC = N_("Automatic")
AUTOMATIC_NOTE = N_("Done by itself when MediaForge is sure. If it isn't, the file waits for you.")
MANUAL = N_("Manual")
MANUAL_NOTE = N_(
    "Put in the queue with this rule filled in. Nothing happens until you press Forge."
)
RIGHT_NOW = N_("Right now in {folder}")
LOOKING = N_("Looking…")
UNREACHABLE = N_("Can't reach this folder right now")
NO_DOWNLOADS = N_("No downloads in this folder right now.")
DELETE = N_("Delete rule")
CANCEL = N_("Cancel")
SAVE = N_("Save")

# The extras a rule may change, per kind, in the order shown (OPTION_LABELS' words).
KIND_EXTRAS: dict[MediaKind, tuple[str, ...]] = {
    MediaKind.MOVIE: (
        "subtitles", "movie_poster", "backdrop", "trailer", "quality_in_name", "nfo"
    ),
    MediaKind.EPISODE: (
        "subtitles", "series_poster", "season_poster", "backdrop", "trailer",
        "quality_in_name", "nfo",
    ),
}
_KINDS: tuple[MediaKind, ...] = (MediaKind.EPISODE, MediaKind.MOVIE)
_QUEUE_KINDS: dict[MediaKind, Kind] = {MediaKind.EPISODE: "series", MediaKind.MOVIE: "movies"}
_SAMPLES: dict[MediaKind, ResolvedMedia] = {
    MediaKind.EPISODE: SAMPLE_EPISODE,
    MediaKind.MOVIE: SAMPLE_MOVIE,
}
_NOTE_COLOURS: dict[str, theme.Color] = {
    "refuse": theme.DANGER,
    "warn": theme.WARNING,
    "info": theme.TEXT_MUTED,
}

type Recheck = Literal["read", "explain"]  # what a change needs of the live check
Files = list[tuple[Path, ParsedMedia]]
Peek = Callable[[Path], Awaitable[Files | None]]
AskFolder = Callable[[str, str], str]  # (title, start folder) → the chosen folder, or ""


class RuleEditor(ctk.CTkToplevel):
    """`rule` at `index` of `rules` (`index == len(rules)`: a new rule); `presets` the stored
    presets; `airgrabber_folders` the folders AirGrabber's rule watches (shown, checked);
    `peek(folder)` the live check's reading; `ask_folder(title, start)` the folder picker;
    `on_save(rule)` (returns why it can't be stored, else None) / `on_delete()` (None: no
    Delete, as for AirGrabber's rule and a new one)."""

    def __init__(
        self,
        master: Any,
        *,
        rule: Rule,
        index: int,
        rules: Sequence[Rule],
        settings: ForgeSettings,
        presets: Sequence[Preset],
        airgrabber_folders: Sequence[str],
        peek: Peek,
        ask_folder: AskFolder,
        on_save: Callable[[Rule], str | None],
        on_delete: Callable[[], None] | None,
    ) -> None:
        look = app_look()
        super().__init__(master, fg_color=look.BG)
        self._look = look
        self._rule = rule
        self._index = index
        self._rules = list(rules)
        self._settings = settings
        self._presets = list(presets)
        self._airgrabber_folders = list(airgrabber_folders)
        self._peek = peek
        self._ask_folder = ask_folder
        self._on_save = on_save
        self._on_delete = on_delete
        self._folder = rule.folder
        self._building = True  # the variables' traces wait until everything is there
        self._closed = False
        self._check_job: str | None = None
        self._check_task: asyncio.Task[None] | None = None
        self._check_generation = 0
        self._pending_read = False  # the debounced check reads the folder again
        # The last reading: the folders and what each held (None: couldn't be reached).
        self._read: tuple[list[Path], list[Files | None]] | None = None
        self.check_lines: list[ctk.CTkLabel] = []
        # Settings holds the grab while it shows: it gets it back when this closes.
        self._grab_before: tk.Misc | None = self.grab_current()
        title = t(EDIT_TITLE) if index < len(rules) else t(NEW_TITLE)
        self.title(title)
        set_window_icon(self)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.cancel)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)
        ctk.CTkFrame(body, width=WIDTH, height=1, fg_color="transparent").pack()  # the width
        heading = heading_row(body, title, right_room=clearance(PAD_X))
        heading.pack(fill="x", pady=(0, LINE_GAP))
        self.scroll = FitScroll(body, follow=True)
        self.scroll.pack(fill="both", expand=True)
        self._form = ctk.CTkFrame(self.scroll, fg_color="transparent")
        self._form.grid(row=0, column=0, sticky="nsew")
        self._form.grid_columnconfigure(1, weight=1)
        self._rows = 0

        self._build_name()
        self._build_folder()
        self._build_take()
        self._build_forge()
        self._build_then()
        self._build_how()
        self.notes_label = self._label(self._form, "", wrap=WIDTH)
        self.notes_label.grid(row=self._next_row(), column=0, columnspan=2, sticky="w")
        self.check_frame = ctk.CTkFrame(
            self._form,
            fg_color=look.SURFACE,
            border_color=look.EDGE,
            border_width=1,
            corner_radius=8,
        )
        self.check_frame.grid(
            row=self._next_row(), column=0, columnspan=2, sticky="ew", pady=(ROW_PAD, 0)
        )
        self.check_frame.grid_columnconfigure(0, weight=1)

        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(fill="x", pady=(BUTTONS_GAP, 0))
        self.delete_button: ctk.CTkButton | None = None
        if on_delete is not None:
            self.delete_button = secondary_button(
                buttons, t(DELETE), self.delete, width=BUTTON_WIDTH + 30, look=look
            )
            self.delete_button.configure(text_color=theme.DANGER)
            self.delete_button.pack(side="left")
        self.save_button = primary_button(
            buttons, t(SAVE), self.save, width=BUTTON_WIDTH, look=look
        )
        self.save_button.pack(side="right")
        self.cancel_button = secondary_button(
            buttons, t(CANCEL), self.cancel, width=BUTTON_WIDTH, look=look
        )
        self.cancel_button.configure(text_color=theme.BUTTON_TEXT)
        self.cancel_button.pack(side="right", padx=(0, 8))

        scale = float(ctk.ScalingTracker.get_widget_scaling(self)) or 1.0
        self.scroll.set_limit(round((max_dialog_height(self, scale) - ROOM_AROUND) * scale))
        self._building = False
        self._changed()
        self.refresh_check()

        self.bind("<Escape>", lambda _event: self.cancel())
        make_borderless(self, drag_widgets=(self, body, heading), on_close=self.cancel, look=look)
        place_over(self, master)
        activate_later(self)

    # --- building ------------------------------------------------------------------------------

    def _next_row(self) -> int:
        self._rows += 1
        return self._rows

    def _question(self, caption: str) -> ctk.CTkFrame:
        """A question's line: the caption at the left, its answer's frame (returned) beside it;
        a thin line above all but the first."""
        if self._rows:
            quiet_line(self._form, self._look).grid(
                row=self._next_row(), column=0, columnspan=2, sticky="ew"
            )
        row = self._next_row()
        ctk.CTkLabel(
            self._form,
            text=caption,
            width=CAPTION_WIDTH,
            anchor="nw",
            font=theme.font(DETAIL_SIZE, bold=True),
            text_color=theme.TEXT,
        ).grid(row=row, column=0, sticky="nw", pady=ROW_PAD)
        answer = ctk.CTkFrame(self._form, fg_color="transparent")
        answer.grid(row=row, column=1, sticky="ew", padx=(14, 0), pady=ROW_PAD)
        answer.grid_columnconfigure(0, weight=1)
        return answer

    def _label(
        self,
        master: Any,
        text: str,
        *,
        color: theme.Color = theme.TEXT_MUTED,
        wrap: int = CONTENT_WIDTH,
        bold: bool = False,
        size: int = TEXT_SIZE,
    ) -> ctk.CTkLabel:
        return ctk.CTkLabel(
            master,
            text=text,
            font=theme.font(size, bold=bold),
            text_color=color,
            anchor="w",
            justify="left",
            wraplength=wrap,
        )

    def _button(self, master: Any, text: str, command: Callable[[], None]) -> ctk.CTkButton:
        button = secondary_button(master, text, command, width=BUTTON_WIDTH, look=self._look)
        button.configure(text_color=theme.BUTTON_TEXT, height=BUTTON_HEIGHT - 6)
        return button

    def _segments(self, master: Any, labels: Sequence[str], variable: tk.StringVar) -> Any:
        return ctk.CTkSegmentedButton(
            master,
            values=list(labels),
            variable=variable,
            height=BUTTON_HEIGHT - 6,
            corner_radius=BUTTON_RADIUS,
            font=theme.font(TEXT_SIZE, bold=True),
            fg_color=self._look.SURFACE,
            selected_color=self._look.SELECTED,
            selected_hover_color=self._look.SELECTED_HOVER,
            unselected_color=self._look.SURFACE,
            unselected_hover_color=self._look.CHIP_HOVER,
            text_color=theme.BUTTON_TEXT,
        )

    def _menu(
        self, master: Any, values: Sequence[str], variable: tk.StringVar
    ) -> ctk.CTkOptionMenu:
        return ctk.CTkOptionMenu(
            master,
            values=list(values) or [""],
            variable=variable,
            width=MENU_WIDTH,
            height=BUTTON_HEIGHT - 6,
            corner_radius=BUTTON_RADIUS,
            font=theme.font(TEXT_SIZE),
            dropdown_font=theme.font(TEXT_SIZE),
            fg_color=self._look.SURFACE,
            button_color=self._look.EDGE,
            button_hover_color=self._look.HOVER,
            text_color=theme.BUTTON_TEXT,
            dynamic_resizing=False,
        )

    def _choice(
        self,
        master: Any,
        variable: tk.Variable,
        value: bool | str,
        text: str,
        note: str = "",
        *,
        bold: bool = False,
    ) -> ctk.CTkFrame:
        """A radio button with wrapping words (a click on them chooses it too), and a muted
        note under them."""
        line = ctk.CTkFrame(master, fg_color="transparent")
        line.grid_columnconfigure(1, weight=1)
        ctk.CTkRadioButton(
            line,
            text="",
            variable=variable,
            value=value,
            width=RADIO_SIZE,
            radiobutton_width=RADIO_SIZE,
            radiobutton_height=RADIO_SIZE,
            fg_color=self._look.ACCENT,
            hover_color=self._look.ACCENT_HOVER,
            border_color=self._look.EDGE,
        ).grid(row=0, column=0, sticky="nw", pady=(2, 0))
        words = self._label(
            line, text, color=theme.TEXT, wrap=CONTENT_WIDTH - RADIO_SIZE - 10, bold=bold
        )
        words.grid(row=0, column=1, sticky="w", padx=(8, 0))
        words.bind("<Button-1>", lambda _event: variable.set(value))
        if note:
            self._label(line, note, wrap=CONTENT_WIDTH - RADIO_SIZE - 10).grid(
                row=1, column=1, sticky="w", padx=(8, 0)
            )
        return line

    def _watch(self, variable: tk.Variable, *, check: Recheck | None = None) -> None:
        variable.trace_add("write", lambda *_args: self._changed(check=check))

    def _build_name(self) -> None:
        answer = self._question(t(NAME_CAPTION))
        self.name_entry = ctk.CTkEntry(
            answer,
            height=BUTTON_HEIGHT - 6,
            corner_radius=BUTTON_RADIUS,
            font=theme.font(DETAIL_SIZE),
            fg_color=self._look.SURFACE,
            border_color=self._look.EDGE,
            placeholder_text=made_name(self._rule),
            placeholder_text_color=self._look.PLACEHOLDER,
        )
        if self._rule.name:
            self.name_entry.insert(0, self._rule.name)
        self.name_entry.grid(row=0, column=0, sticky="ew")
        self.name_entry.bind("<KeyRelease>", lambda _event: self._changed(), add="+")
        self.name_hint = self._label(answer, "")
        self.name_hint.grid(row=1, column=0, sticky="w", pady=(SMALL_GAP, 0))

    def _build_folder(self) -> None:
        answer = self._question(t(FOLDER_CAPTION))
        self.browse_button: ctk.CTkButton | None = None
        line = ctk.CTkFrame(answer, fg_color="transparent")
        line.grid(row=0, column=0, sticky="ew")
        line.grid_columnconfigure(0, weight=1)
        if self._rule.airgrabber:
            self.folder_label = self._label(line, t(AIRGRABBER_FOLDERS), color=theme.TEXT)
            self.folder_label.grid(row=0, column=0, sticky="w")
            for row, folder in enumerate(self._airgrabber_folders, 1):
                self._label(line, folder).grid(row=row, column=0, sticky="w")
        else:
            self.folder_label = self._label(
                line, self._folder, color=theme.TEXT, wrap=CONTENT_WIDTH - BUTTON_WIDTH - 10
            )
            self.folder_label.grid(row=0, column=0, sticky="w")
            self.browse_button = self._button(line, t(BROWSE), self.choose_folder)
            self.browse_button.grid(row=0, column=1, sticky="e", padx=(10, 0))
        self.subfolders = tk.BooleanVar(master=self, value=self._rule.subfolders)
        self._choice(answer, self.subfolders, True, t(WITH_SUBFOLDERS)).grid(
            row=1, column=0, sticky="ew", pady=(SMALL_GAP + 2, 0)
        )
        self._choice(answer, self.subfolders, False, t(TOP_ONLY)).grid(
            row=2, column=0, sticky="ew", pady=(SMALL_GAP, 0)
        )
        self._watch(self.subfolders, check="explain")

    def _build_take(self) -> None:
        answer = self._question(t(TAKE_CAPTION))
        self._take_labels = {value: t(text) for value, text in TAKES.items()}
        self._quality_labels = {value: t(text) for value, text in QUALITIES.items()}
        self._take_var = tk.StringVar(master=self, value=self._take_labels[self._rule.take])
        self._quality_var = tk.StringVar(
            master=self, value=self._quality_labels[self._rule.quality]
        )
        self.take = self._segments(answer, list(self._take_labels.values()), self._take_var)
        self.take.grid(row=0, column=0, sticky="w")
        self.quality = self._segments(
            answer, list(self._quality_labels.values()), self._quality_var
        )
        self.quality.grid(row=1, column=0, sticky="w", pady=(SMALL_GAP + 2, 0))
        self._watch(self._take_var, check="explain")
        self._watch(self._quality_var, check="explain")

    def _build_forge(self) -> None:
        answer = self._question(t(FORGE_CAPTION))
        self.preset_menus: dict[MediaKind, ctk.CTkOptionMenu] = {}
        self.ticks: dict[tuple[MediaKind, str], tk.BooleanVar] = {}
        self._preset_vars: dict[MediaKind, tk.StringVar] = {}
        self._ticked_for: dict[MediaKind, str] = {}  # the preset each kind's ticks were set for
        self._kind_frames: dict[MediaKind, ctk.CTkFrame] = {}
        self._kind_captions: dict[MediaKind, ctk.CTkLabel] = {}
        self._examples: dict[MediaKind, ctk.CTkLabel] = {}
        self._gone: dict[MediaKind, ctk.CTkLabel] = {}
        for row, kind in enumerate(_KINDS):
            target = self._rule.target(kind)
            frame = ctk.CTkFrame(answer, fg_color="transparent")
            frame.grid(row=row, column=0, sticky="ew", pady=(0, SMALL_GAP))
            frame.grid_columnconfigure(0, weight=1)
            self._kind_frames[kind] = frame
            caption = self._label(frame, t(KIND_CAPTIONS[kind]), color=theme.TEXT, bold=True)
            caption.grid(row=0, column=0, sticky="w", pady=(0, 2))
            self._kind_captions[kind] = caption
            variable = tk.StringVar(master=self, value=target.preset)
            self._preset_vars[kind] = variable
            self._ticked_for[kind] = target.preset
            menu = self._menu(frame, self.preset_names(kind), variable)
            menu.grid(row=1, column=0, sticky="w")
            self.preset_menus[kind] = menu
            gone = self._label(frame, t(PRESET_GONE), color=theme.WARNING)
            gone.grid(row=2, column=0, sticky="w", pady=(SMALL_GAP, 0))
            self._gone[kind] = gone
            example = self._label(frame, "")
            example.grid(row=3, column=0, sticky="w", pady=(SMALL_GAP, 0))
            self._examples[kind] = example
            boxes = ctk.CTkFrame(frame, fg_color="transparent")
            boxes.grid(row=4, column=0, sticky="w", pady=(SMALL_GAP, 0))
            options = self._preset_options(target.preset)
            for number, key in enumerate(KIND_EXTRAS[kind]):
                tick = tk.BooleanVar(
                    master=self, value=target.extras.get(key, getattr(options, key))
                )
                self.ticks[kind, key] = tick
                self._checkbox(boxes, t(OPTION_LABELS[key]), tick).grid(
                    row=number // 2, column=number % 2, sticky="w", padx=(0, 16), pady=1
                )
                self._watch(tick)
            variable.trace_add("write", partial(self._preset_chosen, kind))
        self._label(answer, t(TICKS_NOTE)).grid(row=len(_KINDS), column=0, sticky="w")

    def preset_names(self, kind: MediaKind) -> list[str]:
        """`kind`'s preset menu: the presets that can name it (made for it, or Auto; all when
        none can), with the rule's own preset kept last when it isn't among them (one for the
        other kind, or gone) so it still shows."""
        fitting = [preset.name for preset in self._presets if fits(preset, _QUEUE_KINDS[kind])]
        names = fitting or [preset.name for preset in self._presets]
        own = self._rule.target(kind).preset
        return names if own in names else [*names, own]

    def _checkbox(self, master: Any, text: str, variable: tk.BooleanVar) -> ctk.CTkCheckBox:
        return ctk.CTkCheckBox(
            master,
            text=text,
            variable=variable,
            width=0,
            height=26,
            checkbox_width=18,
            checkbox_height=18,
            corner_radius=4,
            font=theme.font(TEXT_SIZE),
            fg_color=self._look.ACCENT,
            hover_color=self._look.ACCENT_HOVER,
            border_color=self._look.EDGE,
        )

    def _build_then(self) -> None:
        answer = self._question(t(THEN_CAPTION))
        self.then = tk.StringVar(master=self, value="stays" if self._rule.stays else "move")
        self._choice(answer, self.then, "move", t(MOVE_TO)).grid(row=0, column=0, sticky="ew")
        # Hidden as a whole for Stays: an empty CTkFrame would keep its default size.
        self._destinations = ctk.CTkFrame(answer, fg_color="transparent")
        self._destinations.grid(row=1, column=0, sticky="ew", padx=(RADIO_SIZE + 8, 0))
        self._destinations.grid_columnconfigure(1, weight=1)
        self.destination_menus: dict[MediaKind, ctk.CTkOptionMenu] = {}
        self.destination_buttons: dict[MediaKind, ctk.CTkButton] = {}
        self._destination_vars: dict[MediaKind, tk.StringVar] = {}
        self._destination_paths: dict[MediaKind, str] = {}
        self._destination_parts: dict[MediaKind, tuple[ctk.CTkLabel, ctk.CTkFrame]] = {}
        for row, kind in enumerate(_KINDS):
            target = self._rule.target(kind)
            library = t(LIBRARY_LABELS[kind])
            self._destination_paths[kind] = target.folder
            chosen = target.folder if target.to == "folder" else library
            variable = tk.StringVar(master=self, value=chosen)
            self._destination_vars[kind] = variable
            caption = self._label(self._destinations, t(KIND_CAPTIONS[kind]), color=theme.TEXT)
            caption.grid(row=row, column=0, sticky="w", padx=(0, 8), pady=(SMALL_GAP, 0))
            line = ctk.CTkFrame(self._destinations, fg_color="transparent")
            line.grid(row=row, column=1, sticky="w", pady=(SMALL_GAP, 0))
            menu = self._menu(line, self._destination_values(kind), variable)
            menu.configure(width=MENU_WIDTH - 40)
            menu.grid(row=0, column=0, sticky="w")
            button = self._button(line, t(BROWSE), partial(self.choose_destination, kind))
            button.grid(row=0, column=1, sticky="w", padx=(8, 0))
            self.destination_menus[kind] = menu
            self.destination_buttons[kind] = button
            self._destination_parts[kind] = (caption, line)
            self._watch(variable)
        self._choice(answer, self.then, "stays", t(SORT_HERE)).grid(
            row=2, column=0, sticky="ew", pady=(SMALL_GAP + 2, 0)
        )
        self.sort_note = self._label(answer, "", wrap=CONTENT_WIDTH - RADIO_SIZE - 10)
        self.sort_note.grid(row=3, column=0, sticky="w", padx=(RADIO_SIZE + 8, 0))
        self._watch(self.then)

    def _build_how(self) -> None:
        answer = self._question(t(HOW_CAPTION))
        self.mode = tk.StringVar(master=self, value=self._rule.mode)
        self._choice(answer, self.mode, "auto", t(AUTOMATIC), t(AUTOMATIC_NOTE), bold=True).grid(
            row=0, column=0, sticky="ew"
        )
        self._choice(answer, self.mode, "manual", t(MANUAL), t(MANUAL_NOTE), bold=True).grid(
            row=1, column=0, sticky="ew", pady=(SMALL_GAP + 2, 0)
        )

    # --- the draft -----------------------------------------------------------------------------

    def _preset_options(self, preset_name: str) -> PresetOptions:
        """The extras of the preset called `preset_name`; none ticked when it is gone."""
        preset = self._preset(preset_name)
        return preset.options if preset is not None else PresetOptions()

    def _preset(self, name: str) -> Preset | None:
        return next((preset for preset in self._presets if preset.name == name), None)

    def _extras(self, kind: MediaKind, preset_name: str | None = None) -> dict[str, bool]:
        """The ticks of `kind` that differ from its preset's (or `preset_name`'s). While the
        rule's own preset is gone its stored extras stay too (nothing to compare them with)."""
        name = preset_name or self._preset_vars[kind].get()
        options = self._preset_options(name)
        target = self._rule.target(kind)
        kept = target.extras if name == target.preset and self._preset(name) is None else {}
        return {
            key: self.ticks[kind, key].get()
            for key in KIND_EXTRAS[kind]
            if self.ticks[kind, key].get() != getattr(options, key) or key in kept
        }

    def _destination_values(self, kind: MediaKind) -> list[str]:
        path = self._destination_paths[kind]
        return [t(LIBRARY_LABELS[kind]), *([path] if path else [])]

    def _target(self, kind: MediaKind) -> RuleTarget:
        chosen = self._destination_vars[kind].get()
        to_folder = chosen != t(LIBRARY_LABELS[kind])
        return RuleTarget(
            preset=self._preset_vars[kind].get(),
            extras=self._extras(kind),
            to="folder" if to_folder else "library",
            folder=chosen if to_folder else "",
        )

    def _value_of[V: str](self, labels: dict[V, str], label: str, fallback: V) -> V:
        return next((value for value, text in labels.items() if text == label), fallback)

    def draft(self) -> Rule:
        """The rule as the window shows it now."""
        mode = "auto" if self.mode.get() == "auto" else "manual"
        return self._rule.model_copy(
            update={
                "name": self.name_entry.get().strip(),
                "folder": "" if self._rule.airgrabber else self._folder,
                "subfolders": bool(self.subfolders.get()),
                "take": self._value_of(self._take_labels, self._take_var.get(), self._rule.take),
                "quality": self._value_of(
                    self._quality_labels, self._quality_var.get(), self._rule.quality
                ),
                "stays": self.then.get() == "stays",
                "shows": self._target(MediaKind.EPISODE),
                "films": self._target(MediaKind.MOVIE),
                "mode": mode,
            }
        )

    def _with_draft(self, draft: Rule) -> list[Rule]:
        """The rules with `draft` in its place (a new rule last)."""
        rules = list(self._rules)
        if self._index < len(rules):
            rules[self._index] = draft
        else:
            rules.append(draft)
        return rules

    # --- changes -------------------------------------------------------------------------------

    def _preset_chosen(self, kind: MediaKind, *_args: object) -> None:
        """Another preset for `kind`: its ticks as in that preset, with this rule's changes."""
        if self._building:
            return
        changed = self._extras(kind, self._ticked_for[kind])
        chosen = self._preset_vars[kind].get()
        self._ticked_for[kind] = chosen
        options = self._preset_options(chosen)
        self._building = True  # one refresh for all the ticks
        try:
            for key in KIND_EXTRAS[kind]:
                self.ticks[kind, key].set(changed.get(key, getattr(options, key)))
        finally:
            self._building = False
        self._changed()

    def choose_folder(self) -> None:
        """Browse… for the folder this rule watches."""
        chosen = self._ask_folder(t(CHOOSE_WATCHED), self._folder)
        if not chosen or self._closed or not self.winfo_exists():
            return
        self._folder = str(Path(chosen))
        self.folder_label.configure(text=self._folder)
        self._changed(check="read")

    def choose_destination(self, kind: MediaKind) -> None:
        """Browse… for the folder `kind` moves to: chosen in its menu."""
        start = self._destination_paths[kind]
        chosen = self._ask_folder(t(CHOOSE_DESTINATION), start)
        if not chosen or self._closed or not self.winfo_exists():
            return
        self._destination_paths[kind] = str(Path(chosen))
        menu = self.destination_menus[kind]
        menu.configure(values=self._destination_values(kind))
        self._destination_vars[kind].set(self._destination_paths[kind])
        self.then.set("move")

    def _changed(self, *, check: Recheck | None = None) -> None:
        """The words that follow the choices; with `check`, the live check again soon (the
        folder read again, or what was read explained again)."""
        if self._building or self._closed or not self.winfo_exists():
            return
        draft = self.draft()
        name = made_name(draft)
        if str(self.name_entry.cget("placeholder_text")) != name:
            self.name_entry.configure(placeholder_text=name)
        self.name_hint.configure(text=t(NAME_HINT, name=name_of(draft)))
        kinds = draft.kinds()
        both = len(kinds) > 1
        for kind in _KINDS:
            shown = kind in kinds
            self._show(self._kind_frames[kind], shown)
            self._show(self._kind_captions[kind], both)
            for part in self._destination_parts[kind]:
                self._show(part, shown and not draft.stays)
            self._show(self._destination_parts[kind][0], both and not draft.stays and shown)
            preset = self._preset(draft.target(kind).preset)
            self._show(self._gone[kind], preset is None)
            example = self._example(kind, draft)
            self._show(self._examples[kind], example is not None)
            if example is not None:
                self._examples[kind].configure(text=t(NAMES_LIKE, example=example))
        self._show(self._destinations, not draft.stays)
        first = self._example(kinds[0], draft)
        parts = [self._folder_name(), first or "…"]
        self.sort_note.configure(
            text=t(SORT_NOTE, example="\\".join(part for part in parts if part))
        )
        self._show_notes(draft)
        if check is not None:
            self._check_soon(check)

    def _show(self, widget: tk.Widget, shown: bool) -> None:
        if shown and not widget.winfo_manager():
            widget.grid()
        elif not shown and widget.winfo_manager():
            widget.grid_remove()

    def _folder_name(self) -> str:
        if self._rule.airgrabber:
            return last_name(self._airgrabber_folders[0]) if self._airgrabber_folders else ""
        return last_name(self._folder) if self._folder else ""

    def _example(self, kind: MediaKind, draft: Rule) -> str | None:
        """How `kind`'s preset (with the ticks) names a made-up film or episode; None when the
        preset is gone."""
        target = draft.target(kind)
        preset = self._preset(target.preset)
        if preset is None:
            return None
        options = preset.options.model_copy(update=target.extras)
        rendered = name_file(with_options(preset.format, options), _SAMPLES[kind], SAMPLE_EXTENSION)
        return "\\".join(rendered.parts)

    def _show_notes(self, draft: Rule) -> None:
        names = [preset.name for preset in self._presets]
        gone = t(PRESET_GONE)  # said under its menu already
        notes = [
            note
            for note in rule_notes(self._with_draft(draft), self._settings, presets=names)[
                self._index
            ]
            if note.text != gone
        ]
        levels = {note.level for note in notes}
        worst = next((level for level in ("refuse", "warn", "info") if level in levels), "info")
        self.notes_label.configure(
            text="\n".join(note.text for note in notes), text_color=_NOTE_COLOURS[worst]
        )
        if notes and not self.notes_label.winfo_manager():
            self.notes_label.grid()
        elif not notes and self.notes_label.winfo_manager():
            self.notes_label.grid_remove()

    # --- the live check ------------------------------------------------------------------------

    def _check_folders(self) -> list[Path]:
        if self._rule.airgrabber:
            return [Path(folder) for folder in self._airgrabber_folders]
        return [Path(self._folder)] if self._folder else []

    def _check_soon(self, check: Recheck) -> None:
        """The live check CHECK_DELAY_MS after the last change: a read pending stays one."""
        if self._check_job is not None:
            self.after_cancel(self._check_job)
        self._pending_read = self._pending_read or check == "read"
        self._check_job = self.after(CHECK_DELAY_MS, self._recheck)

    def _recheck(self) -> None:
        """What the debounced check does: the folder read again (a new folder, or nothing read
        for these folders yet), else what was read explained again (a take / quality /
        subfolder change: no new reading). A reading under way explains with the choices as
        they are when it ends."""
        self._check_job = None
        read, self._pending_read = self._pending_read, False
        if self._closed or not self.winfo_exists():
            return
        folders = self._check_folders()
        if self._check_task is not None and not read:
            return
        if not read and self._read is not None and self._read[0] == folders:
            self._explain(*self._read)
            return
        self.refresh_check()

    def _stop_check(self) -> None:
        self._check_generation += 1
        if self._check_job is not None:
            try:
                self.after_cancel(self._check_job)
            except tk.TclError:
                pass
            self._check_job = None
        self._pending_read = False
        if self._check_task is not None:
            self._check_task.cancel()
            self._check_task = None

    def refresh_check(self) -> None:
        """Look in the folder(s) again now and say what this rule would take."""
        if self._closed or not self.winfo_exists():
            return
        self._stop_check()
        folders = self._check_folders()
        if not folders:
            self._show_check([])
            return
        self._show_check([(folder, [(t(LOOKING), theme.TEXT_MUTED)]) for folder in folders])
        self._check_task = spawn(
            self._check(self._check_generation, folders), name="forge-rule-check"
        )

    async def _check(self, generation: int, folders: Sequence[Path]) -> None:
        found = await asyncio.gather(*(self._peek(folder) for folder in folders))
        if generation != self._check_generation or self._closed or not self.winfo_exists():
            return  # a newer look started, or the window closed
        self._check_task = None
        self._read = (list(folders), list(found))
        self._explain(folders, found)

    def _explain(self, folders: Sequence[Path], found: Sequence[Files | None]) -> None:
        """What was read in `folders`, explained for the choices as they are now."""
        draft = self.draft()
        rules = self._with_draft(draft)
        shown: list[tuple[Path, list[tuple[str, theme.Color]]]] = []
        for folder, files in zip(folders, found, strict=True):
            if files is None:
                shown.append((folder, [(t(UNREACHABLE), theme.WARNING)]))
            elif not files:
                shown.append((folder, [(t(NO_DOWNLOADS), theme.TEXT_MUTED)]))
            else:
                groups = explain(self._index, rules, self._settings, folder, files)
                # The lines that fit come first, one per group (only misses are merged).
                lines = check_lines(groups)
                fitting = sum(1 for group in groups if group.reason == "fits")
                colours = [theme.TEXT] * fitting + [theme.TEXT_MUTED] * (len(lines) - fitting)
                shown.append((folder, list(zip(lines, colours, strict=True))))
        self._show_check(shown)

    def _show_check(
        self, folders: Sequence[tuple[Path, Sequence[tuple[str, theme.Color]]]]
    ) -> None:
        """Each folder's heading and its lines in their colours (what fits in TEXT, the rest
        muted); the box hidden without a folder."""
        for child in self.check_frame.winfo_children():
            child.destroy()
        self.check_lines = []
        self._show(self.check_frame, bool(folders))
        row = 0
        for folder, lines in folders:
            self._label(
                self.check_frame,
                t(RIGHT_NOW, folder=last_name(str(folder))),
                color=theme.SUCCESS,
                bold=True,
                wrap=WIDTH - 30,
            ).grid(row=row, column=0, sticky="w", padx=12, pady=(10 if row == 0 else 8, 2))
            row += 1
            for line, colour in lines:
                label = self._label(self.check_frame, line, color=colour, wrap=WIDTH - 40)
                label.grid(row=row, column=0, sticky="w", padx=(20, 12))
                self.check_lines.append(label)
                row += 1
        if self.check_lines:
            self.check_lines[-1].grid_configure(pady=(0, 10))  # inside the box's border
        self.scroll.refit()

    # --- closing -------------------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    def save(self) -> None:
        """Save: the draft to `on_save`, and close; a draft it refuses keeps the editor open,
        the reason under the questions."""
        if self._closed:
            return
        draft = self.draft()
        refused = self._on_save(draft)
        if refused:
            self.notes_label.configure(text=refused, text_color=theme.DANGER)
            if not self.notes_label.winfo_manager():
                self.notes_label.grid()
            return
        self._close()

    def cancel(self) -> None:
        """Cancel / Esc / ✕: nothing changes."""
        self._close()

    def delete(self) -> None:
        """Delete rule: `on_delete`, and close."""
        if self._closed or self._on_delete is None:
            return
        on_delete = self._on_delete
        self._close()
        on_delete()

    def destroy(self) -> None:
        self._stop_check()
        self._closed = True
        super().destroy()

    def _close(self) -> None:
        if self._closed:
            return
        grab = self._grab_before
        if self.winfo_exists():
            self.destroy()
        self._closed = True
        try:
            if grab is not None and grab.winfo_exists():
                grab.grab_set()  # Settings, modal again
                grab.focus_force()
        except tk.TclError:  # gone meanwhile: only the focus is lost
            pass
