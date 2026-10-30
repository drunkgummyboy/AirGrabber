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

from app.models.domain import (
    CastMember,
    DiscoverShow,
    Movie,
    MovieDetails,
    TitleInfo,
    TVDetails,
    Video,
)
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
    genre_ids: list[int] = Field(default_factory=list)

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
            genre_ids=tuple(self.genre_ids),
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


class TMDBGenre(_Wire):
    name: str = ""


def _genre_names(genres: Sequence[TMDBGenre]) -> tuple[str, ...]:
    return tuple(name for genre in genres if (name := genre.name.strip()))


class TMDBMovieInfo(_Wire):
    """/movie/{id}?language=…: what the info window shows (overview and genres in that
    language)."""

    id: int
    overview: str | None = None
    genres: list[TMDBGenre] = Field(default_factory=list)
    backdrop_path: str | None = None
    poster_path: str | None = None
    runtime: int | None = None
    status: str | None = None
    original_language: str | None = None
    vote_average: float | None = None
    vote_count: int | None = None

    def to_info(self) -> TitleInfo:
        return TitleInfo(
            id=self.id,
            overview=(self.overview or "").strip(),
            genres=_genre_names(self.genres),
            backdrop_path=self.backdrop_path or None,
            poster_path=self.poster_path or None,
            runtime=self.runtime or None,  # TMDB reports 0 when unknown
            status=self.status or None,
            original_language=self.original_language or None,
            vote_average=self.vote_average or 0.0,
            vote_count=self.vote_count or 0,
        )


class TMDBTVInfo(_Wire):
    """/tv/{id}?language=…: what the info window shows of a show."""

    id: int
    overview: str | None = None
    genres: list[TMDBGenre] = Field(default_factory=list)
    backdrop_path: str | None = None
    poster_path: str | None = None
    number_of_seasons: int | None = None
    status: str | None = None
    original_language: str | None = None
    vote_average: float | None = None
    vote_count: int | None = None

    def to_info(self) -> TitleInfo:
        return TitleInfo(
            id=self.id,
            overview=(self.overview or "").strip(),
            genres=_genre_names(self.genres),
            backdrop_path=self.backdrop_path or None,
            poster_path=self.poster_path or None,
            seasons=self.number_of_seasons or None,
            status=self.status or None,
            original_language=self.original_language or None,
            vote_average=self.vote_average or 0.0,
            vote_count=self.vote_count or 0,
        )


class TMDBVideo(_Wire):
    """One entry of /movie/{id}/videos or /tv/{id}/videos."""

    key: str = ""
    site: str = ""
    type: str = ""
    official: bool = False
    iso_639_1: str | None = None
    name: str = ""
    published_at: _OptionalDatetime = None

    def to_video(self) -> Video | None:
        """None for an entry without a key (nothing to open)."""
        key = self.key.strip()
        if not key:
            return None
        return Video(
            key=key,
            site=self.site,
            type=self.type,
            official=self.official,
            language=(self.iso_639_1 or "").strip().lower() or None,
            name=self.name,
            published_at=self.published_at,
        )


class TMDBVideos(_Wire):
    results: list[TMDBVideo] = Field(default_factory=list)

    def to_videos(self) -> list[Video]:
        return [video for item in self.results if (video := item.to_video()) is not None]


class TMDBCastCredit(_Wire):
    """A cast entry of /movie/{id}/credits."""

    name: str = ""
    character: str | None = None
    profile_path: str | None = None

    def to_member(self) -> CastMember | None:
        name = self.name.strip()
        if not name:
            return None
        return CastMember(
            name=name,
            character=(self.character or "").strip(),
            profile_path=self.profile_path or None,
        )


class TMDBCredits(_Wire):
    """/movie/{id}/credits (the cast in billing order)."""

    cast: list[TMDBCastCredit] = Field(default_factory=list)

    def to_cast(self) -> list[CastMember]:
        return [member for item in self.cast if (member := item.to_member()) is not None]


class TMDBRole(_Wire):
    character: str | None = None
    episode_count: int | None = None


class TMDBAggregateCast(_Wire):
    """A cast entry of /tv/{id}/aggregate_credits: one actor with every part they played."""

    name: str = ""
    profile_path: str | None = None
    roles: list[TMDBRole] = Field(default_factory=list)

    def to_member(self) -> CastMember | None:
        name = self.name.strip()
        if not name:
            return None
        # The part they played most (the first such when tied).
        named = [role for role in self.roles if (role.character or "").strip()]
        main = max(named, key=lambda role: role.episode_count or 0, default=None)
        return CastMember(
            name=name,
            character=(main.character or "").strip() if main is not None else "",
            profile_path=self.profile_path or None,
        )


class TMDBAggregateCredits(_Wire):
    """/tv/{id}/aggregate_credits (the whole run's cast, the most seen first)."""

    cast: list[TMDBAggregateCast] = Field(default_factory=list)

    def to_cast(self) -> list[CastMember]:
        return [member for item in self.cast if (member := item.to_member()) is not None]


class TMDBFindResults(_Wire):
    """/find/{external_id}: what TMDB knows under another site's id (only shows are used)."""

    tv_results: list[TMDBTVItem] = Field(default_factory=list)

    def first_tv_id(self) -> int | None:
        return self.tv_results[0].id if self.tv_results else None


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
