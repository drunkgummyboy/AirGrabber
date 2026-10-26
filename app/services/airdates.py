"""Importing the shows a user follows on airdates.tv.

airdates.tv keeps a user's shows as colours in the browser; its "Export your color selection"
link shows them as text: `1534=#ffcc00;5064=7fa2c4;...` (series id = colour). Each id is named
through the site's search (`/s?q=info:<id>`), then matched to TVMaze by LibraryService.

Nothing here logs request URLs or the pasted export text: only counts and outcomes.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from html.parser import HTMLParser

import httpx

from app.i18n import N_, t
from app.models import AirdatesPreview
from app.services.library import LibraryService

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.airdates.tv/s"
# Be polite: one request at a time, at least this many seconds apart.
MIN_INTERVAL = 0.3
TIMEOUT = 10.0
RETRY_DELAY = 1.0

EXPORT_ERROR = N_(  # raised through t()
    "That doesn't look like the text from airdates.tv's \"Export your color selection\"."
)

_PAIR = re.compile(r"(\d+)=#?[0-9A-Fa-f]{3,8}")
_SEPARATORS = re.compile(r"[;,\s]+")
_ALL_EPISODES = re.compile(r"\s*\(all episodes\)\s*$", re.IGNORECASE)


class AirdatesError(Exception):
    """An unusable export text, or airdates.tv could not be reached. Messages are user-safe."""


def parse_export(text: str) -> list[int]:
    """The airdates.tv series ids in an "Export your color selection" text, in order, once each.

    Accepts `id=colour` pairs (colour with or without '#') and bare ids, separated by ';', ','
    or whitespace. When the text holds `id=colour` pairs, bits that are no id are ignored; a
    text of bare ids must hold nothing else. Raises AirdatesError when no id is found.
    """
    tokens = [token for token in _SEPARATORS.split(re.sub(r"\s*=\s*", "=", text)) if token]
    ids: list[int] = []
    paired = stray = False
    for token in tokens:
        if (pair := _PAIR.fullmatch(token)) is not None:
            ids.append(int(pair[1]))
            paired = True
        elif token.isascii() and token.isdigit():
            ids.append(int(token))
        else:
            stray = True
    if stray and not paired:
        ids = []
    unique = [series_id for series_id in dict.fromkeys(ids) if series_id > 0]
    if not unique:
        raise AirdatesError(t(EXPORT_ERROR))
    return unique


def parse_show_name(fragment: str) -> str | None:
    """The show name in an airdates.tv `info:<id>` answer: its first bold text, entities decoded
    and " (all episodes)" dropped. None when the fragment names no show."""
    parser = _BoldText()
    parser.feed(fragment)
    parser.close()
    if parser.text is None:
        return None
    name = _ALL_EPISODES.sub("", " ".join(parser.text.split()))
    return name or None


class _BoldText(HTMLParser):
    """Collects the text of the first <b> element."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: str | None = None
        self._parts: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "b" and self.text is None and self._parts is None:
            self._parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "b" and self._parts is not None and self.text is None:
            self.text = "".join(self._parts)
            self._parts = None

    def handle_data(self, data: str) -> None:
        if self._parts is not None:
            self._parts.append(data)


class AirdatesClient:
    """Names airdates.tv series ids, one request at a time and MIN_INTERVAL apart.

    The shared `httpx.AsyncClient` (User-Agent) belongs to the caller. `sleep` and `clock` are
    injectable for tests.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http
        self._sleep = sleep
        self._clock = clock
        self._turn = asyncio.Lock()
        self._last_request: float | None = None

    async def show_name(self, series_id: int) -> str | None:
        """The name airdates.tv gives `series_id`, or None when it knows no such series.

        A network error or server failure is retried once; then AirdatesError is raised.
        """
        response = await self._fetch(series_id)
        return None if response is None else parse_show_name(response.text)

    async def _fetch(self, series_id: int) -> httpx.Response | None:
        for attempt in (1, 2):
            try:
                response = await self._request(series_id)
            except httpx.TransportError as exc:  # connection failures and timeouts
                reason = type(exc).__name__
            except httpx.RequestError as exc:
                raise AirdatesError(
                    t("airdates.tv lookup failed ({error})", error=type(exc).__name__)
                ) from exc
            else:
                status = response.status_code
                # airdates.tv answers an unknown id with a bare "Application error" page (500).
                if status in (404, 500):
                    return None
                if response.is_success:
                    return response
                reason = f"HTTP {status}"
                if status < 500:
                    raise AirdatesError(t("airdates.tv answered {reason}", reason=reason))
            if attempt == 1:
                log.warning("airdates.tv lookup failed (%s); retrying once", reason)
                await self._sleep(RETRY_DELAY)
        raise AirdatesError(t("airdates.tv could not be reached ({reason})", reason=reason))

    async def _request(self, series_id: int) -> httpx.Response:
        async with self._turn:
            if self._last_request is not None:
                wait = self._last_request + MIN_INTERVAL - self._clock()
                if wait > 0:
                    await self._sleep(wait)
            try:
                return await self._http.get(
                    SEARCH_URL, params={"q": f"info:{series_id}"}, timeout=TIMEOUT
                )
            finally:
                self._last_request = self._clock()


class AirdatesImportService:
    """Previews an airdates.tv export: names its ids, then matches them on TVMaze.

    Importing the chosen matches afterwards goes through `LibraryService.import_shows` with
    their TVMaze ids.
    """

    def __init__(self, client: AirdatesClient, library: LibraryService) -> None:
        self._client = client
        self._library = library

    async def find(
        self, export_text: str, progress: Callable[[int, int], None] | None = None
    ) -> AirdatesPreview:
        """Parse `export_text`, name its ids on airdates.tv and match the names on TVMaze.

        `progress(done, total)` counts both phases as one: total = 2 × the number of ids.
        Ids airdates.tv doesn't know land in `unknown_ids`; lookups that failed (airdates.tv or
        TVMaze) are counted in `failed` and left out of `matches`. Raises AirdatesError for an
        unusable export text.
        """
        ids = parse_export(export_text)
        total = 2 * len(ids)

        def report(done: int) -> None:
            if progress is not None:
                try:
                    progress(done, total)
                except Exception:
                    log.exception("Progress callback %r failed", progress)

        names: list[str] = []
        unknown: list[int] = []
        failed = 0
        for done, series_id in enumerate(ids, start=1):
            try:
                name = await self._client.show_name(series_id)
            except AirdatesError as exc:
                log.warning("airdates.tv lookup gave up: %s", exc)
                failed += 1
            else:
                if name is None:
                    unknown.append(series_id)
                else:
                    names.append(name)
            report(done)

        # Ids without a name to match (unknown, failed, a name already listed) are done.
        unique = list(dict.fromkeys(name.strip() for name in names if name.strip()))
        matched_from = total - len(unique)
        if matched_from > len(ids):
            report(matched_from)
        matches = await self._library.find_matches(
            unique, lambda done, _total: report(matched_from + done)
        )
        failed += sum(match.failed for match in matches)
        kept = tuple(match for match in matches if not match.failed)
        log.info(
            "airdates.tv export: %d ids, %d named, %d unknown, %d found on TVMaze, %d failed",
            len(ids), len(names), len(unknown), sum(match.found for match in kept), failed,
        )
        return AirdatesPreview(matches=kept, unknown_ids=tuple(unknown), failed=failed)
