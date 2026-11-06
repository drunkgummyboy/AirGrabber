"""The undo history as the window sees it: runs and their operations, read-only.

A thin wrapper over `app.forge.db.history.HistoryRepo`, so `app/forge/ui` never sees the database
package (writes to the journal stay with the executor, undo and recovery).
"""

from __future__ import annotations

import builtins
import unicodedata
from collections.abc import Iterable, Sequence
from pathlib import Path, PureWindowsPath

from app.forge.db.history import HistoryRepo
from app.forge.models.history import Operation, RunSummary


def _folded(text: str) -> str:
    """`text` as a search compares it: no capitals, no accents ("Amélie" is found as "amelie")."""
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


def _searched_text(paths: Iterable[str | None]) -> str:
    """What a search looks in: each path's file name and the folder it is in, not the folders
    above those (they are the same for everything, "Downloads" would find it all)."""
    parts: list[str] = []
    for path in paths:
        if path:
            name = PureWindowsPath(path)
            parts += [name.name, name.parent.name]
    return _folded(" ".join(parts))


def matching_operations(
    query: str, operations: Iterable[tuple[int, int, str | None, str | None]]
) -> dict[int, set[int]]:
    """The operations whose names hold every word of `query`, by run. `operations`: (run id,
    operation id, source, target)."""
    words = _folded(query).split()
    found: dict[int, set[int]] = {}
    if words:
        for run_id, op_id, src, dst in operations:
            text = _searched_text((src, dst))
            if all(word in text for word in words):
                found.setdefault(run_id, set()).add(op_id)
    return found


class HistoryService:
    def __init__(self, repository: HistoryRepo) -> None:
        self._repository = repository

    async def list_runs(self) -> builtins.list[RunSummary]:
        """The kept runs, newest first."""
        return await self._repository.list_runs()

    async def operations(self, run_id: int) -> builtins.list[Operation]:
        """One run's operations, in the order they happened."""
        return await self._repository.operations(run_id)

    async def get_operations(self, ids: Sequence[int]) -> builtins.list[Operation]:
        return await self._repository.get_operations(ids)

    async def search(self, query: str) -> dict[int, set[int]]:
        """The kept operations whose names (before or after the rename) hold every word of
        `query`, by run."""
        return matching_operations(query, await self._repository.operation_paths())

    async def was_undone(self, src: Path) -> bool:
        """Whether a rename away from `src` was undone (the file was put back there)."""
        return await self._repository.was_undone(src)
