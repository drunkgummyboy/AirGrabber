"""MediaForge's page (app/forge/ui/page.py, docs/forge/PAGE.md), without AirGrabber's main window
(that part: tests/forge/ui/test_page_in_main_window.py): built in steps, the tabs row's left part
(the queue's + buttons and status line, History's heading and Activity), the rows, the status line,
the Activity panel (only from History, ✕ / Escape / the page changing close it), waiting for a run
or an undo before quitting, the plain frame while Forge is off, fitting the narrowest main window
in both languages.

On real Forge services (a temporary forge.db, TMDB without a key so names come from the files,
HTTP on a MockTransport); Explorer drops through file_drops' fake seam."""

from __future__ import annotations

import asyncio
import gc
import logging
import tkinter as tk
import unittest
from collections.abc import Callable
from typing import cast
from unittest import mock

import customtkinter as ctk
import httpx

from app.config import AppPaths
from app.forge.core.auto import WatchStatus
from app.forge.services import ForgeServices
from app.forge.ui import file_drops
from app.forge.ui.activity_panel import ActivityPanel
from app.forge.ui.history_view import HistoryView
from app.forge.ui.page import AUTO_OFF, GETTING_READY, ForgePage, make_shortcut_once
from app.forge.ui.queue_view import QueueView
from app.forge.ui.settings_pages import SECTION_LIBRARY
from app.i18n import Language, current_language, set_language, t
from app.services.images import ImageCache
from app.services.settings import SettingsService
from app.services.tmdb import TMDBClient
from app.ui import theme
from tests.forge.base import ForgeAsyncTestCase
from tests.forge.tk import pump, pump_until, press

# The main window's smallest size (app/ui/main_window.py) without the band and the status bar.
NARROW = 1220


def _no_network(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404)


class FakeDrops:
    """file_drops' Win32 side: remembers each window's `deliver`, so a test can drop."""

    def __init__(self) -> None:
        self.targets: dict[int, Callable[[list[str]], None]] = {}

    def attach(self, window_id: int, deliver: Callable[[list[str]], None]) -> bool:
        self.targets[window_id] = deliver
        return True

    def detach(self, window_id: int) -> None:
        self.targets.pop(window_id, None)


def destroy_root(root: ctk.CTk) -> None:
    """The root's windows first (a CTkTextbox errors when its timers go before it), then CTk's
    pending timers, then the root."""
    for child in list(root.winfo_children()):
        try:
            child.destroy()
        except tk.TclError:
            pass
    for job in root.tk.splitlist(root.tk.call("after", "info")):
        root.after_cancel(job)
    root.destroy()
    gc.collect()


async def make_services(case: ForgeAsyncTestCase) -> ForgeServices:
    asyncio.get_running_loop().slow_callback_duration = 2.0  # Tk work in a test is slow
    http = httpx.AsyncClient(transport=httpx.MockTransport(_no_network))
    case.addAsyncCleanup(http.aclose)
    services = await ForgeServices.create(
        AppPaths(case.data_dir),
        http,
        TMDBClient(http, api_key=lambda: ""),  # no key: names come from the files
        ImageCache(http, case.data_dir / "posters"),
        cast(SettingsService, mock.Mock(spec=SettingsService)),
    )
    case.addAsyncCleanup(services.aclose)
    return services


class TkCase(ForgeAsyncTestCase):
    """A hidden CTk root (AirGrabber's window in the tray), English, fake Explorer drops."""

    def setUp(self) -> None:
        super().setUp()
        gc.collect()
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(destroy_root, self.root)
        self.addCleanup(set_language, current_language())
        set_language(Language.EN)
        self.addCleanup(ctk.set_appearance_mode, ctk.get_appearance_mode())
        self.drops = FakeDrops()
        self.addCleanup(file_drops.set_native, file_drops.set_native(self.drops))

    def make_stage(self) -> None:
        """What the main window gives the page: its tabs row (the switch in its third column) and
        the frame the page goes in."""
        self.root.grid_columnconfigure(0, weight=1)
        self.root.grid_rowconfigure(1, weight=1)
        self.tabs_row = ctk.CTkFrame(self.root, fg_color="transparent", corner_radius=0)
        self.tabs_row.grid(row=0, column=0, sticky="ew")
        self.tabs_row.grid_columnconfigure(1, weight=1)
        self.switch = tk.Frame(self.tabs_row, width=660, height=90)
        self.switch.grid(row=0, column=2, sticky="e")
        self.holder = ctk.CTkFrame(self.root, fg_color="transparent", corner_radius=0)
        self.holder.grid(row=1, column=0, sticky="nsew", padx=15)

    def show_root(self, width: int = NARROW, height: int = 640) -> None:
        """Laid out and mapped, but see-through."""
        self.root.geometry(f"{width}x{height}+20+20")
        self.root.deiconify()
        self.root.attributes("-alpha", 0.0)

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        asyncio.get_running_loop().slow_callback_duration = 2.0  # Tk work in a test is slow


class PageCase(TkCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.make_stage()
        self.services = await make_services(self)

    def bare_page(self) -> ForgePage:
        page = ForgePage(self.holder, tabs_row=self.tabs_row)
        page.pack(fill="both", expand=True)
        page.set_shown(True)
        return page

    async def make_page(self) -> ForgePage:
        self.show_root()
        page = self.bare_page()
        page.show_forge(self.services)
        queue = page.queue
        assert queue is not None
        await pump_until(self.root, lambda: queue.loaded)
        await pump_until(self.root, lambda: page.pages_built)  # History: a later turn
        await pump(self.root, 0.1)
        return page


class InStepsTests(PageCase):
    """F-33: the queue in one step; History a later turn, or at once when opened first."""

    async def test_the_queue_first_and_history_a_later_turn(self) -> None:
        page = self.bare_page()
        page.show_forge(self.services, WatchStatus("trial", "Manual: watching 1 folder"))
        self.assertIsInstance(page.queue, QueueView)
        self.assertEqual(page.view, "queue")
        self.assertIsNone(page.history)
        self.assertIsNone(page.activity)  # built when it first opens
        self.assertFalse(page.pages_built)
        await pump_until(self.root, lambda: page.pages_built)
        self.assertIsInstance(page.history, HistoryView)
        self.assertEqual(page.view, "queue")  # building it didn't show it

    async def test_history_opened_before_its_turn_is_built_at_once(self) -> None:
        page = self.bare_page()
        page.show_forge(self.services)
        page.toggle_history()
        history = page.history
        self.assertIsInstance(history, HistoryView)
        self.assertEqual(page.view, "history")
        await pump_until(self.root, lambda: page.pages_built)
        self.assertIs(page.history, history)  # its turn came and built nothing again

    async def test_show_forge_twice_builds_once(self) -> None:
        page = self.bare_page()
        page.show_forge(self.services)
        queue = page.queue
        page.show_forge(self.services)
        self.assertIs(page.queue, queue)

    async def test_going_before_the_later_steps_is_quiet(self) -> None:
        page = self.bare_page()
        page.show_forge(self.services)
        page.destroy()
        await pump(self.root, 0.2)  # no step runs on the destroyed page
        self.assertIsNone(page.history)
        self.assertEqual(self.tabs_row.grid_slaves(column=0), [])  # its left part went with it


class RowsTests(PageCase):
    async def test_the_tabs_row_has_the_buttons_and_the_status_line(self) -> None:
        page = await self.make_page()
        queue = page.queue
        assert queue is not None and page.queue_tools is not None
        self.assertEqual(self.tabs_row.grid_slaves(column=0), [page.queue_tools])
        self.assertIs(queue.toolbar.master, page.queue_tools)
        self.assertIs(queue.files_button.master, queue.toolbar)
        self.assertIs(queue.folder_button.master, queue.toolbar)
        assert page.status_button is not None
        self.assertIs(page.status_button.master, page.queue_tools)
        # In their order on the row, left of the switch.
        self.assertLess(queue.files_button.winfo_rootx(), queue.folder_button.winfo_rootx())
        self.assertLess(queue.folder_button.winfo_rootx(), page.status_button.winfo_rootx())
        self.assertLess(
            page.status_button.winfo_rootx() + page.status_button.winfo_width(),
            self.switch.winfo_rootx(),
        )

    async def test_the_preset_card_then_the_filters_and_actions_then_the_list(self) -> None:
        page = await self.make_page()
        queue = page.queue
        assert queue is not None
        card_bottom = queue.card.winfo_rooty() + queue.card.winfo_height()
        filters = queue.filter_buttons["all"]
        self.assertGreaterEqual(filters.winfo_rooty(), card_bottom)
        self.assertLess(filters.winfo_rootx(), queue.forge_button.winfo_rootx())
        # On one row: the middles level.
        self.assertAlmostEqual(
            filters.winfo_rooty() + filters.winfo_height() / 2,
            queue.forge_button.winfo_rooty() + queue.forge_button.winfo_height() / 2,
            delta=3,
        )
        holder = queue.list.master
        self.assertGreaterEqual(holder.winfo_rooty(), filters.winfo_rooty() + filters.winfo_height())
        self.assertLess(queue.clear_button.winfo_rootx(), queue.preview_button.winfo_rootx())
        self.assertLess(queue.preview_button.winfo_rootx(), queue.forge_button.winfo_rootx())

    async def test_history_has_its_heading_its_line_and_activity(self) -> None:
        page = await self.make_page()
        page.toggle_history()
        await pump(self.root, 0.1)
        assert page.history_tools is not None and page.activity_button is not None
        self.assertEqual(self.tabs_row.grid_slaves(column=0), [page.history_tools])
        words = [
            str(child.cget("text"))
            for child in page.history_tools.winfo_children()
            if isinstance(child, ctk.CTkLabel)
        ]
        self.assertEqual(words, ["History", "your last 500 runs · newest first"])
        self.assertIn("Activity", str(page.activity_button.cget("text")))
        self.assertEqual(page.queue.winfo_manager() if page.queue else "x", "")
        assert page.history is not None
        self.assertEqual(page.history.winfo_manager(), "grid")
        page.show_view("queue")
        self.assertEqual(self.tabs_row.grid_slaves(column=0), [page.queue_tools])
        assert page.queue is not None
        self.assertEqual(page.queue.winfo_manager(), "grid")
        self.assertEqual(page.history.winfo_manager(), "")

    async def test_another_page_takes_the_left_part_away_and_brings_it_back(self) -> None:
        page = await self.make_page()
        page.set_shown(False)
        self.assertEqual(self.tabs_row.grid_slaves(column=0), [])
        page.set_shown(True)
        self.assertEqual(self.tabs_row.grid_slaves(column=0), [page.queue_tools])

    async def test_a_run_keeps_the_moved_buttons_states_and_its_progress(self) -> None:
        page = await self.make_page()
        queue = page.queue
        assert queue is not None
        queue._set_running(True, 3)
        await pump(self.root, 0.05)
        self.assertEqual(queue.files_button.cget("state"), "disabled")
        self.assertEqual(queue.folder_button.cget("state"), "disabled")
        self.assertEqual(queue.forge_button.winfo_manager(), "")
        self.assertEqual(queue.progress.winfo_manager(), "pack")
        self.assertEqual(queue.cancel_button.winfo_manager(), "pack")
        # On the filters' row, its middle level with theirs.
        filters = queue.filter_buttons["all"]
        self.assertAlmostEqual(
            filters.winfo_rooty() + filters.winfo_height() / 2,
            queue.progress.winfo_rooty() + queue.progress.winfo_height() / 2,
            delta=3,
        )
        queue._set_running(False)
        self.assertEqual(queue.files_button.cget("state"), "normal")
        self.assertEqual(queue.forge_button.winfo_manager(), "pack")

    async def test_add_paths_queues_files_and_shows_the_queue(self) -> None:
        page = await self.make_page()
        (self.tmp / "Dune.2021.mkv").write_bytes(b"v")
        page.toggle_history()
        page.add_paths([self.tmp])
        await pump_until(self.root, lambda: len(self.services.queue.items) == 1)
        self.assertEqual(page.view, "queue")


class FitTests(PageCase):
    async def fits(self, page: ForgePage) -> None:
        queue = page.queue
        assert queue is not None
        await pump(self.root, 0.3)
        edge = self.root.winfo_rootx() + self.root.winfo_width()
        for widget in (
            queue.forge_button,
            queue.save_as_button,
            queue.changed_note,
            queue.option_line,
            self.switch,
        ):
            if widget.winfo_ismapped():
                self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), edge, widget)
        # The filters never run into the actions, with their counts at their widest.
        queue._paint_filters({"all": 1000, "needs": 1000, "ready": 1000})
        queue.forge_button.configure(text="⚒ " + t("Forge {n} files", n="1,000"))
        await pump(self.root, 0.2)
        last = queue.filter_buttons["ready"]
        self.assertLess(last.winfo_rootx() + last.winfo_width(), queue.actions.winfo_rootx())
        # A run: the progress bar and Cancel in the same place.
        queue._set_running(True, 1000)
        await pump(self.root, 0.2)
        self.assertLess(last.winfo_rootx() + last.winfo_width(), queue.actions.winfo_rootx())
        self.assertLessEqual(
            queue.cancel_button.winfo_rootx() + queue.cancel_button.winfo_width(), edge
        )
        queue._set_running(False)
        # The tabs row's left part, on the queue and on History, stays clear of the switch.
        status = page.status_button
        assert status is not None
        self.assertLess(status.winfo_rootx() + status.winfo_width(), self.switch.winfo_rootx())
        page.toggle_history()
        await pump(self.root, 0.2)
        button = page.activity_button
        assert button is not None
        self.assertLess(button.winfo_rootx() + button.winfo_width(), self.switch.winfo_rootx())
        page.toggle_history()

    async def test_it_fits_the_narrowest_window(self) -> None:
        await self.fits(await self.make_page())

    async def test_it_fits_in_dutch_too(self) -> None:
        set_language(Language.NL)
        await self.fits(await self.make_page())


class StatusLineTests(PageCase):
    async def test_it_fits_the_smallest_main_window_with_the_presets_switch(self) -> None:
        for language, words in ((Language.EN, "Auto-rename off"), (Language.NL, "Automatisch uit")):
            set_language(language)
            page = await self.make_page()
            self.switch.grid_remove()  # this test's stand-in: the real row has nothing there now
            self.root.geometry("1220x640")  # MainWindow's minsize
            await pump(self.root, 0.2)
            page._fit_status()
            with self.subTest(language=language):
                self.assertEqual(page.status_shown, words)
            page.destroy()

    async def test_it_follows_the_status(self) -> None:
        page = await self.make_page()
        self.root.geometry("1600x800")  # room for all of it, whatever this PC's font measures
        await pump(self.root, 0.2)
        # Off at first: quiet.
        self.assertEqual(page.status_shown, t(AUTO_OFF))
        self.assertEqual(page.light, theme.TEXT_MUTED)
        assert page.status_button is not None
        self.assertEqual(tuple(page.status_button.cget("text_color")), theme.TEXT_MUTED)
        page.set_watch_status(WatchStatus("trial", "Manual: watching 1 folder"))
        self.assertEqual(page.status_shown, "Manual: watching 1 folder")
        self.assertEqual(page.light, theme.WARNING)
        self.assertEqual(tuple(page.status_button.cget("text_color")), theme.TEXT)
        page.set_watch_status(WatchStatus("watching", "Watching 2 folders"))
        self.assertEqual(page.light, theme.SUCCESS)
        page.set_watch_status(WatchStatus("unreachable", "Can't reach the NAS"))
        self.assertEqual(page.light, theme.DANGER)
        page.set_watch_status(WatchStatus("off", "Not watching"))
        self.assertEqual(page.status_shown, t(AUTO_OFF))
        # Off because the old MediaForge is still installed: its reason, not "off".
        guard = t("Not watching until the old MediaForge is removed")
        page.set_watch_status(WatchStatus("off", guard))
        self.assertEqual(page.status_shown, guard)

    async def test_a_click_opens_settings_at_auto_rename(self) -> None:
        page = await self.make_page()
        asked: list[str | None] = []
        page.on_settings = asked.append
        assert page.status_button is not None
        page.status_button.invoke()
        self.assertEqual(asked, [SECTION_LIBRARY])
        self.assertEqual(t(SECTION_LIBRARY), "Auto-rename")

    async def test_a_long_status_is_cut_and_never_moves_the_switch(self) -> None:
        page = await self.make_page()
        where = self.switch.winfo_rootx()
        far = "Can't reach \\\\192.168.1.20\\" + "a-very-long-share-name\\" * 8
        page.set_watch_status(WatchStatus("unreachable", far))
        await pump(self.root, 0.2)
        self.assertEqual(page.status_text, far)
        self.assertTrue(page.status_shown.endswith("…"))  # cut to fit
        self.assertEqual(self.switch.winfo_rootx(), where)
        assert page.status_button is not None
        self.assertLess(
            page.status_button.winfo_rootx() + page.status_button.winfo_width(),
            self.switch.winfo_rootx(),
        )


class ActivityTests(PageCase):
    async def test_it_opens_from_historys_button_and_its_cross_closes_it(self) -> None:
        page = await self.make_page()
        self.assertIsNone(page.activity)  # built the first time it opens
        self.services.activity.say("boom happened", level=logging.ERROR)
        page.toggle_history()
        await pump(self.root, 0.1)
        assert page.activity_button is not None
        page.activity_button.invoke()
        await pump(self.root, 0.2)
        self.assertTrue(page.activity_open)
        panel = page.activity
        assert isinstance(panel, ActivityPanel)
        self.assertTrue(panel.winfo_ismapped())
        self.assertIn("boom happened", panel.text())  # what came before it opened
        self.services.activity.say("and then this")
        await pump(self.root, 0.1)
        self.assertIn("and then this", panel.text())
        panel.close_button.invoke()  # its ✕
        await pump(self.root, 0.1)
        self.assertFalse(page.activity_open)

    async def test_it_sits_at_the_top_right_of_the_page(self) -> None:
        page = await self.make_page()
        page.toggle_history()
        page.show_activity()
        await pump(self.root, 0.2)
        panel = page.activity
        assert panel is not None
        self.assertGreaterEqual(panel.winfo_rooty(), page.winfo_rooty())
        right = page.winfo_rootx() + page.winfo_width()
        self.assertLessEqual(panel.winfo_rootx() + panel.winfo_width(), right)
        self.assertGreater(panel.winfo_rootx(), page.winfo_rootx() + page.winfo_width() // 3)

    async def test_escape_closes_it_but_not_from_a_text_field(self) -> None:
        page = await self.make_page()
        entry = ctk.CTkEntry(self.tabs_row)
        entry.grid(row=0, column=1)
        page.toggle_history()
        page.show_activity()
        await pump(self.root, 0.1)
        press(entry._entry, "<Escape>")  # typing: not the panel's
        await pump(self.root, 0.05)
        self.assertTrue(page.activity_open)
        press(self.root, "<Escape>")
        await pump(self.root, 0.05)
        self.assertFalse(page.activity_open)

    async def test_the_page_changing_closes_it(self) -> None:
        page = await self.make_page()
        page.toggle_history()
        page.show_activity()
        page.show_view("queue")  # 🕘 or MediaForge
        self.assertFalse(page.activity_open)
        page.toggle_history()
        page.show_activity()
        page.set_shown(False)  # another page of the main window
        self.assertFalse(page.activity_open)

    async def test_nothing_before_forge_is_there(self) -> None:
        page = self.bare_page()
        page.show_activity()
        self.assertIsNone(page.activity)
        page.toggle_history()
        self.assertIsNone(page.history)


class MessagesTests(PageCase):
    async def test_what_the_queue_says_also_goes_to_on_message(self) -> None:
        page = await self.make_page()
        heard: list[str] = []
        page.on_message = heard.append
        queue = page.queue
        assert queue is not None
        queue._say("Put back 1 file", success=True)
        self.assertEqual(heard, ["Put back 1 file"])
        self.assertIn("Put back 1 file", [line.text for line in self.services.activity.lines()])

    async def test_a_preview_says_where_its_lines_are(self) -> None:
        page = await self.make_page()
        heard: list[str] = []
        page.on_message = heard.append
        (self.tmp / "Dune.2021.mkv").write_bytes(b"v")
        queue = page.queue
        assert queue is not None
        await queue.add([self.tmp])
        heard.clear()
        await queue.preview()
        self.assertEqual(heard, [t("Preview is in History › Activity. Nothing has been changed.")])
        self.assertIn(
            t("Preview (nothing has been changed):"),
            [line.text for line in self.services.activity.lines()],
        )


class ClosingTests(PageCase):
    async def test_may_close_waits_for_a_running_forge(self) -> None:
        page = await self.make_page()
        waiting: list[Callable[[], None]] = []
        queue = page.queue
        assert queue is not None

        def busy(close: Callable[[], None]) -> bool:
            waiting.append(close)
            return False

        queue.request_close = busy  # type: ignore[method-assign]
        retry = mock.Mock()
        self.assertFalse(page.may_close(retry))
        self.assertEqual(waiting, [retry])
        queue.request_close = lambda close: True  # type: ignore[method-assign]
        self.assertTrue(page.may_close(retry))

    async def test_may_close_waits_for_an_undo_in_history(self) -> None:
        page = await self.make_page()
        answers: list[bool] = []

        def retry() -> None:
            answers.append(page.may_close(retry))

        assert page.history is not None
        page.history.busy = True
        self.assertFalse(page.may_close(retry))
        await pump(self.root, 0.4)
        self.assertEqual(answers[:1], [False])  # asked again, still busy
        page.history.busy = False
        await pump_until(self.root, lambda: answers and answers[-1])

    async def test_destroy_lets_go_of_the_main_window(self) -> None:
        page = await self.make_page()
        page.show_activity()
        queue = page.queue
        assert queue is not None
        spawned = queue.spawn(asyncio.sleep(10))
        page.destroy()
        await asyncio.sleep(0)
        self.assertTrue(spawned.cancelled() or spawned.cancelling())  # its tasks are cancelled
        self.assertEqual(self.tabs_row.grid_slaves(column=0), [])
        press(self.root, "<Escape>")  # its binding went: nothing happens


class PlainFrameTests(PageCase):
    async def test_it_says_why_and_tries_again(self) -> None:
        self.show_root()
        page = self.bare_page()
        tries: list[int] = []
        page.show_problem("MediaForge isn't ready yet.", lambda: tries.append(1))
        await pump(self.root, 0.1)
        assert page.message_label is not None and page.try_again_button is not None
        self.assertEqual(page.message_label.cget("text"), "MediaForge isn't ready yet.")
        self.assertEqual(page.try_again_button.cget("text"), "Try again")
        self.assertTrue(page.try_again_button.winfo_ismapped())
        page.try_again_button.invoke()
        self.assertEqual(tries, [1])
        self.assertIsNone(page.queue)
        # Forge starts after all: the queue replaces it.
        page.show_forge(self.services)
        await pump(self.root, 0.1)
        self.assertIsNone(page.message)
        self.assertEqual(page.view, "queue")
        page.show_problem("ignored now", None)
        self.assertIsNone(page.message)

    async def test_getting_ready_has_no_button(self) -> None:
        self.show_root()
        page = self.bare_page()
        page.show_getting_ready()
        await pump(self.root, 0.05)
        assert page.message_label is not None
        self.assertEqual(page.message_label.cget("text"), t(GETTING_READY))
        self.assertIsNone(page.try_again_button)


class DutchTests(PageCase):
    async def test_dutch(self) -> None:
        set_language(Language.NL)
        page = await self.make_page()
        self.root.geometry("1600x800")
        await pump(self.root, 0.2)
        self.assertEqual(page.status_shown, "Automatisch uit")
        page.toggle_history()
        await pump(self.root, 0.05)
        assert page.activity_button is not None
        self.assertEqual(page.activity_button.cget("text"), "☰  Activiteit")
        assert page.history_tools is not None
        words = [
            str(child.cget("text"))
            for child in page.history_tools.winfo_children()
            if isinstance(child, ctk.CTkLabel)
        ]
        self.assertEqual(words[0], "Geschiedenis")


class ShortcutOnceTests(unittest.TestCase):
    """The first visit in a run makes the "MediaForge" Start-menu shortcut, only in an installed
    copy (what the taskbar module started before it went)."""

    def test_a_development_copy_starts_nothing(self) -> None:
        with (
            mock.patch("app.forge.ui.page._shortcut_checked", False),
            mock.patch("app.forge.ui.page.tasks.spawn") as spawn,
        ):
            self.assertFalse(make_shortcut_once())  # this folder has tests/
        spawn.assert_not_called()

    def test_an_installed_copy_starts_it_once(self) -> None:
        def closed(coro: object, **_kwargs: object) -> None:
            getattr(coro, "close")()

        with (
            mock.patch("app.forge.ui.page._shortcut_checked", False),
            mock.patch("app.forge.ui.page.is_development_copy", return_value=False),
            mock.patch("app.forge.ui.page.sys.platform", "win32"),
            mock.patch("app.forge.ui.page.tasks.spawn", side_effect=closed) as spawn,
        ):
            self.assertTrue(make_shortcut_once())
            self.assertFalse(make_shortcut_once())
        spawn.assert_called_once()


if __name__ == "__main__":
    unittest.main()
