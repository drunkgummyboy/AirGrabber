"""Put things back: any run, or any chosen operations, in any order, with safety checks.

Subtitles never stay behind their video. Undoing a video's rename also undoes the subtitle lines
of its run, also when only the video was chosen (History's ticks, a queue band's Undo): a subtitle
Forge fetched (a created file, also one CC ▾ fetched later: journaled in the video's run) is
removed, one moved along goes back. What is still beside the video after that (a subtitle the
owner changed since, or put there) goes back with it under the old name; when that name is taken
it stays where it is, as it is the owner's.

A replace ("Use this one instead") moved the library's copy aside in the same run,
right before its replacement's rename: a card's Undo of that file, or History's tick on it, takes
that move too (`_aside_of`), so both go back.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Collection, Sequence
from pathlib import Path

from pydantic import BaseModel

from app.forge.core import fsops
from app.forge.core.activity import ActivityFeed, say_to
from app.forge.core.library import inside
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
    undone_ids: list[int] = []  # the operations put back


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


def _below(path: Path, folders: Collection[Path]) -> bool:
    """`path` is one of `folders` or inside one."""
    return any(inside(path, folder) for folder in folders)


def _named_after(name: str, stem: str) -> bool:
    """`name` is a file named after `stem`: the stem, then ".", "-", a space or nothing
    ("Alien (1979)-fanart.jpg", not "Alien (1979) Director's Cut - Poster.jpg")."""
    if not name.casefold().startswith(stem.casefold()):
        return False
    rest = name[len(stem) :]
    return rest == "" or rest[0] in ".-" or (rest[0] == " " and not rest[1:2].isalnum())


def _own_folders(
    starts: Collection[Path], others: Collection[Path], created: Collection[Path]
) -> list[Path]:
    """The folders that hold only these files of a run: each start folder when no other file
    of the run went there, and above it each folder the run created that holds none either."""
    own: list[Path] = []
    for start in starts:
        folder = start
        while not any(_below(other, [folder]) for other in others):
            own.append(folder)
            if folder.parent == folder or not _below(folder.parent, created):
                break
            folder = folder.parent
    return own


def _aside_of(op: Operation, chosen: Sequence[Operation]) -> bool:
    """`op` moved a library copy out of the way of one of the `chosen` renames (a replace): a
    video's move in the same run just before it, from that file's name (or the same name as
    another kind of video) to another folder.

    Found by its shape, as the journal has no field for it (no schema change, so an older
    version still reads forge.db). A normal run matches only in a near impossible case: within
    one run, a video moved out of its folder whose old name another file of that run then took;
    a card's Undo of that file would then put the first one back too. Accepted as is."""
    if op.kind is not OpKind.RENAME or not op.src or not op.dst or not is_video(Path(op.src)):
        return False
    src = Path(op.src)
    if _below(Path(op.dst), [src.parent]):
        return False
    for rename in chosen:
        target = Path(rename.dst or "")
        if (
            rename.run_id == op.run_id
            and op.seq < rename.seq
            and os.path.normcase(str(src.parent)) == os.path.normcase(str(target.parent))
            and src.stem.casefold() == target.stem.casefold()
        ):
            return True
    return False


def card_operations(ops: Sequence[Operation], renames: Collection[int]) -> list[Operation]:
    """What a queue card's Undo takes from one run's `ops` for the card's `renames` (op ids).

    All of the run's files on the card: the whole run, as History's "Undo run". Otherwise those
    renames and what the run did for them alone: in the folders no other file of the run went
    to (the film's folder, a show's own folder the run made), every folder made and every file
    saved there (poster, backdrop, NFO, trailer); beside a file, the saved ones named after it;
    the download folders put away that held only these files. Subtitles go along in `_undo`.
    A library copy a file replaced (moved aside just before: `_aside_of`) goes back too, and the
    folders made for it alone.
    """
    chosen = [o for o in ops if o.id in renames and o.kind is OpKind.RENAME and o.dst]
    if not chosen:
        return []
    targets = [Path(o.dst or "") for o in chosen]
    asides = [o for o in ops if o.id not in renames and o.undoable and _aside_of(o, chosen)]
    aside_ids = {o.id for o in asides}
    moved_aside = [Path(o.dst or "") for o in asides]
    others = [
        o
        for o in ops
        if o.kind is OpKind.RENAME
        and o.undoable
        and o.id not in renames
        and o.id not in aside_ids
        and o.dst
        and not any(subtitle_of(video, Path(o.dst)) for video in (*targets, *moved_aside))
    ]
    if not others:
        return list(ops)
    created = [Path(o.dst) for o in ops if o.kind is OpKind.CREATE_DIR and o.dst]
    folders = _own_folders(
        {t.parent for t in targets}, [Path(o.dst or "") for o in others], created
    )
    sources = [Path(o.src) for o in chosen if o.src]
    put_away = [Path(o.src) for o in ops if o.kind is OpKind.REMOVE_DIR and o.src]
    download_folders = _own_folders(
        {s.parent for s in sources}, [Path(o.src) for o in others if o.src], put_away
    )
    aside_folders = _own_folders(
        {p.parent for p in moved_aside}, [Path(o.dst or "") for o in others], created
    )
    taken = {o.id for o in chosen} | aside_ids
    for op in ops:
        if op.kind in (OpKind.CREATE_FILE, OpKind.CREATE_DIR) and op.dst:
            path = Path(op.dst)
            beside = op.kind is OpKind.CREATE_FILE and any(
                path.parent == t.parent and _named_after(path.name, t.stem) for t in targets
            )
            made_aside = op.kind is OpKind.CREATE_DIR and _below(path, aside_folders)
            if beside or made_aside or _below(path, folders):
                taken.add(op.id)
        elif op.kind is OpKind.REMOVE_DIR and op.src and _below(Path(op.src), download_folders):
            taken.add(op.id)
    return [o for o in ops if o.id in taken]


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
            chosen = await self._with_asides(await self._history.get_operations(op_ids))
            return await self._undo(await self._with_subtitles(chosen))

    async def undo_card(self, run_id: int, renames: Collection[int]) -> UndoResult:
        """A queue card's Undo: its files of run `run_id` (`renames`, op ids) and what the run
        did for them alone (`card_operations`); all of the run's files: the whole run.

        In a flat folder (films side by side) that got no other renamed file of the run,
        everything the run saved there goes along, a poster of a file of the same run that was
        already named (skipped) included."""
        async with self._lock:
            chosen = card_operations(await self._history.operations(run_id), renames)
            return await self._undo(await self._with_subtitles(chosen))

    async def _with_asides(self, chosen: Sequence[Operation]) -> list[Operation]:
        """The chosen operations and the library copies their renames replaced (`_aside_of`):
        undoing a replace's file never leaves its place in the library empty."""
        renames = [op for op in chosen if op.kind is OpKind.RENAME and op.dst]
        ops = {op.id: op for op in chosen}
        for run_id in {op.run_id for op in renames}:
            for op in await self._history.operations(run_id):
                if op.id not in ops and op.undoable and _aside_of(op, renames):
                    ops[op.id] = op
        return list(ops.values())

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
                result.undone_ids.append(op.id)
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
