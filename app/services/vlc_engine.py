"""VLC's engine for AirGrabber's own player: does this PC have a libVLC the player can use?

Usable means: a VLC in one of `playback.VLC_PLACES`' folders (Program Files, then Program Files
(x86); a VLC only on the PATH doesn't count) whose `libvlc.dll` is built for x64 (the machine field
of its PE header, read from the file's first HEAD_BYTES with the stdlib) and is VLC 3 or newer
(`libvlccore.dll` beside it), and the python-vlc binding importable (`importlib.util.find_spec`,
never imported here; it is optional in requirements.txt). Anything else counts as "without VLC":
▶ Play then opens VLC or Windows' player as before.

`find_engine` reads files, so the app calls it through `asyncio.to_thread`, once per run.
`engine_problem` says why there is none for the log line (an English word, never a path). The
reads are seams (`is_file`, `read_head`, `has_binding`) that tests replace.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import PureWindowsPath
from typing import Literal

from app.services.playback import VLC_PLACES

HEAD_BYTES = 4096  # the PE header sits in the first few hundred bytes
LIBVLC = "libvlc.dll"
LIBVLCCORE = "libvlccore.dll"  # VLC 3's core library beside libvlc.dll
MACHINE_X64 = 0x8664  # IMAGE_FILE_MACHINE_AMD64

EngineProblem = Literal["no_vlc", "not_64bit", "no_binding"]


@dataclass(frozen=True, slots=True)
class Engine:
    """A usable libVLC: `folder` is the VLC folder holding libvlc.dll."""

    folder: str


def _read_head(path: str) -> bytes:
    """The file's first HEAD_BYTES (blocking: called through `asyncio.to_thread`)."""
    with open(path, "rb") as file:
        return file.read(HEAD_BYTES)


def _has_binding() -> bool:
    """python-vlc can be imported (looked up, not imported)."""
    try:
        return importlib.util.find_spec("vlc") is not None
    except (ImportError, ValueError):
        return False


def pe_machine(head: bytes) -> int | None:
    """The machine field of a PE file's header (0x8664 x64, 0x014c x86); None when `head` isn't
    the start of a PE file."""
    if head[:2] != b"MZ" or len(head) < 0x40:
        return None
    offset = int.from_bytes(head[0x3C:0x40], "little")
    if head[offset : offset + 4] != b"PE\0\0" or len(head) < offset + 6:
        return None
    return int.from_bytes(head[offset + 4 : offset + 6], "little")


def _vlc_folders(environ: Mapping[str, str]) -> list[str]:
    """The folders of VLC_PLACES' vlc.exe, in their order (never the PATH)."""
    folders: list[str] = []
    for variable, below in VLC_PLACES:
        root = environ.get(variable)
        if root:
            folder = str(PureWindowsPath(root, below).parent)
            if folder.casefold() not in (seen.casefold() for seen in folders):
                folders.append(folder)
    return folders


def _look(
    environ: Mapping[str, str],
    is_file: Callable[[str], bool],
    read_head: Callable[[str], bytes],
    has_binding: Callable[[], bool],
) -> tuple[Engine | None, EngineProblem | None]:
    found: str | None = None
    only_32bit = False
    for folder in _vlc_folders(environ):
        library = str(PureWindowsPath(folder, LIBVLC))
        try:
            if not is_file(library) or not is_file(str(PureWindowsPath(folder, LIBVLCCORE))):
                continue
            machine = pe_machine(read_head(library))
        except OSError:
            continue
        if machine == MACHINE_X64:
            found = folder
            break
        if machine is not None:
            only_32bit = True
    if found is None:
        return None, "not_64bit" if only_32bit else "no_vlc"
    if not has_binding():
        return None, "no_binding"
    return Engine(folder=found), None


def find_engine(
    environ: Mapping[str, str] = os.environ,
    *,
    is_file: Callable[[str], bool] = os.path.isfile,
    read_head: Callable[[str], bytes] = _read_head,
    has_binding: Callable[[], bool] = _has_binding,
) -> Engine | None:
    """The first of VLC_PLACES' folders with a 64-bit VLC 3's libvlc.dll, when python-vlc is
    importable; else None. Blocking (file reads): call it through `asyncio.to_thread`."""
    return _look(environ, is_file, read_head, has_binding)[0]


def engine_problem(
    environ: Mapping[str, str] = os.environ,
    *,
    is_file: Callable[[str], bool] = os.path.isfile,
    read_head: Callable[[str], bytes] = _read_head,
    has_binding: Callable[[], bool] = _has_binding,
) -> EngineProblem | None:
    """Why `find_engine` finds none, for the log line: no VLC (or one older than 3, or a file
    that doesn't read), only a 32-bit one, or no python-vlc; None when there is an engine.
    Blocking like `find_engine`."""
    return _look(environ, is_file, read_head, has_binding)[1]
