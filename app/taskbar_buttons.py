"""⏮ ⏯ ⏭ under the player's taskbar preview (app/media_player_window.py, the player's process).

Round 4 (owner, 2026-11-21), as Windows Media Player has them: hovering the player's taskbar
button shows its preview with three buttons. Windows' ITaskbarList3 (plain ctypes COM, no
package) adds them to the player's top-level window (`ThumbBarAddButtons`, icons
app/assets/thumb_*.ico); a click comes to that window as WM_COMMAND / THBN_CLICKED, so the window
procedure is subclassed (after borderless.py's, passing everything else on) and the click handed
on by name: "previous", "play_pause", "next" (`deliver`: the window's events queue, as a media
key; no Tk here). Tk makes the window anew for full screen and the mini player: the window calls
`attach` again with the new one (`ThumbButtons.window`). Stdlib only; logs no titles or paths.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

WM_COMMAND = 0x0111
WM_NCDESTROY = 0x0082
GWLP_WNDPROC = -4
THBN_CLICKED = 0x1800
_THB_ICON, _THB_TOOLTIP, _THB_FLAGS = 0x2, 0x4, 0x8
_THBF_ENABLED, _THBF_DISABLED = 0x0, 0x1
_CLSID_TASKBAR = "{56FDF344-FD6D-11d0-958A-006097C9A090}"
_IID_TASKBAR3 = "{EA1AFB91-9E28-4B86-90E9-9E9F8A5EEFAF}"
_CLSCTX_INPROC = 0x1
_IMAGE_ICON, _LR_LOADFROMFILE = 1, 0x10
_SM_CXSMICON = 49

# The buttons' ids, in order, and their names.
BUTTONS = ((0x41, "previous"), (0x42, "play_pause"), (0x43, "next"))
ASSETS = Path(__file__).resolve().parent / "assets"


class ThumbButtonsSeam(Protocol):
    """The taskbar preview's buttons (`ThumbButtons`; tests: a fake). Tk's thread only."""

    @property
    def window(self) -> int:
        """The window they are on (0: none)."""
        ...

    def attach(self, window: int, deliver: Callable[[str], None]) -> bool: ...

    def update(self, playing: bool, tips: Sequence[str], enabled: Sequence[bool]) -> None:
        """⏯'s look (pause while `playing`), the three tips and which buttons work."""
        ...

    def detach(self) -> None: ...


class _Guid(ctypes.Structure):
    _fields_ = [
        ("data1", ctypes.c_ulong),
        ("data2", ctypes.c_ushort),
        ("data3", ctypes.c_ushort),
        ("data4", ctypes.c_ubyte * 8),
    ]


class _ThumbButton(ctypes.Structure):
    _fields_ = [
        ("mask", ctypes.c_ulong),
        ("id", ctypes.c_uint),
        ("bitmap", ctypes.c_uint),
        ("icon", ctypes.c_void_p),
        ("tip", ctypes.c_wchar * 260),
        ("flags", ctypes.c_ulong),
    ]


def _call(obj: ctypes.c_void_p, index: int, *types: Any) -> Callable[..., int]:
    table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    method = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *types)(table[index])
    return lambda *args: int(method(obj, *args))


class ThumbButtons:
    """ThumbButtonsSeam on Windows: one window at a time."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("Windows only")
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        ole32 = ctypes.WinDLL("ole32")
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
        user32.RegisterWindowMessageW.argtypes = (wintypes.LPCWSTR,)
        user32.RegisterWindowMessageW.restype = uint
        user32.LoadImageW.argtypes = (
            wintypes.HINSTANCE, wintypes.LPCWSTR, uint, ctypes.c_int, ctypes.c_int, uint
        )
        user32.LoadImageW.restype = wintypes.HANDLE
        user32.GetSystemMetrics.argtypes = (ctypes.c_int,)
        user32.GetSystemMetrics.restype = ctypes.c_int
        self._user32 = user32
        ole32.CoInitializeEx(None, 0x2)  # Tk's thread: apartment (S_FALSE when it already is)
        clsid, iid = _Guid(), _Guid()
        ole32.CLSIDFromString(ctypes.c_wchar_p(_CLSID_TASKBAR), ctypes.byref(clsid))
        ole32.CLSIDFromString(ctypes.c_wchar_p(_IID_TASKBAR3), ctypes.byref(iid))
        self._taskbar = ctypes.c_void_p()
        result = ole32.CoCreateInstance(
            ctypes.byref(clsid), None, _CLSCTX_INPROC, ctypes.byref(iid),
            ctypes.byref(self._taskbar),
        )
        if result != 0 or not self._taskbar:
            raise OSError("no taskbar list")
        _call(self._taskbar, 3)()  # HrInit
        self._created = int(user32.RegisterWindowMessageW("TaskbarButtonCreated"))
        side = max(16, int(user32.GetSystemMetrics(_SM_CXSMICON)))
        self._icons = {
            name: user32.LoadImageW(
                None, str(ASSETS / f"thumb_{name}.ico"), _IMAGE_ICON, side, side, _LR_LOADFROMFILE
            )
            for name in ("previous", "play", "pause", "next")
        }
        wndproc = ctypes.WINFUNCTYPE(lresult, hwnd, uint, wparam, lparam)
        self._proc = wndproc(self._window_proc)  # alive as long as this object (the process)
        self._proc_address = int(ctypes.cast(self._proc, ctypes.c_void_p).value or 0)
        self._window = 0
        self._original = 0
        self._added = False
        self._deliver: Callable[[str], None] = _ignore
        self._look: tuple[bool, tuple[str, ...], tuple[bool, ...]] = (
            False, ("", "", ""), (True, True, True)
        )

    @property
    def window(self) -> int:
        return self._window

    def attach(self, window: int, deliver: Callable[[str], None]) -> bool:
        if window == self._window:
            self._deliver = deliver
            return True
        self.detach()
        original = int(self._user32.SetWindowLongPtrW(window, GWLP_WNDPROC, self._proc_address))
        if not original:
            log.warning("Taskbar buttons: subclassing failed (error %d)", ctypes.get_last_error())
            return False
        self._window, self._original, self._deliver = window, original, deliver
        self._added = False
        self._add()  # often too early: then when Windows says the taskbar button is there
        return True

    def update(self, playing: bool, tips: Sequence[str], enabled: Sequence[bool]) -> None:
        self._look = (playing, tuple(tips), tuple(enabled))
        if self._window and self._added:
            buttons = self._buttons()
            _call(self._taskbar, 16, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p)(
                self._window, len(buttons), ctypes.byref(buttons)
            )

    def detach(self) -> None:
        window, self._window = self._window, 0
        self._deliver = _ignore
        self._added = False
        if not window or not self._user32.IsWindow(window):
            return
        if int(self._user32.GetWindowLongPtrW(window, GWLP_WNDPROC)) == self._proc_address:
            self._user32.SetWindowLongPtrW(window, GWLP_WNDPROC, self._original)
            self._original = 0
        else:  # someone subclassed it after us: stay in their chain, passing it on
            self._window = 0

    def _buttons(self) -> Any:
        playing, tips, enabled = self._look
        buttons = (_ThumbButton * len(BUTTONS))()
        for index, (button_id, name) in enumerate(BUTTONS):
            icon = ("pause" if playing else "play") if name == "play_pause" else name
            button = buttons[index]
            button.mask = _THB_ICON | _THB_TOOLTIP | _THB_FLAGS
            button.id = button_id
            button.icon = self._icons.get(icon)
            button.tip = tips[index][:259] if index < len(tips) else ""
            on = enabled[index] if index < len(enabled) else True
            button.flags = _THBF_ENABLED if on else _THBF_DISABLED
        return buttons

    def _add(self) -> None:
        if not self._window:
            return
        buttons = self._buttons()
        add = _call(self._taskbar, 15, ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p)
        result = add(self._window, len(buttons), ctypes.byref(buttons))
        if result == 0:
            self._added = True
        elif self._added:  # already there (Explorer started again): brought up to date
            self.update(*self._look)

    def _window_proc(self, hwnd: int | None, msg: int, wparam: int, lparam: int) -> int:
        try:
            original = self._original
            if not original:
                return int(self._user32.DefWindowProcW(hwnd, msg, wparam, lparam))
            if msg == WM_COMMAND and (int(wparam) >> 16) & 0xFFFF == THBN_CLICKED:
                button_id = int(wparam) & 0xFFFF
                name = next((n for i, n in BUTTONS if i == button_id), None)
                if name is not None:
                    self._deliver(name)
                    return 0
            if msg == self._created and int(hwnd or 0) == self._window:
                self._add()
            if msg == WM_NCDESTROY and int(hwnd or 0) == self._window:
                self._user32.SetWindowLongPtrW(int(hwnd or 0), GWLP_WNDPROC, original)
                self._window, self._original, self._added = 0, 0, False
                self._deliver = _ignore
            return int(self._user32.CallWindowProcW(original, hwnd, msg, wparam, lparam))
        except Exception:  # never out of a window procedure
            log.exception("Taskbar buttons: message %#x failed", msg)
            return 0


def _ignore(_name: str) -> None:
    pass


def thumb_buttons() -> ThumbButtons | None:
    """The taskbar preview's buttons, or None off Windows or when they can't be had."""
    if sys.platform != "win32":
        return None
    try:
        return ThumbButtons()
    except (OSError, AttributeError):
        log.warning("Taskbar buttons unavailable", exc_info=True)
        return None
