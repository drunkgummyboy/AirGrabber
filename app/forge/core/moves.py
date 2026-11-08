"""Forge's move records: where Forge moved each video into the Series or Movies library folder
from (forge.db's `moves`, schema v3; kept for a year, longer than the undo history).

Every rename into a library folder is recorded in the same journal step as the rename itself
(`ForgeMoveService.note` with the caller's media → Executor → HistoryRepo); an undo takes the
record out again. AirGrabber reads them (`moves_from`, `subscribe`): its Downloads page shows
where a finished download ended up.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable, Sequence
from pathlib import Path

from app.forge.core.library import inside
from app.forge.db.moves import MoveRepository, path_key
from app.forge.models.media import MediaKind, ResolvedMedia
from app.forge.models.moves import MoveNote, MoveRecord
from app.forge.models.plan import PlannedFile
from app.forge.models.settings import ForgeSettings

log = logging.getLogger(__name__)

MovesListener = Callable[[], None]


def root_key(folder: Path) -> str:
    """A library folder as Windows compares it (case, slashes, a trailing separator)."""
    return path_key(folder).rstrip("\\/")


def library_roots(settings: ForgeSettings) -> list[tuple[Path, frozenset[MediaKind]]]:
    """The library folders in use with the kind(s) each one holds (one folder may hold both)."""
    roots: dict[str, tuple[Path, set[MediaKind]]] = {}
    for folder, kind in (
        (settings.series_folder, MediaKind.EPISODE),
        (settings.movies_folder, MediaKind.MOVIE),
    ):
        if folder:
            path = Path(folder)
            roots.setdefault(root_key(path), (path, set()))[1].add(kind)
    return [(path, frozenset(kinds)) for path, kinds in roots.values()]


def library_kinds(
    path: Path, roots: Sequence[tuple[Path, frozenset[MediaKind]]]
) -> tuple[Path, frozenset[MediaKind]] | None:
    """The library folder `path` lies in (the deepest one) and the kinds it holds; None when it
    lies in none."""
    holding = [(root, kinds) for root, kinds in roots if inside(path, root)]
    if not holding:
        return None
    return max(holding, key=lambda pair: len(pair[0].parts))


class ForgeMoveService:
    """Forge's move records for Forge and AirGrabber: what a rename records, where downloads
    went, and who hears about changes."""

    def __init__(self, repo: MoveRepository, *, settings: Callable[[], ForgeSettings]) -> None:
        self._repo = repo
        self._settings = settings
        self._listeners: list[MovesListener] = []

    # --- reading --------------------------------------------------------------------------------

    async def moves_from(self, folder: Path) -> list[MoveRecord]:
        """Where the files Forge moved out of `folder` (or a folder below it) went, newest first
        (kept for a year)."""
        return await self._repo.moves_from(folder)

    async def moved_after(self, when: float) -> list[MoveRecord]:
        """The moves made after `when` (seconds since the epoch), oldest first (AirGrabber asks
        Plex to scan where they went)."""
        return await self._repo.moved_after(when)

    async def of_operation(self, op_id: int) -> MoveRecord | None:
        """What the journal's rename `op_id` recorded of its file, if anything (History's CC)."""
        return await self._repo.of_operation(op_id)

    def subscribe(self, listener: MovesListener) -> Callable[[], None]:
        """Call `listener` whenever the move records change. Returns a function that
        unsubscribes."""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            with contextlib.suppress(ValueError):
                self._listeners.remove(listener)

        return unsubscribe

    # --- what a rename records (the executor asks, the journal writes) --------------------------

    def note(self, planned: PlannedFile, media: ResolvedMedia | None = None) -> MoveNote | None:
        """The move record of renaming `planned`: when its target lies in a library folder, what
        the file is by `media` (what the caller's queue knew of it; without it the kind comes
        from the folder). None for a move elsewhere, or into a folder for both kinds with
        nothing known."""
        place = library_kinds(planned.target, library_roots(self._settings()))
        if place is None:
            return None
        kinds = place[1]
        if media is not None:
            return MoveNote(
                source=planned.source, target=planned.target, kind=media.kind, tmdb_id=media.tmdb_id
            )
        if len(kinds) == 1:
            return MoveNote(source=planned.source, target=planned.target, kind=next(iter(kinds)))
        return None

    def changed(self) -> None:
        """The journal changed the move records (HistoryRepo's `on_moves_change`)."""
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:
                log.exception("Move records listener %r failed", listener)

    def close(self) -> None:
        """Forget the listeners (Forge is closing)."""
        self._listeners.clear()


__all__ = ["ForgeMoveService", "library_kinds", "library_roots", "root_key"]
