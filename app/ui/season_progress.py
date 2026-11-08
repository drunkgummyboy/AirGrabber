"""A show window's season progress (owner's choice C of docs/season-progress-sketch.html): a thin
bar under every season chip and, above the open season's rows, a square per episode. Green: in
Plex; blue: downloaded, not in Plex yet; amber: aired, neither; grey: not aired yet.

Only for a followed show while Plex is connected (`progress_shown`): what you have counts only
then (owner's decision for 3.1.0), and `ShowDetails.in_plex` is filled only for followed shows.
Plain canvases in the theme's colours, painted again on a switch between light and dark."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

import tkinter as tk

import customtkinter as ctk

from app.i18n import t
from app.models import Episode, ShowDetails
from app.ui import theme
from app.ui.missing_control import MISSING_COLOR

EpisodeState = Literal["plex", "downloaded", "missing", "later"]
STATE_ORDER: tuple[EpisodeState, ...] = ("plex", "downloaded", "missing", "later")


def state_color(state: EpisodeState) -> theme.Color:
    """A state's colour, read when painted: the accent palette is set after import."""
    if state == "plex":
        return theme.SUCCESS
    if state == "downloaded":
        return theme.LINK
    if state == "missing":
        return MISSING_COLOR
    return theme.CHIP_HOVER  # a quiet grey, unlike the blue of "downloaded"

STRIP_HEIGHT = 4
SQUARE = 18
SQUARE_GAP = 4
SQUARES_PER_ROW = 13  # a long season wraps


def progress_shown(details: ShowDetails | None) -> bool:
    """Whether the window shows season progress: a followed show, with Plex connected."""
    return details is not None and details.plex_connected and details.tracked


def episode_state(details: ShowDetails, episode: Episode, now: datetime) -> EpisodeState:
    if details.is_in_plex(episode):
        return "plex"
    if not episode.has_aired(now):
        return "later"
    if details.is_downloaded(episode):
        return "downloaded"
    return "missing"


def season_states(
    details: ShowDetails, season: int, now: datetime
) -> list[tuple[Episode, EpisodeState]]:
    """The season's numbered episodes with their states, in order."""
    return [
        (episode, episode_state(details, episode, now))
        for episode in details.episodes
        if episode.season == season and episode.number is not None
    ]


def had_text(states: Sequence[EpisodeState]) -> str:
    """A chip's line: "9 of 12" (in Plex or downloaded, of all its episodes)."""
    had = sum(state in ("plex", "downloaded") for state in states)
    return t("{had} of {total}", had=had, total=len(states))


def counts_text(states: Sequence[EpisodeState]) -> str:
    """The squares' line: "7 in Plex · 2 downloaded · 1 not downloaded" (what there is)."""
    counts = Counter(states)
    parts = []
    if counts["plex"]:
        parts.append(t("{n} in Plex", n=counts["plex"]))
    if counts["downloaded"]:
        parts.append(t("{n} downloaded", n=counts["downloaded"]))
    if counts["missing"]:
        parts.append(t("{n} not downloaded", n=counts["missing"]))
    return " · ".join(parts)


def backdrop(widget: Any) -> theme.Color:
    """The colour behind `widget`: its nearest master with a colour of its own."""
    master = widget
    while master is not None:
        try:
            color = master.cget("fg_color")
        except (AttributeError, ValueError, tk.TclError):
            color = None
        if color is not None and color != "transparent":
            return color  # type: ignore[no-any-return]
        master = getattr(master, "master", None)
    return theme.BG


class SeasonStrip(ctk.CTkCanvas):
    """The thin bar under a season chip: its episodes' states as stretches, in STATE_ORDER."""

    def __init__(self, master: Any, width: int) -> None:
        self._scale = float(ctk.ScalingTracker.get_widget_scaling(master)) or 1.0
        self._width = round(width * self._scale)
        super().__init__(
            master,
            width=self._width,
            height=round(STRIP_HEIGHT * self._scale),
            highlightthickness=0,
            bd=0,
        )
        self._states: list[EpisodeState] = []
        self._behind = backdrop(master)
        theme.on_mode_change(self, self._repaint)

    def show(self, states: Sequence[EpisodeState]) -> None:
        self._states = list(states)
        self._repaint()

    def _repaint(self) -> None:
        self.delete("all")
        self.configure(background=theme.pick(self._behind))
        height = round(STRIP_HEIGHT * self._scale)
        total = len(self._states)
        if not total:
            return
        counts = Counter(self._states)
        x = 0.0
        for state in STATE_ORDER:
            if not counts[state]:
                continue
            width = self._width * counts[state] / total
            self.create_rectangle(
                round(x), 0, round(x + width), height,
                fill=theme.pick(state_color(state)), outline="",
            )
            x += width


class EpisodeSquares(ctk.CTkCanvas):
    """A square per episode with its number, SQUARES_PER_ROW to a row."""

    def __init__(self, master: Any) -> None:
        self._scale = float(ctk.ScalingTracker.get_widget_scaling(master)) or 1.0
        super().__init__(master, width=1, height=1, highlightthickness=0, bd=0)
        self._items: list[tuple[int, EpisodeState]] = []
        self._font = theme.font(9, bold=True)
        self._behind = backdrop(master)
        theme.on_mode_change(self, self._repaint)

    def show(self, items: Sequence[tuple[int, EpisodeState]]) -> None:
        """(episode number, state) per square."""
        self._items = list(items)
        self._repaint()

    def _repaint(self) -> None:
        self.delete("all")
        self.configure(background=theme.pick(self._behind))
        scale = self._scale
        side, gap = round(SQUARE * scale), round(SQUARE_GAP * scale)
        columns = min(len(self._items), SQUARES_PER_ROW)
        rows = -(-len(self._items) // SQUARES_PER_ROW) if self._items else 0
        self.configure(
            width=max(1, columns * (side + gap) - gap), height=max(1, rows * (side + gap) - gap)
        )
        font = self._font.create_scaled_tuple(scale)
        for index, (number, state) in enumerate(self._items):
            x = (index % SQUARES_PER_ROW) * (side + gap)
            y = (index // SQUARES_PER_ROW) * (side + gap)
            self.create_rectangle(
                x, y, x + side, y + side, fill=theme.pick(state_color(state)), outline=""
            )
            ink = theme.TEXT_MUTED if state == "later" else self._behind
            self.create_text(
                x + side / 2, y + side / 2, text=str(number), font=font, fill=theme.pick(ink)
            )
