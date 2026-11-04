"""Forge's subtitles: what identifies a video to OpenSubtitles, the download allowance, what a
request did, and the plain-words errors (here, not in the client, so `app/forge/ui` reads them
without importing httpx)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel

from app.forge.models.media import MediaKind
from app.models.domain import DomainModel
from app.models.types import UtcDatetime


class OpenSubtitlesError(Exception):
    """OpenSubtitles refused or failed a request. The message is plain words for the UI and
    never holds a URL, the key, the password or the token."""


class KeyRejected(OpenSubtitlesError):
    """The API key was refused (401/403 without a signed-in user)."""


class LoginRejected(OpenSubtitlesError):
    """/login refused the user name or password (401)."""


class QuotaExceeded(OpenSubtitlesError):
    """Today's downloads are used up (406 on /download); `reset_at` = when they start again."""

    def __init__(self, message: str, reset_at: datetime | None = None) -> None:
        super().__init__(message)
        self.reset_at = reset_at


class Unreachable(OpenSubtitlesError):
    """No answer (network, timeout) or a server error that retrying didn't cure."""


class SubtitleIdentity(BaseModel, frozen=True):
    """What MediaForge knows of a video, which OpenSubtitles finds it by (a film's or a series'
    TMDB id when known, else the title and year; an episode's season and number)."""

    kind: MediaKind
    title: str
    year: int | None = None
    tmdb_id: int | None = None
    season: int | None = None
    episode: int | None = None
    release: str = ""  # the file's name before any rename: which subtitle fits this release best


class SubtitleQuota(DomainModel):
    """OpenSubtitles' download allowance as last heard: downloads left today and when the count
    starts again (midnight UTC)."""

    remaining: int | None = None
    allowed: int | None = None  # per day, when signed in
    reset_at: UtcDatetime | None = None


class SubtitleAccount(DomainModel):
    """The OpenSubtitles account that signed in (Settings' "Sign in")."""

    level: str = ""  # "Sub leecher", "VIP Member"
    allowed_downloads: int = 0  # per day
    remaining_downloads: int | None = None  # left today, when known
    vip: bool = False


class SubtitleFetchOutcome(StrEnum):
    """How a subtitle request for a video went (ForgeSubtitles.fetch)."""

    SAVED = "saved"  # every language asked for is there now (some may have been there already)
    PARTLY = "partly"  # some saved or there, the others not found
    ALREADY = "already"  # every language asked for was there already: no download spent
    NOT_FOUND = "not_found"  # OpenSubtitles has none
    NO_VIDEO = "no_video"  # the video is no longer there
    QUOTA = "quota"  # today's downloads are used up (`message` says until when)
    PROBLEM = "problem"  # key, account, connection or folder (`message`, plain words)
    UNAVAILABLE = "unavailable"  # no API key in this version, or no language chosen


class SubtitleFetch(DomainModel):
    """What a subtitle request did: the languages saved now, those already next to the video,
    those not there after it, and for QUOTA / PROBLEM a plain `message` without URLs or secrets
    (translated) and its `reason` (the same in English, what Forge's journal keeps)."""

    outcome: SubtitleFetchOutcome
    saved: tuple[str, ...] = ()
    already: tuple[str, ...] = ()
    not_found: tuple[str, ...] = ()
    message: str = ""
    reason: str = ""
