"""The header's spotlight band: which titles it shows (`SpotlightService.spotlight`) and their
backdrops (`backdrop_url`).

Series: the episodes of followed shows airing today (the local day: an episode's airstamp in
local time, else its listed air date), in airing order; none today: the next ones in the coming
7 days; none either: TMDB's trending shows (the TV dashboard's, cached by DiscoveryService).
Movies: the films first released digitally in the last 14 days, then trending films to fill up.
At most MAX_ITEMS. Without a TMDB key only followed shows' episodes can show.

Backdrops come from TMDB through DiscoveryService's info-window extras (cached 24 h, shared with
the info windows): a film's list entry often has one already. Any TMDB problem only leaves the
backdrop out (the band then shows its glow).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Sequence
from datetime import date, datetime, time, timedelta, tzinfo

from app.models import (
    CalendarEntry,
    DiscoverShow,
    Episode,
    Movie,
    MovieDashboard,
    Spotlight,
    SpotlightItem,
    SpotlightReason,
    SpotlightSide,
    TitleExtras,
)
from app.services.discovery import DiscoveryService
from app.services.library import LibraryService
from app.services.tmdb import TMDBError

log = logging.getLogger(__name__)

MAX_ITEMS = 5
AHEAD_DAYS = 7  # without an episode today: those of the coming week
POSTER_SIZE = "w185"  # TMDB's poster width for the band's small poster (44 units wide)

Clock = Callable[[], datetime]


def _local_now() -> datetime:
    return datetime.now().astimezone()


def air_moment(episode: Episode, zone: tzinfo | None) -> tuple[date | None, datetime | None]:
    """The local day an episode airs and, when its time is known, the instant in `zone`."""
    if episode.airstamp is not None:
        local = episode.airstamp.astimezone(zone)
        return local.date(), local
    return episode.air_date, None


def _episode_item(entry: CalendarEntry, reason: SpotlightReason, day: date) -> SpotlightItem:
    return SpotlightItem(
        reason=reason,
        title=entry.show.name,
        show=entry.show,
        episode=entry.episode,
        airs_at=entry.episode.airstamp,
        air_day=day,
        poster_url=entry.show.poster_url,
    )


def pick_episodes(
    entries: Iterable[CalendarEntry], now: datetime
) -> tuple[tuple[SpotlightItem, ...], int]:
    """The followed shows' episodes for the band at `now` (aware; its time zone decides the
    days): today's in airing order, else those of the next AHEAD_DAYS days; at most MAX_ITEMS,
    and how many there are in all (the sub-line's "3 episodes today")."""
    today = now.date()
    horizon = today + timedelta(days=AHEAD_DAYS)
    dated: list[tuple[date, datetime, str, int, int, CalendarEntry]] = []
    for entry in entries:
        if not entry.is_tracked:
            continue
        day, moment = air_moment(entry.episode, now.tzinfo)
        if day is None or not today <= day <= horizon:
            continue
        # Without a time: after the episodes of that day with one.
        order = moment or datetime.combine(day, time.max, tzinfo=now.tzinfo)
        episode = entry.episode
        dated.append(
            (day, order, entry.show.name.casefold(), episode.season, episode.number or 0, entry)
        )
    dated.sort(key=lambda row: row[:5])
    tonight = [row for row in dated if row[0] == today]
    if tonight:
        picked, reason = tonight, SpotlightReason.TODAY
    else:
        picked, reason = dated, SpotlightReason.COMING
    items = tuple(_episode_item(row[5], reason, row[0]) for row in picked[:MAX_ITEMS])
    return items, len(picked)


def trending_show_item(show: DiscoverShow) -> SpotlightItem:
    return SpotlightItem(
        reason=SpotlightReason.TRENDING,
        title=show.name,
        discover_show=show,
        poster_url=show.poster_url(POSTER_SIZE),
    )


def pick_films(dashboard: MovieDashboard) -> tuple[SpotlightItem, ...]:
    """The films just released digitally first, then trending ones to fill up (each film
    once), at most MAX_ITEMS."""
    items: list[SpotlightItem] = []
    seen: set[int] = set()
    shelves: Sequence[tuple[SpotlightReason, Sequence[Movie]]] = (
        (SpotlightReason.NEW_TO_STREAM, dashboard.just_dropped),
        (SpotlightReason.TRENDING, dashboard.trending),
    )
    for reason, films in shelves:
        for film in films:
            if len(items) == MAX_ITEMS:
                return tuple(items)
            if film.id in seen:
                continue
            seen.add(film.id)
            items.append(
                SpotlightItem(
                    reason=reason,
                    title=film.title,
                    movie=film,
                    poster_url=film.poster_url(POSTER_SIZE),
                )
            )
    return tuple(items)


class SpotlightService:
    """The spotlight band's titles per side (see the module docstring). `clock`: the local
    time now, aware (tests set it)."""

    def __init__(
        self,
        library: LibraryService,
        discovery: DiscoveryService,
        *,
        clock: Clock = _local_now,
    ) -> None:
        self._library = library
        self._discovery = discovery
        self._clock = clock

    async def spotlight(self, side: SpotlightSide) -> Spotlight:
        if side is SpotlightSide.MOVIES:
            return Spotlight(side=side, items=await self._films())
        now = self._clock()
        today = now.date()
        # A day either side: the listed (network) air dates may differ from the local days.
        entries = await self._library.calendar(
            today - timedelta(days=1), today + timedelta(days=AHEAD_DAYS + 1)
        )
        items, airing = pick_episodes(entries, now)
        if items:
            return Spotlight(side=side, items=items, airing=airing)
        return Spotlight(side=side, items=await self._trending_shows())

    async def _trending_shows(self) -> tuple[SpotlightItem, ...]:
        if not self._discovery.has_tmdb_key:
            return ()
        try:
            dashboard = await self._discovery.tv_dashboard()
        except TMDBError as exc:
            log.info("Spotlight: no trending shows: %s", exc)
            return ()
        return tuple(trending_show_item(entry.show) for entry in dashboard.trending[:MAX_ITEMS])

    async def _films(self) -> tuple[SpotlightItem, ...]:
        if not self._discovery.has_tmdb_key:
            return ()
        try:
            dashboard = await self._discovery.movie_dashboard()
        except TMDBError as exc:
            log.info("Spotlight: no films: %s", exc)
            return ()
        return pick_films(dashboard)

    async def backdrop_url(self, item: SpotlightItem, size: str) -> str | None:
        """The title's TMDB backdrop in TMDB's `size` ("w1280"); None without a key, when TMDB
        has none or can't be reached."""
        if item.movie is not None and (url := item.movie.backdrop_url(size)) is not None:
            return url
        if not self._discovery.has_tmdb_key:
            return None
        extras: TitleExtras | None
        try:
            if item.movie is not None:
                extras = await self._discovery.movie_extras(item.movie.id)
            elif item.discover_show is not None:
                extras = await self._discovery.tv_extras(item.discover_show.id)
            elif item.show is not None:
                extras = await self._discovery.show_extras(item.show)
            else:
                return None
        except TMDBError as exc:
            log.info("Spotlight: no backdrop for %s: %s", item.title, exc)
            return None
        return extras.info.backdrop_url(size) if extras is not None else None
