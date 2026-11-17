"""The folder library's stored files (docs/folder-library/): what was found per video file of
the user's film and series folders (`library_files`, airgrabber.db v12), and where each person
left a video (`watch_places`)."""

from __future__ import annotations

from typing import Literal

from app.models.domain import DomainModel
from app.models.types import UtcDatetime

FileKind = Literal["movie", "episode"]
# What FolderLibraryService.switch_on came to: "on" switched on; "unreachable" no folder could be
# reached (nothing stored or saved); "slow" none was read in time but some answered (as
# "unreachable", but asking again soon uses what was read meanwhile); "overtaken" a later switch
# came first; "off" no folder given, switched off. With Plex connected it is read all the same
# (two shelves: the folders count beside Plex).
SwitchOutcome = Literal["on", "unreachable", "slow", "overtaken", "off"]


class LibraryFile(DomainModel):
    """One video file as the last check left it: its size and change time (an unchanged file
    keeps this row and isn't read again), what it is (`kind`; None: its name couldn't be read)
    and, once recognised, its film's or show's TMDB id; an episode's season and number(s);
    `quality` "4K" / "1080p" / "720p" / "SD" from its name. `recognised`: matched to a TMDB
    title (False: TMDB knows none, or the name says nothing; not asked again until the file
    changes). `path` is never logged."""

    path: str
    size: int
    mtime: float
    kind: FileKind | None = None
    tmdb_id: int | None = None
    season: int | None = None
    episode: int | None = None
    episode_end: int | None = None
    quality: str | None = None
    recognised: bool = False


class FolderLibraryStatus(DomainModel):
    """What Settings' folder library part and the Library page show (FolderLibraryService's
    `status`): whether the folders count now (`on`: switched on with a folder set, with or
    without Plex: `folders_on`), whether a check runs, the last check's films, shows and their episodes, the files it
    couldn't recognise (paths, sorted; never logged), the folders it couldn't reach (as set in
    Settings), those that answered but weren't read in time (`slow`) and when it was."""

    on: bool = False
    checking: bool = False
    films: int = 0
    shows: int = 0
    episodes: int = 0
    unrecognised: tuple[str, ...] = ()
    unreachable: tuple[str, ...] = ()
    slow: tuple[str, ...] = ()
    checked_at: UtcDatetime | None = None


class WatchPlace(DomainModel):
    """Where a person left a folder library video (`watch_places`): the `item` is the title's
    identity ("movie:<tmdb>" / "episode:<tmdb>:<season>:<episode>", playing.py's `item_key`), so
    a file MediaForge renamed or moved keeps it; the place and the length in milliseconds (0:
    from the start; None: the length isn't known), watched, and when it last changed."""

    profile_id: int
    item: str
    position_ms: int = 0
    duration_ms: int | None = None
    watched: bool = False
    updated_at: UtcDatetime
