"""Films' first digital release dates, kept across starts for the Movies page's digital shelves
(`DiscoveryService._first_digital_releases`), so a start doesn't ask TMDB for every shelf film's
details again: `<data>\\release-dates.json`, per TMDB id the date (or none known) and when it was
read; one older than RELEASE_DATES_TTL is read again (and dropped from the file).

A missing, broken or unwritable file counts as empty: nothing here raises over it. The file is
read once and written whole (a temporary file, then a replace), both through `asyncio.to_thread`.
Ids, dates and times only: no titles; nothing about it is logged but a count.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

RELEASE_DATES_FILE = "release-dates.json"  # in the data folder
RELEASE_DATES_TTL = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class Known:
    """A film's first digital release as read from TMDB (None: TMDB knows none), and when."""

    digital: date | None
    read_at: datetime


class ReleaseDates:
    """The dates by TMDB id; with `file` None (tests, other uses) they are kept in memory only."""

    def __init__(self, file: Path | None, clock: Callable[[], datetime]) -> None:
        self._file = file
        self._clock = clock
        self._known: dict[int, Known] = {}
        self._loaded = file is None
        self._dirty = False
        self._lock = asyncio.Lock()  # one read, and one write at a time

    async def load(self) -> None:
        """Read the file, once (later calls return at once)."""
        if self._loaded:
            return
        async with self._lock:
            if self._loaded or self._file is None:
                return
            read = await asyncio.to_thread(_read, self._file)
            for movie_id, known in read.items():
                self._known.setdefault(movie_id, known)  # what this run read wins
            self._loaded = True

    def lookup(self, movie_id: int) -> Known | None:
        """The film's date when it was read less than RELEASE_DATES_TTL ago, else None."""
        known = self._known.get(movie_id)
        if known is None or not _fresh(known, self._clock()):
            return None
        return known

    def remember(self, movie_id: int, digital: date | None, read_at: datetime | None = None) -> None:
        """Keep the film's date as read at `read_at` (when its details were fetched, so a date
        from details kept in memory doesn't outlive them; None: now)."""
        self._known[movie_id] = Known(digital, read_at or self._clock())
        self._dirty = True

    async def save(self) -> None:
        """Write what changed (stale dates left out); never raises over the file."""
        if self._file is None or not self._dirty:
            return
        async with self._lock:
            if not self._dirty:
                return
            now = self._clock()
            self._known = {i: k for i, k in self._known.items() if _fresh(k, now)}
            self._dirty = False
            text = json.dumps(
                {
                    str(movie_id): {
                        "digital": known.digital.isoformat() if known.digital else None,
                        "at": known.read_at.timestamp(),
                    }
                    for movie_id, known in self._known.items()
                }
            )
            if not await asyncio.to_thread(_write, self._file, text):
                log.info("Release dates: %d could not be kept for the next start", len(self._known))


def _fresh(known: Known, now: datetime) -> bool:
    # A clock that went backwards makes the date stale rather than fresh for longer.
    return timedelta(0) <= now - known.read_at < RELEASE_DATES_TTL


def _read(file: Path) -> dict[int, Known]:
    """The file's dates; none when it is missing or broken, an odd entry left out. Blocking."""
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    known: dict[int, Known] = {}
    for key, value in data.items() if isinstance(data, dict) else ():
        if not isinstance(value, dict) or not str(key).isdecimal():
            continue
        digital, at = value.get("digital"), value.get("at")
        if not isinstance(at, (int, float)) or isinstance(at, bool):
            continue
        try:
            day = date.fromisoformat(digital) if isinstance(digital, str) else None
            read_at = datetime.fromtimestamp(at, UTC)
        except (ValueError, OverflowError, OSError):
            continue
        if digital is not None and day is None:
            continue  # a date that isn't one
        known[int(key)] = Known(day, read_at)
    return known


def _write(file: Path, text: str) -> bool:
    """Write `text` to `file` atomically; False when it can't be. Blocking."""
    spare = file.with_name(file.name + ".tmp")
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        spare.write_text(text, encoding="utf-8")
        os.replace(spare, file)
    except OSError:
        try:
            spare.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return True
