"""Report a problem (Settings → Updates): AirGrabber's logs and a few facts about this PC in one zip
on the desktop, for the user to send to whoever helps them with AirGrabber.

Nothing is sent anywhere: the user sends the file themselves. What goes in is what the log rules
(CLAUDE.md) already keep free of keys, tokens, passwords, paths and titles: `airgrabber.log` with
its older parts and `install.log`, plus `about.txt` (AirGrabber's, Windows' and Python's versions
and when the file was made). No settings, no database. The log line about it gives no file name.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import platform
import sys
import zipfile
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

LOG_FILES = (
    "airgrabber.log",
    "airgrabber.log.1",
    "airgrabber.log.2",
    "airgrabber.log.3",
    "install.log",
)
ABOUT_FILE = "about.txt"
_CSIDL_DESKTOPDIRECTORY = 0x0010
_MAX_PATH = 260


def report_name(now: datetime) -> str:
    """The zip's name: the moment it was made, to the second (two never share one)."""
    return f"AirGrabber problem report {now:%Y-%m-%d %H-%M-%S}.zip"


def desktop_folder() -> Path | None:
    """The user's desktop (Windows knows where, also when OneDrive moved it); None without one."""
    if sys.platform == "win32":
        buffer = ctypes.create_unicode_buffer(_MAX_PATH)
        shell = ctypes.windll.shell32  # type: ignore[attr-defined,unused-ignore]
        if shell.SHGetFolderPathW(None, _CSIDL_DESKTOPDIRECTORY, None, 0, buffer) == 0:
            found = Path(buffer.value)
            if buffer.value and found.is_dir():
                return found
    fallback = Path.home() / "Desktop"
    return fallback if fallback.is_dir() else None


def about_text(version: str, now: datetime) -> str:
    return (
        f"AirGrabber {version}\n"
        f"Windows: {platform.platform()}\n"
        f"Python: {platform.python_version()}\n"
        f"Made: {now.isoformat(timespec='seconds')}\n"
    )


def make_report(logs: Path, into: Path, *, version: str, now: datetime) -> Path:
    """Write the zip into `into` and return it (blocking: run it through a thread). The logs that
    aren't there are left out. Raises OSError when the zip can't be written."""
    target = into / report_name(now)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(ABOUT_FILE, about_text(version, now))
        for name in LOG_FILES:
            source = logs / name
            if source.is_file():
                archive.write(source, name)
    return target


class ProblemReports:
    """Makes the report file (Settings → Updates' "Report a problem")."""

    def __init__(
        self,
        logs: Path,
        fallback: Path,
        version: str,
        *,
        desktop: Callable[[], Path | None] = desktop_folder,
        now: Callable[[], datetime] = datetime.now,
    ) -> None:
        """`logs`: the logs folder; `fallback`: where the file goes without a desktop (the data
        folder)."""
        self._logs = logs
        self._fallback = fallback
        self._version = version
        self._desktop = desktop
        self._now = now

    async def save(self) -> tuple[Path, bool]:
        """Make the report file: (where it is, whether that's the desktop). Raises OSError."""
        desktop = await asyncio.to_thread(self._desktop)
        into = desktop if desktop is not None else self._fallback
        path = await asyncio.to_thread(
            make_report, self._logs, into, version=self._version, now=self._now()
        )
        log.info("Made a problem report (%s)", "desktop" if desktop is not None else "data folder")
        return path, desktop is not None
