"""Shared base of the TMDB dashboard pages (Discover, Movies): titled shelves of cards under
the filter bar (genre chips and "Hide what I already have", app/ui/shelf_filter.py), and a
page's See more under its shelves (opt-in, `ShelvesView._see_more`; the Movies page's)."""

from __future__ import annotations

import asyncio
import dataclasses
import tkinter as tk
from abc import abstractmethod
from collections.abc import Callable, Coroutine, Iterable, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar, Generic, TypeVar

import customtkinter as ctk

from app.i18n import N_, t
from app.services import ImageCache, SettingsService
from app.services.tmdb import TMDBAuthError, TMDBError, TMDBNotConfigured
from app.ui import theme
from app.ui.card_scale import FULL, CardScale, grid_item_width
from app.ui.poster_card import POSTER_CARD_WIDTH
from app.ui.scrollable import ScrollableFrame
from app.ui.shelf_filter import HIDE_OWNED_TEXT, FilterBar, Genre, chip_genres, empty_text
from app.ui.tasks import spawn
from app.ui.views.base import BaseView, StatusReporter
from app.ui.widgets import FlexGrid, LoadingBar, MessagePanel, destroy_tree

T = TypeVar("T")

_MESSAGE_HEIGHT = 300  # message panels sit in a scroll frame, which gives them no height
# See more under a shelf (the Movies page's): the next page of its TMDB list, added below.
SEE_MORE_TEXT = N_("See more")
SEE_MORE_LOADING = N_("Loading…")
SEE_MORE_FAILED = N_("Couldn't load more of {title} right now.")
SEE_MORE_WIDTH = 200  # as the Library's Show more


@dataclass(frozen=True, slots=True)
class Shelf(Generic[T]):
    """One section of a dashboard page."""

    icon: str
    title: str
    color: theme.Color  # of the section title
    items: Sequence[T]
    failed: bool = False  # TMDB couldn't load this shelf (the others did)
    # See more (a page with `_see_more`): the shelf's name for `_fetch_more` ("" = none), and
    # whether its TMDB list goes on after `items`.
    key: str = ""
    more: bool = False


def _nothing() -> None:
    pass


@dataclass(frozen=True, slots=True)
class MoreItems(Generic[T]):
    """A shelf's next page for See more (`ShelvesView._fetch_more`): its items, whether the
    list goes on after them, and what the page keeps of it (run only when the page takes it:
    a page that comes after a load is dropped)."""

    items: Sequence[T]
    more: bool
    accept: Callable[[], None] = _nothing


@dataclass(frozen=True, slots=True)
class _Added(Generic[T]):
    """What See more added to a shelf: its items (after the fetched ones, each once), the last
    page it read, and whether the list goes on."""

    items: tuple[T, ...]
    page: int
    more: bool


@dataclass(slots=True)
class _ShelfParts:
    """A shelf on screen that See more adds to: its grid of cards and its button."""

    section: ctk.CTkFrame
    grid: FlexGrid
    button: ctk.CTkButton | None = None


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


def same_items(
    old: Sequence[Shelf[T]], new: Sequence[Shelf[T]], key: Callable[[T], object]
) -> bool:
    """Whether `new` holds the same shelves as `old` with the same items in the same order
    (compared by `key`, which leaves out what may change in place)."""

    def outline(shelves: Sequence[Shelf[T]]) -> list[tuple[str, bool, list[object]]]:
        return [
            (shelf.title, shelf.failed, [key(item) for item in shelf.items]) for shelf in shelves
        ]

    return outline(old) == outline(new)


class ShelvesView(BaseView, Generic[T]):
    """A scrolling column of shelves from one TMDB dashboard, under the filter bar.

    Subclasses fetch the dashboard (`_fetch_shelves`), build one card per item (`_card`, at
    `self._scale`: the card size `card_scale` gave when the load started) and tell the filter
    an item's genres (`_genres`) and whether the user already has it (`_is_owned`).
    `reload()` refetches from TMDB instead of using the dashboard cache.

    A quiet reload (`refresh_quietly`: the library changed under the page on screen) fetches
    the shelves again without clearing the page. When they hold the same items and only
    whether the user has some changed, the page keeps its cards and scroll position and the
    changed cards show it in place (`_owned_changed`); otherwise it loads as usual.

    The filter only re-renders the shelves the last load fetched. The genre chip is kept for
    the session (the page lives as long as the window); the switch is the Settings field
    `_hide_owned_setting`, offered while `_hide_owned_offered` (Movies: only with a library,
    Plex or the folder library, the only thing that counts as had); without one nothing is
    hidden.

    See more (opt-in, `_see_more`: the Movies page): under each shelf whose `more` is set, a
    button that fetches the shelf's next page (`_fetch_more`: 2, 3, …) and adds its new items
    (by `_identity`, each once) to the shelf's grid as it is, through the filter as the others;
    "Loading…" meanwhile, the status bar's line when it fails, gone at the list's end or when a
    page brings nothing new. A load (Refresh, F5, a card size) starts every shelf at its first
    page again; a quiet reload keeps what was added.
    """

    # Offer See more under the shelves `_fetch_shelves` gives a `key` and `more`.
    _see_more: ClassVar[bool] = False

    _body_pady: tuple[int, int] = (5, 5)
    # The page's "Hide what I already have" setting, its switch's text and hover tip, and the
    # page's line when that switch alone left every shelf empty (in English: shown through t()).
    _hide_owned_setting: ClassVar[str]
    _hide_owned_text: ClassVar[str] = HIDE_OWNED_TEXT
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
        self._shelves: list[Shelf[T]] | None = None  # the last load's, unfiltered, with See more's
        self._fetched: list[Shelf[T]] | None = None  # the same, as `_fetch_shelves` gave them
        self._render_generation = 0  # bumped to stop a render in progress
        self._rendered = 0  # the last render that got to its end
        # See more: what it added per shelf key, the keys loading now, the shelves on screen
        # (the last render's), and a generation bumped by every load (a late page is dropped).
        self._added: dict[str, _Added[T]] = {}
        self._loading_more: set[str] = set()
        self._parts: dict[str, _ShelfParts] = {}
        self._more_generation = 0

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        self.filter_bar = FilterBar(
            self,
            hide_owned=self._hide_owned,
            hide_tip=self._hide_owned_tip_text(),
            on_genre=self._pick_genre,
            on_hide_owned=self._set_hide_owned,
            hide_text=self._hide_owned_text,
            offered=self._hide_owned_offered(),
            on_service=self._pick_service,
        )
        # Gridded in row 1 once shelves are loaded (`_show_filter_bar`).
        self._body = ScrollableFrame(self, fg_color="transparent")
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

    def _hide_owned_offered(self) -> bool:
        """Whether the page offers its hide switch now (as the last load found it)."""
        return True

    def _hide_owned_tip_text(self) -> str:
        """The hide switch's tip, translated (read whenever the filter bar is shown)."""
        return t(self._hide_owned_tip)

    def _keep(self, item: T) -> bool:
        """Whether a filter of the page's own (Discover's service) leaves `item` on it."""
        return True

    def _empty_line(self, genre: Genre | None) -> str:
        """The page's one line when the filter left every shelf empty."""
        return empty_text(genre, self.hiding, t(self._all_owned_text))

    def _pick_service(self, service: str | None) -> None:
        """A pick in the filter bar's services menu (Discover's)."""

    @property
    def hiding(self) -> bool:
        """The switch is offered and on: what the user has is hidden."""
        return self._hide_owned and self._hide_owned_offered()

    def _item_key(self, item: T) -> object:
        """`item` without what a quiet reload may change in place (`same_items`)."""
        return item

    def _owned_changed(self, items: Sequence[T]) -> bool:
        """`items` (as just fetched) changed whether the user has them: show it on their cards
        in place. False when the page can't, and the shelves are rendered again."""
        return False

    async def _fetch_more(self, shelf: Shelf[T], page: int) -> MoreItems[T]:
        """See more: page `page` (2, 3, …) of `shelf` (as `_fetch_shelves` gave it). TMDB
        errors propagate."""
        raise NotImplementedError

    def _forget_added(self) -> None:
        """A load starts every shelf at its first page again: forget what the page kept of
        See more's pages (`MoreItems.accept`)."""

    def _identity(self, item: T) -> object:
        """What makes `item` the same title as another on a shelf (See more adds it once)."""
        return self._item_key(item)

    def _added_items(self) -> list[T]:
        """Every item See more added since the last load."""
        return [item for added in self._added.values() for item in added.items]

    def reload(self) -> None:
        """Reload with fresh TMDB lists."""
        self._force = True
        self.refresh()

    # -- loading -------------------------------------------------------------------------------

    def _clear_body(self) -> None:
        for child in self._body.winfo_children():
            destroy_tree(child)  # three full shelves take ~600 ms to destroy() widget by widget

    async def load(self) -> None:
        quiet = self.quiet_load  # before the first await (BaseView)
        if (
            quiet
            and not self._force
            and self._shelves is not None
            and self._card_scale() == self._scale
            and await self._update_quietly()
        ):
            return
        force, self._force = self._force, False
        self._scale = self._card_scale()
        self._shelves = None
        self._fetched = None
        self._added.clear()  # every shelf starts at its first page again
        self._loading_more.clear()
        self._more_generation += 1
        self._forget_added()
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
        self._fetched = shelves
        self._shelves = list(shelves)
        self._render_generation += 1
        await self._render(self._render_generation)

    async def _update_quietly(self) -> bool:
        """The quiet reload (see the class docstring). False: the page needs the usual load."""
        loaded = self._shelves
        if loaded is None:
            return False
        owned = [[self._is_owned(item) for item in shelf.items] for shelf in loaded]
        offered = self._hide_owned_offered()
        try:
            fetched = await self._fetch_shelves(force=False)
        except TMDBError:
            return False  # the usual load shows the problem
        if not self.winfo_exists():
            return True
        # What See more added stays, also a page that came meanwhile (after `loaded`'s items:
        # those are the shelves' first ones still).
        shelves = self._with_added(fetched)
        current = self._shelves
        if current is None or not same_items(current, shelves, self._item_key):
            return False
        if len(current) != len(loaded):
            return False
        self._fetched = fetched
        if self._hide_owned_offered() != offered:  # the switch comes or goes (Plex)
            self._shelves = shelves
            self._render_generation += 1
            await self._render(self._render_generation)
            return True
        changed = [
            item
            for shelf, had in zip(shelves, owned, strict=True)
            for item, was in zip(shelf.items, had)  # a page added meanwhile: as it came
            if self._is_owned(item) != was
        ]
        self._shelves = shelves
        if not changed:
            return True
        if self._owned_changed(changed):
            shown = self._owner_filtered(shelves)
            self._show_filter_bar(chip_genres(self._titles(shown), self._genre))
        else:
            self._render_generation += 1
            await self._render(self._render_generation)
        return True

    def _owner_filtered(self, shelves: Sequence[Shelf[T]]) -> Sequence[Shelf[T]]:
        """`shelves` without what the user has, while "Hide what I already have" is on."""
        if self.hiding:
            return filter_shelves(shelves, lambda item: not self._is_owned(item))
        return shelves

    def _titles(self, shelves: Sequence[Shelf[T]]) -> Iterable[frozenset[Genre]]:
        return (self._genres(item) for shelf in shelves for item in shelf.items)

    def _with_added(self, fetched: Sequence[Shelf[T]]) -> list[Shelf[T]]:
        """`fetched` with what See more added to each shelf after its items (each once)."""
        shelves: list[Shelf[T]] = []
        for shelf in fetched:
            added = self._added.get(shelf.key) if shelf.key and not shelf.failed else None
            if added is None:
                shelves.append(shelf)
                continue
            known = {self._identity(item) for item in shelf.items}
            extra = tuple(item for item in added.items if self._identity(item) not in known)
            shelves.append(
                dataclasses.replace(shelf, items=(*shelf.items, *extra), more=added.more)
            )
        return shelves

    def _passes(self, item: T) -> bool:
        """Whether the filter as it is now shows `item` (as `_render` filters the shelves)."""
        if self.hiding and self._is_owned(item):
            return False
        genre = self._genre
        return self._keep(item) and (genre is None or genre in self._genres(item))

    async def _render(self, generation: int) -> None:
        """Show the loaded shelves through the filter; stops once `generation` is outdated (a
        newer render or load started)."""
        loaded = self._shelves
        if loaded is None or generation != self._render_generation or not self.winfo_exists():
            return
        shelves = filter_shelves(self._owner_filtered(loaded), self._keep)
        genre = self._genre
        self._show_filter_bar(chip_genres(self._titles(shelves), genre))
        if genre is not None:
            shelves = filter_shelves(shelves, lambda item: genre in self._genres(item))
        self._clear_body()
        self._parts = {}
        if not has_items(shelves) and has_items(loaded):
            self._show_empty_line(self._empty_line(genre))
        for shelf in shelves:
            if shelf.failed or shelf.items:
                self._render_shelf(shelf)
                # Lets Tk draw the shelf and handle input before the next one is built.
                await asyncio.sleep(0.001)
                if generation != self._render_generation or not self.winfo_exists():
                    return
        self._rendered = generation

    def _show_filter_bar(self, genres: Sequence[Genre]) -> None:
        self.filter_bar.set_hide_tip(self._hide_owned_tip_text())
        self.filter_bar.offer_hide(self._hide_owned_offered())
        self.filter_bar.show_genres(genres, self._genre)
        if not self.filter_bar.winfo_manager():
            self.filter_bar.grid(row=1, column=0, sticky="ew", padx=25, pady=(5, 0))

    def _show_problem(self, title: str, body: str, action: tuple[str, Callable[[], None]]) -> None:
        if not self.winfo_exists():
            return
        self.filter_bar.grid_remove()
        self._clear_body()
        self._parts = {}
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
        # Whole rows only, until See more was asked for: then every card shows.
        whole_rows = not (shelf.key and shelf.key in self._added)
        grid = FlexGrid(section, item_width=item_width, pad_x=8, pad_y=10, whole_rows=whole_rows)
        grid.pack(fill="x")
        for item in shelf.items:
            self._card(grid, item)
        grid.relayout()
        if self._see_more and shelf.key:
            self._parts[shelf.key] = _ShelfParts(section, grid)
            self._show_more_button(shelf.key)

    # -- See more ------------------------------------------------------------------------------

    def _shelf(self, key: str) -> Shelf[T] | None:
        """The loaded shelf `key` (with what See more added)."""
        return next((shelf for shelf in self._shelves or () if shelf.key == key), None)

    def _show_more_button(self, key: str) -> None:
        """Shelf `key`'s See more as it stands: "Loading…" (greyed) while its page loads, gone
        when its list has nothing more."""
        parts = self._parts.get(key)
        shelf = self._shelf(key)
        if parts is None or shelf is None or not parts.section.winfo_exists():
            return
        loading = key in self._loading_more
        if not (shelf.more or loading):
            if parts.button is not None:
                parts.button.destroy()
                parts.button = None
            return
        if parts.button is None:
            parts.button = ctk.CTkButton(
                parts.section,
                text=t(SEE_MORE_TEXT),
                width=SEE_MORE_WIDTH,
                fg_color="transparent",
                border_width=1,
                border_color=theme.EDGE,
                hover_color=theme.HOVER,
                text_color=theme.BUTTON_TEXT,
                command=lambda: self.see_more(key),
            )
            parts.button.pack(pady=(6, 0))
        parts.button.configure(
            text=t(SEE_MORE_LOADING) if loading else t(SEE_MORE_TEXT),
            state="disabled" if loading else "normal",
        )

    def see_more(self, key: str) -> None:
        """See more under shelf `key`: its next page, added below its cards."""
        shelf = self._shelf(key)
        if key in self._loading_more or shelf is None or not shelf.more:
            return
        self._loading_more.add(key)
        self._show_more_button(key)
        self._start(self._load_more(key), f"{type(self).__name__}.see_more")

    async def _load_more(self, key: str) -> None:
        generation = self._more_generation
        fetched = next((shelf for shelf in self._fetched or () if shelf.key == key), None)
        added = self._added.get(key)
        page = added.page + 1 if added is not None else 2
        if fetched is None:
            self._loading_more.discard(key)
            return
        try:
            result = await self._fetch_more(fetched, page)
        except asyncio.CancelledError:
            if generation == self._more_generation:
                self._loading_more.discard(key)
            raise
        except Exception as exc:
            if generation != self._more_generation or not self.winfo_exists():
                if isinstance(exc, TMDBError):
                    return
                raise
            self._loading_more.discard(key)
            self._show_more_button(key)  # back, to try again
            if not isinstance(exc, TMDBError):
                raise  # a bug: spawn logs and reports it
            self._report(t(SEE_MORE_FAILED, title=fetched.title), clear_after_ms=6000)
            return
        if generation != self._more_generation or not self.winfo_exists():
            return  # a load since: every shelf is at its first page again
        result.accept()
        self._loading_more.discard(key)
        shown = self._shelf(key)
        known = {self._identity(item) for item in (shown.items if shown is not None else ())}
        new: list[T] = []
        for item in result.items:
            identity = self._identity(item)
            if identity not in known:
                known.add(identity)
                new.append(item)
        before = added.items if added is not None else ()
        self._added[key] = _Added((*before, *new), page, result.more and bool(new))
        self._shelves = self._with_added(self._fetched or ())
        self._show_added(key, new)

    def _show_added(self, key: str, new: Sequence[T]) -> None:
        """Cards for `new` (just added to shelf `key`, already in the loaded shelves) that the
        filter shows, into the shelf's grid as it is; its button as it now stands. With the
        shelf not on screen (the filter left nothing of it, or a render under way hasn't
        reached it) the shelves are rendered again when that shows any of `new`."""
        parts = self._parts.get(key)
        if parts is None or not parts.grid.winfo_exists():
            rendering = self._rendered != self._render_generation
            if rendering or any(self._passes(item) for item in new):
                self._render_generation += 1
                self._start(self._render(self._render_generation), f"{type(self).__name__}.filter")
            elif new:
                self._show_chips()  # their genres are offered all the same
            return
        for item in new:
            if self._passes(item):
                self._card(parts.grid, item)
        parts.grid.whole_rows = False
        parts.grid.relayout()  # re-grids the cards already there, never makes them again
        self._show_more_button(key)
        if new:
            self._show_chips()

    def _show_chips(self) -> None:
        """The filter bar's genres for the loaded shelves (as `_render` offers them)."""
        if self._shelves is not None:
            shown = filter_shelves(self._owner_filtered(self._shelves), self._keep)
            self._show_filter_bar(chip_genres(self._titles(shown), self._genre))

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
