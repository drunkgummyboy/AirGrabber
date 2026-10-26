"""Shared base of the TMDB dashboard pages (Discover, Movies): titled shelves of cards under
the filter bar (genre chips and "Hide what I already have", app/ui/shelf_filter.py)."""

from __future__ import annotations

import asyncio
import dataclasses
import tkinter as tk
from abc import abstractmethod
from collections.abc import Callable, Coroutine, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Generic, TypeVar

import customtkinter as ctk

from app.i18n import t
from app.services import ImageCache, SettingsService
from app.services.tmdb import TMDBAuthError, TMDBError, TMDBNotConfigured
from app.ui import theme
from app.ui.card_scale import FULL, CardScale, grid_item_width
from app.ui.poster_card import POSTER_CARD_WIDTH
from app.ui.shelf_filter import FilterBar, Genre, chip_genres, empty_text
from app.ui.tasks import spawn
from app.ui.views.base import BaseView, StatusReporter
from app.ui.widgets import FlexGrid, LoadingBar, MessagePanel, destroy_tree

T = TypeVar("T")

_MESSAGE_HEIGHT = 300  # message panels sit in a scroll frame, which gives them no height


@dataclass(frozen=True, slots=True)
class Shelf(Generic[T]):
    """One section of a dashboard page."""

    icon: str
    title: str
    color: theme.Color  # of the section title
    items: Sequence[T]
    failed: bool = False  # TMDB couldn't load this shelf (the others did)


def filter_shelves(shelves: Sequence[Shelf[T]], keep: Callable[[T], bool]) -> list[Shelf[T]]:
    """The shelves to show with only the items `keep` accepts: a shelf left without items is
    left out; one that failed to load stays (its "Couldn't load" line)."""
    shown: list[Shelf[T]] = []
    for shelf in shelves:
        if shelf.failed:
            shown.append(shelf)
            continue
        items = tuple(item for item in shelf.items if keep(item))
        if items:
            shown.append(dataclasses.replace(shelf, items=items))
    return shown


def has_items(shelves: Sequence[Shelf[T]]) -> bool:
    return any(shelf.items for shelf in shelves)


class ShelvesView(BaseView, Generic[T]):
    """A scrolling column of shelves from one TMDB dashboard, under the filter bar.

    Subclasses fetch the dashboard (`_fetch_shelves`), build one card per item (`_card`, at
    `self._scale`: the card size `card_scale` gave when the load started) and tell the filter
    an item's genres (`_genres`) and whether the user already has it (`_is_owned`).
    `reload()` refetches from TMDB instead of using the dashboard cache.

    The filter only re-renders the shelves the last load fetched. The genre chip is kept for
    the session (the page lives as long as the window); the switch is the Settings field
    `_hide_owned_setting`.
    """

    _body_pady: tuple[int, int] = (5, 5)
    # The page's "Hide what I already have" setting, its switch's hover tip, and the page's
    # line when that switch alone left every shelf empty (both in English: shown through t()).
    _hide_owned_setting: ClassVar[str]
    _hide_owned_tip: ClassVar[str]
    _all_owned_text: ClassVar[str]

    def __init__(
        self,
        master: Any,
        *,
        images: ImageCache,
        status: StatusReporter,
        settings: SettingsService,
        on_open_settings: Callable[[], None],
        card_scale: Callable[[], CardScale] = lambda: FULL,
        **kwargs: Any,
    ) -> None:
        """`card_scale`: the card size (Settings), read at every load. `settings`: where the
        "Hide what I already have" switch is kept."""
        super().__init__(master, **kwargs)
        self._images = images
        self._status = status
        self._settings = settings
        self._on_open_settings = on_open_settings
        self._card_scale = card_scale
        self._scale = card_scale()
        self._force = False  # the next load bypasses the dashboard cache
        self._tasks: set[asyncio.Task[None]] = set()
        self._genre: Genre | None = None  # the selected chip; None = All
        self._hide_owned = bool(getattr(settings.current, self._hide_owned_setting))
        self._shelves: list[Shelf[T]] | None = None  # the last load's, unfiltered
        self._render_generation = 0  # bumped to stop a render in progress

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self.filter_bar = FilterBar(
            self,
            hide_owned=self._hide_owned,
            hide_tip=t(self._hide_owned_tip),
            on_genre=self._pick_genre,
            on_hide_owned=self._set_hide_owned,
        )
        # Gridded in row 1 once shelves are loaded (`_show_filter_bar`).
        self._body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._body.grid(row=2, column=0, sticky="nsew", padx=15, pady=self._body_pady)

    @abstractmethod
    async def _fetch_shelves(self, *, force: bool) -> list[Shelf[T]]:
        """The dashboard's shelves in display order. TMDB errors propagate."""

    @abstractmethod
    def _card(self, master: Any, item: T) -> None:
        """Create the card for `item` as a child of `master` (a FlexGrid lays it out)."""

    @abstractmethod
    def _genres(self, item: T) -> frozenset[Genre]:
        """The genre chips `item` belongs to."""

    @abstractmethod
    def _is_owned(self, item: T) -> bool:
        """Whether "Hide what I already have" hides `item`."""

    def reload(self) -> None:
        """Reload with fresh TMDB lists."""
        self._force = True
        self.refresh()

    # -- loading -------------------------------------------------------------------------------

    def _clear_body(self) -> None:
        for child in self._body.winfo_children():
            destroy_tree(child)  # three full shelves take ~600 ms to destroy() widget by widget

    async def load(self) -> None:
        force, self._force = self._force, False
        self._scale = self._card_scale()
        self._shelves = None
        self._render_generation += 1  # a filter's render still going stops
        self._clear_body()
        LoadingBar(self._body).pack(fill="x")
        try:
            shelves = await self._fetch_shelves(force=force)
        except asyncio.CancelledError:
            self._force = self._force or force  # a reload cut short still owes fresh lists
            raise
        except TMDBNotConfigured:
            self._show_problem(
                t("TMDB API key needed"),
                t("Add your TMDB API key in ⚙ Settings to see trending, airing and upcoming titles."),
                (t("Open Settings"), self._on_open_settings),
            )
            return
        except TMDBAuthError:
            self._show_problem(
                t("TMDB rejected the API key"),
                t("Check the key in ⚙ Settings."),
                (t("Open Settings"), self._on_open_settings),
            )
            return
        except TMDBError as exc:
            self._show_problem(t("Couldn't reach TMDB"), str(exc), (t("Retry"), self.reload))
            return
        if not self.winfo_exists():
            return
        self._shelves = shelves
        self._render_generation += 1
        await self._render(self._render_generation)

    async def _render(self, generation: int) -> None:
        """Show the loaded shelves through the filter; stops once `generation` is outdated (a
        newer render or load started)."""
        loaded = self._shelves
        if loaded is None or generation != self._render_generation or not self.winfo_exists():
            return
        shelves: Sequence[Shelf[T]] = loaded
        if self._hide_owned:
            shelves = filter_shelves(shelves, lambda item: not self._is_owned(item))
        genre = self._genre
        titles = (self._genres(item) for shelf in shelves for item in shelf.items)
        self._show_filter_bar(chip_genres(titles, genre))
        if genre is not None:
            shelves = filter_shelves(shelves, lambda item: genre in self._genres(item))
        self._clear_body()
        if not has_items(shelves) and has_items(loaded):
            self._show_empty_line(empty_text(genre, self._hide_owned, t(self._all_owned_text)))
        for shelf in shelves:
            if shelf.failed or shelf.items:
                self._render_shelf(shelf)
                # Lets Tk draw the shelf and handle input before the next one is built.
                await asyncio.sleep(0.001)
                if generation != self._render_generation or not self.winfo_exists():
                    return

    def _show_filter_bar(self, genres: Sequence[Genre]) -> None:
        self.filter_bar.show_genres(genres, self._genre)
        if not self.filter_bar.winfo_manager():
            self.filter_bar.grid(row=1, column=0, sticky="ew", padx=25, pady=(5, 0))

    def _show_problem(self, title: str, body: str, action: tuple[str, Callable[[], None]]) -> None:
        if not self.winfo_exists():
            return
        self.filter_bar.grid_remove()
        self._clear_body()
        MessagePanel(self._body, title, body, action=action, height=_MESSAGE_HEIGHT).pack(
            fill="x", expand=True
        )

    def _show_empty_line(self, text: str) -> None:
        ctk.CTkLabel(
            self._body,
            text=text,
            font=theme.font(14),
            text_color=theme.TEXT_MUTED,
            anchor="w",
            justify="left",
        ).pack(anchor="w", padx=10, pady=(10, 25))

    def _render_shelf(self, shelf: Shelf[T]) -> None:
        section = ctk.CTkFrame(self._body, fg_color="transparent")
        section.pack(fill="x", pady=(0, 25))
        ctk.CTkLabel(
            section,
            text=f"{shelf.icon} {shelf.title}",
            font=theme.font(18, bold=True),
            text_color=shelf.color,
            anchor="w",
        ).pack(anchor="w", padx=10, pady=(0, 10))
        if shelf.failed:
            ctk.CTkLabel(
                section,
                text=t("Couldn't load {title} right now.", title=shelf.title),
                font=theme.font(13),
                text_color=theme.TEXT_MUTED,
                anchor="w",
            ).pack(anchor="w", padx=10)
            return
        item_width = grid_item_width(POSTER_CARD_WIDTH, self._scale)
        grid = FlexGrid(section, item_width=item_width, pad_x=8, pad_y=10)
        grid.pack(fill="x")
        for item in shelf.items:
            self._card(grid, item)
        grid.relayout()

    # -- the filter ----------------------------------------------------------------------------

    def _pick_genre(self, genre: Genre | None) -> None:
        if genre != self._genre:
            self._genre = genre
            self._refilter()

    def _set_hide_owned(self, hide: bool) -> None:
        if hide == self._hide_owned:
            return
        self._hide_owned = hide
        self._refilter()
        self._start(self._save_hide_owned(hide), f"{type(self).__name__}.hide_owned")

    async def _save_hide_owned(self, hide: bool) -> None:
        # One of MainWindow's off-page settings: saving it reloads no page.
        await self._settings.update(**{self._hide_owned_setting: hide})

    def _refilter(self) -> None:
        """Show the loaded shelves again through the changed filter. While a load is under way
        its own render picks the filter up."""
        if self._shelves is None:
            return
        self._render_generation += 1
        self._start(self._render(self._render_generation), f"{type(self).__name__}.filter")
        self._body._parent_canvas.yview_moveto(0)  # CTk has no scroll API

    # -- actions -------------------------------------------------------------------------------

    def _start(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task = spawn(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _report(self, text: str, *, clear_after_ms: int | None = None) -> None:
        try:
            self._status(text, clear_after_ms=clear_after_ms)
        except tk.TclError:
            pass  # window already destroyed

    def destroy(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        super().destroy()
