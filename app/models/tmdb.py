"""TMDB wire format (https://developer.themoviedb.org/reference) → domain models.

Payloads are validated here, at the boundary; nothing downstream ever sees raw dicts.
Field names mirror the API; shapes were checked against live responses (tests/fixtures/tmdb).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from enum import IntEnum
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from app.models.domain import DiscoverShow, Movie, MovieDetails, TVDetails
from app.models.types import OptionalDate


def _empty_to_none(value: object) -> object:
    return None if value == "" else value


# Release dates are ISO datetimes ("2026-10-06T00:00:00.000Z"); "" when unknown.
_OptionalDatetime = Annotated[datetime | None, BeforeValidator(_empty_to_none)]


class ReleaseType(IntEnum):
    """TMDB release types (`release_dates[].type`)."""

    PREMIERE = 1
    THEATRICAL_LIMITED = 2
    THEATRICAL = 3
    DIGITAL = 4
    PHYSICAL = 5
    TV = 6


class _Wire(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


class TMDBMovieItem(_Wire):
    """A film in a list response: /trending/movie, /discover/movie, /search/movie."""

    id: int
    title: str
    original_title: str | None = None
    original_language: str | None = None
    overview: str | None = None
    release_date: OptionalDate = None  # for /discover with a region: that region's release
    poster_path: str | None = None
    backdrop_path: str | None = None
    vote_average: float | None = None
    vote_count: int | None = None
    popularity: float | None = None
    genre_ids: list[int] = Field(default_factory=list)

    def to_movie(self) -> Movie:
        return Movie(
            id=self.id,
            title=self.title,
            original_title=self.original_title,
            original_language=self.original_language,
            overview=self.overview or "",
            release_date=self.release_date,
            poster_path=self.poster_path or None,
            backdrop_path=self.backdrop_path or None,
            vote_average=self.vote_average or 0.0,
            vote_count=self.vote_count or 0,
            popularity=self.popularity or 0.0,
            genre_ids=tuple(self.genre_ids),
        )


class TMDBTVItem(_Wire):
    """A show in a list response: /trending/tv, /tv/on_the_air, /discover/tv."""

    id: int
    name: str
    original_name: str | None = None
    original_language: str | None = None
    overview: str | None = None
    first_air_date: OptionalDate = None
    poster_path: str | None = None
    vote_average: float | None = None
    vote_count: int | None = None
    popularity: float | None = None
    origin_country: list[str] = Field(default_factory=list)

    def to_discover_show(self) -> DiscoverShow:
        return DiscoverShow(
            id=self.id,
            name=self.name,
            original_name=self.original_name,
            original_language=self.original_language,
            overview=self.overview or "",
            first_air_date=self.first_air_date,
            poster_path=self.poster_path or None,
            vote_average=self.vote_average or 0.0,
            vote_count=self.vote_count or 0,
            popularity=self.popularity or 0.0,
            origin_country=tuple(self.origin_country),
        )


class TMDBExternalIds(_Wire):
    """`append_to_response=external_ids`."""

    imdb_id: str | None = None
    tvdb_id: int | None = None


class TMDBReleaseDate(_Wire):
    type: int
    certification: str | None = None
    release_date: _OptionalDatetime = None

    @property
    def day(self) -> date | None:
        return self.release_date.date() if self.release_date else None


class TMDBCountryReleases(_Wire):
    iso_3166_1: str
    release_dates: list[TMDBReleaseDate] = Field(default_factory=list)


class TMDBReleaseDates(_Wire):
    """`append_to_response=release_dates`: release dates grouped per country."""

    results: list[TMDBCountryReleases] = Field(default_factory=list)

    def for_region(self, region: str) -> list[TMDBReleaseDate]:
        region = region.upper()
        return next(
            (entry.release_dates for entry in self.results if entry.iso_3166_1.upper() == region),
            [],
        )


class TMDBMovieDetails(_Wire):
    """/movie/{id}?append_to_response=release_dates,external_ids"""

    id: int
    imdb_id: str | None = None
    runtime: int | None = None
    external_ids: TMDBExternalIds = Field(default_factory=TMDBExternalIds)
    release_dates: TMDBReleaseDates = Field(default_factory=TMDBReleaseDates)

    def to_details(self, region: str) -> MovieDetails:
        releases = self.release_dates.for_region(region)
        return MovieDetails(
            id=self.id,
            imdb_id=self.imdb_id or self.external_ids.imdb_id,
            runtime=self.runtime or None,  # TMDB reports 0 when unknown
            certification=_certification(releases),
            digital_release=_earliest(releases, ReleaseType.DIGITAL),
            theatrical_release=(
                _earliest(releases, ReleaseType.THEATRICAL)
                or _earliest(releases, ReleaseType.THEATRICAL_LIMITED)
            ),
        )


class TMDBTVDetails(_Wire):
    """/tv/{id}?append_to_response=external_ids"""

    id: int
    status: str | None = None
    number_of_seasons: int | None = None
    external_ids: TMDBExternalIds = Field(default_factory=TMDBExternalIds)

    def to_details(self) -> TVDetails:
        return TVDetails(
            id=self.id,
            status=self.status or None,
            number_of_seasons=self.number_of_seasons,
            imdb_id=self.external_ids.imdb_id,
            tvdb_id=self.external_ids.tvdb_id,
        )


def _earliest(releases: Sequence[TMDBReleaseDate], release_type: ReleaseType) -> date | None:
    days = [day for r in releases if r.type == release_type and (day := r.day) is not None]
    return min(days, default=None)


def _certification(releases: Sequence[TMDBReleaseDate]) -> str | None:
    """First non-empty rating, preferring the digital release's, then the theatrical one's."""
    rated = [(r.type, rating) for r in releases if (rating := (r.certification or "").strip())]
    for preferred in (ReleaseType.DIGITAL, ReleaseType.THEATRICAL):
        for release_type, rating in rated:
            if release_type == preferred:
                return rating
    return rated[0][1] if rated else None
