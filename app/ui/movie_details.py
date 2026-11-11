"""The film info window: the shared info look (app/ui/title_info.py: the backdrop with "▶ Watch
trailer", the poster, title, meta line, genres, synopsis, cast) with a film's actions: ⬇ Auto
(the one-click download) and, in "tabs", ☰ Choose… (its Releases tab; "side" shows the
releases right beside the actions, so it has none) (app/ui/row_actions.py); the watchlist button and IMDb, and a quiet ✓
(tip "In Plex") while Plex is connected and the film is in the user's Plex libraries
(app/services/plex.py), the only thing that counts as had: a film AirGrabber downloaded gets no
✓ (owner's decision for 3.1.0). While it is in Plex, "▶ Play" beside the download row opens its
file in VLC on this PC (app/ui/play_control.py, app/services/playback.py). Its releases (app/ui/release_panel.py) are searched as soon as
the window opens: beside the info ("side") or in the Releases tab ("tabs"), which says how many
there are ("Releases (18)").

Opened from the film cards' cover and title (the Movies page, the watchlist shelf, search
results) and from every "☰ Choose…" of a film (on its releases); one window per film
(`MovieWindows` raises the open one). It shows what the card knew at once (TMDB's list data,
in English), then TMDB's details (runtime, digital release, IMDb id) and the extras in the
app's language (`DiscoveryService.movie_extras`). Without a TMDB key it keeps the card's data
(the poster still loads: TMDB's pictures need no key), has no backdrop, trailer or cast, and a
plain hint says what a key adds.
"""

from __future__ import annotations

import asyncio
import logging
import webbrowser
from collections.abc import Awaitable, Callable, Coroutine
from datetime import date
from typing import TYPE_CHECKING, Any

import customtkinter as ctk

from app.i18n import t
from app.models import InfoLayout, Movie, MovieDetails, TitleExtras
from app.models.plex import PlexTitle
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
from app.ui.missing_control import HAVE_MARK, PLEX_TIP, mark_label
from app.ui.play_control import Play, play_button
from app.ui.release_panel import MovieTarget, PanelServices, ReleasePanel
from app.ui.row_actions import RowActions
from app.ui.shelf_filter import MOVIE_GENRES, Genre, genre_name, genres_of
from app.ui.tasks import spawn
from app.ui.tooltip import add_tooltip
from app.ui.title_info import (
    TitleInfoWindow,
    exists,
    join_meta,
    rating_text,
    release_text,
)
from app.ui.watchlist_button import WatchlistButton

if TYPE_CHECKING:
    from app.services import DiscoveryService, ImageCache, WatchlistService
    from app.services.playback import PlaybackService
    from app.ui.views.base import StatusReporter

log = logging.getLogger(__name__)

Listener = Callable[[], None]
Subscribe = Callable[[Listener], Callable[[], None]]  # returns the unsubscribe function

WATCHLIST_WIDTH = 170
IMDB_WIDTH = 90
OPEN_WAIT_S = 5.0  # a film known only by its id waits this long for TMDB's description


def movie_title(movie: Movie) -> str:
    """"Dune (2021)": a one-click download's label."""
    return f"{movie.title} ({movie.year})" if movie.year else movie.title


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
        layout: InfoLayout = InfoLayout.SIDE,
        releases: PanelServices | None = None,
        in_plex: Callable[[int, str | None], Awaitable[bool]] | None = None,
        playback: PlaybackService | None = None,
        status: StatusReporter | None = None,
    ) -> None:
        """`releases`: what its
        releases panel works with (without it there is none). `in_plex(tmdb_id, imdb_id)`:
        whether the film is in Plex (the quiet ✓ `plex_mark`, made only then). `playback` +
        `status`: "▶ Play" (`play_button`) while the film is in Plex, its outcome on the status
        bar."""
        super().__init__(
            master,
            title=movie.title,
            images=images,
            poster=lambda width: movie.poster_url(tmdb_poster_size(width)),
            layout=layout,
            releases=releases,
        )
        self._movie = movie
        self._discovery = discovery
        self._today = today
        self._in_plex_lookup = in_plex
        self._is_in_plex = False
        self._plex_title: PlexTitle | None = None  # the film in Plex: what ▶ Play plays
        self._playback = playback
        self._play = Play(playback, status) if playback is not None and status is not None else None
        self._details: MovieDetails | None = None
        self._extras: TitleExtras | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._state_task: asyncio.Task[None] | None = None

        # As wide as what it shows (⬇ Auto + ☰ Choose…, a run's outcome, …).
        self.download = RowActions(
            self.buttons,
            item=movie,
            key=movie_key(movie.id),
            base=movie_base(movie, None, today()),
            actions=actions,
            height=ROW_HEIGHT,
            align="left",
            choose=self.layout is not InfoLayout.SIDE,  # "side": the releases are beside it
        )
        self.watch_button = WatchlistButton(
            self.buttons,
            movie=movie,
            watchlist=watchlist,
            width=WATCHLIST_WIDTH,
            height=ROW_HEIGHT,
        )
        # As tall as the Download row beside it, outlined like its ⋯.
        self.imdb_button = ctk.CTkButton(
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
        )
        # In Plex: the quiet ✓ of an episode you have, after the buttons (made only then).
        self.plex_mark: ctk.CTkLabel | None = None
        # In Plex (and ▶ Play offered): "▶ Play" after the download row (made only then).
        self.play_button: ctk.CTkButton | None = None
        self._show_buttons()
        self._paint()

        self.masthead.reserve_hero(discovery.has_tmdb_key)
        self._start(self._load_details(), "MovieDetailsWindow.details")
        self._start(self._load_extras(), "MovieDetailsWindow.extras")
        self.refresh_state()
        # Its releases are searched right away.
        self.open_releases(MovieTarget(movie), reveal=False)

    def show_releases(self, quality: str | None = None) -> ReleasePanel | None:
        """On its releases ("☰ Choose…"): "tabs" chooses the Releases tab and scrolls to it.
        `quality`: the VERSION chip to show (a Library film's ⬆ Upgrade: UPGRADE_KEY)."""
        panel = self.open_releases(MovieTarget(self._movie))
        if panel is not None:
            if quality is not None:
                panel.set_quality(quality)
            panel.focus_table()
        return panel

    def _show_buttons(self) -> None:
        """⬇ Auto (+ ☰ Choose… in "tabs"), ▶ Play while it is in Plex, the watchlist button,
        IMDb, and the ✓ while it is in Plex."""
        if self._is_in_plex and self.plex_mark is None:
            self.plex_mark = mark_label(
                self.buttons, HAVE_MARK, theme.TEXT_MUTED, dot=False, height=ROW_HEIGHT
            )
            add_tooltip(self.plex_mark, t(PLEX_TIP))
        playable = self._is_in_plex and self._plex_title is not None and self._play is not None
        if playable and self.play_button is None:
            self.play_button = play_button(
                self.buttons,
                self.play,
                height=ROW_HEIGHT,
                font_size=CONTROL_FONT_SIZE,
                radius=CONTROL_RADIUS,
            )
        plex = [self.plex_mark] if self.plex_mark is not None else []
        play = [self.play_button] if self.play_button is not None and playable else []
        self.buttons.set_items([self.download, *play, self.watch_button, self.imdb_button, *plex])
        if not self._is_in_plex and self.plex_mark is not None:
            self.plex_mark.destroy()
            self.plex_mark = None
        if not playable and self.play_button is not None:
            self.play_button.destroy()
            self.play_button = None

    def play(self) -> None:
        """"▶ Play": the film's file in VLC on this PC (the status bar says how it went)."""
        if self._play is not None and self._plex_title is not None:
            self._play.film(self._plex_title)

    def refresh_state(self) -> None:
        """Ask again whether it is in Plex (Plex connected or disconnected too)."""
        if self._in_plex_lookup is None:
            return
        if self._state_task is not None and not self._state_task.done():
            self._state_task.cancel()
        self._state_task = spawn(self._load_state(), name="MovieDetailsWindow.state")
        self._tasks.add(self._state_task)
        self._state_task.add_done_callback(self._tasks.discard)

    async def _load_state(self) -> None:
        in_plex = False
        plex_title: PlexTitle | None = None
        if self._in_plex_lookup is not None:
            imdb_id = self._details.imdb_id if self._details is not None else None
            in_plex = await self._in_plex_lookup(self._movie.id, imdb_id)
            if in_plex and self._playback is not None and self._play is not None:
                plex_title = await self._playback.film(self._movie.id, imdb_id)
        if not self.winfo_exists():
            return
        self._is_in_plex = in_plex
        self._plex_title = plex_title
        self._show_buttons()

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
        self.download.set_base(self.download_base())
        self._paint()
        if details.imdb_id and not self._is_in_plex and self._in_plex_lookup is not None:
            self.refresh_state()  # a film Plex knows only by its IMDb id

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
        if self._play is not None:
            self._play.close()
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
        releases: PanelServices | None = None,
        in_plex: Callable[[int, str | None], Awaitable[bool]] | None = None,
        plex_changed: Subscribe | None = None,
        playback: PlaybackService | None = None,
        status: StatusReporter | None = None,
    ) -> None:
        """`releases`: the releases panels' services; its settings say the layout a new window opens in.
        `in_plex` / `plex_changed`: whether a film is in Plex and when that may have changed.
        `playback` / `status`: the windows' "▶ Play" (none without them)."""
        self._master = master
        self._images = images
        self._discovery = discovery
        self._watchlist = watchlist
        self._actions = actions
        self._in_plex = in_plex
        self._releases = releases
        self._playback = playback
        self._status = status
        self._windows: dict[int, MovieDetailsWindow] = {}
        self._unsubscribe = [
            subscribe(self.refresh_all)
            for subscribe in (plex_changed,)
            if subscribe is not None
        ]

    def refresh_all(self) -> None:
        """Every open window asks again whether its film is in Plex."""
        for window in self._live().values():
            window.refresh_state()

    def layout(self) -> InfoLayout:
        """The layout a window opens in now (Settings)."""
        if self._releases is None:
            return InfoLayout.SIDE
        layout: InfoLayout = self._releases.settings.current.info_layout
        return layout

    async def open_movie_id(
        self, movie_id: int, *, releases: bool = False, quality: str | None = None
    ) -> bool:
        """Open (or raise) the window of a film known only by its TMDB id (a grab on the
        Downloads page), once TMDB has described it; `releases`: on its releases. False when
        TMDB can't (no key, offline, too slow): the caller falls back to IMDb or says so.
        `quality`: the releases' VERSION chip (with `releases`)."""
        window = self._live().get(movie_id)
        if window is not None:
            window.raise_window()
            if releases:
                window.show_releases(quality)
            return True
        try:
            async with asyncio.timeout(OPEN_WAIT_S):
                movie = await self._discovery.movie(movie_id)
        except (TMDBError, TimeoutError) as exc:
            log.info("Film %d: no TMDB description to open its window: %s", movie_id, exc)
            return False
        if exists(self._master):
            self.open_movie(movie, releases=releases, quality=quality)
        return True

    def open_movie(
        self, movie: Movie, *, releases: bool = False, quality: str | None = None
    ) -> None:
        """Open the film's window, or raise it; `releases`: on its releases ("☰ Choose…"),
        `quality`: on that VERSION chip."""
        window = self._live().get(movie.id)
        if window is None:
            window = self._windows[movie.id] = self._new_window(movie)
        else:
            window.raise_window()
        if releases:
            window.show_releases(quality)

    def _new_window(self, movie: Movie) -> MovieDetailsWindow:
        return MovieDetailsWindow(
            self._master,
            movie,
            images=self._images,
            discovery=self._discovery,
            watchlist=self._watchlist,
            actions=self._actions,
            layout=self.layout(),
            releases=self._releases,
            in_plex=self._in_plex,
            playback=self._playback,
            status=self._status,
        )

    def close(self) -> None:
        """Stop listening and close every window (the main window is closing)."""
        for unsubscribe in self._unsubscribe:
            unsubscribe()
        self._unsubscribe = []
        self.close_windows()

    def close_windows(self) -> None:
        """Close every window (another person was selected: MainWindow closes the show windows
        then too, one rule for every info window)."""
        for window in self._live().values():
            window.destroy()
        self._windows = {}

    def close_shown(self) -> None:
        """Close the windows on screen (a click outside them). One a click has only just opened
        isn't on screen yet and stays."""
        for window in self._live().values():
            if window.winfo_ismapped():
                window.destroy()

    def _live(self) -> dict[int, MovieDetailsWindow]:
        self._windows = {
            movie_id: window for movie_id, window in self._windows.items() if exists(window)
        }
        return self._windows
