"""The Activity panel (app/forge/ui/activity_panel.py), with MediaForge's log-strip cases from
tests/ui/test_shell.py: lines shown, coloured by level; plus earlier lines shown first, the line
limit, a light/dark repaint, its ✕ and unsubscribing when destroyed. (The window opens it from
the band's Activity button: tests/forge/ui/test_window.py.)"""

from __future__ import annotations

import asyncio
import gc
import logging
import tkinter as tk
from unittest import mock

import customtkinter as ctk

from app.forge.core.activity import ActivityFeed
from app.forge.ui.activity_panel import ActivityPanel
from app.forge.ui.look import forge_look
from app.i18n import Language, current_language, set_language
from app.ui import theme
from tests.forge.base import ForgeAsyncTestCase
from tests.forge.tk import pump


class PanelCase(ForgeAsyncTestCase):
    def setUp(self) -> None:
        super().setUp()
        gc.collect()
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        self.addCleanup(ctk.set_appearance_mode, ctk.get_appearance_mode())
        self.addCleanup(set_language, current_language())
        set_language(Language.EN)
        self.feed = ActivityFeed()

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0

    def _destroy_root(self) -> None:
        # The children first: CTkTextbox keeps the names of its own `after` jobs and fails to
        # delete them once the root's after_cancel below has.
        for child in list(self.root.winfo_children()):
            child.destroy()
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    def panel(self) -> ActivityPanel:
        panel = ActivityPanel(self.root, self.feed)
        panel.pack(fill="both", expand=True)
        return panel

    def tagged(self, panel: ActivityPanel, tag: str) -> str:
        box = panel._box  # pyright: ignore[reportPrivateUsage]
        ranges = box.tag_ranges(tag)
        return "".join(str(box.get(a, b)) for a, b in zip(ranges[::2], ranges[1::2]))


class ActivityPanelTests(PanelCase):
    async def test_a_titled_panel_with_a_close_button(self) -> None:
        closed: list[int] = []
        panel = ActivityPanel(self.root, self.feed, on_close=lambda: closed.append(1))
        panel.pack()
        self.assertEqual(panel.title_label.cget("text"), "Activity")
        panel.close_button.invoke()
        self.assertEqual(closed, [1])
        self.assertEqual(tuple(panel.cget("fg_color")), forge_look().SURFACE)

    async def test_dutch_title(self) -> None:
        set_language(Language.NL)
        self.assertEqual(self.panel().title_label.cget("text"), "Activiteit")

    async def test_shows_lines(self) -> None:
        panel = self.panel()
        self.feed.say("boom happened", level=logging.ERROR)
        await pump(self.root, 0.05)
        self.assertIn("boom happened", panel.text())

    async def test_earlier_lines_first(self) -> None:
        self.feed.say("before the window")
        panel = self.panel()
        self.feed.say("after it")
        await pump(self.root, 0.05)
        text = panel.text()
        self.assertLess(text.index("before the window"), text.index("after it"))

    async def test_colours(self) -> None:
        panel = self.panel()
        self.feed.say("bad thing", level=logging.ERROR)
        self.feed.say("odd thing", level=logging.WARNING)
        self.feed.say("good thing", success=True)
        self.feed.say("plain thing")
        await pump(self.root, 0.05)
        self.assertIn("bad thing", self.tagged(panel, "error"))
        self.assertIn("odd thing", self.tagged(panel, "warning"))
        self.assertIn("good thing", self.tagged(panel, "success"))
        for tag in ("error", "warning", "success"):
            self.assertNotIn("plain thing", self.tagged(panel, tag))

    async def test_tags_repaint_on_mode_switch(self) -> None:
        ctk.set_appearance_mode("light")
        panel = self.panel()
        box = panel._box  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(str(box.tag_cget("error", "foreground")), theme.DANGER[0])
        ctk.set_appearance_mode("dark")
        await pump(self.root, 0.05)
        self.assertEqual(str(box.tag_cget("error", "foreground")), theme.DANGER[1])
        self.assertEqual(str(box.tag_cget("success", "foreground")), theme.SUCCESS[1])

    async def test_line_limit(self) -> None:
        with mock.patch("app.forge.ui.activity_panel.MAX_LINES", 5):
            panel = self.panel()
            for i in range(12):
                self.feed.say(f"line {i}")
            await pump(self.root, 0.05)
        lines = panel.text().strip().splitlines()
        self.assertEqual(lines, [f"line {i}" for i in range(7, 12)])

    async def test_clear(self) -> None:
        panel = self.panel()
        self.feed.say("something")
        await pump(self.root, 0.05)
        panel.clear()
        self.assertEqual(panel.text().strip(), "")

    async def test_destroy_unsubscribes(self) -> None:
        panel = self.panel()
        self.feed.say("pending")  # flush still scheduled
        panel.destroy()
        self.feed.say("after it went")  # nobody listens: no error
        await pump(self.root, 0.05)
        self.assertEqual(self.feed._listeners, [])  # pyright: ignore[reportPrivateUsage]
