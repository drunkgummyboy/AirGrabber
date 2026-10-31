"""The one-click Download control of episode and movie cards, and the run states behind it.

A card shows one 36-unit row: "⬇ Download" with a narrow "⋯" beside it that opens the search
window, or a single button (✓ Grabbed, a disabled "Not Aired"...), or while a run works the
label "Finding the best one…". Texts never get cut off: where a text doesn't fit, a shorter
one stands in ("⬇", "Finding…"). The main window runs the downloads and keeps their state in a
`DownloadStates`; every control showing a title attaches to it, so a card rebuilt mid-run (the
calendar reloads after every grab) picks the run's state up, and a finished run repaints
whichever cards show the title by then.

Hovering "⬇ Download" tells what the one-click download looks for, from Settings ("Looks for
Full HD (1080p), 2.5–4 GB", `looks_for_text`); once a run grabbed something, the button's tip
tells what it picked ("Picked 1080p · 2.8 GB", `picked_text`).

Several titles can be downloaded one after another (`download_in_turn`: the calendar's
"⬇ Download all" of a stack card): each through the same one-click run, each with its own
state, and the batch's progress ("2 of 3") in DownloadStates as well (`BatchState`, keyed by
the batch's titles), so a card rebuilt mid-batch picks it up too.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Generic, Literal, Protocol, TypeVar

import customtkinter as ctk

from app.i18n import N_, number, t, tn
from app.models import GrabOutcome, MediaType, QualityProfile, QuickDownloadResult, Settings
from app.ui import theme
from app.ui.card_scale import FULL, TEXT_MIN, CardScale
from app.ui.text_fit import SLACK, Measure, elide_to_width, unscaled_width
from app.ui.tooltip import add_tooltip

T = TypeVar("T")

ROW_HEIGHT = 36  # every state of the row, so a card never jumps between them
CORNER_RADIUS = 8
FONT_SIZE = 14
MORE_WIDTH = 36  # the square "⋯" button
GAP = 8  # between the Download and "⋯" buttons
PAD_X = 12  # inside the card, left and right of the row
PAD_TOP = 12  # above the row
PAD_BOTTOM = 10  # below the row
ROW_SPACE = PAD_TOP + ROW_HEIGHT + PAD_BOTTOM  # the height the row takes in a card

# The texts are shown through t().
DOWNLOAD_TEXT = N_("⬇ Download")
DOWNLOAD_SHORT = "⬇"  # when the full text doesn't fit (narrow windows)
MORE_TEXT = "⋯"
MORE_TIP = N_("Choose a download yourself")
DOWNLOAD_TIP = N_("Download")  # when the button shows only "⬇" (before what it looks for)
WORKING_TEXT = N_("Finding the best one…")
WORKING_SHORT = N_("Finding…")
CHOOSE_TEXT = N_("Choose…")  # nothing was good enough: pick one in the search window
GRABBED_TEXT = N_("✓ Grabbed")
_OUTCOME_TEXT = {
    GrabOutcome.OPENED: N_("✓ Opened"),
    GrabOutcome.SAVED: N_("✓ Saved"),
    GrabOutcome.SENT: N_("✓ Sent"),
}

# How the Download tip names each quality (Settings → Quality's names, mid-sentence).
LOOKS_FOR_QUALITY: dict[QualityProfile, str] = {
    QualityProfile.FULL_HD: N_("Full HD (1080p)"),
    QualityProfile.HD: N_("HD (720p)"),
    QualityProfile.UHD: N_("4K (2160p)"),
    QualityProfile.HEVC: N_("smaller files (1080p x265)"),
    QualityProfile.ANY: N_("any quality"),
}
_GIB = 1024**3  # Settings' sizes are binary GB, as the search window shows sizes

# (media type, TVMaze show id or TMDB movie id, season, episode)
DownloadKey = tuple[MediaType, int, int | None, int | None]
# A batch of titles downloaded one after another, by their keys (a calendar stack's episodes).
BatchKey = tuple[DownloadKey, ...]


def episode_key(show_id: int, season: int, episode: int | None) -> DownloadKey:
    return (MediaType.TV, show_id, season, episode)


def movie_key(movie_id: int) -> DownloadKey:
    return (MediaType.MOVIE, movie_id, None, None)


class Phase(StrEnum):
    WORKING = "working"
    DONE = "done"  # grabbed (by this run, or already before it)
    NOTHING = "nothing"  # no release was good enough


@dataclass(frozen=True, slots=True)
class RunState:
    phase: Phase
    outcome: GrabOutcome | None = None  # DONE; None = it was already downloaded ("✓ Grabbed")
    # DONE by a run that grabbed: the picked release's resolution ("1080p") and size, if known.
    quality: str | None = None
    size_bytes: int | None = None


@dataclass(frozen=True, slots=True)
class BatchState:
    """A batch at work: fetching its `index`-th title (from 1) of `total`."""

    index: int
    total: int


def grabbed_state(result: QuickDownloadResult) -> RunState:
    """The DONE state of a run that grabbed a release (`result.grab` set), with what it
    picked."""
    outcome = result.grab.outcome if result.grab is not None else None
    return RunState(Phase.DONE, outcome, quality=result.quality, size_bytes=result.size_bytes)


# What a card shows without a run: the Download split control, a "✓ Grabbed" or "Search" button
# (both open the search window), or a disabled button such as "Not Aired".
BaseKind = Literal["download", "grabbed", "search", "unavailable"]


@dataclass(frozen=True, slots=True)
class Base:
    kind: BaseKind
    text: str = ""  # "unavailable" only: "Not Aired", "Digital 15 Oct"...


Action = Literal["download", "choose"]  # choose = open the search window


@dataclass(frozen=True, slots=True)
class Look:
    """How the row looks: the split control, one button, or the working label."""

    layout: Literal["split", "button", "label"]
    text: str
    # The accent's, read when the look is made (the palette is set at startup).
    color: theme.Color = field(default_factory=lambda: theme.ACCENT)
    hover: theme.Color = field(default_factory=lambda: theme.ACCENT_HOVER)
    text_color: theme.Color | None = None  # None = CTk's default (white)
    action: Action | None = None  # None = disabled
    short: tuple[str, ...] = ()  # shorter texts, in order, for when `text` doesn't fit
    border: theme.Color | None = None  # a 1-unit border in this colour

    @property
    def texts(self) -> tuple[str, ...]:
        return (self.text, *self.short)


def outcome_text(outcome: GrabOutcome) -> str:
    """"✓ Opened", "✓ Saved" or "✓ Sent"."""
    return t(_OUTCOME_TEXT[outcome])


def fit_text(texts: Sequence[str], room: float, measure: Measure) -> str:
    """The first of `texts` (longest first) that fits in `room`, measured as text_fit does;
    the last one elided if none does."""
    for text in texts:
        if measure(text) <= room:
            return text
    return elide_to_width(texts[-1], room, measure)


def control_look(base: Base, state: RunState | None) -> Look:
    """The row for a card's base look and the title's run state, if any.

    A run working or done wins over everything; an episode already grabbed (from the search
    window too) wins over an earlier "nothing found".
    """
    if state is not None and state.phase is Phase.WORKING:
        return Look(
            "label", t(WORKING_TEXT), text_color=theme.TEXT_MUTED, short=(t(WORKING_SHORT),)
        )
    if state is not None and state.phase is Phase.DONE:
        text = outcome_text(state.outcome) if state.outcome is not None else t(GRABBED_TEXT)
        return Look("button", text, theme.GRABBED, action="choose")
    if base.kind == "grabbed":
        return Look("button", t(GRABBED_TEXT), theme.GRABBED, action="choose")
    if base.kind == "unavailable":
        return Look("button", base.text, theme.NOT_AIRED, theme.NOT_AIRED)
    if state is not None and state.phase is Phase.NOTHING:
        return Look(
            "button",
            t(CHOOSE_TEXT),
            "transparent",
            theme.CHIP_HOVER,
            theme.PENDING,
            action="choose",
            border=theme.PENDING,
        )
    if base.kind == "search":
        return Look("button", t("Search"), action="choose")
    return Look("split", t(DOWNLOAD_TEXT), action="download", short=(DOWNLOAD_SHORT,))


def gb_text(size_bytes: int) -> str:
    """A size in plain GB, one decimal at most: "2.8", "4", "0.05" (below 0.1 GB: two); a
    decimal comma in Dutch."""
    gb = size_bytes / _GIB
    text = f"{gb:.1f}" if gb >= 0.1 else f"{gb:.2f}"
    return number(float(text.removesuffix(".0")), "g")


def _limit_text(value: float) -> str:
    """A Settings size limit as written: "2.5", "4"."""
    return number(value, "g")


def size_range_text(low: float | None, high: float | None) -> str | None:
    """"2.5–4 GB", "at least 2.5 GB", "up to 4 GB", or None without limits."""
    if low is not None and high is not None:
        if low == high:
            return f"{_limit_text(low)} GB"
        return f"{_limit_text(low)}–{_limit_text(high)} GB"
    if low is not None:
        return t("at least {size} GB", size=_limit_text(low))
    if high is not None:
        return t("up to {size} GB", size=_limit_text(high))
    return None


def looks_for_text(settings: Settings, media_type: MediaType) -> str:
    """What a one-click download looks for: "Looks for Full HD (1080p), 2.5–4 GB" (the
    series settings for an episode, the movie ones for a film); no size range → just the
    quality."""
    if media_type is MediaType.MOVIE:
        quality = settings.quality_movie
        low, high = settings.size_movie_min_gb, settings.size_movie_max_gb
    else:
        quality = settings.quality_tv
        low, high = settings.size_tv_min_gb, settings.size_tv_max_gb
    text = t("Looks for {quality}", quality=t(LOOKS_FOR_QUALITY[quality]))
    size = size_range_text(low, high)
    return f"{text}, {size}" if size else text


def picked_text(quality: str | None, size_bytes: int | None) -> str:
    """What a one-click download picked: "Picked 1080p · 2.8 GB", "Picked 720p", "" when
    neither is known."""
    size = f"{gb_text(size_bytes)} GB" if size_bytes else None
    parts = [part for part in (quality, size) if part]
    return t("Picked {what}", what=" · ".join(parts)) if parts else ""


def control_tip(look: Look, state: RunState | None, looks_for: str, *, shortened: bool) -> str:
    """The main button's hover tip ("" = none): what ⬇ Download looks for ("Download · …"
    when the button shows only "⬇"), what a finished run picked, else nothing."""
    if look.action == "download":
        if shortened:
            return f"{t(DOWNLOAD_TIP)} · {looks_for}" if looks_for else t(DOWNLOAD_TIP)
        return looks_for
    if state is not None and state.phase is Phase.DONE and state.outcome is not None:
        return picked_text(state.quality, state.size_bytes)
    return ""


def nothing_found_text(label: str) -> str:
    """The status bar when a one-click download found nothing good enough."""
    return t("No good download found for {label}. Choose one yourself.", label=label)


def already_downloaded_text(label: str) -> str:
    """The status bar when a one-click download finds its title already downloaded."""
    return t("{label} was already downloaded", label=label)


def download_status_text(label: str, result: QuickDownloadResult) -> str:
    """The status bar after a one-click download:
    "Severance S02E01 · 1080p · 412 seeders · Saved Severance….torrent"."""
    parts = [label]
    if result.quality:
        parts.append(result.quality)
    if result.seeders is not None:
        parts.append(tn("{n} seeder", "{n} seeders", result.seeders))
    if result.grab is not None:
        parts.append(result.grab.message)
    return " · ".join(parts)


class StateView(Protocol):
    """A widget showing a title's run state: the cards' DownloadControl, the show and film
    windows' RowActions (app/ui/row_actions.py)."""

    key: DownloadKey

    def repaint(self) -> None: ...

    def winfo_exists(self) -> Any: ...


class DownloadStates:
    """The run state of every one-click download by title, and the controls showing them."""

    def __init__(self, settings: Callable[[], Settings] | None = None) -> None:
        """`settings`: the current Settings, for the Download tip's `looks_for_text`
        (without it the tip says nothing about it)."""
        self._states: dict[DownloadKey, RunState] = {}
        self._batches: dict[BatchKey, BatchState] = {}
        self._controls: dict[DownloadKey, list[StateView]] = {}
        self._settings = settings

    def get(self, key: DownloadKey) -> RunState | None:
        return self._states.get(key)

    def batch(self, key: BatchKey) -> BatchState | None:
        """The progress of the batch of `key`'s titles, while it runs."""
        return self._batches.get(key)

    def set_batch(self, key: BatchKey, state: BatchState | None) -> None:
        """Record a batch's progress (None: it ended) and repaint its titles' controls."""
        if state is None:
            self._batches.pop(key, None)
        else:
            self._batches[key] = state
        painted: set[int] = set()
        for title in key:
            for control in self._live(title):
                if id(control) not in painted:
                    painted.add(id(control))
                    control.repaint()

    def looks_for(self, key: DownloadKey) -> str:
        """What a one-click download for `key` looks for, as Settings has it now ("" without
        settings)."""
        if self._settings is None:
            return ""
        return looks_for_text(self._settings(), key[0])

    def is_working(self, key: DownloadKey) -> bool:
        state = self._states.get(key)
        return state is not None and state.phase is Phase.WORKING

    def set(self, key: DownloadKey, state: RunState | None) -> None:
        """Record `state` (None: back to the card's own look) and repaint the title's cards."""
        if state is None:
            self._states.pop(key, None)
        else:
            self._states[key] = state
        for control in self._live(key):
            control.repaint()

    def attach(self, control: StateView) -> None:
        self._live(control.key).append(control)

    def detach(self, control: StateView) -> None:
        controls = self._controls.get(control.key, [])
        if control in controls:
            controls.remove(control)

    def _live(self, key: DownloadKey) -> list[StateView]:
        """The title's controls whose widgets still exist (cards go when a page reloads)."""
        live = [control for control in self._controls.get(key, []) if _exists(control)]
        self._controls = {k: v for k, v in self._controls.items() if v and k != key}
        self._controls[key] = live
        return live


def _exists(widget: Any) -> bool:
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:  # the whole window is gone
        return False


@dataclass(frozen=True, slots=True)
class DownloadActions(Generic[T]):
    """What the Download controls of one kind of card do (the main window provides them)."""

    download: Callable[[T], None]  # start a one-click download
    choose: Callable[[T], None]  # open the search window
    states: DownloadStates
    # One-click downloads of several titles, one after another, as the batch of the given key
    # (`download_in_turn`); None: not offered.
    download_all: Callable[[BatchKey, Sequence[T]], None] | None = None
    # A one-click download of a title that was downloaded before (↻ Again → Auto); None: not
    # offered.
    download_again: Callable[[T], None] | None = None


# One title of a batch: its key and its one-click run, which returns False when the grab
# failed (no torrent program, an unwritable folder...): that ends the batch.
BatchStep = tuple[DownloadKey, Callable[[], Awaitable[bool]]]


async def download_in_turn(
    states: DownloadStates, batch: BatchKey, steps: Sequence[BatchStep]
) -> None:
    """Run `steps` one after another, with the batch's progress in `states` ("2 of 3") while
    they run. A title already being fetched on its own is left to that run; a step that fails
    ends the batch (nothing found is no failure: on to the next)."""
    total = len(steps)
    try:
        for index, (key, run) in enumerate(steps, start=1):
            if states.is_working(key):
                continue
            states.set_batch(batch, BatchState(index, total))
            if not await run():
                break
    finally:
        states.set_batch(batch, None)


class DownloadControl(ctk.CTkFrame, Generic[T]):
    """The row itself. Pack it with `pack_row()` as a card's bottom row, before its other
    content, so it spans the card's whole width."""

    def __init__(
        self,
        master: Any,
        *,
        item: T,
        key: DownloadKey,
        base: Base,
        actions: DownloadActions[T],
        scale: CardScale = FULL,
    ) -> None:
        """`scale`: the card size (poster cards; a share of it on the calendar's full cards)."""
        height = scale.px(ROW_HEIGHT)
        super().__init__(master, fg_color="transparent", height=height)
        self.pack_propagate(False)
        self.key = key
        self._item = item
        self._base = base
        self._actions = actions
        self._look: Look | None = None
        self._width: float | None = None  # unscaled, once laid out
        self._more_width = scale.px(MORE_WIDTH)
        self._gap = scale.px(GAP)
        self._corner = scale.px(CORNER_RADIUS)
        font_size = scale.font(FONT_SIZE, TEXT_MIN)
        self._font = theme.font(font_size, bold=True)
        self._label_font = theme.font(font_size)

        self._more = ctk.CTkButton(
            self,
            text=MORE_TEXT,
            width=self._more_width,
            height=height,
            corner_radius=self._corner,
            font=self._font,
            fg_color="transparent",
            hover_color=theme.HOVER,
            text_color=theme.BUTTON_TEXT,
            border_width=1,
            border_color=theme.EDGE,
            command=self._choose,
        )
        self._main = ctk.CTkButton(
            self,
            text="",
            height=height,
            corner_radius=self._corner,
            font=self._font,
            border_width=0,
            text_color_disabled=theme.NOT_AIRED_TEXT,  # "Not Aired", "Digital 15 Oct"
            command=self._click,
        )
        add_tooltip(self._more, t(MORE_TIP))
        # Before the tip's own <Enter>: the tip reads Settings as they are when hovered.
        tk.Misc.bind(self._main, "<Enter>", self._update_tip, add="+")
        self._main_tip = add_tooltip(self._main, "")  # control_tip
        self._shortened = False  # the Download text gave way to "⬇"
        self._default_text_color = self._main.cget("text_color")
        self._label = ctk.CTkLabel(
            self, text="", height=height, font=self._label_font, text_color=theme.TEXT_MUTED
        )
        self.bind("<Configure>", self._on_resize, add="+")
        actions.states.attach(self)
        self.repaint()

    def pack_row(self, before: Any | None = None) -> None:
        """Pack as the card's bottom row (before `before`, the card's first other content)."""
        options: dict[str, Any] = {"before": before} if before is not None else {}
        self.pack(side="bottom", fill="x", padx=PAD_X, pady=(PAD_TOP, PAD_BOTTOM), **options)

    def set_base(self, base: Base) -> None:
        if base != self._base:
            self._base = base
            self.repaint()

    def current_look(self) -> Look:
        """How the row looks now (a stack card's row looks at all its episodes)."""
        return control_look(self._base, self._actions.states.get(self.key))

    def repaint(self) -> None:
        look = self.current_look()
        if look == self._look:
            return
        previous, self._look = self._look, look
        if previous is None or previous.layout != look.layout:
            for widget in (self._more, self._main, self._label):
                widget.pack_forget()
            if look.layout == "label":
                self._label.pack(fill="both", expand=True)
            else:
                if look.layout == "split":
                    self._more.pack(side="right", padx=(self._gap, 0))
                self._main.pack(side="left", fill="both", expand=True)
        if look.layout != "label":
            self._main.configure(
                fg_color=look.color,
                hover_color=look.hover,
                text_color=look.text_color or self._default_text_color,
                border_width=1 if look.border else 0,
                border_color=look.border or theme.EDGE,
                state="normal" if look.action else "disabled",
            )
        self._fit_text()

    def _on_resize(self, event: Any) -> None:
        if event.width > 1:
            self._width = unscaled_width(self, event.width)
            self._fit_text()

    def _fit_text(self) -> None:
        """The look's text, elided to the room its button or label has."""
        look = self._look
        if look is None:
            return
        text = look.text
        if look.layout == "label":
            if self._width is not None:
                text = fit_text(look.texts, self._width - SLACK, self._label_font.measure)
            self._label.configure(text=text)
            self._update_tip()
            return
        if self._width is not None:
            more = self._more_width + self._gap if look.layout == "split" else 0
            room = self._width - more - 2 * self._corner - SLACK
            text = fit_text(look.texts, room, self._font.measure)
        self._main.configure(text=text)
        self._shortened = look.action == "download" and text != look.text
        self._update_tip()

    def _update_tip(self, _event: Any = None) -> None:
        look = self._look
        if look is None or look.layout == "label":
            self._main_tip.set_text("")
            return
        states = self._actions.states
        self._main_tip.set_text(
            control_tip(
                look, states.get(self.key), states.looks_for(self.key), shortened=self._shortened
            )
        )

    def _click(self) -> None:
        look = self._look
        if look is None or look.action is None:
            return
        if look.action == "download":
            self._actions.download(self._item)
        else:
            self._actions.choose(self._item)

    def _choose(self) -> None:
        self._actions.choose(self._item)

    def destroy(self) -> None:
        self._actions.states.detach(self)
        super().destroy()
