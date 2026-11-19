"""MediaForge's parts of AirGrabber's one Settings window (app/ui/settings_dialog.py, its pages in
app/ui/settings_pages_registry.py; docs/settings/2026-11-19-settings-redesign.md §2).

`FORGE_PARTS` holds, per page with a MediaForge part, how it is built and the words Settings'
search finds it by before then. The window builds a page's part with its `build(slots, forge)`
when the page is first shown (Forge starts then); a part may put boxes on another page too (its
`owner` there: Accounts & keys' OpenSubtitles box is the Subtitles part), built with it whichever
of its pages is shown first. The Folders page is AirGrabber's (app/ui/folders_page.py): it marks
MediaForge's Series and Movies folders in its lists through `LibraryDestinations`.

A builder gets the places for its boxes (`slots`: per page, an app/ui/settings_layout.py
`PageSlot`, used like a `SettingsPage`: `box()` / `add()`), AirGrabber's ForgeRuntime and the
window's Committer (app/ui/settings_commit.py), and returns a ForgeSectionPart. Each change
stores itself through the Committer when it is made (spec §3): what the old Save's `problem()`
refused is refused then, in red in its box, and nothing is stored; a stored change shows the
window's Undo bar. A part waits for `forge.start()` (Forge may not have started yet) and says so
while Forge is NOT_READY or FAILED.

The Sorting part is everything about sorting on its own (the queue's status line opens its page),
in the rules for watched folders' look A (docs/forge/2026-11-18-rules-design.md,
mockups/forge-rules.html):

- the status box: the renamer's light and status, live (`AutoRenamer.watch_status`), and the
  "Watching" switch, the pause for all rules (`automatic`: off = "off"; on = "on", or the stored
  value when that isn't "off");
- the rules (`services.rules`): one row per rule in their order (⠿ drags it, 🔒 for AirGrabber's
  rule; its switch; the folder, the filter's chips, the preset, → where the files go, the
  Automatic / Manual pill and ✎, the rule's name as its tooltip: app/forge/ui/rule_rows.py's
  `row_look`), under each row its notes (`rules.rule_notes`: refusals in DANGER, warnings in
  WARNING, the rest muted; the unreachable folders are the renamer's last look's, refreshed with
  its status: Settings reads no disk), "＋ Add a rule", and under the list the files no rule fits
  (`AutoRenamer.unfit`, "Show them" lists their names). ✎ and "＋ Add a rule" open the rule
  editor (app/forge/ui/rule_editor.py, imported when it opens: it loads the renamer), only once
  the presets are read (a read under way is waited for, a failed one tried again; still failing:
  the editor stays shut and the box says so), its live check reading the folder without what the
  owner ignored as stored (a "Stop ignoring" still in the form isn't counted yet); AirGrabber's
  rule and a new one have no Delete. `spots` holds each stored rule's row as `rule:<id>`: Settings scrolls there and calls
  `spot_shown`, which opens its editor (the queue's rule label);
- what is ignored (the queue's Ignore: each entry's name, its path as a tooltip, with "Stop
  ignoring", at once; app/forge/core/ignored.py's IgnoreService, `services.ignored`);
- AirGrabber's folders and why each is (not) watched by its rule (B-01,
  app/forge/core/airgrabber_folders.py), a suggestion's "Add a rule for this folder" (a rule with
  AirGrabber's rule's routing, last), and what to choose;
- (the Series and Movies library folders, which rules moving to "your Series / Movies folder"
  use, are marked in the Folders page's lists: `LibraryDestinations`; the presets a new rule
  starts with are Naming's, the Rules box says so);
- When sorting: cleaning up the download folder, the "needs you" notification, the evening
  summary and whether forged files leave the queue (`remove_after_forge`), each a switch.

What points elsewhere is a link to the page that fixes it (spec §6, `PageLink`): a rule's
"⚠ Choose your Series folder under Folders." and what AirGrabber's downloads ask to fill in lead
to Folders, "…under Naming › For new rules" to Naming. `overview_facts` is what Settings'
Overview shows of MediaForge while it runs (it never starts it, nor reads anything).

Each change stores itself (`_change`, one at a time): a rule's switch, a drag, the editor's Save
or Delete, "Add a rule for this folder", the Watch folders switch, a When sorting tick. A change that leaves a rule with a refusal (`rules.refusal`) is refused (the
editor's Save keeps the editor open and says why). The rules and the settings are stored as
today's Save did, in one step through `RuleService.save` (fresh flags worked out there and the
rules that will watch more marked first: nothing moves unseen from a new folder); Undo stores
the rules and settings from before the same way. A rules change stored elsewhere meanwhile (an
import) shows. While
the old MediaForge is installed the renamer's guard keeps it idle and the status says so. Its
foot (`LibrarySection.foot`, hidden while empty) holds "Bring over…" while the old MediaForge's
data can still be imported (app/forge/ui/import_step.py's BringOverBox, F-26) and, while the old
MediaForge is still installed, the guard's notice with "Remove the old MediaForge" (RemoveOldBox,
F-27).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import tkinter as tk
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, Protocol

import customtkinter as ctk

from app.forge.models.rules import Rule, RuleTarget
from app.forge.models.settings import ForgeSettings
from app.forge.ui.look import app_look, light_colour
from app.forge.ui import settings_menu as _menu
from app.forge.ui import settings_naming as _naming
from app.forge.ui import settings_subtitles as _subtitles
from app.forge.ui.rule_rows import row_look, unfit_line
from app.forge.ui.settings_menu import build_menu
from app.forge.ui.settings_naming import build_naming
from app.forge.ui.settings_subtitles import build_subtitles
from app.i18n import N_, t
from app.ui import dialog_kit, theme
from app.forge.models.media import MediaKind
from app.ui.settings_commit import Committer, Refused
from app.ui.settings_layout import BOX_INNER, PageLink
from app.ui.settings_overview import ForgeFacts
from app.ui.settings_pages_registry import (
    PAGE_ACCOUNTS,
    PAGE_FOLDERS,
    PAGE_NAMING,
    PAGE_SORTING,
    PAGE_SUBTITLES,
    PAGE_WINDOWS,
)
from app.ui.tasks import spawn
from app.ui.tooltip import add_tooltip

if TYPE_CHECKING:
    from app.forge.core.auto import Unfit, WatchStatus
    from app.forge.models.ignored import IgnoredPath
    from app.forge.models.media import ParsedMedia
    from app.forge.models.preset import Preset
    from app.forge.runtime import ForgeRuntime
    from app.ui.settings_layout import SettingsBox, SettingsPage

log = logging.getLogger(__name__)

# Light at import (Settings imports it when it opens; AirGrabber's start doesn't): nothing
# of Forge's beyond its models and the rules' words, so Forge's core, forge.db and guessit
# stay unloaded (INTEGRATION.md 3.6); the rules' checks are imported where they are used.

# -- the registry -------------------------------------------------------------------------------


class ForgeSectionPart(Protocol):
    """A built Forge part, as the Settings window sees it (its changes store themselves)."""

    @property
    def ready(self) -> bool:
        """Whether its boxes show (Forge has started)."""
        ...


# Per page (its key), the place on it for a part's boxes.
PartSlots = Mapping[str, "SettingsPage"]
ForgePartBuilder = Callable[[PartSlots, "ForgeRuntime", Committer], ForgeSectionPart]


class ForgePart(NamedTuple):
    """A page's MediaForge part: how it is built (None: by `owner`'s part, with it), and what
    Settings' search finds it by before then (N_() texts)."""

    build: ForgePartBuilder | None
    words: tuple[str, ...]
    owner: str | None = None  # the page whose part puts this page's boxes too


# -- texts ----------------------------------------------------------------------------------------

GETTING_READY = N_("Getting MediaForge ready…")
# A start that ended without Forge and without a reason (AirGrabber is closing): never "getting
# ready" for ever.
UNAVAILABLE = N_("MediaForge isn't available.")
SAVE_FAILED = N_("MediaForge's settings couldn't be saved. The log has the details.")
WATCHING = N_("Watch folders")
RULES_TITLE = N_("Rules")
RULES_HINT = N_("Checked from the top down. The first rule that fits a file takes it.")
ADD_RULE = N_("＋ Add a rule")
LOCKED_TIP = N_("Follows AirGrabber's settings. You can turn it off but not delete it.")
EDIT_TIP = N_("Edit")
PRESETS_UNREAD = N_("Couldn't read your presets. Try again in a moment.")
SHOW_THEM = N_("Show them")
UNFIT_TITLE = N_("Files no rule fits")
CLOSE = N_("Close")
RULE_REFUSED = N_("Rule {n}: {why}")  # a change refused: that rule's row's reason
RULE_SPOT_PREFIX = "rule:"
RULE_SPOT = RULE_SPOT_PREFIX + "{id}"  # a stored rule's row (`spots`; `spot_shown` opens its editor)
LIBRARY_POINTER = N_("New rules start with the presets chosen under Naming › For new rules.")
OPTIONS_TITLE = N_("When sorting")
CLEAN_TEXT = N_("Clean up downloads after moving (samples, .nfo, .txt, .url, empty folders)")
NEEDS_TEXT = N_("Tell me when files need me")
SUMMARY_TEXT = N_("A summary in the evening")
REMOVE_TEXT = N_("Take sorted files off the list (Undo stays in History)")
AIRGRABBER_TITLE = N_("AirGrabber's downloads")
AIRGRABBER_HINT = N_(
    "The folders where AirGrabber's downloads end up, as AirGrabber's settings say. AirGrabber's "
    "rule watches them; a suggested folder is only watched once you add a rule for it."
)
ADD_RULE_FOR = N_("Add a rule for this folder")
IGNORED_TITLE = N_("Ignored")
IGNORED_TEXT = N_("Files and folders Sorting leaves alone.")
NOTHING_IGNORED = N_("Nothing ignored.")
STOP_IGNORING = N_("Stop ignoring")
FOLDER_MARK = "📁"  # before an ignored folder
GRIP = "⠿"  # drags a rule up or down
LOCK = "🔒"  # AirGrabber's rule (dragged the same way)
EDIT_MARK = "✎"

# What this section edits of the settings, each stored as it changes. The rules are stored as
# a whole (`services.rules`); AirGrabber's rule's switch is `use_airgrabber_downloads`
# (RuleService does that). The library folders are the Folders page's (`LibraryDestinations`):
# the rules' notes and refusals read them as stored.
FIELDS: tuple[str, ...] = (
    "automatic",
    "clean_downloads",
    "notify_needs",
    "daily_summary",
    "remove_after_forge",
)

_BUTTON_WIDTH = 110  # the boxes' buttons
_ROW_GAP = 8
# The wraps follow the page's width (BOX_INNER: inside a box's border and padding).
_LIST_WRAP = BOX_INNER - 122  # a folder's path next to its button
_STATUS_WRAP = BOX_INNER - 142  # the renamer's status between its light and the switch
_STOP_WIDTH = 150  # "Stop ignoring"
_SUGGEST_WIDTH = 210  # "Add a rule for this folder"
_ADD_RULE_WIDTH = 140
_IGNORED_WRAP = BOX_INNER - 162  # an ignored name next to its Stop ignoring button
_ROUTE_WRAP = BOX_INNER - 102  # a rule's preset and destination under its folder
_NOTE_WRAP = BOX_INNER - 72  # a note under a rule's row
_TEXT_SIZE = dialog_kit.SMALL_SIZE + 1  # as the dialog's boxes' explanations
_CHIP_SIZE = dialog_kit.SMALL_SIZE
_NOTE_COLOURS: dict[str, theme.Color] = {
    "refuse": theme.DANGER,
    "warn": theme.WARNING,
    "info": theme.TEXT_MUTED,
}
_UNFIT_WIDTH = 460
_UNFIT_LIST_MAX = 360  # the list's height, at most; a longer one scrolls


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

    async def ignore(self, path: Path, *, folder: bool) -> IgnoredPath: ...

    async def stop_ignoring(self, path: Path) -> bool: ...


AskFolder = Callable[[str, str], str]  # (title, start folder) → the chosen folder, or ""


class SettingsSource(Protocol):
    """What a section needs of ForgeSettingsService."""

    @property
    def current(self) -> ForgeSettings: ...

    async def update(self, **changes: Any) -> ForgeSettings: ...

    def subscribe(self, listener: Callable[[ForgeSettings], None]) -> Callable[[], None]: ...


class StatusSource(Protocol):
    """What the section needs of the AutoRenamer: its status now and every later one, and what
    its last look found (files no rule fits, folders it couldn't reach)."""

    @property
    def last_status(self) -> WatchStatus | None: ...

    def status(self, unreachable: Sequence[Path]) -> WatchStatus: ...

    def watch_status(self, listener: Callable[[WatchStatus], None]) -> Callable[[], None]: ...

    def unfit(self) -> list[Unfit]: ...

    def unreachable_folders(self) -> list[str]: ...


class RulesSource(Protocol):
    """What the section needs of RuleService (app/forge/core/rules_service.py)."""

    @property
    def current(self) -> Sequence[Rule]: ...

    @property
    def airgrabber_rule(self) -> Rule: ...

    async def replace_all(self, rules: Sequence[Rule]) -> Sequence[Rule]: ...

    async def save(
        self, rules: Sequence[Rule] | None, changes: Mapping[str, Any]
    ) -> Sequence[Rule]: ...

    def subscribe(self, listener: Callable[[tuple[Rule, ...]], None]) -> Callable[[], None]: ...


@dataclass
class RuleRowWidgets:
    """One rule's row in the list."""

    frame: ctk.CTkFrame
    grip: ctk.CTkLabel  # ⠿, or 🔒 for AirGrabber's rule
    switch: ctk.CTkSwitch
    labels: list[ctk.CTkLabel]  # the folder, the chips, the preset, → where, the mode
    edit: ctk.CTkButton
    notes: list[ctk.CTkLabel]


def _same_folder(a: str, b: str) -> bool:
    return os.path.normcase(a.rstrip("\\/")) == os.path.normcase(b.rstrip("\\/"))


def _same_rules(a: Sequence[Rule], b: Sequence[Rule]) -> bool:
    """Whether `a` and `b` are the same rules in the same order, their fresh flags aside."""
    return len(a) == len(b) and all(
        x.model_copy(update={"fresh": False}) == y.model_copy(update={"fresh": False})
        for x, y in zip(a, b, strict=True)
    )


def ignored_text(entry: IgnoredPath) -> str:
    """An ignored entry as the list shows it: its name (the whole path is its tooltip), a folder
    with 📁 before it."""
    return f"{FOLDER_MARK}  {entry.name}" if entry.folder else entry.name


def rules_refusal(
    rules: Sequence[Rule], settings: ForgeSettings, *, presets: Sequence[str] = ()
) -> str | None:
    """Why these rules and settings can't be stored (`rules.refusal`): the first refused rule's
    number and its row's reason ("Rule 2: ✕ Can't save: …"), or None. `presets`: their names
    (only the warnings need them, never a refusal)."""
    from app.forge.core.rules import refusal, rule_notes  # not at import time: see the top

    found = refusal(rules, settings)
    if found is None:
        return None
    notes = rule_notes(rules, settings, presets=presets)
    for number, row in enumerate(notes, 1):
        why = next((note.text for note in row if note.level == "refuse"), None)
        if why is not None:
            return t(RULE_REFUSED, n=number, why=why)
    return found


def _folder_notes() -> frozenset[str]:
    """A rule's notes that a choice on the Folders page fixes (translated, as shown)."""
    from app.forge.core.rules import NO_MOVIES_FOLDER, NO_SERIES_FOLDER  # not at import time

    return frozenset({t(NO_SERIES_FOLDER), t(NO_MOVIES_FOLDER)})


# -- the Overview ---------------------------------------------------------------------------------


def overview_facts(forge: _Starter) -> ForgeFacts | None:
    """What Settings' Overview shows of MediaForge (app/ui/settings_overview.py), from what its
    services hold now; None while it isn't running (this never starts it, nor reads forge.db).
    A kind needs its library folder when a rule that is on sends it there (the owner's ruling,
    2026-11-19: presets don't count, so the card is the same whichever pages were visited)."""
    source_settings: SettingsSource | None = getattr(forge.services, "settings", None)
    if source_settings is None:
        return None
    services = forge.services
    settings = source_settings.current
    source: RulesSource | None = getattr(services, "rules", None)
    if source is not None:
        rules: Sequence[Rule] = list(source.current)
    else:
        from app.forge.core.rules import rules_from_settings  # not at import time

        rules = rules_from_settings(settings)
    needs = {MediaKind.EPISODE: False, MediaKind.MOVIE: False}
    for rule in rules:
        if rule.on and not rule.stays:
            for kind in rule.kinds():
                if rule.target(kind).to == "library":
                    needs[kind] = True
    return ForgeFacts(
        watching=settings.automatic != "off",
        rules_on=sum(1 for rule in rules if rule.on),
        series_folder=settings.series_folder,
        movies_folder=settings.movies_folder,
        needs_series=needs[MediaKind.EPISODE],
        needs_movies=needs[MediaKind.MOVIE],
    )


def _rule_editor(master: Any, **options: Any) -> Any:
    """The rule editor (imported here: it loads the renamer, which Settings' start doesn't)."""
    from app.forge.ui.rule_editor import RuleEditor

    return RuleEditor(master, **options)


# -- Sorting --------------------------------------------------------------------------------------


class LibrarySection:
    """The Sorting page's contents. Each change stores itself (`_change`); a change stored
    elsewhere meanwhile (a folder AirGrabber's settings brought, a library folder the Folders page
    marked, rules an import brought, an Undo) shows here."""

    def __init__(
        self,
        page: SettingsPage,
        forge: _Starter,
        *,
        ask_folder: AskFolder | None = None,
        committer: Committer | None = None,
    ) -> None:
        self._page = page
        self._forge = forge
        self._committer = committer if committer is not None else Committer()
        self._changing = asyncio.Lock()  # one change at a time: each starts from the last
        self._look = app_look()
        self.ask_folder: AskFolder = ask_folder if ask_folder is not None else self._ask_folder
        self.open_editor: Callable[..., Any] = _rule_editor  # the rule editor (tests: a recorder)
        self._editor: Any = None  # the open rule editor
        self._settings: SettingsSource | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._presets_task: asyncio.Task[None] | None = None
        self._baseline: dict[str, Any] = {}  # the settings as stored (as last heard)
        self._automatic = "off"
        self._watching_as = "on"  # what the switch turns watching back on to (the last value)
        self._renamer: StatusSource | None = None
        self._unwatch: Callable[[], None] | None = None
        self._rules: RulesSource | None = None
        self._unsubscribe_rules: Callable[[], None] | None = None
        self._unsubscribe_presets: Callable[[], None] | None = None
        self._presets: list[Preset] | None = None  # None until read
        self._opening: asyncio.Task[None] | None = None  # an editor waiting for the presets
        self.rule_drafts: list[Rule] = []  # the rules shown: as stored, or as being stored
        self._rules_baseline: tuple[Rule, ...] = ()  # the rules as stored (as last heard)
        self.rule_rows: list[RuleRowWidgets] = []
        self._drag: int | None = None  # the row being dragged
        self._refresh_after_drag = False
        self.unfit_window: UnfitWindow | None = None
        frame = page.frame

        # Shown until Forge has started (or why it can't).
        self.waiting = page.box()
        self.waiting_label = self.waiting.text(t(GETTING_READY))
        self.waiting.add(self.waiting_label, gap=0, sticky="w")

        # The light and the status, and the switch that pauses all watching.
        status = page.box(search_title=t(WATCHING))
        status_row = status.frame_row(gap=0)
        self.light = ctk.CTkLabel(
            status_row, text="●", font=theme.font(14), text_color=theme.TEXT_MUTED
        )
        self.light.pack(side="left", padx=(0, 6))
        self.watching = ctk.CTkSwitch(
            status_row,
            text=t(WATCHING),
            height=dialog_kit.BUTTON_HEIGHT,
            font=theme.font(dialog_kit.DETAIL_SIZE, bold=True),
            progress_color=self._look.ACCENT,
            text_color=theme.TEXT,
            command=self._watching_clicked,
        )
        self.watching.pack(side="right")
        self._status_box = status
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

        # The rules, in their order.
        rules = page.box(t(RULES_TITLE), t(RULES_HINT))
        self._rules_box = rules
        self.add_rule_button = self._button(rules.body, t(ADD_RULE), self.add_rule)
        self.add_rule_button.configure(width=_ADD_RULE_WIDTH)
        # Beside the box's name, at the right (the heading's own cell).
        self.add_rule_button.grid(row=0, column=0, sticky="e")
        # Why the editor didn't open (the presets couldn't be read); hidden otherwise.
        self.presets_problem = rules.text("")
        self.presets_problem.configure(text_color=theme.WARNING)
        self._presets_problem_row = rules.take_row()
        self.rules_list = ctk.CTkFrame(rules.body, fg_color="transparent")
        self.rules_list.grid_columnconfigure(0, weight=1)
        rules.add(self.rules_list, gap=_ROW_GAP)
        unfit_row = rules.frame_row(gap=_ROW_GAP)
        self._unfit_row = unfit_row
        self.unfit_label = ctk.CTkLabel(
            unfit_row,
            text="",
            font=theme.font(_TEXT_SIZE),
            text_color=theme.TEXT_MUTED,
            anchor="w",
            justify="left",
            wraplength=_LIST_WRAP,
        )
        self.unfit_label.pack(side="left")
        self.show_them = ctk.CTkButton(
            unfit_row,
            text=t(SHOW_THEM),
            width=0,
            height=dialog_kit.BUTTON_HEIGHT,
            fg_color="transparent",
            hover_color=self._look.HOVER,
            text_color=self._look.LINK,
            font=theme.font(_TEXT_SIZE, underline=True),
            command=self.show_unfit,
        )
        self.show_them.pack(side="left", padx=(6, 0))
        unfit_row.grid_remove()
        self.naming_link = PageLink(rules.body, t(LIBRARY_POINTER), PAGE_NAMING)
        rules.add(self.naming_link, gap=_ROW_GAP, sticky="w")

        # When sorting.
        options = page.box(t(OPTIONS_TITLE))
        self._options_box = options
        self._clean = tk.BooleanVar(master=frame, value=True)
        self._needs = tk.BooleanVar(master=frame, value=True)
        self._summary = tk.BooleanVar(master=frame, value=True)
        self._remove = tk.BooleanVar(master=frame, value=False)
        for text, variable, field in (
            (CLEAN_TEXT, self._clean, "clean_downloads"),
            (NEEDS_TEXT, self._needs, "notify_needs"),
            (SUMMARY_TEXT, self._summary, "daily_summary"),
            (REMOVE_TEXT, self._remove, "remove_after_forge"),
        ):
            switch = self._switch(
                options.body,
                t(text),
                variable,
                functools.partial(self._option_clicked, field, variable),
            )
            options.add(switch, gap=_ROW_GAP, sticky="w")

        # What Sorting leaves alone (the queue's Ignore); Stop ignoring stores at once.
        self._ignored: IgnoredSource | None = None
        self._ignored_entries: tuple[IgnoredPath, ...] = ()
        self._unignore: list[Path] = []  # being stopped (hidden until the service says so)
        self._unsubscribe_ignored: Callable[[], None] | None = None
        ignored = page.box(t(IGNORED_TITLE), t(IGNORED_TEXT))
        self._ignored_box = ignored
        # Never empty (an empty CTkFrame keeps its default 200 units): "Nothing ignored." then.
        self.ignored_list = ctk.CTkFrame(ignored.body, fg_color="transparent")
        self.ignored_list.grid_columnconfigure(0, weight=1)
        ignored.add(self.ignored_list)

        # AirGrabber's downloads (B-01): AirGrabber's folders and why, what to choose. Its
        # rule's switch in the list is "Use AirGrabber's downloads".
        airgrabber = page.box(t(AIRGRABBER_TITLE), t(AIRGRABBER_HINT))
        self._airgrabber_box = airgrabber
        self.airgrabber_list = ctk.CTkFrame(airgrabber.body, fg_color="transparent")
        self.airgrabber_list.grid_columnconfigure(0, weight=1)
        airgrabber.add(self.airgrabber_list, gap=_ROW_GAP)
        self.spots: dict[str, tk.Misc] = {}  # go_to_spot: the rules' rows (`rule:<id>`)

        self._content: list[SettingsBox] = [status, rules, options, ignored, airgrabber]

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

    def _button(self, master: Any, text: str, command: Callable[[], object]) -> ctk.CTkButton:
        def clicked() -> None:
            command()  # what it starts (a change's task) isn't the button's

        button = dialog_kit.secondary_button(
            master, text, clicked, width=_BUTTON_WIDTH, look=self._look
        )
        button.configure(text_color=theme.BUTTON_TEXT)
        return button

    def _switch(
        self,
        master: Any,
        text: str,
        variable: tk.BooleanVar,
        command: Callable[[], None] | None = None,
    ) -> ctk.CTkSwitch:
        """An on/off of When sorting (spec §6: every on/off in Settings is a switch)."""
        return ctk.CTkSwitch(
            master,
            text=text,
            variable=variable,
            command=command,
            height=dialog_kit.BUTTON_HEIGHT,
            font=theme.font(dialog_kit.DETAIL_SIZE),
            progress_color=self._look.ACCENT,
            text_color=theme.TEXT,
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
            self.waiting_label.configure(text=self._forge.reason or t(UNAVAILABLE))
            return
        self._ready(services.settings)

    def _ready(self, settings: SettingsSource) -> None:
        self._settings = settings
        services = self._forge.services
        source: IgnoredSource | None = getattr(services, "ignored", None)
        if source is not None:
            self._ignored = source
            self._ignored_entries = source.entries
            self._unsubscribe_ignored = source.subscribe(self._ignored_changed)
        rules: RulesSource | None = getattr(services, "rules", None)
        if rules is not None:
            self._rules = rules
            self._unsubscribe_rules = rules.subscribe(self._rules_saved_elsewhere)
        self._renamer = getattr(services, "auto", None)
        self._load(settings.current)
        self._unsubscribe = settings.subscribe(self._saved_elsewhere)
        if self._renamer is not None:
            self.show_status(self._renamer.last_status or self._renamer.status([]))
            self._unwatch = self._renamer.watch_status(self.show_status)
        presets = getattr(services, "presets", None)
        if presets is not None:
            self._presets_task = spawn(self._read_presets(), name="forge-settings-presets")
            subscribe_presets = getattr(presets, "subscribe", None)
            if subscribe_presets is not None:
                self._unsubscribe_presets = subscribe_presets(self._presets_changed)
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

    async def _read_presets(self) -> None:
        """The stored presets: the rules' notes (a preset gone) and the editor's menus. A read
        that fails leaves them unknown (None): the notes leave out "its preset is gone" and the
        editor reads them again before it opens."""
        source = getattr(self._forge.services, "presets", None)
        if source is None:
            return
        try:
            presets = list(await source.list())
        except Exception as exc:  # forge.db
            log.warning("The presets couldn't be read for the rules (%s)", type(exc).__name__)
            return
        if not self.waiting_label.winfo_exists():
            return
        self._presets = presets
        self._refresh_rules()

    def _presets_changed(self) -> None:
        if not self.waiting_label.winfo_exists():
            return
        if self._presets_task is not None:
            self._presets_task.cancel()  # the newest read wins
        self._presets_task = spawn(self._read_presets(), name="forge-settings-presets")

    async def _presets_read(self) -> list[Preset] | None:
        """The presets, once read: a read under way is waited for, and one that failed (or
        none yet) is tried again now. None when they still can't be read."""
        task = self._presets_task
        if task is not None and not task.done():
            await asyncio.gather(asyncio.shield(task), return_exceptions=True)
        if self._presets is None:
            await self._read_presets()
        return self._presets

    def _destroyed(self, event: tk.Event[Any]) -> None:
        if event.widget is not self._page.frame:
            return
        for task in (self._task, self._presets_task, self._opening):
            if task is not None:
                task.cancel()
        self._task = self._presets_task = self._opening = None
        for stop in (
            self._unsubscribe,
            self._unsubscribe_ignored,
            self._unwatch,
            self._unsubscribe_rules,
            self._unsubscribe_presets,
        ):
            if stop is not None:
                stop()
        self._unsubscribe = self._unsubscribe_ignored = self._unwatch = None
        self._unsubscribe_rules = self._unsubscribe_presets = None

    def show_status(self, status: WatchStatus) -> None:
        """The renamer's status, live: the light's colour and its words; the rules' notes and
        the files no rule fits follow its last look."""
        if self.status_label.winfo_exists():
            self.light.configure(text_color=light_colour(status.state))
            self.status_label.configure(text=status.text)
            if self._settings is not None:
                self._refresh_rules()

    # --- the form --------------------------------------------------------------------------------

    def _load(self, settings: ForgeSettings) -> None:
        """Show `settings` and the stored rules, and take them as what is stored."""
        self._baseline = {name: getattr(settings, name) for name in FIELDS}
        for name in FIELDS:
            self._show(name, self._baseline[name])
        self._rules_baseline = self._stored_rules()
        self.rule_drafts = list(self._rules_baseline)
        self._refresh()

    def _stored_rules(self) -> tuple[Rule, ...]:
        """The rules as stored (`services.rules`; today's setup as rules for a stand-in without
        them, AirGrabber's on as its setting says)."""
        if self._rules is not None:
            return tuple(self._rules.current)
        if self._settings is None:
            return ()
        from app.forge.core.rules import rules_from_settings  # not at import time

        current = self._settings.current
        return tuple(
            rule.model_copy(update={"on": current.airgrabber_on}) if rule.airgrabber else rule
            for rule in rules_from_settings(current)
        )

    def _show(self, name: str, value: Any) -> None:
        if name == "automatic":
            self._automatic = value
            if value != "off":
                self._watching_as = value
        elif name == "clean_downloads":
            self._clean.set(value)
        elif name == "notify_needs":
            self._needs.set(value)
        elif name == "daily_summary":
            self._summary.set(value)
        elif name == "remove_after_forge":
            self._remove.set(value)

    def draft(self) -> dict[str, Any]:
        """What the page shows of the settings, field by field."""
        return {
            "automatic": self._automatic,
            "clean_downloads": bool(self._clean.get()),
            "notify_needs": bool(self._needs.get()),
            "daily_summary": bool(self._summary.get()),
            "remove_after_forge": bool(self._remove.get()),
        }

    @property
    def rules_changed(self) -> bool:
        """Whether the rules shown aren't the stored ones (a change being stored). A fresh flag
        the renamer set or cleared meanwhile is no change (RuleService works those out)."""
        return not _same_rules(self.rule_drafts, self._rules_baseline)

    def _draft_settings(self) -> ForgeSettings:
        """The stored settings with what the page shows (a change being stored); the library
        folders as stored (the Folders page's)."""
        assert self._settings is not None
        return self._settings.current.model_copy(update=self.draft())

    def _saved_elsewhere(self, updated: ForgeSettings) -> None:
        """Settings stored (here, elsewhere, or by an Undo): each shows, unless a change of it
        is being stored. AirGrabber's rule's switch is a setting too: the list follows it, and
        the rules' notes follow the library folders."""
        if not self.waiting_label.winfo_exists():
            return
        draft = self.draft()
        for name in FIELDS:
            value = getattr(updated, name)
            if draft[name] == self._baseline[name] and value != draft[name]:
                self._show(name, value)
            self._baseline[name] = value
        self._take_stored_rules(self._stored_rules())

    def _rules_saved_elsewhere(self, rules: tuple[Rule, ...]) -> None:
        """Rules stored (an import, a change here, an Undo): shown unless a change of the list
        is being stored."""
        if not self.waiting_label.winfo_exists():
            return
        self._take_stored_rules(tuple(rules))

    def _take_stored_rules(self, stored: tuple[Rule, ...]) -> None:
        if not self.rules_changed:
            self.rule_drafts = list(stored)
        self._rules_baseline = stored
        self._refresh()

    def _refresh(self) -> None:
        """The switch, the rules, the ignored entries and AirGrabber's folders."""
        if self._automatic == "off":
            self.watching.deselect()
        else:
            self.watching.select()
        self._refresh_rules()
        self._refresh_ignored()
        self._refresh_airgrabber()

    # --- the rules -----------------------------------------------------------------------------

    def _preset_names(self) -> list[str]:
        """The stored presets' names; until they are read, the rules' own (no "gone" yet)."""
        if self._presets is not None:
            return [preset.name for preset in self._presets]
        return [rule.target(kind).preset for rule in self.rule_drafts for kind in rule.kinds()]

    def _unreachable(self) -> set[str]:
        """The folders the renamer's last look couldn't reach (normcased)."""
        if self._renamer is None:
            return set()
        return {os.path.normcase(folder) for folder in self._renamer.unreachable_folders()}

    def _refresh_rules(self) -> None:
        """The rules' rows with their notes, and the files no rule fits (not while a row is
        being dragged: once it is dropped)."""
        if self._settings is None or not self.rules_list.winfo_exists():
            return
        if self._drag is not None:
            self._refresh_after_drag = True
            return
        from app.forge.core.rules import rule_notes  # not at import time: see the top

        settings = self._draft_settings()
        notes = rule_notes(
            self.rule_drafts,
            settings,
            presets=self._preset_names(),
            unreachable=self._unreachable(),
        )
        for child in self.rules_list.winfo_children():
            child.destroy()
        self.rule_rows = []
        self.spots = {
            name: frame
            for name, frame in self.spots.items()
            if not name.startswith(RULE_SPOT_PREFIX)
        }
        for index, (rule, rule_notes_) in enumerate(zip(self.rule_drafts, notes, strict=True)):
            row = self._rule_row(index, rule, settings, [(n.level, n.text) for n in rule_notes_])
            self.rule_rows.append(row)
            if rule.id:
                self.spots[RULE_SPOT.format(id=rule.id)] = row.frame
        self._refresh_unfit()

    def _rule_row(
        self,
        index: int,
        rule: Rule,
        settings: ForgeSettings,
        notes: Sequence[tuple[str, str]],
    ) -> RuleRowWidgets:
        look = row_look(rule, settings)
        text_colour = theme.TEXT_MUTED if look.dim else theme.TEXT
        frame = ctk.CTkFrame(self.rules_list, fg_color="transparent", corner_radius=8)
        frame.grid(row=index, column=0, sticky="ew", pady=(0 if index == 0 else 2, 0))
        frame.grid_columnconfigure(2, weight=1)
        add_tooltip(frame, look.tip)
        grip = ctk.CTkLabel(
            frame,
            text=LOCK if look.locked else GRIP,
            width=20,
            font=theme.font(14),
            text_color=theme.TEXT_FAINT,
            cursor="fleur",
        )
        grip.grid(row=0, column=0, sticky="w", padx=(4, 4))
        if look.locked:
            add_tooltip(grip, t(LOCKED_TIP))
        for sequence, handler in (
            ("<ButtonPress-1>", functools.partial(self._drag_start, index)),
            ("<B1-Motion>", self._drag_motion),
            ("<ButtonRelease-1>", self._drag_end),
        ):
            grip.bind(sequence, handler, add="+")
        switch = ctk.CTkSwitch(
            frame,
            text="",
            width=44,
            progress_color=self._look.ACCENT,
        )
        if rule.on:
            switch.select()
        switch.configure(command=functools.partial(self._switch_clicked, index, switch))
        switch.grid(row=0, column=1, sticky="w")
        line = ctk.CTkFrame(frame, fg_color="transparent")
        line.grid(row=0, column=2, sticky="ew", padx=(4, 0))
        labels: list[ctk.CTkLabel] = []
        folder = ctk.CTkLabel(
            line,
            text=look.folder,
            font=theme.font(_TEXT_SIZE, bold=True),
            text_color=text_colour,
            anchor="w",
        )
        folder.pack(side="left", padx=(0, 8))
        add_tooltip(folder, look.tip)
        labels.append(folder)
        for chip in look.chips:
            label = ctk.CTkLabel(
                line,
                text=chip,
                height=20,
                corner_radius=10,
                fg_color=self._look.CHIP,
                font=theme.font(_CHIP_SIZE),
                text_color=theme.TEXT_MUTED,
            )
            label.pack(side="left", padx=(0, 4), ipadx=4)
            labels.append(label)
        mode = ctk.CTkLabel(
            frame,
            text=look.mode,
            height=20,
            corner_radius=10,
            fg_color=self._look.CHIP,
            font=theme.font(_CHIP_SIZE, bold=True),
            text_color=(
                theme.TEXT_MUTED
                if look.dim
                else theme.SUCCESS if rule.mode == "auto" else theme.WARNING
            ),
        )
        mode.grid(row=0, column=3, sticky="e", padx=(8, 4), ipadx=6)
        edit = ctk.CTkButton(
            frame,
            text=EDIT_MARK,
            width=32,
            height=dialog_kit.BUTTON_HEIGHT,
            fg_color="transparent",
            hover_color=self._look.HOVER,
            text_color=theme.BUTTON_TEXT,
            font=theme.font(15),
            command=functools.partial(self.edit_rule, index),
        )
        edit.grid(row=0, column=4, sticky="e")
        add_tooltip(edit, t(EDIT_TIP))
        route = ctk.CTkFrame(frame, fg_color="transparent")
        route.grid(row=1, column=2, columnspan=2, sticky="w", padx=(4, 0))
        preset = ctk.CTkLabel(
            route,
            text=look.preset,
            font=theme.font(_TEXT_SIZE),
            text_color=text_colour,
            anchor="w",
        )
        preset.pack(side="left")
        destination = ctk.CTkLabel(
            route,
            text=f"→ {look.destination}",
            font=theme.font(_TEXT_SIZE),
            text_color=theme.TEXT_MUTED,
            anchor="w",
            justify="left",
            wraplength=_ROUTE_WRAP,
        )
        destination.pack(side="left", padx=(8, 0))
        labels += [preset, destination, mode]
        note_labels: list[ctk.CTkLabel] = []
        to_folders = _folder_notes()
        for row_no, (level, text) in enumerate(notes, start=2):
            note: ctk.CTkLabel
            if text in to_folders:  # "⚠ Choose your Series folder under Folders.": a link there
                note = PageLink(
                    frame,
                    text,
                    PAGE_FOLDERS,
                    color=_NOTE_COLOURS[level],
                    size=_TEXT_SIZE,
                    wraplength=_NOTE_WRAP,
                )
            else:
                note = ctk.CTkLabel(
                    frame,
                    text=text,
                    font=theme.font(_TEXT_SIZE),
                    text_color=_NOTE_COLOURS[level],
                    anchor="w",
                    justify="left",
                    wraplength=_NOTE_WRAP,
                )
            note.grid(row=row_no, column=2, columnspan=3, sticky="w", padx=(4, 0), pady=(2, 0))
            note_labels.append(note)
        return RuleRowWidgets(frame, grip, switch, labels, edit, note_labels)

    def _refresh_unfit(self) -> None:
        """Under the list: per listed folder the files no rule fits, and "Show them"."""
        unfit = self._renamer.unfit() if self._renamer is not None else []
        text = "\n".join(unfit_line(found) for found in unfit if found.files)
        self.unfit_label.configure(text=text)
        if text:
            self._unfit_row.grid()
        else:
            self._unfit_row.grid_remove()

    def show_unfit(self) -> None:
        """"Show them": a small window with the names of the files no rule fits."""
        if self._renamer is None:
            return
        names = [file.name for found in self._renamer.unfit() for file in found.files]
        if self.unfit_window is not None and not self.unfit_window.closed:
            self.unfit_window.close()
        self.unfit_window = UnfitWindow(self._page.frame.winfo_toplevel(), names)

    # --- storing a change ---------------------------------------------------------------------

    def _start(
        self,
        box: SettingsBox,
        *,
        rules: Callable[[list[Rule]], list[Rule] | None] | None = None,
        changes: Mapping[str, Any] | None = None,
    ) -> asyncio.Task[bool]:
        return spawn(
            self._change(box, rules=rules, changes=changes or {}), name="forge-sorting-change"
        )

    async def _change(
        self,
        box: SettingsBox,
        *,
        rules: Callable[[list[Rule]], list[Rule] | None] | None = None,
        changes: Mapping[str, Any],
    ) -> bool:
        """One change, after the one before it: `rules` makes the new list from the one shown
        (None: no change of the rules), `changes` the settings. Shown at once; refused (said in
        `box`) when a rule would have a refusal; stored as Save did; Undo stores what was
        before. False when it wasn't stored (the page shows what is stored again)."""
        async with self._changing:
            if self._settings is None:
                return False
            # A change made just before the window closed is still stored; only the page
            # isn't touched then.
            alive = bool(self.rules_list.winfo_exists())
            before_rules = list(self.rule_drafts)
            new_rules = rules(list(before_rules)) if rules is not None else None
            # As stored (a tick or a switch shows its new value already).
            before = {name: self._baseline.get(name) for name in changes}
            if new_rules is None and all(before[n] == v for n, v in changes.items()):
                return True
            if new_rules is not None:
                self.rule_drafts = list(new_rules)
            if alive:
                for name, value in changes.items():
                    self._show(name, value)
                self._refresh()
            undo_rules = before_rules if new_rules is not None else None
            stored = await self._committer.commit(
                functools.partial(self._store, new_rules, dict(changes)),
                functools.partial(self._store_locked, undo_rules, before),
                box=box,
            )
            if not stored:
                if new_rules is not None and self.rule_drafts == new_rules:
                    self.rule_drafts = before_rules
                if self.rules_list.winfo_exists():
                    for name, value in before.items():
                        if self.draft()[name] == changes[name]:
                            self._show(name, value)
                    self._refresh()
            return stored

    def refusal_of(
        self, rules: Sequence[Rule] | None, changes: Mapping[str, Any] | None = None
    ) -> str | None:
        """Why these rules and settings can't be stored (`rules.refusal`): the first refused
        rule's number and its row's reason ("Rule 2: ✕ Can't save: …"), or None."""
        if self._settings is None:
            return None
        settings = self._settings.current.model_copy(update=dict(changes or {}))
        wanted = list(rules if rules is not None else self.rule_drafts)
        return rules_refusal(wanted, settings, presets=self._preset_names())

    async def _store_locked(
        self, rules: Sequence[Rule] | None, changes: Mapping[str, Any]
    ) -> bool:
        """Undo's store: after any change still being stored."""
        async with self._changing:
            return await self._store(rules, changes)

    async def _store(self, rules: Sequence[Rule] | None, changes: Mapping[str, Any]) -> bool:
        """Store the rules (None: as they are) and the settings `changes` in one step, as Save
        did (`RuleService.save`). Refused when a rule would have a refusal or forge.db
        couldn't be written."""
        problem = self.refusal_of(rules, changes)
        if problem is not None:
            raise Refused(problem)
        assert self._settings is not None
        try:
            if self._rules is not None:
                # Both in one step no look comes between; the rules that now watch what a look
                # skipped (a destination gone from under their folder) are marked fresh first.
                saved = tuple(await self._rules.save(rules, dict(changes)))
                if rules is not None:  # with their ids (a new rule's), as stored
                    self.rule_drafts, self._rules_baseline = list(saved), saved
            elif changes:
                await self._settings.update(**changes)
        except Exception as exc:  # a bad value, or forge.db couldn't be written
            # The type only: a message could name a folder (with the user's name in it).
            log.error("MediaForge's settings couldn't be saved (%s)", type(exc).__name__)
            raise Refused(t(SAVE_FAILED)) from exc
        if self.rules_list.winfo_exists():
            self._refresh()
        return True

    # --- changing the rules -------------------------------------------------------------------

    def _switch_clicked(self, index: int, switch: ctk.CTkSwitch) -> None:
        self.toggle_rule(index, bool(switch.get()))

    def toggle_rule(self, index: int, on: bool) -> asyncio.Task[bool]:
        """A rule's switch: on or off (AirGrabber's: "Use AirGrabber's downloads"), stored."""

        def change(rules: list[Rule]) -> list[Rule] | None:
            if not 0 <= index < len(rules):
                return None
            rules[index] = rules[index].model_copy(update={"on": on})
            return rules

        return self._start(self._rules_box, rules=change)

    def move_rule(self, src: int, dst: int) -> asyncio.Task[bool]:
        """The rule at `src` to `dst` (a drag): the rules after it close up; stored."""

        def change(rules: list[Rule]) -> list[Rule] | None:
            count = len(rules)
            if not (0 <= src < count and 0 <= dst < count) or src == dst:
                return None
            rules.insert(dst, rules.pop(src))
            return rules

        return self._start(self._rules_box, rules=change)

    def _row_at(self, y_root: int) -> int | None:
        """The row under the screen's `y_root`: above the first is the first, below the last the
        last."""
        if not self.rule_rows:
            return None
        for index, row in enumerate(self.rule_rows):
            top = row.frame.winfo_rooty()
            if y_root < top + row.frame.winfo_height():
                return index
        return len(self.rule_rows) - 1

    def _drag_start(self, index: int, _event: tk.Event[Any]) -> None:
        self._drag = index

    def _drag_motion(self, event: tk.Event[Any]) -> None:
        if self._drag is None:
            return
        target = self._row_at(event.y_root)
        for index, row in enumerate(self.rule_rows):
            row.frame.configure(fg_color=self._look.HOVER if index == target else "transparent")

    def _drag_end(self, event: tk.Event[Any]) -> None:
        src, self._drag = self._drag, None
        if src is None or not self.rules_list.winfo_exists():
            return
        dst = self._row_at(event.y_root)
        for row in self.rule_rows:
            row.frame.configure(fg_color="transparent")
        if dst is not None and dst != src:
            self.move_rule(src, dst)
        elif self._refresh_after_drag:
            self._refresh_rules()
        self._refresh_after_drag = False

    def _airgrabber_folders(self) -> list[str]:
        """The folders AirGrabber's rule watches with the form's rules and settings."""
        from app.forge.core.airgrabber_folders import airgrabber_uses  # not at import time
        from app.forge.core.rules import destinations

        settings = self._draft_settings()
        dests = destinations(self.rule_drafts, settings)
        return [use.folder.path for use in airgrabber_uses(settings, dests) if use.use == "watched"]

    def _peek(self, folder: Path) -> Awaitable[list[tuple[Path, ParsedMedia]] | None]:
        """The editor's live check: `folder` read as a look reads it, without what the owner
        ignored (in a thread under the look's time limit)."""
        from app.forge.core.rule_check import peek  # not at import time: it loads the renamer

        snapshot = getattr(self._ignored, "snapshot", None)
        return peek(folder, snapshot() if snapshot is not None else None)

    def _edit(self, index: int, rule: Rule, *, deletable: bool) -> None:
        """Open the editor for `rule` at `index` (`len(rule_drafts)`: a new one), never with
        the presets unknown: a read under way is waited for, a failed one tried again; when
        they still can't be read the editor stays shut and the rules box says so. Whether it
        is for a new rule is decided now, at the click: the drafts may change during the wait
        (an import), and a new rule's Save must still add it."""
        if self._settings is None or not self.rules_list.winfo_exists():
            return
        if self._editor is not None and not getattr(self._editor, "closed", True):
            return  # one editor at a time
        if self._opening is not None and not self._opening.done():
            return  # one is about to open
        new = index >= len(self.rule_drafts)
        if self._presets is not None:
            self._open_editor(index, rule, self._presets, deletable=deletable, new=new)
            return
        self._opening = spawn(
            self._edit_once_read(index, rule, deletable=deletable, new=new),
            name="forge-settings-editor",
        )

    async def _edit_once_read(
        self, index: int, rule: Rule, *, deletable: bool, new: bool
    ) -> None:
        presets = await self._presets_read()
        self._opening = None
        if not self.rules_list.winfo_exists():
            return
        if presets is None:
            self._show_presets_problem(t(PRESETS_UNREAD))
            return
        self._open_editor(index, rule, presets, deletable=deletable, new=new)

    def _show_presets_problem(self, text: str) -> None:
        self.presets_problem.configure(text=text)
        if text:
            self.presets_problem.grid(
                row=self._presets_problem_row, column=0, sticky="w", pady=(_ROW_GAP, 0)
            )
        else:
            self.presets_problem.grid_remove()

    def _open_editor(
        self, index: int, rule: Rule, presets: Sequence[Preset], *, deletable: bool, new: bool
    ) -> None:
        if self._editor is not None and not getattr(self._editor, "closed", True):
            return
        self._show_presets_problem("")
        self._editor = self.open_editor(
            self._page.frame.winfo_toplevel(),
            rule=rule,
            index=index,
            rules=list(self.rule_drafts),
            settings=self._draft_settings(),
            presets=list(presets),
            airgrabber_folders=self._airgrabber_folders(),
            peek=self._peek,
            ask_folder=self.ask_folder,
            on_save=functools.partial(self._edited, index, rule, new),
            on_delete=functools.partial(self._deleted, index, rule) if deletable else None,
        )

    def _place_of(self, index: int, opened: Rule) -> int | None:
        """Where the rule the editor opened with is now (the drafts may have been replaced
        meanwhile, an import): at `index` still, else by its id; None when it is gone."""
        if index < len(self.rule_drafts) and self.rule_drafts[index] is opened:
            return index
        if opened.id:
            return next((i for i, r in enumerate(self.rule_drafts) if r.id == opened.id), None)
        return None

    def _edited(self, index: int, opened: Rule, new: bool, rule: Rule) -> str | None:
        """The editor's Save: in the opened rule's place (a new rule last), stored. Why it
        can't be (the editor stays open and says so), or None."""
        place = len(self.rule_drafts) if new else self._place_of(index, opened)
        if place is None:
            return None
        rules = list(self.rule_drafts)
        if place >= len(rules):
            rules.append(rule)
        else:
            rules[place] = rule
        problem = self.refusal_of(rules)
        if problem is not None:
            return problem
        self.rule_saved(place, rule)
        return None

    def _deleted(self, index: int, opened: Rule) -> None:
        place = self._place_of(index, opened)
        if place is not None:
            self.delete_rule(place)

    def edit_rule(self, index: int) -> None:
        """✎: the rule's editor (AirGrabber's rule without Delete)."""
        if not 0 <= index < len(self.rule_drafts):
            return
        rule = self.rule_drafts[index]
        self._edit(index, rule, deletable=not rule.airgrabber)

    def add_rule(self) -> None:
        """＋ Add a rule: the editor for a new rule (last), with the Library presets."""
        if self._settings is None:
            return
        self._edit(len(self.rule_drafts), self._new_rule(), deletable=False)

    def _new_rule(self) -> Rule:
        settings = self._draft_settings()
        return Rule(
            shows=RuleTarget(preset=settings.series_preset),
            films=RuleTarget(preset=settings.movies_preset),
        )

    def rule_saved(self, index: int, rule: Rule) -> asyncio.Task[bool]:
        """The editor's Save: the rule at `index` (a new one last), stored."""

        def change(rules: list[Rule]) -> list[Rule]:
            if index >= len(rules):
                rules.append(rule)
            else:
                rules[index] = rule
            return rules

        return self._start(self._rules_box, rules=change)

    def delete_rule(self, index: int) -> asyncio.Task[bool]:
        """The editor's Delete rule (never AirGrabber's), stored."""

        def change(rules: list[Rule]) -> list[Rule] | None:
            if not 0 <= index < len(rules) or rules[index].airgrabber:
                return None
            del rules[index]
            return rules

        return self._start(self._rules_box, rules=change)

    def _airgrabber_draft(self) -> Rule | None:
        rule = next((rule for rule in self.rule_drafts if rule.airgrabber), None)
        if rule is None and self._rules is not None:
            rule = self._rules.airgrabber_rule
        return rule

    def _watched_by_a_rule(self, folder: str) -> bool:
        return any(
            not rule.airgrabber and _same_folder(rule.folder, folder) for rule in self.rule_drafts
        )

    def add_rule_for(self, folder: str) -> asyncio.Task[bool] | None:
        """A suggestion's "Add a rule for this folder": a rule watching it, with AirGrabber's
        rule's routing (what it takes, presets, extras, destinations, mode), last; stored (new,
        so its first look shows what is already there: nothing moves unseen)."""
        if self._settings is None or self._watched_by_a_rule(folder):
            return None
        airgrabber = self._airgrabber_draft()
        if airgrabber is None:
            rule = self._new_rule().model_copy(update={"folder": folder})
        else:
            rule = airgrabber.model_copy(
                update={
                    "id": 0,
                    "name": "",
                    "folder": folder,
                    "airgrabber": False,
                    "on": True,
                    "fresh": False,
                }
            )

        def change(rules: list[Rule]) -> list[Rule]:
            return [*rules, rule]

        return self._start(self._rules_box, rules=change)

    def spot_shown(self, spot: str) -> None:
        """Settings scrolled to `spot`: a rule's row (`rule:<id>`) opens its editor."""
        if not spot.startswith(RULE_SPOT_PREFIX):
            return
        try:
            rule_id = int(spot.removeprefix(RULE_SPOT_PREFIX))
        except ValueError:
            return
        index = next((i for i, r in enumerate(self.rule_drafts) if r.id == rule_id), None)
        if index is not None:
            self.edit_rule(index)

    # --- ignored files and folders ------------------------------------------------------------

    @property
    def ignored_shown(self) -> list[IgnoredPath]:
        """The ignored entries the box lists (without those being stopped)."""
        stopped = {os.path.normcase(str(path)) for path in self._unignore}
        return [
            e for e in self._ignored_entries if os.path.normcase(str(e.path)) not in stopped
        ]

    @property
    def unignored(self) -> list[Path]:
        """What is being stopped now."""
        return list(self._unignore)

    def stop_ignoring(self, path: Path) -> asyncio.Task[bool] | None:
        """Stop ignoring `path`, at once (Undo ignores it again)."""
        source = self._ignored
        key = os.path.normcase(str(path))
        entry = next(
            (e for e in self._ignored_entries if os.path.normcase(str(e.path)) == key), None
        )
        if source is None or entry is None:
            return None
        if all(key != os.path.normcase(str(p)) for p in self._unignore):
            self._unignore.append(path)
        self._refresh_ignored()
        return spawn(self._stop_ignoring(source, entry), name="forge-stop-ignoring")

    async def _stop_ignoring(self, source: IgnoredSource, entry: IgnoredPath) -> bool:
        async def apply() -> bool:
            try:
                return await source.stop_ignoring(entry.path)
            except Exception as exc:  # forge.db
                log.error("Stopping to ignore failed (%s)", type(exc).__name__)
                raise Refused(t(SAVE_FAILED)) from exc

        async def undo() -> bool:
            try:
                await source.ignore(entry.path, folder=entry.folder)
            except Exception as exc:  # forge.db
                log.error("Ignoring again failed (%s)", type(exc).__name__)
                raise Refused(t(SAVE_FAILED)) from exc
            return True

        stored = await self._committer.commit(apply, undo, box=self._ignored_box)
        key = os.path.normcase(str(entry.path))
        self._unignore = [p for p in self._unignore if os.path.normcase(str(p)) != key]
        if self.ignored_list.winfo_exists():
            self._refresh_ignored()
        return stored

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

    # --- AirGrabber's folders ----------------------------------------------------------------------

    def _refresh_airgrabber(self) -> None:
        """AirGrabber's folders, each with why (or why not) its rule watches it, "Add a rule for
        this folder" on a suggestion no rule watches yet, and what to choose when AirGrabber names
        no folder on this PC (while its rule is on)."""
        from app.forge.core.airgrabber_folders import (  # not at import time: see the top
            airgrabber_uses,
            ask_text,
            use_text,
        )
        from app.forge.core.rules import destinations

        for child in self.airgrabber_list.winfo_children():
            child.destroy()
        if self._settings is None:
            self.airgrabber_list.grid_remove()
            return
        rule = self._airgrabber_draft()
        on = rule is not None and rule.on
        draft = self._draft_settings()
        row_no = 0
        for use in airgrabber_uses(draft, destinations(self.rule_drafts, draft)):
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
            if use.use == "suggested" and not self._watched_by_a_rule(use.folder.path):
                button = self._button(
                    self.airgrabber_list,
                    t(ADD_RULE_FOR),
                    functools.partial(self.add_rule_for, use.folder.path),
                )
                button.configure(width=_SUGGEST_WIDTH)
                button.grid(row=row_no, column=1, rowspan=2, sticky="e", padx=(10, 0), pady=pady)
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
        if ask:  # what to fill in ("Finished downloads" in Settings › Folders): a link there
            PageLink(
                self.airgrabber_list,
                ask,
                PAGE_FOLDERS,
                color=theme.WARNING,
                size=_TEXT_SIZE,
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
        # taken out, so the box ends under its explanation.
        if row_no:
            self.airgrabber_list.grid()
        else:
            self.airgrabber_list.grid_remove()

    # --- the other changes ------------------------------------------------------------------------

    def _watching_clicked(self) -> None:
        self.set_watching(bool(self.watching.get()))

    def set_watching(self, on: bool) -> asyncio.Task[bool]:
        """The "Watch folders" switch: off pauses every rule; on watches again (as it did before
        it was paused, else On: each rule's own mode decides); stored."""
        value = self._watching_as if on else "off"
        return self._start(self._status_box, changes={"automatic": value})

    def _option_clicked(self, field: str, variable: tk.BooleanVar) -> None:
        self._start(self._options_box, changes={field: bool(variable.get())})


class UnfitWindow(ctk.CTkToplevel):
    """"Files no rule fits": the names, a Close button, nothing to do with them (in this
    version). Modal over Settings, which gets the grab back when it closes (as the rule editor)."""

    def __init__(self, master: Any, names: Sequence[str]) -> None:
        from app.ui.borderless import clearance, make_borderless
        from app.ui.branding import set_window_icon

        look = app_look()
        super().__init__(master, fg_color=look.BG)
        self._grab_before: tk.Misc | None = self.grab_current()
        self._closed = False
        title = t(UNFIT_TITLE)
        self.title(title)
        set_window_icon(self)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self.close)
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=25, pady=20)
        ctk.CTkFrame(body, width=_UNFIT_WIDTH, height=1, fg_color="transparent").pack()
        heading = dialog_kit.heading_row(body, title, right_room=clearance(25))
        heading.pack(fill="x", pady=(0, dialog_kit.LINE_GAP))
        self.scroll = dialog_kit.FitScroll(body, follow=True)
        self.scroll.pack(fill="both", expand=True)
        self.names = ctk.CTkFrame(self.scroll, fg_color="transparent")
        self.names.grid(row=0, column=0, sticky="nsew")
        self.names.grid_columnconfigure(0, weight=1)
        scale = float(ctk.ScalingTracker.get_widget_scaling(self)) or 1.0
        self.scroll.set_limit(round(_UNFIT_LIST_MAX * scale))
        for row, name in enumerate(names):
            ctk.CTkLabel(
                self.names,
                text=name,
                font=theme.font(_TEXT_SIZE),
                text_color=theme.TEXT,
                anchor="w",
                justify="left",
                wraplength=_UNFIT_WIDTH,
            ).grid(row=row, column=0, sticky="w", pady=(0 if row == 0 else 2, 0))
        self.scroll.refit()
        self.close_button = dialog_kit.primary_button(
            body, t(CLOSE), self.close, width=100, look=look
        )
        self.close_button.pack(anchor="e", pady=(16, 0))
        self.bind("<Escape>", lambda _event: self.close())
        make_borderless(self, drag_widgets=(self, body, heading), on_close=self.close, look=look)
        dialog_kit.place_over(self, master)
        dialog_kit.activate_later(self)

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Close / Esc / ✕."""
        if self._closed:
            return
        self._closed = True
        grab = self._grab_before
        if self.winfo_exists():
            self.destroy()
        try:
            if grab is not None and grab.winfo_exists():
                grab.grab_set()  # Settings, modal again
                grab.focus_force()
        except tk.TclError:  # gone meanwhile: only the focus is lost
            pass


# -- Folders --------------------------------------------------------------------------------------

DestinationKind = Literal["series", "film"]  # as the Folders page's lists (series / film folders)
_DESTINATION_FIELDS: dict[DestinationKind, str] = {
    "series": "series_folder",
    "film": "movies_folder",
}


class LibraryDestinations:
    """MediaForge's Series and Movies folders (`ForgeSettings.series_folder` / `movies_folder`,
    where rules moving to "your Series / Movies folder" and files added by hand with Move to
    library folder go) as Settings' Folders page marks them in its lists (app/ui/folders_page.py's
    `Destinations`). `start()` starts Forge if nothing has yet; until it is ready nothing is
    known (`ready`), and when it can't start `reason` says why. A change is refused when it
    leaves a rule with a refusal (`rules_refusal`, as Sorting's changes) and stored as Sorting
    stores its settings (`RuleService.save`: the rules that will watch more are marked first,
    nothing moves unseen). Logs never name a folder."""

    def __init__(self, forge: _Starter) -> None:
        self._forge = forge
        self._listeners: list[Callable[[], None]] = []
        self._unsubscribe: Callable[[], None] | None = None
        self._failed = False
        self._closed = False

    def _settings(self) -> SettingsSource | None:
        settings: SettingsSource | None = getattr(self._forge.services, "settings", None)
        return settings

    @property
    def ready(self) -> bool:
        return self._settings() is not None

    @property
    def reason(self) -> str:
        """Why MediaForge isn't there ("" while it is getting ready or once it is)."""
        return (self._forge.reason or t(UNAVAILABLE)) if self._failed else ""

    def folder(self, kind: DestinationKind) -> str:
        """Where sorted files of `kind` go ("": not chosen, or not known yet)."""
        settings = self._settings()
        if settings is None:
            return ""
        value: str = getattr(settings.current, _DESTINATION_FIELDS[kind])
        return value

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]:
        """`listener` hears Forge getting ready (or failing) and every change of the folders."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def _tell(self) -> None:
        for listener in list(self._listeners):
            listener()

    async def start(self) -> bool:
        """Forge started (if nothing has yet); True once the folders are known."""
        if self._forge.services is None:
            await self._forge.start()
        settings = self._settings()
        if self._closed:
            return settings is not None
        if settings is None:
            self._failed = True
        elif self._unsubscribe is None:
            self._unsubscribe = settings.subscribe(lambda _settings: self._tell())
        self._tell()
        return settings is not None

    def close(self) -> None:
        """The page went: nothing is heard any more."""
        self._closed = True
        self._listeners.clear()
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def _changes(self, folders: Mapping[DestinationKind, str]) -> dict[str, str]:
        return {_DESTINATION_FIELDS[kind]: folder for kind, folder in folders.items()}

    def problem(self, folders: Mapping[DestinationKind, str]) -> str | None:
        """Why these folders can't be the destinations (a rule would be refused), or None."""
        services, source = self._forge.services, self._settings()
        if source is None:
            return None
        settings = source.current.model_copy(update=self._changes(folders))
        rules: RulesSource | None = getattr(services, "rules", None)
        if rules is not None:
            stored: Sequence[Rule] = list(rules.current)
        else:
            from app.forge.core.rules import rules_from_settings  # not at import time

            stored = rules_from_settings(settings)
        return rules_refusal(stored, settings)

    async def store(self, folders: Mapping[DestinationKind, str]) -> bool:
        """Store these destinations (False: they are so already). Raises Refused when a rule
        would be refused or forge.db couldn't be written (nothing stored then)."""
        services, source = self._forge.services, self._settings()
        if source is None:
            return False
        changes = {
            name: value
            for name, value in self._changes(folders).items()
            if getattr(source.current, name) != value
        }
        if not changes:
            return False
        problem = self.problem(folders)
        if problem is not None:
            raise Refused(problem)
        rules: RulesSource | None = getattr(services, "rules", None)
        try:
            if rules is not None:
                await rules.save(None, changes)
            else:
                await source.update(**changes)
        except Exception as exc:  # a bad value, or forge.db couldn't be written
            log.error("MediaForge's library folders couldn't be saved (%s)", type(exc).__name__)
            raise Refused(t(SAVE_FAILED)) from exc
        return True


def build_sorting(
    slots: PartSlots, forge: ForgeRuntime, committer: Committer
) -> ForgeSectionPart:
    """Sorting's part."""
    return LibrarySection(slots[PAGE_SORTING], forge, committer=committer)


# Per page with a MediaForge part (app/ui/settings_pages_registry.py): its builder and what
# Settings' search finds it by before its first visit (its boxes' titles and a few words in
# them); once built, the page's own boxes are searched.
FORGE_PARTS: dict[str, ForgePart] = {
    PAGE_SORTING: ForgePart(
        build_sorting, (WATCHING, RULES_TITLE, OPTIONS_TITLE, IGNORED_TITLE, AIRGRABBER_TITLE)
    ),
    PAGE_NAMING: ForgePart(
        build_naming,
        (
            _naming.PRESETS_TITLE,
            _naming.LIBRARY_TITLE,
            _naming.LANGUAGE_TITLE,
            _naming.MATCHES_TITLE,
            N_("Trailer"),
            N_("Poster"),
        ),
    ),
    PAGE_SUBTITLES: ForgePart(
        build_subtitles, (_subtitles.LANGUAGES_TITLE, _subtitles.HEARING_TITLE)
    ),
    PAGE_ACCOUNTS: ForgePart(
        None,
        (_subtitles.OPENSUBTITLES_TITLE, _subtitles.OWN_KEY_LABEL),
        owner=PAGE_SUBTITLES,
    ),
    PAGE_WINDOWS: ForgePart(build_menu, (_menu.MENU_TITLE, _menu.PRESETS_TITLE, _menu.FILES_TITLE)),
}
