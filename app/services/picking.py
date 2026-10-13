"""Choosing the release a one-click download grabs, without asking the user.

`pick_best` looks only at what the search window would show for the request (relevant releases
without an excluded keyword), drops likely fakes, pre-release copies (cam, telesync, telecine,
screener: whatever the excluded keywords say) and dead torrents, prefers a single episode to a
multi-episode pack, and ranks the rest: quality closest to the user's profile, then preferred
release groups, then seeders, then a known size before an unknown one.

Pure functions; nothing here logs.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from app.models import (
    QualityProfile,
    Resolution,
    SearchKind,
    SearchRequest,
    Settings,
    TorrentRelease,
)
from app.services.search import (
    _SOURCE_TAG_FAMILIES,
    _normalize,
    is_preferred,
    is_relevant,
    passes_settings,
)

MIB = 1024 * 1024

# Words (in the normalized title, so ".exe" reads "exe") that mark malware or unusable releases.
FAKE_WORDS = frozenset(
    {"exe", "scr", "bat", "msi", "lnk", "zip", "rar", "password", "passworded", "codec"}
)
# Pre-release sources: every cam / telesync / telecine / screener tag the search's excluded
# keywords know, as normalized words or phrases ("DVDScr" reads "dvdscr", "CAM-Rip" "cam rip").
PRE_RELEASE_TAGS = tuple(dict.fromkeys(tag for _, tags in _SOURCE_TAG_FAMILIES for tag in tags))
# The smallest believable size per resolution, in MiB; the None row covers 480p and titles that
# name no resolution.
EPISODE_MIN_MIB: dict[Resolution | None, int] = {
    Resolution.HD: 100,
    Resolution.FULL_HD: 250,
    Resolution.UHD: 700,
    None: 80,
}
MOVIE_MIN_MIB: dict[Resolution | None, int] = {
    Resolution.HD: 400,
    Resolution.FULL_HD: 700,
    Resolution.UHD: 2048,
    None: 300,
}
# The same for x265/HEVC/H.265 releases, which are much smaller at the same quality.
EPISODE_MIN_MIB_HEVC: dict[Resolution | None, int] = {
    Resolution.HD: 60,
    Resolution.FULL_HD: 120,
    Resolution.UHD: 350,
    None: 50,
}
MOVIE_MIN_MIB_HEVC: dict[Resolution | None, int] = {
    Resolution.HD: 250,
    Resolution.FULL_HD: 400,
    Resolution.UHD: 1024,
    None: 200,
}
# Resolutions in order of closeness to each profile; any resolution not listed (and a title
# without one) comes after them, all equal. ANY has no preference at all.
_RESOLUTION_ORDER: dict[QualityProfile, tuple[Resolution, ...]] = {
    QualityProfile.ANY: (),
    QualityProfile.HD: (Resolution.HD, Resolution.FULL_HD, Resolution.SD, Resolution.UHD),
    QualityProfile.FULL_HD: (Resolution.FULL_HD, Resolution.HD, Resolution.UHD),
    QualityProfile.UHD: (Resolution.UHD, Resolution.FULL_HD, Resolution.HD, Resolution.SD),
    # x265/HEVC first (see quality_rank), each part in the 1080p order.
    QualityProfile.HEVC: (Resolution.FULL_HD, Resolution.HD, Resolution.UHD),
}
# Two episode markers in a row in a normalized title: "S02E01E02", "S02E01-E08" ("s02e01 e08"),
# "S02E01-08" ("s02e01 08"), "S02E01-S02E08", "1x01-1x02". Not "S02E01.720p" or "S02E01.2025".
_MULTI_EPISODE = re.compile(
    r"(?<!\w)(?:s\d{1,3}\s*e\d{1,4}|\d{1,2}x\d{2,3})(?!\d)"
    r"(?:\s*e\d{1,4}|\s+(?:s\d{1,3}\s*e\d{1,4}|\d{1,2}x\d{2,3}|\d{1,3}))(?!\w)"
)


def is_likely_fake(release: TorrentRelease, request: SearchRequest) -> bool:
    """A title word from FAKE_WORDS (unless the show or film's own title has it: a show called
    "Password"), or a known size too small for the resolution (EPISODE_MIN_MIB for TV,
    MOVIE_MIN_MIB for films; the *_HEVC tables for x265/HEVC/H.265 titles). An unknown size (0)
    never counts as too small."""
    words = set(_normalize(release.title).split())
    if (words & FAKE_WORDS) - set(_normalize(request.title).split()):
        return True
    if not release.size_bytes:
        return False
    if request.kind is SearchKind.MOVIE:
        minimums = MOVIE_MIN_MIB_HEVC if release.is_hevc else MOVIE_MIN_MIB
    else:
        minimums = EPISODE_MIN_MIB_HEVC if release.is_hevc else EPISODE_MIN_MIB
    resolution = release.resolution
    floor = minimums[resolution if resolution in minimums else None]
    return release.size_bytes < floor * MIB


def is_pre_release(release: TorrentRelease, request: SearchRequest) -> bool:
    """A cam, telesync, telecine or screener copy: a PRE_RELEASE_TAGS word or phrase in the
    normalized title ("HDTS", "DVDScr", "CAM-Rip"; not "Scream"), unless the show or film's own
    title has it (a film called "Cam")."""
    text = f" {_normalize(release.title)} "
    title = f" {_normalize(request.title)} "
    return any(f" {tag} " in text and f" {tag} " not in title for tag in PRE_RELEASE_TAGS)


def is_multi_episode(release: TorrentRelease) -> bool:
    """Whether the title holds more than one episode in a row ("S02E01-E08"): a pack."""
    return _MULTI_EPISODE.search(_normalize(release.title)) is not None


def quality_rank(release: TorrentRelease, profile: QualityProfile) -> int:
    """How far the release's quality is from `profile`: 0 is what the user asked for.

    1080p: 1080p, then 720p, then 2160p, then the rest. 720p: 720p, 1080p, 480p, 2160p, rest.
    2160p: 2160p, 1080p, 720p, 480p, rest. x265: every x265/HEVC release before every other one,
    each group in the 1080p order. Any: all equal.
    """
    order = _RESOLUTION_ORDER[profile]
    resolution = release.resolution
    rank = order.index(resolution) if resolution in order else len(order)
    if profile is QualityProfile.HEVC and not release.is_hevc:
        rank += len(order) + 1
    return rank


def _sort_key(release: TorrentRelease, settings: Settings) -> tuple[int, bool, int, bool]:
    return (
        quality_rank(release, settings.quality),
        not is_preferred(release, settings),
        -release.seeders,
        not release.size_bytes,
    )


def rank_releases(
    releases: Iterable[TorrentRelease], request: SearchRequest, settings: Settings
) -> list[TorrentRelease]:
    """The releases `pick_best` would choose from, best first.

    Kept: relevant to `request`, no excluded keyword, not a likely fake, not a pre-release
    copy; and, when any of those has seeders, only the ones that have. For an EPISODE request the single-episode releases
    only, unless there are none (then the packs that hold the episode). Ranked by quality
    closeness, preferred group, seeders (most first), known size before unknown; ties keep
    their order.
    """
    candidates = [
        release
        for release in releases
        if is_relevant(release, request)
        and passes_settings(release, settings)
        and not is_likely_fake(release, request)
        and not is_pre_release(release, request)
    ]
    if any(release.seeders for release in candidates):
        candidates = [release for release in candidates if release.seeders > 0]
    if request.kind is SearchKind.EPISODE:
        singles = [release for release in candidates if not is_multi_episode(release)]
        candidates = singles or candidates
    return sorted(candidates, key=lambda release: _sort_key(release, settings))


def pick_best(
    releases: Iterable[TorrentRelease], request: SearchRequest, settings: Settings
) -> TorrentRelease | None:
    """The release a one-click download grabs for `request`; None when nothing qualifies."""
    ranked = rank_releases(releases, request, settings)
    return ranked[0] if ranked else None
