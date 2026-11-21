"""The phone's title page: `GET /v1/title?kind=movie|series&id=<tmdb id>`
(docs/companion/2026-11-20-title-page-design.md).

A full film or series page for any TMDB title, also one the library hasn't (from find's results,
the actor page, the suggestions): exactly `/v1/find`'s result item for it (find.py's `items_of`,
which also remembers it as found, so a download, a follow or an episode list takes it as any
search result) plus the page's details: `backdrop_url`, `tagline`, `genres` (TMDB's English names
split as the library's route does), `rating`, `runtime` (films), `seasons` and `network`
(series), `certification` (films, the PC's region), `trailer` (YouTube's only) and `cast` (at
most TITLE_CAST, billing order, with TMDB's person id for the actor page). `overview` is the
extras' (the PC's language, English where TMDB has none) when there is one.

The title itself (`DiscoveryService.movie` / `show`) is required: TMDB doesn't know it, 404
`not_found`; no key or a refused key, 409 `no_tmdb_key` / `tmdb_key_refused`; TMDB out of reach,
502 `failed` (`/v1/person`'s answers and find's sentences). The extras, the English info and the
film's or series' details are optional: one TMDB can't give (but not for want of a key) only
leaves its fields empty or null.

The log gets counts and kinds only: never a title, a TMDB id, a name or a character.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import Any, TypeVar

from app.i18n import t
from app.models import MovieDetails, TVDetails
from app.models.domain import TitleExtras, TitleInfo
from app.services.discovery import ENGLISH, DiscoveryService
from app.services.phone.coming_up import person_gone, safe_poster
from app.services.phone.find import (
    PhoneFind,
    couldnt_search,
    key_refused_message,
    no_key_message,
    tmdb_trouble,
)
from app.services.phone.server import Handler, Reply, Request, bad_request, error_reply
from app.services.phone.suggestions import split_genres
from app.services.profiles import ProfileService
from app.services.tmdb import (
    TMDBAuthError,
    TMDBClient,
    TMDBError,
    TMDBNotConfigured,
    TMDBNotFound,
)

log = logging.getLogger(__name__)

TITLE_CAST = 15
_ID_DIGITS = 10  # longer is no TMDB id
_KINDS = ("movie", "series")

T = TypeVar("T")
D = TypeVar("D", MovieDetails, TVDetails)


def _not_found() -> Reply:
    return error_reply(404, "not_found", t("AirGrabber couldn't find that title on TMDB."))


def _no_key() -> Reply:
    return error_reply(409, "no_tmdb_key", no_key_message())


def _not_asked(failure: BaseException) -> Reply:
    """No key, a refused key (409) or TMDB out of reach (502); anything but a TMDBError is
    raised again."""
    trouble = tmdb_trouble([failure], route="Phone title")
    if trouble == "no_key":
        return _no_key()
    if trouble == "key_refused":
        return error_reply(409, "tmdb_key_refused", key_refused_message())
    return couldnt_search()


def _fatal(result: object) -> bool:
    """A failure no part may swallow: no key, a refused key, cancellation or a bug."""
    return isinstance(result, BaseException) and (
        not isinstance(result, TMDBError)
        or isinstance(result, (TMDBNotConfigured, TMDBAuthError))
    )


def _told(result: T | BaseException, what: str) -> T | None:
    """An optional part's answer; None when TMDB couldn't give it (logged by its kind)."""
    if isinstance(result, BaseException):
        log.info("Phone title: no %s from TMDB: %s", what, type(result).__name__)
        return None
    return result


def _id_of(raw: str) -> int | None:
    if not (raw.isascii() and raw.isdigit() and len(raw) <= _ID_DIGITS):
        return None
    return int(raw) or None  # 0 is no TMDB id


def _trailer(extras: TitleExtras | None) -> dict[str, str] | None:
    trailer = extras.trailer if extras is not None else None
    if trailer is None or trailer.watch_url is None:  # only YouTube's
        return None
    return {"youtube_key": trailer.key, "name": trailer.name}


def _cast(extras: TitleExtras | None) -> list[dict[str, Any]]:
    if extras is None:
        return []
    return [
        {
            "name": member.name,
            "character": member.character,
            "photo_url": safe_poster(member.photo_url()),
            "tmdb_id": member.id,
        }
        for member in extras.cast[:TITLE_CAST]
    ]


def _page(
    item: dict[str, Any],
    extras: TitleExtras | None,
    english: TitleInfo | None,
    *,
    runtime: int | None,
    seasons: int | None,
    network: str | None,
    certification: str | None,
) -> dict[str, Any]:
    """`item` (find's) with the page's details; the extras' info first, else the English one."""
    info = extras.info if extras is not None else english
    tagline = (extras.info.tagline if extras is not None else "") or (
        english.tagline if english is not None else ""
    )
    page = {
        **item,
        "backdrop_url": safe_poster(info.backdrop_url()) if info is not None else None,
        "tagline": tagline or None,
        "genres": split_genres(english.genres) if english is not None else [],
        "rating": round(info.vote_average, 1) if info is not None and info.vote_count else None,
        "runtime": runtime,
        "seasons": seasons,
        "network": network,
        "certification": certification or None,
        "trailer": _trailer(extras),
        "cast": _cast(extras),
    }
    if extras is not None and extras.info.overview:
        page["overview"] = extras.info.overview
    return page


class PhoneTitle:
    def __init__(
        self,
        find: PhoneFind,
        discovery: DiscoveryService,
        tmdb: TMDBClient,
        profiles: ProfileService,
    ) -> None:
        """`tmdb`: only for the title's English info (its genres, as the library's route)."""
        self._find = find
        self._discovery = discovery
        self._tmdb = tmdb
        self._profiles = profiles

    def routes(self) -> dict[tuple[str, str], Handler]:
        return {("GET", "/v1/title"): self._title}

    async def _title(self, request: Request) -> Reply:
        problem = person_gone(request.phone, self._profiles)
        if problem is not None:
            return problem
        assert request.phone is not None
        kind = request.query.get("kind", "")
        tmdb_id = _id_of(request.query.get("id", ""))
        if kind not in _KINDS or tmdb_id is None:
            return bad_request()
        if not self._discovery.has_tmdb_key:
            return _no_key()
        person = request.phone.profile_id
        if kind == "movie":
            return await self._film(tmdb_id, person)
        return await self._series(tmdb_id, person)

    async def _film(self, tmdb_id: int, person: int) -> Reply:
        got = await self._gather(
            self._discovery.movie(tmdb_id),
            self._discovery.movie_extras(tmdb_id),
            self._tmdb.movie_info(tmdb_id, language=ENGLISH),
            self._discovery.movie_details(tmdb_id),
        )
        if isinstance(got, Reply):
            return got
        movie, extras, english, details = got
        [item], _ = await self._find.items_of([movie], [], person)
        info = extras.info if extras is not None else english
        runtime = (info.runtime if info is not None else None) or (
            details.runtime if details is not None else None
        )
        page = _page(
            item,
            extras,
            english,
            runtime=runtime,
            seasons=None,
            network=None,
            certification=details.certification if details is not None else None,
        )
        _log(page)
        return Reply(200, page)

    async def _series(self, tmdb_id: int, person: int) -> Reply:
        got = await self._gather(
            self._discovery.show(tmdb_id),
            self._discovery.tv_extras(tmdb_id),
            self._tmdb.tv_info(tmdb_id, language=ENGLISH),
            self._discovery.tv_details(tmdb_id),
        )
        if isinstance(got, Reply):
            return got
        show, extras, english, details = got
        _, [item] = await self._find.items_of([], [show], person)
        info = extras.info if extras is not None else english
        seasons = (info.seasons if info is not None else None) or (
            details.number_of_seasons if details is not None else None
        )
        networks = details.networks if details is not None else ()
        page = _page(
            item,
            extras,
            english,
            runtime=None,
            seasons=seasons,
            network=networks[0] if networks else None,
            certification=None,
        )
        _log(page)
        return Reply(200, page)

    async def _gather(
        self,
        title: Awaitable[T],
        extras: Awaitable[TitleExtras],
        english: Awaitable[TitleInfo],
        details: Awaitable[D],
    ) -> tuple[T, TitleExtras | None, TitleInfo | None, D | None] | Reply:
        """The title and its optional parts at once; a Reply when the title can't be had or
        TMDB can't be asked at all."""
        got_title, got_extras, got_english, got_details = await asyncio.gather(
            title, extras, english, details, return_exceptions=True
        )
        if isinstance(got_title, TMDBNotFound):
            log.info("Phone title: not on TMDB")
            return _not_found()
        if isinstance(got_title, BaseException):
            return _not_asked(got_title)
        for part in (got_extras, got_english, got_details):
            if _fatal(part):
                assert isinstance(part, BaseException)
                return _not_asked(part)
        return (
            got_title,
            _told(got_extras, "extras"),
            _told(got_english, "English info"),
            _told(got_details, "details"),
        )


def _log(page: dict[str, Any]) -> None:
    log.info(
        "Phone title: a %s page, %d cast, %s trailer",
        page["kind"], len(page["cast"]), "a" if page["trailer"] is not None else "no",
    )
