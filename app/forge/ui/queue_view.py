"""The queue: what will be renamed, to what, and Forge (ported from MediaForge's ui/queue_view.py;
its pure part, the rows, is app/forge/ui/queue_rows.py). The owner's design:
mockups/forge-chosen.html, except its card's Shows | Films switch: since 3.0.1 every band has its
own preset (the owner's decision of 2026-11-02), set with the band's chip; a change on the card
(the preset bar) goes for every band not done of its kind (2026-11-11, `_spread_bar`).

The page (app/forge/ui/page.py) puts "+ Files", "+ Folder ▾" (its menu: choose a folder, "Scan
subfolders") and "Library ▾" (Fix my library, Find duplicates…) on the main window's tabs row
(`tools_master`; without one they make a row of the view's own at its top). The view's top is the preset card, for the band selected in the list
(every band has its own preset; with nothing selected the first band is, with an empty queue
the card shows the films' default "For: new files"): "For: {name}" (a show with its episode
count, a film with its year) and "Use for all ▾" (its menu copies the band's choice to all films,
all shows, or everything in the queue, each entry only where the choice fits; hidden with one
band); the preset menu (the presets that fit the band's kind), "✎ Custom" (a one-off naming rule
for this item, not saved) and "Save as preset…"; the naming rule with its codes in the link colour and an
example name; one line of option buttons (Extras: Poster, Season posters for shows, Trailer;
Tidy up: Clean junk, Empty folders), and "Changed for this item · Save to preset" at its end while
the options or the rule differ from the preset.

Then a row with the filter All / Needs you / Ready with their counts at its left and "Clear ▾"
(Clear what's done, Clear everything, and the Settings tick "Remove files from the queue after
they're forged", `remove_after_forge`, saved at once), Preview and the big Forge button at its right (gone while a run goes: then the run's dock shows
centred under the list, its progress with Cancel on it), and one list of shows and films
(`queue_rows`), filtered by that:
a band per show with its episodes under it, a band per film; each band's status as a dot and
short words saying why ("Already in your library", "Not sure which film", "Ready to forge",
"Forged 14:32"), with "Name style" and its preset chip under it (always shown on a band not done;
its tip the name the band's file gets; a click opens the presets that fit and sets that band's).
A band that needs the owner asks a question and its buttons are the answers (queue_rows'
docstring; mockups/forge-band-buttons.html, "B"): "Use this one instead" (`replace_band`), "Yes,
forge it" (`accept_band`: the guess taken as the match picker takes it, then forged), "No, pick
the right film…" / "Wrong film?" (the match picker), "Forge this" / "Try again" (this band only);
a ready band's buttons show only under the pointer; "📂 Show file" and the extras are quiet links.
A click on a band selects it. Under the question of a download already in the library, the two
copies' quality and size (app/forge/core/replace_facts.py, read once per name in a thread under a
time limit, `read_facts` replaceable in tests). "Leave it alone ▾" (and "Keep the one I have ▾")
opens a small menu: "Leave this file alone" (its files) and, when they sit in a subfolder of a
watched folder (its own, else one the rules watch: `services.rules.watched_folders`), "Leave this
whole folder alone" (the download's own folder right under the watched one,
`core.ignored.top_folder`); Auto-rename leaves them alone from then on
(`services.ignored`, listed in Settings › Auto-rename) and their rows leave the queue. Forge
renames everything not
waiting for the owner in one go: dropped files where they are, or in their library folder with
the band's Move to library folder on (without that folder chosen they are left alone, and say so),
each band with its own preset (or its Custom rule) in one run, and watched downloads always
where their rule sends them with their band's preset, through the automatic renamer
(`services.auto.rename(naming=…)`). A band's subtitle says which: "📦 Moves to …" or "📁 Stays …"
(a watched download's rule that sorts its files in place: "📁 Sorted in …"). A watched download's
band names its rule ("Rule: 4K films", `route_of`: app/forge/core/rules.py's `route_for_item`); a
click on it opens that rule's editor in Settings › Auto-rename (`open_rule`, through the page's
`on_settings`). A ready one that waits says why: its rule is Manual (or it was taken as Manual
once), its rule's preset is gone, its rule was changed since it took the file, or no rule takes it
any more (then it names no destination) (`_waits`). The bands
follow a change of the rules (`services.rules.subscribe`).

Each band's `Choice` (preset, options, Custom rule) lives as long as its files are queued (in
memory): the files of one show or film share one (`band_identity`); a new band starts with its
kind's default (the one remembered for the kind, the last chosen, the library's, the first that
fits), a watched download with its rule's preset and the rule's extras (`_rule_default`; the kind's
default when that preset is gone); presets read again keep each band's preset
by name, else its default. Choosing a preset for a band remembers it as its kind's last. A
right-click request's preset (`add_paths(paths, preset=)`) names the files it added;
`select_preset(name)` alone names every band not done that it fits. Forge runs one at a time
with the renamer (`services.run_lock`, taken through `taking`: while it waits for another run the
dock says "Waiting for Auto-rename to finish…"); closing the window while a run goes (`request_close`)
stops it after its current file. Keys, bound on the main window: Enter = Preview, Ctrl+Enter =
Forge, Ctrl+Z = undo the last run, Up / Down = the selection; only while the queue shows (nothing
on AirGrabber's other pages or on History), never while typing in a text field (`shortcut`).

With 1,000 files whose lookups land for a minute it stays smooth (F-33): a burst of queue events
brings one refresh at a pace set by the last one's cost, a refresh is built in one turn of the
window and shown in the next, and widgets are configured only when what they show changed.

It talks only to `ForgeServices`' services, starts coroutines through `app.ui.tasks.spawn` (and
cancels them on destroy), reports what it did to the Activity panel (`services.activity`, in the
interface language; the log stays English and names no paths) and, as Activity is a click further
away, to `on_message` (the main window's status bar), and paints in the Forge look.
Build it while the event loop runs (the page is opened from it): it loads the presets at
once and takes Explorer drops on itself (app/forge/ui/file_drops.py).
"""

from __future__ import annotations

import asyncio
import dataclasses
import functools
import logging
import time
import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import field
from datetime import date, datetime
from datetime import time as dt_time
from pathlib import Path
from tkinter import filedialog
from typing import TYPE_CHECKING, Any, Concatenate

import customtkinter as ctk

from app.forge.clients.trailers import ToolInstallError
from app.forge.core import fsops
from app.forge.core.auto import (
    TOOLS_NOT_THERE,
    WAITING_FOR_AUTO,
    NamingChoice,
    held_in_library,
    settling_message,
    taking,
)
from app.forge.core.duplicates import aside_folder, needs_a_place, place_problem
from app.forge.core.executor import ProgressEvent, RunResult
from app.forge.core.formatter import name_file
from app.forge.core.ignored import top_folder
from app.forge.core.library import inside, library_folder, route_inputs
from app.forge.core.parser import VIDEO_EXTENSIONS
from app.forge.core.planner import PlanPart, build_mixed_plan, describe, identity_of
from app.forge.core.queue import ItemStatus, QueueEvent, QueueItem
from app.forge.core.replace_facts import ReplaceFacts, replace_facts
from app.forge.core.rules import Route, in_watched_folder, route_for_item
from app.forge.core.run_progress import PAGE, RunSteps
from app.forge.models.history import OpKind, RunSource
from app.forge.models.media import MediaKind, TypeHint
from app.forge.models.preset import Preset, PresetOptions, has_quality, without_quality
from app.forge.ui.file_drops import attach_file_drops
from app.forge.ui.file_browser import explorer_target, open_explorer
from app.forge.ui.images import BandImages, CardShape, Key
from app.forge.ui.duplicates_window import DuplicatesWindow
from app.forge.ui.look import Pair, app_look
from app.forge.ui.match_picker import MatchPicker
from app.forge.ui.queue_rows import (
    ACCEPT_ACTION,
    CHOOSE_ACTION,
    EXPLORER_ACTION,
    FINISHED,
    FORGE_ACTION,
    IGNORE_ACTION,
    NEEDS_OWNER,
    REPLACE_ACTION,
    REPLACE_LABEL,
    Kind,
    QueueRows,
    Show,
    Waits,
    WatchedLook,
    build_queue_rows,
    empty_texts,
    episode_code,
    is_unsure,
    item_kind,
)
from app.forge.ui.rule_rows import name_of
from app.forge.ui.subtitle_control import CC_ACTION, CcLook, SubtitleDesk, VideoTarget
from app.forge.ui.widgets.chips import ForgeProgress, OptionButton, fit_text
from app.forge.ui.widgets.dialogs import ask_choice, ask_text
from app.forge.ui.widgets.virtual_list import (
    FOLDERS_ACTION,
    MONO_FAMILY,
    RULE_ACTION,
    Row,
    VirtualList,
    faded_fill,
)
from app.i18n import N_, number, t, tn
from app.ui import tasks, theme
from app.ui.missing_control import MenuEntry, popup_menu
from app.ui.tooltip import Tooltip, add_tooltip

from app.forge.ui.queue_parts import (
    ALL_FILMS,
    ALL_SHOWS,
    AskFn,
    AskTextFn,
    BAND_HEIGHT,
    BandId,
    CHANGED,
    CUSTOM_TIP,
    Choice,
    EVERYTHING,
    FILE_GONE,
    FILTERS,
    FOR_ALL_FILMS,
    FOR_ALL_SHOWS,
    FOR_LINE,
    FOR_NEW,
    IGNORE_FOLDER,
    LINE_HEIGHT,
    LIST_PAD,
    MOVE_ALWAYS_TIP,
    MOVE_BUTTON,
    MOVE_TIP,
    NO_DROPS_HINT,
    NO_DROPS_TITLE,
    NO_EXAMPLE,
    NO_FIT,
    NO_LIBRARY_FOLDER,
    NO_LIBRARY_FOLDERS,
    AUTO_MODE,
    MANUAL_MODE,
    MODE_TIP,
    OPTION_GROUPS,
    PRESETS_CAPTION,
    QUALITY_OPTION,
    OptionButtonSpec,
    PAD_X,
    PREVIEW_READY,
    QUEUE_SIZES,
    SUBTITLES_OPTION,
    USE_FOR_ALL,
    _CARD_BAKERS,
    _CODE,
    _FOR_MAX,
    _KEYS,
    _KINDS,
    _PROGRESS_STATUS,
    _REFRESH_EVENT_MS,
    _REFRESH_FORMAT_MS,
    _REFRESH_PROGRESS_MS,
    _REFRESH_SHARE,
    _TYPING_CLASSES,
    _merge,
    _same,
    _set_text,
    band_identity,
    band_name,
    card_colours,
    display_name,
    fits,
    forge_label,
    summary_text,
)

if TYPE_CHECKING:
    from app.forge.services import ForgeServices

log = logging.getLogger(__name__)
FIX_LIBRARY_TEXT = N_("🧰 Fix my library")  # the Series and Movies folders into the queue
FIND_DUPLICATES_TEXT = N_("🔁 Find duplicates…")
LIBRARY_TEXT = N_("Library")  # its menu: Fix my library, Find duplicates…
CLEAR_TEXT = N_("Clear")  # its menu: what's done, everything, the remove-after-forge tick
CLEAR_DONE_TEXT = N_("Clear what's done")
CLEAR_ALL_TEXT = N_("Clear everything")
REMOVE_AFTER_TEXT = N_("Remove files from the queue after they're forged")
SETTLING_TICK_MS = 30_000  # the empty queue's "ready in about 8 min" counts down this often
LOOKING_TEXT = N_("Looking for duplicates in your library…")
# A band's "Use this one instead" (`replace_band`): the question, and the folder picker
# for a library folder at a share or drive root.
REPLACE_QUESTION = N_("Replace {name} in the library? The one there now moves to {folder}.")
REPLACE_QUESTION_MORE = N_(
    "Replace these {n} files in the library? The ones there now move to {folder}."
)
REPLACE_BUTTON = N_("Replace")
WHERE_REPLACED_GO = N_("Where should the copies you replace go?")
# "Yes, forge it" clicked while the band's guess had changed since it was shown: nothing forged.
GUESS_CHANGED = N_("MediaForge's guess changed meanwhile: have another look.")

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


def _switch_label(text: str) -> str:
    """A side of the Automatic | One preset for all switch, with room around its words."""
    return f"  {t(text)}  "


def _waits(item: QueueItem, route: Route) -> Waits:
    """Why a watched download's ready band waits: its rule was changed since it took the file,
    its rule's preset is gone (it acts as Manual, whatever its mode), or it is Manual (the rule's
    mode, or taken as Manual once)."""
    if item.rule_changed:
        return "changed"
    if route.preset is None:
        return "no_preset"
    if item.manual or route.rule.mode == "manual":
        return "manual"
    return ""


class QueueView(ctk.CTkFrame):
    """The queue on MediaForge's page. `tools_master`: where "+ Files" and "+ Folder" go (its
    owner places `toolbar` there and destroys it); without it a row at the view's top."""

    def __init__(
        self,
        master: Any,
        services: ForgeServices,
        *,
        look: theme.Look | None = None,
        tools_master: Any = None,
    ) -> None:
        self.look: theme.Look = app_look() if look is None else look
        super().__init__(master, fg_color=self.look.BG, corner_radius=0)
        self.services = services
        # CC ▾ on the done bands: its requests' states stay through the list's rebuilds.
        self.subtitle_desk = SubtitleDesk(
            services.subtitles,
            spawn=self.spawn,
            after=self.after,
            on_change=self._cc_changed,
        )
        self.presets: list[Preset] = []
        self._choices: dict[int, Choice] = {}  # item id → its band's Choice (shared in a band)
        self._new_choice = Choice()  # the card with an empty queue: the films' default
        # Auto: every band has its preset, its chip changes it, and the card stays out of the way.
        # Manual: the card (the preset bar) shows the selected band's settings.
        self.manual = services.settings.current.presets_manual
        self._card_key: tuple[object, ...] | None = None  # what the card shows (for refreshes)
        self.show: Show = "all"  # the list's filter
        self.loaded = False  # the presets have been read
        self.running = False  # a Forge run is going (its progress and Cancel show)
        self.busy = False  # set by @_one_at_a_time while Preview, Forge or Undo is going
        self.on_counts: Callable[[int], None] = lambda waiting: None  # the switch's badge
        self.on_message: Callable[[str], None] = lambda text: None  # the status bar
        self._bands: dict[int, list[int]] = {}  # band key → its items
        self._targets: dict[int, Path] = {}
        self._targets_in_run: dict[int, Path] = {}
        self._loading: set[Key] = set()  # band pictures being fetched right now
        # Card pictures waiting to be baked: their path and how to bake them, the latest last.
        self._bakes: dict[Key, tuple[str, Callable[[], Awaitable[Any]]]] = {}
        self._bakers = 0  # tasks baking them now
        self._close_after_run: Callable[[], None] | None = None
        # Right-click presets asked before the presets were read: (name, the files it added).
        self._requests: list[tuple[str, list[int] | None]] = []
        self.duplicates_window: DuplicatesWindow | None = None  # "Library ▾" → Find duplicates…
        self._settling_id: str | None = None  # the empty queue's settling line, said again
        self._settling_shown: str | None = None  # … as it shows now (None: not shown)
        self._refresh_id: str | None = None  # a refresh coming (after a queue event, typing …)
        self._show_id: str | None = None  # its rows, built, shown when Tk is idle next
        self._flow_id: str | None = None  # the option line laid out again
        self._refreshed_at = 0.0  # time.monotonic() of the last refresh
        self._refresh_cost = 0.0  # how long it took (s)
        self._told: tuple[object, int] | None = None  # (on_counts, waiting) last told
        # The run's dock: its words and how far (also reported for the dock on every page).
        self._run_steps = RunSteps()
        self._waiting_for_lock = False  # the run waits for another one (`taking`)
        self._setting_rule = False  # the rule's box is being filled, not typed in
        self._example_font: tuple[float, tkfont.Font] | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._cancel = asyncio.Event()
        self._bindings: list[tuple[str, str]] = []
        self.on_choose_folders: Callable[[], object] = lambda: None  # the page: Settings' spot
        # The page's: Settings at a section, scrolled to a spot (a band's rule: its editor).
        self.on_settings: Callable[..., object] = lambda section, spot=None: None
        self.ask: AskFn = self._ask  # the questions, replaceable in tests
        self.ask_folder: Callable[[], str] = self._ask_folder  # the folder picker, the same
        self.ask_name: AskTextFn = self._ask_name
        # "Use this one instead"'s facts per item: (the name it would get, them), the read being
        # replaceable in tests (a fake disk).
        self._facts: dict[int, tuple[Path, ReplaceFacts | None]] = {}
        self._keeps: dict[int, list[int]] = {}  # band → its files in the library (Keep's menu)
        self._guesses: dict[int, int] = {}  # band → the TMDB id its question names
        self.read_facts: Callable[[QueueItem, Path], Awaitable[ReplaceFacts]] = replace_facts

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)
        self._build_toolbar(tools_master)
        self._build_card()
        self._build_filters()
        self._build_list()
        self.images = BandImages(services.images, self.list)
        theme.on_mode_change(self, self._repaint)

        self._unsubscribe = services.queue_events.subscribe(self._on_queue_event)
        # A rule renamed or changed: the bands say so (their labels, routes and waits).
        self._unrules = services.rules.subscribe(lambda _rules: self._schedule_refresh(0))
        self._unwatch = services.auto.watch_status(self._watch_status)  # after each look
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
        """A toolbar or card button: a quiet surface, a thin edge (the accent for the add buttons)."""
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

    def _build_toolbar(self, master: Any) -> None:
        """+ Files, + Folder ▾ and the presets switch, in `master` (the page's tabs row) or in a row of their own."""
        look = self.look
        bar = ctk.CTkFrame(self if master is None else master, fg_color="transparent")
        if master is None:
            bar.grid(row=0, column=0, sticky="ew", padx=PAD_X, pady=(14, 0))
        self.toolbar = bar
        self.files_button = self._tool_button(
            bar, t("+ Files"), self._browse_files, edge=look.ACCENT
        )
        self.files_button.pack(side="left")
        self.folder_button = self._tool_button(
            bar, t("+ Folder") + " ▾", self._folder_menu, edge=look.ACCENT
        )
        self.folder_button.pack(side="left", padx=(10, 0))
        caption = ctk.CTkLabel(
            bar, text=t(PRESETS_CAPTION), font=theme.font(13), text_color=theme.TEXT_MUTED
        )
        caption.pack(side="left", padx=(18, 8))
        self.mode_switch = ctk.CTkSegmentedButton(
            bar,
            values=[_switch_label(AUTO_MODE), _switch_label(MANUAL_MODE)],
            height=32,
            font=theme.font(13),
            fg_color=look.SURFACE,
            selected_color=look.SELECTED,
            selected_hover_color=look.SELECTED_HOVER,
            unselected_color=look.SURFACE,
            unselected_hover_color=look.CHIP_HOVER,
            text_color=theme.BUTTON_TEXT,
            command=self._mode_clicked,
        )
        self.mode_switch.set(_switch_label(MANUAL_MODE if self.manual else AUTO_MODE))
        self.mode_switch.pack(side="left")
        add_tooltip(caption, t(MODE_TIP))
        add_tooltip(self.mode_switch, t(MODE_TIP))


    def _build_actions(self, row: Any) -> None:
        """Library ▾, Clear ▾, Preview and Forge at the right end of the filters' row."""
        look = self.look
        self.actions = ctk.CTkFrame(row, fg_color="transparent")
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
            self.actions, t(CLEAR_TEXT) + " ▾", self._clear_menu
        )
        # Library ▾ before them: here, not in the tabs row, so that row fits a narrow window in
        # Dutch too; a run leaves it there.
        self.library_button = self._tool_button(
            self.actions, t(LIBRARY_TEXT) + " ▾", self._library_menu
        )
        # A run's dock, centred under the list (its row, 4, is empty otherwise), Cancel on it.
        self.progress = ForgeProgress(self, look=look, ground=look.BG)
        self.cancel_button = self.progress.action_button(t("Cancel"), self._cancel_clicked)
        self._show_actions(running=False)

    def _show_actions(self, *, running: bool, installing: bool = False) -> None:
        """The filters' row's right end: Library ▾ (always), Clear ▾, Preview and Forge; or,
        with only Library ▾ there, the dock under the list with Cancel (a run), or only the dock
        (the trailer tools coming in)."""
        actions = (self.library_button, self.clear_button, self.preview_button, self.forge_button)
        for widget in actions:
            widget.pack_forget()
        self.progress.show_action(self.cancel_button, running)
        if running or installing:
            self.progress.grid(row=4, column=0, pady=(12, 14))
            self.progress.lift()
        else:
            self.progress.grid_remove()
            self.forge_button.pack(side="right", padx=(10, 0))
            self.preview_button.pack(side="right", padx=(10, 0))
            self.clear_button.pack(side="right")
        beside_clear = bool(self.clear_button.winfo_manager())
        self.library_button.pack(side="right", padx=(0, 10) if beside_clear else 0)

    def _build_card(self) -> None:
        look = self.look
        ground, edge, rule_bg = card_colours(look)
        self._card_ground = ground
        card = ctk.CTkFrame(
            self, fg_color=ground, corner_radius=12, border_width=1, border_color=edge
        )
        card.grid(row=1, column=0, sticky="ew", padx=PAD_X, pady=(4, 0))
        card.grid_columnconfigure(0, weight=1)
        self.card = card
        if not self.manual:
            card.grid_remove()

        # What the card is for now, and "Use for all ▾", over the preset line.
        line = ctk.CTkFrame(card, fg_color="transparent")
        line.grid(row=0, column=0, sticky="ew", padx=18, pady=(12, 0))
        self.for_label = ctk.CTkLabel(
            line, text=t(FOR_NEW), font=theme.font(13), text_color=theme.TEXT_MUTED, anchor="w"
        )
        self.for_label.pack(side="left")
        self.use_all_link = ctk.CTkLabel(
            line,
            text=t(USE_FOR_ALL),
            font=theme.font(13, underline=True),
            text_color=look.LINK,
            fg_color=ground,
            cursor="hand2",
        )
        self.use_all_link.bind("<Button-1>", lambda _event: self.use_for_all_menu())
        self._for_full = ""
        self._for_font: tuple[float, tkfont.Font] | None = None

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.grid(row=1, column=0, sticky="ew", padx=16, pady=(4, 8))
        ctk.CTkLabel(head, text=t("Preset"), font=theme.font(13), text_color=theme.TEXT_MUTED).pack(
            side="left", padx=(2, 8)
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
        add_tooltip(self.custom_button, t(CUSTOM_TIP))

        box = ctk.CTkFrame(card, fg_color=rule_bg, corner_radius=8)
        box.grid(row=2, column=0, sticky="ew", padx=16)
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
        self.option_line.grid(row=3, column=0, sticky="ew", padx=16, pady=(10, 14))
        self.option_line.grid_propagate(False)
        self.option_line.pack_propagate(False)
        self._option_rows = [
            tk.Frame(self.option_line, bg=theme.pick(ground), bd=0, highlightthickness=0)
            for _ in range(3)
        ]
        self.option_buttons: dict[str, OptionButton] = {}
        self._option_specs: dict[str, OptionButtonSpec] = {}
        self._option_tips: dict[str, Tooltip] = {}
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
                if spec.series == SUBTITLES_OPTION and not self.services.subtitles.available:
                    continue
                button = OptionButton(
                    group,
                    t(spec.name),
                    functools.partial(self._option_clicked, spec.name),
                    look=look,
                    ground=ground,
                )
                button.pack(side="left", padx=(0, 8))
                self._option_tips[spec.name] = add_tooltip(button.canvas, t(spec.tip))
                self.option_buttons[spec.name] = button
                self._option_specs[spec.name] = spec
            self._groups.append(group)
        changed = tk.Frame(self.option_line, bg=theme.pick(ground), bd=0, highlightthickness=0)
        ctk.CTkLabel(
            changed,
            text=t(CHANGED) + " · ",
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
        row.grid(row=2, column=0, sticky="ew", padx=PAD_X, pady=(10, 0))
        self._build_actions(row)
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
        holder.grid(row=3, column=0, sticky="nsew", padx=LIST_PAD, pady=(4, 0))
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
        self.list.select_bands = True  # a click on a band selects it (the card follows)
        self.list.on_remove = self._remove
        self.list.on_action = self._on_band_action
        self.list.images = self._images_for
        self.list.card_picture = self._card_picture
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

    def _say(
        self, text: str, *, level: int = logging.INFO, success: bool = False, status: bool = True
    ) -> None:
        """Say it in Activity and, unless `status` is False, in the status bar."""
        self.services.activity.say(text, level=level, success=success)
        if status:
            self.on_message(text)

    async def _ask(self, title: str, message: str, choices: Sequence[str]) -> str | None:
        return await ask_choice(self, title, message, choices, look=self.look)

    def _ask_folder(self) -> str:
        """Where replaced copies go (native and modal: the loop waits, as Settings' Browse)."""
        return filedialog.askdirectory(parent=self, title=t(WHERE_REPLACED_GO), mustexist=True)

    async def _ask_name(self, title: str, message: str, ok: str) -> str | None:
        return await ask_text(self, title, message, ok, look=self.look)

    @property
    def selected_ids(self) -> list[int]:
        """The files of the band selected in the list (its line or the band itself)."""
        key = self.list.selected
        if key is None:
            return []
        band = next((r.band for r in self.list.rows if r.key == key), None)
        return list(self._bands.get(band, [key])) if band is not None else [key]

    @property
    def kind(self) -> Kind:
        """The kind of the band the card shows (films with an empty queue)."""
        ids = self.selected_ids
        item = self.services.queue.get(ids[0]) if ids else None
        return item_kind(item) if item is not None else "movies"

    @property
    def choice(self) -> Choice:
        """What names the band the card shows (an empty queue's: the films' default)."""
        ids = self.selected_ids
        return self._choices.get(ids[0], self._new_choice) if ids else self._new_choice

    @property
    def fmt(self) -> str:
        """The naming rule the card shows."""
        return self.choice.rule

    def choice_for(self, item: QueueItem) -> Choice:
        """A file's band's choice (its default until the band has one)."""
        choice = self._choices.get(item.id)
        return choice if choice is not None else self._default_choice(item)

    def rule_for(self, item: QueueItem) -> str:
        """A file's naming rule: its band's preset, or the band's Custom rule."""
        return self.choice_for(item).rule

    def pending_items(self) -> list[QueueItem]:
        return self.services.queue.pending()

    # --- presets & options -----------------------------------------------------------------

    async def _load_presets(self) -> None:
        presets = await self.services.presets.list()
        if not self.winfo_exists():
            return
        self.presets = presets
        self.loaded = True
        queue = self.services.queue
        shared: dict[int, list[int]] = {}  # each band's Choice → its files
        for item_id, choice in self._choices.items():
            shared.setdefault(id(choice), []).append(item_id)
        for ids in shared.values():
            item = queue.get(ids[0])
            if item is None:
                continue
            fresh = self._reloaded(self._choices[ids[0]], item)
            for item_id in ids:
                self._choices[item_id] = fresh
        self._new_choice = self._reloaded(self._new_choice, None)
        requests, self._requests = self._requests, []
        for name, request_ids in requests:
            self._apply_request(name, request_ids)
        self._sync_choices()
        self._card_key = None
        self._show_card()
        self.refresh()

    def _reloaded(self, choice: Choice, item: QueueItem | None) -> Choice:
        """A band's choice after the presets were read (again): the same preset when it is still
        there (its options as stored now, unless changed for this item), else its default."""
        kind = item_kind(item) if item is not None else "movies"
        old = choice.preset
        if old is not None:
            fresh = next((p for p in self.presets if p.name == old.name and fits(p, kind)), None)
            if fresh is not None:
                options = fresh.options if choice.options == old.options else choice.options
                return Choice(fresh, options, choice.custom, choice.chosen)
        return self._default_choice(item) if item is not None else self._kind_default(kind)

    def _kind_default(self, kind: Kind) -> Choice:
        """A new band's choice: the preset remembered for its kind, the one last chosen, the
        library's, or the first that fits."""
        presets = self.presets
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

    def route_of(self, item: QueueItem) -> Route | None:
        """A watched download's route by its rule (`route_for_item`; None: no rule takes it)."""
        services = self.services
        return route_for_item(
            item, services.rules.current, services.settings.current, self.presets
        )

    def _rule_default(self, item: QueueItem) -> Choice:
        """A watched download's choice: its rule's preset with the rule's extras (as the renamer
        names it); its kind's default when the rule's preset is gone or no rule takes it."""
        route = self.route_of(item)
        if route is None or route.preset is None:
            return self._kind_default(item_kind(item))
        return Choice(preset=route.preset, options=route.options)

    def _default_choice(self, item: QueueItem) -> Choice:
        if item.watched is not None:
            return self._rule_default(item)
        return self._kind_default(item_kind(item))

    def _sync_choices(self) -> None:
        """Every queued file's band has one Choice: a new file takes its band's (one chosen by
        the owner first), a band without one its default; files that left are forgotten; a
        Choice shared by two bands (one request's files) is copied for each."""
        items = self.services.queue.items
        alive = {i.id for i in items}
        for item_id in [k for k in self._choices if k not in alive]:
            del self._choices[item_id]
        if not self.loaded:
            return  # the defaults come with the presets
        groups: dict[BandId, list[QueueItem]] = {}
        for item in items:
            groups.setdefault(band_identity(item), []).append(item)
        owners: dict[int, BandId] = {}
        for identity, group in groups.items():
            have = [self._choices[i.id] for i in group if i.id in self._choices]
            leader = next((c for c in have if c.chosen), have[0] if have else None)
            if leader is None:
                leader = self._default_choice(group[0])
            elif owners.get(id(leader), identity) != identity:
                leader = dataclasses.replace(leader)
            owners[id(leader)] = identity
            for item in group:
                self._choices[item.id] = leader

    def reload_presets(self) -> None:
        """The presets changed (Settings › Naming, an import): load them again."""
        self.spawn(self._load_presets())

    def presets_for(self, kind: Kind) -> list[Preset]:
        """The presets the card's menu offers for `kind`."""
        return [p for p in self.presets if fits(p, kind)] or list(self.presets)

    def _band_groups(self, *, finished: bool = False) -> dict[BandId, list[int]]:
        """The queue's bands by identity: their files (only those not done unless `finished`)."""
        groups: dict[BandId, list[int]] = {}
        for item in self.services.queue.items:
            if finished or item.status not in FINISHED:
                groups.setdefault(band_identity(item), []).append(item.id)
        return groups

    def _assign(self, ids: Sequence[int], choice: Choice) -> None:
        """`choice` names these files' band(s): every file sharing their Choice follows."""
        old = {id(self._choices[i]) for i in ids if i in self._choices}
        for item_id, current in list(self._choices.items()):
            if id(current) in old:
                self._choices[item_id] = choice
        for item_id in ids:
            self._choices[item_id] = choice

    def select_preset(self, name: str) -> None:
        """Use the preset `name` (a right-click request's without files): for every band not
        done that it fits."""
        if not self.loaded:
            self._requests.append((name, None))  # chosen before the presets have loaded
            return
        self._apply_request(name, None)
        self._card_key = None
        self._show_card()
        self._schedule_refresh(0)

    def _apply_request(self, name: str, ids: Sequence[int] | None) -> None:
        """A request's preset for the files it added (`ids`), or every band not done (None)."""
        preset = next((p for p in self.presets if p.name == name), None)
        if preset is None:
            return
        queue = self.services.queue
        if ids is None:
            groups = list(self._band_groups().values())
        else:
            by_kind: dict[Kind, list[int]] = {}
            for item_id in ids:
                if (item := queue.get(item_id)) is not None:
                    by_kind.setdefault(item_kind(item), []).append(item_id)
            groups = list(by_kind.values())
        kinds: set[Kind] = set()
        for group in groups:
            item = queue.get(group[0])
            if item is None or not fits(preset, item_kind(item)):
                continue
            self._assign(group, Choice(preset=preset, options=preset.options, chosen=True))
            kinds.add(item_kind(item))
        if not groups and ids is None:
            kinds = {k for k in _KINDS if fits(preset, k)}
        for kind in sorted(kinds):
            self._remember(kind, preset.name)
        if ids is None and fits(preset, "movies"):
            self._new_choice = Choice(preset=preset, options=preset.options)

    def set_band_preset(self, ids: Sequence[int], preset: Preset) -> None:
        """`preset` names these files' band from now on (its options; no Custom rule); it is
        remembered as its kind's last."""
        queue = self.services.queue
        item = next((i for i in (queue.get(x) for x in ids) if i is not None), None)
        choice = Choice(preset=preset, options=preset.options, chosen=True)
        if item is None:
            self._new_choice = choice
            self._remember("movies", preset.name)
        else:
            self._assign(ids, choice)
            self._remember(item_kind(item), preset.name)
        self._card_key = None
        self._show_card()
        self._schedule_refresh(0)

    def _remember(self, kind: Kind, name: str) -> None:
        settings = self.services.settings.current
        key = "last_show_preset" if kind == "series" else "last_film_preset"
        if getattr(settings, key) != name or settings.last_preset != name:
            self.spawn(self.services.settings.update(**{key: name, "last_preset": name}))

    def _preset_chosen(self, name: str) -> None:
        preset = next((p for p in self.presets_for(self.kind) if p.name == name), None)
        if preset is not None:
            self.set_band_preset(self.selected_ids, preset)
            self._spread_bar()

    def _spread_bar(self) -> None:
        """A change made in the preset bar goes for every band not done of the bar's kind (the
        owner's decision, 2026-11-11: unticking Trailer for one show of a whole Series folder
        left 25 others fetching theirs). Each band gets a copy of its own; a band's chip still
        changes that band alone (`set_band_preset`)."""
        ids = self.selected_ids
        if not ids:
            return
        choice, kind = self.choice, self.kind
        if not choice.fits(kind):
            return
        choice.chosen = True
        for (band_kind, _title, _year), group in self._band_groups().items():
            if band_kind != kind or ids[0] in group:
                continue
            copy = dataclasses.replace(choice)
            for item_id in group:
                self._choices[item_id] = copy
        self._schedule_refresh(0)

    def _kind_bands(self, kind: Kind) -> int:
        """How many bands not done of `kind` the queue holds."""
        return sum(1 for band in self._band_groups() if band[0] == kind)

    # --- the band's chip and "Use for all" ---------------------------------------------------

    def chip_text(self, item: QueueItem) -> str:
        """A band's preset chip: its preset's name, or "✎ Custom"."""
        choice = self.choice_for(item)
        return choice.label if choice.custom is not None else f"{choice.label} ▾"

    def chip_choices(self, ids: Sequence[int]) -> list[tuple[str, Callable[[], None]]]:
        """The chip's menu: the presets that fit the band's kind."""
        queue = self.services.queue
        item = next((i for i in (queue.get(x) for x in ids) if i is not None), None)
        if item is None:
            return []
        return [
            (preset.name, functools.partial(self._chip_chosen, list(ids), preset))
            for preset in self.presets_for(item_kind(item))
        ]

    def _chip_chosen(self, ids: list[int], preset: Preset) -> None:
        self.set_band_preset(ids, preset)
        if ids and self.services.queue.get(ids[0]) is not None:
            self.list.select(ids[0])

    def _chip_menu(self, key: int, ids: Sequence[int]) -> None:
        box = self.list.chip_box(key)
        canvas = self.list.canvas
        if box is not None:
            x0, _y0, _x1, y1 = box
            point = (
                round(canvas.winfo_rootx() + x0 - canvas.canvasx(0)),
                round(canvas.winfo_rooty() + y1 - canvas.canvasy(0) + 4),
            )
        else:
            point = self.winfo_pointerxy()
        popup_menu(self, self.chip_choices(ids), point=point, look=self.look)

    def use_for_all_entries(self) -> list[MenuEntry]:
        """"Use for all ▾": all films, all shows, everything; each only where the selected
        band's choice fits ("doesn't fit" greyed); everything only when it fits both kinds."""
        groups = self._band_groups()
        counts = {kind: sum(1 for k in groups if k[0] == kind) for kind in _KINDS}
        choice = self.choice
        entries: list[MenuEntry] = []
        entry_kinds: tuple[tuple[Kind, str], ...] = (("movies", ALL_FILMS), ("series", ALL_SHOWS))
        for kind, label in entry_kinds:
            if not counts[kind]:
                continue
            text = t(label, n=counts[kind])
            if choice.fits(kind):
                entries.append(MenuEntry(text, functools.partial(self.use_for_all, (kind,))))
            else:
                entries.append(MenuEntry(text + t(NO_FIT), None))
        if all(choice.fits(kind) for kind in _KINDS):
            entries.append(
                MenuEntry(t(EVERYTHING, n=len(groups)), functools.partial(self.use_for_all, _KINDS))
            )
        return entries

    def use_for_all_menu(self) -> None:
        popup_menu(self.use_all_link, self.use_for_all_entries(), look=self.look)

    def use_for_all(self, kinds: Sequence[Kind]) -> None:
        """Copy the selected band's choice (preset, options, Custom) to every band not done of
        these kinds; the Activity panel says so."""
        choice = self.choice
        ids = self.selected_ids
        if not ids:
            return
        count = 0
        for (kind, _title, _year), group in self._band_groups().items():
            if kind not in kinds or not choice.fits(kind):
                continue
            count += 1
            if ids[0] in group:
                continue
            copy = dataclasses.replace(choice, chosen=True)
            self._assign(group, copy)
        choice.chosen = True
        if choice.preset is not None and choice.custom is None:
            for kind in kinds:
                if choice.fits(kind):
                    self._remember(kind, choice.preset.name)
        name = choice.label
        if len(kinds) > 1:
            line = tn(
                "Used {preset} for {n} item", "Used {preset} for {n} items", count, preset=name
            )
        elif kinds[0] == "movies":
            line = tn(
                "Used {preset} for {n} film", "Used {preset} for {n} films", count, preset=name
            )
        else:
            line = tn(
                "Used {preset} for {n} show", "Used {preset} for {n} shows", count, preset=name
            )
        log.info("Used one naming for %d band(s)", count)
        self._say(line)
        self._schedule_refresh(0)

    def _show_for_line(self) -> None:
        """"Only {name}…" or "For all 26 shows…", and "Use for all ▾" while shows and films are
        both queued."""
        ids = self.selected_ids
        queue = self.services.queue
        items = [i for i in (queue.get(x) for x in ids) if i is not None]
        if not items:
            full = t(FOR_NEW)
        elif (bands := self._kind_bands(self.kind)) > 1:  # the bar changes them all
            full = t(FOR_ALL_SHOWS if self.kind == "series" else FOR_ALL_FILMS, n=bands)
        else:
            full = t(FOR_LINE, name=band_name(items))
        if full != self._for_full:
            self._for_full = full
            scale = float(self._get_widget_scaling())
            if self._for_font is None or self._for_font[0] != scale:
                spec = theme.font(13).create_scaled_tuple(scale)
                self._for_font = (scale, tkfont.Font(root=self, font=spec))
            fitted = fit_text(full, self._for_font[1], round(_FOR_MAX * scale)) or full
            _set_text(self.for_label, fitted)
        # "Use for all ▾" is for the queue's other kind ("Everything"): the bar's own kind
        # follows every change already.
        several = bool(items) and len({band[0] for band in self._band_groups()}) > 1
        if several and not self.use_all_link.winfo_manager():
            self.use_all_link.pack(side="left", padx=(14, 0))
        elif not several and self.use_all_link.winfo_manager():
            self.use_all_link.pack_forget()

    def _card_state(self) -> tuple[object, ...]:
        return (tuple(self.selected_ids[:1]), id(self.choice), self.kind)

    def _mode_clicked(self, value: str) -> None:
        self.set_manual(value == _switch_label(MANUAL_MODE))

    def set_manual(self, value: bool) -> None:
        """The Auto | Manual switch: whether the preset bar shows. Remembered. What was chosen
        for a band stays either way: its chip shows it."""
        if value == self.manual:
            return
        self.manual = value
        self.mode_switch.set(_switch_label(MANUAL_MODE if value else AUTO_MODE))
        if value:
            self.card.grid()
        else:
            self.card.grid_remove()
        self._keep_a_selection()
        self.spawn(self.services.settings.update(presets_manual=value))

    def _show_card(self) -> None:
        """The card for the selected band: its line, the menu, the rule, the options."""
        choice, kind = self.choice, self.kind
        self._card_key = self._card_state()
        self._show_for_line()
        names = [p.name for p in self.presets_for(kind)] or ["…"]
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
        if (int(self.rule_box.cget("border_width")) > 0) != custom:  # typed in: an accent edge
            self.rule_box.configure(border_width=1 if custom else 0, border_color=look.ACCENT)
        for name, button in self.option_buttons.items():
            key = getattr(self._option_specs[name], kind)
            if key == QUALITY_OPTION:  # also when the rule itself has the code
                button.set(has_quality(choice.rule))
            else:
                button.set(bool(key and getattr(choice.options, key)))
        self._show_move_button()
        self._update_example()
        self._schedule_flow()

    def _show_move_button(self) -> None:
        """A download MediaForge found always moves: on, and no click changes that."""
        queue = self.services.queue
        items = (queue.get(item_id) for item_id in self.selected_ids)
        always = any(item is not None and item.watched is not None for item in items)
        button = self.option_buttons[MOVE_BUTTON]
        button.set_locked(always)
        if always:
            button.set(True)
        self._option_tips[MOVE_BUTTON].set_text(t(MOVE_ALWAYS_TIP if always else MOVE_TIP))

    def _option_clicked(self, name: str, value: bool) -> None:
        key = getattr(self._option_specs[name], self.kind)
        if key is None:
            return
        self._set_option(self.choice, key, value)
        self._spread_bar()
        self._schedule_flow()

    def _set_option(self, choice: Choice, key: str, value: bool) -> None:
        choice.options = choice.options.model_copy(update={key: value})
        choice.chosen = True
        if key == QUALITY_OPTION:  # it changes the rule, so the names
            if not value and has_quality(choice.rule):
                choice.custom = without_quality(choice.rule)
            self._card_key = None
            self._show_card()
            self._schedule_refresh(0)

    def option_keys(self) -> dict[str, str]:
        """The option buttons the card shows for its kind: name → the preset option."""
        keys = {name: getattr(spec, self.kind) for name, spec in self._option_specs.items()}
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
        divider never starts or ends a line); "Changed for this item" at the end while the
        options or the rule differ from the preset."""
        self._flow_id = None
        if not self.winfo_exists():
            return
        season = self.option_buttons["Season posters"]
        if self.kind == "series":
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
        """Show `rule` in its box, its codes coloured; typed in only in Custom mode."""
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
        choice.chosen = True
        self._spread_bar()
        self._tag_codes()
        self._update_example()
        self._schedule_flow()
        self._schedule_refresh(_REFRESH_FORMAT_MS)

    def set_custom(self, rule: str | None) -> None:
        """Custom mode for the band the card shows, with `rule` (None: back to the preset's)."""
        self.choice.custom = rule
        self.choice.chosen = True
        self._spread_bar()
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
        choice, ids = self.choice, self.selected_ids
        preset = choice.preset
        if preset is None:
            return
        rule = choice.rule  # the option stays the option: its code doesn't go in the format too
        fmt = without_quality(rule) if choice.options.quality_in_name else rule
        saved = preset.model_copy(update={"format": fmt, "options": choice.options})
        await self.services.presets.save(saved)
        log.info("Saved an item's changes to a preset")
        self._say(t("Saved to the preset {name}", name=preset.name), success=True)
        fresh = Choice(preset=saved, options=saved.options, chosen=True)
        if ids:
            self._assign(ids, fresh)
            self._spread_bar()
        else:
            self._new_choice = fresh
        await self._load_presets()

    def _save_as_clicked(self) -> None:
        self.spawn(self.save_as_preset())

    async def save_as_preset(self) -> None:
        """"Save as preset…": the card's rule and options as a new preset for its band's kind."""
        kind, choice, ids = self.kind, self.choice, self.selected_ids
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
        fresh = Choice(preset=preset, options=preset.options, chosen=True)
        if ids:
            self._assign(ids, fresh)
            self._spread_bar()
        else:
            self._new_choice = fresh
        self._remember(kind, name)
        await self._load_presets()

    # --- the folder and library menus -------------------------------------------------------

    def _folder_menu(self) -> None:
        scan = self.services.settings.current.scan_subfolders
        box = "☑" if scan else "☐"
        items: list[tuple[str, Callable[[], None]]] = [
            (t("Choose a folder…"), self._browse_folder),
            (f"{box}  {t('Scan subfolders')}", lambda: self.set_scan_subfolders(not scan)),
        ]
        popup_menu(self.folder_button, items, look=self.look)

    def _library_menu(self) -> None:
        """"Library ▾": Fix my library (with library folders chosen) and Find duplicates…"""
        items: list[tuple[str, Callable[[], None]]] = []
        if self.services.library_folders():
            items.append((t(FIX_LIBRARY_TEXT), self.fix_library))
        items.append((t(FIND_DUPLICATES_TEXT), self.find_duplicates))
        popup_menu(self.library_button, items, look=self.look)

    def fix_library(self) -> None:
        """"🧰 Fix my library": the Series and Movies folders into the queue, every subfolder
        read. Files already named right stay as they are and get what their preset adds
        (posters, trailers, subtitles); the others are renamed in place."""
        folders = self.services.library_folders()
        if folders:
            self.spawn(self.add(folders, recursive=True))

    def find_duplicates(self) -> None:
        """"🔁 Find duplicates…": the library folders read, then their duplicates in a window."""
        self.spawn(self._show_duplicates())

    async def _show_duplicates(self) -> None:
        services = self.services
        folders = services.library_folders()
        if folders:
            self._say(t(LOOKING_TEXT))
        found = await services.find_duplicates() if folders else []
        if not self.winfo_exists():
            return
        window = self.duplicates_window
        if window is not None and window.winfo_exists():
            window.close()
        self.duplicates_window = DuplicatesWindow(
            self.winfo_toplevel(),
            found,
            on_move=services.move_duplicates,
            has_folders=bool(folders),
            needs_place=services.duplicates_need_a_place(),
            place_problem=services.duplicates_place_problem,
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
        for job in (self._refresh_id, self._show_id, self._flow_id, self._settling_id):
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass  # the window is going
        self._refresh_id = self._show_id = self._flow_id = self._settling_id = None

    # --- downloads on their way (the empty queue says when the first one is ready) ----------

    def _settling_text(self) -> str | None:
        """What the empty queue says of downloads on their way (None: a queue with files)."""
        if self.services.queue.items:
            return None
        return settling_message(self.services.auto.settling, time.time())

    def _watch_status(self, _status: object) -> None:
        """A look ended: the empty queue's settling line again, if it changed. Read when Tk is
        idle next, once the renamer has planned its next look (the line counts to that one)."""
        if self.winfo_exists():
            self.after_idle(self._settling_check)

    def _settling_check(self) -> None:
        if self.winfo_exists() and self._settling_text() != self._settling_shown:
            self.refresh()

    def _settling_tick(self) -> None:
        """The settling line counts down while it shows."""
        self._settling_id = None
        if self._settling_shown is None or not self.winfo_exists():
            return
        if self._settling_text() != self._settling_shown:
            self.refresh()
        else:
            self._settling_id = self.after(SETTLING_TICK_MS, self._settling_tick)

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
        rules = self.services.rules.current
        self._sync_choices()

        def naming(item: QueueItem) -> WatchedLook:
            # Where its rule sends it, named as its band chose (the rule's preset by default).
            fmt = self.rule_for(item)
            route = route_for_item(item, rules, settings, presets)
            if route is None:  # no rule takes it: it says so (Forge holds it with that reason)
                why: Waits = (
                    "no_rule" if in_watched_folder(item.path, rules, settings) else "unwatched"
                )
                return WatchedLook(fmt, item.watched, False, "", why)
            name = name_of(route.rule)
            return WatchedLook(fmt, route.root, route.moves, name, _waits(item, route))

        return build_queue_rows(
            self.services.queue.items,
            self.rule_for,
            naming=naming,
            moving=self._move_route,
            show=self.show,
            today=date.today(),
            chip=self.chip_text,
            cc=self._cc_look,
            facts=self._facts_for,
        )

    def _facts_for(self, item: QueueItem) -> ReplaceFacts | None:
        """What a band already in the library says of the two copies, once read."""
        known = self._facts.get(item.id)
        return known[1] if known is not None else None

    def _ask_facts(self, built: QueueRows) -> None:
        """Read the copies' facts of the downloads already in the library not read yet (or whose
        name in the library changed with their band's preset); a refresh shows them."""
        # The phone names a download in the library as its band does (one pass, few items).
        self.services.band_naming = self._library_naming(
            [item for item in self.services.queue.items if held_in_library(item)]
        )
        if self._facts:  # the ones gone from the queue (one pass, not a lookup per id)
            present = {item.id for item in self.services.queue.items}
            for gone in [item_id for item_id in self._facts if item_id not in present]:
                del self._facts[gone]
        for item, target in built.held.values():
            known = self._facts.get(item.id)
            if known is None or not _same(known[0], target):
                self._facts[item.id] = (target, None)
                self.spawn(self._read_facts(item, target))

    async def _read_facts(self, item: QueueItem, target: Path) -> None:
        facts = await self.read_facts(item, target)
        known = self._facts.get(item.id)
        if known is None or known[0] != target or not self.winfo_exists():
            return
        self._facts[item.id] = (target, facts)
        self._schedule_refresh(0)

    def _show(self, built: QueueRows, started: float) -> None:
        """Show built rows; `started` is when the work began (perf_counter), for the pace of
        refreshes. Widgets are touched only where something changed: CTk redraws a widget on
        every configure."""
        items = self.services.queue.items
        self._targets, self._bands = built.targets, built.bands
        self._keeps, self._guesses = built.keeps, built.guesses
        self.list.set_rows(built.rows)
        self._ask_facts(built)
        if built.rows:
            self._settling_shown = None  # files there: the settling line isn't shown
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
            settling = self._settling_text()
            if settling is not None:
                hint = settling  # downloads on their way: when the first one comes in
                if self._settling_id is None:
                    self._settling_id = self.after(SETTLING_TICK_MS, self._settling_tick)
            self._settling_shown = settling
            _set_text(self.empty_title, title)
            _set_text(self.empty_hint, hint)
            if not self.empty_state.winfo_manager():
                self.empty_state.pack(fill="both", expand=True)
        self._paint_filters(built.counts)
        self._keep_a_selection()
        if not self.running:
            _set_text(self.forge_button, forge_label(len(self._pending(None))))
        self._update_example()
        told = (self.on_counts, built.waiting)
        if told != self._told:  # a new listener (the page, wired after building) hears it
            self._told = told
            self.on_counts(built.waiting)
        self._refreshed_at = time.monotonic()
        self._refresh_cost = time.perf_counter() - started

    def _keep_a_selection(self) -> None:
        """While the preset bar shows one band is selected (the first when none is), and the bar
        shows the selected band; only what changed is configured."""
        lines = self.list.item_keys()
        if self.list.selected is None and lines and self.manual:
            self.list.select(lines[0], notify=False)
        if self._card_state() != self._card_key:
            self._show_card()
        else:
            self._show_for_line()

    # --- the example name ------------------------------------------------------------------

    def _example_item(self) -> QueueItem | None:
        """The file the example name shows: the selected one (the card's band's)."""
        queue = self.services.queue
        ids = self.selected_ids
        return queue.get(ids[0]) if ids else None

    def _update_example(self) -> None:
        item = self._example_item()
        if item is None:
            text, colour = t(NO_EXAMPLE), theme.TEXT_MUTED
        else:
            rendered = name_file(self.fmt, item.media, item.path.suffix)
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

    def _images_for(self, row: Row) -> Any:
        """A band's poster when loaded; otherwise start loading it (again, if the cache let it go;
        never again once TMDB had none). It is asked in CTk units. A done band gets its poster
        faded. The backdrop is behind the whole card (`_card_picture`)."""
        scale = self.list.scale or 1.0
        pw, ph = (max(1, round(px / scale)) for px in self.list.poster_size)
        images = self.images
        faded = faded_fill(self.look) if row.faded else None
        poster = images.cached_poster(row.art, pw, ph, faded=faded) if row.art else None
        wanted: list[tuple[Key, Callable[[], Awaitable[Any]]]] = []
        if row.art and poster is None:
            key = images.poster_key(row.art, pw, ph, faded=faded)
            if self._should_load(key):
                load = functools.partial(images.poster, row.art, pw, ph, faded=faded)
                wanted.append((key, load))
        if wanted:
            self._loading.update(key for key, _ in wanted)
            self.spawn(self._load_images(wanted))
        return poster

    def _card_picture(self, row: Row, shape: CardShape, fill: str) -> Any:
        """A card's picture when baked; otherwise it goes on the list of those to bake (again, if
        the cache let it go; never again once TMDB had none)."""
        path, images = row.backdrop, self.images
        if not path:
            return None
        picture = images.cached_card(path, shape, fill, done=row.faded)
        if picture is None:
            key = images.card_key(path, shape, fill, done=row.faded)
            if self._should_load(key):
                self._loading.add(key)
                bake = functools.partial(images.card, path, shape, fill, done=row.faded)
                self._bakes[key] = (path, bake)
                while self._bakers < _CARD_BAKERS and self._bakers < len(self._bakes):
                    self._bakers += 1
                    self.spawn(self._bake_cards())
        return picture

    async def _bake_cards(self) -> None:
        """Bake what's waiting, the latest asked first; a card scrolled out of sight meanwhile is
        left out (it is asked again when it shows), so a fast scroll past hundreds of cards doesn't
        bake them all."""
        try:
            while self._bakes:
                key = next(reversed(self._bakes))
                path, bake = self._bakes.pop(key)
                try:
                    if not any(r.backdrop == path for r in self.list.cards_in_view()):
                        continue
                    picture = await bake()
                finally:
                    self._loading.discard(key)
                if picture is not None and self.winfo_exists():
                    self.list.refresh_images()
        finally:
            self._bakers -= 1

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
        if action == FORGE_ACTION:
            self.spawn(self.forge(only=ids))
        elif action == ACCEPT_ACTION:
            self.spawn(self.accept_band(ids, self._guesses.get(key)))
        elif action == "undo":
            self.spawn(self.undo_band(ids))
        elif action == REPLACE_ACTION:
            self.spawn(self.replace_band(ids))
        elif action == CC_ACTION:
            self.cc_menu(ids, key)
        elif action == IGNORE_ACTION:  # "Keep the one I have ▾": only the files in the library
            self.ignore_menu(self._keeps.get(key, ids), key)
        elif action == "preset":
            self._chip_menu(key, ids)
        elif action == EXPLORER_ACTION:
            self.spawn(self.open_in_explorer(ids))
        elif action == FOLDERS_ACTION:
            self.on_choose_folders()
        elif action == RULE_ACTION:
            self.open_rule(ids)
        elif action == CHOOSE_ACTION:
            self.spawn(self._pick_band_match(ids))

    def open_rule(self, ids: Sequence[int]) -> None:
        """A band's rule label: Settings › Auto-rename with that rule's editor open (the page's
        `on_settings`, at the rule's spot); only the section when the rule was deleted since."""
        from app.forge.ui.settings_pages import RULE_SPOT, SECTION_LIBRARY

        queue = self.services.queue
        item = next((i for i in (queue.get(n) for n in ids) if i is not None), None)
        route = self.route_of(item) if item is not None else None
        if route is None:
            return
        if route.number and route.rule.id:
            self.on_settings(SECTION_LIBRARY, RULE_SPOT.format(id=route.rule.id))
        else:
            self.on_settings(SECTION_LIBRARY)
        log.info("Opened a band's rule in Settings (rule %d)", route.rule.id)

    async def open_in_explorer(self, ids: Sequence[int]) -> None:
        """A band's 📂: where its files are now (`item.path` follows a rename). The check goes
        through a thread: a NAS can hang."""
        queue = self.services.queue
        paths = [item.path for item in (queue.get(item_id) for item_id in ids) if item is not None]
        if not paths:
            return
        target, select = explorer_target(paths)
        there = await asyncio.to_thread(fsops.exists, target)
        if not self.winfo_exists():
            return
        if there:
            open_explorer(target, select=select)
            log.info("Opened Explorer for a band of %d file(s)", len(paths))
        else:
            self._say(t(FILE_GONE), level=logging.WARNING)

    # --- subtitles by hand (CC ▾) --------------------------------------------------------------

    def _cc_look(self, group: Sequence[QueueItem]) -> CcLook | None:
        """How a done band's CC ▾ shows (`build_queue_rows`)."""
        return self.subtitle_desk.look(band_identity(group[0]))

    def cc_items(self, ids: Sequence[int]) -> list[tuple[str, Callable[[], None]]]:
        """The CC menu of a done band's files."""
        queue = self.services.queue
        items = [item for item in (queue.get(item_id) for item_id in ids) if item is not None]
        if not items:
            return []

        async def targets() -> list[VideoTarget]:
            return [
                VideoTarget(item.path, identity_of(item.media), item.run_id)
                for item in items
                if queue.get(item.id) is item
            ]

        return self.subtitle_desk.menu(band_identity(items[0]), targets)

    def cc_menu(self, ids: Sequence[int], key: int) -> None:
        """A done band's CC ▾: its menu under the button."""
        items = self.cc_items(ids)
        box = self.list.action_box(key, CC_ACTION)
        if box is None:
            popup_menu(self, items, point=self.winfo_pointerxy(), look=self.look)
        else:
            popup_menu(self, items, under=self.list.screen_box(box), look=self.look, align="left")

    def _cc_changed(self) -> None:
        if self.winfo_exists():
            self._schedule_refresh(0)

    # --- ignoring ----------------------------------------------------------------------------

    def ignore_choices(self, ids: Sequence[int]) -> list[tuple[str, Callable[[], None]]]:
        """The Leave-it-alone menu of a band's files (its "Leave it alone ▾" or "Keep the one I
        have ▾"): this file (or these files) and, when they sit in one subfolder of a watched
        folder, that whole folder. Nothing while a run goes."""
        queue = self.services.queue
        items = [
            item
            for item in (queue.get(item_id) for item_id in ids)
            if item is not None and item.status not in FINISHED
        ]
        if not items or self.running:
            return []
        files = tn("Leave this file alone", "Leave these {n} files alone", len(items))
        choices: list[tuple[str, Callable[[], None]]] = [
            (files, lambda: self._spawn_ignore(self.ignore_files(items)))
        ]
        folder = self.ignore_folder_for(items)
        if folder is not None:
            choices.append(
                (t(IGNORE_FOLDER), lambda: self._spawn_ignore(self.ignore_folder(folder)))
            )
        return choices

    def ignore_menu(self, ids: Sequence[int], key: int | None = None) -> None:
        """Band `key`'s Leave-it-alone button: its menu under the button, left edges lined up so
        the band's chip stays in sight (at the pointer when the button isn't drawn)."""
        choices = self.ignore_choices(ids)
        box = self.list.action_box(key, "ignore") if key is not None else None
        if box is None:
            popup_menu(self, choices, point=self.winfo_pointerxy(), look=self.look)
        else:
            under = self.list.screen_box(box)
            popup_menu(self, choices, under=under, look=self.look, align="left")

    def _spawn_ignore(self, coro: Coroutine[Any, Any, None]) -> None:
        self.spawn(coro)

    def ignore_folder_for(self, items: Sequence[QueueItem]) -> Path | None:
        """The one folder right under a watched folder that holds all of `items`, or None (a
        file directly in the watched folder, a file from elsewhere, files from two folders).
        A file without its own watched folder is placed by the folders the rules watch."""
        roots = [Path(folder) for folder in self.services.rules.watched_folders()]
        found: Path | None = None
        for item in items:
            root = item.watched or next((r for r in roots if inside(item.path, r)), None)
            folder = top_folder(item.path, root) if root is not None else None
            if folder is None or (found is not None and not _same(found, folder)):
                return None
            found = found or folder
        return found

    async def ignore_files(self, items: Sequence[QueueItem]) -> None:
        """Auto-rename leaves these files alone; their rows leave the queue."""
        for item in items:
            await self.services.ignored.ignore(item.path, folder=False)
        self.services.queue.remove_many([item.id for item in items])

    async def ignore_folder(self, folder: Path) -> None:
        """Auto-rename leaves `folder` and all under it alone; every row under it leaves."""
        ignored = self.services.ignored
        await ignored.ignore(folder, folder=True)
        snapshot = ignored.snapshot()
        queue = self.services.queue
        queue.remove_many(
            [i.id for i in queue.items if i.status not in FINISHED and snapshot.ignored(i.path)]
        )

    def _on_select(self, key: int | None) -> None:
        self._show_card()

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
        """Run a keyboard shortcut unless the queue isn't on screen (another page of the main
        window, History, a hidden window) or the user is typing in a text field. True when it was
        taken."""
        if not self.winfo_viewable():
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

    def _clear_menu(self) -> None:
        """"Clear ▾": what's done, everything, and the Settings tick (`remove_after_forge`)."""
        remove = self.services.settings.current.remove_after_forge
        box = "☑" if remove else "☐"
        items: list[tuple[str, Callable[[], None]]] = [
            (t(CLEAR_DONE_TEXT), self.clear_done),
            (t(CLEAR_ALL_TEXT), self._clear),
            (f"{box}  {t(REMOVE_AFTER_TEXT)}", lambda: self.set_remove_after_forge(not remove)),
        ]
        popup_menu(self.clear_button, items, look=self.look)

    def clear_done(self) -> None:
        """"Clear what's done": the done bands go (History keeps them); what needs you and what
        is ready stay."""
        if not self.running:
            self.services.queue.forget_finished()

    def _clear(self) -> None:
        """"Clear everything"."""
        if not self.running:
            self.services.queue.clear()

    def set_remove_after_forge(self, value: bool) -> None:
        """Clear ▾'s tick: the same setting as Settings › Auto-rename's, saved at once."""
        self.spawn(self.services.settings.update(remove_after_forge=value))

    def forget_yesterdays(self) -> None:
        """Yesterday's done bands go (the page opening; a look does it too)."""
        midnight = datetime.combine(date.today(), dt_time.min)
        self.services.queue.purge_finished(midnight.timestamp())

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

    def add_paths(self, paths: Sequence[Path], *, preset: str | None = None) -> None:
        """Queue these files and folders (folders are scanned, subfolders per the setting):
        the + buttons, Explorer drops and the window's requests (`preset`: a right-click
        request's, for the files it adds)."""
        if paths:
            self.spawn(self.add(paths, preset=preset))

    async def add(
        self, paths: Sequence[Path], *, preset: str | None = None, recursive: bool | None = None
    ) -> int:
        """`add_paths`, awaited: how many files were new. `recursive`: read the subfolders
        (None: as the "Scan subfolders" setting says)."""
        if recursive is None:
            recursive = self.services.settings.current.scan_subfolders
        queue = self.services.queue
        before = {i.id for i in queue.items}
        added = await queue.add_paths(list(paths), recursive)
        if preset:
            new = [i.id for i in queue.items if i.id not in before]
            if not self.loaded:
                self._requests.append((preset, new))
            elif new and self.winfo_exists():
                self._apply_request(preset, new)
                self._card_key = None
                self._schedule_refresh(0)
        if added:
            log.info("Added %d file(s) to the queue", added)
            self._say(tn("Added {n} file to the queue", "Added {n} files to the queue", added))
        return added

    async def _pick_match(self, item: QueueItem) -> bool:
        choice = await MatchPicker.ask(self, item, self.services, look=self.look)
        if choice == "cancel":
            return False
        await self.services.queue.choose_match(item.id, choice)
        return True

    async def _pick_band_match(self, ids: Sequence[int]) -> None:
        """A band's "Wrong film?" / "No, pick the right film…" / "Pick the film…" (the match
        picker): one question, shown for a file TMDB isn't sure about if it has one, and the
        answer goes for all its files."""
        queue = self.services.queue
        items = [item for item in map(queue.get, ids) if item is not None]
        if not items:
            return
        first = next((i for i in items if i.status is ItemStatus.NEEDS_CHOICE), items[0])
        choice = await MatchPicker.ask(self, first, self.services, look=self.look)
        if choice != "cancel":
            await queue.choose_match_for([first.id, *(i.id for i in items)], choice)

    async def accept_band(self, ids: Sequence[int], asked: int | None) -> None:
        """A band's "Yes, forge it" to the guess its question named (`asked`, a TMDB id, from
        when the band was built): taken as the match picker takes a choice (remembered for the
        file's name, for all the band's files), then the band is forged. A guess that isn't among
        TMDB's results is taken as is ("Use top results"). If the band's guess changed since it
        was shown, nothing happens but a refresh and a word in the status line. While Preview,
        Forge or Undo goes (or a run), ignored like those."""
        if self.busy or self.running or asked is None:
            return
        queue = self.services.queue
        items = [item for item in map(queue.get, ids) if item is not None]
        first = next((i for i in items if is_unsure(i)), None)
        if first is None or first.media.tmdb_id != asked:
            log.info("A band's guess changed before its Yes; nothing forged")
            self.on_message(t(GUESS_CHANGED))
            self._schedule_refresh(0)
            return
        unsure = [i for i in items if is_unsure(i)]
        wanted = asked
        candidates = first.resolution.candidates if first.resolution is not None else ()
        guess = next((c for c in candidates if c.id == wanted), None)
        if guess is not None:
            await queue.choose_match_for([first.id, *(i.id for i in items)], guess)
        else:
            for item in unsure:
                if item.status is ItemStatus.NEEDS_CHOICE:
                    item.status = ItemStatus.READY
        if not self.winfo_exists():
            return
        log.info("Took the guess for a band of %d file(s)", len(items))
        await self.forge(only=ids)

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

    async def _trailers_allowed(self, items: Sequence[QueueItem]) -> bool:
        """Whether this run may fetch trailers: once their tools are there (asked first). True
        when no band of these files wants one."""
        trailers = self.services.trailers
        if not any(self.choice_for(i).options.trailer for i in items):
            return True
        without = False
        if trailers.installing:  # Forge's start is fetching them: no question, no waiting
            self._say(t(TOOLS_NOT_THERE), level=logging.INFO)
            return without
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
        return True

    def _set_running(self, running: bool, total: int = 0) -> None:
        self.running = running
        self._run_steps = RunSteps(total)
        self._waiting_for_lock = False
        if running:
            self._show_progress()
        else:
            self.services.run_progress.idle(PAGE)  # the dock on the other pages goes too
        if not self.winfo_exists():
            return
        state = "disabled" if running else "normal"
        for button in (self.files_button, self.folder_button):
            button.configure(state=state)
        self._show_actions(running=running)
        if not running:
            self.refresh()

    def _on_lock_wait(self, waiting: bool) -> None:
        """The run waits for another one (an automatic run, say) before it can start, or no
        longer does: the dock says so."""
        self._waiting_for_lock = waiting
        if self.running:
            self._show_progress()

    def _show_progress(self) -> None:
        """The dock's words and how far, here and (reported, with its Cancel) on every other
        page of the main window."""
        if self._waiting_for_lock:
            text, fraction = t(WAITING_FOR_AUTO), 0.0
        else:
            text, fraction = self._run_steps.now()
        self.services.run_progress.report(
            text, fraction, source=PAGE, cancel=self._cancel_clicked
        )
        if self.winfo_exists():
            self.progress.show(fraction, text)

    def _heard_progress(self, event: ProgressEvent) -> None:
        """A step of the run (dropped files or downloads): the bar and its words."""
        item = self.services.queue.get(event.item_id) if event.item_id is not None else None
        self._run_steps.heard(event, display_name(item) if item is not None else None)
        if self.running:
            self._show_progress()

    def _on_progress(self, event: ProgressEvent) -> None:
        self._heard_progress(event)
        if event.copying:  # only the dock: the list stays as it is
            return
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
        if self.winfo_exists() and not event.copying:
            self._schedule_refresh(_REFRESH_PROGRESS_MS)

    def request_close(self, close: Callable[[], None]) -> bool:
        """True if the window may close now; otherwise the run stops after its current file (a
        copy between shares stops at once, leaving the file where it was) and `close` is called
        once it has finished and been recorded."""
        if not self.running:
            return True
        self._close_after_run = close
        self._cancel.set()
        log.warning("Stopping after the current file (a copy between shares at once), then closing")
        self._say(t("Stopping after the current file, then closing…"), level=logging.WARNING)
        return False

    async def settled(self) -> None:
        """Wait for every lookup."""
        await self.services.queue.wait_idle()

    def _naming_groups(
        self, items: Sequence[QueueItem]
    ) -> list[tuple[Choice, list[QueueItem]]]:
        """These files by their band's choice (bands that share one are one part)."""
        groups: dict[int, tuple[Choice, list[QueueItem]]] = {}
        for item in items:
            choice = self.choice_for(item)
            groups.setdefault(id(choice), (choice, []))[1].append(item)
        return list(groups.values())

    @staticmethod
    def _run_options(choice: Choice, trailers: bool) -> PresetOptions:
        if trailers or not choice.options.trailer:
            return choice.options
        return choice.options.model_copy(update={"trailer": False})

    def _move_route(self, item: QueueItem) -> tuple[bool, Path | None]:
        """Whether a dropped file's band moves it to a library folder, and that folder."""
        if not self.choice_for(item).options.move_to_library:
            return False, None
        return True, library_folder(item.media.kind, self.services.settings.current)

    def _plan_parts(self, items: Sequence[QueueItem], trailers: bool = True) -> list[PlanPart]:
        """The dropped files, by band, each with its band's rule and options, and starting in
        its library folder when the band moves it."""
        inputs = {i.item_id: i for i in self.services.queue.plan_inputs([i.id for i in items])}
        settings = self.services.settings.current
        parts: list[PlanPart] = []
        for choice, group in self._naming_groups(items):
            mine, _left = route_inputs(
                [inputs[i.id] for i in group if i.id in inputs], choice.options, settings
            )
            if mine:
                parts.append((mine, choice.rule, self._run_options(choice, trailers)))
        return parts

    def _library_naming(self, items: Sequence[QueueItem]) -> dict[int, NamingChoice]:
        """How each watched download goes into its library: its band's preset and options (a
        trailer only when the tools are there: the renamer drops it otherwise)."""
        naming: dict[int, NamingChoice] = {}
        for item in items:
            choice = self.choice_for(item)
            if choice.preset is not None:
                naming[item.id] = NamingChoice(choice.preset, choice.rule, choice.options)
        return naming

    @_one_at_a_time
    async def preview(self) -> None:
        """Write what Forge would do to the Activity panel; nothing changes."""
        await self.settled()
        items = [i for i in self.pending_items() if i.watched is None]  # downloads: their bands
        if not items:
            return
        parts = self._plan_parts(items)
        plan = await asyncio.to_thread(build_mixed_plan, parts)
        log.info("Preview of %d file(s); nothing changed", len(plan.files))
        self._say(t("Preview (nothing has been changed):"), status=False)
        for line in describe(plan):
            self._say(line, status=False)
        self.on_message(t(PREVIEW_READY))

    def _pending(self, only: Sequence[int] | None) -> list[QueueItem]:
        """What Forge renames: a band's files, or (Forge for all) everything except downloads
        that wait for the owner (those go through their own band's Forge this / Try again / Yes,
        forge it only)."""
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
        """Rename every pending file, or only those in `only` (a band's Forge this)."""
        await self.settled()
        if not self._pending(only) or not await self._settle_uncertain(only):
            return
        # Asked for dropped files only; a download's trailer comes once Forge's start has
        # fetched the tools, and until then Activity says it was skipped.
        trailers = await self._trailers_allowed(
            [i for i in self._pending(only) if i.watched is None]
        )
        if not self.services.trailers.tools_present() and any(
            i.watched is not None and self.choice_for(i).options.trailer
            for i in self._pending(only)
        ):
            self.services.activity.say(t(TOOLS_NOT_THERE))
        items = self._pending(only)
        left = [
            i for i in items if i.watched is None and self._move_route(i) == (True, None)
        ]
        if left:  # left alone until the owner has chosen where they go
            count = len(left)
            self._say(
                tn(NO_LIBRARY_FOLDER, NO_LIBRARY_FOLDERS, count), level=logging.WARNING
            )
            log.info("%d file(s) not forged: no library folder chosen", count)
            items = [i for i in items if i not in left]
            if not items:
                return
        self._cancel = asyncio.Event()
        self._set_running(True, len(items))
        try:
            result = RunResult()
            watched = [i.id for i in items if i.watched is not None]
            if watched:  # into the library, with each band's preset
                result = await self.services.auto.rename(
                    watched,
                    source=RunSource.APP,
                    on_progress=self._on_watched_progress,
                    cancel=self._cancel,
                    naming=self._library_naming([i for i in items if i.watched is not None]),
                    on_wait=self._on_lock_wait,
                )
            dropped = [i for i in items if i.watched is None]
            if dropped:
                # Never alongside an automatic run; the dock says when it waits for one.
                async with taking(self.services.run_lock, self._on_lock_wait):
                    result = _merge(result, await self._forge_dropped(dropped, trailers))
        finally:
            self._run_ended()
        log.info(
            "Forged: %d renamed, %d already right, %d failed, %d poster(s), %d trailer(s)%s",
            result.renamed,
            result.skipped,
            result.failed,
            result.posters,
            result.trailers,
            " (cancelled)" if result.cancelled else "",
        )
        moved = sum(
            1
            for item in items
            if item.status is ItemStatus.DONE
            and (item.watched is not None or self._move_route(item)[0])
        )
        if result.failed:
            self._say(summary_text(result, moved), level=logging.WARNING)
        else:
            self._say(summary_text(result, moved), success=True)

    def _run_ended(self) -> None:
        """After Forge or Replace, whatever happened: done files leave the queue when Settings
        say so, the dock goes, and a close asked for meanwhile happens."""
        try:
            if self.services.settings.current.remove_after_forge:
                self.services.queue.forget_finished()
        finally:
            self._set_running(False)  # the dock on every page goes, whatever happened
        close, self._close_after_run = self._close_after_run, None
        if close is not None:
            if self.winfo_exists():
                self.after(10, close)
            else:
                close()

    def _preset_name(self, items: Sequence[QueueItem]) -> str | None:
        """The run's preset, as History shows it: each band's preset in it."""
        names: list[str] = []
        groups = self._naming_groups(items)
        groups.sort(key=lambda group: item_kind(group[1][0]) != "series")  # shows first
        for choice, _group in groups:
            if choice.preset is not None and choice.preset.name not in names:
                names.append(choice.preset.name)
        return " + ".join(names) or None

    async def _forge_dropped(self, items: Sequence[QueueItem], trailers: bool) -> RunResult:
        queue = self.services.queue
        inputs = queue.plan_inputs([i.id for i in items])
        plan = await asyncio.to_thread(build_mixed_plan, self._plan_parts(items, trailers))
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
            elif item.status is ItemStatus.WORKING:  # its copy between shares was stopped
                item.status, item.message = ItemStatus.READY, ""
        return result

    def _aside_place(self, roots: Sequence[Path]) -> tuple[bool, Path | None]:
        """Whether a replace may go on, and the place the copies go when the folder a band's
        names start in (its route's root: a library folder, a rule's own folder or its watched
        folder) is a share or drive root: the one chosen this session, else asked now
        (`ask_folder`) and kept once it is outside the library folders and those roots."""
        services = self.services
        if not needs_a_place(roots):
            return True, None
        known = services.aside_place
        if known is not None and place_problem(known, roots) is None:
            return True, known
        chosen = self.ask_folder()
        if not chosen or not self.winfo_exists():
            return False, None
        place = Path(chosen)
        problem = services.duplicates_place_problem(place) or place_problem(place, roots)
        if problem is not None:
            self._say(t(problem), level=logging.WARNING)
            return False, None
        services.aside_place = place
        return True, place

    @_one_at_a_time
    async def replace_band(self, ids: Sequence[int]) -> None:
        """A band's "Use this one instead", for its downloads held because the film or
        episode is there already: asked first ("… The one there now moves to Duplicates."), then
        the copy there moves aside and the download takes its place, in one run
        (`services.auto.replace`): its Undo (the card's or History's) puts both back."""
        queue = self.services.queue
        items = [
            i
            for i in (queue.get(n) for n in ids)
            if i is not None and i.watched is not None and held_in_library(i)
        ]
        routes = [self.route_of(i) for i in items]
        roots = [route.root for route in routes if route is not None and route.root is not None]
        if not items or len(roots) != len(items):
            return
        go, place = self._aside_place(roots)
        if not go:
            return
        # A band is one show or film: one folder its names start in. Its name only, never the
        # full path.
        folder = aside_folder(roots[0], place) or Path()
        where = folder.name or folder.anchor
        if len(items) == 1:
            first = items[0]
            media = first.media
            code = episode_code(first) if media.kind is MediaKind.EPISODE else ""
            name = f"{media.title} {code}" if code else media.ny
            question = t(REPLACE_QUESTION, name=name, folder=where)
        else:
            question = t(REPLACE_QUESTION_MORE, n=len(items), folder=where)
        replace = t(REPLACE_BUTTON)
        if await self.ask(t(REPLACE_LABEL), question, [replace, t("Cancel")]) != replace:
            return
        self._cancel = asyncio.Event()
        self._set_running(True, len(items))
        try:
            result = await self.services.auto.replace(
                [i.id for i in items],
                source=RunSource.APP,
                place=place,
                on_progress=self._on_watched_progress,
                cancel=self._cancel,
                naming=self._library_naming(items),
                on_wait=self._on_lock_wait,
            )
        finally:
            self._run_ended()
        log.info("Replaced %d copy(ies) in the library, %d failed", result.replaced, result.failed)
        moved = sum(1 for item in items if item.status is ItemStatus.DONE)
        level = logging.WARNING if result.failed else logging.INFO
        self._say(summary_text(result, moved), level=level, success=not result.failed)

    async def undo_band(self, ids: Sequence[int]) -> None:
        """A Done band's Undo: put those files back, with what their run made for them alone
        (the film's folder, poster, backdrop, NFO, trailer: `UndoService.undo_card`; all of a
        run's files: the whole run, as History's "Undo run"); they leave the queue."""
        queue, history = self.services.queue, self.services.history
        items = [i for i in (queue.get(n) for n in ids) if i is not None and i.run_id is not None]
        by_run: dict[int, list[int]] = {}  # run id → the card's renames in it
        put_back: list[Path] = []  # downloads that return to their folder
        async with self.services.run_lock:
            for item in items:
                for op in await history.operations(item.run_id or 0):
                    if op.kind is OpKind.RENAME and op.dst == str(item.path) and op.undoable:
                        by_run.setdefault(op.run_id, []).append(op.id)
                        if item.watched is not None and op.src:
                            put_back.append(Path(op.src))
            if not by_run:
                log.info("Nothing to undo for these files")
                self._say(t("Nothing to undo for these files."))
                return
            files = 0
            for run_id, renames in by_run.items():
                result = await self.services.undo.undo_card(run_id, renames)
                files += len(set(renames).intersection(result.undone_ids))
        if put_back:  # never moved on their own again; Rename moves them when wanted
            await self.services.auto.keep_back(put_back)
        for item in items:
            queue.remove(item.id)
        log.info("Put back %d file(s)", files)
        self._say(tn("Put back {n} file", "Put back {n} files", files), success=True)

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
                    "{n} downloaded poster, trailer or subtitle",
                    "{n} downloaded posters, trailers or subtitles",
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
        self._unrules()
        self._unwatch()
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
