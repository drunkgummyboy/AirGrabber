"""The Downloads page: the recent grabs with what the torrent program says about them, and the
free space where finished downloads end up.

DownloadStatusService (`services.download_status`) keeps the latest DownloadsOverview and tells
its listeners (the page, the header button's badge) about every new one. `run_periodic` checks
every 10 s while the page shows (`set_visible`), every minute while something is on its way
(for the badge), else every 30 minutes, and a few seconds after every grab.

Progress comes from the torrent program on a NAS or another computer (Download Station,
qBittorrent, Transmission: `RemoteDownloads.status`) whenever one is set up and the torrent
program on this PC isn't the one in use: in REMOTE mode, and in FOLDER mode, where the NAS's
Download Station picks the .torrent files up from the watch folder. Grabs are matched to its
tasks by info hash, then by the .torrent's file name, then by name (`match_tasks`).

In sync with the program (3.0.2, the rules in app/services/download_sync.py): whenever its task
list can be read (`DownloadsOverview.synced`), the page lists every task it has. The recent
grabs take theirs first; the tasks left are matched to the rest of the history (an older grab,
or one taken off the list before 3.0.2: `_match_older`), and those left after that were added
in the program itself (`TaskEntry`, recognised by the followed shows and the films AirGrabber
knows: `known`). A grab the program has had (`seen_downloads`) and no longer has leaves the
page (a cleared mark, once `GoneGrabs` confirms); one it never had is PENDING for an hour, then
NOT_FOUND ("never picked it up"). The ⋯ menu's pause / resume / remove and "Clear finished"
(`pause`, `resume`, `remove`, `clear_finished`) show at once (`_Pending` over the last check)
and are confirmed by the next check; a failure puts the rows back and returns the plain reason.
AirGrabber's qBittorrent on this PC isn't asked by a background check that has nothing to ask
about (asking starts it).

Free space: `shutil.disk_usage` of "Where finished downloads end up" (`subtitles_folder`), else
of the watch folder in FOLDER mode (with a films folder of its own, the fuller of the two), in a
worker thread with a time limit (a share that doesn't
answer never blocks the loop, and no second check starts while one hangs); else what
qBittorrent / Transmission report. Below LOW_SPACE_BYTES, or below LOW_SPACE_FRACTION while
also below LOW_SPACE_CAP_BYTES (a big disk with plenty of room never warns), the space is
low: the page shows a banner and `subscribe_space` listeners hear it at most once a day.

The page can take grabs off its list ("Clear finished", "Remove from list": `clear`): marked in
the database (HistoryRepository.clear), never deleted from the history and never their files.
`locate` finds a grab's video in "Where finished downloads end up" (app/services/
subtitle_files.py's scan, in a thread with a time limit, kept LOCATE_TTL_S) for "Open the folder"
(`open_folder`).

Where MediaForge put a finished grab (once Forge runs; `forge`, see `forge_moves`): its move
records (`ForgeMoveService.moves_from`) out of "Where finished downloads end up" and Forge's
watched folders are matched to the grabs (`match_moves`: a film by its TMDB id first, then by
release name with subtitle_files' `grab_files` rules) and set as `DownloadEntry.moved_to`;
`locate` then gives that folder. Such a grab shows as finished even when its program reports a
problem for the file that is gone, or no longer has the task (`moved_state`). The records are
read again when Forge reports a change to them (`subscribe`), at the latest after MOVES_TTL_S.
Without Forge every entry's `moved_to` stays None.

Nothing here logs a URL, magnet, password or release object; RemoteError texts are plain words.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import functools
import logging
import os
import re
import shutil
import sys
import time
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, Protocol

from app.database import HistoryRepository
from app.i18n import t
from app.models import (
    DownloadEntry,
    DownloadMode,
    DownloadRecord,
    DownloadsOverview,
    DownloadState,
    FreeSpace,
    GrabOutcome,
    ListedDownload,
    MediaType,
    Movie,
    RemoteKind,
    RemoteStatus,
    RemoteTask,
    Settings,
    Show,
    TaskEntry,
)
from app.models.downloads import MovedTo
from app.services.download_sync import GoneGrabs, KnownTitles, Recognition, pickup_state
from app.services.downloads import safe_filename
from app.services.remote import RemoteError
from app.services.remote_downloads import RemoteDownloads
from app.services.settings import SettingsService
from app.services.subtitle_files import (
    FolderScan,
    VideoFile,
    grab_files,
    normalize,
    release_name,
    scan_folder,
)

if TYPE_CHECKING:  # Forge's models and runtime: only typed here, nothing of Forge is imported
    from app.forge.models.moves import MoveRecord
    from app.forge.runtime import ForgeRuntime

log = logging.getLogger(__name__)

RECENT = timedelta(days=30)  # grabs listed on the page
RECENT_LIMIT = 100
VISIBLE_INTERVAL_S = 10.0  # while the page shows
BUSY_INTERVAL_S = 60.0  # page hidden, something on its way (the header badge)
IDLE_INTERVAL_S = 30 * 60.0  # page hidden, nothing on its way
AFTER_GRAB_S = 5.0  # a grab: look again this soon (the program has the task by then)
START_DELAY_S = 20.0  # the first check, once the pages have loaded
SPACE_INTERVAL_S = 6 * 3600.0  # free space while the page is hidden
SPACE_VISIBLE_INTERVAL_S = 5 * 60.0  # free space while the page shows
SPACE_TIMEOUT_S = 20.0  # a share that takes longer counts as out of reach this time
NOTICE_INTERVAL_S = 24 * 3600.0  # the status-bar notice about low space: once a day
LOW_SPACE_FRACTION = 0.05
GIB = 1024**3
LOW_SPACE_BYTES = 20 * GIB
LOW_SPACE_CAP_BYTES = 100 * GIB  # the 5 % rule only counts below this much free
_DRIVE_REMOTE = 4  # GetDriveTypeW: a network drive
LOCATE_TTL_S = 60.0  # a scan of the finished-downloads folder serves "Open the folder" this long
MOVES_TTL_S = 10 * 60.0  # Forge's move records are read again at the latest after this long
# A move counts for a grab when it came after the grab (this much earlier allowed: clocks).
MOVE_SLACK = timedelta(minutes=5)

REMOTE_PROGRAMS = frozenset(kind.program for kind in RemoteKind)
_YEAR_RE = re.compile(r"(?<!\d)(?:19|20)\d\d(?!\d)")
_TAG_RE = re.compile(r"\[[^\]]*\]|\{[^}]*\}")

KNOWN_TTL_S = 5 * 60.0  # the shows and films tasks are recognised by: read again after this
SHOW_TTL_S = 10 * 60.0  # a grab's show (its name, its poster), kept this long

ShowLookup = Callable[[int], Awaitable[Show | None]]
# The shows anyone follows and the films on anyone's watchlist (recognising tasks).
KnownLookup = Callable[[], Awaitable[tuple[Sequence[Show], Sequence[Movie]]]]
OverviewListener = Callable[[DownloadsOverview], None]
SpaceListener = Callable[[FreeSpace], None]


@dataclass(frozen=True, slots=True)
class _Pending:
    """A pause / resume / remove the page shows before the program confirms it: until a check
    that started after it finished (`done_seq`: the checks started by then; None: in flight)."""

    kind: Literal["pause", "resume", "remove"]
    done_seq: int | None = None


_PAUSED_STATES = frozenset({DownloadState.DOWNLOADING, DownloadState.WAITING})


def _pending_task(task: RemoteTask, pending: _Pending) -> RemoteTask:
    """A task as the page shows it while `pending` (pause / resume) is not confirmed."""
    if pending.kind == "pause" and task.state in _PAUSED_STATES:
        return task.model_copy(update={"state": DownloadState.STOPPED})
    if pending.kind == "resume" and task.state is DownloadState.STOPPED:
        return task.model_copy(update={"state": DownloadState.DOWNLOADING})
    return task


def _with_pending(
    overview: DownloadsOverview, pending: Mapping[str, _Pending], cleared: Collection[int]
) -> DownloadsOverview:
    """The overview as the page shows it: rows being removed (or taken off the list, when
    they have no task) left out, tasks being paused or resumed in their new state."""
    if not pending and not cleared:
        return overview

    def action(task: RemoteTask | None) -> _Pending | None:
        return pending.get(task.task_id) if task is not None and task.task_id else None

    entries: list[DownloadEntry] = []
    for entry in overview.entries:
        if entry.task is None and entry.record.id in cleared:
            continue
        found = action(entry.task)
        if found is not None and found.kind == "remove":
            continue
        if found is not None and entry.task is not None:
            task = _pending_task(entry.task, found)
            if task is not entry.task:
                entry = entry.model_copy(update={"task": task, "state": task.state})
        entries.append(entry)
    tasks: list[TaskEntry] = []
    for item in overview.tasks:
        found = action(item.task)
        if found is not None and found.kind == "remove":
            continue
        if found is not None:
            task = _pending_task(item.task, found)
            if task is not item.task:
                item = item.model_copy(update={"task": task})
        tasks.append(item)
    return overview.model_copy(update={"entries": tuple(entries), "tasks": tuple(tasks)})


class MoveRecords(Protocol):
    """What the page reads of Forge's move records (ForgeMoveService)."""

    async def moves_from(self, folder: Path) -> list[MoveRecord]: ...

    def subscribe(self, listener: Callable[[], None]) -> Callable[[], None]: ...


class ForgeMoves(NamedTuple):
    """Forge, once it runs: its move records and the folders that matter here."""

    records: MoveRecords
    watch_folders: tuple[str, ...]  # the folders Forge moves downloads out of
    library_folders: tuple[str, ...]  # the Series and Movies library folders


ForgeLookup = Callable[[], ForgeMoves | None]


def forge_moves(runtime: ForgeRuntime) -> ForgeLookup:
    """Forge's part for the Downloads page: None until it runs (and when it is off)."""

    def current() -> ForgeMoves | None:
        services = runtime.services
        if services is None:
            return None
        settings = services.settings.current
        return ForgeMoves(
            services.moves,
            tuple(settings.watch_folders),
            tuple(f for f in (settings.series_folder, settings.movies_folder) if f),
        )

    return current


class DiskUsage(NamedTuple):
    total: int
    used: int
    free: int


DiskUsageReader = Callable[[str], DiskUsage]
FolderScanner = Callable[[str], FolderScan]


class Located(NamedTuple):
    """Where a grab's video is (`folder`: the folder holding it), or why that isn't known:
    no finished-downloads folder in Settings, not found there, or the folder out of reach."""

    folder: str | None
    problem: Literal["", "no_folder", "not_found", "unreachable"] = ""


def _scan_folder(folder: str) -> FolderScan:
    return scan_folder(Path(folder), now=time.time())


def video_folder(paths: Sequence[str]) -> str | None:
    """The folder to open for a grab's videos: the one holding the first (a season pack's
    episodes share their release folder)."""
    return os.path.dirname(paths[0]) if paths else None


# -- pure rules ------------------------------------------------------------------------------


def grab_outcome(saved_path: str | None, mode: DownloadMode) -> tuple[GrabOutcome | None, str]:
    """How a grab was handed over, from what the history kept (`saved_path`: the .torrent file
    written, or the name of the program it went to) and, for a name both a program on this PC
    and one on a NAS can have, the download mode now. Returns (outcome, program name)."""
    if not saved_path:
        return None, ""
    if "/" in saved_path or "\\" in saved_path or saved_path.lower().endswith(".torrent"):
        return GrabOutcome.SAVED, ""
    remote_name = saved_path in REMOTE_PROGRAMS
    if remote_name and (mode is not DownloadMode.APP or saved_path == RemoteKind.SYNOLOGY.program):
        return GrabOutcome.SENT, saved_path
    return GrabOutcome.OPENED, saved_path


def progress_program(settings: Settings) -> str:
    """The program progress comes from ("Download Station"); "" when there is none to ask."""
    if settings.download_mode is DownloadMode.APP or not settings.remote_configured:
        return ""
    return settings.remote_kind.program


def expected_in_program(outcome: GrabOutcome | None, program: str, settings: Settings) -> bool:
    """Whether the program progress comes from should have this grab: it was sent there, or (in
    FOLDER mode) saved to the watch folder it picks .torrent files up from."""
    asked = progress_program(settings)
    if not asked:
        return False
    if outcome is GrabOutcome.SENT:
        return program == asked
    return outcome is GrabOutcome.SAVED and settings.download_mode is DownloadMode.FOLDER


@functools.lru_cache(maxsize=8192)
def _normal(text: str) -> str:
    """`normalize`, kept: the same task and grab names come by every 10 s."""
    return normalize(text)


@functools.lru_cache(maxsize=8192)
def _file_names(release_title: str, saved_path: str | None) -> tuple[str, ...]:
    return tuple(sorted(torrent_file_names_of(release_title, saved_path)))


def torrent_file_names_of(release_title: str, saved_path: str | None) -> frozenset[str]:
    names = {f"{safe_filename(release_title)}.torrent".lower()}
    saved = saved_path or ""
    if saved.lower().endswith(".torrent"):
        names.add(saved.replace("\\", "/").rsplit("/", 1)[-1].lower())
    return frozenset(names)


def match_tasks(
    records: Sequence[DownloadRecord], tasks: Sequence[RemoteTask]
) -> dict[int, RemoteTask]:
    """The program's task for each grab (by index in `records`), each task used once (the
    first unused one in `tasks`' order): by info hash first, then by .torrent file name, then
    by name. `records` newest first, so a newer grab of the same name wins. Each key is
    looked up in a table: hundreds of tasks against the whole history stay cheap."""
    used: set[int] = set()  # positions in `tasks`
    found: dict[int, RemoteTask] = {}

    def table(key: Callable[[RemoteTask], object]) -> dict[object, list[int]]:
        positions: dict[object, list[int]] = {}
        for position, task in enumerate(tasks):
            positions.setdefault(key(task), []).append(position)
        return positions

    def take(index: int, positions: dict[object, list[int]], wanted: object) -> bool:
        for position in positions.get(wanted, ()):
            if position not in used:
                used.add(position)
                found[index] = tasks[position]
                return True
        return False

    if not tasks or not records:
        return found
    by_hash = table(lambda task: task.info_hash)
    for index, record in enumerate(records):
        if record.info_hash:
            take(index, by_hash, record.info_hash)
    by_file = table(lambda task: (task.file_name or "").lower())
    for index, record in enumerate(records):
        if index not in found:
            for file_name in _file_names(record.release_title, record.saved_path):
                if take(index, by_file, file_name):
                    break
    by_name = table(lambda task: _normal(task.name))
    for index, record in enumerate(records):
        if index not in found and (name := _normal(record.release_title)):
            take(index, by_name, name)
    return found


def entry_state(
    task: RemoteTask | None, *, expected: bool, asked: bool
) -> tuple[DownloadState, float | None]:
    """A grab's state and progress: its task's; NOT_FOUND when the program answered without
    it; else UNKNOWN."""
    if task is not None:
        return task.state, task.progress
    if expected and asked:
        return DownloadState.NOT_FOUND, None
    return DownloadState.UNKNOWN, None


_GONE_AFTER_A_MOVE = frozenset(
    {DownloadState.PROBLEM, DownloadState.NOT_FOUND, DownloadState.PENDING}
)


def moved_state(
    state: DownloadState, progress: float | None, moved: MovedTo | None
) -> tuple[DownloadState, float | None]:
    """A grab MediaForge moved into the library is finished, whatever the torrent program
    makes of its files being gone: qBittorrent ("missingFiles"), Transmission (a local error)
    and Download Station ("error") report a problem for a task whose file was moved away while
    it seeded, and a grab it doesn't have (never had, by the page's records) was picked up
    after all. Any other state stays."""
    if moved is not None and state in _GONE_AFTER_A_MOVE:
        return DownloadState.READY, 1.0
    return state, progress


def episode_title(show_name: str, season: int, episode: int | None) -> str:
    """"Fallout S02E03"; a season pack "Fallout Season 2"."""
    if episode is None:
        return t("{show} Season {season}", show=show_name, season=season)
    return f"{show_name} S{season:02d}E{episode:02d}"


def movie_title(release_title: str) -> str:
    """"Gladiator II (2024)" from "Gladiator.II.2024.1080p.WEB-DL": the words before the last
    year that has words before it; else the release title with spaces."""
    text = _TAG_RE.sub(" ", re.sub(r"[._]+", " ", release_title))
    for match in reversed(list(_YEAR_RE.finditer(text))):
        title = " ".join(text[: match.start()].strip(" -([").split())
        if title:
            return f"{title} ({match.group(0)})"
    return " ".join(text.split()) or release_title


def space_is_low(free: int, total: int | None) -> bool:
    """Below LOW_SPACE_BYTES (20 GB) free, or below LOW_SPACE_FRACTION (5 %) of the disk while
    also below LOW_SPACE_CAP_BYTES (100 GB): a 16 TB NAS with 800 GB free is fine."""
    if free < LOW_SPACE_BYTES:
        return True
    if not total or free >= LOW_SPACE_CAP_BYTES:
        return False
    return free < total * LOW_SPACE_FRACTION


def space_folders(settings: Settings) -> tuple[str, ...]:
    """The folders whose free space counts: "Where finished downloads end up"; else in FOLDER
    mode the watch folder, and the films one when that is set apart; () for neither."""
    folder = settings.subtitles_folder.strip()
    if folder:
        return (folder,)
    if settings.download_mode is not DownloadMode.FOLDER:
        return ()
    folders = [str(settings.download_dir)]
    if settings.movie_download_dir is not None:
        folders.append(str(settings.movie_download_dir))
    return tuple(dict.fromkeys(folders))


def is_network_path(path: str) -> bool:
    """A UNC path (\\\\nas\\share), or a drive letter Windows has mapped to a network share."""
    text = path.strip()
    if text.startswith(("\\\\", "//")):
        return True
    drive = os.path.splitdrive(text)[0]
    if sys.platform != "win32" or len(drive) != 2 or drive[1] != ":":
        return False
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return False
    try:
        return int(windll.kernel32.GetDriveTypeW(f"{drive}\\")) == _DRIVE_REMOTE
    except (AttributeError, OSError):
        return False


# -- where MediaForge moved a grab -------------------------------------------------------------


def _key(path: Path) -> tuple[str, ...]:
    """A path as Windows compares it, folder by folder (case, slashes)."""
    return tuple(os.path.normcase(part).rstrip("\\/") for part in path.parts)


def _below(path: Path, folder: Path) -> tuple[str, ...] | None:
    """`path`'s parts below `folder` (whole folder names); None when it isn't below it."""
    mine, theirs = _key(path), _key(folder)
    if len(mine) <= len(theirs) or mine[: len(theirs)] != theirs:
        return None
    return path.parts[len(theirs) :]


def move_folders(finished: str, watched: Iterable[str]) -> tuple[Path, ...]:
    """The folders Forge's moves are read from: "Where finished downloads end up" and Forge's
    watched folders, each once, a folder inside another one left out (its moves come with it)."""
    folders: list[Path] = []
    for text in (finished, *watched):
        if text.strip():
            path = Path(text.strip())
            if not any(_key(path) == _key(known) for known in folders):
                folders.append(path)
    return tuple(
        folder
        for folder in folders
        if not any(other is not folder and _below(folder, other) for other in folders)
    )


def shown_folder(folder: Path, library_folders: Sequence[str]) -> tuple[str, ...]:
    """What a row shows of the folder a video was moved to: the library folder's base name
    onward ("Series", "Gold Rush", "Season 17"); for a folder in no library folder (the
    library folders changed since), its own and its parent's name. Never the path before it
    (it may hold the user's name)."""
    roots = sorted((Path(text) for text in library_folders), key=lambda p: -len(p.parts))
    for root in roots:
        if _key(folder) == _key(root):
            below: tuple[str, ...] = ()
        elif (found := _below(folder, root)) is not None:
            below = found
        else:
            continue
        base = _base_name(root)
        return (base, *below) if base else below
    names = folder.parts[1:] if folder.anchor else folder.parts
    return tuple(names[-2:]) if names else ((_base_name(folder),) if _base_name(folder) else ())


def _base_name(folder: Path) -> str:
    r"""A folder's own name; for a share or drive itself its last name (\\nas\video → video)."""
    return folder.name or folder.anchor.replace("/", "\\").rstrip("\\").rsplit("\\", 1)[-1]


def _aware(moment: datetime) -> datetime:
    """Forge's records hold local time without a zone; the history holds UTC."""
    return moment if moment.tzinfo is not None else moment.astimezone(UTC)


def match_moves(
    records: Sequence[DownloadRecord],
    moves: Sequence[MoveRecord],
    *,
    show_names: Mapping[int, str],
    folders: Sequence[Path],
) -> dict[int, MoveRecord]:
    """The move that shows where each grab ended up (by index in `records`).

    `records` newest first, `moves` newest first. A move counts for a grab only when it came
    after the grab (MOVE_SLACK allowed) and is of its kind. First a film by its TMDB id (the
    grab's and the move's). Then, for the grabs left, by release name with subtitle_files'
    `grab_files` rules (an episode also by its show's name and SxxEyy), the move's source read
    below the folder (`folders`) it came out of. A single grab takes one move, each move used
    once; a season pack takes every move of its season and shows the newest.
    """
    used: set[int] = set()
    found: dict[int, MoveRecord] = {}

    def fits(record: DownloadRecord, move: MoveRecord) -> bool:
        kind = "movie" if record.media_type is MediaType.MOVIE else "episode"
        return move.kind == kind and _aware(move.moved_at) >= record.downloaded_at - MOVE_SLACK

    def take(index: int, record: DownloadRecord, picked: list[int]) -> None:
        if not picked:
            return
        pack = record.media_type is MediaType.TV and record.episode is None
        chosen = picked if pack else picked[:1]
        used.update(chosen)
        found[index] = moves[chosen[0]]

    for index, record in enumerate(records):
        if record.media_type is not MediaType.MOVIE:
            continue
        picked = [
            position
            for position, move in enumerate(moves)
            if position not in used and fits(record, move) and move.tmdb_id == record.media_id
        ]
        take(index, record, picked)

    left = [index for index in range(len(records)) if index not in found]
    if not left:
        return found
    videos: list[VideoFile] = []
    positions: dict[int, int] = {}  # id(video) → its move's position
    for position, move in enumerate(moves):
        parts = next(
            (below for folder in folders if (below := _below(move.source, folder))), None
        )
        video = VideoFile(path=str(move.source), parts=parts or move.source.parts[-2:], size=0)
        videos.append(video)
        positions[id(video)] = position
    for index in left:
        record = records[index]
        unused = [video for video in videos if positions[id(video)] not in used]
        if not unused:
            break
        files = grab_files(
            record,
            FolderScan(videos=unused),
            show_name=show_names.get(record.media_id)
            if record.media_type is MediaType.TV
            else None,
        )
        picked = [
            positions[id(grab.video)]
            for grab in files.videos
            if fits(record, moves[positions[id(grab.video)]])
        ]
        take(index, record, sorted(picked))
    return found


def match_moves_by_name(
    records: Sequence[DownloadRecord],
    moves: Sequence[MoveRecord],
    *,
    folders: Sequence[Path],
) -> dict[int, MoveRecord]:
    """`match_moves` for the older grabs an old task in the program belongs to (there can be
    hundreds): a film by its TMDB id, any grab by a folder or file named like its release
    (each move's names read once, then looked up). The same timing and kind rules; each move
    once, `records` newest first; a season pack takes its moves and shows the newest."""
    names: dict[str, list[int]] = {}
    films: dict[int, list[int]] = {}
    for position, move in enumerate(moves):
        if move.kind == "movie" and move.tmdb_id is not None:
            films.setdefault(move.tmdb_id, []).append(position)
        parts = next(
            (below for folder in folders if (below := _below(move.source, folder))), None
        )
        for part in parts or move.source.parts[-2:]:
            if key := release_name(part):
                names.setdefault(key, []).append(position)
    used: set[int] = set()
    found: dict[int, MoveRecord] = {}
    for index, record in enumerate(records):
        movie = record.media_type is MediaType.MOVIE
        kind = "movie" if movie else "episode"
        candidates = set(films.get(record.media_id, ())) if movie else set()
        candidates.update(names.get(_normal(record.release_title), ()))
        picked = sorted(
            position
            for position in candidates
            if position not in used
            and moves[position].kind == kind
            and _aware(moves[position].moved_at) >= record.downloaded_at - MOVE_SLACK
        )
        if not picked:
            continue
        pack = record.media_type is MediaType.TV and record.episode is None
        chosen = picked if pack else picked[:1]
        used.update(chosen)
        found[index] = moves[chosen[0]]
    return found


def moved_to(move: MoveRecord, library_folders: Sequence[str]) -> MovedTo:
    folder = move.target.parent
    return MovedTo(folder=str(folder), shown=shown_folder(folder, library_folders))


def _read_disk_usage(path: str) -> DiskUsage:
    usage = shutil.disk_usage(path)
    return DiskUsage(usage.total, usage.used, usage.free)


# -- the service -----------------------------------------------------------------------------


class DownloadStatusService:
    def __init__(
        self,
        history: HistoryRepository,
        settings: SettingsService,
        remote: RemoteDownloads,
        *,
        show: ShowLookup,
        disk_usage: DiskUsageReader = _read_disk_usage,
        network_path: Callable[[str], bool] = is_network_path,
        folder_scanner: FolderScanner = _scan_folder,
        opener: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        forge: ForgeLookup | None = None,
        known: KnownLookup | None = None,
    ) -> None:
        """`forge`: Forge's move records once it runs (`forge_moves`), for where grabs ended
        up. `known`: the shows anyone follows and the films on anyone's watchlist, which the
        tasks added in the torrent program are recognised by (none without it)."""
        self._forge = forge
        self._known_lookup = known
        self._known_at: float | None = None  # when the known titles were read
        self._known_stamp: tuple[int, int] | None = None  # … with the history as it was then
        self._recognition = Recognition()
        self._grabs: tuple[tuple[int, int], list[DownloadRecord]] | None = None  # every grab
        self._show_cache: dict[int, tuple[float, Show | None]] = {}
        self._gone = GoneGrabs()
        self._check_seq = 0  # checks started, for the actions' optimistic states
        self._pending: dict[str, _Pending] = {}  # task id -> an action not confirmed yet
        self._raw: DownloadsOverview | None = None  # the last check, without `_pending`
        self._last_status: tuple[str, RemoteStatus] | None = None  # (program, its answer)
        self._moves: tuple[tuple[str, ...], float, list[MoveRecord]] | None = None
        self._watched: MoveRecords | None = None  # the move records listened to
        self._unwatch: Callable[[], None] | None = None
        self._history = history
        self._settings = settings
        self._remote = remote
        self._show = show
        self._disk_usage = disk_usage
        self._network_path = network_path
        self._folder_scanner = folder_scanner
        self._opener = opener if opener is not None else _open_folder
        self._scan: tuple[str, float, FolderScan] | None = None  # (folder, when, scan)
        self._cleared: set[int] = set()  # taken off this run (a check in flight may list them)
        self._clock = clock
        self._now = now
        self._listeners: list[OverviewListener] = []
        self._space_listeners: list[SpaceListener] = []
        self.latest: DownloadsOverview | None = None
        self._visible = False
        self._wake = asyncio.Event()
        self._check_task: asyncio.Task[DownloadsOverview] | None = None
        self._space: FreeSpace | None = None
        self._space_at: float | None = None
        self._disk_job: asyncio.Future[tuple[DiskUsage, bool]] | None = None
        self._noticed_at: float | None = None

    # -- listeners ---------------------------------------------------------------------------

    def subscribe(self, listener: OverviewListener) -> Callable[[], None]:
        """Call `listener` with every new overview. Returns a function that unsubscribes it."""
        return _subscribe(self._listeners, listener)

    def subscribe_space(self, listener: SpaceListener) -> Callable[[], None]:
        """Call `listener` when the space is low, at most once a day (the status bar)."""
        return _subscribe(self._space_listeners, listener)

    # -- when to check -----------------------------------------------------------------------

    def set_visible(self, visible: bool) -> None:
        """The page shows (check now, then every 10 s) or not."""
        changed = visible != self._visible
        self._visible = visible
        if visible and changed:
            self._wake.set()

    def after_grab(self) -> None:
        """A grab was made (DownloadService.subscribe): look again in a few seconds."""
        try:
            asyncio.get_running_loop().call_later(AFTER_GRAB_S, self._wake.set)
        except RuntimeError:
            self._wake.set()

    def interval(self) -> float:
        """Seconds until the next check."""
        if self._visible:
            return VISIBLE_INTERVAL_S
        if self.latest is not None and self.latest.in_progress:
            return BUSY_INTERVAL_S
        return IDLE_INTERVAL_S

    async def run_periodic(self, *, start_delay_s: float = START_DELAY_S) -> None:
        """Check now and then for as long as AirGrabber runs (MainWindow starts it)."""
        await self._sleep(start_delay_s)
        while True:
            self._wake.clear()
            try:
                await self.check()
            except Exception:
                log.exception("Checking the downloads failed")
            await self._sleep(self.interval())

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            async with asyncio.timeout(seconds):
                await self._wake.wait()

    # -- checking ----------------------------------------------------------------------------

    async def check(self) -> DownloadsOverview:
        """A fresh overview (one check at a time: a caller during a check gets its result)."""
        task = self._check_task
        if task is None or task.done():
            task = asyncio.get_running_loop().create_task(self._check(), name="download-status")
            task.add_done_callback(_retrieve)
            self._check_task = task
        return await asyncio.shield(task)

    async def _check(self) -> DownloadsOverview:
        self._check_seq += 1
        seq = self._check_seq
        settings = self._settings.current  # one snapshot for the whole check
        now = self._now()
        since = now - RECENT
        records = [
            record
            for record in await self._history.listed(RECENT_LIMIT)
            if record.downloaded_at >= since
            and record.id is not None
            and record.id not in self._cleared
        ]
        outcomes = [grab_outcome(record.saved_path, settings.download_mode) for record in records]
        expected = [
            expected_in_program(outcome, program, settings) for outcome, program in outcomes
        ]
        program = progress_program(settings)
        asked = [index for index, wanted in enumerate(expected) if wanted]
        status, problem = await self._status(program, expected=bool(asked))
        synced = status is not None
        matches: dict[int, RemoteTask] = {}
        older: list[tuple[DownloadRecord, RemoteTask]] = []
        added: list[RemoteTask] = []
        if status is not None:
            found = match_tasks([records[index] for index in asked], status.tasks)
            matches = {asked[position]: task for position, task in found.items()}
            older, added = await self._match_older(records, status.tasks, matches.values())
        pickup, hidden = await self._sync_marks(records, asked, matches, older, now, synced)
        shown = [(index, record) for index, record in enumerate(records) if record.id not in hidden]
        everything = [record for _, record in shown] + [record for record, _ in older]
        shows = await self._shows(
            record.media_id for record in everything if record.media_type is MediaType.TV
        )
        moved = await self._moved([record for _, record in shown], older, settings, shows)
        entries = []
        for index, record in shown:
            outcome, via = outcomes[index]
            task = matches.get(index)
            if task is None and record.id in pickup:
                base: tuple[DownloadState, float | None] = (pickup[record.id], None)
            else:
                base = entry_state(task, expected=expected[index], asked=synced)
            entries.append(self._entry(record, outcome, via, base, task, shows, moved))
        for record, task in older:
            outcome, via = grab_outcome(record.saved_path, settings.download_mode)
            base = (task.state, task.progress)
            entries.append(self._entry(record, outcome, via, base, task, shows, moved))
        tasks = await self._added_tasks(added, shows)
        space, fresh = await self._free_space(settings)
        overview = DownloadsOverview(
            entries=tuple(entries),
            tasks=tasks,
            program=program,
            problem=problem,
            space=space,
            checked_at=self._now(),
            synced=synced,
            on_this_pc=synced and self._remote.on_this_pc,
            removes_partial=synced and settings.remote_kind is RemoteKind.SYNOLOGY,
        )
        # An action confirmed by a check that started after it finished: the program's word.
        for task_id, pending in list(self._pending.items()):
            if pending.done_seq is not None and seq > pending.done_seq:
                del self._pending[task_id]
        self._raw = overview
        self._publish()
        if fresh and space is not None and space.low:
            clock = self._clock()
            if self._noticed_at is None or clock - self._noticed_at >= NOTICE_INTERVAL_S:
                self._noticed_at = clock
                _tell(self._space_listeners, space)
        assert self.latest is not None
        return self.latest

    def _entry(
        self,
        record: DownloadRecord,
        outcome: GrabOutcome | None,
        via: str,
        base: tuple[DownloadState, float | None],
        task: RemoteTask | None,
        shows: Mapping[int, Show],
        moved: Mapping[int, MovedTo],
    ) -> DownloadEntry:
        assert record.id is not None
        place = moved.get(record.id)
        state, progress = moved_state(*base, place)
        return DownloadEntry(
            record=record,
            title=self._title(record, shows),
            show=shows.get(record.media_id) if record.media_type is MediaType.TV else None,
            outcome=outcome,
            program=via,
            state=state,
            progress=progress,
            moved_to=place,
            task=task,
        )

    async def _match_older(
        self,
        records: Sequence[DownloadRecord],
        tasks: Sequence[RemoteTask],
        taken: Iterable[RemoteTask],
    ) -> tuple[list[tuple[DownloadRecord, RemoteTask]], list[RemoteTask]]:
        """The program's tasks no recent grab took: those of an older grab (or one taken off
        the list before 3.0.2), newest grab first, and those added in the program itself."""
        used = {id(task) for task in taken}
        rest = [task for task in tasks if id(task) not in used]
        if not rest:
            return [], []
        listed = {record.id for record in records}
        history = [record for record in await self._every_grab() if record.id not in listed]
        found = match_tasks(history, rest)
        older = [(history[position], found[position]) for position in sorted(found)]
        matched = {id(task) for _, task in older}
        return older, [task for task in rest if id(task) not in matched]

    async def _every_grab(self) -> list[DownloadRecord]:
        """The whole history, newest first; read again only when a grab was added or removed."""
        stamp = await self._history.stamp()
        cached = self._grabs
        if cached is not None and cached[0] == stamp:
            return cached[1]
        grabs = await self._history.every_grab()
        self._grabs = (stamp, grabs)
        return grabs

    async def _sync_marks(
        self,
        records: Sequence[DownloadRecord],
        asked: Sequence[int],
        matches: Mapping[int, RemoteTask],
        older: Sequence[tuple[DownloadRecord, RemoteTask]],
        now: datetime,
        synced: bool,
    ) -> tuple[dict[int, DownloadState], set[int]]:
        """With the program's task list read: the grabs it has are noted as seen; of the
        recent grabs it should have and doesn't, (state of those it never had: PENDING or
        NOT_FOUND by their age, the ids of those it had: gone, they leave the page). The gone
        ones are marked cleared once `GoneGrabs` confirms it."""
        if not synced:
            return {}, set()
        in_program = [records[index].id for index in matches] + [record.id for record, _ in older]
        missing = [records[index] for index in asked if index not in matches]
        ids = {history_id for history_id in in_program if history_id is not None}
        ids |= {record.id for record in missing if record.id is not None}
        seen = await self._history.seen(ids) if ids else frozenset()
        new = {history_id for history_id in in_program if history_id is not None} - seen
        if new:
            await self._history.mark_seen(new, now)
        pickup: dict[int, DownloadState] = {}
        gone: set[int] = set()
        for record in missing:
            assert record.id is not None
            if record.id in seen:
                gone.add(record.id)
            else:
                pickup[record.id] = pickup_state(record.downloaded_at, now)
        confirmed = self._gone.confirm(gone, self._clock())
        if confirmed:
            await self._history.clear(confirmed, now)
            self._cleared |= set(confirmed)
            log.info(
                "Downloads page: %d grab(s) removed in the torrent program left the list",
                len(confirmed),
            )
        return pickup, gone

    async def _added_tasks(
        self, tasks: Sequence[RemoteTask], shows: Mapping[int, Show]
    ) -> tuple[TaskEntry, ...]:
        """The tasks added in the program itself, recognised when AirGrabber knows the show
        or film, newest first."""
        if not tasks:
            return ()
        await self._refresh_known(shows)
        entries = []
        for task in tasks:
            found = self._recognition.of(task.name)
            title, recognised = found if found is not None else (task.name, None)
            entries.append(TaskEntry(task=task, title=title, recognised=recognised))
        entries.sort(key=lambda entry: entry.listed_at, reverse=True)
        return tuple(entries)

    async def _refresh_known(self, shows: Mapping[int, Show]) -> None:
        """The titles tasks are recognised by: read again after KNOWN_TTL_S, or when the
        history changed (a new grab's show or film)."""
        clock, stamp = self._clock(), self._grabs[0] if self._grabs is not None else None
        fresh = self._known_at is not None and clock - self._known_at < KNOWN_TTL_S
        if fresh and stamp == self._known_stamp:
            return
        followed: Sequence[Show] = ()
        films: Sequence[Movie] = ()
        if self._known_lookup is not None:
            try:
                followed, films = await self._known_lookup()
            except Exception as exc:  # noqa: BLE001 - recognising is a nicety
                log.info("Downloads page: the known titles can't be read (%s)", type(exc).__name__)
        grabs = self._grabs[1] if self._grabs is not None else []
        self._recognition.update(
            KnownTitles.build([*followed, *shows.values()], films, grabs)
        )
        self._known_at, self._known_stamp = clock, stamp

    # -- the page's own actions ------------------------------------------------------------------

    def _publish(self) -> None:
        """`latest` = the last check with the actions not confirmed yet applied; tell the
        listeners."""
        raw = self._raw
        if raw is None:
            return
        self.latest = _with_pending(raw, self._pending, self._cleared)
        _tell(self._listeners, self.latest)

    async def clear(self, history_ids: Iterable[int]) -> int:
        """Take grabs off the page ("Clear finished" without a program to read, "Remove from
        list"); they stay in the history and their files stay where they are. The listeners
        hear the shorter list at once. Returns how many were taken off."""
        ids = {history_id for history_id in history_ids}
        if not ids:
            return 0
        count = await self._history.clear(ids, self._now())
        self._cleared |= ids
        log.info("Downloads page: %d grab(s) taken off the list", len(ids))
        if self._raw is None and self.latest is not None:
            self._raw = self.latest
        self._publish()
        return count

    async def pause(self, item: ListedDownload) -> str | None:
        """Pause a row's task in the program. The page shows it paused at once; on a failure
        it is put back and the plain reason returned (None: done)."""
        return await self._act(item, "pause")

    async def resume(self, item: ListedDownload) -> str | None:
        """Resume a row's task (as `pause`)."""
        return await self._act(item, "resume")

    async def remove(self, item: ListedDownload) -> str | None:
        """Remove a row's task from the program (its files stay; see RemoteDownloads.remove)
        and the row from the page, at once; on a failure the row comes back and the plain
        reason is returned (None: done). A grab stays in the history."""
        return await self._remove([item])

    def finished(self) -> list[ListedDownload]:
        """What "Clear finished" takes now: with the program read, every finished row
        (READY, also seeding), grabs and tasks added there alike."""
        latest = self.latest
        if latest is None:
            return []
        return [item for item in latest.rows if item.state is DownloadState.READY]

    async def clear_finished(self) -> str | None:
        """"Clear finished" with the program read: every finished task removed from the
        program (files kept) and every finished row from the page; on a failure they come
        back and the plain reason is returned (None: done)."""
        return await self._remove(self.finished())

    async def _act(self, item: ListedDownload, kind: Literal["pause", "resume"]) -> str | None:
        task_id = item.task.task_id if item.task is not None else None
        if task_id is None:
            return None
        self._pending[task_id] = _Pending(kind)
        self._publish()
        call = self._remote.pause if kind == "pause" else self._remote.resume
        failure = ""
        try:
            await call([task_id])
        except RemoteError as exc:
            failure = str(exc)
        if failure:
            self._pending.pop(task_id, None)
            log.info("Downloads page: %s failed: %s", kind, failure)
        elif task_id in self._pending:
            self._pending[task_id] = _Pending(kind, done_seq=self._check_seq)
        self._publish()
        return failure or None

    async def _remove(self, items: Sequence[ListedDownload]) -> str | None:
        task_ids = list(
            dict.fromkeys(
                item.task.task_id
                for item in items
                if item.task is not None and item.task.task_id is not None
            )
        )
        grabs = {
            item.record.id
            for item in items
            if isinstance(item, DownloadEntry) and item.record.id is not None
        }
        if not task_ids and not grabs:
            return None
        for task_id in task_ids:
            self._pending[task_id] = _Pending("remove")
        self._publish()
        failure = ""
        if task_ids:
            try:
                await self._remote.remove(task_ids)
            except RemoteError as exc:
                failure = str(exc)
        if failure:
            for task_id in task_ids:
                self._pending.pop(task_id, None)
            log.info("Downloads page: removing %d task(s) failed: %s", len(task_ids), failure)
            self._publish()
            return failure
        for task_id in task_ids:
            if task_id in self._pending:
                self._pending[task_id] = _Pending("remove", done_seq=self._check_seq)
        if grabs:
            await self._history.clear(grabs, self._now())
            self._cleared |= grabs
        log.info(
            "Downloads page: %d task(s) removed from the torrent program, %d grab(s) off the list",
            len(task_ids),
            len(grabs),
        )
        self._publish()
        return None

    async def locate(self, entry: ListedDownload) -> Located:
        """Where the row's video is: where MediaForge moved the grab, else in "Where finished
        downloads end up" (see the module docstring); a task added in the program by a folder
        or file named like it."""
        if isinstance(entry, DownloadEntry) and entry.moved_to is not None:
            return Located(entry.moved_to.folder, "")
        folder = self._settings.current.subtitles_folder.strip()
        if not folder:
            return Located(None, "no_folder")
        scan = await self._folder_scan(folder)
        if scan is None:
            return Located(None, "unreachable")
        if isinstance(entry, TaskEntry):
            name = release_name(entry.task.name)
            paths = [
                video.path
                for video in scan.videos
                if name and any(release_name(part) == name for part in video.parts)
            ]
            found = video_folder(paths)
        else:
            show_name = entry.show.name if entry.show is not None else None
            files = grab_files(entry.record, scan, show_name=show_name)
            found = video_folder([video.video.path for video in files.videos])
        return Located(found, "") if found is not None else Located(None, "not_found")

    async def _folder_scan(self, folder: str) -> FolderScan | None:
        """The folder's scan (kept LOCATE_TTL_S); None when it is out of reach."""
        cached, now = self._scan, self._clock()
        if cached is not None and cached[0] == folder and now - cached[1] < LOCATE_TTL_S:
            return cached[2]
        try:
            async with asyncio.timeout(SPACE_TIMEOUT_S):
                scan = await asyncio.to_thread(self._folder_scanner, folder)
        except (OSError, TimeoutError):
            log.info("Downloads page: the finished-downloads folder can't be read")
            return None
        self._scan = (folder, now, scan)
        return scan

    def open_folder(self, folder: str) -> bool:
        """Open `folder` in Explorer; False when that failed."""
        try:
            self._opener(folder)
        except OSError:
            log.info("Downloads page: Explorer couldn't open the folder")
            return False
        return True

    # -- where MediaForge moved the grabs ---------------------------------------------------------

    async def _moved(
        self,
        records: Sequence[DownloadRecord],
        older: Sequence[tuple[DownloadRecord, RemoteTask]],
        settings: Settings,
        shows: Mapping[int, Show],
    ) -> dict[int, MovedTo]:
        """Where Forge moved each grab (by history id): the recent ones (`match_moves`), and
        the older ones the program still has a task of (`match_moves_by_name`, the moves left);
        {} while Forge doesn't run. A failure (forge.db closing at shutdown) leaves the page as
        it would be without it."""
        forge = self._forge() if self._forge is not None else None
        if forge is None or not (records or older):
            return {}
        try:
            self._listen(forge.records)
            folders = move_folders(settings.subtitles_folder, forge.watch_folders)
            moves = await self._read_moves(forge.records, folders)
            places: dict[int, MovedTo] = {}
            used: set[int] = set()  # id() of the moves taken
            if records:
                oldest = min(record.downloaded_at for record in records) - MOVE_SLACK
                recent = [move for move in moves if _aware(move.moved_at) >= oldest]
                found = match_moves(
                    records,
                    recent,
                    show_names={show_id: show.name for show_id, show in shows.items()},
                    folders=folders,
                ) if recent else {}
                for index, move in found.items():
                    history_id = records[index].id
                    if history_id is not None:
                        places[history_id] = moved_to(move, forge.library_folders)
                    used.add(id(move))
            if older:
                rest = [move for move in moves if id(move) not in used]
                grabs = [record for record, _ in older]
                for index, move in match_moves_by_name(grabs, rest, folders=folders).items():
                    history_id = grabs[index].id
                    if history_id is not None:
                        places[history_id] = moved_to(move, forge.library_folders)
            return places
        except Exception as exc:  # noqa: BLE001 - never the page's problem
            log.info("Downloads page: MediaForge's moves can't be read (%s)", type(exc).__name__)
            return {}

    async def _read_moves(
        self, records: MoveRecords, folders: Sequence[Path]
    ) -> list[MoveRecord]:
        """The moves out of `folders`, newest first (kept until Forge reports a change to them,
        at most MOVES_TTL_S)."""
        key = tuple(os.path.normcase(str(folder)) for folder in folders)
        cached, now = self._moves, self._clock()
        if cached is not None and cached[0] == key and now - cached[1] < MOVES_TTL_S:
            return cached[2]
        moves: list[MoveRecord] = []
        for folder in folders:
            moves.extend(await records.moves_from(folder))
        moves.sort(key=lambda move: _aware(move.moved_at), reverse=True)
        self._moves = (key, now, moves)
        return moves

    def _listen(self, records: MoveRecords) -> None:
        """Hear about changes to Forge's move records (once per Forge start)."""
        if records is self._watched:
            return
        if self._unwatch is not None:
            self._unwatch()
        self._watched = records
        self._moves = None
        self._unwatch = records.subscribe(self._moves_changed)

    def _moves_changed(self) -> None:
        self._moves = None
        if self._visible:  # the page shows: the new place at once
            self._wake.set()

    async def _status(
        self, program: str, *, expected: bool
    ) -> tuple[RemoteStatus | None, str | None]:
        """Every task of the program (`program`: "" = none to ask), or the plain reason it
        couldn't be asked. A program on this PC (AirGrabber's qBittorrent starts when asked) is
        left alone by a background check: while the page is hidden, no recent grab is
        expected there and its last answer had nothing on its way, that answer serves again
        (none yet: no answer)."""
        if not program:
            self._last_status = None
            return None, None
        last = self._last_status if self._last_status and self._last_status[0] == program else None
        if not self._visible and not expected and self._remote.on_this_pc:
            if last is None:
                return None, None
            if not any(task.state.in_progress for task in last[1].tasks):
                return last[1], None
        try:
            status = await self._remote.status()
        except RemoteError as exc:
            log.info("No download progress: %s", exc)
            return None, str(exc)
        self._last_status = (program, status)
        return status, None

    async def _shows(self, show_ids: Iterable[int]) -> dict[int, Show]:
        """The shows by id (each kept SHOW_TTL_S: an old task's show is asked every check)."""
        shows: dict[int, Show] = {}
        clock = self._clock()
        for show_id in set(show_ids):
            cached = self._show_cache.get(show_id)
            if cached is not None and clock - cached[0] < SHOW_TTL_S:
                show = cached[1]
            else:
                show = await self._show(show_id)
                self._show_cache[show_id] = (clock, show)
            if show is not None:
                shows[show_id] = show
        return shows

    @staticmethod
    def _title(record: DownloadRecord, shows: Mapping[int, Show]) -> str:
        if record.media_type is MediaType.MOVIE:
            return movie_title(record.release_title)
        show = shows.get(record.media_id)
        if show is None or record.season is None:
            return record.release_title
        return episode_title(show.name, record.season, record.episode)

    # -- free space --------------------------------------------------------------------------

    async def _free_space(self, settings: Settings) -> tuple[FreeSpace | None, bool]:
        """The free space (measured again when due); and whether it was measured just now."""
        interval = SPACE_VISIBLE_INTERVAL_S if self._visible else SPACE_INTERVAL_S
        now = self._clock()
        if self._space_at is not None and now - self._space_at < interval:
            return self._space, False
        self._space_at = now
        self._space = await self._measure(settings)
        return self._space, True

    async def _measure(self, settings: Settings) -> FreeSpace | None:
        folders = space_folders(settings)
        if folders:
            measured = await self._folder_space(folders)
            if measured is not None:
                usage, on_nas = measured
                return FreeSpace(
                    free_bytes=usage.free,
                    total_bytes=usage.total or None,
                    on_nas=on_nas,
                    low=space_is_low(usage.free, usage.total),
                )
        if not progress_program(settings):
            return None
        try:
            disk = await self._remote.free_space()
        except RemoteError as exc:
            log.info("No free space from the torrent program: %s", exc)
            return None
        if disk is None:
            return None
        return FreeSpace(
            free_bytes=disk.free,
            total_bytes=disk.total,
            on_nas=True,
            low=space_is_low(disk.free, disk.total),
        )

    async def _folder_space(self, folders: Sequence[str]) -> tuple[DiskUsage, bool] | None:
        """disk_usage in a worker thread, with a time limit; None when the folders are out of
        reach (or a check of them still hangs)."""
        if self._disk_job is not None and not self._disk_job.done():
            return None
        job = asyncio.ensure_future(asyncio.to_thread(self._read_folders, folders))
        job.add_done_callback(_retrieve)
        self._disk_job = job
        try:
            async with asyncio.timeout(SPACE_TIMEOUT_S):
                return await asyncio.shield(job)
        except TimeoutError:
            log.info("The downloads folder didn't answer in time; free space unknown")
        except (OSError, ValueError) as exc:
            log.info("No free space for the downloads folder (%s)", type(exc).__name__)
        return None

    def _read_folders(self, folders: Sequence[str]) -> tuple[DiskUsage, bool]:
        """Blocking: the disk usage of the fullest of `folders` (one out of reach is left out
        while another answers) and whether that one is on the network."""
        found: list[tuple[DiskUsage, str]] = []
        failure: OSError | ValueError = FileNotFoundError("no folder")
        for folder in folders:
            try:
                found.append((self._disk_usage(folder), folder))
            except (OSError, ValueError) as exc:
                failure = exc
        if not found:
            raise failure
        usage, folder = min(found, key=lambda item: item[0].free)
        return usage, self._network_path(folder)


def _open_folder(folder: str) -> None:
    """Open a folder in Explorer (the folder's own window, not a file in it)."""
    startfile = getattr(os, "startfile", None)
    if startfile is None:
        raise OSError(0, "Opening folders is only supported on Windows")
    startfile(folder)


def _subscribe[T](listeners: list[T], listener: T) -> Callable[[], None]:
    listeners.append(listener)
    subscribed = True

    def unsubscribe() -> None:
        nonlocal subscribed
        if subscribed:
            subscribed = False
            listeners.remove(listener)

    return unsubscribe


def _tell[T](listeners: list[Callable[[T], None]], value: T) -> None:
    for listener in list(listeners):
        try:
            listener(value)
        except Exception:
            log.exception("Downloads listener %r failed", listener)


def _retrieve(future: asyncio.Future[Any]) -> None:
    """Fetch a background future's exception, so nobody is warned it was never retrieved."""
    if not future.cancelled():
        future.exception()
