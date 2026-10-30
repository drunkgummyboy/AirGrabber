"""The torrent program on this PC: which one Windows opens magnets / .torrent files with,
a one-click qBittorrent install through winget, and qBittorrent set up for AirGrabber (no "Add
new torrent" pop-up for each download, and AirGrabber's look: app/services/qbittorrent_setup.py;
its Web UI for AirGrabber: app/services/qbittorrent_local.py), and qbittorrent.exe's path
(`qbittorrent_executable`, for starting it).

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
from app.models import QBittorrentSetupResult, QBittorrentState, TorrentApp
from app.services import qbittorrent_local
from app.services.qbittorrent_setup import QBittorrentSetup
from app.services.settings import SettingsService

log = logging.getLogger(__name__)

# (hive, key path, value name) -> the string value, or None when the key/value is missing.
# Hives are "HKCU" or "HKCR"; "" names a key's default value.
RegistryReader = Callable[[str, str, str], str | None]
ExistsCheck = Callable[[str], bool]

QBITTORRENT = "qBittorrent"
# install.ps1's Install-QBittorrentIfNeeded runs the same winget command: keep the two in step.
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


def qbittorrent_executable(
    read: RegistryReader | None = None, exists: ExistsCheck = os.path.isfile
) -> str | None:
    """qbittorrent.exe's path, from the same registry entries as `find_torrent_app` (the first
    of them that opens with qBittorrent), so AirGrabber can start it. Blocking."""
    if read is None:
        if sys.platform != "win32":
            return None
        read = _read_registry
    for command in _candidate_commands(read):
        exe = command_executable(command)
        if exe and friendly_name(exe) == QBITTORRENT and exists(exe):
            return exe
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
    """What the UI uses: which torrent program is set up, installing qBittorrent, and setting
    qBittorrent up for AirGrabber (its settings file: `qbittorrent`, injectable for tests; with
    `settings`, the setup also turns its Web UI on and points AirGrabber at it when it may:
    app/services/qbittorrent_local.py)."""

    def __init__(
        self,
        find: Callable[[], TorrentApp | None] = find_torrent_app,
        qbittorrent: QBittorrentSetup | None = None,
        *,
        settings: SettingsService | None = None,
    ) -> None:
        self._find = find
        self._qbittorrent = qbittorrent if qbittorrent is not None else QBittorrentSetup()
        self._settings = settings

    async def find(self) -> TorrentApp | None:
        return await asyncio.to_thread(self._find)

    async def install_qbittorrent(
        self, progress: Callable[[str], None] | None = None
    ) -> bool:
        return await install_qbittorrent(progress, self._find)

    async def qbittorrent_state(self) -> QBittorrentState | None:
        """qBittorrent's settings file (set up for AirGrabber or not, its save folder); None
        when it can't be read."""
        try:
            return await asyncio.to_thread(self._qbittorrent.state)
        except OSError as exc:
            log.info("qBittorrent's settings couldn't be read (%s)", type(exc).__name__)
            return None

    async def set_up_qbittorrent(self) -> QBittorrentSetupResult:
        """Turn qBittorrent's "Add new torrent" pop-up off and give it AirGrabber's look when it
        has no theme yet (not while qBittorrent runs); with settings, also its Web UI for
        AirGrabber (a first run that isn't done yet counts as a new install)."""
        if self._settings is None:
            return await asyncio.to_thread(self._qbittorrent.set_up)
        fresh = not self._settings.current.setup_done
        return await qbittorrent_local.set_up_qbittorrent(
            self._qbittorrent, self._settings, fresh=fresh
        )

    async def qbittorrent_look(self) -> bool | None:
        """Whether qBittorrent has AirGrabber's look on; None when there's nothing to switch
        (a theme of the user's own in use) or the settings can't be read."""
        try:
            return await asyncio.to_thread(self._qbittorrent.look)
        except OSError as exc:
            log.info("qBittorrent's settings couldn't be read (%s)", type(exc).__name__)
            return None

    async def set_qbittorrent_look(self, on: bool) -> QBittorrentSetupResult:
        """Turn AirGrabber's look in qBittorrent on or off (not while qBittorrent runs)."""
        return await asyncio.to_thread(self._qbittorrent.set_look, on)

    async def refresh_qbittorrent_theme(self) -> None:
        """Bring AirGrabber's copy of its qBittorrent theme up to date after an update (once
        per start; only when qBittorrent was given the look before)."""
        await asyncio.to_thread(self._qbittorrent.refresh_theme)
