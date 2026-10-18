"""Show cards: the Tracked grid's `ShowCard` and the search results' poster `SearchShowCard`."""

from __future__ import annotations

import webbrowser
from collections.abc import Callable
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

import customtkinter as ctk

from app.models import DownloadRecord, Episode, Show, ShowActivity
from app.ui import theme
from app.ui.poster_card import PosterCard, TrackButton, TrackState, year_rating_line
from app.ui.widgets import PosterImage

if TYPE_CHECKING:
    from app.services.images import ImageCache

CARD_WIDTH = 245
_TITLE_MAX = 20
TVMAZE_MEDIUM_WIDTH = 210  # TVMaze's "medium" posters are 210 x 295

AUTO_SWITCH_TEXT = "Download new episodes"
_LIBRARY_HEIGHT = 120
# The Tracked view's extra rows under the poster: info line + switch.
_AUTO_GAP = 4  # + the poster's 10 px bottom padding = 14 px above the rows
_INFO_HEIGHT = 18
_SWITCH_HEIGHT = 24
_AUTO_BOTTOM = 10
_AUTO_HEIGHT = _AUTO_GAP + _INFO_HEIGHT + _SWITCH_HEIGHT + _AUTO_BOTTOM
_RECENT_GRAB_DAYS = 6  # "today" … six days ago; older grabs aren't mentioned
_NEAR_DAYS = 6  # a next episode up to this many days away is named by its weekday

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _truncate(text: str) -> str:
    return text if len(text) <= _TITLE_MAX else text[: _TITLE_MAX - 3] + "…"


def _grab_code(record: DownloadRecord) -> str:
    season = record.season or 0
    if record.episode is None:  # a season pack
        return f"S{season:02d}"
    return f"S{season:02d}E{record.episode:02d}"


def _air_day(episode: Episode, now: datetime) -> date | None:
    if episode.airstamp is not None:
        return episode.airstamp.astimezone(now.tzinfo).date()
    return episode.air_date


def card_info_line(activity: ShowActivity | None, now: datetime) -> str | None:
    """The Tracked card's muted line above its switch, the first that applies:
    "Downloaded S02E08 · today" (a grab within the past week: today / yesterday / weekday),
    "Next: S02E04 · Thu" (the next episode; "23 Oct" when more than six days away), or None.

    `now` is an aware instant; its time zone decides the days (the app passes local time).
    """
    if activity is None:
        return None
    today = now.date()
    grab = activity.last_grab
    if grab is not None:
        grabbed = grab.downloaded_at.astimezone(now.tzinfo).date()
        days_ago = (today - grabbed).days
        if 0 <= days_ago <= _RECENT_GRAB_DAYS:
            when = (
                "today"
                if days_ago == 0
                else "yesterday" if days_ago == 1 else _WEEKDAYS[grabbed.weekday()]
            )
            return f"Downloaded {_grab_code(grab)} · {when}"
    episode = activity.next_episode
    if episode is not None and (day := _air_day(episode, now)) is not None:
        days_ahead = (day - today).days
        if days_ahead >= 0:
            when = (
                _WEEKDAYS[day.weekday()]
                if days_ahead <= _NEAR_DAYS
                else f"{day.day} {_MONTHS[day.month - 1]}"
            )
            return f"Next: {episode.code} · {when}"
    return None


def auto_switch_color(on: bool) -> str:
    """The switch label's colour: plain text when on, muted when off."""
    return theme.TEXT if on else theme.TEXT_MUTED


def tvmaze_poster_url(show: Show, width_px: int) -> str | None:
    """The show's poster for a card `width_px` physical pixels wide: TVMaze's medium one (210
    wide) when that is enough, else the original."""
    if width_px <= TVMAZE_MEDIUM_WIDTH:
        return show.poster_url or show.poster_url_original
    return show.poster_url_original or show.poster_url


class ShowCard(ctk.CTkFrame):
    """The Tracked grid's card: poster, title, status, and IMDb / remove buttons.

    With `on_auto_download`, the card also shows `info_line` and the "Download new episodes"
    switch (state `auto_download`) under the poster; toggling it calls
    `on_auto_download(show, on)`. With `on_open`, a click on the poster or the title calls
    `on_open(show)`.
    """

    def __init__(
        self,
        master: Any,
        show: Show,
        *,
        images: ImageCache,
        on_remove: Callable[[Show], None] | None = None,
        auto_download: bool = False,
        info_line: str | None = None,
        on_auto_download: Callable[[Show, bool], None] | None = None,
        on_open: Callable[[Show], None] | None = None,
    ) -> None:
        with_switch = on_auto_download is not None
        super().__init__(
            master,
            fg_color=theme.SURFACE,
            border_color=theme.EDGE,
            border_width=1,
            corner_radius=8,
            width=CARD_WIDTH,
            height=_LIBRARY_HEIGHT + (_AUTO_HEIGHT if with_switch else 0),
        )
        self.grid_propagate(False)
        self.pack_propagate(False)
        self._show = show
        self._on_remove = on_remove
        self._on_auto_download = on_auto_download
        self._auto_switch: ctk.CTkSwitch | None = None
        self._auto_var = ctk.BooleanVar(value=auto_download)

        if with_switch:  # packed first, so the poster row keeps its height above it
            self._build_auto_rows(info_line)

        poster = PosterImage(self, width=68, height=100)
        poster.pack(side="left", padx=8, pady=10)
        poster.show(images, show.poster_url)

        info = ctk.CTkFrame(self, fg_color="transparent")
        info.pack(side="left", fill="both", expand=True, padx=(0, 5), pady=10)

        title = ctk.CTkLabel(
            info,
            text=_truncate(show.name),
            font=theme.font(12, bold=True),
            text_color=theme.TEXT,
            anchor="w",
        )
        title.pack(fill="x")
        if on_open is not None:
            poster.set_on_click(lambda: on_open(show))
            title.configure(cursor="hand2")
            title.bind("<Button-1>", lambda _event: on_open(show))
        ctk.CTkLabel(
            info,
            text=f"Status: {show.status or 'Unknown'}",
            font=theme.font(9),
            text_color="gray50",
            anchor="w",
        ).pack(fill="x")

        bottom = ctk.CTkFrame(info, fg_color="transparent")
        bottom.pack(side="bottom", fill="x")
        ctk.CTkButton(
            bottom,
            text="remove",
            width=40,
            height=20,
            font=theme.font(10, bold=True),
            fg_color=theme.DANGER_BUTTON,
            hover_color=theme.DANGER_BUTTON_HOVER,
            border_width=0,
            command=self._remove,
        ).pack(side="right")
        if imdb_url := show.imdb_url:
            ctk.CTkButton(
                bottom,
                text="IMDb",
                width=40,
                height=20,
                font=theme.font(10, bold=True),
                fg_color=theme.IMDB,
                text_color="black",
                hover_color=theme.IMDB_HOVER,
                border_width=0,
                command=lambda: webbrowser.open(imdb_url),
            ).pack(side="right", padx=(0, 5))

    def _build_auto_rows(self, info_line: str | None) -> None:
        rows = ctk.CTkFrame(self, fg_color="transparent", height=_AUTO_HEIGHT - _AUTO_GAP)
        rows.pack(side="bottom", fill="x", padx=10, pady=(_AUTO_GAP, _AUTO_BOTTOM))
        # The line keeps its place when empty, so every card in the grid is equally tall.
        ctk.CTkLabel(
            rows,
            text=info_line or "",
            height=_INFO_HEIGHT,
            font=theme.font(13),
            text_color=theme.TEXT_MUTED,
            anchor="w",
        ).pack(fill="x")
        self._auto_switch = ctk.CTkSwitch(
            rows,
            text=AUTO_SWITCH_TEXT,
            variable=self._auto_var,
            height=_SWITCH_HEIGHT,
            font=theme.font(14),
            text_color=auto_switch_color(self._auto_var.get()),
            progress_color=theme.ACCENT,
            command=self._auto_toggled,
        )
        self._auto_switch.pack(anchor="w")

    @property
    def auto_download(self) -> bool:
        return bool(self._auto_var.get())

    def set_auto_download(self, on: bool) -> None:
        """Show the switch on or off without calling back (e.g. to undo a failed change)."""
        self._auto_var.set(on)
        if self._auto_switch is not None:
            self._auto_switch.configure(text_color=auto_switch_color(on))

    def _auto_toggled(self) -> None:
        on = self._auto_var.get()
        if self._auto_switch is not None:
            self._auto_switch.configure(text_color=auto_switch_color(on))
        if self._on_auto_download is not None:
            self._on_auto_download(self._show, on)

    def _remove(self) -> None:
        if self._on_remove is not None:
            self._on_remove(self._show)


class SearchShowCard(PosterCard):
    """A search result's TVMaze show as a poster card: year and rating, and a Track button.
    Clicking the cover calls `on_open_show(show)`; without it, it opens the show on IMDb (when
    TVMaze knows its IMDb id)."""

    def __init__(
        self,
        master: Any,
        show: Show,
        *,
        images: ImageCache,
        is_tracked: bool,
        on_track: Callable[[Show], None],
        on_open_show: Callable[[Show], None] | None = None,
    ) -> None:
        super().__init__(
            master,
            title=show.name,
            meta=year_rating_line(show.year, show.rating),
            poster=lambda width: tvmaze_poster_url(show, width),
            images=images,
        )
        self._show = show
        self._on_track = on_track
        self._track_button = TrackButton(self, busy_text="Adding…", command=self._track)
        self._pack_action(self._track_button)
        self.set_track_state("tracked" if is_tracked else "idle")
        if on_open_show is not None:
            self._set_open(lambda: on_open_show(show))
        elif (imdb_url := show.imdb_url) is not None:
            self._set_open(lambda: webbrowser.open(imdb_url))

    def set_track_state(self, state: TrackState) -> None:
        """idle → "+ Track"; busy → "Adding…"; tracked → "✓ Following"."""
        self._track_button.set_state(state)

    def _track(self) -> None:
        self._on_track(self._show)
