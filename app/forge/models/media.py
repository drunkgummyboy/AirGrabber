"""What MediaForge knows about one video file."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel


class TypeHint(StrEnum):
    """The preset's media type: let the filename decide, or force movie / TV."""

    AUTO = "auto"
    MOVIE = "movie"
    TV = "tv"


class MediaKind(StrEnum):
    MOVIE = "movie"
    EPISODE = "episode"


class ParsedMedia(BaseModel, frozen=True):
    """Everything read from the filename alone, before any TMDB lookup."""

    source_name: str
    kind: MediaKind
    title: str
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    episode_end: int | None = None
    episode_title: str | None = None
    tags: tuple[str, ...] = ()
    # "Avatar" of "Avatar - The Last Airbender": searched when the whole title finds nothing.
    short_title: str | None = None
    quality: str | None = None  # what the file name says: "1080p H.265"


class MatchState(StrEnum):
    CONFIDENT = "confident"
    REMEMBERED = "remembered"
    UNCERTAIN = "uncertain"
    NOT_FOUND = "not_found"
    SKIPPED = "skipped"
    OFFLINE = "offline"


class ResolvedMedia(BaseModel, frozen=True):
    """The names to use for a file: TMDB's official data when matched, else the parsed data."""

    parsed: ParsedMedia
    title: str
    year: int | None
    episode_title: str | None
    match_state: MatchState
    tmdb_id: int | None = None
    poster_path: str | None = None
    season_poster_path: str | None = None
    backdrop_path: str | None = None
    original_title: str | None = None  # TMDB's title in the original language
    episode_known: bool = False  # TMDB's season lists this episode (its name came from TMDB)
    overview: str = ""  # the film's or show's summary (for its NFO file)
    episode_overview: str = ""

    @classmethod
    def offline(cls, parsed: ParsedMedia, state: MatchState = MatchState.OFFLINE) -> ResolvedMedia:
        return cls(
            parsed=parsed,
            title=parsed.title,
            year=parsed.year,
            episode_title=parsed.episode_title,
            match_state=state,
        )

    @property
    def kind(self) -> MediaKind:
        return self.parsed.kind

    @property
    def season(self) -> int | None:
        return self.parsed.season

    @property
    def episode(self) -> int | None:
        return self.parsed.episode

    @property
    def episode_end(self) -> int | None:
        return self.parsed.episode_end

    @property
    def ny(self) -> str:
        return f"{self.title} ({self.year})" if self.year else self.title
