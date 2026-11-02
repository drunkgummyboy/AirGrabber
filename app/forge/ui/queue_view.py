"""The queue: what will be renamed, to what, and Forge (ported from MediaForge's ui/queue_view.py;
its pure part, the rows, is app/forge/ui/queue_rows.py). The owner's design:
mockups/forge-chosen.html.

At the top a toolbar: "+ Files", "+ Folder ▾" (its menu: choose a folder, "Scan subfolders"),
then "Clear queue", Preview and the big Forge button, which becomes the run's progress bar with
Cancel next to it while a run goes. Under it the preset card: a Shows | Films switch (with each
kind's count) that only picks which kind's preset the card shows and edits; the preset menu,
"✎ Custom" (a one-off naming rule for this batch, not saved) and "Save as preset…"; the naming
rule with its codes in copper and an example name; one line of option buttons (Extras: Poster,
Season posters for shows, Trailer; Tidy up: Clean junk, Empty folders), and "Changed for this
batch · Save to preset" at its end while the options or the rule differ from the preset.

Then one list of shows and films (`queue_rows`), filtered by All / Needs you / Ready with their
counts: a band per show with its episodes under it, a band per film; each band's status as a dot
and a word, its buttons (Choose…, Rename, a done band's Undo) only under the pointer, except on a
band that needs the owner. "Choose…" opens the match picker. Forge renames everything not
waiting for the owner in one go: dropped files where they are, each with its own kind's preset
(or that kind's Custom rule) in one run, and watched downloads into their library folder with the
library's preset, through the automatic renamer (`services.auto.rename`). Forge runs one at a time
with the renamer (`services.run_lock`); closing the window while a run goes (`request_close`)
stops it after its current file. Keys: Enter = Preview, Ctrl+Enter = Forge, Ctrl+Z = undo the
last run, Up / Down = the selection; ignored while typing in a text field.

With 1,000 files whose lookups land for a minute it stays smooth (F-33): a burst of queue events
brings one refresh at a pace set by the last one's cost, a refresh is built in one turn of the
window and shown in the next, and widgets are configured only when what they show changed.

It talks only to `ForgeServices`' services, starts coroutines through `app.ui.tasks.spawn` (and
cancels them on destroy), reports what it did to the Activity panel (`services.activity`, in the
interface language; the log stays English and names no paths) and paints in the Forge look.
Build it while the event loop runs (the Forge window is opened from it): it loads the presets at
once and takes Explorer drops on itself (app/forge/ui/file_drops.py).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import time
import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from tkinter import filedialog
from typing import TYPE_CHECKING, Any, Concatenate

import customtkinter as ctk

from app.forge.clients.trailers import ToolInstallError
from app.forge.core.auto import library_naming
from app.forge.core.executor import ProgressEvent, RunResult
from app.forge.core.formatter import render
from app.forge.core.parser import VIDEO_EXTENSIONS
from app.forge.core.planner import PlanPart, build_mixed_plan, describe
from app.forge.core.queue import ItemStatus, QueueEvent, QueueItem
from app.forge.models.history import OpKind, RunSource
from app.forge.models.media import TypeHint
from app.forge.models.preset import Preset, PresetOptions
from app.forge.ui.file_drops import attach_file_drops
from app.forge.ui.images import BandImages, Key
from app.forge.ui.look import Pair, forge_look
from app.forge.ui.match_picker import MatchPicker
from app.forge.ui.queue_rows import (
    FINISHED,
    NEEDS_OWNER,
    Kind,
    QueueRows,
    Show,
    build_queue_rows,
    empty_texts,
    item_kind,
)
from app.forge.ui.widgets.chips import ForgeProgress, OptionButton, fit_text
from app.forge.ui.widgets.dialogs import ask_choice, ask_text
from app.forge.ui.widgets.virtual_list import MONO_FAMILY, Row, Sizes, VirtualList, faded_fill
from app.i18n import N_, number, t, tn
from app.ui import tasks, theme
from app.ui.missing_control import popup_menu
from app.ui.tooltip import add_tooltip

if TYPE_CHECKING:
    from app.forge.services import ForgeServices

log = logging.getLogger(__name__)

# A preset's extras by their own names (Settings › Naming shows them).
OPTION_LABELS: dict[str, str] = {
    "movie_poster": N_("Movie poster"),
    "series_poster": N_("Series poster"),
    "season_poster": N_("Season posters"),
    "trailer": N_("Trailer"),
    "clean_junk": N_("Clean junk"),
    "prune_empty": N_("Remove empty folders"),
}


@dataclass(frozen=True, slots=True)
class OptionButtonSpec:
    """An option button of the preset card: its name, its explanation (the tooltip) and the
    preset option it switches for shows and for films (None: not for that kind)."""

    name: str
    tip: str
    series: str | None
    movies: str | None


EXTRAS = N_("Extras")
TIDY_UP = N_("Tidy up")
OPTION_GROUPS: tuple[tuple[str, tuple[OptionButtonSpec, ...]], ...] = (
    (
        EXTRAS,
        (
            OptionButtonSpec(
                N_("Poster"),
                N_("Saves the film or show poster next to the files"),
                "series_poster",
                "movie_poster",
            ),
            OptionButtonSpec(
                N_("Season posters"), N_("One poster per season folder"), "season_poster", None
            ),
            OptionButtonSpec(
                N_("Trailer"), N_("Downloads the trailer next to the files"), "trailer", "trailer"
            ),
        ),
    ),
    (
        TIDY_UP,
        (
            OptionButtonSpec(
                N_("Clean junk"),
                N_("Removes sample files, .nfo and .txt left by the release"),
                "clean_junk",
                "clean_junk",
            ),
            OptionButtonSpec(
                N_("Empty folders"),
                N_("After moving, deletes folders that are left empty"),
                "prune_empty",
                "prune_empty",
            ),
        ),
    ),
)
KINDS: tuple[tuple[Kind, str], ...] = (("series", N_("Shows")), ("movies", N_("Films")))
_KINDS: tuple[Kind, ...] = ("series", "movies")
FILTERS: tuple[tuple[Show, str], ...] = (
    ("all", N_("All")),
    ("needs", N_("Needs you")),
    ("ready", N_("Ready")),
)
# The empty queue when Explorer drops can't reach it (attaching failed).
NO_DROPS_TITLE = N_("Add video files or folders")
NO_DROPS_HINT = N_("with the buttons above. Nothing is renamed until you click Forge.")
NO_EXAMPLE = N_("Add files to see how they'll be named.")

# The queue's bigger lines (mockups/forge-chosen.html): CTk units.
QUEUE_SIZES = Sizes(
    item_title=13,
    band_title=21,
    band_sub=14,
    button=14,
    note=13,
    line=16,
    code=16,
    mono=13,
    state=13,
    button_height=34,
    button_pad=15,
    band_gap=10,
    band_gap_below=False,
    band_radius=10,
    band_edge=0,  # no coloured edge: the status dot and word say it
    band_inset=16,
    poster=(58, 86),
)
BAND_HEIGHT = 10 + 86 + 2 * 12  # the gap above, the poster and the band's padding
LINE_HEIGHT = 40
# The card's colours: (light, dark) pairs from the Forge look's tokens.
_CODE = re.compile(r"\{[^{}]*\}")

_PROGRESS_STATUS = {
    "working": ItemStatus.WORKING,
    "done": ItemStatus.DONE,
    "error": ItemStatus.ERROR,
    "skipped": ItemStatus.SKIPPED,
}
_REFRESH_EVENT_MS = 80  # a burst of queue events becomes one refresh
# ... and while events keep coming (1,000 lookups landing for a minute), refreshes take at most
# this share of the window's time: the next one waits for the last one's cost / share (F-33).
_REFRESH_SHARE = 0.25
_REFRESH_FORMAT_MS = 150  # typing a Custom rule
_REFRESH_PROGRESS_MS = 60
_TYPING_CLASSES = ("Entry", "Text", "TEntry")  # a key there is typing, never a shortcut
_KEYS: dict[str, str] = {
    "<Return>": "preview",
    "<Control-Return>": "forge",
    "<Control-z>": "undo",
    "<Control-Z>": "undo",
    "<Up>": "up",
    "<Down>": "down",
}

type AskFn = Callable[[str, str, Sequence[str]], Awaitable[str | None]]
"""A question with a button per choice: the chosen label, or None (closed)."""
type AskTextFn = Callable[[str, str, str], Awaitable[str | None]]
"""A question for a short text (title, message, main button): the text, or None (closed)."""


def card_colours(look: theme.Look) -> tuple[Pair, Pair, Pair]:
    """The preset card's (ground, edge, the naming rule's box): warm in both looks."""
    return look.STACK_FAR, look.CHIP_HOVER, (look.SURFACE[0], look.BG[1])


def fits(preset: Preset, kind: Kind) -> bool:
    """A preset that can name `kind`: made for it, or for both (Auto)."""
    if preset.type is TypeHint.AUTO:
        return True
    return preset.type is (TypeHint.TV if kind == "series" else TypeHint.MOVIE)


@dataclass
class Choice:
    """What names one kind: the preset chosen for it, its options for this batch and the Custom
    rule (None: the preset's own)."""

    preset: Preset | None = None
    options: PresetOptions = field(default_factory=PresetOptions)
    custom: str | None = None

    @property
    def rule(self) -> str:
        if self.custom is not None:
            return self.custom
        return self.preset.format if self.preset is not None else "{ny}"

    @property
    def changed(self) -> bool:
        """The options or the rule differ from the stored preset."""
        if self.preset is None:
            return False
        return self.options != self.preset.options or self.rule != self.preset.format


def _one_at_a_time[**P](
    method: Callable[Concatenate[QueueView, P], Awaitable[None]],
) -> Callable[Concatenate[QueueView, P], Coroutine[Any, Any, None]]:
    """Ignore Preview / Forge / Undo while another one is still going (double clicks, keys)."""

    @functools.wraps(method)
    async def guarded(self: QueueView, /, *args: P.args, **kwargs: P.kwargs) -> None:
        if self.busy:
            return
        self.busy = True
        try:
            await method(self, *args, **kwargs)
        finally:
            self.busy = False

    return guarded


def _merge(a: RunResult, b: RunResult) -> RunResult:
    """One summary for a Forge that renamed downloads and dropped files."""
    return RunResult(
        run_id=b.run_id or a.run_id,
        renamed=a.renamed + b.renamed,
        skipped=a.skipped + b.skipped,
        failed=a.failed + b.failed,
        posters=a.posters + b.posters,
        trailers=a.trailers + b.trailers,
        cleaned=a.cleaned + b.cleaned,
        pruned=a.pruned + b.pruned,
        cancelled=a.cancelled or b.cancelled,
        errors=[*a.errors, *b.errors],
    )


def _set_text(widget: Any, text: str) -> None:
    """A CTk widget's text, configured only when it changed: CTk redraws on every configure."""
    if widget.cget("text") != text:
        widget.configure(text=text)


def summary_text(result: RunResult) -> str:
    """The Activity panel's line after a Forge (translated)."""
    text = t(
        "Forged: {renamed} renamed, {skipped} already right, {failed} failed",
        renamed=result.renamed,
        skipped=result.skipped,
        failed=result.failed,
    )
    extras: list[str] = []
    if result.posters:
        extras.append(tn("{n} poster", "{n} posters", result.posters))
    if result.trailers:
        extras.append(tn("{n} trailer", "{n} trailers", result.trailers))
    if extras:
        text += " · " + ", ".join(extras)
    if result.cancelled:
        text += " · " + t("cancelled")
    return text


def forge_label(pending: int) -> str:
    """The Forge button's text."""
    return "⚒ " + (tn("Forge {n} file", "Forge {n} files", pending) if pending else t("Forge"))


def display_name(item: QueueItem) -> str:
    """How the progress bar names a file: the episode's or the film's title."""
    media = item.media
    return media.episode_title or media.title or item.path.name


class QueueView(ctk.CTkFrame):
    """The Queue page of the Forge window."""

    def __init__(
        self, master: Any, services: ForgeServices, *, look: theme.Look | None = None
    ) -> None:
        self.look: theme.Look = forge_look() if look is None else look
        super().__init__(master, fg_color=self.look.BG, corner_radius=0)
        self.services = services
        self.presets: list[Preset] = []
        self.choices: dict[Kind, Choice] = {"series": Choice(), "movies": Choice()}
        self.side: Kind = "series"  # the kind the preset card shows
        self.show: Show = "all"  # the list's filter
        self.loaded = False  # the presets have been read
        self.running = False  # a Forge run is going (its progress and Cancel show)
        self.busy = False  # set by @_one_at_a_time while Preview, Forge or Undo is going
        self.on_counts: Callable[[int], None] = lambda needs: None  # the window's badge
        self._bands: dict[int, list[int]] = {}  # band key → its items
        self._targets: dict[int, Path] = {}
        self._targets_in_run: dict[int, Path] = {}
        self._loading: set[Key] = set()  # band pictures being fetched right now
        self._close_after_run: Callable[[], None] | None = None
        self._pending_preset: str | None = None
        self._refresh_id: str | None = None  # a refresh coming (after a queue event, typing …)
        self._show_id: str | None = None  # its rows, built, shown when Tk is idle next
        self._flow_id: str | None = None  # the option line laid out again
        self._refreshed_at = 0.0  # time.monotonic() of the last refresh
        self._refresh_cost = 0.0  # how long it took (s)
        self._told: tuple[object, int] | None = None  # (on_counts, needs) last told
        self._kind_counts: dict[Kind, int] = {"series": 0, "movies": 0}
        self._run_total = 0
        self._run_done: set[int] = set()
        self._run_name = ""
        self._setting_rule = False  # the rule's box is being filled, not typed in
        self._example_font: tuple[float, tkfont.Font] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._cancel = asyncio.Event()
        self._bindings: list[tuple[str, str]] = []
        self.ask: AskFn = self._ask  # the questions, replaceable in tests
        self.ask_name: AskTextFn = self._ask_name

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)
        self._build_toolbar()
        self._build_card()
        self._build_filters()
        self._build_list()
        self.images = BandImages(services.images, self.list, ground=self.look.SURFACE)
        theme.on_mode_change(self, self._repaint)

        self._unsubscribe = services.queue_events.subscribe(self._on_queue_event)
        self.drops = attach_file_drops(self, self.add_paths)  # False: the buttons only
        self.spawn(self._load_presets())
        self._bind_keys()
        self.refresh()

    # --- building --------------------------------------------------------------------------

    def _tool_button(
        self,
        master: Any,
        text: str,
        command: Callable[[], None],
        *,
        edge: Pair | None = None,
        fill: Pair | None = None,
    ) -> ctk.CTkButton:
        """A toolbar or card button: a quiet surface, a thin edge (copper for the add buttons)."""
        look = self.look
        return ctk.CTkButton(
            master,
            text=text,
            width=40,
            height=36,
            corner_radius=9,
            fg_color=look.SURFACE if fill is None else fill,
            border_width=1,
            border_color=look.CHIP_HOVER if edge is None else edge,
            text_color=theme.TEXT,
            text_color_disabled=theme.TEXT_MUTED,
            hover_color=look.CHIP,
            font=theme.font(14, bold=True),
            command=command,
        )

    def _build_toolbar(self) -> None:
        look = self.look
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=22, pady=(14, 0))
        self.toolbar = bar
        self.files_button = self._tool_button(
            bar, t("+ Files"), self._browse_files, edge=look.ACCENT
        )
        self.files_button.pack(side="left")
        self.folder_button = self._tool_button(
            bar, t("+ Folder") + " ▾", self._folder_menu, edge=look.ACCENT
        )
        self.folder_button.pack(side="left", padx=(10, 0))
        self.actions = ctk.CTkFrame(bar, fg_color="transparent")
        self.actions.pack(side="right")
        self.forge_button = ctk.CTkButton(
            self.actions,
            text=forge_label(0),
            width=150,
            height=38,
            corner_radius=10,
            fg_color=look.ACCENT,
            hover_color=look.ACCENT_HOVER,
            text_color=theme.ON_ACCENT,
            text_color_disabled=theme.TEXT_MUTED,
            font=theme.font(15, bold=True),
            command=self._forge_clicked,
        )
        self.preview_button = self._tool_button(self.actions, t("Preview"), self._preview_clicked)
        self.clear_button = self._tool_button(
            self.actions, "✕ " + t("Clear queue"), self._clear
        )
        self.cancel_button = self._tool_button(self.actions, t("Cancel"), self._cancel_clicked)
        self.progress = ForgeProgress(self.actions, look=look, ground=look.BG)
        self._show_actions(running=False)

    def _show_actions(self, *, running: bool, installing: bool = False) -> None:
        """The toolbar's right end: Clear queue, Preview and Forge; or the progress bar and Cancel
        (a run), or only the bar (the trailer tools coming in)."""
        for widget in (
            self.clear_button,
            self.preview_button,
            self.forge_button,
            self.progress,
            self.cancel_button,
        ):
            widget.pack_forget()
        if running or installing:
            if running:
                self.cancel_button.pack(side="right", padx=(10, 0))
            self.progress.pack(side="right")
        else:
            self.forge_button.pack(side="right", padx=(10, 0))
            self.preview_button.pack(side="right", padx=(10, 0))
            self.clear_button.pack(side="right")

    def _build_card(self) -> None:
        look = self.look
        ground, edge, rule_bg = card_colours(look)
        self._card_ground = ground
        card = ctk.CTkFrame(
            self, fg_color=ground, corner_radius=12, border_width=1, border_color=edge
        )
        card.grid(row=1, column=0, sticky="ew", padx=22, pady=(14, 4))
        card.grid_columnconfigure(0, weight=1)
        self.card = card

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 8))
        self.kind_switch = ctk.CTkSegmentedButton(
            head,
            values=self._kind_labels(),
            command=self._kind_clicked,
            height=30,
            corner_radius=15,
            font=theme.font(13, bold=True),
            fg_color=rule_bg,
            selected_color=look.SELECTED,
            selected_hover_color=look.SELECTED_HOVER,
            unselected_color=rule_bg,
            unselected_hover_color=look.CHIP_HOVER,
            text_color=theme.TEXT,
        )
        self.kind_switch.set(self._kind_labels()[0])
        self.kind_switch.pack(side="left")
        ctk.CTkLabel(head, text=t("Preset"), font=theme.font(13), text_color=theme.TEXT_MUTED).pack(
            side="left", padx=(18, 8)
        )
        self.preset_menu = ctk.CTkOptionMenu(
            head,
            values=["…"],
            width=160,
            height=32,
            corner_radius=8,
            fg_color=ground,
            button_color=ground,
            button_hover_color=look.CHIP_HOVER,
            text_color=theme.TEXT,
            dropdown_fg_color=look.SURFACE,
            dropdown_hover_color=look.MENU_HOVER,
            dropdown_text_color=theme.TEXT,
            font=theme.font(17, bold=True),
            dropdown_font=theme.font(13),
            command=self._preset_chosen,
        )
        self.preset_menu.pack(side="left")
        self.save_as_button = self._tool_button(
            head, t("Save as preset…"), self._save_as_clicked, fill=ground
        )
        self.save_as_button.pack(side="right")
        self.custom_button = self._tool_button(
            head, "✎ " + t("Custom"), self._custom_clicked, fill=ground
        )
        self.custom_button.pack(side="right", padx=(0, 8))
        add_tooltip(
            self.custom_button,
            t("Type a naming rule for this batch only; it isn't saved."),
        )

        box = ctk.CTkFrame(card, fg_color=rule_bg, corner_radius=8)
        box.grid(row=1, column=0, sticky="ew", padx=16)
        self.rule_box = box
        self.example_label = ctk.CTkLabel(
            box,
            text="",
            font=ctk.CTkFont(family=MONO_FAMILY, size=13),
            text_color=theme.TEXT_MUTED,
            anchor="e",
        )
        self.example_label.pack(side="right", padx=(8, 12))
        self.rule_text = ctk.CTkTextbox(
            box,
            height=34,
            width=200,
            wrap="none",
            activate_scrollbars=False,
            fg_color=rule_bg,
            text_color=theme.TEXT,
            border_width=0,
            font=ctk.CTkFont(family=MONO_FAMILY, size=15),
        )
        self.rule_text.pack(side="left", fill="x", expand=True, padx=(4, 0), pady=2)
        inner = self.rule_text._textbox
        inner.configure(undo=True)
        inner.bind("<<Modified>>", self._rule_typed, add="+")
        for sequence in ("<Return>", "<KP_Enter>", "<Control-Return>"):
            inner.bind(sequence, lambda _event: "break")
        box.bind("<Configure>", lambda _event: self._fit_example(), add="+")
        self._example_full = ""
        self._paint_rule_tags()

        # The options: one line of buttons (wrapping onto a second at the narrowest).
        self.option_line = tk.Frame(card, bg=theme.pick(ground), bd=0, highlightthickness=0)
        self.option_line.grid(row=2, column=0, sticky="ew", padx=16, pady=(10, 14))
        self.option_line.grid_propagate(False)
        self.option_line.pack_propagate(False)
        self._option_rows = [
            tk.Frame(self.option_line, bg=theme.pick(ground), bd=0, highlightthickness=0)
            for _ in range(3)
        ]
        self.option_buttons: dict[str, OptionButton] = {}
        self._option_specs: dict[str, OptionButtonSpec] = {}
        self._groups: list[tk.Frame] = []  # the option groups with a divider between them
        self._dividers: list[tk.Frame] = []
        self._layout: list[list[tk.Frame]] = []
        for index, (caps, specs) in enumerate(OPTION_GROUPS):
            if index:
                divider = tk.Frame(
                    self.option_line, width=1, bd=0, highlightthickness=0, bg=theme.pick(edge)
                )
                self._groups.append(divider)
                self._dividers.append(divider)
            group = tk.Frame(self.option_line, bg=theme.pick(ground), bd=0, highlightthickness=0)
            ctk.CTkLabel(
                group,
                text=t(caps).upper(),
                font=theme.font(11, bold=True),
                text_color=theme.TEXT_MUTED,
                fg_color=ground,
            ).pack(side="left", padx=(0, 10))
            for spec in specs:
                button = OptionButton(
                    group,
                    t(spec.name),
                    functools.partial(self._option_clicked, spec.name),
                    look=look,
                    ground=ground,
                )
                button.pack(side="left", padx=(0, 8))
                add_tooltip(button.canvas, t(spec.tip))
                self.option_buttons[spec.name] = button
                self._option_specs[spec.name] = spec
            self._groups.append(group)
        changed = tk.Frame(self.option_line, bg=theme.pick(ground), bd=0, highlightthickness=0)
        ctk.CTkLabel(
            changed,
            text=t("Changed for this batch") + " · ",
            font=theme.font(13),
            text_color=look.LINK,
            fg_color=ground,
        ).pack(side="left")
        self.save_link = ctk.CTkLabel(
            changed,
            text=t("Save to preset"),
            font=theme.font(13, underline=True),
            text_color=look.LINK,
            fg_color=ground,
            cursor="hand2",
        )
        self.save_link.pack(side="left")
        self.save_link.bind("<Button-1>", lambda _event: self._save_to_preset_clicked())
        self.changed_note = changed
        self.option_line.bind("<Configure>", lambda _event: self._schedule_flow(), add="+")

    def _build_filters(self) -> None:
        look = self.look
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.grid(row=2, column=0, sticky="ew", padx=22, pady=(10, 0))
        self.filter_buttons: dict[Show, ctk.CTkButton] = {}
        for show, _label in FILTERS:
            button = ctk.CTkButton(
                row,
                text="",
                width=40,
                height=28,
                corner_radius=14,
                border_width=0,
                hover_color=look.CHIP_HOVER,
                font=theme.font(13),
                command=functools.partial(self.set_filter, show),
            )
            button.pack(side="left", padx=(0, 6))
            self.filter_buttons[show] = button
        self._paint_filters()

    def _build_list(self) -> None:
        look = self.look
        holder = ctk.CTkFrame(self, fg_color="transparent")
        holder.grid(row=3, column=0, sticky="nsew", padx=(16, 4), pady=(4, 8))
        self.list = VirtualList(
            holder,
            row_height=LINE_HEIGHT,
            band_height=BAND_HEIGHT,
            look=look,
            thumbnails=False,
            sizes=QUEUE_SIZES,
            hover_actions=True,
        )
        self.list.on_select = self._on_select
        self.list.on_remove = self._remove
        self.list.on_action = self._on_band_action
        self.list.images = self._images_for
        self.empty_state = ctk.CTkFrame(holder, fg_color="transparent")
        ctk.CTkLabel(
            self.empty_state, text="⤓", font=theme.font(40), text_color=theme.TEXT_MUTED
        ).pack(pady=(50, 6))
        self.empty_title = ctk.CTkLabel(
            self.empty_state, text="", font=theme.font(16, bold=True), text_color=theme.TEXT
        )
        self.empty_title.pack()
        self.empty_hint = ctk.CTkLabel(
            self.empty_state,
            text="",
            font=theme.font(13),
            text_color=theme.TEXT_MUTED,
            wraplength=480,
        )
        self.empty_hint.pack(pady=(4, 14))

    def _bind_keys(self) -> None:
        top = self.winfo_toplevel()
        for sequence, action in _KEYS.items():
            bind_id = top.bind(sequence, self._key_handler(action), add="+")
            self._bindings.append((sequence, bind_id))

    def _key_handler(self, action: str) -> Callable[[tk.Event[Any]], str | None]:
        def handler(_event: tk.Event[Any]) -> str | None:
            return "break" if self.shortcut(action) else None

        return handler

    def _repaint(self) -> None:
        ground, edge, _rule = card_colours(self.look)
        for frame in (self.option_line, *self._option_rows, self.changed_note, *self._groups):
            frame.configure(bg=theme.pick(edge if frame in self._dividers else ground))
        self._paint_rule_tags()
        self._paint_filters()

    # --- helpers ---------------------------------------------------------------------------

    def spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        """Start `coro` (logged and reported by app.ui.tasks); cancelled when the view goes."""
        task = tasks.spawn(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _say(self, text: str, *, level: int = logging.INFO, success: bool = False) -> None:
        self.services.activity.say(text, level=level, success=success)

    async def _ask(self, title: str, message: str, choices: Sequence[str]) -> str | None:
        return await ask_choice(self, title, message, choices, look=self.look)

    async def _ask_name(self, title: str, message: str, ok: str) -> str | None:
        return await ask_text(self, title, message, ok, look=self.look)

    @property
    def choice(self) -> Choice:
        """What names the kind the card shows."""
        return self.choices[self.side]

    @property
    def fmt(self) -> str:
        """The naming rule the card shows."""
        return self.choice.rule

    def rule_for(self, item: QueueItem) -> str:
        """A dropped file's naming rule: its kind's preset, or that kind's Custom rule."""
        return self.choices[item_kind(item)].rule

    def pending_items(self) -> list[QueueItem]:
        return self.services.queue.pending()

    # --- presets & options -----------------------------------------------------------------

    async def _load_presets(self) -> None:
        presets = await self.services.presets.list()
        if not self.winfo_exists():
            return
        self.presets = presets
        self.loaded = True
        for kind in _KINDS:
            self.choices[kind] = self._reloaded(kind, presets)
        if self._pending_preset is not None:
            pending, self._pending_preset = self._pending_preset, None
            self.select_preset(pending)
        self._show_card()
        self.refresh()

    def _reloaded(self, kind: Kind, presets: Sequence[Preset]) -> Choice:
        """`kind`'s choice after the presets were read (again): the same preset when it is
        still there (its options as stored now, unless changed for this batch), else the one
        remembered for the kind, the one last chosen, the library's, or the first that fits."""
        choice = self.choices[kind]
        old = choice.preset
        if old is not None:
            fresh = next((p for p in presets if p.name == old.name and fits(p, kind)), None)
            if fresh is not None:
                options = fresh.options if choice.options == old.options else choice.options
                return Choice(preset=fresh, options=options, custom=choice.custom)
        settings = self.services.settings.current
        remembered = settings.last_show_preset if kind == "series" else settings.last_film_preset
        library = settings.series_preset if kind == "series" else settings.movies_preset
        preset = next(
            (
                p
                for name in (remembered, settings.last_preset, library)
                for p in presets
                if name and p.name == name and fits(p, kind)
            ),
            None,
        )
        if preset is None:
            preset = next((p for p in presets if fits(p, kind)), presets[0] if presets else None)
        return Choice(preset=preset, options=preset.options if preset else PresetOptions())

    def reload_presets(self) -> None:
        """The presets changed (Settings › Naming, an import): load them again."""
        self.spawn(self._load_presets())

    def presets_for(self, kind: Kind) -> list[Preset]:
        """The presets the card's menu offers for `kind`."""
        return [p for p in self.presets if fits(p, kind)] or list(self.presets)

    def select_preset(self, name: str) -> None:
        """Use the preset `name` (a right-click request's): for its kind, or for both kinds
        (Auto); the card shows that kind."""
        if not self.loaded:
            self._pending_preset = name  # chosen before the presets have loaded
            return
        preset = next((p for p in self.presets if p.name == name), None)
        if preset is None:
            return
        if preset.type is TypeHint.AUTO:
            kinds: tuple[Kind, ...] = ("series", "movies")
        else:
            kinds = ("series",) if preset.type is TypeHint.TV else ("movies",)
        for kind in kinds:
            self.choose_preset(kind, preset)
        if self.side not in kinds:
            self.set_side(kinds[0])

    def choose_preset(self, kind: Kind, preset: Preset) -> None:
        """`preset` names `kind` from now on (its options; the Custom rule is dropped)."""
        self.choices[kind] = Choice(preset=preset, options=preset.options)
        if kind == self.side:
            self._show_card()
        self._remember(kind, preset.name)
        self._schedule_refresh(0)

    def _remember(self, kind: Kind, name: str) -> None:
        settings = self.services.settings.current
        key = "last_show_preset" if kind == "series" else "last_film_preset"
        if getattr(settings, key) != name or settings.last_preset != name:
            self.spawn(self.services.settings.update(**{key: name, "last_preset": name}))

    def _preset_chosen(self, name: str) -> None:
        preset = next((p for p in self.presets_for(self.side) if p.name == name), None)
        if preset is not None:
            self.choose_preset(self.side, preset)

    def set_side(self, kind: Kind) -> None:
        """The Shows | Films switch: which kind's preset the card shows and edits (the list
        stays one)."""
        if kind != self.side:
            self.side = kind
            self._show_card()

    def _kind_labels(self) -> list[str]:
        labels: list[str] = []
        for kind, label in KINDS:
            n = self._kind_counts.get(kind, 0)
            labels.append(f"{t(label)} · {number(n)}" if n else t(label))
        return labels

    def _kind_clicked(self, value: str) -> None:
        values = list(self.kind_switch.cget("values"))
        if value in values:
            self.set_side(KINDS[values.index(value)][0])

    def _label_kinds(self) -> None:
        labels = self._kind_labels()
        if labels != list(self.kind_switch.cget("values")):  # configuring rebuilds every part
            self.kind_switch.configure(values=labels)
        wanted = labels[[kind for kind, _ in KINDS].index(self.side)]
        if self.kind_switch.get() != wanted:
            self.kind_switch.set(wanted)

    def _show_card(self) -> None:
        """The card for the kind it shows: the switch, the menu, the rule, the options."""
        choice = self.choice
        self._label_kinds()
        names = [p.name for p in self.presets_for(self.side)] or ["…"]
        if list(self.preset_menu.cget("values")) != names:
            self.preset_menu.configure(values=names)
        if choice.preset is not None and self.preset_menu.get() != choice.preset.name:
            self.preset_menu.set(choice.preset.name)
        custom = choice.custom is not None
        self._set_rule(choice.rule, editable=custom)
        look = self.look
        self.custom_button.configure(
            fg_color=look.SELECTED if custom else self._card_ground,
            border_color=look.ACCENT if custom else look.CHIP_HOVER,
        )
        if (int(self.rule_box.cget("border_width")) > 0) != custom:  # typed in: a copper edge
            self.rule_box.configure(border_width=1 if custom else 0, border_color=look.ACCENT)
        for name, button in self.option_buttons.items():
            key = getattr(self._option_specs[name], self.side)
            button.set(bool(key and getattr(choice.options, key)))
        self._update_example()
        self._schedule_flow()

    def _option_clicked(self, name: str, value: bool) -> None:
        key = getattr(self._option_specs[name], self.side)
        if key is None:
            return
        choice = self.choice
        choice.options = choice.options.model_copy(update={key: value})
        self._schedule_flow()

    def option_keys(self) -> dict[str, str]:
        """The option buttons the card shows for its kind: name → the preset option."""
        keys = {name: getattr(spec, self.side) for name, spec in self._option_specs.items()}
        return {name: key for name, key in keys.items() if key}

    def set_scan_subfolders(self, value: bool) -> None:
        async def apply() -> None:
            await self.services.settings.update(scan_subfolders=value)
            await self.services.queue.rescan(value)

        self.spawn(apply())

    # --- the option line's layout ----------------------------------------------------------

    def _schedule_flow(self) -> None:
        if self._flow_id is None:
            self._flow_id = self.after_idle(self._flow)

    def _flow(self) -> None:
        """The option groups on one line, or wrapped at a group's edge when they don't fit (a
        divider never starts or ends a line); "Changed for this batch" at the end while the
        options or the rule differ from the preset."""
        self._flow_id = None
        if not self.winfo_exists():
            return
        season = self.option_buttons["Season posters"]
        if self.side == "series":
            if not season.winfo_manager():
                season.pack(side="left", padx=(0, 8), before=self.option_buttons["Trailer"])
        elif season.winfo_manager():
            season.pack_forget()
        items: list[tk.Frame] = [*self._groups]
        if self.choice.changed:
            items.append(self.changed_note)
        self.update_idletasks()  # the groups' own sizes, with or without Season posters
        available = self.option_line.winfo_width()
        gap = round(14 * self._get_widget_scaling())
        rows: list[list[tk.Frame]] = [[]]
        used = 0
        for widget in items:
            width = widget.winfo_reqwidth()
            divider = widget in self._dividers
            if rows[-1] and not divider and used + gap + width > available > 1:
                if rows[-1][-1] in self._dividers:
                    rows[-1].pop()
                rows.append([])
                used = 0
            if divider and not rows[-1]:
                continue
            rows[-1].append(widget)
            used += (gap if used else 0) + width
        if rows != self._layout:
            self._layout = rows
            for widget in (*self._groups, self.changed_note, *self._option_rows):
                widget.pack_forget()
            for index, (row_frame, row_items) in enumerate(
                zip(self._option_rows, rows, strict=False)
            ):
                for column, widget in enumerate(row_items):
                    if widget is self.changed_note:
                        widget.pack(in_=row_frame, side="right")
                    else:
                        left = 0 if column == 0 else gap
                        widget.pack(in_=row_frame, side="left", fill="y", padx=(left, 0))
                row_frame.pack(fill="x", pady=(0 if index == 0 else 6, 0))
            self.update_idletasks()
        height = sum(f.winfo_reqheight() for f in self._option_rows[: len(rows)])
        height += 6 * (len(rows) - 1)
        if int(self.option_line.cget("height")) != height:
            self.option_line.configure(height=height)

    # --- the naming rule -------------------------------------------------------------------

    def _paint_rule_tags(self) -> None:
        self.rule_text._textbox.tag_config("code", foreground=theme.pick(self.look.BRAND))

    def _set_rule(self, rule: str, *, editable: bool) -> None:
        """Show `rule` in its box, its codes in copper; typed in only in Custom mode."""
        box = self.rule_text
        inner = box._textbox
        if box.get("1.0", "end-1c") != rule:
            self._setting_rule = True
            box.configure(state="normal")
            box.delete("1.0", "end")
            box.insert("1.0", rule)
            inner.edit_modified(False)
            self._setting_rule = False
        self._tag_codes()
        box.configure(state="normal" if editable else "disabled")
        inner.configure(cursor="xterm" if editable else "arrow")

    def _tag_codes(self) -> None:
        inner = self.rule_text._textbox
        inner.tag_remove("code", "1.0", "end")
        text = inner.get("1.0", "end-1c")
        for match in _CODE.finditer(text):
            inner.tag_add("code", f"1.0+{match.start()}c", f"1.0+{match.end()}c")

    def _rule_typed(self, _event: tk.Event[Any] | None = None) -> None:
        inner = self.rule_text._textbox
        if not inner.edit_modified():
            return
        inner.edit_modified(False)
        choice = self.choice
        if self._setting_rule or choice.custom is None:
            return
        text = " ".join(self.rule_text.get("1.0", "end-1c").splitlines())
        choice.custom = text
        self._tag_codes()
        self._update_example()
        self._schedule_flow()
        self._schedule_refresh(_REFRESH_FORMAT_MS)

    def set_custom(self, rule: str | None) -> None:
        """Custom mode for the kind the card shows, with `rule` (None: back to the preset's)."""
        self.choice.custom = rule
        self._show_card()
        self._schedule_refresh(_REFRESH_FORMAT_MS)

    def _custom_clicked(self) -> None:
        if self.choice.custom is None:
            self.set_custom(self.choice.rule)
            self.rule_text.focus_set()
            self.rule_text._textbox.mark_set("insert", "end-1c")
        else:
            self.set_custom(None)

    # --- saving presets --------------------------------------------------------------------

    def _save_to_preset_clicked(self) -> None:
        self.spawn(self.save_to_preset())

    async def save_to_preset(self) -> None:
        """"Save to preset": the card's rule and options into the preset it shows."""
        choice, kind = self.choice, self.side
        preset = choice.preset
        if preset is None:
            return
        saved = preset.model_copy(update={"format": choice.rule, "options": choice.options})
        await self.services.presets.save(saved)
        log.info("Saved the batch's changes to a preset")
        self._say(t("Saved to the preset {name}", name=preset.name), success=True)
        self.choices[kind] = Choice(preset=saved, options=saved.options)
        await self._load_presets()

    def _save_as_clicked(self) -> None:
        self.spawn(self.save_as_preset())

    async def save_as_preset(self) -> None:
        """"Save as preset…": the card's rule and options as a new preset for its kind."""
        kind, choice = self.side, self.choice
        name = await self.ask_name(
            t("Save as preset"), t("A name for the new preset:"), t("Save")
        )
        if not name or not self.winfo_exists():
            return
        if any(p.name == name for p in self.presets):
            replace = t("Replace")
            answer = await self.ask(
                t("Save as preset"),
                t("There is a preset called {name} already. Replace it?", name=name),
                [replace, t("Cancel")],
            )
            if answer != replace:
                return
        hint = TypeHint.TV if kind == "series" else TypeHint.MOVIE
        preset = Preset(name=name, type=hint, format=choice.rule, options=choice.options)
        await self.services.presets.save(preset)
        log.info("Saved a new preset")
        self._say(t("Saved the preset {name}", name=name), success=True)
        self.choices[kind] = Choice(preset=preset, options=preset.options)
        self._remember(kind, name)
        await self._load_presets()

    # --- the folder menu -------------------------------------------------------------------

    def _folder_menu(self) -> None:
        scan = self.services.settings.current.scan_subfolders
        box = "☑" if scan else "☐"
        popup_menu(
            self.folder_button,
            [
                (t("Choose a folder…"), self._browse_folder),
                (f"{box}  {t('Scan subfolders')}", lambda: self.set_scan_subfolders(not scan)),
            ],
            look=self.look,
        )

    # --- the filter --------------------------------------------------------------------------

    def set_filter(self, show: Show) -> None:
        """All, Needs you or Ready."""
        if show != self.show:
            self.show = show
            self._paint_filters()
            self.refresh()

    def _paint_filters(self, counts: dict[Show, int] | None = None) -> None:
        look = self.look
        for show, label in FILTERS:
            button = self.filter_buttons[show]
            on = show == self.show
            text = t(label) if counts is None else f"{t(label)} {number(counts.get(show, 0))}"
            if counts is not None or not button.cget("text"):
                _set_text(button, text)
            colours = (look.ROW_SELECTED, theme.TEXT) if on else (look.SURFACE, theme.TEXT_MUTED)
            if tuple(button.cget("fg_color")) != colours[0]:
                button.configure(fg_color=colours[0], text_color=colours[1])

    # --- refreshing ------------------------------------------------------------------------

    def _on_queue_event(self, event: QueueEvent) -> None:
        # Lookups land a few at a time for a minute with 1,000 files: the first event of a burst
        # brings a refresh, the rest wait for it (never pushed back by the next one), and while
        # they keep coming refreshes take at most _REFRESH_SHARE of the window's time.
        if self._refresh_id is not None:
            return
        since_ms = (time.monotonic() - self._refreshed_at) * 1000
        rest_ms = self._refresh_cost * 1000 / _REFRESH_SHARE - since_ms
        self._schedule_refresh(max(_REFRESH_EVENT_MS, round(rest_ms)))

    def _schedule_refresh(self, delay_ms: int) -> None:
        if self._refresh_id is not None:
            self.after_cancel(self._refresh_id)
        self._refresh_id = self.after(delay_ms, self._refresh_in_turns)

    def _cancel_refresh(self) -> None:
        for job in (self._refresh_id, self._show_id, self._flow_id):
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass  # the window is going
        self._refresh_id = self._show_id = self._flow_id = None

    def refresh(self) -> None:
        """Show the queue as it is now, at once."""
        for job in (self._refresh_id, self._show_id):
            if job is not None:
                self.after_cancel(job)
        self._refresh_id = self._show_id = None
        if self.winfo_exists():
            started = time.perf_counter()
            self._show(self._build(), started)

    def _refresh_in_turns(self) -> None:
        """A scheduled refresh: the rows are built now and shown in a later turn of the window,
        so a key or a click can come in between (with 1,000 files each part is tens of ms)."""
        self._refresh_id = None
        if self._show_id is not None:
            self.after_cancel(self._show_id)
            self._show_id = None
        if not self.winfo_exists():
            return
        started = time.perf_counter()
        rows = self._build()
        building = time.perf_counter() - started
        self._show_id = self.after_idle(self._show_built, rows, building)

    def _show_built(self, rows: QueueRows, building: float) -> None:
        self._show_id = None
        if self.winfo_exists():
            self._show(rows, time.perf_counter() - building)

    def _build(self) -> QueueRows:
        settings = self.services.settings.current
        presets = self.presets
        return build_queue_rows(
            self.services.queue.items,
            self.rule_for,
            naming=lambda item: library_naming(item, settings, presets),
            show=self.show,
            trial=settings.automatic == "trial",
            today=date.today(),
        )

    def _show(self, built: QueueRows, started: float) -> None:
        """Show built rows; `started` is when the work began (perf_counter), for the pace of
        refreshes. Widgets are touched only where something changed: CTk redraws a widget on
        every configure."""
        items = self.services.queue.items
        self._targets, self._bands = built.targets, built.bands
        self.list.set_rows(built.rows)
        if built.rows:
            if self.empty_state.winfo_manager():
                self.empty_state.pack_forget()
            if not self.list.winfo_manager():
                self.list.pack(fill="both", expand=True)
        else:
            if self.list.winfo_manager():
                self.list.pack_forget()
            title, hint = empty_texts(self.show, others=bool(items))
            if not items and not self.drops:
                title, hint = t(NO_DROPS_TITLE), t(NO_DROPS_HINT)
            _set_text(self.empty_title, title)
            _set_text(self.empty_hint, hint)
            if not self.empty_state.winfo_manager():
                self.empty_state.pack(fill="both", expand=True)
        self._paint_filters(built.counts)
        if built.kinds != self._kind_counts:
            self._kind_counts = dict(built.kinds)
            self._label_kinds()
        if not self.running:
            _set_text(self.forge_button, forge_label(len(self._pending(None))))
        self._update_example()
        told = (self.on_counts, built.needs)
        if told != self._told:  # a new listener (the window, wired after building) hears it
            self._told = told
            self.on_counts(built.needs)
        self._refreshed_at = time.monotonic()
        self._refresh_cost = time.perf_counter() - started

    # --- the example name ------------------------------------------------------------------

    def _example_item(self) -> QueueItem | None:
        """The file the example name shows: the one selected when it is of the card's kind,
        else the first dropped file of that kind, else any of that kind."""
        queue = self.services.queue
        selected = queue.get(self.list.selected) if self.list.selected is not None else None
        if selected is not None and item_kind(selected) == self.side:
            return selected
        of_kind = [i for i in queue.items if item_kind(i) == self.side]
        dropped = [i for i in of_kind if i.watched is None and i.status not in FINISHED]
        candidates = dropped or of_kind
        return candidates[0] if candidates else None

    def _update_example(self) -> None:
        item = self._example_item()
        if item is None:
            text, colour = t(NO_EXAMPLE), theme.TEXT_MUTED
        else:
            rendered = render(self.fmt, item.media, item.path.suffix)
            if rendered.unknown:
                codes = ", ".join("{" + code + "}" for code in rendered.unknown)
                text, colour = "⚠ " + t("not a known code: {codes}", codes=codes), theme.WARNING
            else:
                text, colour = "→ " + rendered.parts[-1], theme.TEXT_MUTED
        if text != self._example_full:
            self._example_full = text
            self.example_label.configure(text_color=colour)
            self._fit_example()

    def _fit_example(self) -> None:
        """The example name, cut to at most half the rule's box."""
        width = self.rule_box.winfo_width()
        scale = float(self._get_widget_scaling())
        if self._example_font is None or self._example_font[0] != scale:
            spec = ctk.CTkFont(family=MONO_FAMILY, size=13).create_scaled_tuple(scale)
            self._example_font = (scale, tkfont.Font(root=self, font=spec))
        font = self._example_font[1]
        fitted = fit_text(self._example_full, font, max(width // 2, 1)) if width > 1 else ""
        _set_text(self.example_label, fitted or self._example_full)

    @property
    def example(self) -> str:
        """The example name as it is worked out (before it is cut to fit)."""
        return self._example_full

    # --- bands -----------------------------------------------------------------------------

    def _images_for(self, row: Row, width: int, height: int) -> tuple[Any, Any]:
        """A band's poster and backdrop when loaded; otherwise start loading them (again, if the
        cache let them go; never again once TMDB had none). `width` × `height` is the backdrop
        strip in pixels (the list's `backdrop_size`); the pictures are asked in CTk units. A done
        band gets its poster faded and no backdrop."""
        scale = self.list.scale or 1.0
        pw, ph = (max(1, round(px / scale)) for px in self.list.poster_size)
        bw, bh = (max(1, round(px / scale)) for px in (width, height))
        images = self.images
        faded = faded_fill(self.look) if row.faded else None
        poster = images.cached_poster(row.art, pw, ph, faded=faded) if row.art else None
        wants_backdrop = bool(row.backdrop) and not row.faded
        backdrop = images.cached_backdrop(row.backdrop, bw, bh) if wants_backdrop else None
        wanted: list[tuple[Key, Callable[[], Awaitable[Any]]]] = []
        if row.art and poster is None:
            key = images.poster_key(row.art, pw, ph, faded=faded)
            if self._should_load(key):
                load = functools.partial(images.poster, row.art, pw, ph, faded=faded)
                wanted.append((key, load))
        if wants_backdrop and backdrop is None:
            key = images.backdrop_key(row.backdrop, bw, bh)
            if self._should_load(key):
                wanted.append((key, functools.partial(images.backdrop, row.backdrop, bw, bh)))
        if wanted:
            self._loading.update(key for key, _ in wanted)
            self.spawn(self._load_images(wanted))
        return poster, backdrop

    def _should_load(self, key: Key) -> bool:
        return key not in self._loading and not self.images.known_missing(key)

    async def _load_images(
        self, wanted: Sequence[tuple[Key, Callable[[], Awaitable[Any]]]]
    ) -> None:
        try:
            results = await asyncio.gather(*(load() for _, load in wanted))
        finally:
            self._loading.difference_update(key for key, _ in wanted)
        if any(r is not None for r in results) and self.winfo_exists():
            self.list.refresh_images()

    def _on_band_action(self, key: int, action: str) -> None:
        ids = self._bands.get(key, [])
        if action == "rename":
            self.spawn(self.forge(only=ids))
        elif action == "undo":
            self.spawn(self.undo_band(ids))
        elif action == "choose":
            for item_id in ids:
                item = self.services.queue.get(item_id)
                if item is not None and item.status is ItemStatus.NEEDS_CHOICE:
                    self._change_match(item)
                    return

    def _on_select(self, key: int | None) -> None:
        self._update_example()

    def move_selection(self, delta: int) -> None:
        keys = self.list.item_keys()
        if not keys:
            return
        selected = self.list.selected
        if selected is None or selected not in keys:
            self.list.select(keys[0] if delta > 0 else keys[-1])
            return
        index = keys.index(selected) + delta
        self.list.select(keys[max(0, min(index, len(keys) - 1))])

    # --- actions ---------------------------------------------------------------------------

    def shortcut(self, action: str) -> bool:
        """Run a keyboard shortcut unless the queue isn't shown or the user is typing in a text
        field. True when it was taken."""
        if not self.winfo_ismapped():
            return False
        focused = self.focus_get()
        if focused is not None and focused.winfo_class() in _TYPING_CLASSES:
            return False
        if action == "preview":
            self.spawn(self.preview())
        elif action == "forge":
            self.spawn(self.forge())
        elif action == "undo":
            self.spawn(self.undo_latest())
        elif action in ("up", "down"):
            self.move_selection(-1 if action == "up" else 1)
        return True

    def _forge_clicked(self) -> None:
        self.spawn(self.forge())

    def _preview_clicked(self) -> None:
        self.spawn(self.preview())

    def _cancel_clicked(self) -> None:
        self._cancel.set()

    def _remove(self, key: int) -> None:
        self.services.queue.remove(key)

    def _clear(self) -> None:
        if not self.running:
            self.services.queue.clear()

    def _browse_folder(self) -> None:
        folder = filedialog.askdirectory(
            parent=self.winfo_toplevel(), title=t("Choose a folder with videos"), mustexist=True
        )
        if folder:
            self.add_paths([Path(folder)])

    def _browse_files(self) -> None:
        patterns = " ".join(f"*{ext}" for ext in sorted(VIDEO_EXTENSIONS))
        files = filedialog.askopenfilenames(
            parent=self.winfo_toplevel(),
            title=t("Choose video files"),
            filetypes=[(t("Video files"), patterns), (t("All files"), "*.*")],
        )
        if files:
            self.add_paths([Path(f) for f in files])

    def add_paths(self, paths: Sequence[Path]) -> None:
        """Queue these files and folders (folders are scanned, subfolders per the setting):
        the + buttons, Explorer drops and the window's requests."""
        if paths:
            self.spawn(self.add(paths))

    async def add(self, paths: Sequence[Path]) -> int:
        """`add_paths`, awaited: how many files were new."""
        recursive = self.services.settings.current.scan_subfolders
        added = await self.services.queue.add_paths(list(paths), recursive)
        if added:
            log.info("Added %d file(s) to the queue", added)
            self._say(tn("Added {n} file to the queue", "Added {n} files to the queue", added))
        return added

    def _change_match(self, item: QueueItem) -> None:
        self.spawn(self._pick_match(item))

    async def _pick_match(self, item: QueueItem) -> bool:
        choice = await MatchPicker.ask(self, item, self.services, look=self.look)
        if choice == "cancel":
            return False
        await self.services.queue.choose_match(item.id, choice)
        return True

    async def _settle_uncertain(self, only: Sequence[int] | None = None) -> bool:
        wanted = None if only is None else set(only)
        uncertain = [i for i in self.services.queue.uncertain() if wanted is None or i.id in wanted]
        if not uncertain:
            return True
        review, top = t("Review them"), t("Use top results")
        answer = await self.ask(
            t("Some matches are uncertain"),
            tn(
                "TMDB wasn't sure about {n} file. You can check it now or use TMDB's top result.",
                "TMDB wasn't sure about {n} files. You can check them now or use TMDB's top "
                "result for each.",
                len(uncertain),
            ),
            [review, top, t("Cancel")],
        )
        if answer == review:
            for item in uncertain:
                if item.status is ItemStatus.NEEDS_CHOICE and not await self._pick_match(item):
                    return False
            return True
        if answer == top:
            for item in uncertain:
                item.status = ItemStatus.READY
            return True
        return False

    async def _trailer_options(
        self, options: dict[Kind, PresetOptions]
    ) -> dict[Kind, PresetOptions]:
        """The options for this run: trailers only once their tools are there (asked first)."""
        trailers = self.services.trailers
        if not any(o.trailer for o in options.values()):
            return options
        without = {kind: o.model_copy(update={"trailer": False}) for kind, o in options.items()}
        if not trailers.tools_present():
            download = t("Download")
            answer = await self.ask(
                t("Download trailer tools?"),
                t(
                    "Trailers need two free tools, yt-dlp and ffmpeg (about 110 MB, downloaded "
                    "once from their official GitHub pages and checked before use)."
                ),
                [download, t("Skip trailers")],
            )
            if answer != download:
                return without
            self._show_actions(running=False, installing=True)
            label = t("Downloading the trailer tools…")
            self.progress.show(0, label)
            try:
                await trailers.install_tools(
                    lambda _step, fraction: self.progress.show(fraction, label)
                )
            except ToolInstallError as exc:  # its text is translated and names no address
                log.error("Couldn't install the trailer tools")
                self._say(
                    t("Couldn't install the trailer tools: {reason}", reason=exc),
                    level=logging.ERROR,
                )
                return without
            finally:
                if self.winfo_exists():
                    self._show_actions(running=False)
        await trailers.maybe_update()
        return options

    def _set_running(self, running: bool, total: int = 0) -> None:
        self.running = running
        self._run_total, self._run_done, self._run_name = total, set(), ""
        if not self.winfo_exists():
            return
        state = "disabled" if running else "normal"
        for button in (self.files_button, self.folder_button):
            button.configure(state=state)
        self._show_actions(running=running)
        if running:
            self._show_progress()
        else:
            self.refresh()

    def _show_progress(self) -> None:
        total = max(self._run_total, 1)
        done = len(self._run_done)
        if self._run_name:
            text = t(
                "Renaming {n} of {total} · {name}",
                n=min(done + 1, total),
                total=total,
                name=self._run_name,
            )
        else:
            text = t("Renaming {n} of {total}", n=min(done + 1, total), total=total)
        self.progress.show(done / total, text)

    def _heard_progress(self, event: ProgressEvent) -> None:
        """A step of the run (dropped files or downloads): the bar and its words."""
        item = self.services.queue.get(event.item_id) if event.item_id is not None else None
        if event.status == "working" and item is not None:
            self._run_name = display_name(item)
        elif event.item_id is not None:
            self._run_done.add(event.item_id)
        if self.winfo_exists() and self.running:
            self._show_progress()

    def _on_progress(self, event: ProgressEvent) -> None:
        self._heard_progress(event)
        queue = self.services.queue
        if event.item_id is not None:
            if event.status == "done":
                queue.mark_finished(event.item_id, self._targets_in_run.get(event.item_id))
            elif event.status == "skipped":
                queue.mark_finished(event.item_id, None)
            elif (item := queue.get(event.item_id)) is not None:
                item.status = _PROGRESS_STATUS[event.status]
                item.message = event.message
        if self.winfo_exists():
            self._schedule_refresh(_REFRESH_PROGRESS_MS)

    def _on_watched_progress(self, event: ProgressEvent) -> None:
        self._heard_progress(event)
        if self.winfo_exists():
            self._schedule_refresh(_REFRESH_PROGRESS_MS)

    def request_close(self, close: Callable[[], None]) -> bool:
        """True if the window may close now; otherwise the run stops after its current file and
        `close` is called once it has finished and been recorded."""
        if not self.running:
            return True
        self._close_after_run = close
        self._cancel.set()
        log.warning("Stopping after the current file, then closing")
        self._say(t("Stopping after the current file, then closing…"), level=logging.WARNING)
        return False

    async def settled(self) -> None:
        """Wait for every lookup."""
        await self.services.queue.wait_idle()

    def _plan_parts(
        self, items: Sequence[QueueItem], options: dict[Kind, PresetOptions]
    ) -> list[PlanPart]:
        """The dropped files, by kind, each with its kind's rule and options."""
        inputs = {i.item_id: i for i in self.services.queue.plan_inputs([i.id for i in items])}
        parts: list[PlanPart] = []
        for kind in _KINDS:
            of_kind = [inputs[i.id] for i in items if item_kind(i) == kind and i.id in inputs]
            if of_kind:
                choice = self.choices[kind]
                parts.append((of_kind, choice.rule, options.get(kind, choice.options)))
        return parts

    def _batch_options(self, items: Sequence[QueueItem]) -> dict[Kind, PresetOptions]:
        """The card's options for the kinds of these dropped files (downloads go by the
        library's preset)."""
        kinds = {item_kind(i) for i in items if i.watched is None}
        return {kind: self.choices[kind].options for kind in _KINDS if kind in kinds}

    @_one_at_a_time
    async def preview(self) -> None:
        """Write what Forge would do to the Activity panel; nothing changes."""
        await self.settled()
        items = [i for i in self.pending_items() if i.watched is None]  # downloads: their bands
        if not items:
            return
        parts = self._plan_parts(items, self._batch_options(items))
        plan = await asyncio.to_thread(build_mixed_plan, parts)
        log.info("Preview of %d file(s); nothing changed", len(plan.files))
        self._say(t("Preview (nothing has been changed):"))
        for line in describe(plan):
            self._say(line)

    def _pending(self, only: Sequence[int] | None) -> list[QueueItem]:
        """What Forge renames: a band's files, or (Forge for all) everything except downloads
        that wait for the owner (those go through their own band's Rename only)."""
        pending = [
            i
            for i in self.pending_items()
            if only is not None or i.watched is None or i.status not in NEEDS_OWNER
        ]
        if only is None:
            return pending
        wanted = set(only)
        return [i for i in pending if i.id in wanted]

    @_one_at_a_time
    async def forge(self, only: Sequence[int] | None = None) -> None:
        """Rename every pending file, or only those in `only` (a band's Rename)."""
        await self.settled()
        if not self._pending(only) or not await self._settle_uncertain(only):
            return
        options = await self._trailer_options(self._batch_options(self._pending(only)))
        items = self._pending(only)
        self._cancel = asyncio.Event()
        self._set_running(True, len(items))
        try:
            result = RunResult()
            watched = [i.id for i in items if i.watched is not None]
            if watched:  # into the library, with the Series or Movies preset
                result = await self.services.auto.rename(
                    watched,
                    source=RunSource.APP,
                    on_progress=self._on_watched_progress,
                    cancel=self._cancel,
                )
            dropped = [i for i in items if i.watched is None]
            if dropped:
                async with self.services.run_lock:  # never alongside an automatic run
                    result = _merge(result, await self._forge_dropped(dropped, options))
        finally:
            self._set_running(False)
            close, self._close_after_run = self._close_after_run, None
            if close is not None:
                if self.winfo_exists():
                    self.after(10, close)
                else:
                    close()
        log.info(
            "Forged: %d renamed, %d already right, %d failed, %d poster(s), %d trailer(s)%s",
            result.renamed,
            result.skipped,
            result.failed,
            result.posters,
            result.trailers,
            " (cancelled)" if result.cancelled else "",
        )
        if result.failed:
            self._say(summary_text(result), level=logging.WARNING)
        else:
            self._say(summary_text(result), success=True)

    def _preset_name(self, items: Sequence[QueueItem]) -> str | None:
        """The run's preset, as History shows it: each kind's preset in it."""
        names: list[str] = []
        for kind in _KINDS:
            preset = self.choices[kind].preset
            if preset is not None and any(item_kind(i) == kind for i in items):
                if preset.name not in names:
                    names.append(preset.name)
        return " + ".join(names) or None

    async def _forge_dropped(
        self, items: Sequence[QueueItem], options: dict[Kind, PresetOptions]
    ) -> RunResult:
        queue = self.services.queue
        inputs = queue.plan_inputs([i.id for i in items])
        plan = await asyncio.to_thread(build_mixed_plan, self._plan_parts(items, options))
        self._targets_in_run = {f.item_id: f.target for f in plan.files}
        result = await self.services.executor.run(
            plan,
            source=RunSource.APP,
            preset_name=self._preset_name(items),
            on_progress=self._on_progress,
            cancel=self._cancel,
            kinds={i.media.kind for i in items},
            media={i.item_id: i.media for i in inputs},  # for the move records
        )
        for item in items:  # a Done band's Undo needs the run
            if item.status is ItemStatus.DONE and item.run_id is None:
                item.run_id = result.run_id
        return result

    async def undo_band(self, ids: Sequence[int]) -> None:
        """A Done band's Undo: put those files back; they leave the queue."""
        queue, history = self.services.queue, self.services.history
        items = [i for i in (queue.get(n) for n in ids) if i is not None and i.run_id is not None]
        op_ids: list[int] = []
        put_back: list[Path] = []  # downloads that return to their folder
        async with self.services.run_lock:
            for item in items:
                for op in await history.operations(item.run_id or 0):
                    if op.kind is OpKind.RENAME and op.dst == str(item.path) and op.undoable:
                        op_ids.append(op.id)
                        if item.watched is not None and op.src:
                            put_back.append(Path(op.src))
            if not op_ids:
                log.info("Nothing to undo for these files")
                self._say(t("Nothing to undo for these files."))
                return
            result = await self.services.undo.undo_operations(op_ids)
        if put_back:  # never moved on their own again; Rename moves them when wanted
            await self.services.auto.keep_back(put_back)
        for item in items:
            queue.remove(item.id)
        log.info("Put back %d file(s)", result.undone)
        self._say(
            tn("Put back {n} file", "Put back {n} files", result.undone), success=True
        )

    @_one_at_a_time
    async def undo_latest(self) -> None:
        """Ctrl+Z: undo the newest run that isn't undone yet, after a question."""
        runs = [r for r in await self.services.history.list_runs() if r.status != "undone"]
        if not runs:
            log.info("Nothing to undo")
            self._say(t("Nothing to undo."))
            return
        run = runs[0]
        undo = t("Undo")
        answer = await self.ask(
            t("Undo the last run?"),
            t(
                "This puts back {files} and removes {created} from that run.",
                files=tn("{n} renamed file", "{n} renamed files", run.file_count),
                created=tn(
                    "{n} downloaded poster or trailer",
                    "{n} downloaded posters or trailers",
                    run.created_count,
                ),
            ),
            [undo, t("Cancel")],
        )
        if answer != undo:
            return
        result = await self.services.undo.undo_run(run.id)
        log.info("Undo finished: %d change(s) reversed", result.undone)
        self._say(
            tn(
                "Undo finished: {n} change reversed",
                "Undo finished: {n} changes reversed",
                result.undone,
            ),
            success=True,
        )

    def destroy(self) -> None:
        self._unsubscribe()
        self._cancel_refresh()
        try:
            top = self.winfo_toplevel()
            for sequence, bind_id in self._bindings:
                top.unbind(sequence, bind_id)
        except tk.TclError:
            pass  # the window is going too
        self._bindings = []
        for task in list(self._tasks):
            task.cancel()
        self.images.clear()
        super().destroy()
