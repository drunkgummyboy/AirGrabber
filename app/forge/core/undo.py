"""Put things back: any run, or any chosen operations, in any order, with safety checks.

Subtitles never stay behind their video. Undoing a video's rename also undoes the subtitle lines
of its run, also when only the video was chosen (History's ticks, a queue band's Undo): a subtitle
Forge fetched (a created file, also one CC ▾ fetched later: journaled in the video's run) is
removed, one moved along goes back. What is still beside the video after that (a subtitle the
owner changed since, or put there) goes back with it under the old name; when that name is taken
it stays where it is, as it is the owner's.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from pathlib import Path

from pydantic import BaseModel

from app.forge.core import fsops
from app.forge.core.activity import ActivityFeed, say_to
from app.forge.core.parser import is_video
from app.forge.core.planner import subtitle_moves, subtitle_of
from app.forge.db.history import HistoryRepo
from app.forge.models.history import Operation, OpKind, OpState, UndoState
from app.i18n import N_, t

log = logging.getLogger(__name__)

# Why an operation wasn't undone. Kept in English in the journal (`undo_note`) and in
# `UndoResult.skipped`: t() them where they are shown. An OS error's text is kept as it is.
MOVED_AGAIN = N_("file was moved or renamed again")
OLD_NAME_TAKEN = N_("something else now has the old name")
CHANGED_SINCE = N_("file has changed since")
ALREADY_GONE = N_("already gone")
FOLDER_NOT_EMPTY = N_("folder isn't empty")
JUNK_IS_GONE = N_("deleted junk can't be restored")


class UndoResult(BaseModel):
    undone: int = 0
    skipped: list[tuple[str, str]] = []  # (path, reason in English: t() it where shown)


def _wants_undo(op: Operation) -> bool:
    if op.kind is OpKind.DELETE_FILE:  # reported once, then left alone
        return op.state is OpState.DONE and op.undo_state is None
    return op.undoable


def _order(ops: Sequence[Operation]) -> list[Operation]:
    """Newest first; folders we created go last, deepest first, once they've been emptied."""
    dirs = [o for o in ops if o.kind is OpKind.CREATE_DIR]
    others = [o for o in ops if o.kind is not OpKind.CREATE_DIR]
    others.sort(key=lambda o: (o.run_id, o.seq), reverse=True)
    dirs.sort(key=lambda o: len(Path(o.dst or "").parts), reverse=True)
    return others + dirs


def _subtitles_back(video: Path, old: Path) -> None:
    """The subtitles still beside a video just put back from `video` to `old` go with it. One
    that can't stays; the undo stands."""
    for src, dst in subtitle_moves(video, old, fsops.list_names(video.parent)):
        try:
            fsops.move_file(src, dst)
        except OSError as exc:
            log.warning("Subtitle not put back: %s (%s)", src.name, exc.strerror or type(exc).__name__)


def _reverse(op: Operation) -> tuple[UndoState, str | None]:
    """Undo one operation on disk. Returns the outcome and, when not plainly done, why."""
    src = Path(op.src) if op.src else None
    dst = Path(op.dst) if op.dst else None
    match op.kind:
        case OpKind.RENAME:
            assert src is not None and dst is not None
            if not fsops.exists(dst):
                return UndoState.SKIPPED, MOVED_AGAIN
            same_file = str(src).casefold() == str(dst).casefold()
            if fsops.exists(src) and not same_file:
                return UndoState.SKIPPED, OLD_NAME_TAKEN
            fsops.make_dirs(src.parent)
            fsops.move_file(dst, src)
            if is_video(dst):
                _subtitles_back(dst, src)
            return UndoState.DONE, None
        case OpKind.CREATE_FILE:
            assert dst is not None
            if not fsops.exists(dst):
                return UndoState.DONE, ALREADY_GONE
            if op.size is None or fsops.stamp(dst) != (op.size, op.mtime_ns):
                return UndoState.SKIPPED, CHANGED_SINCE
            fsops.remove_file(dst)
            return UndoState.DONE, None
        case OpKind.CREATE_DIR:
            assert dst is not None
            if not fsops.is_dir(dst):
                return UndoState.DONE, None
            if not fsops.remove_empty_dir(dst):
                return UndoState.SKIPPED, FOLDER_NOT_EMPTY
            return UndoState.DONE, None
        case OpKind.REMOVE_DIR:
            assert src is not None
            fsops.make_dirs(src)
            return UndoState.DONE, None
        case OpKind.DELETE_FILE:
            return UndoState.SKIPPED, JUNK_IS_GONE


class UndoService:
    def __init__(self, history: HistoryRepo, *, activity: ActivityFeed | None = None) -> None:
        self._history = history
        self._say = say_to(activity)  # the Activity strip, next to each English log line
        self._lock = asyncio.Lock()  # one undo at a time; ops are re-read inside it

    async def undo_run(self, run_id: int) -> UndoResult:
        async with self._lock:
            return await self._undo(await self._history.operations(run_id))

    async def undo_operations(self, op_ids: Sequence[int]) -> UndoResult:
        async with self._lock:
            chosen = await self._history.get_operations(op_ids)
            return await self._undo(await self._with_subtitles(chosen))

    async def _with_subtitles(self, chosen: Sequence[Operation]) -> list[Operation]:
        """The chosen operations and the subtitle lines of the videos' renames among them."""
        videos: dict[int, list[Path]] = {}
        for op in chosen:
            if op.kind is OpKind.RENAME and op.dst and is_video(Path(op.dst)):
                videos.setdefault(op.run_id, []).append(Path(op.dst))
        ops = {op.id: op for op in chosen}
        for run_id, paths in videos.items():
            for op in await self._history.operations(run_id):
                if (
                    op.id not in ops
                    and op.dst
                    and op.kind in (OpKind.RENAME, OpKind.CREATE_FILE)
                    and any(subtitle_of(path, Path(op.dst)) for path in paths)
                ):
                    ops[op.id] = op
        return list(ops.values())

    async def _undo(self, ops: Sequence[Operation]) -> UndoResult:
        result = UndoResult()
        for op in _order([o for o in ops if _wants_undo(o)]):
            path = op.dst or op.src or ""
            await self._history.mark_undo(op.id, UndoState.PENDING)
            logged: str | None
            try:
                state, note = await asyncio.to_thread(_reverse, op)
                logged = note
            except OSError as exc:
                state, note = UndoState.SKIPPED, str(exc)
                logged = exc.strerror or type(exc).__name__  # str(exc) names the full path
            await self._history.mark_undo(op.id, state, note)
            if state is UndoState.DONE:
                result.undone += 1
                if op.kind is OpKind.RENAME and op.src:
                    log.info("Put back: %s", Path(op.src).name, extra={"success": True})
                    self._say(t("Put back: {name}", name=Path(op.src).name), success=True)
            if note:
                result.skipped.append((path, note))
                log.warning("Not undone: %s (%s)", Path(path).name, logged)
                reason = t(logged or "")
                self._say(
                    t("Not undone: {name} ({reason})", name=Path(path).name, reason=reason),
                    level=logging.WARNING,
                )
        return result
