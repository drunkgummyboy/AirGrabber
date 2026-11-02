"""Carry out a Plan. Every change is journaled before it happens and settled right after.

With `moves` (ForgeMoveService), a rename into the Series or Movies folder also writes a move
record in the same journal step, with what the caller's `media` (by item id) says the file is.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Collection, Mapping
from functools import partial
from pathlib import Path
from typing import Literal, Protocol, TypeVar, cast

from pydantic import BaseModel

from app.forge.core import fsops
from app.forge.core.activity import ActivityFeed, say_to
from app.forge.core.cleanup import (
    download_busy,
    download_leftovers,
    find_cleanup_targets,
    videos_left,
)
from app.forge.core.planner import subtitle_moves
from app.forge.core.undo import FOLDER_NOT_EMPTY
from app.forge.db.history import HistoryRepo
from app.forge.models.history import OpKind, RunSource
from app.forge.models.media import MediaKind, ResolvedMedia
from app.forge.models.moves import MoveNote
from app.forge.models.plan import Plan, PlannedFile, PlannedTrailer
from app.i18n import N_, t, tn

log = logging.getLogger(__name__)
T = TypeVar("T")
_SUCCESS = {"success": True}
# Journal notes are kept in English (the language can change between runs): t() them where
# they are shown. An OS error's own text is kept as it is (t() leaves it unchanged).
NOTHING_DOWNLOADED = N_("nothing downloaded")


class ProgressEvent(BaseModel):
    item_id: int | None
    status: Literal["working", "done", "error", "skipped"]
    message: str  # shown as it is: already in the interface language
    fraction: float


class RunResult(BaseModel):
    run_id: int | None = None
    renamed: int = 0
    skipped: int = 0
    failed: int = 0
    posters: int = 0
    trailers: int = 0
    cleaned: int = 0
    pruned: int = 0
    subtitles: int = 0  # subtitles moved along with their video
    cancelled: bool = False
    errors: list[str] = []


class PosterFetcher(Protocol):
    async def image(self, path: str, size: str) -> bytes: ...


class TrailerDownloader(Protocol):
    async def download(self, trailer: PlannedTrailer) -> Path | None: ...


class MoveNotes(Protocol):
    """The move record a rename writes (app/forge/core/moves.py)."""

    def note(self, planned: PlannedFile, media: ResolvedMedia | None) -> MoveNote | None: ...


def log_reason(exc: BaseException) -> str:
    """Why something failed, for the log: an OS error's own text without the path it names
    (paths can hold the user name), otherwise just the exception's type."""
    if isinstance(exc, OSError) and exc.strerror:
        return exc.strerror
    return type(exc).__name__


def _journal_text(exc: BaseException) -> str:
    """The error kept in the journal (and shown in History). An httpx error's text can hold a
    URL, so it is kept as its type only."""
    if type(exc).__module__.split(".")[0] in ("httpx", "httpcore"):
        return type(exc).__name__
    return str(exc) or type(exc).__name__


def _remove_dir_or_fail(folder: Path) -> None:
    if not fsops.remove_empty_dir(folder):
        raise OSError(FOLDER_NOT_EMPTY)


def _subtitles_of(source: Path, target: Path) -> list[tuple[Path, Path]]:
    """The subtitles to move along with a video just moved from `source` to `target`."""
    return subtitle_moves(source, target, fsops.list_names(source.parent))


class Executor:
    def __init__(
        self,
        history: HistoryRepo,
        posters: PosterFetcher | None,
        trailers: TrailerDownloader | None,
        *,
        activity: ActivityFeed | None = None,
        moves: MoveNotes | None = None,
    ) -> None:
        self._history = history
        self._posters = posters
        self._trailers = trailers
        self._say = say_to(activity)  # the Activity strip, next to each English log line
        self._moves = moves

    def _note(self, planned: PlannedFile, media: ResolvedMedia | None) -> MoveNote | None:
        """This rename's move record (None: it isn't one); a fault there never stops a rename."""
        if self._moves is None:
            return None
        try:
            return self._moves.note(planned, media)
        except Exception as exc:
            log.error("Move record note failed: %s", type(exc).__name__)
            return None

    async def _journaled(
        self,
        op_id: int,
        work: Callable[[], T],
        *,
        stamped: bool = False,
        move: MoveNote | None = None,
    ) -> T:
        """Run one disk change in a thread and settle its journal row.

        If the run is cancelled meanwhile (window closed), the change still finishes, since
        it can't be stopped halfway, and is journaled before the cancellation goes through.
        """
        future = asyncio.ensure_future(asyncio.to_thread(work))
        cancelled = False
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                cancelled = True
            except Exception:
                break
        error = future.exception()
        if error is not None:
            await self._history.mark_failed(op_id, _journal_text(error))
        else:
            size, mtime = cast(tuple[int, int], future.result()) if stamped else (None, None)
            await self._history.mark_done(op_id, size=size, mtime_ns=mtime, move=move)
        if cancelled:
            raise asyncio.CancelledError
        if error is not None:
            raise error
        return future.result()

    async def _ensure_dir(self, run_id: int, folder: Path) -> None:
        for missing in await asyncio.to_thread(fsops.missing_dirs, folder):
            op = await self._history.record_pending(run_id, OpKind.CREATE_DIR, None, missing)
            try:
                await self._journaled(op, partial(fsops.make_dir, missing))
            except FileExistsError:
                continue  # it was there already: journaled as failed, never removed by undo

    async def run(
        self,
        plan: Plan,
        *,
        source: RunSource,
        preset_name: str | None,
        on_progress: Callable[[ProgressEvent], None],
        cancel: asyncio.Event,
        kinds: Collection[MediaKind] = (),
        media: Mapping[int, ResolvedMedia] | None = None,
    ) -> RunResult:
        """Carry out `plan`. `media` (by item id) is what each file is, for the move records;
        without it a file moved into a library folder is recorded with that folder's kind."""
        history = self._history
        result = RunResult()
        run_id = await history.start_run(source, preset_name, kinds=kinds)
        try:
            await self._clean(run_id, plan, result)
            succeeded = await self._files(run_id, plan, result, on_progress, cancel, media or {})
            if not result.cancelled:
                await self._artwork(run_id, plan, result, succeeded)
                await self._prune(run_id, plan, result)
                await self._leftovers(run_id, plan, result, succeeded)
        finally:
            kept = await history.finish_run(run_id)
            result.run_id = run_id if kept else None
        return result

    async def _clean(self, run_id: int, plan: Plan, result: RunResult) -> None:
        for root in plan.clean_roots:
            for junk in await asyncio.to_thread(find_cleanup_targets, root):
                await self._delete(run_id, junk, result)

    async def _delete(self, run_id: int, junk: Path, result: RunResult) -> None:
        op = await self._history.record_pending(run_id, OpKind.DELETE_FILE, junk, None)
        try:
            await self._journaled(op, partial(fsops.remove_file, junk))
        except OSError as exc:
            log.warning("Couldn't remove %s: %s", junk.name, log_reason(exc))
            self._say(t("Couldn't remove {name}", name=junk.name), level=logging.WARNING)
            return
        result.cleaned += 1
        log.info("Removed junk: %s", junk.name)
        self._say(t("Removed junk: {name}", name=junk.name))

    async def _leftovers(self, run_id: int, plan: Plan, result: RunResult, ok: set[int]) -> None:
        """Clear a download's folder once all its files moved out and no other video is left."""
        for planned in plan.leftovers:
            if not set(planned.item_ids) <= ok:
                continue
            if await asyncio.to_thread(videos_left, planned.folder):
                continue
            if await asyncio.to_thread(download_busy, planned.folder, time.time()):
                continue  # something else in it is still coming in
            for junk in await asyncio.to_thread(download_leftovers, planned.folder):
                await self._delete(run_id, junk, result)
            folders = await asyncio.to_thread(fsops.walk, planned.folder)
            for folder, _ in sorted(folders, key=lambda f: len(f[0].parts), reverse=True):
                if not await asyncio.to_thread(fsops.is_empty_dir, folder):
                    continue  # subtitles or pictures stay, and so does their folder
                op = await self._history.record_pending(run_id, OpKind.REMOVE_DIR, folder, None)
                try:
                    await self._journaled(op, partial(_remove_dir_or_fail, folder))
                except OSError:
                    continue
                result.pruned += 1
                log.info("Removed the download's folder: %s", folder.name)
                self._say(t("Removed the download's folder: {name}", name=folder.name))

    async def _files(
        self,
        run_id: int,
        plan: Plan,
        result: RunResult,
        on_progress: Callable[[ProgressEvent], None],
        cancel: asyncio.Event,
        media: Mapping[int, ResolvedMedia],
    ) -> set[int]:
        """Rename every file; returns the item ids now in their final place."""
        succeeded: set[int] = set()
        total = max(len(plan.files), 1)
        for index, planned in enumerate(plan.files):
            if cancel.is_set():
                result.cancelled = True
                left = len(plan.files) - index
                log.warning("Run cancelled; %d file(s) not processed", left)
                self._say(
                    tn(
                        "Stopped; {n} file not renamed",
                        "Stopped; {n} files not renamed",
                        left,
                    ),
                    level=logging.WARNING,
                )
                break
            fraction = (index + 1) / total
            name = planned.source.name
            if planned.skip_reason:
                result.skipped += 1
                succeeded.add(planned.item_id)
                log.info("Skipped %s (%s)", name, planned.skip_reason)
                self._say(
                    t("Skipped {name} ({reason})", name=name, reason=t(planned.skip_reason))
                )
                on_progress(
                    ProgressEvent(
                        item_id=planned.item_id,
                        status="skipped",
                        message=t(planned.skip_reason),
                        fraction=fraction,
                    )
                )
                continue
            on_progress(
                ProgressEvent(
                    item_id=planned.item_id,
                    status="working",
                    message=t("Renaming {name}", name=name),
                    fraction=fraction,
                )
            )
            try:
                await self._ensure_dir(run_id, planned.target.parent)
                note = self._note(planned, media.get(planned.item_id))
                op = await self._history.record_pending(
                    run_id, OpKind.RENAME, planned.source, planned.target
                )
                await self._journaled(
                    op, partial(fsops.move_file, planned.source, planned.target), move=note
                )
            except OSError as exc:
                result.failed += 1
                result.errors.append(f"{name}: {exc}")
                log.error("Couldn't rename %s: %s", name, log_reason(exc))
                self._say(
                    t("Couldn't rename {name}: {reason}", name=name, reason=t(log_reason(exc))),
                    level=logging.ERROR,
                )
                on_progress(
                    ProgressEvent(
                        item_id=planned.item_id,
                        status="error",
                        message=t(log_reason(exc)),  # str(exc) names the full path
                        fraction=fraction,
                    )
                )
                continue
            result.renamed += 1
            succeeded.add(planned.item_id)
            log.info("Renamed: %s → %s", name, planned.target.name, extra=_SUCCESS)
            self._say(
                t("Renamed: {name} → {target}", name=name, target=planned.target.name),
                success=True,
            )
            await self._subtitles(run_id, planned, result)
            on_progress(
                ProgressEvent(
                    item_id=planned.item_id,
                    status="done",
                    message=planned.target.name,
                    fraction=fraction,
                )
            )
        return succeeded

    async def _subtitles(self, run_id: int, planned: PlannedFile, result: RunResult) -> None:
        """Move the subtitles beside a renamed video along with it (planner.subtitle_moves),
        each journaled as a rename of its own, so undo puts them back too. One that can't be
        moved stays where it is; the video's rename stands."""
        try:
            moves = await asyncio.to_thread(_subtitles_of, planned.source, planned.target)
        except OSError as exc:
            name = planned.target.name
            log.warning("Couldn't look for subtitles of %s: %s", name, log_reason(exc))
            return
        for src, dst in moves:
            op = await self._history.record_pending(run_id, OpKind.RENAME, src, dst)
            try:
                await self._journaled(op, partial(fsops.move_file, src, dst))
            except OSError as exc:
                log.warning("Couldn't move subtitle %s: %s", src.name, log_reason(exc))
                self._say(
                    t("Couldn't move subtitle {name}", name=src.name), level=logging.WARNING
                )
                continue
            result.subtitles += 1
            log.info("Moved subtitle: %s → %s", src.name, dst.name, extra=_SUCCESS)
            self._say(
                t("Moved subtitle: {name} → {target}", name=src.name, target=dst.name),
                success=True,
            )

    async def _artwork(self, run_id: int, plan: Plan, result: RunResult, ok: set[int]) -> None:
        if self._posters is not None:
            for poster in plan.posters:
                if not ok.intersection(poster.item_ids):
                    continue
                if await asyncio.to_thread(fsops.exists, poster.target):
                    continue
                try:
                    data = await self._posters.image(poster.tmdb_path, "w780")
                    await self._ensure_dir(run_id, poster.target.parent)
                except Exception as exc:  # network or disk: the run carries on without it
                    log.warning("Couldn't get poster %s: %s", poster.target.name, log_reason(exc))
                    self._say(
                        t("Couldn't get poster {name}", name=poster.target.name),
                        level=logging.WARNING,
                    )
                    continue
                op = await self._history.record_pending(
                    run_id, OpKind.CREATE_FILE, None, poster.target
                )
                try:
                    await self._journaled(
                        op, partial(fsops.write_new_file, poster.target, data), stamped=True
                    )
                except OSError as exc:
                    log.warning("Couldn't save poster %s: %s", poster.target.name, log_reason(exc))
                    self._say(
                        t("Couldn't save poster {name}", name=poster.target.name),
                        level=logging.WARNING,
                    )
                    continue
                result.posters += 1
                log.info("Saved poster: %s", poster.target.name, extra=_SUCCESS)
                self._say(t("Saved poster: {name}", name=poster.target.name), success=True)

        if self._trailers is not None:
            for trailer in plan.trailers:
                if not ok.intersection(trailer.item_ids):
                    continue
                expected = trailer.target_stem.with_name(trailer.target_stem.name + ".mp4")
                op = await self._history.record_pending(run_id, OpKind.CREATE_FILE, None, expected)
                try:
                    created = await self._trailers.download(trailer)
                except Exception as exc:
                    await self._history.mark_failed(op, _journal_text(exc))
                    log.warning("Trailer for %s failed: %s", trailer.title, log_reason(exc))
                    self._say(
                        t("Trailer for {title} failed", title=trailer.title),
                        level=logging.WARNING,
                    )
                    continue
                if created is None:
                    await self._history.mark_failed(op, NOTHING_DOWNLOADED)
                    continue
                size, mtime = await asyncio.to_thread(fsops.stamp, created)
                await self._history.mark_done(op, size=size, mtime_ns=mtime, dst=created)
                result.trailers += 1
                log.info("Saved trailer: %s", created.name, extra=_SUCCESS)
                self._say(t("Saved trailer: {name}", name=created.name), success=True)

    async def _prune(self, run_id: int, plan: Plan, result: RunResult) -> None:
        for folder in plan.prune_dirs:
            if not await asyncio.to_thread(fsops.is_empty_dir, folder):
                continue
            op = await self._history.record_pending(run_id, OpKind.REMOVE_DIR, folder, None)
            try:
                await self._journaled(op, partial(_remove_dir_or_fail, folder))
            except OSError:
                continue
            result.pruned += 1
            log.info("Removed empty folder: %s", folder.name)
            self._say(t("Removed empty folder: {name}", name=folder.name))
