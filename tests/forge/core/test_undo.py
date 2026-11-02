from __future__ import annotations

import asyncio
from pathlib import Path
from unittest import mock

from app.forge.core import fsops
from app.forge.core import undo as undo_module
from app.forge.core.executor import RunResult
from app.forge.core.planner import build_plan
from app.forge.core.undo import (
    CHANGED_SINCE,
    FOLDER_NOT_EMPTY,
    JUNK_IS_GONE,
    MOVED_AGAIN,
    OLD_NAME_TAKEN,
    UndoService,
)
from app.forge.models.history import OpKind, UndoState
from app.forge.models.preset import PresetOptions
from app.i18n import Language, current_language, set_language, t
from tests.forge.core.journal_base import FakePosters, HistoryTestCase, make_inputs


class UndoTest(HistoryTestCase):
    async def forge(
        self, root: Path, names: dict[str, str], fmt: str, opts: PresetOptions
    ) -> RunResult:
        plan = build_plan(make_inputs(root, names), fmt, opts)
        result = await self.execute(plan, posters=FakePosters(b"poster"), preset_name=None)
        assert result.run_id is not None
        return result

    async def test_undo_whole_run_restores_names_and_removes_created_dirs_and_posters(
        self,
    ) -> None:
        tmp = self.tmp
        run = await self.forge(tmp, {"x.mkv": "Dune"}, "{ny}/{ny}", PresetOptions(movie_poster=True))
        assert run.run_id is not None
        assert (tmp / "Dune (2024)" / "Dune (2024) - Poster.jpg").exists()

        result = await UndoService(self.history).undo_run(run.run_id)
        assert result.undone == 3 and result.skipped == []
        assert (tmp / "x.mkv").read_bytes() == b"video"
        assert not (tmp / "Dune (2024)").exists()
        [summary] = await self.history.list_runs()
        assert summary.status == "undone"

    async def test_undo_selected_files_only(self) -> None:
        tmp = self.tmp
        run = await self.forge(tmp, {"a.mkv": "Alien", "b.mkv": "Heat"}, "{ny}", PresetOptions())
        assert run.run_id is not None
        ops = await self.history.operations(run.run_id)
        first = next(o for o in ops if o.kind is OpKind.RENAME and o.src == str(tmp / "a.mkv"))

        result = await UndoService(self.history).undo_operations([first.id])
        assert result.undone == 1
        assert (tmp / "a.mkv").exists()
        assert (tmp / "Heat (2024).mkv").exists()
        [summary] = await self.history.list_runs()
        assert summary.status == "partly-undone"

    async def test_undo_older_run_out_of_order(self) -> None:
        first = await self.forge(self.tmp, {"a.mkv": "Alien"}, "{ny}", PresetOptions())
        await self.forge(self.tmp, {"b.mkv": "Heat"}, "{ny}", PresetOptions())
        assert first.run_id is not None
        result = await UndoService(self.history).undo_run(first.run_id)
        assert result.undone == 1
        assert (self.tmp / "a.mkv").exists()
        assert (self.tmp / "Heat (2024).mkv").exists()

    async def test_skip_when_moved_again(self) -> None:
        run = await self.forge(self.tmp, {"a.mkv": "Alien"}, "{ny}", PresetOptions())
        assert run.run_id is not None
        (self.tmp / "Alien (2024).mkv").rename(self.tmp / "elsewhere.mkv")
        result = await UndoService(self.history).undo_run(run.run_id)
        assert result.undone == 0
        assert result.skipped == [(str(self.tmp / "Alien (2024).mkv"), MOVED_AGAIN)]
        assert MOVED_AGAIN == "file was moved or renamed again"

    async def test_skip_when_old_name_taken(self) -> None:
        run = await self.forge(self.tmp, {"a.mkv": "Alien"}, "{ny}", PresetOptions())
        assert run.run_id is not None
        (self.tmp / "a.mkv").write_bytes(b"new download")
        result = await UndoService(self.history).undo_run(run.run_id)
        assert result.skipped == [(str(self.tmp / "Alien (2024).mkv"), OLD_NAME_TAKEN)]
        assert OLD_NAME_TAKEN == "something else now has the old name"
        assert (self.tmp / "a.mkv").read_bytes() == b"new download"

    async def test_poster_changed_since_is_kept(self) -> None:
        run = await self.forge(
            self.tmp, {"x.mkv": "Dune"}, "{ny}", PresetOptions(movie_poster=True)
        )
        assert run.run_id is not None
        poster = self.tmp / "Dune (2024) - Poster.jpg"
        poster.write_bytes(b"a different, edited poster")
        result = await UndoService(self.history).undo_run(run.run_id)
        assert (str(poster), CHANGED_SINCE) in result.skipped
        assert CHANGED_SINCE == "file has changed since"
        assert poster.read_bytes() == b"a different, edited poster"
        assert (self.tmp / "x.mkv").exists()

    async def test_pruned_folder_recreated(self) -> None:
        run = await self.forge(
            self.tmp, {"sub/x.mkv": "Dune"}, "{ny}", PresetOptions(prune_empty=True)
        )
        assert run.run_id is not None
        assert not (self.tmp / "sub").exists()
        result = await UndoService(self.history).undo_run(run.run_id)
        assert result.undone == 2
        assert (self.tmp / "sub" / "x.mkv").exists()

    async def test_undo_is_idempotent(self) -> None:
        run = await self.forge(self.tmp, {"a.mkv": "Alien"}, "{ny}", PresetOptions())
        assert run.run_id is not None
        service = UndoService(self.history)
        assert (await service.undo_run(run.run_id)).undone == 1
        again = await service.undo_run(run.run_id)
        assert again.undone == 0 and again.skipped == []

    async def test_create_dir_removed_last_and_only_if_empty(self) -> None:
        run = await self.forge(self.tmp, {"x.mkv": "Dune"}, "{ny}/{ny}", PresetOptions())
        assert run.run_id is not None
        (self.tmp / "Dune (2024)" / "my notes.txt").write_text("keep me", encoding="utf-8")
        result = await UndoService(self.history).undo_run(run.run_id)
        assert (self.tmp / "x.mkv").exists()
        assert (str(self.tmp / "Dune (2024)"), FOLDER_NOT_EMPTY) in result.skipped
        assert FOLDER_NOT_EMPTY == "folder isn't empty"
        assert (self.tmp / "Dune (2024)" / "my notes.txt").exists()

    async def test_junk_delete_reported_once(self) -> None:
        (self.tmp / "Thumbs.db").write_bytes(b"j")
        run = await self.forge(
            self.tmp, {"a.mkv": "Alien"}, "{ny}", PresetOptions(clean_junk=True)
        )
        assert run.run_id is not None
        service = UndoService(self.history)
        first = await service.undo_run(run.run_id)
        assert (str(self.tmp / "Thumbs.db"), JUNK_IS_GONE) in first.skipped
        assert JUNK_IS_GONE == "deleted junk can't be restored"
        assert (await service.undo_run(run.run_id)).skipped == []

    async def test_two_undos_at_once_undo_once(self) -> None:
        # Review I1: double-clicking Undo must not reverse twice or rewrite DONE as SKIPPED.
        names = {f"m{n}.mkv": f"Movie {n}" for n in range(6)}
        run = await self.forge(self.tmp, names, "{ny}", PresetOptions())
        assert run.run_id is not None
        service = UndoService(self.history)
        first, second = await asyncio.gather(
            service.undo_run(run.run_id), service.undo_run(run.run_id)
        )
        assert first.undone + second.undone == 6
        assert first.skipped == [] and second.skipped == []
        ops = await self.history.operations(run.run_id)
        assert all(o.undo_state is UndoState.DONE for o in ops)


class UndoTextsTest(HistoryTestCase):
    """AirGrabber's additions: reasons are kept in English and shown through t(); an OS error
    keeps its text in the journal but the log names only the file."""

    async def test_reasons_have_dutch_and_stay_english_in_the_journal(self) -> None:
        before = current_language()
        set_language(Language.NL)
        self.addCleanup(set_language, before)
        plan = build_plan(make_inputs(self.tmp, {"a.mkv": "Alien"}), "{ny}", PresetOptions())
        run = await self.execute(plan)
        assert run.run_id is not None
        (self.tmp / "Alien (2024).mkv").unlink()
        result = await UndoService(self.history).undo_run(run.run_id)
        [(_, reason)] = result.skipped
        [op] = await self.history.operations(run.run_id)
        assert reason == op.undo_note == MOVED_AGAIN
        assert t(reason) == "bestand is opnieuw verplaatst of hernoemd"

    async def test_os_error_is_kept_but_its_path_is_not_logged(self) -> None:
        folder = self.tmp / "Alex's films"
        plan = build_plan(make_inputs(folder, {"a.mkv": "Alien"}), "{ny}", PresetOptions())
        run = await self.execute(plan)
        assert run.run_id is not None

        def denied(src: Path, dst: Path) -> None:
            raise PermissionError(13, "Access is denied", str(src))

        self.enterContext(mock.patch.object(fsops, "move_file", denied))
        with self.assertLogs(undo_module.log, "INFO") as logs:
            result = await UndoService(self.history).undo_run(run.run_id)
        [(_, reason)] = result.skipped
        assert "Access is denied" in reason
        [op] = await self.history.operations(run.run_id)
        assert op.undo_state is UndoState.SKIPPED and op.undo_note == reason
        assert any("Alien (2024).mkv" in line for line in logs.output)
        assert not any(str(folder) in line for line in logs.output)
