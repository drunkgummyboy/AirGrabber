"""Videos opened from Explorer's "Open with" AirGrabber player (app/services/open_with.py), played
by AirGrabber's player without Plex (MediaPlayerHost.play_file, app/ui/media_player.py;
docs/player/2026-11-14-open-with-design.md).

- `next_file`: the episode after a file in its folder, when the names count on: the nearest
  later SxxEyy of the same show (the text before the code, letters and digits only) among the
  folder's videos (`video_names`, blocking: through `asyncio.to_thread`). Anything else: None.
- `LocalPlaces`: where a file was left, on this PC only (`<data>\\played-files.json`): by a
  SHA-256 of its full path (Windows' case folded), never the path or the name; a place under
  MIN_PLACE_MS is nothing to resume and one past FINISHED_SHARE of the length means watched,
  both forgotten; the newest PLACES_LIMIT are kept. A missing or broken file counts as empty;
  writing never raises.

Stdlib only; nothing here is logged.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import os
import re
import time
from collections.abc import Callable, Iterable
from pathlib import Path

PLACES_FILE = "played-files.json"  # in the data folder
PLACES_LIMIT = 500
MIN_PLACE_MS = 10_000
FINISHED_SHARE = 0.9  # as Plex's watched mark (app/ui/media_player.py's WATCHED_SHARE)
VIDEO_SUFFIXES = frozenset(
    {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".wmv", ".webm", ".ts", ".m2ts", ".mpg", ".mpeg", ".flv"}
)

_CODE = re.compile(r"(?<![a-z0-9])s(\d{1,2})[ ._-]?e(\d{1,3})(?!\d)", re.IGNORECASE)
_NOT_WORD = re.compile(r"[^0-9a-z]+")


def _spot(name: str) -> tuple[str, int, int] | None:
    """(the show part, season, episode) of a file's name, or None without an SxxEyy."""
    stem = ntpath.splitext(ntpath.basename(name))[0]
    match = _CODE.search(stem)
    if match is None:
        return None
    show = _NOT_WORD.sub("", stem[: match.start()].casefold())
    return show, int(match.group(1)), int(match.group(2))


def episode_code(name: str) -> str | None:
    """"S01E03" for a file named with one (any case, "s1e3" too); None without."""
    spot = _spot(name)
    return None if spot is None else f"S{spot[1]:02d}E{spot[2]:02d}"


def next_file(path: str, names: Iterable[str]) -> str | None:
    """The file after `path` among `names` (its folder's videos): the same show's nearest later
    episode, as a path in that folder; None when `path` has no SxxEyy or nothing comes after."""
    current = _spot(path)
    if current is None:
        return None
    show, season, number = current
    best: tuple[int, int, str] | None = None
    for name in names:
        if ntpath.splitext(name)[1].casefold() not in VIDEO_SUFFIXES:
            continue
        spot = _spot(name)
        if spot is None or spot[0] != show or (spot[1], spot[2]) <= (season, number):
            continue
        if best is None or (spot[1], spot[2]) < best[:2]:
            best = (spot[1], spot[2], name)
    return None if best is None else ntpath.join(ntpath.dirname(path), best[2])


def video_names(folder: str) -> list[str]:
    """The video files in `folder` by name, sorted; none when it can't be read. Blocking."""
    try:
        with os.scandir(folder) as entries:
            return sorted(
                entry.name
                for entry in entries
                if os.path.splitext(entry.name)[1].casefold() in VIDEO_SUFFIXES and entry.is_file()
            )
    except OSError:
        return []


def _key(path: str) -> str:
    return hashlib.sha256(ntpath.normcase(ntpath.abspath(path)).encode("utf-8")).hexdigest()


class LocalPlaces:
    """Where each file opened from Explorer was left (see the module docstring)."""

    def __init__(self, file: Path, *, now: Callable[[], float] = time.time) -> None:
        self._file = file
        self._now = now
        self._places: dict[str, tuple[int, float]] | None = None  # key: (ms, when)

    def place(self, path: str) -> int:
        """Where `path` was left (ms), 0 for the start."""
        found = self._read().get(_key(path))
        return found[0] if found else 0

    def remember(self, path: str, ms: int, duration_ms: int) -> None:
        """`path` was left at `ms` of `duration_ms` (0: unknown); written at once."""
        places = self._read()
        key = _key(path)
        finished = duration_ms > 0 and ms >= duration_ms * FINISHED_SHARE
        if finished or ms < MIN_PLACE_MS:
            if places.pop(key, None) is None:
                return
        else:
            places[key] = (ms, self._now())
            if len(places) > PLACES_LIMIT:
                newest = sorted(places.items(), key=lambda item: item[1][1], reverse=True)
                self._places = places = dict(newest[:PLACES_LIMIT])
        self._write(places)

    def _read(self) -> dict[str, tuple[int, float]]:
        if self._places is None:
            self._places = {}
            try:
                data = json.loads(self._file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            for key, value in (data.items() if isinstance(data, dict) else ()):
                if not isinstance(value, dict):
                    continue
                ms, when = value.get("ms"), value.get("at")
                if (
                    isinstance(ms, int) and not isinstance(ms, bool)
                    and isinstance(when, (int, float)) and not isinstance(when, bool)
                ):
                    self._places[str(key)] = (ms, float(when))
        return self._places

    def _write(self, places: dict[str, tuple[int, float]]) -> None:
        data = {key: {"ms": ms, "at": when} for key, (ms, when) in places.items()}
        try:
            self._file.parent.mkdir(parents=True, exist_ok=True)
            spare = self._file.with_name(self._file.name + ".tmp")
            spare.write_text(json.dumps(data), encoding="utf-8")
            os.replace(spare, self._file)
        except OSError:
            pass
