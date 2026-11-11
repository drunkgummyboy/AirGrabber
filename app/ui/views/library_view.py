"""The Library page: what is in Plex, as a grid of posters (the switch's Library part, only while
Plex is connected; app/services/plex_library.py gives it its contents).

At the top (the owner's choice A, mockups/library-filters.html): "Library" with a round search box
at its right; under them the Shows and Films tabs with their counts, a line under the chosen one;
then the filters as chips (shows: All / Not followed / With gaps / Ended; films: All / 4K /
Recently added) and, at the right, Sort ▾ (Recently added / A–Z / Year). "From Plex · checked
08:53" goes to the main window's footer (`on_checked`). The cards follow the card size (Settings › Looks): the poster
from the Plex server (`posters`: an ImageCache fetching through PlexService.poster), the title
(two lines' room on every card, so a row's cards line up; longer ends in "…"),
"3 seasons · 34 episodes" or the year and quality, an amber "2 gaps" pill and, for a show,
"+ Follow" / "✓ Following" with a round phone button (a drawn phone: an emoji shows as a box
there) while a phone is connected: the same PosterCard as the other pages' cards. A poster or title opens the show's window or the film's info window. PAGE_SIZE
cards at first, "Show more" for the next ones: a library of a thousand films stays quick.
"""

from __future__ import annotations

import asyncio
import functools
import tkinter as tk
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import customtkinter as ctk

from app.i18n import N_, t
from app.models.plex import PlexTitle
from app.services import ImageCache, SettingsService
from app.services.plex import poster_url
from app.services.plex_library import (
    can_upgrade,
    FILM_FILTERS,
    FILTER_NAMES,
    SHOW_FILTERS,
    SORT_NAMES,
    LibraryContents,
    LibraryFilter,
    LibraryKindTab,
    LibraryShow,
    LibrarySort,
    PlexLibraryService,
    checked_text,
    film_line,
    gaps_text,
    page_slice,
    show_line,
    shown_films,
    shown_shows,
)
from app.ui import theme
from app.ui.card_scale import FULL, CardScale, grid_item_width
from app.ui.cards import RoundButton, phone_icon
from app.ui.download_control import CORNER_RADIUS as ROW_CORNER_RADIUS
from app.ui.download_control import GAP as ROW_GAP
from app.ui.missing_control import MISSING_COLOR
from app.ui.poster_card import (
    POSTER_CARD_WIDTH,
    PosterCard,
    TrackButton,
    row_font_size,
    row_height,
)
from app.ui.scrollable import ScrollableFrame
from app.ui.shelf_filter import CHIP_FONT_SIZE, CHIP_GAP, CHIP_HEIGHT, CHIP_PAD, chip_color
from app.ui.tasks import spawn
from app.ui.tooltip import Tooltip
from app.ui.views.base import BaseView, StatusReporter
from app.ui.widgets import FlexGrid, MessagePanel

TITLE = N_("Library")
SHOWS_TAB = N_("Shows")
FILMS_TAB = N_("Films")
SEARCH_TEXT = N_("Search your library…")
SORT_TEXT = N_("Sort")
FOLLOWING_TEXT = N_("✓ Following")
ADDING_TEXT = N_("Adding…")
UPGRADE_TEXT = N_("⬆ Upgrade")
UPGRADE_TIP = N_("Find a better copy: 1080p or 4K")
SEND_TIP = N_("Send to phone")
FOLLOW_FAILED = N_("Couldn't follow {name}: TVmaze doesn't know it")
OPEN_FAILED = N_("Couldn't find {name} on TVmaze")
MORE_TEXT = N_("Show more ({n})")
EMPTY_TITLE = N_("Nothing from Plex yet")
EMPTY_TEXT = N_("AirGrabber reads Plex every half hour; Settings → Plex → Check now reads it at once.")
NO_MATCH_TEXT = N_("Nothing here with this search or filter.")
PAGE_SIZE = 60  # cards at first and per "Show more"
SEARCH_WAIT_MS = 250
SEARCH_WIDTH = 280
SEARCH_HEIGHT = 34
TAB_FONT_SIZE = 15
COUNT_FONT_SIZE = 12
UNDERLINE = 3  # the chosen tab's line


class KindTab(ctk.CTkFrame):
    """Shows or Films: its name, its count in a small pill, and a line under it while chosen."""

    def __init__(self, master: Any, name: str, command: Callable[[], None]) -> None:
        super().__init__(master, fg_color="transparent", cursor="hand2")
        self.selected = False
        self._hover = False
        self.name = ctk.CTkLabel(
            self, text=name, width=0, font=theme.font(TAB_FONT_SIZE, bold=True), cursor="hand2"
        )
        self.name.grid(row=0, column=0, padx=(2, 0), pady=(4, 6))
        self.count = ctk.CTkLabel(
            self, text="", width=0, font=theme.font(COUNT_FONT_SIZE, bold=True), height=20,
            corner_radius=10, cursor="hand2",
        )
        self.count.grid(row=0, column=1, padx=(7, 2), pady=(4, 6))
        self.underline = ctk.CTkFrame(
            self, width=1, height=UNDERLINE, corner_radius=1, fg_color="transparent"
        )
        self.underline.grid(row=1, column=0, columnspan=2, sticky="ew")
        for widget in (self, self.name, self.count):
            widget.bind("<Button-1>", lambda _event: command(), add="+")
            widget.bind("<Enter>", lambda _event: self._set_hover(True), add="+")
            widget.bind("<Leave>", lambda _event: self._set_hover(False), add="+")
        self._paint()

    def set_count(self, n: int) -> None:
        self.count.configure(text=f" {n} ")

    def set_selected(self, selected: bool) -> None:
        if selected != self.selected:
            self.selected = selected
            self._paint()

    def _set_hover(self, inside: bool) -> None:
        if inside != self._hover:
            self._hover = inside
            self._paint()

    def _paint(self) -> None:
        on = self.selected
        self.name.configure(text_color=theme.TEXT if on or self._hover else theme.TEXT_MUTED)
        fill, _hover = chip_color(on)
        self.count.configure(fg_color=fill, text_color=theme.ON_ACCENT if on else theme.TEXT_MUTED)
        self.underline.configure(fg_color=theme.BRAND if on else "transparent")


class LibraryCard(PosterCard):
    """One show or film, as the other pages' poster cards (the same size, the title and its line
    on the cover's faded bottom): for a show "2 seasons · 34 episodes", an amber "2 gaps" pill in
    the corner (as My shows' "2 missing") and the row of a Discover card, + Follow / ✓ Following
    (TrackButton) with the round phone button beside it; for a film its year and quality, and as
    its row "⬆ Upgrade" (below 1080p, `on_upgrade`) with the round phone beside it, or "Send to
    phone" alone, or no row (the text at the bottom, as Get started's cards)."""

    def __init__(
        self,
        master: Any,
        title: PlexTitle,
        *,
        posters: ImageCache,
        scale: CardScale,
        line: str,
        tag: str = "",
        following: bool | None = None,
        on_open: Callable[[], object],
        on_follow: Callable[[], object] | None = None,
        on_send: Callable[[], object] | None = None,
        on_upgrade: Callable[[], object] | None = None,
    ) -> None:
        """`on_send`: Send to phone (the round phone beside the main button, or a film's own
        row); None: no phone to send to. `on_upgrade`: a film below 1080p's "⬆ Upgrade"."""
        has_row = following is not None or on_send is not None or on_upgrade is not None
        url = poster_url(title)
        super().__init__(
            master,
            title=title.title,
            meta=line,
            poster=lambda _width: url,
            images=posters,
            extra_height=0 if has_row else -row_height(scale),
            scale=scale,
            meta_row=True,
        )
        self.title = title
        self._set_open(on_open)
        if tag:
            self._set_badge(tag, MISSING_COLOR)
        self.follow_button: TrackButton | None = None
        self.send_button: ctk.CTkButton | RoundButton | None = None
        self.upgrade_button: ctk.CTkButton | None = None
        if not has_row:
            return
        row = ctk.CTkFrame(self, fg_color="transparent", height=row_height(scale))
        row.pack_propagate(False)
        self._pack_action(row)

        def send() -> None:
            if on_send is not None:
                on_send()

        def follow() -> None:
            if on_follow is not None:
                on_follow()

        def upgrade() -> None:
            if on_upgrade is not None:
                on_upgrade()

        if following is not None or on_upgrade is not None:
            if on_send is not None:
                phone = RoundButton(
                    row, icon=phone_icon, size=row_height(scale),
                    font=theme.font(row_font_size(scale)), command=send,
                )
                phone.pack(side="right", padx=(scale.px(ROW_GAP), 0))
                Tooltip(phone, t(SEND_TIP))
                self._follow_scale(phone.rescale)
                self.send_button = phone
            if following is not None:
                self.follow_button = TrackButton(
                    row, busy_text=t(ADDING_TEXT), command=follow, scale=scale
                )
                self.follow_button.pack(side="left", fill="both", expand=True)
                self.set_following(following)
            else:  # a film below 1080p: "do it", the filled accent like Follow
                self.upgrade_button = ctk.CTkButton(
                    row, text=t(UPGRADE_TEXT), height=row_height(scale),
                    corner_radius=scale.px(ROW_CORNER_RADIUS),
                    font=theme.font(row_font_size(scale), bold=True), fg_color=theme.ACCENT,
                    hover_color=theme.ACCENT_HOVER, text_color=theme.ON_ACCENT, command=upgrade,
                )
                self.upgrade_button.pack(side="left", fill="both", expand=True)
                Tooltip(self.upgrade_button, t(UPGRADE_TIP))
        elif on_send is not None:
            self.send_button = ctk.CTkButton(
                row, text=t(SEND_TIP), height=row_height(scale),
                corner_radius=scale.px(ROW_CORNER_RADIUS),
                font=theme.font(row_font_size(scale), bold=True), fg_color="transparent",
                border_width=1, border_color=theme.EDGE, hover_color=theme.HOVER,
                text_color=theme.BUTTON_TEXT, command=send,
            )
            self.send_button.pack(fill="both", expand=True)

    def set_following(self, following: bool) -> None:
        if self.follow_button is not None:
            self.follow_button.set_state("tracked" if following else "idle")

    def set_busy(self) -> None:
        """While Track looks the show up and adds it."""
        if self.follow_button is not None:
            self.follow_button.set_state("busy")

    @property
    def following(self) -> bool:
        return self.follow_button is not None and self.follow_button.cget("text") == t(
            FOLLOWING_TEXT
        )


class LibraryView(BaseView):
    def __init__(
        self,
        master: Any,
        *,
        service: PlexLibraryService,
        posters: ImageCache,
        settings: SettingsService,
        status: StatusReporter,
        on_open_show: Callable[[PlexTitle], Awaitable[bool]],
        on_open_film: Callable[[PlexTitle], None],
        on_upgrade_film: Callable[[PlexTitle], None] | None = None,
        on_send: Callable[[PlexTitle], None] | None = None,
        can_send: Callable[[], bool] = lambda: False,
        card_scale: Callable[[], CardScale] = lambda: FULL,
        on_checked: Callable[[str], None] = lambda text: None,
        **kwargs: Any,
    ) -> None:
        """`on_open_show(title)`: opens the show's window; False when TVmaze knows none.
        `on_open_film(title)`: the film's info window. `on_send(title)`: the Send to phone
        window, on the cards while `can_send()` (a phone is connected), read at every render.
        `on_checked(text)`: "From Plex · checked 08:53" after every load (the footer's)."""
        super().__init__(master, **kwargs)
        self._service = service
        self._posters = posters
        self._settings = settings
        self._status = status
        self._on_open_show = on_open_show
        self._on_open_film = on_open_film
        self._on_upgrade_film = on_upgrade_film
        self._on_send = on_send
        self._can_send = can_send
        self._card_scale = card_scale
        self._on_checked = on_checked
        self.checked_text = ""
        self._contents = LibraryContents()
        self._kind = LibraryKindTab.SHOWS
        self._sort = LibrarySort.ADDED
        self._filter = LibraryFilter.ALL
        self._shown = PAGE_SIZE
        self._search_job: str | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self.cards: list[LibraryCard] = []

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)
        # "Library" and the search box.
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=15, pady=(5, 0))
        header.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            header, text=t(TITLE), font=theme.font(20, bold=True), text_color=theme.TEXT
        ).grid(row=0, column=0, sticky="w")
        self.search = ctk.CTkEntry(
            header, width=SEARCH_WIDTH, height=SEARCH_HEIGHT, corner_radius=SEARCH_HEIGHT // 2,
            placeholder_text="🔍  " + t(SEARCH_TEXT), fg_color=theme.SURFACE,
            border_color=theme.CHIP_HOVER, text_color=theme.TEXT,
        )
        self.search.grid(row=0, column=2, sticky="e")
        self.search.bind("<KeyRelease>", self._search_typed, add="+")

        # The Shows and Films tabs on a thin line.
        tabs = ctk.CTkFrame(self, fg_color="transparent")
        tabs.grid(row=1, column=0, sticky="ew", padx=15, pady=(6, 0))
        tabs.grid_columnconfigure(2, weight=1)
        self.kind_tabs: dict[LibraryKindTab, KindTab] = {}
        kinds = ((LibraryKindTab.SHOWS, SHOWS_TAB), (LibraryKindTab.FILMS, FILMS_TAB))
        for column, (kind, name) in enumerate(kinds):
            tab = KindTab(tabs, t(name), functools.partial(self.set_kind, kind))
            tab.grid(row=0, column=column, sticky="w", padx=(0, 26))
            self.kind_tabs[kind] = tab
        ctk.CTkFrame(tabs, height=1, corner_radius=0, fg_color=theme.CHIP_HOVER).grid(
            row=1, column=0, columnspan=3, sticky="ew"
        )
        self._show_kinds()

        # The filters as chips, Sort ▾ at the right.
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=2, column=0, sticky="ew", padx=15, pady=(12, 10))
        bar.grid_columnconfigure(0, weight=1)
        self._chip_font = theme.font(CHIP_FONT_SIZE)
        self._chip_row = ctk.CTkFrame(bar, fg_color="transparent")
        self._chip_row.grid(row=0, column=0, sticky="w")
        self.filter_chips: dict[LibraryFilter, ctk.CTkButton] = {}
        self._make_chips()
        sort = ctk.CTkFrame(bar, fg_color="transparent")
        sort.grid(row=0, column=1, sticky="e")
        ctk.CTkLabel(
            sort, text=t(SORT_TEXT), font=self._chip_font, text_color=theme.TEXT_MUTED
        ).pack(side="left", padx=(0, 8))
        fill, hover = chip_color(False)
        self.sort_menu = ctk.CTkOptionMenu(
            sort, values=[t(name) for name in SORT_NAMES.values()], height=CHIP_HEIGHT,
            corner_radius=8, font=self._chip_font, dropdown_font=self._chip_font,
            fg_color=fill, button_color=fill, button_hover_color=hover,
            text_color=theme.BUTTON_TEXT, command=self._choose_sort,
        )
        self.sort_menu.set(t(SORT_NAMES[self._sort]))
        self.sort_menu.pack(side="left")

        self._body = ScrollableFrame(self, fg_color="transparent")
        self._body.grid(row=3, column=0, sticky="nsew", padx=15, pady=(0, 5))
        self._body.grid_columnconfigure(0, weight=1)
        self._grid: FlexGrid | None = None
        self.more_button: ctk.CTkButton | None = None
        self._message: MessagePanel | ctk.CTkLabel | None = None

    def _filters(self) -> tuple[LibraryFilter, ...]:
        return SHOW_FILTERS if self._kind is LibraryKindTab.SHOWS else FILM_FILTERS

    def _make_chips(self) -> None:
        """The chips of the chosen tab's filters (shows' and films' differ)."""
        for chip in self.filter_chips.values():
            chip.destroy()
        self.filter_chips = {}
        for choice in self._filters():
            text = t(FILTER_NAMES[choice])
            chip = ctk.CTkButton(
                self._chip_row, text=text,
                width=round(self._chip_font.measure(text)) + 2 * CHIP_PAD, height=CHIP_HEIGHT,
                corner_radius=CHIP_HEIGHT // 2, font=self._chip_font,
                command=functools.partial(self._choose_filter, choice),
            )
            chip.pack(side="left", padx=(0, CHIP_GAP))
            self.filter_chips[choice] = chip
        self._paint_chips()

    def _paint_chips(self) -> None:
        for choice, chip in self.filter_chips.items():
            on = choice is self._filter
            fill, hover = chip_color(on)
            chip.configure(
                fg_color=fill, hover_color=hover,
                text_color=theme.ON_ACCENT if on else theme.BUTTON_TEXT,
            )

    def _show_kinds(self) -> None:
        """The tabs' counts and which one is chosen."""
        counts = {
            LibraryKindTab.SHOWS: len(self._contents.shows),
            LibraryKindTab.FILMS: len(self._contents.films),
        }
        for kind, tab in self.kind_tabs.items():
            tab.set_count(counts[kind])
            tab.set_selected(kind is self._kind)

    # -- choices ---------------------------------------------------------------------------

    def set_kind(self, kind: LibraryKindTab) -> None:
        """Shows or Films, as a click on its tab."""
        if kind is self._kind:
            return
        self._kind = kind
        self._filter = LibraryFilter.ALL
        self._show_kinds()
        self._make_chips()
        self._shown = PAGE_SIZE
        self._render()

    def _choose_sort(self, label: str) -> None:
        self._sort = next(s for s, name in SORT_NAMES.items() if t(name) == label)
        self._shown = PAGE_SIZE
        self._render()

    def _choose_filter(self, choice: LibraryFilter) -> None:
        if choice is self._filter:
            return
        self._filter = choice
        self._paint_chips()
        self._shown = PAGE_SIZE
        self._render()

    def _search_typed(self, _event: object = None) -> None:
        if self._search_job is not None:
            self.after_cancel(self._search_job)
        self._search_job = self.after(SEARCH_WAIT_MS, self._searched)

    def _searched(self) -> None:
        self._search_job = None
        self._shown = PAGE_SIZE
        self._render()

    # -- loading and showing -------------------------------------------------------------------

    async def load(self) -> None:
        contents = await self._service.contents(profile_id=self._settings.current.current_profile)
        if not self.winfo_exists():
            return
        self._contents = contents
        self._show_kinds()
        self.checked_text = checked_text(contents.checked_at)
        self._on_checked(self.checked_text)
        self._render()

    def _clear(self) -> None:
        for child in self._body.winfo_children():
            child.destroy()
        self.cards = []
        self._grid = None
        self.more_button = None
        self._message = None

    def _render(self) -> None:
        self._clear()
        query = self.search.get()
        contents = self._contents
        if not contents.shows and not contents.films:
            self._message = MessagePanel(self._body, t(EMPTY_TITLE), t(EMPTY_TEXT), height=300)
            self._message.grid(row=0, column=0, sticky="nsew")
            return
        scale = self._card_scale()
        items: Sequence[LibraryShow | PlexTitle]
        if self._kind is LibraryKindTab.SHOWS:
            items = shown_shows(contents.shows, self._filter, self._sort, query)
        else:
            items = shown_films(contents.films, self._filter, self._sort, query)
        if not items:
            self._message = ctk.CTkLabel(
                self._body, text=t(NO_MATCH_TEXT), font=theme.font(14),
                text_color=theme.TEXT_MUTED,
            )
            self._message.grid(row=0, column=0, pady=40)
            return
        page: Sequence[LibraryShow | PlexTitle]
        page, more = page_slice(items, self._shown)
        item_width = grid_item_width(POSTER_CARD_WIDTH, scale)  # as every page's poster cards
        grid = FlexGrid(self._body, item_width=item_width, pad_x=8, pad_y=10)
        grid.grid(row=0, column=0, sticky="ew")
        self._grid = grid
        for item in page:
            self.cards.append(self._card(grid, item, scale))
        grid.relayout()
        if more:
            self.more_button = ctk.CTkButton(
                self._body, text=t(MORE_TEXT, n=more), width=200, fg_color="transparent",
                border_width=1, border_color=theme.EDGE, hover_color=theme.HOVER,
                text_color=theme.BUTTON_TEXT, command=self.show_more,
            )
            self.more_button.grid(row=1, column=0, pady=(6, 16))

    def _card(self, grid: FlexGrid, item: LibraryShow | PlexTitle, scale: CardScale) -> LibraryCard:
        send = self._on_send if self._on_send is not None and self._can_send() else None
        if isinstance(item, LibraryShow):
            title = item.title
            card = LibraryCard(
                grid, title, posters=self._posters, scale=scale, line=show_line(title),
                tag=gaps_text(item.gaps), following=item.following,
                on_open=lambda: self._open_show(title),
                on_send=(lambda: send(title)) if send is not None else None,
            )
            if card.follow_button is not None:
                card.follow_button.configure(command=lambda: self._follow(card, item))
            return card
        line = film_line(item)
        if item.resolution:
            line = f"{line} · {item.resolution}" if line else item.resolution
        upgrade = self._on_upgrade_film if can_upgrade(item) else None
        return LibraryCard(
            grid, item, posters=self._posters, scale=scale, line=line,
            on_open=lambda: self._on_open_film(item),
            on_send=(lambda: send(item)) if send is not None else None,
            on_upgrade=(lambda: upgrade(item)) if upgrade is not None else None,
        )

    def show_more(self) -> None:
        self._shown += PAGE_SIZE
        top = self._body._parent_canvas.yview()[0]  # noqa: SLF001 - keep the place
        self._render()
        self.after_idle(lambda: self._body._parent_canvas.yview_moveto(top))  # noqa: SLF001

    # -- actions -------------------------------------------------------------------------------

    def _start(self, coro: Any, name: str) -> None:
        task: asyncio.Task[Any] = spawn(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _open_show(self, title: PlexTitle) -> None:
        async def run() -> None:
            if not await self._on_open_show(title) and self.winfo_exists():
                self._report(t(OPEN_FAILED, name=title.title))

        self._start(run(), "LibraryView.open")

    def _follow(self, card: LibraryCard, show: LibraryShow) -> None:
        if card.follow_button is None or card.following:
            return
        card.set_busy()

        async def run() -> None:
            followed = await self._service.follow(
                show.title, profile_id=self._settings.current.current_profile
            )
            if not card.winfo_exists():
                return
            card.set_following(followed)
            if not followed:
                self._report(t(FOLLOW_FAILED, name=show.title.title))

        self._start(run(), "LibraryView.follow")

    def _report(self, text: str) -> None:
        try:
            self._status(text, clear_after_ms=6000)
        except tk.TclError:
            pass

    def destroy(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._search_job is not None:
            self.after_cancel(self._search_job)
            self._search_job = None
        super().destroy()
