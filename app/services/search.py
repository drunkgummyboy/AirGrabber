"""Torrent search: queries, relevance and settings filters, de-duplication, concurrent search.

`TorrentSearchService.search` runs every usable indexer at once and yields a `SearchUpdate`
snapshot whenever one of them finishes, so the UI can show results while slower sources are
still working. The pure functions below are the building blocks and are used on their own too.

Release titles are compared in a normalized form: accents folded, apostrophes dropped, case
folded and every run of other non-alphanumeric characters (dots, dashes, brackets...) turned
into one space, so "Grey's.Anatomy.S02E01" reads "greys anatomy s02e01".

Log hygiene: indexer download links may embed an API key, so nothing here logs a release or a
URL; failures are logged by indexer label and IndexerError text only.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import unicodedata
from collections.abc import AsyncGenerator, Iterable, Sequence
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from app.indexers import BaseIndexer, EztvIndexer, IndexerError, ProwlarrIndexer
from app.indexers.base import text_queries
from app.models import (
    IndexerState,
    IndexerStatus,
    Movie,
    MovieDetails,
    QualityProfile,
    Resolution,
    SearchKind,
    SearchRequest,
    SearchUpdate,
    Settings,
    Show,
    TorrentRelease,
)
from app.services.settings import SettingsService

log = logging.getLogger(__name__)

QUALITY_KEYS = ("2160p", "1080p", "720p", "480p", "x265", "any")

_STOPWORDS = frozenset({"the", "a", "an", "of", "and", "&"})
_APOSTROPHES = re.compile(r"['’`´]")
_SEPARATORS = re.compile(r"[\W_]+")
_PUNCTUATION = re.compile(r"[^\w\s]|_")
# Any TV episode marker: "S01E02", "S1 E2", "1x02" (but not "5.1x264": an audio and a codec tag).
_ANY_EPISODE = re.compile(
    r"(?<!\w)(?:s\d{1,2}\s*e\d{1,3}|\d{1,2}x(?!26[45](?!\d))\d{2,3})(?!\d)"
)
# Where a show's name ends in a release title: the first season/episode marker or "complete".
_SERIES_END = re.compile(
    r"(?<!\w)(?:s\d{1,3}(?:\s*e\d{1,4})?(?!\d)|\d{1,2}x(?!26[45](?!\d))\d{2,3}(?!\d)"
    r"|season\s*\d{1,3}(?!\d)|complete(?!\w))"
)
_YEAR = re.compile(r"(?:19|20)\d\d")
# The release group: what follows the last '-' up to a space, bracket, parenthesis or dot.
_GROUP = re.compile(r"[^\s\[\](){}.]+")
_RESOLUTION_KEYS = {
    "2160p": Resolution.UHD,
    "1080p": Resolution.FULL_HD,
    "720p": Resolution.HD,
    "480p": Resolution.SD,
}
_PROFILE_KEYS = {
    QualityProfile.ANY: "any",
    QualityProfile.HD: "720p",
    QualityProfile.FULL_HD: "1080p",
    QualityProfile.UHD: "2160p",
    QualityProfile.HEVC: "x265",
}
_UNAVAILABLE = "not available for this search"


# --- queries ----------------------------------------------------------------------------------


def build_queries(
    kind: SearchKind,
    title: str,
    *,
    year: int | None = None,
    season: int | None = None,
    episode: int | None = None,
) -> tuple[str, ...]:
    """Free-text indexer queries, best first, without repeats (as the 1.x app built them).

    `clean` is the title with everything but letters, digits and spaces removed
    ("Marvel's Agents of S.H.I.E.L.D." -> "Marvels Agents of SHIELD"). Raises ValueError when an
    episode search lacks its season or episode, or a season search its season.
    """
    title = " ".join(title.split())
    if not title:
        return ()
    clean = " ".join(_PUNCTUATION.sub("", title).split())
    names = [title, clean] if clean else [title]
    queries: list[str] = []
    if kind is SearchKind.MOVIE:
        if year is not None:
            queries += [f"{name} {year}" for name in names]
        queries += names
    elif kind is SearchKind.EPISODE:
        if season is None or episode is None:
            raise ValueError("an episode search needs a season and an episode")
        code = f"S{season:02d}E{episode:02d}"
        queries.append(f"{title} {code}")
        if year is not None:
            queries.append(f"{title} {year} {code}")
        if clean:
            queries.append(f"{clean} {code}")
    else:
        if season is None:
            raise ValueError("a season search needs a season")
        code = f"S{season:02d}"
        queries += [f"{title} {code}", f"{title} Season {season}", f"{title} {code} Complete"]
        if year is not None:
            queries += [f"{title} {year} {code}", f"{title} {year} Season {season}"]
        if clean:
            queries += [f"{clean} {code}", f"{clean} Season {season}"]
    return tuple(dict.fromkeys(queries))


# --- relevance --------------------------------------------------------------------------------


def _normalize(text: str) -> str:
    """Accent-folded, case-folded, apostrophe-free text with single spaces between words."""
    folded = unicodedata.normalize("NFKD", text.casefold())
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    return _SEPARATORS.sub(" ", _APOSTROPHES.sub("", folded)).strip()


def _words(normalized: str) -> list[str]:
    """The words of normalized text, single-letter runs joined: "s h i e l d" -> "shield"."""
    words: list[str] = []
    letters: list[str] = []

    def flush() -> None:
        if len(letters) > 1:
            words.append("".join(letters))
        else:
            words.extend(letters)
        letters.clear()

    for word in normalized.split():
        if len(word) == 1 and word.isalpha():
            letters.append(word)
            continue
        flush()
        words.append(word)
    flush()
    return words


def _title_matches(title_words: tuple[str, ...], release_words: list[str]) -> bool:
    """Every significant title word is a word of the release title; failing that, the title
    written without spaces equals a run of whole release words ("Spider-Man" vs "Spiderman")."""
    if not title_words:
        return True
    present = set(release_words)
    if all(word in present for word in title_words):
        return True
    compact = "".join(title_words)
    significant = [word for word in release_words if word not in _STOPWORDS]
    for start in range(len(significant)):
        joined = ""
        for word in significant[start:]:
            joined += word
            if joined == compact:
                return True
            if not compact.startswith(joined):
                break
    return False


def _mostly_overlap(title_words: Iterable[str], series_words: Iterable[str]) -> bool:
    """At least 2 shared words, covering at least 60% of the smaller set: "Law & Order: Special
    Victims Unit" vs "Law.and.Order.SVU", "Marvel's Daredevil: Born Again" vs
    "Daredevil.Born.Again". A one-word title can never pass this way."""
    title_set, series_set = set(title_words), set(series_words)
    shared = len(title_set & series_set)
    return shared >= 2 and shared * 5 >= 3 * min(len(title_set), len(series_set))


@dataclass(frozen=True, slots=True)
class _Matcher:
    """`is_relevant` for one request, with its patterns compiled once."""

    kind: SearchKind
    title_words: tuple[str, ...]
    required: tuple[re.Pattern[str], ...]  # at least one must match
    forbidden: tuple[re.Pattern[str], ...]  # none may match
    years: frozenset[str]  # MOVIE: the accepted release years; empty when the year is unknown

    def matches(self, title: str) -> bool:
        text = _normalize(title)
        if self.required and not any(pattern.search(text) for pattern in self.required):
            return False
        if any(pattern.search(text) for pattern in self.forbidden):
            return False
        if _title_matches(self.title_words, _words(text)):
            return True
        return _mostly_overlap(self.title_words, self._series_words(text))

    def _series_words(self, text: str) -> list[str]:
        """The significant words naming the show or film in a normalized release title: those
        before the first season/episode marker (TV) or before the year (MOVIE)."""
        if self.kind is SearchKind.MOVIE:
            words = _words(text)
            cut = next(
                (
                    index
                    for index, word in enumerate(words)
                    if index and (word in self.years if self.years else _YEAR.fullmatch(word))
                ),
                len(words),
            )
            series = words[:cut]
        else:
            end = _SERIES_END.search(text)
            series = _words(text[: end.start()] if end else text)
            # A year in a show's release name ("Law.and.Order.SVU.1999.S27E01") is no title word.
            series = [
                word for word in series if word in self.title_words or not _YEAR.fullmatch(word)
            ]
        return [word for word in series if word not in _STOPWORDS]


def _word(pattern: str) -> re.Pattern[str]:
    """`pattern` as a whole word (or words) of normalized text."""
    return re.compile(rf"(?<!\w)(?:{pattern})(?!\w)")


def _episode_marker(season: int, episode: int) -> re.Pattern[str]:
    # "S02E01", "S2 E1", "2x01", "Season 2 Episode 1"; "S02E01E02" and "S02E01-02" count too.
    return re.compile(
        rf"(?<!\w)(?:s0*{season}\s*e0*{episode}|0*{season}x0*{episode}"
        rf"|season\s*0*{season}\s*episode\s*0*{episode})(?!\d)"
    )


@functools.lru_cache(maxsize=64)
def _matcher(request: SearchRequest) -> _Matcher:
    title_words = tuple(
        word for word in _words(_normalize(request.title)) if word not in _STOPWORDS
    )
    required: list[re.Pattern[str]] = []
    forbidden: list[re.Pattern[str]] = []
    years: frozenset[str] = frozenset()
    season = request.season
    if request.kind is SearchKind.EPISODE:
        if season is not None and request.episode is not None:
            required.append(_episode_marker(season, request.episode))
    elif request.kind is SearchKind.SEASON:
        if season is not None:
            # "S02", "Season 2", "Complete 2" / "2 Complete" ("Complete S02" has "S02" already).
            required.append(
                re.compile(
                    rf"(?<!\w)(?:s0*{season}(?!\d)(?!\s*e\d)|season\s*0*{season}(?!\d)"
                    rf"|complete\s*0*{season}(?!\w)|0*{season}\s*complete(?!\w))"
                )
            )
            # A single episode of that season is not a pack.
            forbidden.append(
                re.compile(
                    rf"(?<!\w)(?:s0*{season}\s*e\d|0*{season}x(?!26[45](?!\d))\d"
                    rf"|season\s*0*{season}\s*episode\s*\d)"
                )
            )
    else:
        if request.year is not None:
            # Release years drift by one.
            years = frozenset(str(request.year + delta) for delta in (-1, 0, 1))
            required.append(_word("|".join(sorted(years))))
        forbidden.append(_ANY_EPISODE)
    return _Matcher(request.kind, title_words, tuple(required), tuple(forbidden), years)


def is_relevant(release: TorrentRelease, request: SearchRequest) -> bool:
    """Whether `release` is what `request` asks for, judged by its title.

    Title: every significant title word (minus the/a/an/of/and/&) is a word of the release
    title, or the title and the release's series part (the words before the first season/episode
    marker, or before the year for films) share at least 2 words covering 60% of the smaller
    set. Markers: EPISODE needs that episode's marker; SEASON a season marker and no
    single-episode marker of that season; MOVIE the year (or one either side) when it is known,
    and no TV episode marker.
    """
    return _matcher(request).matches(release.title)


# --- settings filters -------------------------------------------------------------------------


# Scene shorthand for low-quality sources: an excluded keyword that is a key here also excludes
# every tag of its family (normalized forms: "CAM-Rip" reads "cam rip"). Other keywords match
# only themselves.
_SOURCE_TAG_FAMILIES: tuple[tuple[tuple[str, ...], tuple[str, ...]], ...] = (
    (("cam", "camrip"), ("cam", "camrip", "hdcam", "hdcamrip", "cam rip")),
    (("telesync", "ts"), ("telesync", "ts", "hdts", "tsrip", "hdtsrip", "pdvd", "predvd")),
    (("telecine", "tc"), ("telecine", "tc", "hdtc")),
    (("screener", "scr"), ("screener", "scr", "dvdscr", "bdscr", "webscr")),
)
_SOURCE_TAG_ALIASES = {key: tags for keys, tags in _SOURCE_TAG_FAMILIES for key in keys}


@functools.lru_cache(maxsize=16)
def _excluded_phrases(keywords: tuple[str, ...]) -> tuple[str, ...]:
    """The normalized phrases the excluded keywords stand for, source-tag families expanded."""
    phrases: dict[str, None] = {}
    for keyword in keywords:
        phrase = _normalize(keyword)
        if phrase:
            phrases.update(dict.fromkeys(_SOURCE_TAG_ALIASES.get(phrase, (phrase,))))
    return tuple(phrases)


def passes_settings(release: TorrentRelease, settings: Settings) -> bool:
    """False for releases with an excluded keyword: a whole word or phrase; "cam", "telesync",
    "telecine" and "screener" also cover their scene tags such as CAMRip or HDTS."""
    text = f" {_normalize(release.title)} "
    for phrase in _excluded_phrases(settings.excluded_keywords):
        if f" {phrase} " in text:
            return False
    return True


def _release_group(title: str) -> str | None:
    """The text after the last '-', up to a space, bracket or extension: "x265-ELiTE EZTV"
    -> "ELiTE", "h264-successfulcrab[EZTVx.to].mkv" -> "successfulcrab"."""
    _, dash, tail = title.rpartition("-")
    if not dash:
        return None
    match = _GROUP.search(tail)
    return match.group() if match else None


def is_preferred(release: TorrentRelease, settings: Settings) -> bool:
    """Whether the release group is one of the user's `preferred_groups` (ignoring case)."""
    group = _release_group(release.title)
    if group is None:
        return False
    return group.casefold() in {preferred.casefold() for preferred in settings.preferred_groups}


# --- de-duplication -----------------------------------------------------------------------------


def _torrent_file_first(urls: list[str]) -> str:
    def rank(url: str) -> int:
        parts = urlsplit(url)
        if parts.scheme.lower() not in ("http", "https"):
            return 2
        return 0 if parts.path.lower().endswith(".torrent") else 1

    return min(urls, key=rank)  # min keeps the first of equal rank


def _combine(group: list[TorrentRelease]) -> TorrentRelease:
    """One release for a group of duplicates: the first one, with the best counts, every source,
    and the links and dates it lacks taken from the others."""
    kept = group[0]
    if len(group) == 1:
        return kept
    sources = dict.fromkeys(part for release in group for part in release.source.split("+"))
    update: dict[str, Any] = {
        "source": "+".join(sources),
        "seeders": max(release.seeders for release in group),
        "leechers": max(release.leechers for release in group),
    }
    if not kept.download_url:
        links = [release.download_url for release in group if release.download_url]
        if links:
            update["download_url"] = _torrent_file_first(links)
    for field in ("magnet_uri", "info_url", "published_at"):
        if getattr(kept, field) is None:
            values = [getattr(release, field) for release in group]
            update[field] = next((value for value in values if value is not None), None)
    if not kept.size_bytes:
        update["size_bytes"] = max(release.size_bytes for release in group)
    return kept.model_copy(update=update)


def merge(releases: Iterable[TorrentRelease]) -> list[TorrentRelease]:
    """De-duplicate releases from several indexers, keeping the order of first appearance.

    Releases with the same info hash become one (see `_combine`); releases without a hash are
    duplicates when their normalized title and size match.
    """
    groups: dict[tuple[object, ...], list[TorrentRelease]] = {}
    for release in releases:
        key: tuple[object, ...] = (
            ("hash", release.info_hash)
            if release.info_hash
            else ("title", _normalize(release.title), release.size_bytes)
        )
        groups.setdefault(key, []).append(release)
    return [_combine(group) for group in groups.values()]


# --- quality ----------------------------------------------------------------------------------


def matches_quality(release: TorrentRelease, key: str) -> bool:
    """Whether `release` belongs under the quality filter `key` (one of QUALITY_KEYS)."""
    if key == "any":
        return True
    if key == "x265":
        return release.is_hevc
    if key not in _RESOLUTION_KEYS:
        raise ValueError(f"unknown quality key {key!r}")
    return release.resolution is _RESOLUTION_KEYS[key]


def quality_counts(releases: Iterable[TorrentRelease]) -> dict[str, int]:
    """How many releases each quality filter would show, for every key in QUALITY_KEYS."""
    items = list(releases)
    return {key: sum(matches_quality(release, key) for release in items) for key in QUALITY_KEYS}


def quality_key(profile: QualityProfile) -> str:
    """The quality filter that matches the user's preferred quality profile."""
    return _PROFILE_KEYS[profile]


# --- the service ------------------------------------------------------------------------------


def _skip_reason(indexer: BaseIndexer, request: SearchRequest, settings: Settings) -> str | None:
    """Why `indexer` sits this search out, or None when it searches."""
    if indexer.name.casefold() in {name.casefold() for name in settings.disabled_indexers}:
        return "disabled in Settings"
    if indexer.supports(request):
        return None
    if request.kind not in indexer.kinds:
        if indexer.kinds == {SearchKind.MOVIE}:
            return "movies only"
        if SearchKind.MOVIE not in indexer.kinds:
            return "TV only"
        return _UNAVAILABLE
    if isinstance(indexer, ProwlarrIndexer):
        return "not configured"
    if isinstance(indexer, EztvIndexer) and not request.imdb_id:
        return "needs an IMDb id"
    return _UNAVAILABLE


def _partition(
    results: dict[int, list[TorrentRelease]], request: SearchRequest, settings: Settings
) -> tuple[tuple[TorrentRelease, ...], tuple[TorrentRelease, ...]]:
    """The releases merged in indexer order, split into (shown, hidden): shown ones are relevant
    and allowed by the settings. Both sorted by seeders, most first."""
    merged = merge(release for index in sorted(results) for release in results[index])
    shown: list[TorrentRelease] = []
    hidden: list[TorrentRelease] = []
    for release in merged:
        ok = is_relevant(release, request) and passes_settings(release, settings)
        (shown if ok else hidden).append(release)
    shown.sort(key=lambda release: release.seeders, reverse=True)
    hidden.sort(key=lambda release: release.seeders, reverse=True)
    return tuple(shown), tuple(hidden)


def episode_request(show: Show, season: int, episode: int) -> SearchRequest:
    """The search for one episode (also used without the UI, by the automatic downloads)."""
    return SearchRequest(
        kind=SearchKind.EPISODE,
        title=show.name,
        year=show.year,
        season=season,
        episode=episode,
        imdb_id=show.imdb_id,
        queries=build_queries(
            SearchKind.EPISODE, show.name, year=show.year, season=season, episode=episode
        ),
    )


_Outcome = tuple[IndexerStatus, list[TorrentRelease]]


class TorrentSearchService:
    """Searches every indexer concurrently and streams the merged, filtered results."""

    def __init__(
        self, indexers: Sequence[BaseIndexer], settings: SettingsService, *, timeout: float = 25.0
    ) -> None:
        """`timeout` caps each indexer's whole search (retries included), in seconds, unless
        the indexer sets its own `search_timeout`."""
        self._indexers = tuple(indexers)
        self._settings = settings
        self._timeout = timeout

    # Request builders: what the UI searches for.

    def episode_request(self, show: Show, season: int, episode: int) -> SearchRequest:
        return episode_request(show, season, episode)

    def season_request(self, show: Show, season: int) -> SearchRequest:
        return SearchRequest(
            kind=SearchKind.SEASON,
            title=show.name,
            year=show.year,
            season=season,
            imdb_id=show.imdb_id,
            queries=build_queries(SearchKind.SEASON, show.name, year=show.year, season=season),
        )

    def movie_request(self, movie: Movie, details: MovieDetails | None = None) -> SearchRequest:
        imdb_id = (details.imdb_id if details else None) or movie.imdb_id
        return SearchRequest(
            kind=SearchKind.MOVIE,
            title=movie.title,
            year=movie.year,
            imdb_id=imdb_id,
            queries=build_queries(SearchKind.MOVIE, movie.title, year=movie.year),
        )

    async def search(self, request: SearchRequest) -> AsyncGenerator[SearchUpdate, None]:
        """Stream the search for `request` as snapshots.

        The first update lists every indexer as SEARCHING or SKIPPED; one more follows each time
        an indexer finishes, and the last one has `done=True` (the first already does when no
        indexer searches). Settings are read once, when the search starts. Closing the generator
        (`aclose()`, or cancelling the task iterating it) cancels and awaits the indexers still
        running; consumers that may stop early should iterate it under `contextlib.aclosing`.
        """
        settings = self._settings.current
        queries = text_queries(request)
        statuses: list[IndexerStatus] = []
        results: dict[int, list[TorrentRelease]] = {}
        tasks: dict[asyncio.Task[_Outcome], int] = {}  # running search -> indexer position
        try:
            for index, indexer in enumerate(self._indexers):
                reason = _skip_reason(indexer, request, settings)
                status = IndexerStatus(
                    name=indexer.name,
                    label=indexer.label,
                    state=IndexerState.SEARCHING if reason is None else IndexerState.SKIPPED,
                    message=reason,
                    web_url=indexer.web_search_url(queries[0]) if queries else None,
                )
                statuses.append(status)
                if reason is None:
                    task = asyncio.create_task(
                        self._run(indexer, request, status),
                        name=f"torrent search: {indexer.name}",
                    )
                    tasks[task] = index
            yield SearchUpdate(statuses=tuple(statuses), releases=(), done=not tasks)
            pending: set[asyncio.Task[_Outcome]] = set(tasks)
            while pending:
                finished, pending = await asyncio.wait(
                    pending, return_when=asyncio.FIRST_COMPLETED
                )
                ordered = sorted(finished, key=tasks.__getitem__)
                for position, task in enumerate(ordered, start=1):
                    index = tasks[task]
                    statuses[index], results[index] = task.result()
                    shown, hidden = _partition(results, request, settings)
                    yield SearchUpdate(
                        statuses=tuple(statuses),
                        releases=shown,
                        hidden=hidden,
                        done=not pending and position == len(ordered),
                    )
        finally:
            running = [task for task in tasks if not task.done()]
            for task in running:
                task.cancel()
            if running:
                await asyncio.wait(running)

    async def _run(
        self, indexer: BaseIndexer, request: SearchRequest, status: IndexerStatus
    ) -> _Outcome:
        """One indexer's search, turned into its final status; never raises but CancelledError."""
        limit = self._timeout if indexer.search_timeout is None else indexer.search_timeout
        deadline = asyncio.timeout(limit)
        try:
            async with deadline:
                releases = await indexer.search(request)
        except IndexerError as exc:
            log.warning("%s search failed: %s", indexer.label, exc)
            failure = str(exc)
        except Exception as exc:
            if isinstance(exc, TimeoutError) and deadline.expired():
                log.warning("%s search timed out after %g s", indexer.label, limit)
                failure = "timed out"
            else:
                log.exception("%s search failed unexpectedly", indexer.label)
                failure = "unexpected error"
        else:
            state = IndexerState.FOUND if releases else IndexerState.EMPTY
            return status.model_copy(update={"state": state, "count": len(releases)}), releases
        return status.model_copy(update={"state": IndexerState.FAILED, "message": failure}), []

    async def test_prowlarr(self) -> str:
        """'Prowlarr <version>' when the configured Prowlarr answers; IndexerError otherwise."""
        for indexer in self._indexers:
            if isinstance(indexer, ProwlarrIndexer):
                return await indexer.test()
        raise IndexerError("Prowlarr is not available")
