"""A window opened from a search result comes to the front once it shows (app/ui/window_front.py),
and the search window's openers go through it."""

from __future__ import annotations

import gc
import time
import tkinter as tk
import unittest
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from app.ui import window_front
from app.ui.search import SearchResultsWindow
from app.ui.window_front import _focused_window, open_in_front


class OpenInFrontTests(unittest.TestCase):
    def setUp(self) -> None:
        gc.collect()
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)
        for name, value in (("SETTLE_MS", 1), ("POLL_MS", 1)):
            patcher = patch.object(window_front, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.opener = tk.Toplevel(self.root)
        self.opener.withdraw()
        self.calls: list[str] = []
        self.opened: tk.Toplevel | None = None

    def open_hidden(self) -> None:
        """As a CTkToplevel opens: hidden at first, shown a little later."""
        window = self.opened = tk.Toplevel(self.root)
        window.withdraw()
        window.lift = lambda *_args: self.calls.append("lift")  # type: ignore[method-assign]
        window.focus_force = lambda: self.calls.append("focus")  # type: ignore[method-assign]

    def pump(self, seconds: float = 0.15) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.root.update()
            time.sleep(0.005)

    def test_raised_and_focused_only_once_it_shows(self) -> None:
        open_in_front(self.opener, self.open_hidden)
        self.pump()
        self.assertEqual(self.calls, [])  # still hidden: lifting it now would be lost
        assert self.opened is not None
        self.opened.deiconify()
        self.pump()
        self.assertEqual(self.calls, ["lift", "focus"])

    def test_a_window_closed_before_it_shows_is_left_alone(self) -> None:
        open_in_front(self.opener, self.open_hidden)
        assert self.opened is not None
        self.opened.destroy()
        self.pump()
        self.assertEqual(self.calls, [])

    def test_nothing_opened_and_no_other_window_focused(self) -> None:
        with patch.object(window_front, "bring_to_front") as bring:
            open_in_front(self.opener, lambda: None)
        bring.assert_not_called()

    def test_an_open_window_that_took_the_focus_is_brought_forward(self) -> None:
        other = tk.Toplevel(self.root)
        with patch.object(self.root, "focus_get", return_value=other):
            self.assertIs(_focused_window(self.root, self.opener), other)
        for focused in (self.opener, self.root, None):  # the opener's own, the main window's
            with patch.object(self.root, "focus_get", return_value=focused):
                self.assertIsNone(_focused_window(self.root, self.opener))


class SearchWindowWiringTests(unittest.TestCase):
    def test_openers_go_through_open_in_front(self) -> None:
        opened: list[Any] = []
        window: Any = SimpleNamespace()
        with patch("app.ui.search.open_in_front") as front:
            wrapped = SearchResultsWindow._in_front(window, opened.append)
            wrapped("Severance")
            front.assert_called_once()
            owner, open_window = front.call_args.args
            self.assertIs(owner, window)
            open_window()
        self.assertEqual(opened, ["Severance"])


if __name__ == "__main__":
    unittest.main()
