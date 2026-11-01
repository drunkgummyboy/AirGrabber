"""The backup file (Settings → Backup, app/services/backup.py): the followed shows, the film
watchlist, the download history and the settings in one JSON file (UTF-8), to keep or to load
on another PC.

Its header names the format and its version (FORMAT_NAME, FORMAT_VERSION); a file of a newer
version isn't read. Every model here is frozen and ignores fields it doesn't know, and the
TVMaze / TMDB data inside (Show, Episode, Movie, DownloadRecord) is read by the fields this
version knows, so a later AirGrabber may add fields without a new format version."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from app.i18n import Language
from app.models.domain import DownloadRecord, Episode, Movie, Show
from app.models.settings import Accent
from app.models.types import ImdbId, UtcDatetime

FORMAT_NAME = "airgrabber-backup"
FORMAT_VERSION = 1
FILE_SUFFIX = ".airgrabber"


class BackupModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")


def _known_fields(model: type[BaseModel]) -> Any:
    """A before-validator: only the fields `model` has (a newer version's extra ones dropped)."""

    def keep(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: item for key, item in value.items() if key in model.model_fields}
        return value

    return BeforeValidator(keep)


KnownShow = Annotated[Show, _known_fields(Show)]
KnownEpisode = Annotated[Episode, _known_fields(Episode)]
KnownMovie = Annotated[Movie, _known_fields(Movie)]
KnownRecord = Annotated[DownloadRecord, _known_fields(DownloadRecord)]


class BackupShow(BackupModel):
    """A followed show with what the user chose for it: its automatic downloads (`auto_since`:
    from when) and the episodes marked "I already have this" (`have`, episode ids). Its
    episodes come along, so the marks have their episodes and the show draws before a sync."""

    show: KnownShow
    tracked_at: UtcDatetime
    auto_download: bool = False
    auto_since: UtcDatetime | None = None
    episodes: tuple[KnownEpisode, ...] = ()
    have: tuple[int, ...] = ()

    @model_validator(mode="after")
    def _episodes_are_its_own(self) -> Self:
        if any(episode.show_id != self.show.id for episode in self.episodes):
            raise ValueError(f"an episode of show {self.show.id} belongs to another show")
        return self


class BackupFilm(BackupModel):
    """A film on the watchlist: the TMDB data it was saved with, its "Download when it's out"
    switch and the release dates as last read (read again after a load)."""

    movie: KnownMovie
    added_at: UtcDatetime
    auto_download: bool = True
    imdb_id: ImdbId = None
    digital_release: date | None = None
    theatrical_release: date | None = None


class BackupGrab(BackupModel):
    """A downloaded release (the history, without its database id) and whether it was taken
    off the Downloads page."""

    record: KnownRecord
    cleared: bool = False


class BackupData(BackupModel):
    """The database part of a backup (BackupRepository)."""

    shows: tuple[BackupShow, ...] = ()
    films: tuple[BackupFilm, ...] = ()
    grabs: tuple[BackupGrab, ...] = ()


class BackupFile(BackupModel):
    """The whole file. `settings`: as Settings stores them (JSON values), without this PC's own
    markers and, unless the user chose otherwise, without keys and passwords."""

    format: Literal["airgrabber-backup"]
    version: int
    app_version: str = ""
    created: UtcDatetime
    shows: tuple[BackupShow, ...] = ()
    watchlist: tuple[BackupFilm, ...] = ()
    history: tuple[BackupGrab, ...] = ()
    settings: dict[str, Any] = Field(default_factory=dict)

    @property
    def data(self) -> BackupData:
        return BackupData(shows=self.shows, films=self.watchlist, grabs=self.history)


class BackupSummary(BackupModel):
    """What a backup holds, for the question before loading it and the line after saving."""

    shows: int
    films: int
    grabs: int
    settings: bool  # it has settings
    secrets: bool  # keys or passwords are in it
    created: UtcDatetime
    app_version: str = ""


class LoadMode(StrEnum):
    REPLACE = "replace"  # the shows, watchlist and settings become the backup's
    ADD = "add"  # the backup's shows and films join the current ones; settings stay


class RestoreCounts(BackupModel):
    """What loading the database part changed (BackupRepository.restore)."""

    shows_added: int = 0
    shows_removed: int = 0
    films_added: int = 0
    films_removed: int = 0
    grabs_added: int = 0


class KeptDownloads(StrEnum):
    """Whether a Replace kept this PC's download connection (app/services/backup.py)."""

    NONE = "none"  # where downloads go came from the backup (or the settings stayed anyway)
    LOCAL = "local"  # this PC sends its downloads to its own qBittorrent: that stays
    THIS_PC = "this_pc"  # the backup's went to the other PC's own qBittorrent: this PC's stays


class BackupLoadResult(BackupModel):
    """What a load did. `language` / `accent`: the settings' after it (they apply at the next
    start; the UI compares them with the running ones)."""

    mode: LoadMode
    counts: RestoreCounts
    settings_replaced: bool
    secrets_loaded: bool  # keys or passwords came from the file
    language: Language
    accent: Accent
    downloads_kept: KeptDownloads = KeptDownloads.NONE
