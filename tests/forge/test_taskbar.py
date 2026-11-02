"""The Forge window's own taskbar button (app/forge/ui/taskbar.py, D8): the seam's calls with a
fake (set before showing, again for a new wrapper, cleared at the end; the icon after CTk's), the
Start-menu check once per run, and the real property store on a throwaway window."""

from __future__ import annotations

import asyncio
import ctypes
import gc
import inspect
import sys
import tkinter as tk
import unittest
from pathlib import Path
from unittest import mock

import customtkinter as ctk

from app.forge.shortcut import APP_USER_MODEL_ID, WindowIdentity
from app.forge.ui import taskbar
from app.forge.ui.taskbar import (
    APP_ID,
    ICON,
    ICON_DELAY_MS,
    RELAUNCH_COMMAND,
    RELAUNCH_ICON,
    RELAUNCH_NAME,
    Values,
    attach,
    cleared_values,
    identity_values,
)
from tests.forge.base import ForgeAsyncTestCase
from tests.forge.tk import pump

PROJECT = Path(__file__).resolve().parents[2]
IDENTITY = WindowIdentity(
    app_id=APP_USER_MODEL_ID,
    relaunch_command=r'"C:\Python313\pythonw.exe" "C:\Air Grabber\main.py" --forge',
    display_name="MediaForge",
    icon=r"C:\Air Grabber\app\assets\forge\MediaForge.ico,0",
)


class FakeNative:
    def __init__(self) -> None:
        self.wrapper_id = 0x1234
        self.refuse = False
        self.calls: list[tuple[int, list[tuple[int, str | None]]]] = []

    def wrapper(self, window_id: int) -> int:
        return self.wrapper_id

    def set_properties(self, hwnd: int, values: Values) -> bool:
        self.calls.append((hwnd, list(values)))
        return not self.refuse


class TkCase(ForgeAsyncTestCase):
    def setUp(self) -> None:
        super().setUp()
        gc.collect()
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
        window.title("taskbar test")
        window.geometry("200x120+20+20")
        window.attributes("-alpha", 0.0)  # mapped, but see-through
        return window


class ValuesTests(unittest.TestCase):
    def test_the_relaunch_properties_go_before_the_id(self) -> None:
        self.assertEqual(
            identity_values(IDENTITY),
            [
                (RELAUNCH_COMMAND, IDENTITY.relaunch_command),
                (RELAUNCH_NAME, "MediaForge"),
                (RELAUNCH_ICON, IDENTITY.icon),
                (APP_ID, APP_USER_MODEL_ID),
            ],
        )
        self.assertEqual([pid for pid, _ in cleared_values()], [APP_ID, RELAUNCH_COMMAND, RELAUNCH_NAME, RELAUNCH_ICON])
        self.assertEqual({text for _, text in cleared_values()}, {None})

    def test_the_property_ids(self) -> None:
        # PKEY_AppUserModel_RelaunchCommand / RelaunchIconResource / RelaunchDisplayNameResource / ID
        self.assertEqual((RELAUNCH_COMMAND, RELAUNCH_ICON, RELAUNCH_NAME, APP_ID), (2, 3, 4, 5))

    def test_the_forge_window_attaches_once(self) -> None:
        from app.forge.ui.window import ForgeWindow

        source = inspect.getsource(ForgeWindow.__init__)
        self.assertEqual(source.count("taskbar.attach(self)"), 1)
        # Before the window can show: before the logo is loaded (the last line).
        self.assertLess(source.index("taskbar.attach(self)"), source.index("self._spawn(self._load_logo())"))


class AttachTests(TkCase):
    def setUp(self) -> None:
        super().setUp()
        self.native = FakeNative()
        self.addCleanup(taskbar.set_native, taskbar.set_native(self.native))
        self.enterContext(mock.patch.object(taskbar, "make_shortcut_once", return_value=False))

    async def test_set_on_the_wrapper_before_the_window_shows(self) -> None:
        window = self.window()
        window.withdraw()
        bar = attach(window, identity=IDENTITY)
        assert bar is not None
        self.assertEqual(self.native.calls, [(0x1234, identity_values(IDENTITY))])
        self.assertEqual(bar.hwnd, 0x1234)
        window.deiconify()
        await pump(window, 0.1)
        self.assertEqual(len(self.native.calls), 1)  # the same wrapper: nothing again

    async def test_a_new_wrapper_gets_it_again_on_map(self) -> None:
        window = self.window()
        bar = attach(window, identity=IDENTITY)
        assert bar is not None
        await pump(window, 0.1)
        window.withdraw()
        await pump(window, 0.05)
        self.native.wrapper_id = 0x5678  # Tk replaced the wrapper
        window.deiconify()
        await pump(window, 0.1)
        self.assertEqual(self.native.calls[-1], (0x5678, identity_values(IDENTITY)))
        self.assertEqual(bar.hwnd, 0x5678)

    async def test_a_childs_map_is_not_the_windows(self) -> None:
        window = self.window()
        attach(window, identity=IDENTITY)
        await pump(window, 0.1)  # shown
        self.native.wrapper_id = 0x9999
        label = ctk.CTkLabel(window, text="x")
        label.pack()
        await pump(window, 0.1)
        self.assertTrue(label.winfo_ismapped())
        self.assertNotIn(0x9999, [hwnd for hwnd, _ in self.native.calls])

    async def test_cleared_when_the_window_goes(self) -> None:
        window = self.window()
        attach(window, identity=IDENTITY)
        await pump(window, 0.05)
        window.destroy()
        self.assertEqual(self.native.calls[-1], (0x1234, cleared_values()))

    async def test_refused_is_logged_and_tried_again(self) -> None:
        self.native.refuse = True
        window = self.window()
        window.withdraw()
        with self.assertLogs("app.forge.ui.taskbar", "WARNING"):
            bar = attach(window, identity=IDENTITY)
        assert bar is not None
        self.assertEqual(bar.hwnd, 0)
        self.native.refuse = False
        window.deiconify()
        await pump(window, 0.1)
        self.assertEqual(bar.hwnd, 0x1234)
        window.destroy()
        self.assertEqual(self.native.calls[-1][1], cleared_values())

    async def test_no_wrapper_no_call(self) -> None:
        self.native.wrapper_id = 0
        window = self.window()
        bar = attach(window, identity=IDENTITY)
        assert bar is not None
        self.assertEqual(self.native.calls, [])
        window.destroy()
        self.assertEqual(self.native.calls, [])

    async def test_without_native_nothing_happens(self) -> None:
        taskbar.set_native(None)
        self.assertIsNone(attach(self.window(), identity=IDENTITY))

    async def test_the_icon_after_ctks(self) -> None:
        window = self.window()
        calls: list[object] = []
        with (
            mock.patch.object(window, "iconbitmap", side_effect=lambda *args: calls.append("CTk's")),
            mock.patch.object(taskbar, "set_own_icon", side_effect=lambda _w, icon: calls.append(icon)),
        ):
            attach(window, identity=IDENTITY)
            await pump(window, (ICON_DELAY_MS + 150) / 1000)
        if sys.platform == "win32":
            # CTk's own icon (200 ms after the window opened; iconbitmap was mocked, so CTk
            # didn't know it had been set), then MediaForge's, read once (branding's
            # set_own_icon): the last word is ours.
            self.assertEqual(calls, ["CTk's", ICON])
        self.assertTrue(ICON.is_file())

    async def test_the_icon_job_goes_with_the_window(self) -> None:
        window = self.window()
        bar = attach(window, identity=IDENTITY)
        assert bar is not None and bar.icon_job is not None
        window.destroy()
        self.assertIsNone(bar.icon_job)
        await pump(self.root, (ICON_DELAY_MS + 100) / 1000)  # nothing runs on the dead window


class ShortcutOnceTests(ForgeAsyncTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.enterContext(mock.patch.object(taskbar, "_shortcut_checked", False))
        self.ensure = self.enterContext(mock.patch("app.forge.shortcut.ensure_start_menu_shortcut"))

    @unittest.skipUnless(sys.platform == "win32", "Windows only")
    async def test_once_per_run_in_an_installed_copy(self) -> None:
        install = self.tmp / "AirGrabber"
        install.mkdir()

        async def done(**kwargs: object) -> None:
            return None

        self.ensure.side_effect = done
        self.assertTrue(taskbar.make_shortcut_once(install))
        self.assertFalse(taskbar.make_shortcut_once(install))
        await asyncio.sleep(0)
        self.ensure.assert_called_once_with(install_dir=install)

    async def test_never_in_a_development_copy(self) -> None:
        self.assertFalse(taskbar.make_shortcut_once(PROJECT))
        self.ensure.assert_not_called()


@unittest.skipUnless(sys.platform == "win32", "the real property store is Windows-only")
class RealStoreTests(TkCase):
    """The real SHGetPropertyStoreForWindow / IPropertyStore on a throwaway window."""

    async def test_sets_and_clears_the_properties(self) -> None:
        native = taskbar.windows_native()
        assert native is not None
        self.addCleanup(taskbar.set_native, taskbar.set_native(native))
        self.enterContext(mock.patch.object(taskbar, "make_shortcut_once", return_value=False))
        identity = WindowIdentity(
            app_id="drunkgummyboy.AirGrabber.TaskbarTest",
            relaunch_command=IDENTITY.relaunch_command,
            display_name="MediaForge",
            icon=IDENTITY.icon,
        )
        window = self.window()
        bar = attach(window, identity=identity)
        assert bar is not None
        hwnd = bar.hwnd
        self.assertEqual(hwnd, int(window.wm_frame(), 16))  # Tk's wrapper
        await pump(window, 0.1)
        self.assertEqual(
            {pid: _read(hwnd, pid) for pid in (APP_ID, RELAUNCH_COMMAND, RELAUNCH_NAME, RELAUNCH_ICON)},
            {
                APP_ID: identity.app_id,
                RELAUNCH_COMMAND: identity.relaunch_command,
                RELAUNCH_NAME: "MediaForge",
                RELAUNCH_ICON: identity.icon,
            },
        )
        # Cleared on the way out (read in <Destroy>, while the wrapper is still there).
        seen: list[str | None] = []
        window.bind("<Destroy>", lambda event: seen.append(_read(hwnd, APP_ID)) if event.widget is window else None, add="+")
        window.destroy()
        self.assertEqual(seen, [None])


def _read(hwnd: int, pid: int) -> str | None:
    """A window property's text (None: empty), through the same store."""
    from ctypes import wintypes

    shell32 = ctypes.WinDLL("shell32")
    get_store = shell32.SHGetPropertyStoreForWindow
    get_store.argtypes = (wintypes.HWND, ctypes.POINTER(taskbar._Guid), ctypes.POINTER(ctypes.c_void_p))
    get_store.restype = ctypes.c_long
    store = ctypes.c_void_p()
    iid = taskbar._guid(taskbar._IID_PROPERTY_STORE)
    assert get_store(hwnd, ctypes.byref(iid), ctypes.byref(store)) >= 0
    table = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    get_value = ctypes.WINFUNCTYPE(
        ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(taskbar._PropertyKey), ctypes.POINTER(taskbar._PropVariant)
    )(table[5])
    key = taskbar._PropertyKey(taskbar._guid(taskbar.PROPERTY_FORMAT), pid)
    value = taskbar._PropVariant()
    try:
        assert get_value(store, ctypes.byref(key), ctypes.byref(value)) >= 0
        return ctypes.wstring_at(value.pointer) if value.vt == 31 and value.pointer else None
    finally:
        ctypes.WinDLL("ole32").PropVariantClear(ctypes.byref(value))
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(table[2])(store)


if __name__ == "__main__":
    unittest.main()
