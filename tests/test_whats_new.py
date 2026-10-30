"""What's new after an update: which notes show, the dialog, and waiting for the window when
AirGrabber started hidden in the tray."""

from __future__ import annotations

import gc
import tkinter as tk
import unittest
from typing import Any

import customtkinter as ctk

from app.ui.main_window import MainWindow
from app.ui.whats_new_dialog import WhatsNewDialog
from app.whats_new import NOTES, WhatsNew, dialog_title, whats_new

FAKE_NOTES = {
    "2.0.5": ("Five a", "Five b", "Five c"),
    "2.0.7": ("Seven a", "Seven b", "Seven c"),
    "2.1": ("Two one a", "Two one b", "Two one c"),
}


class WhatsNewTests(unittest.TestCase):
    def test_after_an_update_the_newest_entry_up_to_this_version(self) -> None:
        self.assertEqual(
            whats_new("2.0.4", "2.0.5", FAKE_NOTES), WhatsNew("2.0.5", FAKE_NOTES["2.0.5"])
        )
        self.assertEqual(whats_new("2.0.4", "2.0.9", FAKE_NOTES), WhatsNew("2.0.7", FAKE_NOTES["2.0.7"]))
        self.assertEqual(whats_new("2.0.4", "2.1.0", FAKE_NOTES), WhatsNew("2.1", FAKE_NOTES["2.1"]))

    def test_nothing_without_a_new_entry(self) -> None:
        self.assertIsNone(whats_new("2.0.3", "2.0.4", FAKE_NOTES))  # no entry for it
        self.assertIsNone(whats_new("2.0.5", "2.0.6", FAKE_NOTES))  # 2.0.5's were shown already

    def test_nothing_on_a_first_start_or_without_a_newer_version(self) -> None:
        self.assertIsNone(whats_new("", "2.0.5", FAKE_NOTES))  # first start: the welcome shows
        self.assertIsNone(whats_new("2.0.5", "2.0.5", FAKE_NOTES))
        self.assertIsNone(whats_new("2.0.7", "2.0.5", FAKE_NOTES))  # went back a version
        self.assertIsNone(whats_new("rubbish", "2.0.5", FAKE_NOTES))

    def test_the_release_notes(self) -> None:
        for version, lines in NOTES.items():
            with self.subTest(version):
                self.assertTrue(3 <= len(lines) <= 6)
                for line in lines:
                    self.assertLessEqual(len(line), 120)
                    self.assertTrue(line.endswith("."), line)
        self.assertEqual(whats_new("2.0.4", "2.0.5"), WhatsNew("2.0.5", NOTES["2.0.5"]))

    def test_title(self) -> None:
        self.assertEqual(dialog_title("2.0.5"), "What's new in AirGrabber 2.0.5")


class WhatsNewWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        gc.collect()
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)

    def _destroy_root(self) -> None:
        for dialog in self.dialogs():  # first: their own timers belong to them
            dialog.destroy()
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    def dialogs(self) -> list[WhatsNewDialog]:
        return [child for child in self.root.winfo_children() if isinstance(child, WhatsNewDialog)]

    def texts(self, widget: Any) -> list[str]:
        found: list[str] = []
        for child in widget.winfo_children():
            if isinstance(child, ctk.CTkLabel):
                found.append(str(child.cget("text")))
            found.extend(self.texts(child))
        return found

    def test_the_dialog(self) -> None:
        dialog = WhatsNewDialog(self.root, version="2.0.5", lines=("One.", "Two.", "Three."))
        self.root.update()
        self.assertEqual(dialog.title(), "What's new in AirGrabber 2.0.5")
        texts = self.texts(dialog)
        self.assertIn("What's new in AirGrabber 2.0.5", texts)
        for line in ("One.", "Two.", "Three."):
            self.assertIn(line, texts)
        # Tk delivers a generated key press to the window with the keyboard focus, which Windows
        # withholds while another program is in front (a desktop in use while the tests run).
        self.assertTrue(dialog.bind("<Escape>"))
        dialog.focus_force()
        self.root.update()
        if dialog.focus_get() is None:
            self.skipTest("no keyboard focus for the test window")
        dialog.event_generate("<Escape>")
        self.root.update()
        self.assertEqual(self.dialogs(), [])

    def test_started_hidden_it_waits_for_the_window(self) -> None:
        window: Any = self.root
        window._whats_new_binding = None
        MainWindow._show_whats_new(window, WhatsNew("2.0.5", ("One.", "Two.", "Three.")))
        self.root.update()
        self.assertEqual(self.dialogs(), [])  # the window is still hidden in the tray
        self.assertIsNotNone(window._whats_new_binding)
        self.root.deiconify()  # opened from the tray
        self.root.update()
        self.assertEqual(len(self.dialogs()), 1)
        self.assertIsNone(window._whats_new_binding)
        self.root.withdraw()
        self.root.deiconify()  # only once
        self.root.update()
        self.assertEqual(len(self.dialogs()), 1)


if __name__ == "__main__":
    unittest.main()
