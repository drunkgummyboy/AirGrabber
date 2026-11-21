"""▶ Play: a film or an episode from Plex opened in VLC on this PC (the info windows' "▶ Play").

Plex runs on this PC or on another machine (a NAS). This PC finds the file: as Plex names it when
that is a Windows path that is there (Plex on this PC); else through the user's own folders of that
kind (Settings → Folders' lists, the folder library's `series_folders` / `film_folders`, whatever
its "Count what's in them" switch says: only whether a file is there is read); else through
MediaForge's Series and Movies folders (Forge's settings `series_folder` / `movies_folder`, read
through the Forge runtime like app/services/download_status.py does: no SQL across the databases,
Forge's types only under TYPE_CHECKING). Those folders are often network shares.

1. Plex says which file a title is (PlexService.film_video / show_videos): the server's path,
   such as /volume1/video/Movies/Dune (2021)/Dune (2021).mkv.
2. `local_candidates` turns it into the paths Windows may see under each folder: the library
   folder (one of PlexService.locations) that holds the file gives the part below it, joined onto
   the folder; then the part below the folder's own ending in the file's path (`plex_folder`'s idea
   the other way round); then the file's last TAIL_PARTS names under that folder. Each folder is
   looked at in its own thread (a NAS can hang), all at once within FIND_TIMEOUT_S
   (`_first_found`): the first folder in that order holding the file wins; one that doesn't
   answer in time leaves the others theirs, and when none had it while one didn't answer, the
   outcome says the folder didn't answer (TIMED_OUT), not that the file isn't there.
3. `launch_video` starts VLC when it is installed (`find_vlc`: Program Files, Program Files
   (x86), then the PATH), else Windows' own player (`os.startfile`). The real starts are only
   `_start_process` / `_open_with_windows`, which tests/__init__.py refuses.

`play` is `locate` (steps 1-2: a `Located` file, or the PlayOutcome saying why not) and then
`launch` (step 3). ▶ Play (app/ui/play_control.py) reads a film's video with `film_video` and a
show's with `episodes`, and hands it to AirGrabber's own player (app/ui/media_player.py), which
uses `locate` alone and `launch` only when it falls back to VLC.

The folder library (docs/folder-library/; `folder_library`, app/services/folder_library/
playing.py) is the other source, beside Plex or without it: its titles, a film's video, a show's
episodes (each at the person's place: `person`, the PC's selected one) and the file come from
what its last check stored, without Plex or MediaForge. A title of one copy plays; of two, the
best one still there (`copies`, Plex's order). With neither, ▶ Play isn't there (`available`).

A show's and a film's window sit on no shelf (docs/folder-library/2026-11-16-alongside-plex-
design.md, "The show and film windows"): a show's titles in both (`ShowTitles`, joined by TVMaze,
TheTVDB or IMDb id) give one episode list (`merged_episodes`), each episode from where it is and
the folder copy where both have it; a film in both is the folders'. What plays where is decided
per video (`is_folder_video`, its key), never per title.

What goes wrong comes back as a PlayOutcome whose `text` says so in plain words for the status
bar: Plex didn't answer, MediaForge is off or its folder isn't set, the file isn't in that folder
from this PC (or in the folder library's folders), a folder didn't answer in time. Logs give ids
and counts only: never a path, file name or title.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import time
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Literal

from app.i18n import N_, t
from app.models import FIRST_PROFILE_ID, Settings, Show
from app.models.plex import LibraryKind, PlexTitle, PlexVideo
from app.services.folder_library.playing import (
    NOT_IN_FOLDERS_TEXT,
    FolderPlaying,
    film_item,
    from_folders,
    is_folder_video,
    logged_key,
    parse_item,
)
from app.services.plex import PlexError, PlexService, PlexUnreachable, episode_order

if TYPE_CHECKING:  # Forge's runtime: only typed here, nothing of Forge is imported
    from app.forge.runtime import ForgeRuntime

log = logging.getLogger(__name__)

TAIL_PARTS = (3, 2)  # "Show/Season 01/file.mkv", "Dune (2021)/Dune (2021).mkv"
FIND_TIMEOUT_S = 20.0  # a share that takes longer counts as not holding the file this time
EPISODES_TTL_S = 300.0  # a show's episodes read this recently show at once in the picker
VLC_PLACES = (
    ("ProgramFiles", r"VideoLAN\VLC\vlc.exe"),
    ("ProgramFiles(x86)", r"VideoLAN\VLC\vlc.exe"),
)

Player = Literal["vlc", "default"]

FINDING_TEXT = N_("▶ Finding {name}…")
PLAYING_VLC_TEXT = N_("▶ Playing {name} in VLC.")
PLAYING_DEFAULT_TEXT = N_("▶ Playing {name} in your video player.")
NOT_CONNECTED_TEXT = N_("Connect Plex in Settings → Plex & library first.")
PLEX_AWAY_TEXT = N_("Plex didn't answer, so try ▶ Play again in a moment.")
NO_FILE_TEXT = N_("Plex doesn't say which file {name} is.")
FORGE_OFF_TEXT = N_(
    "MediaForge is off, so AirGrabber doesn't know your Series and Movies folders."
)
NO_SERIES_TEXT = N_("Choose your Series folder in Settings › Folders, so ▶ Play can find {name}.")
NO_MOVIES_TEXT = N_("Choose your Movies folder in Settings › Folders, so ▶ Play can find {name}.")
NOT_IN_SERIES_TEXT = N_(
    "{name} isn't in your Series folder from this PC: check the Series folder in Settings › Folders."
)
NOT_IN_MOVIES_TEXT = N_(
    "{name} isn't in your Movies folder from this PC: check the Movies folder in Settings › Folders."
)
NO_PLAYER_TEXT = N_("AirGrabber couldn't start a video player for {name}.")
TIMED_OUT_TEXT = N_("The folder didn't answer in time. Try again.")


class PlayProblem(StrEnum):
    NOT_CONNECTED = "not_connected"
    PLEX_AWAY = "plex_away"  # Plex didn't answer (or said no)
    NO_FILE = "no_file"  # Plex names no file
    FORGE_OFF = "forge_off"  # MediaForge isn't running: its folders can't be read
    NO_FOLDER = "no_folder"  # its Series / Movies folder isn't set
    NOT_FOUND = "not_found"  # not in that folder from this PC
    NO_PLAYER = "no_player"  # starting the player failed
    TIMED_OUT = "timed_out"  # a folder (a share asleep) didn't answer in time, none had it


@dataclass(frozen=True, slots=True)
class PlayOutcome:
    """What ▶ Play did: played in `player`, or the `problem`; `name` is what the status bar
    names ("Dune", "The Ark S02E03"), `kind` which library folder counted, `folders` that the
    title is the folder library's (its own words for a file not found)."""

    name: str
    kind: LibraryKind
    player: Player | None = None
    problem: PlayProblem | None = None
    folders: bool = False

    @property
    def played(self) -> bool:
        return self.problem is None

    @property
    def text(self) -> str:
        """The status bar's line (translated)."""
        shows = self.kind == "show"
        match self.problem:
            case None:
                text = PLAYING_VLC_TEXT if self.player == "vlc" else PLAYING_DEFAULT_TEXT
            case PlayProblem.NOT_CONNECTED:
                return t(NOT_CONNECTED_TEXT)
            case PlayProblem.PLEX_AWAY:
                return t(PLEX_AWAY_TEXT)
            case PlayProblem.NO_FILE:
                text = NO_FILE_TEXT
            case PlayProblem.FORGE_OFF:
                return t(FORGE_OFF_TEXT)
            case PlayProblem.NO_FOLDER:
                text = NO_SERIES_TEXT if shows else NO_MOVIES_TEXT
            case PlayProblem.NOT_FOUND if self.folders:
                text = NOT_IN_FOLDERS_TEXT
            case PlayProblem.NOT_FOUND:
                text = NOT_IN_SERIES_TEXT if shows else NOT_IN_MOVIES_TEXT
            case PlayProblem.NO_PLAYER:
                text = NO_PLAYER_TEXT
            case PlayProblem.TIMED_OUT:
                return t(TIMED_OUT_TEXT)
        return t(text, name=self.name)


@dataclass(frozen=True, slots=True)
class Located:
    """Where `locate` found a video's file from this PC (`path`: never logged), with the status
    bar's `name` ("Dune", "The Ark S02E03") and the library `kind` that counted. `video`: the
    video found instead of the one asked for (a folder copy not there: Plex's copy of the same
    episode or film, which then plays and is reported as Plex's); None for the one asked."""

    name: str
    kind: LibraryKind
    path: str
    video: PlexVideo | None = None


@dataclass(frozen=True, slots=True)
class LibraryFolders:
    """MediaForge's Series and Movies folders as this PC sees them ("" = not set)."""

    series: str = ""
    movies: str = ""

    def of(self, kind: LibraryKind) -> str:
        return self.series if kind == "show" else self.movies


FolderLookup = Callable[[], Awaitable[LibraryFolders | None]]
OwnFolders = Callable[[LibraryKind], Sequence[str]]  # Settings → Folders' list of a kind
Launcher = Callable[[str], Awaitable[Player]]


def settings_folders(current: Callable[[], Settings]) -> OwnFolders:
    """The user's own folders of a kind as Settings → Folders lists them (the folder library's
    `series_folders` / `film_folders`), read when asked, whether they are counted or not."""

    def of(kind: LibraryKind) -> Sequence[str]:
        settings = current()
        return settings.series_folders if kind == "show" else settings.film_folders

    return of


def forge_folders(runtime: ForgeRuntime) -> FolderLookup:
    """MediaForge's library folders, read when asked (Forge started first when it hasn't yet);
    None while it is off."""

    async def current() -> LibraryFolders | None:
        services = runtime.services or await runtime.start()
        if services is None:
            return None
        settings = services.settings.current
        return LibraryFolders(series=settings.series_folder, movies=settings.movies_folder)

    return current


# -- the pure part ---------------------------------------------------------------------------------


def _server_names(path: str) -> list[str]:
    """A path's names as the Plex server writes it: POSIX ("/volume1/video/TV/…") or Windows
    ("D:\\Media\\TV\\…", its drive the first name)."""
    if "\\" in path or (len(path) > 1 and path[1] == ":"):
        windows = PureWindowsPath(path)
        rest = [name for name in windows.parts[1:] if name]
        return [windows.drive, *rest] if windows.drive else rest
    return [name for name in PurePosixPath(path).parts[1:] if name]


def _safe(names: Sequence[str]) -> bool:
    """Names that stay below the folder they are joined onto."""
    return bool(names) and all(
        name not in (".", "..") and not any(c in name for c in ':\\/') for name in names
    )


def _windows_names(folder: str) -> list[str]:
    """A Windows folder's names, a network path's share first (its server left out), as
    app/services/plex.py's `plex_folder` reads them."""
    path = PureWindowsPath(folder)
    share = [path.drive.rstrip("\\").split("\\")[-1]] if path.drive.startswith("\\\\") else []
    return [name for name in [*share, *path.parts[1:]] if name]


def _below_shared_ending(names: Sequence[str], folder: str) -> list[str] | None:
    """The file's names below the longest ending of `folder` that its path holds in one piece
    (the share video's folder TV is /volume1/video/TV on the server: `plex_folder` the other
    way round); None when no ending fits."""
    mine = [name.casefold() for name in _windows_names(folder)]
    folded = [name.casefold() for name in names]
    for size in range(len(mine), 0, -1):
        ending = mine[-size:]
        for start in range(len(folded) - size - 1, -1, -1):  # the deepest, leaving a name below
            if folded[start : start + size] == ending:
                return list(names[start + size :])
    return None


def local_candidates(server_file: str, locations: Iterable[str], folder: str) -> list[str]:
    """Where the server's `server_file` may be under `folder` (as Windows sees it), most likely
    first: the part below the library folder (one of `locations`, the longest that holds it)
    joined onto `folder`; the part below the longest ending of `folder` the file's path holds;
    then the file's last TAIL_PARTS names. None without a folder."""
    names = _server_names(server_file)
    if not folder.strip() or not names:
        return []
    folded = [name.casefold() for name in names]
    below: list[str] | None = None
    held = 0
    for location in locations:
        prefix = [name.casefold() for name in _server_names(location)]
        if held < len(prefix) < len(folded) and folded[: len(prefix)] == prefix:
            below, held = names[len(prefix) :], len(prefix)
    tails = [names[-size:] for size in TAIL_PARTS if size < len(names)]
    found: list[str] = []
    for rest in [below, _below_shared_ending(names, folder), *tails]:
        if rest is None or not _safe(rest):
            continue
        path = str(PureWindowsPath(folder, *rest))
        if path not in found:
            found.append(path)
    return found


def next_episode(videos: Sequence[PlexVideo]) -> PlexVideo | None:
    """The episode the picker selects: the first unwatched one in order (the specials only when
    there are no others); when all are watched, the first. Each video is watched by its own
    source's state (Plex's, or the person's place in the folders), so a show watched in Plex
    goes on into the folders' first unwatched episode."""
    regular = [video for video in videos if video.season]
    pool = regular or list(videos)
    return next((video for video in pool if not video.watched), pool[0] if pool else None)


def episode_name(show: str, video: PlexVideo) -> str:
    """"The Ark S02E03" (the status bar's name of an episode)."""
    code = ""
    if video.season is not None:
        code = f"S{video.season:02d}"
        if video.episode is not None:
            code += f"E{video.episode:02d}"
    return f"{show} {code}".strip()


def find_vlc(
    environ: Mapping[str, str] = os.environ,
    *,
    is_file: Callable[[str], bool] = os.path.isfile,
    which: Callable[[str], str | None] = shutil.which,
) -> str | None:
    """VLC's vlc.exe: in Program Files, Program Files (x86), else on the PATH; None when it
    isn't installed."""
    for variable, below in VLC_PLACES:
        root = environ.get(variable)
        if root:
            path = str(PureWindowsPath(root, below))
            if is_file(path):
                return path
    return which("vlc")


# -- starting the player ---------------------------------------------------------------------------


def _start_process(args: list[str]) -> None:
    """Start a program and never wait for it (it outlives AirGrabber). The one real start."""
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    subprocess.Popen(  # noqa: S603
        args,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
        close_fds=True,
    )


def _open_with_windows(path: str) -> None:
    """Windows' own player for the file (its file type's program)."""
    startfile: Callable[[str], None] = getattr(os, "startfile")
    startfile(path)


async def launch_video(path: str) -> Player:
    """Open `path` in VLC when it is installed, else in Windows' own player. Raises OSError.
    VLC is started straight from the loop (Popen returns at once; asyncio's own subprocess
    would end VLC when AirGrabber quits); Windows' file-type lookup runs in a thread."""
    vlc = await asyncio.to_thread(find_vlc)
    if vlc is not None:
        _start_process([vlc, path])
        return "vlc"
    await asyncio.to_thread(_open_with_windows, path)
    return "default"


def _first_existing(paths: Sequence[str], exists: Callable[[str], bool]) -> int | None:
    for index, path in enumerate(paths):
        try:
            if exists(path):
                return index
        except OSError:
            continue
    return None


def windows_file(path: str) -> bool:
    """A file name Windows can open as it is: on a drive (D:\\Media\\…) or a network share
    (\\\\server\\share\\…); not a POSIX path (a NAS's /volume1/…)."""
    if not path.strip():
        return False
    windows = PureWindowsPath(path)
    return bool(windows.drive) and windows.is_absolute()


def _hit(task: asyncio.Future[int | None]) -> int | None:
    """A finished look's index of the path found; None when it found none or failed."""
    if not task.done() or task.cancelled() or task.exception() is not None:
        return None
    return task.result()


# -- the service -----------------------------------------------------------------------------------


def same_show(one: PlexTitle, other: PlexTitle) -> bool:
    """Two Plex titles of one show: the same TVMaze id, TheTVDB id or IMDb id."""
    return any(
        mine is not None and mine == theirs
        for mine, theirs in (
            (one.show_id, other.show_id),
            (one.tvdb_id, other.tvdb_id),
            (one.imdb_id, other.imdb_id),
        )
    )


@dataclass(frozen=True, slots=True)
class ShowTitles:
    """A show's window's titles: Plex's and the folder library's, joined by TVMaze, TheTVDB or
    IMDb id (one of them at least). Its episodes are both's (`PlaybackService.episodes`)."""

    plex: PlexTitle | None = None
    folders: PlexTitle | None = None
    tmdb_id: int | None = None  # TMDB's id of the show when neither title names one

    def __post_init__(self) -> None:
        if self.plex is None and self.folders is None:
            raise ValueError("A show's titles need one at least")

    @property
    def main(self) -> PlexTitle:
        """The title the picker and the player name and carry languages by: Plex's (with the
        folders' TMDB id, else `tmdb_id`, when Plex's has none), else the folders'."""
        plex, folders = self.plex, self.folders
        if plex is None:
            if folders is None:  # never: __post_init__
                raise ValueError("A show's titles need one at least")
            return folders
        tmdb_id = (folders.tmdb_id if folders is not None else None) or self.tmdb_id
        if plex.tmdb_id is None and tmdb_id is not None:
            return plex.model_copy(update={"tmdb_id": tmdb_id})
        return plex

    @property
    def key(self) -> str:
        """What a kept episode list is kept by."""
        return " ".join(one.key for one in (self.plex, self.folders) if one is not None)

    @classmethod
    def of(cls, title: PlexTitle) -> ShowTitles:
        """A lone title (a Continue watching card's): its own source only."""
        return cls(folders=title) if from_folders(title) else cls(plex=title)


def _matched_show(show: Show, shows: Sequence[PlexTitle]) -> PlexTitle | None:
    """The title of a TVMaze show among `shows`: by the TVMaze id a check matched it to, else its
    TheTVDB id, else its IMDb id (as app/services/plex.py's `match_scan` matches); of several
    (Plex may keep one show as two titles), the one with most episodes."""
    by_id = [title for title in shows if title.show_id == show.id]
    by_tvdb = [one for one in shows if show.tvdb_id is not None and one.tvdb_id == show.tvdb_id]
    by_imdb = [one for one in shows if show.imdb_id is not None and one.imdb_id == show.imdb_id]
    found = by_id or by_tvdb or by_imdb
    return max(found, key=lambda title: title.episodes or 0) if found else None


def _film(
    tmdb_id: int | None, imdb_id: str | None, titles: Sequence[PlexTitle]
) -> PlexTitle | None:
    for title in titles:
        if title.kind == "movie" and (
            (tmdb_id is not None and title.tmdb_id == tmdb_id)
            or (imdb_id is not None and title.imdb_id == imdb_id)
        ):
            return title
    return None


def _titles_of(titles: ShowTitles | PlexTitle) -> ShowTitles:
    return titles if isinstance(titles, ShowTitles) else ShowTitles.of(titles)


def merged_episodes(
    plex: Sequence[PlexVideo],
    folders: Sequence[PlexVideo],
    marked: Collection[tuple[int, int]] = (),
) -> list[PlexVideo]:
    """A show's episodes from both places, in order, one per (season, episode): the folder copy
    where both have one (it is the one just downloaded, on this PC), at its own place. Watched
    when either source says so: Plex's mark, or the person's in `watch_places` (the folder
    copy's own, or `marked`: the (season, episode)s the person watched from the folders, which
    counts for a Plex-only episode too: one moved into Plex's folder by hand, say)."""
    plex_spots = {
        (video.season, video.episode): video
        for video in plex
        if video.season is not None and video.episode is not None
    }
    found: list[PlexVideo] = []
    taken: set[tuple[int, int]] = set()
    for video in folders:
        if video.season is not None and video.episode is not None:
            taken.add((video.season, video.episode))
            other = plex_spots.get((video.season, video.episode))
            if other is not None and other.watched and not video.watched:
                video = video.model_copy(update={"watched": True})
        found.append(video)
    for video in plex:
        spot = (video.season, video.episode)
        if spot[0] is None or spot[1] is None:
            found.append(video)
        elif (spot[0], spot[1]) not in taken:
            if not video.watched and (spot[0], spot[1]) in marked:
                video = video.model_copy(update={"watched": True})
            found.append(video)
    found.sort(key=episode_order)
    return found


def plex_copies(plex: Sequence[PlexVideo], folders: Sequence[PlexVideo]) -> dict[str, PlexVideo]:
    """Plex's copy of each episode both places have, by the folder copy's key: what plays when
    the folder copy isn't there from this PC (moved into Plex's folder by hand, a sleeping
    NAS)."""
    by_spot: dict[tuple[int | None, int | None], PlexVideo] = {
        (video.season, video.episode): video
        for video in plex
        if video.season is not None and video.episode is not None
    }
    return {
        video.key: by_spot[video.season, video.episode]
        for video in folders
        if (video.season, video.episode) in by_spot
    }


class PlaybackService:
    """`services.playback`: which library title an info window shows, and ▶ Play."""

    def __init__(
        self,
        plex: PlexService,
        folders: FolderLookup,
        *,
        folder_library: FolderPlaying | None = None,
        person: Callable[[], int] | None = None,
        show_tmdb_id: Callable[[Show], Awaitable[int | None]] | None = None,
        own_folders: OwnFolders | None = None,
        launch: Launcher = launch_video,
        exists: Callable[[str], bool] = os.path.isfile,
        timeout_s: float = FIND_TIMEOUT_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """`folders`: MediaForge's Series / Movies folders (Plex's files from this PC);
        `folder_library`: the folder library's titles and files, at the place of `person` (the
        PC's selected person; the first without it). `show_tmdb_id`: a TVMaze show's TMDB id
        (DiscoveryService.show_tmdb_id), asked when neither of its titles names one: the
        person's marks in the folders are joined by it. `own_folders`: Settings → Folders'
        folders of a kind (`settings_folders`), where a Plex file is looked for before
        MediaForge's. `launch` / `exists`: the seams tests replace (never a real player);
        `clock`: how old a kept episode list is. `timeout_s`: how long all of one video's
        folders together may take to answer."""
        self._plex = plex
        self._folders = folders
        self._library = folder_library
        self._person = person or (lambda: FIRST_PROFILE_ID)
        self._show_tmdb_id = show_tmdb_id
        self._own_folders: OwnFolders = own_folders or (lambda _kind: ())
        self._launch = launch
        self._exists = exists
        self._timeout_s = timeout_s
        self._clock = clock
        self._kept: dict[str, tuple[float, list[PlexVideo]]] = {}  # a show's key: (read, list)
        # Plex's copies of what the folders have too, by the folder copy's key (`locate`'s
        # fallback): an episode's video, a film's title.
        self._plex_episodes_of: dict[str, PlexVideo] = {}
        self._plex_films_of: dict[str, PlexTitle] = {}
        self._film_tmdb_ids: dict[str, int] = {}  # a Plex film's TMDB id by its key (`film`)

    @property
    def available(self) -> bool:
        """There is a library to play from: Plex is connected, or the folder library is the
        library."""
        return self._plex.connected or self._folder_library() is not None

    def _folder_library(self) -> FolderPlaying | None:
        """The folder library while its folders count (switched on, with or without Plex)."""
        library = self._library
        return library if library is not None and library.on else None

    async def _plex_titles(self) -> list[PlexTitle]:
        """Plex's titles of its last check (none without a token)."""
        return await self._plex.titles() if self._plex.connected else []

    async def _folder_titles(self) -> list[PlexTitle]:
        """The folder library's titles of its last check (none while its folders are off)."""
        library = self._folder_library()
        return await library.titles() if library is not None else []

    async def film(self, tmdb_id: int | None, imdb_id: str | None = None) -> PlexTitle | None:
        """The film with this TMDB id (else IMDb id), from the last checks: the folders' copy
        when both have it (it plays from the folder), else Plex's."""
        if tmdb_id is None and imdb_id is None:
            return None
        folders = _film(tmdb_id, imdb_id, await self._folder_titles())
        plex = _film(tmdb_id, imdb_id, await self._plex_titles())
        if folders is not None and folders.tmdb_id is not None and plex is not None:
            self._plex_films_of[film_item(folders.tmdb_id)] = plex
        if plex is not None and plex.tmdb_id is None and tmdb_id is not None:
            self._film_tmdb_ids[plex.key] = tmdb_id  # matched by IMDb id: its marks by this one
        return folders or plex

    async def show(self, show: Show) -> ShowTitles | None:
        """A TVMaze show's titles in Plex and in the folders (`_matched_show` in each); None
        when neither has it."""
        plex = _matched_show(show, [one for one in await self._plex_titles() if one.kind == "show"])
        folders = _matched_show(
            show, [one for one in await self._folder_titles() if one.kind == "show"]
        )
        if plex is None and folders is None:
            return None
        tmdb_id: int | None = None
        if self._show_tmdb_id is not None and all(
            one is None or one.tmdb_id is None for one in (plex, folders)
        ):
            tmdb_id = await self._show_tmdb_id(show)
        return ShowTitles(plex=plex, folders=folders, tmdb_id=tmdb_id)

    def kept_episodes(self, titles: ShowTitles | PlexTitle) -> list[PlexVideo] | None:
        """The show's episodes as `episodes` last read them, if that was less than
        EPISODES_TTL_S ago (the picker shows them at once while it reads them again); else
        None."""
        kept = self._kept.get(self._kept_key(_titles_of(titles)))
        if kept is None or self._clock() - kept[0] >= EPISODES_TTL_S:
            return None
        return list(kept[1])

    async def episodes(self, titles: ShowTitles | PlexTitle) -> list[PlexVideo]:
        """A show's episodes with their files (the picker), in order, from Plex and the folders
        together (`merged_episodes`: one per episode, the folder copy where both have it).
        Kept for `kept_episodes`. A lone title (`ShowTitles.of`) is its own source's. Raises
        PlexError when Plex doesn't answer and the folders have none of the show; with some,
        those are given (not kept: the whole list is read again next time)."""
        both = _titles_of(titles)
        library = self._folder_library()
        folder_videos: list[PlexVideo] = []
        if both.folders is not None and library is not None:
            folder_videos = await library.show_videos(both.folders, profile_id=self._person())
        plex_videos: list[PlexVideo] = []
        if both.plex is not None:
            try:
                plex_videos = await self._plex_episodes(both.plex)
            except PlexError as exc:
                if not folder_videos:
                    raise
                kept = self.kept_episodes(both)
                log.info(
                    "Play: Plex's episodes didn't read (%s): %s", type(exc).__name__,
                    "the list read a moment ago" if kept else "the folders' only",
                )
                return kept or folder_videos
        marked = await self._marked_episodes(both.main.tmdb_id) if plex_videos else set()
        videos = merged_episodes(plex_videos, folder_videos, marked)
        self._plex_episodes_of.update(plex_copies(plex_videos, folder_videos))
        if plex_videos and folder_videos:
            log.info(
                "Play: a show's episodes from Plex (%d) and the folders (%d)",
                len(plex_videos), len(folder_videos),
            )
        self._kept[self._kept_key(both)] = (self._clock(), list(videos))
        return videos

    def _kept_key(self, titles: ShowTitles) -> str:
        """A kept list is the person's own (their places and marks are in it)."""
        return f"{titles.key} {self._person()}"

    async def _watched_items(self) -> set[str]:
        """The item keys the person watched from the folders (whether the folders are on now or
        not: the mark is the person's own)."""
        library = self._library
        return await library.watched_items(profile_id=self._person()) if library else set()

    async def _marked_episodes(self, tmdb_id: int | None) -> set[tuple[int, int]]:
        """The (season, episode)s of the show with this TMDB id the person watched from the
        folders."""
        if tmdb_id is None:
            return set()
        marked: set[tuple[int, int]] = set()
        for item in await self._watched_items():
            parsed = parse_item(item)
            if parsed is not None and parsed[0] == "episode" and parsed[1] == tmdb_id:
                season, number = parsed[2], parsed[3]
                if season is not None and number is not None:
                    marked.add((season, number))
        return marked

    async def _plex_episodes(self, title: PlexTitle) -> list[PlexVideo]:
        """A Plex show's episodes with their files, in order: those of every title Plex keeps
        the show as (`same_show`; it splits one when some files got matched apart), an episode
        in two of them once: the copy of the title with the most. Raises PlexError."""
        titles = [title] + [
            other
            for other in await self._plex.titles()
            if other.kind == "show" and other.key != title.key and same_show(other, title)
        ]
        titles.sort(key=lambda one: one.episodes or 0, reverse=True)
        videos: list[PlexVideo] = []
        seen: set[tuple[int, int]] = set()
        for one in titles:
            for video in await self._plex.show_videos(one):
                spot = (video.season, video.episode)
                if spot[0] is not None and spot[1] is not None:
                    if (spot[0], spot[1]) in seen:
                        continue
                    seen.add((spot[0], spot[1]))
                videos.append(video)
        if len(titles) > 1:
            log.info("Play: a show kept as %d titles in Plex", len(titles))
            videos.sort(key=episode_order)
        return videos

    async def film_video(self, title: PlexTitle) -> PlexVideo | PlayOutcome:
        """A film's video as Plex has it now (its file, where it was left, its length); the
        PlayOutcome saying why not when Plex can't say. A folder library film's: its best copy
        at the person's place."""
        if from_folders(title):
            library = self._folder_library()
            found = (
                await library.film_video(title, profile_id=self._person())
                if library is not None
                else None
            )
            if found is not None:
                return found
            plex = self._plex_films_of.get(film_item(title.tmdb_id or 0))
            if plex is not None and self._plex.connected:
                log.info("Play: no file for a folder film: Plex's copy")
                return await self.film_video(plex)
            log.info("Play: no file for folder film %s", logged_key(title.key))
            return PlayOutcome(title.title, "movie", problem=PlayProblem.NOT_FOUND, folders=True)
        if not self._plex.connected:
            return PlayOutcome(title.title, "movie", problem=PlayProblem.NOT_CONNECTED)
        try:
            video = await self._plex.film_video(title.key)
        except PlexError as exc:
            log.info("Play: film %s didn't read (%s)", logged_key(title.key), type(exc).__name__)
            return PlayOutcome(title.title, "movie", problem=PlayProblem.PLEX_AWAY)
        if video is None:
            log.info("Play: Plex names no file for film %s", logged_key(title.key))
            return PlayOutcome(title.title, "movie", problem=PlayProblem.NO_FILE)
        film_tmdb_id = title.tmdb_id or self._film_tmdb_ids.get(title.key)
        if not video.watched and film_tmdb_id is not None:
            if film_item(film_tmdb_id) in await self._watched_items():  # watched from the folders
                video = video.model_copy(update={"watched": True})
        return video

    async def play_film(self, title: PlexTitle) -> PlayOutcome:
        """Play a film: Plex says its file (`film_video`), then as `play`."""
        video = await self.film_video(title)
        if isinstance(video, PlayOutcome):
            return video
        return await self.play(title, video)

    async def play(self, title: PlexTitle, video: PlexVideo) -> PlayOutcome:
        """Find `video`'s file from this PC in the title's library folder and open it."""
        found = await self.locate(title, video)
        if isinstance(found, PlayOutcome):
            return found
        return await self.launch(found)

    async def locate(self, title: PlexTitle, video: PlexVideo) -> Located | PlayOutcome:
        """Where `video`'s file is from this PC, in the title's library folder; the PlayOutcome
        saying why not when it can't be found. A folder video not there from this PC (moved into
        Plex's folder by hand, a sleeping NAS) is Plex's copy when Plex has one (`Located.video`:
        it plays as Plex's), else the folders' "can't find it"."""
        kind = title.kind
        name = title.title if kind == "movie" else episode_name(title.title, video)
        if not is_folder_video(video):  # per video: a show's window plays from both places
            return await self._locate_plex(name, kind, video)
        found = await self._locate_copy(name, kind, video)
        if isinstance(found, Located) or not self._plex.connected:
            return found
        plex = await self._plex_copy(video)
        if plex is None:
            return found
        log.info("Play: a %s not in the folders: Plex's copy", kind)
        other = await self._locate_plex(name, kind, plex)
        if isinstance(other, PlayOutcome):
            return found
        return Located(other.name, other.kind, other.path, video=plex)

    async def _plex_copy(self, video: PlexVideo) -> PlexVideo | None:
        """Plex's copy of a folder video (an episode from the last `episodes`, a film from the
        last `film`), None when Plex has none or doesn't say."""
        copy = self._plex_episodes_of.get(video.key)
        if copy is not None:
            return copy
        film = self._plex_films_of.get(video.key)
        if film is None:
            return None
        try:
            return await self._plex.film_video(film.key)
        except PlexError as exc:
            log.info("Play: Plex's copy of a film didn't read (%s)", type(exc).__name__)
            return None

    async def _locate_plex(
        self, name: str, kind: LibraryKind, video: PlexVideo
    ) -> Located | PlayOutcome:
        """A Plex video's file from this PC: Plex's own file name when it is a Windows path
        (Plex on this PC), then each of the user's own folders of the kind (Settings →
        Folders), then MediaForge's Series / Movies folder; all looked at together within the
        time limit (`_first_found`)."""
        if not self._plex.connected:
            return PlayOutcome(name, kind, problem=PlayProblem.NOT_CONNECTED)
        own = [folder for folder in self._own_folders(kind) if folder.strip()]
        forge = await self._folders()
        forge_folder = forge.of(kind).strip() if forge is not None else ""
        folders = list(own)
        if forge_folder and forge_folder.casefold() not in {one.casefold() for one in own}:
            folders.append(forge_folder)
        groups: list[list[str]] = [[video.file]] if windows_file(video.file) else []
        if folders:
            locations = await self._locations()
            groups += [local_candidates(video.file, locations, folder) for folder in folders]
        found, timed_out = await self._first_found(groups)
        if found is not None:
            group, index = found
            log.info(
                "Play: %s %s found (folder %d of %d, place %d)",
                kind, logged_key(video.key), group + 1, len(groups), index + 1,
            )
            return Located(name, kind, groups[group][index])
        log.info(
            "Play: %s %s not found from this PC (%d folder(s), %d places tried%s)",
            kind, logged_key(video.key), len(groups), sum(len(one) for one in groups),
            ", one didn't answer in time" if timed_out else "",
        )
        if timed_out:
            return PlayOutcome(name, kind, problem=PlayProblem.TIMED_OUT)
        if not own and forge is None:
            return PlayOutcome(name, kind, problem=PlayProblem.FORGE_OFF)
        if not own and not forge_folder:
            return PlayOutcome(name, kind, problem=PlayProblem.NO_FOLDER)
        return PlayOutcome(name, kind, problem=PlayProblem.NOT_FOUND)

    async def _locations(self) -> list[str]:
        """The Plex libraries' folders as the server sees them; none when they don't read (the
        file's last names may still find it)."""
        try:
            return [path for _key, path in await self._plex.locations()]
        except PlexUnreachable:
            return []
        except PlexError as exc:
            log.info("Play: the libraries' folders didn't read (%s)", type(exc).__name__)
            return []

    async def _first_found(
        self, groups: Sequence[Sequence[str]]
    ) -> tuple[tuple[int, int] | None, bool]:
        """The first group (a folder's places, in order) holding a file that is there, and
        that one's index in it; and whether a group didn't answer in time. Each group is looked
        at in its own thread, all at once (a share asleep holds only its own), within
        `timeout_s` for them all: a group wins once every group before it has answered without
        one; at the time limit, the first that found one of those that answered."""
        groups = [group for group in groups if group]
        if not groups:
            return None, False
        loop = asyncio.get_running_loop()
        looks = [
            asyncio.ensure_future(asyncio.to_thread(_first_existing, list(group), self._exists))
            for group in groups
        ]
        deadline = loop.time() + self._timeout_s
        try:
            while True:
                for number, look in enumerate(looks):
                    if not look.done():
                        break  # an earlier folder may still have it
                    index = _hit(look)
                    if index is not None:
                        return (number, index), False
                else:
                    return None, False  # every folder answered: none has it
                left = deadline - loop.time()
                if left <= 0:
                    break
                pending = [look for look in looks if not look.done()]
                await asyncio.wait(pending, timeout=left, return_when=asyncio.FIRST_COMPLETED)
            for number, look in enumerate(looks):
                index = _hit(look)
                if index is not None:
                    return (number, index), True
            return None, True
        finally:
            for look in looks:
                look.cancel()  # a thread still looking runs on; its answer is dropped

    async def _locate_copy(
        self, name: str, kind: LibraryKind, video: PlexVideo
    ) -> Located | PlayOutcome:
        """A folder library video's file: the best of its copies still there from this PC (the
        paths as they are), each looked at in its own thread within the time limit
        (`_first_found`); none there while one didn't answer is TIMED_OUT."""
        library = self._folder_library()
        paths = await library.copies(video) if library is not None else []
        found, timed_out = await self._first_found([[path] for path in paths])
        index = found[0] if found is not None else None
        if index is None:
            log.info(
                "Play: %s %s not found in the folders (%d copies tried%s)",
                kind, logged_key(video.key), len(paths),
                ", one didn't answer in time" if timed_out else "",
            )
            problem = PlayProblem.TIMED_OUT if timed_out else PlayProblem.NOT_FOUND
            return PlayOutcome(name, kind, problem=problem, folders=True)
        log.info(
            "Play: %s %s found in the folders (copy %d)", kind, logged_key(video.key), index + 1
        )
        return Located(name, kind, paths[index])

    async def launch(self, located: Located) -> PlayOutcome:
        """Open a located file in VLC or Windows' own player (3.14.1's ▶ Play)."""
        try:
            player = await self._launch(located.path)
        except OSError as exc:
            log.warning("Play: the player didn't start (%s)", type(exc).__name__)
            return PlayOutcome(located.name, located.kind, problem=PlayProblem.NO_PLAYER)
        log.info("Play: the %s opened (%s)", located.kind, player)
        return PlayOutcome(located.name, located.kind, player=player)
