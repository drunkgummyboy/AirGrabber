"""The one-click Download control of episode and movie cards, and the run states behind it.

A card shows one 36-unit row: "⬇ Download" with a narrow "⋯" beside it that opens the search
window, or a single button (✓ Grabbed, a disabled "Not Aired"...), or while a run works the
label "Finding the best one…". Texts never get cut off: where a text doesn't fit, a shorter
one stands in ("⬇", "Finding…"). The main window runs the downloads and keeps their state in a
`DownloadStates`; every control showing a title attaches to it, so a card rebuilt mid-run (the
calendar reloads after every grab) picks the run's state up, and a finished run repaints
whichever cards show the title by then.
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Generic, Literal, TypeVar

import customtkinter as ctk

from app.models import GrabOutcome, MediaType, QuickDownloadResult
from app.ui import theme
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

DOWNLOAD_TEXT = "⬇ Download"
DOWNLOAD_SHORT = "⬇"  # when the full text doesn't fit (narrow windows)
MORE_TEXT = "⋯"
MORE_TIP = "Choose a download yourself"
DOWNLOAD_TIP = "Download"  # when the button shows only "⬇"
WORKING_TEXT = "Finding the best one…"
WORKING_SHORT = "Finding…"
CHOOSE_TEXT = "Choose…"  # nothing was good enough: pick one in the search window
GRABBED_TEXT = "✓ Grabbed"
_OUTCOME_TEXT = {
    GrabOutcome.OPENED: "✓ Opened",
    GrabOutcome.SAVED: "✓ Saved",
    GrabOutcome.SENT: "✓ Sent",
}

# (media type, TVMaze show id or TMDB movie id, season, episode)
DownloadKey = tuple[MediaType, int, int | None, int | None]


def episode_key(show_id: int, season: int, episode: int | None) -> DownloadKey:
    return (MediaType.TV, show_id, season, episode)


def movie_key(movie_id: int) -> DownloadKey:
    return (MediaType.MOVIE, movie_id, None, None)


class Phase(StrEnum):
    WORKING = "working"
    DONE = "done"  # grabbed
    NOTHING = "nothing"  # no release was good enough


@dataclass(frozen=True, slots=True)
class RunState:
    phase: Phase
    outcome: GrabOutcome | None = None  # DONE


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
    color: str = theme.ACCENT
    hover: str = theme.ACCENT_HOVER
    text_color: str | None = None  # None = CTk's default (white)
    action: Action | None = None  # None = disabled
    short: tuple[str, ...] = ()  # shorter texts, in order, for when `text` doesn't fit
    border: str | None = None  # a 1-unit border in this colour

    @property
    def texts(self) -> tuple[str, ...]:
        return (self.text, *self.short)


def outcome_text(outcome: GrabOutcome) -> str:
    """"✓ Opened", "✓ Saved" or "✓ Sent"."""
    return _OUTCOME_TEXT[outcome]


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
        return Look("label", WORKING_TEXT, text_color=theme.TEXT_MUTED, short=(WORKING_SHORT,))
    if state is not None and state.phase is Phase.DONE and state.outcome is not None:
        return Look("button", outcome_text(state.outcome), theme.GRABBED, action="choose")
    if base.kind == "grabbed":
        return Look("button", GRABBED_TEXT, theme.GRABBED, action="choose")
    if base.kind == "unavailable":
        return Look("button", base.text, theme.NOT_AIRED, theme.NOT_AIRED)
    if state is not None and state.phase is Phase.NOTHING:
        return Look(
            "button",
            CHOOSE_TEXT,
            "transparent",
            theme.CHIP_HOVER,
            theme.PENDING,
            action="choose",
            border=theme.PENDING,
        )
    if base.kind == "search":
        return Look("button", "Search", action="choose")
    return Look("split", DOWNLOAD_TEXT, action="download", short=(DOWNLOAD_SHORT,))


def nothing_found_text(label: str) -> str:
    """The status bar when a one-click download found nothing good enough."""
    return f"No good download found for {label}. Choose one yourself."


def download_status_text(label: str, result: QuickDownloadResult) -> str:
    """The status bar after a one-click download:
    "Severance S02E01 · 1080p · 412 seeders · Saved Severance….torrent"."""
    parts = [label]
    if result.quality:
        parts.append(result.quality)
    if result.seeders is not None:
        parts.append(f"{result.seeders} seeder" + ("" if result.seeders == 1 else "s"))
    if result.grab is not None:
        parts.append(result.grab.message)
    return " · ".join(parts)


class DownloadStates:
    """The run state of every one-click download by title, and the controls showing them."""

    def __init__(self) -> None:
        self._states: dict[DownloadKey, RunState] = {}
        self._controls: dict[DownloadKey, list[DownloadControl[Any]]] = {}

    def get(self, key: DownloadKey) -> RunState | None:
        return self._states.get(key)

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

    def attach(self, control: DownloadControl[Any]) -> None:
        self._live(control.key).append(control)

    def detach(self, control: DownloadControl[Any]) -> None:
        controls = self._controls.get(control.key, [])
        if control in controls:
            controls.remove(control)

    def _live(self, key: DownloadKey) -> list[DownloadControl[Any]]:
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


class DownloadControl(ctk.CTkFrame, Generic[T]):
    """The row itself. Pack it with `pack_row()` as a card's bottom row, before its other
    content, so it spans the card's whole width."""

    def __init__(
        self, master: Any, *, item: T, key: DownloadKey, base: Base, actions: DownloadActions[T]
    ) -> None:
        super().__init__(master, fg_color="transparent", height=ROW_HEIGHT)
        self.pack_propagate(False)
        self.key = key
        self._item = item
        self._base = base
        self._actions = actions
        self._look: Look | None = None
        self._width: float | None = None  # unscaled, once laid out
        self._font = theme.font(FONT_SIZE, bold=True)
        self._label_font = theme.font(FONT_SIZE)

        self._more = ctk.CTkButton(
            self,
            text=MORE_TEXT,
            width=MORE_WIDTH,
            height=ROW_HEIGHT,
            corner_radius=CORNER_RADIUS,
            font=self._font,
            fg_color="transparent",
            hover_color=theme.ACCENT_HOVER,
            border_width=1,
            border_color=theme.EDGE,
            command=self._choose,
        )
        self._main = ctk.CTkButton(
            self,
            text="",
            height=ROW_HEIGHT,
            corner_radius=CORNER_RADIUS,
            font=self._font,
            border_width=0,
            command=self._click,
        )
        add_tooltip(self._more, MORE_TIP)
        self._main_tip = add_tooltip(self._main, "")  # only for a shortened Download text
        self._default_text_color = self._main.cget("text_color")
        self._label = ctk.CTkLabel(
            self, text="", height=ROW_HEIGHT, font=self._label_font, text_color=theme.TEXT_MUTED
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

    def repaint(self) -> None:
        look = control_look(self._base, self._actions.states.get(self.key))
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
                    self._more.pack(side="right", padx=(GAP, 0))
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
            return
        if self._width is not None:
            width = self._width - (MORE_WIDTH + GAP if look.layout == "split" else 0)
            room = width - 2 * CORNER_RADIUS - SLACK
            text = fit_text(look.texts, room, self._font.measure)
        self._main.configure(text=text)
        shortened = look.action == "download" and text != look.text
        self._main_tip.set_text(DOWNLOAD_TIP if shortened else "")

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
