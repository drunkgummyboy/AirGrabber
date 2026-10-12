"""TMDB discovery: TV and movie dashboards, cached details, and tracking TMDB shows via TVMaze.

Everything is cached in memory only: TMDB's lists for DASHBOARD_TTL, details for DETAILS_TTL.
A TMDB show is tracked through its TVMaze counterpart, found by IMDb id, then TheTVDB id, then
by name + first-air year.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import unicodedata
from collections import Counter
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Generic, TypeVar

from app.models import (
    DiscoverShow,
    DiscoverShowEntry,
    Movie,
    MovieDashboard,
    MovieDetails,
    Show,
    TVDashboard,
    TVDetails,
)
from app.services.library import LibraryService
from app.services.tmdb import TMDBAuthError, TMDBClient, TMDBError, TMDBNotConfigured
from app.services.tvmaze import TVMazeClient

log = logging.getLogger(__name__)

T = TypeVar("T")

Clock = Callable[[], datetime]

DASHBOARD_TTL = timedelta(minutes=30)
DETAILS_TTL = timedelta(hours=24)
SHELF_SIZE = 18
PREMIERES_AHEAD = timedelta(days=90)
JUST_DROPPED_SINCE = timedelta(days=14)
UPCOMING_AHEAD = timedelta(days=21)
# Pages of TMDB's digital-release lists fetched per shelf: re-releases are filtered out
# afterwards, so one page (20 films) could leave a shelf short.
DIGITAL_PAGES = 2
# TVMaze search hits checked for a name + year match when no external id resolves.
TRACK_SEARCH_LIMIT = 5

_IGNORED_WORDS = frozenset({"the", "and", "&"})


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


def _is_fresh(stored_at: datetime, now: datetime, ttl: timedelta) -> bool:
    # A clock that went backwards makes the entry stale rather than fresh for longer.
    return timedelta(0) <= now - stored_at < ttl


@dataclass(frozen=True, slots=True)
class _Entry(Generic[T]):
    value: T
    stored_at: datetime


@dataclass(frozen=True, slots=True)
class _Shelves(Generic[T]):
    """One dashboard's TMDB lists, by shelf name."""

    day: date  # the local day the date windows were computed for
    lists: Mapping[str, tuple[T, ...]]
    stored_at: datetime


class _DetailsCache(Generic[T]):
    """Details by id, kept for `ttl`; concurrent requests for one id share a single fetch.

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

    def clear(self) -> None:
        """Forget every entry. Fetches still in flight finish for their callers but store
        nothing, and later calls start new ones."""
        self._entries.clear()
        self._in_flight.clear()

    async def _load(self, item_id: int) -> T:
        value = await self._fetch(item_id)
        if self._in_flight.get(item_id) is asyncio.current_task():  # not cleared meanwhile
            self._entries[item_id] = _Entry(value, self._clock())
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
    ) -> None:
        self._tmdb = tmdb
        self._tvmaze = tvmaze
        self._library = library
        self._clock = clock
        self._tv_shelves: _Shelves[DiscoverShow] | None = None
        self._movie_shelves: _Shelves[Movie] | None = None
        # Bumped by clear_cache(), so dashboards fetched across a clear aren't stored.
        self._generation = 0
        self._movie_details = _DetailsCache("tmdb-movie", tmdb.movie_details, DETAILS_TTL, clock)
        self._tv_details = _DetailsCache("tmdb-tv", tmdb.tv_details, DETAILS_TTL, clock)
        # TMDB id → TVMaze id of every show tracked through track(): flags it as tracked even
        # when the two services spell its name differently.
        self._tracked_via_tmdb: dict[int, int] = {}

    def clear_cache(self) -> None:
        """Forget every cached TMDB list and details (e.g. after the API key changed)."""
        self._tv_shelves = None
        self._movie_shelves = None
        self._generation += 1
        self._movie_details.clear()
        self._tv_details.clear()

    # -- dashboards ----------------------------------------------------------------------------

    async def tv_dashboard(self, *, force: bool = False) -> TVDashboard:
        """Trending shows, shows on the air, and premieres in the next 90 days; 18 at most each.

        TMDB's lists are cached for DASHBOARD_TTL and for the current local day (`force`
        refetches them); the tracked flags are recomputed from the library on every call. A
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
        is_tracked = await self._tracked_matcher()
        shelves = {
            name: tuple(DiscoverShowEntry(show=show, is_tracked=is_tracked(show)) for show in shows)
            for name, shows in lists.items()
        }
        return TVDashboard(
            trending=shelves.get("trending", ()),
            on_air=shelves.get("on_air", ()),
            premieres=shelves.get("premieres", ()),
            failed=failed,
        )

    async def movie_dashboard(self, *, force: bool = False) -> MovieDashboard:
        """Films first released digitally in the last 14 days, trending films, and films first
        released digitally in the next 21 days; 18 at most each.

        The digital shelves are filtered on each film's details (see `_first_digital_releases`).
        Cached like `tv_dashboard`, filtered.
        """
        today = self._today()
        lists = None if force else self._fresh(self._movie_shelves, today)
        failed: tuple[str, ...] = ()
        if lists is None:
            generation = self._generation
            fetchers: dict[str, Callable[[], Awaitable[list[Movie]]]] = {
                "just_dropped": lambda: self._first_digital_releases(
                    today - JUST_DROPPED_SINCE, today
                ),
                "trending": self._tmdb.trending_movies,
                "upcoming": lambda: self._first_digital_releases(
                    today + timedelta(days=1), today + UPCOMING_AHEAD
                ),
            }
            lists, failed = await self._load_shelves("Movie", fetchers)
            if not failed and generation == self._generation:
                self._movie_shelves = _Shelves(today, lists, self._clock())
        return MovieDashboard(
            just_dropped=lists.get("just_dropped", ()),
            trending=lists.get("trending", ()),
            upcoming=lists.get("upcoming", ()),
            failed=failed,
        )

    async def _first_digital_releases(self, start: date, end: date) -> list[Movie]:
        """Films whose FIRST digital release in the region falls in [start, end], most popular
        first.

        TMDB lists a film when any of its digital releases falls in the window, so re-releases
        show up there too. The first DIGITAL_PAGES pages are fetched and every film's details
        (through the shared details cache) decide: a film is dropped only when its first
        digital release is known and outside the window. Films without a known digital date,
        or whose details fail to load, are kept. A failing page fails the shelf; a missing or
        rejected key propagates, also from the details.
        """
        pages = await asyncio.gather(
            *(self._tmdb.digital_releases(start, end, page=n) for n in range(1, DIGITAL_PAGES + 1)),
            return_exceptions=True,
        )
        if (error := _first_failure(pages)) is not None:
            raise error
        candidates: dict[int, Movie] = {}
        for movie in (movie for page in pages if isinstance(page, list) for movie in page):
            candidates.setdefault(movie.id, movie)  # popularity can shift a film to page 2
        keep = await asyncio.gather(
            *(self._first_digital_in(movie, start, end) for movie in candidates.values()),
            return_exceptions=True,
        )
        if (error := _first_failure(keep)) is not None:
            raise error  # only fatal errors get here: _first_digital_in keeps the film otherwise
        return [movie for movie, kept in zip(candidates.values(), keep, strict=True) if kept is True]

    async def _first_digital_in(self, movie: Movie, start: date, end: date) -> bool:
        """False only when the film's first digital release is known and outside [start, end]."""
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

    async def _tracked_matcher(self) -> Callable[[DiscoverShow], bool]:
        """Flags a TMDB show as tracked when track() added it and its TVMaze show is still
        tracked, or when its name matches a tracked show's (see normalize_title)."""
        entries = await self._library.tracked()
        tracked_ids = {entry.show.id for entry in entries}
        tracked_names = {key for entry in entries if (key := normalize_title(entry.show.name))}

        def is_tracked(show: DiscoverShow) -> bool:
            if self._tracked_via_tmdb.get(show.id) in tracked_ids:
                return True
            return normalize_title(show.name) in tracked_names

        return is_tracked

    # -- details and search --------------------------------------------------------------------

    async def movie_details(self, movie_id: int) -> MovieDetails:
        """TMDB's details of a film; cached for DETAILS_TTL, concurrent calls share one fetch."""
        return await self._movie_details.get(movie_id)

    async def tv_details(self, tv_id: int) -> TVDetails:
        """TMDB's details of a show; cached for DETAILS_TTL, concurrent calls share one fetch."""
        return await self._tv_details.get(tv_id)

    async def search_movies(self, query: str, *, limit: int = 12) -> list[Movie]:
        return await self._tmdb.search_movies(query, limit=limit)

    # -- tracking ------------------------------------------------------------------------------

    async def track(self, show: DiscoverShow) -> Show:
        """Add a TMDB show to the library through its TVMaze counterpart; returns that show.

        Raises NoTVMazeMatch when no counterpart is found. TMDB and TVMaze errors propagate.
        """
        match, matched_by = await self._find_on_tvmaze(show)
        log.info(
            "Tracking %s (TMDB id %d) as TVMaze show %d, matched by %s",
            show.name, show.id, match.id, matched_by,
        )
        tracked = await self._library.track(match.id)
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
