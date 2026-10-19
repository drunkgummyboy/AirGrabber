"""OpenSubtitles.com REST API wire schemas (https://opensubtitles.stoplight.io/docs/opensubtitles-api).

Validated at the client boundary into the small frozen models the subtitle service uses. Only
the fields AirGrabber reads are declared; everything else in a payload is ignored.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.domain import DomainModel
from app.models.types import Count, UtcDatetime


class _Wire(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


# -- /login and /infos/user --------------------------------------------------------------------


class OSLoginUser(_Wire):
    allowed_downloads: Count = 0
    level: str = ""
    vip: bool = False


class OSLogin(_Wire):
    """POST /login: the session token (a JWT, valid 24 h) and the host to use from now on."""

    token: str = Field(min_length=1, repr=False)
    base_url: str = "api.opensubtitles.com"
    user: OSLoginUser = Field(default_factory=OSLoginUser)


class OSUserInfoData(_Wire):
    allowed_downloads: Count = 0
    level: str = ""
    vip: bool = False
    remaining_downloads: int | None = None


class OSUserInfo(_Wire):
    """GET /infos/user."""

    data: OSUserInfoData


# -- /subtitles --------------------------------------------------------------------------------


class OSFile(_Wire):
    file_id: int
    file_name: str | None = None


class OSAttributes(_Wire):
    language: str | None = None
    download_count: Count = 0
    hearing_impaired: bool = False
    ai_translated: bool = False
    machine_translated: bool = False
    moviehash_match: bool = False  # only present when a moviehash was sent
    release: str | None = None
    files: tuple[OSFile, ...] = ()


class OSSubtitle(_Wire):
    id: str | None = None
    attributes: OSAttributes


class OSSearch(_Wire):
    """GET /subtitles (one page)."""

    total_count: int = 0
    data: tuple[OSSubtitle, ...] = ()


# -- /download ---------------------------------------------------------------------------------


class OSDownload(_Wire):
    """POST /download: a temporary link (valid 3 hours) and the quota after this download."""

    link: str = Field(min_length=1, repr=False)
    file_name: str | None = None
    requests: int | None = None
    remaining: int | None = None
    reset_time_utc: datetime | None = None


class OSQuotaAnswer(_Wire):
    """What /download says when today's downloads are used up (fields optional)."""

    remaining: int | None = None
    message: str | None = None
    reset_time_utc: datetime | None = None


# -- what the service works with ----------------------------------------------------------------


class SubtitleCandidate(DomainModel):
    """One usable subtitle from a search: its single file, its language and what ranks it."""

    file_id: int
    language: str  # as OpenSubtitles spells it ("en", "pt-BR")
    download_count: int = 0
    hearing_impaired: bool = False
    moviehash_match: bool = False
    release: str = ""


class SubtitleLink(DomainModel):
    """The temporary download link of a subtitle file, and the quota after asking for it."""

    link: str = Field(repr=False)
    remaining: int | None = None
    reset_at: UtcDatetime | None = None


def candidates(search: OSSearch) -> list[SubtitleCandidate]:
    """The results that are one file each, and neither machine- nor AI-translated."""
    found: list[SubtitleCandidate] = []
    for item in search.data:
        attributes = item.attributes
        if attributes.machine_translated or attributes.ai_translated:
            continue
        if len(attributes.files) != 1 or not attributes.language:
            continue  # a subtitle split over several CDs doesn't fit one video file
        found.append(
            SubtitleCandidate(
                file_id=attributes.files[0].file_id,
                language=attributes.language,
                download_count=attributes.download_count,
                hearing_impaired=attributes.hearing_impaired,
                moviehash_match=attributes.moviehash_match,
                release=attributes.release or "",
            )
        )
    return found
