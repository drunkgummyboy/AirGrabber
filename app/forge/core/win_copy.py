"""Windows' CopyFile2 (ctypes) for a move between shares: progress and a Stop.

`copier()` gives `copy_file`, or None away from Windows or without CopyFile2 (then fsops copies
in chunks as before). `copy_file(src, dst, progress, stop)` is blocking: it runs in the same
worker thread as the rest of the move (fsops is always called through `asyncio.to_thread`), and
so do its callbacks. `progress(copied, size)` hears every chunk; `stop()` is asked at every
callback and, when true, cancels the copy (COPYFILE2_PROGRESS_CANCEL): CopyFile2 removes what it
wrote and this raises CopyCancelled. COPY_FILE_FAIL_IF_EXISTS: never over a file that is there
(FileExistsError). Other failures are the OSError Windows names (`ctypes.WinError`). Nothing
here logs; the callbacks' own exceptions end the copy and are raised again here.
Stop is checked only when Windows calls back (a chunk started or finished), so it can't end a
copy stuck on a hung share until that read or write returns.
"""

from __future__ import annotations

import ctypes
import errno
import sys
from collections.abc import Callable

from app.i18n import N_

Progress = Callable[[int, int], None]  # (bytes copied, the file's size), on the copy's thread
Stop = Callable[[], bool]  # True: end the copy now
NativeCopy = Callable[[str, str, Progress | None, Stop | None], None]

COPY_FILE_FAIL_IF_EXISTS = 0x00000001
_CHUNK_FINISHED = 2  # COPYFILE2_CALLBACK_CHUNK_FINISHED
_CONTINUE = 0  # COPYFILE2_PROGRESS_CONTINUE
_CANCEL = 1  # COPYFILE2_PROGRESS_CANCEL
ERROR_REQUEST_ABORTED = 1235  # what a cancelled copy returns (as an HRESULT)
_WIN32_FACILITY = 0x80070000  # HRESULT_FROM_WIN32's top half
COPY_STOPPED = N_("The copy was stopped")  # CopyCancelled's text, kept in the journal: t() it


class CopyCancelled(OSError):
    """The copy was stopped on request; CopyFile2 left nothing behind."""


def _code(hresult: int) -> int:
    """The Windows error code inside an HRESULT (HRESULT_FROM_WIN32), else the HRESULT."""
    if hresult & 0xFFFF0000 == _WIN32_FACILITY:
        return hresult & 0xFFFF
    return hresult & 0xFFFFFFFF


if sys.platform == "win32":
    from ctypes import wintypes

    class _ChunkFinished(ctypes.Structure):
        _fields_ = [
            ("dwStreamNumber", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("hSourceFile", wintypes.HANDLE),
            ("hDestinationFile", wintypes.HANDLE),
            ("uliChunkNumber", ctypes.c_ulonglong),
            ("uliChunkSize", ctypes.c_ulonglong),
            ("uliStreamSize", ctypes.c_ulonglong),
            ("uliStreamBytesTransferred", ctypes.c_ulonglong),
            ("uliTotalFileSize", ctypes.c_ulonglong),
            ("uliTotalBytesTransferred", ctypes.c_ulonglong),
        ]

    class _Info(ctypes.Union):
        _fields_ = [("ChunkFinished", _ChunkFinished), ("_room", ctypes.c_ulonglong * 12)]

    class _Message(ctypes.Structure):  # COPYFILE2_MESSAGE
        _fields_ = [("Type", ctypes.c_int), ("dwPadding", wintypes.DWORD), ("Info", _Info)]

    _ROUTINE = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.POINTER(_Message), ctypes.c_void_p)

    class _Parameters(ctypes.Structure):  # COPYFILE2_EXTENDED_PARAMETERS
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("dwCopyFlags", wintypes.DWORD),
            ("pfCancel", ctypes.POINTER(wintypes.BOOL)),
            ("pProgressRoutine", _ROUTINE),
            ("pvCallbackContext", ctypes.c_void_p),
        ]

    def _load() -> Callable[..., int] | None:
        try:
            function = ctypes.WinDLL("kernel32", use_last_error=True).CopyFile2
        except (OSError, AttributeError):  # before Windows 8
            return None
        function.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.POINTER(_Parameters)]
        function.restype = ctypes.c_long  # HRESULT, checked here
        return function

    _COPY_FILE_2 = _load()

    def copy_file(src: str, dst: str, progress: Progress | None, stop: Stop | None) -> None:
        """Copy `src` to the new file `dst` (both may be extended-length paths)."""
        assert _COPY_FILE_2 is not None
        raised: list[BaseException] = []

        def routine(message: ctypes._Pointer[_Message], _context: int | None) -> int:
            try:
                if stop is not None and stop():
                    return _CANCEL
                if progress is not None and message.contents.Type == _CHUNK_FINISHED:
                    chunk = message.contents.Info.ChunkFinished
                    progress(chunk.uliTotalBytesTransferred, chunk.uliTotalFileSize)
            except BaseException as exc:  # never into Windows' own stack
                raised.append(exc)
                return _CANCEL
            return _CONTINUE

        callback = _ROUTINE(routine)  # kept alive for the whole call
        parameters = _Parameters(
            ctypes.sizeof(_Parameters), COPY_FILE_FAIL_IF_EXISTS, None, callback, None
        )
        hresult = _COPY_FILE_2(src, dst, ctypes.byref(parameters))
        if raised:
            raise raised[0]
        if hresult >= 0:
            return
        code = _code(hresult)
        if code == ERROR_REQUEST_ABORTED:
            raise CopyCancelled(errno.ECANCELED, COPY_STOPPED)
        raise ctypes.WinError(code)

    def copier() -> NativeCopy | None:
        """CopyFile2's copy, or None where Windows has none."""
        return copy_file if _COPY_FILE_2 is not None else None

else:

    def copier() -> NativeCopy | None:
        """No CopyFile2 away from Windows: fsops copies in chunks."""
        return None
