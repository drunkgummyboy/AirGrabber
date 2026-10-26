"""Global search: live suggestions under the header search box and a results window,
both covering TV shows (TVMaze) and movies (TMDB)."""

from __future__ import annotations

import asyncio
import logging
import tkinter as tk
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import customtkinter as ctk

from app.i18n import t
from app.models import Movie, Show, ShowSearchResult
from app.services.tmdb import TMDBAuthError, TMDBError, TMDBNotConfigured
from app.services.tvmaze import TVMazeError
from app.ui import theme
from app.ui.branding import set_window_icon
from app.ui.card_scale import FULL, CardScale, grid_item_width
from app.ui.cards import SearchShowCard
from app.ui.download_control import DownloadActions
from app.ui.media_cards import MovieCard
from app.ui.poster_card import POSTER_CARD_WIDTH, TrackState
from app.ui.tasks import spawn
from app.ui.text_fit import SLACK, elide_to_width
from app.ui.widgets import FlexGrid, LoadingBar

if TYPE_CHECKING:
    from app.services import ImageCache, LibraryService, WatchlistService
    from app.services.discovery import DiscoveryService
    from app.ui.views.base import StatusReporter

log = logging.getLogger(__name__)

SUGGESTION_LIMIT = 3  # per media type: at most 3 shows + 3 movies in the dropdown
RESULTS_LIMIT = 12
_MIN_QUERY = 3
_DEBOUNCE_MS = 300
_FOCUS_CHECK_MS = 350
_ROW_HEIGHT = 35
_ROW_TEXT_INSET = 24  # the row button's own text padding plus the popup border
_IGNORED_KEYS = frozenset({"Return", "KP_Enter", "Escape", "Tab", "Up", "Down", "Left", "Right"})


def show_label(show: Show) -> str:
    return f"📺 {show.name} ({show.year})" if show.year else f"📺 {show.name}"


def movie_label(movie: Movie) -> str:
    return f"🎬 {movie.title} ({movie.year})" if movie.year else f"🎬 {movie.title}"


def suggestion_rows(
    shows: list[ShowSearchResult], movies: list[Movie]
) -> list[tuple[str, str]]:
    """The dropdown's rows as (text, query to open): shows first, then movies."""
    rows = [
        (show_label(result.show) + ("  ✓" if result.is_tracked else ""), result.show.name)
        for result in shows
    ]
    rows.extend((movie_label(movie), movie.title) for movie in movies)
    return rows


class SearchSuggestions:
    """A dropdown of the best TVMaze and TMDB matches for what is typed in `entry` (debounced,
    latest wins). Each source fails silently on its own: the other's rows still show.

    The popup is one borderless top-level window, created once and shown/withdrawn: CTkToplevel
    schedules Windows titlebar fixes right after creation that would fail on a destroyed window.
    """

    def __init__(
        self,
        entry: ctk.CTkEntry,
        *,
        root: ctk.CTk,
        library: LibraryService,
        discovery: DiscoveryService,
        on_open: Callable[[str], None],
    ) -> None:
        self._entry = entry
        self._root = root
        self._library = library
        self._discovery = discovery
        self._on_open = on_open
        self._debounce_job: str | None = None
        self._focus_job: str | None = None
        self._fetch: asyncio.Task[None] | None = None
        self._popup: ctk.CTkToplevel | None = None
        self._visible = False
        self._root_geometry: tuple[int, int, int, int] | None = None
        self._closed = False

        entry.bind("<KeyRelease>", self._on_key)
        entry.bind("<Escape>", lambda _event: self.hide())
        entry.bind("<FocusOut>", self._on_focus_out)
        self._configure_id = root.bind("<Configure>", self._on_root_configure, add="+")
        self._unmap_id = root.bind("<Unmap>", self._on_root_unmap, add="+")

    # -- events ------------------------------------------------------------------------------

    def _on_key(self, event: tk.Event[Any]) -> None:
        if self._closed or event.keysym in _IGNORED_KEYS:
            return
        self._cancel_job("_debounce_job")
        self._debounce_job = self._root.after(_DEBOUNCE_MS, self._start_fetch)

    def _on_focus_out(self, _event: tk.Event[Any]) -> None:
        if self._closed:
            return
        self._cancel_job("_focus_job")
        self._focus_job = self._root.after(_FOCUS_CHECK_MS, self._hide_unless_focus_in_popup)

    def _hide_unless_focus_in_popup(self) -> None:
        self._focus_job = None
        try:
            focused = self._root.focus_get()
        except (KeyError, tk.TclError):  # focus_get fails for some Tk-internal widgets
            focused = None
        popup = self._popup
        if focused is not None and popup is not None and str(focused).startswith(str(popup)):
            return
        if focused is not None and str(focused).startswith(str(self._entry)):
            return  # focus came back to the entry in the meantime
        self.hide()

    def _on_root_configure(self, event: tk.Event[Any]) -> None:
        # Children of the root carry its bind tag too; only the window itself matters here.
        if event.widget is not self._root:
            return
        geometry = (event.x, event.y, event.width, event.height)
        if geometry != self._root_geometry:
            self._root_geometry = geometry
            self._withdraw()

    def _on_root_unmap(self, event: tk.Event[Any]) -> None:
        if event.widget is self._root:
            self._withdraw()

    # -- fetching ----------------------------------------------------------------------------

    def _start_fetch(self) -> None:
        self._debounce_job = None
        query = self._entry.get().strip()
        self._cancel_fetch()
        if len(query) < _MIN_QUERY:
            self._withdraw()
            return
        self._fetch = spawn(self._suggest(query), name="SearchSuggestions.suggest")

    async def _suggest(self, query: str) -> None:
        shows, movies = await asyncio.gather(
            self._suggest_shows(query), self._suggest_movies(query)
        )
        if self._closed or not self._entry.winfo_exists():
            return
        if self._entry.get().strip() != query:
            return  # the user kept typing; a newer fetch owns the dropdown
        self._render(suggestion_rows(shows, movies))

    async def _suggest_shows(self, query: str) -> list[ShowSearchResult]:
        try:
            return await self._library.search(query, limit=SUGGESTION_LIMIT)
        except TVMazeError as exc:
            log.debug("Show suggestions for %r failed: %s", query, exc)
            return []

    async def _suggest_movies(self, query: str) -> list[Movie]:
        try:
            return await self._discovery.search_movies(query, limit=SUGGESTION_LIMIT)
        except TMDBError as exc:
            log.debug("Movie suggestions for %r failed: %s", query, exc)
            return []

    # -- popup -------------------------------------------------------------------------------

    def _ensure_popup(self) -> ctk.CTkToplevel:
        if self._popup is None or not self._popup.winfo_exists():
            popup = ctk.CTkToplevel(self._root, fg_color=theme.SURFACE)
            popup.withdraw()
            popup.overrideredirect(True)
            popup.attributes("-topmost", True)
            self._popup = popup
        return self._popup

    def _render(self, rows: list[tuple[str, str]]) -> None:
        if not rows:
            self._withdraw()
            return
        popup = self._ensure_popup()
        for child in popup.winfo_children():
            child.destroy()

        container = ctk.CTkFrame(
            popup,
            fg_color=theme.SURFACE,
            border_width=1,
            border_color=theme.EDGE,
            corner_radius=4,
        )
        container.pack(fill="both", expand=True)

        # winfo_* report physical pixels; CTk scales geometry() width/height (not x/y).
        entry = self._entry
        scaling = ctk.ScalingTracker.get_window_scaling(popup)
        x = entry.winfo_rootx()
        y = entry.winfo_rooty() + entry.winfo_height() + 2
        width = round(entry.winfo_width() / scaling)
        height = len(rows) * _ROW_HEIGHT + 2

        font = theme.font(13)
        text_room = width - _ROW_TEXT_INSET - SLACK
        last = len(rows) - 1
        for idx, (text, target) in enumerate(rows):
            ctk.CTkButton(
                container,
                text=elide_to_width(text, text_room, font.measure),
                font=font,
                height=_ROW_HEIGHT,
                fg_color="transparent",
                hover_color=theme.HOVER,
                text_color=theme.BUTTON_TEXT,
                anchor="w",
                corner_radius=0,
                command=lambda name=target: self._choose(name),
            ).pack(fill="x", padx=1, pady=(1 if idx == 0 else 0, 1 if idx == last else 0))

        popup.geometry(f"{width}x{height}+{x}+{y}")
        popup.deiconify()
        popup.lift()
        self._visible = True

    def _choose(self, name: str) -> None:
        self.hide()
        self._entry.delete(0, "end")
        self._on_open(name)

    def _withdraw(self) -> None:
        if self._visible and self._popup is not None and self._popup.winfo_exists():
            self._popup.withdraw()
        self._visible = False

    # -- public ------------------------------------------------------------------------------

    @property
    def visible(self) -> bool:
        return self._visible

    def hide(self) -> None:
        """Close the dropdown and drop any pending or in-flight lookup."""
        self._cancel_job("_debounce_job")
        self._cancel_job("_focus_job")
        self._cancel_fetch()
        self._withdraw()

    def destroy(self) -> None:
        if self._closed:
            return
        self.hide()
        self._closed = True
        try:
            self._root.unbind("<Configure>", self._configure_id)
            self._root.unbind("<Unmap>", self._unmap_id)
        except tk.TclError:
            pass
        if self._popup is not None and self._popup.winfo_exists():
            self._popup.destroy()
        self._popup = None

    # -- helpers -----------------------------------------------------------------------------

    def _cancel_job(self, attr: str) -> None:
        job: str | None = getattr(self, attr)
        if job is not None:
            try:
                self._root.after_cancel(job)
            except tk.TclError:
                pass
            setattr(self, attr, None)

    def _cancel_fetch(self) -> None:
        if self._fetch is not None and not self._fetch.done():
            self._fetch.cancel()
        self._fetch = None


class SearchResultsWindow(ctk.CTkToplevel):
    """Results for `query` (not modal): TVMaze shows with a "+ Track" button (a click on a
    show's cover calls `on_open_show`), then TMDB movies.

    Both searches run at once; each section renders as soon as its own search is done. The cards
    are drawn at `card_scale`, the card size when the window opened: a window already open
    keeps its size until the next search opens a new one.
    """

    WIDTH = 1150
    HEIGHT = 800

    def __init__(
        self,
        master: ctk.CTk,
        *,
        library: LibraryService,
        images: ImageCache,
        discovery: DiscoveryService,
        status: StatusReporter,
        query: str,
        movie_actions: DownloadActions[Movie],
        watchlist: WatchlistService,
        on_open_settings: Callable[[], None],
        on_open_show: Callable[[Show], None] | None = None,
        card_scale: CardScale = FULL,
    ) -> None:
        """`on_open_show`: what a click on a show card's cover does (the show details)."""
        super().__init__(master, fg_color=theme.BG)
        self._scale = card_scale
        self._library = library
        self._discovery = discovery
        self._images = images
        self._status = status
        self._query = query
        self._movie_actions = movie_actions  # the movie cards' Download and ⋯ buttons
        self._watchlist = watchlist  # the movie cards' watchlist buttons
        self._on_open_settings = on_open_settings
        self._on_open_show = on_open_show
        self._cards: dict[int, SearchShowCard] = {}

        self.title(t("Search Results: {query}", query=query))
        set_window_icon(self)
        self.transient(master)
        # CTkToplevel only applies its dark Windows title bar reliably from resizable().
        self.resizable(True, True)
        self._place_over(master)

        self._body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._body.pack(fill="both", expand=True, padx=20, pady=20)
        # One section per media type, each with its own loading bar until its search is done.
        self._shows_section = self._section(t("📺 TV Shows"))
        self._shows_loading: LoadingBar | None = self._loading_bar(self._shows_section)
        self._movies_section = self._section(t("🎬 Movies"))
        self._movies_loading: LoadingBar | None = self._loading_bar(self._movies_section)

        self._searches: list[asyncio.Task[None]] = [
            spawn(self._load_shows(), name="SearchResultsWindow.load_shows"),
            spawn(self._load_movies(), name="SearchResultsWindow.load_movies"),
        ]
        self.bind("<Escape>", lambda _event: self.destroy())
        # CTkToplevel can open behind its parent on Windows; raise it once it is mapped.
        self.after(150, self._activate)

    # -- layout ------------------------------------------------------------------------------

    def _place_over(self, master: ctk.CTk) -> None:
        """Centre over `master`, clamped to the screen (see MainWindow._place for the DPI math)."""
        scaling = ctk.ScalingTracker.get_window_scaling(self)
        screen_w, screen_h = self.wm_maxsize()
        width = min(self.WIDTH, int(screen_w / scaling) - 40)
        height = min(self.HEIGHT, int(screen_h / scaling) - 40)
        width_px, height_px = round(width * scaling), round(height * scaling)
        x = master.winfo_rootx() + (master.winfo_width() - width_px) // 2
        y = master.winfo_rooty() + (master.winfo_height() - height_px) // 2
        x = min(max(0, x), max(0, screen_w - width_px))
        y = min(max(0, y), max(0, screen_h - height_px))
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _section(self, title: str) -> ctk.CTkFrame:
        section = ctk.CTkFrame(self._body, fg_color="transparent")
        section.pack(fill="x", pady=(0, 20))
        ctk.CTkLabel(
            section,
            text=title,
            font=theme.font(20, bold=True),
            text_color=theme.TEXT_MUTED,
            anchor="w",
        ).pack(fill="x")
        return section

    @staticmethod
    def _loading_bar(section: ctk.CTkFrame) -> LoadingBar:
        bar = LoadingBar(section)
        bar.pack(fill="x", pady=(10, 0))
        return bar

    def _activate(self) -> None:
        if self.winfo_exists():
            self.lift()
            self.focus_force()

    # -- shows -------------------------------------------------------------------------------

    async def _load_shows(self) -> None:
        try:
            results = await self._library.search(self._query, limit=RESULTS_LIMIT)
        except TVMazeError as exc:
            if self.winfo_exists():
                self._clear_shows_loading()
                self._message(
                    self._shows_section, t("Couldn't reach TVMaze: {error}", error=exc), theme.DANGER
                )
            return
        if not self.winfo_exists():
            return
        self._clear_shows_loading()
        if not results:
            self._message(self._shows_section, t("No TV shows found."), theme.TEXT_QUIET)
            return
        item_width = grid_item_width(POSTER_CARD_WIDTH, self._scale)
        grid = FlexGrid(self._shows_section, item_width=item_width, pad_x=8, pad_y=10)
        grid.pack(fill="x")
        for result in results:
            self._cards[result.show.id] = SearchShowCard(
                grid,
                result.show,
                images=self._images,
                is_tracked=result.is_tracked,
                on_track=self._track,
                on_open_show=self._on_open_show,
                scale=self._scale,
            )
        grid.relayout()

    def _clear_shows_loading(self) -> None:
        if self._shows_loading is not None:
            self._shows_loading.destroy()
            self._shows_loading = None

    def _message(
        self,
        section: ctk.CTkFrame,
        text: str,
        color: theme.Color,
        *,
        settings_button: bool = False,
    ) -> None:
        ctk.CTkLabel(section, text=text, text_color=color, anchor="w").pack(fill="x", pady=(10, 0))
        if settings_button:
            ctk.CTkButton(
                section,
                text=t("Open Settings"),
                width=120,
                height=30,
                fg_color=theme.ACCENT,
                hover_color=theme.ACCENT_HOVER,
                command=self._on_open_settings,
            ).pack(anchor="w", pady=(8, 0))

    # -- movies ------------------------------------------------------------------------------

    async def _load_movies(self) -> None:
        try:
            movies = await self._discovery.search_movies(self._query, limit=RESULTS_LIMIT)
        except TMDBError as exc:
            if self.winfo_exists():
                self._clear_movies_loading()
                self._movie_error(exc)
            return
        if not self.winfo_exists():
            return
        self._clear_movies_loading()
        if not movies:
            self._message(self._movies_section, t("No movies found."), theme.TEXT_QUIET)
            return
        item_width = grid_item_width(POSTER_CARD_WIDTH, self._scale)
        grid = FlexGrid(self._movies_section, item_width=item_width, pad_x=8, pad_y=10)
        grid.pack(fill="x")
        for movie in movies:
            MovieCard(
                grid,
                movie,
                images=self._images,
                details=self._discovery.movie_details,
                actions=self._movie_actions,
                watchlist=self._watchlist,
                scale=self._scale,
            )
        grid.relayout()

    def _movie_error(self, exc: TMDBError) -> None:
        section = self._movies_section
        if isinstance(exc, TMDBNotConfigured):
            text = t("Add a TMDB API key in ⚙ Settings to search movies.")
            self._message(section, text, theme.TEXT_QUIET, settings_button=True)
        elif isinstance(exc, TMDBAuthError):
            text = t("TMDB rejected the API key — check it in ⚙ Settings.")
            self._message(section, text, theme.TEXT_QUIET, settings_button=True)
        else:
            self._message(section, t("Couldn't reach TMDB: {error}", error=exc), theme.DANGER)

    def _clear_movies_loading(self) -> None:
        if self._movies_loading is not None:
            self._movies_loading.destroy()
            self._movies_loading = None

    def _track(self, show: Show) -> None:
        card = self._cards.get(show.id)
        if card is not None:
            card.set_track_state("busy")
        spawn(self._track_show(show), name="SearchResultsWindow.track")

    async def _track_show(self, show: Show) -> None:
        try:
            await self._library.track(show.id)
        except TVMazeError as exc:
            self._set_card_state(show, "idle")
            self._report(t("Couldn't track {name}: {error}", name=show.name, error=exc), 6000)
            return
        self._set_card_state(show, "tracked")
        self._report(t("Now tracking {name}", name=show.name), 4000)

    def _set_card_state(self, show: Show, state: TrackState) -> None:
        card = self._cards.get(show.id)
        if card is None or not card.winfo_exists():
            return
        card.set_track_state(state)

    def _report(self, text: str, clear_after_ms: int) -> None:
        # The status bar belongs to the main window, which may outlive this one (or not).
        try:
            self._status(text, clear_after_ms=clear_after_ms)
        except tk.TclError:
            pass

    def destroy(self) -> None:
        for task in self._searches:
            if not task.done():
                task.cancel()
        self._searches = []
        super().destroy()
