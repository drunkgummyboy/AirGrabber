"""The film info window: the shared info look (app/ui/title_info.py: the backdrop with "▶ Watch
trailer", the poster, title, meta line, genres, synopsis, cast) with a film's actions: ⬇ Auto
(the one-click download) and ☰ Choose… (the search window), or once downloaded ✓ Downloaded
with ↻ Again and the subtitles' CC ▾ (app/ui/row_actions.py, app/ui/subtitle_control.py); the
watchlist button and IMDb.

Opened from the film cards' cover and title (the Movies page, the watchlist shelf, search
results); one window per film (`MovieWindows` raises the open one). It shows what the card
knew at once (TMDB's list data, in English), then TMDB's details (runtime, digital release,
IMDb id) and the extras in the app's language (`DiscoveryService.movie_extras`). Without a
TMDB key it keeps the card's data (the poster still loads: TMDB's pictures need no key), has
no backdrop, trailer or cast, and a plain hint says what a key adds.
"""

from __future__ import annotations

import asyncio
import logging
import webbrowser
from collections.abc import Awaitable, Callable, Coroutine
from datetime import date
from typing import TYPE_CHECKING, Any

import customtkinter as ctk

from app.models import Movie, MovieDetails, TitleExtras
from app.services.tmdb import TMDBError, TMDBNotConfigured
from app.ui import theme
from app.ui.download_control import CORNER_RADIUS as CONTROL_RADIUS
from app.ui.download_control import FONT_SIZE as CONTROL_FONT_SIZE
from app.ui.download_control import ROW_HEIGHT, DownloadActions, movie_key
from app.ui.download_control import Base
from app.ui.media_cards import (
    format_runtime,
    imdb_search_url,
    movie_base,
    tmdb_poster_size,
    tmdb_rating,
)
from app.ui.row_actions import RowActions
from app.ui.shelf_filter import MOVIE_GENRES, Genre, genre_name, genres_of
from app.ui.subtitle_control import CCControl, SubtitleActions
from app.ui.tasks import spawn
from app.ui.title_info import (
    BUTTON_GAP,
    TitleInfoWindow,
    exists,
    join_meta,
    rating_text,
    release_text,
)
from app.ui.watchlist_button import WatchlistButton

if TYPE_CHECKING:
    from app.services import DiscoveryService, ImageCache, WatchlistService

log = logging.getLogger(__name__)

Listener = Callable[[], None]
Subscribe = Callable[[Listener], Callable[[], None]]  # returns the unsubscribe function

WATCHLIST_WIDTH = 170
IMDB_WIDTH = 90
OPEN_WAIT_S = 5.0  # a film known only by its id waits this long for TMDB's description


def movie_meta_line(
    movie: Movie,
    details: MovieDetails | None,
    extras: TitleExtras | None,
    today: date,
) -> str:
    """"2024 · 2h 28m · ★ 6.8 · Out digitally since 24 Dec": year, runtime, rating and the
    digital release (or TMDB's status while the film isn't out), each left out when unknown."""
    info = extras.info if extras is not None else None
    runtime = (details.runtime if details is not None else None) or (
        info.runtime if info is not None else None
    )
    if info is not None and info.vote_count:
        rating = tmdb_rating(info.vote_average, info.vote_count)
    else:
        rating = tmdb_rating(movie.vote_average, movie.vote_count)
    return join_meta(
        (
            str(movie.year) if movie.year else None,
            format_runtime(runtime) if runtime else None,
            rating_text(rating),
            release_text(details, info.status if info is not None else None, today),
        )
    )


def movie_genres(movie: Movie, extras: TitleExtras | None) -> tuple[str, ...]:
    """TMDB's genre names (in the app's language) once known, else the card's genre chips."""
    if extras is not None and extras.info.genres:
        return extras.info.genres
    found = genres_of(movie.genre_ids, MOVIE_GENRES)
    return tuple(genre_name(genre) for genre in Genre if genre in found)


def movie_synopsis(movie: Movie, extras: TitleExtras | None) -> str:
    """TMDB's overview in the app's language (else English) once known, else the card's."""
    if extras is not None and extras.info.overview:
        return extras.info.overview
    return movie.overview


class MovieDetailsWindow(TitleInfoWindow):
    """A film's info window (not modal). Opened through `MovieWindows`."""

    def __init__(
        self,
        master: ctk.CTk,
        movie: Movie,
        *,
        images: ImageCache,
        discovery: DiscoveryService,
        watchlist: WatchlistService,
        actions: DownloadActions[Movie],
        today: Callable[[], date] = date.today,
        downloaded: Callable[[int], Awaitable[bool]] | None = None,
        subtitle_actions: SubtitleActions | None = None,
        subtitle_mark: Callable[[int], Awaitable[str]] | None = None,
    ) -> None:
        """`downloaded(tmdb_id)`: whether the film is in the download history (✓ Downloaded
        and its subtitles); `subtitle_actions` / `subtitle_mark(tmdb_id)`: its CC ▾ menu and
        the subtitles saved for it ("CC EN NL")."""
        super().__init__(
            master,
            title=movie.title,
            images=images,
            poster=lambda width: movie.poster_url(tmdb_poster_size(width)),
        )
        self._movie = movie
        self._discovery = discovery
        self._today = today
        self._downloaded_lookup = downloaded
        self._subtitle_mark = subtitle_mark
        self._is_downloaded = False
        self._details: MovieDetails | None = None
        self._extras: TitleExtras | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._state_task: asyncio.Task[None] | None = None

        # As wide as what it shows (⬇ Auto + ☰ Choose…, ✓ Downloaded + ↻ Again, …).
        self.download = RowActions(
            self.buttons,
            item=movie,
            key=movie_key(movie.id),
            base=movie_base(movie, None, today()),
            actions=actions,
            height=ROW_HEIGHT,
            align="left",
        )
        self.download.pack(side="left")
        # The subtitles of a downloaded film (CC ▾), right of the download controls.
        self.cc_control = CCControl(
            self.buttons, key=movie_key(movie.id), actions=subtitle_actions, height=ROW_HEIGHT
        )
        self.cc_control.show("", shown=False)
        self.watch_button = WatchlistButton(
            self.buttons,
            movie=movie,
            watchlist=watchlist,
            width=WATCHLIST_WIDTH,
            height=ROW_HEIGHT,
        )
        self.watch_button.pack(side="left", padx=(BUTTON_GAP, 0))
        # As tall as the Download row beside it, outlined like its ⋯.
        ctk.CTkButton(
            self.buttons,
            text="IMDb",
            width=IMDB_WIDTH,
            height=ROW_HEIGHT,
            corner_radius=CONTROL_RADIUS,
            font=theme.font(CONTROL_FONT_SIZE, bold=True),
            fg_color="transparent",
            hover_color=theme.HOVER,
            text_color=theme.BUTTON_TEXT,
            border_width=1,
            border_color=theme.EDGE,
            command=self._open_imdb,
        ).pack(side="left", padx=(BUTTON_GAP, 0))
        self._paint()

        self.masthead.reserve_hero(discovery.has_tmdb_key)
        self._start(self._load_details(), "MovieDetailsWindow.details")
        self._start(self._load_extras(), "MovieDetailsWindow.extras")
        self.refresh_state()

    def refresh_state(self) -> None:
        """Ask again whether the film was downloaded and which subtitles it has (a grab, or
        subtitles saved)."""
        if self._downloaded_lookup is None:
            return
        if self._state_task is not None and not self._state_task.done():
            self._state_task.cancel()
        self._state_task = spawn(self._load_state(), name="MovieDetailsWindow.state")
        self._tasks.add(self._state_task)
        self._state_task.add_done_callback(self._tasks.discard)

    async def _load_state(self) -> None:
        lookup = self._downloaded_lookup
        assert lookup is not None
        downloaded = await lookup(self._movie.id)
        mark = ""
        if downloaded and self._subtitle_mark is not None:
            mark = await self._subtitle_mark(self._movie.id)
        if not self.winfo_exists():
            return
        self._is_downloaded = downloaded
        self.download.set_base(self.download_base(), downloaded=downloaded)
        self.cc_control.show(mark, shown=downloaded)
        if self.cc_control.has_content and not self.cc_control.winfo_manager():
            self.cc_control.pack(side="left", padx=(BUTTON_GAP, 0), after=self.download)
        elif not self.cc_control.has_content and self.cc_control.winfo_manager():
            self.cc_control.pack_forget()

    def download_base(self) -> Base:
        return movie_base(self._movie, self._details, self._today())

    @property
    def imdb_url(self) -> str:
        """Its IMDb page once TMDB's details name it, IMDb's search until then."""
        details = self._details
        imdb_url = details.imdb_url if details is not None else None
        return imdb_url or imdb_search_url(self._movie)

    def _open_imdb(self) -> None:
        webbrowser.open(self.imdb_url)

    def _paint(self) -> None:
        movie, extras = self._movie, self._extras
        self._set_meta(movie_meta_line(movie, self._details, extras, self._today()))
        self.set_genres(movie_genres(movie, extras))
        self._set_summary(movie_synopsis(movie, extras))

    # -- loading -----------------------------------------------------------------------------

    def _start(self, coro: Coroutine[Any, Any, None], name: str) -> None:
        task = spawn(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _load_details(self) -> None:
        """Runtime, digital release (whether ⬇ Download is offered yet) and the IMDb id."""
        try:
            details = await self._discovery.movie_details(self._movie.id)
        except TMDBError as exc:
            log.debug("Film %d: no TMDB details: %s", self._movie.id, exc)
            return
        if not self.winfo_exists():
            return
        self._details = details
        self.download.set_base(self.download_base(), downloaded=self._is_downloaded)
        self._paint()

    async def _load_extras(self) -> None:
        try:
            extras = await self._discovery.movie_extras(self._movie.id)
        except TMDBNotConfigured:
            if self.winfo_exists():
                self.masthead.set_hero(None)
                self.show_hint(True)
            return
        except TMDBError as exc:
            log.info("Film %d: no TMDB extras: %s", self._movie.id, exc)
            extras = None
        if self.winfo_exists():
            self._show_extras(extras)

    def _show_extras(self, extras: TitleExtras | None) -> None:
        """The backdrop (TMDB's, else the poster blurred), the trailer, the cast, and the
        synopsis and genres in the app's language."""
        self._extras = extras
        size = self.backdrop_size()
        backdrop = extras.info.backdrop_url(size) if extras is not None else None
        backdrop = backdrop or self._movie.backdrop_url(size)  # the list's, when TMDB failed
        if backdrop is not None:
            self.masthead.set_hero(backdrop)
        else:
            self.masthead.set_hero(self._movie.poster_url("w342"), blur=True)
        trailer = extras.trailer if extras is not None else None
        self.masthead.set_trailer(trailer.watch_url if trailer is not None else None)
        self.show_cast(extras.cast if extras is not None else ())
        self._paint()

    def destroy(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        super().destroy()


class MovieWindows:
    """Opens film info windows, one per film (TMDB id): opening an open film raises its
    window."""

    def __init__(
        self,
        master: ctk.CTk,
        *,
        images: ImageCache,
        discovery: DiscoveryService,
        watchlist: WatchlistService,
        actions: DownloadActions[Movie],
        downloaded: Callable[[int], Awaitable[bool]] | None = None,
        downloads_changed: Subscribe | None = None,
        subtitle_actions: SubtitleActions | None = None,
        subtitle_mark: Callable[[int], Awaitable[str]] | None = None,
        subtitles_changed: Subscribe | None = None,
    ) -> None:
        """`downloaded` / `downloads_changed`: whether a film is in the download history and
        when that changes; `subtitle_actions`, `subtitle_mark` / `subtitles_changed`: a
        downloaded film's CC ▾ and its saved subtitles (see MovieDetailsWindow)."""
        self._master = master
        self._images = images
        self._discovery = discovery
        self._watchlist = watchlist
        self._actions = actions
        self._downloaded = downloaded
        self._subtitle_actions = subtitle_actions
        self._subtitle_mark = subtitle_mark
        self._windows: dict[int, MovieDetailsWindow] = {}
        self._unsubscribe = [
            subscribe(self.refresh_all)
            for subscribe in (downloads_changed, subtitles_changed)
            if subscribe is not None
        ]

    def refresh_all(self) -> None:
        """Every open window asks again whether its film was downloaded, and its subtitles."""
        for window in self._live().values():
            window.refresh_state()

    async def open_movie_id(self, movie_id: int) -> bool:
        """Open (or raise) the window of a film known only by its TMDB id (a grab on the
        Downloads page), once TMDB has described it. False when TMDB can't (no key, offline,
        too slow): the caller falls back to IMDb."""
        window = self._live().get(movie_id)
        if window is not None:
            window.raise_window()
            return True
        try:
            async with asyncio.timeout(OPEN_WAIT_S):
                movie = await self._discovery.movie(movie_id)
        except (TMDBError, TimeoutError) as exc:
            log.info("Film %d: no TMDB description to open its window: %s", movie_id, exc)
            return False
        if exists(self._master):
            self.open_movie(movie)
        return True

    def open_movie(self, movie: Movie) -> None:
        """Open the film's window, or raise it."""
        window = self._live().get(movie.id)
        if window is not None:
            window.raise_window()
            return
        self._windows[movie.id] = MovieDetailsWindow(
            self._master,
            movie,
            images=self._images,
            discovery=self._discovery,
            watchlist=self._watchlist,
            actions=self._actions,
            downloaded=self._downloaded,
            subtitle_actions=self._subtitle_actions,
            subtitle_mark=self._subtitle_mark,
        )

    def close(self) -> None:
        """Stop listening and close every window (the main window is closing)."""
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe = []
        for window in self._live().values():
            window.destroy()
        self._windows = {}

    def _live(self) -> dict[int, MovieDetailsWindow]:
        self._windows = {
            movie_id: window for movie_id, window in self._windows.items() if exists(window)
        }
        return self._windows
