"""Pop-ups at a Windows scaling change: customtkinter's ScalingTracker registers the plain Tk
window a pop-up's CTk widgets sit in and, at a new scaling, makes it see-through (alpha 0.15),
calls its `block_update_dimensions_event`, rescales its widgets and makes it solid again. A
plain Toplevel lacked that method: the AttributeError left the pop-up see-through and ended
CTk's scaling checks for the rest of the run. PlainPopup (app/ui/plain_popup.py) has it, and
the app's pop-ups (PopupMenu, the hover tips, the spotlight band's tips) are made from it."""

from __future__ import annotations

import tkinter as tk
import unittest
from typing import Any
from unittest import mock

import customtkinter as ctk

from app.ui import tooltip
from app.ui.missing_control import popup_menu
from app.ui.plain_popup import PlainPopup
from app.ui.spotlight_band import _Tip


class PlainPopupTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()

    def test_a_dpi_check_with_a_popup_open_keeps_going(self) -> None:
        tracker: Any = ctk.ScalingTracker
        popup = PlainPopup(self.root)
        self.addCleanup(popup.destroy)  # before the root's: it takes its own after() jobs along
        ctk.CTkLabel(popup, text="Tip").pack()  # registers the popup's window with the tracker
        self.assertIn(popup, tracker.window_widgets_dict)
        before = tracker.window_dpi_scaling_dict[popup]
        # Only this window is checked (windows of earlier tests may be gone), its scaling has
        # changed, and the check's next round is recorded instead of scheduled.
        only = {popup: tracker.window_widgets_dict[popup]}
        with (
            mock.patch.dict(tracker.window_widgets_dict, only, clear=True),
            mock.patch.dict(tracker.window_dpi_scaling_dict),
            mock.patch.object(tracker, "get_window_dpi_scaling", return_value=before + 0.5),
            mock.patch.object(popup, "after") as after,
        ):
            tracker.check_dpi_scaling()  # raised AttributeError on a plain Toplevel
            self.assertEqual(tracker.window_dpi_scaling_dict[popup], before + 0.5)
        self.assertEqual(float(popup.attributes("-alpha")), 1.0)  # solid again
        after.assert_called_once()  # the checks go on
        self.assertEqual(after.call_args.args[1], tracker.check_dpi_scaling)

    def test_the_apps_popups_are_plain_popups(self) -> None:
        anchor = ctk.CTkButton(self.root, text="⋯")
        anchor.pack()
        menu = popup_menu(anchor, [("Open", lambda: None)])
        assert menu is not None
        self.addCleanup(menu.close)
        self.assertIsInstance(menu._top, PlainPopup)
        tip = tooltip._TipWindow(self.root)
        self.addCleanup(tip.destroy)
        self.assertIsInstance(tip._top, PlainPopup)
        band_tip = _Tip(self.root)
        band_tip._build()
        self.assertIsInstance(band_tip._top, PlainPopup)


if __name__ == "__main__":
    unittest.main()
