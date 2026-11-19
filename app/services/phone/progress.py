"""How far a download is, for the phone's `downloading` items (`"progress"`, 0 to 100 or null;
docs/companion/2026-11-18-person-progress-design.md).

Only what the Downloads page already knows: `DownloadStatusService.latest`, as its last check
left it. A phone's request never makes AirGrabber ask the torrent program (it checks every
minute while something is on its way). No check yet, or no program that reports progress: null.

A film by its TMDB id; an episode by its show's TVMaze id, season and number, its season's pack
counting for it; several grabs of one title: the newest that is on its way (as Coming up calls
it `downloading` when any is, `coming_up._page_states`), else the newest. Unfinished (downloading, waiting,
paused, a problem): the share done, rounded down, at most 99. Finished, or moved into the library
by MediaForge: 100. A grab the program never had or no longer has (or whose state can't be told
just now): null. Nothing here logs.
"""

from __future__ import annotations

import math
from typing import Protocol

from app.models import DownloadEntry, DownloadsOverview, DownloadState, MediaType

_Key = tuple[MediaType, int, int | None, int | None]

_NOT_KNOWN = frozenset({DownloadState.NOT_FOUND, DownloadState.UNKNOWN})
# On its way, as the Downloads page tells it (Coming up's `downloading` too).
ON_ITS_WAY = frozenset(
    {
        DownloadState.DOWNLOADING,
        DownloadState.WAITING,
        DownloadState.PENDING,
        DownloadState.STOPPED,
    }
)


def on_its_way(entry: DownloadEntry) -> bool:
    return entry.state in ON_ITS_WAY and entry.moved_to is None


class _Status(Protocol):
    """What's read of DownloadStatusService: its latest overview, nothing else."""

    @property
    def latest(self) -> DownloadsOverview | None: ...


def percent(entry: DownloadEntry) -> int | None:
    """A grab's progress as the phone shows it."""
    if entry.moved_to is not None or entry.state is DownloadState.READY:
        return 100
    if entry.state in _NOT_KNOWN or entry.progress is None:
        return None
    return min(math.floor(entry.progress * 100), 99)


class Progress:
    """The Downloads page's grabs as its last check left them, by title."""

    def __init__(self, overview: DownloadsOverview | None) -> None:
        self._grabs: dict[_Key, list[DownloadEntry]] = {}
        for entry in overview.entries if overview is not None else ():
            grab = entry.record
            key = (grab.media_type, grab.media_id, grab.season, grab.episode)
            self._grabs.setdefault(key, []).append(entry)

    def film(self, movie_id: int) -> int | None:
        return self._of((MediaType.MOVIE, movie_id, None, None))

    def episode(self, show_id: int, season: int, number: int) -> int | None:
        """An episode of a TVMaze show: its own grabs and its season's packs."""
        return self._of(
            (MediaType.TV, show_id, season, number), (MediaType.TV, show_id, season, None)
        )

    def _of(self, *keys: _Key) -> int | None:
        grabs = [entry for key in keys for entry in self._grabs.get(key, ())]
        if not grabs:
            return None
        going = [entry for entry in grabs if on_its_way(entry)]
        return percent(max(going or grabs, key=lambda entry: entry.record.downloaded_at))


def progress_now(status: _Status) -> Progress:
    """What the Downloads page knows now; read once per phone request."""
    return Progress(status.latest)
