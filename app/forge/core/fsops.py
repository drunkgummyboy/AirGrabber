"""Disk operations that never overwrite anything. All are blocking: call via asyncio.to_thread.

Paths go through long_path() so deep NAS folders past 260 characters still work.
"""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
from pathlib import Path

from app.i18n import N_

_ERROR_NOT_SAME_DEVICE = 17  # Windows' code for a move across drives or shares
NAME_TAKEN = N_("Something already has this name")  # an OSError's text: t() it where shown
PARTIAL_SUFFIX = ".mediaforge-partial"  # a copy between shares that isn't whole yet
_PREFIX = "\\\\?\\"
_UNC_PREFIX = "\\\\?\\UNC\\"


def long_path(path: Path) -> str:
    r"""The extended-length form (\\?\C:\... or \\?\UNC\server\share\...) of an absolute path."""
    text = str(path)
    if text.startswith("\\\\?\\") or not path.is_absolute():
        return text
    text = os.path.normpath(text)
    if text.startswith("\\\\"):
        return "\\\\?\\UNC\\" + text[2:]
    return "\\\\?\\" + text


def short_path(text: str) -> Path:
    """Undo long_path(): back to the path as the user knows it."""
    if text.startswith(_UNC_PREFIX):
        return Path("\\\\" + text[len(_UNC_PREFIX) :])
    if text.startswith(_PREFIX):
        return Path(text[len(_PREFIX) :])
    return Path(text)


def walk(root: Path) -> list[tuple[Path, list[str]]]:
    """(folder, file names) for root and everything below it; unreadable folders skipped."""
    return [(short_path(d), names) for d, _, names in os.walk(long_path(root))]


def list_files(folder: Path) -> list[Path]:
    with os.scandir(long_path(folder)) as it:
        return [folder / e.name for e in it if e.is_file()]


def list_names(folder: Path) -> list[str]:
    try:
        return os.listdir(long_path(folder))
    except OSError:
        return []


def _same_file_name(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


def _cross_device(exc: OSError) -> bool:
    return exc.errno == errno.EXDEV or getattr(exc, "winerror", None) == _ERROR_NOT_SAME_DEVICE


def move_file(src: Path, dst: Path) -> None:
    """Move src to dst. Raises FileExistsError rather than replacing anything."""
    if os.path.lexists(long_path(dst)) and not _same_file_name(src, dst):
        raise FileExistsError(errno.EEXIST, NAME_TAKEN, str(dst))
    try:
        os.rename(long_path(src), long_path(dst))
    except OSError as exc:
        if not _cross_device(exc):
            raise
        _copy_then_delete(src, dst)


def partial_path(dst: Path) -> Path:
    """Where a copy between shares is written before it gets its real name."""
    return dst.with_name(dst.name + PARTIAL_SUFFIX)


def _copy_then_delete(src: Path, dst: Path) -> None:
    # The copy gets its real name only once it is whole: a copy cut off (the NAS gone, the PC
    # switched off) never leaves a half file under the name; recovery clears the partial one.
    partial = partial_path(dst)
    with open(long_path(src), "rb") as reader:
        # Opened exclusively *outside* the cleanup: if it exists already this raises
        # FileExistsError and we never touch a file that is already there.
        writer = open(long_path(partial), "xb")  # noqa: SIM115 - closed below
        try:
            with writer:
                shutil.copyfileobj(reader, writer, 16 * 1024 * 1024)
            shutil.copystat(long_path(src), long_path(partial))
            if os.path.lexists(long_path(dst)):
                raise FileExistsError(errno.EEXIST, NAME_TAKEN, str(dst))
            os.rename(long_path(partial), long_path(dst))
        except BaseException:
            with contextlib.suppress(OSError):
                os.remove(long_path(partial))  # only the copy this call created
            raise
    try:
        os.remove(long_path(src))
    except OSError:
        os.remove(long_path(dst))  # source locked: undo the copy, keep one file
        raise


def make_dirs(path: Path) -> list[Path]:
    """Create path and any missing parents; return the folders actually created, top first."""
    missing: list[Path] = []
    current = path
    while not os.path.isdir(long_path(current)):
        missing.append(current)
        if current.parent == current:
            break
        current = current.parent
    created: list[Path] = []
    for folder in reversed(missing):
        os.mkdir(long_path(folder))
        created.append(folder)
    return created


def missing_dirs(path: Path) -> list[Path]:
    """The folders make_dirs(path) would create, top first."""
    missing: list[Path] = []
    current = path
    while not os.path.isdir(long_path(current)) and current.parent != current:
        missing.append(current)
        current = current.parent
    return list(reversed(missing))


def make_dir(path: Path) -> None:
    os.mkdir(long_path(path))


def write_new_file(path: Path, data: bytes) -> tuple[int, int]:
    """Write a file that must not exist yet. Returns (size, mtime_ns)."""
    with open(long_path(path), "xb") as f:
        f.write(data)
    return stamp(path)


def stamp(path: Path) -> tuple[int, int]:
    st = os.stat(long_path(path))
    return st.st_size, st.st_mtime_ns


def exists(path: Path) -> bool:
    return os.path.lexists(long_path(path))


def is_dir(path: Path) -> bool:
    return os.path.isdir(long_path(path))


def is_file(path: Path) -> bool:
    return os.path.isfile(long_path(path))


def exact_exists(path: Path) -> bool:
    """True if an entry with exactly this name (same capitals) exists."""
    try:
        with os.scandir(long_path(path.parent)) as it:
            return any(entry.name == path.name for entry in it)
    except OSError:
        return False


def remove_file(path: Path) -> None:
    os.remove(long_path(path))


def is_empty_dir(path: Path) -> bool:
    try:
        with os.scandir(long_path(path)) as it:
            return next(it, None) is None
    except (FileNotFoundError, NotADirectoryError):
        return False


def remove_empty_dir(path: Path) -> bool:
    if not is_empty_dir(path):
        return False
    os.rmdir(long_path(path))
    return True
