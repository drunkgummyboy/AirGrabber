"""Import the shows a Sonarr follows (My shows → Import Shows → "From Sonarr").

One request: `GET <address>/api/v3/series` with the API key in the `X-Api-Key` header. The answer
becomes import lines for LibraryService.import_shows: "tvdb:<TheTVDB id> <title>" (TVMaze looks
the show up by that id, the title is the fallback), or the title alone.

The key is a secret: header only, never in a URL, a message or the log, never stored (the import
is a one-off). The address is never logged either, nor `str(httpx_exc)`; the request never
follows a redirect (it would carry the key elsewhere).
"""

from __future__ import annotations

import logging
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.i18n import N_, t

log = logging.getLogger(__name__)

TIMEOUT = httpx.Timeout(20.0, connect=5.0)
DEFAULT_ADDRESS = "http://localhost:8989"
NO_ADDRESS = N_("Enter Sonarr's address, such as http://localhost:8989.")
NO_KEY = N_("Enter Sonarr's API key (Sonarr › Settings › General › API Key).")
WRONG_KEY = N_("Sonarr didn't accept that API key.")
NOT_SONARR = N_("That address doesn't answer like Sonarr does.")
UNREACHABLE = N_("Couldn't reach Sonarr at that address.")


class SonarrError(Exception):
    """A Sonarr import that couldn't happen; its text is translated and names no address."""


class SonarrSeries(BaseModel):
    """One series of Sonarr's /api/v3/series (only what an import needs)."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)

    title: str
    tvdb_id: int = Field(default=0, alias="tvdbId")
    imdb_id: str = Field(default="", alias="imdbId")


def series_url(address: str) -> str:
    """"localhost:8989/" -> "http://localhost:8989/api/v3/series"."""
    text = address.strip().rstrip("/")
    if not text:
        raise SonarrError(t(NO_ADDRESS))
    if "://" not in text:
        text = "http://" + text
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise SonarrError(t(NO_ADDRESS))
    return f"{text}/api/v3/series"


def import_lines(series: list[SonarrSeries]) -> list[str]:
    """What LibraryService.import_shows gets: "tvdb:81189 Breaking Bad", or the title alone."""
    lines = []
    for one in series:
        title = " ".join(one.title.split())
        if one.tvdb_id > 0:
            lines.append(f"tvdb:{one.tvdb_id} {title}".strip())
        elif title:
            lines.append(title)
    return lines


class SonarrClient:
    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def series(self, address: str, api_key: str) -> list[SonarrSeries]:
        """Every series the Sonarr at `address` has. Raises SonarrError."""
        url = series_url(address)
        if not api_key.strip():
            raise SonarrError(t(NO_KEY))
        try:
            response = await self._http.get(
                url,
                headers={"X-Api-Key": api_key.strip(), "Accept": "application/json"},
                timeout=TIMEOUT,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            log.info("Sonarr couldn't be reached (%s)", type(exc).__name__)
            raise SonarrError(t(UNREACHABLE)) from None
        if response.status_code in (401, 403):
            raise SonarrError(t(WRONG_KEY))
        if response.status_code != 200:
            log.info("Sonarr answered %d", response.status_code)
            raise SonarrError(t(NOT_SONARR))
        try:
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("not a list")
            found = [SonarrSeries.model_validate(item) for item in payload]
        except (ValueError, ValidationError):
            raise SonarrError(t(NOT_SONARR)) from None
        log.info("Sonarr has %d series", len(found))
        return found

    async def import_lines(self, address: str, api_key: str) -> list[str]:
        """`series` as import lines."""
        return import_lines(await self.series(address, api_key))
