"""Cards for TMDB titles: films on the Movies page, shows on the Discover tab.

Both look like `ShowCard`: poster, a title of up to two lines, a meta line, a details line and
one action button (films: the Download row across the card's bottom), with an ⓘ IMDb link in
the corner. Each card fetches its title's TMDB details
itself through an injected loader; until they arrive (or if they can't be loaded) it shows what
the list entry had.
"""

from __future__ import annotations

import asyncio
import logging
import webbrowser
from collections.abc import Awaitable, Callable
from datetime import date
from typing import TYPE_CHECKING, Any, Literal, TypeVar
from urllib.parse import quote

import customtkinter as ctk

from app.models import DiscoverShow, Movie, MovieDetails, TVDetails
from app.services.tmdb import TMDBError
from app.ui import theme
from app.ui.cards import CARD_WIDTH
from app.ui.download_control import (
    ROW_SPACE,
    Base,
    DownloadActions,
    DownloadControl,
    movie_key,
)
from app.ui.tasks import spawn
from app.ui.text_fit import SLACK, elide_to_width, fit_to_lines, title_lines, unscaled_width
from app.ui.tooltip import add_tooltip
from app.ui.widgets import PosterImage

if TYPE_CHECKING:
    from app.services.images import ImageCache

log = logging.getLogger(__name__)

D = TypeVar("D")
TrackState = Literal["idle", "busy", "tracked", "missing"]

CARD_HEIGHT = 135
_POSTER_HEIGHT = 100
# Movie cards: the poster with 10 above it, then the Download row across the card.
MOVIE_CARD_HEIGHT = 10 + _POSTER_HEIGHT + ROW_SPACE
# Room a card's text keeps clear on the right for the ⓘ (24 units wide, 8 from the card's edge).
IMDB_ICON_ROOM = 28
IMDB_FIND_URL = "https://www.imdb.com/find/?q="
_TITLE_MAX_LINES = 2
_POSTER_SIZE = "w185"


# -- text ------------------------------------------------------------------------------------


def format_runtime(minutes: int) -> str:
    """"2h 25m", "2h" or "45m"."""
    hours, rest = divmod(minutes, 60)
    if not hours:
        return f"{rest}m"
    return f"{hours}h {rest}m" if rest else f"{hours}h"


def _rating(vote_average: float, vote_count: int) -> str:
    return f"★ {vote_average:.1f}" if vote_count else "★ N/A"


def movie_meta(movie: Movie, details: MovieDetails | None) -> str:
    """"15 Oct 2026 | ★ 7.3": the digital release date once known, else the release date."""
    day = (details.digital_release if details else None) or movie.release_date
    when = f"{day:%d %b %Y}" if day else "TBA"
    return f"{when} | {_rating(movie.vote_average, movie.vote_count)}"


def movie_details_line(details: MovieDetails | None) -> str:
    """"Rated PG-13 · 2h 25m", or whichever part is known ("" for neither)."""
    if details is None:
        return ""
    parts: list[str] = []
    if details.certification:
        parts.append(f"Rated {details.certification}")
    if details.runtime:
        parts.append(format_runtime(details.runtime))
    return " · ".join(parts)


def movie_action(movie: Movie, details: MovieDetails | None, today: date) -> tuple[str, bool]:
    """The grab button's label, and whether the film can be grabbed (it is out digitally).

    Without a known digital release date the film counts as out once it has been released.
    """
    digital = details.digital_release if details else None
    if digital is not None:
        if digital <= today:
            return "Search / Grab", True
        return f"Digital {digital:%d %b}", False
    if movie.release_date is None or movie.release_date > today:
        return "Not Released", False
    return "Search / Grab", True


def imdb_search_url(movie: Movie) -> str:
    """IMDb's search for the film's title and year."""
    query = f"{movie.title} {movie.year}" if movie.year else movie.title
    return IMDB_FIND_URL + quote(query, safe="")


def show_meta(show: DiscoverShow) -> str:
    """"2024 | ★ 8.1"."""
    return f"{show.year or 'TBA'} | {_rating(show.vote_average, show.vote_count)}"


def show_details_line(details: TVDetails | None) -> str:
    """"Returning Series • 3 Seasons", or whichever part is known ("" for neither)."""
    if details is None:
        return ""
    parts: list[str] = []
    if details.status:
        parts.append(details.status)
    if seasons := details.number_of_seasons:
        parts.append(f"{seasons} Season" if seasons == 1 else f"{seasons} Seasons")
    return " • ".join(parts)


def show_imdb_url(details: TVDetails) -> str | None:
    return f"https://www.imdb.com/title/{details.imdb_id}/" if details.imdb_id else None


# -- shared parts ----------------------------------------------------------------------------


IMDB_TIP = "Open on IMDb"


class ImdbIcon(ctk.CTkLabel):
    """An ⓘ in a card's top-right corner that opens `url` (an IMDb page); `url` may change."""

    def __init__(self, card: ctk.CTkFrame, url: str) -> None:
        super().__init__(
            card,
            text="ⓘ",
            width=24,
            height=24,
            font=theme.font(16),
            text_color=theme.TEXT_MUTED,
            cursor="hand2",
        )
        self.url = url
        add_tooltip(self, IMDB_TIP)
        self.place(relx=1.0, x=-8, y=8, anchor="ne")
        self.bind("<Button-1>", lambda _event: webbrowser.open(self.url))
        self.bind("<Enter>", lambda _event: self.configure(text_color=theme.TEXT))
        self.bind("<Leave>", self._restore)
        card.bind("<Leave>", self._restore)

    def _restore(self, _event: Any) -> None:
        self.configure(text_color=theme.TEXT_MUTED)


def add_imdb_icon(card: ctk.CTkFrame, url: str | None) -> ImdbIcon | None:
    """An `ImdbIcon` on `card`; none without a url."""
    return None if url is None else ImdbIcon(card, url)


class _MediaCard(ctk.CTkFrame):
    """Poster | title (two lines at most), meta line, details line and an action button.

    Without a `command` the card has no button of its own: the subclass packs a row across the
    card's bottom (before `self._poster`) and passes a `height` with room for it.
    """

    def __init__(
        self,
        master: Any,
        *,
        title: str,
        poster_url: str | None,
        images: ImageCache,
        command: Callable[[], None] | None,
        height: int = CARD_HEIGHT,
    ) -> None:
        super().__init__(
            master,
            fg_color=theme.SURFACE,
            border_color=theme.EDGE,
            border_width=1,
            corner_radius=8,
            width=CARD_WIDTH,
            height=height,
        )
        self.grid_propagate(False)
        self.pack_propagate(False)
        self._title_text = title_lines(title)
        self._meta_text = ""
        self._details_text = ""
        self._text_width: float | None = None  # the text column's unscaled width, once laid out
        self._details_task: asyncio.Task[None] | None = None
        self._imdb: ImdbIcon | None = None

        pady: int | tuple[int, int] = 10 if command is not None else (10, 0)
        poster = PosterImage(self, width=68, height=_POSTER_HEIGHT)
        poster.pack(side="left", anchor="n", padx=10, pady=pady)
        poster.show(images, poster_url)
        self._poster = poster

        info = ctk.CTkFrame(self, fg_color="transparent")
        info.pack(side="left", fill="both", expand=True, padx=(0, 5), pady=pady)
        self._button: ctk.CTkButton | None = None
        if command is not None:
            # Packed first so the button keeps its room at the bottom whatever the text needs.
            self._button = ctk.CTkButton(
                info,
                text="",
                height=22,
                font=theme.font(10, bold=True),
                corner_radius=4,
                border_width=0,
                command=command,
            )
            self._button.pack(side="bottom", fill="x")
        self._title_font = theme.font(12, bold=True)
        self._title = ctk.CTkLabel(
            info,
            text=self._title_text,
            font=self._title_font,
            text_color=theme.TEXT,
            wraplength=100,  # until the first layout reports the real width
            justify="left",
            anchor="nw",
        )
        self._title.pack(anchor="nw")
        self._line_font = theme.font(9)
        self._meta = ctk.CTkLabel(
            info, text="", font=self._line_font, text_color=theme.TEXT_MUTED, anchor="w"
        )
        self._meta.pack(fill="x")
        self._details = ctk.CTkLabel(
            info, text="", font=self._line_font, text_color="gray75", anchor="w"
        )
        self._details.pack(fill="x")
        info.bind("<Configure>", self._on_resize)

    # -- text ----------------------------------------------------------------------------------

    def _on_resize(self, event: Any) -> None:
        if event.width > 1:
            self._text_width = unscaled_width(self, event.width)
            self._fit_title()
            self._fit_lines()

    def _fit_title(self) -> None:
        """Wrap the title at the width the column really leaves beside the ⓘ; a title needing
        more than two lines gets its second line elided."""
        if self._text_width is None:
            return
        width = self._text_width - IMDB_ICON_ROOM - SLACK
        self._title.configure(
            wraplength=max(1, round(width)),
            text=fit_to_lines(self._title_text, width, _TITLE_MAX_LINES, self._title_font.measure),
        )

    def _set_lines(self, meta: str, details: str) -> None:
        self._meta_text = meta
        self._details_text = details
        self._fit_lines()

    def _fit_lines(self) -> None:
        meta, details = self._meta_text, self._details_text
        if self._text_width is not None:
            width = self._text_width - SLACK
            meta = elide_to_width(meta, width, self._line_font.measure)
            details = elide_to_width(details, width, self._line_font.measure)
        self._meta.configure(text=meta)
        self._details.configure(text=details)

    # -- button and link -----------------------------------------------------------------------

    def _style_button(self, text: str, color: str, hover: str, *, enabled: bool) -> None:
        if self._button is None:
            raise RuntimeError("this card has no button of its own")
        self._button.configure(
            text=text,
            fg_color=color,
            hover_color=hover,
            state="normal" if enabled else "disabled",
        )

    def _set_imdb_url(self, url: str | None) -> None:
        """Point the ⓘ at `url`, adding it if needed; None leaves the icon as it is."""
        if url is None:
            return
        if self._imdb is None:
            self._imdb = ImdbIcon(self, url)
        else:
            self._imdb.url = url

    # -- details -------------------------------------------------------------------------------

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


# -- cards -----------------------------------------------------------------------------------


def movie_base(movie: Movie, details: MovieDetails | None, today: date) -> Base:
    """The Download control once the film is out digitally (`movie_action`), else a disabled
    "Digital 15 Oct" / "Not Released"."""
    label, can_grab = movie_action(movie, details, today)
    return Base("download") if can_grab else Base("unavailable", label)


class MovieCard(_MediaCard):
    """A film: release date and rating, certification and runtime, and a Download row ("⬇
    Download" and "⋯" for the search window) that unlocks once the film is out digitally."""

    def __init__(
        self,
        master: Any,
        movie: Movie,
        *,
        images: ImageCache,
        details: Callable[[int], Awaitable[MovieDetails]],
        actions: DownloadActions[Movie],
    ) -> None:
        super().__init__(
            master,
            title=movie.title,
            poster_url=movie.poster_url(_POSTER_SIZE),
            images=images,
            command=None,
            height=MOVIE_CARD_HEIGHT,
        )
        self._movie = movie
        self._control = DownloadControl(
            self,
            item=movie,
            key=movie_key(movie.id),
            base=movie_base(movie, None, date.today()),
            actions=actions,
        )
        self._control.pack_row(before=self._poster)
        self._show_details(None)
        self._load_details(details, movie.id, self._show_details)

    def _show_details(self, details: MovieDetails | None) -> None:
        movie = self._movie
        self._set_lines(movie_meta(movie, details), movie_details_line(details))
        self._control.set_base(movie_base(movie, details, date.today()))
        self._set_imdb_url((details.imdb_url if details else None) or imdb_search_url(movie))


class DiscoverShowCard(_MediaCard):
    """A TMDB show: first-air year and rating, status and seasons, and a Track button."""

    def __init__(
        self,
        master: Any,
        show: DiscoverShow,
        *,
        images: ImageCache,
        details: Callable[[int], Awaitable[TVDetails]],
        is_tracked: bool,
        on_track: Callable[[DiscoverShow], None],
    ) -> None:
        super().__init__(
            master,
            title=show.name,
            poster_url=show.poster_url(_POSTER_SIZE),
            images=images,
            command=self._track,
        )
        self._show = show
        self._on_track = on_track
        self._set_lines(show_meta(show), "")
        self.set_track_state("tracked" if is_tracked else "idle")
        self._load_details(details, show.id, self._show_details)

    def set_track_state(self, state: TrackState) -> None:
        """idle → "+ Track"; busy → "Finding…"; tracked → "Tracking"; missing → "Not on TVMaze"."""
        if state == "idle":
            self._style_button("+ Track", theme.ACCENT, theme.ACCENT_HOVER, enabled=True)
        elif state == "busy":
            self._style_button("Finding…", theme.ACCENT, theme.ACCENT_HOVER, enabled=False)
        elif state == "tracked":
            self._style_button("Tracking", "transparent", theme.ACCENT_HOVER, enabled=False)
        else:
            self._style_button(
                "Not on TVMaze", theme.DANGER_BUTTON, theme.DANGER_BUTTON_HOVER, enabled=False
            )

    def _show_details(self, details: TVDetails) -> None:
        self._set_lines(show_meta(self._show), show_details_line(details))
        self._set_imdb_url(show_imdb_url(details))

    def _track(self) -> None:
        self._on_track(self._show)
