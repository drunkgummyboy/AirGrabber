"""Carry out a Plan. Every change is journaled before it happens and settled right after.

With `checker` (app/forge/core/file_check.py), every video renamed is read afterwards and what
doesn't match (audio in a language the user doesn't understand, a smaller picture than its name
promised) is said in the Activity strip as a warning.

With `moves` (ForgeMoveService), a rename into the Series or Movies folder also writes a move
record in the same journal step, with what the caller's `media` (by item id) says the file is.

A plan's `aside` ("Use this one instead"): right before a file's rename, the library's
copies it replaces move aside (each a journaled rename, its subtitles along), so undoing the run
puts them back too. One that can't move fails that file: it stays where it was. When anything
after a move aside fails (the next copy, the file's own rename, Stop during its copy, or the run
cancelled), what was moved aside for it goes back at once (`_put_back`: those journal lines
undone through UndoService, shielded from the cancellation; any other error too, re-raised
after), so the library is never left without the film. A copy that can't go back is said in the
Activity strip (by its file name), and History can undo it. Nothing is deleted (a move never goes
over a file: `fsops.move_file`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable, Collection, Mapping, Sequence
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
from app.forge.core.parser import VIDEO_EXTENSIONS, is_video
from app.forge.core.planner import subtitle_moves
from app.forge.core.undo import FOLDER_NOT_EMPTY, UndoService
from app.forge.core.win_copy import COPY_STOPPED
from app.forge.db.history import HistoryRepo
from app.forge.models.history import OpKind, OpState, RunSource
from app.forge.models.media import MediaKind, ResolvedMedia
from app.forge.models.moves import MoveNote
from app.forge.models.plan import (
    Plan,
    PlannedAside,
    PlannedFile,
    PlannedNfo,
    PlannedSubtitles,
    PlannedTrailer,
)
from app.forge.models.subtitles import SubtitleFetch, SubtitleFetchOutcome, SubtitleIdentity
from app.i18n import N_, number, t, tn

log = logging.getLogger(__name__)
T = TypeVar("T")
_SUCCESS = {"success": True}
# Journal notes are kept in English (the language can change between runs): t() them where
# they are shown. An OS error's own text is kept as it is (t() leaves it unchanged).
NOTHING_DOWNLOADED = N_("nothing downloaded")
NO_SUBTITLE_FOUND = N_("no subtitle found")
SUBTITLES_TOO_SLOW = N_("OpenSubtitles took too long")
SUBTITLES_FAILED = N_("Couldn't get subtitles")
ASIDE_FAILED = N_("Couldn't move the copy in the library aside: {name} ({reason})")
PUT_BACK = N_("Put the copy back in the library: {name}")
NOT_PUT_BACK = N_("Couldn't put the copy back in the library: {name}. History can undo it.")
SUBTITLE_LIMIT_S = 30.0  # one video's subtitles at most; then the run's other videos go without
SUBTITLES_RUN_LIMIT_S = 300.0  # all subtitles of one run at most
COPY_PROGRESS_EVERY_S = 0.25  # a copy between shares tells how far at most this often
_GB, _MB = 1024**3, 1024**2


def _gigabytes(size: int) -> str:
    """"4.2", "20" (no ".0"), with Dutch separators while Dutch is on."""
    value = round(size / _GB, 1)
    return number(value, ".0f" if value.is_integer() else ".1f")


def copy_text(copied: int, size: int) -> str:
    """"Copying 4.2 of 20 GB" ("Copying 300 of 700 MB" below a GB), translated."""
    if size >= _GB:
        return t(
            "Copying {done} of {total} GB", done=_gigabytes(copied), total=_gigabytes(size)
        )
    return t("Copying {done} of {total} MB", done=round(copied / _MB), total=round(size / _MB))


class ProgressEvent(BaseModel):
    """A step of a run. With an `item_id`: that file's rename. Without one, "working": a step
    after the renames (a trailer download), `message` saying which and `fraction` how far.
    `copying`: a "working" event of that file's copy between shares, `message` "Copying 4.2 of
    20 GB" and `fraction` the run's with this file's part copied; sent on the loop.
    `in_library`: an "error" of a file that replaces a copy in the library, which is still there
    (`message` says why in full): it stays held as already in the library."""

    item_id: int | None
    status: Literal["working", "done", "error", "skipped"]
    message: str  # shown as it is: already in the interface language
    fraction: float
    copying: bool = False
    in_library: bool = False


class _AsideFailed(Exception):
    """A library copy couldn't move aside (its file name, and the OSError)."""

    def __init__(self, name: str, error: OSError) -> None:
        super().__init__(name)
        self.name = name
        self.error = error


class RunResult(BaseModel):
    run_id: int | None = None
    renamed: int = 0
    skipped: int = 0
    failed: int = 0
    posters: int = 0
    backdrops: int = 0
    nfos: int = 0
    trailers: int = 0
    cleaned: int = 0
    pruned: int = 0
    subtitles: int = 0  # subtitles moved along with their video
    subtitles_fetched: int = 0  # subtitles found and saved right after the rename
    replaced: int = 0  # library copies moved aside for the files that replaced them
    warnings: int = 0  # what the file check found wrong in the renamed videos
    cancelled: bool = False
    errors: list[str] = []


class PosterFetcher(Protocol):
    async def image(self, path: str, size: str) -> bytes: ...


class TrailerDownloader(Protocol):
    async def download(self, trailer: PlannedTrailer) -> Path | None: ...


class SubtitleFetcher(Protocol):
    """The Subtitles extra (app/forge/core/subtitles.py's ForgeSubtitles)."""

    async def fetch(
        self, video: Path, identity: SubtitleIdentity, *, run_id: int | None = None
    ) -> SubtitleFetch: ...

    async def missing(self, video: Path) -> tuple[str, ...]: ...


class FileChecker(Protocol):
    """The check of a renamed video (app/forge/core/file_check.py's FileCheck)."""

    async def check(self, video: Path, release_name: str) -> list[str]: ...


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
    URL, so it is kept as its type only; a stopped copy as COPY_STOPPED (English, t() it)."""
    if isinstance(exc, fsops.CopyCancelled):
        return COPY_STOPPED
    if type(exc).__module__.split(".")[0] in ("httpx", "httpcore"):
        return type(exc).__name__
    return str(exc) or type(exc).__name__


_STOPS = (SubtitleFetchOutcome.QUOTA, SubtitleFetchOutcome.PROBLEM)


def _too_slow() -> SubtitleFetch:
    return SubtitleFetch(
        outcome=SubtitleFetchOutcome.PROBLEM,
        message=t(SUBTITLES_TOO_SLOW),
        reason=SUBTITLES_TOO_SLOW,
    )


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
        subtitles: SubtitleFetcher | None = None,
        checker: FileChecker | None = None,
    ) -> None:
        self._history = history
        self._checker = checker
        self._posters = posters
        self._trailers = trailers
        self._say = say_to(activity)  # the Activity strip, next to each English log line
        self._moves = moves
        self._fetcher = subtitles

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
        (A copy between shares does end on Stop, the cancel flag: CopyCancelled, journaled as
        failed, nothing moved.)
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

    async def _ensure_dir(
        self, run_id: int, folder: Path, made: list[int] | None = None
    ) -> None:
        """Make `folder` and the folders above it that are missing, each journaled; `made`
        hears each journal line."""
        for missing in await asyncio.to_thread(fsops.missing_dirs, folder):
            op = await self._history.record_pending(run_id, OpKind.CREATE_DIR, None, missing)
            if made is not None:
                made.append(op)
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
        check_files: bool = True,
    ) -> RunResult:
        """Carry out `plan`. `media` (by item id) is what each file is, for the move records;
        without it a file moved into a library folder is recorded with that folder's kind.
        `check_files`: read the renamed videos (False: duplicates moved aside)."""
        history = self._history
        result = RunResult()
        run_id = await history.start_run(source, preset_name, kinds=kinds)
        try:
            await self._clean(run_id, plan, result)
            succeeded = await self._files(run_id, plan, result, on_progress, cancel, media or {})
            if not result.cancelled:
                await self._fetch_subtitles(run_id, plan, result, succeeded, cancel)
                if check_files:
                    await self._check_files(plan, result, succeeded, cancel)
                await self._artwork(run_id, plan, result, succeeded, on_progress, cancel)
                await self._prune(run_id, plan, result)
                await self._leftovers(run_id, plan, result, succeeded)
        finally:
            kept = await history.finish_run(run_id)
            result.run_id = run_id if kept else None
        return result

    async def _check_files(
        self, plan: Plan, result: RunResult, ok: set[int], cancel: asyncio.Event
    ) -> None:
        """Read every video renamed this run; each finding is an Activity warning. Never raises
        (but for a cancellation): a rename never fails for its check."""
        if self._checker is None:
            return
        for planned in plan.files:
            if cancel.is_set():
                return
            renamed = planned.item_id in ok and not planned.skip_reason
            if not renamed or planned.target.suffix.lower() not in VIDEO_EXTENSIONS:
                continue
            try:
                found = await self._checker.check(planned.target, planned.source.name)
            except Exception as exc:  # noqa: BLE001 - only the check is lost
                log.warning("Couldn't check a renamed video: %s", log_reason(exc))
                continue
            for warning in found:
                result.warnings += 1
                self._say(warning, level=logging.WARNING)
        if result.warnings:
            log.warning("The file check found %d thing(s) to look at", result.warnings)

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

    def _stopped(self, left: int) -> None:
        log.warning("Run cancelled; %d file(s) not processed", left)
        self._say(
            tn("Stopped; {n} file not renamed", "Stopped; {n} files not renamed", left),
            level=logging.WARNING,
        )

    @staticmethod
    def _copy_progress(
        item_id: int, index: int, total: int, on_progress: Callable[[ProgressEvent], None]
    ) -> Callable[[int, int], None]:
        """What a copy between shares calls on its own thread: at most every
        COPY_PROGRESS_EVERY_S (and at its end) a `copying` event, handed to the loop."""
        loop = asyncio.get_running_loop()
        last = [0.0]  # time.monotonic() of the last one handed over

        def send(copied: int, size: int) -> None:  # on the loop
            part = copied / size if size else 1.0
            on_progress(
                ProgressEvent(
                    item_id=item_id,
                    status="working",
                    message=copy_text(copied, size),
                    fraction=(index + part) / total,
                    copying=True,
                )
            )

        def heard(copied: int, size: int) -> None:  # on the copy's thread
            now = time.monotonic()
            if now - last[0] < COPY_PROGRESS_EVERY_S and copied < size:
                return
            last[0] = now
            with contextlib.suppress(RuntimeError):  # the loop closed meanwhile
                loop.call_soon_threadsafe(send, copied, size)

        return heard

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
        asides: dict[int, list[PlannedAside]] = {}
        for aside in plan.aside:
            asides.setdefault(aside.item_id, []).append(aside)
        for index, planned in enumerate(plan.files):
            if cancel.is_set():
                result.cancelled = True
                self._stopped(len(plan.files) - index)
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
            replacing = planned.item_id in asides
            mine = asides.get(planned.item_id, [])
            made: list[int] = []  # the journal lines of this file's moves aside
            moved_aside = RunResult()  # counted only once the file is in their place
            rename_op: int | None = None
            try:
                for aside in mine:
                    await self._move_aside(run_id, aside, moved_aside, cancel, made)
                heard = self._copy_progress(planned.item_id, index, total, on_progress)
                await self._ensure_dir(run_id, planned.target.parent)
                note = self._note(planned, media.get(planned.item_id))
                op = await self._history.record_pending(
                    run_id, OpKind.RENAME, planned.source, planned.target
                )
                rename_op = op
                move = partial(
                    fsops.move_file,
                    planned.source,
                    planned.target,
                    progress=heard,
                    stop=cancel.is_set,  # read on the copy's thread: a flag, never changed there
                )
                await self._journaled(op, move, move=note)
            except asyncio.CancelledError:  # the window closed: the library keeps its copy
                await self._put_back_shielded(made, rename_op, mine)
                raise
            except fsops.CopyCancelled:  # Stop during a copy between shares: nothing moved
                await self._put_back_shielded(made, rename_op, mine)
                result.cancelled = True
                self._stopped(len(plan.files) - index)
                break
            except _AsideFailed as exc:
                await self._put_back_shielded(made, rename_op, mine)
                result.failed += 1
                text = t(ASIDE_FAILED, name=exc.name, reason=t(log_reason(exc.error)))
                result.errors.append(text)
                self._say(text, level=logging.ERROR)
                on_progress(
                    ProgressEvent(
                        item_id=planned.item_id,
                        status="error",
                        message=text,
                        fraction=fraction,
                        in_library=True,
                    )
                )
                continue
            except OSError as exc:
                await self._put_back_shielded(made, rename_op, mine)
                result.failed += 1
                result.errors.append(f"{name}: {exc}")
                log.error("Couldn't rename %s: %s", name, log_reason(exc))
                text = t("Couldn't rename {name}: {reason}", name=name, reason=t(log_reason(exc)))
                self._say(text, level=logging.ERROR)
                on_progress(
                    ProgressEvent(
                        item_id=planned.item_id,
                        status="error",
                        # str(exc) names the full path; a replace's file stays held as in the library
                        message=text if replacing else t(log_reason(exc)),
                        fraction=fraction,
                        in_library=replacing,
                    )
                )
                continue
            except Exception:  # an unforeseen error (the journal's, say): the library keeps it
                await self._put_back_shielded(made, rename_op, mine)
                raise
            result.replaced += moved_aside.replaced
            result.subtitles += moved_aside.subtitles
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

    async def _move_aside(
        self,
        run_id: int,
        aside: PlannedAside,
        result: RunResult,
        cancel: asyncio.Event,
        made: list[int],
    ) -> None:
        """Move a library copy out of the way of the file replacing it, journaled (each line in
        `made`), with the subtitles beside it. No progress of its own: the bar is the file's.
        Raises _AsideFailed (CopyCancelled on Stop) when it can't: that file then stays where it
        is."""
        name = aside.source.name
        try:
            await self._ensure_dir(run_id, aside.target.parent, made)
            op = await self._history.record_pending(
                run_id, OpKind.RENAME, aside.source, aside.target
            )
            made.append(op)
            move = partial(fsops.move_file, aside.source, aside.target, stop=cancel.is_set)
            await self._journaled(op, move)
        except fsops.CopyCancelled:
            raise
        except OSError as exc:
            log.error("Couldn't move the library's copy %s aside: %s", name, log_reason(exc))
            raise _AsideFailed(name, exc) from exc
        result.replaced += 1
        log.info("Moved the library's copy aside: %s", name, extra=_SUCCESS)
        self._say(t("Moved the copy in the library aside: {name}", name=name))
        await self._subtitles(
            run_id,
            PlannedFile(item_id=aside.item_id, source=aside.source, target=aside.target),
            result,
            made,
        )

    async def _put_back(
        self, made: list[int], rename_op: int | None, asides: Sequence[PlannedAside] = ()
    ) -> None:
        """What was moved aside for a file back into the library (its journal lines `made`
        undone, newest first, the folders made for it removed once empty), unless the file's
        own rename stood after all. Never raises: what can't go back stays, and History shows
        it (and can undo it later); the Activity strip says which copy (`asides`: the file's
        planned moves aside, for the library copy's name)."""
        if not made:
            return
        try:
            if rename_op is not None:
                [renamed] = await self._history.get_operations([rename_op])
                if renamed.state is OpState.DONE:
                    return  # it is in the copy's place: the moves aside stand
            undone = await UndoService(self._history).undo_operations(made)
            back = await self._history.get_operations(undone.undone_ids)
        except Exception as exc:  # noqa: BLE001 - the run's own error is what is reported
            log.error("Couldn't put the library's copy back: %s", log_reason(exc))
            for aside in asides:
                if is_video(aside.source):
                    self._say_not_put_back(aside.source.name)
            return
        for op in back:
            if op.kind is OpKind.RENAME and op.src and is_video(Path(op.src)):
                name = Path(op.src).name
                log.info("Put the library's copy back: %s", name)
                self._say(t(PUT_BACK, name=name))
        if undone.skipped:
            log.warning("%d move(s) aside not put back", len(undone.skipped))
        by_place = {str(aside.target): aside.source.name for aside in asides}
        for path, _reason in undone.skipped:
            if is_video(Path(path)):
                self._say_not_put_back(by_place.get(path, Path(path).name))

    def _say_not_put_back(self, name: str) -> None:
        """A library copy still aside after a failed replace: said, by its file name only."""
        self._say(t(NOT_PUT_BACK, name=name), level=logging.WARNING)

    async def _put_back_shielded(
        self, made: list[int], rename_op: int | None, asides: Sequence[PlannedAside] = ()
    ) -> None:
        """`_put_back` that a cancellation can't stop halfway (the window closed)."""
        task = asyncio.ensure_future(self._put_back(made, rename_op, asides))
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue

    async def _subtitles(
        self, run_id: int, planned: PlannedFile, result: RunResult, made: list[int] | None = None
    ) -> None:
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
            if made is not None:
                made.append(op)
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

    async def _fetch_subtitles(
        self, run_id: int, plan: Plan, result: RunResult, ok: set[int], cancel: asyncio.Event
    ) -> None:
        """The Subtitles extra, once the files are in place: one try per video, each in a time
        limit. What goes wrong here never touches a rename: it is said in the Activity strip and
        journaled as a failed line, which History shows. Today's allowance used up, a refused
        key or no answer ends the asking for this run (said once)."""
        fetcher = self._fetcher
        if fetcher is None:
            return
        stopped: SubtitleFetch | None = None
        deadline = time.monotonic() + SUBTITLES_RUN_LIMIT_S
        for planned in plan.subtitles:
            if planned.item_id not in ok:
                continue
            if cancel.is_set():
                return
            if stopped is None and time.monotonic() >= deadline:
                stopped = _too_slow()
                self._say_stopped(stopped)
            found = await self._one_video(fetcher, run_id, planned, stopped)
            if found.outcome is SubtitleFetchOutcome.UNAVAILABLE:
                return  # no key in this version, or no language chosen: nothing to say
            if found.outcome is SubtitleFetchOutcome.NO_VIDEO:
                continue  # moved away by hand meanwhile: nothing to say about it
            await self._journal_missing(run_id, planned, found)
            result.subtitles_fetched += len(found.saved)
            self._say_found(planned, found)
            if stopped is None and found.outcome in _STOPS:
                stopped = found
                self._say_stopped(found)

    async def _one_video(
        self,
        fetcher: SubtitleFetcher,
        run_id: int,
        planned: PlannedSubtitles,
        stopped: SubtitleFetch | None,
    ) -> SubtitleFetch:
        """What became of one video's subtitles; never raises (but for a cancellation)."""
        try:
            async with asyncio.timeout(SUBTITLE_LIMIT_S):
                if stopped is not None:  # nothing more is asked: only what is still missing
                    return stopped.model_copy(
                        update={
                            "saved": (),
                            "already": (),
                            "not_found": await fetcher.missing(planned.video),
                        }
                    )
                return await fetcher.fetch(planned.video, planned.identity, run_id=run_id)
        except TimeoutError:
            log.warning("Subtitles: no answer in time (item %d)", planned.item_id)
            missing = await self._still_missing(fetcher, planned)
            return _too_slow().model_copy(update={"not_found": missing})
        except Exception as exc:  # noqa: BLE001 - a rename never fails for its subtitles
            log.error("Subtitles failed (%s)", type(exc).__name__)
            missing = await self._still_missing(fetcher, planned)
            return SubtitleFetch(
                outcome=SubtitleFetchOutcome.PROBLEM,
                not_found=missing,
                message=t(SUBTITLES_FAILED),
                reason=SUBTITLES_FAILED,
            )

    @staticmethod
    async def _still_missing(
        fetcher: SubtitleFetcher, planned: PlannedSubtitles
    ) -> tuple[str, ...]:
        try:
            return await fetcher.missing(planned.video)
        except Exception as exc:  # noqa: BLE001
            log.warning("Subtitles: couldn't look beside the video (%s)", type(exc).__name__)
            return ()

    async def _journal_missing(
        self, run_id: int, planned: PlannedSubtitles, found: SubtitleFetch
    ) -> None:
        """A failed line per language not there, with why: what History's rows say."""
        note = found.reason or NO_SUBTITLE_FOUND
        video = planned.video
        for language in found.not_found:
            target = video.with_name(f"{video.stem}.{language}.srt")
            op = await self._history.record_pending(run_id, OpKind.CREATE_FILE, None, target)
            await self._history.mark_failed(op, note)

    def _say_found(self, planned: PlannedSubtitles, found: SubtitleFetch) -> None:
        name = planned.video.name
        codes = " ".join(code.upper() for code in found.saved)
        if found.saved:
            log.info(
                "Saved %d subtitle(s) (item %d)", len(found.saved), planned.item_id, extra=_SUCCESS
            )
            self._say(
                t("Subtitles for {name}: {languages}", name=name, languages=codes), success=True
            )
        if found.not_found and found.outcome not in _STOPS:
            log.info(
                "No subtitle found in %d language(s) (item %d)",
                len(found.not_found),
                planned.item_id,
            )
            self._say(
                t(
                    "No subtitles found for {name}: {languages}",
                    name=name,
                    languages=" ".join(code.upper() for code in found.not_found),
                )
            )

    def _say_stopped(self, found: SubtitleFetch) -> None:
        log.warning("Subtitles stopped for this run (%s)", found.outcome.value)
        self._say(found.message, level=logging.WARNING)

    async def _artwork(
        self,
        run_id: int,
        plan: Plan,
        result: RunResult,
        ok: set[int],
        on_progress: Callable[[ProgressEvent], None] = lambda _event: None,
        cancel: asyncio.Event | None = None,
    ) -> None:
        if self._posters is not None:
            for poster in plan.posters:
                if not ok.intersection(poster.item_ids):
                    continue
                if await asyncio.to_thread(fsops.exists, poster.target):
                    continue
                backdrop = poster.kind == "backdrop"
                try:
                    data = await self._posters.image(
                        poster.tmdb_path, "w1280" if backdrop else "w780"
                    )
                    await self._ensure_dir(run_id, poster.target.parent)
                except Exception as exc:  # network or disk: the run carries on without it
                    log.warning("Couldn't get poster %s: %s", poster.target.name, log_reason(exc))
                    self._say(
                        t("Couldn't get background {name}", name=poster.target.name)
                        if backdrop
                        else t("Couldn't get poster {name}", name=poster.target.name),
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
                        t("Couldn't save background {name}", name=poster.target.name)
                        if backdrop
                        else t("Couldn't save poster {name}", name=poster.target.name),
                        level=logging.WARNING,
                    )
                    continue
                if backdrop:
                    result.backdrops += 1
                else:
                    result.posters += 1
                log.info("Saved poster: %s", poster.target.name, extra=_SUCCESS)
                self._say(
                    t("Saved background: {name}", name=poster.target.name)
                    if backdrop
                    else t("Saved poster: {name}", name=poster.target.name),
                    success=True,
                )

        for nfo in plan.nfos:
            if ok.intersection(nfo.item_ids):
                await self._write_nfo(run_id, nfo, result)

        if self._trailers is not None:
            await self._fetch_trailers(run_id, plan, result, ok, on_progress, cancel)

    async def _fetch_trailers(
        self,
        run_id: int,
        plan: Plan,
        result: RunResult,
        ok: set[int],
        on_progress: Callable[[ProgressEvent], None],
        cancel: asyncio.Event | None,
    ) -> None:
        """One at a time (each takes half a minute or so), each shown on the progress bar
        ("Downloading trailer 3 of 28 · Thor"); Stop ends them after the current one."""
        trailers = self._trailers
        if trailers is None:
            return
        due = [trailer for trailer in plan.trailers if ok.intersection(trailer.item_ids)]
        for index, trailer in enumerate(due):
            if cancel is not None and cancel.is_set():
                left = len(due) - index
                result.cancelled = True
                log.warning("Run stopped; %d trailer(s) not downloaded", left)
                self._say(
                    tn(
                        "Stopped; {n} trailer not downloaded",
                        "Stopped; {n} trailers not downloaded",
                        left,
                    ),
                    level=logging.WARNING,
                )
                return
            on_progress(
                ProgressEvent(
                    item_id=None,
                    status="working",
                    message=t(
                        "Downloading trailer {n} of {total} · {title}",
                        n=index + 1,
                        total=len(due),
                        title=trailer.title,
                    ),
                    fraction=index / len(due),
                )
            )
            expected = trailer.target_stem.with_name(trailer.target_stem.name + ".mp4")
            op = await self._history.record_pending(run_id, OpKind.CREATE_FILE, None, expected)
            try:
                created = await trailers.download(trailer)
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

    async def _write_nfo(self, run_id: int, nfo: PlannedNfo, result: RunResult) -> None:
        name = nfo.target.name
        try:
            if await asyncio.to_thread(fsops.exists, nfo.target):
                return
            await self._ensure_dir(run_id, nfo.target.parent)
            op = await self._history.record_pending(run_id, OpKind.CREATE_FILE, None, nfo.target)
            data = nfo.content.encode("utf-8")
            await self._journaled(op, partial(fsops.write_new_file, nfo.target, data), stamped=True)
        except OSError as exc:  # the run carries on without it
            log.warning("Couldn't save NFO %s: %s", name, log_reason(exc))
            self._say(t("Couldn't save NFO {name}", name=name), level=logging.WARNING)
            return
        result.nfos += 1
        log.info("Saved NFO: %s", name, extra=_SUCCESS)
        self._say(t("Saved NFO: {name}", name=name), success=True)

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
