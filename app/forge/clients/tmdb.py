"""Forge's TMDB lookups on AirGrabber's `TMDBClient` (MediaForge's `api/tmdb.py`, without aiohttp).

`ForgeTmdb` is what the matcher's `TmdbLike` protocol asks for: a film search with the year and
then without (de-duplicated, in that order), a show search, a title's details and a season's
episode names. The key, retries, the semaphore and the error texts are the client's (the key is read
per request; no URL or httpx text ever reaches an error or a log). Added here:

- the names language: Forge's own `tmdb_language` (default en-US), read per lookup and passed
  to every call, so file names never follow AirGrabber's interface language;
- MediaForge's 7-day JSON cache in forge.db (`CacheRepo`). Its keys are MediaForge's own
  ("/search/movie?include_adult=false&language=en-US&query=Dune&year=2021": the path and the
  sorted parameters, never the API key), so a cache brought over from MediaForge keeps working.
  A search is stored as {"results": [...]}, a title or season as the object; a 404 isn't stored.

`TmdbError` is the client's `TMDBError` under MediaForge's name: what a lookup raises when TMDB
can't be reached, rejects the key or no key is set (`TMDBNotConfigured`, before any request).
`TmdbBadReply` (a `TmdbError`) is the one raised when TMDB answered with a body that isn't JSON or
hasn't the expected shape, where MediaForge's client returned None: the resolver tells it apart.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import ValidationError

from app.forge.db.cache import CacheRepo
from app.forge.models.media import MediaKind
from app.models.domain import MediaType
from app.models.tmdb import TMDBSeason, TMDBTitle
from app.services.tmdb import TMDBBadReply, TMDBClient, TMDBError

__all__ = [
    "DEFAULT_LANGUAGE", "JSON_TTL_S", "ForgeTmdb", "TmdbBadReply", "TmdbError", "cache_key",
]

TmdbError = TMDBError
TmdbBadReply = TMDBBadReply

JSON_TTL_S = 7 * 24 * 3600
DEFAULT_LANGUAGE = "en-US"


def cache_key(path: str, params: Mapping[str, str | int], language: str) -> str:
    """MediaForge's cache key: the path and the sorted parameters with `language` and
    `include_adult=false` (never the API key)."""
    query = {name: str(value) for name, value in params.items()}
    query["language"] = language
    query["include_adult"] = "false"
    return path + "?" + "&".join(f"{name}={value}" for name, value in sorted(query.items()))


def _kind(kind: MediaKind) -> MediaType:
    return MediaType.MOVIE if kind is MediaKind.MOVIE else MediaType.TV


def _results(data: Any) -> list[TMDBTitle] | None:
    """The titles of a cached search; malformed entries skipped, None when it isn't one."""
    raw = data.get("results") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return None
    titles: list[TMDBTitle] = []
    for item in raw:
        try:
            titles.append(TMDBTitle.model_validate(item))
        except ValidationError:
            continue
    return titles


class ForgeTmdb:
    """MediaForge's `TmdbLike` on AirGrabber's TMDB client, with Forge's cache and language."""

    def __init__(
        self,
        client: TMDBClient,
        *,
        cache: CacheRepo | None = None,
        language: Callable[[], str] = lambda: DEFAULT_LANGUAGE,
    ) -> None:
        """`language` is called for every lookup (Forge's `tmdb_language`), so a changed
        setting applies at once; empty means DEFAULT_LANGUAGE."""
        self._client = client
        self._cache = cache
        self._language = language

    def configured(self) -> bool:
        """Whether a TMDB key is set (not whether TMDB accepts it): without one Forge names
        files from what it reads in them."""
        return self._client.configured

    @property
    def language(self) -> str:
        return self._language().strip() or DEFAULT_LANGUAGE

    async def search_movie(self, query: str, year: int | None) -> list[TMDBTitle]:
        """Films matching `query`: with `year` first (when given), then without; each film once,
        in that order."""
        found: list[TMDBTitle] = []
        if year:
            found += await self._search(MediaType.MOVIE, query, year)
        found += await self._search(MediaType.MOVIE, query, None)
        seen: set[int] = set()
        unique: list[TMDBTitle] = []
        for title in found:
            if title.id not in seen:
                seen.add(title.id)
                unique.append(title)
        return unique

    async def search_tv(self, query: str, year: int | None) -> list[TMDBTitle]:
        """Shows matching `query`, first aired in `year` when given."""
        return await self._search(MediaType.TV, query, year)

    async def details(self, kind: MediaKind, tmdb_id: int) -> TMDBTitle | None:
        """The film (MediaKind.MOVIE) or show (an episode's) with this TMDB id; None when TMDB
        has none."""
        media = _kind(kind)
        language = self.language
        key = cache_key(f"/{media.value}/{tmdb_id}", {}, language)
        cached = await self._cached(key)
        if cached is not None:
            try:
                return TMDBTitle.model_validate(cached)
            except ValidationError:
                pass  # asked again below
        title = await self._client.title(media, tmdb_id, language=language)
        if title is not None:
            await self._store(key, title.model_dump(mode="json"))
        return title

    async def season(self, tv_id: int, number: int) -> TMDBSeason | None:
        """Season `number` of show `tv_id`; None when TMDB has no such season."""
        language = self.language
        key = cache_key(f"/tv/{tv_id}/season/{number}", {}, language)
        cached = await self._cached(key)
        if cached is not None:
            try:
                return TMDBSeason.model_validate(cached)
            except ValidationError:
                pass
        season = await self._client.tv_season(tv_id, number, language=language)
        if season is not None:
            await self._store(key, season.model_dump(mode="json"))
        return season

    async def _search(self, kind: MediaType, query: str, year: int | None) -> list[TMDBTitle]:
        if not query.strip():
            return []
        language = self.language
        path = "/search/movie" if kind is MediaType.MOVIE else "/search/tv"
        params: dict[str, str | int] = {"query": query}
        if year:
            params["year" if kind is MediaType.MOVIE else "first_air_date_year"] = year
        key = cache_key(path, params, language)
        cached = _results(await self._cached(key))
        if cached is not None:
            return cached
        titles = await self._client.find_titles(kind, query, year=year, language=language)
        await self._store(key, {"results": [title.model_dump(mode="json") for title in titles]})
        return titles

    async def _cached(self, key: str) -> Any | None:
        if self._cache is None:
            return None
        return await self._cache.get_json(key, JSON_TTL_S)

    async def _store(self, key: str, value: Any) -> None:
        if self._cache is not None:
            await self._cache.put_json(key, value)
