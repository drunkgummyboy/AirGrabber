"""TMDB discovery: TV and movie dashboards, cached details, the info window's extras (the details
in the app's language, the trailer and the cast), and tracking TMDB shows via TVMaze.

Everything is cached in memory only: TMDB's lists for DASHBOARD_TTL, details and extras for
DETAILS_TTL; but the digital shelves' first digital release dates, which are also kept a day
across starts (app/services/release_dates.py). A film's info window takes one TMDB request
(`TMDBClient.film_page`) for its extras and details. A TMDB show is tracked through its TVMaze
counterpart, found by IMDb id, then TheTVDB id, then by name + first-air year; a TVMaze show's
TMDB counterpart (for its extras) is the one it was matched from, else TMDB's by its IMDb id.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import unicodedata
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Generic, TypeVar

from app.i18n import is_dutch, t
from app.models import (
    CastMember,
    DiscoverShow,
    DiscoverShowEntry,
    MediaType,
    Movie,
    MovieDashboard,
    MovieDetails,
    MoviePage,
    Show,
    TitleExtras,
    TitleInfo,
    TVDashboard,
    TVDetails,
    Video,
)
from app.models.person import Person, PersonMatch
from app.models.plex import PlexFilms, PlexShows
from app.services.library import LibraryService
from app.services.release_dates import ReleaseDates
from app.services.tmdb import (
    MAX_PAGE,
    ResultPage,
    TMDBAuthError,
    TMDBClient,
    TMDBError,
    TMDBNotConfigured,
)
from app.services.tvmaze import TVMazeClient

log = logging.getLogger(__name__)

T = TypeVar("T")

Clock = Callable[[], datetime]

DASHBOARD_TTL = timedelta(minutes=30)
DETAILS_TTL = timedelta(hours=24)
DETAILS_KEPT = 2000  # per kind of details: the oldest stored go first (the phone's search feeds them)
SHELF_SIZE = 18
BECAUSE_FROM = 5  # "Because you follow …" picks one of the shows followed last, this many
PREMIERES_AHEAD = timedelta(days=90)
JUST_DROPPED_SINCE = timedelta(days=14)
UPCOMING_AHEAD = timedelta(days=21)
# Pages of TMDB's digital-release lists fetched per shelf: re-releases are filtered out
# afterwards, so one page (20 films) could leave a shelf short.
DIGITAL_PAGES = 2
# The movie dashboard's shelves, by name (MovieDashboard's fields, `more_movies`' `shelf`).
MOVIE_SHELVES = ("just_dropped", "trending", "upcoming")
# The movie dashboard's last fetches whose See more state is kept (`MovieDashboard.listing`):
# the cached one and the ones after it that weren't cached (a shelf failed) or are newer.
PAGINGS_KEPT = 4
# TVMaze search hits checked for a name + year match when no external id resolves.
TRACK_SEARCH_LIMIT = 5
CAST_LIMIT = 8  # the info window's cast row
ENGLISH = "en-US"  # TMDB's language for what the app's own language has no text for

_IGNORED_WORDS = frozenset({"the", "and", "&"})


def _optional(
    result: list[CastMember] | BaseException, kind: MediaType, item_id: int, what: str
) -> list[CastMember]:
    """A gathered extra, or none when it failed with a TMDBError that concerns only it
    (logged); a missing or rejected key, cancellation and bugs propagate."""
    if not isinstance(result, BaseException):
        return result
    if _is_fatal(result):
        raise result
    log.info("TMDB %s %d: no %s: %s", kind.value, item_id, what, result)
    return []


async def _optional_call(
    call: Awaitable[T], kind: MediaType, item_id: int, what: str
) -> T | None:
    """`call`'s result, or None when it fails with a TMDBError that concerns only it."""
    try:
        return await call
    except TMDBError as exc:
        if _is_fatal(exc):
            raise
        log.info("TMDB %s %d: no %s: %s", kind.value, item_id, what, exc)
        return None


class NoTVMazeMatch(Exception):
    """A TMDB show has no TVMaze counterpart we could identify."""


def normalize_title(name: str) -> str:
    """A show name reduced for loose comparison between TMDB and TVMaze.

    Casefolded; punctuation dropped (so "Mandy's" and "Mandys" agree); the words "the", "and"
    and "&" dropped; whitespace collapsed. "The Office" and "Office" both become "office".
    """
    text = "".join(ch for ch in name.casefold() if not unicodedata.category(ch).startswith("P"))
    return " ".join(word for word in text.split() if word not in _IGNORED_WORDS)


def _is_fatal(exc: BaseException) -> bool:
    """True for errors that must not be confined to one shelf or one film: a missing or rejected
    TMDB key (nothing else can load either), cancellation, and bugs."""
    return not isinstance(exc, TMDBError) or isinstance(exc, (TMDBNotConfigured, TMDBAuthError))


def _first_failure(results: Iterable[object]) -> BaseException | None:
    """The first fatal error among gathered results, else the first error, else None."""
    errors = [result for result in results if isinstance(result, BaseException)]
    return next((exc for exc in errors if _is_fatal(exc)), errors[0] if errors else None)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def tmdb_language() -> str:
    """The language the info window asks TMDB for: the app's ("nl-NL" while Dutch is on)."""
    return "nl-NL" if is_dutch() else ENGLISH


def trailer_languages() -> tuple[str, ...]:
    """The trailer languages preferred, best first (ISO 639-1): Dutch, then English while
    Dutch is on; else English."""
    return ("nl", "en") if is_dutch() else ("en",)


def pick_trailer(videos: Iterable[Video], languages: Sequence[str]) -> Video | None:
    """The trailer the info window offers: a YouTube video of type Trailer; official ones
    first, then by language (`languages` best first, then any other), then the newest. None
    when there is no such video."""
    candidates = [
        video
        for video in videos
        if video.site.casefold() == "youtube" and video.type.casefold() == "trailer"
    ]

    def rank(video: Video) -> tuple[bool, int, float]:
        language = (video.language or "").casefold()
        by_language = languages.index(language) if language in languages else len(languages)
        published = video.published_at.timestamp() if video.published_at is not None else 0.0
        return (not video.official, by_language, -published)

    return min(candidates, key=rank, default=None)


def _is_fresh(stored_at: datetime, now: datetime, ttl: timedelta) -> bool:
    # A clock that went backwards makes the entry stale rather than fresh for longer.
    return timedelta(0) <= now - stored_at < ttl


@dataclass(frozen=True, slots=True)
class _Entry(Generic[T]):
    value: T
    stored_at: datetime


@dataclass(frozen=True, slots=True)
class _Film:
    """A film's info window's TMDB request, made once for both."""

    details: MovieDetails
    extras: TitleExtras


@dataclass(frozen=True, slots=True)
class _Shelves(Generic[T]):
    """One dashboard's TMDB lists, by shelf name."""

    day: date  # the local day the date windows were computed for
    lists: Mapping[str, tuple[T, ...]]
    stored_at: datetime
    listing: int = 0  # the movie dashboard's: its See more state (`_MoviePaging`)


@dataclass(slots=True)
class _MoviePaging:
    """See more on the movie shelves, for the lists the last TMDB fetch of the dashboard gave
    (a fresh fetch starts over): its day (the date windows), the films it fetched beyond
    SHELF_SIZE (the start of page 2), the shelves whose TMDB list goes on after it, and the
    pages fetched since, by (shelf, page): their films and whether the list goes on."""

    day: date
    spare: Mapping[str, tuple[Movie, ...]] = field(default_factory=dict)
    goes_on: frozenset[str] = frozenset()
    pages: dict[tuple[str, int], tuple[tuple[Movie, ...], bool]] = field(default_factory=dict)

    @property
    def more(self) -> frozenset[str]:
        """The shelves with films after the dashboard's."""
        return self.goes_on | {name for name, films in self.spare.items() if films}


async def _as_tuple(items: Awaitable[Iterable[T]]) -> tuple[T, ...]:
    return tuple(await items)


class _DetailsCache(Generic[T]):
    """Details by id, kept for `ttl` (the newest DETAILS_KEPT); concurrent requests for one id
    share a single fetch.

    Failures are not cached. A fetch is cancelled once no caller waits for it any more.
    """

    def __init__(
        self, label: str, fetch: Callable[[int], Awaitable[T]], ttl: timedelta, clock: Clock
    ) -> None:
        self._label = label
        self._fetch = fetch
        self._ttl = ttl
        self._clock = clock
        self._entries: dict[int, _Entry[T]] = {}
        self._in_flight: dict[int, asyncio.Task[T]] = {}
        self._waiters: Counter[asyncio.Task[T]] = Counter()

    async def get(self, item_id: int) -> T:
        entry = self._entries.get(item_id)
        if entry is not None and _is_fresh(entry.stored_at, self._clock(), self._ttl):
            return entry.value
        task = self._in_flight.get(item_id)
        if task is None or task.cancelling():
            task = asyncio.create_task(self._load(item_id), name=f"{self._label}-{item_id}")
            self._in_flight[item_id] = task
            task.add_done_callback(functools.partial(self._forget, item_id))
        return await self._join(task)

    def has(self, item_id: int) -> bool:
        """Whether a `get` now would need no new fetch: a fresh entry, or one on its way."""
        entry = self._entries.get(item_id)
        if entry is not None and _is_fresh(entry.stored_at, self._clock(), self._ttl):
            return True
        task = self._in_flight.get(item_id)
        return task is not None and not task.cancelling()

    def stored_at(self, item_id: int) -> datetime | None:
        """When the fresh entry for `item_id` was fetched; None without one."""
        entry = self._entries.get(item_id)
        if entry is None or not _is_fresh(entry.stored_at, self._clock(), self._ttl):
            return None
        return entry.stored_at

    def put(self, item_id: int, value: T) -> None:
        """Keep `value` for `item_id` as a fetch would (a fetch on its way still stores its own)."""
        self._store(item_id, value)

    def _store(self, item_id: int, value: T) -> None:
        self._entries.pop(item_id, None)  # stored again: the newest
        self._entries[item_id] = _Entry(value, self._clock())
        while len(self._entries) > DETAILS_KEPT:
            del self._entries[next(iter(self._entries))]

    def clear(self) -> None:
        """Forget every entry. Fetches still in flight finish for their callers but store
        nothing, and later calls start new ones."""
        self._entries.clear()
        self._in_flight.clear()

    async def _load(self, item_id: int) -> T:
        value = await self._fetch(item_id)
        if self._in_flight.get(item_id) is asyncio.current_task():  # not cleared meanwhile
            self._store(item_id, value)
        return value

    def _forget(self, item_id: int, task: asyncio.Task[T]) -> None:
        if self._in_flight.get(item_id) is task:
            del self._in_flight[item_id]

    async def _join(self, task: asyncio.Task[T]) -> T:
        """Wait for a fetch other calls may share; cancel it when this call was its last waiter
        and stops waiting early (its own cancellation)."""
        self._waiters[task] += 1
        try:
            await asyncio.wait([task])
        finally:
            self._waiters[task] -= 1
            if self._waiters[task] <= 0:
                del self._waiters[task]
                if not task.done():
                    task.cancel()
                    await asyncio.wait([task])
            if task.done() and not task.cancelled():
                task.exception()  # mark a failure as retrieved even if nobody re-raises it
        return task.result()


class DiscoveryService:
    def __init__(
        self,
        tmdb: TMDBClient,
        tvmaze: TVMazeClient,
        library: LibraryService,
        *,
        clock: Clock = _utc_now,
        release_dates: Path | None = None,
    ) -> None:
        """`release_dates`: the file the digital shelves' dates are kept in across starts
        (app/services/release_dates.py); None keeps them for this run only."""
        self._tmdb = tmdb
        self._tvmaze = tvmaze
        self._library = library
        self._clock = clock
        self._tv_shelves: _Shelves[DiscoverShow] | None = None
        self._movie_shelves: _Shelves[Movie] | None = None
        # See more's state per fetch of the movie dashboard's lists (`MovieDashboard.listing`).
        self._movie_pagings: dict[int, _MoviePaging] = {}
        self._listings = 0
        # Bumped by clear_cache(), so dashboards fetched across a clear aren't stored.
        self._generation = 0
        self._movie_details = _DetailsCache("tmdb-movie", tmdb.movie_details, DETAILS_TTL, clock)
        # Not cleared by clear_cache: a film's release dates don't depend on the key.
        self._release_dates = ReleaseDates(release_dates, clock)
        self._season_trailers: dict[tuple[int, int], _Entry[Video | None]] = {}
        self._tv_details = _DetailsCache("tmdb-tv", tmdb.tv_details, DETAILS_TTL, clock)
        # The info window's extras by TMDB id (in the app's language, which is fixed per run).
        self._movies = _DetailsCache("tmdb-movie-item", tmdb.movie, DETAILS_TTL, clock)
        self._film_series = _DetailsCache(
            "tmdb-film-series", lambda id: _as_tuple(tmdb.collection_of(id)), DETAILS_TTL, clock
        )
        self._shows = _DetailsCache("tmdb-tv-item", lambda id: tmdb.tv(id), DETAILS_TTL, clock)
        # A film's info window: its extras and its details from one TMDB request.
        self._films = _DetailsCache("tmdb-film", self._fetch_film, DETAILS_TTL, clock)
        self._tv_extras = _DetailsCache("tmdb-tv-extras", self._fetch_tv_extras, DETAILS_TTL, clock)
        # TVMaze id → its TMDB id by IMDb id (None: TMDB knows none), for a show's extras.
        self._tmdb_ids: dict[int, _Entry[int | None]] = {}
        self._genre_names: dict[MediaType, _Entry[Mapping[int, str]]] = {}
        # TMDB id → TVMaze id of every show matched through resolve() (and so track()): flags
        # it as tracked even when the two services spell its name differently.
        self._tracked_via_tmdb: dict[int, int] = {}
        # TMDB id → TMDB's picks for people who like that show ("Because you follow …").
        self._recommended: dict[int, _Entry[tuple[DiscoverShow, ...]]] = {}

    def clear_cache(self) -> None:
        """Forget every cached TMDB list and details (e.g. after the API key changed)."""
        self._tv_shelves = None
        self._movie_shelves = None
        self._movie_pagings.clear()
        self._generation += 1
        self._movie_details.clear()
        self._tv_details.clear()
        self._season_trailers.clear()
        self._movies.clear()
        self._film_series.clear()
        self._shows.clear()
        self._films.clear()
        self._tv_extras.clear()
        self._tmdb_ids.clear()
        self._genre_names.clear()
        self._recommended.clear()

    # -- dashboards ----------------------------------------------------------------------------

    async def tv_dashboard(self, *, profile_id: int, force: bool = False) -> TVDashboard:
        """Trending shows, shows on the air, and premieres in the next 90 days; 18 at most each.

        TMDB's lists are cached for DASHBOARD_TTL and for the current local day (`force`
        refetches them); the tracked flags (the shows the person follows) are recomputed from
        the library on every call. A
        fetch with failed shelves is returned but not cached (nor does it evict the previous
        complete one). See `_load_shelves` for errors.
        """
        today = self._today()
        lists = None if force else self._fresh(self._tv_shelves, today)
        failed: tuple[str, ...] = ()
        if lists is None:
            generation = self._generation
            fetchers: dict[str, Callable[[], Awaitable[list[DiscoverShow]]]] = {
                "trending": self._tmdb.trending_tv,
                "on_air": self._tmdb.tv_on_the_air,
                "premieres": lambda: self._tmdb.tv_premieres(today, today + PREMIERES_AHEAD),
            }
            lists, failed = await self._load_shelves("TV", fetchers)
            if not failed and generation == self._generation:
                self._tv_shelves = _Shelves(today, lists, self._clock())
        is_tracked = await self.tracked_matcher(profile_id)
        shelves = {
            name: tuple(DiscoverShowEntry(show=show, is_tracked=is_tracked(show)) for show in shows)
            for name, shows in lists.items()
        }
        because_name, because = await self._because(profile_id, is_tracked)
        return TVDashboard(
            trending=shelves.get("trending", ()),
            on_air=shelves.get("on_air", ()),
            premieres=shelves.get("premieres", ()),
            because_name=because_name,
            because=because,
            failed=failed,
        )

    async def _because(
        self, profile_id: int, is_tracked: Callable[[DiscoverShow], bool]
    ) -> tuple[str, tuple[DiscoverShowEntry, ...]]:
        """TMDB's picks for one of the BECAUSE_FROM shows the person followed last, another one
        each day; shows they follow left out. ("", ()) when there is none, or TMDB can't say."""
        entries = sorted(
            await self._library.tracked(profile_id=profile_id),
            key=lambda entry: entry.tracked_at,
            reverse=True,
        )[:BECAUSE_FROM]
        if not entries:
            return "", ()
        start = self._today().toordinal() % len(entries)
        for entry in entries[start:] + entries[:start]:
            try:
                tmdb_id = await self._tmdb_id_of(entry.show)
                if tmdb_id is None:
                    continue
                picks = await self._recommendations(tmdb_id)
            except TMDBError as exc:
                log.info("Discover: no picks for TVMaze show %d (%s)", entry.show.id, type(exc).__name__)
                return "", ()
            shown = tuple(
                DiscoverShowEntry(show=show, is_tracked=False)
                for show in picks
                if not is_tracked(show)
            )[:SHELF_SIZE]
            if shown:
                return entry.show.name, shown
        return "", ()

    async def _recommendations(self, tmdb_id: int) -> tuple[DiscoverShow, ...]:
        cached = self._recommended.get(tmdb_id)
        if cached is not None and _is_fresh(cached.stored_at, self._clock(), DASHBOARD_TTL):
            return cached.value
        generation = self._generation
        picks = tuple(await self._tmdb.tv_recommendations(tmdb_id))
        if generation == self._generation:
            self._recommended[tmdb_id] = _Entry(picks, self._clock())
        return picks

    async def plex_films(self) -> PlexFilms:
        """The films had, in Plex or in the folders (the film cards' library mark,
        app/ui/library_mark.py; `only_in_folders` beside Plex); none without a library."""
        return await self._library.plex_films()

    async def plex_shows(self) -> PlexShows:
        """The series had, in Plex or in the folders (Discover's show cards' library mark);
        none without a library."""
        return await self._library.plex_shows()

    async def movie_dashboard(
        self, *, force: bool = False, also: Iterable[int] = ()
    ) -> MovieDashboard:
        """Films first released digitally in the last 14 days, trending films, and films first
        released digitally in the next 21 days; 18 at most each.

        The digital shelves are filtered on each film's details (see `_first_digital_releases`).
        Cached like `tv_dashboard`, filtered; whether there is a library and the shelves'
        films had (`in_plex`, the main source's; `in_folders`, beside Plex the folders' own;
        none without a library) are recomputed on every call, as the TV dashboard's tracked
        flags are; `also`: more films (TMDB ids) to say that of (the ones See more added).
        `more`: the shelves See more can go on with; `listing`: these lists' fetch, which
        `more_movies` goes on from (the cached lists' own, also after a newer fetch).
        """
        today = self._today()
        cached = self._movie_shelves
        lists = None if force else self._fresh(cached, today)
        listing = cached.listing if lists is not None and cached is not None else 0
        failed: tuple[str, ...] = ()
        if lists is None:
            generation = self._generation
            fetched: dict[str, tuple[list[Movie], bool]] = {}

            def shelf(
                name: str, fetch: Callable[[], Awaitable[tuple[list[Movie], bool]]]
            ) -> Callable[[], Awaitable[list[Movie]]]:
                async def run() -> list[Movie]:
                    fetched[name] = await fetch()
                    return fetched[name][0]

                return run

            fetchers: dict[str, Callable[[], Awaitable[list[Movie]]]] = {
                "just_dropped": shelf(
                    "just_dropped",
                    lambda: self._first_digital_releases(*self._window("just_dropped", today)),
                ),
                "trending": shelf("trending", self._trending_first),
                "upcoming": shelf(
                    "upcoming",
                    lambda: self._first_digital_releases(*self._window("upcoming", today)),
                ),
            }
            lists, failed = await self._load_shelves("Movie", fetchers)
            self._listings += 1
            listing = self._listings
            paging = _MoviePaging(
                today,
                spare={name: tuple(fetched[name][0][SHELF_SIZE:]) for name in lists},
                goes_on=frozenset(name for name in lists if fetched[name][1]),
            )
            if generation == self._generation:
                self._keep_paging(listing, paging)
                if not failed:
                    self._movie_shelves = _Shelves(today, lists, self._clock(), listing)
        paging_of = self._movie_pagings.get(listing)
        on_shelves = {movie.id for movies in lists.values() for movie in movies}
        in_plex, in_folders = await self._films_had(on_shelves | set(also))
        return MovieDashboard(
            just_dropped=lists.get("just_dropped", ()),
            trending=lists.get("trending", ()),
            upcoming=lists.get("upcoming", ()),
            failed=failed,
            plex_connected=self._library.has_library,
            in_plex=in_plex,
            in_folders=in_folders,
            more=paging_of.more & set(lists) if paging_of is not None else frozenset(),
            listing=listing,
        )

    def _keep_paging(self, listing: int, paging: _MoviePaging) -> None:
        """Keep a fetch's See more state: the cached lists' and the PAGINGS_KEPT newest."""
        self._movie_pagings[listing] = paging
        cached = self._movie_shelves.listing if self._movie_shelves is not None else 0
        for old in sorted(self._movie_pagings)[:-PAGINGS_KEPT]:
            if old != cached:
                del self._movie_pagings[old]

    async def more_movies(self, shelf: str, page: int, *, listing: int = 0) -> MoviePage:
        """Page `page` (2, 3, …) of a movie shelf: the films after the dashboard's (the Movies
        page's See more), in TMDB's order, not checked against what the shelf shows already.

        `listing`: the dashboard's (`MovieDashboard.listing`), whose fetch it goes on from: its
        date windows, its films beyond SHELF_SIZE (page 2 starts with them) and the pages
        asked since (asked again, a page comes from memory). A listing no longer kept (or 0)
        goes on from today's windows, without those films.

        Trending: TMDB's page `page`. The digital shelves: DIGITAL_PAGES of TMDB's pages per
        page (page 2: TMDB's 3 and 4), filtered as the dashboard's are. `more`: TMDB's list
        goes on after it. The films had as in `movie_dashboard`. TMDB errors propagate.
        """
        if shelf not in MOVIE_SHELVES:
            raise ValueError(f"no movie shelf {shelf!r}")
        if page < 2:
            raise ValueError("See more starts at page 2")
        paging = self._movie_pagings.get(listing)
        if paging is None:  # not kept (or cleared): today's windows, nothing beyond 18
            paging = _MoviePaging(self._today(), goes_on=frozenset(MOVIE_SHELVES))
            self._keep_paging(listing, paging)
        found = paging.pages.get((shelf, page))
        if found is None:
            generation = self._generation
            spare = paging.spare.get(shelf, ()) if page == 2 else ()
            if page == 2 and shelf not in paging.goes_on:
                films: list[Movie] = []
                goes_on = False  # TMDB's list ended with the dashboard's pages: the spare only
            else:
                films, goes_on = await self._next_films(shelf, page, paging.day)
            found = ((*spare, *films), goes_on)
            if generation == self._generation and self._movie_pagings.get(listing) is paging:
                paging.pages[(shelf, page)] = found
        movies, goes_on = found
        in_plex, in_folders = await self._films_had(movie.id for movie in movies)
        return MoviePage(movies=movies, more=goes_on, in_plex=in_plex, in_folders=in_folders)

    async def _films_had(self, ids: Iterable[int]) -> tuple[frozenset[int], frozenset[int]]:
        """Of the films `ids`, the ones had: (the main source's, beside Plex the folders' own)."""
        plex = await self._library.plex_films()
        had = {movie_id for movie_id in ids if plex.has(movie_id)}
        in_folders = frozenset(movie_id for movie_id in had if plex.only_in_folders(movie_id))
        return frozenset(had - in_folders), in_folders

    def _window(self, shelf: str, day: date) -> tuple[date, date]:
        """A digital shelf's date window, for the local day `day`."""
        if shelf == "just_dropped":
            return day - JUST_DROPPED_SINCE, day
        return day + timedelta(days=1), day + UPCOMING_AHEAD

    async def _trending_first(self) -> tuple[list[Movie], bool]:
        """The trending shelf's first page, and whether TMDB has more (its `total_pages`)."""
        result = await self._tmdb.trending_movies_page(1)
        return list(result.items), not result.is_last

    async def _next_films(self, shelf: str, page: int, day: date) -> tuple[list[Movie], bool]:
        """`more_movies`' TMDB request(s) for a shelf's page `page` (2 or later), and whether
        the list goes on after them."""
        if shelf == "trending":
            if page > MAX_PAGE:
                return [], False
            result = await self._tmdb.trending_movies_page(page)
            return list(result.items), not result.is_last
        start, end = self._window(shelf, day)
        first = (page - 1) * DIGITAL_PAGES + 1
        numbers = range(first, min(first + DIGITAL_PAGES, MAX_PAGE + 1))
        if not numbers:
            return [], False
        results = await asyncio.gather(
            *(self._tmdb.digital_releases_page(start, end, page=n) for n in numbers),
            return_exceptions=True,
        )
        if (error := _first_failure(results)) is not None:
            raise error
        pages = [result for result in results if isinstance(result, ResultPage)]
        films = await self._first_digital_only([result.items for result in pages], start, end)
        return films, not any(result.is_last for result in pages)

    async def _first_digital_releases(self, start: date, end: date) -> tuple[list[Movie], bool]:
        """Films whose FIRST digital release in the region falls in [start, end], most popular
        first, and whether TMDB's list goes on after them (its `total_pages`).

        TMDB lists a film when any of its digital releases falls in the window, so re-releases
        show up there too. The first DIGITAL_PAGES pages are fetched and filtered
        (`_first_digital_only`). A failing page fails the shelf; a missing or rejected key
        propagates, also from the details.
        """
        results = await asyncio.gather(
            *(
                self._tmdb.digital_releases_page(start, end, page=n)
                for n in range(1, DIGITAL_PAGES + 1)
            ),
            return_exceptions=True,
        )
        if (error := _first_failure(results)) is not None:
            raise error
        pages = [result for result in results if isinstance(result, ResultPage)]
        films = await self._first_digital_only([page.items for page in pages], start, end)
        return films, bool(pages) and not any(page.is_last for page in pages)

    async def _first_digital_only(
        self, pages: Sequence[Sequence[Movie]], start: date, end: date
    ) -> list[Movie]:
        """The films of TMDB's digital-release `pages` (in order, each once) whose first
        digital release falls in [start, end] or isn't known.

        Every film's first digital release decides: a film is dropped only when it is known and
        outside the window. Films without a known digital date, or whose details fail to load,
        are kept. The dates come from the films' details (through the shared details cache)
        and are kept a day, across starts too (`ReleaseDates`). A missing or rejected key
        propagates from the details.
        """
        await self._release_dates.load()
        candidates: dict[int, Movie] = {}
        for movie in (movie for page in pages for movie in page):
            candidates.setdefault(movie.id, movie)  # popularity can shift a film to page 2
        keep = await asyncio.gather(
            *(self._first_digital_in(movie, start, end) for movie in candidates.values()),
            return_exceptions=True,
        )
        await self._release_dates.save()
        if (error := _first_failure(keep)) is not None:
            raise error  # only fatal errors get here: _first_digital_in keeps the film otherwise
        return [movie for movie, kept in zip(candidates.values(), keep, strict=True) if kept is True]

    async def _first_digital_in(self, movie: Movie, start: date, end: date) -> bool:
        """False only when the film's first digital release is known and outside [start, end]."""
        known = self._release_dates.lookup(movie.id)
        if known is not None:
            first = known.digital
        else:
            try:
                details = await self.movie_details(movie.id)
            except TMDBError as exc:
                if _is_fatal(exc):
                    raise
                log.debug(
                    "Keeping %s (TMDB id %d) unfiltered; its details are unavailable: %s",
                    movie.title, movie.id, exc,
                )
                return True
            first = details.digital_release
            # Dated by when the details were fetched: a date lives about a day, never two.
            read_at = self._movie_details.stored_at(movie.id)
            self._release_dates.remember(movie.id, first, read_at)
        return first is None or start <= first <= end

    def _today(self) -> date:
        """The local calendar day."""
        return self._clock().astimezone().date()

    def _fresh(
        self, cached: _Shelves[T] | None, today: date
    ) -> Mapping[str, tuple[T, ...]] | None:
        """The cached lists if they are recent and were fetched for today's date windows."""
        if cached is None or cached.day != today:
            return None
        return cached.lists if _is_fresh(cached.stored_at, self._clock(), DASHBOARD_TTL) else None

    async def _load_shelves(
        self, kind: str, fetchers: Mapping[str, Callable[[], Awaitable[list[T]]]]
    ) -> tuple[dict[str, tuple[T, ...]], tuple[str, ...]]:
        """Fetch every shelf concurrently, each capped at SHELF_SIZE.

        Returns the shelves that loaded and the names of those that failed. TMDBNotConfigured
        and TMDBAuthError propagate (no shelf can load); any other TMDBError only empties its
        own shelf and is logged, unless every shelf failed: then the first error is raised.
        """
        names = list(fetchers)
        results = await asyncio.gather(
            *(fetch() for fetch in fetchers.values()), return_exceptions=True
        )
        if (error := _first_failure(results)) is not None and _is_fatal(error):
            raise error
        loaded: dict[str, tuple[T, ...]] = {}
        failures: list[tuple[str, TMDBError]] = []
        for name, result in zip(names, results, strict=True):
            if isinstance(result, TMDBError):
                failures.append((name, result))
            elif not isinstance(result, BaseException):
                loaded[name] = tuple(result[:SHELF_SIZE])
        for name, error in failures:
            log.warning("%s dashboard: the %s shelf could not be loaded: %s", kind, name, error)
        if not loaded and failures:
            raise failures[0][1]
        return loaded, tuple(name for name, _ in failures)

    async def tracked_matcher(self, profile_id: int) -> Callable[[DiscoverShow], bool]:
        """Flags a TMDB show as tracked when its TVMaze match (`resolve`) is one the person
        follows, or when its name matches such a show's (see normalize_title)."""
        entries = await self._library.tracked(profile_id=profile_id)
        tracked_ids = {entry.show.id for entry in entries}
        tracked_names = {key for entry in entries if (key := normalize_title(entry.show.name))}

        def is_tracked(show: DiscoverShow) -> bool:
            if self._tracked_via_tmdb.get(show.id) in tracked_ids:
                return True
            return normalize_title(show.name) in tracked_names

        return is_tracked

    # -- details and search --------------------------------------------------------------------

    async def movie_details(self, movie_id: int) -> MovieDetails:
        """TMDB's details of a film; cached for DETAILS_TTL, concurrent calls share one fetch.
        While the film's info window's request (`movie_extras`) is on its way, they come from
        that one."""
        if not self._movie_details.has(movie_id) and self._films.has(movie_id):
            return (await self._films.get(movie_id)).details
        return await self._movie_details.get(movie_id)

    async def tv_details(self, tv_id: int) -> TVDetails:
        """TMDB's details of a show; cached for DETAILS_TTL, concurrent calls share one fetch."""
        return await self._tv_details.get(tv_id)

    # -- the info window's extras --------------------------------------------------------------

    @property
    def has_tmdb_key(self) -> bool:
        """Whether a TMDB key is set (not whether TMDB accepts it)."""
        return self._tmdb.configured

    async def movie(self, movie_id: int) -> Movie:
        """A film by its TMDB id, as the cards know films (title, year, poster, overview,
        rating); cached for DETAILS_TTL. TMDB errors propagate."""
        return await self._movies.get(movie_id)

    async def film_series(self, movie_id: int) -> tuple[Movie, ...]:
        """The films of the collection (film series) a film belongs to, by release date; empty
        when it belongs to none. Cached for DETAILS_TTL. TMDB errors propagate."""
        return await self._film_series.get(movie_id)

    async def show(self, tv_id: int) -> DiscoverShow:
        """A show by its TMDB id, as the lists know shows; cached for DETAILS_TTL. TMDB errors
        propagate."""
        return await self._shows.get(tv_id)

    async def movie_extras(self, movie_id: int) -> TitleExtras:
        """A film's details in the app's language (the overview in English where TMDB has none
        in it), its trailer (`pick_trailer`) and its first CAST_LIMIT actors; cached for
        DETAILS_TTL, concurrent calls share one fetch. One TMDB request (`film_page`, which
        also brings `movie_details`' details), and a second only for the English overview.

        Raises TMDBNotConfigured without a key and TMDBAuthError for a rejected one; another
        TMDBError when the details can't be read (a broken trailer or cast part only leaves
        those out)."""
        return (await self._films.get(movie_id)).extras

    async def show_extras(self, show: Show) -> TitleExtras | None:
        """A TVMaze show's extras (see `movie_extras`) from its TMDB counterpart: the one it was
        matched from on Discover, else TMDB's show with its IMDb id. None when there is none.

        Raises TMDBNotConfigured without a key, whatever the show."""
        if not self._tmdb.configured:
            raise TMDBNotConfigured(t("TMDB API key is not set"))
        tmdb_id = await self._tmdb_id_of(show)
        return None if tmdb_id is None else await self._tv_extras.get(tmdb_id)

    async def season_trailer(self, show: Show, season: int) -> Video | None:
        """A TVMaze show's season's own trailer (`pick_trailer`), kept for DETAILS_TTL. None when
        it has none, or TMDB can't be asked: the show's own trailer then still stands."""
        if not self._tmdb.configured:
            return None
        try:
            tmdb_id = await self._tmdb_id_of(show)
            if tmdb_id is None:
                return None
            key = (tmdb_id, season)
            cached = self._season_trailers.get(key)
            if cached is not None and _is_fresh(cached.stored_at, self._clock(), DETAILS_TTL):
                return cached.value
            generation = self._generation
            preferred = trailer_languages()
            videos = await self._tmdb.season_videos(tmdb_id, season, languages=preferred)
        except TMDBError as exc:
            log.info("No season trailer for TVMaze show %d: %s", show.id, type(exc).__name__)
            return None
        trailer = pick_trailer(videos, preferred)
        if generation == self._generation:  # not cleared meanwhile
            self._season_trailers[key] = _Entry(trailer, self._clock())
        return trailer

    async def tv_extras(self, tv_id: int) -> TitleExtras:
        """A TMDB show's extras by its TMDB id (the spotlight band's trending shows); see
        `movie_extras`, whose errors it raises too. Shared with `show_extras`' cache."""
        return await self._tv_extras.get(tv_id)

    async def show_tmdb_id(self, show: Show) -> int | None:
        """A TVMaze show's TMDB id (`_tmdb_id_of`: the one it was matched from on Discover, else
        TMDB's show with its IMDb id, kept for DETAILS_TTL); None when there is none, without a
        key, or when TMDB can't say now. ▶ Play joins a show's marks in the user's own folders
        by it when neither Plex nor the folders name it."""
        if not self._tmdb.configured:
            return None
        try:
            return await self._tmdb_id_of(show)
        except TMDBError as exc:
            log.info("No TMDB id for TVMaze show %d: %s", show.id, type(exc).__name__)
            return None

    async def _tmdb_id_of(self, show: Show) -> int | None:
        for matched_tmdb_id, tvmaze_id in self._tracked_via_tmdb.items():
            if tvmaze_id == show.id:
                return matched_tmdb_id
        if not show.imdb_id:
            return None
        cached = self._tmdb_ids.get(show.id)
        if cached is not None and _is_fresh(cached.stored_at, self._clock(), DETAILS_TTL):
            return cached.value
        generation = self._generation
        tmdb_id = await self._tmdb.find_tv(show.imdb_id)
        if generation == self._generation:  # not cleared meanwhile
            self._tmdb_ids[show.id] = _Entry(tmdb_id, self._clock())
        return tmdb_id

    async def _fetch_film(self, movie_id: int) -> _Film:
        """One request for the details, the info, the cast and the videos (in the preferred
        languages); the overview again in English when the app's language has none, and the
        videos again with the film's own language when the preferred ones hold no trailer. The
        details are kept for `movie_details` too."""
        generation = self._generation
        language = tmdb_language()
        preferred = trailer_languages()
        page = await self._tmdb.film_page(movie_id, language=language, video_languages=preferred)
        if generation == self._generation:  # not cleared meanwhile
            self._movie_details.put(movie_id, page.details)
        info = await self._english_overview(MediaType.MOVIE, movie_id, page.info, language)
        trailer = pick_trailer(page.videos, preferred)
        own = page.info.original_language
        if trailer is None and own and own not in preferred:
            # None in the preferred languages: one more call, the film's own language too.
            videos = await _optional_call(
                self._tmdb.videos(MediaType.MOVIE, movie_id, languages=(*preferred, own)),
                MediaType.MOVIE, movie_id, "videos",
            )
            trailer = pick_trailer(videos or (), preferred)
        extras = TitleExtras(info=info, trailer=trailer, cast=page.cast[:CAST_LIMIT])
        return _Film(page.details, extras)

    async def _english_overview(
        self, kind: MediaType, item_id: int, info: TitleInfo, language: str
    ) -> TitleInfo:
        """`info` with TMDB's English overview when it has none in the app's language."""
        if info.overview or language == ENGLISH:
            return info
        english = await _optional_call(
            self._info(kind, item_id, ENGLISH), kind, item_id, "English overview"
        )
        if english is not None and english.overview:
            return info.model_copy(update={"overview": english.overview})
        return info

    async def _fetch_tv_extras(self, tv_id: int) -> TitleExtras:
        """A show's details and cast at once, then its videos (in the preferred languages and
        the show's own); the overview again in English when the app's language has none."""
        kind, item_id = MediaType.TV, tv_id
        language = tmdb_language()
        info_result, cast_result = await asyncio.gather(
            self._info(kind, item_id, language),
            self._tmdb.cast(kind, item_id),
            return_exceptions=True,
        )
        if isinstance(info_result, BaseException):
            raise info_result
        cast = _optional(cast_result, kind, item_id, "cast")
        info = await self._english_overview(kind, item_id, info_result, language)
        preferred = trailer_languages()
        asked = [code for code in dict.fromkeys((*preferred, info.original_language or "")) if code]
        videos = await _optional_call(
            self._tmdb.videos(kind, item_id, languages=asked), kind, item_id, "videos"
        )
        return TitleExtras(
            info=info,
            trailer=pick_trailer(videos or (), preferred),
            cast=tuple(cast[:CAST_LIMIT]),
        )

    def _info(self, kind: MediaType, item_id: int, language: str) -> Awaitable[TitleInfo]:
        if kind is MediaType.MOVIE:
            return self._tmdb.movie_info(item_id, language=language)
        return self._tmdb.tv_info(item_id, language=language)

    async def genre_names(self, kind: MediaType) -> Mapping[int, str]:
        """What the lists' `genre_ids` mean for this kind, in English as Plex's genres are, to go
        with those on the phone; kept for DETAILS_TTL. TMDB errors propagate."""
        cached = self._genre_names.get(kind)
        if cached is not None and _is_fresh(cached.stored_at, self._clock(), DETAILS_TTL):
            return cached.value
        generation = self._generation
        names = await self._tmdb.genres(kind, language=ENGLISH)
        if generation == self._generation:  # not cleared meanwhile
            self._genre_names[kind] = _Entry(names, self._clock())
        return names

    async def search_movies(self, query: str, *, limit: int = 12) -> list[Movie]:
        return await self._tmdb.search_movies(query, limit=limit)

    async def search_shows(self, query: str, *, limit: int = 12) -> list[DiscoverShow]:
        """TMDB's shows matching `query`, best match first. TMDB errors propagate."""
        return await self._tmdb.search_tv(query, limit=limit)

    async def search_people(self, name: str) -> list[PersonMatch]:
        """TMDB's people matching `name`, best match first. TMDB errors propagate."""
        return await self._tmdb.search_people(name)

    async def person(self, person_id: int) -> Person:
        """TMDB's person with their cast parts (`TMDBClient.person`); not cached. TMDB errors
        propagate (TMDBNotFound: no such person)."""
        return await self._tmdb.person(person_id)

    async def check_tmdb_key(self, key: str) -> bool:
        """Whether TMDB accepts `key` (not yet saved). Raises TMDBError when TMDB can't be
        reached; see TMDBClient.check_key."""
        return await self._tmdb.check_key(key)

    # -- tracking ------------------------------------------------------------------------------

    async def resolve(self, show: DiscoverShow) -> Show:
        """The TVMaze counterpart of a TMDB show (see `_find_on_tvmaze`), without tracking it.

        Remembered, so the dashboards flag the show as tracked once that TVMaze show is, however
        it was tracked. Raises NoTVMazeMatch when no counterpart is found. TMDB and TVMaze errors
        propagate.
        """
        match, matched_by = await self._find_on_tvmaze(show)
        # Neither its name nor its TMDB id: the phone's search finds shows by what a person typed.
        log.info("Matched a TMDB show to TVMaze show %d by %s", match.id, matched_by)
        self._tracked_via_tmdb[show.id] = match.id
        return match

    async def track(
        self, show: DiscoverShow, *, profile_id: int, match: Show | None = None
    ) -> Show:
        """Add a TMDB show to a person's library through its TVMaze counterpart (`resolve`, or
        `match` when the caller resolved it just now); returns that show.

        Raises NoTVMazeMatch when no counterpart is found. TMDB and TVMaze errors propagate.
        """
        if match is None:
            match = await self.resolve(show)
        tracked = await self._library.track(match.id, profile_id=profile_id)
        self._tracked_via_tmdb[show.id] = tracked.id
        return tracked

    async def _find_on_tvmaze(self, show: DiscoverShow) -> tuple[Show, str]:
        """TVMaze's show for `show` and how it was found: "imdb", "thetvdb" or "name+year"."""
        details = await self.tv_details(show.id)
        if details.imdb_id:
            if (hit := await self._tvmaze.lookup_show(imdb=details.imdb_id)) is not None:
                return hit, "imdb"
        if details.tvdb_id is not None:
            if (hit := await self._tvmaze.lookup_show(thetvdb=details.tvdb_id)) is not None:
                return hit, "thetvdb"
        # Without a year the name alone is too weak a match (TVMaze search is fuzzy).
        if show.year is not None:
            for hit in await self._tvmaze.search_shows(show.name, limit=TRACK_SEARCH_LIMIT):
                if hit.year == show.year:
                    return hit, "name+year"
        raise NoTVMazeMatch(show.name)
