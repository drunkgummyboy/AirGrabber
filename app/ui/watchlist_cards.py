"""The Movies page's "📌 My watchlist" shelf and its cards.

`WatchlistShelf` follows the watchlist: it hides itself while the list is empty, repaints its
cards in place when only their state changed (a switch, release dates, a search) and rebuilds
its grid when films came or went. Everything it shows is stored data, so it works without a
TMDB key.

A `WatchlistCard` has one row, like every poster card: the main button and a round ⋯ beside it.
For a film not out yet the main button is the grey "not yet" outline with the date after a
drawn icon (`release_date`: ⬇ "17 Nov", 🎬 "3 Dec", ⬇ "~30 Nov"; its words in the hover
tip); once the film can be downloaded, the usual ⬇ Download (a right-click chooses a download
yourself). The ⋯ menu (`watch_menu`): "☑ / ☐ Download when it's out" (while
the film isn't out yet, or is out and looked for automatically), "Choose a download yourself"
(once it is out), "Remove from watchlist". While AirGrabber looks for it automatically, a blue
"Auto" pill sits in the cover's corner. Under the title: the year and rating, or what is going
on once the film is out ("Out now" / "No good release found in 30 days" in amber, "Waiting
for a good release (searched 2 h ago)" muted). Its cover and title open the film's info window
(`on_open`; on IMDb without one).

A downloaded film stays on the list until it is removed (owner, 2026-11-17; its switch went off
with the grab, app/services/watchlist.py), after the films still to come (`by_release`). Instead
of ⬇ Download it has the outlined "⬇ Again" ("Download again…" where it fits, the words in its
tip), which opens the Downloads page's "Download again…" menu (⬇ Download again automatically /
☰ Choose a release…, `again_items`). No "Auto" pill and no "Download when it's out" in its ⋯ menu
(switched on it would only download it again); "Choose a download yourself" and "Remove from
watchlist" stay. A run of it shows on the button as any run does. Under its title, only while
there is a library (`has_library`; CLAUDE.md: without one no have-cues, not even for
AirGrabber's own grabs) and the cover has no library mark (in Plex / in the folders: the mark
says it), the muted "Downloaded 17 Nov" (its latest grab; no ✓, the date sets it apart from
the folders' "✓ Downloaded"); else the year and rating (`downloaded_line`).

The cards are drawn at the page's `CardScale` (Settings, Card size); the shelf rebuilds its grid
when the size changed since it was built.
"""

from __future__ import annotations

import asyncio
import webbrowser
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import TYPE_CHECKING, Any, Literal

import customtkinter as ctk

from app.i18n import N_, month_short, t, tn
from app.models import Movie, WatchlistEntry
from app.models.plex import PlexFilms
from app.services.watchlist import GIVE_UP_AFTER, available_on, is_out
from app.ui import theme
from app.ui.card_scale import FULL, CardScale, grid_item_width
from app.ui.cards import MENU_TEXT, MENU_TIP, RoundButton, toggle_text
from app.ui.download_control import (
    GAP,
    MORE_TIP,
    Base,
    DateIcon,
    DownloadActions,
    DownloadControl,
    movie_key,
)
from app.ui.library_mark import LibraryMark, library_mark
from app.ui.media_cards import (
    IN_CINEMAS_TEXT,
    NOT_OUT_TEXT,
    OUT_TO_DOWNLOAD_TEXT,
    imdb_search_url,
    release_day,
    tmdb_poster_size,
    tmdb_rating,
)
from app.ui.missing_control import MenuItem, popup_menu
from app.ui.row_actions import again_items
from app.ui.poster_card import (
    POSTER_CARD_WIDTH,
    Badge,
    PosterCard,
    row_font_size,
    row_height,
    year_rating_line,
)
from app.ui.tasks import spawn
from app.ui.tooltip import add_tooltip
from app.ui.widgets import FlexGrid, destroy_tree

if TYPE_CHECKING:
    from app.services.images import ImageCache
    from app.services.watchlist import WatchlistService

# Shown through t().
SHELF_TITLE = N_("📌 My watchlist")
AUTO_TEXT = N_("Download when it's out")
AUTO_PILL_TEXT = N_("Auto")  # the cover's blue pill while it is looked for automatically
OUT_NOW_TEXT = N_("Out now")
WAITING_TEXT = N_("Waiting for a good release")
UNKNOWN_TEXT = N_("Release date unknown")
GAVE_UP_SHORT = N_("No good release found")
GUESS_TEXT = N_("~{date}")  # the "not yet" button's date when it is AirGrabber's own guess
REMOVE_TEXT = N_("Remove from watchlist")
DOWNLOADED_TEXT = N_("Downloaded")  # under the title of a film downloaded already (no date)
DOWNLOADED_ON_TEXT = N_("Downloaded {date}")  # … on that day ("Downloaded 17 Nov")
AGAIN_TEXT = N_("Download again…")  # its main button, as the Downloads page's menu says it
AGAIN_SHORT = N_("⬇ Again")  # … where that won't fit (every card size, in both languages)

Tone = Literal["soon", "out", "waiting", "gave_up", "downloaded"]
# later: the grey "not yet" button with the date; again: "Download again…" (downloaded already)
BottomRow = Literal["later", "download", "again"]
WatchMenuKey = Literal["auto", "choose", "remove"]


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


@dataclass(frozen=True, slots=True)
class ReleaseDate:
    """When a film that isn't out yet comes out, as its "not yet" button shows it: the date
    after a drawn icon (the download arrow: out to download; the clapperboard: in cinemas),
    shorter forms where the longest won't fit, and the words for its hover tip."""

    texts: tuple[str, ...]
    icon: DateIcon | None
    tip: str


def release_date(entry: WatchlistEntry, today: date) -> ReleaseDate:
    """TMDB's digital release: ⬇ "12 Nov". Only a cinema release, still ahead: 🎬 "16 Dec".
    The cinema release has been: ⬇ "~30 Nov", AirGrabber's own guess (`available_on`, the
    day it starts looking). No date: "Release date unknown" / "Not out yet", no icon."""
    if entry.digital_release is not None:
        day = release_day(entry.digital_release, today)
        return ReleaseDate((day,), "download", t(OUT_TO_DOWNLOAD_TEXT, date=day))
    theatrical = entry.theatrical_release or entry.movie.release_date
    if theatrical is not None and theatrical > today:
        day = release_day(theatrical, today)
        tip = t(IN_CINEMAS_TEXT, date=day)
        return ReleaseDate((day,), "cinema", tip)
    expected = available_on(entry)
    if expected is None or theatrical is None:
        return ReleaseDate((t(UNKNOWN_TEXT), t(NOT_OUT_TEXT)), None, "")
    day = release_day(expected, today)
    tip = t(
        "Expected to download around {date}, about 2 months after the cinema ({cinema})",
        date=day,
        cinema=release_day(theatrical, today),
    )
    return ReleaseDate((t(GUESS_TEXT, date=day),), "download", tip)


def release_text(entry: WatchlistEntry, today: date) -> str:
    """When a film that isn't out yet comes out, in words: the button's tip, or its text when
    there is no date."""
    when = release_date(entry, today)
    return when.tip or when.texts[0]


@dataclass(frozen=True, slots=True)
class WatchStatus:
    """What a watchlist card says about its film, and what its row offers."""

    text: str
    tone: Tone
    bottom: BottomRow
    auto: bool  # the ⋯ menu offers "Download when it's out"
    short: tuple[str, ...] = ()  # shorter forms of `text`, for where it won't fit
    icon: DateIcon | None = None  # not out yet: the icon before the date
    tip: str = ""  # not out yet: the date in words, the button's hover tip

    @property
    def texts(self) -> tuple[str, ...]:
        """`text` and its shorter forms, longest first."""
        return (self.text, *self.short)


def watch_status(entry: WatchlistEntry, today: date, now: datetime) -> WatchStatus:
    """Downloaded already → "Downloaded" + "Download again…", no toggle; given up → "No good
    release found in 30 days" + Download; not out yet → when it comes out on the "not yet"
    button, the menu's toggle; out and looked for automatically → "Waiting for a good release
    (searched 2 h ago)" + Download, the menu's toggle; out and not looked for → "Out now" +
    Download."""
    if entry.downloaded:
        return WatchStatus(downloaded_text(entry, today), "downloaded", "again", auto=False)
    if entry.gave_up:
        text = t("No good release found in {days} days", days=GIVE_UP_AFTER.days)
        return WatchStatus(text, "gave_up", "download", auto=False, short=(t(GAVE_UP_SHORT),))
    if not is_out(entry, today):
        when = release_date(entry, today)
        full, *short = when.texts
        return WatchStatus(
            full, "soon", "later", auto=True, short=tuple(short), icon=when.icon, tip=when.tip
        )
    if entry.auto_download:
        text = t(WAITING_TEXT)
        if entry.last_try is None:
            return WatchStatus(text, "waiting", "download", auto=True)
        ago = searched_ago(entry.last_try, now)
        searched = t("{status} (searched {ago})", status=text, ago=ago)
        return WatchStatus(searched, "waiting", "download", auto=True, short=(text,))
    return WatchStatus(t(OUT_NOW_TEXT), "out", "download", auto=False)


def downloaded_text(entry: WatchlistEntry, today: date) -> str:
    """"Downloaded 17 Nov" (its latest grab, the local day; with the year when it isn't this
    year's); "Downloaded" when the moment isn't known."""
    if entry.downloaded_at is None:
        return t(DOWNLOADED_TEXT)
    day = entry.downloaded_at.astimezone().date()
    return t(DOWNLOADED_ON_TEXT, date=release_day(day, today))


def downloaded_line(*, library: bool, marked: bool) -> bool:
    """Whether a downloaded film's card says "Downloaded 17 Nov" under its title: only while
    there is a library, and not when its cover has the library mark (that says it); else the
    year and rating."""
    return library and not marked


def watch_menu(status: WatchStatus, auto_download: bool) -> list[tuple[str, WatchMenuKey]]:
    """A watchlist card's ⋯ menu: "☑ / ☐ Download when it's out" (while the status offers it;
    never for a downloaded film), "Choose a download yourself" (once it can be downloaded, or
    again), "Remove from watchlist"."""
    items: list[tuple[str, WatchMenuKey]] = []
    if status.auto:
        items.append((toggle_text(t(AUTO_TEXT), auto_download), "auto"))
    if status.bottom in ("download", "again"):
        items.append((t(MORE_TIP), "choose"))
    items.append((t(REMOVE_TEXT), "remove"))
    return items


def auto_shown(status: WatchStatus, auto_download: bool) -> bool:
    """The blue "Auto" pill: while AirGrabber looks for the film by itself (not after giving
    up)."""
    return auto_download and status.auto


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
        has_library: Callable[[], bool] = lambda: False,
    ) -> None:
        """`has_library`: whether there is a library now (Plex or the folders), read at every
        paint (a downloaded film's "Downloaded 17 Nov", `downloaded_line`)."""
        movie = entry.movie
        self._year_rating = year_rating_line(
            movie.year, tmdb_rating(movie.vote_average, movie.vote_count)
        )
        super().__init__(
            master,
            title=movie.title,
            meta=self._year_rating,
            meta_row=True,
            poster=lambda width: movie.poster_url(tmdb_poster_size(width)),
            images=images,
            scale=scale,
        )
        self.movie_id = movie.id
        self.plex_mark: LibraryMark | None = None  # the books while the film is in Plex
        self._movie = movie
        self._entry = entry
        self._watchlist = watchlist
        self._actions = actions
        self._today = today
        self._now = now
        self._has_library = has_library
        self._status: WatchStatus | None = None

        # One row: the main button (the Download control: "17 Nov" or ⬇ Download) and ⋯.
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
        self.menu_button.pack(side="right", padx=(scale.px(GAP), 0))
        add_tooltip(self.menu_button, t(MENU_TIP))
        self._follow_scale(self.menu_button.rescale)
        self._control = DownloadControl(
            row,
            item=movie,
            key=movie_key(movie.id),
            base=Base("download"),
            actions=actions,
            scale=scale,
            more=False,
            outline_unavailable=True,
            on_again=self._open_again,
        )
        self._control.pack(side="left", fill="both", expand=True)

        if on_open is not None:
            self._set_open(lambda: on_open(movie))
        else:
            self._set_open(self._open_imdb)
        self._paint()

    @property
    def status(self) -> WatchStatus | None:
        return self._status

    @property
    def auto_download(self) -> bool:
        return self._entry.auto_download

    def update_entry(self, entry: WatchlistEntry) -> None:
        """Repaint for `entry`, a newer state of this card's film."""
        self._entry = entry
        self._paint()

    @property
    def imdb_id(self) -> str | None:
        return self._entry.imdb_id

    def set_in_plex(self, in_plex: bool, *, from_folders: bool = False) -> None:
        """The library mark ("In Plex"; `from_folders`: beside Plex, only in the user's own
        folders, "Downloaded…") on the cover's corner, or not (app/ui/library_mark.py)."""
        mark = self.plex_mark
        if in_plex and mark is not None and mark.from_folders != from_folders:
            mark.destroy()
            self.plex_mark = mark = None
        if in_plex and mark is None:
            self.plex_mark = library_mark(self, from_folders=from_folders)
        elif not in_plex and self.plex_mark is not None:
            self.plex_mark.destroy()
            self.plex_mark = None
        self._paint_meta()  # a downloaded film's line gives way to the mark

    def _paint(self) -> None:
        entry = self._entry
        status = watch_status(entry, self._today(), self._now())
        auto = auto_shown(status, entry.auto_download)
        self._set_badges([Badge(t(AUTO_PILL_TEXT), theme.BRAND, t(AUTO_TEXT))] if auto else [])
        if status == self._status:
            self._paint_meta()  # the library may have come or gone
            return
        self._status = status
        if status.bottom == "later":
            self._control.set_base(
                Base("unavailable", status.text, status.short, icon=status.icon, tip=status.tip)
            )
        elif status.bottom == "again":
            self._control.set_base(Base("again", t(AGAIN_TEXT), (t(AGAIN_SHORT),)))
        else:
            self._control.set_base(Base("download"))
        self._paint_meta()

    def _paint_meta(self) -> None:
        """The line under the title for the status (see the module docstring)."""
        status = self._status
        if status is None:
            return
        marked = getattr(self, "plex_mark", None) is not None
        if status.bottom == "later" or (
            status.bottom == "again"
            and not downloaded_line(library=self._has_library(), marked=marked)
        ):
            self._set_meta(self._year_rating)
        else:
            self._set_meta(status.texts, tone_color(status.tone))

    # -- the menu ------------------------------------------------------------------------------

    def menu_items(self) -> list[MenuItem]:
        """The ⋯ menu as it would open now (`watch_menu`), with its actions."""
        status = self._status or watch_status(self._entry, self._today(), self._now())
        doing: dict[WatchMenuKey, Callable[[], None]] = {
            "auto": self._toggle_auto,
            "choose": lambda: self._actions.choose(self._movie),
            "remove": self._remove,
        }
        return [(label, doing[key]) for label, key in watch_menu(status, self.auto_download)]

    def _open_menu(self) -> None:
        popup_menu(self.menu_button, self.menu_items())

    def again_items(self) -> list[MenuItem]:
        """"Download again…"'s menu, as the Downloads page has it: download it again
        automatically (the one-click download, also when grabbed before) or choose a release."""
        movie = self._movie
        return again_items(
            lambda: self._actions.download(movie), lambda: self._actions.choose(movie)
        )

    def _open_again(self) -> None:
        popup_menu(self._control, self.again_items(), align="left")

    def _toggle_auto(self) -> None:
        on = not self._entry.auto_download
        watchlist = self._watchlist  # the card shows the list in memory: the selected person's
        spawn(
            watchlist.set_auto_download(self.movie_id, on, profile_id=watchlist.profile_id),
            name="watchlist-auto",
        )

    def _remove(self) -> None:
        watchlist = self._watchlist
        spawn(
            watchlist.remove(self.movie_id, profile_id=watchlist.profile_id),
            name="watchlist-remove",
        )

    def _open_imdb(self) -> None:
        webbrowser.open(imdb_url(self._entry))


# -- the shelf -------------------------------------------------------------------------------


class WatchlistShelf(ctk.CTkFrame):
    """The "📌 My watchlist" section: packed first in its master while the list has films,
    unpacked while it is empty. Starts out unpacked until `start()`, which also reads the films
    in Plex again (`plex`): their cards get the library mark ("In Plex")."""

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
        plex: Callable[[], Awaitable[PlexFilms]] | None = None,
        has_library: Callable[[], bool] = lambda: False,
    ) -> None:
        """`card_scale`: the card size to draw the cards at, read at every rebuild. `on_open`:
        what a click on a card's cover or title does (the film's info window). `plex`: the
        films in Plex (none without it). `has_library`: whether there is a library now (the
        cards' "Downloaded 17 Nov")."""
        super().__init__(master, fg_color="transparent")
        self._on_open = on_open
        self._plex = plex
        self._has_library = has_library
        self._in_plex = PlexFilms()
        self._plex_task: asyncio.Task[None] | None = None
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
        if self._plex is not None:
            if self._plex_task is not None:
                self._plex_task.cancel()
            self._plex_task = spawn(self._read_plex(self._plex), name="WatchlistShelf.plex")

    async def _read_plex(self, plex: Callable[[], Awaitable[PlexFilms]]) -> None:
        films = await plex()
        if self.winfo_exists():
            self._in_plex = films
            self._mark_plex()

    def _mark_plex(self) -> None:
        films = self._in_plex
        for card in self._cards.values():
            card.set_in_plex(
                films.has(card.movie_id, card.imdb_id),
                from_folders=films.only_in_folders(card.movie_id, card.imdb_id),
            )

    @property
    def cards(self) -> dict[int, WatchlistCard]:
        return self._cards

    def sync(self) -> None:
        """Show the watchlist as it is now."""
        if not self.winfo_exists():
            return
        entries = self._watchlist.by_release()
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
        self._mark_plex()
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
                has_library=self._has_library,
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
        if self._plex_task is not None:
            self._plex_task.cancel()
        super().destroy()
