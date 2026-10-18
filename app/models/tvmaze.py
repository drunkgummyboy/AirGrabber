"""TVMaze wire format (https://www.tvmaze.com/api) → domain models.

Payloads are validated here, at the boundary; nothing downstream ever sees raw dicts.
Field names mirror the API; shapes were checked against live responses and the legacy cache.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.domain import Episode, Show
from app.models.types import OptionalDate, OptionalTime, strip_html


class _Wire(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class TVMazeImage(_Wire):
    medium: str | None = None
    original: str | None = None


class TVMazeRating(_Wire):
    average: float | None = None


class TVMazeCountry(_Wire):
    code: str | None = None  # ISO 3166-1 alpha-2, e.g. "US", "GB"


class TVMazeChannel(_Wire):
    """Either a broadcast `network` or a streaming `webChannel`."""

    id: int
    name: str
    country: TVMazeCountry | None = None  # null for worldwide streaming services


class TVMazeExternals(_Wire):
    imdb: str | None = None
    thetvdb: int | None = None


class TVMazeEpisode(_Wire):
    id: int
    name: str | None = None
    season: int
    number: int | None = None  # null for specials
    type: str = "regular"
    airdate: OptionalDate = None
    airtime: OptionalTime = None  # "" when unknown
    airstamp: datetime | None = None
    runtime: int | None = None
    rating: TVMazeRating = Field(default_factory=TVMazeRating)
    image: TVMazeImage | None = None
    summary: str | None = None

    def to_domain(self, show_id: int) -> Episode:
        return Episode(
            id=self.id,
            show_id=show_id,
            season=self.season,
            number=self.number,
            name=self.name or "",
            type=self.type,
            air_date=self.airdate,
            air_time=self.airtime,
            airstamp=self.airstamp,
            runtime=self.runtime,
            rating=self.rating.average,
            image_url=self.image.medium if self.image else None,
            summary=strip_html(self.summary),
        )


class TVMazeEmbedded(_Wire):
    episodes: list[TVMazeEpisode] = Field(default_factory=list)


class TVMazeShow(_Wire):
    id: int
    name: str
    type: str | None = None
    language: str | None = None
    genres: list[str] = Field(default_factory=list)
    status: str | None = None
    runtime: int | None = None
    average_runtime: int | None = Field(default=None, alias="averageRuntime")
    premiered: OptionalDate = None
    ended: OptionalDate = None
    weight: int | None = None
    rating: TVMazeRating = Field(default_factory=TVMazeRating)
    network: TVMazeChannel | None = None
    web_channel: TVMazeChannel | None = Field(default=None, alias="webChannel")
    externals: TVMazeExternals = Field(default_factory=TVMazeExternals)
    image: TVMazeImage | None = None
    summary: str | None = None
    updated: int | None = None
    embedded: TVMazeEmbedded | None = Field(default=None, alias="_embedded")

    def to_show(self) -> Show:
        channel = self.network or self.web_channel
        return Show(
            id=self.id,
            name=self.name,
            status=self.status,
            type=self.type,
            language=self.language,
            genres=tuple(self.genres),
            premiered=self.premiered,
            ended=self.ended,
            network=channel.name if channel else None,
            runtime=self.average_runtime or self.runtime,
            rating=self.rating.average,
            weight=self.weight or 0,
            imdb_id=self.externals.imdb,
            poster_url=self.image.medium if self.image else None,
            poster_url_original=self.image.original if self.image else None,
            summary=strip_html(self.summary),
            updated=self.updated,
        )

    @property
    def country(self) -> str | None:
        """The country code of the show's network (or streaming service), when it has one."""
        for channel in (self.network, self.web_channel):
            if channel is not None and channel.country is not None and channel.country.code:
                return channel.country.code
        return None

    def to_episodes(self) -> list[Episode]:
        """Episodes from `?embed[]=episodes`; empty when the payload wasn't embedded."""
        if self.embedded is None:
            return []
        return [episode.to_domain(self.id) for episode in self.embedded.episodes]


class TVMazeScheduleItem(TVMazeEpisode):
    """An entry from /schedule: one airing episode with its show embedded."""

    show: TVMazeShow


class TVMazeSearchResult(_Wire):
    score: float
    show: TVMazeShow
