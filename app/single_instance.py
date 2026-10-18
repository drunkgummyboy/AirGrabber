"""One AirGrabber at a time (Windows).

The running copy holds the named mutex `Local\\AirGrabber`. A second start finds it there, sets the
named auto-reset event `Local\\AirGrabber-show` and exits; the running copy polls that event
(`show_requested`, non-blocking) and brings its window back.

Stdlib only: main.py uses this before the package check. Elsewhere than Windows everything is a
no-op and `acquire` always succeeds.
"""

from __future__ import annotations

import ctypes
import functools
import logging
import sys
from typing import Any

log = logging.getLogger(__name__)

MUTEX_NAME = "Local\\AirGrabber"
SHOW_EVENT_NAME = "Local\\AirGrabber-show"

_ERROR_ALREADY_EXISTS = 183
_WAIT_OBJECT_0 = 0
_ASFW_ANY = 0xFFFFFFFF  # AllowSetForegroundWindow: any process


class InstanceLock:
    """The single-instance lock of one process. Names are injectable for tests."""

    def __init__(self, mutex_name: str = MUTEX_NAME, show_event_name: str = SHOW_EVENT_NAME) -> None:
        self._mutex_name = mutex_name
        self._event_name = show_event_name
        self._mutex: int | None = None
        self._event: int | None = None

    def acquire(self) -> bool:
        """Claim the lock. False: another AirGrabber holds it (call `signal_show`, then exit).

        True outside Windows, and also when Windows refuses to create the objects: two copies
        running beats none starting.
        """
        if sys.platform != "win32":
            return True
        kernel32 = _kernel32()
        # The event first, so a second start that finds the mutex taken can always reach it.
        self._event = kernel32.CreateEventW(None, False, False, self._event_name) or None
        ctypes.set_last_error(0)
        mutex = kernel32.CreateMutexW(None, False, self._mutex_name)
        error = ctypes.get_last_error()
        if not mutex:
            log.warning("Could not create the single-instance lock (error %d); starting anyway", error)
            return True
        if error == _ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(mutex)  # only the running copy keeps the mutex alive
            return False
        self._mutex = mutex
        return True

    def signal_show(self) -> bool:
        """Ask the running copy to show its window (after `acquire` returned False)."""
        if sys.platform != "win32" or self._event is None:
            return False
        # This process was just started by the user, so it may bring another one to the front.
        _user32().AllowSetForegroundWindow(_ASFW_ANY)
        return bool(_kernel32().SetEvent(self._event))

    def show_requested(self) -> bool:
        """Whether a second start asked for the window since the last call. Never blocks."""
        if sys.platform != "win32" or self._event is None:
            return False
        return bool(_kernel32().WaitForSingleObject(self._event, 0) == _WAIT_OBJECT_0)

    def release(self) -> None:
        """Let a new copy start (at exit, and before a restart). Safe to call more than once."""
        if sys.platform != "win32":
            return
        mutex, self._mutex = self._mutex, None
        event, self._event = self._event, None
        for handle in (mutex, event):  # the mutex first: a start in between then just runs
            if handle is not None:
                _kernel32().CloseHandle(handle)


@functools.cache
def _kernel32() -> Any:
    """kernel32 with typed prototypes (64-bit handles), separate from ctypes.windll's copy."""
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CreateEventW.argtypes = (
        wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR,
    )  # fmt: skip
    kernel32.CreateEventW.restype = wintypes.HANDLE
    kernel32.SetEvent.argtypes = (wintypes.HANDLE,)
    kernel32.SetEvent.restype = wintypes.BOOL
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


@functools.cache
def _user32() -> Any:
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.AllowSetForegroundWindow.argtypes = (wintypes.DWORD,)
    user32.AllowSetForegroundWindow.restype = wintypes.BOOL
    return user32
