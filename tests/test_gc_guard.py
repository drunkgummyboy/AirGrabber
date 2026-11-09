"""The GC guard (app/ui/gc_guard.py): automatic collection off, the Tk thread collects on a timer."""

from __future__ import annotations

import gc
import threading
import time
import tkinter as tk
import unittest
from typing import Any

from app.ui import gc_guard


class GcGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = tk.Tk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._close)

    def _close(self) -> None:
        gc_guard.stop()
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()

    def test_collections_run_only_on_the_tk_thread(self) -> None:
        threads: list[int] = []

        def seen(phase: str, _info: dict[str, Any]) -> None:
            if phase == "start":
                threads.append(threading.get_ident())

        gc.callbacks.append(seen)
        self.addCleanup(gc.callbacks.remove, seen)
        gc_guard.start(self.root, tick_ms=5)
        self.assertFalse(gc.isenabled())
        threads.clear()

        def busy() -> None:  # a worker making plenty of cycles, as a busy thread would
            for _ in range(50_000):
                one: list[Any] = []
                one.append(one)

        worker = threading.Thread(target=busy)
        worker.start()
        worker.join()
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            self.root.update()
        self.assertTrue(threads)  # the timer collected
        self.assertEqual(set(threads), {threading.get_ident()})  # and only the Tk thread did

    def test_stop_gives_python_its_own_collecting_back(self) -> None:
        gc_guard.start(self.root, tick_ms=5)
        gc_guard.stop()
        self.assertTrue(gc.isenabled())

    def test_the_timer_ends_with_the_window(self) -> None:
        gc_guard.start(self.root, tick_ms=5)
        self.root.update()
        other = tk.Toplevel(self.root)
        other.destroy()
        for _ in range(5):
            self.root.update()
            time.sleep(0.01)
        self.assertTrue(self.root.tk.splitlist(self.root.tk.call("after", "info")))


if __name__ == "__main__":
    unittest.main()
