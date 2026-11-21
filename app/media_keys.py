"""The media keys for the player (app/media_player_window.py, in the player's process).

The keyboard's ▶⏸ / ⏭ / ⏮ / ⏹ keys and a headset's buttons (which Windows hands on as the same
keys) reach the player also while another window is in front (owner, 2026-11-21): each key is a
Windows hot key (`RegisterHotKey`) of the player's video window for as long as the window is
open, answered by a window-procedure subclass (`SetWindowLongPtrW`, app/forge/ui/file_drops.py's
pattern) that only hands the key's name on (`deliver`: the window's events queue; no Tk there).
A key another program has taken already stays that program's. The same window also hears
Windows lock (Windows+L, the screen saver's lock): "lock" (round 4, owner 2026-11-21: the video
pauses; `WTSRegisterSessionNotification`). Stdlib only (ctypes); logs no paths or titles.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from collections.abc import Callable
from typing import Any, Protocol

log = logging.getLogger(__name__)

WM_HOTKEY = 0x0312
WM_WTSSESSION_CHANGE = 0x02B1
WTS_SESSION_LOCK = 0x7
WM_NCDESTROY = 0x0082
GWLP_WNDPROC = -4
MOD_NOREPEAT = 0x4000

# The hot keys' ids (any number of this window's own) → the virtual key and its name.
KEYS: dict[int, tuple[int, str]] = {
    0xA101: (0xB3, "play_pause"),  # VK_MEDIA_PLAY_PAUSE
    0xA102: (0xB0, "next"),  # VK_MEDIA_NEXT_TRACK
    0xA103: (0xB1, "previous"),  # VK_MEDIA_PREV_TRACK
    0xA104: (0xB2, "stop"),  # VK_MEDIA_STOP
}

Deliver = Callable[[str], None]


class MediaKeysSeam(Protocol):
    """Where the media keys come from (`WindowsMediaKeys`; tests: a fake). Tk's thread only."""

    def attach(self, window_id: int, deliver: Deliver) -> bool:
        """The keys to `window_id` (a Tk window's `winfo_id()`), each as its name to `deliver`."""
        ...

    def detach(self) -> None: ...


class WindowsMediaKeys:
    """The Win32 side: one window at a time."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("Windows only")
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
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
        user32.RegisterHotKey.argtypes = (hwnd, ctypes.c_int, uint, uint)
        user32.RegisterHotKey.restype = wintypes.BOOL
        user32.UnregisterHotKey.argtypes = (hwnd, ctypes.c_int)
        user32.UnregisterHotKey.restype = wintypes.BOOL
        user32.IsWindow.argtypes = (hwnd,)
        user32.IsWindow.restype = wintypes.BOOL
        self._user32 = user32
        try:
            wtsapi32 = ctypes.WinDLL("wtsapi32")
            wtsapi32.WTSRegisterSessionNotification.argtypes = (hwnd, wintypes.DWORD)
            wtsapi32.WTSRegisterSessionNotification.restype = wintypes.BOOL
            wtsapi32.WTSUnRegisterSessionNotification.argtypes = (hwnd,)
            wtsapi32.WTSUnRegisterSessionNotification.restype = wintypes.BOOL
            self._wtsapi32: Any = wtsapi32
        except (OSError, AttributeError):
            self._wtsapi32 = None
        self._session = False
        wndproc = ctypes.WINFUNCTYPE(lresult, hwnd, uint, wparam, lparam)
        self._proc = wndproc(self._window_proc)  # alive as long as this object (the process)
        self._proc_address = int(ctypes.cast(self._proc, ctypes.c_void_p).value or 0)
        self._window = 0
        self._original = 0
        self._registered: list[int] = []
        self._deliver: Deliver = _ignore

    def attach(self, window_id: int, deliver: Deliver) -> bool:
        if self._window:
            self.detach()
        original = int(self._user32.SetWindowLongPtrW(window_id, GWLP_WNDPROC, self._proc_address))
        if not original:
            log.warning("Media keys: subclassing failed (error %d)", ctypes.get_last_error())
            return False
        self._window, self._original, self._deliver = window_id, original, deliver
        for key_id, (virtual_key, _name) in KEYS.items():
            if self._user32.RegisterHotKey(window_id, key_id, MOD_NOREPEAT, virtual_key):
                self._registered.append(key_id)
        if self._wtsapi32 is not None:
            self._session = bool(self._wtsapi32.WTSRegisterSessionNotification(window_id, 0))
        taken = len(KEYS) - len(self._registered)
        if taken:
            log.info("Media keys: %d of %d taken by another program", taken, len(KEYS))
        return True

    def detach(self) -> None:
        window, self._window = self._window, 0
        self._deliver = _ignore
        if not window:
            return
        alive = bool(self._user32.IsWindow(window))
        for key_id in self._registered:
            if alive:
                self._user32.UnregisterHotKey(window, key_id)
        self._registered = []
        if self._session and alive and self._wtsapi32 is not None:
            self._wtsapi32.WTSUnRegisterSessionNotification(window)
        self._session = False
        if alive and int(self._user32.GetWindowLongPtrW(window, GWLP_WNDPROC)) == self._proc_address:
            self._user32.SetWindowLongPtrW(window, GWLP_WNDPROC, self._original)
        elif alive:  # someone subclassed it after us: stay in their chain, passing it on
            self._window = window

    def _window_proc(self, hwnd: int | None, msg: int, wparam: int, lparam: int) -> int:
        try:
            if not self._original:
                return int(self._user32.DefWindowProcW(hwnd, msg, wparam, lparam))
            if msg == WM_HOTKEY and int(wparam) in KEYS:
                self._deliver(KEYS[int(wparam)][1])
                return 0
            if msg == WM_WTSSESSION_CHANGE and int(wparam) == WTS_SESSION_LOCK:
                self._deliver("lock")  # and on to the window's own procedure
            original = self._original
            if msg == WM_NCDESTROY:
                self._user32.SetWindowLongPtrW(int(hwnd or 0), GWLP_WNDPROC, original)
                self._window, self._registered, self._deliver = 0, [], _ignore
            return int(self._user32.CallWindowProcW(original, hwnd, msg, wparam, lparam))
        except Exception:  # never out of a window procedure
            log.exception("Media keys: message %#x failed", msg)
            return 0


def _ignore(_name: str) -> None:
    pass


def windows_media_keys() -> WindowsMediaKeys | None:
    """The media keys, or None off Windows or when they can't be had."""
    if sys.platform != "win32":
        return None
    try:
        return WindowsMediaKeys()
    except (OSError, AttributeError):
        log.warning("Media keys unavailable", exc_info=True)
        return None
