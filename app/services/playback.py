"""▶ Play: a film or an episode from Plex opened in VLC on this PC (the info windows' "▶ Play").

Plex runs on another machine (a NAS); this PC reaches the same files through MediaForge's Series
and Movies folders, which are network shares (Forge's settings `series_folder` /
`movies_folder`, read through the Forge runtime like app/services/download_status.py does: no SQL
across the databases, Forge's types only under TYPE_CHECKING).

1. Plex says which file a title is (PlexService.film_video / show_videos): the server's path,
   such as /volume1/video/Movies/Dune (2021)/Dune (2021).mkv.
2. `local_candidates` turns it into the paths Windows may see: the library folder (one of
   PlexService.locations) that holds the file gives the part below it, joined onto the Series
   folder (shows) or the Movies folder (films); then the part below the folder's own ending in
   the file's path (`plex_folder`'s idea the other way round); then the file's last TAIL_PARTS
   names under that folder. The first that exists is played; the checks run in a thread (a NAS can hang) with
   FIND_TIMEOUT_S.
3. `launch_video` starts VLC when it is installed (`find_vlc`: Program Files, Program Files
   (x86), then the PATH), else Windows' own player (`os.startfile`). The real starts are only
   `_start_process` / `_open_with_windows`, which tests/__init__.py refuses.

`play` is `locate` (steps 1-2: a `Located` file, or the PlayOutcome saying why not) and then
`launch` (step 3). ▶ Play (app/ui/play_control.py) reads a film's video with `film_video` and a
show's with `episodes`, and hands it to AirGrabber's own player (app/ui/media_player.py), which
uses `locate` alone and `launch` only when it falls back to VLC.

What goes wrong comes back as a PlayOutcome whose `text` says so in plain words for the status
bar: Plex didn't answer, MediaForge is off or its folder isn't set, the file isn't in that folder
from this PC. Logs give ids and counts only: never a path, file name or title.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from typing import TYPE_CHECKING, Literal

from app.i18n import N_, t
from app.models import Show
from app.models.plex import LibraryKind, PlexTitle, PlexVideo
from app.services.plex import PlexError, PlexService, PlexUnreachable, episode_order

if TYPE_CHECKING:  # Forge's runtime: only typed here, nothing of Forge is imported
    from app.forge.runtime import ForgeRuntime

log = logging.getLogger(__name__)

TAIL_PARTS = (3, 2)  # "Show/Season 01/file.mkv", "Dune (2021)/Dune (2021).mkv"
FIND_TIMEOUT_S = 20.0  # a share that takes longer counts as not holding the file this time
VLC_PLACES = (
    ("ProgramFiles", r"VideoLAN\VLC\vlc.exe"),
    ("ProgramFiles(x86)", r"VideoLAN\VLC\vlc.exe"),
)

Player = Literal["vlc", "default"]

FINDING_TEXT = N_("▶ Finding {name}…")
PLAYING_VLC_TEXT = N_("▶ Playing {name} in VLC.")
PLAYING_DEFAULT_TEXT = N_("▶ Playing {name} in your video player.")
NOT_CONNECTED_TEXT = N_("Connect Plex in Settings → Plex first.")
PLEX_AWAY_TEXT = N_("Plex didn't answer, so try ▶ Play again in a moment.")
NO_FILE_TEXT = N_("Plex doesn't say which file {name} is.")
FORGE_OFF_TEXT = N_(
    "MediaForge is off, so AirGrabber doesn't know your Series and Movies folders."
)
NO_SERIES_TEXT = N_("Choose your Series folder in Settings › Auto-rename, so ▶ Play can find {name}.")
NO_MOVIES_TEXT = N_("Choose your Movies folder in Settings › Auto-rename, so ▶ Play can find {name}.")
NOT_IN_SERIES_TEXT = N_(
    "{name} isn't in your Series folder from this PC: check the Series folder in Settings › Auto-rename."
)
NOT_IN_MOVIES_TEXT = N_(
    "{name} isn't in your Movies folder from this PC: check the Movies folder in Settings › Auto-rename."
)
NO_PLAYER_TEXT = N_("AirGrabber couldn't start a video player for {name}.")


class PlayProblem(StrEnum):
    NOT_CONNECTED = "not_connected"
    PLEX_AWAY = "plex_away"  # Plex didn't answer (or said no)
    NO_FILE = "no_file"  # Plex names no file
    FORGE_OFF = "forge_off"  # MediaForge isn't running: its folders can't be read
    NO_FOLDER = "no_folder"  # its Series / Movies folder isn't set
    NOT_FOUND = "not_found"  # not in that folder from this PC
    NO_PLAYER = "no_player"  # starting the player failed


@dataclass(frozen=True, slots=True)
class PlayOutcome:
    """What ▶ Play did: played in `player`, or the `problem`; `name` is what the status bar
    names ("Dune", "The Ark S02E03"), `kind` which library folder counted."""

    name: str
    kind: LibraryKind
    player: Player | None = None
    problem: PlayProblem | None = None

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
            case PlayProblem.NOT_FOUND:
                text = NOT_IN_SERIES_TEXT if shows else NOT_IN_MOVIES_TEXT
            case PlayProblem.NO_PLAYER:
                text = NO_PLAYER_TEXT
        return t(text, name=self.name)


@dataclass(frozen=True, slots=True)
class Located:
    """Where `locate` found a video's file from this PC (`path`: never logged), with the status
    bar's `name` ("Dune", "The Ark S02E03") and the library `kind` that counted."""

    name: str
    kind: LibraryKind
    path: str


@dataclass(frozen=True, slots=True)
class LibraryFolders:
    """MediaForge's Series and Movies folders as this PC sees them ("" = not set)."""

    series: str = ""
    movies: str = ""

    def of(self, kind: LibraryKind) -> str:
        return self.series if kind == "show" else self.movies


FolderLookup = Callable[[], Awaitable[LibraryFolders | None]]
Launcher = Callable[[str], Awaitable[Player]]


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
    there are no others); when all are watched, the first."""
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


class PlaybackService:
    """`services.playback`: which Plex title an info window shows, and ▶ Play."""

    def __init__(
        self,
        plex: PlexService,
        folders: FolderLookup,
        *,
        launch: Launcher = launch_video,
        exists: Callable[[str], bool] = os.path.isfile,
        timeout_s: float = FIND_TIMEOUT_S,
    ) -> None:
        """`launch` / `exists`: the seams tests replace (never a real player)."""
        self._plex = plex
        self._folders = folders
        self._launch = launch
        self._exists = exists
        self._timeout_s = timeout_s

    @property
    def available(self) -> bool:
        """Plex is connected."""
        return self._plex.connected

    async def film(self, tmdb_id: int | None, imdb_id: str | None = None) -> PlexTitle | None:
        """The film in Plex with this TMDB id (else IMDb id), from the last check."""
        if tmdb_id is None and imdb_id is None:
            return None
        for title in await self._plex.titles():
            if title.kind == "movie" and (
                (tmdb_id is not None and title.tmdb_id == tmdb_id)
                or (imdb_id is not None and title.imdb_id == imdb_id)
            ):
                return title
        return None

    async def show(self, show: Show) -> PlexTitle | None:
        """The show in Plex for a TVMaze show: by the TVMaze id the check matched it to, else
        its TheTVDB id, else its IMDb id (as app/services/plex.py's `match_scan` matches)."""
        shows = [title for title in await self._plex.titles() if title.kind == "show"]
        by_id = [title for title in shows if title.show_id == show.id]
        by_tvdb = [one for one in shows if show.tvdb_id is not None and one.tvdb_id == show.tvdb_id]
        by_imdb = [one for one in shows if show.imdb_id is not None and one.imdb_id == show.imdb_id]
        found = by_id or by_tvdb or by_imdb
        # Plex may keep one show as two titles (files it matched apart): the one with most.
        return max(found, key=lambda title: title.episodes or 0) if found else None

    async def episodes(self, title: PlexTitle) -> list[PlexVideo]:
        """A show's episodes with their files (the picker), in order: those of every title Plex
        keeps the show as (`same_show`; it splits one when some files got matched apart), an
        episode in two of them once: the copy of the title with the most. Raises PlexError."""
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
        PlayOutcome saying why not when Plex can't say."""
        if not self._plex.connected:
            return PlayOutcome(title.title, "movie", problem=PlayProblem.NOT_CONNECTED)
        try:
            video = await self._plex.film_video(title.key)
        except PlexError as exc:
            log.info("Play: film %s didn't read (%s)", title.key, type(exc).__name__)
            return PlayOutcome(title.title, "movie", problem=PlayProblem.PLEX_AWAY)
        if video is None:
            log.info("Play: Plex names no file for film %s", title.key)
            return PlayOutcome(title.title, "movie", problem=PlayProblem.NO_FILE)
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
        saying why not when it can't be found."""
        kind = title.kind
        name = title.title if kind == "movie" else episode_name(title.title, video)
        if not self._plex.connected:
            return PlayOutcome(name, kind, problem=PlayProblem.NOT_CONNECTED)
        folders = await self._folders()
        if folders is None:
            return PlayOutcome(name, kind, problem=PlayProblem.FORGE_OFF)
        folder = folders.of(kind)
        if not folder.strip():
            return PlayOutcome(name, kind, problem=PlayProblem.NO_FOLDER)
        try:
            locations = [path for _key, path in await self._plex.locations()]
        except PlexUnreachable:
            locations = []  # the file's last names may still find it
        except PlexError as exc:
            log.info("Play: the libraries' folders didn't read (%s)", type(exc).__name__)
            locations = []
        candidates = local_candidates(video.file, locations, folder)
        try:
            async with asyncio.timeout(self._timeout_s):
                index = await asyncio.to_thread(_first_existing, candidates, self._exists)
        except TimeoutError:
            log.info("Play: the %s folder didn't answer in time", kind)
            index = None
        if index is None:
            log.info(
                "Play: %s %s not found from this PC (%d places tried)",
                kind, video.key, len(candidates),
            )
            return PlayOutcome(name, kind, problem=PlayProblem.NOT_FOUND)
        log.info("Play: %s %s found (place %d)", kind, video.key, index + 1)
        return Located(name, kind, candidates[index])

    async def launch(self, located: Located) -> PlayOutcome:
        """Open a located file in VLC or Windows' own player (3.14.1's ▶ Play)."""
        try:
            player = await self._launch(located.path)
        except OSError as exc:
            log.warning("Play: the player didn't start (%s)", type(exc).__name__)
            return PlayOutcome(located.name, located.kind, problem=PlayProblem.NO_PLAYER)
        log.info("Play: the %s opened (%s)", located.kind, player)
        return PlayOutcome(located.name, located.kind, player=player)
