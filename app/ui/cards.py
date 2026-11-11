"""Show cards: My shows' `ShowCard` and the search results' `SearchShowCard`, both tall poster
cards (`PosterCard`) drawn at a `CardScale` (Settings, Card size: app/ui/card_scale.py).

A My shows card: pills in the cover's corner, an amber "3 missing" while episodes are missing
(only with Plex: app/services/plex.py) and beside it a blue "Auto" while new episodes download
by themselves; the title; the info line (`card_info_line`: the next episode, or while
"Download missing" runs for the show its progress and then its outcome: `run_line`); and one
bottom row: the main button, "⬇ Download missing (3)" (accent) while episodes are missing and
no run is working for the show, else "✓ Following" (green outline, does nothing:
`card_main`), with a round ⋯ beside it that opens the card's menu (`card_menu`: ☑ / ☐
Auto-download new episodes, Open on IMDb, Remove show…). What AirGrabber downloaded is never
shown as had (owner's decision, 3.1.0).

`RoundButton` is every poster card's round button (My shows' ⋯, the film cards' ★, the Library
cards' phone): a text, or a small picture drawn with Pillow (`phone_icon`, `star_icon`).
"""

from __future__ import annotations

import math
import tkinter as tk
import webbrowser
from collections.abc import Callable
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Literal

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageTk

from app.i18n import N_, day_short, month_short, t
from app.models import Episode, Show, ShowActivity
from app.ui import theme
from app.ui.card_scale import FULL, TEXT_MIN, CardScale
from app.ui.download_control import CORNER_RADIUS as ROW_CORNER_RADIUS
from app.ui.download_control import GAP as ROW_GAP
from app.ui.download_control import QUEUED_TEXT, fit_text
from app.ui.library_mark import LibraryMark, library_mark
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
    FOLLOWING_TEXT,
    RGB,
    Badge,
    PosterCard,
    TrackButton,
    TrackState,
    pill_image,
    rgb,
    row_font_size,
    row_height,
    text_width,
    track_look,
    year_rating_line,
)
from app.ui.text_fit import SLACK
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
AUTO_ITEM_TEXT = N_("Auto-download new episodes")  # the ⋯ menu's toggle, after ☑ / ☐
MISSING_SHORT = N_("⬇ Missing ({n})")  # the main button where "⬇ Download missing (3)" won't fit
IMDB_ITEM_TEXT = N_("Open on IMDb")
REMOVE_ITEM_TEXT = N_("Remove show…")
MENU_TEXT = "⋯"
MENU_TIP = SEASON_MENU_TIP  # "More options"
CHECKED_BOX = "☑"  # a menu's toggle entry, on
UNCHECKED_BOX = "☐"  # … off
# A round button smaller than the row (no longer on the poster cards: theirs is the row's height).
MENU_BUTTON_SIZE = 24
MENU_BUTTON_MIN = 20  # never smaller than this, so it stays easy to hit
ICON_SHARE = 0.46  # a drawn icon's square, of the round button's diameter
_ICON_SUPERSAMPLE = 4

_NEAR_DAYS = 6  # a next episode up to this many days away is named by its weekday

CardMenuKey = Literal["auto", "imdb", "remove"]
CardMain = Literal["following", "download_missing"]
# Draws an icon in `colour` on a transparent square `side` pixels wide (supersampled).
IconPainter = Callable[[ImageDraw.ImageDraw, int, RGB], None]


# -- texts -----------------------------------------------------------------------------------


def _air_day(episode: Episode, now: datetime) -> date | None:
    if episode.airstamp is not None:
        return episode.airstamp.astimezone(now.tzinfo).date()
    return episode.air_date


def card_info_line(activity: ShowActivity | None, now: datetime) -> str | None:
    """The My shows card's muted line under its title: "Next: S02E04 · Thu" (the next
    episode; "23 Oct" when more than six days away), or None. Short enough for the smallest
    card; a card shortens what still doesn't fit. (A recent grab isn't mentioned: what
    AirGrabber downloaded is never shown as had.)

    `now` is an aware instant; its time zone decides the days (the app passes local time).
    """
    if activity is None:
        return None
    today = now.date()
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
    and their colour. "Queued" (waiting in the download queue), "Downloading 2 of 3…" muted;
    then the outcome: in green with episodes downloaded, red when the run stopped on an error,
    amber when nothing was found. None without a run."""
    if state is None:
        return None
    report = state.report
    if report is None and state.queued:
        return (t(QUEUED_TEXT),), theme.TEXT_MUTED
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


def toggle_text(text: str, on: bool) -> str:
    """A menu's toggle entry: "☑ Download when it's out" / "☐ Download when it's out"."""
    return f"{CHECKED_BOX if on else UNCHECKED_BOX} {text}"


def card_menu(
    *, has_imdb: bool, removable: bool, auto: bool | None = None
) -> list[tuple[str, CardMenuKey]]:
    """The My shows card's ⋯ menu: "☑ / ☐ Auto-download new episodes" (`auto`: on, off, or
    None for a card without it), "Open on IMDb" (when the show's IMDb page is known), "Remove
    show…". (⬇ Download missing is the card's main button: `card_main`.)"""
    items: list[tuple[str, CardMenuKey]] = []
    if auto is not None:
        items.append((toggle_text(t(AUTO_ITEM_TEXT), auto), "auto"))
    if has_imdb:
        items.append((t(IMDB_ITEM_TEXT), "imdb"))
    if removable:
        items.append((t(REMOVE_ITEM_TEXT), "remove"))
    return items


def card_main(missing: int, working: bool) -> CardMain:
    """The My shows card's main button: "⬇ Download missing (3)" while episodes are missing and
    no run is working for the show, else "✓ Following"."""
    return "download_missing" if missing > 0 and not working else "following"


def card_main_texts(main: CardMain, missing: int) -> tuple[str, ...]:
    """The main button's texts, longest first: "⬇ Download missing (3)", "⬇ Missing (3)",
    "⬇ 3"; "✓ Following", "✓"."""
    if main == "download_missing":
        return (download_missing_text(missing), t(MISSING_SHORT, n=missing), f"⬇ {missing}")
    return (t(FOLLOWING_TEXT), "✓")


def auto_badge(on: bool) -> Badge | None:
    """The blue "Auto" pill on a My shows card while its new episodes download by themselves."""
    return Badge(t(AUTO_SWITCH_TEXT), theme.BRAND, t(AUTO_SWITCH_TIP)) if on else None


def auto_switch_color(on: bool) -> theme.Color:
    """The switch label's colour: plain text when on, muted when off."""
    return theme.TEXT if on else theme.TEXT_MUTED


def tvmaze_poster_url(show: Show, width_px: int) -> str | None:
    """The show's poster for a card `width_px` physical pixels wide: TVMaze's medium one (210
    wide) when that is enough, else the original."""
    if width_px <= TVMAZE_MEDIUM_WIDTH:
        return show.poster_url or show.poster_url_original
    return show.poster_url_original or show.poster_url


# -- the round button ------------------------------------------------------------------------


def phone_icon(draw: ImageDraw.ImageDraw, side: int, colour: RGB) -> None:
    """A phone: a rounded upright rectangle's outline with a dot near its bottom."""
    stroke = max(1, round(side * 0.09))
    width, height = side * 0.52, side * 0.86
    left, top = (side - width) / 2, (side - height) / 2
    draw.rounded_rectangle(
        (left, top, left + width, top + height),
        radius=round(side * 0.13),
        outline=colour,
        width=stroke,
    )
    dot = side * 0.055
    cx, cy = side / 2, top + height - stroke - side * 0.11
    draw.ellipse((cx - dot, cy - dot, cx + dot, cy + dot), fill=colour)


def _star_points(side: int) -> list[tuple[float, float]]:
    centre, outer = side / 2, side * 0.5
    inner = outer * 0.45
    points: list[tuple[float, float]] = []
    for index in range(10):
        radius = outer if index % 2 == 0 else inner
        angle = -math.pi / 2 + index * math.pi / 5
        drop = side * 0.04  # a star's points reach higher than its feet: a little lower
        points.append((centre + radius * math.cos(angle), centre + drop + radius * math.sin(angle)))
    return points


def star_icon(filled: bool) -> IconPainter:
    """A five-pointed star, filled or only its outline."""

    def paint(draw: ImageDraw.ImageDraw, side: int, colour: RGB) -> None:
        points = _star_points(side)
        if filled:
            draw.polygon(points, fill=colour)
        else:
            stroke = max(1, round(side * 0.08))
            draw.line([*points, points[0], points[1]], fill=colour, width=stroke, joint="curve")

    return paint


def icon_image(painter: IconPainter, side: int, colour: RGB) -> Image.Image:
    """`painter`'s icon `side` physical pixels square in RGBA, anti-aliased (drawn larger, then
    scaled down). Small: fine on the event loop."""
    big = Image.new("RGBA", (side * _ICON_SUPERSAMPLE, side * _ICON_SUPERSAMPLE), (*colour, 0))
    painter(ImageDraw.Draw(big), side * _ICON_SUPERSAMPLE, colour)
    return big.resize((side, side), Image.Resampling.BOX)  # BOX: no ringing at the edges


class RoundButton(tk.Canvas):
    """A round button with `text` in it, or a small drawn picture (`icon`, in the text colour):
    a 1-unit `edge` ring on `bg`, filled with `hover` under the pointer. Drawn on its own
    canvas, because a CTkButton grows wider than it is tall around any text. `size` is in CTk
    units (diameter); `rescale` follows a change of Windows' scaling, and of light / dark;
    `set_look` changes its text, picture or colours (the film cards' ★)."""

    def __init__(
        self,
        master: Any,
        *,
        text: str = "",
        size: int,
        font: ctk.CTkFont,
        command: Callable[[], None],
        bg: theme.Color | None = None,
        edge: theme.Color | None = None,
        hover: theme.Color | None = None,
        text_color: theme.Color = theme.TEXT,
        icon: IconPainter | None = None,
    ) -> None:
        """`bg`, `edge`, `hover`: BG, EDGE and HOVER when not given (read now: the accent's).
        `icon`: a picture instead of `text` (`phone_icon`, `star_icon(...)`)."""
        super().__init__(master, highlightthickness=0, borderwidth=0, cursor="hand2")
        self._size = size
        self._font = font
        self._command = command
        self._colors = (
            theme.BG if bg is None else bg,
            theme.EDGE if edge is None else edge,
            theme.HOVER if hover is None else hover,
        )
        self._text_color = text_color
        self._icon = icon
        self._images: tuple[ImageTk.PhotoImage, ImageTk.PhotoImage] | None = None
        self._hovered = False
        self._label = "" if icon else text
        self._ring = self.create_image(0, 0, anchor="nw")
        self._text = self.create_text(0, 0, text=self._label, anchor="center")
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
        rings = [pill_image((diameter, diameter), border, fill, edge) for fill in (bg, hover)]
        if self._icon is not None:
            side = round(diameter * ICON_SHARE)
            icon = icon_image(self._icon, side, rgb(self, self._text_color))
            at = ((diameter - side) // 2, (diameter - side) // 2)
            rings = [ring.copy() for ring in rings]
            for ring in rings:
                ring.alpha_composite(icon, at)
        self._images = (
            ImageTk.PhotoImage(rings[0], master=self),
            ImageTk.PhotoImage(rings[1], master=self),
        )
        self.configure(width=diameter, height=diameter)
        self.coords(self._text, diameter / 2, diameter / 2)
        self.itemconfigure(self._text, font=self._font.create_scaled_tuple(scaling))
        self._paint()

    def set_look(
        self,
        *,
        text: str | None = None,
        icon: IconPainter | None = None,
        text_color: theme.Color | None = None,
        edge: theme.Color | None = None,
    ) -> None:
        """Change what is drawn (given parts only; `icon` replaces the text) and draw again."""
        if icon is not None:
            self._icon, self._label = icon, ""
        elif text is not None:
            self._icon, self._label = None, text
        self.itemconfigure(self._text, text=self._label)
        if text_color is not None:
            self._text_color = text_color
        if edge is not None:
            self._colors = (self._colors[0], edge, self._colors[2])
        self.rescale()

    @property
    def text(self) -> str:
        """The text in it ("" while it shows a picture)."""
        return self._label

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

    `on_auto_download(show, on)`: the menu's "☑ / ☐ Auto-download new episodes" toggle (state
    `auto_download`, the blue "Auto" pill while on) is shown and calls it when chosen.
    `missing_actions`: the card shows the show's `missing` episodes (the amber pill, the main
    button's "⬇ Download missing (3)") and follows the show's "Download missing" run.
    `on_open(show)`: a click on the cover or the title. `on_remove(show)`: the menu's "Remove
    show…". `scale` sizes the whole card. `in_plex`: the show is in Plex (the library mark).
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
        in_plex: bool = False,
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
        self._auto = auto_download
        self._main: CardMain | None = None
        self.plex_mark: LibraryMark | None = library_mark(self) if in_plex else None

        height = row_height(scale)
        row = ctk.CTkFrame(self, fg_color="transparent", height=height)
        row.pack_propagate(False)
        self._pack_action(row)
        self.menu_button = RoundButton(
            row,
            text=MENU_TEXT,
            size=height,
            font=theme.font(row_font_size(scale), bold=True),
            command=self._open_menu,
        )
        gap = scale.px(ROW_GAP)
        self.menu_button.pack(side="right", padx=(gap, 0))
        add_tooltip(self.menu_button, t(MENU_TIP))
        self._follow_scale(self.menu_button.rescale)
        self.main_button = ctk.CTkButton(
            row,
            text="",
            height=height,
            corner_radius=scale.px(ROW_CORNER_RADIUS),
            font=theme.font(row_font_size(scale), bold=True),
            border_width=0,
            command=self._main_clicked,
        )
        self.main_button.pack(side="left", fill="both", expand=True)
        self._main_font = theme.font(row_font_size(scale), bold=True)
        self._main_room = (
            text_width(scale) - height - gap - 2 * scale.px(ROW_CORNER_RADIUS) - SLACK
        )
        self._main_tip = add_tooltip(self.main_button, "")
        self._default_text_color = self.main_button.cget("text_color")
        self._default_disabled_color = self.main_button.cget("text_color_disabled")

        self._show_badges()
        if on_open is not None:
            self._set_open(lambda: on_open(show))
        if missing_actions is not None:
            missing_actions.runs.attach(self)
        self.repaint()

    # -- the info line and the main button -----------------------------------------------------

    def repaint(self) -> None:
        """Show the info line, or the show's "Download missing" run while there is one, and the
        main button that goes with it (called by MissingRuns when the run moves on)."""
        state = self._actions.runs.get(self.show_id) if self._actions is not None else None
        line = run_line(state)
        if line is None:
            self._set_meta(self._info_line)
        else:
            texts, color = line
            self._set_meta(texts, color)
        working = self._actions is not None and self._actions.runs.is_working(self.show_id)
        self._paint_main(card_main(self._missing, working))

    @property
    def main_text(self) -> str:
        return str(self.main_button.cget("text"))

    def _paint_main(self, main: CardMain) -> None:
        if main == self._main:
            return
        self._main = main
        look = track_look("idle" if main == "download_missing" else "tracked", "")
        texts = card_main_texts(main, self._missing)
        text = fit_text(texts, self._main_room, self._main_font.measure)
        self.main_button.configure(
            text=text,
            fg_color=look.color,
            hover_color=look.hover,
            border_width=1 if look.border else 0,
            border_color=look.border or theme.EDGE,
            text_color=look.text_color or self._default_text_color,
            text_color_disabled=look.text_color or self._default_disabled_color,
            state="normal" if look.enabled else "disabled",
        )
        self._main_tip.set_text(texts[0] if text != texts[0] else "")

    def _main_clicked(self) -> None:
        if self._main == "download_missing":
            self._download_missing()

    # -- auto-download -------------------------------------------------------------------------

    @property
    def auto_download(self) -> bool:
        return self._auto

    def set_auto_download(self, on: bool) -> None:
        """Show auto-download on or off without calling back (e.g. to undo a failed change)."""
        self._auto = on
        self._show_badges()

    def _toggle_auto(self) -> None:
        self.set_auto_download(not self._auto)
        if self._on_auto_download is not None:
            self._on_auto_download(self._show, self._auto)

    def _show_badges(self) -> None:
        """The amber "3 missing" pill, then the blue "Auto" one."""
        badges: list[Badge] = []
        if self._missing > 0:
            badges.append(Badge(missing_text(self._missing), MISSING_COLOR))
        if self._on_auto_download is not None and (auto := auto_badge(self._auto)) is not None:
            badges.append(auto)
        self._set_badges(badges)

    # -- the menu ------------------------------------------------------------------------------

    def menu_items(self) -> list[MenuItem]:
        """The ⋯ menu as it would open now (`card_menu`), with its actions."""
        doing: dict[CardMenuKey, Callable[[], None]] = {
            "auto": self._toggle_auto,
            "imdb": self._open_imdb,
            "remove": self._remove,
        }
        keys = card_menu(
            has_imdb=self._show.imdb_url is not None,
            removable=self._on_remove is not None,
            auto=self._auto if self._on_auto_download is not None else None,
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

    # -- teardown ------------------------------------------------------------------------------

    def destroy(self) -> None:
        if self._actions is not None:
            self._actions.runs.detach(self)
        super().destroy()


class SearchShowCard(PosterCard):
    """A search result's TVMaze show as a poster card: year and rating, and + Follow.
    Clicking the cover calls `on_open_show(show)`; without it, it opens the show on IMDb (when
    TVMaze knows its IMDb id). `in_plex`: the show is in Plex (the library mark)."""

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
        in_plex: bool = False,
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
        self.plex_mark: LibraryMark | None = library_mark(self) if in_plex else None
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
        """idle → "+ Follow"; busy → "Adding…"; tracked → "✓ Following"."""
        self._track_button.set_state(state)

    def _track(self) -> None:
        self._on_track(self._show)
