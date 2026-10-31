"""Several episodes of one tracked show on one day: one stack card instead of a card each.

The calendar view groups a day's tracked episodes per show (`stack_entries`); a show with two
or more that day gets a stack card: the wide `StackCard` (recent days and later) or the slim
`CompactStackCard` (older days). Suggestions are never stacked.

`StackCard` is the wide episode card (`EpisodeCard`, app/ui/calendar_cards.py) with two card
edges peeking out behind it at the bottom right (app/ui/calendar_face.py bakes their corners),
the episodes' range under the name ("S01E07 – E09", `range_text`), a pill "3 episodes ▾" and the
bottom row "⬇ Download all (2)" (`stack_look`). Clicking the pill unfolds a row per episode
inside the card (the pill turns "▴"): its code with the compact cards' small buttons
(`SlimDownload`: ⬇ with ⋯, ✓, …) and under it its state ("✓ downloaded", amber "still needed",
"not aired": `row_state`); a right-click on a row opens the search window. Clicking the pill
again folds them. The view remembers the unfolded stacks for the session (`on_fold`), so a
reload keeps them open.

"⬇ Download all" fetches the episodes still needed (aired, not downloaded, not marked "I
already have this": `needed_entries`) one after another through the one-click download
(`DownloadActions.download_all`: the main window runs `download_in_turn`), so each episode
keeps its own state everywhere (the show window too); meanwhile the row shows "Downloading 2 of
3…" (the batch's `BatchState`, which a card rebuilt mid-run picks up as well), afterwards the
usual outcome. Once every aired episode is downloaded the row is a ✓ like a single card's; a
click on it unfolds the rows. A stack with any episode still needed gets the amber left edge.

`CompactStackCard` is one slim line: the name, the range and a small "3" badge, then a small ⬇
(download all still needed) or ✓. Clicking the name opens the show's window; a right-click (or
the ✓) opens a small menu of the episodes, each opening its search window.
"""

from __future__ import annotations

import asyncio
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
    MISSING_TEXTS,
    MISSING_TIP,
    NEW_NOTE_TEXT,
    NEW_NOTE_TIP,
    NOTE_HEIGHT,
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
    _ICON_RADIUS,
    _ICON_SIZE,
    _NAME_SIZE,
    _NOTE_SIZE,
    EpisodeActions,
    EpisodeCard,
    IconLook,
    OpenShow,
    SlimDownload,
    _descendants,
    _font_linespace,
    _linespace,
    _open_on_click,
    _rgb,
    buttons_width,
    compact_height,
    missing_note,
    new_note,
)
from app.ui.calendar_face import (
    MARK_WIDTH,
    PILL_PAD_X,
    EpisodeLayout,
    PillFace,
    StackEdges,
    episode_layout,
    shared_image,
    stack_edges,
    stack_step_px,
)
from app.ui.card_scale import FULL, SMALL_TEXT_MIN, TITLE_MIN, CardScale
from app.ui.download_control import (
    DOWNLOAD_SHORT,
    GRABBED_TEXT,
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
from app.ui.missing_control import MISSING_COLOR, popup_menu, progress_texts
from app.ui.tasks import spawn
from app.ui.text_fit import SLACK, elide_to_width, unscaled_width
from app.ui.tooltip import add_tooltip

if TYPE_CHECKING:
    from app.services import ImageCache

FoldKey = tuple[int, date]  # (show id, air date): which stacks are unfolded
StateOf = Callable[[DownloadKey], RunState | None]  # a title's one-click run state
RowState = Literal["not_aired", "downloaded", "have", "new", "needed"]

# The texts are shown through t().
DOWNLOAD_ALL_TEXT = N_("⬇ Download all ({n})")
DOWNLOAD_ALL_SHORTER = (N_("⬇ Download ({n})"), N_("⬇ All ({n})"))  # when it doesn't fit
ROW_TEXTS: dict[RowState, str] = {
    "not_aired": N_("not aired"),
    "downloaded": N_("✓ downloaded"),
    "have": N_("✓ have it"),
    "new": N_("new"),
    "needed": N_("still needed"),
}
FOLDED_ARROW = "▾"
UNFOLDED_ARROW = "▴"

# The "3 episodes ▾" pill and the compact card's "3" badge are ROW_SELECTED (read when drawn).
PILL_TEXT_COLOR = theme.BUTTON_TEXT
# The unfolded rows (XL units; the buttons keep the compact cards' size): the gap under the pill,
# then per episode a line with its code and small buttons and a line with its state.
ROWS_GAP = 6
EPISODE_LINE = SLIM_ICON
EPISODE_ROW_GAP = 4  # between two episodes
CODE_BUTTONS_GAP = 6  # at least, between an episode's code and its buttons
# The compact card's "3" badge.
BADGE_GAP = 4  # between the range and the badge
BADGE_PAD_X = 5
BADGE_HEIGHT = 16
BADGE_RADIUS = 8
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


def row_state(
    entry: CalendarEntry,
    state: RunState | None,
    had: Collection[int],
    new: Collection[int] = frozenset(),
) -> RowState:
    """An episode's state in its unfolded row: not aired; downloaded (before, or by a run that
    just finished); marked "I already have this" (`had`: episode ids); aired less than a day
    ago (`new`: episode ids; a quiet blue "new", not missing yet); else still needed."""
    if not entry.episode.has_aired():
        return "not_aired"
    if entry.is_downloaded or (state is not None and state.phase is Phase.DONE):
        return "downloaded"
    if entry.episode.id in had:
        return "have"
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
    """"✓ downloaded", "still needed"..."""
    return t(ROW_TEXTS[kind])


def needed_entries(
    entries: Iterable[CalendarEntry], state_of: StateOf, had: Collection[int]
) -> tuple[CalendarEntry, ...]:
    """The episodes "⬇ Download all" fetches: still needed (new ones too), with a number to
    download by (a special is searched for in its own row)."""
    return tuple(
        entry
        for entry in entries
        if entry.episode.number is not None
        and row_state(entry, state_of(entry_key(entry)), had) == "needed"
    )


def download_all_texts(count: int) -> tuple[str, ...]:
    """"⬇ Download all (2)" and its shorter forms, longest first."""
    shorter = tuple(t(text, n=count) for text in DOWNLOAD_ALL_SHORTER)
    return (t(DOWNLOAD_ALL_TEXT, n=count), *shorter, f"{DOWNLOAD_SHORT} {count}")


def stack_look(
    entries: Sequence[CalendarEntry],
    state_of: StateOf,
    batch: BatchState | None,
    had: Collection[int],
) -> Look:
    """A stack's bottom row: "Downloading 2 of 3…" while its "Download all" runs, the working
    label while one of its episodes is fetched on its own, "⬇ Download all (2)" while episodes
    are still needed, else a ✓ like a single card's (a click unfolds the rows: "choose");
    disabled "Not Aired" when none has aired."""
    if batch is not None:
        first, *rest = progress_texts(batch.index, batch.total)
        return Look("label", first, text_color=theme.TEXT_MUTED, short=tuple(rest))
    states = [state_of(entry_key(entry)) for entry in entries]
    if any(state is not None and state.phase is Phase.WORKING for state in states):
        return Look(
            "label", t(WORKING_TEXT), text_color=theme.TEXT_MUTED, short=(t(WORKING_SHORT),)
        )
    needed = len(needed_entries(entries, state_of, had))
    if needed:
        text, *short = download_all_texts(needed)
        return Look("button", text, action="download", short=tuple(short))
    aired = [state for entry, state in zip(entries, states) if entry.episode.has_aired()]
    if not aired:
        return Look("button", t("Not Aired"), theme.NOT_AIRED, theme.NOT_AIRED)
    outcomes = {state.outcome if state is not None else None for state in aired}
    outcome = outcomes.pop() if len(outcomes) == 1 else None
    text = outcome_text(outcome) if outcome is not None else t(GRABBED_TEXT)
    return Look("button", text, theme.GRABBED, action="choose")


def pill_texts(count: int, unfolded: bool) -> tuple[str, str]:
    """The pill: "3 episodes ▾" ("▴" while unfolded), and "3 ▾" for when that doesn't fit."""
    arrow = UNFOLDED_ARROW if unfolded else FOLDED_ARROW
    return f"{tn('{n} episode', '{n} episodes', count)} {arrow}", f"{count} {arrow}"


def stack_icon(look: Look) -> IconLook:
    """The compact stack card's small button for its row's `look`: … while working (the tip
    says what), ⬇ = download all still needed, ✓ once all are there (a click lists the
    episodes), none before any has aired."""
    if look.layout == "label":
        return IconLook(WORKING_ICON, look.text, "transparent", theme.SURFACE, theme.TEXT_MUTED)
    if look.action == "download":
        return IconLook(
            DOWNLOAD_SHORT, look.text, look.color, look.hover, look.text_color, action="download"
        )
    if look.action == "choose":
        return IconLook(GRABBED_ICON, look.text, look.color, look.hover, action="choose")
    return IconLook("")


def menu_label(entry: CalendarEntry, state: RunState | None, had: Collection[int]) -> str:
    """An episode in the compact stack card's menu: "S01E07 · ✓ downloaded"."""
    return f"{entry.episode.code} · {row_text(row_state(entry, state, had))}"


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


def start_download_all(stack: Stack, actions: EpisodeActions, had: Collection[int]) -> None:
    """"⬇ Download all": the episodes still needed, one after another."""
    needed = needed_entries(stack.entries, actions.states.get, had)
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
    """A stack card's bottom row (`stack_look`): "⬇ Download all (2)", the batch's progress,
    or a ✓ that unfolds the rows (`on_choose`)."""

    def __init__(
        self,
        master: Any,
        stack: Stack,
        *,
        had: Collection[int],
        actions: EpisodeActions,
        scale: CardScale = FULL,
        on_choose: Callable[[], None] | None = None,
    ) -> None:
        self._stack = stack
        self._had = had
        self._on_choose = on_choose
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
        return stack_look(
            self._stack.entries, states.get, states.batch(self._stack.keys), self._had
        )

    def _click(self) -> None:
        look = self._look
        if look is None or look.action is None:
            return
        if look.action == "download":
            start_download_all(self._stack, self._actions, self._had)
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
    `missing`: the amber left edge (an episode still missing); `had`: the ids of the episodes
    marked "I already have this"; `unfolded`: the rows open from the start; `on_fold(open)`
    hears every fold and unfold."""

    def __init__(
        self,
        master: Any,
        stack: Stack,
        *,
        images: ImageCache,
        actions: EpisodeActions,
        scale: CardScale = FULL,
        on_open_show: OpenShow | None = None,
        missing: bool = False,
        had: Collection[int] = frozenset(),
        unfolded: bool = False,
        on_fold: Callable[[bool], None] | None = None,
        new: Collection[int] = frozenset(),
    ) -> None:
        # What the overridden steps of EpisodeCard's set-up read.
        self._stack = stack
        self._had = frozenset(had)
        self._new_ids = frozenset(new)
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
            missing=missing,
        )
        self._on_fold = on_fold
        self._code_text = stack.range
        self._pill_font = theme.font(self._layout.pill_size, bold=True)
        self._row_font = theme.font(self._layout.code_size)
        self._row_code_font = theme.font(self._layout.code_size, bold=True)
        self._pill_face: PillFace | None = None
        self._pill_task: asyncio.Task[None] | None = None

        canvas = self._art
        # The cards behind it: their straight stretch along the bottom, under everything (the
        # baked poster piece and cap hold their ends). Farthest first: fill, edge line.
        self._behind = [canvas.create_rectangle(0, 0, 0, 0, outline="", width=0) for _ in range(4)]
        for item in reversed(self._behind):
            canvas.tag_lower(item)
        self._pill = canvas.create_image(0, 0, anchor="nw", tags=("pill",))
        self._pill_text = canvas.create_text(0, 0, anchor="w", text="", tags=("pill",))
        canvas.tag_bind("pill", "<Button-1>", lambda _event: self.toggle())
        canvas.tag_bind("pill", "<Enter>", lambda _event: canvas.configure(cursor="hand2"))
        canvas.tag_bind("pill", "<Leave>", lambda _event: canvas.configure(cursor=""))
        self._watches = _watch(stack, actions, self)
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

    def _card_layout(self, scale: CardScale, missing: bool) -> EpisodeLayout:
        return episode_layout(scale, linespace=_font_linespace, stack=True)

    def _card_height(self, layout: EpisodeLayout) -> int:
        height = layout.height + layout.stack_depth
        if self._unfolded:
            state_line = _font_linespace(layout.code_size, False)
            height += rows_height(len(self._stack.entries), state_line, self._scale)
        return height

    def _make_control(self, entry: CalendarEntry) -> DownloadControl[CalendarEntry] | None:
        if not any(item.episode.has_aired() for item in self._stack.entries):
            return None  # "Not aired" instead
        return StackControl(
            self,
            self._stack,
            had=self._had,
            actions=self._actions,
            scale=self._layout.row_scale,
            on_choose=self.toggle,
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
        super()._set_appearance_mode(mode_string)
        self._paint_stack()
        self._pill_face = None  # baked again in the new colour
        if self._size[0] > 1:
            self._place_texts()

    def _paint_stack(self) -> None:
        """The stack's canvas colours in the current mode (the rows' state colours: in
        `_place_texts`)."""
        canvas, pick = self._art, theme.pick
        far_fill, far_edge, near_fill, near_edge = self._behind
        canvas.itemconfigure(far_fill, fill=pick(theme.STACK_FAR))
        canvas.itemconfigure(far_edge, fill=pick(theme.STACK_FAR_EDGE))
        canvas.itemconfigure(near_fill, fill=pick(theme.STACK_NEAR))
        canvas.itemconfigure(near_edge, fill=pick(theme.STACK_NEAR_EDGE))
        canvas.itemconfigure(self._pill_text, fill=pick(PILL_TEXT_COLOR))
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
        """The pill under the range, and the unfolded rows under the pill."""
        layout, canvas, scaling = self._layout, self._art, self._scaling
        room = self._text_room()
        x = round(self._area.text_x * scaling)
        box = canvas.bbox(self._code)
        top = (box[3] if box else round(layout.pad_top * scaling)) + round(
            layout.pill_gap * scaling
        )

        full, short = pill_texts(len(self._stack.entries), self._unfolded)
        text = fit_text([full, short], room - 2 * PILL_PAD_X, self._pill_font.measure)
        width = self._pill_font.measure(text) + 2 * PILL_PAD_X
        size = (round(width * scaling), round(layout.pill_height * scaling))
        canvas.itemconfigure(
            self._pill_text, text=text, font=self._pill_font.create_scaled_tuple(scaling)
        )
        canvas.coords(self._pill, x, top)
        canvas.coords(self._pill_text, x + round(PILL_PAD_X * scaling), top + size[1] / 2)
        face = PillFace(size, _rgb(self, theme.ROW_SELECTED))
        if face != self._pill_face:
            self._pill_face = face
            if self._pill_task is not None:
                self._pill_task.cancel()
            self._pill_task = spawn(self._load_pill(face), name="StackCard.pill")

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
            kind = row_state(row.entry, states.get(entry_key(row.entry)), self._had, self._new_ids)
            canvas.itemconfigure(
                row.state,
                text=elide_to_width(row_text(kind), room, self._row_font.measure),
                font=state_font,
                fill=theme.pick(row_color(kind)),
            )
            canvas.coords(row.state, x, y + round(EPISODE_LINE * scaling))
            row.buttons.place(relx=1.0, x=-right, y=row_top, anchor="ne")

    async def _load_pill(self, face: PillFace) -> None:
        self._show_image("pill", self._pill, await shared_image(face, face))

    def destroy(self) -> None:
        _unwatch(self._watches, self._actions)
        if self._pill_task is not None:
            self._pill_task.cancel()
        super().destroy()


class CompactStackCard(ctk.CTkFrame):
    """Several episodes of one show on an older day, on one slim line: the name and the range
    with a small "3" badge, then a small ⬇ (download all still needed) or ✓. Clicking the name
    opens the show's window; a right-click, or the ✓, opens a menu of the episodes (each opens
    its search window). `missing`: the amber bar on the left edge and the note line."""

    def __init__(
        self,
        master: Any,
        stack: Stack,
        *,
        actions: EpisodeActions,
        scale: CardScale = FULL,
        on_open_show: OpenShow | None = None,
        missing: bool = False,
        had: Collection[int] = frozenset(),
        new: Collection[int] = frozenset(),
    ) -> None:
        any_new = not missing and any(entry.episode.id in new for entry in stack.entries)
        height = compact_height(missing or any_new)
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
        self._had = frozenset(had)
        self._look: IconLook | None = None
        show = stack.show
        self.mark: ctk.CTkFrame | None = None
        if missing:
            self.mark = ctk.CTkFrame(
                self,
                width=MARK_WIDTH,
                height=height - 2 * SLIM_RADIUS,
                corner_radius=0,
                fg_color=MISSING_COLOR,
            )
            self.mark.place(x=0, y=SLIM_RADIUS)

        line = ctk.CTkFrame(self, fg_color="transparent", height=SLIM_LINE)
        line.pack(side="top", fill="x", padx=(SLIM_PAD_X, SLIM_PAD_RIGHT), pady=(SLIM_PAD_Y, 0))
        line.pack_propagate(False)
        self.main = ctk.CTkButton(
            line,
            text="",
            width=SLIM_ICON,
            height=SLIM_ICON,
            corner_radius=_ICON_RADIUS,
            font=theme.font(_ICON_SIZE, bold=True),
            border_width=0,
            text_color_disabled=theme.TEXT_MUTED,
            command=self._click,
        )
        self._main_tip = add_tooltip(self.main, "")
        self._default_text_color = self.main.cget("text_color")
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
        count = str(len(stack.entries))
        badge_font = theme.font(_BADGE_SIZE, bold=True)
        badge_width = max(BADGE_HEIGHT, round(badge_font.measure(count)) + 2 * BADGE_PAD_X)
        self.badge = ctk.CTkLabel(
            line,
            text=count,
            font=badge_font,
            fg_color=theme.ROW_SELECTED,
            text_color=PILL_TEXT_COLOR,
            corner_radius=BADGE_RADIUS,
            width=badge_width,
            height=BADGE_HEIGHT,
        )
        self.badge.pack(side="left", padx=(BADGE_GAP, 0))
        add_tooltip(self.badge, tn("{n} episode", "{n} episodes", len(stack.entries)))

        self.note: ctk.CTkLabel | None = None
        note_font = theme.font(scale.font(_NOTE_SIZE, SMALL_TEXT_MIN))
        if missing or any_new:
            self.note = ctk.CTkLabel(
                self,
                text=t(NEW_NOTE_TEXT) if any_new else t(MISSING_TEXTS[0]),
                font=note_font,
                text_color=theme.LINK if any_new else MISSING_COLOR,
                height=NOTE_HEIGHT,
                anchor="w",
            )
            self.note.pack(side="top", anchor="w", padx=SLIM_PAD_X)
            add_tooltip(self.note, t(NEW_NOTE_TIP) if any_new else t(MISSING_TIP))
        note_text = new_note if any_new else missing_note

        line_width: float | None = None  # unscaled, once laid out

        def fit() -> None:
            if line_width is None:
                return
            icon = SLIM_ICON if self._look is not None and self._look.text else 0
            name_room, range_room = slim_stack_rooms(
                line_width, code_font.measure(range_label), badge_width, icon
            )
            self.name_label.configure(
                text=elide_to_width(show.name, name_room, name_font.measure)
            )
            self.range_label.configure(
                text=elide_to_width(range_label, range_room, code_font.measure)
            )
            if self.note is not None:
                self.note.configure(text=note_text(line_width - SLACK, note_font.measure))

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
            stack_look(self._stack.entries, states.get, states.batch(self._stack.keys), self._had)
        )
        if look == self._look:
            return
        self._look = look
        self.main.pack_forget()
        if look.text:
            self.main.pack(side="right", before=self.name_label)
        self.main.configure(
            text=look.text,
            fg_color=look.color,
            hover_color=look.hover,
            text_color=look.text_color or self._default_text_color,
            border_width=1 if look.border else 0,
            border_color=look.border or theme.EDGE,
            state="normal" if look.action else "disabled",
        )
        self._main_tip.set_text(look.tip)
        self._fit()

    def menu_items(self) -> list[tuple[str, Callable[[], None]]]:
        """The episodes' menu: one item per episode, opening its search window."""
        states, choose = self._actions.states, self._actions.choose

        def opener(entry: CalendarEntry) -> Callable[[], None]:
            return lambda: choose(entry)

        return [
            (menu_label(entry, states.get(entry_key(entry)), self._had), opener(entry))
            for entry in self._stack.entries
        ]

    def _click(self) -> None:
        look = self._look
        if look is None or look.action is None:
            return
        if look.action == "download":
            start_download_all(self._stack, self._actions, self._had)
        else:
            self._right_click()

    def _right_click(self, _event: Any = None) -> None:
        popup_menu(self, self.menu_items())

    def destroy(self) -> None:
        _unwatch(self._watches, self._actions)
        super().destroy()
