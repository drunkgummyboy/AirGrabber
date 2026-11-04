"""Explorer drops on a window (app/forge/ui/file_drops.py): the hand-over logic with a fake
Win32 side, and, on Windows, the real window procedure fed WM_DROPFILES messages built the way the
shell builds them (an HDROP: DROPFILES + a double-NUL-terminated UTF-16 list) on a hidden window.
A real Explorer drag needs the mouse: that stays a manual check."""

from __future__ import annotations

import ctypes
import gc
import sys
import tkinter as tk
import unittest
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import customtkinter as ctk

from app.forge.ui import file_drops
from app.forge.ui.file_drops import Deliver, attach_file_drops, set_native
from tests.forge.base import ForgeAsyncTestCase
from tests.forge.tk import pump, pump_until


class FakeNative:
    """Records what a drop target asks of Windows; `drop` plays Windows' part."""

    def __init__(self, *, attaches: bool = True) -> None:
        self.attaches = attaches
        self.targets: dict[int, Deliver] = {}
        self.detached: list[int] = []

    def attach(self, window_id: int, deliver: Deliver) -> bool:
        if self.attaches:
            self.targets[window_id] = deliver
        return self.attaches

    def detach(self, window_id: int) -> None:
        self.detached.append(window_id)
        self.targets.pop(window_id, None)

    def drop(self, window_id: int, paths: list[str]) -> None:
        self.targets[window_id](paths)


class TkCase(ForgeAsyncTestCase):
    native: Any

    def setUp(self) -> None:
        super().setUp()
        gc.collect()
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        self.drops: list[list[Path]] = []

    def use(self, native: file_drops.Native | None) -> None:
        self.native = native
        self.addCleanup(set_native, set_native(native))

    def _destroy_root(self) -> None:
        for child in self.root.winfo_children():
            if isinstance(child, tk.Toplevel):
                child.destroy()
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    def window(self) -> ctk.CTkToplevel:
        window = ctk.CTkToplevel(self.root)
        window.attributes("-alpha", 0.0)  # mapped but invisible: no flash on the screen
        window.geometry("400x300+40+40")
        return window


class HandOverTests(TkCase):
    def setUp(self) -> None:
        super().setUp()
        self.use(FakeNative())

    async def test_paths_reach_the_callback_on_the_loop_not_in_the_window_procedure(self) -> None:
        window = self.window()
        self.assertTrue(attach_file_drops(window, self.drops.append))
        self.assertIn(window.winfo_id(), self.native.targets)
        self.native.drop(window.winfo_id(), [r"C:\Downloads\a.mkv", r"\\NAS\Films\b"])
        self.assertEqual(self.drops, [])  # only queued
        await pump_until(window, lambda: self.drops)
        self.assertEqual(self.drops, [[Path(r"C:\Downloads\a.mkv"), Path(r"\\NAS\Films\b")]])

    async def test_an_empty_drop_calls_nothing(self) -> None:
        window = self.window()
        attach_file_drops(window, self.drops.append)
        self.native.drop(window.winfo_id(), [])
        await pump(window)
        self.assertEqual(self.drops, [])

    async def test_a_frame_has_its_own_hook(self) -> None:
        window = self.window()
        frame = ctk.CTkFrame(window)
        frame.pack(fill="both", expand=True)
        self.assertTrue(attach_file_drops(frame, self.drops.append))
        self.native.drop(frame.winfo_id(), [r"C:\Downloads\c.mkv"])
        await pump_until(window, lambda: self.drops)
        self.assertEqual(self.drops, [[Path(r"C:\Downloads\c.mkv")]])

    async def test_destroying_the_widget_gives_the_window_back(self) -> None:
        window = self.window()
        child = ctk.CTkFrame(window)
        attach_file_drops(window, self.drops.append)
        window_id = window.winfo_id()
        child.destroy()  # a child's <Destroy> also reaches the window's binding
        self.assertEqual(self.native.detached, [])
        window.destroy()
        self.assertEqual(self.native.detached, [window_id])

    async def test_a_drop_queued_before_the_widget_went_calls_nothing(self) -> None:
        window = self.window()
        attach_file_drops(window, self.drops.append)
        self.native.drop(window.winfo_id(), [r"C:\Downloads\d.mkv"])
        window.destroy()
        await pump(self.root)
        self.assertEqual(self.drops, [])

    async def test_a_failing_callback_is_logged_not_raised(self) -> None:
        window = self.window()

        def fails(_paths: list[Path]) -> None:
            raise ValueError("boom")

        attach_file_drops(window, fails)
        self.native.drop(window.winfo_id(), [r"C:\Downloads\e.mkv"])
        with self.assertLogs(file_drops.log, "ERROR") as logs:
            await pump(window)
        self.assertIn("Handling a file drop failed", logs.output[0])

    async def test_no_drops_without_a_native_or_when_windows_refuses(self) -> None:
        window = self.window()
        self.use(None)
        self.assertFalse(attach_file_drops(window, self.drops.append))
        self.use(FakeNative(attaches=False))
        with self.assertLogs(file_drops.log, "WARNING"):
            self.assertFalse(attach_file_drops(window, self.drops.append))

    async def test_set_native_returns_the_one_before(self) -> None:
        inert = file_drops.InertNative()
        before = set_native(inert)
        self.assertIs(set_native(before), inert)
        self.assertIs(before, self.native)


def cancel_jobs(root: tk.Misc) -> None:
    for job in root.tk.splitlist(root.tk.call("after", "info")):
        root.after_cancel(job)


class NoLoopTests(unittest.TestCase):
    def test_no_drops_without_a_running_loop(self) -> None:
        previous = set_native(FakeNative())
        self.addCleanup(set_native, previous)
        try:
            root = ctk.CTk()
        except tk.TclError as exc:
            self.skipTest(f"Tk unavailable: {exc}")
        self.addCleanup(root.destroy)
        self.addCleanup(cancel_jobs, root)  # Tcl's timers are per thread: none for later tests
        root.withdraw()
        with self.assertLogs(file_drops.log, "WARNING"):
            self.assertFalse(attach_file_drops(root, lambda _paths: None))


# -- the real window procedure (Windows) ---------------------------------------------------------

UNC = r"\\NAS\Multimedia\Series\Gold Rush\Gold.Rush.S17E05.1080p.WEB.h264-EDITH.mkv"
LONG = "C:\\Temp\\" + "\\".join(["a" * 60] * 5) + "\\Dune.Part.Two.2024.2160p.mkv"
FOUR = [r"C:\Downloads\Amélie (2001) 日本語.mkv", UNC, LONG, r"C:\Downloads\A folder"]


def post_drop(window_id: int, paths: list[str]) -> None:
    """What Explorer hands a WM_DROPFILES window: an HDROP the receiver frees with DragFinish."""
    from ctypes import wintypes

    class DROPFILES(ctypes.Structure):
        _fields_ = [
            ("pFiles", wintypes.DWORD),
            ("pt", wintypes.POINT),
            ("fNC", wintypes.BOOL),
            ("fWide", wintypes.BOOL),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = (ctypes.c_void_p,)
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
    user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    user32.PostMessageW.restype = wintypes.BOOL
    text = ("\0".join(paths) + "\0\0").encode("utf-16-le")
    header = ctypes.sizeof(DROPFILES)
    handle = kernel32.GlobalAlloc(0x0002 | 0x0040, header + len(text))  # MOVEABLE | ZEROINIT
    pointer = kernel32.GlobalLock(handle)
    drop = DROPFILES.from_address(pointer)
    drop.pFiles, drop.fWide = header, True
    ctypes.memmove(pointer + header, text, len(text))
    kernel32.GlobalUnlock(handle)
    if not user32.PostMessageW(window_id, file_drops.WM_DROPFILES, handle, 0):
        raise OSError("PostMessageW failed")


@unittest.skipUnless(sys.platform == "win32", "Windows only")
class RealDropTests(TkCase):
    def setUp(self) -> None:
        super().setUp()
        real = file_drops.windows_native()
        if real is None:
            self.skipTest("Win32 file drops unavailable")
        self.use(real)
        self.addCleanup(ctk.set_appearance_mode, ctk.get_appearance_mode())

    async def dropped(self, window: tk.Misc, window_id: int, paths: list[str]) -> list[Path]:
        count = len(self.drops)
        post_drop(window_id, paths)
        await pump_until(window, lambda: len(self.drops) > count)
        return self.drops[-1]

    async def test_drops_arrive_whole_and_survive_what_tk_does_to_the_window(self) -> None:
        window = self.window()
        await pump(window, 0.3)
        window_id = window.winfo_id()
        self.assertTrue(attach_file_drops(window, self.drops.append))
        self.assertTrue(self.native.hooked(window_id))
        self.assertGreater(len(LONG), 260)
        self.assertEqual(await self.dropped(window, window_id, FOUR), [Path(p) for p in FOUR])
        many = [rf"\\NAS\Multimedia\Downloads\Show.S01E{i:02d}.mkv" for i in range(1, 201)]
        self.assertEqual(await self.dropped(window, window_id, many), [Path(p) for p in many])

        def switch_mode() -> None:
            ctk.set_appearance_mode("light" if ctk.get_appearance_mode() == "Dark" else "dark")

        async def withdraw_and_show() -> None:
            window.withdraw()
            await pump(window)
            window.deiconify()

        async def transient_and_back() -> None:
            window.transient(self.root)
            await pump(window)
            window.wm_transient("")

        def not_resizable() -> None:
            window.resizable(False, False)

        steps: dict[str, Callable[[], Awaitable[None] | None]] = {
            "resizable": not_resizable,
            "transient": transient_and_back,
            "withdraw": withdraw_and_show,
            "light/dark": switch_mode,
        }
        for label, step in steps.items():
            with self.subTest(after=label):
                result = step()
                if result is not None:
                    await result
                await pump(window, 0.4)
                self.assertEqual(window.winfo_id(), window_id)
                self.assertTrue(self.native.hooked(window_id))
                path = rf"C:\Downloads\after {label.replace('/', ' ')}.mkv"
                self.assertEqual(await self.dropped(window, window_id, [path]), [Path(path)])

        window.destroy()
        self.assertFalse(self.native.hooked(window_id))

    async def test_a_frame_inside_the_window(self) -> None:
        window = self.window()
        frame = ctk.CTkFrame(window)
        frame.pack(fill="both", expand=True)
        await pump(window, 0.3)
        self.assertTrue(attach_file_drops(frame, self.drops.append))
        frame_id = frame.winfo_id()
        self.assertEqual(await self.dropped(window, frame_id, FOUR), [Path(p) for p in FOUR])
        frame.destroy()
        self.assertFalse(self.native.hooked(frame_id))


if __name__ == "__main__":
    unittest.main()
