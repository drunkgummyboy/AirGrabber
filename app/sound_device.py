"""Whether the sound output went away, for the player (app/media_player_window.py, its process).

Round 4 (owner, 2026-11-21): Bluetooth headphones switched off or a cable pulled while a video
plays pauses it, as a phone does. Windows then makes another output the default one; a thread
of its own (`SoundWatch`) asks Windows' Core Audio (IMMDeviceEnumerator, plain ctypes COM, no
package) every POLL_S which output is the default and, when it changed, whether the one before
is still there (`device_gone`): only then "gone" is handed on (`deliver`: the window's events
queue; no Tk here). Picking another output or plugging one in doesn't pause. Stdlib only;
logs no device names or ids.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import threading
from collections.abc import Callable
from typing import Any, Protocol

from app.media_player_logic import device_gone

log = logging.getLogger(__name__)

POLL_S = 1.0
_CLSID_ENUMERATOR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
_IID_ENUMERATOR = "{A95664D2-9614-4F35-A746-DE8DB63617E6}"
_CLSCTX_ALL = 0x17
_COINIT_MULTITHREADED = 0x0
_RENDER, _MULTIMEDIA = 0, 1
_ACTIVE = 0x1  # DEVICE_STATE_ACTIVE


class SoundWatchSeam(Protocol):
    """Where "the sound output went away" comes from (`SoundWatch`; tests: a fake)."""

    def start(self, deliver: Callable[[str], None]) -> None: ...

    def stop(self) -> None: ...


class _Guid(ctypes.Structure):
    _fields_ = [
        ("data1", ctypes.c_ulong),
        ("data2", ctypes.c_ushort),
        ("data3", ctypes.c_ushort),
        ("data4", ctypes.c_ubyte * 8),
    ]


def _call(obj: ctypes.c_void_p, index: int, *types: Any) -> Callable[..., int]:
    """The COM method at `index` of `obj`'s table, bound to `obj` (HRESULT as an int)."""
    table = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    method = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *types)(table[index])
    return lambda *args: int(method(obj, *args))


class _Endpoints:
    """Core Audio's device enumerator, on the thread that made it."""

    def __init__(self) -> None:
        ole32 = ctypes.WinDLL("ole32")
        self._ole32 = ole32
        ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
        clsid, iid = _Guid(), _Guid()
        ole32.CLSIDFromString(ctypes.c_wchar_p(_CLSID_ENUMERATOR), ctypes.byref(clsid))
        ole32.CLSIDFromString(ctypes.c_wchar_p(_IID_ENUMERATOR), ctypes.byref(iid))
        self._enumerator = ctypes.c_void_p()
        result = ole32.CoCreateInstance(
            ctypes.byref(clsid), None, _CLSCTX_ALL, ctypes.byref(iid),
            ctypes.byref(self._enumerator),
        )
        if result != 0 or not self._enumerator:
            raise OSError("no device enumerator")

    def _release(self, obj: ctypes.c_void_p) -> None:
        _call(obj, 2)()

    def _id(self, device: ctypes.c_void_p) -> str | None:
        text = ctypes.c_void_p()
        if _call(device, 5, ctypes.POINTER(ctypes.c_void_p))(ctypes.byref(text)) != 0:
            return None
        try:
            return ctypes.wstring_at(text.value) if text.value else None
        finally:
            self._ole32.CoTaskMemFree(text)

    def default(self) -> str | None:
        """The default output's id (None: there is none)."""
        device = ctypes.c_void_p()
        out = ctypes.POINTER(ctypes.c_void_p)
        get = _call(self._enumerator, 4, ctypes.c_int, ctypes.c_int, out)
        if get(_RENDER, _MULTIMEDIA, ctypes.byref(device)) != 0 or not device:
            return None
        try:
            return self._id(device)
        finally:
            self._release(device)

    def active(self, device_id: str) -> bool:
        device = ctypes.c_void_p()
        get = _call(self._enumerator, 5, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_void_p))
        if get(device_id, ctypes.byref(device)) != 0 or not device:
            return False
        try:
            state = ctypes.c_ulong()
            if _call(device, 6, ctypes.POINTER(ctypes.c_ulong))(ctypes.byref(state)) != 0:
                return False
            return bool(state.value & _ACTIVE)
        finally:
            self._release(device)


class SoundWatch:
    """SoundWatchSeam on Windows: a daemon thread polling the default output every POLL_S."""

    def __init__(self, poll_s: float = POLL_S) -> None:
        self._poll_s = poll_s
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, deliver: Callable[[str], None]) -> None:
        if self._thread is None:
            self._thread = threading.Thread(
                target=self._run, args=(deliver,), name="sound-watch", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        self._stopped.set()

    def _run(self, deliver: Callable[[str], None]) -> None:
        try:
            endpoints = _Endpoints()
        except Exception as exc:
            log.info("Sound output watch unavailable (%s)", type(exc).__name__)
            return
        previous: str | None = None
        first = True
        while not self._stopped.wait(0 if first else self._poll_s):
            first = False
            try:
                current = endpoints.default()
                if current != previous and previous is not None:
                    if device_gone(previous, current, endpoints.active(previous)):
                        deliver("sound_gone")
                previous = current
            except Exception as exc:  # one look missed, never the thread
                log.warning("Sound output watch failed (%s)", type(exc).__name__)
                previous = None


def sound_watch() -> SoundWatch | None:
    return SoundWatch() if sys.platform == "win32" else None
