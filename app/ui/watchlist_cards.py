"""The Movies page's "📌 My watchlist" shelf and its cards.

`WatchlistShelf` follows the watchlist: it hides itself while the list is empty, repaints its
cards in place when only their state changed (a switch, release dates, a search) and rebuilds
its grid when films came or went. Everything it shows is stored data, so it works without a
TMDB key.

A `WatchlistCard` shows, under the title, the film's status (`watch_status`) beside a narrow
"✓" watchlist button (a click takes the film off the list), and at the bottom either the
"Download when it's out" switch or, for a film that is out and not looked for automatically
(or given up on), the usual ⬇ Download + ⋯ row. Its cover and title open the film's info
window (`on_open`; on IMDb without one).

The cards are drawn at the page's `CardScale` (Settings, Card size); the shelf rebuilds its grid
when the size changed since it was built.
"""

from __future__ import annotations

import webbrowser
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Literal

import customtkinter as ctk

from app.i18n import N_, month_short, t, tn
from app.models import Movie, WatchlistEntry
from app.services.watchlist import GIVE_UP_AFTER, available_on, is_out
from app.ui import theme
from app.ui.card_scale import FULL, SMALL_TEXT_MIN, CardScale, grid_item_width
from app.ui.cards import auto_switch_color
from app.ui.download_control import Base, DownloadActions, DownloadControl, movie_key
from app.ui.media_cards import imdb_search_url, tmdb_poster_size, tmdb_rating
from app.ui.poster_card import (
    POSTER_CARD_WIDTH,
    PosterCard,
    row_height,
    text_width,
    year_rating_line,
)
from app.ui.tasks import spawn
from app.ui.text_fit import SLACK, elide_to_width, fit_to_lines
from app.ui.tooltip import add_tooltip
from app.ui.watchlist_button import WatchlistButton
from app.ui.widgets import FlexGrid, destroy_tree

if TYPE_CHECKING:
    from app.services.images import ImageCache
    from app.services.watchlist import WatchlistService

# Shown through t().
SHELF_TITLE = N_("📌 My watchlist")
AUTO_TEXT = N_("Download when it's out")
OUT_NOW_TEXT = N_("Out now")
WAITING_TEXT = N_("Waiting for a good copy")
UNKNOWN_TEXT = N_("Release date unknown")

STATUS_SIZE = 12  # font size of the status line
STATUS_LINES = 2  # the status line's room: "Waiting for a good copy / (searched 2 h ago)"
STATUS_GAP = 6  # between the status line and the bottom row
CHIP_HEIGHT = 22
CHIP_PAD = 8  # inside a chip, left and right
REMOVE_WIDTH = 26  # the narrow "✓" watchlist button beside the status
REMOVE_HEIGHT = 26
REMOVE_GAP = 4
SWITCH_SIZE = (32, 16)  # the "Download when it's out" switch's track
SWITCH_TEXT_GAP = 6  # CTkSwitch's gap between the track and its text

Tone = Literal["soon", "out", "waiting", "gave_up"]
BottomRow = Literal["switch", "download"]


# -- text ------------------------------------------------------------------------------------


def film_count(count: int) -> str:
    """"1 film", "4 films"."""
    return tn("{n} film", "{n} films", count)


def searched_ago(last_try: datetime, now: datetime) -> str:
    """"just now", "25 min ago", "2 h ago", "1 day ago", "3 days ago"."""
    seconds = max(0.0, (now - last_try).total_seconds())
    if seconds < 60:
        return t("just now")
    if seconds < 3600:
        return t("{n} min ago", n=int(seconds // 60))
    if seconds < 86400:
        return t("{n} h ago", n=int(seconds // 3600))
    return tn("{n} day ago", "{n} days ago", int(seconds // 86400))


def _day(day: date, today: date) -> str:
    """"12 Nov", with the year when it isn't this year's."""
    text = f"{day:%d} {month_short(day)}"
    return text if day.year == today.year else f"{text} {day:%Y}"


def release_text(entry: WatchlistEntry, today: date) -> str:
    """When a film that isn't out yet comes out: "Out digitally 12 Nov" (TMDB knows the digital
    release), "In cinemas 12 Nov" (only the cinema release, still ahead), "Expected 31 Dec" (the
    cinema release has been: AirGrabber's own estimate), or "Release date unknown"."""
    if entry.digital_release is not None:
        return t("Out digitally {date}", date=_day(entry.digital_release, today))
    theatrical = entry.theatrical_release or entry.movie.release_date
    if theatrical is not None and theatrical > today:
        return t("In cinemas {date}", date=_day(theatrical, today))
    expected = available_on(entry)
    if expected is None:
        return t(UNKNOWN_TEXT)
    return t("Expected {date}", date=_day(expected, today))


@dataclass(frozen=True, slots=True)
class WatchStatus:
    """What a watchlist card says about its film, and what its bottom row offers."""

    text: str
    tone: Tone
    chip: bool  # a rounded chip ("Out digitally 12 Nov", "Out now"), else a line of text
    bottom: BottomRow


def watch_status(entry: WatchlistEntry, today: date, now: datetime) -> WatchStatus:
    """Given up → "No good copy found in 30 days" + Download; not out yet → when it comes out
    + the switch; out with the switch on → "Waiting for a good copy (searched 2 h ago)" + the
    switch; out with the switch off → "Out now" + Download."""
    if entry.gave_up:
        text = t("No good copy found in {days} days", days=GIVE_UP_AFTER.days)
        return WatchStatus(text, "gave_up", chip=False, bottom="download")
    if not is_out(entry, today):
        return WatchStatus(release_text(entry, today), "soon", chip=True, bottom="switch")
    if entry.auto_download:
        text = t(WAITING_TEXT)
        if entry.last_try is not None:
            ago = searched_ago(entry.last_try, now)
            text = t("{status} (searched {ago})", status=text, ago=ago)
        return WatchStatus(text, "waiting", chip=False, bottom="switch")
    return WatchStatus(t(OUT_NOW_TEXT), "out", chip=True, bottom="download")


def tone_color(tone: Tone) -> theme.Color:
    """Amber for what wants the user's attention (out now, given up), muted otherwise."""
    return theme.WARNING if tone in ("out", "gave_up") else theme.TEXT_MUTED


def imdb_url(entry: WatchlistEntry) -> str:
    imdb_id = entry.imdb_id or entry.movie.imdb_id
    return f"https://www.imdb.com/title/{imdb_id}/" if imdb_id else imdb_search_url(entry.movie)


def _utc_now() -> datetime:
    return datetime.now(UTC)


# -- the card --------------------------------------------------------------------------------


class WatchlistCard(PosterCard):
    """A film on the watchlist (see the module docstring); `update()` repaints it for a newer
    state of the same film."""

    def __init__(
        self,
        master: Any,
        entry: WatchlistEntry,
        *,
        images: ImageCache,
        watchlist: WatchlistService,
        actions: DownloadActions[Movie],
        today: Callable[[], date] = date.today,
        now: Callable[[], datetime] = _utc_now,
        scale: CardScale = FULL,
        on_open: Callable[[Movie], None] | None = None,
    ) -> None:
        movie = entry.movie
        self._status_font = theme.font(scale.font(STATUS_SIZE, SMALL_TEXT_MIN))
        status_height = STATUS_LINES * int(self._status_font.metrics("linespace")) + 2
        status_gap = scale.px(STATUS_GAP)
        super().__init__(
            master,
            title=movie.title,
            meta=year_rating_line(movie.year, tmdb_rating(movie.vote_average, movie.vote_count)),
            poster=lambda width: movie.poster_url(tmdb_poster_size(width)),
            images=images,
            extra_height=status_height + status_gap,
            scale=scale,
        )
        self.movie_id = movie.id
        self._entry = entry
        self._watchlist = watchlist
        self._today = today
        self._now = now
        self._status: WatchStatus | None = None
        status_size = scale.font(STATUS_SIZE, SMALL_TEXT_MIN)
        self._chip_height = scale.text_height(CHIP_HEIGHT, status_size)
        self._chip_pad = scale.px(CHIP_PAD)
        remove_width, remove_gap = scale.px(REMOVE_WIDTH), scale.px(REMOVE_GAP)
        # The status line's room: the card's text width less the narrow "✓" button.
        self._status_room = text_width(scale) - remove_width - remove_gap - SLACK

        # The bottom row: the switch or the Download row, one at a time, always the row height.
        self._bottom = ctk.CTkFrame(self, fg_color="transparent", height=row_height(scale))
        self._bottom.pack_propagate(False)
        self._pack_action(self._bottom)
        self._auto_var = ctk.BooleanVar(value=entry.auto_download)
        switch_w, switch_h = (scale.px(size) for size in SWITCH_SIZE)
        switch_room = text_width(scale) - switch_w - SWITCH_TEXT_GAP - SLACK
        switch_text = elide_to_width(t(AUTO_TEXT), switch_room, self._status_font.measure)
        self._switch = ctk.CTkSwitch(
            self._bottom,
            text=switch_text,
            variable=self._auto_var,
            font=self._status_font,
            switch_width=switch_w,
            switch_height=switch_h,
            progress_color=theme.ACCENT,
            command=self._auto_toggled,
        )
        if switch_text != t(AUTO_TEXT):  # shortened on a small card: the tip says it in full
            add_tooltip(self._switch, t(AUTO_TEXT))
        self._control = DownloadControl(
            self._bottom,
            item=movie,
            key=movie_key(movie.id),
            base=Base("download"),
            actions=actions,
            scale=scale,
        )

        # The status line, with the narrow "✓" watchlist button on its right.
        status_row = ctk.CTkFrame(self, fg_color="transparent", height=status_height)
        status_row.pack_propagate(False)
        self._pack_above_action(status_row, status_gap)
        self.watch_button = WatchlistButton(
            status_row,
            movie=movie,
            watchlist=watchlist,
            width=remove_width,
            height=scale.px(REMOVE_HEIGHT),
            scale=scale,
        )
        self.watch_button.pack(side="right", anchor="n", padx=(remove_gap, 0))
        self._status_label = ctk.CTkLabel(
            status_row,
            text="",
            height=self._chip_height,
            font=self._status_font,
            corner_radius=self._chip_height // 2,
            anchor="w",
            justify="left",
        )
        self._status_label.pack(side="left", anchor="nw")

        if on_open is not None:
            self._set_open(lambda: on_open(movie))
        else:
            self._set_open(self._open_imdb)
        self._paint()

    @property
    def status(self) -> WatchStatus | None:
        return self._status

    def update_entry(self, entry: WatchlistEntry) -> None:
        """Repaint for `entry`, a newer state of this card's film."""
        self._entry = entry
        self._paint()

    def _paint(self) -> None:
        entry = self._entry
        status = watch_status(entry, self._today(), self._now())
        if self._auto_var.get() != entry.auto_download:
            self._auto_var.set(entry.auto_download)
        self._switch.configure(text_color=auto_switch_color(entry.auto_download))
        if status == self._status:
            return
        previous, self._status = self._status, status
        room = self._status_room
        if status.chip:
            chip_font = self._status_font
            text = elide_to_width(status.text, room - 2 * self._chip_pad, chip_font.measure)
            self._status_label.configure(
                text=text,
                width=round(chip_font.measure(text)) + 2 * self._chip_pad,
                corner_radius=self._chip_height // 2,
                anchor="center",
                fg_color=theme.SURFACE,
                text_color=tone_color(status.tone),
                font=chip_font,
                wraplength=0,
            )
        else:
            # "Waiting for a good copy" / "(searched 2 h ago)": the aside on a line of its own.
            text = status.text.replace(" (", "\n(")
            text = fit_to_lines(text, room, STATUS_LINES, self._status_font.measure)
            self._status_label.configure(
                text=text,
                width=0,
                corner_radius=0,
                anchor="w",
                fg_color="transparent",
                text_color=tone_color(status.tone),
                font=self._status_font,
                wraplength=room,
            )
        if previous is None or previous.bottom != status.bottom:
            for widget in (self._switch, self._control):
                widget.pack_forget()
            if status.bottom == "switch":
                self._switch.pack(side="left")
            else:
                self._control.pack(fill="both", expand=True)

    def _auto_toggled(self) -> None:
        on = bool(self._auto_var.get())
        self._switch.configure(text_color=auto_switch_color(on))
        spawn(self._watchlist.set_auto_download(self.movie_id, on), name="watchlist-auto")

    def _open_imdb(self) -> None:
        webbrowser.open(imdb_url(self._entry))


# -- the shelf -------------------------------------------------------------------------------


class WatchlistShelf(ctk.CTkFrame):
    """The "📌 My watchlist" section: packed first in its master while the list has films,
    unpacked while it is empty. Starts out unpacked until `start()`."""

    def __init__(
        self,
        master: Any,
        *,
        watchlist: WatchlistService,
        images: ImageCache,
        actions: DownloadActions[Movie],
        pady: tuple[int, int] = (0, 25),
        card_scale: Callable[[], CardScale] = lambda: FULL,
        on_open: Callable[[Movie], None] | None = None,
    ) -> None:
        """`card_scale`: the card size to draw the cards at, read at every rebuild. `on_open`:
        what a click on a card's cover or title does (the film's info window)."""
        super().__init__(master, fg_color="transparent")
        self._on_open = on_open
        self._watchlist = watchlist
        self._images = images
        self._actions = actions
        self._pady = pady
        self._card_scale = card_scale
        self._scale: CardScale | None = None  # what the grid was built at
        self._cards: dict[int, WatchlistCard] = {}
        self._order: list[int] = []
        self._grid: FlexGrid | None = None

        heading = ctk.CTkFrame(self, fg_color="transparent")
        heading.pack(anchor="w", padx=10, pady=(0, 10))
        ctk.CTkLabel(
            heading, text=t(SHELF_TITLE), font=theme.font(18, bold=True), text_color=theme.TEXT
        ).pack(side="left")
        self._count = ctk.CTkLabel(
            heading, text="", font=theme.font(14), text_color=theme.TEXT_MUTED
        )
        self._count.pack(side="left", padx=(12, 0))
        self._unsubscribe: Callable[[], None] | None = None

    def start(self) -> None:
        """Show the watchlist and follow it from now on. Cards load their covers in tasks, so
        call this once the event loop runs (the page's load does); calling it again resyncs."""
        if self._unsubscribe is None:
            self._unsubscribe = self._watchlist.subscribe(self.sync)
        self.sync()

    @property
    def cards(self) -> dict[int, WatchlistCard]:
        return self._cards

    def sync(self) -> None:
        """Show the watchlist as it is now."""
        if not self.winfo_exists():
            return
        entries = self._watchlist.entries()
        if not entries:
            self._clear_grid()
            self.pack_forget()
            return
        self._count.configure(text=film_count(len(entries)))
        order = [entry.movie_id for entry in entries]
        if order == self._order and self._scale == self._card_scale():
            for entry in entries:
                self._cards[entry.movie_id].update_entry(entry)
        else:
            self._rebuild(entries)
        if not self.winfo_manager():
            others = [child for child in self.master.pack_slaves() if child is not self]
            options: dict[str, Any] = {"before": others[0]} if others else {}
            self.pack(fill="x", pady=self._pady, **options)

    def _rebuild(self, entries: tuple[WatchlistEntry, ...]) -> None:
        self._clear_grid()
        scale = self._scale = self._card_scale()
        grid = FlexGrid(
            self, item_width=grid_item_width(POSTER_CARD_WIDTH, scale), pad_x=8, pad_y=10
        )
        grid.pack(fill="x")
        self._grid = grid
        for entry in entries:
            self._cards[entry.movie_id] = WatchlistCard(
                grid,
                entry,
                images=self._images,
                watchlist=self._watchlist,
                actions=self._actions,
                scale=scale,
                on_open=self._on_open,
            )
        self._order = [entry.movie_id for entry in entries]
        grid.relayout()

    def _clear_grid(self) -> None:
        if self._grid is not None:
            destroy_tree(self._grid)
        self._grid = None
        self._scale = None
        self._cards = {}
        self._order = []

    def destroy(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
        super().destroy()
