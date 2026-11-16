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
whose /identity names the server is kept (`plex_server_uri`). The next check asks that address
and plex.tv at the same time; only when it doesn't answer do the others race (all asked at once,
the first to answer wins; of several answering together, the one first in that order).

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

▶ Play (app/services/playback.py) asks which file a title is: a film's /library/metadata/{key}
with its markers (`film_video`: the tallest version, then the biggest, its first part), a show's
/library/metadata/{key}/allLeaves (`show_videos`: each episode's file and whether it was
watched), and the libraries' folders (`locations`, kept from the last check) to find that file
from this PC. Each video carries where Plex says it was left (viewOffset) and its length.
AirGrabber's own player asks for an episode's intro / credits markers (`markers`:
…?includeMarkers=1), reports where it is playing to /:/timeline (`report`: playing / paused /
stopped) and marks it watched with /:/scrobble (`mark_watched`). The Library page's Continue
watching row reads Plex's /hubs/continueWatching (a server without it: /library/onDeck), at most
CONTINUE_SIZE episodes and films in Plex's order (`continue_watching`, `continue_item`).

Calls a person waits on (`film_video`, `show_videos`, `episodes`, `markers`,
`continue_watching`, `locations` when not kept) take SCREEN_TIMEOUT instead of the check's LIBRARY_TIMEOUT, fail at once while Plex
was found away less than FAIL_FAST_S ago, and after a call Plex didn't answer look for the
server once (`_reach` within SCREEN_REACH_S; asked again only at a new address) before saying
Plex is away (`_on_screen`).

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
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from pathlib import PurePosixPath, PureWindowsPath
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from app import APP_NAME, __version__
from app.database import PlexRepository
from app.models.plex import (
    LibraryKind,
    PlexConnection,
    PlexContinue,
    PlexEpisode,
    PlexFilmFile,
    PlexItemReply,
    PlexLibrary,
    PlexMediaReply,
    PlexPartReply,
    PlexPin,
    PlexPinReply,
    PlexProblem,
    PlexReply,
    PlexResourceReply,
    PlexScan,
    PlexServer,
    PlexTitle,
    PlexStatus,
    PlexVideo,
)
from app.models import Settings
from app.models.domain import TMDB_IMAGE_BASE
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
# What a person waits on (▶ Play's film, the episode pickers, the player's markers): an answer
# within this, or Plex is said to be away.
SCREEN_TIMEOUT = httpx.Timeout(8.0, connect=3.0)
SCREEN_REACH_S = 2.0  # after a failed screen call: the server found again by then, or given up
FAIL_FAST_S = 60.0  # Plex found unreachable this recently: a screen call says so at once
REPORT_TIMEOUT = httpx.Timeout(5.0, connect=3.0)  # a position report or the watched mark
CONTINUE_SIZE = 12  # the Library page's Continue watching: at most this many, in Plex's order
LIBRARY_IDENTIFIER = "com.plexapp.plugins.library"  # /:/scrobble's identifier
# The library types of /library/sections/{key}/all?type=…
TYPE_MOVIE = 1
TYPE_SHOW = 2
TYPE_EPISODE = 4

_RESOURCES = TypeAdapter(list[PlexResourceReply])
_SECTION_KEY = re.compile(r"\d{1,9}")
_ITEM_KEY = re.compile(r"\d{1,12}")  # a show's, an episode's or a film's ratingKey

PlayState = Literal["playing", "paused", "stopped"]  # /:/timeline's state
Markers = tuple[tuple[int, int] | None, int | None]  # the intro's (start, end), the credits' start


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
# A folder library title's key (app/services/folder_library/match.py's `title_key`); its `thumb`
# is TMDB's poster path ("/abc.jpg"), shown in this TMDB size.
FOLDER_KEY_PREFIX = "folder:"
FOLDER_POSTER_SIZE = "w342"
POSTER_SIZE = (400, 600)  # asked of the server: enough for the biggest card at 175 % scaling
PICTURE_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
FETCH_TIMEOUT = httpx.Timeout(60.0, connect=5.0)  # a film's file: a pause this long gives up
FETCH_CHUNK = 1024 * 1024
_PART_KEY = re.compile(r"/library/parts/\d{1,12}/\d{1,15}/[^?#\\]{1,200}")


def poster_url(title: PlexTitle) -> str | None:
    """The "url" a Library card loads its picture by (None: the title has none): Plex's
    "plex:<thumb>", a folder library title's TMDB poster (no Plex, no token)."""
    if title.key.startswith(FOLDER_KEY_PREFIX):
        thumb = title.thumb
        if not thumb or thumb[:1] != "/":
            return None
        return f"{TMDB_IMAGE_BASE}{FOLDER_POSTER_SIZE}{thumb}"
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

    async def libraries(
        self, uri: str, token: str, *, timeout: httpx.Timeout | None = None
    ) -> tuple[list[PlexLibrary], list[tuple[str, str]]]:
        """One /library/sections: the server's show and film libraries (all on) and their
        folders as the server sees them, (library key, path)."""
        data = await self._request(
            "GET", f"{uri}/library/sections", what="libraries", token=token, timeout=timeout
        )
        libraries: list[PlexLibrary] = []
        folders: list[tuple[str, str]] = []
        for directory in _read(PlexReply, data, "libraries").container.directories:
            kind = library_kind(directory.type)
            if kind is None or not _SECTION_KEY.fullmatch(directory.key):
                continue
            libraries.append(PlexLibrary(key=directory.key, title=directory.title, kind=kind))
            folders += [(directory.key, where.path) for where in directory.locations if where.path]
        return libraries, folders

    async def sections(self, uri: str, token: str) -> list[PlexLibrary]:
        """The server's show and film libraries (all on)."""
        return (await self.libraries(uri, token))[0]

    async def locations(
        self, uri: str, token: str, *, timeout: httpx.Timeout | None = None
    ) -> list[tuple[str, str]]:
        """The show and film libraries' folders as the server sees them: (library key, path)."""
        return (await self.libraries(uri, token, timeout=timeout))[1]

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


    async def episodes(
        self, uri: str, token: str, show_key: str, *, timeout: httpx.Timeout = LIBRARY_TIMEOUT
    ) -> list[PlexItemReply]:
        """Every episode of a show (its `allLeaves`), specials included."""
        if not _ITEM_KEY.fullmatch(show_key):
            raise PlexError("A show's key that doesn't read")
        data = await self._request(
            "GET",
            f"{uri}/library/metadata/{show_key}/allLeaves",
            what="episodes",
            token=token,
            timeout=timeout,
        )
        return list(_read(PlexReply, data, "episodes").container.metadata)

    async def metadata(
        self,
        uri: str,
        token: str,
        key: str,
        *,
        markers: bool = False,
        timeout: httpx.Timeout = LIBRARY_TIMEOUT,
    ) -> PlexItemReply | None:
        """One title with its versions and their files (and its intro / credits markers with
        `markers`); None when the server doesn't have it."""
        if not _ITEM_KEY.fullmatch(key):
            raise PlexError("A title's key that doesn't read")
        try:
            data = await self._request(
                "GET", f"{uri}/library/metadata/{key}", what="title", token=token,
                params={"includeMarkers": 1} if markers else None,
                timeout=timeout,
            )
        except PlexError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise
        found = _read(PlexReply, data, "title").container.metadata
        return found[0] if found else None

    async def continue_watching(
        self, uri: str, token: str, *, timeout: httpx.Timeout = LIBRARY_TIMEOUT
    ) -> list[PlexItemReply]:
        """What Plex says to go on with: its Continue Watching hub (/hubs/continueWatching),
        else, on a server without it (404), its On Deck (/library/onDeck); at most
        CONTINUE_SIZE asked."""
        size = {"X-Plex-Container-Start": "0", "X-Plex-Container-Size": str(CONTINUE_SIZE)}
        try:
            data = await self._request(
                "GET", f"{uri}/hubs/continueWatching", what="continue watching", token=token,
                headers=size, timeout=timeout,
            )
        except PlexError as exc:
            if type(exc) is not PlexError or "HTTP 404" not in str(exc):
                raise
            log.info("Plex continue watching: no such hub, On Deck instead")
            data = await self._request(
                "GET", f"{uri}/library/onDeck", what="on deck", token=token, headers=size,
                timeout=timeout,
            )
            return list(_read(PlexReply, data, "on deck").container.metadata)
        container = _read(PlexReply, data, "continue watching").container
        hub = next(
            (one for one in container.hubs if "continue" in one.hub_identifier.casefold()),
            container.hubs[0] if container.hubs else None,
        )
        return list(hub.metadata if hub is not None else container.metadata)

    async def timeline(
        self, uri: str, token: str, key: str, state: PlayState, time_ms: int, duration_ms: int
    ) -> None:
        """Tell the server where a title is being played (Continue watching on every device)."""
        if not _ITEM_KEY.fullmatch(key):
            raise PlexError("A title's key that doesn't read")
        await self._request(
            "GET",
            f"{uri}/:/timeline",
            what="timeline",
            token=token,
            params={
                "ratingKey": key,
                "key": f"/library/metadata/{key}",
                "state": state,
                "time": max(0, int(time_ms)),
                "duration": max(0, int(duration_ms)),
            },
            timeout=REPORT_TIMEOUT,
            answer=False,
        )

    async def scrobble(self, uri: str, token: str, key: str) -> None:
        """Mark a title watched."""
        if not _ITEM_KEY.fullmatch(key):
            raise PlexError("A title's key that doesn't read")
        await self._request(
            "GET",
            f"{uri}/:/scrobble",
            what="scrobble",
            token=token,
            params={"identifier": LIBRARY_IDENTIFIER, "key": key},
            timeout=REPORT_TIMEOUT,
            answer=False,
        )

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


def episode_order(video: PlexVideo) -> tuple[bool, int, bool, int]:
    """Seasons in order with the specials (season 0, or none) last; within one, by number."""
    special = not video.season
    return (special, video.season or 0, video.episode is None, video.episode or 0)


def item_markers(item: PlexItemReply) -> Markers:
    """An item's intro (start, end) and the earliest credits marker's start, from its `Marker`
    list; None for one Plex doesn't have (or that doesn't read). A credits marker before half
    the item's length (opening credits, or a wrong one) is passed over, and so is every one
    while that length is unknown: the next episode countdown starts there."""
    intro: tuple[int, int] | None = None
    credits_ms: int | None = None
    half = (item.duration or 0) / 2
    for marker in item.markers:
        start, end = marker.start_ms, marker.end_ms
        if start is None or start < 0:
            continue
        if marker.type == "intro" and intro is None and end is not None and end > start:
            intro = (start, end)
        elif (
            marker.type == "credits"
            and half > 0
            and start >= half
            and (credits_ms is None or start < credits_ms)
        ):
            credits_ms = start
    return intro, credits_ms


def version_rank(height: int, parts: int, size: int) -> tuple[int, ...]:
    """▶ Play's order of a film's copies: the tallest picture, then a copy in one file over one
    in several parts, then the biggest (all its parts). Plex's versions (`play_rank`) and the
    folder library's copies (app/services/folder_library/playing.py) both go by it."""
    return (height, int(parts == 1), size)


def play_rank(media: PlexMediaReply) -> tuple[int, ...]:
    """▶ Play's order of a film's versions (`version_rank`)."""
    return version_rank(
        media.height or 0, len(media.parts), sum(one.size or 0 for one in media.parts)
    )


def height_rank(media: PlexMediaReply) -> tuple[int, ...]:
    """The tallest picture only: of equally tall versions, Plex's first (phone-sized copies)."""
    return (media.height or 0,)


def best_version(
    item: PlexItemReply,
    usable: Callable[[PlexPartReply], bool],
    rank: Callable[[PlexMediaReply], tuple[int, ...]] = play_rank,
) -> tuple[PlexMediaReply, PlexPartReply] | None:
    """The version of a film to play or copy: of those whose first part is `usable`, the
    highest by `rank` (of equal ones, the first); None when none is."""
    best: tuple[tuple[int, ...], PlexMediaReply, PlexPartReply] | None = None
    for media in item.media:
        part = media.parts[0] if media.parts else None
        if part is None or not usable(part):
            continue
        ranked = rank(media)
        if best is None or ranked > best[0]:
            best = (ranked, media, part)
    return None if best is None else (best[1], best[2])


def _video(
    item: PlexItemReply,
    key: str,
    file: str,
    *,
    episode: bool = False,
    markers_known: bool = False,
) -> PlexVideo:
    """▶ Play's video of a film's or (`episode`) an episode's reply (`markers_known`: asked
    with includeMarkers)."""
    intro, credits_ms = item_markers(item)
    return PlexVideo(
        key=key,
        file=file,
        season=item.parent_index if episode else None,
        episode=item.index if episode else None,
        title=item.title.strip(),
        watched=bool(item.view_count),
        view_offset_ms=max(0, item.view_offset or 0),
        duration_ms=max(0, item.duration or 0),
        intro=intro,
        credits_ms=credits_ms,
        markers_known=markers_known,
    )


def continue_keys(items: Iterable[PlexItemReply]) -> set[tuple[str, str]]:
    """The (kind, key) of the show or film each of Continue watching's items belongs to: what
    `continue_item` looks up among the stored titles."""
    keys: set[tuple[str, str]] = set()
    for item in items:
        if item.type == "movie" and item.rating_key and _ITEM_KEY.fullmatch(item.rating_key):
            keys.add(("movie", item.rating_key))
        elif item.type == "episode":
            show = item.grandparent_rating_key
            if show and _ITEM_KEY.fullmatch(show):
                keys.add(("show", show))
    return keys


def continue_item(
    item: PlexItemReply, stored: Mapping[tuple[str, str], PlexTitle]
) -> PlexContinue | None:
    """One of Continue watching's items as the Library page's card: an episode with its show,
    a film with the version ▶ Play picks (`best_version`, `play_rank`); the show or film from
    `stored` (the last check's, by (kind, key)) when it knows it. None for anything else (a
    clip, a track) or an item without a key that reads."""
    key = item.rating_key
    if key is None or not _ITEM_KEY.fullmatch(key):
        return None
    if item.type == "movie":
        title = stored.get(("movie", key)) or plex_title(item, "movie")
        best = best_version(item, lambda part: bool(part.file))
        file = (best[1].file or "") if best is not None else ""
        if title is None:
            return None
        return PlexContinue(title=title, video=_video(item, key, file))
    if item.type != "episode":
        return None
    show_key = item.grandparent_rating_key
    if show_key is None or not _ITEM_KEY.fullmatch(show_key):
        return None
    show = stored.get(("show", show_key))
    if show is None:
        name = item.grandparent_title.strip()
        if not name:
            return None
        thumb = item.grandparent_thumb
        show = PlexTitle(
            kind="show",
            key=show_key,
            title=name,
            thumb=thumb if thumb and _THUMB.fullmatch(thumb) else None,
        )
    media = item.media[0] if item.media else None
    part = media.parts[0] if media is not None and media.parts else None
    file = (part.file or "") if part is not None else ""
    return PlexContinue(title=show, video=_video(item, key, file, episode=True))


def _answered(task: asyncio.Task[bool] | None) -> bool:
    """A connection try that is done and found the server."""
    return task is not None and task.done() and not task.cancelled() and task.result()


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
        self._unreachable_at: datetime | None = None  # when Plex was last found away
        self._locations: list[tuple[str, str]] | None = None  # the libraries' folders, kept
        self._later: asyncio.Task[None] | None = None  # `check_later`'s
        self._behind: set[asyncio.Task[None]] = set()  # plex.tv's answer after a quick reach

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
        self._stop_behind()  # an older server's list must not land on this one
        await self._settings.update(
            plex_server_id=server.machine_id,
            plex_server_name=server.name,
            plex_server_uri="",
            plex_sections_off=(),
        )
        log.info("Plex: server chosen (%d connections)", len(server.connections))
        self._problem = None
        self._unreachable_at = None
        self._locations = None
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
        self._stop_behind()
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
        self._unreachable_at = None
        self._locations = None
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

    def check_later(self, delay_s: float) -> None:
        """One check about `delay_s` seconds from now (after a scan: the Library page and the
        marks catch up with what Plex just took in). One waiting at a time: asking again
        meanwhile changes nothing."""
        if self._later is not None and not self._later.done():
            return
        self._later = asyncio.get_running_loop().create_task(
            self._check_after(delay_s), name="plex-check-later"
        )

    async def _check_after(self, delay_s: float) -> None:
        await self._sleep(delay_s)
        try:
            await self.check(again=True)
        except Exception:
            log.exception("Plex check failed")

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
            self._unreachable_at = self._clock()
            log.warning("Plex check: Plex didn't answer, the last list stays")
        except PlexError as exc:
            self._problem = PlexProblem.FAILED
            log.warning("Plex check failed (%s), the last list stays", type(exc).__name__)
        else:
            self._problem = None
            self._unreachable_at = None
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
        replies = await self._on_screen(
            "episodes",
            lambda at, token: self._client.episodes(at, token, show.key, timeout=SCREEN_TIMEOUT),
        )
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
        """A film's tallest version's file (its first part; of equally tall ones Plex's first:
        `best_version` with `height_rank`), from the server the last check
        reached; None when Plex isn't connected, the server doesn't have it, or it has no file
        to hand out. Raises PlexError (PlexUnreachable when it didn't answer)."""
        uri = self._settings.current.plex_server_uri
        if not self.connected or not uri:
            return None
        item = await self._client.metadata(uri, self._token(), key)
        if item is None:
            return None
        best = best_version(item, lambda part: bool(part.key and part.size), height_rank)
        if best is None:
            return None
        media, part = best
        return PlexFilmFile(
            part_key=part.key or "",
            size=part.size or 0,
            height=media.height or 0,
            duration_s=(media.duration or 0) / 1000,
            container=part.container or "",
        )

    async def fetch_film(
        self, film: PlexFilmFile, write: Callable[[bytes], Awaitable[None]]
    ) -> None:
        """`film`'s file, piece by piece to `write` (see PlexClient.fetch)."""
        uri = self._settings.current.plex_server_uri
        if not self.connected or not uri:
            raise PlexUnreachable("Plex isn't connected")
        await self._client.fetch(uri, self._token(), film.part_key, write)

    # -- ▶ Play (app/services/playback.py) ------------------------------------------------------

    async def film_video(self, key: str) -> PlexVideo | None:
        """A film's file as the server sees it, with its intro / credits markers (asked at
        once: the player needn't ask again): the version `best_version` picks (the tallest,
        then the biggest), its first part (a version in several parts plays its first). None
        without Plex or a known server, when the server doesn't have it, or it names no file.
        Raises PlexError (PlexUnreachable when it didn't answer)."""
        uri = self._settings.current.plex_server_uri
        if not _ITEM_KEY.fullmatch(key):
            raise PlexError("A title's key that doesn't read")
        if not self.connected or not uri:
            return None
        item = await self._on_screen(
            "title",
            lambda at, token: self._client.metadata(
                at, token, key, markers=True, timeout=SCREEN_TIMEOUT
            ),
        )
        if item is None:
            return None
        best = best_version(item, lambda part: bool(part.file))
        if best is None:
            return None
        media, part = best
        if len(media.parts) > 1:
            log.info("Play: film has %d parts (%s), the first plays", len(media.parts), key)
        return _video(item, item.rating_key or key, part.file or "", markers_known=True)

    async def show_videos(self, show: PlexTitle) -> list[PlexVideo]:
        """A show's episodes with their files, by season and number (the specials, season 0,
        last), from the server the last check reached; none without Plex or a known server.
        An episode without a file is left out. Raises PlexError (PlexUnreachable when it
        didn't answer)."""
        uri = self._settings.current.plex_server_uri
        if not self.connected or not uri or show.kind != "show":
            return []
        found: list[PlexVideo] = []
        replies = await self._on_screen(
            "episodes",
            lambda at, token: self._client.episodes(at, token, show.key, timeout=SCREEN_TIMEOUT),
        )
        for reply in replies:
            media = reply.media[0] if reply.media else None
            part = media.parts[0] if media is not None and media.parts else None
            if reply.rating_key is None or part is None or not part.file:
                continue
            found.append(_video(reply, reply.rating_key, part.file, episode=True))
        found.sort(key=episode_order)
        log.info("Plex: %d episode file(s) of a show read", len(found))
        return found

    async def continue_watching(self) -> list[PlexContinue]:
        """The Library page's Continue watching: what Plex says to go on with, in its order, at
        most CONTINUE_SIZE (`continue_item`: episodes and films only), from the server the last
        check reached; none without Plex or a known server. A call a person waits on
        (`_on_screen`). Raises PlexError (PlexUnreachable when it didn't answer)."""
        uri = self._settings.current.plex_server_uri
        if not self.connected or not uri:
            return []
        replies = await self._on_screen(
            "continue watching",
            lambda at, token: self._client.continue_watching(at, token, timeout=SCREEN_TIMEOUT),
        )
        stored = await self._repository.titles_by_key(continue_keys(replies))
        found: list[PlexContinue] = []
        for reply in replies:
            chosen = continue_item(reply, stored)
            if chosen is not None:
                found.append(chosen)
            if len(found) == CONTINUE_SIZE:
                break
        log.info("Plex: %d to continue watching", len(found))
        return found

    # -- AirGrabber's player: Plex in sync -------------------------------------------------------

    async def markers(self, key: str) -> Markers:
        """A film's or an episode's intro (start, end) and credits start in milliseconds, from
        the server the last check reached; (None, None) when Plex has none, doesn't have the
        title, or isn't connected. Raises PlexError (PlexUnreachable when it didn't answer)."""
        uri = self._settings.current.plex_server_uri
        if not _ITEM_KEY.fullmatch(key):
            raise PlexError("A title's key that doesn't read")
        if not self.connected or not uri:
            return None, None
        item = await self._on_screen(
            "markers",
            lambda at, token: self._client.metadata(
                at, token, key, markers=True, timeout=SCREEN_TIMEOUT
            ),
        )
        return (None, None) if item is None else item_markers(item)

    async def report(
        self, key: str, state: PlayState, position_ms: int, duration_ms: int
    ) -> None:
        """Tell Plex where `key` is being played (its timeline: Continue watching on the TV and
        the phone). Raises PlexError (PlexUnreachable when it didn't answer or isn't
        connected)."""
        uri = self._connected_uri()
        await self._client.timeline(uri, self._token(), key, state, position_ms, duration_ms)

    async def mark_watched(self, key: str) -> None:
        """Mark `key` watched in Plex (a scrobble). Raises PlexError (PlexUnreachable when it
        didn't answer or isn't connected)."""
        uri = self._connected_uri()
        await self._client.scrobble(uri, self._token(), key)

    def _connected_uri(self) -> str:
        uri = self._settings.current.plex_server_uri
        if not self.connected or not uri:
            raise PlexUnreachable("Plex isn't connected")
        return uri

    async def locations(self) -> list[tuple[str, str]]:
        """The show and film libraries' folders as the server sees them (library key, path);
        none without Plex or a known server. Kept for the run from the last check (which
        refreshes them) or the first time they were asked. Raises PlexError."""
        uri = self._settings.current.plex_server_uri
        if not self.connected or not uri:
            return []
        if self._locations is None:
            self._locations = await self._on_screen(
                "libraries",
                lambda at, token: self._client.locations(at, token, timeout=SCREEN_TIMEOUT),
            )
        return list(self._locations)

    async def scan_folders(self, folders: Iterable[str]) -> int:
        """Ask Plex to scan these folders (where MediaForge just moved files): how many scans
        were asked for (0 without Plex connected or folders; a folder no library holds is left
        alone). Raises PlexError when it couldn't all be asked: the libraries didn't read, a
        scan failed (the others are still asked), or no check has found the server yet
        (PlexUnreachable), so the caller asks again later. Logs counts and the error's kind
        only."""
        uri = self._settings.current.plex_server_uri
        wanted = sorted(set(folders))
        if not self.connected or not wanted:
            return 0
        if not uri:
            raise PlexUnreachable("Plex's address isn't known yet")
        token = self._token()
        try:
            locations = await self._client.locations(uri, token)
        except PlexError as exc:
            log.info("Plex scan: the libraries didn't read (%s)", type(exc).__name__)
            raise
        self._locations = locations
        scans = {found for folder in wanted if (found := plex_folder(folder, locations))}
        asked = 0
        failed: PlexError | None = None
        for key, path in sorted(scans):
            try:
                await self._client.scan(uri, token, key, path)
            except PlexError as exc:
                log.info("Plex scan of library %s failed (%s)", key, type(exc).__name__)
                failed = exc
                continue
            asked += 1
        log.info(
            "Plex scan: %d folder(s) asked for, %d of %d not in a library",
            asked,
            len(wanted) - len({folder for folder in wanted if plex_folder(folder, locations)}),
            len(wanted),
        )
        if failed is not None:
            raise failed
        return asked

    async def _reach(self, token: str) -> str:
        """The address of the chosen server that answers (see the module docstring): the
        stored address and plex.tv's list are asked at the same time; the stored one answering
        is used at once (plex.tv's list still lands, behind); else the server's other
        addresses race (with the stored one still in it), the first that answers wins and is
        stored, the rest are cancelled."""
        current = self._settings.current
        stored, chosen = current.plex_server_uri, current.plex_server_id
        listing = asyncio.create_task(self._listing(token), name="plex-servers")
        tries: dict[str, asyncio.Task[bool]] = {}
        handed = False
        try:
            if stored and chosen:
                tries[stored] = asyncio.create_task(self._answers(stored, chosen, token))
                await asyncio.wait({tries[stored], listing}, return_when=asyncio.FIRST_COMPLETED)
                if tries[stored].done() and tries[stored].result():
                    self._finish_behind(listing, chosen)
                    handed = True
                    return stored
            servers = await listing
            server = await self._take_listing(servers, chosen)
            chosen = server.machine_id if server is not None else chosen
            if not chosen:
                raise PlexUnreachable("plex.tv didn't answer")
            candidates = [stored] if stored else []
            if server is not None:
                candidates += [c.uri for c in connection_order(server) if c.uri != stored]
            for uri in candidates:
                if uri not in tries:
                    tries[uri] = asyncio.create_task(self._answers(uri, chosen, token))
            pending = set(tries.values())
            while pending:
                _done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                found = next(
                    (uri for uri in candidates if _answered(tries.get(uri))), None
                )
                if found is not None:
                    if found != stored:
                        await self._settings.update(plex_server_uri=found)
                        log.info(
                            "Plex: the server answered on connection %d",
                            candidates.index(found) + 1,
                        )
                    return found
            raise PlexUnreachable("The server didn't answer")
        finally:
            for task in tries.values():
                task.cancel()
            if not handed:
                listing.cancel()

    async def _listing(self, token: str) -> list[PlexServer] | None:
        """plex.tv's list of the account's servers; None while plex.tv is away (the server's
        own address may still answer). Raises PlexAuthError."""
        try:
            return await self._client.servers(token)
        except PlexUnreachable:
            return None

    async def _take_listing(
        self, servers: list[PlexServer] | None, chosen: str
    ) -> PlexServer | None:
        """The chosen server in plex.tv's list (the only one when none was chosen yet), its
        name and id stored; None while plex.tv is away. Raises _NoServer."""
        if servers is None:
            return None
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
        current = self._settings.current
        if (server.machine_id, server.name) != (current.plex_server_id, current.plex_server_name):
            await self._settings.update(
                plex_server_id=server.machine_id, plex_server_name=server.name
            )
        return server

    def _stop_behind(self) -> None:
        """Disconnect or another server chosen: plex.tv's lists still on their way, and a check
        waiting to happen, are dropped (they would write the old server back)."""
        later, self._later = self._later, None
        if later is not None:
            later.cancel()
        for task in list(self._behind):
            task.cancel()
        self._behind.clear()

    def _finish_behind(self, listing: asyncio.Task[list[PlexServer] | None], chosen: str) -> None:
        """The stored address answered first: plex.tv's list is taken in when it comes (the
        Settings box's servers, a renamed server), only while Plex is still connected to
        `chosen`; the account no longer having it sets `problem`, other failures change
        nothing."""

        async def finish() -> None:
            try:
                servers = await listing
                if not self.connected or self._settings.current.plex_server_id != chosen:
                    return  # disconnected or another server chosen meanwhile
                await self._take_listing(servers, chosen)
            except _NoServer as exc:
                self._problem = exc.problem
                log.info("Plex: plex.tv's list: %s", exc.problem.value)
                self._notify()
            except PlexError as exc:
                log.info("Plex: plex.tv's list didn't come (%s)", type(exc).__name__)
            finally:
                if not listing.done():
                    listing.cancel()

        task = asyncio.create_task(finish(), name="plex-servers-behind")
        self._behind.add(task)
        task.add_done_callback(self._behind.discard)

    async def _on_screen[T](self, what: str, call: Callable[[str, str], Awaitable[T]]) -> T:
        """A call a person waits on, at the stored address (SCREEN_TIMEOUT is the call's own):
        at once PlexUnreachable when Plex was found away less than FAIL_FAST_S ago; after a
        call Plex didn't answer, the server is looked for once (`_reach`, at most
        SCREEN_REACH_S) and the call made again only when it answered somewhere else (that
        address is stored); else PlexUnreachable."""
        if self._unreachable_at is not None and (
            self._clock() - self._unreachable_at < timedelta(seconds=FAIL_FAST_S)
        ):
            raise PlexUnreachable(f"Plex didn't answer a moment ago ({what})")
        uri, token = self._settings.current.plex_server_uri, self._token()
        try:
            return await call(uri, token)
        except PlexUnreachable:
            log.info("Plex %s: looking for the server again", what)
        try:
            async with asyncio.timeout(SCREEN_REACH_S):
                found = await self._reach(token)
        except PlexAuthError:
            raise
        except (PlexError, TimeoutError):  # away, or no server to be found
            self._unreachable_at = self._clock()
            log.info("Plex %s: the server wasn't found", what)
            raise PlexUnreachable(f"Plex didn't answer ({what})") from None
        if found == uri:  # it answers, but not this in time: no second wait, nor for a while
            self._unreachable_at = self._clock()
            raise PlexUnreachable(f"Plex didn't answer in time ({what})")
        return await call(found, token)

    async def _answers(self, uri: str, machine_id: str, token: str) -> bool:
        try:
            found = await self._client.identity(uri, token)
        except PlexError:
            return False
        return found == machine_id

    async def _read(self, uri: str, token: str) -> PlexScan:
        off = set(self._settings.current.plex_sections_off)
        found, self._locations = await self._client.libraries(uri, token)
        libraries = [library.model_copy(update={"on": library.key not in off}) for library in found]
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
