"""Hover tips: where a tip goes (pure), and showing / hiding on an invisible Tk root."""

from __future__ import annotations

import time
import tkinter as tk
import unittest

import customtkinter as ctk

from app.ui import tooltip
from app.ui.tooltip import add_tooltip, tip_position

SCREEN = (0, 0, 1920, 1080)


class TipPositionTests(unittest.TestCase):
    def test_centred_below_the_widget(self) -> None:
        # widget 100..140 wide at y 200..240; tip 60×30
        self.assertEqual(tip_position((100, 200, 140, 240), (60, 30), SCREEN, 8), (90, 248))

    def test_flips_above_when_no_room_below(self) -> None:
        self.assertEqual(
            tip_position((100, 1030, 140, 1070), (60, 30), SCREEN, 8), (90, 1030 - 8 - 30)
        )

    def test_fits_exactly_at_the_bottom_stays_below(self) -> None:
        self.assertEqual(tip_position((100, 1000, 140, 1042), (60, 30), SCREEN, 8), (90, 1050))

    def test_clamped_at_the_right_edge(self) -> None:
        self.assertEqual(tip_position((1890, 10, 1920, 40), (200, 30), SCREEN, 8), (1720, 48))

    def test_clamped_at_the_left_edge(self) -> None:
        self.assertEqual(tip_position((0, 10, 20, 40), (200, 30), SCREEN, 8), (0, 48))

    def test_second_screen_keeps_its_own_bounds(self) -> None:
        screen = (1920, 0, 3840, 1080)  # a monitor right of the primary one
        self.assertEqual(tip_position((1920, 10, 1940, 40), (200, 30), screen, 8), (1920, 48))
        self.assertEqual(tip_position((3830, 10, 3840, 40), (200, 30), screen, 8), (3640, 48))

    def test_never_above_the_screen_top(self) -> None:
        # Too tall for either side of a widget filling the screen: pinned to the top.
        self.assertEqual(tip_position((0, 0, 100, 1080), (60, 30), SCREEN, 8), (20, 0))


class TooltipTkTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        # Tk only delivers Enter/Leave to mapped widgets: show the root, invisible and off-screen.
        self.root.attributes("-alpha", 0.0)
        self.root.geometry("200x200+-3000+-3000")
        self.addCleanup(self._destroy_root)
        self.button = ctk.CTkButton(self.root, text="⚙", width=40)
        self.button.pack()
        self.other = ctk.CTkButton(self.root, text="◀", width=40)
        self.other.pack()
        self.root.update()

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()

    def pump(self, ms: int) -> None:
        end = time.monotonic() + ms / 1000
        while time.monotonic() < end:
            self.root.update()
            time.sleep(0.01)

    def tip_window_shown(self) -> bool:
        window = tooltip._manager._window
        if window is None or not window.belongs_to(self.root):
            return False
        return bool(window._top.winfo_ismapped())

    def hover(self, widget: ctk.CTkButton) -> None:
        widget.event_generate("<Enter>")
        self.pump(tooltip.DELAY_MS + 200)

    def test_hover_shows_after_the_delay_and_leave_hides(self) -> None:
        tip = add_tooltip(self.button, "Settings")
        self.button.event_generate("<Enter>")
        self.pump(tooltip.DELAY_MS // 3)
        self.assertFalse(tip.visible)  # not yet
        self.pump(tooltip.DELAY_MS)
        self.assertTrue(tip.visible)
        self.assertTrue(self.tip_window_shown())
        assert tooltip._manager._window is not None
        self.assertEqual(tooltip._manager._window._label.cget("text"), "Settings")
        self.button.event_generate("<Leave>")
        self.root.update()
        self.assertFalse(tip.visible)
        self.assertFalse(self.tip_window_shown())

    def test_leave_before_the_delay_shows_nothing(self) -> None:
        tip = add_tooltip(self.button, "Settings")
        self.button.event_generate("<Enter>")
        self.pump(50)
        self.button.event_generate("<Leave>")
        self.pump(tooltip.DELAY_MS + 200)
        self.assertFalse(tip.visible)
        self.assertFalse(self.tip_window_shown())

    def test_click_hides(self) -> None:
        clicks: list[int] = []
        self.button.configure(command=lambda: clicks.append(1))
        tip = add_tooltip(self.button, "Settings")
        self.hover(self.button)
        self.assertTrue(tip.visible)
        self.button._canvas.event_generate("<ButtonPress-1>")
        self.root.update()
        self.assertFalse(tip.visible)

    def test_destroying_the_widget_hides_and_cancels(self) -> None:
        tip = add_tooltip(self.button, "Settings")
        self.hover(self.button)
        self.assertTrue(tip.visible)
        self.button.destroy()
        self.root.update()
        self.assertFalse(self.tip_window_shown())
        # A pending tip of a destroyed widget leaves no timer behind.
        other = add_tooltip(self.other, "Previous week")
        self.other.event_generate("<Enter>")
        self.other.destroy()
        self.assertIsNone(other._job)
        self.pump(tooltip.DELAY_MS + 200)
        self.assertFalse(self.tip_window_shown())

    def test_one_tip_at_a_time(self) -> None:
        first = add_tooltip(self.button, "Settings")
        second = add_tooltip(self.other, "Previous week")
        self.hover(self.button)
        self.assertTrue(first.visible)
        self.hover(self.other)
        self.assertFalse(first.visible)
        self.assertTrue(second.visible)

    def test_empty_text_is_no_tip_and_set_text_switches(self) -> None:
        tip = add_tooltip(self.button, "")
        self.hover(self.button)
        self.assertFalse(tip.visible)
        self.button.event_generate("<Leave>")
        tip.set_text("Download")
        self.hover(self.button)
        self.assertTrue(tip.visible)
        tip.set_text("")
        self.assertFalse(tip.visible)

    def test_keeps_the_widgets_own_bindings(self) -> None:
        seen: list[str] = []
        tk.Misc.bind(self.button, "<Enter>", lambda _e: seen.append("enter"), add="+")
        add_tooltip(self.button, "Settings")
        self.button.event_generate("<Enter>")
        self.root.update()
        self.assertEqual(seen, ["enter"])


if __name__ == "__main__":
    unittest.main()
