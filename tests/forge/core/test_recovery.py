from __future__ import annotations

from app.forge.core import fsops
from app.forge.core.recovery import INTERRUPTED, recover
from app.forge.models.history import OpKind, OpState, RunSource, UndoState
from app.i18n import Language, current_language, set_language, t
from tests.forge.core.journal_base import HistoryTestCase


class RecoveryTest(HistoryTestCase):
    async def test_recover_pending_rename_done_and_failed(self) -> None:
        tmp, history = self.tmp, self.history
        run_id = await history.start_run(RunSource.APP, None)
        moved_dst = tmp / "new.mkv"
        moved_dst.write_bytes(b"v")
        done = await history.record_pending(run_id, OpKind.RENAME, tmp / "old.mkv", moved_dst)
        stuck_src = tmp / "still.mkv"
        stuck_src.write_bytes(b"v")
        failed = await history.record_pending(run_id, OpKind.RENAME, stuck_src, tmp / "x.mkv")
        poster = tmp / "p.jpg"
        poster.write_bytes(b"jpg")
        created = await history.record_pending(run_id, OpKind.CREATE_FILE, None, poster)

        assert await recover(history) == 3
        ops = {o.id: o for o in await history.operations(run_id)}
        assert ops[done].state is OpState.DONE
        assert ops[failed].state is OpState.FAILED and ops[failed].error == "interrupted"
        assert ops[created].state is OpState.DONE and ops[created].size == 3
        assert await history.pending_operations() == []

    async def test_recover_pending_undo(self) -> None:
        tmp, history = self.tmp, self.history
        run_id = await history.start_run(RunSource.APP, None)
        back = tmp / "a.mkv"
        back.write_bytes(b"v")
        finished = await history.record_pending(run_id, OpKind.RENAME, back, tmp / "b.mkv")
        unclear_src = tmp / "c.mkv"
        unclear = await history.record_pending(run_id, OpKind.RENAME, unclear_src, tmp / "d.mkv")
        (tmp / "d.mkv").write_bytes(b"v")
        for op in (finished, unclear):
            await history.mark_done(op)
            await history.mark_undo(op, UndoState.PENDING)

        assert await recover(history) == 2
        ops = {o.id: o for o in await history.operations(run_id)}
        assert ops[finished].undo_state is UndoState.DONE
        assert ops[unclear].undo_state is UndoState.SKIPPED
        assert ops[unclear].undo_note == "interrupted"

    async def test_unreachable_share_leaves_ops_pending(self) -> None:
        # Review I5: with the NAS offline both names look missing; don't decide anything yet.
        share = self.tmp / "offline-share"  # never created
        history = self.history
        run_id = await history.start_run(RunSource.APP, None)
        rename = await history.record_pending(run_id, OpKind.RENAME, share / "a.mkv", share / "b.mkv")
        poster = await history.record_pending(run_id, OpKind.CREATE_FILE, None, share / "p.jpg")
        assert await recover(history) == 0
        assert {o.id for o in await history.pending_operations()} == {rename, poster}

    async def test_case_only_rename_judged_by_real_name(self) -> None:
        tmp, history = self.tmp, self.history
        run_id = await history.start_run(RunSource.APP, None)
        (tmp / "The Office.mkv").write_bytes(b"v")
        done = await history.record_pending(
            run_id, OpKind.RENAME, tmp / "the office.mkv", tmp / "The Office.mkv"
        )
        (tmp / "heat.mkv").write_bytes(b"v")
        not_done = await history.record_pending(
            run_id, OpKind.RENAME, tmp / "heat.mkv", tmp / "Heat.mkv"
        )
        await recover(history)
        ops = {o.id: o for o in await history.operations(run_id)}
        assert ops[done].state is OpState.DONE
        assert ops[not_done].state is OpState.FAILED

    async def test_recovery_clears_a_partial_copy(self) -> None:
        history = self.history
        src, dst = self.tmp / "dl" / "a.mkv", self.tmp / "lib" / "a.mkv"
        src.parent.mkdir()
        dst.parent.mkdir()
        src.write_bytes(b"video")
        partial = dst.with_name(dst.name + fsops.PARTIAL_SUFFIX)
        partial.write_bytes(b"vid")  # the PC went off during the copy
        run_id = await history.start_run(RunSource.AUTOMATIC, "Movie (Folder)")
        await history.record_pending(run_id, OpKind.RENAME, src, dst)
        await recover(history)
        assert not partial.exists() and src.exists() and not dst.exists()
        [op] = await history.operations(run_id)
        assert op.state is OpState.FAILED

    async def test_interrupted_note_is_english_with_a_dutch_text(self) -> None:
        before = current_language()
        set_language(Language.NL)
        self.addCleanup(set_language, before)
        run_id = await self.history.start_run(RunSource.APP, None)
        (self.tmp / "still.mkv").write_bytes(b"v")
        op_id = await self.history.record_pending(
            run_id, OpKind.RENAME, self.tmp / "still.mkv", self.tmp / "x.mkv"
        )
        assert await recover(self.history) == 1
        [op] = await self.history.get_operations([op_id])
        assert op.error == INTERRUPTED == "interrupted"
        assert t(op.error) == "onderbroken"
