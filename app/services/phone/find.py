"""Find on the phone: films and series by name, also what the library hasn't
(docs/companion/2026-11-18-find-design.md).

- `GET /v1/find?q=<text>`: TMDB's films, then its series (`/search/movie`, `/search/tv`), at most
  FIND_LIMIT of each in TMDB's order, with what the PC knows of each (`state`) and whether it's
  out. The text is stripped and must be SHORTEST to LONGEST characters. Without a TMDB key (or
  with one TMDB refuses) the answer is empty and says so; TMDB out of reach is a plain 502. One
  title TMDB can't detail just keeps its simplest answer (out, not had by an outside id): never an
  error.
- `GET /v1/find/series/{id}`: a found series' numbered episodes by season (no specials), through
  its TVMaze counterpart (`DiscoveryService.resolve`) and `LibraryService.show_details` (a show
  nobody follows is fetched from TVMaze and kept in memory, nothing stored).
- `POST /v1/follow {"id"}`: follow a found (or suggested) series for the phone's own person.

Every `id` is a **TMDB id**. Only what was found can be fetched: what `/v1/find` answered is
remembered for this run (per kind, by TMDB id, the newest FOUND_KEEP each), and an episode list, a
follow or a download (`PhoneDownloads`: a found film, a found series' `season` or
`found_episode`) asks for one of those, or for a series the suggestions offered. Anything else is
410: after a restart the phone searches again. Nothing is ever searched for by text a phone sent
to a download, or any phone could download anything by naming it.

States. Film: `in_library` (Plex or the folders) beats `downloading` (AirGrabber grabbed it)
beats `on_list` (the phone's person's watchlist) beats `new`. Series: `in_library` beats
`following` (the phone's person follows it, as Discover tells: its TVMaze match or its name) beats
`new`. Episode: `in_library` beats `downloading` (in the download history, itself or its season's
pack) beats `new`. Without a library nothing is had (and the phone connection answers 409 anyway).

What's had of a series counts whether AirGrabber's shows cache knows the show or not (Plex's and
the folders' episode rows hold only shows it knows): Plex's show by its TMDB, TheTVDB or IMDb id
among the last check's titles (`PlexService.titles`), its episodes read from Plex itself for an
episode list (`PlexService.episodes`, under its screen time limit; a Plex that doesn't answer
counts nothing, never an error), and the folders' files of that TMDB id (`library_files`), besides
the cached episodes' own marks (`LibraryService.had_episodes`).

The log gets counts and kinds only: never the search text, a title or a TMDB id (a search is
something a person typed). A `poster_url` that could hold a secret isn't sent.
"""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Generic, Literal, TypeGuard, TypeVar

from app.i18n import t
from app.models import (
    DiscoverShow,
    GrabTarget,
    MediaType,
    Movie,
    MovieDetails,
    Show,
    ShowDetails,
)
from app.database import LibraryFileRepository
from app.models.folder_library import LibraryFile
from app.models.plex import PlexFilms, PlexTitle
from app.services.discovery import DiscoveryService, NoTVMazeMatch
from app.services.downloads import DownloadService
from app.services.library import LibraryService
from app.services.library_source import folders_on
from app.services.phone.coming_up import person_gone, safe_poster
from app.services.phone.server import Handler, Reply, Request, bad_request, error_reply
from app.services.phone.suggestions import (
    Suggestions,
    as_entry,
    cant_find,
    details_of,
    no_lookup,
)
from app.services.plex import PlexError, PlexService, plex_connected
from app.services.profiles import ProfileService
from app.services.settings import SettingsService
from app.services.tmdb import TMDBAuthError, TMDBError, TMDBNotConfigured
from app.services.tvmaze import TVMazeError
from app.services.watchlist import WatchlistService, is_out

log = logging.getLogger(__name__)

FIND_LIMIT = 10  # of each kind
FOUND_KEEP = 500  # of each kind, the newest
SHORTEST = 2
LONGEST = 100
_ID_DIGITS = 10  # longer is no TMDB id (and int() refuses the longest texts)
_MOST_EPISODES = 100  # a file named "S01E01-E99" covers at most this many
_POSTER_SIZE = "w342"

FilmState = Literal["in_library", "downloading", "on_list", "new"]
SeriesState = Literal["in_library", "following", "new"]
EpisodeState = Literal["in_library", "downloading", "new"]

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class FoundFilm:
    """A film found here, ready to search for."""

    movie: Movie
    imdb_id: str | None
    out: bool


class _Newest(Generic[T]):
    """What was found, by TMDB id; the newest FOUND_KEEP are kept."""

    def __init__(self) -> None:
        self._items: OrderedDict[int, T] = OrderedDict()

    def keep(self, id: int, item: T) -> None:
        self._items.pop(id, None)  # found again: the newest
        self._items[id] = item
        while len(self._items) > FOUND_KEEP:
            self._items.popitem(last=False)

    def get(self, id: int) -> T | None:
        return self._items.get(id)


def _whole_number(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def search_again() -> Reply:
    """For what this run's searches didn't find (or no longer remember)."""
    return error_reply(
        410, "no_longer_there", t("That search isn't there any more, so search again.")
    )


def _no_key() -> Reply:
    message = t("Searching needs a TMDB key: add one in AirGrabber's Settings.")
    return Reply(200, {"results": [], "message": message})


def _key_refused() -> Reply:
    message = t("TMDB refused the key in AirGrabber's Settings, so check it there.")
    return Reply(200, {"results": [], "message": message})


def _same_show(title: PlexTitle, tmdb_id: int, tvdb_id: int | None, imdb_id: str | None) -> bool:
    """A Plex show is this series: by its TMDB id, else TheTVDB's or IMDb's (as `match_scan`)."""
    return title.kind == "show" and (
        title.tmdb_id == tmdb_id
        or (tvdb_id is not None and title.tvdb_id == tvdb_id)
        or (imdb_id is not None and title.imdb_id == imdb_id)
    )


def _file_episodes(files: list[LibraryFile]) -> set[tuple[int, int]]:
    """(season, number) of each episode the folders' files hold ("S01E02-E03": both)."""
    had: set[tuple[int, int]] = set()
    for file in files:
        if file.season is None or file.episode is None:
            continue
        end = max(file.episode_end or file.episode, file.episode)
        last = min(end, file.episode + _MOST_EPISODES)
        had.update((file.season, number) for number in range(file.episode, last + 1))
    return had


def _cant_follow(title: str) -> Reply:
    message = t("AirGrabber can't find {title} to follow, so choose something else.", title=title)
    return error_reply(502, "failed", message)


def _found_film(movie: Movie, details: MovieDetails | None, now: datetime) -> FoundFilm:
    """Out as a list entry would be; without TMDB's details simply out (a search then says)."""
    if details is None:
        return FoundFilm(movie, movie.imdb_id, True)
    out = is_out(as_entry(movie, details, now), now.date())
    return FoundFilm(movie, details.imdb_id or movie.imdb_id, out)


def _item(
    kind: str,
    id: int,
    title: str,
    year: int | None,
    poster: str | None,
    overview: str,
    state: str,
    out: bool,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "id": id,
        "title": title,
        "year": year,
        "poster_url": safe_poster(poster),
        "overview": overview,
        "state": state,
        "out": out,
    }


class PhoneFind:
    def __init__(
        self,
        library: LibraryService,
        watchlist: WatchlistService,
        discovery: DiscoveryService,
        downloads: DownloadService,
        profiles: ProfileService,
        suggestions: Suggestions,
        *,
        settings: SettingsService,
        plex: PlexService,
        files: LibraryFileRepository,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        """`plex`, `files`: what Plex and the folders have of a series the shows cache doesn't
        know. `now`: an aware instant, whose time zone decides which day "today" is; for tests."""
        self._settings = settings
        self._plex = plex
        self._files = files
        self._library = library
        self._watchlist = watchlist
        self._discovery = discovery
        self._downloads = downloads
        self._profiles = profiles
        self._suggestions = suggestions
        self._now = now
        self._films: _Newest[Movie] = _Newest()
        self._shows: _Newest[DiscoverShow] = _Newest()

    def routes(self) -> dict[tuple[str, str], Handler]:
        return {
            ("GET", "/v1/find"): self._find,
            ("GET", "/v1/find/series/{id}"): self._episodes,
            ("POST", "/v1/follow"): self._follow,
        }

    # -- searching -------------------------------------------------------------------------------

    async def _find(self, request: Request) -> Reply:
        problem = person_gone(request.phone, self._profiles)
        if problem is not None:
            return problem
        assert request.phone is not None
        person = request.phone.profile_id
        text = request.query.get("q", "").strip()
        if not SHORTEST <= len(text) <= LONGEST:
            return bad_request()
        if not self._discovery.has_tmdb_key:
            return _no_key()
        films, shows = await asyncio.gather(
            self._discovery.search_movies(text, limit=FIND_LIMIT),
            self._discovery.search_shows(text, limit=FIND_LIMIT),
            return_exceptions=True,
        )
        if isinstance(films, BaseException) or isinstance(shows, BaseException):
            return _not_searched(films, shows)
        films, shows = films[:FIND_LIMIT], shows[:FIND_LIMIT]
        for movie in films:
            self._films.keep(movie.id, movie)
        for show in shows:
            self._shows.keep(show.id, show)
        results = [*await self._film_items(films, person), *await self._series_items(shows, person)]
        log.info("Phone find: %d films and %d series found", len(films), len(shows))
        if not results:
            message = t("Nothing was found by that name, so try other words.")
            return Reply(200, {"results": [], "message": message})
        return Reply(200, {"results": results})

    async def _film_items(self, films: list[Movie], person: int) -> list[dict[str, Any]]:
        if not films:
            return []
        now = self._now()
        details = await details_of(self._discovery.movie_details, [movie.id for movie in films])
        plex = await self._library.plex_films()
        listed = {entry.movie_id for entry in await self._watchlist.by_release_of(person)}
        items = []
        for movie in films:
            found = _found_film(movie, details.get(movie.id), now)
            state = await self._film_state(found, plex, listed)
            items.append(
                _item(
                    "movie",
                    movie.id,
                    movie.title,
                    movie.year,
                    movie.poster_url(_POSTER_SIZE),
                    movie.overview,
                    state,
                    found.out,
                )
            )
        return items

    async def _film_state(
        self, found: FoundFilm, plex: PlexFilms, listed: set[int]
    ) -> FilmState:
        movie_id = found.movie.id
        if plex.has(movie_id, found.imdb_id):
            return "in_library"
        target = GrabTarget(media_type=MediaType.MOVIE, media_id=movie_id)
        if await self._downloads.has_grabbed(target):
            return "downloading"
        return "on_list" if movie_id in listed else "new"

    async def _series_items(self, shows: list[DiscoverShow], person: int) -> list[dict[str, Any]]:
        if not shows:
            return []
        today = self._now().date()
        plex = await self._library.plex_shows()
        in_plex = await self._plex_shows()
        details = (  # the outside ids take a call per show, cached by Discovery
            await details_of(self._discovery.tv_details, [show.id for show in shows])
            if plex.tvdb or plex.imdb or in_plex
            else {}
        )
        followed = await self._discovery.tracked_matcher(person)
        items = []
        for show in shows:
            ids = details.get(show.id)
            tvdb_id, imdb_id = (ids.tvdb_id, ids.imdb_id) if ids is not None else (None, None)
            state: SeriesState
            if (
                plex.has(tvdb_id, imdb_id)
                or any(_same_show(title, show.id, tvdb_id, imdb_id) for title in in_plex)
                or await self._in_folders(show.id)
            ):
                state = "in_library"
            else:
                state = "following" if followed(show) else "new"
            out = show.first_air_date is None or show.first_air_date <= today
            items.append(
                _item(
                    "series",
                    show.id,
                    show.name,
                    show.year,
                    show.poster_url(_POSTER_SIZE),
                    show.overview,
                    state,
                    out,
                )
            )
        return items

    # -- what's had of a series ------------------------------------------------------------------

    async def _plex_shows(self) -> list[PlexTitle]:
        """Plex's shows as the last check stored them (all of them, known here or not); none
        without Plex."""
        if not plex_connected(self._settings.current):
            return []
        return [title for title in await self._plex.titles() if title.kind == "show"]

    async def _in_folders(self, tmdb_id: int) -> bool:
        if not folders_on(self._settings.current):
            return False
        return bool(await self._files.of_title("episode", tmdb_id))

    async def _had(self, tmdb_id: int, details: ShowDetails) -> frozenset[tuple[int, int]]:
        """(season, number) of the series' episodes had: the cached episodes' marks, Plex's own
        list of that show's episodes, and the folders' files of its TMDB id."""
        cached = await self._library.had_episodes(details.show.id)
        had = {
            (episode.season, episode.number)
            for episode in details.episodes
            if episode.id in cached and episode.number is not None
        }
        had |= await self._plex_episodes(tmdb_id, details.show)
        if folders_on(self._settings.current):
            had |= _file_episodes(await self._files.of_title("episode", tmdb_id))
        return frozenset(had)

    async def _plex_episodes(self, tmdb_id: int, show: Show) -> set[tuple[int, int]]:
        """What Plex has of the series now, read from Plex itself; nothing when it doesn't hold
        the show or doesn't answer in time."""
        titles = await self._plex_shows()
        if not titles:
            return set()
        ids = (await details_of(self._discovery.tv_details, [tmdb_id])).get(tmdb_id)
        tvdb_id = show.tvdb_id or (ids.tvdb_id if ids is not None else None)
        imdb_id = show.imdb_id or (ids.imdb_id if ids is not None else None)
        title = next(
            (
                one
                for one in titles
                if one.show_id == show.id or _same_show(one, tmdb_id, tvdb_id, imdb_id)
            ),
            None,
        )
        if title is None:
            return set()
        try:
            episodes = await self._plex.episodes(title)
        except PlexError as exc:
            log.info("Phone find: Plex's episodes not read: %s", type(exc).__name__)
            return set()
        return {
            (episode.season, episode.episode)
            for episode in episodes
            if episode.season is not None and episode.episode is not None
        }

    # -- what was found, for the other routes ----------------------------------------------------

    async def film(self, movie_id: int) -> FoundFilm | None:
        """A film found in this run (its release dates from TMDB, cached by Discovery); None
        when it wasn't found."""
        movie = self._films.get(movie_id)
        if movie is None:
            return None
        details = await details_of(self._discovery.movie_details, [movie_id])
        return _found_film(movie, details.get(movie_id), self._now())

    async def series(self, tmdb_id: int, person: int) -> ShowDetails | Reply:
        """A series found in this run or offered by the suggestions: its TVMaze counterpart with
        its episodes, as the person sees it (`show_details`). A Reply when it wasn't found (410)
        or can't be found out just now (502)."""
        show = await self._show(tmdb_id, person)
        if isinstance(show, Reply):
            return show
        match = await self._counterpart(show, cant_find)
        if isinstance(match, Reply):
            return match
        try:
            return await self._library.show_details(match.id, profile_id=person, now=self._now())
        except TVMazeError as exc:
            log.info("Phone find: no episodes from TVMaze: %s", type(exc).__name__)
            return no_lookup(show.name)

    async def _show(self, tmdb_id: int, person: int) -> DiscoverShow | Reply:
        found = self._shows.get(tmdb_id)
        if found is not None:
            return found
        offered = await self._suggestions.show(tmdb_id, person)
        return search_again() if offered is None else offered

    async def _counterpart(
        self, show: DiscoverShow, no_match: Callable[[str], Reply]
    ) -> Show | Reply:
        try:
            return await self._discovery.resolve(show)
        except NoTVMazeMatch:
            return no_match(show.name)
        except (TMDBError, TVMazeError) as exc:
            log.info("Phone find: no TVMaze counterpart just now: %s", type(exc).__name__)
            return no_lookup(show.name)

    # -- a series' episodes ----------------------------------------------------------------------

    async def _episodes(self, request: Request) -> Reply:
        problem = person_gone(request.phone, self._profiles)
        if problem is not None:
            return problem
        assert request.phone is not None
        raw = request.query.get("id", "")
        if not (raw.isascii() and raw.isdigit()) or len(raw) > _ID_DIGITS:
            return bad_request()
        tmdb_id = int(raw)
        details = await self.series(tmdb_id, request.phone.profile_id)
        if isinstance(details, Reply):
            return details
        had = await self._had(tmdb_id, details)
        now = self._now()
        seasons: dict[int, list[dict[str, Any]]] = {}
        for episode in sorted(
            (one for one in details.episodes if one.number is not None),
            key=lambda one: (one.season, one.number or 0),
        ):
            state: EpisodeState
            if (episode.season, episode.number) in had:
                state = "in_library"
            elif details.is_downloaded(episode):
                state = "downloading"
            else:
                state = "new"
            seasons.setdefault(episode.season, []).append(
                {
                    "number": episode.number,
                    "name": episode.name,
                    "out": episode.has_aired(now),
                    "state": state,
                }
            )
        log.info("Phone find: a series' episodes, %d seasons", len(seasons))
        return Reply(
            200,
            {
                "following": details.tracked,
                "seasons": [
                    {
                        "number": number,
                        "out": any(one["out"] for one in episodes),
                        "episodes": episodes,
                    }
                    for number, episodes in sorted(seasons.items())
                ],
            },
        )

    # -- following -------------------------------------------------------------------------------

    async def _follow(self, request: Request) -> Reply:
        problem = person_gone(request.phone, self._profiles)
        if problem is not None:
            return problem
        assert request.phone is not None
        person = request.phone.profile_id
        tmdb_id = request.body.get("id")
        if not _whole_number(tmdb_id):
            return bad_request()
        show = await self._show(tmdb_id, person)
        if isinstance(show, Reply):
            return show
        match = await self._counterpart(show, _cant_follow)
        if isinstance(match, Reply):
            return match
        if match.id in await self._library.tracked_ids(profile_id=person):
            log.info("Phone find: a series followed already")
            message = t("You already follow {title}.", title=show.name)
            return Reply(200, {"outcome": "already", "message": message})
        try:
            await self._discovery.track(show, profile_id=person, match=match)
        except NoTVMazeMatch:
            return _cant_follow(show.name)
        except (TMDBError, TVMazeError) as exc:
            log.info("Phone find: a series not followed: %s", type(exc).__name__)
            return no_lookup(show.name)
        log.info("Phone find: a series followed")
        message = t("You follow {title} now.", title=show.name)
        return Reply(200, {"outcome": "following", "message": message})


def _not_searched(*results: object) -> Reply:
    """The answer when TMDB couldn't be asked: no key, or out of reach."""
    failures = [result for result in results if isinstance(result, BaseException)]
    for failure in failures:
        if not isinstance(failure, TMDBError):
            raise failure
    if any(isinstance(failure, TMDBNotConfigured) for failure in failures):
        return _no_key()
    if any(isinstance(failure, TMDBAuthError) for failure in failures):
        return _key_refused()
    log.info("Phone find: TMDB couldn't be asked: %s", type(failures[0]).__name__)
    message = t("AirGrabber couldn't search just now, so try again in a moment.")
    return error_reply(502, "failed", message)
