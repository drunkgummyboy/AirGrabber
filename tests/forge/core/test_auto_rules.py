"""Auto-rename driven by the rules for watched folders (app/forge/core/auto.py's look, status and
runs with `RenamerParts.rules` / `outputs`; docs/forge/2026-11-18-rules-design.md): rules top-down
with their own destinations and modes, files no rule fits left alone, a fresh rule's and an
un-pause's first look queueing as Manual (per rule and folder, stored for a restart), a changed rule or a gone preset holding its files, a
library folder not chosen holding only its rules' files, Stays sorting with its memory in the move
records (after a restart too) and never taking a file already named so, an unreachable
destination, one listing per folder, and logs without names.

Built like test_auto_renamer.py's `RenamerTestCase`: a temporary forge.db, the real queue,
resolver, executor, undo and move records, a fake TMDB, a fake clock and a fake rule service."""

from __future__ import annotations

import asyncio
import dataclasses
import os
import threading
from collections.abc import Collection, Sequence
from pathlib import Path
from unittest import mock

from app.forge.core import auto as auto_module
from app.forge.core import watch
from app.forge.core.auto import UNDONE, AutoRenamer, Unfit
from app.forge.core.executor import Executor
from app.forge.core.matcher import Resolver
from app.forge.core.moves import ForgeMoveService
from app.forge.core.queue import ItemStatus, Queue, QueueItem
from app.forge.core.rules import WatchedRule
from app.forge.core.rules_service import RuleService
from app.forge.core.undo import UndoService
from app.forge.db.history import HistoryRepo
from app.forge.db.matches import MatchRepo
from app.forge.db.moves import MoveRepository
from app.forge.db.rules import RuleRepo
from app.forge.models.rules import Rule, RuleTarget
from app.forge.models.settings import AirGrabberDownloads, AirGrabberFolder
from app.i18n import t
from app.models.tmdb import TMDBTitle
from tests.forge.core.journal_base import FakePosters
from tests.forge.core.test_auto_renamer import NOON, Clock, RenamerTestCase, Tmdb

SHOW = "VisionQuest.S01E02.Theseus.1080p.WEB.mkv"
SORTED = Path("VisionQuest (2025)", "VisionQuest (2025) - Season 01", "VisionQuest (2025) - S01E02 - Theseus.mkv")


class YearTmdb(Tmdb):
    """A film is found in the year its name says (Arrival 2016 too), each title its own id."""

    async def search_movie(self, query: str, year: int | None) -> list[TMDBTitle]:
        if self.down:
            return await super().search_movie(query, year)
        date = f"{year or 2021}-09-15"
        return [TMDBTitle(id=100 + len(query), title=query, date=date, poster_path="/8.jpg")]


class FakeRules:
    """RuleService as far as the renamer needs it: the rules in a list, first looks recorded."""

    def __init__(self) -> None:
        self.rules: list[Rule] = []
        self.done: list[list[int]] = []
        self.marked: list[int] = []
        self.changes = 0  # `generation`: bumped by a fresh mark (as RuleService)

    @property
    def current(self) -> tuple[Rule, ...]:
        return tuple(self.rules)

    @property
    def generation(self) -> int | None:
        return self.changes

    async def first_look_done(
        self, rule_ids: Collection[int], *, generation: int | None = None
    ) -> None:
        if generation is not None and generation != self.changes:
            return  # the rules changed since the look read them
        self.done.append(sorted(rule_ids))
        self.rules = [
            r.model_copy(update={"fresh": False}) if r.id in rule_ids else r for r in self.rules
        ]

    async def mark_fresh(self, rule_id: int) -> None:
        self.marked.append(rule_id)
        self.changes += 1
        self.rules = [
            r.model_copy(update={"fresh": True}) if r.id == rule_id else r for r in self.rules
        ]


def put(folder: Path, name: str, age_s: float = 3600) -> Path:
    """A finished download directly in `folder`."""
    folder.mkdir(parents=True, exist_ok=True)
    file = folder / name
    file.write_bytes(b"video")
    when = NOON - age_s
    os.utime(file, (when, when))
    return file


class RulesTestCase(RenamerTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.tmdb = YearTmdb()
        self.queue = Queue(Resolver(self.tmdb, MatchRepo(self.db)), lambda event: None)
        self.rules = FakeRules()
        self.moves = ForgeMoveService(
            MoveRepository(self.db),
            settings=lambda: self.settings.current,
            rules=lambda: self.rules.current,
        )
        self.history = HistoryRepo(self.db, on_moves_change=self.moves.changed)
        self.undo = UndoService(self.history)
        self.parts = dataclasses.replace(
            self.parts,
            queue=self.queue,
            history=self.history,
            executor=Executor(self.history, FakePosters(), None, moves=self.moves),
            tmdb=self.tmdb,
            rules=self.rules,
            outputs=self.moves,
        )
        self.dl = self.folders.downloads
        self.big = self.tmp / "Movies 4K"
        self.big.mkdir()
        await self.settings.update(
            series_folder=str(self.folders.series),
            movies_folder=str(self.folders.movies),
            automatic="on",
        )

    def set_rules(self, *rules: Rule) -> None:
        self.rules.rules = list(rules)

    def item(self, file: Path) -> QueueItem:
        [found] = [i for i in self.queue.items if i.path == file]
        return found


def films(rule_id: int, folder: Path, **changes: object) -> Rule:
    return Rule(id=rule_id, folder=str(folder), take="films", mode="auto").model_copy(
        update=changes
    )


class RulesLookTest(RulesTestCase):
    async def test_4k_films_and_other_films_go_to_their_own_folders(self) -> None:
        big = RuleTarget(preset="Movie (Folder)", to="folder", folder=str(self.big))
        self.set_rules(films(1, self.dl, quality="4k", films=big), films(2, self.dl))
        self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        self.folders.download("Arrival.2016.1080p.mkv")
        clock = Clock()
        await self.two_looks(self.renamer(clock), clock)
        assert (self.big / "Dune (2021)" / "Dune (2021).mkv").exists()
        assert (self.folders.movies / "Arrival (2016)" / "Arrival (2016).mkv").exists()
        runs = await self.history.list_runs()
        assert len(runs) == 2  # one run per rule

    async def test_a_manual_rule_puts_files_in_the_queue_only(self) -> None:
        self.set_rules(films(3, self.dl, mode="manual"))
        file = self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        clock = Clock()
        await self.two_looks(self.renamer(clock), clock)
        assert file.exists()
        item = self.item(file)
        assert item.status is ItemStatus.READY and item.watched == self.dl
        assert item.rule is not None and item.rule.id == 3 and not item.manual

    async def test_a_file_no_rule_fits_is_left_alone(self) -> None:
        self.set_rules(Rule(id=1, folder=str(self.dl), take="shows", mode="auto"))
        file = self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        clock = Clock()
        auto = self.renamer(clock)
        await self.two_looks(auto, clock)
        assert self.queue.items == [] and file.exists()
        assert auto.unfit() == [Unfit(self.dl, (file,))]
        clock.now += 300
        await auto.look()  # still there, still counted (its name read once)
        assert auto.unfit() == [Unfit(self.dl, (file,))]

    async def test_a_new_automatic_rule_queues_whats_there_as_manual_once(self) -> None:
        self.set_rules(films(1, self.dl, fresh=True))
        there = self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        clock = Clock()
        auto = self.renamer(clock)
        await self.two_looks(auto, clock)
        assert there.exists()
        item = self.item(there)
        assert item.status is ItemStatus.READY and item.manual
        assert self.rules.done == [[1]]
        self.folders.download("Arrival.2016.1080p.mkv")  # arrives after its first look
        clock.now += 300
        await self.two_looks(auto, clock)
        assert (self.folders.movies / "Arrival (2016)" / "Arrival (2016).mkv").exists()
        assert there.exists() and self.rules.done == [[1]]

    async def test_unpausing_queues_what_came_in_while_paused(self) -> None:
        self.set_rules(films(1, self.dl))
        await self.settings.update(automatic="off")
        clock = Clock()
        auto = self.renamer(clock)
        auto.reconfigure()  # paused
        file = self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        await self.settings.update(automatic="on")
        with mock.patch.object(auto, "start"):  # the looks are this test's own
            auto.reconfigure()  # watching again
        await self.two_looks(auto, clock)
        assert file.exists()
        item = self.item(file)
        assert item.status is ItemStatus.READY and item.manual

    async def test_a_changed_rule_holds_its_files_as_manual(self) -> None:
        self.set_rules(films(1, self.dl))
        file = self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        self.tmdb.down = True  # not sure yet: it waits
        clock = Clock()
        auto = self.renamer(clock)
        await self.two_looks(auto, clock)
        item = self.item(file)
        assert item.status is ItemStatus.HELD and not item.rule_changed
        big = RuleTarget(preset="Movie (Folder)", to="folder", folder=str(self.big))
        self.set_rules(films(1, self.dl, films=big))  # edited: it goes elsewhere now
        clock.now += 300
        await auto.look()
        assert item.rule_changed
        # Stored: after a restart it still waits for the owner (as Manual once).
        assert self.settings.current.manual_once == [os.path.normcase(str(file))]
        self.tmdb.down = False  # sure now
        clock.now += 300
        await auto.look()
        assert item.status is ItemStatus.READY and file.exists()

    async def test_a_gone_preset_acts_manual(self) -> None:
        self.set_rules(films(1, self.dl, films=RuleTarget(preset="Gone")))
        file = self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        clock = Clock()
        await self.two_looks(self.renamer(clock), clock)
        assert file.exists()
        assert self.item(file).status is ItemStatus.READY
        assert not any(self.folders.movies.iterdir())  # never another preset

    async def test_a_missing_library_folder_holds_only_its_rules(self) -> None:
        await self.settings.update(movies_folder="")
        big = RuleTarget(preset="Movie (Folder)", to="folder", folder=str(self.big))
        self.set_rules(films(1, self.dl, quality="4k", films=big), films(2, self.dl))
        self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        waits = self.folders.download("Arrival.2016.1080p.mkv")
        clock = Clock()
        await self.two_looks(self.renamer(clock), clock)
        assert (self.big / "Dune (2021)" / "Dune (2021).mkv").exists()
        item = self.item(waits)
        assert item.status is ItemStatus.HELD
        assert item.message == "Choose the Movies library folder."

    async def test_a_stays_rule_sorts_and_writes_a_move_record(self) -> None:
        self.set_rules(Rule(id=1, folder=str(self.dl), take="shows", stays=True, mode="auto"))
        self.folders.download(SHOW)
        clock = Clock()
        await self.two_looks(self.renamer(clock), clock)
        assert (self.dl / SORTED).exists()
        [move] = await self.moves.moves_from(self.dl)
        assert move.target == self.dl / SORTED and move.tmdb_id == 7

    async def test_a_nested_stays_rule_sorts_inside_its_own_folder(self) -> None:
        courses = self.dl / "Courses"
        stays = Rule(id=1, folder=str(courses), take="shows", stays=True, mode="auto")
        self.set_rules(stays, Rule(id=2, folder=str(self.dl), mode="auto"))
        put(courses, SHOW)
        clock = Clock()
        await self.two_looks(self.renamer(clock), clock)
        [done] = self.queue.items
        assert done.watched == courses  # the rule's own folder, not the listed one
        assert (courses / SORTED).exists() and not (self.dl / SORTED).exists()

    async def test_sorted_output_is_never_taken_again_after_a_restart(self) -> None:
        top = Rule(id=1, folder=str(self.dl), subfolders=False, stays=True, mode="auto")
        deep = Rule(id=2, folder=str(self.dl), subfolders=True, stays=True, mode="auto")
        self.set_rules(top, deep)
        file = put(self.dl, SHOW)
        clock = Clock()
        await self.two_looks(self.renamer(clock), clock)
        assert (self.dl / SORTED).exists() and not file.exists()
        [done] = self.queue.items
        assert done.run_id is not None
        run_id = done.run_id
        self.queue.clear()
        again = self.renamer(clock)  # as after a restart: the move records remember
        clock.now += 300
        await self.two_looks(again, clock)
        assert self.queue.items == []
        await self.undo.undo_run(run_id)
        assert file.exists()
        clock.now += 300
        await self.two_looks(again, clock)
        [held] = self.queue.items
        assert held.path == file and held.status is ItemStatus.HELD
        assert held.message == t(UNDONE)

    async def test_one_listing_per_folder_and_an_asleep_share_stops_only_its_rules(self) -> None:
        share = self.tmp / "Share"
        share.mkdir()
        big = RuleTarget(preset="Movie (Folder)", to="folder", folder=str(self.big))
        self.set_rules(
            Rule(id=1, folder=str(self.dl), take="shows", mode="auto"),
            films(2, self.dl, quality="4k", films=big),
            films(3, self.dl),
            films(4, share),
        )
        self.folders.download(SHOW)
        self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        put(share, "Heat.1995.1080p.mkv")
        free = threading.Event()
        self.addCleanup(free.set)  # the abandoned thread ends with the test
        calls: list[Path] = []
        real = watch.candidates

        def counted(root: Path, ignored: object = None, prune: object = ()) -> list[Path]:
            calls.append(root)
            if root == share:
                free.wait(10)  # asleep: takes the connection and says nothing
            return real(root, None)

        clock = Clock()
        auto = AutoRenamer(self.parts, now=clock, step_timeout_s=0.2)
        self.addAsyncCleanup(auto.stop)
        statuses: list[str] = []
        auto.on_status = lambda status: statuses.append(status.state)
        with mock.patch.object(auto_module, "candidates", counted):
            await self.two_looks(auto, clock)
        assert calls.count(self.dl) == 2  # one listing per look for its three rules
        assert calls.count(share) == 1  # the second look skips it while its thread hangs
        assert (self.folders.series / "VisionQuest (2025)").exists()
        assert (self.big / "Dune (2021)" / "Dune (2021).mkv").exists()
        assert statuses[-1] == "unreachable"
        assert auto.unreachable_folders() == [str(share)]

    async def test_a_destination_is_never_watched(self) -> None:
        other = self.tmp / "Other"
        into = self.dl / "Sorted"
        into.mkdir()
        to_dl = RuleTarget(preset="Movie (Folder)", to="folder", folder=str(into))
        self.set_rules(Rule(id=1, folder=str(self.dl), mode="auto"), films(2, other, films=to_dl))
        put(other, "Dune.2021.2160p.WEB-DL.mkv")
        clock = Clock()
        auto = self.renamer(clock)
        await self.two_looks(auto, clock)
        moved = into / "Dune (2021)" / "Dune (2021).mkv"
        assert moved.exists()
        clock.now += 300
        await self.two_looks(auto, clock)
        [done] = self.queue.items
        assert done.status is ItemStatus.DONE and moved.exists()

    async def test_status_counts_folders_and_says_manual(self) -> None:
        share = self.tmp / "Share"
        share.mkdir()
        self.set_rules(films(1, self.dl, mode="manual"), films(2, share, mode="manual"))
        auto = self.renamer(Clock())
        assert auto.status([]) == ("trial", "Manual: watching 2 folders")
        self.set_rules(films(1, self.dl, mode="manual"), films(2, share))
        assert auto.status([]) == ("watching", "Watching 2 folders")
        self.set_rules()
        assert auto.status([]) == ("setup", "Set up watching in Settings")

    async def test_a_look_logs_no_names(self) -> None:
        courses = self.tmp / "Courses"
        self.set_rules(films(1, courses, name="4K films", quality="4k"), films(2, self.dl))
        put(courses / "Dune.2021.2160p.WEB-DL", "Dune.2021.2160p.WEB-DL.mkv")
        put(self.dl, "Dune.Part.Two.2024.1080p.mkv")
        clock = Clock()
        auto = self.renamer(clock)
        with self.assertLogs("app.forge.core.auto", "DEBUG") as logs:
            await self.two_looks(auto, clock)
        names = ("Dune", "Courses", "4K films", str(self.tmp))
        assert not any(n in line for line in logs.output for n in names)
        assert any("Look:" in line for line in logs.output)


class AsleepShare:
    """A share that takes the connection and says nothing until `wake()`; `candidates` patched."""

    def __init__(self, test: RulesTestCase) -> None:
        self.folder = test.tmp / "Share"
        self.folder.mkdir()
        self.free = threading.Event()
        test.addCleanup(self.free.set)  # the abandoned thread ends with the test
        real = watch.candidates

        def candidates(root: Path, ignored: object = None, prune: object = ()) -> list[Path]:
            if root == self.folder:
                self.free.wait(10)
            return real(root, None)

        test.enterContext(mock.patch.object(auto_module, "candidates", candidates))

    def wake(self) -> None:
        self.free.set()


class IntakeTest(RulesTestCase):
    """Nothing moves unseen, per rule and per folder: a folder away keeps only its own first look
    pending, and what waits as Manual once is stored (`ForgeSettings.manual_once`)."""

    def renamer(self, clock: Clock, notes: list[tuple[str, str]] | None = None) -> AutoRenamer:
        auto = AutoRenamer(self.parts, now=clock, step_timeout_s=0.2)
        self.addAsyncCleanup(auto.stop)
        return auto

    async def until_reached(self, auto: AutoRenamer, clock: Clock) -> None:
        for _ in range(100):
            clock.now += 300
            if not (await auto.look()).unreachable:
                return
            await asyncio.sleep(0.02)
        raise AssertionError("the share never answered")

    async def test_a_fresh_rule_with_a_folder_away_takes_in_the_one_reached(self) -> None:
        share = AsleepShare(self)
        self.set_rules(films(1, self.dl, fresh=True))

        def both_folders(rules: Sequence[Rule], settings: object) -> list[WatchedRule]:
            rule = rules[0]  # e.g. AirGrabber's rule: a folder here and one on the NAS
            return [WatchedRule(rule, 1, self.dl), WatchedRule(rule, 1, share.folder)]

        self.enterContext(mock.patch.object(auto_module, "watch_list", both_folders))
        there = put(self.dl, "Arrival.2016.1080p.mkv")  # there before the rule's first look
        on_share = put(share.folder, "Heat.1995.1080p.mkv")
        clock = Clock()
        auto = self.renamer(clock)
        await self.two_looks(auto, clock)
        assert there.exists() and self.item(there).manual
        assert self.rules.done == []  # its share's first look is still to come
        later = put(self.dl, "Dune.2021.1080p.mkv")  # the folder reached works as usual
        clock.now += 300
        await self.two_looks(auto, clock)
        assert not later.exists() and there.exists()
        share.wake()
        await self.until_reached(auto, clock)
        clock.now += 300
        await auto.look()
        assert self.rules.done == [[1]]
        assert on_share.exists() and self.item(on_share).manual

    async def test_unpausing_while_a_share_sleeps_holds_only_that_share(self) -> None:
        share = AsleepShare(self)
        self.set_rules(films(1, self.dl), films(2, share.folder))
        await self.settings.update(automatic="off")
        clock = Clock()
        auto = self.renamer(clock)
        auto.reconfigure()
        on_share = put(share.folder, "Heat.1995.1080p.mkv")  # came in while paused
        await self.settings.update(automatic="on")
        with mock.patch.object(auto, "start"):  # the looks are this test's own
            auto.reconfigure()
        await self.two_looks(auto, clock)
        later = put(self.dl, "Arrival.2016.1080p.mkv")  # after the un-pause's look
        clock.now += 300
        await self.two_looks(auto, clock)
        assert not later.exists()  # moved: Downloads' rule works again
        share.wake()
        await self.until_reached(auto, clock)
        clock.now += 300
        await auto.look()
        assert on_share.exists() and self.item(on_share).manual

    async def test_what_waits_as_manual_once_still_waits_after_a_restart(self) -> None:
        self.set_rules(films(1, self.dl, fresh=True))
        there = self.folders.download("Arrival.2016.1080p.mkv")
        clock = Clock()
        await self.two_looks(self.renamer(clock), clock)
        assert self.item(there).manual
        assert self.settings.current.manual_once == [os.path.normcase(str(there))]
        self.queue.clear()
        again = self.renamer(clock)  # as after a restart: the rule's flag has gone
        clock.now += 300
        await self.two_looks(again, clock)
        assert there.exists() and self.item(there).manual
        there.unlink()  # taken away by hand
        clock.now += 300
        await again.look()
        assert self.settings.current.manual_once == []

    async def test_a_stays_rule_never_takes_a_file_already_named_so(self) -> None:
        self.set_rules(Rule(id=1, folder=str(self.dl), take="shows", stays=True, mode="auto"))
        done = put((self.dl / SORTED).parent, SORTED.name)  # sorted long ago: no move record
        clock = Clock()
        auto = self.renamer(clock)
        await self.two_looks(auto, clock)
        clock.now += 300
        await auto.look()
        assert self.queue.items == [] and done.exists()
        assert await self.history.list_runs() == []

    async def test_an_unreachable_destination_holds_and_retries(self) -> None:
        big = RuleTarget(preset="Movie (Folder)", to="folder", folder=str(self.big))
        self.set_rules(films(1, self.dl, films=big))
        file = self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        self.big.rmdir()  # the NAS share is away
        clock = Clock()
        auto = self.renamer(clock)
        await self.two_looks(auto, clock)
        item = self.item(file)
        assert item.status is ItemStatus.HELD
        assert item.message == t("Can't reach {folder}.", folder=self.big)
        assert auto.unreachable_folders() == [str(self.big)]
        self.big.mkdir()  # back
        clock.now += 300
        await auto.look()
        assert (self.big / "Dune (2021)" / "Dune (2021).mkv").exists()
        assert auto.unreachable_folders() == []

    async def test_an_unpause_still_takes_in_a_share_after_a_restart(self) -> None:
        share = AsleepShare(self)
        self.set_rules(films(1, self.dl), films(2, share.folder))
        await self.settings.update(automatic="off")
        clock = Clock()
        auto = self.renamer(clock)
        auto.reconfigure()
        on_share = put(share.folder, "Heat.1995.1080p.mkv")  # came in while paused
        await self.settings.update(automatic="on")
        with mock.patch.object(auto, "start"):  # the looks are this test's own
            auto.reconfigure()
        await self.two_looks(auto, clock)
        assert sorted(self.rules.marked) == [1, 2]  # stored, not only remembered
        assert self.rules.done == [[1]]  # the share's first look is still to come
        share.wake()
        await asyncio.sleep(0.3)  # its hanging thread ends
        self.queue.clear()
        again = self.renamer(clock)  # a restart, the share awake now
        clock.now += 300
        await self.two_looks(again, clock)
        assert on_share.exists() and self.item(on_share).manual
        assert self.rules.done == [[1], [2]]

    async def test_a_first_look_never_remembers_a_destinations_files(self) -> None:
        other = self.tmp / "Other"
        into = self.dl / "Sorted"
        moved = put(into / "Dune (2021)", "Dune (2021).mkv")  # moved there by rule 2 before
        to_dl = RuleTarget(preset="Movie (Folder)", to="folder", folder=str(into))
        self.set_rules(
            Rule(id=1, folder=str(self.dl), mode="auto", fresh=True), films(2, other, films=to_dl)
        )
        there = put(self.dl, "Arrival.2016.1080p.mkv")
        other.mkdir()
        clock = Clock()
        await self.two_looks(self.renamer(clock), clock)
        assert self.settings.current.manual_once == [os.path.normcase(str(there))]
        assert moved.exists()

    async def test_a_waiting_file_its_changed_rule_already_sorted_leaves_the_queue(self) -> None:
        self.set_rules(films(1, self.dl, mode="manual"))
        done = put(self.dl / "Dune (2021)", "Dune (2021).mkv")
        clock = Clock()
        auto = self.renamer(clock)
        await self.two_looks(auto, clock)
        assert self.item(done).status is ItemStatus.READY  # its rule moves it: it waits
        self.set_rules(films(1, self.dl, mode="manual", stays=True))  # now: sorted where it is
        clock.now += 300
        await auto.look()
        assert self.queue.items == [] and done.exists()


class HoldLooksTest(RulesTestCase):
    """`hold_looks` and the rules' generation: no look commits between the steps of a held block
    (AirGrabber's folders stored and its rule marked fresh); nothing waits for a look's listing,
    TMDB or moves; a look that read the rules before a change commits nothing of it."""

    async def test_a_look_waits_for_a_held_block_and_a_block_for_a_look(self) -> None:
        self.set_rules(films(1, self.dl))
        auto = self.renamer(Clock())
        async with auto.hold_looks():
            look = asyncio.get_running_loop().create_task(auto.look())
            await asyncio.sleep(0.05)
            self.assertFalse(look.done())  # held off
        await asyncio.wait_for(look, 5)


    async def test_a_block_during_a_listing_goes_on_and_the_look_commits_nothing(self) -> None:
        self.set_rules(films(1, self.dl))
        file = self.folders.download("Dune.2021.2160p.WEB-DL.mkv")
        clock = Clock()
        auto = self.renamer(clock)
        await auto.look()  # first sight
        gate, listing = asyncio.Event(), asyncio.Event()
        real = auto._look_in

        async def slow(root: Path, ignored: object, *args: object) -> object:
            listing.set()
            await gate.wait()
            return await real(root, ignored, *args)  # type: ignore[arg-type]

        clock.now += 300
        with mock.patch.object(auto, "_look_in", slow):
            look = asyncio.get_running_loop().create_task(auto.look())
            await asyncio.wait_for(listing.wait(), 5)
            async with asyncio.timeout(1):  # never waits for the listing
                async with auto.hold_looks():
                    await self.rules.mark_fresh(1)  # e.g. AirGrabber's new folder
            gate.set()
            await asyncio.wait_for(look, 5)
        # It read the rules before the mark: it took nothing in, moved nothing, looks again.
        assert self.queue.items == [] and file.exists() and auto._redo
        clock.now += 300
        await auto.look()
        assert file.exists() and self.item(file).manual  # the fresh rule's first look

    async def test_a_block_never_waits_for_a_looks_moves(self) -> None:
        # The moves (a copy to a NAS can take minutes) come after the look's taking in, without
        # `_look_lock`: Settings' Save never hangs on them.
        self.set_rules(films(1, self.dl))
        auto = self.renamer(Clock())
        gate, moving = asyncio.Event(), asyncio.Event()

        async def slow_moves(*args: object, **kwargs: object) -> int:
            moving.set()
            await gate.wait()
            return 0

        with mock.patch.object(auto, "_move_sure", slow_moves):
            look = asyncio.get_running_loop().create_task(auto.look())
            await asyncio.wait_for(moving.wait(), 5)
            async with asyncio.timeout(5):
                async with auto.hold_looks():
                    self.assertFalse(look.done())  # still moving, the block got in
            gate.set()
            await asyncio.wait_for(look, 5)


class CarriedRulesTest(RulesTestCase):
    """The one-time carry-over stores every rule fresh: its first look queues what is already
    there as Manual once (3.19 never watched some of it), what comes after goes as its mode says."""

    async def test_the_first_start_moves_nothing_already_there(self) -> None:
        inner = self.dl / "Complete"
        inner.mkdir()
        old = put(self.dl, "Heat.1995.1080p.mkv")  # in AirGrabber's folder: unwatched in 3.19
        mine = put(inner, "Arrival.2016.1080p.mkv")  # in the owner's own watched folder
        await self.settings.update(
            watch_folders=[str(inner)],
            use_airgrabber_downloads=True,
            airgrabber=AirGrabberDownloads(
                folders=[AirGrabberFolder(path=str(self.dl), why="finished")]
            ),
            automatic="on",
        )
        rules = RuleService(RuleRepo(self.db), self.settings)
        await rules.load()  # the first start of the rules version
        self.assertEqual([r.mode for r in rules.current], ["auto", "auto"])
        self.assertTrue(all(r.fresh for r in rules.current))
        self.parts = dataclasses.replace(self.parts, rules=rules)
        clock = Clock()
        auto = self.renamer(clock)
        await self.two_looks(auto, clock)
        assert old.exists() and mine.exists()
        assert self.item(old).manual and self.item(mine).manual
        assert not any(r.fresh for r in rules.current)  # their first looks are done
        later = put(self.dl, "Dune.2021.1080p.mkv")  # arrives after the first look
        clock.now += 300
        await self.two_looks(auto, clock)
        assert not later.exists()
        assert (self.folders.movies / "Dune (2021)" / "Dune (2021).mkv").exists()
        assert old.exists() and mine.exists()  # still waiting for the owner


class DestinationGoneTest(RulesTestCase):
    """A destination that stops being one (its rule deleted, switched off or re-pointed, a
    library folder changed) exposes its files to a rule watching a folder above it: Settings'
    Save (`RuleService.save`) marks that rule fresh, so they wait once instead of moving."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.service = RuleService(RuleRepo(self.db), self.settings)
        await self.service.load()
        self.parts = dataclasses.replace(self.parts, rules=self.service)
        self.other = self.tmp / "Other"
        self.other.mkdir()
        self.sorted_ = self.dl / "Sorted"
        to_sorted = RuleTarget(preset="Movie (Folder)", to="folder", folder=str(self.sorted_))
        saved = await self.service.replace_all(
            [
                Rule(folder=str(self.dl), take="films", mode="auto"),
                Rule(folder=str(self.other), take="films", mode="auto", films=to_sorted),
            ]
        )
        await self.service.first_look_done([rule.id for rule in saved])

    def fresh(self, rule_id: int) -> bool:
        rule = self.service.get(rule_id)
        assert rule is not None
        return rule.fresh

    async def test_a_deleted_rules_destination_waits_once(self) -> None:
        mine = put(self.sorted_, "Heat.1995.1080p.mkv")  # the owner's own, never forged
        clock = Clock()
        auto = self.renamer(clock)
        await self.two_looks(auto, clock)
        assert mine.exists() and self.queue.items == []  # a destination: never looked at
        watching, _gone, ag = self.service.current
        await self.service.save([watching, ag], {})  # rule 2 deleted
        assert self.fresh(watching.id)
        clock.now += 300
        await self.two_looks(auto, clock)
        assert mine.exists() and self.item(mine).manual  # waits for the owner
        assert list(self.folders.movies.rglob("*.mkv")) == []

    async def test_a_switched_off_rule_and_a_changed_library_folder_mark_too(self) -> None:
        watching, sorter, ag = self.service.current
        await self.service.save([watching, sorter.model_copy(update={"on": False}), ag], {})
        assert self.fresh(watching.id)
        # A shows rule on the downloads, the Movies folder inside them, then chosen elsewhere.
        await self.settings.update(movies_folder=str(self.dl / "Films"))
        shows = Rule(folder=str(self.dl), take="shows", mode="auto")
        saved = await self.service.replace_all([shows, ag])
        await self.service.first_look_done([r.id for r in saved])
        await self.service.save(None, {"movies_folder": str(self.folders.movies)})
        assert self.fresh(saved[0].id)
        assert self.settings.current.movies_folder == str(self.folders.movies)
        await self.service.first_look_done([saved[0].id])
        await self.service.save(None, {"notify_needs": False})  # nothing exposed: no mark
        assert not self.fresh(saved[0].id)

    async def test_a_watched_folder_inside_a_gone_destination_waits_once(self) -> None:
        # Rule 1 watches Downloads\Incoming while rule 2 moves films to Downloads: idle (a look
        # skips a destination's files). Rule 2 deleted: its whole folder is exposed at once.
        incoming = self.dl / "Incoming"
        to_dl = RuleTarget(preset="Movie (Folder)", to="folder", folder=str(self.dl))
        saved = await self.service.replace_all(
            [
                Rule(folder=str(incoming), take="films", mode="auto"),
                Rule(folder=str(self.other), take="films", mode="auto", films=to_dl),
            ]
        )
        await self.service.first_look_done([rule.id for rule in saved])
        mine = put(incoming, "Heat.1995.1080p.mkv")
        clock = Clock()
        auto = self.renamer(clock)
        await self.two_looks(auto, clock)
        assert mine.exists() and self.queue.items == []
        watching, _gone, ag = self.service.current
        await self.service.save([watching, ag], {})
        assert self.fresh(watching.id)
        clock.now += 300
        await self.two_looks(auto, clock)
        assert mine.exists() and self.item(mine).manual
        assert list(self.folders.movies.rglob("*.mkv")) == []


class SaveDuringCommitTest(RulesTestCase):
    """A Save that comes while a look commits its take-in waits for that commit (milliseconds:
    `hold_looks`), so the look's first look of the old rules never clears the Save's fresh
    mark; and a first look's clearing never undoes a change made since the look read the rules."""

    async def test_a_save_during_the_take_in_keeps_its_fresh_mark(self) -> None:
        service = RuleService(RuleRepo(self.db), self.settings)
        await service.load()
        self.parts = dataclasses.replace(self.parts, rules=service)
        f1, f2 = self.tmp / "F1", self.tmp / "F2"
        rule, ag = await service.replace_all([Rule(folder=str(f1), take="films", mode="auto")])
        assert rule.fresh
        put(f1, "Arrival.2016.1080p.mkv")
        there = put(f2, "Heat.1995.1080p.mkv")  # already in the folder the rule watches next
        clock = Clock()
        auto = self.renamer(clock)
        service.hold_looks_with(auto.hold_looks)  # as ForgeServices wires it
        real = self.settings.update
        saving: list[asyncio.Task[object]] = []

        async def update(**changes: object) -> object:
            if "manual_once" in changes and not saving:  # the look's commit A is under way
                moved = rule.model_copy(update={"folder": str(f2)})
                saving.append(asyncio.get_running_loop().create_task(service.save([moved, ag], {})))
                await asyncio.sleep(0.05)  # the Save's turn comes now: it waits for the commit
            return await real(**changes)

        with mock.patch.object(self.settings, "update", update):
            await self.two_looks(auto, clock)
            await asyncio.wait_for(saving[0], 5)
        moved_rule = service.get(rule.id)
        assert moved_rule is not None and moved_rule.folder == str(f2)
        for _ in range(2):
            clock.now += 300
            await self.two_looks(auto, clock)
        assert there.exists() and self.item(there).manual  # the new folder's first look
        assert list(self.folders.movies.rglob("*.mkv")) == []

    async def test_a_first_look_clears_nothing_once_the_rules_changed(self) -> None:
        service = RuleService(RuleRepo(self.db), self.settings)
        await service.load()
        [rule, _ag] = await service.replace_all([Rule(folder=str(self.dl), mode="auto")])
        noted = service.generation
        await service.mark_fresh(rule.id)  # e.g. an un-pause while the look read the rules
        await service.first_look_done([rule.id], generation=noted)
        stored = service.get(rule.id)
        assert stored is not None and stored.fresh
        await service.first_look_done([rule.id], generation=service.generation)
        stored = service.get(rule.id)
        assert stored is not None and not stored.fresh


class SkippedBeforeCheckTest(RulesTestCase):
    """A look never stats or opens what it would never take: a destination's files (its folder
    isn't walked) and a Stays rule's past outputs, so a sorted collection costs it nothing."""

    async def test_destinations_and_outputs_never_reach_the_finished_check(self) -> None:
        courses = self.tmp / "Courses"
        sorted_ = self.dl / "Sorted"
        to_sorted = RuleTarget(preset="Movie (Folder)", to="folder", folder=str(sorted_))
        other = self.tmp / "Other"
        other.mkdir()
        self.set_rules(
            films(1, self.dl),
            films(2, other, films=to_sorted),
            Rule(id=3, folder=str(courses), take="shows", stays=True, mode="auto"),
        )
        collection = [put(sorted_ / f"Film {n}", f"Film.{n}.1990.1080p.mkv") for n in range(5)]
        put(courses, SHOW)
        checked: list[Path] = []
        clock = Clock()

        class Counting(watch.Watcher):
            def finished(self, files: Sequence[Path]) -> list[Path]:
                checked.extend(files)
                return super().finished(files)

        auto = AutoRenamer(self.parts, now=clock, new_watcher=lambda: Counting(now=clock))
        self.addAsyncCleanup(auto.stop)
        await self.two_looks(auto, clock)
        assert (courses / SORTED).exists()  # sorted where it is: a move record
        assert not set(checked) & set(collection)
        checked.clear()
        for _ in range(2):
            clock.now += 300
            await auto.look()
        assert checked == []  # nothing else there: no file stat'ed or opened
        assert self.queue.items[0].status in (ItemStatus.DONE, ItemStatus.SKIPPED)

class SaveNeverWaitsTest(RulesTestCase):
    """Settings' Save (`RuleService.save`) never waits for a look, not even one held up by a NAS
    asleep; the look commits nothing it read before the Save and looks again."""

    async def test_save_returns_at_once_while_a_look_sits_in_a_slow_folder(self) -> None:
        service = RuleService(RuleRepo(self.db), self.settings)
        await service.load()
        self.parts = dataclasses.replace(self.parts, rules=service)
        share = self.tmp / "Share"
        share.mkdir()
        saved = await service.replace_all([Rule(folder=str(share), take="films", mode="auto")])
        await service.first_look_done([rule.id for rule in saved])
        put(share, "Heat.1995.1080p.mkv")
        free = threading.Event()
        self.addCleanup(free.set)
        real = watch.candidates

        def slow(root: Path, *args: object) -> list[Path]:
            if root == share:
                free.wait(10)  # asleep: takes the connection and says nothing
            return real(root, *args)  # type: ignore[arg-type]

        clock = Clock()
        auto = AutoRenamer(self.parts, now=clock, step_timeout_s=3)
        self.addAsyncCleanup(auto.stop)
        with mock.patch.object(auto_module, "candidates", slow):
            look = asyncio.get_running_loop().create_task(auto.look())
            await asyncio.sleep(0.3)  # the look sits in the share's listing
            async with asyncio.timeout(1):
                await service.save(None, {"notify_needs": False})
            self.assertFalse(look.done())
            free.set()
            await asyncio.wait_for(look, 10)
        assert self.queue.items == [] and auto._redo  # read before the Save: commits nothing
        assert self.settings.current.notify_needs is False
