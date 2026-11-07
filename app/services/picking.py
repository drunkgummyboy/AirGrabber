"""Choosing the release a one-click download grabs, without asking the user.

`pick_best` looks only at what the search window would show for the request (relevant releases
without an excluded keyword), drops likely fakes, pre-release copies (cam, telesync, telecine,
screener: whatever the excluded keywords say) and dead torrents, prefers a single episode to a
multi-episode pack, and ranks the rest: quality closest to the user's profile for the kind (the
series one for episodes and season packs, the movie one for films), then the size range (inside
it first, an unknown size next, then the closest size; never a filter), then preferred release
groups, then seeders, then a known size before an unknown one.

`recommend` is the same pick with its reasons, in the picker's own terms (the release list's
★ Recommended row and the line under it); `speed` says how fast a release will likely download
by its seeders (the list's coloured dots, FAST_SEEDERS / STALL_SEEDERS).

Pure functions; nothing here logs.
"""

from __future__ import annotations

import re
import statistics
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

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
GIB = 1024 * MIB  # the size ranges' "GB" (binary, as the search window shows sizes)

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


# More than one season in a normalized title: "S01-S03" ("s01 s03"), "Seasons 1-3", "Complete Series".
_SEASONS_SPAN = re.compile(
    r"(?<!\w)(?:s\d{1,3}\s+s\d{1,3}|seasons?\s+\d{1,3}\s+(?:to\s+)?\d{1,3}|complete\s+series)(?!\w)"
)
# A pack that says which episodes it holds: "S02E01-E08", "S02E01-08", "S02E01-S02E08".
_EPISODE_SPAN = re.compile(
    r"(?<!\w)s(\d{1,3})\s*e(\d{1,4})\s*(?:e|s\1\s*e)?\s*(\d{1,4})(?!\w)"
)
MOST_EPISODES_IN_A_SEASON = 200  # a higher "end" is a year or a resolution, not an episode
# A pack is too small under this share of its episodes' minimums together: they are only a floor
# for a normal episode, and a season of short ones is real.
PACK_FLOOR_SHARE = 0.5


def pack_episodes(release: TorrentRelease, request: SearchRequest) -> int | None:
    """How many episodes a season pack holds: what its title says ("S02E01-E08"), else the
    season's aired ones (`request.episode_count`); None when it isn't known, or it holds more
    than one season (its size says nothing against one season's)."""
    title = _normalize(release.title)
    if _SEASONS_SPAN.search(title):
        return None
    span = _EPISODE_SPAN.search(title)
    if span is not None:
        first, last = int(span.group(2)), int(span.group(3))
        if first < last <= MOST_EPISODES_IN_A_SEASON:
            return last - first + 1
    return request.episode_count


def is_likely_fake(release: TorrentRelease, request: SearchRequest) -> bool:
    """A title word from FAKE_WORDS (unless the show or film's own title has it: a show called
    "Password"), or a known size too small for the resolution (EPISODE_MIN_MIB for TV,
    MOVIE_MIN_MIB for films; the *_HEVC tables for x265/HEVC/H.265 titles; a season pack:
    PACK_FLOOR_SHARE of that for all its episodes). An unknown size (0) never counts as too small."""
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
    floor: float = minimums[resolution if resolution in minimums else None]
    if request.kind is SearchKind.SEASON:
        episodes = pack_episodes(release, request)
        if episodes is not None:
            floor = max(floor, floor * episodes * PACK_FLOOR_SHARE)
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


def quality_profile(kind: SearchKind, settings: Settings) -> QualityProfile:
    """The quality a request asks for: the movie one for a film, else the series one."""
    return settings.quality_movie if kind is SearchKind.MOVIE else settings.quality_tv


def size_range(
    release: TorrentRelease, request: SearchRequest, settings: Settings
) -> tuple[float | None, float | None] | None:
    """The (smallest, largest) size in GB the release is judged by; None when no range applies:
    none is set for the kind, a season pack whose episodes aren't known (`pack_episodes`; with
    them the series range times their number), or the release holds more than one episode (a
    pack picked for one episode)."""
    times = 1
    if request.kind is SearchKind.MOVIE:
        low, high = settings.size_movie_min_gb, settings.size_movie_max_gb
    elif request.kind is SearchKind.EPISODE and not is_multi_episode(release):
        low, high = settings.size_tv_min_gb, settings.size_tv_max_gb
    elif request.kind is SearchKind.SEASON:
        episodes = pack_episodes(release, request)
        if episodes is None:
            return None
        low, high, times = settings.size_tv_min_gb, settings.size_tv_max_gb, episodes
    else:
        return None
    if low is None and high is None:
        return None
    return (
        low * times if low is not None else None,
        high * times if high is not None else None,
    )


def _has_tv_range(settings: Settings) -> bool:
    return settings.size_tv_min_gb is not None or settings.size_tv_max_gb is not None


def size_rank(
    release: TorrentRelease, request: SearchRequest, settings: Settings
) -> tuple[int, float]:
    """How well the size fits the range, within one quality: (0, 0) inside it (or when no range
    applies), (1, 0) an unknown size, (2, GB away from the range) outside it, closest first."""
    limits = size_range(release, request, settings)
    if limits is None:
        if request.kind is SearchKind.SEASON and _has_tv_range(settings):
            # Not judged while others are: a pack of several seasons doesn't beat one that fits.
            if request.episode_count is not None and pack_episodes(release, request) is None:
                return 1, 0.0
        return 0, 0.0
    if not release.size_bytes:
        return 1, 0.0
    size = release.size_bytes / GIB
    low, high = limits
    if low is not None and size < low:
        return 2, low - size
    if high is not None and size > high:
        return 2, size - high
    return 0, 0.0


def is_dual_audio(release: TorrentRelease, request: SearchRequest) -> bool:
    """A "DUAL" release: two audio tracks, and the one that plays first may be a dub (the owner
    got Russian ones). Unless the show or film's own title has the word."""
    words = set(_normalize(release.title).split())
    return "dual" in words and "dual" not in _normalize(request.title).split()


def _sort_key(
    release: TorrentRelease, request: SearchRequest, settings: Settings
) -> tuple[int, bool, tuple[int, float], bool, int, bool]:
    return (
        quality_rank(release, quality_profile(request.kind, settings)),
        is_dual_audio(release, request),
        size_rank(release, request, settings),
        not is_preferred(release, settings),
        -release.seeders,
        not release.size_bytes,
    )


def rank_releases(
    releases: Iterable[TorrentRelease], request: SearchRequest, settings: Settings
) -> list[TorrentRelease]:
    """The releases `pick_best` would choose from, best first.

    Kept: relevant to `request`, no excluded keyword, not a likely fake, not a pre-release
    copy; and, when any of those has seeders, only the ones that have. For an EPISODE request
    the single-episode releases only, unless there are none (then the packs that hold the
    episode). Ranked by quality closeness (`quality_profile` for the request's kind), single
    audio before "DUAL" (`is_dual_audio`), the size range (`size_rank`), preferred group, seeders
    (most first), known size before unknown; ties keep their order.
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
    return sorted(candidates, key=lambda release: _sort_key(release, request, settings))


def pick_best(
    releases: Iterable[TorrentRelease], request: SearchRequest, settings: Settings
) -> TorrentRelease | None:
    """The release a one-click download grabs for `request`; None when nothing qualifies."""
    ranked = rank_releases(releases, request, settings)
    return ranked[0] if ranked else None


# -- how fast a release will likely download, and why one is picked ------------------------------

FAST_SEEDERS = 20  # at least this many seeders: fast (the release list's green dot)
STALL_SEEDERS = 2  # at most this many: it may stall (red); in between: slower (amber)
# No size range set: a size under SMALL_SIZE times the listed releases' median size is small,
# one over LARGE_SIZE times it large, the rest normal.
SMALL_SIZE = 2 / 3
LARGE_SIZE = 1.5


class Speed(StrEnum):
    """How fast a release will likely download, judged by its seeders alone."""

    FAST = "fast"
    SLOWER = "slower"
    STALL = "stall"
    UNKNOWN = "unknown"  # the source gave no count


def speed(release: TorrentRelease) -> Speed:
    """FAST from FAST_SEEDERS seeders on, STALL at STALL_SEEDERS or fewer, SLOWER in between;
    UNKNOWN for 0: the indexers drop the releases nobody seeds, so a 0 is a source that didn't
    say."""
    seeders = release.seeders
    if seeders <= 0:
        return Speed.UNKNOWN
    if seeders >= FAST_SEEDERS:
        return Speed.FAST
    if seeders <= STALL_SEEDERS:
        return Speed.STALL
    return Speed.SLOWER


class SeedNote(StrEnum):
    """What the recommendation says about its seeders."""

    MOST = "most"  # the most of the listed releases (and not a STALL count)
    LOTS = "lots"  # FAST
    FEW = "few"  # below FAST_SEEDERS


class SizeNote(StrEnum):
    """What the recommendation says about its size."""

    RIGHT = "right"  # inside the user's size range
    BELOW_RANGE = "below_range"  # a range applies and it is smaller (the closest there was)
    ABOVE_RANGE = "above_range"  # ... bigger
    SMALL = "small"  # no range applies: compared with the listed releases' median size
    NORMAL = "normal"
    LARGE = "large"
    UNKNOWN = "unknown"  # the source didn't say


@dataclass(frozen=True, slots=True)
class Recommendation:
    """The release `pick_best` picks, and why: the criteria it weighs, as plain notes."""

    release: TorrentRelease
    seeds: SeedNote | None  # None: the source gave no count
    size: SizeNote | None  # None: no range applies and no other size to compare it with
    preferred: bool  # from one of the user's preferred release groups
    pack: bool  # an episode was asked for, and no single-episode release qualified


def seed_note(release: TorrentRelease, listed: Iterable[TorrentRelease]) -> SeedNote | None:
    """FEW for a STALL count; MOST when no other listed release has more; else LOTS for a FAST
    count, FEW below it. None when the source gave no count."""
    level = speed(release)
    if level is Speed.UNKNOWN:
        return None
    if level is Speed.STALL:
        return SeedNote.FEW
    others = [other for other in listed if other is not release]
    if others and all(release.seeders >= other.seeders for other in others):
        return SeedNote.MOST
    return SeedNote.LOTS if level is Speed.FAST else SeedNote.FEW


def size_note(
    release: TorrentRelease,
    listed: Iterable[TorrentRelease],
    request: SearchRequest,
    settings: Settings,
) -> SizeNote | None:
    """UNKNOWN without a size. With a size range for it (`size_range`): RIGHT inside it, else
    BELOW_RANGE / ABOVE_RANGE. Without one: SMALL / NORMAL / LARGE against the median size of
    the listed releases (SMALL_SIZE, LARGE_SIZE); None when no other one has a size."""
    if not release.size_bytes:
        return SizeNote.UNKNOWN
    limits = size_range(release, request, settings)
    if limits is not None:
        if size_rank(release, request, settings)[0] == 0:
            return SizeNote.RIGHT
        low, _high = limits
        below = low is not None and release.size_bytes / GIB < low
        return SizeNote.BELOW_RANGE if below else SizeNote.ABOVE_RANGE
    sizes = [other.size_bytes for other in listed if other.size_bytes and other is not release]
    if not sizes:
        return None
    share = release.size_bytes / statistics.median([*sizes, release.size_bytes])
    if share < SMALL_SIZE:
        return SizeNote.SMALL
    if share > LARGE_SIZE:
        return SizeNote.LARGE
    return SizeNote.NORMAL


def recommend(
    releases: Iterable[TorrentRelease], request: SearchRequest, settings: Settings
) -> Recommendation | None:
    """`pick_best`'s pick from `releases` (the one-click download's very choice), with its
    reasons against the other `releases`; None when it picks nothing."""
    listed = list(releases)
    best = pick_best(listed, request, settings)
    if best is None:
        return None
    return Recommendation(
        release=best,
        seeds=seed_note(best, listed),
        size=size_note(best, listed, request, settings),
        preferred=is_preferred(best, settings),
        pack=request.kind is SearchKind.EPISODE and is_multi_episode(best),
    )
