"""Settings › Naming: the old MediaForge's Settings › Naming tab (`ui/settings_view.py`:
`_build_presets`, the editor, `_update_sample`, `_build_matches`), the names language from its
TMDB tab and the Series / Movies presets from its Library tab, as a section of MediaForge's
Settings window (docs/forge/INTEGRATION.md 3.7; registered in `settings_pages.FORGE_SECTIONS`).

- **Presets**: the list in order; the chosen one is edited in place: its name, type Auto /
  Movie / TV, the format with a live sample on a sample episode and film (a `{code}` the
  formatter doesn't know is flagged), buttons that add a code at the format's cursor, and its
  extras. New adds one at the end; Delete removes the chosen one, never the last.
- **Library presets**: which preset names what is moved into the Series and Movies folders. The
  choice follows a preset renamed here; a preset deleted here hands it to the first one of the
  same type.
- **Names language**: TMDB's language for titles and episode names (MediaForge's LANGUAGES,
  en-US by default), separate from AirGrabber's own language.
- **Remembered matches**: the picks made for uncertain matches; forget one, or all.

As the rest of Settings, nothing is stored before Save (D9): `problem()` refuses a preset
without a name or a name used twice; `save()` stores the presets (merged onto what is stored by
then, so a preset changed elsewhere meanwhile and untouched here keeps that change), then the
changed settings (and `last_preset`, the Inbox's remembered preset, when its preset was
renamed or deleted: `last_preset_after`), then forgets the matches. Cancel and ✕ change
nothing. After a Save that changed the presets, every listener connected with
`on_presets_saved` is told (the right-click menu refreshes its entries, F-25); while none is
connected (the Right-click menu's page wasn't visited) settings_menu's `carry_presets` runs.

The Settings window imports this module with `settings_pages`: at import time it loads only
Forge's models (the formatter and the Inbox's option texts load when a section is built), so
Forge's core, forge.db and guessit stay unloaded (INTEGRATION.md 3.6).
"""

from __future__ import annotations

import builtins
import contextlib
import functools
import logging
import tkinter as tk
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

import customtkinter as ctk

from app.forge.models.match import RememberedMatch
from app.forge.models.media import MatchState, MediaKind, ParsedMedia, ResolvedMedia, TypeHint
from app.forge.models.preset import Preset, PresetOptions
from app.forge.models.settings import ForgeSettings
from app.forge.ui.look import app_look
from app.i18n import N_, t
from app.ui import dialog_kit, theme
from app.ui.settings_layout import BOX_INNER
from app.ui.tasks import spawn
from app.ui.tooltip import add_tooltip

if TYPE_CHECKING:
    import asyncio

    from app.forge.runtime import ForgeRuntime
    from app.forge.ui.settings_pages import SettingsSource
    from app.forge.ui.settings_pages import ForgeSectionPart
    from app.ui.settings_layout import SettingsBox, SettingsPage

log = logging.getLogger(__name__)

# -- the registry's entry (settings_pages.FORGE_SECTIONS) ---------------------------------------

SECTION_NAMING = N_("Naming")
NAMING_ICON = "🏷"
NAMING_NOTE = N_("How MediaForge names shows and movies, and the matches it remembers.")

# -- texts ----------------------------------------------------------------------------------------

PRESETS_TITLE = N_("Presets")
PRESETS_TEXT = N_("A preset is a media type, a naming format and extras.")
NEW_PRESET = N_("New preset")  # the button, and a new preset's name
DELETE_PRESET = N_("Delete preset")
NAME_CAPTION = N_("Name")
TYPE_CAPTION = N_("Type")
FORMAT_CAPTION = N_("Format")
EXTRAS_CAPTION = N_("Extras")
TYPE_LABELS: dict[TypeHint, str] = {
    TypeHint.AUTO: N_("Auto"),
    TypeHint.MOVIE: N_("Movie"),
    TypeHint.TV: N_("TV"),
}
UNKNOWN_CODES = N_("⚠ Not a known code: {codes}")
CODES_CAPTION = N_("Click to add to the format:")
FOLDER_INSERT = "/"
DASH_INSERT = " - "
# The buttons under the format, row by row: what each is called and what it adds.
FORMAT_CODES: tuple[tuple[str, str], ...] = (
    (N_("Name (year)"), "{ny}"),
    (N_("Title"), "{t}"),
    (N_("Episode name"), "{en}"),
    (N_("Season + episode"), "{s00e00}"),
    (N_("Season"), "{s}"),
    (N_("Episode"), "{e}"),
    (N_("New folder"), FOLDER_INSERT),
    (N_("Dash"), DASH_INSERT),
)
_FIXED_EXAMPLES: dict[str, str] = {FOLDER_INSERT: "\\", DASH_INSERT: "-"}
CODE_TIP = N_("Adds {code} to the format")
FOLDER_TIP = N_("Whatever comes before it becomes a folder")
NAME_NEEDED = N_("Give every preset a name.")
NAME_MISSING = N_("Give this preset a name.")
NAME_TWICE = N_("Two presets are called “{name}”. Give each its own name.")
NAME_TAKEN = N_("Another preset is called that already.")
LIBRARY_TITLE = N_("Library presets")
LIBRARY_TEXT = N_("The preset that names what is moved into your Series and Movies folders.")
LANGUAGE_TITLE = N_("Names language")
LANGUAGE_TEXT = N_(
    "The language of titles and episode names from TMDB. AirGrabber's own language doesn't "
    "change it."
)
MATCHES_TITLE = N_("Remembered matches")
MATCHES_TEXT = N_("Your picks for uncertain TMDB matches. Forget one to be asked again.")
FORGET = N_("Forget")
CLEAR_ALL = N_("Clear all")
MATCH_SHOW = N_("{title} → {label} · show")
MATCH_MOVIE = N_("{title} → {label} · movie")
LOAD_FAILED = N_("MediaForge's presets couldn't be read. The log has the details.")

# MediaForge's names languages (TMDB's codes), en-US by default.
LANGUAGES: tuple[str, ...] = (
    "en-US",
    "en-GB",
    "nl-NL",
    "de-DE",
    "fr-FR",
    "es-ES",
    "it-IT",
    "pt-BR",
    "sv-SE",
)
LANGUAGE_NAMES: dict[str, str] = {
    "en-US": N_("English (US)"),
    "en-GB": N_("English (UK)"),
    "nl-NL": N_("Dutch"),
    "de-DE": N_("German"),
    "fr-FR": N_("French"),
    "es-ES": N_("Spanish"),
    "it-IT": N_("Italian"),
    "pt-BR": N_("Portuguese (Brazil)"),
    "sv-SE": N_("Swedish"),
}

DEFAULT_FORMAT = "{ny}"  # an empty format names a file by its name (year)
SAMPLE_EXTENSION = ".mkv"
SAMPLE_EPISODE = ResolvedMedia(
    parsed=ParsedMedia(
        source_name="sample.mkv", kind=MediaKind.EPISODE, title="VisionQuest", season=1, episode=2
    ),
    title="VisionQuest",
    year=2026,
    episode_title="Theseus",
    match_state=MatchState.CONFIDENT,
)
SAMPLE_MOVIE = ResolvedMedia(
    parsed=ParsedMedia(source_name="sample.mkv", kind=MediaKind.MOVIE, title="Dune Part Two"),
    title="Dune Part Two",
    year=2024,
    episode_title=None,
    match_state=MatchState.CONFIDENT,
)
_SAMPLES: dict[TypeHint, tuple[ResolvedMedia, ...]] = {
    TypeHint.AUTO: (SAMPLE_EPISODE, SAMPLE_MOVIE),  # Auto names both kinds
    TypeHint.MOVIE: (SAMPLE_MOVIE,),
    TypeHint.TV: (SAMPLE_EPISODE,),
}

# What this section edits of ForgeSettings; Save stores only the ones changed here.
FIELDS: tuple[str, ...] = ("series_preset", "movies_preset", "tmdb_language")
# Which preset type takes over a Series / Movies choice whose preset is deleted here.
_CHOICE_TYPES: dict[str, TypeHint] = {"series_preset": TypeHint.TV, "movies_preset": TypeHint.MOVIE}

_BOX_INNER = BOX_INNER  # the page's width inside a box's border and padding
_LIST_WIDTH = 170  # the presets list
_LIST_PAD = 4
_ITEM_HEIGHT = 32
_COLUMN_GAP = 18  # between the list and the editor
_EDITOR_WIDTH = _BOX_INNER - _LIST_WIDTH - _COLUMN_GAP
_ROW_GAP = 8
_CAPTION_GAP = 12  # above an editor field's caption
_LABEL_WIDTH = 90  # "Series" / "Movies"
_MENU_WIDTH = 260
_BUTTON_WIDTH = 110
_TEXT_SIZE = dialog_kit.SMALL_SIZE + 1  # as the dialog's boxes' explanations
_CODE_COLUMNS = 3
_CODE_GAP = 4  # between the code buttons
_CODE_LABEL_SIZE = 12
_CODE_EXAMPLE_SIZE = 10
_CODE_PAD_X = 7  # inside a code button; "Season + episode" just fits
_MATCH_WRAP = _BOX_INNER - _BUTTON_WIDTH - 10


# -- the right-click menu's hook ----------------------------------------------------------------


class PresetsChange(NamedTuple):
    """What a Save changed of the presets."""

    names: tuple[str, ...]  # the presets now, in order
    renamed: Mapping[str, str]  # old name → new name
    removed: tuple[str, ...]  # deleted


PresetsListener = Callable[[PresetsChange], Awaitable[None]]
_listeners: list[PresetsListener] = []


def on_presets_saved(listener: PresetsListener) -> Callable[[], None]:
    """Have `listener` awaited after every Settings Save that stored changed presets (the
    right-click menu refreshes its entries, F-25). Returns the function that disconnects it.
    A listener that fails is logged; the Save stands."""
    _listeners.append(listener)

    def disconnect() -> None:
        with contextlib.suppress(ValueError):
            _listeners.remove(listener)

    return disconnect


async def _tell(change: PresetsChange, parts: _Parts) -> None:
    listeners = list(_listeners)
    if not listeners:  # Settings builds a page on its first visit: the menu's isn't there
        from app.forge.ui.settings_menu import carry_presets  # it imports this module

        listeners = [functools.partial(carry_presets, parts)]
    for listener in listeners:
        try:
            await listener(change)
        except Exception as exc:  # the presets are stored; the menu catches up at Forge's start
            log.error("A listener to saved presets failed (%s)", type(exc).__name__)


# -- the pure part ---------------------------------------------------------------------------------


@dataclass(eq=False)  # compared by identity: the editor and the Series / Movies choice hold one
class PresetDraft:
    """One preset in the form. `origin` is the stored preset it started from (None: new here)."""

    origin: str | None
    preset: Preset


def sample_lines(fmt: str, type_hint: TypeHint) -> tuple[list[str], list[str]]:
    """The sample episode's and / or film's path under `fmt` (folders joined with "\\"), and
    the codes in `fmt` the formatter doesn't know, each once."""
    from app.forge.core.formatter import render  # not at import time: see the top

    lines: list[str] = []
    unknown: list[str] = []
    for sample in _SAMPLES[type_hint]:
        rendered = render(fmt, sample, SAMPLE_EXTENSION)
        lines.append("\\".join(rendered.parts))
        for code in rendered.unknown:
            if code not in unknown:
                unknown.append(code)
    return lines, unknown


def code_example(code: str) -> str:
    """What a code button's `code` gives: the formatter's own answer on the sample episode, so
    it can't drift; the folder separator and the dash as they show in a path."""
    fixed = _FIXED_EXAMPLES.get(code)
    if fixed is not None:
        return fixed
    from app.forge.core.formatter import render  # not at import time: see the top

    return "\\".join(render(code, SAMPLE_EPISODE, "").parts)


def new_preset_name(taken: Iterable[str]) -> str:
    """"New preset", or "New preset 2", 3 … when that name is taken."""
    used = {name.casefold() for name in taken}
    base = t(NEW_PRESET)
    name, number = base, 2
    while name.casefold() in used:
        name, number = f"{base} {number}", number + 1
    return name


def preset_problem(names: Sequence[str]) -> str | None:
    """Why these preset names can't be stored (translated), or None: each needs one, and no
    two may be the same (ignoring case: the right-click menu's keys don't tell them apart)."""
    if any(not name for name in names):
        return t(NAME_NEEDED)
    seen: set[str] = set()
    for name in names:
        if name.casefold() in seen:
            return t(NAME_TWICE, name=name)
        seen.add(name.casefold())
    return None


def merged_presets(
    stored: Sequence[Preset], baseline: Sequence[Preset], drafts: Sequence[PresetDraft]
) -> list[Preset]:
    """The presets to store: `stored` (what is stored now) with the form's changes made to it.
    `baseline` is what the form loaded. A preset deleted in the form goes; one edited or
    renamed in the form replaces its stored one in place; one untouched in the form stays as it
    is stored (also when changed elsewhere meanwhile); new ones come last. With no change made
    in between (`stored` == `baseline`), that is the form's list as it shows."""
    before = {preset.name: preset for preset in baseline}
    kept = {draft.origin for draft in drafts if draft.origin is not None}
    edited = {
        draft.origin: draft.preset
        for draft in drafts
        if draft.origin is not None and draft.preset != before.get(draft.origin)
    }
    result: list[Preset] = []
    placed: set[str] = set()
    for preset in stored:
        if preset.name in before and preset.name not in kept:
            continue  # deleted in the form
        if preset.name in edited:
            result.append(edited[preset.name])
            placed.add(preset.name)
        else:
            result.append(preset)
    for draft in drafts:
        if draft.origin is None or (draft.origin in edited and draft.origin not in placed):
            result.append(draft.preset)  # new here, or edited here but gone from storage since
    unique: list[Preset] = []
    seen: set[str] = set()
    for preset in result:  # a name taken meanwhile elsewhere: the first one stays
        if preset.name.casefold() not in seen:
            seen.add(preset.name.casefold())
            unique.append(preset)
    return unique or list(stored)


def presets_change(
    names: Sequence[str], baseline: Sequence[Preset], drafts: Sequence[PresetDraft]
) -> PresetsChange:
    """What the form changed of `baseline`, told to the listeners (`names`: what is stored)."""
    renamed = {
        draft.origin: draft.preset.name
        for draft in drafts
        if draft.origin is not None and draft.origin != draft.preset.name
    }
    kept = {draft.origin for draft in drafts if draft.origin is not None}
    removed = tuple(preset.name for preset in baseline if preset.name not in kept)
    return PresetsChange(tuple(names), renamed, removed)


def fallback_preset(drafts: Sequence[PresetDraft], wanted: TypeHint) -> PresetDraft | None:
    """The preset that takes over a Series / Movies choice: the first of `wanted`'s type, else
    the first Auto one, else the first."""
    for type_hint in (wanted, TypeHint.AUTO):
        found = next((draft for draft in drafts if draft.preset.type is type_hint), None)
        if found is not None:
            return found
    return drafts[0] if drafts else None


def last_preset_after(
    last: str | None, change: PresetsChange, before: Sequence[Preset], now: Sequence[Preset]
) -> str | None:
    """The Inbox's remembered preset (`last_preset`) after a Save changed the presets: a renamed
    one by its new name; a deleted one handed to the first preset of its type now stored
    (`now`), or forgotten (None) when none is left; any other as it was. `before`: the presets
    as the form loaded them."""
    if last is None:
        return None
    if last in change.renamed:
        return change.renamed[last]
    if last in change.removed:
        kind = next((preset.type for preset in before if preset.name == last), None)
        return next((preset.name for preset in now if preset.type is kind), None)
    return last


def language_name(code: str) -> str:
    """A names language as the menu shows it; a code not in LANGUAGES as it is."""
    return t(LANGUAGE_NAMES[code]) if code in LANGUAGE_NAMES else code


def match_text(match: RememberedMatch) -> str:
    title = f"{match.norm_title} ({match.year})" if match.year else match.norm_title
    text = MATCH_SHOW if match.kind is MediaKind.EPISODE else MATCH_MOVIE
    return t(text, title=title, label=t(match.label))  # "Don't use TMDB" is stored in English


_MatchKey = tuple[MediaKind, str, int | None]


def _match_key(match: RememberedMatch) -> _MatchKey:
    return (match.kind, match.norm_title, match.year)


# -- what the section needs of Forge (tests pass fakes) -------------------------------------------


class _PresetSource(Protocol):
    async def list(self) -> builtins.list[Preset]: ...

    async def replace_all(self, presets: Sequence[Preset]) -> None: ...


class _MatchSource(Protocol):
    async def list(self) -> builtins.list[RememberedMatch]: ...

    async def forget(self, match: RememberedMatch) -> None: ...


class _SubtitleState(Protocol):
    @property
    def available(self) -> bool: ...


class _Parts(Protocol):
    @property
    def subtitles(self) -> _SubtitleState: ...

    @property
    def settings(self) -> SettingsSource: ...

    @property
    def presets(self) -> _PresetSource: ...

    @property
    def matches(self) -> _MatchSource: ...


class _Starter(Protocol):
    """What the section needs of the ForgeRuntime."""

    @property
    def reason(self) -> str: ...

    @property
    def services(self) -> _Parts | None: ...

    async def start(self) -> _Parts | None: ...


# -- the section -----------------------------------------------------------------------------------


class CodeChip(ctk.CTkFrame):
    """A button under the format: the plain word, under it what it gives. All of it is clickable;
    the hover colour stays while the pointer moves between its parts."""

    def __init__(
        self, master: Any, look: theme.Look, text: str, code: str, on_click: Callable[[str], None]
    ) -> None:
        super().__init__(
            master,
            fg_color=look.SURFACE,
            corner_radius=dialog_kit.BUTTON_RADIUS,
            border_width=1,
            border_color=look.EDGE,
        )
        self.code = code
        self._on_click = on_click
        self._surface = look.SURFACE
        self._hover = look.CHIP_HOVER
        self.grid_columnconfigure(0, weight=1)
        self.title_label = ctk.CTkLabel(
            self,
            text=text,
            height=0,
            font=theme.font(_CODE_LABEL_SIZE, bold=True),
            text_color=theme.TEXT,
            anchor="w",
        )
        self.title_label.grid(row=0, column=0, sticky="ew", padx=_CODE_PAD_X, pady=(4, 0))
        self.example_label = ctk.CTkLabel(
            self,
            text=code_example(code),
            height=0,
            font=theme.font(_CODE_EXAMPLE_SIZE),
            text_color=theme.TEXT_MUTED,
            anchor="w",
        )
        self.example_label.grid(row=1, column=0, sticky="ew", padx=_CODE_PAD_X, pady=(0, 4))
        parts: list[tk.Misc] = [self, *self._descendants(self)]
        for widget in parts:
            # Tk's own option: a CTk widget's configure() keeps it to its inner label.
            widget.tk.call(str(widget), "configure", "-cursor", "hand2")
            tk.Misc.bind(widget, "<Button-1>", self._clicked, add="+")
            tk.Misc.bind(widget, "<Enter>", self._entered, add="+")
            tk.Misc.bind(widget, "<Leave>", self._left, add="+")
        shown = code.strip()  # the dash without its spaces
        tip = t(FOLDER_TIP) if code == FOLDER_INSERT else t(CODE_TIP, code=shown)
        self._tip = add_tooltip(self, tip)

    @classmethod
    def _descendants(cls, widget: tk.Misc) -> list[tk.Misc]:
        found: list[tk.Misc] = []
        # tk.Misc's: a CTkFrame's own leaves out the canvas that is most of the chip.
        for child in tk.Misc.winfo_children(widget):
            found += [child, *cls._descendants(child)]
        return found

    def _clicked(self, _event: tk.Event[Any]) -> str:
        """"break": CTk's click on "all" would give the keyboard to the clicked label, after
        `on_click` put it in the format field. The tooltip's click on "all" is skipped with it."""
        self._tip.hide()
        self._on_click(self.code)
        return "break"

    def _entered(self, _event: tk.Event[Any]) -> None:
        self.configure(fg_color=self._hover)

    def _left(self, event: tk.Event[Any]) -> None:
        left, top = self.winfo_rootx(), self.winfo_rooty()
        inside = (
            left <= event.x_root < left + self.winfo_width()
            and top <= event.y_root < top + self.winfo_height()
        )
        if not inside:  # moving between the chip's own parts is no leave
            self.configure(fg_color=self._surface)


class NamingSection:
    """The Naming page's contents. Changes stay in the form until Save (`problem` / `save`); a
    setting saved elsewhere meanwhile shows here unless the same setting was changed here."""

    def __init__(self, page: SettingsPage, forge: _Starter) -> None:
        # Not at import time (see the top): the Inbox's option texts, the dialog's shared texts.
        from app.forge.ui.queue_view import OPTION_LABELS
        from app.forge.ui.settings_pages import GETTING_READY

        self._page = page
        self._forge = forge
        self._look = look = app_look()
        self._parts: _Parts | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._task: asyncio.Task[None] | None = None
        self._baseline: dict[str, Any] = {}  # what the form showed of the stored settings
        self._stored: list[Preset] = []  # the presets as loaded (or last saved)
        self._drafts: list[PresetDraft] = []
        self._editing: PresetDraft | None = None
        self._choice: dict[str, PresetDraft | str] = {name: "" for name in FIELDS[:2]}
        self._language = "en-US"
        self._language_codes: dict[str, str] = {}  # the menu's names → their codes
        self._matches: list[RememberedMatch] = []
        self._forgotten: set[_MatchKey] = set()
        self._filling = False  # the editor is being filled: its fields' traces change nothing
        self._list_font = theme.font(_TEXT_SIZE, bold=True)  # measures the list's names
        frame = page.frame

        # Shown until Forge has started and the presets are read (or why that can't be).
        self.waiting = page.box()
        self.waiting_label = self.waiting.text(t(GETTING_READY))
        self.waiting.add(self.waiting_label, gap=0, sticky="w")

        # The presets: the list, and the editor of the one chosen in it.
        presets = page.box(t(PRESETS_TITLE), t(PRESETS_TEXT))
        area = presets.frame_row()
        area.grid_columnconfigure(1, weight=1)
        side = ctk.CTkFrame(area, fg_color="transparent")
        side.grid(row=0, column=0, sticky="nw")
        self.preset_list = ctk.CTkFrame(
            side,
            fg_color=look.SURFACE,
            corner_radius=dialog_kit.BUTTON_RADIUS,
            border_width=1,
            border_color=look.PLACEHOLDER,  # theme.PANEL_EDGE in the look
        )
        self.preset_list.grid(row=0, column=0, sticky="ew")
        self.preset_list.grid_columnconfigure(0, weight=1)
        self.preset_buttons: list[ctk.CTkButton] = []
        self.new_button = self._button(side, t(NEW_PRESET), self.new_preset, width=_LIST_WIDTH)
        self.new_button.grid(row=1, column=0, sticky="ew", pady=(_ROW_GAP + 2, 0))

        self.editor = ctk.CTkFrame(area, fg_color="transparent", width=_EDITOR_WIDTH)
        self.editor.grid(row=0, column=1, sticky="new", padx=(_COLUMN_GAP, 0))
        self.editor.grid_columnconfigure(0, weight=1)
        self._editor_row = 0
        self._caption(t(NAME_CAPTION), first=True)
        self._name = tk.StringVar(master=frame)
        self.name_entry = self._entry(self._name)
        self._place(self.name_entry, gap=4)
        self.name_problem = self._note(theme.WARNING)
        self._name_problem_row = self._place(self.name_problem, gap=4, sticky="w")
        self._caption(t(TYPE_CAPTION))
        self.type_switch = ctk.CTkSegmentedButton(
            self.editor,
            values=[t(label) for label in TYPE_LABELS.values()],
            width=240,
            height=dialog_kit.BUTTON_HEIGHT,
            corner_radius=dialog_kit.BUTTON_RADIUS,
            font=theme.font(dialog_kit.DETAIL_SIZE, bold=True),
            fg_color=look.SURFACE,
            selected_color=look.SELECTED,
            selected_hover_color=look.SELECTED_HOVER,
            unselected_color=look.SURFACE,
            unselected_hover_color=look.CHIP_HOVER,
            text_color=theme.BUTTON_TEXT,
            dynamic_resizing=False,
            command=self._type_clicked,
        )
        self._place(self.type_switch, gap=4, sticky="w")
        self._caption(t(FORMAT_CAPTION))
        self._format = tk.StringVar(master=frame)
        self.format_entry = self._entry(self._format)
        self._place(self.format_entry, gap=4)
        self.format_sample = self._note(theme.TEXT_MUTED)
        self._place(self.format_sample, gap=6, sticky="w")
        self.unknown_label = self._note(theme.WARNING)
        self._unknown_row = self._place(self.unknown_label, gap=4, sticky="w")
        self.codes_caption = self._note(theme.TEXT_MUTED, size=dialog_kit.SMALL_SIZE)
        self.codes_caption.configure(text=t(CODES_CAPTION))
        self._place(self.codes_caption, gap=6, sticky="w")
        self.code_chips = ctk.CTkFrame(self.editor, fg_color="transparent")
        self._place(self.code_chips, gap=4)
        self.chips: list[CodeChip] = []
        for column in range(_CODE_COLUMNS):
            self.code_chips.grid_columnconfigure(column, weight=1, uniform="codes")
        for index, (label, code) in enumerate(FORMAT_CODES):
            chip = CodeChip(self.code_chips, look, t(label), code, self.insert_code)
            row_no, column = divmod(index, _CODE_COLUMNS)
            chip.grid(
                row=row_no,
                column=column,
                sticky="ew",
                padx=(_CODE_GAP if column else 0, 0),
                pady=(_CODE_GAP if row_no else 0, 0),
            )
            self.chips.append(chip)
        self._caption(t(EXTRAS_CAPTION))
        checks = ctk.CTkFrame(self.editor, fg_color="transparent")
        self._place(checks, gap=2, sticky="w")
        self._options: dict[str, tk.BooleanVar] = {}
        self.option_boxes: dict[str, ctk.CTkCheckBox] = {}
        for index, (key, label) in enumerate(OPTION_LABELS.items()):
            variable = tk.BooleanVar(master=frame, value=False)
            box = self._checkbox(checks, t(label), variable, self._options_clicked)
            box.grid(row=index // 2, column=index % 2, sticky="w", padx=(0, 12), pady=(2, 0))
            self._options[key] = variable
            self.option_boxes[key] = box
        self.delete_button = self._button(self.editor, t(DELETE_PRESET), self.delete_preset)
        self.delete_button.configure(width=140)
        self._place(self.delete_button, gap=_CAPTION_GAP + 2, sticky="w")
        # Removed when the page goes (_destroyed): Tk holds a trace's callback, out of the
        # garbage collector's sight, and would keep the closed dialog in memory.
        self._traces = [
            (self._name, self._name.trace_add("write", self._name_typed)),
            (self._format, self._format.trace_add("write", self._format_typed)),
        ]

        # Which preset names what goes into the library folders.
        library = page.box(t(LIBRARY_TITLE), t(LIBRARY_TEXT))
        rows = library.frame_row()
        self.choice_menus: dict[str, ctk.CTkOptionMenu] = {}
        for row_no, (name, label) in enumerate(
            (("series_preset", t("Series")), ("movies_preset", t("Movies")))
        ):
            pady = (0 if row_no == 0 else _ROW_GAP, 0)
            ctk.CTkLabel(
                rows,
                text=label,
                width=_LABEL_WIDTH,
                anchor="w",
                font=theme.font(dialog_kit.DETAIL_SIZE, bold=True),
                text_color=theme.TEXT,
            ).grid(row=row_no, column=0, sticky="w", pady=pady)
            menu = self._option_menu(rows, functools.partial(self._choose, name))
            menu.grid(row=row_no, column=1, sticky="w", padx=(8, 0), pady=pady)
            self.choice_menus[name] = menu

        # The names language.
        language = page.box(t(LANGUAGE_TITLE), t(LANGUAGE_TEXT))
        self.language_menu = self._option_menu(language.body, self._language_chosen)
        language.add(self.language_menu, sticky="w")

        # Remembered matches.
        matches = page.box(t(MATCHES_TITLE), t(MATCHES_TEXT))
        self.match_list = ctk.CTkFrame(matches.body, fg_color="transparent")
        self.match_list.grid_columnconfigure(0, weight=1)
        matches.add(self.match_list)
        self.clear_button = self._button(matches.body, t(CLEAR_ALL), self.clear_matches)
        matches.add(self.clear_button, gap=_ROW_GAP + 2, sticky="w")

        self._content: list[SettingsBox] = [presets, library, language, matches]
        for box in self._content:
            box.frame.grid_remove()
        tk.Misc.bind(frame, "<Destroy>", self._destroyed, "+")
        self._task = spawn(self._load(), name="forge-naming-load")

    # --- widgets ---------------------------------------------------------------------------------

    def _button(
        self, master: Any, text: str, command: Callable[[], None], *, width: int = _BUTTON_WIDTH
    ) -> ctk.CTkButton:
        button = dialog_kit.secondary_button(master, text, command, width=width, look=self._look)
        button.configure(text_color=theme.BUTTON_TEXT)
        return button

    def _checkbox(
        self, master: Any, text: str, variable: tk.BooleanVar, command: Callable[[], None]
    ) -> ctk.CTkCheckBox:
        return ctk.CTkCheckBox(
            master,
            text=text,
            variable=variable,
            command=command,
            width=0,
            height=30,
            checkbox_width=20,
            checkbox_height=20,
            corner_radius=5,
            font=theme.font(_TEXT_SIZE),
            fg_color=self._look.ACCENT,
            hover_color=self._look.ACCENT_HOVER,
            border_color=self._look.EDGE,
        )

    def _entry(self, variable: tk.StringVar) -> ctk.CTkEntry:
        return ctk.CTkEntry(
            self.editor,
            textvariable=variable,
            height=dialog_kit.BUTTON_HEIGHT,
            corner_radius=dialog_kit.BUTTON_RADIUS,
            font=theme.font(dialog_kit.DETAIL_SIZE),
            fg_color=self._look.SURFACE,
            border_color=self._look.EDGE,
        )

    def _option_menu(self, master: Any, command: Callable[[str], None]) -> ctk.CTkOptionMenu:
        return ctk.CTkOptionMenu(
            master,
            values=[""],
            command=command,
            width=_MENU_WIDTH,
            height=dialog_kit.BUTTON_HEIGHT,
            corner_radius=dialog_kit.BUTTON_RADIUS,
            font=theme.font(dialog_kit.DETAIL_SIZE),
            dropdown_font=theme.font(dialog_kit.DETAIL_SIZE),
            fg_color=self._look.SURFACE,
            button_color=self._look.EDGE,
            button_hover_color=self._look.HOVER,
            text_color=theme.BUTTON_TEXT,
            dynamic_resizing=False,
        )

    def _note(self, color: theme.Color, *, size: int = _TEXT_SIZE) -> ctk.CTkLabel:
        return ctk.CTkLabel(
            self.editor,
            text="",
            font=theme.font(size),
            text_color=color,
            anchor="w",
            justify="left",
            wraplength=_EDITOR_WIDTH,
        )

    def _caption(self, text: str, *, first: bool = False) -> None:
        caption = ctk.CTkLabel(
            self.editor,
            text=text,
            font=theme.font(dialog_kit.DETAIL_SIZE, bold=True),
            text_color=theme.TEXT,
            anchor="w",
        )
        self._place(caption, gap=0 if first else _CAPTION_GAP, sticky="w")

    def _place(self, widget: Any, *, gap: int, sticky: str = "ew") -> int:
        row = self._editor_row
        self._editor_row += 1
        widget.grid(row=row, column=0, sticky=sticky, pady=(gap, 0))
        return row

    @staticmethod
    def _show_line(label: ctk.CTkLabel, text: str) -> None:
        label.configure(text=text)
        if text:
            label.grid()
        else:
            label.grid_remove()

    def _fitted(self, text: str) -> str:
        """`text` cut with "…" to fit a list button."""
        font = self._list_font
        room = _LIST_WIDTH - 2 * _LIST_PAD - 24  # the button's own padding
        if font.measure(text) <= room:
            return text
        while text and font.measure(text + "…") > room:
            text = text[:-1]
        return text.rstrip() + "…"

    # --- starting ---------------------------------------------------------------------------------

    @property
    def ready(self) -> bool:
        """Whether Forge's presets and settings are showing (Forge has started)."""
        return self._parts is not None

    async def _load(self) -> None:
        parts = self._forge.services
        if parts is None:
            parts = await self._forge.start()
        if not self.waiting_label.winfo_exists():
            return
        if parts is None:
            # NOT_READY (guessit missing) or FAILED: say why; nothing here can be changed.
            from app.forge.ui.settings_pages import GETTING_READY

            self.waiting_label.configure(text=self._forge.reason or t(GETTING_READY))
            return
        try:
            presets = await parts.presets.list()
            matches = await parts.matches.list()
        except Exception as exc:  # forge.db couldn't be read
            log.error("MediaForge's presets couldn't be read (%s)", type(exc).__name__)
            if self.waiting_label.winfo_exists():
                self.waiting_label.configure(text=t(LOAD_FAILED))
            return
        if self.waiting_label.winfo_exists():
            self._ready(parts, presets, matches)

    def _ready(
        self, parts: _Parts, presets: Sequence[Preset], matches: Sequence[RememberedMatch]
    ) -> None:
        self._parts = parts
        if not parts.subtitles.available:  # no key in this version: no Subtitles extra
            self.option_boxes["subtitles"].grid_remove()
        self._stored = list(presets)
        self._drafts = [PresetDraft(preset.name, preset) for preset in presets]
        self._editing = self._drafts[0] if self._drafts else None
        self._matches = list(matches)
        self._load_settings(parts.settings.current)
        self._unsubscribe = parts.settings.subscribe(self._saved_elsewhere)
        first_gap = self.waiting.frame.grid_info().get("pady")
        self.waiting.frame.grid_remove()
        for box in self._content:
            box.frame.grid()
        if first_gap is not None:  # the first box sits where the waiting box sat
            self._content[0].frame.grid_configure(pady=first_gap)
        self._refresh_presets()
        self._fill_language()
        self._fill_matches()

    def _destroyed(self, event: tk.Event[Any]) -> None:
        if event.widget is not self._page.frame:
            return
        if self._task is not None:
            self._task.cancel()
            self._task = None
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        for variable, name in self._traces:
            try:
                variable.trace_remove("write", name)
            except tk.TclError:
                pass  # gone with its interpreter
        self._traces.clear()

    # --- the settings (Series / Movies presets, names language) ----------------------------------

    def _load_settings(self, settings: ForgeSettings) -> None:
        self._baseline = {name: getattr(settings, name) for name in FIELDS}
        for name in FIELDS:
            self._show(name, self._baseline[name])

    def _show(self, name: str, value: Any) -> None:
        if name == "tmdb_language":
            self._language = value
        else:
            self._choice[name] = self._named(value) or value

    def _named(self, name: str) -> PresetDraft | None:
        return next((draft for draft in self._drafts if draft.preset.name == name), None)

    def _choice_name(self, name: str) -> str:
        choice = self._choice[name]
        return choice if isinstance(choice, str) else choice.preset.name

    def draft(self) -> dict[str, Any]:
        """What the form holds of this section's settings, field by field."""
        return {
            "series_preset": self._choice_name("series_preset"),
            "movies_preset": self._choice_name("movies_preset"),
            "tmdb_language": self._language,
        }

    def changes(self) -> dict[str, Any]:
        """The settings changed in the form (what Save stores of ForgeSettings)."""
        if self._parts is None:
            return {}
        return {
            name: value for name, value in self.draft().items() if value != self._baseline[name]
        }

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
        self._fill_choices()
        self._fill_language()

    def _choose(self, name: str, label: str) -> None:
        self._choice[name] = self._named(label) or label

    def _fill_choices(self) -> None:
        names = [draft.preset.name for draft in self._drafts if draft.preset.name]
        for name, menu in self.choice_menus.items():
            menu.configure(values=names or [""])
            menu.set(self._choice_name(name))

    def _fill_language(self) -> None:
        codes = list(LANGUAGES)
        if self._language not in codes:
            codes.append(self._language)  # one MediaForge's free field took: kept as it is
        self._language_codes = {language_name(code): code for code in codes}
        self.language_menu.configure(values=list(self._language_codes))
        self.language_menu.set(language_name(self._language))

    def _language_chosen(self, label: str) -> None:
        self._language = self._language_codes.get(label, self._language)

    def choose_language(self, code: str) -> None:
        """Choose a names language by its code (as picking it in the menu)."""
        self._language_chosen(language_name(code))
        self.language_menu.set(language_name(self._language))

    def choose_preset_for(self, name: str, preset: str) -> None:
        """Choose the preset for "series_preset" / "movies_preset" (as picking it in the menu)."""
        self._choose(name, preset)
        self.choice_menus[name].set(self._choice_name(name))

    # --- the presets ------------------------------------------------------------------------------

    @property
    def presets(self) -> list[Preset]:
        """The presets as the form holds them, in order."""
        return [draft.preset for draft in self._drafts]

    @property
    def editing(self) -> PresetDraft | None:
        """The preset shown in the editor."""
        return self._editing

    def presets_changed(self) -> bool:
        return [(draft.origin, draft.preset) for draft in self._drafts] != [
            (preset.name, preset) for preset in self._stored
        ]

    def _refresh_presets(self) -> None:
        self._fill_preset_list()
        self._fill_editor()
        self._fill_choices()

    def _fill_preset_list(self) -> None:
        for child in self.preset_list.winfo_children():
            child.destroy()
        self.preset_buttons = []
        last = len(self._drafts) - 1
        for row_no, draft in enumerate(self._drafts):
            chosen = draft is self._editing
            button = ctk.CTkButton(
                self.preset_list,
                text=self._fitted(draft.preset.name or "…"),
                anchor="w",
                width=_LIST_WIDTH - 2 * _LIST_PAD,
                height=_ITEM_HEIGHT,
                corner_radius=6,
                fg_color=self._look.SELECTED if chosen else "transparent",
                hover_color=self._look.SELECTED_HOVER if chosen else self._look.CHIP_HOVER,
                text_color=theme.BUTTON_TEXT,
                font=theme.font(_TEXT_SIZE, bold=chosen),
                command=functools.partial(self.select_preset, draft),
            )
            button.grid(
                row=row_no,
                column=0,
                sticky="ew",
                padx=_LIST_PAD,
                pady=(_LIST_PAD if row_no == 0 else 2, _LIST_PAD if row_no == last else 0),
            )
            self.preset_buttons.append(button)

    def _fill_editor(self) -> None:
        draft = self._editing
        if draft is None:
            self.editor.grid_remove()
            return
        self.editor.grid()
        self._filling = True
        try:
            self._name.set(draft.preset.name)
            self.type_switch.set(t(TYPE_LABELS[draft.preset.type]))
            self._format.set(draft.preset.format)
            for key, variable in self._options.items():
                variable.set(bool(getattr(draft.preset.options, key)))
        finally:
            self._filling = False
        self._refresh_editor()

    def _refresh_editor(self) -> None:
        """The sample, an unknown code, the name's problem, Delete (never the last preset)."""
        draft = self._editing
        if draft is None:
            return
        lines, unknown = sample_lines(draft.preset.format, draft.preset.type)
        self.format_sample.configure(text="\n".join(f"↳ {line}" for line in lines))
        codes = ", ".join("{" + code + "}" for code in unknown)
        self._show_line(self.unknown_label, t(UNKNOWN_CODES, codes=codes) if unknown else "")
        self._show_line(self.name_problem, self._name_problem(draft))
        self.delete_button.configure(state="normal" if len(self._drafts) > 1 else "disabled")

    def _name_problem(self, draft: PresetDraft) -> str:
        name = draft.preset.name.casefold()
        if not name:
            return t(NAME_MISSING)
        others = (other.preset.name.casefold() for other in self._drafts if other is not draft)
        if name in others:
            return t(NAME_TAKEN)
        return ""

    def _edit(self, **update: Any) -> None:
        if self._editing is not None:
            self._editing.preset = self._editing.preset.model_copy(update=update)

    def select_preset(self, draft: PresetDraft) -> None:
        """Show `draft` in the editor."""
        self._editing = draft
        self._fill_preset_list()
        self._fill_editor()

    def _name_typed(self, *_args: object) -> None:
        if self._filling or self._editing is None:
            return
        self._edit(name=self._name.get().strip())
        index = self._drafts.index(self._editing)
        self.preset_buttons[index].configure(text=self._fitted(self._editing.preset.name or "…"))
        self._fill_choices()  # a Series / Movies choice follows its preset's new name
        self._refresh_editor()

    def _format_typed(self, *_args: object) -> None:
        if self._filling or self._editing is None:
            return
        self._edit(format=self._format.get().strip() or DEFAULT_FORMAT)
        self._refresh_editor()

    def insert_code(self, code: str) -> None:
        """Put `code` at the format field's cursor, over its selection if it has one, with the
        cursor right after it and the keyboard back in the field. The field's trace does the rest,
        as when typing."""
        if self._editing is None:
            return
        entry = self.format_entry
        if entry.select_present():
            start = entry.index("sel.first")
            entry.delete("sel.first", "sel.last")
            entry.icursor(start)
        position = entry.index("insert")
        entry.insert(position, code)
        entry.icursor(position + len(code))
        entry.focus_set()

    def _type_clicked(self, label: str) -> None:
        if self._filling:
            return
        chosen = next((hint for hint, text in TYPE_LABELS.items() if t(text) == label), None)
        if chosen is not None:
            self._edit(type=chosen)
            self._refresh_editor()

    def set_type(self, type_hint: TypeHint) -> None:
        """Choose the edited preset's type (as clicking it)."""
        self.type_switch.set(t(TYPE_LABELS[type_hint]))
        self._type_clicked(t(TYPE_LABELS[type_hint]))

    def set_option(self, key: str, value: bool) -> None:
        """Tick or untick one of the edited preset's extras (as clicking it)."""
        self._options[key].set(value)
        self._options_clicked()

    def _options_clicked(self) -> None:
        if self._filling:
            return
        self._edit(options=PresetOptions(**{k: bool(v.get()) for k, v in self._options.items()}))

    def new_preset(self) -> None:
        """Add a preset at the end ("New preset", Auto, `{ny}`) and edit it."""
        if self._parts is None:
            return
        name = new_preset_name(draft.preset.name for draft in self._drafts)
        draft = PresetDraft(None, Preset(name=name, type=TypeHint.AUTO, format=DEFAULT_FORMAT))
        self._drafts.append(draft)
        self.select_preset(draft)
        self._fill_choices()
        self.name_entry.focus_set()
        self.name_entry.select_range(0, "end")

    def delete_preset(self) -> None:
        """Delete the edited preset; the last one can't go. A Series / Movies choice it held
        goes to the first preset of that type."""
        draft = self._editing
        if draft is None or len(self._drafts) <= 1:
            return
        index = self._drafts.index(draft)
        self._drafts.pop(index)
        for name, wanted in _CHOICE_TYPES.items():
            if self._choice[name] is draft:
                self._choice[name] = fallback_preset(self._drafts, wanted) or ""
        self._editing = self._drafts[min(index, len(self._drafts) - 1)]
        self._refresh_presets()

    # --- remembered matches ----------------------------------------------------------------------

    @property
    def matches(self) -> list[RememberedMatch]:
        """The remembered matches the form shows (the forgotten ones left out)."""
        return [match for match in self._matches if _match_key(match) not in self._forgotten]

    def _fill_matches(self) -> None:
        for child in self.match_list.winfo_children():
            child.destroy()
        shown = self.matches
        if not shown:
            ctk.CTkLabel(
                self.match_list,
                text=t("None yet"),
                font=theme.font(_TEXT_SIZE),
                text_color=theme.TEXT_MUTED,
                anchor="w",
            ).grid(row=0, column=0, sticky="w")
        for row_no, match in enumerate(shown):
            pady = (0 if row_no == 0 else _ROW_GAP // 2, 0)
            ctk.CTkLabel(
                self.match_list,
                text=match_text(match),
                font=theme.font(_TEXT_SIZE),
                text_color=theme.TEXT,
                anchor="w",
                justify="left",
                wraplength=_MATCH_WRAP,
            ).grid(row=row_no, column=0, sticky="w", pady=pady)
            self._button(
                self.match_list, t(FORGET), functools.partial(self.forget_match, match)
            ).grid(row=row_no, column=1, sticky="e", padx=(10, 0), pady=pady)
        self.clear_button.configure(state="normal" if shown else "disabled")

    def forget_match(self, match: RememberedMatch) -> None:
        """Forget one remembered match (on Save)."""
        self._forgotten.add(_match_key(match))
        self._fill_matches()

    def clear_matches(self) -> None:
        """Forget every remembered match shown (on Save)."""
        self._forgotten.update(_match_key(match) for match in self._matches)
        self._fill_matches()

    # --- Save -------------------------------------------------------------------------------------

    def problem(self) -> str | None:
        """Changed presets need a name each, no name twice. Unchanged presets never stop Save
        (AirGrabber's own settings can still be saved)."""
        if self._parts is None or not self.presets_changed():
            return None
        return preset_problem([draft.preset.name for draft in self._drafts])

    async def save(self) -> str | None:
        parts = self._parts
        if parts is None:
            return None
        from app.forge.ui.settings_pages import SAVE_FAILED

        try:
            saved = await self._save_presets(parts)
            change = None if saved is None else saved[0]
            changes = self.changes()
            if saved is not None:  # the queue's remembered presets follow a rename or delete
                current = parts.settings.current
                for key in ("last_preset", "last_show_preset", "last_film_preset"):
                    last = getattr(current, key)
                    after = last_preset_after(last, saved[0], saved[1], self._stored)
                    if after != last:
                        changes[key] = after
            if changes:
                await parts.settings.update(**changes)
            await self._save_forgotten(parts)
        except Exception as exc:  # a bad value, or forge.db couldn't be written
            # The type only: a message could hold a preset's or a folder's name.
            log.error("MediaForge's naming settings couldn't be saved (%s)", type(exc).__name__)
            return t(SAVE_FAILED)
        if change is not None:
            await _tell(change, parts)
        return None

    async def _save_presets(self, parts: _Parts) -> tuple[PresetsChange, list[Preset]] | None:
        """Store the changed presets: what changed, and the presets as the form had loaded
        them (or None: nothing changed)."""
        if not self.presets_changed():
            return None
        stored = await parts.presets.list()
        before = self._stored
        merged = merged_presets(stored, before, self._drafts)
        if merged != stored:
            await parts.presets.replace_all(merged)
        change = presets_change([preset.name for preset in merged], before, self._drafts)
        self._rebase(merged)
        return change, before

    def _rebase(self, stored: Sequence[Preset]) -> None:
        """Take `stored` as what is stored; the form shows it, keeping its drafts (the editor's
        and the Series / Movies choices) where their names are stored."""
        by_name = {draft.preset.name: draft for draft in self._drafts}
        drafts: list[PresetDraft] = []
        for preset in stored:
            draft = by_name.pop(preset.name, None) or PresetDraft(preset.name, preset)
            draft.origin, draft.preset = preset.name, preset
            drafts.append(draft)
        self._stored = list(stored)
        self._drafts = drafts
        if self._editing not in drafts:
            self._editing = drafts[0] if drafts else None
        for name in self._choice:
            if not isinstance(self._choice[name], str) and self._choice[name] not in drafts:
                self._choice[name] = self._choice_name(name)
        if self.waiting_label.winfo_exists():
            self._refresh_presets()

    async def _save_forgotten(self, parts: _Parts) -> None:
        for match in [m for m in self._matches if _match_key(m) in self._forgotten]:
            await parts.matches.forget(match)
            self._matches.remove(match)
        self._forgotten.clear()


def build_naming(page: SettingsPage, forge: ForgeRuntime) -> ForgeSectionPart:
    """The Naming section's builder (settings_pages.FORGE_SECTIONS)."""
    return NamingSection(page, forge)
