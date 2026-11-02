"""The Forge window (app/forge/ui/window.py), ported from MediaForge's tests/ui/test_shell.py and
redesigned (mockups/forge-chosen.html): the band (a dark-copper gradient with a copper line, the
pages as words with the one shown underlined, the "needs you" badge on Queue, the watching chip,
the Activity button and ⚙ Settings), the pages, the Activity panel, the band and the queue fitting the
smallest window, adding files and Explorer drops on the window, the watching chip (and the
Auto-rename page with it), waiting for a Forge run or an undo before closing, size and place.
Plus AirGrabber's parts: the plain page while Forge is off, Dutch, the logo and icon, never
transient, and the copper close button of Forge's borderless dialogs (app/ui/borderless.py's
`look=`).

On real Forge services (a temporary forge.db, TMDB without a key so names come from the files,
HTTP on a MockTransport); Explorer drops through file_drops' fake seam."""

from __future__ import annotations

import asyncio
import gc
import logging
import sys
import tkinter as tk
import unittest
from collections.abc import Callable
from typing import Any, cast
from unittest import mock

import customtkinter as ctk
import httpx

from app.config import AppPaths
from app.forge.core.auto import WatchStatus
from app.forge.core.matcher import Resolution
from app.forge.core.queue import ItemStatus
from app.forge.models.media import MatchState, ResolvedMedia
from app.forge.services import ForgeServices
from app.forge.ui import file_drops
from app.forge.ui.activity_panel import ActivityPanel
from app.forge.ui.history_view import HistoryView
from app.forge.ui.library_page import LibraryPage
from app.forge.ui.look import forge_look
from app.forge.ui.queue_view import QueueView
from app.forge.ui.settings_pages import SECTION_LIBRARY
from app.forge.ui.window import (
    ICON_FILE,
    LOGO_FILE,
    MIN_HEIGHT,
    MIN_WIDTH,
    ForgeWindow,
    band_gradient,
    parse_geometry,
)
from app.i18n import Language, current_language, set_language
from app.services.images import ImageCache
from app.services.settings import SettingsService
from app.services.tmdb import TMDBClient
from app.ui import borderless, palettes, theme
from tests.forge.base import ForgeAsyncTestCase
from tests.forge.tk import pump, pump_until
from tests.forge.ui.test_file_drops import post_drop


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


def destroy_window(window: tk.Misc) -> None:
    try:
        if window.winfo_exists():
            window.destroy()
    except tk.TclError:
        pass


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


def rgb(widget: tk.Misc, color: theme.Color) -> tuple[int, int, int]:
    red, green, blue = widget.winfo_rgb(theme.pick(color))
    return red >> 8, green >> 8, blue >> 8


def fills(canvas: tk.Canvas, tag: str) -> list[str]:
    return [
        str(canvas.tk.call(str(canvas), "itemcget", item, "-fill")).upper()
        for item in canvas.find_withtag(tag)
    ]


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

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        asyncio.get_running_loop().slow_callback_duration = 2.0  # Tk work in a test is slow


class WindowCase(TkCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.services = await make_services(self)

    async def make_window(self, geometry: str | None = "1200x800+20+20") -> ForgeWindow:
        window = ForgeWindow(self.root, geometry=geometry)
        window.attributes("-alpha", 0.0)  # laid out and mapped, but see-through
        self.addCleanup(destroy_window, window)
        window.show_forge(self.services)
        queue = window.queue
        assert queue is not None
        await pump_until(window, lambda: queue.loaded)
        await pump_until(window, lambda: window.pages_built)  # History, Auto-rename: later turns
        await pump(window, 0.1)
        return window

    async def window(self) -> ForgeWindow:
        if not hasattr(self, "_window"):
            self._window = await self.make_window()
        return self._window


class PagesTests(WindowCase):
    async def test_builds_the_three_pages_and_switches_between_them(self) -> None:
        window = await self.window()
        self.assertEqual(window.current_page, "queue")
        for page in ("history", "auto", "queue"):
            window.show_page(page)
            await pump(window, 0.05)
            self.assertEqual(window.current_page, page)
            self.assertEqual(window.band.current, page)
        self.assertIsInstance(window.queue, QueueView)
        self.assertIsInstance(window.history, HistoryView)
        self.assertIsInstance(window.library, LibraryPage)
        queue = window.queue
        assert queue is not None
        await pump_until(window, queue.winfo_ismapped)
        window.show_page("history")
        await pump_until(window, lambda: not queue.winfo_ismapped())

    async def test_the_band_shows_the_pages_as_words_and_marks_the_one_shown(self) -> None:
        window = await self.window()
        band = window.band
        self.assertEqual([word for _, word in band.pages], ["Queue", "History", "Auto-rename"])
        self.assertEqual(band.item_text("tab-history"), "History")
        look = forge_look()
        [mark] = band.canvas.find_withtag("tab-mark")
        self.assertIn(mark, band.canvas.find_withtag("tab-queue"))  # under Queue
        self.assertEqual(fills(band.canvas, "tab-mark"), [theme.pick(look.BRAND).upper()])
        band.click("tab-auto")  # a click on a word shows its page
        await pump(window, 0.05)
        self.assertEqual(window.current_page, "auto")
        [mark] = band.canvas.find_withtag("tab-mark")
        self.assertIn(mark, band.canvas.find_withtag("tab-auto"))
        # A real click inside the word's box.
        x0, _y0, x1, _y1 = band.canvas.bbox("tab-history")
        band.canvas.event_generate("<Button-1>", x=(x0 + x1) // 2, y=band.canvas.winfo_height() // 2)
        await pump(window, 0.05)
        self.assertEqual(window.current_page, "history")

    async def test_the_band_is_a_copper_gradient_with_a_copper_line(self) -> None:
        window = await self.window()
        look = forge_look()
        for mode, dark in (("light", False), ("dark", True)):
            ctk.set_appearance_mode(mode)
            await pump(window, 0.1)
            canvas = window.band.canvas
            rects = [i for i in canvas.find_all() if str(canvas.type(i)) == "rectangle"]
            colours = [
                str(canvas.tk.call(str(canvas), "itemcget", i, "-fill")).upper() for i in rects
            ]
            left, right = band_gradient(look)
            with self.subTest(mode=mode):
                self.assertEqual(colours[0], theme.pick(left, dark=dark).upper())
                self.assertIn(theme.pick(right, dark=dark).upper(), colours)
                self.assertIn(theme.pick(look.ACCENT, dark=dark).upper(), colours)  # the line

    async def test_history_reads_its_runs_when_it_shows(self) -> None:
        window = await self.window()
        history = window.history
        assert history is not None
        self.assertEqual(history.kind, "all")  # no Series | Movies switch any more
        window.show_page("history")
        await pump_until(window, lambda: history.empty_label.winfo_manager() != "")
        self.assertEqual(
            history.empty_label.cget("text"),
            "Nothing here yet. Runs show up after you Forge some files.",
        )

    async def test_the_auto_rename_page_scrolls_so_more_in_settings_stays_reachable(
        self,
    ) -> None:
        # Orchestrator note (F-18): in a short window with the Activity panel open.
        window = await self.window()
        clicks: list[str | None] = []
        window.on_settings = clicks.append
        assert window.library is not None
        self.assertIsInstance(window.library.master, ctk.CTkScrollableFrame)
        window.show_activity()
        window.geometry(f"{MIN_WIDTH}x{MIN_HEIGHT}")
        window.show_page("auto")
        await pump(window, 0.2)
        window.library.more_button.invoke()
        self.assertEqual(clicks, [SECTION_LIBRARY])  # MediaForge's Settings at that section


class BadgeTests(WindowCase):
    async def test_the_badge_shows_the_needs_count(self) -> None:
        window = await self.window()
        assert window.queue is not None
        window.queue.on_counts = lambda needs: None  # only this count is under test
        window.set_needs(3)
        await pump(window, 0.05)
        band = window.band
        self.assertEqual(band.item_text("badge"), "3")
        self.assertEqual(fills(band.canvas, "badge")[0], theme.pick(forge_look().ACCENT).upper())
        badge_box = band.canvas.bbox("badge")
        queue_word = [
            i for i in band.canvas.find_withtag("tab-queue") if str(band.canvas.type(i)) == "text"
        ][0]
        self.assertGreater(badge_box[0], band.canvas.bbox(queue_word)[2])  # after the word
        self.assertLess(badge_box[2], band.canvas.bbox("tab-history")[0])
        window.set_needs(0)
        await pump(window, 0.05)
        self.assertEqual(band.canvas.find_withtag("badge"), ())
        self.assertEqual(window.needs, 0)

    async def test_files_that_need_you_light_the_badge(self) -> None:
        window = await self.window()
        folder = self.tmp / "in"
        folder.mkdir()
        (folder / "Dune.2021.mkv").write_bytes(b"v")
        (folder / "Heat.1995.mkv").write_bytes(b"v")
        window.add_paths([folder])
        await pump_until(window, lambda: len(self.services.queue.items) == 2)
        await self.services.queue.wait_idle()
        await pump(window, 0.2)
        self.assertEqual(window.needs, 0)  # ready to rename: nothing needs the owner
        for item in self.services.queue.items:
            item.resolution = Resolution(
                media=ResolvedMedia.offline(item.parsed, MatchState.UNCERTAIN)
            )
            item.status = ItemStatus.NEEDS_CHOICE
        assert window.queue is not None
        window.queue.refresh()
        await pump_until(window, lambda: window.needs == 2)
        self.assertEqual(window.band.item_text("badge"), "2")

    async def test_the_badge_is_readable_in_both_looks(self) -> None:
        look = forge_look()
        for dark in (False, True):
            with self.subTest(dark=dark):
                fill = theme.pick(look.ACCENT, dark=dark)
                self.assertGreaterEqual(palettes.contrast_ratio(theme.ON_COLOR, fill), 4.5)


class LayoutTests(WindowCase):
    async def _fits(self, window: ForgeWindow) -> None:
        width, height = window.wm_minsize()
        scale = ctk.ScalingTracker.get_window_scaling(window)
        window.geometry(f"{round(width / scale)}x{round(height / scale)}")
        await pump(window, 0.4)
        band = window.band
        right = band.canvas.winfo_width()
        self.assertLessEqual(band.needed_width(), right)
        words = band.canvas.bbox("tab-auto")
        chip_left = band.watch_chip.winfo_rootx() - band.canvas.winfo_rootx()
        self.assertLess(words[2], chip_left)  # the words never run into the chip
        settings = band.settings_button
        edge = settings.winfo_rootx() + settings.winfo_width() - band.canvas.winfo_rootx()
        self.assertLessEqual(edge, right)
        queue = window.queue
        assert queue is not None
        edge = queue.winfo_rootx() + queue.winfo_width()
        for widget in (queue.forge_button, queue.save_as_button, queue.changed_note):
            if widget.winfo_ismapped():
                self.assertLessEqual(widget.winfo_rootx() + widget.winfo_width(), edge, widget)

    async def test_the_band_and_the_queue_fit_the_smallest_window(self) -> None:
        window = await self.window()
        width, height = window.wm_minsize()
        scale = ctk.ScalingTracker.get_window_scaling(window)
        self.assertGreaterEqual(width, round(MIN_WIDTH * scale) - 1)  # never below MediaForge's
        self.assertGreaterEqual(height, round(MIN_HEIGHT * scale) - 1)
        window.set_watch_status(WatchStatus("trial", "Trial: watching 2 folders"))
        await self._fits(window)

    async def test_a_long_status_doesnt_widen_the_window(self) -> None:
        normal = await self.window()
        window = ForgeWindow(self.root, geometry="1200x800+20+20")
        window.attributes("-alpha", 0.0)
        self.addCleanup(destroy_window, window)
        far = "Can't reach \\\\192.168.1.20\\" + "a-very-long-share-name\\" * 8
        window.show_forge(self.services, WatchStatus("unreachable", far))
        await pump_until(window, lambda: window.pages_built)  # the minimum width: a later turn
        await pump(window, 0.1)
        self.assertEqual(window.band.status_text, far)
        self.assertTrue(window.band.chip_text.endswith("…"))  # cut to fit
        self.assertEqual(window.wm_minsize(), normal.wm_minsize())

    async def test_they_fit_in_dutch_too(self) -> None:
        set_language(Language.NL)
        window = await self.make_window()
        window.set_watch_status(WatchStatus("trial", "Proef: 2 mappen bewaakt"))
        await self._fits(window)


class ActivityTests(WindowCase):
    async def test_the_activity_button_opens_the_panel_over_the_pages(self) -> None:
        window = await self.window()
        self.assertFalse(window.activity_open)
        self.assertIsNone(window.activity)  # built the first time it opens
        self.services.activity.say("boom happened", level=logging.ERROR)
        window.band.click("activity")
        await pump(window, 0.2)
        self.assertTrue(window.activity_open)
        panel = window.activity
        assert isinstance(panel, ActivityPanel)
        self.assertTrue(panel.winfo_ismapped())
        self.assertIn("boom happened", panel.text())  # what came before it opened
        self.assertTrue(window.band.activity_open)
        self.services.activity.say("and then this")
        await pump(window, 0.1)
        self.assertIn("and then this", panel.text())
        panel.close_button.invoke()  # its ✕
        await pump(window, 0.1)
        self.assertFalse(window.activity_open)
        self.assertFalse(window.band.activity_open)
        window.toggle_activity()
        window.event_generate("<Escape>")
        await pump(window, 0.1)
        self.assertFalse(window.activity_open)

    async def test_the_panel_sits_at_the_top_right_under_the_band(self) -> None:
        window = await self.window()
        window.show_activity()
        await pump(window, 0.2)
        panel = window.activity
        assert panel is not None
        band_bottom = window.band.winfo_rooty() + window.band.winfo_height()
        self.assertGreaterEqual(panel.winfo_rooty(), band_bottom)
        window_right = window.winfo_rootx() + window.winfo_width()
        self.assertLessEqual(panel.winfo_rootx() + panel.winfo_width(), window_right)
        self.assertGreater(panel.winfo_rootx(), window.winfo_rootx() + window.winfo_width() // 3)


class FilesTests(WindowCase):
    async def test_add_paths_queues_files_and_shows_the_queue(self) -> None:
        window = await self.window()
        (self.tmp / "Dune.2021.mkv").write_bytes(b"v")
        window.show_page("history")
        window.add_paths([self.tmp])
        await pump_until(window, lambda: len(self.services.queue.items) == 1)
        self.assertEqual(window.current_page, "queue")

    async def test_a_drop_anywhere_on_the_window_goes_to_the_queue(self) -> None:
        window = await self.window()
        self.assertTrue(window.drops)
        deliver = self.drops.targets[window.winfo_id()]
        assert window.queue is not None
        self.assertIn(window.queue.winfo_id(), self.drops.targets)  # the queue's own as well
        (self.tmp / "in").mkdir()
        (self.tmp / "in" / "Dune.2021.mkv").write_bytes(b"v")
        (self.tmp / "folder").mkdir()
        (self.tmp / "folder" / "Heat.1995.mkv").write_bytes(b"v")
        (self.tmp / "folder" / "notes.txt").write_bytes(b"n")
        window.show_page("auto")
        deliver([str(self.tmp / "in" / "Dune.2021.mkv"), str(self.tmp / "folder")])
        await pump_until(window, lambda: len(self.services.queue.items) == 2)
        self.assertEqual(
            sorted(i.path.name for i in self.services.queue.items),
            ["Dune.2021.mkv", "Heat.1995.mkv"],
        )
        self.assertEqual(window.current_page, "queue")
        window_id = window.winfo_id()
        window.destroy()
        self.assertNotIn(window_id, self.drops.targets)  # the window's own procedure is back

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    async def test_a_real_drop_on_the_band_reaches_the_queue(self) -> None:
        # The real Win32 side on the window's own window (the band, History and Auto-rename hand
        # a drop up to it), with a drop posted as Explorer posts it.
        real = file_drops.windows_native()
        if real is None:
            self.skipTest("Win32 file drops unavailable")
        file_drops.set_native(real)
        window = await self.make_window()
        self.assertTrue(window.drops)
        (self.tmp / "in").mkdir()
        (self.tmp / "in" / "Amélie.2001.mkv").write_bytes(b"v")
        (self.tmp / "folder").mkdir()
        (self.tmp / "folder" / "Ted.Lasso.S04E03.mkv").write_bytes(b"v")
        window.show_page("history")
        post_drop(
            window.winfo_id(), [str(self.tmp / "in" / "Amélie.2001.mkv"), str(self.tmp / "folder")]
        )
        await pump_until(window, lambda: len(self.services.queue.items) == 2)
        self.assertEqual(
            sorted(i.path.name for i in self.services.queue.items),
            ["Amélie.2001.mkv", "Ted.Lasso.S04E03.mkv"],
        )
        self.assertEqual(window.current_page, "queue")

    async def test_without_drops_nothing_says_to_drop(self) -> None:
        file_drops.set_native(None)
        window = await self.make_window()
        self.assertFalse(window.drops)
        self.assertEqual(window.band.chip_text, "Auto-rename off")  # never "Drop files here"
        assert window.queue is not None
        self.assertEqual(window.queue.empty_title.cget("text"), "Add video files or folders")


class WatchingChipTests(WindowCase):
    async def test_the_watching_chip_follows_the_status(self) -> None:
        window = await self.window()
        band = window.band
        assert window.library is not None
        look = forge_look()
        # Off at first: a quiet chip, its outline only, that opens the Auto-rename page too.
        self.assertEqual(band.chip_text, "Auto-rename off")
        self.assertEqual(band.light, theme.TEXT_MUTED)
        self.assertEqual(tuple(band.watch_chip.cget("fg_color")), look.TAB_BG)
        band.watch_chip.invoke()
        self.assertEqual(window.current_page, "auto")
        window.show_page("queue")
        window.set_watch_status(WatchStatus("trial", "Trial: watching 1 folder"))
        await pump(window, 0.05)
        self.assertEqual(band.chip_text, "Trial: watching 1 folder")
        self.assertEqual(band.light, theme.WARNING)
        self.assertEqual(tuple(band.watch_chip.cget("fg_color")), look.BG)
        self.assertEqual(window.library.status_label.cget("text"), "Trial: watching 1 folder")
        self.assertEqual(window.library.state, "trial")
        window.set_watch_status(WatchStatus("watching", "Watching 2 folders"))
        self.assertEqual(band.light, theme.SUCCESS)
        window.set_watch_status(WatchStatus("unreachable", "Can't reach the NAS"))
        self.assertEqual(band.light, theme.DANGER)
        band.click("watch")  # a click on the chip
        self.assertEqual(window.current_page, "auto")
        window.set_watch_status(WatchStatus("off", "Not watching"))
        await pump(window, 0.05)
        self.assertEqual(band.chip_text, "Auto-rename off")
        # Off because the old MediaForge is still installed: its reason, not "off".
        guard = "Not watching until the old MediaForge is removed"
        window.set_watch_status(WatchStatus("off", guard))
        self.assertEqual(band.chip_text, guard)

    async def test_settings_opens_mediaforges_settings(self) -> None:
        window = await self.window()
        clicks: list[str | None] = []
        window.on_settings = clicks.append
        button = window.band.settings_button
        self.assertEqual(button.cget("text"), "⚙")
        button.invoke()
        self.assertEqual(clicks, [None])  # its own Settings, at the first section
        window.band.click("settings")
        self.assertEqual(clicks, [None, None])
        # The same style as Activity, and a tooltip.
        activity = window.band.activity_button
        for option in ("fg_color", "border_color", "corner_radius", "height"):
            self.assertEqual(button.cget(option), activity.cget(option), option)
        from app.ui import tooltip

        tip = tooltip._manager
        button.event_generate("<Enter>")
        await pump_until(window, lambda: tip.owner is not None, within=3)
        owner = tip.owner
        assert owner is not None
        self.assertEqual(owner.text, "Settings")
        owner.hide()


class ClosingTests(WindowCase):
    async def test_may_close_waits_for_a_running_forge(self) -> None:
        window = await self.window()
        waiting: list[Callable[[], None]] = []
        queue = window.queue
        assert queue is not None

        def busy(close: Callable[[], None]) -> bool:
            waiting.append(close)
            return False

        queue.request_close = busy  # type: ignore[method-assign]
        retry = mock.Mock()
        self.assertFalse(window.may_close(retry))
        self.assertEqual(waiting, [retry])
        queue.request_close = lambda close: True  # type: ignore[method-assign]
        self.assertTrue(window.may_close(retry))

    async def test_may_close_waits_for_an_undo_in_history(self) -> None:
        window = await self.window()
        answers: list[bool] = []

        def retry() -> None:
            answers.append(window.may_close(retry))

        assert window.history is not None
        window.history.busy = True
        self.assertFalse(window.may_close(retry))
        await pump(window, 0.4)
        self.assertEqual(answers[:1], [False])  # asked again, still busy
        window.history.busy = False
        await pump_until(window, lambda: answers and answers[-1])


class PlaceTests(WindowCase):
    async def test_a_remembered_size_and_place_come_back(self) -> None:
        window = await self.make_window(geometry="1100x700+30+40")
        await pump(window, 0.1)
        saved = parse_geometry(window.saved_geometry())
        assert saved is not None
        self.assertEqual(saved[:2], (1100, 700))
        self.assertLess(abs(saved[2] - 30) + abs(saved[3] - 40), 3)

    async def test_a_place_on_a_screen_that_is_gone_moves_onto_one(self) -> None:
        window = await self.make_window(geometry="1100x700+40000+30000")
        await pump(window, 0.1)
        saved = parse_geometry(window.saved_geometry())
        assert saved is not None
        screen_w, screen_h = window.wm_maxsize()
        self.assertLess(saved[2], 40000)
        self.assertLess(saved[3], 30000)
        self.assertLess(saved[2], screen_w * 4)

    async def test_a_new_window_gets_the_default_size(self) -> None:
        window = await self.make_window(geometry=None)
        saved = parse_geometry(window.saved_geometry())
        assert saved is not None
        self.assertGreaterEqual(saved[0], MIN_WIDTH)
        self.assertGreaterEqual(saved[1], MIN_HEIGHT)

    async def test_minimized_or_maximized_isnt_remembered(self) -> None:
        window = await self.window()
        window.state("zoomed")
        await pump(window, 0.1)
        self.assertIsNone(window.saved_geometry())

    def test_parse_geometry(self) -> None:
        self.assertEqual(parse_geometry("1200x800+40+-8"), (1200, 800, 40, -8))
        self.assertIsNone(parse_geometry("1200x800"))
        self.assertIsNone(parse_geometry(None))
        self.assertIsNone(parse_geometry("big"))


class ProblemPageTests(TkCase):
    async def test_the_plain_page_says_why_and_tries_again(self) -> None:
        window = ForgeWindow(self.root, geometry="1100x700+20+20")
        window.attributes("-alpha", 0.0)
        self.addCleanup(destroy_window, window)
        tries: list[int] = []
        window.show_problem("MediaForge isn't ready yet.", lambda: tries.append(1))
        await pump(window, 0.1)
        assert window.message_label is not None and window.try_again_button is not None
        self.assertEqual(window.message_label.cget("text"), "MediaForge isn't ready yet.")
        self.assertEqual(window.try_again_button.cget("text"), "Try again")
        self.assertTrue(window.try_again_button.winfo_ismapped())
        window.try_again_button.invoke()
        self.assertEqual(tries, [1])
        self.assertIsNone(window.tabs)
        self.assertEqual(window.band.pages, [])  # no page words yet
        window.band.click("activity")  # nothing to show yet: harmless
        self.assertIsNone(window.activity)
        # Forge starts after all: the pages replace the plain page.
        services = await make_services(self)
        window.show_forge(services)
        await pump(window, 0.1)
        self.assertIsNone(window.message)
        self.assertEqual(window.current_page, "queue")
        window.show_problem("ignored now", None)
        self.assertIsNone(window.message)


class LookTests(WindowCase):
    async def test_dutch(self) -> None:
        set_language(Language.NL)
        window = await self.make_window()
        band = window.band
        self.assertEqual(
            [word for _, word in band.pages], ["Wachtrij", "Geschiedenis", "Automatisch"]
        )
        self.assertEqual(band.chip_text, "Automatisch uit")
        self.assertEqual(band.activity_button.cget("text"), "☰  Activiteit")

    async def test_logo_icon_title_and_never_transient(self) -> None:
        self.assertTrue(LOGO_FILE.is_file())
        self.assertTrue(ICON_FILE.is_file())
        window = await self.window()
        await pump_until(window, lambda: window.band.logo is not None)
        self.assertTrue(window.band.canvas.find_withtag("logo"))
        self.assertEqual(window.title(), "MediaForge")
        self.assertEqual(str(window.transient()), "")
        self.assertIs(window.master, self.root)


class CloseButtonLookTests(TkCase):
    """Orchestrator note (F-12): Forge's borderless windows get a copper-tinted close button."""

    def _disc(self, button: borderless.CloseButton) -> tuple[int, int, int]:
        discs = button._discs
        assert discs is not None
        size = int(button.cget("width"))
        photo: Any = discs[0]
        pixel = photo._PhotoImage__photo.get(size // 2, size // 2)
        return int(pixel[0]), int(pixel[1]), int(pixel[2])

    async def test_the_close_button_takes_a_look(self) -> None:
        top = ctk.CTkToplevel(self.root)
        top.attributes("-alpha", 0.0)
        look = forge_look()
        plain = borderless.CloseButton(top, command=lambda: None)
        copper = borderless.CloseButton(top, command=lambda: None, look=look)
        await pump(top, 0.05)
        for dark in (False, True):
            ctk.set_appearance_mode("dark" if dark else "light")
            await pump(top, 0.1)
            self.assertEqual(self._disc(copper), rgb(top, look.CHIP))
            self.assertEqual(self._disc(plain), rgb(top, theme.CLOSE_BUTTON))
        top.destroy()

    async def test_make_borderless_hands_the_look_on(self) -> None:
        class FakeNative:
            def attach(self, window_id: int, *, dark: bool) -> bool:
                return True

            def start_drag(self, window_id: int) -> None:
                pass

            def set_dark(self, window_id: int, dark: bool) -> None:
                pass

        self.addCleanup(borderless.set_native, borderless.set_native(FakeNative()))
        top = ctk.CTkToplevel(self.root)
        top.attributes("-alpha", 0.0)
        look = forge_look()
        made = borderless.make_borderless(top, look=look)
        assert made is not None
        self.assertIs(made.close_button._look, look)
        top.destroy()


class OpeningInStepsTests(WindowCase):
    """F-33: opening the window is one step for the band and the queue; the minimum width,
    History and Auto-rename follow a step per turn, or at once when opened first."""

    def bare_window(self) -> ForgeWindow:
        window = ForgeWindow(self.root, geometry="1200x800+20+20")
        window.attributes("-alpha", 0.0)
        self.addCleanup(destroy_window, window)
        return window

    async def test_show_forge_builds_the_band_and_the_queue_and_the_rest_later(self) -> None:
        window = self.bare_window()
        window.show_forge(self.services, WatchStatus("trial", "Trial: watching 1 folder"))
        self.assertIsInstance(window.queue, QueueView)
        self.assertEqual(len(window.band.pages), 3)
        self.assertEqual(window.current_page, "queue")
        self.assertIsNone(window.history)
        self.assertIsNone(window.library)
        self.assertIsNone(window.activity)  # built when it first opens
        self.assertFalse(window.pages_built)
        await pump_until(window, lambda: window.pages_built)
        self.assertIsInstance(window.history, HistoryView)
        self.assertIsInstance(window.library, LibraryPage)
        self.assertEqual(window.current_page, "queue")  # building them didn't show them
        assert window.library is not None
        self.assertEqual(window.library.state, "trial")  # the status it missed
        self.assertGreater(window.wm_minsize()[0], 0)

    async def test_a_page_opened_before_its_turn_is_built_at_once(self) -> None:
        window = self.bare_window()
        window.show_forge(self.services)
        window.show_page("history")
        history = window.history
        self.assertIsInstance(history, HistoryView)
        self.assertEqual(window.current_page, "history")
        window.band.click("tab-auto")  # a click on the band's word
        library = window.library
        self.assertIsInstance(library, LibraryPage)
        await pump_until(window, lambda: window.pages_built)
        self.assertIs(window.history, history)  # its turn came and built nothing again
        self.assertIs(window.library, library)
        self.assertEqual(window.current_page, "auto")

    async def test_closing_before_the_later_steps_is_quiet(self) -> None:
        window = self.bare_window()
        window.show_forge(self.services)
        window.destroy()
        await pump(self.root, 0.2)  # no step runs on the destroyed window
        self.assertIsNone(window.history)


if __name__ == "__main__":
    unittest.main()
