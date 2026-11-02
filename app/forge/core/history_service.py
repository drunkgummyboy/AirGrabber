"""The undo history as the window sees it: runs and their operations, read-only.

A thin wrapper over `app.forge.db.history.HistoryRepo`, so `app/forge/ui` never sees the database
package (writes to the journal stay with the executor, undo and recovery).
"""

from __future__ import annotations

import builtins
from collections.abc import Sequence
from pathlib import Path

from app.forge.db.history import HistoryRepo
from app.forge.models.history import Operation, RunSummary


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

    async def was_undone(self, src: Path) -> bool:
        """Whether a rename away from `src` was undone (the file was put back there)."""
        return await self._repository.was_undone(src)
