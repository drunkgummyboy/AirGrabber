"""The Forge window's own taskbar button with MediaForge's icon (D8, INTEGRATION.md 4 "Taskbar").

    attach(window) -> ForgeTaskbar | None

AirGrabber's process has one AppUserModelID (app/identity.py), so every window it shows sits in
one taskbar group with AirGrabber's icon. The Forge window's top-level window (Tk's wrapper,
`GetParent(winfo_id())`) gets its own: `SHGetPropertyStoreForWindow` and
`IPropertyStore::SetValue` give it MediaForge's AppUserModelID (app/forge/shortcut.py's
`APP_USER_MODEL_ID`, which the "MediaForge" Start-menu shortcut carries too), and for pinning the
relaunch command (`"<pythonw.exe>" "<install>\\main.py" --forge`), display name ("MediaForge") and
icon (app\\assets\\forge\\MediaForge.ico). Windows then shows it as a button of its own with the
window's icon, and a pin of it starts the Forge window.

When: `attach` (from the window's __init__, before the window first shows) sets them at once, and
again whenever Tk has replaced the wrapper with a new one (checked on every `<Map>` of the window:
the wrapper differs from the one that has them). The window's icon is set to MediaForge's once
more after CTk's own delayed icon (200 ms after a window opens). When the window is destroyed the
properties are cleared again, as Windows asks.

`attach` also starts, once per run and only in an installed copy, the check that makes the
"MediaForge" Start-menu shortcut for an AirGrabber that updated to 3.0.0
(`shortcut.ensure_start_menu_shortcut`; it never raises).

The Win32 / COM calls sit behind `Native` (`set_native`): tests use a fake, the real one
(`WindowsNative`, plain ctypes vtable calls) runs elsewhere. Only Win32 is called here, from Tk's
thread.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import tkinter as tk
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

import customtkinter as ctk

from app.forge import shortcut
from app.forge.shortcut import WindowIdentity
from app.services.updates import is_development_copy
from app.ui import tasks
from app.ui.branding import set_own_icon

log = logging.getLogger(__name__)

ICON = shortcut.INSTALL_DIR / "app" / "assets" / "forge" / "MediaForge.ico"
ICON_DELAY_MS = 250  # after CTk's own icon, which comes 200 ms after a window opens

# The System.AppUserModel.* properties: {9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}, <id>.
PROPERTY_FORMAT = "9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3"
RELAUNCH_COMMAND = 2
RELAUNCH_ICON = 3
RELAUNCH_NAME = 4
APP_ID = 5

type Values = Sequence[tuple[int, str | None]]  # (property id, text); None: clear it


def identity_values(identity: WindowIdentity) -> list[tuple[int, str | None]]:
    """The properties to set, the AppUserModelID last (the relaunch ones belong to it)."""
    return [
        (RELAUNCH_COMMAND, identity.relaunch_command),
        (RELAUNCH_NAME, identity.display_name),
        (RELAUNCH_ICON, identity.icon),
        (APP_ID, identity.app_id),
    ]


def cleared_values() -> list[tuple[int, str | None]]:
    """The properties cleared again, the AppUserModelID first."""
    return [(APP_ID, None), (RELAUNCH_COMMAND, None), (RELAUNCH_NAME, None), (RELAUNCH_ICON, None)]


# -- the Win32 / COM seam ------------------------------------------------------------------------


class Native(Protocol):
    def wrapper(self, window_id: int) -> int:
        """The top-level window Tk wraps the window (`winfo_id()`) in; 0 when there is none."""
        ...

    def set_properties(self, hwnd: int, values: Values) -> bool:
        """Set (or, for None, clear) each property on `hwnd`'s property store; False when that
        wasn't possible (logged)."""
        ...


_native: Native | None = None


def set_native(native: Native | None) -> Native | None:
    """Use `native` from now on (None: the window keeps AirGrabber's group); returns the one
    before. Tests put a fake in."""
    global _native
    previous, _native = _native, native
    return previous


# -- attaching -----------------------------------------------------------------------------------


def attach(
    window: ctk.CTkToplevel,
    *,
    identity: WindowIdentity | None = None,
    icon: Path = ICON,
) -> ForgeTaskbar | None:
    """Give `window` its own taskbar button (see the module's notes). None where that isn't done
    (not on Windows, or Windows refused: the window stays in AirGrabber's group)."""
    native = _native
    if native is None:
        return None
    if identity is None:
        identity = shortcut.window_identity(shortcut.INSTALL_DIR, shortcut.current_runner())
    taskbar = ForgeTaskbar(window, native, identity, icon)
    taskbar.apply()
    window.bind("<Map>", taskbar.mapped, add="+")
    window.bind("<Destroy>", taskbar.destroyed, add="+")
    taskbar.icon_job = window.after(ICON_DELAY_MS, taskbar.set_icon)
    make_shortcut_once()
    return taskbar


class ForgeTaskbar:
    """One Forge window's taskbar identity: on which wrapper it was set."""

    def __init__(self, window: ctk.CTkToplevel, native: Native, identity: WindowIdentity, icon: Path) -> None:
        self.window = window
        self.identity = identity
        self.icon = icon
        self.hwnd = 0  # the wrapper that has the properties
        self.icon_job: str | None = None
        self._native = native
        self._window_id = window.winfo_id()

    def apply(self) -> bool:
        """Set the properties on the window's wrapper unless it has them already."""
        hwnd = self._native.wrapper(self._window_id)
        if not hwnd:
            return False
        if hwnd == self.hwnd:
            return True
        if not self._native.set_properties(hwnd, identity_values(self.identity)):
            log.warning("Couldn't give the Forge window its own taskbar button")
            return False
        if self.hwnd:
            log.debug("The Forge window's wrapper was replaced; its taskbar identity set again")
        self.hwnd = hwnd
        return True

    def mapped(self, event: Any) -> None:
        if event.widget is self.window:
            self.apply()

    def destroyed(self, event: Any) -> None:
        if event.widget is not self.window:
            return
        if self.icon_job is not None:
            try:
                self.window.after_cancel(self.icon_job)
            except tk.TclError:
                pass
            self.icon_job = None
        if self.hwnd:
            self._native.set_properties(self.hwnd, cleared_values())
            self.hwnd = 0

    def set_icon(self) -> None:
        """MediaForge's icon on the window (title bar and taskbar button), after CTk's own;
        read from the .ico once (`branding.set_own_icon`), not per window."""
        self.icon_job = None
        try:
            if self.window.winfo_exists():
                set_own_icon(self.window, self.icon)
        except tk.TclError as exc:
            log.debug("Could not set the Forge window's icon: %r", exc)


# -- the Start-menu shortcut, once per run -------------------------------------------------------

_shortcut_checked = False


def make_shortcut_once(install_dir: Path | None = None) -> bool:
    """Start `shortcut.ensure_start_menu_shortcut` the first time a Forge window is made in this
    run, in an installed copy on Windows only. True when it was started."""
    global _shortcut_checked
    folder = install_dir if install_dir is not None else shortcut.INSTALL_DIR
    if _shortcut_checked or sys.platform != "win32" or is_development_copy(folder):
        return False
    _shortcut_checked = True
    tasks.spawn(shortcut.ensure_start_menu_shortcut(install_dir=folder), name="forge-start-menu-shortcut")
    return True


# -- the real one --------------------------------------------------------------------------------


class _Guid(ctypes.Structure):
    _fields_ = [
        ("data1", ctypes.c_uint32),
        ("data2", ctypes.c_uint16),
        ("data3", ctypes.c_uint16),
        ("data4", ctypes.c_ubyte * 8),
    ]


class _PropertyKey(ctypes.Structure):
    _fields_ = [("fmtid", _Guid), ("pid", ctypes.c_uint32)]


class _PropVariant(ctypes.Structure):
    # A type tag, three reserved words, then a union two pointers wide. Only strings are set.
    _fields_ = [
        ("vt", ctypes.c_ushort),
        ("reserved1", ctypes.c_ushort),
        ("reserved2", ctypes.c_ushort),
        ("reserved3", ctypes.c_ushort),
        ("pointer", ctypes.c_void_p),
        ("unused", ctypes.c_void_p),
    ]


def _guid(text: str) -> _Guid:
    import uuid

    guid = _Guid()
    ctypes.memmove(ctypes.byref(guid), uuid.UUID(text).bytes_le, ctypes.sizeof(guid))
    return guid


_IID_PROPERTY_STORE = "886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"
_VT_EMPTY = 0
_VT_LPWSTR = 31
_CO_E_NOTINITIALIZED = 0x800401F0
_COINIT_APARTMENTTHREADED = 0x2
# IPropertyStore's vtable: IUnknown's three, GetCount, GetAt, GetValue, SetValue, Commit.
_RELEASE, _SET_VALUE, _COMMIT = 2, 6, 7


class WindowsNative:
    """`Native` through user32 (GetParent) and shell32's window property store."""

    def __init__(self) -> None:
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32")
        self._get_parent = user32.GetParent
        self._get_parent.argtypes = (wintypes.HWND,)
        self._get_parent.restype = wintypes.HWND
        self._is_window = user32.IsWindow
        self._is_window.argtypes = (wintypes.HWND,)
        self._is_window.restype = wintypes.BOOL
        shell32 = ctypes.WinDLL("shell32")
        self._get_store = shell32.SHGetPropertyStoreForWindow
        self._get_store.argtypes = (wintypes.HWND, ctypes.POINTER(_Guid), ctypes.POINTER(ctypes.c_void_p))
        self._get_store.restype = ctypes.c_long
        self._ole32 = ctypes.WinDLL("ole32")
        self._ole32.CoInitializeEx.argtypes = (ctypes.c_void_p, wintypes.DWORD)
        self._ole32.CoInitializeEx.restype = ctypes.c_long
        self._iid = _guid(_IID_PROPERTY_STORE)
        self._format = _guid(PROPERTY_FORMAT)
        self._release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)
        self._set_value = ctypes.WINFUNCTYPE(
            ctypes.c_long, ctypes.c_void_p, ctypes.POINTER(_PropertyKey), ctypes.POINTER(_PropVariant)
        )
        self._commit = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p)

    def wrapper(self, window_id: int) -> int:
        return int(self._get_parent(window_id) or 0)

    def set_properties(self, hwnd: int, values: Values) -> bool:
        if not self._is_window(hwnd):
            return False
        store = self._store(hwnd)
        if store is None:
            return False
        ok = True
        table = ctypes.cast(store, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        try:
            set_value = self._set_value(table[_SET_VALUE])
            for pid, text in values:
                key = _PropertyKey(self._format, pid)
                value = _PropVariant()
                buffer = ctypes.create_unicode_buffer(text or "")  # SetValue copies it
                if text is None:
                    value.vt = _VT_EMPTY
                else:
                    value.vt = _VT_LPWSTR
                    value.pointer = ctypes.addressof(buffer)
                result = int(set_value(store, ctypes.byref(key), ctypes.byref(value)))
                if result < 0:
                    log.warning("Taskbar property %s not set (HRESULT 0x%08X)", pid, result & 0xFFFFFFFF)
                    ok = False
            self._commit(table[_COMMIT])(store)
        finally:
            self._release(table[_RELEASE])(store)
        return ok

    def _store(self, hwnd: int) -> int | None:
        store = ctypes.c_void_p()
        result = int(self._get_store(hwnd, ctypes.byref(self._iid), ctypes.byref(store)))
        if (result & 0xFFFFFFFF) == _CO_E_NOTINITIALIZED:
            self._ole32.CoInitializeEx(None, _COINIT_APARTMENTTHREADED)
            result = int(self._get_store(hwnd, ctypes.byref(self._iid), ctypes.byref(store)))
        if result < 0 or not store.value:
            log.warning("No property store for the Forge window (HRESULT 0x%08X)", result & 0xFFFFFFFF)
            return None
        return store.value


def windows_native() -> WindowsNative | None:
    """The real Native; None off Windows or when it can't be made."""
    if sys.platform != "win32":
        return None
    try:
        return WindowsNative()
    except (OSError, AttributeError):
        log.warning("The Forge window's own taskbar button is unavailable", exc_info=True)
        return None


_native = windows_native()
