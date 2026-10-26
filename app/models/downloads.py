"""The Downloads page's models: what the torrent program on a NAS or another computer says about
its tasks (RemoteTask, RemoteStatus), the recent grabs with their state (DownloadEntry,
DownloadsOverview) and the free space of the folder downloads end up in (FreeSpace)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from app.models.domain import DomainModel, DownloadRecord, GrabOutcome, Show
from app.models.types import InfoHash, UtcDatetime


class DownloadState(StrEnum):
    """Where a grab is, as the torrent program tells it (or why that isn't known)."""

    DOWNLOADING = "downloading"
    WAITING = "waiting"  # queued, checking, fetching its details
    STOPPED = "stopped"  # paused before it was complete
    READY = "ready"  # complete (seeding, or stopped once complete)
    PROBLEM = "problem"  # the program reports an error for it (disk full, files missing)
    NOT_FOUND = "not_found"  # the program answered but doesn't have it (removed?)
    UNKNOWN = "unknown"  # no program to ask, it can't be reached, or the grab went elsewhere

    @property
    def in_progress(self) -> bool:
        """Still on its way: the header button's badge counts these."""
        return self in (DownloadState.DOWNLOADING, DownloadState.WAITING)


class RemoteTask(DomainModel):
    """One task (torrent) in the torrent program. Never holds its link: a magnet or URL may
    carry a tracker passkey or an API key."""

    name: str
    info_hash: InfoHash = None  # lowercase hex, when the program says (or its magnet does)
    file_name: str | None = None  # the .torrent file it was made from (Download Station)
    progress: float = Field(default=0.0, ge=0.0, le=1.0)
    state: DownloadState


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


class DownloadEntry(DomainModel):
    """One recent grab on the Downloads page."""

    record: DownloadRecord
    title: str  # "Fallout S02E03", "Gladiator II (2024)"
    show: Show | None = None  # an episode's show (its window, its poster)
    outcome: GrabOutcome | None = None  # how it was handed over, as far as the history says
    program: str = ""  # the program it was sent to or opened in
    state: DownloadState = DownloadState.UNKNOWN
    progress: float | None = Field(default=None, ge=0.0, le=1.0)  # known for a found task
    subtitles: tuple[str, ...] = ()  # languages saved for it (OpenSubtitles codes)


class DownloadsOverview(DomainModel):
    """Everything the Downloads page shows."""

    entries: tuple[DownloadEntry, ...] = ()
    # The program asked for progress ("Download Station"); "" when there is none to ask (no
    # program on a NAS or another computer set up, or the torrent program on this PC is used).
    program: str = ""
    problem: str | None = None  # why it couldn't be asked: one plain line, no URL or secret
    space: FreeSpace | None = None  # the last free-space check, when it worked
    checked_at: UtcDatetime | None = None

    @property
    def progress_available(self) -> bool:
        return bool(self.program)

    @property
    def in_progress(self) -> int:
        return sum(1 for entry in self.entries if entry.state.in_progress)

