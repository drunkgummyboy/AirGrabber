"""Several episodes of one tracked show on one day: one stack card instead of a card each.

The calendar view groups a day's tracked episodes per show (`stack_entries`); a show with two
or more that day gets a stack card: the wide `StackCard` (recent days and later) or the slim
`CompactStackCard` (older days). Suggestions are never stacked.

`StackCard` is the wide episode card (`EpisodeCard`, app/ui/calendar_cards.py) with two card
edges peeking out behind it at the bottom right (app/ui/calendar_face.py bakes their corners),
the episodes' range under the name ("S01E07 – E09", `range_text`), a blue pill "3 episodes ▾"
and the bottom row: the poster cards' row (`stack_look`'s main button) with the round ⋯ beside
it (a menu of the episodes, each opening its search window: `episode_menu`). Clicking the pill
unfolds a row per episode inside the card (the pill turns "▴"): its code with the compact
cards' round buttons (`SlimDownload`: ⬇ with ⋯, …) and under it its state ("not aired"; with
Plex "✓ in Plex", amber "still needed", blue "new": `row_state`); a right-click on a row opens
the search window. Clicking the pill again folds them. The view remembers the unfolded stacks
for the session (`on_fold`), so a reload keeps them open.

What you have and miss shows only while Plex is connected (`marks`; owner's decision for 3.1.0,
app/services/plex.py), and only Plex counts as had: an episode grabbed by AirGrabber that Plex
doesn't have yet is on its way, neither ✓ nor still needed. With Plex the bottom row is
"⬇ Download all (2)" while episodes are still needed (aired, neither in Plex nor grabbed:
`needed_entries`); it fetches them one after another through the one-click download
(`DownloadActions.download_all`: the main window queues them as one batch in the download
queue, app/ui/queue_link.py), so each episode keeps its own state everywhere (the show window
too); meanwhile the row shows "Downloading 2 of 3…" (the batch's `BatchState`, which a card
rebuilt mid-run picks up as well), afterwards the usual outcome. Otherwise (and always without
Plex) the row shows only the queue's states and a finished run's outcome, nothing else.

`CompactStackCard` is one slim line: the name, the range and a blue "3 episodes" pill (just "3"
on a narrow line, `slim_badge`), then a round ⬇ (with Plex: download all still needed), … while
one works, or a run's ✓. Clicking the name opens the show's window; a right-click (or the ✓)
opens the menu of the episodes.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Any, Literal, cast

import customtkinter as ctk

from app.i18n import N_, t, tn
from app.models import CalendarEntry, Episode, Show
from app.ui import theme
from app.ui.calendar_cards import (
    GRABBED_ICON,
    SLIM_BUTTONS_GAP,
    SLIM_CODE_GAP,
    SLIM_ICON,
    SLIM_LINE,
    SLIM_PAD_RIGHT,
    SLIM_PAD_X,
    SLIM_PAD_Y,
    SLIM_RADIUS,
    WORKING_ICON,
    _CODE_SIZE,
    _ICON_SIZE,
    _NAME_SIZE,
    CanvasPill,
    EpisodeActions,
    EpisodeCard,
    HaveMark,
    IconLook,
    OpenShow,
    PillLabel,
    RoundIcon,
    SlimDownload,
    _descendants,
    _font_linespace,
    _linespace,
    _note_pill,
    _open_on_click,
    _pack_note,
    _rgb,
    _scaling,
    buttons_width,
    card_note,
    compact_height,
    fit_note,
    with_source,
)
from app.ui.calendar_face import (
    PILL_PAD_X,
    EpisodeLayout,
    StackEdges,
    episode_layout,
    stack_edges,
    stack_step_px,
)
from app.ui.card_scale import FULL, SMALL_TEXT_MIN, TITLE_MIN, CardScale
from app.ui.download_control import (
    DOWNLOAD_SHORT,
    QUEUED_TEXT,
    WORKING_SHORT,
    WORKING_TEXT,
    Base,
    BatchKey,
    BatchState,
    DownloadControl,
    DownloadKey,
    Look,
    Phase,
    RunState,
    episode_key,
    fit_text,
    outcome_text,
)
from app.ui.missing_control import MISSING_COLOR, SEASON_MENU_TIP, popup_menu, progress_texts
from app.ui.text_fit import SLACK, elide_to_width, unscaled_width
from app.ui.tooltip import add_tooltip

if TYPE_CHECKING:
    from app.services import ImageCache

FoldKey = tuple[int, date]  # (show id, air date): which stacks are unfolded
StateOf = Callable[[DownloadKey], RunState | None]  # a title's one-click run state
# "quiet": nothing to say (every aired episode without Plex). "downloaded": with Plex, one grabbed
# by AirGrabber that Plex doesn't have yet, on its way.
RowState = Literal["not_aired", "plex", "downloaded", "new", "needed", "quiet"]

# The texts are shown through t().
DOWNLOAD_ALL_TEXT = N_("⬇ Download all ({n})")
DOWNLOAD_ALL_SHORTER = (N_("⬇ Download ({n})"), N_("⬇ All ({n})"))  # when it doesn't fit
ROW_TEXTS: dict[RowState, str] = {
    "not_aired": N_("not aired"),
    "plex": N_("✓ in Plex"),  # in the user's Plex libraries (app/services/plex.py)
    "downloaded": N_("✓ downloaded"),
    "new": N_("new"),
    "needed": N_("still needed"),
}
FOLDED_ARROW = "▾"
UNFOLDED_ARROW = "▴"


def pill_color() -> theme.Color:
    """The "3 episodes ▾" pill and the compact card's "3 episodes" pill: blue, information.
    Read when drawn (the palette is set at startup)."""
    return theme.BRAND


# The unfolded rows (XL units; the buttons keep the compact cards' size): the gap under the pill,
# then per episode a line with its code and small buttons and a line with its state.
ROWS_GAP = 6
EPISODE_LINE = SLIM_ICON
EPISODE_ROW_GAP = 4  # between two episodes
CODE_BUTTONS_GAP = 6  # at least, between an episode's code and its buttons
# The compact card's "3 episodes" pill.
BADGE_GAP = 4  # between the range and the pill
BADGE_HEIGHT = 16
_BADGE_SIZE = 9  # bold
SLIM_NAME_MIN = 48  # on a narrow compact stack card the range gives way before the name


# -- the rules (pure) ------------------------------------------------------------------------


def entry_key(entry: CalendarEntry) -> DownloadKey:
    """The episode's one-click download key."""
    episode = entry.episode
    return episode_key(entry.show.id, episode.season, episode.number)


def _episode_order(entry: CalendarEntry) -> tuple[int, bool, int]:
    episode = entry.episode
    return episode.season, episode.number is None, episode.number or 0


def stack_entries(entries: Iterable[CalendarEntry]) -> list[tuple[CalendarEntry, ...]]:
    """A day's tracked episodes as its cards: one per show, the shows in the order they first
    come, each show's episodes in episode order (specials last). A card of more than one
    episode is a stack."""
    shows: dict[int, list[CalendarEntry]] = {}
    for entry in entries:
        shows.setdefault(entry.show.id, []).append(entry)
    return [tuple(sorted(group, key=_episode_order)) for group in shows.values()]


@dataclass(frozen=True, slots=True)
class Stack:
    """The episodes of one show on one day, in episode order (`stack_entries`)."""

    entries: tuple[CalendarEntry, ...]

    @property
    def show(self) -> Show:
        return self.entries[0].show

    @property
    def keys(self) -> BatchKey:
        """The episodes' one-click download keys: the key of the stack's "Download all"."""
        return tuple(entry_key(entry) for entry in self.entries)

    @property
    def fold_key(self) -> FoldKey:
        return self.show.id, self.entries[0].episode.air_date or date.min

    @property
    def range(self) -> str:
        return range_text([entry.episode for entry in self.entries])


def range_text(episodes: Sequence[Episode]) -> str:
    """The episodes' codes in short: "S01E07 – E09" for a run in one season, "S01E07, E09" (or
    "S01E07 – E09, E11") with gaps, "S01E10 – S02E01" across seasons (or with a special)."""
    if not episodes:
        return ""
    first, last = episodes[0], episodes[-1]
    if len(episodes) == 1:
        return first.code
    numbers = [episode.number for episode in episodes]
    if len({episode.season for episode in episodes}) > 1 or None in numbers:
        return f"{first.code} – {last.code}"
    runs: list[list[int]] = []
    for number in sorted({number for number in numbers if number is not None}):
        if runs and number == runs[-1][-1] + 1:
            runs[-1].append(number)
        else:
            runs.append([number])
    parts: list[str] = []
    for index, run in enumerate(runs):
        start = f"S{first.season:02d}E{run[0]:02d}" if index == 0 else f"E{run[0]:02d}"
        parts.append(start if len(run) == 1 else f"{start} – E{run[-1]:02d}")
    return ", ".join(parts)


def is_needed(entry: CalendarEntry, state: RunState | None, in_plex: Collection[int]) -> bool:
    """Aired and neither in Plex (`in_plex`: episode ids) nor grabbed (before: the download
    history, or by a run that just finished). Without Plex nothing is in it: then this only
    keeps the episodes grabbed before from being fetched again, and is never shown."""
    return (
        entry.episode.has_aired()
        and entry.episode.id not in in_plex
        and not entry.is_downloaded
        and not (state is not None and state.phase is Phase.DONE)
    )


def row_state(
    entry: CalendarEntry,
    state: RunState | None,
    in_plex: Collection[int],
    new: Collection[int] = frozenset(),
    *,
    marks: bool = False,
    downloaded: Collection[int] = frozenset(),
) -> RowState:
    """An episode's state in its unfolded row: not aired; without Plex (`marks` False) nothing
    more; with Plex: in Plex (`in_plex`: episode ids), still needed (`is_needed`), unless aired
    less than a day ago (`new`: episode ids; a quiet blue "new", not missing yet); downloaded
    and not in Plex yet (`downloaded`: episode ids, or a run that just grabbed it)."""
    if not entry.episode.has_aired():
        return "not_aired"
    if not marks:
        return "quiet"
    if entry.episode.id in in_plex:
        return "plex"
    if entry.episode.id in downloaded or (state is not None and state.phase is Phase.DONE):
        return "downloaded"
    if not is_needed(entry, state, in_plex):
        return "quiet"
    if entry.episode.id in new:
        return "new"
    return "needed"


def row_color(kind: RowState) -> theme.Color:
    """Amber for still needed, blue for new, else muted."""
    if kind == "needed":
        return MISSING_COLOR
    if kind == "new":
        return theme.LINK  # blue: new
    return theme.TEXT_MUTED


def row_text(kind: RowState) -> str:
    """"✓ in Plex", "still needed"...; "" for nothing to say."""
    return t(ROW_TEXTS[kind]) if kind != "quiet" else ""


def needed_entries(
    entries: Iterable[CalendarEntry], state_of: StateOf, in_plex: Collection[int]
) -> tuple[CalendarEntry, ...]:
    """The episodes "⬇ Download all" (and a season's fallback) fetches: still needed
    (`is_needed`, new ones too), with a number to download by (a special is searched for in
    its own row)."""
    return tuple(
        entry
        for entry in entries
        if entry.episode.number is not None
        and is_needed(entry, state_of(entry_key(entry)), in_plex)
    )


def download_all_texts(count: int) -> tuple[str, ...]:
    """"⬇ Download all (2)" and its shorter forms, longest first."""
    shorter = tuple(t(text, n=count) for text in DOWNLOAD_ALL_SHORTER)
    return (t(DOWNLOAD_ALL_TEXT, n=count), *shorter, f"{DOWNLOAD_SHORT} {count}")


# A stack's bottom row with nothing to say (no button, no text).
QUIET_LOOK_TEXT = ""


def stack_look(
    entries: Sequence[CalendarEntry],
    state_of: StateOf,
    batch: BatchState | None,
    in_plex: Collection[int],
    *,
    marks: bool = False,
) -> Look:
    """A stack's bottom row: "Downloading 2 of 3…" while its "Download all" runs, the working
    label while one of its episodes is fetched on its own, with Plex (`marks`) "⬇ Download all
    (2)" while episodes are still needed, disabled "Not Aired" when none has aired, the
    outcome once every aired one was grabbed by a run (a click unfolds the rows: "choose"),
    else an empty label (nothing to say: without Plex it never tells what you have or miss).
    "Queued" while they wait in the download queue (the batch, or an episode on its own)."""
    if batch is not None and batch.queued:
        return Look("label", t(QUEUED_TEXT), text_color=theme.TEXT_MUTED)
    if batch is not None:
        first, *rest = progress_texts(batch.index, batch.total)
        return Look("label", first, text_color=theme.TEXT_MUTED, short=tuple(rest))
    states = [state_of(entry_key(entry)) for entry in entries]
    if any(state is not None and state.phase is Phase.WORKING for state in states):
        return Look(
            "label", t(WORKING_TEXT), text_color=theme.TEXT_MUTED, short=(t(WORKING_SHORT),)
        )
    if any(state is not None and state.phase is Phase.QUEUED for state in states):
        return Look("label", t(QUEUED_TEXT), text_color=theme.TEXT_MUTED)
    needed = len(needed_entries(entries, state_of, in_plex)) if marks else 0
    if needed:
        text, *short = download_all_texts(needed)
        return Look("button", text, action="download", short=tuple(short))
    aired = [state for entry, state in zip(entries, states) if entry.episode.has_aired()]
    if not aired:
        return Look("button", t("Not Aired"), theme.NOT_AIRED, theme.NOT_AIRED)
    outcomes = {
        state.outcome if state is not None and state.phase is Phase.DONE else None
        for state in aired
    }
    outcome = outcomes.pop() if len(outcomes) == 1 else None
    if outcome is not None:
        return Look("button", outcome_text(outcome), theme.GRABBED, action="choose")
    return Look("label", QUIET_LOOK_TEXT, text_color=theme.TEXT_MUTED)


def pill_texts(count: int, unfolded: bool) -> tuple[str, str]:
    """The pill: "3 episodes ▾" ("▴" while unfolded), and "3 ▾" for when that doesn't fit."""
    arrow = UNFOLDED_ARROW if unfolded else FOLDED_ARROW
    return f"{tn('{n} episode', '{n} episodes', count)} {arrow}", f"{count} {arrow}"


def stack_icon(look: Look) -> IconLook:
    """The compact stack card's small button for its row's `look`: … while working (the tip
    says what), ⬇ = download all still needed, ✓ once a run grabbed them all (a click lists the
    episodes), none before any has aired or with nothing to say."""
    if look.layout == "label":
        if not look.text:
            return IconLook("")
        return IconLook(WORKING_ICON, look.text, "transparent", theme.SURFACE, theme.TEXT_MUTED)
    if look.action == "download":
        return IconLook(
            DOWNLOAD_SHORT, look.text, look.color, look.hover, look.text_color, action="download"
        )
    if look.action == "choose":
        return IconLook(GRABBED_ICON, look.text, look.color, look.hover, action="choose")
    return IconLook("")


def menu_label(
    entry: CalendarEntry,
    state: RunState | None,
    in_plex: Collection[int],
    *,
    marks: bool = False,
    downloaded: Collection[int] = frozenset(),
) -> str:
    """An episode in the compact stack card's menu: "S01E07 · ✓ in Plex", or just "S01E07"
    with nothing to say."""
    text = row_text(row_state(entry, state, in_plex, marks=marks, downloaded=downloaded))
    return f"{entry.episode.code} · {text}" if text else entry.episode.code


def more_fits(room: float, code_width: float, look: IconLook) -> bool:
    """Whether an unfolded row (`room` wide) has room for its buttons in `look`, the ⋯ beside
    the ⬇ included, after its code (`code_width`). Without, the ⋯ stays away (a right-click
    still opens the search window) rather than the code being cut short."""
    return room - buttons_width(look) - CODE_BUTTONS_GAP >= code_width


def rows_height(count: int, state_line: int, scale: CardScale = FULL) -> int:
    """How much taller the unfolded rows make a stack card, in CTk units: the gap under the
    pill, then `count` rows of the buttons' line and the state's line (`state_line` tall)."""
    if count <= 0:
        return 0
    row = EPISODE_LINE + state_line
    return scale.px(ROWS_GAP) + count * row + (count - 1) * scale.px(EPISODE_ROW_GAP)


def slim_stack_rooms(
    row_width: float, range_width: float, badge_width: float, icon_width: float
) -> tuple[float, float]:
    """The room the compact stack card's name and range have in its line, `row_width` units
    wide: what the badge, the gaps and the small button leave, the range in full while the
    name keeps SLIM_NAME_MIN; on a narrower line the range gives way first."""
    name = (
        row_width
        - SLIM_CODE_GAP
        - range_width
        - BADGE_GAP
        - badge_width
        - SLIM_BUTTONS_GAP
        - icon_width
        - SLACK
    )
    if name >= SLIM_NAME_MIN:
        return name, range_width
    give = min(SLIM_NAME_MIN - name, range_width)
    return name + give, range_width - give


def badge_texts(count: int) -> tuple[str, str]:
    """The compact stack card's pill: "3 episodes", and "3" for a narrow line."""
    return tn("{n} episode", "{n} episodes", count), str(count)


def slim_badge(
    row_width: float, range_width: float, icon_width: float, badge_widths: Sequence[float]
) -> int:
    """Which of the compact stack card's pills (`badge_widths`, longest first) its line,
    `row_width` units wide, has room for: the first that leaves the range in full and the name
    its least room (`slim_stack_rooms`); else the last."""
    for index, width in enumerate(badge_widths):
        _name, range_room = slim_stack_rooms(row_width, range_width, width, icon_width)
        if range_room >= range_width:
            return index
    return len(badge_widths) - 1


MenuItems = list[tuple[str, Callable[[], None]]]


def episode_menu(
    stack: Stack,
    actions: EpisodeActions,
    in_plex: Collection[int],
    *,
    marks: bool = False,
    downloaded: Collection[int] = frozenset(),
) -> MenuItems:
    """The stack's episodes as a menu (the wide card's ⋯, the compact card's right-click): one
    item per episode (`menu_label`), opening its search window."""
    states, choose = actions.states, actions.choose

    def opener(entry: CalendarEntry) -> Callable[[], None]:
        return lambda: choose(entry)

    return [
        (
            menu_label(
                entry, states.get(entry_key(entry)), in_plex, marks=marks, downloaded=downloaded
            ),
            opener(entry),
        )
        for entry in stack.entries
    ]


def start_download_all(
    stack: Stack, actions: EpisodeActions, in_plex: Collection[int]
) -> None:
    """"⬇ Download all": the episodes still needed, one after another."""
    needed = needed_entries(stack.entries, actions.states.get, in_plex)
    if not needed:
        return
    if actions.download_all is not None:
        actions.download_all(stack.keys, needed)
    else:  # not offered: each on its own
        for entry in needed:
            actions.download(entry)


# -- widgets ----------------------------------------------------------------------------------


class _Watch:
    """Repaints a stack's widget when one of its episodes' run states changes: DownloadStates
    keeps one per key, and only uses its `key`, `repaint()` and `winfo_exists()`."""

    def __init__(self, key: DownloadKey, owner: Any) -> None:
        self.key = key
        self._owner = owner

    def repaint(self) -> None:
        self._owner.repaint()

    def winfo_exists(self) -> bool:
        try:
            return bool(self._owner.winfo_exists())
        except tk.TclError:
            return False


def _as_control(watch: _Watch) -> DownloadControl[Any]:
    """DownloadStates keeps DownloadControls, but only uses what `_Watch` has as well."""
    return cast(DownloadControl[Any], watch)


def _watch(stack: Stack, actions: EpisodeActions, owner: Any) -> list[_Watch]:
    watches = [_Watch(key, owner) for key in stack.keys]
    for watch in watches:
        actions.states.attach(_as_control(watch))
    return watches


def _unwatch(watches: Iterable[_Watch], actions: EpisodeActions) -> None:
    for watch in watches:
        actions.states.detach(_as_control(watch))


class StackControl(DownloadControl[CalendarEntry]):
    """A stack card's main button (`stack_look`), the poster cards' row: with Plex "⬇ Download
    all (2)", the batch's progress, or a run's outcome that unfolds the rows (`on_choose`). The
    card's round ⋯ sits beside it."""

    def __init__(
        self,
        master: Any,
        stack: Stack,
        *,
        in_plex: Collection[int],
        actions: EpisodeActions,
        scale: CardScale = FULL,
        on_choose: Callable[[], None] | None = None,
        marks: bool = False,
    ) -> None:
        self._stack = stack
        self._in_plex = in_plex
        self._marks = marks
        self._on_choose = on_choose
        # Whether it shows nothing (QUIET_LOOK_TEXT); `on_blank` hears a change (the card's ⋯).
        self.blank = False
        self.on_blank: Callable[[], None] | None = None
        super().__init__(
            master,
            item=stack.entries[0],
            key=stack.keys[0],
            base=Base("download"),
            actions=actions,
            scale=scale,
        )

    def current_look(self) -> Look:
        states = self._actions.states
        look = stack_look(
            self._stack.entries,
            states.get,
            states.batch(self._stack.keys),
            self._in_plex,
            marks=self._marks,
        )
        blank = look.layout == "label" and look.text == QUIET_LOOK_TEXT
        if blank != self.blank:
            self.blank = blank
            if self.on_blank is not None:
                self.on_blank()
        return look

    def _click(self) -> None:
        look = self._look
        if look is None or look.action is None:
            return
        if look.action == "download":
            start_download_all(self._stack, self._actions, self._in_plex)
        elif self._on_choose is not None:
            self._on_choose()

    def _update_tip(self, _event: Any = None) -> None:
        look = self._look
        if look is not None and look.action == "download":
            super()._update_tip()  # what a one-click download looks for
        else:
            self._main_tip.set_text("")


@dataclass(slots=True)
class _EpisodeRow:
    entry: CalendarEntry
    code: int  # canvas text items
    state: int
    buttons: SlimDownload


class StackCard(EpisodeCard):
    """Several episodes of one show on one day as one wide card (see the module docstring).
    `marks`: Plex is connected (what you have and miss shows); `in_plex`: the ids of the
    episodes in Plex ("✓ in Plex"), `new`: those aired less than a day ago, `downloaded`: those
    downloaded and not in Plex yet; `have`: the note's pill when that goes for all of them;
    `unfolded`: the rows open from the start; `on_fold(open)` hears every fold and unfold."""

    def __init__(
        self,
        master: Any,
        stack: Stack,
        *,
        images: ImageCache,
        actions: EpisodeActions,
        scale: CardScale = FULL,
        on_open_show: OpenShow | None = None,
        unfolded: bool = False,
        on_fold: Callable[[bool], None] | None = None,
        marks: bool = False,
        in_plex: Collection[int] = frozenset(),
        new: Collection[int] = frozenset(),
        downloaded: Collection[int] = frozenset(),
        have: HaveMark | None = None,
    ) -> None:
        # What the overridden steps of EpisodeCard's set-up read.
        self._stack = stack
        self._marks = marks
        self._in_plex = frozenset(in_plex)
        self._new_ids = frozenset(new)
        self._downloaded = frozenset(downloaded)
        self._unfolded = unfolded
        self._scale = scale
        self._rows: list[_EpisodeRow] = []
        self._ready = False
        self._placing = False
        super().__init__(
            master,
            stack.entries[0],
            images=images,
            actions=actions,
            scale=scale,
            on_open_show=on_open_show,
            have=have,
        )
        self._on_fold = on_fold
        self._code_text = with_source(stack.range, stack.entries[0].show)
        self._row_font = theme.font(self._layout.code_size)
        self._row_code_font = theme.font(self._layout.code_size, bold=True)

        canvas = self._art
        # The cards behind it: their straight stretch along the bottom, under everything (the
        # baked poster piece and cap hold their ends). Farthest first: fill, edge line.
        self._behind = [canvas.create_rectangle(0, 0, 0, 0, outline="", width=0) for _ in range(4)]
        for item in reversed(self._behind):
            canvas.tag_lower(item)
        self._count_pill = CanvasPill(canvas, tags=("pill",))
        self._pill = self._count_pill.image
        self._pill_text = self._count_pill.text
        canvas.tag_bind("pill", "<Button-1>", lambda _event: self.toggle())
        canvas.tag_bind("pill", "<Enter>", lambda _event: canvas.configure(cursor="hand2"))
        canvas.tag_bind("pill", "<Leave>", lambda _event: canvas.configure(cursor=""))
        self._watches = _watch(stack, actions, self)
        if isinstance(self._control, StackControl):
            self._control.on_blank = self._sync_menu
            self._sync_menu()
        self._ready = True
        if unfolded:
            self._make_rows()
        self._paint_stack()

    @property
    def unfolded(self) -> bool:
        return self._unfolded

    @property
    def rows(self) -> list[SlimDownload]:
        """The unfolded rows' buttons, in episode order (none while folded)."""
        return [row.buttons for row in self._rows]

    # -- EpisodeCard's steps, for a stack ------------------------------------------------------

    def _card_layout(self, scale: CardScale, noted: bool) -> EpisodeLayout:
        return episode_layout(scale, linespace=_font_linespace, stack=True, note=noted)

    def _card_height(self, layout: EpisodeLayout) -> int:
        height = layout.height + layout.stack_depth
        if self._unfolded:
            state_line = _font_linespace(layout.code_size, False)
            height += rows_height(len(self._stack.entries), state_line, self._scale)
        return height

    def _make_control(
        self, master: ctk.CTkFrame, entry: CalendarEntry
    ) -> DownloadControl[CalendarEntry] | None:
        if not any(item.episode.has_aired() for item in self._stack.entries):
            return None  # "Not aired" instead
        return StackControl(
            master,
            self._stack,
            in_plex=self._in_plex,
            actions=self._actions,
            scale=self._layout.row_scale,
            on_choose=self.toggle,
            marks=self._marks,
        )

    def _wants_menu(self) -> bool:
        return self._control is not None  # the episodes, whatever the main button says

    def _menu_alone(self) -> bool:
        control = self._control
        return isinstance(control, StackControl) and control.blank

    def _menu_tip(self) -> str:
        return t(SEASON_MENU_TIP)

    def _menu_clicked(self) -> None:
        """The ⋯: the menu of the episodes, under it."""
        popup_menu(self.menu_button or self, self.menu_items())

    def menu_items(self) -> MenuItems:
        """The ⋯'s menu (`episode_menu`)."""
        return episode_menu(
            self._stack,
            self._actions,
            self._in_plex,
            marks=self._marks,
            downloaded=self._downloaded,
        )

    def _stack_edges(self, scaling: float) -> StackEdges | None:
        return stack_edges(
            self._layout,
            scaling,
            fills=(_rgb(self, theme.STACK_NEAR), _rgb(self, theme.STACK_FAR)),
            edges=(_rgb(self, theme.STACK_NEAR_EDGE), _rgb(self, theme.STACK_FAR_EDGE)),
        )

    def _place_behind(self) -> None:
        if not self._ready:
            return
        canvas, scaling = self._art, self._scaling
        width = self._size[0]
        front_height = self._front_size()[1]
        step = stack_step_px(self._layout, scaling)
        border = max(1, round(self._layout.border * scaling))
        far_fill, far_edge, near_fill, near_edge = self._behind
        for fill, edge, reach in (
            (far_fill, far_edge, 2 * step),
            (near_fill, near_edge, step),
        ):
            bottom = front_height + reach
            canvas.coords(fill, 0, front_height, width, bottom)
            canvas.coords(edge, 0, bottom - border, width, bottom)

    def _set_appearance_mode(self, mode_string: str) -> None:
        self._paint_stack()  # before EpisodeCard's, which places the texts and pills again
        super()._set_appearance_mode(mode_string)

    def _paint_stack(self) -> None:
        """The stack's canvas colours in the current mode (the pill's and the rows' state
        colours: in `_place_texts`)."""
        canvas, pick = self._art, theme.pick
        far_fill, far_edge, near_fill, near_edge = self._behind
        canvas.itemconfigure(far_fill, fill=pick(theme.STACK_FAR))
        canvas.itemconfigure(far_edge, fill=pick(theme.STACK_FAR_EDGE))
        canvas.itemconfigure(near_fill, fill=pick(theme.STACK_NEAR))
        canvas.itemconfigure(near_edge, fill=pick(theme.STACK_NEAR_EDGE))
        for row in self._rows:
            canvas.itemconfigure(row.code, fill=pick(theme.TEXT))

    # -- the pill and the rows -----------------------------------------------------------------

    def toggle(self) -> None:
        """Unfold the rows, or fold them away."""
        self._unfolded = not self._unfolded
        if self._unfolded:
            self._make_rows()
        else:
            self._drop_rows()
        self.configure(height=self._card_height(self._layout))
        if self._size[0] > 1:
            self._place_texts()
        if self._on_fold is not None:
            self._on_fold(self._unfolded)

    def repaint(self) -> None:
        """An episode's run state changed: the bottom row and the rows' states follow."""
        if self._control is not None:
            self._control.repaint()
        if self._size[0] > 1:
            self._place_texts()

    def _make_rows(self) -> None:
        canvas, actions = self._art, self._actions
        for index, entry in enumerate(self._stack.entries):
            tag = f"row{index}"
            code = canvas.create_text(0, 0, anchor="w", text="", tags=("rows", tag))
            state = canvas.create_text(0, 0, anchor="nw", text="", tags=("rows", tag))
            buttons = SlimDownload(self, entry, actions)

            def choose(_event: Any = None, entry: CalendarEntry = entry) -> None:
                actions.choose(entry)

            canvas.tag_bind(tag, "<Button-3>", choose)
            widgets: list[tk.Misc] = [buttons, *_descendants(buttons)]
            for widget in widgets:
                tk.Misc.bind(widget, "<Button-3>", choose, add="+")
            buttons.on_change = self._buttons_changed  # ✓ needs less room than ⬇ with ⋯
            self._rows.append(_EpisodeRow(entry, code, state, buttons))
        self._paint_stack()

    def _drop_rows(self) -> None:
        self._art.delete("rows")
        for row in self._rows:
            row.buttons.destroy()
        self._rows = []

    def _buttons_changed(self) -> None:
        if not self._placing and self._size[0] > 1:
            self._place_texts()

    def _place_texts(self) -> None:
        """EpisodeCard's texts (the range in place of the code), then the pill under them and
        the unfolded rows under the pill."""
        super()._place_texts()
        if not self._ready or not self.winfo_exists():
            return
        self._placing = True  # a row's buttons changing meanwhile needs no second pass
        try:
            self._place_stack()
        finally:
            self._placing = False

    def _place_stack(self) -> None:
        """The pill under the range (and its note's pill), and the unfolded rows under it."""
        layout, canvas, scaling = self._layout, self._art, self._scaling
        room = self._text_room()
        x = round(self._area.text_x * scaling)
        above = self.note_bottom()
        if above is None:
            box = canvas.bbox(self._code)
            above = box[3] if box else round(layout.pad_top * scaling)
        top = above + round(layout.pill_gap * scaling)

        full, short = pill_texts(len(self._stack.entries), self._unfolded)
        text = fit_text([full, short], room - 2 * PILL_PAD_X, self._pill_font.measure)
        size = self._count_pill.draw(
            x,
            top,
            text,
            font=self._pill_font,
            height=layout.pill_height,
            scaling=scaling,
            color=pill_color(),
        )

        if not self._rows:
            return
        state_line = _linespace(self._row_font)
        step = EPISODE_LINE + state_line + self._scale.px(EPISODE_ROW_GAP)
        first = (top + size[1]) / scaling + self._scale.px(ROWS_GAP)  # CTk units
        right = layout.pad_right + layout.stack_depth
        code_font = self._row_code_font.create_scaled_tuple(scaling)
        state_font = self._row_font.create_scaled_tuple(scaling)
        states = self._actions.states
        for index, row in enumerate(self._rows):
            row_top = first + index * step
            y = round(row_top * scaling)
            code = row.entry.episode.code
            measure = self._row_code_font.measure
            if row.buttons.look is not None:
                row.buttons.allow_more(more_fits(room, measure(code), row.buttons.look))
            code_room = room - row.buttons.width_units - CODE_BUTTONS_GAP
            canvas.itemconfigure(
                row.code, text=elide_to_width(code, code_room, measure), font=code_font
            )
            canvas.coords(row.code, x, y + round(EPISODE_LINE * scaling / 2))
            kind = row_state(
                row.entry,
                states.get(entry_key(row.entry)),
                self._in_plex,
                self._new_ids,
                marks=self._marks,
                downloaded=self._downloaded,
            )
            canvas.itemconfigure(
                row.state,
                text=elide_to_width(row_text(kind), room, self._row_font.measure),
                font=state_font,
                fill=theme.pick(row_color(kind)),
            )
            canvas.coords(row.state, x, y + round(EPISODE_LINE * scaling))
            row.buttons.place(relx=1.0, x=-right, y=row_top, anchor="ne")

    def destroy(self) -> None:
        _unwatch(self._watches, self._actions)
        super().destroy()


class CompactStackCard(ctk.CTkFrame):
    """Several episodes of one show on an older day, on one slim line: the name and the range
    with a blue "3 episodes" pill (`badge`; "3" on a narrow line), then a round ⬇ (with Plex:
    download all still needed), … or a run's ✓. Clicking the name opens the show's window; a
    right-click, or the ✓, opens the menu of the episodes (`episode_menu`). `missing` (with
    Plex): the amber note's pill under it; `marks`, `in_plex`, `new`, `downloaded`, `have`: as
    StackCard's."""

    def __init__(
        self,
        master: Any,
        stack: Stack,
        *,
        actions: EpisodeActions,
        scale: CardScale = FULL,
        on_open_show: OpenShow | None = None,
        missing: bool = False,
        marks: bool = False,
        in_plex: Collection[int] = frozenset(),
        new: Collection[int] = frozenset(),
        downloaded: Collection[int] = frozenset(),
        have: HaveMark | None = None,
    ) -> None:
        any_new = any(entry.episode.id in new for entry in stack.entries)
        noted = card_note(missing=missing, new=any_new, have=have)
        height = compact_height(noted is not None)
        super().__init__(
            master,
            fg_color=theme.SURFACE,
            border_color=theme.EDGE,
            border_width=1,
            corner_radius=SLIM_RADIUS,
            height=height,
        )
        self.pack_propagate(False)
        self._stack = stack
        self._actions = actions
        self._marks = marks
        self._in_plex = frozenset(in_plex)
        self._downloaded = frozenset(downloaded)
        self._look: IconLook | None = None
        show = stack.show
        line = ctk.CTkFrame(self, fg_color="transparent", height=SLIM_LINE)
        line.pack(side="top", fill="x", padx=(SLIM_PAD_X, SLIM_PAD_RIGHT), pady=(SLIM_PAD_Y, 0))
        line.pack_propagate(False)
        self.main = RoundIcon(
            line, size=SLIM_ICON, font=theme.font(_ICON_SIZE, bold=True), command=self._click
        )
        self._main_tip = add_tooltip(self.main, "")
        name_font = theme.font(scale.font(_NAME_SIZE, TITLE_MIN), bold=True)
        code_font = theme.font(scale.font(_CODE_SIZE, SMALL_TEXT_MIN))
        self.name_label = ctk.CTkLabel(
            line, text=show.name, font=name_font, text_color=theme.TEXT, height=SLIM_LINE
        )
        self.name_label.pack(side="left")
        self.open_show = _open_on_click(self.name_label, show, on_open_show)
        range_label = stack.range
        self.range_label = ctk.CTkLabel(
            line, text=range_label, font=code_font, text_color=theme.TEXT_MUTED, height=SLIM_LINE
        )
        self.range_label.pack(side="left", padx=(SLIM_CODE_GAP, 0))
        badges = badge_texts(len(stack.entries))
        self.badge = PillLabel(
            line,
            text=badges[0],
            color=pill_color(),
            font=theme.font(_BADGE_SIZE, bold=True),
            height=BADGE_HEIGHT,
        )
        self.badge.pack(side="left", padx=(round(BADGE_GAP * _scaling(line)), 0))  # plain Tk
        add_tooltip(self.badge, badges[0])

        self.note: PillLabel | None = None
        if noted is not None:
            self.note = _note_pill(self, noted, scale)
            _pack_note(self.note, self)

        line_width: float | None = None  # unscaled, once laid out

        def fit() -> None:
            if line_width is None:
                return
            icon = SLIM_ICON if self._look is not None and self._look.text else 0
            range_width = code_font.measure(range_label)
            widths = [self.badge.width_of(text) for text in badges]
            badge = slim_badge(line_width, range_width, icon, widths)
            self.badge.set_text(badges[badge])
            name_room, range_room = slim_stack_rooms(line_width, range_width, widths[badge], icon)
            self.name_label.configure(
                text=elide_to_width(show.name, name_room, name_font.measure)
            )
            self.range_label.configure(
                text=elide_to_width(range_label, range_room, code_font.measure)
            )
            if self.note is not None and noted is not None:
                fit_note(self.note, noted, line_width)

        def resized(event: Any) -> None:
            nonlocal line_width
            if event.width > 1:
                line_width = unscaled_width(line, event.width)
                fit()

        line.bind("<Configure>", resized)
        self._fit = fit
        self._watches = _watch(stack, actions, self)
        self.repaint()
        widgets: list[tk.Misc] = [self, *_descendants(self)]
        for widget in widgets:
            tk.Misc.bind(widget, "<Button-3>", self._right_click, add="+")

    @property
    def look(self) -> IconLook | None:
        return self._look

    def repaint(self) -> None:
        states = self._actions.states
        look = stack_icon(
            stack_look(
                self._stack.entries,
                states.get,
                states.batch(self._stack.keys),
                self._in_plex,
                marks=self._marks,
            )
        )
        if look == self._look:
            return
        self._look = look
        self.main.pack_forget()
        if look.text:
            self.main.pack(side="right", before=self.name_label)
        self.main.set_look(
            look.text,
            fill=look.color,
            hover=look.hover,
            text_color=look.text_color,
            edge=look.border,
            enabled=look.action is not None,
        )
        self._main_tip.set_text(look.tip)
        self._fit()

    def menu_items(self) -> MenuItems:
        """The episodes' menu (`episode_menu`)."""
        return episode_menu(
            self._stack,
            self._actions,
            self._in_plex,
            marks=self._marks,
            downloaded=self._downloaded,
        )

    def _click(self) -> None:
        look = self._look
        if look is None or look.action is None:
            return
        if look.action == "download":
            start_download_all(self._stack, self._actions, self._in_plex)
        else:
            self._right_click()

    def _right_click(self, _event: Any = None) -> None:
        popup_menu(self, self.menu_items())

    def _set_scaling(self, *args: Any, **kwargs: Any) -> None:
        """Windows' scaling changed: the pills and the round button (plain Tk) are drawn
        again."""
        super()._set_scaling(*args, **kwargs)
        for part in (self.badge, self.note, self.main):
            if part is not None:
                part.redraw()

    def destroy(self) -> None:
        _unwatch(self._watches, self._actions)
        super().destroy()
