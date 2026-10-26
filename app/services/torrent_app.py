"""The torrent program on this PC: which one Windows opens magnets / .torrent files with,
and a one-click qBittorrent install through winget.

Detection only reads the registry (stdlib `winreg`, read access). The reader is injectable so
tests never touch the real registry. On other platforms nothing is detected or installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import PureWindowsPath

from app.i18n import t
from app.models import TorrentApp

log = logging.getLogger(__name__)

# (hive, key path, value name) -> the string value, or None when the key/value is missing.
# Hives are "HKCU" or "HKCR"; "" names a key's default value.
RegistryReader = Callable[[str, str, str], str | None]
ExistsCheck = Callable[[str], bool]

QBITTORRENT = "qBittorrent"
WINGET_COMMAND = (
    "install",
    "--id",
    "qBittorrent.qBittorrent",
    "--exact",
    "--silent",
    "--accept-package-agreements",
    "--accept-source-agreements",
)

_MAGNET_USER_CHOICE = (
    r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\magnet\UserChoice"
)
_TORRENT_USER_CHOICE = (
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts\.torrent\UserChoice"
)
_OPEN_COMMAND = r"shell\open\command"

# Checked in order: "qbittorrent" contains "bittorrent", so it must come first.
_KNOWN_NAMES: tuple[tuple[str, str], ...] = (
    ("qbittorrent", "qBittorrent"),
    ("utorrent", "µTorrent"),
    ("bittorrent", "BitTorrent"),
    ("deluge", "Deluge"),
    ("transmission", "Transmission"),
    ("tixati", "Tixati"),
    ("vuze", "Vuze"),
    ("azureus", "Vuze"),
    ("biglybt", "BiglyBT"),
)
_QUOTED_EXE = re.compile(r'^\s*"([^"]+)"')
_UNQUOTED_EXE = re.compile(r"^\s*(.+?\.exe)(?=\s|$)", re.IGNORECASE)


def find_torrent_app(
    read: RegistryReader | None = None, exists: ExistsCheck = os.path.isfile
) -> TorrentApp | None:
    """The program registered for magnet links (else for .torrent files), when its exe exists.

    Blocking (registry + file system): call it through `asyncio.to_thread` from the loop.
    """
    if read is None:
        if sys.platform != "win32":
            return None
        read = _read_registry
    for command in _candidate_commands(read):
        exe = command_executable(command)
        if exe and exists(exe):
            return TorrentApp(name=friendly_name(exe))
    return None


def _candidate_commands(read: RegistryReader) -> list[str]:
    """Open commands, the user's own choices first (magnet links before .torrent files)."""
    commands: list[str | None] = []
    if prog_id := read("HKCU", _MAGNET_USER_CHOICE, "ProgId"):
        commands.append(read("HKCR", rf"{prog_id}\{_OPEN_COMMAND}", ""))
    commands.append(read("HKCU", rf"Software\Classes\magnet\{_OPEN_COMMAND}", ""))
    commands.append(read("HKCR", rf"magnet\{_OPEN_COMMAND}", ""))
    if prog_id := read("HKCU", _TORRENT_USER_CHOICE, "ProgId"):
        commands.append(read("HKCR", rf"{prog_id}\{_OPEN_COMMAND}", ""))
    if prog_id := read("HKCR", ".torrent", ""):
        commands.append(read("HKCR", rf"{prog_id}\{_OPEN_COMMAND}", ""))
    return [command for command in commands if command]


def command_executable(command: str) -> str | None:
    """The program path in a shell open command such as '"C:\\...\\qbittorrent.exe" "%1"'."""
    text = os.path.expandvars(command)
    match = _QUOTED_EXE.match(text) or _UNQUOTED_EXE.match(text)
    if match is None:
        parts = text.split()
        return parts[0] if parts else None
    return match.group(1).strip()


def friendly_name(exe: str) -> str:
    stem = PureWindowsPath(exe).stem
    lowered = stem.lower()
    return next((name for key, name in _KNOWN_NAMES if key in lowered), stem)


def _read_registry(hive: str, key: str, value: str) -> str | None:
    if sys.platform != "win32":
        return None
    import winreg

    root = winreg.HKEY_CURRENT_USER if hive == "HKCU" else winreg.HKEY_CLASSES_ROOT
    try:
        with winreg.OpenKey(root, key, 0, winreg.KEY_READ) as handle:
            data, kind = winreg.QueryValueEx(handle, value)
    except OSError:
        return None
    if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not isinstance(data, str):
        return None
    return data.strip() or None


async def install_qbittorrent(
    progress: Callable[[str], None] | None = None,
    find: Callable[[], TorrentApp | None] = find_torrent_app,
) -> bool:
    """Install qBittorrent with winget (Windows shows a UAC prompt). True when winget succeeds
    and qBittorrent is then registered; False when winget is missing or fails."""

    def report(text: str) -> None:
        if progress is not None:
            progress(text)

    winget = shutil.which("winget")
    if sys.platform != "win32" or winget is None:
        log.info("qBittorrent install skipped: winget is not available")
        return False
    report(t("Installing qBittorrent…"))
    try:
        process = await asyncio.create_subprocess_exec(
            winget,
            *WINGET_COMMAND,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except OSError as exc:
        log.info("qBittorrent install failed: winget could not start (%s)", type(exc).__name__)
        return False
    try:
        await process.communicate()
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        raise
    if process.returncode != 0:
        log.info("qBittorrent install failed: winget exit code %s", process.returncode)
        return False
    report(t("Checking the installation…"))
    app = await asyncio.to_thread(find)
    if app is None or app.name != QBITTORRENT:
        log.info("winget finished, but qBittorrent is not registered for torrents")
        return False
    log.info("qBittorrent installed")
    return True


class TorrentAppService:
    """What the UI uses: which torrent program is set up, and installing qBittorrent."""

    def __init__(
        self,
        find: Callable[[], TorrentApp | None] = find_torrent_app,
    ) -> None:
        self._find = find

    async def find(self) -> TorrentApp | None:
        return await asyncio.to_thread(self._find)

    async def install_qbittorrent(
        self, progress: Callable[[str], None] | None = None
    ) -> bool:
        return await install_qbittorrent(progress, self._find)
