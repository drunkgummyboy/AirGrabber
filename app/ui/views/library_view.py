"""The Library page: what is in Plex, as a grid of posters (the switch's Library part, only while
Plex is connected; app/services/plex_library.py gives it its contents).

At the top: "Library", Shows (n) | Films (n), a search box and "From Plex · checked 08:53"; under
it Sort (Recently added / A–Z / Year) and Show (shows: All / Not followed / With gaps / Ended;
films: All / 4K / Recently added). The cards follow the card size (Settings › Looks): the poster
from the Plex server (`posters`: an ImageCache fetching through PlexService.poster), the title
(two lines' room on every card, so a row's cards line up; longer ends in "…"),
"3 seasons · 34 episodes" or the year and quality, an amber "2 gaps" and, for a show, "✓ Following"
or "+ Follow". A poster or title opens the show's window or the film's info window. PAGE_SIZE
cards at first, "Show more" for the next ones: a library of a thousand films stays quick.
"""

from __future__ import annotations

import asyncio
import tkinter as tk
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import customtkinter as ctk

from app.i18n import N_, t
from app.models.plex import PlexTitle
from app.services import ImageCache, SettingsService
from app.services.plex import poster_url
from app.services.plex_library import (
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
from app.ui.card_scale import FULL, CardScale
from app.ui.scrollable import ScrollableFrame
from app.ui.tasks import spawn
from app.ui.text_fit import SLACK, fit_to_lines
from app.ui.tooltip import Tooltip
from app.ui.views.base import BaseView, StatusReporter
from app.ui.widgets import FlexGrid, MessagePanel, PosterImage

TITLE = N_("Library")
SHOWS_TAB = N_("Shows ({n})")
FILMS_TAB = N_("Films ({n})")
SEARCH_TEXT = N_("Search your library…")
SORT_TEXT = N_("Sort:")
SHOW_TEXT = N_("Show:")
FOLLOW_TEXT = N_("+ Follow")
FOLLOWING_TEXT = N_("✓ Following")
SEND_TEXT = N_("📱 Send to phone")
SEND_TIP = N_("Send to phone")
SEND_WIDTH = 40  # the 📱 beside a show's Follow
FOLLOW_FAILED = N_("Couldn't follow {name}: TVmaze doesn't know it")
OPEN_FAILED = N_("Couldn't find {name} on TVmaze")
MORE_TEXT = N_("Show more ({n})")
EMPTY_TITLE = N_("Nothing from Plex yet")
EMPTY_TEXT = N_("AirGrabber reads Plex every half hour; Settings → Plex → Check now reads it at once.")
NO_MATCH_TEXT = N_("Nothing here with this search or filter.")
PAGE_SIZE = 60  # cards at first and per "Show more"
CARD_WIDTH = 176  # units at the XL card size
TITLE_LINES = 2  # a card's title: longer ones end in "…"
SEARCH_WAIT_MS = 250
BAR_HEIGHT = 28
BAR_RADIUS = 7
BAR_FONT_SIZE = 13


def _choices(
    master: Any, values: Sequence[str], command: Callable[[str], None]
) -> ctk.CTkSegmentedButton:
    """Compact segmented buttons, as My shows' sort and filter."""
    return ctk.CTkSegmentedButton(
        master,
        values=list(values),
        height=BAR_HEIGHT,
        corner_radius=BAR_RADIUS,
        font=theme.font(BAR_FONT_SIZE),
        fg_color=theme.SURFACE,
        selected_color=theme.SELECTED,
        selected_hover_color=theme.SELECTED_HOVER,
        unselected_color=theme.SURFACE,
        unselected_hover_color=theme.CHIP_HOVER,
        text_color=theme.BUTTON_TEXT,
        command=command,
    )


class LibraryCard(ctk.CTkFrame):
    """One show or film: poster, title, its line, a tag, and for a show ✓ Following / + Follow."""

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
    ) -> None:
        """`on_send`: 📱 Send to phone (beside Follow for a show, its own button for a film);
        None: no phone to send to."""
        super().__init__(
            master, fg_color=theme.SURFACE, corner_radius=10, border_width=1,
            border_color=theme.EDGE,
        )
        width = scale.px(CARD_WIDTH)
        self.title = title
        self.poster = PosterImage(self, width=width - 12, height=round((width - 12) * 1.5))
        self.poster.grid(row=0, column=0, padx=6, pady=(6, 4))
        self.poster.set_on_click(on_open)
        self.poster.show(posters, poster_url(title))
        # At most TITLE_LINES lines, and always that tall: a long title neither stretches its card
        # nor makes it taller than its neighbours (the whole title in a tip when it's cut).
        title_font = theme.font(scale.font(14, 12), bold=True)
        shown = fit_to_lines(title.title, width - 20 - SLACK, TITLE_LINES, title_font.measure)
        name = ctk.CTkLabel(
            self, text=shown, font=title_font, text_color=theme.TEXT, wraplength=width - 20,
            height=TITLE_LINES * title_font.metrics("linespace"), cursor="hand2",
        )
        name.grid(row=1, column=0, padx=8, sticky="ew")
        name.bind("<Button-1>", lambda _event: on_open())
        if shown != title.title:
            Tooltip(name, title.title)
        ctk.CTkLabel(
            self, text=line, font=theme.font(scale.font(12, 10)), text_color=theme.TEXT_MUTED,
            height=scale.text_height(18, scale.font(12, 10)),
        ).grid(row=2, column=0, padx=8)
        self.tag = ctk.CTkLabel(
            self, text=tag, font=theme.font(scale.font(12, 10), bold=True),
            text_color=theme.WARNING, height=scale.text_height(18, scale.font(12, 10)),
        )
        self.tag.grid(row=3, column=0, padx=8)
        self.follow_button: ctk.CTkButton | None = None
        self.send_button: ctk.CTkButton | None = None
        if following is None and on_send is None:
            ctk.CTkFrame(self, width=1, height=6, fg_color="transparent").grid(row=4, column=0)
            return
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=4, column=0, padx=8, pady=(4, 8), sticky="ew")
        actions.grid_columnconfigure(0, weight=1)
        height = scale.px(30, minimum=24)
        font = theme.font(scale.font(13, 11), bold=True)
        if following is not None:
            self.follow_button = ctk.CTkButton(
                actions, height=height, font=font, command=on_follow
            )
            self.follow_button.grid(row=0, column=0, sticky="ew")
            self.set_following(following)
        if on_send is not None:
            alone = following is None
            self.send_button = ctk.CTkButton(
                actions,
                text=t(SEND_TEXT) if alone else "📱",
                width=0 if alone else scale.px(SEND_WIDTH, minimum=30),
                height=height,
                font=font,
                fg_color="transparent",
                border_width=1,
                border_color=theme.EDGE,
                hover_color=theme.HOVER,
                text_color=theme.BUTTON_TEXT,
                command=on_send,
            )
            self.send_button.grid(
                row=0, column=0 if alone else 1, sticky="ew", padx=(0 if alone else 6, 0)
            )
            if not alone:
                Tooltip(self.send_button, t(SEND_TIP))

    def set_following(self, following: bool) -> None:
        button = self.follow_button
        if button is None:
            return
        if following:
            button.configure(
                text=t(FOLLOWING_TEXT), fg_color="transparent", border_width=1,
                border_color=theme.SUCCESS, text_color=theme.SUCCESS, hover=False, state="normal",
            )
        else:
            button.configure(
                text=t(FOLLOW_TEXT), fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER,
                border_width=0, text_color=theme.ON_ACCENT, hover=True, state="normal",
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
        on_send: Callable[[PlexTitle], None] | None = None,
        can_send: Callable[[], bool] = lambda: False,
        card_scale: Callable[[], CardScale] = lambda: FULL,
        **kwargs: Any,
    ) -> None:
        """`on_open_show(title)`: opens the show's window; False when TVmaze knows none.
        `on_open_film(title)`: the film's info window. `on_send(title)`: the Send to phone
        window, on the cards while `can_send()` (a phone is connected), read at every render."""
        super().__init__(master, **kwargs)
        self._service = service
        self._posters = posters
        self._settings = settings
        self._status = status
        self._on_open_show = on_open_show
        self._on_open_film = on_open_film
        self._on_send = on_send
        self._can_send = can_send
        self._card_scale = card_scale
        self._contents = LibraryContents()
        self._kind = LibraryKindTab.SHOWS
        self._sort = LibrarySort.ADDED
        self._filter = LibraryFilter.ALL
        self._shown = PAGE_SIZE
        self._search_job: str | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self.cards: list[LibraryCard] = []

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=15, pady=5)
        header.grid_columnconfigure(3, weight=1)
        ctk.CTkLabel(
            header, text=t(TITLE), font=theme.font(18, bold=True), text_color=theme.TEXT
        ).grid(row=0, column=0, padx=(0, 14))
        self.kind_choice = _choices(header, self._kind_labels(), self._choose_kind)
        self.kind_choice.grid(row=0, column=1)
        self.kind_choice.set(self._kind_labels()[0])
        self.search = ctk.CTkEntry(
            header, width=240, placeholder_text=t(SEARCH_TEXT), fg_color=theme.SURFACE,
            border_color=theme.EDGE, text_color=theme.TEXT,
        )
        self.search.grid(row=0, column=2, padx=(14, 0))
        self.search.bind("<KeyRelease>", self._search_typed, add="+")
        self.checked = ctk.CTkLabel(
            header, text="", font=theme.font(13), text_color=theme.TEXT_MUTED
        )
        self.checked.grid(row=0, column=4, sticky="e")

        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="w", padx=15, pady=(0, 8))
        self._label(bar, t(SORT_TEXT)).pack(side="left", padx=(0, 6))
        sorts = [t(name) for name in SORT_NAMES.values()]
        self.sort_choice = _choices(bar, sorts, self._choose_sort)
        self.sort_choice.set(t(SORT_NAMES[self._sort]))
        self.sort_choice.pack(side="left")
        self._label(bar, t(SHOW_TEXT)).pack(side="left", padx=(20, 6))
        self._filter_bar = bar
        self.filter_choice = self._new_filter_choice()

        self._body = ScrollableFrame(self, fg_color="transparent")
        self._body.grid(row=2, column=0, sticky="nsew", padx=15, pady=(0, 5))
        self._body.grid_columnconfigure(0, weight=1)
        self._grid: FlexGrid | None = None
        self.more_button: ctk.CTkButton | None = None
        self._message: MessagePanel | ctk.CTkLabel | None = None

    def _label(self, master: Any, text: str) -> ctk.CTkLabel:
        return ctk.CTkLabel(
            master, text=text, font=theme.font(BAR_FONT_SIZE), text_color=theme.TEXT_MUTED
        )

    def _filters(self) -> tuple[LibraryFilter, ...]:
        return SHOW_FILTERS if self._kind is LibraryKindTab.SHOWS else FILM_FILTERS

    def _new_filter_choice(self) -> ctk.CTkSegmentedButton:
        choice = _choices(
            self._filter_bar, [t(FILTER_NAMES[f]) for f in self._filters()], self._choose_filter
        )
        choice.set(t(FILTER_NAMES[self._filter]))
        choice.pack(side="left")
        return choice

    def _kind_labels(self) -> list[str]:
        shows, films = len(self._contents.shows), len(self._contents.films)
        return [t(SHOWS_TAB, n=shows), t(FILMS_TAB, n=films)]

    # -- choices ---------------------------------------------------------------------------

    def _choose_kind(self, label: str) -> None:
        kind = LibraryKindTab.SHOWS if label == self._kind_labels()[0] else LibraryKindTab.FILMS
        if kind is self._kind:
            return
        self._kind = kind
        self._filter = LibraryFilter.ALL
        self.filter_choice.destroy()
        self.filter_choice = self._new_filter_choice()
        self._shown = PAGE_SIZE
        self._render()

    def _choose_sort(self, label: str) -> None:
        self._sort = next(s for s, name in SORT_NAMES.items() if t(name) == label)
        self._shown = PAGE_SIZE
        self._render()

    def _choose_filter(self, label: str) -> None:
        self._filter = next(f for f in self._filters() if t(FILTER_NAMES[f]) == label)
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

    def set_kind(self, kind: LibraryKindTab) -> None:
        """Shows or Films, as a click on the tabs."""
        label = self._kind_labels()[0 if kind is LibraryKindTab.SHOWS else 1]
        self.kind_choice.set(label)
        self._choose_kind(label)

    # -- loading and showing -------------------------------------------------------------------

    async def load(self) -> None:
        contents = await self._service.contents(profile_id=self._settings.current.current_profile)
        if not self.winfo_exists():
            return
        self._contents = contents
        labels = self._kind_labels()
        self.kind_choice.configure(values=labels)
        self.kind_choice.set(labels[0 if self._kind is LibraryKindTab.SHOWS else 1])
        self.checked.configure(text=checked_text(contents.checked_at))
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
        grid = FlexGrid(self._body, item_width=scale.px(CARD_WIDTH), pad_x=8, pad_y=10)
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
        return LibraryCard(
            grid, item, posters=self._posters, scale=scale, line=line,
            on_open=lambda: self._on_open_film(item),
            on_send=(lambda: send(item)) if send is not None else None,
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
        if card.follow_button is None or card.follow_button.cget("text") == t(FOLLOWING_TEXT):
            return
        card.follow_button.configure(state="disabled")

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
