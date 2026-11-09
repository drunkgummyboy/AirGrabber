"""Plex: what is in the user's Plex libraries is the only thing that counts as had ("have it")
everywhere AirGrabber asks whether an episode or a film is still wanted, and the automatic
downloads skip it. Without Plex connected (`plex_connected`: no token stored) AirGrabber shows
nothing of what you have or miss (owner's decision for 3.1.0): no ✓, no "missing", no counts.

Signing in uses Plex's PIN flow, never a password: `PlexClient.create_pin` asks plex.tv for a
short code (4 characters, not a "strong" one), the user enters it at plex.tv/link, and
`PlexService.wait_for_pin` asks plex.tv every PIN_POLL_S whether it was, until plex.tv hands
over the token or the code expires. Every request carries X-Plex-Client-Identifier (a random
UUID made once and kept in the settings, `plex_client_id`), X-Plex-Product, X-Plex-Version and
`Accept: application/json`.

Finding the server: plex.tv's resources (`includeHttps=1&includeRelay=0`) list the servers the
account owns. With one it is picked; with several the user picks one in Settings
(`choose_server`). Its connections are tried in `connection_order` (local https, the
plex.direct addresses, first; then the other local ones; then the remote ones) and the first
whose /identity names the server is kept (`plex_server_uri`); the next check tries it first and
the others only when it stops answering.

Reading (`check`): /library/sections lists the libraries; the show and film libraries count
unless switched off in Settings (`plex_sections_off`). Shows (`type=2&includeGuids=1`), their
episodes (`type=4`: the show's ratingKey, season and number) and films (`type=1`) are read
PAGE_SIZE at a time (X-Plex-Container-Start / -Size), so a big library works. `match_scan` maps
them to AirGrabber's ids: a Plex show is the TVMaze show of the shows cache with its TheTVDB id,
else its IMDb id (`parse_guid` reads Plex's new agents' tvdb:// / imdb:// / tmdb:// guids and the
old agents' com.plexapp.agents.thetvdb://12345?lang=en and the like); a show AirGrabber doesn't
know is counted only. A film is kept by its TMDB id and IMDb id. Each check replaces what is
stored in one transaction (PlexRepository.replace); Plex unreachable or the token refused
keeps the last list, and `status().problem` says why.

Checks run at start, every CHECK_INTERVAL_S (`run_periodic`) and on "↻ Check now"; one at a time
(a caller during a check waits for that one). After a check that changed what is stored, and
whenever Plex gets connected or disconnected, `on_change` runs (the pages reload: their have /
missing cues appear or vanish without a restart).

After MediaForge moves a file into a library folder, `scan_folders` asks the server to scan just
that folder (/library/sections/{key}/refresh?path=…), so it shows in Plex within minutes; the
folder as Windows sees it is found in a library's own folders by their shared ending
(`plex_folder`: the share video's folder TV/Show is /volume1/video/TV/Show). Nothing is asked when no
library holds it. app/services/plex_scans.py hears the moves.

Secrets: the token goes in the X-Plex-Token header only, never in a URL, log line, message or
exception text. Request URLs are never logged, no httpx exception (which may carry a URL) is
chained to or left as the context of a PlexError, and redirects are never followed (a header
would travel along). Logs name ids and counts only.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from pathlib import PurePosixPath, PureWindowsPath
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from app import APP_NAME, __version__
from app.database import PlexRepository
from app.models.plex import (
    LibraryKind,
    PlexConnection,
    PlexEpisode,
    PlexFilmFile,
    PlexItemReply,
    PlexLibrary,
    PlexPin,
    PlexPinReply,
    PlexProblem,
    PlexReply,
    PlexResourceReply,
    PlexScan,
    PlexServer,
    PlexTitle,
    PlexStatus,
)
from app.models import Settings
from app.services.settings import SettingsService

log = logging.getLogger(__name__)

PLEX_TV = "https://plex.tv"
LINK_URL = "https://plex.tv/link"  # where the user enters the code
PRODUCT = APP_NAME
PIN_POLL_S = 2.0  # how often plex.tv is asked whether the code was entered
PIN_LIFETIME = timedelta(minutes=15)  # when plex.tv's answer doesn't say
CHECK_INTERVAL_S = 1800.0  # 30 minutes between checks
PAGE_SIZE = 500  # items per library page
MAX_PAGES = 400  # 200,000 items: a stop for a server that keeps sending pages
IDENTITY_TIMEOUT = httpx.Timeout(5.0, connect=3.0)  # trying a connection
LIBRARY_TIMEOUT = httpx.Timeout(30.0, connect=5.0)  # a page of a big library
# The library types of /library/sections/{key}/all?type=…
TYPE_MOVIE = 1
TYPE_SHOW = 2
TYPE_EPISODE = 4

_RESOURCES = TypeAdapter(list[PlexResourceReply])
_SECTION_KEY = re.compile(r"\d{1,9}")
_ITEM_KEY = re.compile(r"\d{1,12}")  # a show's, an episode's or a film's ratingKey


class PlexError(Exception):
    """A Plex call that failed; its text is plain and holds no URL, address or token."""


class PlexUnreachable(PlexError):
    """plex.tv or the server didn't answer (or answered with a server error)."""


class PlexAuthError(PlexError):
    """The token was refused (401): connect again."""


class PlexPinExpired(PlexError):
    """plex.tv no longer knows the code."""


def _utc_now() -> datetime:
    return datetime.now(UTC)


def plex_connected(settings: Settings) -> bool:
    """Plex is connected: a token is stored (also while the server is briefly unreachable:
    then the last list read counts). Only then does AirGrabber show what you have and miss."""
    return bool(settings.plex_token.get_secret_value())


# -- guids -------------------------------------------------------------------------------------

# A guid's agent → the id it names. Plex's new agents write "tvdb://81189", "imdb://tt0903747",
# "tmdb://1396"; the old ones "com.plexapp.agents.thetvdb://81189?lang=en" (an episode's adds
# "/1/2"), "com.plexapp.agents.imdb://tt0903747?lang=en", "com.plexapp.agents.themoviedb://1396".
_AGENTS: dict[str, str] = {
    "tvdb": "tvdb",
    "thetvdb": "tvdb",
    "imdb": "imdb",
    "tmdb": "tmdb",
    "themoviedb": "tmdb",
    "com.plexapp.agents.thetvdb": "tvdb",
    "com.plexapp.agents.thetvdbdvdorder": "tvdb",
    "com.plexapp.agents.imdb": "imdb",
    "com.plexapp.agents.themoviedb": "tmdb",
    "com.plexapp.agents.tmdb": "tmdb",
}
_GUID = re.compile(r"(?P<agent>[A-Za-z0-9_.-]+)://(?P<value>[^/?#]+)")
_IMDB = re.compile(r"tt\d{5,10}")
# Hama (anime): "com.plexapp.agents.hama://tvdb-81189?lang=en" (also tvdb2- … tvdb6-).
_HAMA_TVDB = re.compile(r"tvdb\d?-(\d{1,9})")


def parse_guid(text: str) -> tuple[str, str] | None:
    """A guid's outside id: ("tvdb", "81189"), ("imdb", "tt0903747") or ("tmdb", "1396");
    None for Plex's own ("plex://show/…"), a local item's or one that doesn't read."""
    match = _GUID.match(text.strip())
    if match is None:
        return None
    agent, value = match["agent"].lower(), match["value"]
    if agent == "com.plexapp.agents.hama":
        hama = _HAMA_TVDB.fullmatch(value)
        return ("tvdb", str(int(hama[1]))) if hama else None
    kind = _AGENTS.get(agent)
    if kind is None:
        return None
    if kind == "imdb":
        return ("imdb", value) if _IMDB.fullmatch(value) else None
    return (kind, str(int(value))) if value.isdigit() and int(value) > 0 else None


def external_ids(item: PlexItemReply) -> dict[str, list[str]]:
    """The item's outside ids by kind ("tvdb", "imdb", "tmdb"), in the order Plex names them."""
    found: dict[str, list[str]] = {}
    for text in item.guid_texts():
        parsed = parse_guid(text)
        if parsed is not None and parsed[1] not in found.setdefault(parsed[0], []):
            found[parsed[0]].append(parsed[1])
    return found


def _first_int(values: Sequence[str]) -> int | None:
    return int(values[0]) if values else None


# A thumb the server names for an item: "/library/metadata/1001/thumb/1699999999".
_THUMB = re.compile(r"/library/metadata/\d+/(?:thumb|art|poster)(?:/\d+)?")
_RESOLUTIONS = {"4k": "4K", "2160": "4K", "1080": "1080p", "720": "720p", "576": "SD", "480": "SD", "sd": "SD"}


def resolution_name(value: str | None) -> str | None:
    """Plex's videoResolution ("1080", "4k", "sd") as the cards say it ("1080p", "4K", "SD")."""
    if not value:
        return None
    return _RESOLUTIONS.get(value.strip().lower())


POSTER_PREFIX = "plex:"  # a Library card's picture "url": "plex:/library/metadata/1001/thumb/…"
POSTER_SIZE = (400, 600)  # asked of the server: enough for the biggest card at 175 % scaling
PICTURE_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
FETCH_TIMEOUT = httpx.Timeout(60.0, connect=5.0)  # a film's file: a pause this long gives up
FETCH_CHUNK = 1024 * 1024
_PART_KEY = re.compile(r"/library/parts/\d{1,12}/\d{1,15}/[^?#\\]{1,200}")


def poster_url(title: PlexTitle) -> str | None:
    """The "url" a Library card loads its picture by (None: the title has none)."""
    return f"{POSTER_PREFIX}{title.thumb}" if title.thumb else None


def plex_title(
    item: PlexItemReply, kind: LibraryKind, show_id: int | None = None
) -> PlexTitle | None:
    """A show or film for the Library page; None without a rating key or a title."""
    if item.rating_key is None or not item.title.strip():
        return None
    ids = external_ids(item)
    thumb = item.thumb if item.thumb and _THUMB.fullmatch(item.thumb) else None
    added = (
        datetime.fromtimestamp(item.added_at, UTC)
        if item.added_at is not None and item.added_at > 0
        else None
    )
    return PlexTitle(
        kind=kind,
        key=item.rating_key,
        title=item.title.strip(),
        year=item.year if item.year and item.year > 0 else None,
        thumb=thumb,
        added_at=added,
        seasons=item.child_count if kind == "show" else None,
        episodes=item.leaf_count if kind == "show" else None,
        resolution=resolution_name(item.media[0].video_resolution) if item.media else None,
        show_id=show_id,
        tvdb_id=_first_int(ids.get("tvdb", [])),
        imdb_id=ids["imdb"][0] if ids.get("imdb") else None,
        tmdb_id=_first_int(ids.get("tmdb", [])),
    )


def match_scan(
    *,
    shows: Iterable[PlexItemReply],
    episodes: Iterable[PlexItemReply],
    films: Iterable[PlexItemReply],
    known: Iterable[tuple[int, int | None, str | None]],
    libraries: Sequence[PlexLibrary] = (),
) -> PlexScan:
    """Plex's items as AirGrabber's ids. `known`: the shows cache's (TVMaze id, TheTVDB id,
    IMDb id). A Plex show is the cached show with its TheTVDB id, else its IMDb id; one that
    matches none is counted (`unmatched_shows`), its episodes dropped. An episode needs its
    show, a season and a number. A film is kept with its TMDB and IMDb ids (one of them is
    enough); one with neither is counted (`unmatched_films`)."""
    by_tvdb: dict[int, int] = {}
    by_imdb: dict[str, int] = {}
    for known_id, tvdb_id, imdb_id in known:
        if tvdb_id is not None:
            by_tvdb.setdefault(tvdb_id, known_id)
        if imdb_id:
            by_imdb.setdefault(imdb_id, known_id)
    show_of: dict[str, int] = {}  # Plex ratingKey → TVMaze id
    titles: list[PlexTitle] = []
    read = unmatched = 0
    for item in shows:
        read += 1
        ids = external_ids(item)
        match = next((by_tvdb[int(v)] for v in ids.get("tvdb", ()) if int(v) in by_tvdb), None)
        if match is None:
            match = next((by_imdb[v] for v in ids.get("imdb", ()) if v in by_imdb), None)
        if (title := plex_title(item, "show", match)) is not None:
            titles.append(title)
        if match is None or item.rating_key is None:
            unmatched += 1
            continue
        show_of[item.rating_key] = match
    found: set[tuple[int, int, int]] = set()
    for episode in episodes:
        owner = show_of.get(episode.grandparent_rating_key or "")
        season, number = episode.parent_index, episode.index
        if owner is None or season is None or number is None or season < 0 or number < 0:
            continue
        found.add((owner, season, number))
    kept: set[tuple[int | None, str | None]] = set()
    no_ids = 0
    for film in films:
        if (title := plex_title(film, "movie")) is not None:
            titles.append(title)
        ids = external_ids(film)
        tmdb_id = _first_int(ids.get("tmdb", []))
        imdb_id = ids["imdb"][0] if ids.get("imdb") else None
        if tmdb_id is None and imdb_id is None:
            no_ids += 1
            continue
        kept.add((tmdb_id, imdb_id))
    return PlexScan(
        episodes=frozenset(found),
        films=frozenset(kept),
        shows=read,
        unmatched_shows=unmatched,
        unmatched_films=no_ids,
        libraries=tuple(libraries),
        titles=tuple(titles),
    )


# -- folders ------------------------------------------------------------------------------------


def _windows_parts(folder: str) -> list[str]:
    """A Windows folder's names, a network path's share first (its server left out)."""
    path = PureWindowsPath(folder)
    share = [path.drive.rstrip("\\").split("\\")[-1]] if path.drive.startswith("\\\\") else []
    return [name for name in [*share, *path.parts[1:]] if name]


def _server_parts(location: str) -> tuple[list[str], str]:
    """A library folder's names as the server writes them, and its separator."""
    if "\\" in location or re.match(r"^[A-Za-z]:", location):
        return _windows_parts(location), "\\"
    return [name for name in PurePosixPath(location).parts[1:] if name], "/"


def plex_folder(folder: str, locations: Sequence[tuple[str, str]]) -> tuple[str, str] | None:
    """Where `folder` (as Windows sees it) is on the Plex server: (library key, the server's
    path), found by the longest ending of a library's folder that `folder` holds in one piece;
    None when no library's folder fits, or two fit equally well with only their last name."""
    names = [name.casefold() for name in _windows_parts(folder)]
    best: list[tuple[int, str, str]] = []
    for key, location in locations:
        parts, separator = _server_parts(location)
        lowered = [name.casefold() for name in parts]
        for size in range(len(lowered), 0, -1):
            ending = lowered[-size:]
            found = next(
                (i for i in range(len(names) - size + 1) if names[i : i + size] == ending), None
            )
            if found is not None:
                rest = _windows_parts(folder)[found + size :]
                path = separator.join([location.rstrip("/\\"), *rest])
                best.append((size, key, path))
                break
    if not best:
        return None
    best.sort(key=lambda match: match[0], reverse=True)
    if best[0][0] == 1 and len(best) > 1 and best[1][0] == 1:
        return None  # only a last name in common, with more than one library: unsure
    _size, key, path = best[0]
    return key, path


def connection_order(server: PlexServer) -> list[PlexConnection]:
    """The order a server's connections are tried in: local https (the plex.direct
    addresses) first, then the other local ones, then the remote ones."""
    connections = list(server.connections)
    return (
        [c for c in connections if c.local and c.https]
        + [c for c in connections if c.local and not c.https]
        + [c for c in connections if not c.local]
    )


def library_kind(type_name: str) -> LibraryKind | None:
    """A /library/sections type that counts: "show" or "movie"."""
    if type_name == "show":
        return "show"
    if type_name == "movie":
        return "movie"
    return None


# -- the client --------------------------------------------------------------------------------


class PlexClient:
    """plex.tv and the Plex Media Server over the shared HTTP client. `client_id` returns the
    X-Plex-Client-Identifier (read on every call)."""

    def __init__(
        self,
        http: httpx.AsyncClient,
        *,
        client_id: Callable[[], str],
        version: str = __version__,
        base_url: str = PLEX_TV,
    ) -> None:
        self._http = http
        self._client_id = client_id
        self._version = version
        self._base = base_url.rstrip("/")

    def _headers(self, token: str | None, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "X-Plex-Client-Identifier": self._client_id(),
            "X-Plex-Product": PRODUCT,
            "X-Plex-Version": self._version,
        }
        if token:
            headers["X-Plex-Token"] = token
        if extra:
            headers.update(extra)
        return headers

    async def _request(
        self,
        method: str,
        url: str,
        *,
        what: str,
        token: str | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: httpx.Timeout | None = None,
        answer: bool = True,
    ) -> Any:
        """The JSON of a call (None without `answer`: a call that returns nothing). `what`
        names it in errors and logs (never the URL)."""
        error_name = ""
        response: httpx.Response | None = None
        try:
            response = await self._http.request(
                method,
                url,
                params=params,
                headers=self._headers(token, headers),
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            error_name = type(exc).__name__
        if response is None:
            log.info("Plex %s: no answer (%s)", what, error_name)
            raise PlexUnreachable(f"Plex didn't answer ({what})")
        status = response.status_code
        if status == 401:
            raise PlexAuthError(f"Plex refused the sign-in ({what})")
        if status == 404 and what == "pin":
            raise PlexPinExpired("Plex no longer knows the code")
        if status >= 500:
            log.info("Plex %s: HTTP %d", what, status)
            raise PlexUnreachable(f"Plex didn't answer ({what}, HTTP {status})")
        if status >= 300:
            log.info("Plex %s: HTTP %d", what, status)
            raise PlexError(f"Plex said no ({what}, HTTP {status})")
        if not answer:
            return None
        try:
            return response.json()
        except ValueError:
            pass
        raise PlexError(f"Plex's answer didn't read ({what})")

    # -- plex.tv ---------------------------------------------------------------------------

    async def create_pin(self, *, now: datetime | None = None) -> PlexPin:
        """A new short code for plex.tv/link."""
        data = await self._request(
            "POST", f"{self._base}/api/v2/pins", what="new pin", params={"strong": "false"}
        )
        reply = _read(PlexPinReply, data, "new pin")
        now = now if now is not None else _utc_now()
        if reply.expires_at is not None:
            expires = reply.expires_at
        elif reply.expires_in:
            expires = now + timedelta(seconds=reply.expires_in)
        else:
            expires = now + PIN_LIFETIME
        return PlexPin(id=reply.id, code=reply.code, expires_at=expires)

    async def check_pin(self, pin_id: int) -> str | None:
        """The token once the code was entered; None while it wasn't yet. PlexPinExpired once
        plex.tv no longer knows the code."""
        data = await self._request("GET", f"{self._base}/api/v2/pins/{int(pin_id)}", what="pin")
        token = _read(PlexPinReply, data, "pin").auth_token
        return token or None

    async def servers(self, token: str) -> list[PlexServer]:
        """The Plex Media Servers the account owns, with their connections (no relays)."""
        data = await self._request(
            "GET",
            f"{self._base}/api/v2/resources",
            what="resources",
            token=token,
            params={"includeHttps": 1, "includeRelay": 0},
        )
        try:
            resources = _RESOURCES.validate_python(data)
        except ValidationError:
            raise PlexError("Plex's list of servers didn't read") from None
        return [
            PlexServer(
                machine_id=resource.client_identifier,
                name=resource.name,
                connections=tuple(
                    PlexConnection(
                        uri=connection.uri.rstrip("/"),
                        local=connection.local,
                        https=connection.protocol.lower() == "https",
                    )
                    for connection in resource.connections
                    if not connection.relay and connection.uri.startswith(("http://", "https://"))
                ),
            )
            for resource in resources
            if resource.is_server and resource.owned
        ]

    # -- the server ------------------------------------------------------------------------

    async def identity(self, uri: str, token: str) -> str | None:
        """The server's machine identifier, as the server at `uri` names it."""
        data = await self._request(
            "GET", f"{uri}/identity", what="identity", token=token, timeout=IDENTITY_TIMEOUT
        )
        return _read(PlexReply, data, "identity").container.machine_identifier

    async def sections(self, uri: str, token: str) -> list[PlexLibrary]:
        """The server's show and film libraries (all on)."""
        data = await self._request(
            "GET", f"{uri}/library/sections", what="libraries", token=token
        )
        libraries: list[PlexLibrary] = []
        for directory in _read(PlexReply, data, "libraries").container.directories:
            kind = library_kind(directory.type)
            if kind is not None and _SECTION_KEY.fullmatch(directory.key):
                libraries.append(PlexLibrary(key=directory.key, title=directory.title, kind=kind))
        return libraries

    async def locations(self, uri: str, token: str) -> list[tuple[str, str]]:
        """The show and film libraries' folders as the server sees them: (library key, path)."""
        data = await self._request(
            "GET", f"{uri}/library/sections", what="libraries", token=token
        )
        return [
            (directory.key, location.path)
            for directory in _read(PlexReply, data, "libraries").container.directories
            if library_kind(directory.type) is not None and _SECTION_KEY.fullmatch(directory.key)
            for location in directory.locations
            if location.path
        ]

    async def scan(self, uri: str, token: str, section: str, path: str) -> None:
        """Ask the server to scan one folder of a library."""
        if not _SECTION_KEY.fullmatch(section):
            raise PlexError("A library key that doesn't read")
        await self._request(
            "GET",
            f"{uri}/library/sections/{section}/refresh",
            what="scan",
            token=token,
            params={"path": path},
            answer=False,
        )

    async def items(
        self, uri: str, token: str, section: str, item_type: int, *, guids: bool = False
    ) -> list[PlexItemReply]:
        """Every item of `item_type` in a library, PAGE_SIZE at a time."""
        if not _SECTION_KEY.fullmatch(section):
            raise PlexError("A library key that doesn't read")
        params: dict[str, Any] = {"type": item_type}
        if guids:
            params["includeGuids"] = 1
        found: list[PlexItemReply] = []
        start = 0
        for _ in range(MAX_PAGES):
            data = await self._request(
                "GET",
                f"{uri}/library/sections/{section}/all",
                what="library page",
                token=token,
                params=params,
                headers={
                    "X-Plex-Container-Start": str(start),
                    "X-Plex-Container-Size": str(PAGE_SIZE),
                },
                timeout=LIBRARY_TIMEOUT,
            )
            container = _read(PlexReply, data, "library page").container
            page = container.metadata
            found.extend(page)
            start += len(page)
            total = container.total_size
            if (
                not page
                or len(page) != PAGE_SIZE  # the last page, or a server that ignored the size
                or (total is not None and start >= total)
            ):
                break
        return found


    async def episodes(self, uri: str, token: str, show_key: str) -> list[PlexItemReply]:
        """Every episode of a show (its `allLeaves`), specials included."""
        if not _ITEM_KEY.fullmatch(show_key):
            raise PlexError("A show's key that doesn't read")
        data = await self._request(
            "GET",
            f"{uri}/library/metadata/{show_key}/allLeaves",
            what="episodes",
            token=token,
            timeout=LIBRARY_TIMEOUT,
        )
        return list(_read(PlexReply, data, "episodes").container.metadata)

    async def metadata(self, uri: str, token: str, key: str) -> PlexItemReply | None:
        """One title with its versions and their files; None when the server doesn't have it."""
        if not _ITEM_KEY.fullmatch(key):
            raise PlexError("A title's key that doesn't read")
        try:
            data = await self._request(
                "GET", f"{uri}/library/metadata/{key}", what="title", token=token,
                timeout=LIBRARY_TIMEOUT,
            )
        except PlexError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        found = _read(PlexReply, data, "title").container.metadata
        return found[0] if found else None

    async def fetch(
        self,
        uri: str,
        token: str,
        part_key: str,
        write: Callable[[bytes], Awaitable[None]],
    ) -> None:
        """A file the server hands out at `part_key`, piece by piece to `write`. Raises
        PlexUnreachable when it breaks off, PlexError when the server says no. The token in
        its header only; nothing of the address is logged."""
        if not _PART_KEY.fullmatch(part_key):
            raise PlexError("A file's key that doesn't read")
        try:
            async with self._http.stream(
                "GET",
                f"{uri}{part_key}",
                params={"download": 1},
                headers=self._headers(token, {"Accept": "*/*"}),
                timeout=FETCH_TIMEOUT,
                follow_redirects=False,
            ) as response:
                if response.status_code == 401:
                    raise PlexAuthError("Plex refused the sign-in (file)")
                if response.status_code != 200:
                    log.info("Plex file: HTTP %d", response.status_code)
                    raise PlexError(f"Plex said no (file, HTTP {response.status_code})")
                async for piece in response.aiter_bytes(FETCH_CHUNK):
                    await write(piece)
        except httpx.HTTPError as exc:
            log.info("Plex file: broke off (%s)", type(exc).__name__)
            raise PlexUnreachable("Plex stopped sending the file") from None

    async def picture(self, uri: str, token: str, thumb: str, width: int, height: int) -> bytes | None:
        """A title's picture from the server, resized there; None when it can't be had. The
        token in its header only; neither the address nor the answer is logged."""
        try:
            response = await self._http.get(
                f"{uri}/photo/:/transcode",
                params={
                    "url": thumb,
                    "width": width,
                    "height": height,
                    "minSize": 1,
                    "upscale": 1,
                },
                headers=self._headers(token, {"Accept": "image/*"}),
                timeout=PICTURE_TIMEOUT,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            log.debug("Plex picture: no answer (%s)", type(exc).__name__)
            return None
        if response.status_code != 200:
            log.debug("Plex picture: HTTP %d", response.status_code)
            return None
        return response.content


def _read[M: BaseModel](model: type[M], data: Any, what: str) -> M:
    try:
        return model.model_validate(data)
    except ValidationError:
        pass
    raise PlexError(f"Plex's answer didn't read ({what})")


# -- the service -------------------------------------------------------------------------------


class _NoServer(PlexError):
    def __init__(self, problem: PlexProblem) -> None:
        super().__init__(problem.value)
        self.problem = problem


StatusListener = Callable[[], None]


class PlexService:
    """Connecting to Plex, the checks and what the Settings box shows (see the module
    docstring)."""

    def __init__(
        self,
        client: PlexClient,
        repository: PlexRepository,
        settings: SettingsService,
        *,
        on_change: Callable[[], None] | None = None,
        clock: Callable[[], datetime] = _utc_now,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        """`on_change`: after a check changed what is stored, and when Plex gets connected or
        disconnected (the library's listeners: the pages reload, their have / missing cues
        following). `clock` / `sleep`: tests move them."""
        self._client = client
        self._repository = repository
        self._settings = settings
        self._on_change = on_change
        self._clock = clock
        self._sleep = sleep
        self._servers: tuple[PlexServer, ...] = ()
        self._problem: PlexProblem | None = None
        self._run: asyncio.Task[None] | None = None
        self._listeners: list[StatusListener] = []

    # -- listeners -----------------------------------------------------------------------------

    def subscribe(self, listener: StatusListener) -> Callable[[], None]:
        """Call `listener` whenever the status may have changed (a check starts or ends, a
        connect, a choice, Disconnect). Returns a function that unsubscribes it."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(listener)

        return unsubscribe

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:
                log.exception("Plex listener %r failed", listener)

    def _changed(self) -> None:
        if self._on_change is not None:
            try:
                self._on_change()
            except Exception:
                log.exception("Plex change listener failed")

    # -- state ---------------------------------------------------------------------------------

    def _token(self) -> str:
        return self._settings.current.plex_token.get_secret_value()

    @property
    def connected(self) -> bool:
        """A token is stored (`plex_connected`)."""
        return plex_connected(self._settings.current)

    @property
    def checking(self) -> bool:
        return self._run is not None and not self._run.done()

    async def status(self) -> PlexStatus:
        """What the Settings box shows."""
        current = self._settings.current
        off = set(current.plex_sections_off)
        libraries = tuple(
            library.model_copy(update={"on": library.key not in off})
            for library in await self._repository.libraries()
        )
        server = (
            PlexServer(machine_id=current.plex_server_id, name=current.plex_server_name)
            if current.plex_server_id
            else None
        )
        return PlexStatus(
            connected=self.connected,
            server=server,
            servers=self._servers,
            libraries=libraries,
            counts=await self._repository.counts(),
            problem=self._problem if self.connected else None,
            checking=self.checking,
        )

    async def _ensure_client_id(self) -> None:
        if not self._settings.current.plex_client_id:
            await self._settings.update(plex_client_id=str(uuid.uuid4()))

    # -- connecting ----------------------------------------------------------------------------

    async def new_pin(self) -> PlexPin:
        """A new code for plex.tv/link (the connect window). Raises PlexError."""
        await self._ensure_client_id()
        pin = await self._client.create_pin(now=self._clock())
        log.info("Plex: a new sign-in code, pin %d", pin.id)
        return pin

    async def wait_for_pin(self, pin: PlexPin) -> bool:
        """Ask plex.tv every PIN_POLL_S whether `pin`'s code was entered: True once it was
        (the token is stored, a server not chosen yet), False once the code expired. Cancel
        the task to stop waiting. plex.tv not answering for a moment is waited out."""
        while self._clock() < pin.expires_at:
            try:
                token = await self._client.check_pin(pin.id)
            except PlexPinExpired:
                break
            except PlexAuthError:
                break
            except PlexError as exc:
                log.info("Plex: asking about the code failed (%s), trying again", type(exc).__name__)
                token = None
            if token:
                await self._signed_in(token)
                return True
            await self._sleep(PIN_POLL_S)
        log.info("Plex: sign-in code %d expired", pin.id)
        return False

    async def _signed_in(self, token: str) -> None:
        # Signing in again (the token was refused) keeps the server chosen.
        kept = bool(self._settings.current.plex_server_id)
        newly = not self.connected
        await self._settings.update(plex_token=token)
        self._problem = None
        log.info("Plex: connected (server %s)", "kept" if kept else "not chosen yet")
        self._notify()
        if newly:
            self._changed()  # the pages show what you have and miss from now on

    async def choose_server(self, machine_id: str) -> None:
        """Use the owned server `machine_id` (Settings' dropdown), then check."""
        server = next((s for s in self._servers if s.machine_id == machine_id), None)
        if server is None:
            return
        await self._settings.update(
            plex_server_id=server.machine_id,
            plex_server_name=server.name,
            plex_server_uri="",
            plex_sections_off=(),
        )
        log.info("Plex: server chosen (%d connections)", len(server.connections))
        self._problem = None
        self._notify()
        await self.check(again=True)

    async def set_library(self, key: str, on: bool) -> None:
        """Count a library's titles or not (Settings' tick boxes), then check."""
        off = [k for k in self._settings.current.plex_sections_off if k != key]
        if not on:
            off.append(key)
        await self._settings.update(plex_sections_off=tuple(off))
        self._notify()
        await self.check(again=True)

    async def disconnect(self) -> None:
        """Forget the token, the server and everything read from Plex."""
        run = self._run
        if run is not None and not run.done():
            run.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await run
        await self._settings.update(
            plex_token="",
            plex_server_id="",
            plex_server_name="",
            plex_server_uri="",
            plex_sections_off=(),
        )
        await self._repository.clear()
        self._servers = ()
        self._problem = None
        log.info("Plex: disconnected, its lists forgotten")
        self._notify()
        self._changed()

    # -- checks --------------------------------------------------------------------------------

    async def run_periodic(self, interval_s: float = CHECK_INTERVAL_S) -> None:
        """Check now (when connected), then every `interval_s` seconds, until cancelled."""
        while True:
            if self.connected:
                try:
                    await self.check()
                except Exception:
                    log.exception("Plex check failed")
            await self._sleep(interval_s)

    async def check(self, *, again: bool = False) -> None:
        """Read Plex's libraries and store what matches (see the module docstring). One at a
        time: a call during a check waits for that one (`again`: then checks once more, for a
        change made meanwhile). Nothing without a token."""
        if not self.connected:
            return
        run = self._run
        if again and run is not None and not run.done():
            await asyncio.wait({run})
            run = self._run  # another caller may have started the next one meanwhile
        if run is None or run.done():
            run = asyncio.create_task(self._check(), name="plex-check")
            self._run = run
            self._notify()
        # Waits without cancelling the check when the caller is cancelled; a check cancelled by
        # Disconnect ends the wait quietly.
        await asyncio.wait({run})
        if not run.cancelled() and (error := run.exception()) is not None:
            raise error

    async def _check(self) -> None:
        token = self._token()
        try:
            uri = await self._reach(token)
            scan = await self._read(uri, token)
            before = await self._repository.contents()
            await self._repository.replace(scan, self._clock())
        except _NoServer as exc:
            self._problem = exc.problem
            log.info("Plex check: %s", exc.problem.value)
        except PlexAuthError:
            self._problem = PlexProblem.SIGNED_OUT
            log.warning("Plex check: the sign-in was refused, the last list stays")
        except PlexUnreachable:
            self._problem = PlexProblem.UNREACHABLE
            log.warning("Plex check: Plex didn't answer, the last list stays")
        except PlexError as exc:
            self._problem = PlexProblem.FAILED
            log.warning("Plex check failed (%s), the last list stays", type(exc).__name__)
        else:
            self._problem = None
            log.info(
                "Plex check: %d shows read (%d not known to AirGrabber), %d episodes, "
                "%d films (%d without an id)",
                scan.shows,
                scan.unmatched_shows,
                len(scan.episodes),
                len(scan.films),
                scan.unmatched_films,
            )
            if before != (scan.episodes, scan.films):
                self._changed()
        finally:
            self._run = None
            self._notify()

    async def titles(self) -> list[PlexTitle]:
        """The shows and films of the last check (the Library page); none without Plex."""
        if not self.connected:
            return []
        return await self._repository.titles()

    async def checked_at(self) -> datetime | None:
        """When the last check succeeded."""
        return (await self._repository.counts()).checked_at

    async def poster(self, url: str) -> bytes | None:
        """A Library card's picture: `url` is "plex:<the title's thumb path>" (the Plex
        posters' ImageCache hands it here). From the server the last check reached; None
        without one, for a path that isn't a title's picture, or when it can't be had."""
        thumb = url.removeprefix(POSTER_PREFIX)
        uri = self._settings.current.plex_server_uri
        if not url.startswith(POSTER_PREFIX) or not _THUMB.fullmatch(thumb) or not uri:
            return None
        if not self.connected:
            return None
        return await self._client.picture(uri, self._token(), thumb, *POSTER_SIZE)

    async def episodes(self, show: PlexTitle) -> list[PlexEpisode]:
        """A show's episodes as the Plex server has them now, by season and number, from the
        server the last check reached. Raises PlexError (PlexUnreachable when it didn't
        answer); none without Plex or a known server."""
        uri = self._settings.current.plex_server_uri
        if not self.connected or not uri or show.kind != "show":
            return []
        replies = await self._client.episodes(uri, self._token(), show.key)
        found = [
            PlexEpisode(
                key=reply.rating_key,
                season=reply.parent_index,
                episode=reply.index,
                title=reply.title.strip(),
            )
            for reply in replies
            if reply.rating_key is not None and _ITEM_KEY.fullmatch(reply.rating_key)
        ]
        found.sort(key=lambda one: (one.season is None, one.season or 0, one.episode or 0))
        log.info("Plex: %d episode(s) of a show read", len(found))
        return found

    async def film_file(self, key: str) -> PlexFilmFile | None:
        """A film's tallest version's file (its first part), from the server the last check
        reached; None when Plex isn't connected, the server doesn't have it, or it has no file
        to hand out. Raises PlexError (PlexUnreachable when it didn't answer)."""
        uri = self._settings.current.plex_server_uri
        if not self.connected or not uri:
            return None
        item = await self._client.metadata(uri, self._token(), key)
        if item is None:
            return None
        best: PlexFilmFile | None = None
        for media in item.media:
            part = media.parts[0] if media.parts else None
            if part is None or not part.key or not part.size:
                continue
            found = PlexFilmFile(
                part_key=part.key,
                size=part.size,
                height=media.height or 0,
                duration_s=(media.duration or 0) / 1000,
                container=part.container or "",
            )
            if best is None or found.height > best.height:
                best = found
        return best

    async def fetch_film(
        self, film: PlexFilmFile, write: Callable[[bytes], Awaitable[None]]
    ) -> None:
        """`film`'s file, piece by piece to `write` (see PlexClient.fetch)."""
        uri = self._settings.current.plex_server_uri
        if not self.connected or not uri:
            raise PlexUnreachable("Plex isn't connected")
        await self._client.fetch(uri, self._token(), film.part_key, write)

    async def scan_folders(self, folders: Iterable[str]) -> int:
        """Ask Plex to scan these folders (where MediaForge just moved files): how many scans
        were asked for. Only with Plex connected and its address known (a check found it);
        a folder no library holds is left alone. Never raises: a failure is logged (counts and
        the error's kind only)."""
        uri = self._settings.current.plex_server_uri
        wanted = sorted(set(folders))
        if not self.connected or not uri or not wanted:
            return 0
        token = self._token()
        try:
            locations = await self._client.locations(uri, token)
        except PlexError as exc:
            log.info("Plex scan: the libraries didn't read (%s)", type(exc).__name__)
            return 0
        scans = {found for folder in wanted if (found := plex_folder(folder, locations))}
        asked = 0
        for key, path in sorted(scans):
            try:
                await self._client.scan(uri, token, key, path)
            except PlexError as exc:
                log.info("Plex scan of library %s failed (%s)", key, type(exc).__name__)
                continue
            asked += 1
        log.info(
            "Plex scan: %d folder(s) asked for, %d of %d not in a library",
            asked,
            len(wanted) - len({folder for folder in wanted if plex_folder(folder, locations)}),
            len(wanted),
        )
        return asked

    async def _reach(self, token: str) -> str:
        """The address of the chosen server that answers (see the module docstring)."""
        current = self._settings.current
        servers: list[PlexServer] | None
        try:
            servers = await self._client.servers(token)
        except PlexUnreachable:
            servers = None  # plex.tv is away: the server's own address may still answer
        chosen = current.plex_server_id
        server: PlexServer | None = None
        if servers is not None:
            self._servers = tuple(servers)
            if not chosen:
                if not servers:
                    raise _NoServer(PlexProblem.NO_SERVER)
                if len(servers) > 1:
                    raise _NoServer(PlexProblem.CHOOSE_SERVER)
                chosen = servers[0].machine_id
            server = next((s for s in servers if s.machine_id == chosen), None)
            if server is None:
                raise _NoServer(PlexProblem.NO_SERVER)
            if (server.machine_id, server.name) != (
                current.plex_server_id,
                current.plex_server_name,
            ):
                await self._settings.update(
                    plex_server_id=server.machine_id, plex_server_name=server.name
                )
        elif not chosen:
            raise PlexUnreachable("plex.tv didn't answer")
        stored = current.plex_server_uri
        candidates = [stored] if stored else []
        if server is not None:
            candidates += [c.uri for c in connection_order(server) if c.uri != stored]
        for uri in candidates:
            if await self._answers(uri, chosen, token):
                if uri != stored:
                    await self._settings.update(plex_server_uri=uri)
                    log.info("Plex: the server answered on connection %d", candidates.index(uri) + 1)
                return uri
        raise PlexUnreachable("The server didn't answer")

    async def _answers(self, uri: str, machine_id: str, token: str) -> bool:
        try:
            found = await self._client.identity(uri, token)
        except PlexError:
            return False
        return found == machine_id

    async def _read(self, uri: str, token: str) -> PlexScan:
        off = set(self._settings.current.plex_sections_off)
        libraries = [
            library.model_copy(update={"on": library.key not in off})
            for library in await self._client.sections(uri, token)
        ]
        shows: list[PlexItemReply] = []
        episodes: list[PlexItemReply] = []
        films: list[PlexItemReply] = []
        for library in libraries:
            if not library.on:
                continue
            if library.kind == "show":
                shows += await self._client.items(uri, token, library.key, TYPE_SHOW, guids=True)
                episodes += await self._client.items(uri, token, library.key, TYPE_EPISODE)
            else:
                films += await self._client.items(uri, token, library.key, TYPE_MOVIE, guids=True)
        known = await self._repository.known_shows()
        # Stored with every library on: the tick boxes follow the settings.
        listed = [library.model_copy(update={"on": True}) for library in libraries]
        return match_scan(
            shows=shows, episodes=episodes, films=films, known=known, libraries=listed
        )
