"""Show cards: the Tracked grid's `ShowCard` and the search results' poster `SearchShowCard`.

Both are drawn at a `CardScale` (Settings, Card size: app/ui/card_scale.py); `show_card_layout`
holds the My shows card's sizes at a scale.
"""

from __future__ import annotations

import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

import customtkinter as ctk

from app.models import DownloadRecord, Episode, Show, ShowActivity
from app.ui import theme
from app.ui.card_scale import FULL, SMALL_TEXT_MIN, TEXT_MIN, CardScale
from app.ui.missing_control import MISSING_COLOR, MissingActions, MissingButton, chip, missing_text
from app.ui.poster_card import PosterCard, TrackButton, TrackState, year_rating_line
from app.ui.text_fit import SLACK, elide_to_width
from app.ui.tooltip import add_tooltip
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
# With missing episodes shown: a row above the info line, "3 missing" + "⬇ Download missing".
_MISSING_HEIGHT = 26
_MISSING_GAP = 8  # below that row
_MISSING_ROOM = _MISSING_HEIGHT + _MISSING_GAP
_MISSING_FONT = 13
# The rest of the card at XL: poster, paddings, fonts, the small buttons, the switch.
_POSTER_SIZE = (68, 100)
_POSTER_PAD = 8  # left and right of the poster
_ROW_PAD = 10  # above and below the poster; left and right of the rows under it
_INFO_PAD = 5  # right of the title column
_TITLE_FONT = 12
_STATUS_FONT = 9
_BUTTON_FONT = 10
_BUTTON_SIZE = (40, 20)  # remove / IMDb
_BUTTON_GAP = 5
_INFO_FONT = 13
_SWITCH_FONT = 14
_SWITCH_SIZE = (36, 18)  # CTkSwitch's own default track
_SWITCH_TEXT_GAP = 6  # CTkSwitch's gap between the track and its text
_CHIP_GAP = 8  # right of the "3 missing" chip
_RECENT_GRAB_DAYS = 6  # "today" … six days ago; older grabs aren't mentioned
_NEAR_DAYS = 6  # a next episode up to this many days away is named by its weekday

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _truncate(text: str) -> str:
    return text if len(text) <= _TITLE_MAX else text[: _TITLE_MAX - 3] + "…"


@dataclass(frozen=True, slots=True)
class ShowCardLayout:
    """A My shows card's sizes (CTk units) and font sizes at one card size."""

    width: int
    library_height: int  # the poster row
    poster: tuple[int, int]
    poster_pad: int
    row_pad: int
    info_pad: int
    title_font: int
    status_font: int
    button_font: int
    button: tuple[int, int]
    button_gap: int
    auto_gap: int
    info_height: int
    info_font: int
    switch_height: int
    switch_font: int
    switch: tuple[int, int]
    auto_bottom: int
    missing_height: int
    missing_gap: int
    missing_font: int
    chip_gap: int

    @property
    def auto_height(self) -> int:
        """The info line and the switch under the poster row, gaps included."""
        return self.auto_gap + self.info_height + self.switch_height + self.auto_bottom

    @property
    def missing_room(self) -> int:
        return self.missing_height + self.missing_gap

    def height(self, *, with_switch: bool, with_missing: bool) -> int:
        return (
            self.library_height
            + (self.auto_height if with_switch else 0)
            + (self.missing_room if with_switch and with_missing else 0)
        )

    @property
    def title_room(self) -> int:
        """The title and status lines' width: beside the poster, inside the 1-unit border."""
        return self.width - 2 - self.poster[0] - 2 * self.poster_pad - self.info_pad - SLACK

    @property
    def row_room(self) -> int:
        """The width of the rows under the poster (the info line)."""
        return self.width - 2 - 2 * self.row_pad - SLACK

    @property
    def switch_text_room(self) -> int:
        """Room for the switch's text: the row's width less the track and its gap."""
        return self.row_room - self.switch[0] - _SWITCH_TEXT_GAP


def show_card_layout(scale: CardScale = FULL) -> ShowCardLayout:
    """The My shows card at `scale`: everything shrinks with the card, the fonts down to their
    minimums (card_scale), each text row at least as tall as its font needs."""
    poster_w, poster_h = _POSTER_SIZE
    poster = (scale.px(poster_w), scale.px(poster_h))
    row_pad = scale.px(_ROW_PAD)
    title_font = scale.font(_TITLE_FONT, TEXT_MIN)
    status_font = scale.font(_STATUS_FONT, SMALL_TEXT_MIN)
    button_font = scale.font(_BUTTON_FONT, SMALL_TEXT_MIN)
    info_font = scale.font(_INFO_FONT, SMALL_TEXT_MIN)
    switch_font = scale.font(_SWITCH_FONT, TEXT_MIN)
    missing_font = scale.font(_MISSING_FONT, TEXT_MIN)
    button_w, button_h = _BUTTON_SIZE
    switch_w, switch_h = _SWITCH_SIZE
    return ShowCardLayout(
        width=scale.px(CARD_WIDTH),
        library_height=max(scale.px(_LIBRARY_HEIGHT), poster[1] + 2 * row_pad),
        poster=poster,
        poster_pad=scale.px(_POSTER_PAD),
        row_pad=row_pad,
        info_pad=scale.px(_INFO_PAD),
        title_font=title_font,
        status_font=status_font,
        button_font=button_font,
        button=(scale.px(button_w), scale.text_height(button_h, button_font)),
        button_gap=scale.px(_BUTTON_GAP),
        auto_gap=scale.px(_AUTO_GAP),
        info_height=scale.text_height(_INFO_HEIGHT, info_font),
        info_font=info_font,
        switch_height=scale.text_height(_SWITCH_HEIGHT, switch_font),
        switch_font=switch_font,
        switch=(scale.px(switch_w), scale.px(switch_h)),
        auto_bottom=scale.px(_AUTO_BOTTOM),
        missing_height=scale.text_height(_MISSING_HEIGHT, missing_font),
        missing_gap=scale.px(_MISSING_GAP),
        missing_font=missing_font,
        chip_gap=scale.px(_CHIP_GAP),
    )


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
    `on_auto_download(show, on)`. With `missing_actions` as well, a row above them shows the
    show's `missing` episodes ("3 missing" + "⬇ Download missing"; blank but kept when none, so
    every card is equally tall). With `on_open`, a click on the poster or the title calls
    `on_open(show)`. `scale` sizes the whole card (`show_card_layout`).
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
        missing: int = 0,
        missing_actions: MissingActions | None = None,
        scale: CardScale = FULL,
    ) -> None:
        with_switch = on_auto_download is not None
        with_missing = with_switch and missing_actions is not None
        layout = self._layout = show_card_layout(scale)
        super().__init__(
            master,
            fg_color=theme.SURFACE,
            border_color=theme.EDGE,
            border_width=1,
            corner_radius=scale.px(8),
            width=layout.width,
            height=layout.height(with_switch=with_switch, with_missing=with_missing),
        )
        self.grid_propagate(False)
        self.pack_propagate(False)
        self._show = show
        self._on_remove = on_remove
        self._on_auto_download = on_auto_download
        self._auto_switch: ctk.CTkSwitch | None = None
        self._auto_var = ctk.BooleanVar(value=auto_download)
        self.missing_button: MissingButton | None = None

        if with_switch:  # packed first, so the poster row keeps its height above it
            rows = self._build_auto_rows(info_line, with_missing=with_missing)
            if with_missing:
                assert missing_actions is not None
                self._build_missing_row(rows, missing, missing_actions)

        poster_w, poster_h = layout.poster
        poster = PosterImage(self, width=poster_w, height=poster_h)
        poster.pack(side="left", padx=layout.poster_pad, pady=layout.row_pad)
        poster.show(images, show.poster_url)

        info = ctk.CTkFrame(self, fg_color="transparent")
        info.pack(
            side="left", fill="both", expand=True, padx=(0, layout.info_pad), pady=layout.row_pad
        )

        title_font = theme.font(layout.title_font, bold=True)
        title = ctk.CTkLabel(
            info,
            text=elide_to_width(_truncate(show.name), layout.title_room, title_font.measure),
            font=title_font,
            text_color=theme.TEXT,
            anchor="w",
        )
        title.pack(fill="x")
        if on_open is not None:
            poster.set_on_click(lambda: on_open(show))
            title.configure(cursor="hand2")
            title.bind("<Button-1>", lambda _event: on_open(show))
        status_font = theme.font(layout.status_font)
        ctk.CTkLabel(
            info,
            text=elide_to_width(
                f"Status: {show.status or 'Unknown'}", layout.title_room, status_font.measure
            ),
            font=status_font,
            text_color="gray50",
            anchor="w",
        ).pack(fill="x")

        bottom = ctk.CTkFrame(info, fg_color="transparent")
        bottom.pack(side="bottom", fill="x")
        button_w, button_h = layout.button
        ctk.CTkButton(
            bottom,
            text="remove",
            width=button_w,
            height=button_h,
            font=theme.font(layout.button_font, bold=True),
            fg_color=theme.DANGER_BUTTON,
            hover_color=theme.DANGER_BUTTON_HOVER,
            border_width=0,
            command=self._remove,
        ).pack(side="right")
        if imdb_url := show.imdb_url:
            ctk.CTkButton(
                bottom,
                text="IMDb",
                width=button_w,
                height=button_h,
                font=theme.font(layout.button_font, bold=True),
                fg_color=theme.IMDB,
                text_color="black",
                hover_color=theme.IMDB_HOVER,
                border_width=0,
                command=lambda: webbrowser.open(imdb_url),
            ).pack(side="right", padx=(0, layout.button_gap))

    def _build_auto_rows(self, info_line: str | None, *, with_missing: bool) -> ctk.CTkFrame:
        layout = self._layout
        rows = ctk.CTkFrame(
            self,
            fg_color="transparent",
            height=layout.auto_height
            - layout.auto_gap
            + (layout.missing_room if with_missing else 0),
        )
        rows.pack(
            side="bottom", fill="x", padx=layout.row_pad, pady=(layout.auto_gap, layout.auto_bottom)
        )
        # The line keeps its place when empty, so every card in the grid is equally tall.
        info_font = theme.font(layout.info_font)
        ctk.CTkLabel(
            rows,
            text=elide_to_width(info_line or "", layout.row_room, info_font.measure),
            height=layout.info_height,
            font=info_font,
            text_color=theme.TEXT_MUTED,
            anchor="w",
        ).pack(fill="x")
        switch_font = theme.font(layout.switch_font)
        text = elide_to_width(AUTO_SWITCH_TEXT, layout.switch_text_room, switch_font.measure)
        switch_w, switch_h = layout.switch
        self._auto_switch = ctk.CTkSwitch(
            rows,
            text=text,
            variable=self._auto_var,
            height=layout.switch_height,
            switch_width=switch_w,
            switch_height=switch_h,
            font=switch_font,
            text_color=auto_switch_color(self._auto_var.get()),
            progress_color=theme.ACCENT,
            command=self._auto_toggled,
        )
        self._auto_switch.pack(anchor="w")
        if text != AUTO_SWITCH_TEXT:  # shortened on a small card: the tip says it in full
            add_tooltip(self._auto_switch, AUTO_SWITCH_TEXT)
        return rows

    def _build_missing_row(self, rows: ctk.CTkFrame, count: int, actions: MissingActions) -> None:
        """"3 missing" + "⬇ Download missing", above the info line."""
        layout = self._layout
        row = ctk.CTkFrame(rows, fg_color="transparent", height=layout.missing_height)
        row.pack(fill="x", pady=(0, layout.missing_gap), before=rows.winfo_children()[0])
        row.pack_propagate(False)
        if count > 0:
            chip(
                row,
                missing_text(count),
                MISSING_COLOR,
                height=layout.missing_height,
                font_size=layout.missing_font,
            ).pack(side="left", padx=(0, layout.chip_gap))
        self.missing_button = MissingButton(
            row,
            show=self._show,
            count=count,
            actions=actions,
            with_count=False,
            height=layout.missing_height,
            font_size=layout.missing_font,
        )
        self.missing_button.pack(side="left", fill="both", expand=True)

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
        scale: CardScale = FULL,
    ) -> None:
        super().__init__(
            master,
            title=show.name,
            meta=year_rating_line(show.year, show.rating),
            poster=lambda width: tvmaze_poster_url(show, width),
            images=images,
            scale=scale,
        )
        self._show = show
        self._on_track = on_track
        self._track_button = TrackButton(
            self, busy_text="Adding…", command=self._track, scale=scale
        )
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
