"""The Downloads page's models: what the torrent program on a NAS or another computer says about
its tasks (RemoteTask, RemoteStatus), the recent grabs with their state and where MediaForge
moved them (DownloadEntry, MovedTo, DownloadsOverview), the tasks added in the torrent program
itself (TaskEntry, with what AirGrabber recognised them as: Recognised) and the free space of the
folder downloads end up in (FreeSpace).
The download queue's states and events (QueueState, QueueEvent, QueuedItem)."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import Field

from app.models.domain import (
    DomainModel,
    DownloadRecord,
    GrabOutcome,
    MediaType,
    QuickDownloadResult,
    Show,
)
from app.models.types import InfoHash, UtcDatetime


class DownloadState(StrEnum):
    """Where a grab is, as the torrent program tells it (or why that isn't known)."""

    DOWNLOADING = "downloading"
    WAITING = "waiting"  # queued, checking, fetching its details
    STOPPED = "stopped"  # paused before it was complete
    READY = "ready"  # complete (seeding, or stopped once complete)
    PROBLEM = "problem"  # the program reports an error for it (disk full, files missing)
    # A grab the program never had: picked up within the hour (PENDING), else never (NOT_FOUND).
    PENDING = "pending"  # sent / saved less than an hour ago, the program hasn't got it yet
    NOT_FOUND = "not_found"  # the program answered but never had it (never picked it up)
    UNKNOWN = "unknown"  # no program to ask, it can't be reached, or the grab went elsewhere

    @property
    def in_progress(self) -> bool:
        """Still on its way: the header button's badge counts these."""
        return self in (DownloadState.DOWNLOADING, DownloadState.WAITING, DownloadState.PENDING)


class RemoteTask(DomainModel):
    """One task (torrent) in the torrent program. Never holds its link: a magnet or URL may
    carry a tracker passkey or an API key."""

    name: str
    info_hash: InfoHash = None  # lowercase hex, when the program says (or its magnet does)
    file_name: str | None = None  # the .torrent file it was made from (Download Station)
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    state: DownloadState
    # The program's own id for pause / resume / remove (Download Station "dbid_…", qBittorrent's
    # and Transmission's hash). Not a secret, but never logged together with a name.
    task_id: str | None = None
    size: int | None = Field(default=None, ge=0)  # bytes, when the program says
    added_at: UtcDatetime | None = None  # when it was added to the program


class RemoteStatus(DomainModel):
    """The torrent program's task list."""

    program: str  # "Download Station"
    tasks: tuple[RemoteTask, ...] = ()


class FreeSpace(DomainModel):
    """The free space where finished downloads end up."""

    free_bytes: int = Field(ge=0)
    total_bytes: int | None = None
    on_nas: bool = True  # a network share or the remote program's own disk, not this PC's
    low: bool = False  # below LOW_SPACE_FRACTION or LOW_SPACE_BYTES (download_status.py)


class MovedTo(DomainModel):
    """Where MediaForge moved a finished grab's video: the library folder now holding it."""

    folder: str  # the whole path, for "Open the folder": never shown or logged (a user name)
    # What the row shows: the library folder's base name onward ("Series", "Gold Rush",
    # "Season 17"), never the part before it.
    shown: tuple[str, ...]


class DownloadEntry(DomainModel):
    """One grab on the Downloads page: a recent one, or an older one its program still has."""

    record: DownloadRecord
    title: str  # "Fallout S02E03", "Gladiator II (2024)"
    show: Show | None = None  # an episode's show (its window, its poster)
    outcome: GrabOutcome | None = None  # how it was handed over, as far as the history says
    program: str = ""  # the program it was sent to or opened in
    state: DownloadState = DownloadState.UNKNOWN
    progress: float | None = Field(default=None, ge=0.0, le=1.0)  # known for a found task
    moved_to: MovedTo | None = None  # where MediaForge moved its video (None: not known)
    task: RemoteTask | None = None  # its task in the program (the page mirrors the program)

    @property
    def listed_at(self) -> datetime:
        """When it was grabbed: the page lists newest first."""
        return self.record.downloaded_at


class Recognised(DomainModel):
    """What a task added in the torrent program itself is, when AirGrabber knows the series
    (a followed show) or the film (a watchlist film, a film in the history)."""

    name: str  # "Severance", "Dune: Part Two"
    show: Show | None = None  # an episode's show (its window, its poster)
    movie_id: int | None = None  # a film's TMDB id (its info window)
    poster_url: str | None = None  # a film's poster, when the watchlist has one


class TaskEntry(DomainModel):
    """A task in the torrent program that no grab of AirGrabber's matches: added there."""

    task: RemoteTask
    title: str  # "Severance S02E04" when recognised, else the task's own name
    recognised: Recognised | None = None

    @property
    def state(self) -> DownloadState:
        return self.task.state

    @property
    def progress(self) -> float:
        return self.task.progress

    @property
    def listed_at(self) -> datetime:
        """When it was added to the program (not known: listed last)."""
        return self.task.added_at or _LONG_AGO


_LONG_AGO = datetime.min.replace(tzinfo=UTC)

# A row of the Downloads page: a grab, or a task added in the torrent program.
ListedDownload = DownloadEntry | TaskEntry


class DownloadsOverview(DomainModel):
    """Everything the Downloads page shows."""

    entries: tuple[DownloadEntry, ...] = ()
    # The program's tasks no grab matches (added in the program itself); only while `synced`.
    tasks: tuple[TaskEntry, ...] = ()
    # The program asked for progress ("Download Station"); "" when there is none to ask (no
    # program on a NAS or another computer set up, or the torrent program on this PC is used).
    program: str = ""
    problem: str | None = None  # why it couldn't be asked: one plain line, no URL or secret
    space: FreeSpace | None = None  # the last free-space check, when it worked
    checked_at: UtcDatetime | None = None
    # The program's task list was read this check: the page mirrors it (pause, resume, remove).
    synced: bool = False
    on_this_pc: bool = False  # the program runs on this PC (what it downloads stays here)
    # Removing an unfinished task deletes what it downloaded so far (Download Station).
    removes_partial: bool = False

    @property
    def progress_available(self) -> bool:
        return bool(self.program)

    @property
    def in_progress(self) -> int:
        grabs = sum(1 for entry in self.entries if entry.state.in_progress)
        return grabs + sum(1 for item in self.tasks if item.state.in_progress)

    @property
    def rows(self) -> tuple[ListedDownload, ...]:
        """The grabs and the tasks added in the program, newest first (a stable order)."""
        listed: list[ListedDownload] = [*self.entries, *self.tasks]
        listed.sort(key=lambda item: item.listed_at, reverse=True)
        return tuple(listed)


# -- the download queue (app/services/download_queue.py) -----------------------------------------

# A one-click download's title: (media type, TVMaze show id or TMDB film id, season, episode);
# an episode None with a season = that season's pack.
QueueKey = tuple[MediaType, int, int | None, int | None]


class QueueState(StrEnum):
    """Where a one-click download in the download queue is."""

    WAITING = "waiting"  # queued, its turn not come yet
    RUNNING = "running"  # searching, or sending what it picked
    DONE = "done"  # grabbed, or it was downloaded already
    NOTHING = "nothing"  # no release was good enough
    FAILED = "failed"  # the grab failed (`QueueEvent.message` says why)
    DROPPED = "dropped"  # taken out before its turn (✕, its batch's grab failed)

    @property
    def active(self) -> bool:
        """Still in the queue: waiting or running."""
        return self in (QueueState.WAITING, QueueState.RUNNING)


class QueueEvent(DomainModel):
    """A queued download's new state, as the queue tells its listeners."""

    key: QueueKey
    label: str  # "Severance S02E01", "Severance S02", "Dune (2021)": never a release
    state: QueueState
    result: QuickDownloadResult | None = None  # DONE and NOTHING
    message: str = ""  # FAILED: why, in plain words (never a URL or a key)
    # NOTHING of a season pack: its episodes, queued instead (empty: none were needed).
    fallback: tuple[QueueKey, ...] = ()


class QueuedItem(DomainModel):
    """A download waiting in the queue, as the Downloads page lists it."""

    key: QueueKey
    label: str
    state: QueueState = QueueState.WAITING

