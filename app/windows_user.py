"""What Windows knows about the user that AirGrabber's defaults follow (stdlib only): their
Downloads folder (`downloads_folder`: where Windows has it, which may be moved to another drive
or a OneDrive folder, not always %USERPROFILE%\\Downloads). TMDB's region stays "US" for
everyone (the owner, 2026-11-21).

It asks Windows through ctypes in a small function of its own (`_known_downloads`), which tests
replace (tests/__init__.py); off Windows, or when Windows won't say, it falls back. Nothing here
is logged (a path is the user's)."""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

# FOLDERID_Downloads: {374DE290-123F-4565-9164-39C4925E467B}
_DOWNLOADS_ID = (0x374DE290, 0x123F, 0x4565, (0x91, 0x64, 0x39, 0xC4, 0x92, 0x5E, 0x46, 0x7B))


def downloads_folder(known: Callable[[], str | None] | None = None) -> Path:
    """The user's Downloads folder as Windows has it (SHGetKnownFolderPath, `known` in tests);
    %USERPROFILE%\\Downloads when Windows won't say or off Windows."""
    try:
        found = (known or _known_downloads)()
    except OSError:
        found = None
    return Path(found) if found else Path.home() / "Downloads"


def _known_downloads() -> str | None:
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class Guid(ctypes.Structure):
        _fields_ = [
            ("data1", wintypes.DWORD),
            ("data2", wintypes.WORD),
            ("data3", wintypes.WORD),
            ("data4", ctypes.c_ubyte * 8),
        ]

    first, second, third, rest = _DOWNLOADS_ID
    guid = Guid(first, second, third, (ctypes.c_ubyte * 8)(*rest))
    path = ctypes.c_void_p()
    try:
        shell32, ole32 = ctypes.windll.shell32, ctypes.windll.ole32
        shell32.SHGetKnownFolderPath.argtypes = [
            ctypes.POINTER(Guid),
            wintypes.DWORD,
            wintypes.HANDLE,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        shell32.SHGetKnownFolderPath.restype = wintypes.LONG  # an HRESULT, without the raise
        ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
        result = shell32.SHGetKnownFolderPath(ctypes.byref(guid), 0, None, ctypes.byref(path))
    except (AttributeError, OSError):
        return None
    try:
        return ctypes.wstring_at(path.value) if result == 0 and path.value else None
    finally:
        if path.value:
            ole32.CoTaskMemFree(path)
