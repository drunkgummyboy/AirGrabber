"""The film watchlists: films each person wants, including ones that aren't out yet.

`WatchlistService` (`services.watchlist`) keeps the selected person's list in memory (loaded at
startup and at every switch: `load`, app/services/profiles.py), so cards can ask `contains()`
while they are built, and tells its listeners after every change. A change names the person
whose list it is (`profile_id`): one for someone else's list (a click just before a switch) is
stored but leaves the list in memory alone. A film leaves every list once it is downloaded,
however that happens: `HistoryRepository.add` deletes its rows and the service, hearing the grab
from `DownloadService`, drops it here.

When a film counts as out (`available_on`): its digital release date, as TMDB's details give it
for the client's region; without one, its theatrical release (else TMDB's own release date)
plus DIGITAL_AFTER_THEATRICAL. A film without any date never counts as out.

The automatic downloads (`AutoDownloadService`) fetch the films whose "Download when it's out"
switch is on, on anyone's list, once they are out (`is_due`): at most one search per film every
RETRY_AFTER, and after GIVE_UP_AFTER of searches without a good enough release since the first
one, the film is given up (every switch of it goes off; the card offers the manual Download).
The tries are the film's, not a person's: a film on two lists is searched for once.

Release dates are re-read from TMDB (`refresh_release_dates`, every film on anyone's list) when
a film is added and when they are older than REFRESH_AFTER; without a TMDB key that is simply
skipped.

Log hygiene: only film titles, ids and outcomes are logged.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

from app.database import WatchlistRepository
from app.models import FIRST_PROFILE_ID, GrabTarget, MediaType, Movie, WatchlistEntry
from app.services.discovery import DiscoveryService
from app.services.downloads import DownloadService
from app.services.tmdb import TMDBAuthError, TMDBError, TMDBNotConfigured

log = logging.getLogger(__name__)

WatchlistListener = Callable[[], None]

# Without a known digital release, a film counts as out this long after its theatrical one.
DIGITAL_AFTER_THEATRICAL = timedelta(days=60)
RETRY_AFTER = timedelta(hours=4)  # a film is searched at most this often
GIVE_UP_AFTER = timedelta(days=30)  # stop searching this long after the first search
REFRESH_AFTER = timedelta(hours=24)  # release dates older than this are read again


def _utc_now() -> datetime:
    return datetime.now(UTC)


def available_on(entry: WatchlistEntry) -> date | None:
    """The day the film counts as out digitally (see the module docstring); None: unknown."""
    if entry.digital_release is not None:
        return entry.digital_release
    theatrical = entry.theatrical_release or entry.movie.release_date
    return theatrical + DIGITAL_AFTER_THEATRICAL if theatrical is not None else None


def is_out(entry: WatchlistEntry, today: date) -> bool:
    day = available_on(entry)
    return day is not None and day <= today


def is_due(entry: WatchlistEntry, now: datetime) -> bool:
    """Whether the automatic downloads should search for the film now: its switch is on, it
    isn't given up, it is out, and its last search was at least RETRY_AFTER ago."""
    if not entry.auto_download or entry.gave_up or not is_out(entry, now.date()):
        return False
    return entry.last_try is None or now - entry.last_try >= RETRY_AFTER


def should_give_up(entry: WatchlistEntry, now: datetime) -> bool:
    """After one more search without a grab at `now`: has the film been searched for long
    enough (GIVE_UP_AFTER since its first search)?"""
    first = entry.first_try or now
    return now - first >= GIVE_UP_AFTER


def needs_refresh(entry: WatchlistEntry, now: datetime) -> bool:
    """Whether the film's release dates should be read from TMDB again."""
    if entry.details_at is None:
        return True
    age = now - entry.details_at
    return age >= REFRESH_AFTER or age < timedelta(0)  # a clock that went back: refresh


def due_order(entry: WatchlistEntry) -> tuple[int, datetime, date, int]:
    """Untried and least-tried films first, then the one tried longest ago, then the earliest
    out (so films that keep coming back empty can't crowd newer ones out of a run)."""
    never = datetime.min.replace(tzinfo=UTC)
    return (
        entry.tries,
        entry.last_try or never,
        available_on(entry) or date.min,
        entry.movie.id,
    )


class WatchlistService:
    """The watchlists over their repository, the selected person's kept in memory; listeners
    hear every change to it."""

    def __init__(
        self,
        repository: WatchlistRepository,
        discovery: DiscoveryService,
        downloads: DownloadService,
        *,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        """`clock` returns the current UTC instant (tests move it)."""
        self._repository = repository
        self._discovery = discovery
        self._clock = clock
        self._profile_id = FIRST_PROFILE_ID  # whose list is in memory
        self._entries: dict[int, WatchlistEntry] = {}
        self._listeners: list[WatchlistListener] = []
        downloads.subscribe_grabs(self._on_grab)

    # -- listeners -------------------------------------------------------------------------------

    def subscribe(self, listener: WatchlistListener) -> Callable[[], None]:
        """Call `listener` after every change to the list in memory (films added, removed or
        downloaded, a switch, release dates, tries, another person's list loaded). Returns a
        function that unsubscribes it."""
        self._listeners.append(listener)
        subscribed = True

        def unsubscribe() -> None:
            nonlocal subscribed
            if subscribed:
                subscribed = False
                self._listeners.remove(listener)

        return unsubscribe

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:
                log.exception("Watchlist listener %r failed", listener)

    # -- reading ---------------------------------------------------------------------------------

    @property
    def profile_id(self) -> int:
        """The person whose list is in memory (`contains`, `entry`, `entries`)."""
        return self._profile_id

    async def load(self, profile_id: int | None = None) -> None:
        """Read a person's list from the database into memory (`profile_id`; None: the one in
        memory again, to resynchronize)."""
        person = self._profile_id if profile_id is None else profile_id
        entries = await self._repository.entries(profile_id=person)
        self._profile_id = person
        self._entries = {entry.movie_id: entry for entry in entries}
        self._notify()

    def contains(self, movie_id: int) -> bool:
        return movie_id in self._entries

    def entry(self, movie_id: int) -> WatchlistEntry | None:
        return self._entries.get(movie_id)

    def entries(self) -> tuple[WatchlistEntry, ...]:
        """The films on the list in memory, newest first."""
        return tuple(
            sorted(
                self._entries.values(),
                key=lambda entry: (entry.added_at, entry.movie_id),
                reverse=True,
            )
        )

    async def due(self, now: datetime, limit: int) -> list[WatchlistEntry]:
        """The films the automatic downloads should search for now (`is_due`), on anyone's
        list (each film once, its switch on when anyone's is), at most `limit`, in
        `due_order`."""
        due = [entry for entry in await self._repository.films() if is_due(entry, now)]
        return sorted(due, key=due_order)[:limit]

    # -- changes ---------------------------------------------------------------------------------

    async def add(self, movie: Movie, *, profile_id: int, auto_download: bool = True) -> None:
        """Put a film on a person's list (its switch on by default), then read its release
        dates from TMDB. A film already on their list is left as it is."""
        added = await self._repository.add(
            movie, profile_id=profile_id, auto_download=auto_download, when=self._clock()
        )
        if not added:
            return
        log.info("Watchlist of person %d: added %s (TMDB id %d)", profile_id, movie.title, movie.id)
        await self._reload(movie.id)
        await self._refresh(movie.id)

    async def remove(self, movie_id: int, *, profile_id: int) -> None:
        """Take a film off a person's list."""
        if not await self._repository.remove(movie_id, profile_id=profile_id):
            return
        log.info("Watchlist of person %d: removed TMDB id %d", profile_id, movie_id)
        if profile_id == self._profile_id and self._entries.pop(movie_id, None) is not None:
            self._notify()

    async def set_auto_download(self, movie_id: int, on: bool, *, profile_id: int) -> None:
        """Switch a person's "Download when it's out" on or off; on again after giving up
        starts afresh."""
        await self._repository.set_auto_download(movie_id, on, profile_id=profile_id)
        await self._reload(movie_id)

    async def switch_off(self, movie_id: int) -> None:
        """Switch "Download when it's out" off on every list that has the film."""
        await self._repository.switch_off(movie_id)
        await self._reload(movie_id)

    async def record_try(self, movie_id: int, *, when: datetime | None = None) -> bool:
        """Count an automatic search for a film that grabbed nothing (on every list that has
        it); returns whether the film was given up with it (`should_give_up`)."""
        film = await self._repository.film(movie_id)
        if film is None:
            return False
        when = when or self._clock()
        give_up = should_give_up(film, when)
        await self._repository.record_try(movie_id, when, give_up=give_up)
        await self._reload(movie_id)
        return give_up

    async def refresh_release_dates(self, *, force: bool = False) -> None:
        """Read the release dates of every film on anyone's list whose dates are stale
        (`needs_refresh`; every film with `force`) from TMDB, one after another. Without a TMDB
        key, or with one TMDB rejects, nothing is read; other TMDB failures skip that film
        until the next call."""
        now = self._clock()
        for film in await self._repository.films():
            if not (force or needs_refresh(film, now)):
                continue
            if not await self._refresh(film.movie_id):
                return

    async def _refresh(self, movie_id: int) -> bool:
        """Read one film's release dates; False when TMDB can't be used at all (no or rejected
        key), so a caller stops asking."""
        try:
            details = await self._discovery.movie_details(movie_id)
        except (TMDBNotConfigured, TMDBAuthError) as exc:
            log.debug("Watchlist: release dates not refreshed: %s", exc)
            return False
        except TMDBError as exc:
            log.info("Watchlist: no TMDB details for TMDB id %d: %s", movie_id, exc)
            return True
        await self._repository.set_release_dates(
            movie_id,
            imdb_id=details.imdb_id,
            digital=details.digital_release,
            theatrical=details.theatrical_release,
            when=self._clock(),
        )
        await self._reload(movie_id)
        return True

    async def _reload(self, movie_id: int) -> None:
        """Read one film's row of the list in memory again (gone: dropped) and tell the
        listeners."""
        person = self._profile_id
        entry = await self._repository.entry(movie_id, profile_id=person)
        if person != self._profile_id:
            return  # another person's list was loaded meanwhile: that read has it
        if entry is None:
            if self._entries.pop(movie_id, None) is None:
                return
        else:
            self._entries[movie_id] = entry
        self._notify()

    def _on_grab(self, target: GrabTarget) -> None:
        """A film was grabbed (its rows are gone already: HistoryRepository.add): it leaves."""
        if target.media_type is not MediaType.MOVIE:
            return
        if self._entries.pop(target.media_id, None) is not None:
            log.info("Watchlist: TMDB id %d was downloaded, it leaves the list", target.media_id)
            self._notify()
