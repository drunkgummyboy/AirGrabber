"""Runs and their operations: written before each change, settled right after.

A rename's move record (app/forge/db/moves.py) goes into the same transaction that settles it
or its undo; `on_moves_change` hears about it once that transaction is committed.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Collection, Iterable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from app.forge.db.database import Database
from app.forge.db.moves import forget_move, record_move
from app.forge.models.history import (
    HISTORY_LIMIT,
    Operation,
    OpKind,
    OpState,
    RunSource,
    RunSummary,
    UndoState,
)
from app.forge.models.media import MediaKind
from app.forge.models.moves import MoveNote

_OP_COLUMNS = (
    "id, run_id, seq, kind, src, dst, size, mtime_ns, state, error, "
    "undo_state, undo_note, undone_at"
)

_SUMMARY_SQL = """
SELECT r.id, r.started_at, r.finished_at, r.source, r.preset_name, r.kinds,
  COALESCE(SUM(o.kind = 'rename' AND o.state = 'done'), 0),
  COALESCE(SUM(o.kind = 'create_file' AND o.state = 'done'), 0),
  COALESCE(SUM(o.state = 'done' AND o.kind != 'delete_file'
               AND (o.undo_state IS NULL OR o.undo_state != 'done')), 0),
  COALESCE(SUM(o.undo_state = 'done'), 0)
FROM runs r LEFT JOIN operations o ON o.run_id = r.id
GROUP BY r.id
ORDER BY r.started_at DESC, r.id DESC
"""


def _ts(value: float | None) -> datetime | None:
    return datetime.fromtimestamp(value) if value is not None else None


def _operation(row: Iterable[Any]) -> Operation:
    (op_id, run_id, seq, kind, src, dst, size, mtime_ns, state, error, undo_state, note, undone) = (
        row
    )
    return Operation(
        id=op_id,
        run_id=run_id,
        seq=seq,
        kind=OpKind(kind),
        src=src,
        dst=dst,
        size=size,
        mtime_ns=mtime_ns,
        state=OpState(state),
        error=error,
        undo_state=UndoState(undo_state) if undo_state else None,
        undo_note=note,
        undone_at=_ts(undone),
    )


class HistoryRepo:
    def __init__(
        self,
        db: Database,
        *,
        on_moves_change: Callable[[], None] | None = None,
    ) -> None:
        self._db = db
        self._on_moves_change = on_moves_change

    def _moves_changed(self, changed: bool) -> None:
        if changed and self._on_moves_change is not None:
            self._on_moves_change()

    async def start_run(
        self,
        source: RunSource,
        preset_name: str | None,
        started_at: datetime | None = None,
        kinds: Collection[MediaKind] = (),
    ) -> int:
        when = started_at.timestamp() if started_at else time.time()
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO runs (started_at, source, preset_name, kinds) VALUES (?, ?, ?, ?)",
                (when, source.value, preset_name, ",".join(sorted(k.value for k in set(kinds)))),
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def record_pending(
        self, run_id: int, kind: OpKind, src: Path | None, dst: Path | None
    ) -> int:
        async with self._db.transaction() as conn:
            cursor = await conn.execute(
                "INSERT INTO operations (run_id, seq, kind, src, dst, state) VALUES "
                "(?, (SELECT COALESCE(MAX(seq), 0) + 1 FROM operations WHERE run_id = ?), "
                "?, ?, ?, ?)",
                (
                    run_id,
                    run_id,
                    kind.value,
                    str(src) if src else None,
                    str(dst) if dst else None,
                    OpState.PENDING.value,
                ),
            )
        assert cursor.lastrowid is not None
        return cursor.lastrowid

    async def mark_done(
        self,
        op_id: int,
        size: int | None = None,
        mtime_ns: int | None = None,
        dst: Path | None = None,
        move: MoveNote | None = None,
    ) -> None:
        """Settle a change as done; for a rename into a library folder, `move` is its move
        record, written in the same transaction."""
        changed = False
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE operations SET state = ?, size = COALESCE(?, size), "
                "mtime_ns = COALESCE(?, mtime_ns), dst = COALESCE(?, dst) WHERE id = ?",
                (OpState.DONE.value, size, mtime_ns, str(dst) if dst else None, op_id),
            )
            if move is not None:
                changed = await record_move(conn, op_id, move)
        self._moves_changed(changed)

    async def mark_failed(self, op_id: int, error: str) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE operations SET state = ?, error = ? WHERE id = ?",
                (OpState.FAILED.value, error, op_id),
            )

    async def finish_run(self, run_id: int) -> bool:
        """Close a run. A run that changed nothing is removed; returns whether it was kept.

        Runs with unsettled (pending) changes are kept so crash recovery can settle them.
        Only the newest HISTORY_LIMIT runs stay.
        """
        async with self._db.transaction() as conn:
            rows = list(
                await conn.execute_fetchall(
                    "SELECT COUNT(*) FROM operations WHERE run_id = ? AND state IN (?, ?)",
                    (run_id, OpState.DONE.value, OpState.PENDING.value),
                )
            )
            kept = bool(rows and rows[0][0])
            if kept:
                await conn.execute(
                    "UPDATE runs SET finished_at = ? WHERE id = ?", (time.time(), run_id)
                )
            else:
                await conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
            await conn.execute(
                "DELETE FROM runs WHERE id NOT IN "
                "(SELECT id FROM runs ORDER BY started_at DESC, id DESC LIMIT ?)",
                (HISTORY_LIMIT,),
            )
        return kept

    async def list_runs(self) -> list[RunSummary]:
        rows = await self._db.fetch_all(_SUMMARY_SQL)
        return [
            RunSummary(
                id=r[0],
                started_at=datetime.fromtimestamp(r[1]),
                finished_at=_ts(r[2]),
                source=RunSource(r[3]),
                preset_name=r[4],
                kinds=frozenset(MediaKind(k) for k in r[5].split(",") if k),
                file_count=r[6],
                created_count=r[7],
                undoable_count=r[8],
                undone_count=r[9],
            )
            for r in rows
        ]

    async def was_undone(self, src: Path) -> bool:
        """Whether a rename away from `src` was undone (the file was put back there)."""
        row = await self._db.fetch_one(
            "SELECT 1 FROM operations WHERE kind = ? AND src = ? COLLATE NOCASE "
            "AND undo_state = ? LIMIT 1",
            (OpKind.RENAME.value, str(src), UndoState.DONE.value),
        )
        return row is not None

    async def _select_ops(self, where: str, params: Sequence[Any]) -> list[Operation]:
        rows = await self._db.fetch_all(
            f"SELECT {_OP_COLUMNS} FROM operations WHERE {where} ORDER BY run_id, seq", params
        )
        return [_operation(r) for r in rows]

    async def operations(self, run_id: int) -> list[Operation]:
        return await self._select_ops("run_id = ?", (run_id,))

    async def get_operations(self, ids: Sequence[int]) -> list[Operation]:
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return await self._select_ops(f"id IN ({marks})", list(ids))

    async def pending_operations(self) -> list[Operation]:
        return await self._select_ops("state = ?", (OpState.PENDING.value,))

    async def pending_undos(self) -> list[Operation]:
        return await self._select_ops("undo_state = ?", (UndoState.PENDING.value,))

    async def mark_undo(self, op_id: int, state: UndoState, note: str | None = None) -> None:
        """Settle an undo; a done one also takes the rename's move record out, in the same
        transaction."""
        undone_at = time.time() if state is UndoState.DONE else None
        changed = False
        async with self._db.transaction() as conn:
            await conn.execute(
                "UPDATE operations SET undo_state = ?, undo_note = ?, undone_at = ? WHERE id = ?",
                (state.value, note, undone_at, op_id),
            )
            if state is UndoState.DONE:
                changed = await forget_move(conn, op_id)
        self._moves_changed(changed)
