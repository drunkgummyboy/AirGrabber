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
from app.forge.models.plan import (
    Plan,
    PlannedFile,
    PlannedNfo,
    PlannedSubtitles,
    PlannedTrailer,
)
from app.forge.models.subtitles import SubtitleFetch, SubtitleFetchOutcome, SubtitleIdentity
from app.i18n import N_, t, tn

log = logging.getLogger(__name__)
T = TypeVar("T")
_SUCCESS = {"success": True}
# Journal notes are kept in English (the language can change between runs): t() them where
# they are shown. An OS error's own text is kept as it is (t() leaves it unchanged).
NOTHING_DOWNLOADED = N_("nothing downloaded")
NO_SUBTITLE_FOUND = N_("no subtitle found")
SUBTITLES_TOO_SLOW = N_("OpenSubtitles took too long")
SUBTITLES_FAILED = N_("Couldn't get subtitles")
SUBTITLE_LIMIT_S = 30.0  # one video's subtitles at most; then the run's other videos go without
SUBTITLES_RUN_LIMIT_S = 300.0  # all subtitles of one run at most


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
    backdrops: int = 0
    nfos: int = 0
    trailers: int = 0
    cleaned: int = 0
    pruned: int = 0
    subtitles: int = 0  # subtitles moved along with their video
    subtitles_fetched: int = 0  # subtitles found and saved right after the rename
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
    ) -> None:
        self._history = history
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
                await self._fetch_subtitles(run_id, plan, result, succeeded, cancel)
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

    async def _artwork(self, run_id: int, plan: Plan, result: RunResult, ok: set[int]) -> None:
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
