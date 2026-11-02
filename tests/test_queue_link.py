"""The download queue as the buttons and the Downloads page see it (3.0.1): QueueLink mirrors
the queue's events into DownloadStates ("Queued", working, the outcome) and reports the
outcomes as before; a batch's progress; a season pack's fallback; a row closed mid-run (its
window closed) doesn't stop its download, and one opened later shows where it is; the
Downloads page's "Waiting (n)" group with its ✕; My shows' "Download missing" as one batch in
the queue (its run state, report and status line). Fakes only (tests/test_download_queue.py's
FakeQuick); a hidden Tk root for the widgets."""

from __future__ import annotations

import asyncio
import gc
import tkinter as tk
import unittest
from types import SimpleNamespace
from typing import Any

import customtkinter as ctk

from app.models import (
    CalendarEntry,
    Episode,
    GrabOutcome,
    MissingRunReport,
    QueueEvent,
    QueueState,
    QuickDownloadResult,
)
from app.services.download_queue import DownloadQueue
from app.services.downloads import GrabError
from app.ui.download_control import (
    QUEUED_TEXT,
    Base,
    BatchState,
    DownloadActions,
    DownloadStates,
    Phase,
    RunState,
    control_look,
    episode_key,
)
from app.services.search import TorrentSearchService
from app.ui.cards import run_line
from app.ui.main_window import MainWindow
from app.ui.missing_control import MissingRuns, MissingRunState, queued_run_report
from app.ui.queue_link import NO_PACK_TEXT, QueueLink
from app.ui.row_actions import RowActions, row_look
from app.ui.views.downloads_view import DownloadsView
from tests.test_download_queue import GOLD_RUSH, GRABBED, NOTHING, FakeQuick, item, key, pack
from tests.test_downloads_view import FakeImages, FakeSettings, FakeStatus
from tests.test_search import settings_service


def entry(number: int) -> CalendarEntry:
    episode = Episode(id=1700 + number, show_id=GOLD_RUSH.id, season=17, number=number)
    return CalendarEntry(show=GOLD_RUSH, episode=episode, is_tracked=True)


class LinkTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.quick = FakeQuick()
        self.queue = DownloadQueue(self.quick, workers=1)  # type: ignore[arg-type]
        self.addCleanup(self.queue.close)
        self.states = DownloadStates()
        self.reports: list[str] = []
        self.link = QueueLink(self.queue, self.states, report=self.reports.append)
        self.addCleanup(self.link.close)

    async def until(self, condition: Any, timeout_s: float = 2.0) -> None:
        async with asyncio.timeout(timeout_s):
            while not condition():
                await asyncio.sleep(0.005)

    async def idle(self) -> None:
        await self.until(lambda: not self.queue.running and not self.queue.waiting)

    def phase(self, number: int | None) -> Phase | None:
        state = self.states.get(key(17, number))
        return state.phase if state is not None else None


class LinkTests(LinkTestCase):
    async def test_states_follow_the_queue_and_outcomes_are_reported(self) -> None:
        self.quick.gate(key(17, 1))
        self.quick.results[key(17, 2)] = NOTHING
        self.quick.results[key(17, 3)] = QuickDownloadResult(found=False, already_downloaded=True)
        for number in (1, 2, 3):
            self.link.download(item(number))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.assertEqual(self.phase(1), Phase.WORKING)
        self.assertEqual(self.phase(2), Phase.QUEUED)
        self.assertEqual(self.phase(3), Phase.QUEUED)
        self.link.download(item(2))  # clicked again while it waits: nothing changes
        self.assertEqual(len(self.queue.waiting), 2)
        self.quick.open(key(17, 1))
        await self.idle()
        done = self.states.get(key(17, 1))
        assert done is not None
        self.assertEqual(
            (done.phase, done.outcome, done.quality), (Phase.DONE, GrabOutcome.SENT, "1080p")
        )
        self.assertEqual(self.phase(2), Phase.NOTHING)
        self.assertEqual(self.phase(3), Phase.DONE)
        self.assertEqual(
            self.reports,
            [
                "Gold Rush S17E01 · 1080p · 40 seeders · Sent to qBittorrent",
                "No good download found for Gold Rush S17E02. Choose one yourself.",
                "Gold Rush S17E03 was already downloaded",
            ],
        )

    async def test_a_failed_grab_goes_back_to_download_with_the_reason(self) -> None:
        self.quick.errors[key(17, 1)] = GrabError("No torrent program is set up on this PC.")
        self.quick.gate(key(17, 1))
        stack = (key(17, 1), key(17, 2))
        self.link.download_batch(stack, [item(1), item(2)])
        await asyncio.sleep(0)
        with self.assertLogs("app.services.download_queue", "WARNING"):
            self.quick.open(key(17, 1))
            await self.idle()
        self.assertIsNone(self.states.get(key(17, 1)))
        self.assertIsNone(self.states.get(key(17, 2)))  # dropped with its batch
        self.assertEqual(self.reports, ["No torrent program is set up on this PC."])
        self.assertIsNone(self.states.batch(stack))

    async def test_a_batch_shows_queued_then_its_progress(self) -> None:
        batch = (key(17, 1), key(17, 2), key(17, 3))
        self.quick.gate(key(17, 9), *batch)
        self.link.download(item(9))  # something else goes first
        self.link.download_batch(batch, [item(1), item(2), item(3)])
        self.link.download_batch(batch, [item(1), item(2), item(3)])  # clicked twice
        await asyncio.sleep(0)
        self.assertEqual(self.states.batch(batch), BatchState(1, 3, queued=True))
        self.assertEqual(len(self.queue.waiting), 3)
        self.quick.open(key(17, 9))
        await self.until(lambda: self.phase(1) is Phase.WORKING)
        self.assertEqual(self.states.batch(batch), BatchState(1, 3))
        self.quick.open(key(17, 1))
        await self.until(lambda: self.phase(2) is Phase.WORKING)
        self.assertEqual(self.states.batch(batch), BatchState(2, 3))
        self.assertEqual(self.states.batch_for([key(17, 3)]), BatchState(2, 3))
        self.quick.open(key(17, 2))
        self.quick.open(key(17, 3))
        await self.idle()
        self.assertIsNone(self.states.batch(batch))

    async def test_no_season_pack_queues_the_episodes_and_says_so(self) -> None:
        self.quick.results[key(17, None)] = NOTHING
        self.quick.gate(key(17, 2))
        self.link.download(pack(fallback=(item(2), item(3))))
        await self.until(lambda: self.phase(2) is Phase.WORKING)
        self.assertIsNone(self.states.get(key(17, None)))  # follows its episodes now
        self.assertEqual(self.phase(3), Phase.QUEUED)
        self.assertEqual(self.states.batch_for([key(17, 2)]), BatchState(1, 2))
        self.assertEqual(self.reports, [NO_PACK_TEXT])
        self.assertEqual(self.reports[0], "No season pack found; the episodes are queued.")
        self.quick.open(key(17, 2))
        await self.idle()
        self.assertEqual(self.phase(2), Phase.DONE)
        self.assertEqual(self.phase(3), Phase.DONE)
        self.assertIsNone(self.states.batch_for([key(17, 2)]))

    async def test_dropped_goes_back_to_its_own_look(self) -> None:
        self.quick.gate(key(17, 1))
        self.link.download(item(1))
        self.link.download(item(2))
        await asyncio.sleep(0)
        self.assertEqual(self.phase(2), Phase.QUEUED)
        self.queue.remove(key(17, 2))
        self.assertIsNone(self.states.get(key(17, 2)))
        self.assertEqual(self.reports, [])
        self.quick.open(key(17, 1))
        await self.idle()

    async def test_closed_link_stops_listening(self) -> None:
        self.link.close()
        self.link.close()  # twice is fine
        self.link.download(item(1))
        await self.idle()
        self.assertIsNone(self.states.get(key(17, 1)))
        self.assertEqual(self.queue.state(key(17, 1)), QueueState.DONE)


class LookTests(unittest.TestCase):
    def test_a_row_waiting_in_the_queue_says_queued(self) -> None:
        look = row_look(Base("download"), RunState(Phase.QUEUED))
        self.assertEqual((look.kind, look.status, look.buttons), ("queued", ("Queued",), ()))
        card = control_look(Base("download"), RunState(Phase.QUEUED))
        self.assertEqual((card.layout, card.text, card.action), ("label", "Queued", None))
        self.assertEqual(QUEUED_TEXT, "Queued")


class TkLinkTestCase(LinkTestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        asyncio.get_running_loop().slow_callback_duration = 2.0

    async def settle(self) -> None:
        for _ in range(3):
            await asyncio.sleep(0.01)
            self.root.update()


class WindowClosedTests(TkLinkTestCase):
    def row(self, number: int, master: Any) -> RowActions[CalendarEntry]:
        actions = DownloadActions[CalendarEntry](
            download=lambda e: self.link.download(item(e.episode.number or 0)),
            choose=lambda e: None,
            states=self.states,
        )
        row = RowActions(
            master,
            item=entry(number),
            key=episode_key(GOLD_RUSH.id, 17, number),
            base=Base("download"),
            actions=actions,
        )
        row.pack(fill="x")
        return row

    async def test_closing_the_window_doesnt_cancel_and_a_new_one_shows_the_state(self) -> None:
        self.quick.gate(key(17, 1), key(17, 2))
        window = ctk.CTkFrame(self.root)  # a show window's rows (its window: test_season_downloads)
        window.pack()
        rows = [self.row(number, window) for number in (1, 2, 3)]
        for row in rows:
            row.auto.invoke()  # the wife's clicks
        await self.settle()
        kinds = [row.look.kind for row in rows if row.look]
        self.assertEqual(kinds, ["working", "queued", "queued"])
        self.assertEqual(rows[1].status.cget("text"), "Queued")
        window.destroy()  # closed mid-run
        await self.settle()
        # A window opened while they wait shows where they are.
        again = ctk.CTkFrame(self.root)
        again.pack()
        reopened = [self.row(number, again) for number in (1, 2, 3)]
        await self.settle()
        kinds = [row.look.kind for row in reopened if row.look]
        self.assertEqual(kinds, ["working", "queued", "queued"])
        again.destroy()
        self.quick.open(key(17, 1))
        self.quick.open(key(17, 2))
        await self.idle()
        self.assertEqual(self.quick.started, [key(17, 1), key(17, 2), key(17, 3)])
        later = ctk.CTkFrame(self.root)
        later.pack()
        done = [self.row(number, later) for number in (1, 2, 3)]
        await self.settle()
        self.assertEqual([row.look.kind for row in done if row.look], ["done"] * 3)
        self.assertEqual(done[0].status.cget("text"), "✓ Sent")
        self.assertEqual(len(self.reports), 3)


class WaitingGroupTests(TkLinkTestCase):
    def view(self) -> DownloadsView:
        page = DownloadsView(
            self.root,
            downloads=FakeStatus(),  # type: ignore[arg-type]
            settings=FakeSettings(),  # type: ignore[arg-type]
            images=FakeImages(),  # type: ignore[arg-type]
            discovery=None,  # type: ignore[arg-type]
            on_open_show=lambda show: None,
            queue=self.queue,
        )
        page.pack(fill="both", expand=True)
        return page

    async def test_waiting_downloads_show_at_the_top_with_a_cross(self) -> None:
        page = self.view()
        await self.settle()
        group = page.waiting
        assert group is not None
        self.assertFalse(group.winfo_manager())  # nothing waits
        self.quick.gate(key(17, 1))
        for number in (1, 2, 3, 4):
            self.link.download(item(number))
        await self.settle()
        self.assertTrue(group.winfo_ismapped())
        self.assertEqual(group.title.cget("text"), "Waiting (3)")
        labels = ("Gold Rush S17E02", "Gold Rush S17E03", "Gold Rush S17E04")
        self.assertEqual(group.labels, labels)
        self.assertEqual(int(group.grid_info()["row"]), 0)  # above the grabs
        group.drops[1].invoke()  # ✕ on S17E03
        await self.settle()
        self.assertEqual(group.title.cget("text"), "Waiting (2)")
        self.assertEqual(group.labels, ("Gold Rush S17E02", "Gold Rush S17E04"))
        self.assertEqual(self.queue.state(key(17, 3)), QueueState.DROPPED)
        self.assertIsNone(self.states.get(key(17, 3)))
        self.quick.open(key(17, 1))
        await self.idle()
        await self.settle()
        self.assertFalse(group.winfo_manager())
        self.assertNotIn(key(17, 3), self.quick.started)
        page.destroy()
        self.link.download(item(5))  # the page is gone: no error
        await self.idle()

    async def test_without_a_queue_there_is_no_group(self) -> None:
        page = DownloadsView(
            self.root,
            downloads=FakeStatus(),  # type: ignore[arg-type]
            settings=FakeSettings(),  # type: ignore[arg-type]
            images=FakeImages(),  # type: ignore[arg-type]
            discovery=None,  # type: ignore[arg-type]
            on_open_show=lambda show: None,
        )
        self.assertIsNone(page.waiting)
        page.destroy()


# -- My shows' "Download missing" ------------------------------------------------------------


class MissingRunTextTests(unittest.TestCase):
    def test_the_report_from_the_queues_events(self) -> None:
        def event(number: int, state: QueueState, **fields: Any) -> QueueEvent:
            label = f"Gold Rush S17E0{number}"
            return QueueEvent(key=key(17, number), label=label, state=state, **fields)

        already = QuickDownloadResult(found=False, already_downloaded=True)
        events = (
            event(1, QueueState.DONE, result=GRABBED),
            event(2, QueueState.DONE, result=already),
            event(3, QueueState.NOTHING, result=NOTHING),
            event(4, QueueState.FAILED, message="No torrent program is set up on this PC."),
            event(5, QueueState.DROPPED),
        )
        self.assertEqual(
            queued_run_report(7, 5, events, left_over=2),
            MissingRunReport(
                show_id=7,
                total=5,
                downloaded=("Gold Rush S17E01",),
                already=("Gold Rush S17E02",),
                not_found=("Gold Rush S17E03",),
                left_over=2,
                error="No torrent program is set up on this PC.",
            ),
        )
        self.assertEqual(queued_run_report(7, 0, ()), MissingRunReport(show_id=7))

    def test_a_card_says_queued_while_the_run_waits(self) -> None:
        line = run_line(MissingRunState(1, 3, queued=True))
        assert line is not None
        self.assertEqual(line[0], ("Queued",))
        line = run_line(MissingRunState(2, 3))
        assert line is not None
        self.assertEqual(line[0][0], "Downloading 2 of 3…")


class MissingLibrary:
    def __init__(self, numbers: list[int]) -> None:
        self.numbers = numbers

    async def missing_episodes(self, show_id: int, *, profile_id: int) -> list[CalendarEntry]:
        return [entry(number) for number in self.numbers]


class DownloadMissingTests(LinkTestCase):
    """MainWindow's "Download missing" (bound to a stand-in) into the real queue."""

    def main(self, numbers: list[int]) -> Any:
        self.runs = MissingRuns()
        self.expiries: list[Any] = []
        main = SimpleNamespace(
            _missing_runs=self.runs,
            _download_tasks=set(),
            _queue_link=self.link,
            _services=SimpleNamespace(
                library=MissingLibrary(numbers),
                search=TorrentSearchService([], settings_service()),
                profiles=SimpleNamespace(current_id=1),
            ),
            _report=lambda text, ms: self.reports.append(text),
            after=lambda ms, call: self.expiries.append(call),
        )
        names = ("_download_missing", "_queue_missing", "_episode_item", "_tv_item")
        for name in names:
            setattr(main, name, getattr(MainWindow, name).__get__(main))
        return main

    def run_state(self) -> MissingRunState | None:
        return self.runs.get(GOLD_RUSH.id)

    def finished(self) -> bool:
        state = self.run_state()
        return state is not None and not state.working

    async def test_its_episodes_go_into_the_queue_as_one_batch(self) -> None:
        main = self.main([1, 2, 3])
        self.quick.gate(key(17, 9), key(17, 1))
        self.quick.results[key(17, 2)] = NOTHING
        self.link.download(item(9))  # something else goes first
        main._download_missing(GOLD_RUSH)
        self.assertEqual(self.run_state(), MissingRunState())  # at once
        main._download_missing(GOLD_RUSH)  # clicked twice: one run
        await self.until(lambda: len(self.queue.waiting) == 3)
        self.assertEqual(self.run_state(), MissingRunState(1, 3, queued=True))
        self.assertEqual(self.queue.waiting[0].label, "Gold Rush S17E01")
        self.assertEqual(self.phase(1), Phase.QUEUED)  # the rows and cards say so too
        self.quick.open(key(17, 9))
        await self.until(lambda: self.phase(1) is Phase.WORKING)
        self.assertEqual(self.run_state(), MissingRunState(1, 3))
        self.quick.open(key(17, 1))
        await self.idle()
        state = self.run_state()
        assert state is not None and state.report is not None
        self.assertEqual(
            (state.report.downloaded, state.report.not_found),
            (("Gold Rush S17E01", "Gold Rush S17E03"), ("Gold Rush S17E02",)),
        )
        self.assertEqual(self.reports[-1], "Gold Rush: 2 downloaded · 1 not found yet")
        self.assertEqual(len(self.expiries), 1)
        self.expiries[0]()
        self.assertIsNone(self.run_state())

    async def test_a_failed_grab_stops_the_run(self) -> None:
        main = self.main([1, 2, 3])
        self.quick.errors[key(17, 1)] = GrabError("No torrent program is set up on this PC.")
        with self.assertLogs("app.services.download_queue", "INFO"):
            main._download_missing(GOLD_RUSH)
            await self.until(self.finished)
        state = self.run_state()
        assert state is not None and state.report is not None
        self.assertEqual(state.report.error, "No torrent program is set up on this PC.")
        self.assertEqual(self.quick.started, [key(17, 1)])  # the rest dropped
        self.assertEqual(
            self.reports[-1], "Gold Rush: stopped: No torrent program is set up on this PC."
        )

    async def test_nothing_missing(self) -> None:
        main = self.main([])
        main._download_missing(GOLD_RUSH)
        await self.until(self.finished)
        self.assertEqual(self.reports, ["Gold Rush: nothing missing"])


if __name__ == "__main__":
    unittest.main()
