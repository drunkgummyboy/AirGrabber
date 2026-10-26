"""Show cards: My shows' `ShowCard` and the search results' `SearchShowCard`, both tall poster
cards (`PosterCard`) drawn at a `CardScale` (Settings, Card size: app/ui/card_scale.py).

A My shows card: an amber "3 missing" badge in the cover's corner while episodes are missing;
the title; the info line (`card_info_line`, or while "Download missing" runs for the show its
progress and then its outcome: `run_line`); and a bottom row with the "Auto" switch (new
episodes download by themselves) and a round ⋯ that opens the card's menu (`card_menu`: Open
on IMDb, ⬇ Download missing (3), Remove show…).
"""

from __future__ import annotations

import tkinter as tk
import webbrowser
from collections.abc import Callable
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Literal

import customtkinter as ctk
from PIL import ImageTk

from app.i18n import N_, day_short, month_short, t
from app.models import DownloadRecord, Episode, Show, ShowActivity
from app.ui import theme
from app.ui.card_scale import FULL, TEXT_MIN, CardScale
from app.ui.missing_control import (
    MISSING_COLOR,
    SEASON_MENU_TIP,
    MenuItem,
    MissingActions,
    MissingRunState,
    download_missing_text,
    missing_text,
    outcome_texts,
    popup_menu,
    progress_texts,
)
from app.ui.poster_card import (
    PosterCard,
    TrackButton,
    TrackState,
    pill_image,
    rgb,
    row_font_size,
    row_height,
    year_rating_line,
)
from app.ui.tooltip import add_tooltip

if TYPE_CHECKING:
    from app.services.images import ImageCache

TVMAZE_MEDIUM_WIDTH = 210  # TVMaze's "medium" posters are 210 x 295

# Shown through t().
AUTO_SWITCH_TEXT = N_("Auto")
AUTO_SWITCH_TIP = N_(
    "Auto-download: new episodes of this show download by themselves, "
    "about 2 hours after they air."
)
IMDB_ITEM_TEXT = N_("Open on IMDb")
REMOVE_ITEM_TEXT = N_("Remove show…")
MENU_TEXT = "⋯"
MENU_TIP = SEASON_MENU_TIP  # "More options"
# The My shows card's bottom row, at XL: the switch and the round ⋯.
MENU_BUTTON_SIZE = 24
MENU_BUTTON_MIN = 20  # never smaller than this, so it stays easy to hit
SWITCH_FONT = 14
SWITCH_SIZE = (36, 18)  # CTkSwitch's own default track

_RECENT_GRAB_DAYS = 6  # "today" … six days ago; older grabs aren't mentioned
_NEAR_DAYS = 6  # a next episode up to this many days away is named by its weekday

CardMenuKey = Literal["imdb", "download_missing", "remove"]


# -- texts -----------------------------------------------------------------------------------


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
    """The My shows card's muted line under its title, the first that applies:
    "✓ S02E08 · today" (downloaded within the past week: today / yesterday / weekday),
    "Next: S02E04 · Thu" (the next episode; "23 Oct" when more than six days away), or None.
    Short enough for the smallest card; a card shortens what still doesn't fit.

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
                t("today")
                if days_ago == 0
                else t("yesterday") if days_ago == 1 else day_short(grabbed)
            )
            return f"✓ {_grab_code(grab)} · {when}"
    episode = activity.next_episode
    if episode is not None and (day := _air_day(episode, now)) is not None:
        days_ahead = (day - today).days
        if days_ahead >= 0:
            when = (
                day_short(day) if days_ahead <= _NEAR_DAYS else f"{day.day} {month_short(day)}"
            )
            return t("Next: {code} · {when}", code=episode.code, when=when)
    return None


def run_line(state: MissingRunState | None) -> tuple[tuple[str, ...], theme.Color] | None:
    """What a My shows card says instead of its info line while "Download missing" runs for the
    show and for a while after: the texts (longest first, the card shows the first that fits)
    and their colour. "Downloading 2 of 3…" muted; then the outcome: in green with episodes
    downloaded, red when the run stopped on an error, amber when nothing was found. None
    without a run."""
    if state is None:
        return None
    report = state.report
    if report is None:
        return progress_texts(state.index, state.total), theme.TEXT_MUTED
    if report.error is not None:
        color = theme.DANGER
    elif report.downloaded or report.already:
        color = theme.SUCCESS
    elif report.not_found:
        color = theme.PENDING
    else:
        color = theme.TEXT_MUTED
    return outcome_texts(report), color


def card_menu(
    *, has_imdb: bool, missing: int, working: bool, removable: bool
) -> list[tuple[str, CardMenuKey]]:
    """The My shows card's ⋯ menu: "Open on IMDb" (when the show's IMDb page is known),
    "⬇ Download missing (3)" (episodes missing and no run working for the show), "Remove
    show…"."""
    items: list[tuple[str, CardMenuKey]] = []
    if has_imdb:
        items.append((t(IMDB_ITEM_TEXT), "imdb"))
    if missing > 0 and not working:
        items.append((download_missing_text(missing), "download_missing"))
    if removable:
        items.append((t(REMOVE_ITEM_TEXT), "remove"))
    return items


def auto_switch_color(on: bool) -> theme.Color:
    """The switch label's colour: plain text when on, muted when off."""
    return theme.TEXT if on else theme.TEXT_MUTED


def tvmaze_poster_url(show: Show, width_px: int) -> str | None:
    """The show's poster for a card `width_px` physical pixels wide: TVMaze's medium one (210
    wide) when that is enough, else the original."""
    if width_px <= TVMAZE_MEDIUM_WIDTH:
        return show.poster_url or show.poster_url_original
    return show.poster_url_original or show.poster_url


# -- the round ⋯ -------------------------------------------------------------------------------


class RoundButton(tk.Canvas):
    """A round button with `text` in it: a 1-unit `edge` ring on `bg`, filled with `hover`
    under the pointer. Drawn on its own canvas, because a CTkButton grows wider than it is
    tall around any text. `size` is in CTk units (diameter); `rescale` follows a change of
    Windows' scaling, and of light / dark."""

    def __init__(
        self,
        master: Any,
        *,
        text: str,
        size: int,
        font: ctk.CTkFont,
        command: Callable[[], None],
        bg: theme.Color = theme.BG,
        edge: theme.Color = theme.EDGE,
        hover: theme.Color = theme.HOVER,
        text_color: theme.Color = theme.TEXT,
    ) -> None:
        super().__init__(master, highlightthickness=0, borderwidth=0, cursor="hand2")
        self._size = size
        self._font = font
        self._command = command
        self._colors = (bg, edge, hover)
        self._text_color = text_color
        self._images: tuple[ImageTk.PhotoImage, ImageTk.PhotoImage] | None = None
        self._hovered = False
        self._ring = self.create_image(0, 0, anchor="nw")
        self._text = self.create_text(0, 0, text=text, anchor="center")
        self.bind("<Enter>", lambda _event: self._hover(True), add="+")
        self.bind("<Leave>", lambda _event: self._hover(False), add="+")
        self.bind("<Button-1>", lambda _event: self._command(), add="+")
        self.rescale()
        theme.on_mode_change(self, self.rescale)

    def rescale(self) -> None:
        """Draw the button at the current scaling, in the current mode's colours."""
        scaling: float = ctk.ScalingTracker.get_widget_scaling(self.master)
        diameter = round(self._size * scaling)
        border = max(1, round(scaling))
        self.configure(background=theme.pick(self._colors[0]))
        self.itemconfigure(self._text, fill=theme.pick(self._text_color))
        bg, edge, hover = (rgb(self, color) for color in self._colors)
        self._images = (
            ImageTk.PhotoImage(pill_image((diameter, diameter), border, bg, edge), master=self),
            ImageTk.PhotoImage(pill_image((diameter, diameter), border, hover, edge), master=self),
        )
        self.configure(width=diameter, height=diameter)
        self.coords(self._text, diameter / 2, diameter / 2)
        self.itemconfigure(self._text, font=self._font.create_scaled_tuple(scaling))
        self._paint()

    @property
    def hovered(self) -> bool:
        return self._hovered

    def _hover(self, on: bool) -> None:
        self._hovered = on
        self._paint()

    def _paint(self) -> None:
        if self._images is not None:
            self.itemconfigure(self._ring, image=self._images[1 if self._hovered else 0])

    def invoke(self) -> None:
        self._command()


# -- cards -----------------------------------------------------------------------------------


class ShowCard(PosterCard):
    """A My shows card (see the module docstring).

    `on_auto_download(show, on)`: the "Auto" switch (state `auto_download`) is shown and calls
    it when toggled. `missing_actions`: the card shows the show's `missing` episodes (the
    badge, the menu's "⬇ Download missing") and follows the show's "Download missing" run.
    `on_open(show)`: a click on the cover or the title. `on_remove(show)`: the menu's "Remove
    show…". `scale` sizes the whole card.
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
        super().__init__(
            master,
            title=show.name,
            meta=info_line or "",
            meta_row=True,
            poster=lambda width: tvmaze_poster_url(show, width),
            images=images,
            scale=scale,
        )
        self.show_id = show.id
        self._show = show
        self._info_line = info_line or ""
        self._on_remove = on_remove
        self._on_auto_download = on_auto_download
        self._actions = missing_actions
        self._missing = missing if missing_actions is not None else 0
        self._auto_var = ctk.BooleanVar(value=auto_download)
        self._auto_switch: ctk.CTkSwitch | None = None

        row = ctk.CTkFrame(self, fg_color="transparent", height=row_height(scale))
        row.pack_propagate(False)
        self._pack_action(row)
        self.menu_button = RoundButton(
            row,
            text=MENU_TEXT,
            size=scale.px(MENU_BUTTON_SIZE, minimum=MENU_BUTTON_MIN),
            font=theme.font(row_font_size(scale), bold=True),
            command=self._open_menu,
        )
        self.menu_button.pack(side="right")
        add_tooltip(self.menu_button, t(MENU_TIP))
        if on_auto_download is not None:
            switch_w, switch_h = SWITCH_SIZE
            self._auto_switch = ctk.CTkSwitch(
                row,
                text=t(AUTO_SWITCH_TEXT),
                variable=self._auto_var,
                switch_width=scale.px(switch_w),
                switch_height=scale.px(switch_h),
                font=theme.font(scale.font(SWITCH_FONT, TEXT_MIN)),
                text_color=auto_switch_color(auto_download),
                progress_color=theme.ACCENT,
                command=self._auto_toggled,
            )
            self._auto_switch.pack(side="left")
            add_tooltip(self._auto_switch, t(AUTO_SWITCH_TIP))

        if self._missing > 0:
            self._set_badge(missing_text(self._missing), MISSING_COLOR)
        if on_open is not None:
            self._set_open(lambda: on_open(show))
        if missing_actions is not None:
            missing_actions.runs.attach(self)
        self.repaint()

    # -- the info line -------------------------------------------------------------------------

    def repaint(self) -> None:
        """Show the info line, or the show's "Download missing" run while there is one (called
        by MissingRuns when the run moves on)."""
        state = self._actions.runs.get(self.show_id) if self._actions is not None else None
        line = run_line(state)
        if line is None:
            self._set_meta(self._info_line)
        else:
            texts, color = line
            self._set_meta(texts, color)

    # -- the switch ----------------------------------------------------------------------------

    @property
    def auto_download(self) -> bool:
        return bool(self._auto_var.get())

    def set_auto_download(self, on: bool) -> None:
        """Show the switch on or off without calling back (e.g. to undo a failed change)."""
        self._auto_var.set(on)
        if self._auto_switch is not None:
            self._auto_switch.configure(text_color=auto_switch_color(on))

    def _auto_toggled(self) -> None:
        on = bool(self._auto_var.get())
        if self._auto_switch is not None:
            self._auto_switch.configure(text_color=auto_switch_color(on))
        if self._on_auto_download is not None:
            self._on_auto_download(self._show, on)

    # -- the menu ------------------------------------------------------------------------------

    def menu_items(self) -> list[MenuItem]:
        """The ⋯ menu as it would open now (`card_menu`), with its actions."""
        actions = self._actions
        working = actions is not None and actions.runs.is_working(self.show_id)
        doing: dict[CardMenuKey, Callable[[], None]] = {
            "imdb": self._open_imdb,
            "download_missing": self._download_missing,
            "remove": self._remove,
        }
        keys = card_menu(
            has_imdb=self._show.imdb_url is not None,
            missing=self._missing,
            working=working,
            removable=self._on_remove is not None,
        )
        return [(label, doing[key]) for label, key in keys]

    def _open_menu(self) -> None:
        popup_menu(self.menu_button, self.menu_items())

    def _open_imdb(self) -> None:
        if (imdb_url := self._show.imdb_url) is not None:
            webbrowser.open(imdb_url)

    def _download_missing(self) -> None:
        if self._actions is not None:
            self._actions.download(self._show)

    def _remove(self) -> None:
        if self._on_remove is not None:
            self._on_remove(self._show)

    # -- scaling and teardown --------------------------------------------------------------------

    def _apply_scale(self) -> None:
        super()._apply_scale()
        menu = getattr(self, "menu_button", None)  # not yet built on the first call
        if menu is not None:
            menu.rescale()

    def destroy(self) -> None:
        if self._actions is not None:
            self._actions.runs.detach(self)
        super().destroy()


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
            self, busy_text=t("Adding…"), command=self._track, scale=scale
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
