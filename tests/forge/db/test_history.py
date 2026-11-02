from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import aiosqlite

from app.forge.db.database import MIGRATIONS, Database
from app.forge.db.history import HistoryRepo
from app.forge.models.history import HISTORY_LIMIT, OpKind, OpState, RunSource, UndoState
from app.forge.models.media import MediaKind
from tests.forge.db.base import ForgeDbTestCase

A, B = Path(r"C:\m\a.mkv"), Path(r"C:\m\b.mkv")

class HistoryRepoTest(ForgeDbTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.repo = HistoryRepo(self.db)

    async def _run_with_rename(self, started: datetime | None = None) -> int:
        run_id = await self.repo.start_run(RunSource.APP, "Auto", started_at=started)
        op = await self.repo.record_pending(run_id, OpKind.RENAME, A, B)
        await self.repo.mark_done(op)
        assert await self.repo.finish_run(run_id)
        return run_id

    async def test_run_lifecycle(self) -> None:
        repo = self.repo
        run_id = await repo.start_run(RunSource.RIGHT_CLICK, "Series (Flat)")
        op = await repo.record_pending(run_id, OpKind.RENAME, A, B)
        poster = await repo.record_pending(run_id, OpKind.CREATE_FILE, None, Path(r"C:\m\p.jpg"))
        await repo.mark_done(op)
        await repo.mark_done(poster, size=10, mtime_ns=99)
        assert await repo.finish_run(run_id) is True

        [summary] = await repo.list_runs()
        assert summary.id == run_id
        assert summary.source is RunSource.RIGHT_CLICK
        assert summary.preset_name == "Series (Flat)"
        assert (summary.file_count, summary.created_count, summary.undoable_count) == (1, 1, 2)
        assert summary.finished_at is not None
        assert summary.status == "active"

        ops = await repo.operations(run_id)
        assert [o.kind for o in ops] == [OpKind.RENAME, OpKind.CREATE_FILE]
        assert ops[0].src == str(A) and ops[0].dst == str(B)
        assert (ops[1].size, ops[1].mtime_ns, ops[1].state) == (10, 99, OpState.DONE)

    async def test_mark_done_can_record_actual_destination(self) -> None:
        run_id = await self.repo.start_run(RunSource.APP, None)
        op = await self.repo.record_pending(
            run_id, OpKind.CREATE_FILE, None, Path(r"C:\m\t.mp4")
        )
        await self.repo.mark_done(op, size=3, mtime_ns=4, dst=Path(r"C:\m\t.url"))
        [o] = await self.repo.operations(run_id)
        assert o.dst == r"C:\m\t.url"

    async def test_empty_run_is_dropped(self) -> None:
        run_id = await self.repo.start_run(RunSource.APP, None)
        failed = await self.repo.record_pending(run_id, OpKind.RENAME, A, B)
        await self.repo.mark_failed(failed, "locked")
        assert await self.repo.finish_run(run_id) is False
        assert await self.repo.list_runs() == []

    async def test_retention_keeps_50(self) -> None:
        start = datetime(2026, 1, 1)
        ids = [await self._run_with_rename(start + timedelta(minutes=i)) for i in range(52)]
        runs = await self.repo.list_runs()
        assert len(runs) == HISTORY_LIMIT == 50
        assert [r.id for r in runs] == list(reversed(ids[2:]))
        assert await self.repo.operations(ids[0]) == []  # cascaded away

    async def test_status_partly_undone_and_undone(self) -> None:
        repo = self.repo
        run_id = await repo.start_run(RunSource.APP, None)
        first = await repo.record_pending(run_id, OpKind.RENAME, A, B)
        second = await repo.record_pending(run_id, OpKind.RENAME, B, A)
        await repo.mark_done(first)
        await repo.mark_done(second)
        await repo.finish_run(run_id)

        await repo.mark_undo(first, UndoState.DONE)
        [summary] = await repo.list_runs()
        assert summary.status == "partly-undone"
        assert (summary.undoable_count, summary.undone_count) == (1, 1)

        await repo.mark_undo(second, UndoState.DONE)
        [summary] = await repo.list_runs()
        assert summary.status == "undone"
        ops = await repo.operations(run_id)
        assert all(o.undone_at is not None and not o.undoable for o in ops)

    async def test_skipped_undo_stays_undoable(self) -> None:
        run_id = await self.repo.start_run(RunSource.APP, None)
        op = await self.repo.record_pending(run_id, OpKind.RENAME, A, B)
        await self.repo.mark_done(op)
        await self.repo.finish_run(run_id)
        await self.repo.mark_undo(op, UndoState.SKIPPED, "file was moved or renamed again")
        [o] = await self.repo.operations(run_id)
        assert o.undoable and o.undo_note == "file was moved or renamed again"

    async def test_delete_file_is_never_undoable(self) -> None:
        run_id = await self.repo.start_run(RunSource.APP, None)
        op = await self.repo.record_pending(
            run_id, OpKind.DELETE_FILE, Path(r"C:\m\Thumbs.db"), None
        )
        await self.repo.mark_done(op)
        [o] = await self.repo.operations(run_id)
        assert not o.undoable

    async def test_seq_increments_per_run(self) -> None:
        r1 = await self.repo.start_run(RunSource.APP, None)
        r2 = await self.repo.start_run(RunSource.APP, None)
        for run_id in (r1, r2, r1):
            await self.repo.record_pending(run_id, OpKind.CREATE_DIR, None, Path(r"C:\m\d"))
        assert [o.seq for o in await self.repo.operations(r1)] == [1, 2]
        assert [o.seq for o in await self.repo.operations(r2)] == [1]

    async def test_pending_operations_and_undos_listed(self) -> None:
        repo = self.repo
        run_id = await repo.start_run(RunSource.APP, None)
        pending = await repo.record_pending(run_id, OpKind.RENAME, A, B)
        done = await repo.record_pending(run_id, OpKind.RENAME, B, A)
        await repo.mark_done(done)
        await repo.mark_undo(done, UndoState.PENDING)
        assert [o.id for o in await repo.pending_operations()] == [pending]
        assert [o.id for o in await repo.pending_undos()] == [done]
        assert [o.id for o in await repo.get_operations([done, pending])] == [pending, done]
        assert await repo.get_operations([]) == []

    async def test_run_with_pending_ops_is_kept(self) -> None:
        # MediaForge review I4: a run interrupted mid-rename must stay in history so recovery
        # can settle it.
        run_id = await self.repo.start_run(RunSource.RIGHT_CLICK, None)
        await self.repo.record_pending(run_id, OpKind.RENAME, A, B)
        assert await self.repo.finish_run(run_id) is True
        assert [o.run_id for o in await self.repo.pending_operations()] == [run_id]

    async def test_was_undone(self) -> None:
        run_id = await self.repo.start_run(RunSource.AUTOMATIC, "Series (Folder)")
        op = await self.repo.record_pending(run_id, OpKind.RENAME, A, B)
        await self.repo.mark_done(op)
        assert await self.repo.finish_run(run_id)
        assert not await self.repo.was_undone(A)
        await self.repo.mark_undo(op, UndoState.DONE)
        assert await self.repo.was_undone(A)
        assert await self.repo.was_undone(Path(str(A).upper()))  # Windows paths: any case
        assert not await self.repo.was_undone(B)

    async def test_runs_remember_their_kinds(self) -> None:
        repo = self.repo
        both = await repo.start_run(
            RunSource.APP, "Auto", kinds=[MediaKind.EPISODE, MediaKind.MOVIE]
        )
        shows = await repo.start_run(
            RunSource.AUTOMATIC, "Series (Folder)", kinds=[MediaKind.EPISODE]
        )
        unknown = await repo.start_run(RunSource.APP, "Auto")
        for run_id in (both, shows, unknown):
            op = await repo.record_pending(run_id, OpKind.RENAME, A, B)
            await repo.mark_done(op)
            assert await repo.finish_run(run_id)
        kinds = {r.id: r.kinds for r in await repo.list_runs()}
        assert kinds[both] == {MediaKind.EPISODE, MediaKind.MOVIE}
        assert kinds[shows] == {MediaKind.EPISODE}
        assert kinds[unknown] == frozenset()

    async def test_a_version_1_database_gains_kinds(self) -> None:
        path = self.tmp / "old.db"
        async with aiosqlite.connect(path) as conn:
            await conn.executescript(MIGRATIONS[0])  # MediaForge 1.0–1.2's schema
            await conn.execute("PRAGMA user_version=1")
            await conn.execute(
                "INSERT INTO runs (started_at, finished_at, source, preset_name) "
                "VALUES (1, 2, 'app', 'Auto')"
            )
            await conn.execute(
                "INSERT INTO operations (run_id, seq, kind, src, dst, state) "
                "VALUES (1, 1, 'rename', 'a', 'b', 'done')"
            )
            await conn.commit()
        db = await Database.open(path)
        try:
            [run] = await HistoryRepo(db).list_runs()
            assert run.kinds == frozenset() and run.file_count == 1
        finally:
            await db.close()
