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
import logging
import os
import re
import shutil
import sys
import time
from collections.abc import Awaitable, Callable, Collection, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, NamedTuple, Protocol

from app.database import HistoryRepository, SubtitleRepository
from app.i18n import t
from app.models import (
    DownloadEntry,
    DownloadMode,
    DownloadRecord,
    DownloadsOverview,
    DownloadState,
    FreeSpace,
    GrabOutcome,
    MediaType,
    RemoteKind,
    RemoteStatus,
    RemoteTask,
    Settings,
    Show,
)
from app.models.downloads import MovedTo
from app.services.downloads import safe_filename
from app.services.remote import RemoteError
from app.services.remote_downloads import RemoteDownloads
from app.services.settings import SettingsService
from app.services.subtitle_files import FolderScan, VideoFile, grab_files, normalize, scan_folder

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

ShowLookup = Callable[[int], Awaitable[Show | None]]
OverviewListener = Callable[[DownloadsOverview], None]
SpaceListener = Callable[[FreeSpace], None]


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


def torrent_file_names(record: DownloadRecord) -> frozenset[str]:
    """The .torrent file names (lowercase) a grab may show up under in Download Station: the
    file saved to the watch folder, and the name a .torrent is sent with."""
    names = {f"{safe_filename(record.release_title)}.torrent".lower()}
    saved = record.saved_path or ""
    if saved.lower().endswith(".torrent"):
        names.add(saved.replace("\\", "/").rsplit("/", 1)[-1].lower())
    return frozenset(names)


def match_tasks(
    records: Sequence[DownloadRecord], tasks: Sequence[RemoteTask]
) -> dict[int, RemoteTask]:
    """The program's task for each grab (by index in `records`), each task used once: by info
    hash first, then by .torrent file name, then by name. `records` newest first, so a newer
    grab of the same name wins."""
    unused = list(tasks)
    found: dict[int, RemoteTask] = {}

    def take(index: int, key: Callable[[RemoteTask], object], wanted: object) -> None:
        for task in unused:
            if key(task) == wanted:
                found[index] = task
                unused.remove(task)
                return

    def by_hash(task: RemoteTask) -> object:
        return task.info_hash

    def by_file(task: RemoteTask) -> object:
        return (task.file_name or "").lower()

    def by_name(task: RemoteTask) -> object:
        return normalize(task.name)

    for index, record in enumerate(records):
        if record.info_hash:
            take(index, by_hash, record.info_hash)
    for index, record in enumerate(records):
        if index not in found:
            for file_name in sorted(torrent_file_names(record)):
                if index not in found:
                    take(index, by_file, file_name)
    for index, record in enumerate(records):
        if index not in found and (name := normalize(record.release_title)):
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


_GONE_AFTER_A_MOVE = frozenset({DownloadState.PROBLEM, DownloadState.NOT_FOUND})


def moved_state(
    state: DownloadState, progress: float | None, moved: MovedTo | None
) -> tuple[DownloadState, float | None]:
    """A grab MediaForge moved into the library is finished, whatever the torrent program
    makes of its files being gone: qBittorrent ("missingFiles"), Transmission (a local error)
    and Download Station ("error") report a problem for a task whose file was moved away while
    it seeded, and a task removed afterwards is not found. Any other state stays."""
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
        subtitles: SubtitleRepository,
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
    ) -> None:
        """`forge`: Forge's move records once it runs (`forge_moves`), for where grabs ended
        up."""
        self._forge = forge
        self._moves: tuple[tuple[str, ...], float, list[MoveRecord]] | None = None
        self._watched: MoveRecords | None = None  # the move records listened to
        self._unwatch: Callable[[], None] | None = None
        self._history = history
        self._subtitles = subtitles
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
        settings = self._settings.current  # one snapshot for the whole check
        since = self._now() - RECENT
        records = [
            record
            for record in await self._history.listed(RECENT_LIMIT)
            if record.downloaded_at >= since
            and record.id is not None
            and record.id not in self._cleared
        ]
        languages = await self._subtitles.grab_languages(
            record.id for record in records if record.id is not None
        )
        shows = await self._shows(
            record.media_id for record in records if record.media_type is MediaType.TV
        )
        outcomes = [grab_outcome(record.saved_path, settings.download_mode) for record in records]
        expected = [
            expected_in_program(outcome, program, settings) for outcome, program in outcomes
        ]
        program = progress_program(settings)
        status, problem = await self._status(
            [record for record, wanted in zip(records, expected, strict=True) if wanted]
        )
        matches: dict[int, RemoteTask] = {}
        if status is not None:
            asked = [index for index, wanted in enumerate(expected) if wanted]
            found = match_tasks([records[index] for index in asked], status.tasks)
            matches = {asked[position]: task for position, task in found.items()}
        moved = await self._moved(records, settings, shows)
        entries = []
        for index, record in enumerate(records):
            outcome, via = outcomes[index]
            state, progress = moved_state(
                *entry_state(
                    matches.get(index), expected=expected[index], asked=status is not None
                ),
                moved.get(index),
            )
            assert record.id is not None
            entries.append(
                DownloadEntry(
                    record=record,
                    title=self._title(record, shows),
                    show=shows.get(record.media_id) if record.media_type is MediaType.TV else None,
                    outcome=outcome,
                    program=via,
                    state=state,
                    progress=progress,
                    subtitles=tuple(sorted(languages.get(record.id, ()))),
                    moved_to=moved.get(index),
                )
            )
        space, fresh = await self._free_space(settings)
        overview = DownloadsOverview(
            entries=tuple(entries),
            program=program,
            problem=problem,
            space=space,
            checked_at=self._now(),
        )
        self.latest = overview
        _tell(self._listeners, overview)
        if fresh and space is not None and space.low:
            now = self._clock()
            if self._noticed_at is None or now - self._noticed_at >= NOTICE_INTERVAL_S:
                self._noticed_at = now
                _tell(self._space_listeners, space)
        return overview

    # -- the page's own actions ------------------------------------------------------------------

    async def clear(self, history_ids: Iterable[int]) -> int:
        """Take grabs off the page ("Clear finished", "Remove from list"); they stay in the
        history and their files stay where they are. The listeners hear the shorter list at
        once. Returns how many were taken off."""
        ids = {history_id for history_id in history_ids}
        if not ids:
            return 0
        count = await self._history.clear(ids, self._now())
        self._cleared |= ids
        log.info("Downloads page: %d grab(s) taken off the list", len(ids))
        latest = self.latest
        if latest is not None:
            entries = tuple(entry for entry in latest.entries if entry.record.id not in ids)
            self.latest = latest.model_copy(update={"entries": entries})
            _tell(self._listeners, self.latest)
        return count

    async def locate(self, entry: DownloadEntry) -> Located:
        """Where the grab's video is: where MediaForge moved it, else in "Where finished
        downloads end up" (see the module docstring)."""
        if entry.moved_to is not None:
            return Located(entry.moved_to.folder, "")
        folder = self._settings.current.subtitles_folder.strip()
        if not folder:
            return Located(None, "no_folder")
        scan = await self._folder_scan(folder)
        if scan is None:
            return Located(None, "unreachable")
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
        self, records: Sequence[DownloadRecord], settings: Settings, shows: Mapping[int, Show]
    ) -> dict[int, MovedTo]:
        """Where Forge moved each grab (by index in `records`); {} while Forge doesn't run.
        A failure (forge.db closing at shutdown) leaves the page as it would be without it."""
        forge = self._forge() if self._forge is not None else None
        if forge is None or not records:
            return {}
        try:
            self._listen(forge.records)
            folders = move_folders(settings.subtitles_folder, forge.watch_folders)
            moves = await self._read_moves(forge.records, folders)
            oldest = min(record.downloaded_at for record in records) - MOVE_SLACK
            recent = [move for move in moves if _aware(move.moved_at) >= oldest]
            if not recent:
                return {}
            found = match_moves(
                records,
                recent,
                show_names={show_id: show.name for show_id, show in shows.items()},
                folders=folders,
            )
            return {index: moved_to(move, forge.library_folders) for index, move in found.items()}
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
        self, wanted: Sequence[DownloadRecord]
    ) -> tuple[RemoteStatus | None, str | None]:
        """The program's tasks, unless no grab is expected there; or the plain reason it
        couldn't be asked."""
        if not wanted:
            return None, None
        hashes: Collection[str] = ()
        if all(record.info_hash for record in wanted):
            hashes = frozenset(record.info_hash for record in wanted if record.info_hash)
        try:
            return await self._remote.status(hashes), None
        except RemoteError as exc:
            log.info("No download progress: %s", exc)
            return None, str(exc)

    async def _shows(self, show_ids: Iterable[int]) -> dict[int, Show]:
        shows: dict[int, Show] = {}
        for show_id in set(show_ids):
            show = await self._show(show_id)
            if show is not None:
                shows[show_id] = show
        return shows

    @staticmethod
    def _title(record: DownloadRecord, shows: dict[int, Show]) -> str:
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
