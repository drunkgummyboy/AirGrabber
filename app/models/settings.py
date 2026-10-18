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
    model_validator,
)


class QualityProfile(StrEnum):
    ANY = "Any"
    HD = "720p"
    FULL_HD = "1080p"
    UHD = "2160p"
    HEVC = "x265"


class DownloadMode(StrEnum):
    APP = "app"  # open the .torrent / magnet in the torrent program on this PC
    REMOTE = "remote"  # send the .torrent / magnet to the torrent program on a NAS or another PC
    # Save the .torrent into `download_dir`; a magnet-only release goes to the remote program.
    FOLDER = "folder"


class RemoteKind(StrEnum):
    """The torrent program on a NAS or another computer (`remote_*` settings)."""

    SYNOLOGY = "synology"  # Synology Download Station (DSM Web API)
    QBITTORRENT = "qbittorrent"  # qBittorrent's Web UI (Web API v2)
    TRANSMISSION = "transmission"  # Transmission's RPC

    @property
    def program(self) -> str:
        """Its short name in messages: "Sent to qBittorrent"."""
        return _PROGRAM_NAMES[self]


_PROGRAM_NAMES = {
    RemoteKind.SYNOLOGY: "Download Station",
    RemoteKind.QBITTORRENT: "qBittorrent",
    RemoteKind.TRANSMISSION: "Transmission",
}


def remote_ready(kind: RemoteKind, url: str, username: str, password: str) -> bool:
    """Whether these settings are enough to reach the program: an address, plus a DSM account
    for Download Station (qBittorrent and Transmission may run without a sign-in)."""
    if not url.strip():
        return False
    return kind is not RemoteKind.SYNOLOGY or bool(username.strip() and password)


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
    # Newly followed shows get their "download new episodes automatically" switch turned on.
    auto_download_new_shows: bool = False
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
    # The torrent program on a NAS or another computer: DownloadMode.REMOTE sends everything
    # there, FOLDER only the releases that have no .torrent file.
    remote_kind: RemoteKind = RemoteKind.SYNOLOGY
    remote_url: ServiceUrl = ""  # e.g. http://192.168.1.10:8080
    remote_username: str = ""
    remote_password: StoredSecret = SecretStr("")
    remote_verify_tls: bool = True  # NAS boxes often use self-signed certificates
    # Its download folder: "video/tv" (Download Station), "/downloads/tv"; "" = its default.
    remote_destination: str = ""
    disabled_indexers: KeywordList = ()
    # The first-run welcome was completed or dismissed (or skipped for a library that already had
    # shows); never shown in Settings.
    setup_done: bool = False
    # Download new versions in the background and install them when AirGrabber closes.
    auto_update: bool = True
    # The version that ran last (stored at every start), for the "Updated to v…" notice; never
    # shown in Settings.
    last_run_version: str = ""
    # Closing the window keeps AirGrabber running in the tray (when the tray icon works).
    keep_in_tray: bool = True
    # The "AirGrabber keeps running" notice was shown (the first close to the tray).
    tray_notice_shown: bool = False
    # Mirrors the Startup-folder shortcut (app/services/startup.py) that starts it in the tray.
    start_with_windows: bool = False

    @property
    def remote_configured(self) -> bool:
        """The remote torrent program has what it needs to be reached (see `remote_ready`)."""
        return remote_ready(
            self.remote_kind,
            self.remote_url,
            self.remote_username,
            self.remote_password.get_secret_value(),
        )

    @model_validator(mode="before")
    @classmethod
    def _from_download_station(cls, data: Any) -> Any:
        """Settings stored before other torrent programs existed keep their Synology Download
        Station: `ds_*` become `remote_*` (kind Synology). The next save drops the `ds_*` keys."""
        if not isinstance(data, dict) or any(str(key).startswith("remote_") for key in data):
            return data
        moved = {new: data[old] for old, new in _DOWNLOAD_STATION_KEYS if old in data}
        if not moved:
            return data
        return {**data, **moved, "remote_kind": RemoteKind.SYNOLOGY}

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

    @field_validator(
        "tmdb_api_key", "prowlarr_api_key", "remote_username", "remote_destination", mode="before"
    )
    @classmethod
    def _strip_key(cls, value: Any) -> Any:
        # Not the slashes of a destination: qBittorrent and Transmission want absolute paths
        # ("/downloads/tv"); the Download Station client strips them itself.
        return value.strip() if isinstance(value, str) else value


# Synology Download Station settings of AirGrabber 2.0 before other programs: (old, new) key.
_DOWNLOAD_STATION_KEYS = (
    ("ds_url", "remote_url"),
    ("ds_username", "remote_username"),
    ("ds_password", "remote_password"),
    ("ds_verify_tls", "remote_verify_tls"),
    ("ds_destination", "remote_destination"),
)
