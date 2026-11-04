"""Files and folders dropped from Explorer onto AirGrabber's main window, without tkinterdnd2.

    attach_file_drops(widget, on_drop) -> bool

`on_drop(paths)` runs on the event loop (`loop.call_soon`) with the dropped files and folders,
in Explorer's order; False off Windows, without a running loop, or when Windows refuses. A
drop made over a child widget reaches the window (Windows looks for WS_EX_ACCEPTFILES up the
parent chain); a frame can be given its own hook too. Names Explorer can't hand over as files (a
phone, inside a zip) drop nothing.

How (INTEGRATION.md 3.5): `DragAcceptFiles` on the widget's own window (`winfo_id()`, not Tk's
wrapper: Tk moves that window into a new wrapper on `wm resizable` / `wm transient` and the hook
stays with it), a window-procedure subclass (`SetWindowLongPtrW`, app/ui/borderless.py's
pattern) answering WM_DROPFILES with `DragQueryFileW` / `DragFinish`, and
`ChangeWindowMessageFilterEx` so drops reach an AirGrabber running as administrator. The window
procedure only calls Win32 and hands the paths on; it never calls tkinter and never lets an
exception out. The original procedure comes back on the widget's <Destroy> (or WM_NCDESTROY).

The Win32 side sits behind `Native` (`set_native`): tests use a fake or `InertNative`.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import sys
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

Deliver = Callable[[list[str]], None]


class Native(Protocol):
    """What Windows does for a drop target. `window_id` is the widget's own `winfo_id()`."""

    def attach(self, window_id: int, deliver: Deliver) -> bool:
        """Accept drops on the window: `deliver(paths)` from its window procedure (only queue
        there). Attaching again replaces `deliver`. False when that isn't possible."""
        ...

    def detach(self, window_id: int) -> None:
        """Stop accepting drops and give the window its own procedure back."""
        ...


class InertNative:
    """A Native that accepts every window and never delivers a drop (UI tests)."""

    def attach(self, window_id: int, deliver: Deliver) -> bool:
        return True

    def detach(self, window_id: int) -> None:
        pass


_native: Native | None = None


def set_native(native: Native | None) -> Native | None:
    """Use `native` from now on (None: no drops anywhere); returns the one before."""
    global _native
    previous, _native = _native, native
    return previous


def attach_file_drops(widget: tk.Misc, on_drop: Callable[[list[Path]], None]) -> bool:
    """Call `on_drop` with what Explorer drops on `widget` (see the module's notes)."""
    native = _native
    if native is None:
        return False
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("File drops need the running event loop; not attached")
        return False
    window_id = widget.winfo_id()

    def deliver(paths: list[str]) -> None:  # inside the window procedure: only queue
        if paths:
            loop.call_soon(_hand_over, widget, on_drop, paths)

    if not native.attach(window_id, deliver):
        log.warning("Couldn't accept file drops on a window")
        return False

    def destroyed(event: Any) -> None:
        if str(getattr(event, "widget", "")) == str(widget):
            native.detach(window_id)

    tk.Misc.bind(widget, "<Destroy>", destroyed, "+")  # not CTk's bind: the widget's own
    return True


def _hand_over(widget: tk.Misc, on_drop: Callable[[list[Path]], None], paths: list[str]) -> None:
    try:
        if not widget.winfo_exists():
            return
    except tk.TclError:  # the interpreter is gone
        return
    try:
        on_drop([Path(path) for path in paths])
    except Exception:
        log.exception("Handling a file drop failed")


# -- Windows ---------------------------------------------------------------------------------------

WM_COPYGLOBALDATA = 0x0049
WM_COPYDATA = 0x004A
WM_NCDESTROY = 0x0082
WM_DROPFILES = 0x0233
GWLP_WNDPROC = -4
MSGFLT_ALLOW = 1
ALL_FILES = 0xFFFFFFFF  # DragQueryFileW's index for "how many"


@dataclass
class _Target:
    original: int  # the window's own procedure
    deliver: Deliver


class WindowsNative:
    """The Win32 side. One per process (`windows_native()`): its window procedure must outlive
    every window it was given to."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("Windows only")
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        lresult = ctypes.c_ssize_t
        hwnd, uint = wintypes.HWND, wintypes.UINT
        wparam, lparam = wintypes.WPARAM, wintypes.LPARAM
        user32.SetWindowLongPtrW.argtypes = (hwnd, ctypes.c_int, ctypes.c_ssize_t)
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.GetWindowLongPtrW.argtypes = (hwnd, ctypes.c_int)
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.CallWindowProcW.argtypes = (ctypes.c_ssize_t, hwnd, uint, wparam, lparam)
        user32.CallWindowProcW.restype = lresult
        user32.DefWindowProcW.argtypes = (hwnd, uint, wparam, lparam)
        user32.DefWindowProcW.restype = lresult
        user32.IsWindow.argtypes = (hwnd,)
        user32.IsWindow.restype = wintypes.BOOL
        user32.ChangeWindowMessageFilterEx.argtypes = (hwnd, uint, wintypes.DWORD, ctypes.c_void_p)
        user32.ChangeWindowMessageFilterEx.restype = wintypes.BOOL
        shell32.DragAcceptFiles.argtypes = (hwnd, wintypes.BOOL)
        shell32.DragAcceptFiles.restype = None
        shell32.DragQueryFileW.argtypes = (ctypes.c_void_p, uint, wintypes.LPWSTR, uint)
        shell32.DragQueryFileW.restype = uint
        shell32.DragFinish.argtypes = (ctypes.c_void_p,)
        shell32.DragFinish.restype = None
        self._user32 = user32
        self._shell32 = shell32
        wndproc = ctypes.WINFUNCTYPE(lresult, hwnd, uint, wparam, lparam)
        self._proc = wndproc(self._window_proc)  # alive as long as this object (the process)
        self._proc_address = int(ctypes.cast(self._proc, ctypes.c_void_p).value or 0)
        self._targets: dict[int, _Target] = {}

    def attach(self, window_id: int, deliver: Deliver) -> bool:
        target = self._targets.get(window_id)
        if target is not None:  # still in its chain (WM_NCDESTROY forgets a window)
            target.deliver = deliver
            self._shell32.DragAcceptFiles(window_id, True)
            return True
        original = int(self._user32.SetWindowLongPtrW(window_id, GWLP_WNDPROC, self._proc_address))
        if not original:
            log.warning("File drops: subclassing failed (error %d)", ctypes.get_last_error())
            return False
        self._targets[window_id] = _Target(original=original, deliver=deliver)
        for msg in (WM_DROPFILES, WM_COPYDATA, WM_COPYGLOBALDATA):  # drops into an elevated app
            self._user32.ChangeWindowMessageFilterEx(window_id, msg, MSGFLT_ALLOW, None)
        self._shell32.DragAcceptFiles(window_id, True)
        return True

    def detach(self, window_id: int) -> None:
        target = self._targets.pop(window_id, None)
        if target is None or not self._user32.IsWindow(window_id):
            return
        self._shell32.DragAcceptFiles(window_id, False)
        if self._hooked(window_id):
            self._user32.SetWindowLongPtrW(window_id, GWLP_WNDPROC, target.original)
        else:  # someone subclassed it after us: stay in their chain, passing everything on
            self._targets[window_id] = _Target(original=target.original, deliver=_ignore)

    def hooked(self, window_id: int) -> bool:
        """Whether drops on the window come here (tests)."""
        return window_id in self._targets and self._hooked(window_id)

    def _hooked(self, window_id: int) -> bool:
        current = int(self._user32.GetWindowLongPtrW(window_id, GWLP_WNDPROC))
        return current == self._proc_address

    def _window_proc(self, hwnd: int | None, msg: int, wparam: int, lparam: int) -> int:
        try:
            window_id = int(hwnd or 0)
            target = self._targets.get(window_id)
            if target is None:  # not ours (any more): shouldn't happen
                return int(self._user32.DefWindowProcW(hwnd, msg, wparam, lparam))
            if msg == WM_DROPFILES:
                self._dropped(wparam, target)
                return 0
            if msg == WM_NCDESTROY:
                self._user32.SetWindowLongPtrW(window_id, GWLP_WNDPROC, target.original)
                del self._targets[window_id]
            return int(self._user32.CallWindowProcW(target.original, hwnd, msg, wparam, lparam))
        except Exception:  # never out of a window procedure
            log.exception("File drops: message %#x failed", msg)
            return 0

    def _dropped(self, hdrop: int, target: _Target) -> None:
        try:
            try:
                paths = self._paths(hdrop)
            finally:
                self._shell32.DragFinish(hdrop)
            target.deliver(paths)
        except Exception:
            log.exception("File drops: reading a drop failed")

    def _paths(self, hdrop: int) -> list[str]:
        query = self._shell32.DragQueryFileW
        paths: list[str] = []
        for index in range(int(query(hdrop, ALL_FILES, None, 0))):
            length = int(query(hdrop, index, None, 0))  # without the closing NUL
            if not length:
                continue
            buffer = ctypes.create_unicode_buffer(length + 1)
            if query(hdrop, index, buffer, length + 1):
                paths.append(buffer.value)
        return paths


def _ignore(_paths: list[str]) -> None:
    pass


_windows_native: WindowsNative | None = None


def windows_native() -> WindowsNative | None:
    """The process's one WindowsNative; None off Windows or when it can't be made."""
    global _windows_native
    if _windows_native is None and sys.platform == "win32":
        try:
            _windows_native = WindowsNative()
        except (OSError, AttributeError):
            log.warning("File drops unavailable", exc_info=True)
    return _windows_native


_native = windows_native()
