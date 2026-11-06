"""MediaForge's page in AirGrabber's real main window (app/ui/main_window.py, docs/forge/PAGE.md):
the switch's third part and its badge, the band's compact state and back, 🕘 only here and lit on
History, a first visit building once, the queue's keys only on the queue and never in a text field,
a drop from another page, each kind of request with the window hidden, minimised and shown, a
confirm-only start leaving (D5), Settings over the main window, the problem pages, a reload that
leaves the page alone, closing the window on this page, and quitting.

Real Services on a temporary data folder, every HTTP client on a MockTransport (no answers),
Explorer drops through file_drops' fake seam, the window mapped but see-through."""

from __future__ import annotations

import asyncio
import gc
import sys
import tkinter as tk
import unittest
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest import mock

import customtkinter as ctk
import httpx

from app.config import AppPaths
from app.forge.core.queue import ItemStatus
from app.forge.runtime import ForgeState
from app.forge.ui import file_drops
from app.forge.ui.page import ForgePage
from app.forge.ui.settings_pages import SECTION_LIBRARY
from app.i18n import Language, current_language, set_language
from app.models import CardSize, Settings
from app.services import Services
from app.single_instance import ForgeRequest
from app.ui.forge_link import quits_after_forge
from app.ui.main_window import MainWindow, Mode, settings_affect_pages
from app.ui.spotlight import BAND_HEIGHT, COMPACT_HEIGHT, SwitchPart
from tests.forge.base import ForgeAsyncTestCase
from tests.forge.tk import pump, pump_until, press
from tests.forge.ui.test_file_drops import post_drop
from tests.forge.ui.test_page import FakeDrops

_real_client_init = httpx.AsyncClient.__init__


def _no_network(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404)


def _offline_client(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
    kwargs["transport"] = httpx.MockTransport(_no_network)
    _real_client_init(self, *args, **kwargs)


class MainWindowCase(ForgeAsyncTestCase):
    start_hidden = False
    request: ForgeRequest | None = None
    guessit = True

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        asyncio.get_running_loop().slow_callback_duration = 3.0  # building a window is slow
        gc.collect()
        self.addCleanup(set_language, current_language())
        set_language(Language.EN)
        self.addCleanup(ctk.set_appearance_mode, ctk.get_appearance_mode())
        self.enterContext(mock.patch.object(httpx.AsyncClient, "__init__", _offline_client))

        async def parser_ready() -> bool:
            return self.guessit

        self.enterContext(mock.patch("app.forge.runtime._parser_ready", parser_ready))
        self.enterContext(mock.patch("app.forge.services._parser_warm", True))
        self.drops = FakeDrops()
        self.addCleanup(file_drops.set_native, file_drops.set_native(self.drops))
        self.services = await Services.create(AppPaths(self.data_dir).ensure())
        try:
            self.window = MainWindow(
                self.services,
                start_hidden=self.start_hidden,
                forge_request=self.request,
            )
        except tk.TclError as exc:  # no display
            await self.services.aclose()
            self.skipTest(f"Tk unavailable: {exc}")
        self.addAsyncCleanup(self._close)
        self.window.attributes("-alpha", 0.0)  # laid out and mapped, but see-through
        self.window._go_home()  # as `_start`, without its background work
        self.window._attach_drops()
        await pump(self.window, 0.1)

    async def _close(self) -> None:
        window = self.window
        await window._forge.shutdown()
        try:
            if window.winfo_exists():
                window.destroy()
        except tk.TclError:
            # A page's LoadingBar: MainWindow.destroy cancels its timer behind its back, and Tk's
            # destroy stops half way (as `_destroy_for_good` swallows at a real quit). The next
            # test's root must become the default root: CTkImage makes its photos there.
            try:
                window.tk.call("destroy", ".")
            except tk.TclError:
                pass
            setattr(tk, "_default_root", None)
        await self.services.aclose()
        gc.collect()

    @property
    def host(self) -> Any:
        return self.window._forge

    async def on_page(self) -> ForgePage:
        """The switch's MediaForge, the queue built."""
        self.window._part_clicked(SwitchPart.FORGE)
        await pump_until(self.window, lambda: self.page_ready())
        await pump(self.window, 0.1)
        page: ForgePage | None = self.host.page
        assert page is not None
        return page

    def page_ready(self) -> bool:
        page = self.host.page
        return page is not None and page.queue is not None and page.queue.loaded

    def forge(self) -> Any:
        services = self.services.forge.services
        assert services is not None
        return services

    def video(self, name: str = "Dune.2021.mkv") -> Path:
        path = self.tmp / name
        path.write_bytes(b"v")
        return path

    def item_count(self) -> int:
        services = self.services.forge.services
        return 0 if services is None else len(services.queue.items)


class SwitchTests(MainWindowCase):
    async def test_three_parts_lead_to_their_pages(self) -> None:
        window = self.window
        self.assertIs(window._mode, Mode.SERIES)
        self.assertIs(window._band.part, SwitchPart.SERIES)
        window._part_clicked(SwitchPart.MOVIES)
        self.assertIs(window._mode, Mode.MOVIES)
        self.assertIs(window._band.part, SwitchPart.MOVIES)
        window._part_clicked(SwitchPart.FORGE)
        self.assertIs(window._mode, Mode.FORGE)
        self.assertIs(window._band.part, SwitchPart.FORGE)
        self.assertEqual(window._forge_holder.winfo_manager(), "grid")
        self.assertEqual(window._movie_view.winfo_manager(), "")
        await pump_until(window, self.page_ready)
        window._select_mode(Mode.DOWNLOADS)
        self.assertIsNone(window._band.part)
        self.assertEqual(window._forge_holder.winfo_manager(), "")
        window._part_clicked(SwitchPart.SERIES)
        self.assertIs(window._band.part, SwitchPart.SERIES)

    async def test_the_badge_shows_from_every_page(self) -> None:
        window = self.window
        tray: list[int] = []
        self.enterContext(mock.patch.object(window.tray, "set_forge_count", tray.append))
        window._on_forge_needs(3)
        self.assertEqual(window._band.forge_count, 3)
        self.assertEqual(tray, [3])
        for mode in (Mode.SERIES, Mode.MOVIES, Mode.DOWNLOADS):
            window._select_mode(mode)
            self.assertEqual(window._band.forge_count, 3, mode)

    async def test_the_queues_needs_reach_the_badge(self) -> None:
        window = self.window
        page = await self.on_page()
        self.assertEqual(window._band.forge_count, 0)
        queue = page.queue
        assert queue is not None
        await queue.add([self.video()])
        await queue.settled()
        for item in self.forge().queue.items:
            item.status = ItemStatus.NEEDS_CHOICE
        queue.refresh()
        await pump_until(window, lambda: window._band.forge_count == 1)
        window._select_mode(Mode.SERIES)  # shown from every page
        self.assertEqual(window._band.forge_count, 1)

    async def test_the_tabs_rows_left_part_follows_the_page(self) -> None:
        window = self.window
        pills = window._series_tabs.tab_row
        assert pills is not None
        self.assertEqual(pills.winfo_manager(), "grid")
        page = await self.on_page()
        self.assertEqual(pills.winfo_manager(), "")
        self.assertEqual(window._tabs_row.grid_slaves(column=0), [page.queue_tools])
        window._part_clicked(SwitchPart.SERIES)
        await pump(window, 0.1)
        self.assertEqual(window._tabs_row.grid_slaves(column=0), [pills])
        self.assertEqual(window._tabs_row.winfo_manager(), "grid")
        window._part_clicked(SwitchPart.MOVIES)
        self.assertEqual(window._tabs_row.grid_slaves(column=0), [])
        self.assertEqual(window._tabs_row.winfo_manager(), "")  # nothing to put on it

    async def test_the_calendars_controls_show_with_the_calendar_only(self) -> None:
        window = self.window
        calendar = window._series_views["Calendar"]
        dates, right = calendar._dates, calendar._right
        self.assertEqual(dates.winfo_manager(), "place")
        self.assertEqual(right.master, window._tabs_row)
        window._series_tabs.set("Discover")
        window._on_tab_changed()
        self.assertEqual(dates.winfo_manager(), "")
        self.assertEqual(right.winfo_manager(), "")
        window._series_tabs.set("Calendar")
        window._on_tab_changed()
        self.assertEqual(dates.winfo_manager(), "place")
        window._part_clicked(SwitchPart.MOVIES)
        self.assertEqual(dates.winfo_manager(), "")
        window._part_clicked(SwitchPart.SERIES)
        self.assertEqual(right.winfo_manager(), "grid")


class BandTests(MainWindowCase):
    async def test_compact_on_the_page_full_everywhere_else(self) -> None:
        window = self.window
        band = window._band
        self.assertFalse(band.compact)
        self.assertEqual(int(band.cget("height")), band._px(BAND_HEIGHT))
        await self.on_page()
        self.assertTrue(band.compact)
        self.assertEqual(int(band.cget("height")), band._px(COMPACT_HEIGHT))
        for mode in (Mode.SERIES, Mode.MOVIES, Mode.DOWNLOADS):
            window._select_mode(Mode.FORGE)
            self.assertTrue(band.compact)
            window._select_mode(mode)
            self.assertFalse(band.compact, mode)
            self.assertEqual(int(band.cget("height")), band._px(BAND_HEIGHT))

    async def test_the_top_row_makes_room_for_the_clock_beside_settings(self) -> None:
        window = self.window
        band = window._band
        await pump(window, 0.1)
        before = band.layout
        assert before is not None
        await self.on_page()
        inside = band.layout
        assert inside is not None
        self.assertEqual(inside.settings, before.settings)
        self.assertEqual(inside.person, before.person)
        self.assertEqual(inside.brand, before.brand)
        self.assertGreater(inside.history.w, 0)
        self.assertLess(inside.history.x, inside.settings.x)
        self.assertLess(inside.downloads.x, inside.history.x)
        window._part_clicked(SwitchPart.SERIES)
        after = band.layout
        assert after is not None
        self.assertEqual(after.history.w, 0)
        self.assertEqual(after.search, before.search)
        self.assertEqual(after.downloads, before.downloads)

    async def test_the_search_still_goes_where_it_goes(self) -> None:
        window = self.window
        await self.on_page()
        opened: list[str] = []
        self.enterContext(mock.patch.object(window, "_open_search_results", opened.append))
        window._search.insert(0, "Northern Pines")
        window._on_search()
        self.assertEqual(opened, ["Northern Pines"])
        self.assertIs(window._mode, Mode.FORGE)  # the results open as a window of their own


class HistoryIconTests(MainWindowCase):
    async def test_a_click_toggles_and_it_is_lit_on_history(self) -> None:
        window = self.window
        page = await self.on_page()
        band = window._band
        self.assertFalse(band._history_lit)
        window._toggle_forge_history()
        self.assertEqual(page.view, "history")
        self.assertTrue(band._history_lit)
        window._toggle_forge_history()
        self.assertEqual(page.view, "queue")
        self.assertFalse(band._history_lit)

    async def test_mediaforge_in_the_switch_goes_back_to_the_queue(self) -> None:
        window = self.window
        page = await self.on_page()
        window._toggle_forge_history()
        window._part_clicked(SwitchPart.FORGE)
        self.assertEqual(page.view, "queue")
        self.assertFalse(window._band._history_lit)

    async def test_coming_back_shows_the_queue_again(self) -> None:
        window = self.window
        page = await self.on_page()
        window._toggle_forge_history()
        window._part_clicked(SwitchPart.MOVIES)
        window._part_clicked(SwitchPart.FORGE)
        self.assertEqual(page.view, "queue")
        self.assertFalse(window._band._history_lit)

    async def test_activity_closes_when_the_main_windows_page_changes(self) -> None:
        window = self.window
        page = await self.on_page()
        window._toggle_forge_history()
        page.show_activity()
        window._part_clicked(SwitchPart.SERIES)
        self.assertFalse(page.activity_open)


class FirstVisitTests(MainWindowCase):
    async def test_builds_once_and_starts_forge(self) -> None:
        self.assertIs(self.services.forge.state, ForgeState.OFF)
        self.assertEqual(self.window._forge_holder.winfo_children(), [])  # nothing before it
        made: list[ForgePage] = []
        original = ForgePage.__init__

        def counted(page: ForgePage, *args: Any, **kwargs: Any) -> None:
            made.append(page)
            original(page, *args, **kwargs)

        with mock.patch.object(ForgePage, "__init__", counted):
            page = await self.on_page()
            queue = page.queue
            self.window._part_clicked(SwitchPart.SERIES)
            self.window._part_clicked(SwitchPart.FORGE)
            await pump(self.window, 0.1)
        self.assertEqual(made, [page])
        self.assertIs(page.queue, queue)
        self.assertIs(self.services.forge.state, ForgeState.READY)

    async def test_settings_and_library_changes_leave_the_page_alone(self) -> None:
        window = self.window
        page = await self.on_page()
        queue = page.queue
        with mock.patch.object(ForgePage, "refresh_page") as refreshed:
            window._reload_pages()  # a setting that touches the pages
            window._reload_pages(quiet=True)  # the library
            await pump(window, 0.1)
        refreshed.assert_not_called()
        self.assertIs(page.queue, queue)
        self.assertIs(self.host.page, page)
        self.assertIsNone(window._visible_view())
        self.assertTrue(
            settings_affect_pages(Settings(card_size=CardSize.S), Settings(card_size=CardSize.L))
        )

    async def test_f5_reads_the_page_again(self) -> None:
        window = self.window
        page = await self.on_page()
        with mock.patch.object(ForgePage, "refresh_page") as refreshed:
            window._refresh_page(mock.Mock(widget=window))
        refreshed.assert_called_once_with()
        self.assertIsNotNone(page.queue)


class NotReadyTests(MainWindowCase):
    guessit = False

    async def test_the_page_says_why_and_try_again_restarts(self) -> None:
        window = self.window
        window._part_clicked(SwitchPart.FORGE)
        await pump_until(window, lambda: self.host.page is not None)
        page = self.host.page
        await pump_until(window, lambda: page.try_again_button is not None)
        await pump(window, 0.1)
        self.assertTrue(page.try_again_button.winfo_ismapped())
        self.assertTrue(window._band.compact)
        window._toggle_forge_history()  # nothing to show: harmless
        self.assertIsNone(page.history)
        with mock.patch("app.bootstrap.retry_optional") as retry, mock.patch.object(
            window._quit_gate, "request"
        ) as quits:
            page.try_again_button.invoke()
        retry.assert_called_once_with("guessit")
        self.assertTrue(window.restart_requested)
        quits.assert_called_once_with()
        window.restart_requested = False


class FailedTests(MainWindowCase):
    async def test_the_page_says_why_and_try_again_starts_it_again(self) -> None:
        window = self.window
        from app.forge.services import ForgeServices

        with mock.patch.object(ForgeServices, "create", side_effect=OSError("locked")):
            window._part_clicked(SwitchPart.FORGE)
            await pump_until(window, lambda: self.services.forge.state is ForgeState.FAILED)
            await pump_until(
                window,
                lambda: self.host.page is not None and self.host.page.try_again_button is not None,
            )
        page = self.host.page
        page.try_again_button.invoke()
        await pump_until(window, self.page_ready)
        self.assertIsNone(page.message)


class KeysTests(MainWindowCase):
    """The queue's keys are bound on the main window: only while the queue shows, never in a text
    field."""

    KEYS = ("<Return>", "<Control-Return>", "<Control-z>", "<Up>", "<Down>")

    async def spy(self) -> tuple[ForgePage, list[str]]:
        page = await self.on_page()
        queue = page.queue
        assert queue is not None
        taken: list[str] = []

        async def nothing() -> None:
            return None

        def recorded(name: str) -> Callable[..., Any]:
            def call(*_args: Any, **_kwargs: Any) -> Any:
                taken.append(name)
                return nothing()

            return call

        self.enterContext(mock.patch.object(queue, "preview", recorded("preview")))
        self.enterContext(mock.patch.object(queue, "forge", recorded("forge")))
        self.enterContext(mock.patch.object(queue, "undo_latest", recorded("undo")))

        def moved(_step: int) -> None:
            taken.append("move")

        self.enterContext(mock.patch.object(queue, "move_selection", moved))
        return page, taken

    def press_all(self, widget: tk.Misc) -> None:
        for key in self.KEYS:
            press(widget, key)

    async def test_on_the_queue_they_work(self) -> None:
        _page, taken = await self.spy()
        self.window.focus_force()
        self.press_all(self.window)
        await pump(self.window, 0.05)
        self.assertEqual(taken, ["preview", "forge", "undo", "move", "move"])

    async def test_on_airgrabbers_other_pages_they_do_nothing(self) -> None:
        _page, taken = await self.spy()
        for mode in (Mode.SERIES, Mode.MOVIES, Mode.DOWNLOADS):
            with self.subTest(mode):
                self.window._select_mode(mode)
                await pump(self.window, 0.05)
                self.press_all(self.window)
                await pump(self.window, 0.05)
                self.assertEqual(taken, [])

    async def test_on_history_they_do_nothing(self) -> None:
        _page, taken = await self.spy()
        self.window._toggle_forge_history()
        await pump(self.window, 0.05)
        self.press_all(self.window)
        await pump(self.window, 0.05)
        self.assertEqual(taken, [])

    async def test_in_airgrabbers_search_they_do_nothing(self) -> None:
        _page, taken = await self.spy()
        entry = self.window._search._entry
        self.press_all(entry)
        await pump(self.window, 0.05)
        self.assertEqual(taken, [])

    async def test_in_the_rule_box_they_do_nothing(self) -> None:
        page, taken = await self.spy()
        queue = page.queue
        assert queue is not None
        self.press_all(queue.rule_text._textbox)
        await pump(self.window, 0.05)
        self.assertEqual(taken, [])

    async def test_while_the_window_is_hidden_they_do_nothing(self) -> None:
        page, _taken = await self.spy()
        queue = page.queue
        assert queue is not None
        self.window.withdraw()
        await pump(self.window, 0.1)
        self.assertFalse(queue.shortcut("undo"))
        self.window.deiconify()

    async def test_ctrl_f_and_f5_are_the_main_windows_still(self) -> None:
        await self.spy()
        press(self.window, "<Control-f>")
        await pump(self.window, 0.05)
        self.assertEqual(self.window.focus_get(), self.window._search._entry)


class EscapeTests(MainWindowCase):
    async def test_escape_closes_activity_only_while_it_shows_on_the_page(self) -> None:
        window = self.window
        page = await self.on_page()
        window._toggle_forge_history()
        page.show_activity()
        await pump(window, 0.05)
        window.focus_force()
        press(window, "<Escape>")
        await pump(window, 0.05)
        self.assertFalse(page.activity_open)
        press(window, "<Escape>")  # nothing open: nothing happens
        window._part_clicked(SwitchPart.SERIES)
        press(window, "<Escape>")
        self.assertIs(window._mode, Mode.SERIES)


class DropTests(MainWindowCase):
    async def test_a_drop_from_another_page_goes_to_the_queue_and_shows_the_page(self) -> None:
        window = self.window
        deliver = self.drops.targets[window.winfo_id()]
        video = self.video()
        window._select_mode(Mode.MOVIES)
        self.assertIsNone(self.host.page)  # Forge hasn't started: it waits for the page
        deliver([str(video)])
        await pump_until(window, lambda: self.item_count() == 1)
        self.assertIs(window._mode, Mode.FORGE)
        self.assertEqual(self.forge().queue.items[0].path, video)
        self.assertEqual(self.host.page.view, "queue")

    async def test_a_drop_on_history_shows_the_queue(self) -> None:
        window = self.window
        page = await self.on_page()
        window._toggle_forge_history()
        self.drops.targets[window.winfo_id()]([str(self.video())])
        await pump_until(window, lambda: self.item_count() == 1)
        self.assertEqual(page.view, "queue")
        self.assertFalse(window._band._history_lit)

    async def test_a_drop_during_a_run_is_queued_as_before(self) -> None:
        window = self.window
        page = await self.on_page()
        queue = page.queue
        assert queue is not None
        queue._set_running(True, 1)
        self.drops.targets[window.winfo_id()]([str(self.video())])
        await pump_until(window, lambda: self.item_count() == 1)
        self.assertEqual(queue.files_button.cget("state"), "disabled")  # it goes on
        queue._set_running(False)


class RealDropTests(MainWindowCase):
    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    async def test_a_real_drop_on_the_main_window_reaches_the_queue(self) -> None:
        # The real Win32 side on the main window's own window (its children hand a drop up to it),
        # with a drop posted as Explorer posts it.
        real = file_drops.windows_native()
        if real is None:
            self.skipTest("Win32 file drops unavailable")
        file_drops.set_native(real)
        window = self.window
        window._attach_drops()
        (self.tmp / "in").mkdir()
        video = self.tmp / "in" / "Amélie.2001.mkv"
        video.write_bytes(b"v")
        window._select_mode(Mode.MOVIES)
        post_drop(window.winfo_id(), [str(video)])
        await pump_until(window, lambda: self.item_count() == 1)
        self.assertIs(window._mode, Mode.FORGE)
        self.assertEqual(self.forge().queue.items[0].path, video)
        file_drops.set_native(self.drops)


class RequestTests(MainWindowCase):
    """Each request with the main window hidden, minimised and shown."""

    def set_state(self, state: str) -> None:
        window = self.window
        if state == "hidden":
            window.withdraw()
        elif state == "minimised":
            window.iconify()
        window.update()

    async def test_a_plain_request_shows_it_on_the_page_with_its_files(self) -> None:
        window = self.window
        for state in ("shown", "minimised", "hidden"):
            with self.subTest(state):
                window._select_mode(Mode.SERIES)
                self.set_state(state)
                video = self.video(f"Film.{state}.2020.mkv")
                self.host.handle(ForgeRequest(paths=(str(video),)))
                await pump_until(window, lambda: window.state() == "normal", within=3)
                self.assertIs(window._mode, Mode.FORGE)
                await pump_until(
                    window,
                    lambda: self.services.forge.services is not None
                    and any(i.path == video for i in self.forge().queue.items),
                )
                self.assertEqual(self.host.page.view, "queue")

    async def test_the_tray_and_the_shortcut_open_it_too(self) -> None:
        window = self.window
        self.set_state("hidden")
        window.open_forge()  # the tray's Open MediaForge; the shortcut's plain `--forge` likewise
        await pump_until(window, self.page_ready)
        self.assertEqual(window.state(), "normal")
        self.assertIs(window._mode, Mode.FORGE)

    async def test_trays_open_airgrabber_still_goes_home(self) -> None:
        window = self.window
        await self.on_page()
        window.withdraw()
        window.update()
        window.tray.open_window()
        self.assertEqual(window.state(), "normal")
        self.assertIs(window._mode, Mode.SERIES)

    async def test_a_confirm_request_shows_only_its_window(self) -> None:
        window = self.window
        for state in ("hidden", "minimised", "shown"):
            with self.subTest(state):
                self.set_state(state)
                before = window.state()
                self.host.handle(
                    ForgeRequest(paths=(str(self.video(f"Film.{state}.mkv")),), confirm=True)
                )
                await pump_until(window, lambda: self.host.confirm_windows, within=5)
                confirm = self.host.confirm_windows[0]
                confirm.attributes("-alpha", 0.0)
                self.assertEqual(window.state(), before)
                self.assertIs(window._mode, Mode.SERIES)
                self.assertIsNone(self.host.page)
                with mock.patch.object(window, "quit_app"):
                    confirm.cancel()
                window.deiconify()
                window.update()

    async def test_open_in_mediaforge_shows_the_hidden_window(self) -> None:
        window = self.window
        self.set_state("hidden")
        self.host.handle(ForgeRequest(paths=(str(self.video()),), confirm=True))
        await pump_until(window, lambda: self.host.confirm_windows, within=5)
        confirm = self.host.confirm_windows[0]
        confirm.attributes("-alpha", 0.0)
        await confirm.loaded()
        with mock.patch.object(window, "quit_app") as quits:
            confirm.open_in_forge()
            await pump_until(window, lambda: self.item_count() == 1)
        self.assertEqual(window.state(), "normal")
        self.assertIs(window._mode, Mode.FORGE)
        quits.assert_not_called()  # it stays: the window shows now


class ConfirmStartTests(MainWindowCase):
    """D5: a start for Forge with a confirm request."""

    request = ForgeRequest(paths=(r"C:\made\up\Dune.2021.mkv",), confirm=True)

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.window.withdraw()
        self.window.update()

    async def confirm(self) -> Any:
        self.host.handle(ForgeRequest(paths=(str(self.video()),), confirm=True))
        await pump_until(self.window, lambda: self.host.confirm_windows, within=5)
        confirm = self.host.confirm_windows[0]
        confirm.attributes("-alpha", 0.0)
        return confirm

    async def test_it_leaves_when_its_last_confirm_window_closes(self) -> None:
        window = self.window
        confirm = await self.confirm()
        self.assertEqual(window.state(), "withdrawn")
        self.assertFalse(window.tray.running)  # no tray icon: nothing to stay in
        with mock.patch.object(window, "quit_app") as quits:
            confirm.cancel()
        quits.assert_called_once_with()

    async def test_it_stays_in_the_tray_with_keep_in_tray(self) -> None:
        window = self.window
        confirm = await self.confirm()
        await self.services.settings.update(keep_in_tray=True)
        with (
            mock.patch.object(type(window.tray), "running", new_callable=mock.PropertyMock) as run,
            mock.patch.object(window, "quit_app") as quits,
        ):
            run.return_value = True
            confirm.cancel()
        quits.assert_not_called()

    async def test_it_stays_once_the_window_has_shown(self) -> None:
        window = self.window
        window.deiconify()
        window.update()
        window.attributes("-alpha", 0.0)
        confirm = await self.confirm()
        with mock.patch.object(window, "quit_app") as quits:
            confirm.cancel()
        quits.assert_not_called()


class PlainStartTests(MainWindowCase):
    """A start for Forge with a plain request: the main window on the page."""

    request = ForgeRequest()

    async def test_it_shows_the_page_when_its_request_comes(self) -> None:
        window = self.window
        window.withdraw()
        window.update()
        await window._forge_pump.run()  # the request this start carries (no mailbox: it ends)
        await pump_until(window, self.page_ready)
        self.assertEqual(window.state(), "normal")
        self.assertIs(window._mode, Mode.FORGE)
        self.assertFalse(
            quits_after_forge(
                started_for_forge=True,
                forge_windows_open=self.host.windows_open,
                main_hidden=window.state() == "withdrawn",
                keep_in_tray=False,
                tray_running=False,
            )
        )


class SettingsTests(MainWindowCase):
    async def test_the_status_line_opens_settings_over_the_main_window(self) -> None:
        window = self.window
        page = await self.on_page()
        assert page.status_button is not None
        page.status_button.invoke()
        dialog = window._settings_dialog
        assert dialog is not None
        dialog.attributes("-alpha", 0.0)
        self.assertIs(dialog.master, window)
        self.assertEqual(str(dialog.transient()), str(window))
        self.assertEqual(dialog.section, SECTION_LIBRARY)
        dialog.cancel()

    async def test_gear_opens_it_at_its_usual_page_on_this_page_too(self) -> None:
        window = self.window
        await self.on_page()
        window._open_settings()
        dialog = window._settings_dialog
        assert dialog is not None
        dialog.attributes("-alpha", 0.0)
        self.assertIs(dialog.master, window)
        self.assertNotEqual(dialog.section, SECTION_LIBRARY)
        dialog.cancel()

    async def test_the_sidebars_order(self) -> None:
        window = self.window
        window._open_settings(SECTION_LIBRARY)
        dialog = window._settings_dialog
        assert dialog is not None
        dialog.attributes("-alpha", 0.0)
        sections = dialog.sections
        first = sections.index("Auto-rename")
        self.assertEqual(
            sections[first : first + 4], ["Auto-rename", "Naming", "Subtitles", "Right-click menu"]
        )
        dialog.cancel()

    async def test_closing_the_window_cancels_settings_first(self) -> None:
        window = self.window
        window._open_settings()
        dialog = window._settings_dialog
        assert dialog is not None
        with mock.patch.object(window.tray, "close_window") as closes:
            window._close_window()
        self.assertFalse(dialog.winfo_exists())
        self.assertIsNone(window._settings_dialog)
        closes.assert_called_once_with()


class ClosingTests(MainWindowCase):
    async def test_closing_on_this_page_does_what_it_does_elsewhere(self) -> None:
        window = self.window
        page = await self.on_page()
        queue = page.queue
        assert queue is not None
        queue._set_running(True, 1)
        with mock.patch.object(window.tray, "close_window") as closes:
            window._close_window()
        closes.assert_called_once_with()
        self.assertTrue(queue.running)  # a run goes on
        queue._set_running(False)

    async def test_quitting_waits_through_may_close(self) -> None:
        window = self.window
        page = await self.on_page()
        queue = page.queue
        assert queue is not None
        waited: list[Callable[[], None]] = []

        def busy(close: Callable[[], None]) -> bool:
            waited.append(close)
            return False

        with mock.patch.object(queue, "request_close", busy):
            window.quit_app()
            self.assertEqual(len(waited), 1)
            self.assertFalse(window._quit_gate.leaving)
        window._quit_gate._asked = False
        window._forge_pump._stopped = False
