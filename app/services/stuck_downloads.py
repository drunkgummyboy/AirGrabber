"""Downloads that stopped moving: swapped for the next best release (Settings › Automatic).

`StuckWatch` hears every Downloads overview (DownloadStatusService: every minute while something
is on its way) and remembers since when each of AirGrabber's grabs has stood still. A grab is
stuck once its task in the torrent program has been downloading for `stuck_swap_hours` without
gaining MOVED of its size (a task still at 0 % counts from when the program got it). The main
window then removes the task from the program and downloads the same episode or film again,
leaving out the releases fetched for it before (`avoided`). Each grab is swapped once, and a
title at most MAX_SWAPS times a run, so a show nobody seeds doesn't swap forever.

Only a program AirGrabber can read (the overview `synced`), only a task that is downloading
(not paused, waiting in the program's own queue or finished), never a task added in the program
itself. Nothing here logs a release title.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta

from app.models import DownloadEntry, DownloadRecord, DownloadsOverview, DownloadState, MediaType

MOVED = 0.002  # progress (0.2 % of the size) that counts as moving
MAX_SWAPS = 2  # swaps of one title per run

_Item = tuple[MediaType, int, int | None, int | None]


def _item(record: DownloadRecord) -> _Item:
    return record.media_type, record.media_id, record.season, record.episode


def avoided(grabs: Iterable[DownloadRecord], record: DownloadRecord) -> tuple[str, ...]:
    """What a swap leaves out: the release titles and info hashes of every earlier grab of
    `record`'s episode, season pack or film (`grabs`: the history), `record` itself too."""
    item = _item(record)
    keys: dict[str, None] = {}
    for grab in (record, *grabs):
        if _item(grab) == item:
            keys[grab.release_title.casefold()] = None
            if grab.info_hash:
                keys[grab.info_hash.casefold()] = None
    return tuple(keys)


class StuckWatch:
    """Since when each grab stood still, and which ones to swap now."""

    def __init__(self, now: Callable[[], datetime] = lambda: datetime.now(UTC)) -> None:
        self._now = now
        self._still: dict[int, tuple[float, datetime]] = {}  # grab id -> (progress, since)
        self._swapped: set[int] = set()
        self._swaps: Counter[_Item] = Counter()

    def stuck(self, overview: DownloadsOverview, hours: int) -> list[DownloadEntry]:
        """The grabs of `overview` to swap now (each one once); none while `hours` is 0."""
        if not overview.synced:
            return []  # nothing known about the program this time: keep what was seen
        now = self._now()
        limit = timedelta(hours=hours)
        due: list[DownloadEntry] = []
        live: set[int] = set()
        for entry in overview.entries:
            record, task = entry.record, entry.task
            if (
                record.id is None
                or task is None
                or task.task_id is None
                or entry.state is not DownloadState.DOWNLOADING
                or entry.moved_to is not None
            ):
                continue
            live.add(record.id)
            progress = entry.progress or 0.0
            seen = self._still.get(record.id)
            if seen is None or progress >= seen[0] + MOVED:
                since = task.added_at if progress == 0 and task.added_at is not None else now
                self._still[record.id] = (progress, min(since, now))
                if seen is not None:
                    continue
                seen = self._still[record.id]
            if (
                hours > 0
                and now - seen[1] >= limit
                and record.id not in self._swapped
                and self._swaps[_item(record)] < MAX_SWAPS
            ):
                self._swapped.add(record.id)
                self._swaps[_item(record)] += 1
                due.append(entry)
        for grab_id in set(self._still) - live:
            del self._still[grab_id]
        return due
