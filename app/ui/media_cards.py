"""Cards for TMDB titles: films on the Movies page and in search results, shows on the Discover
tab. Both are tall poster cards (`PosterCard`): films with the one-click Download row, shows with
"+ Follow". Clicking a film's cover or title calls `on_open` (its info window), or opens it on
IMDb without one; a show's calls `on_open_show`, or opens the show on IMDb without one.

A film's card has one row: the Download control (without its ⋯: a right-click on it chooses a
download yourself; a film not out yet, "Digital 17 Nov", in the grey "not yet" outline) and the
round watchlist ★ beside it (`WatchlistStar`: filled while on the list); the watchlist shelf's
own cards are in `watchlist_cards`. A film in the user's Plex libraries (`in_plex`) gets the library mark, a small
shelf of books, in the cover's top-right corner, its tip "In Plex" (app/ui/library_mark.py; the
watchlist shelf's cards, the search results' and Discover's shows get it too).

Each card fetches its title's TMDB details itself through an injected loader: a film's decide
whether it can be downloaded yet, and both name the title's IMDb page.

"""

from __future__ import annotations

import asyncio
import logging
import webbrowser
from collections.abc import Awaitable, Callable
from datetime import date
from typing import TYPE_CHECKING, Any, TypeVar
from urllib.parse import quote

import customtkinter as ctk

from app.i18n import month_short, t
from app.models import DiscoverShow, Movie, MovieDetails, TVDetails
from app.models.plex import PlexShows
from app.services.tmdb import TMDBError
from app.ui import theme
from app.ui.card_scale import FULL, CardScale
from app.ui.download_control import GAP, Base, DownloadActions, DownloadControl, movie_key
from app.ui.library_mark import LibraryMark, library_mark
from app.ui.poster_card import PosterCard, TrackButton, TrackState, row_height, year_rating_line
from app.ui.tasks import spawn
from app.ui.watchlist_button import WatchlistStar

if TYPE_CHECKING:
    from app.services.images import ImageCache
    from app.services.watchlist import WatchlistService

log = logging.getLogger(__name__)

D = TypeVar("D")

IMDB_FIND_URL = "https://www.imdb.com/find/?q="
# TMDB's poster widths, smallest first ("original" beyond them).
TMDB_POSTER_WIDTHS = (92, 154, 185, 342, 500, 780)


# -- text ------------------------------------------------------------------------------------


def format_runtime(minutes: int) -> str:
    """"2h 25m", "2h" or "45m"."""
    hours, rest = divmod(minutes, 60)
    if not hours:
        return t("{minutes}m", minutes=rest)
    if rest:
        return t("{hours}h {minutes}m", hours=hours, minutes=rest)
    return t("{hours}h", hours=hours)


def tmdb_rating(vote_average: float, vote_count: int) -> float | None:
    """The average vote, or None (unknown) without any votes."""
    return vote_average if vote_count else None


def tmdb_poster_size(width_px: int) -> str:
    """TMDB's smallest poster size at least `width_px` pixels wide ("w342"), else "original"."""
    for width in TMDB_POSTER_WIDTHS:
        if width >= width_px:
            return f"w{width}"
    return "original"


def movie_action(movie: Movie, details: MovieDetails | None, today: date) -> tuple[str, bool]:
    """The grab button's label, and whether the film can be grabbed (it is out digitally).

    Without a known digital release date the film counts as out once it has been released.
    """
    digital = details.digital_release if details else None
    if digital is not None:
        if digital <= today:
            return t("Search / Grab"), True
        return t("Digital {date}", date=f"{digital:%d} {month_short(digital)}"), False
    if movie.release_date is None or movie.release_date > today:
        return t("Not Released"), False
    return t("Search / Grab"), True


def imdb_search_url(movie: Movie) -> str:
    """IMDb's search for the film's title and year."""
    query = f"{movie.title} {movie.year}" if movie.year else movie.title
    return IMDB_FIND_URL + quote(query, safe="")


def show_imdb_url(details: TVDetails) -> str | None:
    return f"https://www.imdb.com/title/{details.imdb_id}/" if details.imdb_id else None


# -- cards -----------------------------------------------------------------------------------


class _TmdbCard(PosterCard):
    """A poster card that loads its title's TMDB details in the background (`_load_details`)."""

    _details_task: asyncio.Task[None] | None = None

    def _load_details(
        self, load: Callable[[int], Awaitable[D]], item_id: int, show: Callable[[D], None]
    ) -> None:
        self._details_task = spawn(
            self._fetch_details(load, item_id, show), name=f"{type(self).__name__}.details"
        )

    async def _fetch_details(
        self, load: Callable[[int], Awaitable[D]], item_id: int, show: Callable[[D], None]
    ) -> None:
        try:
            details = await load(item_id)
        except TMDBError as exc:
            # The card keeps its list data; the error is already in the log if it matters.
            log.debug("%s %d: no TMDB details: %s", type(self).__name__, item_id, exc)
            return
        if self.winfo_exists():
            show(details)

    def destroy(self) -> None:
        if self._details_task is not None:
            self._details_task.cancel()
        super().destroy()


def movie_base(movie: Movie, details: MovieDetails | None, today: date) -> Base:
    """The Download control once the film is out digitally (`movie_action`), else a disabled
    "Digital 15 Oct" / "Not Released" (on the film cards in the grey "not yet" outline)."""
    label, can_grab = movie_action(movie, details, today)
    if can_grab:
        return Base("download")
    digital = details.digital_release if details else None
    # Where "Digital 15 Oct" doesn't fit: the date alone (its tip says it in full).
    short = (f"{digital:%d} {month_short(digital)}",) if digital is not None else ()
    return Base("unavailable", label, short)


class MovieCard(_TmdbCard):
    """A film: year and rating, and one row: "⬇ Download" (a right-click: the search window),
    which unlocks once the film is out digitally, with the round watchlist ★ beside it. Clicking the cover or the title calls `on_open(movie)` (the film's info window);
    without it, it opens the film on IMDb: its own page once the details name it, IMDb's search
    until then. `in_plex`: the film is in the user's Plex libraries (`plex_mark`, the books)."""

    def __init__(
        self,
        master: Any,
        movie: Movie,
        *,
        images: ImageCache,
        details: Callable[[int], Awaitable[MovieDetails]],
        actions: DownloadActions[Movie],
        watchlist: WatchlistService,
        scale: CardScale = FULL,
        on_open: Callable[[Movie], None] | None = None,
        in_plex: bool = False,
    ) -> None:
        super().__init__(
            master,
            title=movie.title,
            meta=year_rating_line(movie.year, tmdb_rating(movie.vote_average, movie.vote_count)),
            poster=lambda width: movie.poster_url(tmdb_poster_size(width)),
            images=images,
            scale=scale,
        )
        self._movie = movie
        self._imdb_url = imdb_search_url(movie)
        height = row_height(scale)
        row = ctk.CTkFrame(self, fg_color="transparent", height=height)
        row.pack_propagate(False)
        self._pack_action(row)
        self.watch_button = WatchlistStar(
            row, movie=movie, watchlist=watchlist, scale=scale, size=height
        )
        self.watch_button.pack(side="right", padx=(scale.px(GAP), 0))
        self._follow_scale(self.watch_button.rescale)
        self._control = DownloadControl(
            row,
            item=movie,
            key=movie_key(movie.id),
            base=movie_base(movie, None, date.today()),
            actions=actions,
            scale=scale,
            more=False,
            outline_unavailable=True,
        )
        self._control.pack(side="left", fill="both", expand=True)
        self.plex_mark: LibraryMark | None = library_mark(self) if in_plex else None
        if on_open is not None:
            self._set_open(lambda: on_open(movie))
        else:
            self._set_open(self._open_imdb)
        self._load_details(details, movie.id, self._show_details)

    def _show_details(self, details: MovieDetails) -> None:
        movie = self._movie
        self._control.set_base(movie_base(movie, details, date.today()))
        self._imdb_url = details.imdb_url or imdb_search_url(movie)

    def _open_imdb(self) -> None:
        webbrowser.open(self._imdb_url)


def source_lines(year: int | None, rating: float | None, source: str) -> tuple[str, ...]:
    """A Discover card's line with its source, longest first: without the rating when that
    doesn't fit, and as it was when the source doesn't fit at all."""
    line = year_rating_line(year, rating)
    return (
        f"{line} · {source}" if line else source,
        f"{year} · {source}" if year else source,
        line,
    )


class DiscoverShowCard(_TmdbCard):
    """A TMDB show: first-air year, rating and (once the details are in) where it airs, and
    + Follow. Clicking the cover calls `on_open_show(show)`; without it, it opens the show on IMDb
    once the details name it. `plex_shows`: the shows in Plex; the details' ids put the library
    mark on the card when it is one of them."""

    def __init__(
        self,
        master: Any,
        show: DiscoverShow,
        *,
        images: ImageCache,
        details: Callable[[int], Awaitable[TVDetails]],
        is_tracked: bool,
        on_track: Callable[[DiscoverShow], None],
        on_open_show: Callable[[DiscoverShow], None] | None = None,
        scale: CardScale = FULL,
        plex_shows: PlexShows | None = None,
    ) -> None:
        super().__init__(
            master,
            title=show.name,
            meta=year_rating_line(show.year, tmdb_rating(show.vote_average, show.vote_count)),
            poster=lambda width: show.poster_url(tmdb_poster_size(width)),
            images=images,
            scale=scale,
            meta_row=True,
        )
        self._show = show
        self._on_track = on_track
        self._on_open_show = on_open_show
        self._plex_shows = plex_shows
        self.plex_mark: LibraryMark | None = None
        self._track_button = TrackButton(
            self, busy_text=t("Finding…"), command=self._track, scale=scale
        )
        self._pack_action(self._track_button)
        self.set_track_state("tracked" if is_tracked else "idle")
        if on_open_show is not None:
            self._set_open(lambda: on_open_show(show))
        self._load_details(details, show.id, self._show_details)

    def set_track_state(self, state: TrackState) -> None:
        """idle → "+ Follow"; busy → "Finding…"; tracked → "✓ Following"; missing → "Not on
        TVMaze"."""
        self._track_button.set_state(state)

    def _show_details(self, details: TVDetails) -> None:
        plex = self._plex_shows
        if plex is not None and self.plex_mark is None and plex.has(details.tvdb_id, details.imdb_id):
            self.plex_mark = library_mark(self)
        if self._on_open_show is None and (url := show_imdb_url(details)) is not None:
            self._set_open(lambda: webbrowser.open(url))
        if details.networks:
            show = self._show
            rating = tmdb_rating(show.vote_average, show.vote_count)
            self._set_meta(source_lines(show.year, rating, details.networks[0]))

    def _track(self) -> None:
        self._on_track(self._show)
