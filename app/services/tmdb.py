"""Async TMDB API client (https://developer.themoviedb.org/reference).

The shared `httpx.AsyncClient` (timeouts, User-Agent) belongs to the caller: this client only
issues requests on it. Payloads are validated into domain models here, at the boundary.

Secret hygiene: a v3 API key travels in the query string, so request URLs and httpx exceptions
may contain it. Error messages and log records are therefore built from the endpoint path only, and no
httpx or validation exception is ever chained to (or left as the context of) a TMDBError.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Generic, TypeVar

import httpx
from pydantic import ValidationError

from app.i18n import t
from app.models.domain import (
    CastMember,
    DiscoverShow,
    MediaType,
    Movie,
    MovieDetails,
    TitleInfo,
    TVDetails,
    Video,
)
from app.models.tmdb import (
    TMDBAggregateCredits,
    TMDBCredits,
    TMDBFindResults,
    TMDBGenreList,
    TMDBMovieCollection,
    TMDBMovieDetails,
    TMDBMovieInfo,
    TMDBMovieItem,
    TMDBPerson,
    TMDBPersonResult,
    TMDBSeason,
    TMDBTitle,
    TMDBTitleIds,
    TMDBTVDetails,
    TMDBTVInfo,
    TMDBTVItem,
    TMDBVideos,
    to_person_credit,
)
from app.models.person import Person, PersonMatch

log = logging.getLogger(__name__)

T = TypeVar("T")

_RATE_LIMITED_ATTEMPTS = 4
# 5xx responses and transport errors (timeouts included).
_FAILED_ATTEMPTS = 3
# v4 "API Read Access Tokens" are JWTs; anything else is a v3 API key.
_V4_TOKEN_PREFIX = "eyJ"
_IMDB_ID = re.compile(r"tt\d{1,10}")
# What `film_page` appends to a film's /movie/{id}.
FILM_PAGE_PARTS = "credits,videos,release_dates,external_ids"
# TMDB's lists: 20 results a page, and no page after 500 (a later one is refused).
PAGE_SIZE = 20
MAX_PAGE = 500


class TMDBError(Exception):
    """TMDB unreachable, returned an error status, or sent an unparseable payload."""


class TMDBNotConfigured(TMDBError):
    """No TMDB API key is set; raised before any request is made."""


class TMDBAuthError(TMDBError):
    """TMDB answered 401: the API key is invalid or was revoked."""


class TMDBNotFound(TMDBError):
    """TMDB answered 404: the requested resource does not exist."""


class TMDBBadReply(TMDBError):
    """TMDB answered, but the body isn't JSON or doesn't have the expected shape."""


@dataclass(frozen=True, slots=True)
class ResultPage(Generic[T]):
    """One page of a TMDB list and how many pages the list has (None: TMDB didn't say)."""

    items: tuple[T, ...]
    page: int
    total_pages: int | None

    @property
    def is_last(self) -> bool:
        """No page follows this one: TMDB says so, it is page MAX_PAGE, or (without a total)
        the page wasn't full."""
        if self.page >= MAX_PAGE:
            return True
        if self.total_pages is not None:
            return self.page >= self.total_pages
        return len(self.items) < PAGE_SIZE


@dataclass(frozen=True, slots=True)
class FilmPage:
    """All a film's info window needs from TMDB, from one request (`TMDBClient.film_page`)."""

    details: MovieDetails
    info: TitleInfo
    cast: tuple[CastMember, ...]
    videos: tuple[Video, ...]


class TMDBClient:
    BASE_URL = "https://api.themoviedb.org/3"

    def __init__(
        self,
        http: httpx.AsyncClient,
        api_key: Callable[[], str],
        *,
        max_concurrent: int = 6,
        retry_delay: float = 1.0,
        language: str = "en-US",
        region: str = "US",
    ) -> None:
        """`api_key` is called for every request, so a key changed in Settings applies at once."""
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        self._http = http
        self._api_key = api_key
        self._slots = asyncio.Semaphore(max_concurrent)
        self._retry_delay = retry_delay
        self._language = language
        self._region = region

    async def trending_tv(self) -> list[DiscoverShow]:
        """This week's trending shows (first page, at most 20)."""
        return await self._list("/trending/tv/week", {}, _discover_show)

    async def tv_recommendations(self, tv_id: int) -> list[DiscoverShow]:
        """TMDB's picks for people who like show `tv_id` (first page)."""
        return await self._list(f"/tv/{int(tv_id)}/recommendations", {}, _discover_show)

    async def tv_on_the_air(self) -> list[DiscoverShow]:
        """Shows with an episode airing in the next seven days (first page)."""
        return await self._list("/tv/on_the_air", {}, _discover_show)

    async def tv_premieres(self, start: date, end: date) -> list[DiscoverShow]:
        """English-language shows first airing between `start` and `end`, most popular first."""
        params = {
            "sort_by": "popularity.desc",
            "with_original_language": "en",
            "first_air_date.gte": start.isoformat(),
            "first_air_date.lte": end.isoformat(),
        }
        return await self._list("/discover/tv", params, _discover_show)

    async def trending_movies(self) -> list[Movie]:
        """This week's trending films (first page, at most 20)."""
        return await self._list("/trending/movie/week", {}, _movie)

    async def trending_movies_page(self, page: int) -> ResultPage[Movie]:
        """Page `page` (1 to MAX_PAGE) of this week's trending films (the Movies page's See
        more)."""
        return await self._page("/trending/movie/week", {}, _movie, page)

    async def digital_releases(self, start: date, end: date, *, page: int = 1) -> list[Movie]:
        """Films released digitally in the client's region between `start` and `end`.

        One page (at most 20) of TMDB's results, most popular first. TMDB lists a film when any
        of its digital releases falls in the window, later re-releases included.
        """
        return list((await self.digital_releases_page(start, end, page=page)).items)

    async def digital_releases_page(
        self, start: date, end: date, *, page: int = 1
    ) -> ResultPage[Movie]:
        """`digital_releases`' page `page` (1 to MAX_PAGE) with the list's number of pages."""
        params = {
            "sort_by": "popularity.desc",
            "with_release_type": "4",
            "region": self._region,
            "release_date.gte": start.isoformat(),
            "release_date.lte": end.isoformat(),
        }
        return await self._page("/discover/movie", params, _movie, page)

    async def search_movies(self, query: str, *, limit: int = 12) -> list[Movie]:
        """Films matching `query`, TMDB's best match first."""
        query = query.strip()
        if not query or limit <= 0:
            return []
        params = {"query": query, "include_adult": "false"}
        return (await self._list("/search/movie", params, _movie))[:limit]

    async def search_tv(self, query: str, *, limit: int = 12) -> list[DiscoverShow]:
        """Shows matching `query`, TMDB's best match first (as the lists know shows)."""
        query = query.strip()
        if not query or limit <= 0:
            return []
        params = {"query": query, "include_adult": "false"}
        return (await self._list("/search/tv", params, _discover_show))[:limit]

    async def search_people(self, name: str) -> list[PersonMatch]:
        """People matching `name`, TMDB's best match first (one page; none for no text)."""
        name = name.strip()
        if not name:
            return []
        params = {"query": name, "include_adult": "false"}
        return await self._list("/search/person", params, _person_match)

    async def person(self, person_id: int) -> Person:
        """A person (/person/{id}) in the client's language with their combined_credits' cast
        parts appended, in one request. A malformed part is left out (as in a list); a part
        that is neither a film nor a series too. TMDBNotFound when TMDB has no such person."""
        path = f"/person/{person_id}"
        payload = await self._get_json(path, {"append_to_response": "combined_credits"})
        person = _parse_one(path, payload, TMDBPerson.model_validate)
        combined = payload.get("combined_credits") if isinstance(payload, dict) else None
        parts = (
            _parse_results(path, combined, to_person_credit, key="cast")
            if isinstance(combined, dict) and isinstance(combined.get("cast"), list)
            else []
        )
        return person.to_person([part for part in parts if part is not None])

    async def movie_details(self, movie_id: int) -> MovieDetails:
        """IMDb id, runtime, and the client region's certification and release dates."""
        path = f"/movie/{movie_id}"
        payload = await self._get_json(path, {"append_to_response": "release_dates,external_ids"})
        region = self._region
        return _parse_one(path, payload, lambda raw: _movie_details(raw, region))

    async def tv_details(self, tv_id: int) -> TVDetails:
        path = f"/tv/{tv_id}"
        payload = await self._get_json(path, {"append_to_response": "external_ids"})
        return _parse_one(path, payload, _tv_details)

    async def movie(self, movie_id: int) -> Movie:
        """A film as TMDB's lists describe it (title, release date, poster, overview, rating),
        from its details: for a film known only by its id (a grab in the download history)."""
        path = f"/movie/{movie_id}"
        payload = await self._get_json(path, {})
        return _parse_one(path, payload, _movie)

    async def collection_of(self, movie_id: int) -> list[Movie]:
        """The films of the collection (film series) film `movie_id` belongs to, itself included,
        by release date (those without one last); empty when it belongs to none. A malformed
        part is left out, as in a list."""
        path = f"/movie/{movie_id}"
        payload = await self._get_json(path, {})
        link = _parse_one(
            path, payload, lambda raw: TMDBMovieCollection.model_validate(raw).belongs_to_collection
        )
        if link is None:
            return []
        path = f"/collection/{link.id}"
        parts = _parse_results(path, await self._get_json(path, {}), _movie, key="parts")
        return sorted(
            parts,
            key=lambda part: (part.release_date is None, part.release_date or date.min, part.id),
        )

    async def tv(self, tv_id: int) -> DiscoverShow:
        """A show as TMDB's lists describe it, from its details: for a show known only by its id
        (a row of the phones' shortlist)."""
        path = f"/tv/{tv_id}"
        payload = await self._get_json(path, {})
        return _parse_one(path, payload, _discover_show)

    @property
    def configured(self) -> bool:
        """Whether a key is set (not whether TMDB accepts it)."""
        return bool(self._api_key().strip())

    async def movie_info(self, movie_id: int, *, language: str | None = None) -> TitleInfo:
        """A film's overview, genres, images, runtime, status and rating in `language` (an
        IETF tag such as "nl-NL"; None = the client's). TMDB leaves the overview empty when it
        has none in that language."""
        path = f"/movie/{movie_id}"
        payload = await self._get_json(path, {}, language=language)
        return _parse_one(path, payload, lambda raw: TMDBMovieInfo.model_validate(raw).to_info())

    async def film_page(
        self, movie_id: int, *, language: str | None = None, video_languages: Sequence[str] = ()
    ) -> FilmPage:
        """A film's details (as `movie_details`), its info in `language` (as `movie_info`), its
        cast (as `cast`) and its videos in `video_languages` and none (as `videos`), in one
        request: /movie/{id} with the others appended. A broken cast or videos part only leaves
        that out (logged); broken details or info raise TMDBBadReply."""
        path = f"/movie/{movie_id}"
        params = {"append_to_response": FILM_PAGE_PARTS}
        params["include_video_language"] = ",".join(dict.fromkeys((*video_languages, "null")))
        payload = await self._get_json(path, params, language=language)
        region = self._region
        details = _parse_one(path, payload, lambda raw: _movie_details(raw, region))
        info = _parse_one(path, payload, lambda raw: TMDBMovieInfo.model_validate(raw).to_info())
        raw = payload if isinstance(payload, dict) else {}
        cast = _parse_part(
            path, "credits", raw, lambda part: TMDBCredits.model_validate(part).to_cast()
        )
        videos = _parse_part(
            path, "videos", raw, lambda part: TMDBVideos.model_validate(part).to_videos()
        )
        return FilmPage(details, info, tuple(cast), tuple(videos))

    async def tv_info(self, tv_id: int, *, language: str | None = None) -> TitleInfo:
        """A show's overview, genres, images, seasons, status and rating in `language`."""
        path = f"/tv/{tv_id}"
        payload = await self._get_json(path, {}, language=language)
        return _parse_one(path, payload, lambda raw: TMDBTVInfo.model_validate(raw).to_info())

    async def genres(self, kind: MediaType, *, language: str | None = None) -> dict[int, str]:
        """What a list item's `genre_ids` mean for this kind, in `language`."""
        path = f"/genre/{kind.value}/list"
        payload = await self._get_json(path, {}, language=language)
        return _parse_one(path, payload, lambda raw: TMDBGenreList.model_validate(raw).to_names())

    async def videos(
        self, kind: MediaType, item_id: int, *, languages: Sequence[str] = ()
    ) -> list[Video]:
        """A film's or show's videos (trailers, teasers, clips), in `languages` (ISO 639-1
        codes) and those without a language; without `languages`, TMDB's default."""
        path = f"/{kind.value}/{item_id}/videos"
        params = {"include_video_language": ",".join((*languages, "null"))} if languages else {}
        payload = await self._get_json(path, params)
        return _parse_one(path, payload, lambda raw: TMDBVideos.model_validate(raw).to_videos())

    async def season_videos(
        self, tv_id: int, season: int, *, languages: Sequence[str] = ()
    ) -> list[Video]:
        """One season's videos, in `languages` as `videos` has them."""
        path = f"/tv/{tv_id}/season/{season}/videos"
        params = {"include_video_language": ",".join((*languages, "null"))} if languages else {}
        payload = await self._get_json(path, params)
        return _parse_one(path, payload, lambda raw: TMDBVideos.model_validate(raw).to_videos())

    async def cast(self, kind: MediaType, item_id: int) -> list[CastMember]:
        """A film's cast in billing order (/credits), or a show's whole run's cast, the most
        seen first (/aggregate_credits)."""
        if kind is MediaType.MOVIE:
            path = f"/movie/{item_id}/credits"
            payload = await self._get_json(path, {})
            return _parse_one(path, payload, lambda raw: TMDBCredits.model_validate(raw).to_cast())
        path = f"/tv/{item_id}/aggregate_credits"
        payload = await self._get_json(path, {})
        return _parse_one(
            path, payload, lambda raw: TMDBAggregateCredits.model_validate(raw).to_cast()
        )

    async def find_tv(self, imdb_id: str) -> int | None:
        """The TMDB id of the show with this IMDb id ("tt…"); None when TMDB knows none (or
        the id isn't one: nothing is asked then)."""
        if not _IMDB_ID.fullmatch(imdb_id):
            return None
        path = f"/find/{imdb_id}"
        payload = await self._get_json(path, {"external_source": "imdb_id"})
        return _parse_one(
            path, payload, lambda raw: TMDBFindResults.model_validate(raw).first_tv_id()
        )

    # MediaForge's (Forge's) lookups: no adult titles, in the language asked for.

    async def find_titles(
        self,
        kind: MediaType,
        query: str,
        *,
        year: int | None = None,
        language: str | None = None,
    ) -> list[TMDBTitle]:
        """Films (/search/movie, `year`) or shows (/search/tv, `first_air_date_year`) matching
        `query`, TMDB's best match first, in `language` (None = the client's). One page;
        malformed entries are skipped; a 404 gives an empty list."""
        query = query.strip()
        if not query:
            return []
        path = "/search/movie" if kind is MediaType.MOVIE else "/search/tv"
        params = {"query": query, "include_adult": "false"}
        if year:
            params["year" if kind is MediaType.MOVIE else "first_air_date_year"] = str(year)
        try:
            payload = await self._get_json(path, params, language=language)
        except TMDBNotFound:
            return []
        return _parse_results(path, payload, TMDBTitle.model_validate)

    async def title(
        self, kind: MediaType, tmdb_id: int, *, language: str | None = None
    ) -> TMDBTitle | None:
        """A film (/movie/{id}) or show (/tv/{id}) in `language`; None when TMDB has none."""
        path = f"/{kind.value}/{tmdb_id}"
        try:
            payload = await self._get_json(path, {"include_adult": "false"}, language=language)
        except TMDBNotFound:
            return None
        return _parse_one(path, payload, TMDBTitle.model_validate)

    async def title_ids(self, kind: MediaType, tmdb_id: int) -> TMDBTitleIds | None:
        """A film (/movie/{id}) or show (/tv/{id}) in the client's language, with its IMDb and
        TheTVDB ids (external_ids appended), in one request: the folder library's titles.
        None when TMDB has none."""
        path = f"/{kind.value}/{tmdb_id}"
        try:
            payload = await self._get_json(path, {"append_to_response": "external_ids"})
        except TMDBNotFound:
            return None
        return _parse_one(path, payload, TMDBTitleIds.model_validate)

    async def tv_season(
        self, tv_id: int, number: int, *, language: str | None = None
    ) -> TMDBSeason | None:
        """Season `number` of show `tv_id` (its poster and episode names) in `language`; None
        when TMDB has no such season."""
        path = f"/tv/{tv_id}/season/{number}"
        try:
            payload = await self._get_json(path, {"include_adult": "false"}, language=language)
        except TMDBNotFound:
            return None
        return _parse_one(path, payload, TMDBSeason.model_validate)

    async def check_key(self, key: str) -> bool:
        """Whether TMDB accepts `key` (a v3 API key or a v4 read access token), whatever key the
        client is configured with.

        One request to /authentication, without retries. False when TMDB rejects the key (401)
        or it is empty or contains invalid characters (then nothing is sent). Raises TMDBError
        when TMDB can't be reached or answers anything else. Like every request here, neither
        the key nor the URL ends up in a log record or an error.
        """
        key = key.strip()
        if not key or not (key.isascii() and key.isprintable()):
            return False
        path = "/authentication"
        query, headers = _auth(key)
        response: httpx.Response | None = None
        error_name = ""
        try:
            async with self._slots:
                response = await self._http.get(self.BASE_URL + path, params=query, headers=headers)
        except httpx.HTTPError as exc:  # connection failures, timeouts, undecodable bodies
            error_name = type(exc).__name__
        # Raised out here so no httpx exception (which may carry the URL) becomes the context.
        if response is None:
            raise TMDBError(t("TMDB {path}: request failed ({error})", path=path, error=error_name))
        if response.status_code == 401:
            return False
        if not response.is_success:
            raise TMDBError(f"TMDB {path}: HTTP {response.status_code}")
        return True

    async def _list(
        self, path: str, params: Mapping[str, str], convert: Callable[[object], T]
    ) -> list[T]:
        return _parse_results(path, await self._get_json(path, params), convert)

    async def _page(
        self, path: str, params: Mapping[str, str], convert: Callable[[object], T], page: int
    ) -> ResultPage[T]:
        """Page `page` of a list (no `page` parameter for the first, as `_list` asks it)."""
        if not 1 <= page <= MAX_PAGE:
            raise ValueError(f"page must be from 1 to {MAX_PAGE}")
        query = {**params, "page": str(page)} if page > 1 else dict(params)
        payload = await self._get_json(path, query)
        items = tuple(_parse_results(path, payload, convert))
        total = payload.get("total_pages") if isinstance(payload, dict) else None
        if not isinstance(total, int) or isinstance(total, bool) or total < 0:
            total = None
        return ResultPage(items, page, total)

    async def _get_json(
        self, path: str, params: Mapping[str, str], *, language: str | None = None
    ) -> object:
        """GET `path` and decode its JSON body (in `language`; None = the client's).

        429 is retried up to 4 attempts in all, 5xx and transport errors up to 3; attempts are
        counted together and the limit of the latest failure applies. The semaphore is held only
        for the request itself, never while waiting to retry. Every raise sits outside the
        `except` blocks so no httpx exception (which may carry the URL) becomes its context.
        """
        key = self._api_key().strip()
        if not key:
            raise TMDBNotConfigured(t("TMDB API key is not set"))
        if not (key.isascii() and key.isprintable()):  # e.g. a pasted zero-width space
            raise TMDBAuthError(t("TMDB API key contains invalid characters"))
        auth_query, headers = _auth(key)
        query = {**params, "language": language or self._language, **auth_query}
        url = self.BASE_URL + path
        attempt = 0
        while True:
            attempt += 1
            response: httpx.Response | None = None
            error_name = ""
            retryable = True
            try:
                async with self._slots:
                    response = await self._http.get(url, params=query, headers=headers)
            except httpx.TransportError as exc:  # connection failures and timeouts
                error_name = type(exc).__name__
            except httpx.RequestError as exc:  # e.g. an undecodable body; retrying won't help
                error_name = type(exc).__name__
                retryable = False
            if response is None:
                if not retryable:
                    raise TMDBError(
                        t("TMDB {path}: request failed ({error})", path=path, error=error_name)
                    ) from None
                reason = error_name
                max_attempts = _FAILED_ATTEMPTS
                delay = self._backoff(attempt)
            else:
                status = response.status_code
                if response.is_success:
                    return _decode_json(path, response)
                if status == 401:
                    raise TMDBAuthError(t("TMDB rejected the API key (HTTP 401 on {path})", path=path))
                if status == 404:
                    raise TMDBNotFound(t("TMDB {path}: not found (HTTP 404)", path=path))
                reason = f"HTTP {status}"
                if status == 429:
                    max_attempts = _RATE_LIMITED_ATTEMPTS
                    retry_after = _retry_after(response)
                    delay = self._backoff(attempt) if retry_after is None else retry_after
                elif status >= 500:
                    max_attempts = _FAILED_ATTEMPTS
                    delay = self._backoff(attempt)
                else:
                    raise TMDBError(f"TMDB {path}: {reason}")
            if attempt >= max_attempts:
                raise TMDBError(
                    t(
                        "TMDB {path}: {reason} (gave up after {n} attempts)",
                        path=path,
                        reason=reason,
                        n=attempt,
                    )
                ) from None
            log.warning(
                "TMDB %s: %s; retrying in %.1f s (attempt %d of %d)",
                _logged(path), reason, delay, attempt + 1, max_attempts,
            )
            await asyncio.sleep(delay)

    def _backoff(self, attempt: int) -> float:
        """Exponential delay after the `attempt`-th (1-based) failed attempt."""
        return self._retry_delay * 2.0 ** (attempt - 1)


def _auth(key: str) -> tuple[dict[str, str], dict[str, str]]:
    """(query parameters, headers) that authenticate a request with `key`: a v4 token travels
    as a Bearer header, a v3 key as the `api_key` parameter."""
    if key.startswith(_V4_TOKEN_PREFIX):
        return {}, {"Authorization": f"Bearer {key}"}
    return {"api_key": key}, {}


def _retry_after(response: httpx.Response) -> float | None:
    """Seconds from a numeric `Retry-After` header; None when absent or an HTTP date."""
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _decode_json(path: str, response: httpx.Response) -> object:
    try:
        return response.json()
    except ValueError:  # JSONDecodeError and UnicodeDecodeError are both ValueErrors
        pass
    # Raised out here so the decode error (which holds the whole body) isn't its context.
    raise TMDBBadReply(t("TMDB {path}: response is not valid JSON", path=path))


def _summarize(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
        for error in exc.errors()
    )


def _parse_one(path: str, payload: object, convert: Callable[[object], T]) -> T:
    try:
        return convert(payload)
    except ValidationError as exc:
        problem = _summarize(exc)
    # Deliberately not chained: a ValidationError's str() quotes the input payload.
    raise TMDBBadReply(t("TMDB {path}: unexpected payload ({problem})", path=path, problem=problem))


def _parse_part(
    path: str, name: str, payload: Mapping[str, object], convert: Callable[[object], list[T]]
) -> list[T]:
    """An appended part (`append_to_response`) of `payload`; none when it is missing or broken
    (logged by its problem only: a ValidationError's str() quotes the payload)."""
    if name not in payload:
        return []
    try:
        return convert(payload[name])
    except ValidationError as exc:
        log.info("TMDB %s: no %s: %s", _logged(path), name, _summarize(exc))
        return []


def _parse_results(
    path: str, payload: object, convert: Callable[[object], T], *, key: str = "results"
) -> list[T]:
    """Convert a page's `results` (or `key`) one by one so a single malformed entry can't sink
    the rest."""
    results = payload.get(key) if isinstance(payload, dict) else None
    if not isinstance(results, list):
        if key != "results":
            raise TMDBBadReply(f"TMDB {path}: expected an object with a '{key}' list")
        raise TMDBBadReply(t("TMDB {path}: expected an object with a 'results' list", path=path))
    parsed: list[T] = []
    for index, item in enumerate(results):
        try:
            parsed.append(convert(item))
        except ValidationError as exc:
            log.warning(
                "TMDB %s: skipping malformed item %d: %s", _logged(path), index, _summarize(exc)
            )
    return parsed


_AN_ID = re.compile(r"/\d+")


def _logged(path: str) -> str:
    """A request's path as the log may hold it: "/movie/{id}", never a title's id (the phone's
    search asks about what a person typed)."""
    return _AN_ID.sub("/{id}", path)


def _movie(raw: object) -> Movie:
    return TMDBMovieItem.model_validate(raw).to_movie()


def _discover_show(raw: object) -> DiscoverShow:
    return TMDBTVItem.model_validate(raw).to_discover_show()


def _person_match(raw: object) -> PersonMatch:
    return TMDBPersonResult.model_validate(raw).to_match()


def _movie_details(raw: object, region: str) -> MovieDetails:
    return TMDBMovieDetails.model_validate(raw).to_details(region)


def _tv_details(raw: object) -> TVDetails:
    return TMDBTVDetails.model_validate(raw).to_details()
