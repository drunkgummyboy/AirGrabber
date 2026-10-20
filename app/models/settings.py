"""User preferences: stored as key/value rows, validated as a single model."""

from __future__ import annotations

import math
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    SecretStr,
    ValidationInfo,
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


# Subtitle languages offered in Settings: OpenSubtitles' code -> plain name, in menu order.
# OpenSubtitles uses ISO 639-1 codes, plus pt-PT / pt-BR and zh-CN / zh-TW.
SUBTITLE_LANGUAGES: dict[str, str] = {
    "en": "English",
    "nl": "Dutch",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "it": "Italian",
    "pt-PT": "Portuguese",
    "pt-BR": "Portuguese (Brazil)",
    "sv": "Swedish",
    "da": "Danish",
    "no": "Norwegian",
    "fi": "Finnish",
    "pl": "Polish",
    "cs": "Czech",
    "hu": "Hungarian",
    "ro": "Romanian",
    "el": "Greek",
    "tr": "Turkish",
    "ru": "Russian",
    "uk": "Ukrainian",
    "ar": "Arabic",
    "he": "Hebrew",
    "hi": "Hindi",
    "id": "Indonesian",
    "ja": "Japanese",
    "ko": "Korean",
    "zh-CN": "Chinese (simplified)",
    "zh-TW": "Chinese (traditional)",
}
_LANGUAGE_CODES = {code.lower(): code for code in SUBTITLE_LANGUAGES}


def _subtitle_languages(value: Any) -> Any:
    """Known codes in their canonical spelling ("pt-br" -> "pt-BR"), without repeats, in the
    order given; unknown codes are dropped. A comma-separated string is accepted too."""
    if isinstance(value, str):
        value = [part for part in value.split(",")]
    if not isinstance(value, (list, tuple)):
        return value
    codes: list[str] = []
    for item in value:
        code = _LANGUAGE_CODES.get(str(item).strip().lower())
        if code is not None and code not in codes:
            codes.append(code)
    return tuple(codes)


SubtitleLanguages = Annotated[tuple[str, ...], BeforeValidator(_subtitle_languages)]


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


class CardSize(StrEnum):
    """How big the poster cards (Movies, Discover, search results, the watchlist) and the My
    shows cards are drawn, smallest first. XL is the original size; M is the default."""

    XS = "xs"
    S = "s"
    M = "m"
    L = "l"
    XL = "xl"


def _split_csv(value: Any) -> Any:
    if isinstance(value, str):
        return tuple(part.strip().lower() for part in value.split(",") if part.strip())
    return value


KeywordList = Annotated[tuple[str, ...], BeforeValidator(_split_csv)]


def _size_gb(value: Any) -> Any:
    """A size limit as typed: "" = no limit, "2,5" and "2.5" both 2.5 (GB)."""
    if not isinstance(value, str):
        return value
    text = value.strip().replace(",", ".")
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        raise ValueError("Use a number, like 2.5") from None


def _positive_size(value: float | None) -> float | None:
    if value is not None and not (math.isfinite(value) and value > 0):
        raise ValueError("Use a size above 0")
    return value


# A release size limit in GB (binary, as the search window shows sizes); None = no limit.
SizeLimit = Annotated[float | None, BeforeValidator(_size_gb), AfterValidator(_positive_size)]


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

    # The quality a one-click or automatic download picks: series (an episode or a season pack)
    # and films each have their own. Settings stored with the one `quality` of before move it
    # into both (`_from_one_quality`); the next save drops it.
    quality_tv: QualityProfile = QualityProfile.FULL_HD
    quality_movie: QualityProfile = QualityProfile.FULL_HD
    # The size range (GB) a release of the right quality should be in; None = no limit. Never a
    # filter: outside it the closest size wins (app/services/picking.py). Season packs and
    # multi-episode releases ignore the series range. Each `*_min_gb` comes before its
    # `*_max_gb`, which is checked against it.
    size_tv_min_gb: SizeLimit = None
    size_tv_max_gb: SizeLimit = None
    size_movie_min_gb: SizeLimit = None
    size_movie_max_gb: SizeLimit = None
    # FOLDER keeps the 1.x/2.0 behaviour for existing users; new users choose on first run.
    download_mode: DownloadMode = DownloadMode.FOLDER
    download_dir: Path = Field(default_factory=lambda: Path.home() / "Downloads")
    # Newly followed shows get their "download new episodes automatically" switch turned on.
    auto_download_new_shows: bool = False
    first_day: Weekday = Weekday.MONDAY
    weeks_to_show: int = Field(default=3, ge=1, le=5)
    show_suggestions: bool = True  # the calendar's suggested (untracked) show per day
    # The cards' size on every page, the calendar too (app/ui/card_scale.py). A stored size
    # stays as it is.
    card_size: CardSize = CardSize.M
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
    # Subtitles from OpenSubtitles.com for the grabbed episodes and films
    # (app/services/subtitles.py), saved next to the videos in `subtitles_folder`.
    subtitles_enabled: bool = False
    subtitle_languages: SubtitleLanguages = ("en",)
    # Where the torrent program puts finished downloads ("" = not chosen); a UNC path such as
    # \\NAS\video works too.
    subtitles_folder: str = ""
    # The user's own OpenSubtitles account, optional (downloads count against it; AirGrabber's
    # API key is shipped with the app). The password only ever travels in the body of the login
    # POST; the session token lives in memory. An `opensubtitles_api_key` stored by an earlier
    # build is ignored on load and dropped by the next save.
    opensubtitles_username: str = ""
    opensubtitles_password: StoredSecret = SecretStr("")
    # The user's own OpenSubtitles API key, optional: when set it is used instead of the shipped
    # one (app/services/opensubtitles.py, `effective_api_key`). Header only, never logged.
    opensubtitles_own_key: StoredSecret = SecretStr("")

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

    @model_validator(mode="before")
    @classmethod
    def _from_one_quality(cls, data: Any) -> Any:
        """Settings stored when series and films shared one `quality`: it becomes both
        `quality_tv` and `quality_movie` (a new key already there wins). The next save drops
        `quality`."""
        if not isinstance(data, dict) or "quality" not in data:
            return data
        old = data["quality"]
        return {"quality_tv": old, "quality_movie": old, **data}

    @field_validator("quality_tv", "quality_movie", mode="before")
    @classmethod
    def _legacy_quality(cls, value: Any) -> Any:
        return QualityProfile.HEVC if value == "x265/HEVC" else value

    @field_validator("size_tv_max_gb", "size_movie_max_gb")
    @classmethod
    def _max_not_below_min(cls, value: float | None, info: ValidationInfo) -> float | None:
        name = info.field_name or ""
        low = info.data.get(name.replace("_max_", "_min_"))
        if value is not None and low is not None and value < low:
            raise ValueError("The first number can't be bigger than the second")
        return value

    @field_validator("download_dir", mode="before")
    @classmethod
    def _require_folder(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            raise ValueError("Choose a folder")
        return value

    @field_validator(
        "tmdb_api_key",
        "prowlarr_api_key",
        "remote_username",
        "remote_destination",
        "subtitles_folder",
        "opensubtitles_username",
        "opensubtitles_own_key",
        mode="before",
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
