"""The Subtitles extra in a run (Executor) and Undo with subtitles: a rename always stands, whatever
OpenSubtitles does; a fetched subtitle never stays behind its video."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast
from unittest import mock

from app.forge.clients.opensubtitles import OpenSubtitlesClient
from app.forge.core import executor as executor_module
from app.forge.core.executor import Executor, RunResult
from app.forge.core.planner import build_mixed_plan, build_plan, describe
from app.forge.core.subtitles import ForgeSubtitles, subtitle_path
from app.forge.core.undo import UndoService
from app.forge.models.history import OpKind, OpState, RunSource, UndoState
from app.forge.models.plan import Plan
from app.forge.models.preset import PresetOptions
from app.forge.models.subtitles import (
    SubtitleFetch,
    SubtitleFetchOutcome as Outcome,
    SubtitleIdentity,
    Unreachable,
)
from tests.forge.core.journal_base import HistoryTestCase, make_inputs
from tests.forge.core.test_subtitles import EPISODE, FakeClient, FakeSettings

ON = PresetOptions(subtitles=True)
RULE = "{ny}/{ny}"


class FakeFetcher:
    """What a run asks: gives `outcome` (or raises `error`, or hangs)."""

    def __init__(self, result: SubtitleFetch | None = None) -> None:
        self.result = result or SubtitleFetch(outcome=Outcome.SAVED, saved=("en",))
        self.calls: list[Path] = []
        self.error: Exception | None = None
        self.hang = False
        self.left: tuple[str, ...] = ("en",)

    async def fetch(
        self, video: Path, identity: SubtitleIdentity, *, run_id: int | None = None
    ) -> SubtitleFetch:
        self.calls.append(video)
        if self.error is not None:
            raise self.error
        if self.hang:
            await asyncio.Event().wait()
        return self.result

    async def missing(self, video: Path) -> tuple[str, ...]:
        return self.left


class RunCase(HistoryTestCase):
    async def run_with(self, plan: Plan, fetcher: object) -> RunResult:
        executor = Executor(self.history, None, None, subtitles=fetcher)  # type: ignore[arg-type]
        return await executor.run(
            plan,
            source=RunSource.APP,
            preset_name="p",
            on_progress=lambda event: None,
            cancel=asyncio.Event(),
        )

    def real(self, client: FakeClient | None = None) -> ForgeSubtitles:
        return ForgeSubtitles(
            cast(OpenSubtitlesClient, client or FakeClient()), FakeSettings(), self.history
        )


class PlanTests(RunCase):
    async def test_the_extra_plans_every_video_at_its_new_name(self) -> None:
        plan = build_plan(make_inputs(self.tmp, {"x.mkv": "Dune"}), RULE, ON)
        self.assertEqual(
            [s.video for s in plan.subtitles], [self.tmp / "Dune (2024)" / "Dune (2024).mkv"]
        )
        self.assertEqual(plan.subtitles[0].identity.title, "Dune")
        self.assertEqual(plan.subtitles[0].identity.release, "m.mkv")  # make_inputs' source_name
        self.assertIn("Subtitles for: Dune (2024).mkv", describe(plan))

    async def test_off_plans_none(self) -> None:
        plan = build_plan(make_inputs(self.tmp, {"x.mkv": "Dune"}), RULE, PresetOptions())
        self.assertEqual(plan.subtitles, ())

    async def test_a_mixed_plan_keeps_them(self) -> None:
        one = make_inputs(self.tmp, {"a.mkv": "Dune"})
        two = make_inputs(self.tmp, {"b.mkv": "Heat"})
        two = [two[0].model_copy(update={"item_id": 5})]
        plan = build_mixed_plan([(one, RULE, ON), (two, RULE, PresetOptions())])
        self.assertEqual([s.item_id for s in plan.subtitles], [1])


class ExtraTests(RunCase):
    async def test_on_gets_them_right_after_the_rename(self) -> None:
        plan = build_plan(make_inputs(self.tmp, {"x.mkv": "Dune"}), RULE, ON)
        result = await self.run_with(plan, self.real())
        video = self.tmp / "Dune (2024)" / "Dune (2024).mkv"
        self.assertTrue(video.exists())
        self.assertTrue((video.parent / "Dune (2024).en.srt").exists())
        self.assertTrue((video.parent / "Dune (2024).nl.srt").exists())
        self.assertEqual((result.renamed, result.subtitles_fetched, result.failed), (1, 2, 0))
        assert result.run_id is not None
        ops = await self.history.operations(result.run_id)
        kinds = [o.kind for o in ops if o.dst and o.dst.endswith(".srt")]
        self.assertEqual(kinds, [OpKind.CREATE_FILE, OpKind.CREATE_FILE])

    async def test_off_asks_nothing(self) -> None:
        fetcher = FakeFetcher()
        plan = build_plan(make_inputs(self.tmp, {"x.mkv": "Dune"}), RULE, PresetOptions())
        result = await self.run_with(plan, fetcher)
        self.assertEqual(fetcher.calls, [])
        self.assertEqual(result.subtitles_fetched, 0)

    async def test_an_existing_srt_spends_nothing(self) -> None:
        client = FakeClient()
        inputs = make_inputs(self.tmp, {"x.mkv": "Dune"})
        target = self.tmp / "Dune (2024)" / "Dune (2024).mkv"
        target.parent.mkdir()
        for language in ("en", "nl"):
            subtitle_path(target, language).write_bytes(b"mine")
        result = await self.run_with(build_plan(inputs, RULE, ON), self.real(client))
        self.assertEqual(result.renamed, 1)
        self.assertEqual(client.downloads, [])
        self.assertEqual(client.searches, [])

    async def test_none_found_says_so_in_the_journal(self) -> None:
        client = FakeClient()
        client.found = []
        plan = build_plan(make_inputs(self.tmp, {"x.mkv": "Dune"}), RULE, ON)
        result = await self.run_with(plan, self.real(client))
        self.assertEqual((result.renamed, result.failed), (1, 0))
        assert result.run_id is not None
        failed = [
            o for o in await self.history.operations(result.run_id) if o.state is OpState.FAILED
        ]
        self.assertEqual([Path(o.dst or "").name for o in failed], ["Dune (2024).en.srt", "Dune (2024).nl.srt"])
        self.assertEqual({o.error for o in failed}, {executor_module.NO_SUBTITLE_FOUND})

    async def test_a_used_up_allowance_leaves_the_rename_done(self) -> None:
        client = FakeClient()
        client.quota_error = True
        inputs = make_inputs(self.tmp, {"a.mkv": "Dune", "b.mkv": "Heat"})
        result = await self.run_with(build_plan(inputs, RULE, ON), self.real(client))
        self.assertEqual((result.renamed, result.failed, result.subtitles_fetched), (2, 0, 0))
        self.assertTrue((self.tmp / "Dune (2024)" / "Dune (2024).mkv").exists())
        self.assertTrue((self.tmp / "Heat (2024)" / "Heat (2024).mkv").exists())
        assert result.run_id is not None
        ops = await self.history.operations(result.run_id)
        notes = {o.error for o in ops if o.state is OpState.FAILED}
        self.assertEqual(notes, {"Today's subtitle downloads are used up"})
        self.assertEqual(len([o for o in ops if o.state is OpState.FAILED]), 4)  # each says so

    async def test_a_failure_leaves_the_rename_done_and_stops_the_asking(self) -> None:
        client = FakeClient()
        client.search_error = Unreachable("Couldn't reach OpenSubtitles")
        inputs = make_inputs(self.tmp, {"a.mkv": "Dune", "b.mkv": "Heat"})
        result = await self.run_with(build_plan(inputs, RULE, ON), self.real(client))
        self.assertEqual((result.renamed, result.failed), (2, 0))
        self.assertEqual(len(client.searches), 1)  # one try, not one per video

    async def test_an_unexpected_error_never_fails_a_rename(self) -> None:
        fetcher = FakeFetcher()
        fetcher.error = RuntimeError("boom")
        plan = build_plan(make_inputs(self.tmp, {"x.mkv": "Dune"}), RULE, ON)
        result = await self.run_with(plan, fetcher)
        self.assertEqual((result.renamed, result.failed), (1, 0))

    async def test_it_is_time_limited(self) -> None:
        fetcher = FakeFetcher()
        fetcher.hang = True
        inputs = make_inputs(self.tmp, {"a.mkv": "Dune", "b.mkv": "Heat"})
        with mock.patch.object(executor_module, "SUBTITLE_LIMIT_S", 0.01):
            result = await self.run_with(build_plan(inputs, RULE, ON), fetcher)
        self.assertEqual((result.renamed, result.failed), (2, 0))
        self.assertEqual(len(fetcher.calls), 1)  # no answer: the others aren't asked
        assert result.run_id is not None
        notes = {
            o.error
            for o in await self.history.operations(result.run_id)
            if o.state is OpState.FAILED
        }
        self.assertEqual(notes, {executor_module.SUBTITLES_TOO_SLOW})

    async def test_a_failed_rename_gets_none(self) -> None:
        fetcher = FakeFetcher()
        inputs = make_inputs(self.tmp, {"x.mkv": "Dune"})
        plan = build_plan(inputs, RULE, ON)
        inputs[0].source.unlink()
        result = await self.run_with(plan, fetcher)
        self.assertEqual(result.failed, 1)
        self.assertEqual(fetcher.calls, [])

    async def test_a_file_already_right_gets_them_too(self) -> None:
        fetcher = FakeFetcher()
        inputs = make_inputs(self.tmp, {"Dune (2024).mkv": "Dune"})
        result = await self.run_with(build_plan(inputs, "{ny}", ON), fetcher)
        self.assertEqual(result.skipped, 1)
        self.assertEqual(fetcher.calls, [self.tmp / "Dune (2024).mkv"])

    async def test_cancelled_asks_nothing_more(self) -> None:
        fetcher = FakeFetcher()
        plan = build_plan(make_inputs(self.tmp, {"x.mkv": "Dune"}), RULE, ON)
        executor = Executor(self.history, None, None, subtitles=fetcher)
        cancel = asyncio.Event()
        cancel.set()
        await executor.run(
            plan,
            source=RunSource.APP,
            preset_name="p",
            on_progress=lambda event: None,
            cancel=cancel,
        )
        self.assertEqual(fetcher.calls, [])


class UndoCase(RunCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.undo = UndoService(self.history)
        self.inputs = make_inputs(self.tmp, {"x.mkv": "Dune"})
        self.source = self.tmp / "x.mkv"
        self.video = self.tmp / "Dune (2024)" / "Dune (2024).mkv"
        self.subtitle = self.video.parent / "Dune (2024).en.srt"

    async def rename(self, options: PresetOptions) -> int:
        result = await self.run_with(build_plan(self.inputs, RULE, options), self.real())
        assert result.run_id is not None
        return result.run_id

    async def op_of(self, run_id: int, dst: Path) -> int:
        ops = await self.history.operations(run_id)
        return next(o.id for o in ops if o.dst == str(dst))


class UndoFetchedDuringTheRenameTests(UndoCase):
    async def test_undoing_the_run_takes_it_away(self) -> None:
        run = await self.rename(ON)
        self.assertTrue(self.subtitle.exists())
        result = await self.undo.undo_run(run)
        self.assertEqual(result.skipped, [])
        self.assertTrue(self.source.exists())
        self.assertFalse(self.video.parent.exists())  # nothing keeps the new folder

    async def test_undoing_only_the_video_takes_it_too(self) -> None:
        run = await self.rename(ON)
        await self.undo.undo_operations([await self.op_of(run, self.video)])
        self.assertTrue(self.source.exists())
        self.assertFalse(self.subtitle.exists())
        self.assertFalse((self.tmp / "x.en.srt").exists())
        ops = {o.dst: o for o in await self.history.operations(run)}
        self.assertEqual(ops[str(self.subtitle)].undo_state, UndoState.DONE)

    async def test_undoing_only_the_subtitle_leaves_the_video(self) -> None:
        run = await self.rename(ON)
        await self.undo.undo_operations([await self.op_of(run, self.subtitle)])
        self.assertFalse(self.subtitle.exists())
        self.assertTrue(self.video.exists())

    async def test_one_changed_since_goes_back_with_its_video(self) -> None:
        run = await self.rename(ON)
        self.subtitle.write_bytes(b"my own corrections")
        await self.undo.undo_run(run)
        self.assertTrue(self.source.exists())
        self.assertEqual((self.tmp / "x.en.srt").read_bytes(), b"my own corrections")
        self.assertFalse(self.subtitle.exists())

    async def test_its_failed_lines_undo_nothing(self) -> None:
        client = FakeClient()
        client.found = []
        result = await self.run_with(build_plan(self.inputs, RULE, ON), self.real(client))
        assert result.run_id is not None
        undone = await self.undo.undo_run(result.run_id)
        self.assertEqual(undone.skipped, [])
        self.assertTrue(self.source.exists())


class UndoFetchedLaterTests(UndoCase):
    async def later(self, run: int) -> None:
        found = await self.real().fetch(self.video, EPISODE, ("en",), run_id=run)
        self.assertEqual(found.outcome, Outcome.SAVED)

    async def test_undoing_the_run_takes_it_away(self) -> None:
        run = await self.rename(PresetOptions())
        await self.later(run)
        self.assertTrue(self.subtitle.exists())
        result = await self.undo.undo_run(run)
        self.assertEqual(result.skipped, [])
        self.assertTrue(self.source.exists())
        self.assertFalse(self.video.parent.exists())

    async def test_undoing_only_the_video_takes_it_too(self) -> None:
        run = await self.rename(PresetOptions())
        await self.later(run)
        await self.undo.undo_operations([await self.op_of(run, self.video)])
        self.assertTrue(self.source.exists())
        self.assertFalse(self.subtitle.exists())

    async def test_the_run_counts_it_as_downloaded(self) -> None:
        run = await self.rename(PresetOptions())
        await self.later(run)
        summary = next(r for r in await self.history.list_runs() if r.id == run)
        self.assertEqual(summary.created_count, 1)


class UndoNotJournaledTests(UndoCase):
    """A subtitle the journal doesn't know, beside a video being put back (the owner's, or from
    AirGrabber's days)."""

    async def test_goes_back_with_its_video_under_the_old_name(self) -> None:
        run = await self.rename(PresetOptions())
        self.subtitle.write_bytes(b"mine")
        result = await self.undo.undo_run(run)
        self.assertEqual((self.tmp / "x.en.srt").read_bytes(), b"mine")
        self.assertFalse(self.video.parent.exists())
        self.assertEqual(result.skipped, [])

    async def test_stays_when_the_old_name_is_taken(self) -> None:
        run = await self.rename(PresetOptions())
        self.subtitle.write_bytes(b"mine")
        (self.tmp / "x.en.srt").write_bytes(b"other")
        await self.undo.undo_run(run)
        self.assertTrue(self.source.exists())
        self.assertEqual(self.subtitle.read_bytes(), b"mine")
        self.assertEqual((self.tmp / "x.en.srt").read_bytes(), b"other")

    async def test_another_videos_subtitle_stays(self) -> None:
        run = await self.rename(PresetOptions())
        other = self.video.parent / "Other.en.srt"
        other.write_bytes(b"other")
        await self.undo.undo_run(run)
        self.assertEqual(other.read_bytes(), b"other")


class UndoMovedAlongTests(UndoCase):
    async def test_a_subtitle_moved_along_goes_back_with_only_the_video_chosen(self) -> None:
        (self.tmp / "x.en.srt").write_bytes(b"there before")
        run = await self.rename(PresetOptions())
        self.assertTrue(self.subtitle.exists())
        result = await self.undo.undo_operations([await self.op_of(run, self.video)])
        self.assertEqual(result.skipped, [])
        self.assertEqual((self.tmp / "x.en.srt").read_bytes(), b"there before")
        self.assertFalse(self.subtitle.exists())
