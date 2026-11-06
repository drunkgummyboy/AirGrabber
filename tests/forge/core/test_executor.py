from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from unittest import mock

import httpx

from app.forge.core import executor as executor_module
from app.forge.core import fsops
from app.forge.core.executor import NOTHING_DOWNLOADED, Executor, ProgressEvent
from app.forge.core.planner import ALREADY_NAMED, build_plan
from app.forge.core.undo import UndoService
from app.forge.models.history import OpKind, OpState, RunSource
from app.forge.models.plan import Plan, PlannedLeftovers, PlannedTrailer
from app.forge.models.preset import PresetOptions
from app.i18n import Language, current_language, set_language
from tests.forge.core.journal_base import FakePosters, HistoryTestCase, make_inputs


class FakeTrailers:
    async def download(self, trailer: PlannedTrailer) -> Path | None:
        url = trailer.target_stem.with_name(trailer.target_stem.name + ".url")
        url.write_text("[InternetShortcut]\nURL=https://example\n", encoding="utf-8")
        return url


class FailingTrailers:
    def __init__(self, error: Exception | None) -> None:
        self.error = error

    async def download(self, trailer: PlannedTrailer) -> Path | None:
        if self.error is not None:
            raise self.error
        return None


class FailingPosters:
    async def image(self, path: str, size: str) -> bytes:
        raise httpx.ConnectError("https://image.example/t/p/w780/p.jpg?api_key=SECRET")


class ExecutorTest(HistoryTestCase):
    async def test_nfo_and_background_are_written_and_journaled(self) -> None:
        tmp = self.tmp
        inputs = make_inputs(tmp, {"x.mkv": "Dune"})
        media = inputs[0].media.model_copy(update={"tmdb_id": 9, "backdrop_path": "/b.jpg"})
        inputs = [inputs[0].model_copy(update={"media": media})]
        plan = build_plan(inputs, "{ny}/{ny}", PresetOptions(nfo=True, backdrop=True))
        posters = FakePosters()
        result = await self.execute(plan, posters=posters)
        folder = tmp / "Dune (2024)"
        assert "<title>Dune</title>" in (folder / "Dune (2024).nfo").read_text(encoding="utf-8")
        assert (folder / "Dune (2024)-fanart.jpg").read_bytes() == b"jpeg-bytes"
        assert posters.calls == [("/b.jpg", "w1280")]
        assert (result.nfos, result.backdrops, result.posters) == (1, 1, 0)
        assert result.run_id is not None
        undone = await UndoService(self.history).undo_run(result.run_id)
        assert undone.skipped == []
        assert (tmp / "x.mkv").exists() and not folder.exists()  # Undo takes them along

    async def test_an_nfo_that_cannot_be_written_never_stops_the_run(self) -> None:
        tmp = self.tmp
        inputs = make_inputs(tmp, {"x.mkv": "Dune"})
        media = inputs[0].media.model_copy(update={"tmdb_id": 9})
        inputs = [inputs[0].model_copy(update={"media": media})]
        plan = build_plan(inputs, "{ny}", PresetOptions(nfo=True))
        with mock.patch("app.forge.core.executor.fsops.write_new_file", side_effect=OSError("full")):
            result = await self.execute(plan, posters=FakePosters())
        assert (result.renamed, result.nfos, result.failed) == (1, 0, 0)

    async def test_rename_run_journals_everything(self) -> None:
        tmp = self.tmp
        inputs = make_inputs(tmp, {"x.mkv": "Dune"})
        plan = build_plan(inputs, "{ny}/{ny}", PresetOptions(movie_poster=True))
        posters = FakePosters()
        result = await self.execute(plan, posters=posters)

        target = tmp / "Dune (2024)" / "Dune (2024).mkv"
        assert target.read_bytes() == b"video"
        assert (tmp / "Dune (2024)" / "Dune (2024) - Poster.jpg").read_bytes() == b"jpeg-bytes"
        assert posters.calls == [("/p.jpg", "w780")]
        assert (result.renamed, result.posters, result.failed) == (1, 1, 0)
        assert result.run_id is not None

        ops = await self.history.operations(result.run_id)
        assert [o.kind for o in ops] == [OpKind.CREATE_DIR, OpKind.RENAME, OpKind.CREATE_FILE]
        assert all(o.state is OpState.DONE for o in ops)
        assert ops[0].dst == str(tmp / "Dune (2024)")
        assert ops[1].src == str(tmp / "x.mkv") and ops[1].dst == str(target)
        assert ops[2].size == len(b"jpeg-bytes") and ops[2].mtime_ns

    async def test_order_clean_then_files_then_prune(self) -> None:
        tmp = self.tmp
        (tmp / "Thumbs.db").write_bytes(b"junk")
        inputs = make_inputs(tmp, {"sub/x.mkv": "Dune"})
        plan = build_plan(inputs, "{ny}", PresetOptions(clean_junk=True, prune_empty=True))
        result = await self.execute(plan)

        assert (result.cleaned, result.renamed, result.pruned) == (1, 1, 1)
        assert not (tmp / "sub").exists()
        assert result.run_id is not None
        ops = await self.history.operations(result.run_id)
        assert [o.kind for o in ops] == [OpKind.DELETE_FILE, OpKind.RENAME, OpKind.REMOVE_DIR]
        assert ops[2].src == str(tmp / "sub")

    async def test_one_failure_does_not_stop_run(self) -> None:
        inputs = make_inputs(self.tmp, {"a.mkv": "Alien", "b.mkv": "Heat"})
        plan = build_plan(inputs, "{ny}", PresetOptions())
        (self.tmp / "a.mkv").unlink()  # vanished after planning
        result = await self.execute(plan)
        assert (result.renamed, result.failed) == (1, 1)
        assert (self.tmp / "Heat (2024).mkv").exists()
        assert result.errors and "a.mkv" in result.errors[0]

    async def test_locked_file_is_error_not_crash(self) -> None:
        # Review focus 3: a file open in a player or being scanned by Plex.
        inputs = make_inputs(self.tmp, {"a.mkv": "Alien", "b.mkv": "Heat", "c.mkv": "Ran"})
        plan = build_plan(inputs, "{ny}", PresetOptions())
        real_move = fsops.move_file

        def locked(src: Path, dst: Path) -> None:
            if src.name == "b.mkv":
                raise PermissionError(32, "The process cannot access the file")
            real_move(src, dst)

        self.enterContext(mock.patch.object(fsops, "move_file", locked))
        events: list[ProgressEvent] = []
        result = await self.execute(plan, events)
        assert (result.renamed, result.failed) == (2, 1)
        assert result.run_id is not None
        ops = await self.history.operations(result.run_id)
        failed = [o for o in ops if o.state is OpState.FAILED]
        assert len(failed) == 1 and "cannot access" in (failed[0].error or "")
        assert [e.status for e in events if e.item_id == 2][-1] == "error"

    async def test_share_disappears_mid_run(self) -> None:
        # Review focus 2: the NAS share goes away after the first file.
        inputs = make_inputs(self.tmp, {"a.mkv": "Alien", "b.mkv": "Heat", "c.mkv": "Ran"})
        plan = build_plan(inputs, "{ny}", PresetOptions())
        real_move = fsops.move_file
        calls = [0]

        def flaky(src: Path, dst: Path) -> None:
            calls[0] += 1
            if calls[0] > 1:
                raise OSError(53, "The network path was not found")
            real_move(src, dst)

        self.enterContext(mock.patch.object(fsops, "move_file", flaky))
        result = await self.execute(plan)
        assert (result.renamed, result.failed) == (1, 2)
        assert await self.history.pending_operations() == []

    async def test_cancel_between_files(self) -> None:
        inputs = make_inputs(self.tmp, {"a.mkv": "Alien", "b.mkv": "Heat"})
        plan = build_plan(inputs, "{ny}", PresetOptions(movie_poster=True))
        cancel = asyncio.Event()
        events: list[ProgressEvent] = []

        def stopper(event: ProgressEvent) -> None:
            events.append(event)
            if event.status == "done":
                cancel.set()

        executor = Executor(self.history, FakePosters(), None)
        result = await executor.run(
            plan, source=RunSource.APP, preset_name=None, on_progress=stopper, cancel=cancel
        )
        assert result.cancelled
        assert result.renamed == 1 and result.posters == 0
        assert (self.tmp / "Alien (2024).mkv").exists()
        assert (self.tmp / "b.mkv").exists()

    async def test_poster_not_overwritten(self) -> None:
        inputs = make_inputs(self.tmp, {"x.mkv": "Dune"})
        plan = build_plan(inputs, "{ny}", PresetOptions(movie_poster=True))
        existing = self.tmp / "Dune (2024) - Poster.jpg"
        existing.write_bytes(b"mine")  # appeared after planning
        result = await self.execute(plan)
        assert existing.read_bytes() == b"mine"
        assert result.posters == 0
        assert result.run_id is not None
        kinds = [o.kind for o in await self.history.operations(result.run_id)]
        assert OpKind.CREATE_FILE not in kinds

    async def test_poster_skipped_when_its_file_failed(self) -> None:
        inputs = make_inputs(self.tmp, {"a.mkv": "Alien", "b.mkv": "Heat"})
        plan = build_plan(inputs, "{ny}", PresetOptions(movie_poster=True))
        (self.tmp / "a.mkv").unlink()
        result = await self.execute(plan)
        assert result.posters == 1
        assert not (self.tmp / "Alien (2024) - Poster.jpg").exists()
        assert (self.tmp / "Heat (2024) - Poster.jpg").exists()

    async def test_trailer_journaled_with_actual_path(self) -> None:
        inputs = make_inputs(self.tmp, {"x.mkv": "Dune"})
        plan = build_plan(inputs, "{ny}", PresetOptions(trailer=True))
        result = await self.execute(plan, trailers=FakeTrailers())
        assert result.trailers == 1
        assert result.run_id is not None
        [trailer_op] = [
            o for o in await self.history.operations(result.run_id) if o.kind is OpKind.CREATE_FILE
        ]
        assert trailer_op.dst == str(self.tmp / "Dune (2024) - Trailer.url")
        assert trailer_op.state is OpState.DONE and trailer_op.size

    async def test_junk_deletes_are_journaled_as_delete_file(self) -> None:
        (self.tmp / "desktop.ini").write_bytes(b"j")
        inputs = make_inputs(self.tmp, {"x.mkv": "Dune"})
        plan = build_plan(inputs, "{ny}", PresetOptions(clean_junk=True))
        result = await self.execute(plan)
        assert result.run_id is not None
        [junk] = [
            o for o in await self.history.operations(result.run_id) if o.kind is OpKind.DELETE_FILE
        ]
        assert junk.src == str(self.tmp / "desktop.ini") and not junk.undoable

    async def test_empty_run_leaves_no_history(self) -> None:
        inputs = make_inputs(self.tmp, {"Dune (2024).mkv": "Dune"})
        plan = build_plan(inputs, "{ny}", PresetOptions())
        result = await self.execute(plan)
        assert result.run_id is None and result.skipped == 1
        assert await self.history.list_runs() == []

    async def test_cancelled_task_still_journals_the_rename(self) -> None:
        # Review I4: closing the window cancels the run task while a rename is in flight. The
        # rename thread finishes anyway, so the journal must say so (undoable), not lose it.
        inputs = make_inputs(self.tmp, {"a.mkv": "Alien"})
        plan = build_plan(inputs, "{ny}", PresetOptions())
        real_move = fsops.move_file

        def slow_move(src: Path, dst: Path) -> None:
            time.sleep(0.4)
            real_move(src, dst)

        self.enterContext(mock.patch.object(fsops, "move_file", slow_move))
        task = asyncio.create_task(self.execute(plan))
        await asyncio.sleep(0.15)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        assert (self.tmp / "Alien (2024).mkv").exists()
        [summary] = await self.history.list_runs()
        ops = await self.history.operations(summary.id)
        assert [(o.kind, o.state) for o in ops] == [(OpKind.RENAME, OpState.DONE)]


class ExecutorTextsTest(HistoryTestCase):
    """AirGrabber's additions: shown texts in the interface language, logs without paths or
    URLs, and no httpx error text in the journal."""

    def _dutch(self) -> None:
        before = current_language()
        set_language(Language.NL)
        self.addCleanup(set_language, before)

    async def test_progress_texts_are_translated(self) -> None:
        self._dutch()
        inputs = make_inputs(self.tmp, {"Dune (2024).mkv": "Dune", "a.mkv": "Alien"})
        plan = build_plan(inputs, "{ny}", PresetOptions())
        events: list[ProgressEvent] = []
        await self.execute(plan, events)
        skipped = next(e for e in events if e.status == "skipped")
        assert ALREADY_NAMED == "already correctly named"  # the planner's English note
        assert skipped.message == "heeft al de juiste naam"
        working = next(e for e in events if e.status == "working")
        assert working.message == "a.mkv hernoemen"

    async def test_logs_name_files_not_full_paths(self) -> None:
        folder = self.tmp / "Alex's downloads"
        inputs = make_inputs(folder, {"a.mkv": "Alien"})
        plan = build_plan(inputs, "{ny}", PresetOptions())

        def denied(src: Path, dst: Path) -> None:
            raise PermissionError(13, "Access is denied", str(src))

        self.enterContext(mock.patch.object(fsops, "move_file", denied))
        with self.assertLogs(executor_module.log, "INFO") as logs:
            result = await self.execute(plan)
        assert result.failed == 1
        assert any("a.mkv" in line and "Access is denied" in line for line in logs.output)
        assert not any(str(folder) in line for line in logs.output)

    async def test_poster_failure_keeps_the_url_out_of_the_log(self) -> None:
        inputs = make_inputs(self.tmp, {"x.mkv": "Dune"})
        plan = build_plan(inputs, "{ny}", PresetOptions(movie_poster=True))
        with self.assertLogs(executor_module.log, "INFO") as logs:
            result = await self.execute(plan, posters=FailingPosters())
        assert (result.renamed, result.posters) == (1, 0)
        assert any("ConnectError" in line for line in logs.output)
        assert not any("SECRET" in line or "https://" in line for line in logs.output)

    async def test_trailer_failures_are_journaled_without_httpx_text(self) -> None:
        cases: list[tuple[Exception | None, str]] = [
            (httpx.ConnectError("https://github.example/latest?token=SECRET"), "ConnectError"),
            (None, NOTHING_DOWNLOADED),
        ]
        for index, (error, expected) in enumerate(cases):
            with self.subTest(expected=expected):
                root = self.tmp / f"case{index}"
                inputs = make_inputs(root, {"x.mkv": "Dune"})
                plan = build_plan(inputs, "{ny}", PresetOptions(trailer=True))
                result = await self.execute(plan, trailers=FailingTrailers(error))
                assert (result.renamed, result.trailers) == (1, 0)
                assert result.run_id is not None
                [op] = [
                    o
                    for o in await self.history.operations(result.run_id)
                    if o.kind is OpKind.CREATE_FILE
                ]
                assert op.state is OpState.FAILED and op.error == expected


# --- a download's leftovers (1.3) ---------------------------------------------------------------


def download(tmp: Path, extra: tuple[str, ...] = ()) -> tuple[Plan, Path]:
    """One movie in a release folder with junk beside it, planned into a library folder."""
    release = tmp / "dl" / "Heat.1995.1080p-GRP"
    old = time.time() - 3600  # written with the download, an hour ago
    for name in ("Sample/heat-sample.mkv", "heat.nfo", "RARBG.txt", *extra):
        (release / name).parent.mkdir(parents=True, exist_ok=True)
        (release / name).write_bytes(b"x")
        os.utime(release / name, (old, old))
    [item] = make_inputs(release, {"heat.1995.mkv": "Heat"})
    item = item.model_copy(update={"root": tmp / "lib", "dropped_folder": None})
    plan = build_plan([item], "{ny}/{ny}", PresetOptions())
    plan = plan.model_copy(update={"leftovers": (PlannedLeftovers(folder=release, item_ids=(1,)),)})
    return plan, release


class LeftoversTest(HistoryTestCase):
    async def test_leftovers_go_after_a_successful_move(self) -> None:
        plan, release = download(self.tmp)
        result = await self.execute(plan)
        assert result.renamed == 1
        assert not release.exists()  # junk removed, then the emptied release folder
        assert (self.tmp / "dl").exists()  # never above the release folder
        ops = await self.history.operations(result.run_id or 0)
        kinds = [o.kind for o in ops]
        assert kinds.count(OpKind.DELETE_FILE) == 3
        assert OpKind.REMOVE_DIR in kinds
        assert all(o.state is OpState.DONE for o in ops)

    async def test_leftovers_stay_when_a_move_failed(self) -> None:
        plan, release = download(self.tmp)
        target = plan.files[0].target
        target.parent.mkdir(parents=True)
        target.write_bytes(b"something already here")  # the move fails: the name is taken
        result = await self.execute(plan)
        assert result.failed == 1
        assert (release / "heat.nfo").exists() and (release / "heat.1995.mkv").exists()

    async def test_leftovers_stay_when_another_video_is_left(self) -> None:
        plan, release = download(self.tmp, extra=("heat.extras.mkv",))
        result = await self.execute(plan)
        assert result.renamed == 1
        assert (release / "heat.nfo").exists() and (release / "heat.extras.mkv").exists()

    async def test_leftovers_stay_while_another_download_is_coming_in(self) -> None:
        plan, release = download(self.tmp, extra=("heat.part2.mkv.!qB",))
        result = await self.execute(plan)
        assert result.renamed == 1
        assert (release / "heat.nfo").exists()


# --- subtitles go along with their video (B-08) -------------------------------------------------

SUBTITLES = ("heat.1995.en.srt", "heat.1995.nl.forced.srt")


class SubtitlesFollowTest(HistoryTestCase):
    async def test_subtitles_follow_journaled_and_the_download_folder_still_goes(self) -> None:
        plan, release = download(self.tmp, extra=SUBTITLES)
        result = await self.execute(plan)
        assert (result.renamed, result.subtitles, result.failed) == (1, 2, 0)
        folder = plan.files[0].target.parent
        assert (folder / "Heat (2024).en.srt").read_bytes() == b"x"
        assert (folder / "Heat (2024).nl.forced.srt").read_bytes() == b"x"
        assert not release.exists()  # nothing of it left behind

        ops = await self.history.operations(result.run_id or 0)
        renames = [(Path(o.src or "").name, Path(o.dst or "").name) for o in ops if o.kind is OpKind.RENAME]
        assert renames == [
            ("heat.1995.mkv", "Heat (2024).mkv"),
            ("heat.1995.en.srt", "Heat (2024).en.srt"),
            ("heat.1995.nl.forced.srt", "Heat (2024).nl.forced.srt"),
        ]
        assert all(o.state is OpState.DONE for o in ops)

    async def test_undo_puts_the_subtitles_back_with_the_video(self) -> None:
        plan, release = download(self.tmp, extra=SUBTITLES)
        result = await self.execute(plan)
        assert result.run_id is not None
        undone = await UndoService(self.history).undo_run(result.run_id)
        assert (release / "heat.1995.mkv").exists()
        for name in SUBTITLES:
            assert (release / name).read_bytes() == b"x"
        assert not plan.files[0].target.parent.exists()
        assert undone.undone >= 3

    async def test_a_subtitle_that_cant_move_stays_and_the_video_stands(self) -> None:
        plan, release = download(self.tmp, extra=SUBTITLES)
        real_move = fsops.move_file

        def locked(src: Path, dst: Path) -> None:
            if src.name == "heat.1995.en.srt":
                raise PermissionError(32, "The process cannot access the file")
            real_move(src, dst)

        self.enterContext(mock.patch.object(fsops, "move_file", locked))
        result = await self.execute(plan)
        assert (result.renamed, result.subtitles, result.failed) == (1, 1, 0)
        assert plan.files[0].target.exists()
        assert (release / "heat.1995.en.srt").exists()  # and so does its folder
        ops = await self.history.operations(result.run_id or 0)
        [failed] = [o for o in ops if o.state is OpState.FAILED]
        assert failed.kind is OpKind.RENAME and Path(failed.src or "").name == "heat.1995.en.srt"

    async def test_a_subtitle_whose_new_name_is_taken_is_left_alone(self) -> None:
        plan, release = download(self.tmp, extra=SUBTITLES)
        folder = plan.files[0].target.parent
        folder.mkdir(parents=True)
        (folder / "Heat (2024).en.srt").write_bytes(b"mine")
        result = await self.execute(plan)
        assert (result.renamed, result.subtitles) == (1, 1)
        assert (folder / "Heat (2024).en.srt").read_bytes() == b"mine"
        assert (release / "heat.1995.en.srt").exists()
        ops = await self.history.operations(result.run_id or 0)
        assert all(o.state is OpState.DONE for o in ops)  # never tried, so nothing failed

    async def test_logs_and_activity_name_files_not_paths(self) -> None:
        plan, _release = download(self.tmp, extra=SUBTITLES[:1])
        with self.assertLogs("app.forge.core.executor", "INFO") as logs:
            await self.execute(plan)
        lines = [line for line in logs.output if "subtitle" in line]
        assert lines == ["INFO:app.forge.core.executor:Moved subtitle: heat.1995.en.srt → Heat (2024).en.srt"]
        assert all(str(self.tmp) not in line for line in logs.output)
