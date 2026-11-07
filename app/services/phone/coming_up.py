"""`GET /v1/coming-up`: one list for the phone, nearest day first.

Series come from the calendar of the person the phone follows, films from that person's watchlist
(never the one selected on the PC). A film out before the window stays listed until it is fetched,
or it would never get its button; a film further off than the window waits, and one without a date
comes last. A special has no episode number to download by, and isn't listed.

A grab takes a film off every list, so a grabbed film is listed from the PC's Downloads page, for
as long as that page has it, and for a moment before the page has looked again (`GrabbedFilms`).
The history doesn't say whose grab it was: what the lists looked like while the film was on them
does, as far as this run has seen them. A film not seen on any list shows on every phone.

`state` says what the PC says, from the same sources:

- `downloading`: a one-click run at work or waiting (`QuickDownloadService`, `DownloadQueue`), or
  the Downloads page (`DownloadStatusService.latest`) says it's on its way or paused, or it was
  grabbed a moment ago.
- `downloaded`: the Downloads page says it's finished, or it's in Plex or grabbed lately and on its
  way there (the calendar's marks, none without Plex). "Status unknown" on that page isn't finished:
  it also means the program can't be reached just now, whatever the grab's progress.
- `not_out`: not aired (`Episode.has_aired`), not out (`is_out`).
- `can_download`: the rest. A grab known only from the download history gets no mark here either
  (the owner's decision for 3.1.0): a tap says it was downloaded already.

Nothing here logs. A `poster_url` that could hold a secret isn't sent.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

from app.i18n import t
from app.models import (
    DownloadEntry,
    DownloadState,
    GrabTarget,
    MediaType,
    Movie,
    PairedPhone,
    WatchlistEntry,
)
from app.models.plex import PlexFilms
from app.services.download_queue import DownloadQueue, target_key
from app.services.download_status import DownloadStatusService
from app.services.downloads import DownloadService
from app.services.library import LibraryService
from app.services.phone.server import Handler, Reply, Request, bad_request, error_reply
from app.services.profiles import ProfileService
from app.services.quick_download import QuickDownloadService
from app.services.watchlist import WatchlistService, available_on, is_out

DEFAULT_DAYS = 21
MAX_DAYS = 366
JUST_GRABBED_S = 60.0  # by then the Downloads page has looked again (AFTER_GRAB_S, and its checks)

ItemState = Literal["not_out", "can_download", "downloading", "downloaded"]
_TargetKey = tuple[MediaType, int, int | None, int | None]

_ON_ITS_WAY = frozenset(
    {
        DownloadState.DOWNLOADING,
        DownloadState.WAITING,
        DownloadState.PENDING,
        DownloadState.STOPPED,
    }
)
_YEAR_AT_THE_END = re.compile(r"^(.+) \((\d{4})\)$")


def person_gone(phone: PairedPhone | None, profiles: ProfileService) -> Reply | None:
    """The answer for a phone whose person has been removed; None while they're still here. Such
    a phone is never given someone else's lists: connecting again makes it follow someone."""
    assert phone is not None  # the server lets only paired phones through
    if profiles.get(phone.profile_id) is not None:
        return None
    return error_reply(
        401,
        "not_paired",
        t(
            "The person this phone follows was removed from AirGrabber, "
            "so connect the phone again from AirGrabber's Settings."
        ),
    )


def _safe_poster(url: str | None) -> str | None:
    """A plain web address, or nothing: a query, a sign-in or a fragment could hold a key."""
    if not url:
        return None
    parts = urlsplit(url)
    plain = not (parts.query or parts.fragment or "@" in parts.netloc)
    return url if plain and parts.scheme in {"http", "https"} else None


def _page_states(entries: Iterable[DownloadEntry]) -> dict[_TargetKey, ItemState]:
    """What the Downloads page says per grabbed target; on its way beats finished. A problem, a
    grab the program never picked up or an unknown status says nothing: a tap then says it was
    downloaded already, and can be asked to do it again."""
    states: dict[_TargetKey, ItemState] = {}
    for entry in entries:
        grab = entry.record
        key = (grab.media_type, grab.media_id, grab.season, grab.episode)
        if entry.state in _ON_ITS_WAY and entry.moved_to is None:
            states[key] = "downloading"
        elif entry.state is DownloadState.READY or entry.moved_to is not None:
            states.setdefault(key, "downloaded")
    return states


@dataclass(slots=True)
class _SeenFilm:
    entry: WatchlistEntry
    people: set[int] = field(default_factory=set)


class GrabbedFilms:
    """What's known of the films on the lists, kept for when a grab has taken them off."""

    def __init__(self, status: DownloadStatusService) -> None:
        self._status = status
        self._seen: dict[int, _SeenFilm] = {}

    def remember(self, lists: Mapping[int, Sequence[WatchlistEntry]]) -> None:
        """Everyone's lists as they are now (person -> films). Films on none stay as last seen."""
        people: dict[int, set[int]] = {}
        for person, entries in lists.items():
            for entry in entries:
                people.setdefault(entry.movie_id, set()).add(person)
                self._seen[entry.movie_id] = _SeenFilm(entry)
        for movie_id, holders in people.items():
            self._seen[movie_id].people = holders

    def note(self, entry: WatchlistEntry, person: int) -> None:
        """A phone asked about this film on its person's list."""
        self._seen.setdefault(entry.movie_id, _SeenFilm(entry)).people.add(person)

    def rows(self) -> dict[int, DownloadEntry]:
        """The films on the Downloads page now, by TMDB id."""
        overview = self._status.latest
        if overview is None:
            return {}
        return {
            entry.record.media_id: entry
            for entry in overview.entries
            if entry.record.media_type is MediaType.MOVIE
        }

    def concerns(self, movie_id: int, person: int) -> bool:
        """False only when it's known to have been on other people's lists and not on theirs."""
        seen = self._seen.get(movie_id)
        return seen is None or person in seen.people

    def seen(self, movie_id: int) -> WatchlistEntry | None:
        seen = self._seen.get(movie_id)
        return seen.entry if seen is not None else None

    def movie(self, movie_id: int) -> Movie | None:
        """The film as seen on a list, else as far as its Downloads page row tells."""
        seen = self.seen(movie_id)
        if seen is not None:
            return seen.movie
        row = self.rows().get(movie_id)
        if row is None:
            return None
        found = _YEAR_AT_THE_END.match(row.title)
        if found is None:
            return Movie(id=movie_id, title=row.title)
        return Movie(
            id=movie_id, title=found.group(1), release_date=date(int(found.group(2)), 1, 1)
        )


class ComingUp:
    def __init__(
        self,
        library: LibraryService,
        watchlist: WatchlistService,
        status: DownloadStatusService,
        downloads: DownloadService,
        quick: QuickDownloadService,
        queue: DownloadQueue,
        profiles: ProfileService,
        films: GrabbedFilms,
        *,
        now: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """`now`: an aware instant, whose time zone decides which day "today" is; `clock`:
        seconds. Both for tests."""
        self._library = library
        self._watchlist = watchlist
        self._status = status
        self._quick = quick
        self._queue = queue
        self._profiles = profiles
        self._grabbed_films = films
        self._now = now
        self._clock = clock
        self._grabbed_at: dict[_TargetKey, float] = {}
        downloads.subscribe_grabs(self._on_grab)

    def routes(self) -> dict[tuple[str, str], Handler]:
        return {("GET", "/v1/coming-up"): self._coming_up}

    def _on_grab(self, target: GrabTarget) -> None:
        self._grabbed_at[target_key(target)] = self._clock()

    async def _coming_up(self, request: Request) -> Reply:
        problem = person_gone(request.phone, self._profiles)
        if problem is not None:
            return problem
        assert request.phone is not None
        now = self._now()
        today = now.date()
        window = _window(request.query, today)
        if window is None:
            return bad_request()
        start, end = window
        items = await self._episodes(request.phone.profile_id, start, end, now)
        items += await self._films(request.phone.profile_id, start, end, today)
        # By day, then what's on that day in a steady order; no date comes last.
        items.sort(
            key=lambda item: (
                item["date"] or date.max.isoformat(),
                item["title"].casefold(),
                item["season"] or 0,
                item["episode"] or 0,
            )
        )
        return Reply(200, {"items": items})

    async def _episodes(
        self, person: int, start: date, end: date, now: datetime
    ) -> list[dict[str, Any]]:
        entries = await self._library.calendar(start, end, profile_id=person)
        in_plex = await self._library.plex_between(start, end)
        on_its_way = await self._library.downloaded_between(start, end, today=now.date())
        page = self._page()
        items = []
        for entry in entries:
            episode = entry.episode
            if episode.number is None:
                continue
            target = GrabTarget(
                media_type=MediaType.TV,
                media_id=entry.show.id,
                season=episode.season,
                episode=episode.number,
            )
            pack = GrabTarget(
                media_type=MediaType.TV, media_id=entry.show.id, season=episode.season
            )
            items.append(
                {
                    "kind": "episode",
                    "id": entry.show.id,
                    "season": episode.season,
                    "episode": episode.number,
                    "title": entry.show.name,
                    "code": episode.code,
                    "date": episode.air_date.isoformat() if episode.air_date else None,
                    "poster_url": _safe_poster(entry.show.poster_url),
                    "state": self._state(
                        (target, pack),
                        out=episode.has_aired(now),
                        have=episode.id in in_plex or episode.id in on_its_way,
                        page=page,
                    ),
                }
            )
        return items

    async def _films(
        self, person: int, start: date, end: date, today: date
    ) -> list[dict[str, Any]]:
        lists = {
            one.id: await self._watchlist.by_release_of(one.id) for one in self._profiles.people
        }
        self._grabbed_films.remember(lists)
        plex = await self._library.plex_films()
        page = self._page()
        rows = self._grabbed_films.rows()
        items = []
        for entry in lists.get(person, ()):
            day = available_on(entry)
            if day is not None and day > end:
                continue
            state = self._state(
                (GrabTarget(media_type=MediaType.MOVIE, media_id=entry.movie.id),),
                out=is_out(entry, today),
                have=plex.has(entry.movie.id, entry.imdb_id or entry.movie.imdb_id),
                page=page,
            )
            fetched_long_ago = day is not None and day < start and state == "downloaded"
            if not fetched_long_ago or entry.movie.id in rows:
                items.append(_film_item(entry.movie, day, state))
        on_the_list = {entry.movie.id for entry in lists.get(person, ())}
        for movie_id in self._films_grabbed_lately(rows):
            if movie_id not in on_the_list and self._grabbed_films.concerns(movie_id, person):
                items.append(self._grabbed_item(movie_id, rows.get(movie_id), plex, page))
        return items

    def _films_grabbed_lately(self, rows: Mapping[int, DownloadEntry]) -> list[int]:
        """On the Downloads page, or just grabbed and known well enough to name."""
        just_grabbed = [
            key[1]
            for key in list(self._grabbed_at)
            if key[0] is MediaType.MOVIE
            and self._just_grabbed(key)
            and self._grabbed_films.seen(key[1]) is not None
        ]
        return list(dict.fromkeys([*rows, *just_grabbed]))

    def _grabbed_item(
        self,
        movie_id: int,
        row: DownloadEntry | None,
        plex: PlexFilms,
        page: dict[_TargetKey, ItemState],
    ) -> dict[str, Any]:
        seen = self._grabbed_films.seen(movie_id)
        movie = self._grabbed_films.movie(movie_id)
        assert movie is not None  # seen on a list, or on the Downloads page
        day = available_on(seen) if seen is not None else None
        if day is None and row is not None:
            day = row.record.downloaded_at.date()
        state = self._state(
            (GrabTarget(media_type=MediaType.MOVIE, media_id=movie_id),),
            out=True,
            have=plex.has(movie_id, (seen.imdb_id if seen is not None else None) or movie.imdb_id),
            page=page,
        )
        return _film_item(movie, day, state)

    def _page(self) -> dict[_TargetKey, ItemState]:
        overview = self._status.latest
        return _page_states(overview.entries) if overview is not None else {}

    def _state(
        self,
        targets: Collection[GrabTarget],
        *,
        out: bool,
        have: bool,
        page: dict[_TargetKey, ItemState],
    ) -> ItemState:
        """`targets`: what it is, and for an episode also its season's pack."""
        keys = [target_key(target) for target in targets]
        said = [page[key] for key in keys if key in page]
        if (
            "downloading" in said
            or any(self._quick.is_running(target) for target in targets)
            or any(self._queue.is_active(key) for key in keys)
        ):
            return "downloading"
        if said:
            return "downloaded"
        if any(self._just_grabbed(key) for key in keys):
            return "downloading"
        if have:
            return "downloaded"
        return "can_download" if out else "not_out"

    def _just_grabbed(self, key: _TargetKey) -> bool:
        grabbed_at = self._grabbed_at.get(key)
        if grabbed_at is None:
            return False
        if self._clock() - grabbed_at > JUST_GRABBED_S:
            del self._grabbed_at[key]
            return False
        return True


def _film_item(movie: Movie, day: date | None, state: ItemState) -> dict[str, Any]:
    return {
        "kind": "movie",
        "id": movie.id,
        "season": None,
        "episode": None,
        "title": movie.title,
        "code": None,
        "date": day.isoformat() if day else None,
        "poster_url": _safe_poster(movie.poster_url()),
        "state": state,
    }


def _window(query: dict[str, str], today: date) -> tuple[date, date] | None:
    """The first and last day asked for; None when they can't be read."""
    try:
        start = date.fromisoformat(query["from"]) if "from" in query else today
        days = int(query.get("days", DEFAULT_DAYS))
        if not 1 <= days <= MAX_DAYS:
            return None
        return start, start + timedelta(days=days - 1)
    except (ValueError, OverflowError):
        return None
