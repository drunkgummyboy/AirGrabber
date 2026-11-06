"""The History page (app/forge/ui/history_view.py), ported from MediaForge's
tests/ui/test_history_view.py: runs newest first and grouped by day, undo a whole run or ticked
files, partly / fully undone, skipped files reported, the Series | Movies switch, one undo at a
time, big runs and many runs stay smooth. Plus: any of the kept runs can be undone in any order,
day labels and journal notes in Dutch, and the Forge look."""

from __future__ import annotations

import asyncio
import gc
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import cast
from unittest import mock

import customtkinter as ctk

from app.forge.core.executor import Executor
from app.forge.core.history_service import HistoryService, matching_operations
from app.forge.core.planner import build_plan
from app.forge.core.undo import MOVED_AGAIN, UndoService
from app.forge.db.database import Database
from app.forge.db.history import HistoryRepo
from app.forge.db.moves import MoveRepository
from app.forge.models.history import OpKind, RunSource
from app.forge.models.media import MediaKind
from app.forge.models.preset import PresetOptions
from app.forge.services import ForgeServices
from app.forge.ui.history_view import HistoryView, day_label
from app.forge.ui.look import app_look
from app.i18n import Language, current_language, set_language, t
from tests.forge.base import ForgeAsyncTestCase, ForgeTestCase
from tests.forge.core.journal_base import FakePosters, make_inputs
from tests.forge.ui.subtitle_kit import FakeSubtitleSource
from tests.forge.tk import pump


@dataclass
class _Services:
    """The parts of ForgeServices the History page uses, on a temporary forge.db."""

    history: HistoryService
    undo: UndoService
    executor: Executor
    repo: HistoryRepo
    subtitles: FakeSubtitleSource
    moves: MoveRepository
    db: Database

    def warm_up(self) -> None:
        pass


class HistoryViewCase(ForgeAsyncTestCase):
    def setUp(self) -> None:
        super().setUp()
        gc.collect()
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.root.geometry("1200x800+20+20")
        self.addCleanup(self._destroy_root)
        language = current_language()
        self.addCleanup(set_language, language)

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        asyncio.get_running_loop().slow_callback_duration = 2.0  # Tk work in a test is slow
        db = await Database.open(self.data_dir / "forge.db")
        self.addAsyncCleanup(db.close)
        repo = HistoryRepo(db)
        self.parts = _Services(
            history=HistoryService(repo),
            undo=UndoService(repo),
            executor=Executor(repo, FakePosters(), None),
            repo=repo,
            subtitles=FakeSubtitleSource(),
            moves=MoveRepository(db),
            db=db,
        )
        self.services = cast(ForgeServices, self.parts)
        self.view = HistoryView(self.root, self.services)
        self.view.pack(fill="both", expand=True)
        self.asked: list[str] = []
        self.told: list[str] = []

        async def yes(title: str, message: str) -> bool:
            self.asked.append(message)
            return True

        async def note(title: str, message: str) -> None:
            self.told.append(message)

        self.view.confirm = yes
        self.view.inform = note
        self.addCleanup(self._destroy_view)
        await pump(self.root, 0.05)

    def _destroy_view(self) -> None:
        if self.view.winfo_exists():
            self.view.destroy()

    async def forge(self, names: dict[str, str], preset: str) -> int:
        plan = build_plan(make_inputs(self.tmp, names), "{ny}", PresetOptions())
        result = await self.parts.executor.run(
            plan,
            source=RunSource.APP,
            preset_name=preset,
            on_progress=lambda e: None,
            cancel=asyncio.Event(),
        )
        assert result.run_id is not None
        return result.run_id

    async def run_of(
        self,
        kinds: tuple[MediaKind, ...] = (),
        *,
        source: RunSource = RunSource.APP,
        started_at: datetime | None = None,
    ) -> int:
        repo = self.parts.repo
        run_id = await repo.start_run(source, "Auto", started_at=started_at, kinds=kinds)
        op = await repo.record_pending(run_id, OpKind.RENAME, Path("a.mkv"), Path("b.mkv"))
        await repo.mark_done(op)
        assert await repo.finish_run(run_id)
        return run_id

    def videos(self) -> list[str]:
        return sorted(p.name for p in self.tmp.iterdir() if p.suffix == ".mkv")


class HistoryViewTest(HistoryViewCase):
    async def test_lists_runs_newest_first(self) -> None:
        first = await self.forge({"a.mkv": "Alien"}, "Movie (Flat)")
        second = await self.forge({"b.mkv": "Heat", "c.mkv": "Ran"}, "Auto")
        await self.view.reload()
        await pump(self.root, 0.05)
        self.assertEqual([card.run.id for card in self.view.cards], [second, first])
        self.assertIn("2 files", self.view.cards[0].summary_text())
        self.assertIn("Auto", self.view.cards[0].summary_text())
        self.assertIn("1 file ·", self.view.cards[1].summary_text())

    async def test_empty_history_message(self) -> None:
        await self.view.reload()
        self.assertEqual(self.view.cards, [])
        self.assertNotEqual(self.view.empty_label.winfo_manager(), "")
        self.assertEqual(
            self.view.empty_label.cget("text"),
            "Nothing here yet. Runs show up after you Forge some files.",
        )

    async def test_undo_run_button_reverts_files(self) -> None:
        run_id = await self.forge({"a.mkv": "Alien", "b.mkv": "Heat"}, "Auto")
        await self.view.reload()
        await self.view.undo_run(run_id)
        self.assertEqual(self.videos(), ["a.mkv", "b.mkv"])
        self.assertTrue(self.asked and "2 renamed files" in self.asked[0])
        self.assertEqual(self.view.cards[0].run.status, "undone")

    async def test_undo_selected_only(self) -> None:
        run_id = await self.forge({"a.mkv": "Alien", "b.mkv": "Heat"}, "Auto")
        await self.view.reload()
        card = self.view.cards[0]
        await card.expand()
        renames = [o for o in card.operations if o.kind is OpKind.RENAME]
        first = next(o for o in renames if o.src == str(self.tmp / "a.mkv"))
        for op_id in list(card.checks):
            card.tick(op_id, op_id == first.id)
        self.assertEqual(card.selected_ids(), [first.id])
        assert card.files is not None and card.selected_button is not None
        self.assertEqual(
            [row.checked for row in card.files.rows],
            [row.key == first.id for row in card.files.rows],
        )
        self.assertEqual(card.selected_button.cget("text"), "Undo 1 selected")
        await self.view.undo_selected(run_id)
        self.assertTrue((self.tmp / "a.mkv").exists())
        self.assertTrue((self.tmp / "Heat (2024).mkv").exists())
        self.assertEqual(self.view.cards[0].run.status, "partly-undone")
        self.assertEqual(self.view.cards[0].tag_text(), "Partly undone")

    async def test_undone_files_are_shown_but_cannot_be_ticked_again(self) -> None:
        run_id = await self.forge({"a.mkv": "Alien", "b.mkv": "Heat"}, "Auto")
        await self.view.reload()
        card = self.view.cards[0]
        await card.expand()
        first = next(o for o in card.operations if o.src == str(self.tmp / "a.mkv"))
        card.tick(next(i for i in card.checks if i != first.id), False)
        await self.view.undo_selected(run_id)
        card = self.view.cards[0]
        await card.expand()
        assert card.files is not None
        row = next(r for r in card.files.rows if r.key == first.id)
        self.assertTrue(row.check_disabled)
        self.assertIn("already undone", row.subtitle)
        self.assertIn("was a.mkv", row.subtitle)
        self.assertEqual(len(card.selected_ids()), 1)

    async def test_undone_run_dimmed_with_tag(self) -> None:
        run_id = await self.forge({"a.mkv": "Alien"}, "Auto")
        await self.view.reload()
        await self.view.undo_run(run_id)
        card = self.view.cards[0]
        self.assertTrue(card.tag_text().startswith("Undone"))
        self.assertEqual(card.undo_button.cget("state"), "disabled")

    async def test_skipped_items_are_reported(self) -> None:
        run_id = await self.forge({"a.mkv": "Alien"}, "Auto")
        (self.tmp / "Alien (2024).mkv").rename(self.tmp / "moved.mkv")
        await self.view.reload()
        await self.view.undo_run(run_id)
        self.assertTrue(self.told)
        self.assertIn("moved or renamed again", self.told[-1])
        self.assertIn("0 changes reversed", self.told[-1])

    async def test_skipped_reasons_are_shown_in_dutch(self) -> None:
        run_id = await self.forge({"a.mkv": "Alien"}, "Auto")
        (self.tmp / "Alien (2024).mkv").rename(self.tmp / "moved.mkv")
        await self.view.reload()
        set_language(Language.NL)
        await self.view.undo_run(run_id)
        dutch = t(MOVED_AGAIN)
        self.assertNotEqual(dutch, MOVED_AGAIN)
        self.assertIn(f"Alien (2024).mkv ({dutch})", self.told[-1])

    async def test_undo_twice_at_once_asks_once(self) -> None:
        run_id = await self.forge({"a.mkv": "Alien"}, "Auto")
        await self.view.reload()
        await asyncio.gather(self.view.undo_run(run_id), self.view.undo_run(run_id))
        self.assertEqual(len(self.asked), 1)
        self.assertFalse(self.view.busy)

    async def test_busy_while_an_undo_runs(self) -> None:
        run_id = await self.forge({"a.mkv": "Alien"}, "Auto")
        await self.view.reload()
        seen: list[bool] = []

        async def look(title: str, message: str) -> bool:
            seen.append(self.view.busy)
            return False  # Cancel: nothing happens

        self.view.confirm = look
        await self.view.undo_run(run_id)
        self.assertEqual(seen, [True])
        self.assertFalse(self.view.busy)
        self.assertEqual(self.videos(), ["Alien (2024).mkv"])

    async def test_any_run_can_be_undone_in_any_order(self) -> None:
        # The owner's to-do: undo any of the last 50 runs, not only the latest, in any order.
        first = await self.forge({"a.mkv": "Alien"}, "Auto")
        second = await self.forge({"b.mkv": "Heat"}, "Auto")
        third = await self.forge({"c.mkv": "Ran"}, "Auto")
        await self.view.reload()
        self.assertTrue(all(c.undo_button.cget("state") == "normal" for c in self.view.cards))
        await self.view.undo_run(second)
        self.assertEqual(self.videos(), ["Alien (2024).mkv", "Ran (2024).mkv", "b.mkv"])
        await self.view.undo_run(first)
        self.assertEqual(self.videos(), ["Ran (2024).mkv", "a.mkv", "b.mkv"])
        status = {c.run.id: c.run.status for c in self.view.cards}
        self.assertEqual(status, {first: "undone", second: "undone", third: "active"})

    async def test_show_files_for_a_big_run_opens_quickly(self) -> None:
        # MediaForge's review C1: one tick-box widget per file froze the window on big runs.
        await self.forge({f"m{n}.mkv": f"Film {n}" for n in range(300)}, "Auto")
        await self.view.reload()
        card = self.view.cards[0]
        start = time.perf_counter()
        await card.expand()
        self.root.update()
        self.assertLess(time.perf_counter() - start, 0.3)
        self.assertEqual(len(card.selected_ids()), 300)
        await card.toggle()
        self.assertFalse(card.expanded)
        self.assertEqual(card.toggle_button.cget("text"), "Show files ▾")

    async def test_reload_gives_the_window_turns_while_building_cards(self) -> None:
        await self.forge({"a.mkv": "Alien"}, "Auto")
        [run] = await self.parts.history.list_runs()
        runs = [run.model_copy() for _ in range(50)]

        async def fifty_runs() -> list[object]:
            return list(runs)

        gaps: list[float] = []
        done = False

        async def ticker() -> None:
            while not done:
                start = time.perf_counter()
                await asyncio.sleep(0)
                gaps.append(time.perf_counter() - start)

        with mock.patch.object(self.parts.history, "list_runs", fifty_runs):
            tick = asyncio.ensure_future(ticker())
            await asyncio.sleep(0)
            await self.view.reload()
            done = True
            await tick
        self.assertEqual(len(self.view.cards), 50)
        self.assertLess(max(gaps), 0.2)

    async def test_runs_are_grouped_under_day_headings(self) -> None:
        await self.run_of(started_at=datetime.now() - timedelta(days=1))
        await self.run_of()
        await self.run_of()
        await self.view.reload()
        await pump(self.root, 0.05)
        self.assertEqual(self.view.day_labels(), ["Today", "Yesterday"])
        self.assertEqual([len(c) for c in self.view.cards_by_day().values()], [2, 1])
        self.assertEqual(self.view._headings[0][1].cget("text"), "TODAY")

    async def test_automatic_runs_are_labelled(self) -> None:
        await self.run_of(source=RunSource.AUTOMATIC)
        await self.view.reload()
        await pump(self.root, 0.05)
        self.assertEqual(self.view.cards[0].tag_text(), "automatic")

    async def test_every_run_shows_by_default(self) -> None:
        old = await self.run_of(())
        episodes = await self.run_of((MediaKind.EPISODE,))
        movies = await self.run_of((MediaKind.MOVIE,))
        self.assertEqual(self.view.kind, "all")
        await self.view.reload()
        await pump(self.root, 0.05)
        self.assertEqual([c.run.id for c in self.view.cards], [movies, episodes, old])

    async def test_history_follows_the_switch(self) -> None:
        old = await self.run_of(())  # from before 1.3: no kinds, shown on both sides
        episodes = await self.run_of((MediaKind.EPISODE,))
        movies = await self.run_of((MediaKind.MOVIE,))
        self.view.set_kind("movies")
        await self.view.reload()
        await pump(self.root, 0.05)
        self.assertEqual([c.run.id for c in self.view.cards], [movies, old])
        self.view.set_kind("series")
        await self.view.reload()
        await pump(self.root, 0.05)
        self.assertEqual([c.run.id for c in self.view.cards], [episodes, old])

    async def test_an_empty_side_says_so(self) -> None:
        await self.run_of((MediaKind.MOVIE,))
        self.view.set_kind("series")
        await self.view.reload()
        await pump(self.root, 0.05)
        self.assertEqual(self.view.cards, [])
        self.assertNotEqual(self.view.empty_label.winfo_manager(), "")
        self.assertEqual(self.view.empty_label.cget("text"), "No renamed shows yet")

    async def test_dutch_texts(self) -> None:
        set_language(Language.NL)
        await self.run_of(source=RunSource.AUTOMATIC)
        await self.view.reload()
        card = self.view.cards[0]
        self.assertEqual(self.view.day_labels(), ["Vandaag"])
        self.assertEqual(card.tag_text(), "automatisch")
        self.assertEqual(card.summary_text(), "1 bestand · Auto")
        self.assertEqual(card.undo_button.cget("text"), "Ronde ongedaan maken")

    async def test_cards_wear_the_app_look(self) -> None:
        await self.run_of()
        await self.view.reload()
        look = app_look()
        card = self.view.cards[0]
        self.assertEqual(tuple(card.cget("fg_color")), look.SURFACE)
        self.assertEqual(tuple(card.undo_button.cget("fg_color")), look.ACCENT)
        self.assertEqual(tuple(self.view.cget("fg_color")), look.BG)

    async def test_destroy_cancels_its_tasks(self) -> None:
        started = asyncio.Event()

        async def forever() -> None:
            started.set()
            await asyncio.sleep(60)

        task = self.view.spawn(forever())
        await started.wait()
        self.view.destroy()
        await asyncio.sleep(0)
        self.assertTrue(task.cancelled())


class HistorySearchTest(HistoryViewCase):
    async def searched(self, query: str) -> None:
        self.view.search(query)
        await asyncio.gather(*self.view._tasks, return_exceptions=True)
        await pump(self.root, 0.05)

    async def test_keeps_the_runs_with_a_match_and_shows_just_those_files(self) -> None:
        heat = await self.forge({"a.mkv": "Heat", "b.mkv": "Ran"}, "Auto")
        alien = await self.forge({"c.mkv": "Alien"}, "Movie (Flat)")
        await self.view.reload()
        self.assertEqual([card.run.id for card in self.view.cards], [alien, heat])
        await self.searched("heat")
        [card] = self.view.cards
        self.assertEqual(card.run.id, heat)
        self.assertTrue(card.expanded)  # opens by itself
        assert card.files is not None
        self.assertEqual(len([r for r in card.files.rows if r.kind == "item"]), 1)
        self.assertEqual(len(card.checks), 1)  # Undo N selected takes just that one
        await self.searched("")
        self.assertEqual([card.run.id for card in self.view.cards], [alien, heat])
        self.assertFalse(self.view.cards[0].expanded)

    async def test_nothing_found_says_so(self) -> None:
        await self.forge({"a.mkv": "Heat"}, "Auto")
        await self.view.reload()
        await self.searched("dune")
        self.assertEqual(self.view.cards, [])
        self.assertIn("dune", self.view.empty_label.cget("text"))
        self.assertTrue(self.view.empty_label.winfo_manager())

    async def test_typing_searches_a_moment_after_the_last_key_and_escape_clears(self) -> None:
        await self.forge({"a.mkv": "Heat"}, "Auto")
        await self.forge({"c.mkv": "Alien"}, "Auto")
        await self.view.reload()
        self.view.search_entry.insert(0, "alien")
        self.view._search_typed()
        self.assertEqual(self.view.query, "")  # not yet
        await pump(self.root, 0.3)
        await asyncio.gather(*self.view._tasks, return_exceptions=True)
        self.assertEqual(self.view.query, "alien")
        self.assertEqual(len(self.view.cards), 1)
        self.assertEqual(self.view._search_cleared(), "break")
        await pump(self.root, 0.3)
        await asyncio.gather(*self.view._tasks, return_exceptions=True)
        self.assertEqual(self.view.query, "")
        self.assertEqual(len(self.view.cards), 2)


class ManyRunsTest(HistoryViewCase):
    async def test_a_batch_at_a_time_with_the_rest_on_request(self) -> None:
        with mock.patch("app.forge.ui.history_view._RUNS_AT_A_TIME", 2):
            newest_first = [
                await self.forge({f"{n}.mkv": f"Film {n}"}, "Auto") for n in range(5)
            ][::-1]
            await self.view.reload()
            await pump(self.root, 0.05)
            self.assertEqual([c.run.id for c in self.view.cards], newest_first[:2])
            self.assertEqual(self.view.more_button.cget("text"), "Show 2 more")
            self.assertTrue(self.view.more_button.winfo_manager())
            await self.view._add_cards(self.view._generation)
            self.assertEqual([c.run.id for c in self.view.cards], newest_first[:4])
            self.assertEqual(self.view.more_button.cget("text"), "Show 1 more")
            await self.view._add_cards(self.view._generation)
            self.assertEqual([c.run.id for c in self.view.cards], newest_first)
            self.assertEqual(self.view.more_button.winfo_manager(), "")

    async def test_a_short_list_asks_only_for_its_rows_room(self) -> None:
        run = await self.forge({"a.mkv": "Heat"}, "Auto")
        await self.view.reload()
        await pump(self.root, 0.05)
        card = self.view._card(run)
        assert card is not None
        await card.expand()
        await pump(self.root, 0.05)
        assert card.files is not None
        self.assertLess(card.files.winfo_reqheight(), card.files._px(200))


class MatchingTest(ForgeTestCase):
    def test_every_word_in_a_name_without_capitals_or_accents(self) -> None:
        ops = [
            (1, 10, r"D:\Downloads\Copper.Harbor.S02E01.mkv", r"S:\Series\Copper Harbor\S02E01.mkv"),
            (1, 11, r"D:\Downloads\Amélie.2001.mkv", r"M:\Amélie (2001).mkv"),
            (2, 12, None, r"M:\Heat (1995)\Heat (1995).mkv"),
        ]
        self.assertEqual(matching_operations("copper s02e01", ops), {1: {10}})
        self.assertEqual(matching_operations("AMELIE", ops), {1: {11}})
        self.assertEqual(matching_operations("heat", ops), {2: {12}})
        self.assertEqual(matching_operations("copper amelie", ops), {})  # one file holds them all
        self.assertEqual(matching_operations("  ", ops), {})

    def test_folders_higher_up_are_not_searched(self) -> None:
        ops = [(1, 10, r"D:\Downloads\Heat\heat.mkv", r"S:\Library\Heat\heat.mkv")]
        self.assertEqual(matching_operations("downloads", ops), {})
        self.assertEqual(matching_operations("library", ops), {})


class LabelTest(ForgeTestCase):
    def setUp(self) -> None:
        super().setUp()
        language = current_language()
        self.addCleanup(set_language, language)
        set_language(Language.EN)

    def test_day_label_texts(self) -> None:
        today = datetime(2026, 10, 31, 15, 0)
        self.assertEqual(day_label(datetime(2026, 10, 31, 9, 5), today), "Today")
        self.assertEqual(day_label(datetime(2026, 10, 30, 23, 59), today), "Yesterday")
        self.assertEqual(day_label(datetime(2026, 10, 29, 8, 0), today), "Thu 29 Oct")
        self.assertEqual(day_label(datetime(2025, 3, 4, 8, 5), today), "Tue 4 Mar 2025")

    def test_day_labels_in_dutch(self) -> None:
        set_language(Language.NL)
        today = datetime(2026, 10, 31, 15, 0)
        self.assertEqual(day_label(datetime(2026, 10, 31, 9, 5), today), "Vandaag")
        self.assertEqual(day_label(datetime(2026, 10, 30, 23, 59), today), "Gisteren")
        self.assertEqual(day_label(datetime(2026, 10, 29, 8, 0), today), "do 29 okt")
        self.assertEqual(day_label(datetime(2025, 3, 4, 8, 5), today), "di 4 mrt 2025")
