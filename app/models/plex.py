"""Plex (app/services/plex.py): plex.tv's and the Plex Media Server's JSON → models.

The wire models validate the replies at the boundary (unknown fields ignored); the domain models
are what the services hand on and the Settings box and the connect window show. No model ever
holds the Plex token: it lives in the settings (`plex_token`) and travels in the X-Plex-Token
header only.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from app.models.domain import DomainModel
from app.models.types import UtcDatetime


def _text(value: Any) -> Any:
    """Plex writes keys as strings, but a number is read as one too."""
    return str(value) if isinstance(value, int) and not isinstance(value, bool) else value


def _number(value: Any) -> Any:
    """An index Plex may write as a string ("3")."""
    if isinstance(value, str) and value.strip().isdigit():
        return int(value)
    return value


_Key = Annotated[str, BeforeValidator(_text)]
_Index = Annotated[int | None, BeforeValidator(_number)]


class _Wire(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)


# -- plex.tv ---------------------------------------------------------------------------------------


class PlexPinReply(_Wire):
    """POST /api/v2/pins and GET /api/v2/pins/{id}: `auth_token` once the code was entered."""

    id: int
    code: str
    expires_at: datetime | None = Field(default=None, alias="expiresAt")
    expires_in: int | None = Field(default=None, alias="expiresIn")  # seconds
    auth_token: str | None = Field(default=None, alias="authToken")


class PlexConnectionReply(_Wire):
    protocol: str = "http"
    address: str = ""
    port: int | None = None
    uri: str
    local: bool = False
    relay: bool = False


class PlexResourceReply(_Wire):
    """One of /api/v2/resources: a server (`provides` holds "server"), player or the like."""

    name: str = ""
    product: str = ""
    client_identifier: str = Field(alias="clientIdentifier")
    provides: str = ""
    owned: bool = False
    connections: list[PlexConnectionReply] = Field(default_factory=list)

    @property
    def is_server(self) -> bool:
        return "server" in (part.strip() for part in self.provides.split(","))


# -- the server ------------------------------------------------------------------------------------


class PlexGuidReply(_Wire):
    id: str


class PlexItemReply(_Wire):
    """A show, a film or an episode of /library/sections/{id}/all."""

    rating_key: _Key | None = Field(default=None, alias="ratingKey")
    title: str = ""
    guid: str | None = None  # "plex://show/…" or an old agent's "com.plexapp.agents.thetvdb://…"
    guids: list[PlexGuidReply] = Field(default_factory=list, alias="Guid")  # includeGuids=1
    grandparent_rating_key: _Key | None = Field(default=None, alias="grandparentRatingKey")
    parent_index: _Index = Field(default=None, alias="parentIndex")  # an episode's season
    index: _Index = None  # an episode's number

    def guid_texts(self) -> tuple[str, ...]:
        """Every guid the item names: its own, then the Guid list."""
        own = (self.guid,) if self.guid else ()
        return own + tuple(guid.id for guid in self.guids)


class PlexLocationReply(_Wire):
    """One of a library's folders, as the server sees it ("/volume1/video/TV", "D:\\TV")."""

    path: str = ""


class PlexDirectoryReply(_Wire):
    """A library of /library/sections."""

    key: _Key
    type: str = ""
    title: str = ""
    locations: list[PlexLocationReply] = Field(default_factory=list, alias="Location")


class PlexContainerReply(_Wire):
    size: int = 0
    total_size: int | None = Field(default=None, alias="totalSize")
    offset: int = 0
    machine_identifier: str | None = Field(default=None, alias="machineIdentifier")
    directories: list[PlexDirectoryReply] = Field(default_factory=list, alias="Directory")
    metadata: list[PlexItemReply] = Field(default_factory=list, alias="Metadata")


class PlexReply(_Wire):
    container: PlexContainerReply = Field(alias="MediaContainer")


# -- domain ----------------------------------------------------------------------------------------


class PlexPin(DomainModel):
    """The code the user enters at plex.tv/link, and until when it works."""

    id: int
    code: str
    expires_at: UtcDatetime


class PlexConnection(DomainModel):
    uri: str
    local: bool = False
    https: bool = False


class PlexServer(DomainModel):
    """A Plex Media Server the account owns, with the ways to reach it plex.tv lists."""

    machine_id: str
    name: str
    connections: tuple[PlexConnection, ...] = ()


LibraryKind = Literal["show", "movie"]


class PlexLibrary(DomainModel):
    """A show or film library on the server; `on`: its titles count (Settings' tick box)."""

    key: str
    title: str
    kind: LibraryKind
    on: bool = True


class PlexProblem(StrEnum):
    UNREACHABLE = "unreachable"  # plex.tv or the server didn't answer
    SIGNED_OUT = "signed_out"  # the token was refused: connect again
    NO_SERVER = "no_server"  # the account owns no server (or the chosen one is gone)
    CHOOSE_SERVER = "choose_server"  # several servers: the user picks one
    FAILED = "failed"  # an answer that didn't read


class PlexFilms(DomainModel):
    """The films in Plex by their TMDB and IMDb ids (a film from Plex's old IMDb agent may
    have only the second)."""

    tmdb: frozenset[int] = frozenset()
    imdb: frozenset[str] = frozenset()

    def has(self, tmdb_id: int | None, imdb_id: str | None = None) -> bool:
        return (tmdb_id is not None and tmdb_id in self.tmdb) or (
            imdb_id is not None and imdb_id in self.imdb
        )


class PlexCounts(DomainModel):
    """What the last check found: `episodes` of `shows` of the series anyone follows, `films`,
    and the Plex shows AirGrabber doesn't know (`unmatched_shows`, counted, not stored)."""

    episodes: int = 0
    shows: int = 0
    films: int = 0
    unmatched_shows: int = 0
    checked_at: UtcDatetime | None = None


class PlexStatus(DomainModel):
    """The Settings box's state."""

    connected: bool = False  # a token is stored
    server: PlexServer | None = None  # the chosen server (its name), once picked
    servers: tuple[PlexServer, ...] = ()  # the owned servers (the dropdown with two or more)
    libraries: tuple[PlexLibrary, ...] = ()
    counts: PlexCounts = Field(default_factory=PlexCounts)
    problem: PlexProblem | None = None
    checking: bool = False


class PlexScan(DomainModel):
    """One check's result, ready to store: the matched episodes as (TVMaze show id, season,
    episode), the films as (TMDB id, IMDb id), and what was counted."""

    episodes: frozenset[tuple[int, int, int]] = frozenset()
    films: frozenset[tuple[int | None, str | None]] = frozenset()
    shows: int = 0  # Plex shows read
    unmatched_shows: int = 0
    unmatched_films: int = 0  # films without a TMDB or IMDb id
    libraries: tuple[PlexLibrary, ...] = ()
