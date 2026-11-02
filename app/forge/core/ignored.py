"""Ignored files and folders: what Auto-rename leaves alone (the owner's "Ignore" in the queue,
listed in MediaForge Settings › Library & watching).

IgnoreService wraps `app.forge.db.ignored.IgnoredRepo` (`app/forge/ui` never sees the database
package) and keeps what is ignored in memory as an `IgnoreList`: a frozen snapshot the renamer
hands to `watch.candidates` in a worker thread. Ignoring only applies to watching and
Auto-rename; a file added by hand is never refused.

`top_folder(path, watched)`: the folder "Ignore this whole folder" means, the download's own
folder right under the watched folder (a course, a season pack); None for a file directly in
the watched folder.

Paths are never logged: a file's or folder's last name only.
"""

from __future__ import annotations

import builtins
import contextlib
import logging
from collections.abc import Callable, Iterable
from pathlib import Path

from app.forge.core.activity import ActivityFeed, say_to
from app.forge.db.ignored import IgnoredRepo, ignore_key
from app.forge.models.ignored import IgnoredPath
from app.i18n import N_, t

log = logging.getLogger(__name__)

IGNORED_LINE = N_("Ignored: {name}. MediaForge won't pick it up again.")

IgnoreListener = Callable[[tuple[IgnoredPath, ...]], None]


class IgnoreList:
    """A snapshot of what is ignored, by path key (safe to use from a worker thread)."""

    def __init__(self, entries: Iterable[IgnoredPath] = ()) -> None:
        files: set[str] = set()
        folders: set[str] = set()
        for entry in entries:
            (folders if entry.folder else files).add(ignore_key(entry.path))
        self._files = frozenset(files)
        self._folders = frozenset(folders)

    def __bool__(self) -> bool:
        return bool(self._files or self._folders)

    def folder_ignored(self, folder: Path | str) -> bool:
        """Whether `folder` is an ignored folder or lies under one."""
        return self._under_folder(ignore_key(folder), itself=True)

    def ignored(self, path: Path | str) -> bool:
        """Whether the file `path` is ignored, itself or by a folder above it."""
        key = ignore_key(path)
        return key in self._files or self._under_folder(key, itself=False)

    def _under_folder(self, key: str, *, itself: bool) -> bool:
        if itself and key in self._folders:
            return True
        return any(key.startswith(folder.rstrip("\\/") + "\\") for folder in self._folders)


def top_folder(path: Path, watched: Path) -> Path | None:
    """The folder right under `watched` that holds `path` (the download's own folder), or None
    when `path` sits directly in `watched` (or isn't under it)."""
    prefix = ignore_key(watched).rstrip("\\/") + "\\"  # compared as Windows does
    key = ignore_key(path)
    if not key.startswith(prefix) or len(Path(key[len(prefix) :]).parts) < 2:
        return None
    return watched / path.parts[len(watched.parts)]


class IgnoreService:
    """Ignore and stop ignoring; `snapshot` for the renamer, `subscribe` for the Settings box."""

    def __init__(self, repository: IgnoredRepo, *, activity: ActivityFeed | None = None) -> None:
        self._repository = repository
        self._say = say_to(activity)
        self._entries: tuple[IgnoredPath, ...] = ()
        self._snapshot = IgnoreList()
        self._listeners: list[IgnoreListener] = []

    async def load(self) -> None:
        """Read what is ignored (Forge's start)."""
        self._set(tuple(await self._repository.list()))

    @property
    def entries(self) -> tuple[IgnoredPath, ...]:
        """What is ignored, oldest first."""
        return self._entries

    def snapshot(self) -> IgnoreList:
        return self._snapshot

    async def list(self) -> builtins.list[IgnoredPath]:
        return list(self._entries)

    async def ignore(self, path: Path, *, folder: bool) -> IgnoredPath:
        """Ignore a file, or a folder with everything under it. The Activity line names it."""
        entry = await self._repository.add(path, folder=folder)
        others = [e for e in self._entries if ignore_key(e.path) != ignore_key(path)]
        self._set((*others, entry))
        log.info("Ignoring %s %s", "folder" if folder else "file", entry.name)
        self._say(t(IGNORED_LINE, name=entry.name))
        return entry

    async def stop_ignoring(self, path: Path) -> bool:
        """Auto-rename may pick `path` up again. Returns whether it was ignored."""
        removed = await self._repository.remove(path)
        self._set(tuple(e for e in self._entries if ignore_key(e.path) != ignore_key(path)))
        if removed:
            log.info("No longer ignoring %s", path.name or path.anchor)
        return removed

    def subscribe(self, listener: IgnoreListener) -> Callable[[], None]:
        """Call `listener` with every change. Returns a function that unsubscribes it."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(listener)

        return unsubscribe

    def _set(self, entries: tuple[IgnoredPath, ...]) -> None:
        self._entries = entries
        self._snapshot = IgnoreList(entries)
        for listener in list(self._listeners):
            try:
                listener(entries)
            except Exception:
                log.exception("An ignore listener failed")
