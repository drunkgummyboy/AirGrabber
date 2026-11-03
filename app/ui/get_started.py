"""The "Let's get started" window (mockups/get-started.html): pick a few current series to
follow in one go, so an empty calendar fills up.

Opened from the empty calendar's box ("Pick shows to follow…"), from My shows ("+ Pick
shows…") and once by itself right after the first-run welcome when the selected person still
follows no shows (MainWindow, Settings' `get_started_shown`).

Three lists behind chips: with a TMDB key TMDB's TV dashboard (`DiscoveryService.tv_dashboard`:
"Popular now" = trending, "On the air", "Coming soon" = premieres); without one only "On the
air", from this week's TVMaze schedule (`LibraryService.suggestions`, the calendar's
suggestions: today and the six days after it, one pick per show, each day's best first). The
search field looks a show up on TVMaze (`LibraryService.search`, as the search window does); its
results replace the grid until a chip is clicked again. Shows the person follows are left out.

A click on a poster chooses it (a ring and a filled ✓ in the brand blue) or unchooses it; the chosen
shows are kept by a stable key (`Pick.key`: TMDB or TVMaze id) across chips and searches.
"Follow N shows" follows each through the call its "+ Track" buttons use
(`DiscoveryService.track` for TMDB shows, `LibraryService.track` for TVMaze ones) for the
person the window was opened for; the calendar then reloads through the library's listeners.
A show that fails is named in one line and the rest still go through; without failures the
window closes.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from typing import Any, Protocol

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageTk

from app.i18n import N_, day_short, t, tn
from app.models import CalendarEntry, CardSize, DiscoverShow, DiscoverShowEntry, Show
from app.models import ShowSearchResult, TVDashboard
from app.services import ImageCache
from app.ui import theme
from app.ui.borderless import make_borderless
from app.ui.branding import set_window_icon
from app.ui.card_scale import CardScale
from app.ui.cards import tvmaze_poster_url
from app.ui.dialog_kit import (
    CLOSE_CLEARANCE,
    DETAIL_SIZE,
    LINE_GAP,
    PAD_X,
    PAD_Y,
    SCREEN_MARGIN,
    WINDOW_FRAME,
    activate_later,
    heading_row,
    max_dialog_height,
    place_over,
    primary_button,
    screen_area,
    secondary_button,
    text_label,
)
from app.ui.media_cards import tmdb_poster_size
from app.ui.poster_card import RGB, POSTER_CARD_WIDTH, PosterCard, pill_image, rgb, row_height
from app.ui.scrollable import ScrollableFrame
from app.ui.shelf_filter import CHIP_FONT_SIZE, CHIP_GAP, CHIP_HEIGHT, CHIP_PAD, chip_color
from app.ui.tasks import spawn
from app.ui.widgets import FlexGrid, destroy_tree

log = logging.getLogger(__name__)

# Shown through t().
TITLE = N_("Let's get started")
SUBTITLE = N_("Click the series you watch. Their new episodes show up on your calendar.")
SEARCH_HINT = N_("Looking for another show? Type its name…")
LOADING_TEXT = N_("Loading…")
FAILED_TEXT = N_("Couldn't load this list.")
EMPTY_LIST_TEXT = N_("No shows in this list.")
NO_RESULTS_TEXT = N_("No TV shows found.")
NOTHING_CHOSEN = N_("Nothing chosen yet")
NOT_NOW_TEXT = N_("Not now")
FOLLOWING_TEXT = N_("Following…")


class ListKind(StrEnum):
    """The chips, in the order the bar shows them."""

    POPULAR = "popular"
    ON_AIR = "on_air"
    COMING = "coming"


CHIP_TEXTS: dict[ListKind, str] = {
    ListKind.POPULAR: N_("Popular now"),
    ListKind.ON_AIR: N_("On the air"),
    ListKind.COMING: N_("Coming soon"),
}
# TVDashboard's shelf names (its `failed`) per chip.
_SHELVES: dict[ListKind, str] = {
    ListKind.POPULAR: "trending",
    ListKind.ON_AIR: "on_air",
    ListKind.COMING: "premieres",
}

LIST_SIZE = 18  # at most this many shows a list (TMDB's shelves hold 18)
SCHEDULE_PER_DAY = 16  # the schedule's best shows fetched a day (one pick per show is kept)
SCHEDULE_DAYS = 7  # today and the six days after it
SEARCH_LIMIT = 18

# Logical (CTk) units.
DIALOG_WIDTH = 960  # 6 columns of posters
DIALOG_HEIGHT = 780  # at most; less on a smaller screen (`max_dialog_height`)
PICK_SCALE = CardScale.of(CardSize.XS)  # the posters: 128 x 192
GRID_PAD = 6  # around each poster (a slot is the poster + 2 x this)
AREA_RADIUS = 10
BAR_GAP = 16  # above the chips and the search field
AREA_GAP = 12  # between them and the posters
FOOTER_GAP = 14  # above the footer
SEARCH_HEIGHT = 32
FOLLOW_WIDTH = 180
MESSAGE_PAD = 40  # around "Loading…" and the other lines in the poster area
# The round "+" / "✓" at a poster's top right, and the ring around a chosen one.
MARK_SIZE = 22
MARK_INSET = 7
MARK_FONT_SIZE = 12
RING_WIDTH = 3
_SUPERSAMPLE = 4

ADD_MARK = "+"
CHOSEN_MARK = "✓"


# -- what the grid shows -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Pick:
    """A show the window offers: a TMDB show (`DiscoverShow`) or a TVMaze one (`Show`), with
    its muted line."""

    source: DiscoverShow | Show
    meta: str = ""

    @property
    def key(self) -> tuple[str, int]:
        """Stable across lists and searches: ("tmdb", id) or ("tvmaze", id)."""
        if isinstance(self.source, DiscoverShow):
            return ("tmdb", self.source.id)
        return ("tvmaze", self.source.id)

    @property
    def name(self) -> str:
        return self.source.name


def meta_line(network: str | None, weekday: str | None, year: int | None) -> str:
    """The muted line under a poster: "NBC · Mon"; without a weekday the year after the
    network ("HBO · 2019"); "2024" alone; "" when nothing is known."""
    second = weekday or (str(year) if year else None)
    return " · ".join(part for part in (network, second) if part)


def pick_poster(pick: Pick, width_px: int) -> str | None:
    """The cover for a card `width_px` physical pixels wide."""
    source = pick.source
    if isinstance(source, DiscoverShow):
        return source.poster_url(tmdb_poster_size(width_px))
    return tvmaze_poster_url(source, width_px)


def tmdb_picks(entries: Iterable[DiscoverShowEntry], *, limit: int = LIST_SIZE) -> list[Pick]:
    """A TMDB shelf's shows the person doesn't follow, in its order (TMDB lists no network:
    the year)."""
    picks = [
        Pick(entry.show, meta_line(None, None, entry.show.year))
        for entry in entries
        if not entry.is_tracked
    ]
    return picks[:limit]


def dashboard_lists(
    dashboard: TVDashboard,
) -> tuple[dict[ListKind, list[Pick]], frozenset[ListKind]]:
    """The three chips' lists from TMDB's TV dashboard, and the chips whose shelf failed."""
    lists = {
        ListKind.POPULAR: tmdb_picks(dashboard.trending),
        ListKind.ON_AIR: tmdb_picks(dashboard.on_air),
        ListKind.COMING: tmdb_picks(dashboard.premieres),
    }
    failed = frozenset(kind for kind, shelf in _SHELVES.items() if shelf in dashboard.failed)
    return lists, failed


def schedule_picks(entries: Iterable[CalendarEntry], *, limit: int = LIST_SIZE) -> list[Pick]:
    """"On the air" without a TMDB key, from the TVMaze schedule's suggestions (best first
    within each day): every day's best show first, then every day's second best, and so on,
    from the earliest day; one pick per show (its first airing: "NBC · Mon")."""
    days: dict[date, list[CalendarEntry]] = {}
    for entry in entries:
        if entry.episode.air_date is not None and not entry.is_tracked:
            days.setdefault(entry.episode.air_date, []).append(entry)
    ordered = [days[day] for day in sorted(days)]
    picks: list[Pick] = []
    seen: set[int] = set()
    for rank in range(max((len(day) for day in ordered), default=0)):
        for day_entries in ordered:
            if rank >= len(day_entries):
                continue
            entry = day_entries[rank]
            if entry.show.id in seen:
                continue
            seen.add(entry.show.id)
            air_date = entry.episode.air_date
            weekday = day_short(air_date) if air_date is not None else None
            picks.append(Pick(entry.show, meta_line(entry.show.network, weekday, entry.show.year)))
            if len(picks) >= limit:
                return picks
    return picks


def search_picks(results: Iterable[ShowSearchResult]) -> list[Pick]:
    """TVMaze's search results the person doesn't follow, best match first."""
    return [
        Pick(result.show, meta_line(result.show.network, None, result.show.year))
        for result in results
        if not result.is_tracked
    ]


def available_lists(has_tmdb_key: bool) -> tuple[ListKind, ...]:
    """The chips offered: all three with a TMDB key, else only "On the air" (the schedule)."""
    return tuple(ListKind) if has_tmdb_key else (ListKind.ON_AIR,)


def chosen_text(count: int) -> str:
    """The footer's count: "Nothing chosen yet", "1 show chosen", "3 shows chosen"."""
    if count == 0:
        return t(NOTHING_CHOSEN)
    return tn("{n} show chosen", "{n} shows chosen", count)


def follow_text(count: int) -> str:
    """The primary button: "Follow 1 show" / "Follow 3 shows"."""
    return tn("Follow {n} show", "Follow {n} shows", count)


def failed_text(names: Sequence[str]) -> str:
    """The one line naming the shows that couldn't be followed."""
    return t("Couldn't follow: {names}", names=", ".join(names))


def results_heading(query: str) -> str:
    return t('Results for "{query}"', query=query)


def get_started_due(shown: bool, tracked_shows: int) -> bool:
    """Whether the window opens by itself after the welcome: never shown before and the
    selected person follows no shows."""
    return not shown and tracked_shows == 0


# -- the services it uses ----------------------------------------------------------------------


class PickLibrary(Protocol):
    """LibraryService's calls the window makes."""

    async def suggestions(
        self, start: date, end: date, *, profile_id: int, per_day: int
    ) -> list[CalendarEntry]: ...

    async def search(
        self, query: str, *, profile_id: int, limit: int = ...
    ) -> list[ShowSearchResult]: ...

    async def track(self, show_id: int, *, profile_id: int) -> Show: ...


class PickDiscovery(Protocol):
    """DiscoveryService's calls the window makes."""

    @property
    def has_tmdb_key(self) -> bool: ...

    async def tv_dashboard(self, *, profile_id: int, force: bool = ...) -> TVDashboard: ...

    async def track(self, show: DiscoverShow, *, profile_id: int) -> Show: ...


# -- a poster that can be chosen -----------------------------------------------------------------


def ring_image(size: tuple[int, int], width: int, radius: int, colour: RGB) -> Image.Image:
    """An RGBA ring `width` physical pixels wide in `colour` along the inside of a rounded
    rectangle of `size` (corner `radius`), see-through elsewhere; anti-aliased. CPU-heavy at
    a poster's size: call it in a worker thread."""
    big_w, big_h = size[0] * _SUPERSAMPLE, size[1] * _SUPERSAMPLE
    mask = Image.new("L", (big_w, big_h), 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, big_w - 1, big_h - 1), radius=radius * _SUPERSAMPLE, fill=255)
    inset = width * _SUPERSAMPLE
    draw.rounded_rectangle(
        (inset, inset, big_w - 1 - inset, big_h - 1 - inset),
        radius=max(0, radius - width) * _SUPERSAMPLE,
        fill=0,
    )
    ring = Image.new("RGBA", size, (*colour, 255))
    ring.putalpha(mask.resize(size, Image.Resampling.BOX))
    return ring


# Rings by (size, width, radius, colour), shared between cards: read-only. Touched on the
# event-loop thread only.
_rings: dict[tuple[tuple[int, int], int, int, RGB], Image.Image] = {}


async def _ring(size: tuple[int, int], width: int, radius: int, colour: RGB) -> Image.Image:
    key = (size, width, radius, colour)
    image = _rings.get(key)
    if image is None:
        image = await asyncio.to_thread(ring_image, size, width, radius, colour)
        _rings[key] = image
    return image


class PickCard(PosterCard):
    """A poster of the grid: the cover with the title and the muted line on it (no action
    row), a round "+" at its top right; chosen, a brand-blue ring around it and a filled brand-blue
    "✓". A click anywhere on it calls `on_toggle(pick)`."""

    def __init__(
        self,
        master: Any,
        pick: Pick,
        *,
        images: ImageCache,
        chosen: bool,
        on_toggle: Callable[[Pick], None],
    ) -> None:
        # Before PosterCard's __init__: its first `_apply_scale` draws them.
        self.pick = pick
        self._chosen = chosen
        self._mark_items: tuple[int, int] | None = None  # the round mark, its "+" / "✓"
        self._mark_photo: ImageTk.PhotoImage | None = None
        self._ring_item: int | None = None
        self._ring_photo: ImageTk.PhotoImage | None = None
        self._ring_task: asyncio.Task[None] | None = None
        scale = PICK_SCALE
        super().__init__(
            master,
            title=pick.name,
            meta=pick.meta,
            poster=lambda width: pick_poster(pick, width),
            images=images,
            extra_height=-row_height(scale),  # no action row: the text sits at the bottom
            scale=scale,
            meta_row=True,
        )
        self._set_open(lambda: on_toggle(pick))

    @property
    def chosen(self) -> bool:
        return self._chosen

    @property
    def mark_text(self) -> str:
        """The round mark's text: "+" or "✓"."""
        return CHOSEN_MARK if self._chosen else ADD_MARK

    def set_chosen(self, chosen: bool) -> None:
        if chosen == self._chosen:
            return
        self._chosen = chosen
        self._draw_mark()
        self._show_ring()

    def _apply_scale(self) -> None:
        super()._apply_scale()
        self._draw_mark()
        if self._ring_task is not None:
            self._ring_task.cancel()
        self._ring_task = spawn(self._load_ring(), name="PickCard.ring")

    def _draw_mark(self) -> None:
        canvas = self._sheet
        scaling = self.scaling
        size = round(MARK_SIZE * scaling)
        inset = round(MARK_INSET * scaling)
        x = round(self._units[0] * scaling) - inset - size
        if self._chosen:
            fill, edge, text = theme.BRAND, theme.BRAND, theme.BG
        else:
            fill, edge, text = theme.BG, theme.TEXT_MUTED, theme.TEXT
        mark = pill_image((size, size), max(1, round(scaling)), rgb(self, fill), rgb(self, edge))
        self._mark_photo = ImageTk.PhotoImage(mark, master=canvas)
        if self._mark_items is None:
            self._mark_items = (
                canvas.create_image(0, 0, anchor="nw"),
                canvas.create_text(0, 0, anchor="center"),
            )
        image_item, text_item = self._mark_items
        canvas.coords(image_item, x, inset)
        canvas.itemconfigure(image_item, image=self._mark_photo)
        canvas.coords(text_item, x + size / 2, inset + size / 2)
        canvas.itemconfigure(
            text_item,
            text=self.mark_text,
            fill=theme.pick(text),
            font=theme.font(MARK_FONT_SIZE, bold=True).create_scaled_tuple(scaling),
        )
        canvas.tag_raise(image_item)
        canvas.tag_raise(text_item)

    async def _load_ring(self) -> None:
        face = self._face
        width = max(1, round(RING_WIDTH * self.scaling))
        image = await _ring(face.size, width, face.radius, rgb(self, theme.BRAND))
        if not self.winfo_exists():
            return
        canvas = self._sheet
        self._ring_photo = ImageTk.PhotoImage(image, master=canvas)
        if self._ring_item is None:
            self._ring_item = canvas.create_image(0, 0, anchor="nw")
        canvas.itemconfigure(self._ring_item, image=self._ring_photo)
        if self._mark_items is not None:  # the mark stays on top of the ring
            canvas.tag_raise(self._ring_item)
            for item in self._mark_items:
                canvas.tag_raise(item)
        self._show_ring()

    @property
    def ring_shown(self) -> bool:
        if self._ring_item is None:
            return False
        state = self._sheet.tk.call(str(self._sheet), "itemcget", self._ring_item, "-state")
        return str(state) != "hidden"

    def _show_ring(self) -> None:
        if self._ring_item is not None:
            self._sheet.itemconfigure(self._ring_item, state="normal" if self._chosen else "hidden")

    def destroy(self) -> None:
        if self._ring_task is not None:
            self._ring_task.cancel()
        super().destroy()


# -- the window ----------------------------------------------------------------------------------


class GetStartedDialog(ctk.CTkToplevel):
    """Borderless and modal over `master`. `profile_id`: the person whose shows it leaves out
    and follows."""

    def __init__(
        self,
        master: ctk.CTk,
        *,
        library: PickLibrary,
        discovery: PickDiscovery,
        images: ImageCache,
        profile_id: int,
    ) -> None:
        super().__init__(master, fg_color=theme.BG)
        self._library = library
        self._discovery = discovery
        self._images = images
        self._profile_id = profile_id
        self._tasks: set[asyncio.Task[Any]] = set()
        self._search_task: asyncio.Task[None] | None = None
        self._kinds = available_lists(discovery.has_tmdb_key)
        self._lists: dict[ListKind, list[Pick]] | None = None  # None while loading
        self._failed: frozenset[ListKind] = frozenset()
        self._current: ListKind | None = self._kinds[0]  # None: search results show
        self._query = ""
        self._results: list[Pick] | None = None  # the search's; None while it runs
        self._search_failed = False
        self._chosen: dict[tuple[str, int], Pick] = {}  # in the order they were chosen
        self._followed: set[tuple[str, int]] = set()  # followed from here: left out
        self._cards: dict[tuple[str, int], PickCard] = {}
        self._following = False

        self.title(t(TITLE))
        set_window_icon(self)
        self.resizable(False, False)
        self.transient(master)

        scaling = ctk.ScalingTracker.get_window_scaling(self)
        left, _top, right, _bottom = screen_area(master)
        room = int((right - left) / scaling) - WINDOW_FRAME - 2 * SCREEN_MARGIN
        width = max(1, min(DIALOG_WIDTH, room))
        height = min(DIALOG_HEIGHT, max_dialog_height(master, scaling))
        # Its size: the frame doesn't follow what it holds.
        outer = ctk.CTkFrame(self, fg_color="transparent", width=width, height=height)
        outer.pack(fill="both", expand=True)
        outer.pack_propagate(False)
        body = ctk.CTkFrame(outer, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_Y)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(3, weight=1)

        heading = heading_row(body, t(TITLE), right_room=CLOSE_CLEARANCE)
        heading.grid(row=0, column=0, sticky="ew")
        subtitle = text_label(
            body, t(SUBTITLE), size=DETAIL_SIZE, color=theme.TEXT_MUTED, wraplength=width - 2 * PAD_X
        )
        subtitle.grid(row=1, column=0, sticky="w", pady=(LINE_GAP, 0))
        self._build_bar(body).grid(row=2, column=0, sticky="ew", pady=(BAR_GAP, AREA_GAP))
        self._area = ScrollableFrame(
            body,
            fg_color=theme.CELL_BG,
            corner_radius=AREA_RADIUS,
            scrollbar_button_color=theme.SURFACE,
            scrollbar_button_hover_color=theme.EDGE,
        )
        self._area.grid(row=3, column=0, sticky="nsew")
        self._message = ctk.CTkLabel(
            body,
            text="",
            font=theme.font(DETAIL_SIZE),
            text_color=theme.DANGER,
            anchor="w",
            justify="left",
            wraplength=width - 2 * PAD_X,
        )
        self._build_footer(body).grid(row=5, column=0, sticky="ew", pady=(FOOTER_GAP, 0))

        self.bind("<Escape>", lambda _event: self.destroy())
        self.borderless = make_borderless(
            self, drag_widgets=(self, outer, body, heading, subtitle), on_close=self.destroy
        )
        self._paint_chips()
        self._update_footer()
        self._render()
        place_over(self, master)
        activate_later(self)
        self._spawn(self._load_lists(), "GetStarted.lists")

    # -- layout --------------------------------------------------------------------------------

    def _build_bar(self, master: Any) -> ctk.CTkFrame:
        bar = ctk.CTkFrame(master, fg_color="transparent")
        bar.grid_columnconfigure(1, weight=1)
        chips = ctk.CTkFrame(bar, fg_color="transparent")
        chips.grid(row=0, column=0, sticky="w")
        font = theme.font(CHIP_FONT_SIZE)
        self._chips: dict[ListKind, ctk.CTkButton] = {}
        for kind in self._kinds:
            text = t(CHIP_TEXTS[kind])
            chip = ctk.CTkButton(
                chips,
                text=text,
                width=round(font.measure(text)) + 2 * CHIP_PAD,
                height=CHIP_HEIGHT,
                corner_radius=CHIP_HEIGHT // 2,
                font=font,
                command=lambda kind=kind: self.show_list(kind),
            )
            chip.pack(side="left", padx=(0, CHIP_GAP))
            self._chips[kind] = chip
        self.search_field = ctk.CTkEntry(
            bar,
            placeholder_text=t(SEARCH_HINT),
            height=SEARCH_HEIGHT,
            corner_radius=8,
            border_width=1,
            border_color=theme.EDGE,
            fg_color=theme.SURFACE,
            text_color=theme.TEXT,
            placeholder_text_color=theme.TEXT_MUTED,
            font=theme.font(CHIP_FONT_SIZE),
        )
        self.search_field.grid(row=0, column=1, sticky="ew", padx=(BAR_GAP - CHIP_GAP, 0))
        self.search_field.bind("<Return>", lambda _event: self.search(self.search_field.get()))
        return bar

    def _build_footer(self, master: Any) -> ctk.CTkFrame:
        footer = ctk.CTkFrame(master, fg_color="transparent")
        self._count = ctk.CTkLabel(
            footer, text="", font=theme.font(DETAIL_SIZE), text_color=theme.TEXT_MUTED
        )
        self._count.pack(side="left")
        self.follow_button = primary_button(
            footer, follow_text(0), self.follow, width=FOLLOW_WIDTH
        )
        self.follow_button.pack(side="right")
        self.not_now_button = secondary_button(footer, t(NOT_NOW_TEXT), self.destroy)
        self.not_now_button.pack(side="right", padx=(0, 10))
        return footer

    # -- state the tests read ------------------------------------------------------------------

    @property
    def current(self) -> ListKind | None:
        """The chip selected; None while search results show."""
        return self._current

    @property
    def chips(self) -> tuple[ListKind, ...]:
        return self._kinds

    @property
    def chosen(self) -> tuple[Pick, ...]:
        return tuple(self._chosen.values())

    @property
    def cards(self) -> dict[tuple[str, int], PickCard]:
        """The posters on show, by `Pick.key`."""
        return dict(self._cards)

    @property
    def count_text(self) -> str:
        return str(self._count.cget("text"))

    @property
    def message_text(self) -> str:
        """The failure line ("" when hidden)."""
        return str(self._message.cget("text")) if self._message.winfo_manager() else ""

    def area_texts(self) -> list[str]:
        """The texts of the poster area's lines ("Loading…", "Results for …")."""
        return [
            str(child.cget("text"))
            for child in self._area.winfo_children()
            if isinstance(child, ctk.CTkLabel)
        ]

    # -- the lists -----------------------------------------------------------------------------

    async def _load_lists(self) -> None:
        lists: dict[ListKind, list[Pick]]
        failed: frozenset[ListKind]
        if self._discovery.has_tmdb_key:
            try:
                dashboard = await self._discovery.tv_dashboard(profile_id=self._profile_id)
            except Exception as exc:
                log.warning("Get started: TMDB's TV lists didn't load (%s)", type(exc).__name__)
                lists, failed = {kind: [] for kind in ListKind}, frozenset(ListKind)
            else:
                lists, failed = dashboard_lists(dashboard)
        else:
            today = date.today()
            try:
                entries = await self._library.suggestions(
                    today,
                    today + timedelta(days=SCHEDULE_DAYS - 1),
                    profile_id=self._profile_id,
                    per_day=SCHEDULE_PER_DAY,
                )
            except Exception as exc:
                log.warning("Get started: the TV schedule didn't load (%s)", type(exc).__name__)
                lists, failed = {ListKind.ON_AIR: []}, frozenset({ListKind.ON_AIR})
            else:
                lists, failed = {ListKind.ON_AIR: schedule_picks(entries)}, frozenset()
        if not self.winfo_exists():
            return
        self._lists, self._failed = lists, failed
        log.info(
            "Get started: lists loaded (%s)",
            ", ".join(f"{kind.value} {len(picks)}" for kind, picks in lists.items()),
        )
        if self._current is not None:
            self._render()

    def show_list(self, kind: ListKind) -> None:
        """A chip: its list (also from search results, back to the lists)."""
        if kind == self._current:
            return
        self._cancel_search()
        self._current = kind
        self._paint_chips()
        self._render()

    def search(self, query: str) -> None:
        """Look `query` up on TVMaze; the results replace the grid, no chip selected."""
        query = query.strip()
        if not query:
            return
        self._cancel_search()
        self._current = None
        self._query = query
        self._results = None
        self._search_failed = False
        self._paint_chips()
        self._render()
        self._search_task = spawn(self._run_search(query), name="GetStarted.search")
        self._tasks.add(self._search_task)
        self._search_task.add_done_callback(self._tasks.discard)

    async def _run_search(self, query: str) -> None:
        try:
            results = await self._library.search(
                query, profile_id=self._profile_id, limit=SEARCH_LIMIT
            )
        except Exception as exc:
            log.warning("Get started: the show search failed (%s)", type(exc).__name__)
            picks: list[Pick] = []
            failed = True
        else:
            picks, failed = search_picks(results), False
        if not self.winfo_exists() or self._current is not None or query != self._query:
            return
        self._results, self._search_failed = picks, failed
        self._render()

    def _cancel_search(self) -> None:
        if self._search_task is not None:
            self._search_task.cancel()
            self._search_task = None

    def _paint_chips(self) -> None:
        for kind, chip in self._chips.items():
            selected = kind == self._current
            fill, hover = chip_color(selected)
            text = theme.ON_ACCENT if selected else theme.BUTTON_TEXT
            chip.configure(fg_color=fill, hover_color=hover, text_color=text)

    # -- the poster area -----------------------------------------------------------------------

    def _render(self) -> None:
        """The poster area for the chip selected, or for the search."""
        for child in self._area.winfo_children():
            destroy_tree(child)
        self._cards = {}
        if self._current is None:
            self._area_line(results_heading(self._query), theme.TEXT, pad=(12, 0), bold=True)
            if self._results is None:
                self._area_line(t(LOADING_TEXT))
            elif self._search_failed:
                self._area_line(t(FAILED_TEXT))
            else:
                self._show_picks(self._results, t(NO_RESULTS_TEXT))
        elif self._lists is None:
            self._area_line(t(LOADING_TEXT))
        elif self._current in self._failed:
            self._area_line(t(FAILED_TEXT))
        else:
            self._show_picks(self._lists.get(self._current, []), t(EMPTY_LIST_TEXT))
        self._area._parent_canvas.yview_moveto(0)  # CTk has no scroll API

    def _area_line(
        self,
        text: str,
        color: theme.Color = theme.TEXT_MUTED,
        *,
        pad: tuple[int, int] = (MESSAGE_PAD, MESSAGE_PAD),
        bold: bool = False,
    ) -> None:
        ctk.CTkLabel(
            self._area, text=text, font=theme.font(DETAIL_SIZE, bold=bold), text_color=color
        ).pack(fill="x", pady=pad)

    def _show_picks(self, picks: Sequence[Pick], empty: str) -> None:
        shown = [pick for pick in picks if pick.key not in self._followed]
        if not shown:
            self._area_line(empty)
            return
        grid = FlexGrid(
            self._area,
            item_width=PICK_SCALE.px(POSTER_CARD_WIDTH),
            pad_x=GRID_PAD,
            pad_y=GRID_PAD,
        )
        grid.pack(fill="x", pady=GRID_PAD)
        for pick in shown:
            self._cards[pick.key] = PickCard(
                grid,
                pick,
                images=self._images,
                chosen=pick.key in self._chosen,
                on_toggle=self.toggle,
            )
        grid.relayout()

    # -- choosing and following ----------------------------------------------------------------

    def toggle(self, pick: Pick) -> None:
        """Choose `pick`, or unchoose it."""
        if self._following:
            return
        if pick.key in self._chosen:
            del self._chosen[pick.key]
        else:
            self._chosen[pick.key] = pick
        card = self._cards.get(pick.key)
        if card is not None and card.winfo_exists():
            card.set_chosen(pick.key in self._chosen)
        self._update_footer()

    def _update_footer(self) -> None:
        count = len(self._chosen)
        self._count.configure(text=chosen_text(count))
        if self._following:
            self.follow_button.configure(text=t(FOLLOWING_TEXT), state="disabled")
        else:
            state = "normal" if count else "disabled"
            self.follow_button.configure(text=follow_text(count), state=state)

    def follow(self) -> None:
        """Follow every chosen show."""
        if self._following or not self._chosen:
            return
        self._following = True
        self._update_footer()
        self._spawn(self._follow(list(self._chosen.values())), "GetStarted.follow")

    async def _follow(self, picks: Sequence[Pick]) -> None:
        failed: list[Pick] = []
        try:
            for pick in picks:
                try:
                    if isinstance(pick.source, DiscoverShow):
                        await self._discovery.track(pick.source, profile_id=self._profile_id)
                    else:
                        await self._library.track(pick.source.id, profile_id=self._profile_id)
                except Exception as exc:
                    kind, show_id = pick.key
                    log.warning(
                        "Get started: couldn't follow %s show %d (%s)",
                        kind, show_id, type(exc).__name__,
                    )
                    failed.append(pick)
                else:
                    self._chosen.pop(pick.key, None)
                    self._followed.add(pick.key)
        finally:
            self._following = False
        log.info(
            "Get started: followed %d of %d shows (person %d)",
            len(picks) - len(failed), len(picks), self._profile_id,
        )
        if not self.winfo_exists():
            return
        if not failed:
            self.destroy()
            return
        self._message.configure(text=failed_text([pick.name for pick in failed]))
        self._message.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        self._update_footer()
        self._render()  # the followed ones leave the grid

    # -- helpers -------------------------------------------------------------------------------

    def _spawn(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task = spawn(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def destroy(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        super().destroy()
