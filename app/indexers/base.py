"""The indexer contract (Strategy pattern) and the HTTP plumbing every indexer shares.

Each indexer turns a `SearchRequest` into normalized `TorrentRelease`s. The shared
`httpx.AsyncClient` (timeouts, redirects) belongs to the caller: indexers only issue requests on it.

Secret hygiene: Prowlarr download links embed its API key and any URL may carry one, so error
messages and log records name the endpoint path only, never a URL, and no httpx or validation
exception is ever chained to (or left as the context of) an IndexerError.
"""

from __future__ import annotations

import asyncio
import logging
import math
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Annotated, ClassVar, TypeVar

import httpx
from pydantic import BaseModel, BeforeValidator, ConfigDict, ValidationError

from app.models.domain import SearchKind, SearchRequest, TorrentRelease

log = logging.getLogger(__name__)

T = TypeVar("T")

# Several indexers (or the CDNs in front of them) turn away non-browser agents.
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)

_RATE_LIMITED_ATTEMPTS = 4
# 5xx responses and transport errors (timeouts included).
FAILED_ATTEMPTS = 3


class IndexerError(Exception):
    """An indexer was unreachable, answered with an error status, or sent an unparseable payload."""


class WireModel(BaseModel):
    """Base for the private schemas indexers validate raw payload items with."""

    model_config = ConfigDict(frozen=True, extra="ignore")


def _lenient_int(value: object) -> int | None:
    """Indexers send numbers as ints, numeric strings, '' or None; anything unparseable is None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except ValueError:
        return None


LenientInt = Annotated[int | None, BeforeValidator(_lenient_int)]


def from_unix(value: int | None) -> datetime | None:
    """A UTC datetime from unix seconds; None for missing, zero or out-of-range values."""
    if value is None or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, UTC)
    except (OverflowError, OSError, ValueError):
        return None


def http_url(value: str | None) -> str | None:
    """`value` when it is an http(s) URL, else None."""
    if value and value.strip().lower().startswith(("http://", "https://")):
        return value.strip()
    return None


def magnet_link(value: str | None) -> str | None:
    """`value` when it is a magnet link, else None."""
    if value and value.strip().lower().startswith("magnet:"):
        return value.strip()
    return None


def text_queries(request: SearchRequest) -> list[str]:
    """The request's free-text queries, stripped, without blanks or repeats, best first."""
    seen: set[str] = set()
    queries: list[str] = []
    for query in request.queries:
        text = " ".join(query.split())
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            queries.append(text)
    return queries


class BaseIndexer(ABC):
    """One torrent source. Subclasses set the class attributes and implement `search`."""

    name: ClassVar[str]  # stable id: "apibay", "eztv", "bitsearch", "yts", "prowlarr"
    label: ClassVar[str]  # short UI label: "APIBay", "EZTV", "Bitsearch", "YTS", "Prowlarr"
    kinds: ClassVar[frozenset[SearchKind]]  # request kinds it can answer

    def __init__(
        self, http: httpx.AsyncClient, *, max_concurrent: int = 2, retry_delay: float = 1.0
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        self._http = http
        self._slots = asyncio.Semaphore(max_concurrent)
        self._retry_delay = retry_delay

    def supports(self, request: SearchRequest) -> bool:
        """Whether `search` can answer `request`; `search` returns [] for anything else."""
        return request.kind in self.kinds

    @abstractmethod
    async def search(self, request: SearchRequest) -> list[TorrentRelease]:
        """Releases for `request` (never with 0 seeders); raises IndexerError on failure."""

    def web_search_url(self, query: str) -> str | None:
        """The indexer's own search page for `query`, for "open in browser"; None if it has none."""
        return None

    async def _first_hit(
        self, queries: Sequence[str], search: Callable[[str], Awaitable[list[TorrentRelease]]]
    ) -> list[TorrentRelease]:
        """Run `search` for each query in order and return the first non-empty result."""
        for query in queries:
            if releases := await search(query):
                return releases
        return []

    async def _get_json(
        self,
        base: str,
        path: str,
        params: Mapping[str, str] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        failed_attempts: int = FAILED_ATTEMPTS,
    ) -> object:
        """GET `base + path` and decode its JSON body.

        429 is retried up to 4 attempts in all, 5xx and transport errors up to `failed_attempts`;
        attempts are counted together and the limit of the latest failure applies. The semaphore
        is held only for the request itself, never while waiting to retry. Every raise sits
        outside the `except` blocks so no httpx exception (which may carry a URL) becomes its
        context.
        """
        url = base + path
        request_headers = {"User-Agent": USER_AGENT, **(headers or {})}
        attempt = 0
        while True:
            attempt += 1
            response: httpx.Response | None = None
            error_name = ""
            retryable = True
            try:
                async with self._slots:
                    response = await self._http.get(url, params=params, headers=request_headers)
            except httpx.UnsupportedProtocol as exc:  # e.g. no http(s)://; retrying won't help
                error_name = type(exc).__name__
                retryable = False
            except httpx.TransportError as exc:  # connection failures and timeouts
                error_name = type(exc).__name__
            except (httpx.RequestError, httpx.InvalidURL) as exc:  # e.g. an undecodable body
                error_name = type(exc).__name__
                retryable = False
            if response is None:
                if not retryable:
                    raise IndexerError(f"{self.label}: request to {path} failed ({error_name})")
                reason = f"no response from {path} ({error_name})"
                max_attempts = failed_attempts
                delay = self._backoff(attempt)
            else:
                status = response.status_code
                if response.is_success:
                    return self._decode_json(path, response)
                reason = f"{path} answered HTTP {status}"
                if status == 429:
                    max_attempts = _RATE_LIMITED_ATTEMPTS
                    retry_after = _retry_after(response)
                    delay = self._backoff(attempt) if retry_after is None else retry_after
                elif status >= 500:
                    max_attempts = failed_attempts
                    delay = self._backoff(attempt)
                else:
                    raise self._status_error(path, status)
            if attempt >= max_attempts:
                attempts = f"{attempt} attempt{'' if attempt == 1 else 's'}"
                raise IndexerError(f"{self.label}: {reason}; gave up after {attempts}")
            log.warning(
                "%s: %s; retrying in %.1f s (attempt %d of %d)",
                self.label, reason, delay, attempt + 1, max_attempts,
            )
            await asyncio.sleep(delay)

    def _status_error(self, path: str, status: int) -> IndexerError:
        """The error for a 4xx answer other than 429 (which is never retried)."""
        return IndexerError(f"{self.label}: {path} answered HTTP {status}")

    def _backoff(self, attempt: int) -> float:
        """Exponential delay after the `attempt`-th (1-based) failed attempt."""
        return self._retry_delay * 2.0 ** (attempt - 1)

    def _decode_json(self, path: str, response: httpx.Response) -> object:
        try:
            return response.json()
        except ValueError:  # JSONDecodeError and UnicodeDecodeError are both ValueErrors
            pass
        # Raised out here so the decode error (which holds the whole body) isn't its context.
        raise IndexerError(f"{self.label}: {path} did not answer with valid JSON")

    def _unexpected(self, path: str, detail: str) -> IndexerError:
        return IndexerError(f"{self.label}: unexpected answer from {path} ({detail})")

    def _parse_items(
        self, path: str, items: object, convert: Callable[[object], T | None]
    ) -> list[T]:
        """Convert list items one by one so a single malformed entry can't sink the rest.

        `convert` returns None for items it deliberately leaves out and raises ValueError
        (ValidationError included) for malformed ones, which are skipped with a warning.
        """
        if not isinstance(items, list):
            raise self._unexpected(path, f"expected a list, got {type(items).__name__}")
        parsed: list[T] = []
        for index, item in enumerate(items):
            try:
                converted = convert(item)
            except ValueError as exc:
                problem = _summarize(exc)
            else:
                if converted is not None:
                    parsed.append(converted)
                continue
            log.warning(
                "%s: skipping malformed item %d from %s: %s", self.label, index, path, problem
            )
        return parsed

    def _parse_releases(
        self, path: str, items: object, convert: Callable[[object], TorrentRelease | None]
    ) -> list[TorrentRelease]:
        """`_parse_items` for releases, dropping the ones nobody seeds."""
        releases = self._parse_items(path, items, convert)
        return [release for release in releases if release.seeders > 0]


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


def _summarize(exc: ValueError) -> str:
    """Field locations and messages only: a ValidationError's str() would quote the input."""
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
            for error in exc.errors()
        )
    return f"{type(exc).__name__}: {exc}"
