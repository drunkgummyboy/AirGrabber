"""The way back after a bad update. Standard library only: main.py runs it before the package check,
and a bad update may be exactly what broke the packages.

An update (app/services/updates.py) keeps what it replaced in `<install>/.previous`: the old app files,
a copy of the databases as they were, the two version numbers and a count of the starts that haven't
been seen to work yet.

- Going back by itself: `before_start` counts every start, `confirm_started` clears the count once the
  new version has run for a while or closed normally. When the last MAX_UNFINISHED_STARTS starts never
  got that far, the next start puts the old version back.
- Going back on request: `ask_to_go_back` (Settings) leaves a note; the restart that follows, with the
  database closed, finds it in `before_start`.

The version left behind is remembered (`skipped_version`) so it isn't installed again; any newer one is.
The databases only go back when the new version changed their layout, which the old code can't read;
the newer file then stays next to the database (`*.before-going-back`), never deleted. One thing this
can't catch: a mistake in main.py's own first lines, which run before any of this.

Nothing here raises, and logs give version numbers only, never a path.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import sqlite3
import stat
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

APP_ENTRIES = ("main.py", "requirements.txt", "app")  # keep in step with install.ps1
STAGING_DIR = ".update"
PREVIOUS_DIR = ".previous"
SKIPPED_FILE = ".skipped_version"
WENT_BACK_FILE = ".went_back"
MAX_UNFINISHED_STARTS = 2
CONFIRM_AFTER_S = 30.0  # running this long counts as a start that worked

ASIDE_SUFFIX = ".before-going-back"

_BUILDING = ".previous.new"
_FILES = "files"
_DATA = "data"
_WAS = "was"  # the version that was replaced
_NOW = "now"  # the version that replaced it
_STARTS = "starts"  # present while the new version hasn't been seen to work
_ASKED = "go_back"  # present when the user asked to go back
_UNDONE = "undone"
_RENAME_ATTEMPTS = 5  # antivirus scanners briefly lock freshly written files on Windows
_VERSION_LINE = re.compile(r"""^__version__\s*=\s*["']([^"']+)["']""", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class WentBack:
    """What a start just did: from version `left` to version `back`."""

    left: str
    back: str
    asked: bool  # the user asked; else the new version didn't start


def is_development_copy(install_dir: Path) -> bool:
    """The source folder (it has the tests, or is a git checkout) is never overwritten."""
    return (install_dir / "tests").is_dir() or (install_dir / ".git").exists()


def parse_version(source: str) -> str | None:
    """`__version__` from the text of app/__init__.py."""
    match = _VERSION_LINE.search(source)
    return match[1] if match else None


def read_version(init_file: Path) -> str | None:
    try:
        return parse_version(init_file.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


# -- what an update keeps ------------------------------------------------------------------------


def keep_previous(
    install_dir: Path, old_files: Path, *, was: str | None, now: str, databases: Sequence[Path] = ()
) -> bool:
    """Right after a swap: keep the replaced `old_files` as the way back, replacing the last one.
    False when it couldn't (a database wouldn't copy, say): that update simply has no way back."""
    building = install_dir / _BUILDING
    previous = install_dir / PREVIOUS_DIR
    try:
        remove_tree(building)
        remove_tree(previous)
        (install_dir / SKIPPED_FILE).unlink(missing_ok=True)
        if was is None or previous.exists():
            return False
        building.mkdir()
        _copy_databases(databases, building / _DATA)
        rename(old_files, building / _FILES)
        (building / _WAS).write_text(was, encoding="utf-8")
        (building / _NOW).write_text(now, encoding="utf-8")
        (building / _STARTS).write_text("0", encoding="utf-8")
        rename(building, previous)
        return True
    except (OSError, sqlite3.Error) as exc:
        log.warning("This update has no way back; keeping the old version failed: %r", type(exc))
        remove_tree(building)
        return False


def _copy_databases(databases: Sequence[Path], folder: Path) -> None:
    folder.mkdir()
    for database in databases:
        if database.exists():
            _copy_database(database, folder / database.name)


def _copy_database(source: Path, target: Path) -> None:
    """Through SQLite: right also when the app has the database open (Settings' "Update to …")."""
    origin = sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True)
    try:
        copy = sqlite3.connect(target)
        try:
            origin.backup(copy)
        finally:
            copy.close()
    finally:
        origin.close()


# -- what the app asks ---------------------------------------------------------------------------


def previous_version(install_dir: Path) -> str | None:
    """The version this copy can go back to. None without one, also when the installer has put
    another version in since (what was kept doesn't belong to it)."""
    previous = install_dir / PREVIOUS_DIR
    if is_development_copy(install_dir) or not (previous / _FILES).is_dir():
        return None
    installed = read_version(install_dir / "app" / "__init__.py")
    if installed is None or _read(previous / _NOW) != installed:
        return None
    return _read(previous / _WAS)


def skipped_version(install_dir: Path) -> str | None:
    """The version this copy went back from: not to be installed again."""
    return _read(install_dir / SKIPPED_FILE)


def ask_to_go_back(install_dir: Path) -> bool:
    """Go back at the next start (the caller restarts). False when there's nothing to go back to."""
    if previous_version(install_dir) is None:
        return False
    try:
        (install_dir / PREVIOUS_DIR / _ASKED).write_text("yes", encoding="utf-8")
    except OSError as exc:
        log.warning("Could not note the way back: %r", type(exc))
        return False
    remove_tree(install_dir / STAGING_DIR)  # a downloaded update would go in at this very exit
    return True


def going_back(install_dir: Path) -> bool:
    return (install_dir / PREVIOUS_DIR / _ASKED).exists()


def take_went_back(install_dir: Path) -> WentBack | None:
    """What the last start did, once."""
    marker = install_dir / WENT_BACK_FILE
    text = _read(marker)
    if text is None:
        return None
    with contextlib.suppress(OSError):
        marker.unlink()
    parts = text.split("\n")
    if len(parts) != 3:
        return None
    return WentBack(left=parts[0], back=parts[1], asked=parts[2] == "asked")


# -- what main.py calls --------------------------------------------------------------------------


def before_start(install_dir: Path, databases: Sequence[Path] = ()) -> bool:
    """At every start of the app. True when it went back: this process still has the left version
    loaded, so main.py starts a fresh one. Never raises."""
    try:
        return _before_start(install_dir, databases)
    except Exception:
        log.exception("Checking the last update failed")
        return False


def _before_start(install_dir: Path, databases: Sequence[Path]) -> bool:
    previous = install_dir / PREVIOUS_DIR
    if is_development_copy(install_dir) or not previous.exists():
        return False
    if previous_version(install_dir) is None:
        remove_tree(previous)  # the installer ran since
        return False
    if going_back(install_dir):
        return _go_back(install_dir, databases, asked=True)
    starts_file = previous / _STARTS
    text = _read(starts_file)
    if text is None:
        return False  # seen working
    unfinished = int(text) if text.isdecimal() else 0
    if unfinished >= MAX_UNFINISHED_STARTS:
        return _go_back(install_dir, databases, asked=False)
    starts_file.write_text(str(unfinished + 1), encoding="utf-8")
    return False


def confirm_started(install_dir: Path, running: str) -> None:
    """Version `running` works: no more counting. An update installed while it ran (Settings'
    "Update to …") has yet to start, so its count stays. Never raises."""
    previous = install_dir / PREVIOUS_DIR
    if _read(previous / _NOW) == running:
        with contextlib.suppress(OSError):
            (previous / _STARTS).unlink(missing_ok=True)


def _go_back(install_dir: Path, databases: Sequence[Path], *, asked: bool) -> bool:
    previous = install_dir / PREVIOUS_DIR
    left, back = _read(previous / _NOW), _read(previous / _WAS)
    if left is None or back is None:
        return False
    undone = previous / _UNDONE
    remove_tree(install_dir / STAGING_DIR)
    remove_tree(undone)
    undone.mkdir()
    swapped: list[str] = []
    try:
        for entry in APP_ENTRIES:
            swapped.append(entry)
            rename(install_dir / entry, undone / entry)
            rename(previous / _FILES / entry, install_dir / entry)
        _restore_databases(databases, previous / _DATA)
    except (OSError, sqlite3.Error) as exc:
        log.error("Going back to %s failed; staying on %s: %r", back, left, type(exc))
        _put_back(install_dir, previous, swapped)
        # Not again at every start. A request is answered: there's nothing more to ask.
        (previous / _ASKED).unlink(missing_ok=True)
        (previous / _STARTS).unlink(missing_ok=True)
        return False
    (install_dir / SKIPPED_FILE).write_text(left, encoding="utf-8")
    (install_dir / WENT_BACK_FILE).write_text(
        f"{left}\n{back}\n{'asked' if asked else 'failed'}", encoding="utf-8"
    )
    remove_tree(previous)
    log.warning("Went back from %s to %s (%s)", left, back, "asked" if asked else "it didn't start")
    return True


def _put_back(install_dir: Path, previous: Path, swapped: list[str]) -> None:
    for entry in reversed(swapped):
        undone = previous / _UNDONE / entry
        target = install_dir / entry
        if not undone.exists():
            continue  # never moved
        if target.exists():
            rename(target, previous / _FILES / entry)
        rename(undone, target)


def _restore_databases(databases: Sequence[Path], kept: Path) -> None:
    for database in databases:
        copy = kept / database.name
        if not database.exists():
            if copy.exists():
                shutil.copyfile(copy, database)
            continue
        if copy.exists() and _schema(database) <= _schema(copy):
            continue  # the old code reads it as it is: nothing is lost
        aside = database.with_name(database.name + ASIDE_SUFFIX)
        aside.unlink(missing_ok=True)
        rename(database, aside)
        for extra in ("-wal", "-shm"):
            database.with_name(database.name + extra).unlink(missing_ok=True)
        if copy.exists():
            shutil.copyfile(copy, database)
        # Without a copy the old version had none: it makes a new one.


def _schema(database: Path) -> int:
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    try:
        row = connection.execute("PRAGMA user_version").fetchone()
    finally:
        connection.close()
    return int(row[0])


# -- file helpers (updates.py uses them too) -------------------------------------------------------


def _read(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def rename(source: Path, destination: Path) -> None:
    for attempt in range(_RENAME_ATTEMPTS):
        try:
            os.rename(source, destination)
            return
        except PermissionError:
            if attempt == _RENAME_ATTEMPTS - 1:
                raise
            time.sleep(0.2 * (attempt + 1))


def remove_tree(path: Path) -> None:
    with contextlib.suppress(OSError):
        shutil.rmtree(path, onexc=clear_read_only)


def clear_read_only(function: Callable[[str], object], path: str, _exc: BaseException) -> None:
    """rmtree helper: Windows refuses to delete read-only files and folders."""
    os.chmod(path, stat.S_IWRITE)
    function(path)
