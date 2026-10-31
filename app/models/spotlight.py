"""The header's spotlight band: what it shows on the Series and the Movies side
(app/services/spotlight.py picks it, app/ui/spotlight_band.py shows it)."""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from app.models.domain import DiscoverShow, DomainModel, Episode, Movie, Show
from app.models.types import UtcDatetime


class SpotlightSide(StrEnum):
    """The side of the band's Series / Movies switch whose titles it shows."""

    SERIES = "series"
    MOVIES = "movies"


class SpotlightReason(StrEnum):
    """Why a title is in the spotlight (its tag: "TONIGHT · 21:00", "TRENDING", …)."""

    TODAY = "today"  # an episode of a followed show airing today (the local day)
    COMING = "coming"  # one airing in the next 7 days (none airs today)
    TRENDING = "trending"  # TMDB's trending shows or films of the week
    NEW_TO_STREAM = "new_to_stream"  # a film first released digitally in the last 14 days


class SpotlightItem(DomainModel):
    """One title in the spotlight: a followed show's episode (`show` + `episode`), a TMDB show
    (`discover_show`) or a film (`movie`)."""

    reason: SpotlightReason
    title: str  # the show's name or the film's title
    show: Show | None = None  # a followed show (TVMaze): its info window
    episode: Episode | None = None
    discover_show: DiscoverShow | None = None  # a TMDB show (trending)
    movie: Movie | None = None
    airs_at: UtcDatetime | None = None  # when the episode airs, when TVMaze knows the time
    air_day: date | None = None  # the local day it airs
    poster_url: str | None = None

    @property
    def key(self) -> tuple[str, int, int]:
        """Which title this is (the band keeps its place across reloads by it)."""
        if self.episode is not None:
            return ("episode", self.episode.show_id, self.episode.id)
        if self.discover_show is not None:
            return ("tmdb-tv", self.discover_show.id, 0)
        if self.movie is not None:
            return ("movie", self.movie.id, 0)
        return ("show", self.show.id if self.show is not None else 0, 0)


class Spotlight(DomainModel):
    """One side's titles, at most five, best first. `airing`: how many episodes of followed
    shows air today (TODAY) or in the next 7 days (COMING), also those not in `items`."""

    side: SpotlightSide
    items: tuple[SpotlightItem, ...] = ()
    airing: int = 0

    @property
    def reason(self) -> SpotlightReason | None:
        """The first title's reason (what `airing` counts); None without titles."""
        return self.items[0].reason if self.items else None
