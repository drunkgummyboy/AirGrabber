"""Find junk and leftover artwork under a dropped folder (the legacy cleanup rules), and what a
download leaves behind once its video has moved into the library."""

from __future__ import annotations

import os
import re
from pathlib import Path

from app.forge.core import fsops
from app.forge.core.parser import VIDEO_EXTENSIONS
from app.forge.core.watch import QUIET_S, TEMP_SUFFIXES, is_sample

JUNK_NAMES = frozenset({"thumbs.db", ".ds_store", "desktop.ini"})
# Notes a release ships beside its video; subtitles and pictures are not among them.
DOWNLOAD_NOTES = frozenset({".nfo", ".txt", ".url", ".sfv"})

_POSTER = re.compile(r"^(?P<base>.+) - poster\.jpg$")
_SERIES_POSTER = re.compile(r"^.+ - series poster\.jpg$")
_SEASON_POSTER = re.compile(r"^season \d{2} poster\.jpg$")


def _is_video_name(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in VIDEO_EXTENSIONS


def _tree_has_videos(folder: Path) -> bool:
    return any(_is_video_name(f) for _, files in fsops.walk(folder) for f in files)


def find_cleanup_targets(root: Path) -> list[Path]:
    targets: list[Path] = []
    for dirpath, files in fsops.walk(root):
        lower = {f.lower() for f in files}
        has_video_here = any(_is_video_name(f) for f in files)

        def has_video_named(base: str, lower: set[str] = lower) -> bool:
            return any(f"{base}{ext}" in lower for ext in VIDEO_EXTENSIONS)

        for name in sorted(files):
            low = name.lower()
            path = Path(dirpath) / name
            if low in JUNK_NAMES:
                targets.append(path)
            elif low.endswith(".url"):
                if has_video_named(low[: -len(".url")]):
                    targets.append(path)
            elif _SERIES_POSTER.match(low):
                if not _tree_has_videos(dirpath):
                    targets.append(path)
            elif _SEASON_POSTER.match(low):
                if not has_video_here:
                    targets.append(path)
            elif m := _POSTER.match(low):
                if not has_video_named(m.group("base")):
                    targets.append(path)
    return targets


def download_leftovers(folder: Path) -> list[Path]:
    """Samples, release notes and junk in a download's folder (blocking: run it in a thread)."""
    found: list[Path] = []
    for dirpath, files in fsops.walk(folder):
        for name in sorted(files):
            path = Path(dirpath) / name
            low = name.lower()
            video = _is_video_name(name)
            if (video and is_sample(path)) or low in JUNK_NAMES:
                found.append(path)
            elif not video and os.path.splitext(low)[1] in DOWNLOAD_NOTES:
                found.append(path)
    return found


def videos_left(folder: Path) -> bool:
    """Whether a real video (not a sample) is still somewhere in `folder`."""
    return any(
        _is_video_name(f) and not is_sample(Path(f))
        for _, files in fsops.walk(folder)
        for f in files
    )


def download_busy(folder: Path, now: float) -> bool:
    """Whether something in `folder` is still being downloaded or was written very recently."""
    for dirpath, files in fsops.walk(folder):
        for name in files:
            if name.lower().endswith(TEMP_SUFFIXES):
                return True
            try:
                if now - fsops.stamp(Path(dirpath) / name)[1] / 1_000_000_000 < QUIET_S:
                    return True
            except OSError:
                return True
    return False
