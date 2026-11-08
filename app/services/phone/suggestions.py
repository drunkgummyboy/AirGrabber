"""`GET /v1/suggestions?kind=movie|series`: what could be fetched for tonight, for the Watch tab.

The phone's person's watchlist first (films only: AirGrabber has no list of wanted series), then
Discover's films or series out now, at most MAX_ITEMS, without what Plex has. Discover's "coming
soon" shelves stay out, as does what isn't out yet: on Watch it couldn't be downloaded (Coming up
shows it). Without a TMDB key, or with TMDB down, Discover just adds nothing: never an error.

An `id` is what `POST /v1/download` takes with the same `kind`: a film's TMDB id, as ever, and a
**series' TMDB id**. It's Discover's own id: finding a TVMaze id for all of them would cost two
calls a show whenever the list is asked for. `season_one` makes the step on a tap, through
`DiscoveryService.resolve`; the grab is recorded under the TVMaze id, as all TV grabs are, so "already
downloaded" and the Downloads page work as for any other. Only `kind` tells the two apart: `episode`
still means a TVMaze id.

A tap is honoured only for what was offered: on the Discover shelves now, offered earlier in this
run, or held by a shortlist row (which outlives a restart; `Shortlist` keeps a `suggestion_id` only
if `offered` says so). Anything else is no longer there, as for a film taken off a list. What's
searched for always comes from TMDB, by id: never from text a phone sent, or any phone could
download anything by naming it. With TMDB out of reach a row's title gets a plain answer, no search.

What Plex has comes from `LibraryService`. A Plex show AirGrabber's shows cache doesn't know isn't
stored at all (`match_scan`), so it can't be left out here: the phone sees its own Plex library.

The log gets the kind of a failure only. A `poster_url` that could hold a secret isn't sent.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, TypeVar

from app.i18n import t
from app.models import (
    DiscoverShow,
    Episode,
    MediaType,
    Movie,
    MovieDetails,
    ShortItem,
    Show,
    WatchlistEntry,
)
from app.services.discovery import DiscoveryService, NoTVMazeMatch
from app.services.library import LibraryService
from app.services.phone.coming_up import person_gone, safe_poster
from app.services.phone.server import Handler, Reply, Request, bad_request, error_reply
from app.services.profiles import ProfileService
from app.services.settings import SettingsService
from app.services.tmdb import TMDBError, TMDBNotFound
from app.services.tvmaze import TVMazeError
from app.services.watchlist import WatchlistService, is_out

log = logging.getLogger(__name__)

MAX_ITEMS = 60
_POSTER_SIZE = "w342"

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class SeasonOne:
    """A series a phone tapped, ready to search for."""

    show: Show  # TVMaze's
    aired: int | None  # its first season's aired episodes; None when that isn't known
    out: bool


# TMDB's series genres that name two at once. Plex has them apart, and the phone joins by name.
_TWO_GENRES = {
    "Action & Adventure": ("Action", "Adventure"),
    "Sci-Fi & Fantasy": ("Science Fiction", "Fantasy"),
    "War & Politics": ("War", "Politics"),
}


def _genre_list(genre_ids: Iterable[int], genres: Mapping[int, str]) -> list[str]:
    """The names under which Plex would have these, each once."""
    names = (genres[genre] for genre in genre_ids if genre in genres)
    return list(dict.fromkeys(one for name in names for one in _TWO_GENRES.get(name, (name,))))


def _item(
    kind: str,
    id: int,
    title: str,
    year: int | None,
    poster: str | None,
    genre_ids: Iterable[int],
    genres: Mapping[int, str],
    summary: str,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "id": id,
        "title": title,
        "year": year,
        "poster_url": safe_poster(poster),
        "genres": _genre_list(genre_ids, genres),
        "summary": summary,
    }


def _once_each(items: Iterable[T], id_of: Callable[[T], int]) -> dict[int, T]:
    found: dict[int, T] = {}
    for item in items:
        found.setdefault(id_of(item), item)
    return found


async def _details_of(fetch: Callable[[int], Awaitable[T]], ids: Iterable[int]) -> dict[int, T]:
    """What TMDB says of these; one it can't say leaves only that one out."""
    wanted = list(ids)
    results = await asyncio.gather(*(fetch(id) for id in wanted), return_exceptions=True)
    found: dict[int, T] = {}
    for id, result in zip(wanted, results, strict=True):
        if isinstance(result, BaseException):
            if not isinstance(result, TMDBError):
                raise result
        else:
            found[id] = result
    return found


class Suggestions:
    def __init__(
        self,
        library: LibraryService,
        watchlist: WatchlistService,
        discovery: DiscoveryService,
        profiles: ProfileService,
        settings: SettingsService,
        *,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        """`now`: an aware instant, whose time zone decides which day "today" is; for tests."""
        self._library = library
        self._watchlist = watchlist
        self._discovery = discovery
        self._profiles = profiles
        self._settings = settings
        self._now = now
        # What Discover has offered in this run, by TMDB id: a tap may come after it left a shelf.
        self._films: dict[int, Movie] = {}
        self._shows: dict[int, DiscoverShow] = {}

    def routes(self) -> dict[tuple[str, str], Handler]:
        return {("GET", "/v1/suggestions"): self._suggestions}

    async def _suggestions(self, request: Request) -> Reply:
        problem = person_gone(request.phone, self._profiles)
        if problem is not None:
            return problem
        assert request.phone is not None
        person = request.phone.profile_id
        kind = request.query.get("kind")
        if kind == "movie":
            items = await self._film_items(person)
        elif kind == "series":
            items = await self._series_items(person)
        else:
            return bad_request()
        return Reply(200, {"items": items[:MAX_ITEMS]})

    # -- the lists -------------------------------------------------------------------------------

    async def _film_items(self, person: int) -> list[dict[str, Any]]:
        now = self._now()
        listed = await self._watchlist.by_release_of(person)
        on_the_list = {entry.movie_id for entry in listed}
        shelf = [movie for movie in await self._shelf_films() if movie.id not in on_the_list]
        details = await _details_of(self._discovery.movie_details, [movie.id for movie in shelf])
        entries = [*listed, *(_entry(movie, details.get(movie.id), now) for movie in shelf)]
        plex = await self._library.plex_films()
        genres = await self._genre_names(MediaType.MOVIE)
        return [
            _item(
                "movie",
                entry.movie_id,
                entry.movie.title,
                entry.movie.year,
                entry.movie.poster_url(_POSTER_SIZE),
                entry.movie.genre_ids,
                genres,
                entry.movie.overview,
            )
            for entry in entries
            if is_out(entry, now.date())
            and not plex.has(entry.movie_id, entry.imdb_id or entry.movie.imdb_id)
        ]

    async def _series_items(self, person: int) -> list[dict[str, Any]]:
        today = self._now().date()
        shows = [
            show
            for show in await self._shelf_shows(person)
            if show.first_air_date is None or show.first_air_date <= today
        ]
        plex = await self._library.plex_shows()
        if plex.tvdb or plex.imdb:  # the outside ids take a call per show, cached by Discovery
            details = await _details_of(self._discovery.tv_details, [show.id for show in shows])
            shows = [
                show
                for show in shows
                if show.id not in details
                or not plex.has(details[show.id].tvdb_id, details[show.id].imdb_id)
            ]
        genres = await self._genre_names(MediaType.TV)
        return [
            _item(
                "series",
                show.id,
                show.name,
                show.year,
                show.poster_url(_POSTER_SIZE),
                show.genre_ids,
                genres,
                show.overview,
            )
            for show in shows
        ]

    async def _shelf_films(self) -> list[Movie]:
        """Discover's films out now, each once; none when TMDB can't be asked."""
        try:
            board = await self._discovery.movie_dashboard()
        except TMDBError as exc:
            log.info("Phone suggestions: no films from TMDB: %s", type(exc).__name__)
            return []
        films = _once_each((*board.just_dropped, *board.trending), lambda movie: movie.id)
        self._films.update(films)
        return list(films.values())

    async def _shelf_shows(self, person: int) -> list[DiscoverShow]:
        """Discover's series on now, each once; none when TMDB can't be asked."""
        try:
            board = await self._discovery.tv_dashboard(profile_id=person)
        except TMDBError as exc:
            log.info("Phone suggestions: no series from TMDB: %s", type(exc).__name__)
            return []
        shows = _once_each(
            (entry.show for entry in (*board.trending, *board.on_air)), lambda show: show.id
        )
        self._shows.update(shows)
        return list(shows.values())

    async def _genre_names(self, kind: MediaType) -> Mapping[int, str]:
        """TMDB's names; none when it can't be asked. The titles still show, without genres."""
        try:
            return await self._discovery.genre_names(kind)
        except TMDBError as exc:
            log.info("Phone suggestions: no genre names from TMDB: %s", type(exc).__name__)
            return {}

    # -- a tap -----------------------------------------------------------------------------------

    async def offered(self, kind: str, id: int, person: int) -> bool:
        """Whether this phone's person could have been offered it: what a shortlist row may hold
        as its `suggestion_id`. A row already holding it was offered to someone."""
        if self._held(kind, id) is not None:
            return True
        if kind == "movie":
            if id in self._films:
                return True
            listed = await self._watchlist.by_release_of(person)
            if any(entry.movie_id == id for entry in listed):
                return True
            await self._shelf_films()
            return id in self._films
        if id in self._shows:
            return True
        await self._shelf_shows(person)
        return id in self._shows

    def _held(self, kind: str, suggestion_id: int) -> ShortItem | None:
        for item in self._settings.current.phone_shortlist:
            if item.kind == kind and item.suggestion_id == suggestion_id:
                return item
        return None

    async def film(self, movie_id: int) -> WatchlistEntry | Reply | None:
        """A film offered here as a list entry would hold it. None when it wasn't offered; a
        Reply says it can't be found out just now."""
        movie = self._films.get(movie_id)
        if movie is None:
            await self._shelf_films()  # after a restart this run hasn't offered anything yet
            movie = self._films.get(movie_id)
        if movie is None:
            held = self._held("movie", movie_id)
            if held is None:
                return None
            try:
                movie = await self._discovery.movie(movie_id)
            except TMDBNotFound:
                return None
            except TMDBError:
                return _no_lookup(held.title)
        details = await _details_of(self._discovery.movie_details, [movie_id])
        return _entry(movie, details.get(movie_id), self._now())

    async def season_one(self, tmdb_id: int, person: int) -> SeasonOne | Reply | None:
        """A series offered here, by the TVMaze show the rest of the app knows it as. None when
        it wasn't offered; a Reply says it can't be found out just now."""
        found = self._shows.get(tmdb_id)
        if found is None:
            await self._shelf_shows(person)  # after a restart this run hasn't offered anything yet
            found = self._shows.get(tmdb_id)
        if found is None:
            held = self._held("series", tmdb_id)
            if held is None:
                return None
            try:
                found = await self._discovery.show(tmdb_id)
            except TMDBNotFound:
                return None
            except TMDBError:
                return _no_lookup(held.title)
        try:
            show = await self._discovery.resolve(found)
        except NoTVMazeMatch:
            message = t(
                "AirGrabber can't find {title} to download, so choose something else.",
                title=found.name,
            )
            return error_reply(502, "failed", message)
        except (TMDBError, TVMazeError):
            return _no_lookup(found.name)
        return await self._state_of_season_one(show, person)

    async def _state_of_season_one(self, show: Show, person: int) -> SeasonOne:
        """How many episodes it has out. Not knowing must never stop a download: the count only
        helps to judge a pack's size."""
        now = self._now()
        try:
            details = await self._library.show_details(show.id, profile_id=person, now=now)
        except TVMazeError as exc:
            log.info("Phone suggestions: no episodes from TVMaze: %s", type(exc).__name__)
            episodes: tuple[Episode, ...] = ()
        else:
            episodes = details.episodes
        numbered = [one for one in episodes if one.season == 1 and one.number is not None]
        aired = sum(one.has_aired(now) for one in numbered)
        if numbered:
            out = aired > 0
        else:
            out = show.premiered is None or show.premiered <= now.date()
        return SeasonOne(show, aired or None, out)


def _no_lookup(title: str) -> Reply:
    message = t(
        "AirGrabber couldn't look up {title} just now, so try again in a moment.", title=title
    )
    return error_reply(502, "failed", message)


def _entry(movie: Movie, details: MovieDetails | None, now: datetime) -> WatchlistEntry:
    """A film as a list entry holds it, so that one rule says when any film counts as out."""
    return WatchlistEntry(
        movie=movie,
        added_at=now,
        imdb_id=details.imdb_id if details is not None else None,
        digital_release=details.digital_release if details is not None else None,
        theatrical_release=details.theatrical_release if details is not None else None,
    )
