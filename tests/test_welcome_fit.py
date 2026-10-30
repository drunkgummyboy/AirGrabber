"""The welcome and Quick setup dialogs on a small screen (app/ui/dialog_kit.py `fit_height`,
`on_screen`, `place_over`, FitScroll): at most as tall as the work area allows, the step that
needs more scrolls while Back / Next (Save / Cancel) stay visible; where they fit, nothing
scrolls and the welcome keeps one size. The work area is faked; the widgets run on a real
(see-through) Tk root."""

from __future__ import annotations

import asyncio
import gc
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

import customtkinter as ctk

from app.i18n import Language
from app.models import Settings
from app.services import SettingsService
from app.ui import dialog_kit
from app.ui.dialog_kit import SCREEN_MARGIN, WINDOW_FRAME, fit_height, on_screen
from app.ui.quick_setup import QuickSetupDialog
from app.ui.welcome_dialog import Page, WelcomeDialog


class FakeRepository:
    async def save(self, settings: Settings) -> None:
        pass


class FitMathTests(unittest.TestCase):
    def test_fit_height(self) -> None:
        room = 688 - WINDOW_FRAME - 2 * SCREEN_MARGIN  # 1920 × 1080 at 150 %
        self.assertEqual(fit_height(756, 688), room)
        self.assertEqual(fit_height(500, 688), 500)  # fits: unchanged
        self.assertEqual(fit_height(500, 10), 1)

    def test_on_screen(self) -> None:
        area = (0, 0, 1920, 1032)
        self.assertEqual(on_screen(100, 100, 800, 600, area), (100, 100))
        self.assertEqual(on_screen(1500, 700, 800, 600, area), (1120, 432))
        self.assertEqual(on_screen(-50, -20, 800, 600, area), (0, 0))
        self.assertEqual(on_screen(10, 10, 800, 1200, area), (10, 0))  # top stays on screen
        self.assertEqual(on_screen(-3000, 50, 800, 600, (-1920, 0, 0, 1080)), (-1920, 50))


class FitTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.attributes("-alpha", 0.0)  # on screen, but see-through
        self.root.geometry("600x400+40+40")
        self.addCleanup(self._destroy_root)
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.scaling = ctk.ScalingTracker.get_window_scaling(self.root)

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0  # building a dialog is slow

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.tk.call("after", "cancel", job)
        self.root.destroy()
        gc.collect()

    async def settle(self) -> None:
        for _ in range(6):
            await asyncio.sleep(0.01)
            self.root.update()

    def screen(self, room: int) -> Any:
        """A work area where a dialog's content may be `room` CTk units tall."""
        height = round((room + WINDOW_FRAME + 2 * SCREEN_MARGIN) * self.scaling)
        return mock.patch.object(dialog_kit, "work_area", return_value=(0, 0, 3000, height))

    def settings(self) -> SettingsService:
        return SettingsService(
            FakeRepository(),  # type: ignore[arg-type]
            Settings(download_dir=self.folder),
        )

    def assert_inside(self, widget: Any, window: Any) -> None:
        """`widget` lies fully within `window`'s content."""
        bottom = widget.winfo_rooty() + widget.winfo_height()
        self.assertTrue(widget.winfo_ismapped())
        self.assertLessEqual(bottom, window.winfo_rooty() + window.winfo_height())

    @staticmethod
    def close(widget: Any) -> None:
        if widget.winfo_exists():
            widget.destroy()


class WelcomeFitTests(FitTestCase):
    def welcome(self) -> WelcomeDialog:
        dialog = WelcomeDialog(
            self.root,
            settings=self.settings(),
            torrent_app=mock.MagicMock(find=mock.AsyncMock(return_value=None)),
            discovery=mock.MagicMock(),
            airdates=mock.MagicMock(),
            library=mock.MagicMock(),
        )
        dialog.attributes("-alpha", 0.0)
        self.addCleanup(self.close, dialog)
        return dialog

    async def test_a_big_screen_changes_nothing(self) -> None:
        with self.screen(3000):
            dialog = self.welcome()
            await self.settle()
        heights = set()
        for page in (Page.WELCOME, Page.AIRDATES, Page.TMDB, Page.QUICK):
            dialog._show_page(page)
            await self.settle()
            self.assertFalse(dialog._steps.scrolls, page)
            heights.add(dialog.winfo_height())
        self.assertEqual(len(heights), 1)  # one size for every step
        self.assertFalse(dialog._steps.reserved)  # no room kept for a scrollbar

    async def test_a_small_screen_scrolls_the_tall_step(self) -> None:
        room = 520
        with self.screen(room):
            dialog = self.welcome()
            await self.settle()
        self.assertLessEqual(dialog.winfo_height(), round(room * self.scaling) + 2)
        self.assertTrue(dialog._steps.reserved)
        dialog._show_page(Page.QUICK)
        await self.settle()
        self.assertTrue(dialog._steps.scrolls)
        self.assert_inside(dialog._right, dialog)  # Finish
        self.assert_inside(dialog._left, dialog)  # Back
        dialog._show_page(Page.WELCOME)  # fits: no scrollbar
        await self.settle()
        self.assertFalse(dialog._steps.scrolls)
        # Placed on the (faked) work area.
        self.assertGreaterEqual(dialog.winfo_rooty(), 0)


class QuickSetupFitTests(FitTestCase):
    def quick(self) -> QuickSetupDialog:
        dialog = QuickSetupDialog(self.root, settings=self.settings())
        dialog.attributes("-alpha", 0.0)
        self.addCleanup(self.close, dialog)
        return dialog

    async def test_a_big_screen_changes_nothing(self) -> None:
        with self.screen(3000):
            dialog = self.quick()
            await self.settle()
        self.assertFalse(dialog._scroll.scrolls)
        height = dialog.winfo_height()
        dialog.panel.language.choose(Language.NL)  # the language note: it grows, as before
        await self.settle()
        self.assertFalse(dialog._scroll.scrolls)
        self.assertGreater(dialog.winfo_height(), height)
        dialog.panel.language.choose(Language.EN)
        await self.settle()
        self.assertEqual(dialog.winfo_height(), height)

    async def test_a_small_screen_scrolls_above_the_buttons(self) -> None:
        room = 420
        with self.screen(room):
            dialog = self.quick()
            await self.settle()
        self.assertTrue(dialog._scroll.scrolls)
        self.assertLessEqual(dialog.winfo_height(), round(room * self.scaling) + 2)
        self.assert_inside(dialog.save_button, dialog)


if __name__ == "__main__":
    unittest.main()
