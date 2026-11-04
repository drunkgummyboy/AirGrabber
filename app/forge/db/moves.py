"""The move records (`moves`, schema v3): where Forge moved each video into a library folder from.

A rename's record is written by the journal itself (`record_move` / `forget_move`, called by
HistoryRepo inside the transaction that settles the rename or its undo), so the records can
never disagree with the journal about a move. Reading goes through MoveRepository.

Paths are matched by `path_key` (os.path.normcase): the way Windows compares them.
"""

from __future__ import annotations

import os
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from app.forge.db.database import Database
from app.forge.models.media import MediaKind
from app.forge.models.moves import MOVES_KEPT_S, MoveNote, MoveRecord

_MOVE_COLUMNS = "src, dst, moved_at, kind, tmdb_id, run_id"


def path_key(path: Path | str) -> str:
    """How a path is matched in the records: Windows' own comparison (case and slashes)."""
    return os.path.normcase(str(path))


def _below(folder: Path) -> tuple[str, str]:
    r"""The `path_key` range of everything below `folder`: keys from `c:\lib\` up to (not
    including) `c:\lib]` (']' follows '\' in code-point order, which SQLite's BINARY keeps)."""
    prefix = path_key(folder).rstrip("\\/") + "\\"
    return prefix, prefix[:-1] + "]"


def _move(row: Sequence[Any]) -> MoveRecord:
    src, dst, moved_at, kind, tmdb_id, run_id = row
    return MoveRecord(
        source=Path(src),
        target=Path(dst),
        moved_at=datetime.fromtimestamp(moved_at),
        kind=MediaKind(kind),
        tmdb_id=tmdb_id,
        run_id=run_id,
    )


async def record_move(
    conn: aiosqlite.Connection, op_id: int, note: MoveNote, when: float | None = None
) -> bool:
    """A rename into a library folder (journal row `op_id`) has landed: record where the file
    came from; records older than a year go meanwhile. Call it inside the transaction that marks
    the rename done. Returns True (the records changed)."""
    when = time.time() if when is None else when
    await conn.execute(
        "INSERT INTO moves (op_id, run_id, src, src_key, dst, moved_at, kind, tmdb_id) "
        "VALUES (?, (SELECT run_id FROM operations WHERE id = ?), ?, ?, ?, ?, ?, ?)",
        (
            op_id,
            op_id,
            str(note.source),
            path_key(note.source),
            str(note.target),
            when,
            note.kind.value,
            note.tmdb_id,
        ),
    )
    await conn.execute("DELETE FROM moves WHERE moved_at < ?", (when - MOVES_KEPT_S,))
    return True


async def forget_move(conn: aiosqlite.Connection, op_id: int) -> bool:
    """The rename `op_id` was undone: its move record goes. Call it inside the transaction that
    marks the undo done. Returns whether a record went."""
    cursor = await conn.execute("DELETE FROM moves WHERE op_id = ?", (op_id,))
    return cursor.rowcount > 0


class MoveRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def moves_from(self, folder: Path) -> list[MoveRecord]:
        """The moves out of `folder` (or a folder below it), newest first."""
        low, high = _below(folder)
        rows = await self._db.fetch_all(
            f"SELECT {_MOVE_COLUMNS} FROM moves WHERE src_key >= ? AND src_key < ? "
            "ORDER BY moved_at DESC, id DESC",
            (low, high),
        )
        return [_move(r) for r in rows]

    async def of_operation(self, op_id: int) -> MoveRecord | None:
        """The record of the rename `op_id` (None: it went nowhere near a library folder, or
        the record is older than a year)."""
        row = await self._db.fetch_one(
            f"SELECT {_MOVE_COLUMNS} FROM moves WHERE op_id = ?", (op_id,)
        )
        return None if row is None else _move(row)
