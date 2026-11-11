"""▶ Play in the info windows (app/ui/movie_details.py, app/ui/show_details.py): a title that is
in Plex opens in VLC on this PC (app/services/playback.py finds its file through MediaForge's
Series / Movies folders).

`play_button` is the buttons row's outlined "▶ Play" (tip "Play in VLC"); `Play` runs a play and
says on the status bar how it went (one at a time per window). A film plays at once; a show first
opens the EpisodePicker: borderless in AirGrabber's dialog look, its seasons as chips and the
season's episodes as rows ("E03 · Title", a muted ✓ on a watched one), the next unwatched one
(`playback.next_episode`) selected and scrolled into view. A click on a row, or Enter on the
selection, plays it and closes the window; Up / Down move the selection; Escape and ✕ close it.
"""

from __future__ import annotations

import asyncio
import logging
import tkinter as tk
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import customtkinter as ctk

from app.i18n import N_, t
from app.models.plex import PlexTitle, PlexVideo
from app.services.playback import FINDING_TEXT, PlayOutcome, episode_name, next_episode
from app.services.plex import PlexError
from app.ui import theme
from app.ui.borderless import make_borderless
from app.ui.branding import set_window_icon
from app.ui.dialog_kit import (
    CLOSE_CLEARANCE,
    DETAIL_SIZE,
    LINE_GAP,
    PAD_X,
    PAD_Y,
    activate_later,
    heading_row,
    place_over,
    text_label,
)
from app.ui.scrollable import ScrollableFrame
from app.ui.send_to_phone import (
    LOADING_TEXT,
    NO_EPISODES_TEXT,
    PLEX_AWAY_TEXT,
    season_text,
)
from app.ui.tasks import spawn
from app.ui.tooltip import add_tooltip

if TYPE_CHECKING:
    from app.services.playback import PlaybackService
    from app.ui.views.base import StatusReporter

log = logging.getLogger(__name__)

PLAY_TEXT = N_("▶ Play")
PLAY_TIP = N_("Play in VLC")
PICK_TEXT = N_("Choose an episode to play.")
WATCHED_MARK = "✓"
PLAY_WIDTH = 100
STATUS_MS = 8000  # how long the status bar keeps what Play said
WIDTH = 460  # the picker's list (CTk units)
LIST_HEIGHT = 340
ROW_HEIGHT = 32
CHIP_WIDTH = 92
CHIP_HEIGHT = 30
CHIPS_PER_ROW = 4


def play_button(
    master: Any, command: Callable[[], None], *, height: int, font_size: int = 15, radius: int = 8
) -> ctk.CTkButton:
    """The buttons row's outlined "▶ Play", as tall as the row's other buttons."""
    button = ctk.CTkButton(
        master,
        text=t(PLAY_TEXT),
        width=PLAY_WIDTH,
        height=height,
        corner_radius=radius,
        font=theme.font(font_size, bold=True),
        fg_color="transparent",
        hover_color=theme.HOVER,
        text_color=theme.BUTTON_TEXT,
        text_color_disabled=theme.TEXT_MUTED,
        border_width=1,
        border_color=theme.EDGE,
        command=command,
    )
    add_tooltip(button, t(PLAY_TIP))
    return button


def episode_line(video: PlexVideo) -> str:
    """"E03 · Title" (the picker's row)."""
    code = f"E{video.episode:02d}" if video.episode is not None else ""
    return " · ".join(part for part in (code, video.title) if part)


class Play:
    """One window's ▶ Play: plays through `playback`, the outcome on the status bar."""

    def __init__(self, playback: PlaybackService, status: StatusReporter) -> None:
        self._playback = playback
        self._status = status
        self._task: asyncio.Task[None] | None = None
        self.picker: EpisodePicker | None = None

    @property
    def busy(self) -> bool:
        return self._task is not None and not self._task.done()

    def film(self, title: PlexTitle) -> None:
        """Play a film at once."""
        self._run(lambda: self._playback.play_film(title), title.title)

    def episode(self, title: PlexTitle, video: PlexVideo) -> None:
        self._run(lambda: self._playback.play(title, video), episode_name(title.title, video))

    def pick(self, master: Any, title: PlexTitle) -> EpisodePicker:
        """A show: the episode picker (one per window; an open one is raised)."""
        picker = self.picker
        if picker is not None and picker.winfo_exists():
            picker.lift()
            picker.focus_force()
            return picker
        self.picker = EpisodePicker(
            master,
            title,
            episodes=self._playback.episodes,
            on_play=lambda video: self.episode(title, video),
        )
        return self.picker

    def _run(self, play: Callable[[], Awaitable[PlayOutcome]], name: str) -> None:
        if self.busy:  # one at a time: a second click while it looks does nothing
            return
        self._report(t(FINDING_TEXT, name=name), None)
        self._task = spawn(self._play(play), name="play")

    async def _play(self, play: Callable[[], Awaitable[PlayOutcome]]) -> None:
        outcome = await play()
        self._report(outcome.text, STATUS_MS)

    def _report(self, text: str, clear_after_ms: int | None) -> None:
        try:  # the status bar belongs to the main window, which may be gone by now
            self._status(text, clear_after_ms=clear_after_ms)
        except tk.TclError:
            pass

    async def settled(self) -> None:
        """Wait for a play (tests)."""
        if self._task is not None and not self._task.done():
            await asyncio.wait({self._task})

    def close(self) -> None:
        """The window closes: its picker goes too; a play under way finishes."""
        if self.picker is not None and self.picker.winfo_exists():
            self.picker.close()
        self.picker = None


class EpisodePicker(ctk.CTkToplevel):
    """Which episode of a show ▶ Play plays (see the module docstring)."""

    def __init__(
        self,
        master: Any,
        title: PlexTitle,
        *,
        episodes: Callable[[PlexTitle], Awaitable[list[PlexVideo]]],
        on_play: Callable[[PlexVideo], None],
    ) -> None:
        super().__init__(master, fg_color=theme.BG)
        self.title(title.title)
        set_window_icon(self)
        self.resizable(False, False)
        self.transient(master)
        self.protocol("WM_DELETE_WINDOW", self.close)
        self._item = title
        self._read = episodes
        self._on_play = on_play
        self._videos: list[PlexVideo] = []
        self.season: int | None = None
        self.selected: PlexVideo | None = None
        self.chips: dict[int | None, ctk.CTkButton] = {}
        self.rows: dict[str, ctk.CTkButton] = {}  # by an episode's key
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)
        ctk.CTkFrame(body, width=WIDTH, height=1, fg_color="transparent").pack()
        heading = heading_row(body, title.title, right_room=CLOSE_CLEARANCE)
        heading.pack(fill="x")
        self.note = text_label(
            body, t(LOADING_TEXT), size=DETAIL_SIZE, color=theme.TEXT_MUTED, wraplength=WIDTH
        )
        self.note.pack(anchor="w", pady=(LINE_GAP, 0))
        self._chip_row = ctk.CTkFrame(body, fg_color="transparent")
        self._chip_row.pack(fill="x", pady=(LINE_GAP + 4, 0))
        self._list = ScrollableFrame(body, fg_color="transparent", width=WIDTH, height=LIST_HEIGHT)
        self._list.pack(fill="both", expand=True, pady=(LINE_GAP, 0))
        self._list.grid_columnconfigure(0, weight=1)
        self.bind("<Escape>", lambda _event: self.close())
        self.bind("<Return>", lambda _event: self.play_selected())
        self.bind("<KP_Enter>", lambda _event: self.play_selected())
        self.bind("<Up>", lambda _event: self.move(-1))
        self.bind("<Down>", lambda _event: self.move(1))
        self.borderless = make_borderless(self, drag_widgets=(self, body, heading))
        place_over(self, master)
        activate_later(self)
        self._task: asyncio.Task[None] | None = spawn(self._load(), name="play-episodes")

    async def _load(self) -> None:
        try:
            videos = await self._read(self._item)
        except PlexError as exc:
            log.info("Play: the episodes didn't read (%s)", type(exc).__name__)
            if self.winfo_exists():
                self.note.configure(text=t(PLEX_AWAY_TEXT))
            return
        if not self.winfo_exists():
            return
        self._videos = videos
        chosen = next_episode(videos)
        if chosen is None:
            self.note.configure(text=t(NO_EPISODES_TEXT))
            return
        self.note.configure(text=t(PICK_TEXT))
        self._build_chips()
        self.choose_season(chosen.season, select=chosen)

    async def loaded(self) -> None:
        """Wait for the episodes (tests)."""
        if self._task is not None and not self._task.done():
            await asyncio.wait({self._task})

    # -- seasons -----------------------------------------------------------------------------

    def _seasons(self) -> list[int | None]:
        seasons: list[int | None] = []
        for video in self._videos:  # in order: the specials last (PlexService.show_videos)
            if video.season not in seasons:
                seasons.append(video.season)
        return seasons

    def _build_chips(self) -> None:
        seasons = self._seasons()
        if len(seasons) < 2:
            self._chip_row.pack_forget()  # one season: nothing to choose
            return
        for index, season in enumerate(seasons):
            chip = ctk.CTkButton(
                self._chip_row,
                text=season_text(season),
                width=CHIP_WIDTH,
                height=CHIP_HEIGHT,
                corner_radius=CHIP_HEIGHT // 2,
                font=theme.font(13),
                command=lambda s=season: self.choose_season(s),
                **theme.choice_colors(False),
            )
            chip.grid(
                row=index // CHIPS_PER_ROW, column=index % CHIPS_PER_ROW, padx=(0, 6), pady=(0, 6)
            )
            self.chips[season] = chip

    def choose_season(self, season: int | None, *, select: PlexVideo | None = None) -> None:
        """Show a season's episodes, `select` (else its next unwatched one) selected."""
        self.season = season
        for chip_season, chip in self.chips.items():
            chip.configure(**theme.choice_colors(chip_season == season))
        for row in self.rows.values():
            row.master.destroy()
        self.rows = {}
        shown = self._shown()
        for index, video in enumerate(shown):
            self._row(index, video)
        self.select(select if select in shown else next_episode(shown))

    def _shown(self) -> list[PlexVideo]:
        return [video for video in self._videos if video.season == self.season]

    def _row(self, index: int, video: PlexVideo) -> None:
        row = ctk.CTkFrame(self._list, fg_color="transparent")
        row.grid(row=index, column=0, sticky="ew", pady=(0, 2))
        row.grid_columnconfigure(0, weight=1)
        button = ctk.CTkButton(
            row,
            text=episode_line(video),
            height=ROW_HEIGHT,
            corner_radius=6,
            anchor="w",
            font=theme.font(DETAIL_SIZE),
            fg_color="transparent",
            hover_color=theme.HOVER,
            text_color=theme.BUTTON_TEXT,
            command=lambda: self.play(video),
        )
        button.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(
            row,
            text=WATCHED_MARK if video.watched else "",
            width=24,
            font=theme.font(DETAIL_SIZE),
            text_color=theme.TEXT_MUTED,
        ).grid(row=0, column=1, padx=(4, 0))
        self.rows[video.key] = button

    # -- choosing ----------------------------------------------------------------------------

    def select(self, video: PlexVideo | None) -> None:
        """Mark `video`'s row as the selection and scroll it into view."""
        self.selected = video
        for key, button in self.rows.items():
            chosen = video is not None and key == video.key
            if chosen:
                button.configure(fg_color=theme.ROW_SELECTED, hover_color=theme.SELECTED_HOVER)
            else:
                button.configure(fg_color="transparent", hover_color=theme.HOVER)
        if video is not None:
            self.after_idle(self._scroll_to, video.key)

    def _scroll_to(self, key: str) -> None:
        button = self.rows.get(key)
        if button is None or not button.winfo_exists():
            return
        try:
            canvas = self._list._parent_canvas  # CTk has no scroll API
            box = canvas.bbox("all")
            if not box or box[3] <= 0:
                return
            row = button.master
            top, height = row.winfo_y(), row.winfo_height()
            seen_top = canvas.canvasy(0)
            seen_bottom = seen_top + canvas.winfo_height()
            if top < seen_top or top + height > seen_bottom:
                canvas.yview_moveto(max(0, top - height) / box[3])
        except tk.TclError:
            pass

    def move(self, step: int) -> None:
        """Up / Down: the row above or below the selection."""
        shown = self._shown()
        if not shown:
            return
        index = shown.index(self.selected) if self.selected in shown else -step
        self.select(shown[max(0, min(len(shown) - 1, index + step))])

    def play_selected(self) -> None:
        if self.selected is not None:
            self.play(self.selected)

    def play(self, video: PlexVideo) -> None:
        """Play `video` and close the picker."""
        self.close()
        self._on_play(video)

    def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
        if self.winfo_exists():
            self.destroy()

