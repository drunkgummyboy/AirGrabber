"""What a closed window leaves behind: nothing, however long AirGrabber runs.

- Window icons are read from their .ico once (app/ui/branding.py): AirGrabber's as the root's
  default, MediaForge's as Tk images made once per root. Windows' GDI and USER handle counts
  stay flat as windows open and close (Tk never gives back an icon it read from a file, and a
  process gets 10,000 of each: a long session ended in "Fail to allocate bitmap").
- A closed window is freed while the app runs on: customtkinter forgets it
  (`install_window_release`, app/ui/widgets.py), ScrollableFrame takes its bindings on "all"
  back (app/ui/scrollable.py), and the windows remove their variable traces (Settings, the TMDB
  key guide here; Forge's Naming page and Inbox in tests/forge/ui).
- No app code uses customtkinter's own CTkScrollableFrame.

tests/__init__.py keeps .ico files unread in every other test; the handle test here puts Tk's
own `wm iconbitmap` back."""

from __future__ import annotations

import ast
import asyncio
import ctypes
import gc
import sys
import tkinter as tk
import unittest
import weakref
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import customtkinter as ctk

from app.forge.ui.confirm_window import ICON_FILE as FORGE_ICON
from app.models import Movie, Settings, TitleExtras
from app.services.settings import SettingsService
from app.ui import branding
from app.ui.branding import ICON_FILE, OWN_ICON_MAX, set_own_icon, set_window_icon
from app.ui.download_control import DownloadActions, DownloadStates
from app.ui.movie_details import MovieWindows
from app.ui.scrollable import ScrollableFrame
from app.ui.settings_dialog import SettingsDialog
from app.ui.tmdb_key_guide import TmdbKeyDialog
from app.ui.widgets import install_window_release
from tests import REAL_ICONBITMAP
from tests.test_card_size import RecordingImages
from tests.test_info_window import CAST, FILM, INFO, TRAILER, FakeDiscovery, FakeImages, FakeWatchlist
from tests.test_settings_dialog import FakeProwlarr, FakeRepository, FakeShortcut

PROJECT = Path(__file__).resolve().parents[1]
WINDOWS = sys.platform == "win32"
SHIFT_KEYS = ("<KeyPress-Shift_L>", "<KeyPress-Shift_R>", "<KeyRelease-Shift_L>", "<KeyRelease-Shift_R>")


def handles() -> tuple[int, int]:
    """This process's GDI and USER objects."""
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32")
    kernel32 = ctypes.WinDLL("kernel32")
    user32.GetGuiResources.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    user32.GetGuiResources.restype = wintypes.DWORD
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    process = kernel32.GetCurrentProcess()
    return int(user32.GetGuiResources(process, 0)), int(user32.GetGuiResources(process, 1))


class RootCase(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)

    def _destroy_root(self) -> None:
        for child in list(self.root.winfo_children()):
            try:
                child.destroy()
            except tk.TclError:
                pass
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    def window(self) -> ctk.CTkToplevel:
        window = ctk.CTkToplevel(self.root)
        window.withdraw()
        return window


@unittest.skipUnless(WINDOWS, "window icons are set on Windows only")
class IconTests(RootCase):
    def record_icons(self) -> list[tuple[str, object, object]]:
        """Every `wm iconbitmap` from now on: (window, bitmap, default)."""
        calls: list[tuple[str, object, object]] = []
        current = tk.Wm.wm_iconbitmap  # tests/__init__.py's: nothing is read

        def recording(window: tk.Wm, bitmap: object = None, default: object = None) -> Any:
            calls.append((str(window), bitmap, default))
            return current(window, bitmap, default)  # type: ignore[no-untyped-call]

        self.enterContext(mock.patch.object(tk.Wm, "wm_iconbitmap", recording))
        return calls

    def test_the_root_reads_airgrabbers_icon_once_and_its_windows_take_it(self) -> None:
        calls = self.record_icons()
        set_window_icon(self.root)
        windows = [self.window() for _ in range(3)]
        for window in windows:
            set_window_icon(window)
        self.assertEqual(
            calls,
            [(".", None, str(ICON_FILE))] + [(str(window), "", None) for window in windows],
        )

    def test_a_window_first_gives_its_root_the_default(self) -> None:
        calls = self.record_icons()
        window = self.window()
        set_window_icon(window)
        set_window_icon(self.window())
        self.assertEqual(calls[:2], [(".", None, str(ICON_FILE)), (str(window), "", None)])
        self.assertEqual(len(calls), 3)  # the second window: nothing read

    def test_an_own_icon_is_read_once_and_made_once_per_root(self) -> None:
        branding._icon_frames.cache_clear()
        calls = self.record_icons()
        given: list[tuple[str, ...]] = []
        real = tk.Wm.wm_iconphoto

        def recording(window: tk.Wm, default: bool, *images: Any) -> Any:
            given.append(tuple(str(image) for image in images))
            return real(window, default, *images)

        self.enterContext(mock.patch.object(tk.Wm, "wm_iconphoto", recording))
        windows = [self.window() for _ in range(3)]
        for window in windows:
            set_own_icon(window, FORGE_ICON)
        set_own_icon(windows[0], FORGE_ICON)  # again
        self.assertEqual(branding._icon_frames.cache_info().misses, 1)
        self.assertEqual(len(given), 4)
        self.assertEqual(len(set(given)), 1)  # the same Tk images every time
        sizes = [
            (int(self.root.tk.call("image", "width", name)), int(self.root.tk.call("image", "height", name)))
            for name in given[0]
        ]
        self.assertTrue(sizes)
        self.assertTrue(all(max(size) <= OWN_ICON_MAX for size in sizes), sizes)
        # Nothing read through wm iconbitmap: only the empty one, which stops CTk's own icon.
        self.assertEqual({bitmap for _window, bitmap, _default in calls}, {""})

    def test_windows_opening_and_closing_leave_no_handles(self) -> None:
        self.enterContext(mock.patch.object(tk.Wm, "wm_iconbitmap", REAL_ICONBITMAP))
        set_window_icon(self.root)  # AirGrabber's icon, read here once
        self.root.update()
        set_own_icon(self.window(), FORGE_ICON)  # MediaForge's, read and made once
        self.root.update()
        gc.collect()
        before = handles()
        for _ in range(20):
            for give_icon in (set_window_icon, lambda w: set_own_icon(w, FORGE_ICON)):
                window = ctk.CTkToplevel(self.root)
                give_icon(window)
                window.update()
                window.destroy()
        self.root.update()
        gc.collect()
        gdi, user = handles()
        # Reading an .ico per window cost about 21 GDI and 7 USER objects each time (40 here).
        self.assertLessEqual(gdi - before[0], 4)
        self.assertLessEqual(user - before[1], 4)


class WindowReleaseTests(RootCase):
    def setUp(self) -> None:
        super().setUp()
        install_window_release(self.root)

    def test_a_closed_window_leaves_customtkinters_tables_and_is_freed(self) -> None:
        for make in (lambda: ctk.CTkToplevel(self.root), lambda: tk.Toplevel(self.root)):
            window = make()
            ctk.CTkButton(window, text="OK").pack()  # a plain window's too (PopupMenu's)
            window.update()
            self.assertIn(window, ctk.ScalingTracker.window_dpi_scaling_dict)
            ref = weakref.ref(window)
            window.destroy()
            del window
            gc.collect()
            self.assertIsNone(ref(), make)
            self.assertNotIn(None, ctk.ScalingTracker.window_dpi_scaling_dict)  # sanity

    def test_a_scrollable_frame_takes_its_bindings_back(self) -> None:
        other = self.root.bind_all("<MouseWheel>", lambda _event: None, add="+")
        window = self.window()
        frame = ScrollableFrame(window)
        frame.pack()
        window.update()
        self.assertIn("_mouse_wheel_all", self.root.bind_all("<MouseWheel>"))
        ref = weakref.ref(frame)
        window.destroy()
        del frame, window
        gc.collect()
        self.assertIsNone(ref())
        wheel = self.root.bind_all("<MouseWheel>")
        self.assertNotIn("_mouse_wheel_all", wheel)
        self.assertIn(other, wheel)  # the others stay
        for sequence in SHIFT_KEYS:
            self.assertNotIn("_keyboard_shift_", self.root.bind_all(sequence))

    def test_no_app_code_uses_customtkinters_own_scrollable_frame(self) -> None:
        found: list[str] = []
        for path in sorted((PROJECT / "app").rglob("*.py")):
            if path == PROJECT / "app" / "ui" / "scrollable.py":
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                used: list[ast.expr] = []
                if isinstance(node, ast.Call):
                    used = [node.func]
                elif isinstance(node, ast.ClassDef):
                    used = list(node.bases)
                for expr in used:
                    if isinstance(expr, ast.Attribute) and expr.attr == "CTkScrollableFrame":
                        found.append(f"{path.relative_to(PROJECT)}:{expr.lineno}")
        self.assertEqual(found, [], "use app.ui.scrollable.ScrollableFrame")


class ClosedWindowTests(unittest.IsolatedAsyncioTestCase):
    """The real windows, closed while the root (AirGrabber's main window) stays."""

    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(RootCase._destroy_root, self)  # type: ignore[arg-type]
        install_window_release(self.root)

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0  # building a window is slow

    async def settle(self) -> None:
        for _ in range(5):
            await asyncio.sleep(0.01)
            self.root.update()

    async def assert_freed(self, ref: weakref.ReferenceType[Any]) -> None:
        await self.settle()
        gc.collect()
        self.assertIsNone(ref(), "the closed window is still held")

    def settings(self) -> SettingsService:
        return SettingsService(
            FakeRepository(),  # type: ignore[arg-type]
            Settings(download_dir=Path.home() / "Downloads"),
        )

    async def test_settings(self) -> None:
        dialog = SettingsDialog(
            self.root,
            self.settings(),
            search=mock.MagicMock(),
            station=mock.MagicMock(),
            updates=SimpleNamespace(current="3.0.0", previous_version=None),  # type: ignore[arg-type]
            torrent_app=mock.MagicMock(find=mock.AsyncMock(return_value=None)),
            discovery=mock.MagicMock(check_tmdb_key=mock.AsyncMock(return_value=True)),
            prowlarr=FakeProwlarr(),  # type: ignore[arg-type]
            startup_shortcut=FakeShortcut(),
            images=RecordingImages(),  # type: ignore[arg-type]  # the Card size box (a trace)
        )
        await self.settle()
        dialog._tmdb_key.set("abc")  # the TMDB key's trace runs
        ref = weakref.ref(dialog)
        dialog.destroy()
        del dialog
        await self.assert_freed(ref)

    async def test_the_tmdb_key_guide(self) -> None:
        dialog = TmdbKeyDialog(
            self.root,
            settings=self.settings(),
            discovery=mock.MagicMock(check_tmdb_key=mock.AsyncMock(return_value=True)),
        )
        await self.settle()
        ref = weakref.ref(dialog)
        dialog.destroy()
        del dialog
        await self.assert_freed(ref)

    async def test_a_film_info_window(self) -> None:
        discovery: Any = FakeDiscovery(TitleExtras(info=INFO, trailer=TRAILER, cast=CAST))
        windows = MovieWindows(
            self.root,
            images=FakeImages(),  # type: ignore[arg-type]
            discovery=discovery,
            watchlist=FakeWatchlist(),  # type: ignore[arg-type]
            actions=DownloadActions[Movie](
                download=lambda _movie: None, choose=lambda _movie: None, states=DownloadStates()
            ),
        )
        self.addCleanup(windows.close)
        windows.open_movie(FILM)
        await self.settle()
        [window] = windows._live().values()
        ref = weakref.ref(window)
        window.destroy()
        del window
        await self.settle()
        windows._live()  # MovieWindows lets go of a closed window the next time it looks
        await self.assert_freed(ref)


if __name__ == "__main__":
    unittest.main()
