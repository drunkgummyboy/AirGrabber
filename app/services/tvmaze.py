"""Async TVMaze API client (https://www.tvmaze.com/api).

The shared `httpx.AsyncClient` (timeouts, User-Agent) belongs to the caller: this client only
issues requests on it. Payloads are validated into domain models here, at the boundary.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Callable, Mapping
from datetime import date
from typing import Literal, TypeVar

import httpx
from pydantic import TypeAdapter, ValidationError

from app.models.domain import Episode, Show
from app.models.tvmaze import TVMazeScheduleItem, TVMazeSearchResult, TVMazeShow

log = logging.getLogger(__name__)

T = TypeVar("T")

# TVMaze allows ~20 calls per 10 s per IP and answers 429 beyond that.
_RATE_LIMITED_ATTEMPTS = 4
# 5xx responses and transport errors (timeouts included).
_FAILED_ATTEMPTS = 3

_UPDATES = TypeAdapter(dict[int, int])


class TVMazeError(Exception):
    """TVMaze unreachable, returned an error status, or sent an unparseable payload."""


class TVMazeNotFound(TVMazeError):
    """TVMaze answered 404: the requested resource does not exist."""


class TVMazeClient:
    BASE_URL = "https://api.tvmaze.com"

    def __init__(
        self, http: httpx.AsyncClient, *, max_concurrent: int = 4, retry_delay: float = 1.0
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        self._http = http
        self._slots = asyncio.Semaphore(max_concurrent)
        self._retry_delay = retry_delay

    async def search_shows(self, query: str, *, limit: int = 10) -> list[Show]:
        """Shows matching `query`, best match first (TVMaze returns at most 10)."""
        return [show for show, _ in await self.search_shows_with_country(query, limit=limit)]

    async def search_shows_with_country(
        self, query: str, *, limit: int = 10
    ) -> list[tuple[Show, str | None]]:
        """Like `search_shows`, each show paired with its network's country code (or None)."""
        query = query.strip()
        if not query or limit <= 0:
            return []
        path = "/search/shows"
        payload = await self._get_json(path, {"q": query})
        return _parse_items(path, payload, _search_hit)[:limit]

    async def get_show(self, show_id: int) -> Show:
        path = f"/shows/{show_id}"
        return _parse_one(path, await self._get_json(path), _show)

    async def lookup_show(
        self, *, imdb: str | None = None, thetvdb: int | None = None
    ) -> Show | None:
        """The show TVMaze files under an IMDb or TheTVDB id, or None when it knows none.

        Pass exactly one of the two ids. TVMaze answers a hit with a redirect to the show (the
        shared client follows redirects) and a miss with 404.
        """
        if (imdb is None) == (thetvdb is None):
            raise ValueError("pass exactly one of imdb or thetvdb")
        path = "/lookup/shows"
        params = {"imdb": imdb} if imdb is not None else {"thetvdb": str(thetvdb)}
        try:
            payload = await self._get_json(path, params)
        except TVMazeNotFound:
            return None
        return _parse_one(path, payload, _show)

    async def get_show_with_episodes(self, show_id: int) -> tuple[Show, list[Episode]]:
        path = f"/shows/{show_id}"
        payload = await self._get_json(path, {"embed[]": "episodes"})
        return _parse_one(path, payload, _show_with_episodes)

    async def schedule(self, day: date, *, country: str = "US") -> list[tuple[Show, Episode]]:
        """Every episode airing on `day` in `country`, each paired with its show."""
        path = "/schedule"
        payload = await self._get_json(path, {"country": country, "date": day.isoformat()})
        return _parse_items(path, payload, _schedule_entry)

    async def updated_shows(self, since: Literal["day", "week", "month"]) -> dict[int, int]:
        """Show id → unix time of its last change, for every show changed within `since`."""
        path = "/updates/shows"
        payload = await self._get_json(path, {"since": since})
        return _parse_one(path, payload, _UPDATES.validate_python)

    async def _get_json(self, path: str, params: Mapping[str, str] | None = None) -> object:
        """GET `path` and decode its JSON body.

        429 is retried up to 4 attempts in all, 5xx and transport errors up to 3; attempts are
        counted together and the limit of the latest failure applies. The semaphore is held only
        for the request itself, never while waiting to retry.
        """
        url = self.BASE_URL + path
        attempt = 0
        while True:
            attempt += 1
            cause: Exception | None = None
            try:
                async with self._slots:
                    response = await self._http.get(url, params=params)
            except httpx.TransportError as exc:  # connection failures and timeouts
                cause = exc
                reason = _describe(exc)
                max_attempts = _FAILED_ATTEMPTS
                delay = self._backoff(attempt)
            except httpx.RequestError as exc:  # e.g. an undecodable body; retrying won't help
                raise TVMazeError(f"TVMaze {path}: request failed ({_describe(exc)})") from exc
            else:
                status = response.status_code
                if response.is_success:
                    return _decode_json(path, response)
                if status == 404:
                    raise TVMazeNotFound(f"TVMaze {path}: not found (HTTP 404)")
                reason = f"HTTP {status}"
                if status == 429:
                    max_attempts = _RATE_LIMITED_ATTEMPTS
                    retry_after = _retry_after(response)
                    delay = self._backoff(attempt) if retry_after is None else retry_after
                elif status >= 500:
                    max_attempts = _FAILED_ATTEMPTS
                    delay = self._backoff(attempt)
                else:
                    raise TVMazeError(f"TVMaze {path}: {reason}")
            if attempt >= max_attempts:
                raise TVMazeError(
                    f"TVMaze {path}: {reason} (gave up after {attempt} attempts)"
                ) from cause
            log.warning(
                "TVMaze %s: %s; retrying in %.1f s (attempt %d of %d)",
                path, reason, delay, attempt + 1, max_attempts,
            )
            await asyncio.sleep(delay)

    def _backoff(self, attempt: int) -> float:
        """Exponential delay after the `attempt`-th (1-based) failed attempt."""
        return self._retry_delay * 2.0 ** (attempt - 1)


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


def _describe(exc: Exception) -> str:
    message = str(exc)
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _decode_json(path: str, response: httpx.Response) -> object:
    try:
        payload: object = response.json()
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError are both ValueErrors
        raise TVMazeError(f"TVMaze {path}: response is not valid JSON") from exc
    return payload


def _summarize(exc: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
        for error in exc.errors()
    )


def _parse_one(path: str, payload: object, convert: Callable[[object], T]) -> T:
    try:
        return convert(payload)
    except ValidationError as exc:
        raise TVMazeError(f"TVMaze {path}: unexpected payload ({_summarize(exc)})") from exc


def _parse_items(path: str, payload: object, convert: Callable[[object], T]) -> list[T]:
    """Convert list items one by one so a single malformed entry can't sink the rest."""
    if not isinstance(payload, list):
        raise TVMazeError(f"TVMaze {path}: expected a JSON list, got {type(payload).__name__}")
    parsed: list[T] = []
    for index, item in enumerate(payload):
        try:
            parsed.append(convert(item))
        except ValidationError as exc:
            label = _item_label(item, index)
            log.warning("TVMaze %s: skipping malformed %s: %s", path, label, _summarize(exc))
    return parsed


def _item_label(item: object, index: int) -> str:
    """Names the raw item by its ids when it carries them: 'item 3 (episode 12, show 34)'."""
    ids: list[str] = []
    if isinstance(item, dict):
        if (episode_id := item.get("id")) is not None:
            ids.append(f"episode {episode_id!r}")
        show = item.get("show")
        if isinstance(show, dict) and (show_id := show.get("id")) is not None:
            ids.append(f"show {show_id!r}")
    return f"item {index} ({', '.join(ids)})" if ids else f"item {index}"


def _show(raw: object) -> Show:
    return TVMazeShow.model_validate(raw).to_show()


def _show_with_episodes(raw: object) -> tuple[Show, list[Episode]]:
    wire = TVMazeShow.model_validate(raw)
    return wire.to_show(), wire.to_episodes()


def _search_hit(raw: object) -> tuple[Show, str | None]:
    wire = TVMazeSearchResult.model_validate(raw).show
    return wire.to_show(), wire.country


def _schedule_entry(raw: object) -> tuple[Show, Episode]:
    item = TVMazeScheduleItem.model_validate(raw)
    return item.show.to_show(), item.to_domain(item.show.id)
