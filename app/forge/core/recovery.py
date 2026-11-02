"""After a crash, settle journal entries left 'pending' by checking what's on disk."""

from __future__ import annotations

import asyncio
import contextlib
import os
from pathlib import Path

from app.forge.core import fsops
from app.forge.db.history import HistoryRepo
from app.forge.models.history import Operation, OpKind, UndoState
from app.i18n import N_

# The journal note for a change (or an undo) a crash cut off: kept in English, t() it where shown.
INTERRUPTED = N_("interrupted")


def _path(value: str | None) -> Path | None:
    return Path(value) if value else None


def _reachable(*paths: Path | None) -> bool:
    """False when none of the folders involved can be seen (e.g. the NAS is offline)."""
    folders = [p.parent for p in paths if p is not None]
    return any(fsops.is_dir(folder) for folder in folders)


def _exists(path: Path | None) -> bool:
    return path is not None and fsops.exists(path)


def _rename_landed(src: Path, dst: Path) -> bool:
    if os.path.normcase(str(src)) == os.path.normcase(str(dst)):  # case-only rename
        return fsops.exact_exists(dst)
    return _exists(dst) and not _exists(src)


def _change_happened(op: Operation) -> bool | None:
    """Whether the change reached the disk; None when that can't be told right now."""
    src, dst = _path(op.src), _path(op.dst)
    if not _reachable(src, dst):
        return None
    match op.kind:
        case OpKind.RENAME:
            assert src is not None and dst is not None
            return _rename_landed(src, dst)
        case OpKind.CREATE_FILE | OpKind.CREATE_DIR:
            return _exists(dst)
        case OpKind.REMOVE_DIR | OpKind.DELETE_FILE:
            return not _exists(src)


def _undo_happened(op: Operation) -> bool | None:
    src, dst = _path(op.src), _path(op.dst)
    if not _reachable(src, dst):
        return None
    match op.kind:
        case OpKind.RENAME:
            assert src is not None and dst is not None
            return _rename_landed(dst, src)
        case OpKind.CREATE_FILE | OpKind.CREATE_DIR:
            return not _exists(dst)
        case OpKind.REMOVE_DIR:
            return _exists(src)
        case OpKind.DELETE_FILE:
            return False


def _clear_partial(dst: Path) -> None:
    """A copy between shares cut off by a crash: its partial file goes; the source is intact."""
    partial = fsops.partial_path(dst)
    if _exists(partial):
        with contextlib.suppress(OSError):
            fsops.remove_file(partial)


async def recover(history: HistoryRepo) -> int:
    """Settle every pending change and pending undo whose folders can be seen; returns how many."""
    settled = 0
    for op in await history.pending_operations():
        if op.kind is OpKind.RENAME and op.dst:
            await asyncio.to_thread(_clear_partial, Path(op.dst))
        happened = await asyncio.to_thread(_change_happened, op)
        if happened is None:
            continue  # folder not reachable now: try again next start
        if happened:
            size = mtime = None
            if op.kind is OpKind.CREATE_FILE and op.dst:
                size, mtime = await asyncio.to_thread(fsops.stamp, Path(op.dst))
            await history.mark_done(op.id, size=size, mtime_ns=mtime)
        else:
            await history.mark_failed(op.id, INTERRUPTED)
        settled += 1
    for op in await history.pending_undos():
        undone = await asyncio.to_thread(_undo_happened, op)
        if undone is None:
            continue
        if undone:
            await history.mark_undo(op.id, UndoState.DONE)
        else:
            await history.mark_undo(op.id, UndoState.SKIPPED, INTERRUPTED)
        settled += 1
    return settled
