"""Validated, immutable domain models. The UI renders these and nothing else."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time
from enum import StrEnum
from typing import Any, Self
from urllib.parse import quote

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.types import Count, ImdbId, InfoHash, UtcDatetime

TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/"


class DomainModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class MediaType(StrEnum):
    TV = "tv"
    MOVIE = "movie"


class Show(DomainModel):
    """A TV series. Ids are TVMaze ids."""

    id: int
    name: str
    status: str | None = None  # Running / Ended / To Be Determined / In Development
    type: str | None = None  # Scripted / Animation / Reality / ...
    language: str | None = None
    genres: tuple[str, ...] = ()
    premiered: date | None = None
    ended: date | None = None
    network: str | None = None  # broadcast network or streaming service
    runtime: int | None = None  # minutes
    rating: float | None = None
    weight: int = 0  # TVMaze popularity, 0-100
    imdb_id: ImdbId = None
    poster_url: str | None = None
    poster_url_original: str | None = None
    summary: str = ""
    updated: int | None = None  # TVMaze change stamp; lets sync skip unchanged shows

    @property
    def is_ended(self) -> bool:
        return self.status == "Ended"

    @property
    def year(self) -> int | None:
        return self.premiered.year if self.premiered else None

    @property
    def imdb_url(self) -> str | None:
        return f"https://www.imdb.com/title/{self.imdb_id}/" if self.imdb_id else None


class Episode(DomainModel):
    id: int
    show_id: int
    season: int
    number: int | None = None  # None for specials
    name: str = ""
    type: str = "regular"
    air_date: date | None = None  # network-local calendar date
    air_time: time | None = None  # network-local
    airstamp: UtcDatetime | None = None  # exact broadcast instant
    runtime: int | None = None
    rating: float | None = None
    image_url: str | None = None
    summary: str = ""

    @property
    def code(self) -> str:
        if self.number is None:
            return f"S{self.season:02d} Special"
        return f"S{self.season:02d}E{self.number:02d}"

    def has_aired(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        if self.airstamp is not None:
            return self.airstamp <= now
        return self.air_date is not None and self.air_date <= now.date()


class LibraryEntry(DomainModel):
    """A show in the user's tracked library."""

    show: Show
    tracked_at: UtcDatetime
    auto_download: bool = False
    synced_at: UtcDatetime | None = None  # None = metadata never synced from TVMaze


class LibraryStats(DomainModel):
    tracked_shows: int
    episodes: int
    last_synced: UtcDatetime | None = None


class CalendarEntry(DomainModel):
    """One episode slot on the calendar grid, produced by a single SQL JOIN."""

    show: Show
    episode: Episode
    is_tracked: bool
    is_downloaded: bool = False


class ShowSearchResult(DomainModel):
    show: Show
    is_tracked: bool


class SyncProgress(DomainModel):
    done: int
    total: int
    current: str | None = None  # name of the show just refreshed


class SyncReport(DomainModel):
    checked: int  # tracked shows considered
    refreshed: int  # re-fetched from TVMaze
    failed: int
    started_at: UtcDatetime
    finished_at: UtcDatetime


class ImportResult(DomainModel):
    added: tuple[str, ...] = ()  # display names
    already_tracked: tuple[str, ...] = ()
    not_found: tuple[str, ...] = ()  # the input lines that matched nothing
    failed: tuple[str, ...] = ()  # input lines that hit a TVMaze/network error


class Movie(DomainModel):
    """A film. Ids are TMDB ids. (Checked against live TMDB payloads in Step 3.)"""

    id: int
    title: str
    original_title: str | None = None
    original_language: str | None = None
    overview: str = ""
    release_date: date | None = None
    poster_path: str | None = None
    backdrop_path: str | None = None
    vote_average: float = 0.0
    vote_count: int = 0
    popularity: float = 0.0
    genre_ids: tuple[int, ...] = ()
    imdb_id: ImdbId = None

    @property
    def year(self) -> int | None:
        return self.release_date.year if self.release_date else None

    def poster_url(self, size: str = "w342") -> str | None:
        return f"{TMDB_IMAGE_BASE}{size}{self.poster_path}" if self.poster_path else None


class MovieDetails(DomainModel):
    """Per-film extras from TMDB's details endpoint; release data is for one region."""

    id: int  # TMDB id
    imdb_id: ImdbId = None
    runtime: int | None = None  # minutes
    certification: str | None = None  # e.g. "PG-13"
    digital_release: date | None = None
    theatrical_release: date | None = None

    @property
    def imdb_url(self) -> str | None:
        return f"https://www.imdb.com/title/{self.imdb_id}/" if self.imdb_id else None


class DiscoverShow(DomainModel):
    """A TV show from TMDB lists. Ids are TMDB ids — not TVMaze ids."""

    id: int
    name: str
    original_name: str | None = None
    original_language: str | None = None
    overview: str = ""
    first_air_date: date | None = None
    poster_path: str | None = None
    vote_average: float = 0.0
    vote_count: int = 0
    popularity: float = 0.0
    origin_country: tuple[str, ...] = ()

    @property
    def year(self) -> int | None:
        return self.first_air_date.year if self.first_air_date else None

    def poster_url(self, size: str = "w185") -> str | None:
        return f"{TMDB_IMAGE_BASE}{size}{self.poster_path}" if self.poster_path else None


class TVDetails(DomainModel):
    """Per-show extras from TMDB's details endpoint."""

    id: int  # TMDB id
    status: str | None = None  # "Returning Series", "Ended", ...
    number_of_seasons: int | None = None
    imdb_id: ImdbId = None
    tvdb_id: int | None = None


class DiscoverShowEntry(DomainModel):
    """A TMDB show on a TV dashboard shelf, flagged when it is (probably) in the library."""

    show: DiscoverShow
    is_tracked: bool


class TVDashboard(DomainModel):
    trending: tuple[DiscoverShowEntry, ...] = ()
    on_air: tuple[DiscoverShowEntry, ...] = ()
    premieres: tuple[DiscoverShowEntry, ...] = ()
    failed: tuple[str, ...] = ()  # names of the shelves that couldn't be loaded ("trending", ...)


class MovieDashboard(DomainModel):
    just_dropped: tuple[Movie, ...] = ()
    trending: tuple[Movie, ...] = ()
    upcoming: tuple[Movie, ...] = ()
    failed: tuple[str, ...] = ()  # names of the shelves that couldn't be loaded


class SearchKind(StrEnum):
    EPISODE = "episode"
    SEASON = "season"  # whole-season pack
    MOVIE = "movie"


class SearchRequest(DomainModel):
    """What to look for on the torrent indexers; every indexer answers from these fields."""

    kind: SearchKind
    title: str  # show or movie title as the user knows it
    year: int | None = None
    season: int | None = None  # EPISODE and SEASON
    episode: int | None = None  # EPISODE only
    imdb_id: ImdbId = None
    tvdb_id: int | None = None
    queries: tuple[str, ...] = ()  # free-text queries, best first (built by the search service)


class Resolution(StrEnum):
    UHD = "2160p"
    FULL_HD = "1080p"
    HD = "720p"
    SD = "480p"


def _release_token(pattern: str) -> re.Pattern[str]:
    # Release names separate tokens with '.', '-', '_' or spaces; \b would treat '_' as a word char.
    return re.compile(rf"(?<![a-z0-9])(?:{pattern})(?![a-z0-9])", re.IGNORECASE)


_RESOLUTION_PATTERNS = (
    (Resolution.UHD, _release_token(r"2160p|4k|uhd")),
    (Resolution.FULL_HD, _release_token(r"1080[pi]")),
    (Resolution.HD, _release_token(r"720p")),
    (Resolution.SD, _release_token(r"480p|576p|dvdrip|sdtv")),
)
_HEVC_PATTERN = _release_token(r"x\.?265|h\.?265|hevc")
_BTIH_PATTERN = re.compile(r"urn:btih:([0-9a-f]{40}|[a-z2-7]{32})", re.IGNORECASE)


class TorrentRelease(DomainModel):
    """A search hit from any indexer, normalized so results from every source sort together."""

    title: str
    source: str  # indexer that produced the hit
    size_bytes: Count = 0
    seeders: Count = 0
    leechers: Count = 0
    info_hash: InfoHash = None
    magnet_uri: str | None = None
    # Direct .torrent link. Kept out of repr: Prowlarr's links embed its API key.
    download_url: str | None = Field(default=None, repr=False)
    info_url: str | None = None  # indexer's details page
    published_at: UtcDatetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _hash_from_magnet(cls, data: Any) -> Any:
        if isinstance(data, dict) and not data.get("info_hash"):
            if match := _BTIH_PATTERN.search(data.get("magnet_uri") or ""):
                return {**data, "info_hash": match.group(1)}
        return data

    @model_validator(mode="after")
    def _must_be_fetchable(self) -> Self:
        if not (self.info_hash or self.magnet_uri or self.download_url):
            raise ValueError("release has no info hash, magnet URI or download URL")
        return self

    @property
    def resolution(self) -> Resolution | None:
        return next((res for res, pattern in _RESOLUTION_PATTERNS if pattern.search(self.title)), None)

    @property
    def is_hevc(self) -> bool:
        return bool(_HEVC_PATTERN.search(self.title))

    @property
    def magnet(self) -> str | None:
        if self.magnet_uri:
            return self.magnet_uri
        if self.info_hash:
            return f"magnet:?xt=urn:btih:{self.info_hash}&dn={quote(self.title)}"
        return None


class DownloadRecord(DomainModel):
    """A grabbed release; used to flag episodes and movies that were already downloaded."""

    id: int | None = None  # assigned by the database
    media_type: MediaType
    media_id: int  # TVMaze show id (tv) or TMDB movie id (movie)
    season: int | None = None
    episode: int | None = None  # None with a season set = whole-season pack
    release_title: str
    info_hash: InfoHash = None
    source: str | None = None
    size_bytes: int | None = None
    saved_path: str | None = None
    downloaded_at: UtcDatetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _tv_needs_season(self) -> Self:
        if self.media_type is MediaType.TV and self.season is None:
            raise ValueError("TV downloads must record a season")
        return self


class GrabTarget(DomainModel):
    """What a grabbed release is for: recorded in the download history, drives calendar flags."""

    media_type: MediaType
    media_id: int  # TVMaze show id (tv) or TMDB movie id (movie)
    season: int | None = None
    episode: int | None = None  # None with a season set = whole-season pack

    @model_validator(mode="after")
    def _tv_needs_season(self) -> Self:
        if self.media_type is MediaType.TV and self.season is None:
            raise ValueError("TV grabs must name a season")
        if self.episode is not None and self.season is None:
            raise ValueError("an episode needs a season")
        return self


class GrabOutcome(StrEnum):
    SAVED = "saved"  # .torrent written to the watch folder
    SENT = "sent"  # magnet handed to Download Station


class GrabResult(DomainModel):
    outcome: GrabOutcome
    path: str | None = None  # the .torrent file written (SAVED)
    message: str  # short text for the UI


class IndexerState(StrEnum):
    WAITING = "waiting"
    SEARCHING = "searching"
    FOUND = "found"
    EMPTY = "empty"
    FAILED = "failed"
    SKIPPED = "skipped"


class IndexerStatus(DomainModel):
    """Where one indexer stands in a running torrent search."""

    name: str  # BaseIndexer.name
    label: str  # BaseIndexer.label
    state: IndexerState
    count: int = 0  # raw hits (FOUND), before merging and filtering
    message: str | None = None  # why it was SKIPPED or FAILED
    web_url: str | None = None  # that indexer's own search page for the first query


class SearchUpdate(DomainModel):
    """A snapshot of a running torrent search; every update replaces the previous one."""

    statuses: tuple[IndexerStatus, ...]
    releases: tuple[TorrentRelease, ...]  # merged, relevant, settings-filtered; seeders desc
    # The merged releases that failed the relevance or settings filters; seeders desc.
    hidden: tuple[TorrentRelease, ...] = ()
    done: bool


class UpdateInfo(DomainModel):
    current: str  # version running now
    latest: str  # version on GitHub
    available: bool  # latest is newer than current
    installable: bool  # False in a development copy, which is never overwritten
