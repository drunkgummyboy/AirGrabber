"""Decide which TMDB result a file is, and fetch the official names for it.

`Resolver` works with any `TmdbLike` (in the app: `app.forge.clients.tmdb.ForgeTmdb`). It asks
`configured()` on every lookup, so a key added or removed in Settings applies at
once: without one, files are named from what the file name says (OFFLINE).

A remembered match whose details lookup fails because TMDB sent a malformed reply
(`TmdbBadReply`) falls back to a search, as MediaForge did (its client returned None there); any
other TMDB error (offline, key rejected) makes the file OFFLINE.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel

from app.forge.clients.tmdb import TmdbBadReply, TmdbError
from app.forge.core.activity import ActivityFeed, say_to
from app.forge.db.matches import MatchRepo
from app.forge.models.match import RememberedMatch
from app.forge.models.media import MatchState, MediaKind, ParsedMedia, ResolvedMedia
from app.i18n import N_, t
from app.models.tmdb import TMDBSeason, TMDBTitle

log = logging.getLogger(__name__)

MAX_CANDIDATES = 8
# The label stored for "don't use TMDB for this title". Kept in English in forge.db (as
# MediaForge stored it); show it through t() where a remembered match is listed.
SKIP_LABEL = N_("Don't use TMDB")
_NOT_WORD = re.compile(r"[^\w\s]")
_SPACES = re.compile(r"\s+")


class TmdbLike(Protocol):
    def configured(self) -> bool: ...
    async def search_movie(self, query: str, year: int | None) -> list[TMDBTitle]: ...
    async def search_tv(self, query: str, year: int | None) -> list[TMDBTitle]: ...
    async def details(self, kind: MediaKind, tmdb_id: int) -> TMDBTitle | None: ...
    async def season(self, tv_id: int, number: int) -> TMDBSeason | None: ...


class Resolution(BaseModel, frozen=True):
    media: ResolvedMedia
    candidates: tuple[TMDBTitle, ...] = ()


def normalize_title(text: str) -> str:
    text = text.casefold().replace("&", " and ")
    text = _NOT_WORD.sub(" ", text).replace("_", " ")
    return _SPACES.sub(" ", text).strip()


def match_key(parsed: ParsedMedia) -> tuple[MediaKind, str, int | None]:
    return parsed.kind, normalize_title(parsed.title), parsed.year


def _names(result: TMDBTitle) -> set[str]:
    return {normalize_title(result.title), normalize_title(result.original_title or "")}


def choose(
    results: Sequence[TMDBTitle], title: str, year: int | None
) -> tuple[MatchState, TMDBTitle | None]:
    """The result a file is, and how sure that is. CONFIDENT: a result whose (original) title is
    the file's and whose year is the file's; or the only result, when its (original) title is
    the file's and, if the file has a year, so is its year. Anything else is UNCERTAIN (the
    top result, the owner chooses): a lone result with another name is not taken on trust (a
    home video or a course in a watched folder would otherwise be renamed after some film)."""
    if not results:
        return MatchState.NOT_FOUND, None
    wanted = normalize_title(title)
    if year:
        for r in results:
            if r.year == year and wanted in _names(r):
                return MatchState.CONFIDENT, r
    if len(results) == 1:
        only = results[0]
        if wanted in _names(only) and (not year or only.year == year):
            return MatchState.CONFIDENT, only
    return MatchState.UNCERTAIN, results[0]


class Resolver:
    def __init__(
        self,
        tmdb: TmdbLike | None,
        matches: MatchRepo,
        *,
        activity: ActivityFeed | None = None,
    ) -> None:
        self._tmdb = tmdb
        self._matches = matches
        self._say = say_to(activity)  # the Activity strip, next to each English log line

    def set_client(self, tmdb: TmdbLike | None) -> None:
        """Swap the TMDB client (not needed with ForgeTmdb, which follows the key itself)."""
        self._tmdb = tmdb

    def _online(self) -> TmdbLike | None:
        tmdb = self._tmdb
        return tmdb if tmdb is not None and tmdb.configured() else None

    async def resolve(self, parsed: ParsedMedia) -> Resolution:
        tmdb = self._online()
        if tmdb is None:
            return Resolution(media=ResolvedMedia.offline(parsed))
        try:
            remembered = await self._matches.get(*match_key(parsed))
            if remembered is not None:
                if remembered.tmdb_id is None:
                    return Resolution(media=ResolvedMedia.offline(parsed, MatchState.SKIPPED))
                details: TMDBTitle | None = None
                try:
                    details = await tmdb.details(parsed.kind, remembered.tmdb_id)
                except TmdbBadReply:
                    log.warning("TMDB sent a malformed reply for a remembered match; searching")
                if details is not None:
                    media = await self._build(parsed, details, MatchState.REMEMBERED)
                    return Resolution(media=media)
            results = await self._search(tmdb, parsed, parsed.title)
            if not results and parsed.short_title:
                results = await self._search(tmdb, parsed, parsed.short_title)
            state, chosen = choose(results, parsed.title, parsed.year)
            if chosen is None:
                return Resolution(media=ResolvedMedia.offline(parsed, MatchState.NOT_FOUND))
            media = await self._build(parsed, chosen, state)
            return Resolution(media=media, candidates=tuple(results[:MAX_CANDIDATES]))
        except TmdbError as exc:
            # The client's error texts hold the endpoint path only (no URL, no key).
            log.warning("TMDB lookup failed (%s): %s", type(exc).__name__, exc)
            self._say(
                t("TMDB lookup for {name} failed", name=parsed.source_name),
                level=logging.WARNING,
            )
            return Resolution(media=ResolvedMedia.offline(parsed))

    @staticmethod
    async def _search(tmdb: TmdbLike, parsed: ParsedMedia, query: str) -> list[TMDBTitle]:
        if parsed.kind is MediaKind.EPISODE:
            return await tmdb.search_tv(query, parsed.year)
        return await tmdb.search_movie(query, parsed.year)

    async def apply_choice(
        self, parsed: ParsedMedia, chosen: TMDBTitle | None, *, remember: bool = True
    ) -> ResolvedMedia:
        kind, norm, year = match_key(parsed)
        if remember:
            label = f"{chosen.title} ({chosen.year})" if chosen else SKIP_LABEL
            await self._matches.remember(
                RememberedMatch(
                    kind=kind,
                    norm_title=norm,
                    year=year,
                    tmdb_id=chosen.id if chosen else None,
                    label=label,
                )
            )
        if chosen is None:
            return ResolvedMedia.offline(parsed, MatchState.SKIPPED)
        try:
            return await self._build(parsed, chosen, MatchState.REMEMBERED)
        except TmdbError:
            return self._from_result(parsed, chosen, MatchState.REMEMBERED, None)

    @staticmethod
    def _from_result(
        parsed: ParsedMedia, chosen: TMDBTitle, state: MatchState, season: TMDBSeason | None
    ) -> ResolvedMedia:
        episode_title = parsed.episode_title
        episode_overview = ""
        known = False
        if season is not None and parsed.episode is not None:
            for ep in season.episodes:
                if ep.episode_number == parsed.episode and ep.name:
                    episode_title = ep.name
                    episode_overview = ep.overview or ""
                    known = True
                    break
        return ResolvedMedia(
            parsed=parsed,
            title=chosen.title,
            year=chosen.year or parsed.year,
            episode_title=episode_title,
            match_state=state,
            tmdb_id=chosen.id,
            poster_path=chosen.poster_path,
            backdrop_path=chosen.backdrop_path,
            season_poster_path=season.poster_path if season else None,
            original_title=chosen.original_title,
            episode_known=known,
            overview=chosen.overview,
            episode_overview=episode_overview,
        )

    async def _build(
        self, parsed: ParsedMedia, chosen: TMDBTitle, state: MatchState
    ) -> ResolvedMedia:
        season: TMDBSeason | None = None
        tmdb = self._online()
        if parsed.kind is MediaKind.EPISODE and parsed.season is not None and tmdb is not None:
            try:
                season = await tmdb.season(chosen.id, parsed.season)
            except TmdbError as exc:
                log.warning("Season lookup failed (%s): %s", type(exc).__name__, exc)
                self._say(
                    t("Season lookup for {title} failed", title=chosen.title),
                    level=logging.WARNING,
                )
        return self._from_result(parsed, chosen, state, season)
