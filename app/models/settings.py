"""User preferences: stored as key/value rows, validated as a single model."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    SecretStr,
    field_validator,
)


class QualityProfile(StrEnum):
    ANY = "Any"
    HD = "720p"
    FULL_HD = "1080p"
    UHD = "2160p"
    HEVC = "x265"


class DownloadMode(StrEnum):
    APP = "app"  # open the .torrent / magnet in the torrent program on this PC
    FOLDER = "folder"  # save the .torrent into `download_dir` (else magnet → Download Station)


class Weekday(StrEnum):
    MONDAY = "Monday"
    TUESDAY = "Tuesday"
    WEDNESDAY = "Wednesday"
    THURSDAY = "Thursday"
    FRIDAY = "Friday"
    SATURDAY = "Saturday"
    SUNDAY = "Sunday"

    @property
    def weekday(self) -> int:
        """0 = Monday, matching date.weekday()."""
        return list(Weekday).index(self)


def _split_csv(value: Any) -> Any:
    if isinstance(value, str):
        return tuple(part.strip().lower() for part in value.split(",") if part.strip())
    return value


KeywordList = Annotated[tuple[str, ...], BeforeValidator(_split_csv)]


def _reveal(value: SecretStr) -> str:
    return value.get_secret_value()


# Persisted verbatim in JSON mode; repr() and logs keep showing '**********'.
StoredSecret = Annotated[SecretStr, PlainSerializer(_reveal, return_type=str, when_used="json")]


def _normalize_url(value: Any) -> Any:
    """'' or an absolute http(s) address without a trailing slash."""
    if not isinstance(value, str):
        return value
    text = value.strip().rstrip("/")
    if not text:
        return ""
    parts = urlsplit(text)
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        raise ValueError("Use a full address like https://nas.local:5001")
    return text


ServiceUrl = Annotated[str, BeforeValidator(_normalize_url)]


class Settings(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore")

    quality: QualityProfile = QualityProfile.FULL_HD
    # FOLDER keeps the 1.x/2.0 behaviour for existing users; new users choose on first run.
    download_mode: DownloadMode = DownloadMode.FOLDER
    download_dir: Path = Field(default_factory=lambda: Path.home() / "Downloads")
    first_day: Weekday = Weekday.MONDAY
    weeks_to_show: int = Field(default=3, ge=1, le=5)
    show_suggestions: bool = True  # the calendar's suggested (untracked) show per day
    tmdb_api_key: StoredSecret = SecretStr("")
    excluded_keywords: KeywordList = ("cam", "telesync", "hdcam", "sample")
    preferred_groups: KeywordList = ()
    prowlarr_url: ServiceUrl = ""
    prowlarr_api_key: StoredSecret = SecretStr("")
    # The Prowlarr on this PC that AirGrabber already added its public sites to (its base URL);
    # set by the automatic setup only, never shown in the UI.
    prowlarr_seeded_url: str = ""
    ds_url: ServiceUrl = ""  # Synology DSM address, e.g. https://nas.local:5001
    ds_username: str = ""
    ds_password: StoredSecret = SecretStr("")
    ds_verify_tls: bool = True  # NAS boxes often use self-signed certificates
    ds_destination: str = ""  # Download Station folder ("video/tv"); "" = its default
    disabled_indexers: KeywordList = ()
    # The first-run welcome was completed or dismissed (or skipped for a library that already had
    # shows); never shown in Settings.
    setup_done: bool = False
    # Download new versions in the background and install them when AirGrabber closes.
    auto_update: bool = True
    # The version that ran last (stored at every start), for the "Updated to v…" notice; never
    # shown in Settings.
    last_run_version: str = ""

    @field_validator("quality", mode="before")
    @classmethod
    def _legacy_quality(cls, value: Any) -> Any:
        return QualityProfile.HEVC if value == "x265/HEVC" else value

    @field_validator("download_dir", mode="before")
    @classmethod
    def _require_folder(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            raise ValueError("Choose a folder")
        return value

    @field_validator("tmdb_api_key", "prowlarr_api_key", "ds_username", mode="before")
    @classmethod
    def _strip_key(cls, value: Any) -> Any:
        return value.strip() if isinstance(value, str) else value

    @field_validator("ds_destination", mode="before")
    @classmethod
    def _strip_destination(cls, value: Any) -> Any:
        # Download Station wants "shared folder/sub folder", without leading or trailing slashes.
        return value.strip().strip("/").strip() if isinstance(value, str) else value
