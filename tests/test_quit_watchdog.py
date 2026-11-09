"""Quitting always ends AirGrabber: the watchdog (app/ui/quit_watchdog.py) and the main window's
last step, which takes a window that fails to go down at Tk's level."""

from __future__ import annotations

import threading
import tkinter as tk
import unittest
from types import SimpleNamespace
from typing import Any, cast

from app.ui import quit_watchdog
from app.ui.main_window import MainWindow


class WatchdogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.addCleanup(quit_watchdog.disarm)
        self.ended = threading.Event()
        self.codes: list[int] = []

    def end(self, code: int) -> None:
        self.codes.append(code)
        self.ended.set()

    def test_a_stuck_close_is_ended_with_where_it_was(self) -> None:
        with self.assertLogs("app.ui.quit_watchdog", "ERROR") as logs:
            quit_watchdog.arm(0.05, end=self.end)
            self.assertTrue(self.ended.wait(5))
        self.assertEqual(self.codes, [quit_watchdog.EXIT_CODE])
        self.assertIn("Thread MainThread", logs.output[0])  # where every thread was
        self.assertIn("test_quit_watchdog.py", logs.output[0])

    def test_a_close_that_finishes_disarms_it(self) -> None:
        quit_watchdog.arm(0.2, end=self.end)
        self.assertTrue(quit_watchdog.armed())
        quit_watchdog.disarm()
        self.assertFalse(quit_watchdog.armed())
        self.assertFalse(self.ended.wait(0.5))

    def test_arming_twice_keeps_the_first_deadline(self) -> None:
        quit_watchdog.arm(0.05, end=self.end)
        quit_watchdog.arm(60, end=lambda _code: None)  # Quit, then main.py's shutdown
        self.assertTrue(self.ended.wait(5))
        self.assertEqual(self.codes, [quit_watchdog.EXIT_CODE])


class DestroyForGoodTests(unittest.TestCase):
    """MainWindow._destroy_for_good on a stand-in window."""

    def window(self, *, exists: bool = True, fails: BaseException | None = None) -> Any:
        calls: list[Any] = []

        def destroy() -> None:
            calls.append("destroy")
            if fails is not None:
                raise fails

        return SimpleNamespace(
            calls=calls,
            winfo_exists=lambda: exists,
            destroy=destroy,
            tk=SimpleNamespace(call=lambda *args: calls.append(args)),
        )

    def finish(self, window: Any) -> None:
        MainWindow._destroy_for_good(cast(MainWindow, window))

    def test_a_window_goes(self) -> None:
        window = self.window()
        self.finish(window)
        self.assertEqual(window.calls, ["destroy"])

    def test_a_window_gone_already_is_left_alone(self) -> None:
        window = self.window(exists=False)
        self.finish(window)
        self.assertEqual(window.calls, [])

    def test_a_window_that_fails_to_go_is_ended_at_tks_level(self) -> None:
        # Before, it stayed half-destroyed: empty, the loop running, Quit doing nothing.
        for error in (tk.TclError("can't delete Tcl command"), RuntimeError("a page's own")):
            with self.subTest(error=type(error).__name__):
                window = self.window(fails=error)
                with self.assertLogs("app.ui.main_window", "WARNING"):
                    self.finish(window)
                self.assertEqual(window.calls, ["destroy", ("destroy", ".")])


if __name__ == "__main__":
    unittest.main()
