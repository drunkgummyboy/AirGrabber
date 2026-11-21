"""The player's media keys (app/media_keys.py): the window procedure hands a hot key on by its
name and passes everything else to the window's own; on a real Tk window, attaching subclasses
it and detaching puts its own procedure back. No key is ever pressed."""

from __future__ import annotations

import sys
import tkinter as tk
import unittest

from app import media_keys


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class WindowsMediaKeysTests(unittest.TestCase):
    def test_a_hot_key_goes_out_by_its_name(self) -> None:
        keys = media_keys.WindowsMediaKeys()
        heard: list[str] = []
        keys._original = 1  # as if attached (no window: nothing reaches Windows)
        keys._deliver = heard.append
        self.assertEqual(keys._window_proc(None, media_keys.WM_HOTKEY, 0xA101, 0), 0)
        self.assertEqual(keys._window_proc(None, media_keys.WM_HOTKEY, 0xA102, 0), 0)
        self.assertEqual(heard, ["play_pause", "next"])

    def test_attach_and_detach_on_a_real_window(self) -> None:
        try:
            root = tk.Tk()
        except tk.TclError as exc:
            self.skipTest(f"no display: {exc}")
        self.addCleanup(root.destroy)
        root.withdraw()
        frame = tk.Frame(root)
        frame.pack()
        root.update_idletasks()
        window = frame.winfo_id()
        keys = media_keys.WindowsMediaKeys()
        user32 = keys._user32
        own = int(user32.GetWindowLongPtrW(window, media_keys.GWLP_WNDPROC))
        self.assertTrue(keys.attach(window, lambda _name: None))
        self.assertNotEqual(int(user32.GetWindowLongPtrW(window, media_keys.GWLP_WNDPROC)), own)
        root.update()  # the window still answers through its own procedure
        keys.detach()
        self.assertEqual(int(user32.GetWindowLongPtrW(window, media_keys.GWLP_WNDPROC)), own)
        self.assertEqual(keys._registered, [])

    def test_on_windows_there_are_some(self) -> None:
        self.assertIsNotNone(media_keys.windows_media_keys())


if __name__ == "__main__":
    unittest.main()
