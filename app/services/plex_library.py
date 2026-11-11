"""The Library page: what is in Plex, as cards (only while Plex is connected).

`PlexLibraryService.contents` gives the page every show and film the last Plex check read
(`plex_titles`), each show with whether the selected person follows it, its gaps (episodes Plex
lacks in the seasons it has some of: LibraryService.gaps) and whether it ended. `follow` adds a
show to My shows (by its TVMaze id when AirGrabber knows it, else looked up by TheTVDB / IMDb id,
the name last), then has Plex checked again so its episodes count. `show` finds the TVMaze show a
card opens.

The pure part (filters, sort, search, the cards' lines) is here too, tested without Tk. Nothing
here logs a title.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from app.i18n import N_, t, tn
from app.models import Show
from app.models.plex import PlexTitle
from app.services.library import LibraryService
from app.services.plex import PlexService

log = logging.getLogger(__name__)

RECENT_DAYS = 30  # Films' "Recently added": what came in this many days ago at most


class LibraryKindTab(StrEnum):
    SHOWS = "show"
    FILMS = "movie"


class LibrarySort(StrEnum):
    ADDED = "added"
    NAME = "name"
    YEAR = "year"


class LibraryFilter(StrEnum):
    ALL = "all"
    NOT_FOLLOWED = "not_followed"  # shows
    GAPS = "gaps"  # shows
    ENDED = "ended"  # shows
    UHD = "4k"  # films
    RECENT = "recent"  # films


SHOW_FILTERS = (LibraryFilter.ALL, LibraryFilter.NOT_FOLLOWED, LibraryFilter.GAPS, LibraryFilter.ENDED)
FILM_FILTERS = (LibraryFilter.ALL, LibraryFilter.UHD, LibraryFilter.RECENT)
SORT_NAMES = {
    LibrarySort.ADDED: N_("Recently added"),
    LibrarySort.NAME: N_("A–Z"),
    LibrarySort.YEAR: N_("Year"),
}
FILTER_NAMES = {
    LibraryFilter.ALL: N_("All"),
    LibraryFilter.NOT_FOLLOWED: N_("Not followed"),
    LibraryFilter.GAPS: N_("With gaps"),
    LibraryFilter.ENDED: N_("Ended"),
    LibraryFilter.UHD: N_("4K"),
    LibraryFilter.RECENT: N_("Recently added"),
}


@dataclass(frozen=True, slots=True)
class LibraryShow:
    """A show card: the Plex title, followed by the selected person, its gaps, ended."""

    title: PlexTitle
    following: bool = False
    gaps: int = 0
    ended: bool = False


@dataclass(frozen=True, slots=True)
class LibraryContents:
    shows: tuple[LibraryShow, ...] = ()
    films: tuple[PlexTitle, ...] = ()
    checked_at: datetime | None = None


# -- the pure part -----------------------------------------------------------------------------------


def _folded(text: str) -> str:
    return " ".join(text.casefold().split())


def matches(title: PlexTitle, query: str) -> bool:
    """The search box: every word of `query` in the title (or its year)."""
    words = _folded(query).split()
    haystack = f"{_folded(title.title)} {title.year or ''}"
    return all(word in haystack for word in words)


def _sort_key(title: PlexTitle, sort: LibrarySort) -> tuple[object, ...]:
    name = _folded(title.title)
    if sort is LibrarySort.ADDED:
        added = title.added_at.timestamp() if title.added_at is not None else 0.0
        return (-added, name)
    if sort is LibrarySort.YEAR:
        return (-(title.year or 0), name)
    return (name,)


def shown_shows(
    shows: Iterable[LibraryShow], chosen: LibraryFilter, sort: LibrarySort, query: str = ""
) -> list[LibraryShow]:
    """The show cards the page shows, in order."""
    kept = [
        show
        for show in shows
        if matches(show.title, query)
        and (
            chosen is LibraryFilter.ALL
            or (chosen is LibraryFilter.NOT_FOLLOWED and not show.following)
            or (chosen is LibraryFilter.GAPS and show.gaps > 0)
            or (chosen is LibraryFilter.ENDED and show.ended)
        )
    ]
    return sorted(kept, key=lambda show: _sort_key(show.title, sort))


def shown_films(
    films: Iterable[PlexTitle],
    chosen: LibraryFilter,
    sort: LibrarySort,
    query: str = "",
    *,
    now: datetime | None = None,
) -> list[PlexTitle]:
    """The film cards the page shows, in order."""
    since = (now or datetime.now(UTC)) - timedelta(days=RECENT_DAYS)
    kept = [
        film
        for film in films
        if matches(film, query)
        and (
            chosen is LibraryFilter.ALL
            or (chosen is LibraryFilter.UHD and film.resolution == "4K")
            or (
                chosen is LibraryFilter.RECENT
                and film.added_at is not None
                and film.added_at >= since
            )
        )
    ]
    return sorted(kept, key=lambda film: _sort_key(film, sort))


def show_line(title: PlexTitle) -> str:
    """"3 seasons · 34 episodes" (what Plex has)."""
    parts = []
    if title.seasons:
        parts.append(tn("{n} season", "{n} seasons", title.seasons))
    if title.episodes:
        parts.append(tn("{n} episode", "{n} episodes", title.episodes))
    return " · ".join(parts)


def film_line(title: PlexTitle) -> str:
    """"2024"."""
    return str(title.year) if title.year else ""


# A Library film below 1080p gets "⬆ Upgrade" (the owner's line, 2026-11-11): Plex's 720p and SD.
UPGRADABLE = frozenset({"720p", "SD"})


def can_upgrade(title: PlexTitle) -> bool:
    """A film Plex has only below 1080p, that its releases can be looked up for (a TMDB id);
    an unknown quality never counts."""
    return title.kind == "movie" and title.resolution in UPGRADABLE and title.tmdb_id is not None


def gaps_text(count: int) -> str:
    return tn("{n} gap", "{n} gaps", count) if count else ""


def checked_text(when: datetime | None) -> str:
    """"From Plex · checked 08:53"."""
    if when is None:
        return t("From Plex")
    return t("From Plex · checked {time}", time=when.astimezone().strftime("%H:%M"))


def follow_line(title: PlexTitle) -> str:
    """The import line `follow` hands LibraryService.import_shows."""
    if title.show_id is not None:
        return str(title.show_id)
    if title.tvdb_id is not None:
        return f"tvdb:{title.tvdb_id} {title.title}"
    if title.imdb_id is not None:
        return f"imdb:{title.imdb_id} {title.title}"
    return title.title


# -- the service ---------------------------------------------------------------------------------------


class PlexLibraryService:
    def __init__(self, plex: PlexService, library: LibraryService) -> None:
        self._plex = plex
        self._library = library

    @property
    def available(self) -> bool:
        """Plex is connected: the Library page shows on the switch."""
        return self._plex.connected

    async def contents(self, *, profile_id: int) -> LibraryContents:
        """Every show and film of the last check, the shows with the person's follows and gaps;
        empty without Plex."""
        if not self._plex.connected:
            return LibraryContents()
        titles = await self._plex.titles()
        tracked = await self._library.tracked_ids(profile_id=profile_id)
        gaps: dict[int, int] = {}
        for entry in await self._library.gaps(profile_id=profile_id):
            if not entry.is_downloaded:
                gaps[entry.show.id] = gaps.get(entry.show.id, 0) + 1
        known = await self._library.cached_shows(
            title.show_id for title in titles if title.show_id is not None
        )
        shows = tuple(
            LibraryShow(
                title=title,
                following=title.show_id in tracked,
                gaps=gaps.get(title.show_id, 0) if title.show_id is not None else 0,
                ended=_ended(known.get(title.show_id) if title.show_id is not None else None),
            )
            for title in titles
            if title.kind == "show"
        )
        films = tuple(title for title in titles if title.kind == "movie")
        return LibraryContents(shows=shows, films=films, checked_at=await self._plex.checked_at())

    async def follow(self, title: PlexTitle, *, profile_id: int) -> bool:
        """Follow a show from Plex; whether it was added. A show AirGrabber didn't know yet has
        Plex checked again, so its episodes count."""
        result = await self._library.import_shows([follow_line(title)], profile_id=profile_id)
        if result.added and title.show_id is None:
            await self._plex.check(again=True)
        return bool(result.added or result.already_tracked)

    async def show(self, title: PlexTitle) -> Show | None:
        """The TVMaze show a show card opens: the cached one, else looked up by its ids or
        its name; None when TVMaze knows none."""
        return await self._library.find_show(
            show_id=title.show_id, tvdb_id=title.tvdb_id, imdb_id=title.imdb_id, name=title.title
        )


def _ended(show: Show | None) -> bool:
    return show is not None and (show.status or "").casefold() == "ended"


def library_counts(contents: LibraryContents) -> tuple[int, int]:
    return len(contents.shows), len(contents.films)


def page_slice[T](items: Sequence[T], shown: int) -> tuple[Sequence[T], int]:
    """The first `shown` items and how many more there are."""
    return items[:shown], max(0, len(items) - shown)
