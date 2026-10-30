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
from app.models import (
    QBittorrentInstallResult,
    QBittorrentSetupResult,
    QBittorrentState,
    TorrentApp,
)
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
# `--source winget`: only winget's own catalogue. Without it winget also searches the Microsoft
# Store source, and when that one fails (e.g. its certificate check) winget asks which source to
# use instead of installing.
WINGET_COMMAND = (
    "install",
    "--id",
    "qBittorrent.qBittorrent",
    "--exact",
    "--source",
    "winget",
    "--silent",
    "--accept-package-agreements",
    "--accept-source-agreements",
)

# winget's exit codes (github.com/microsoft/winget-cli, doc/.../winget/returnCodes.md) and the
# Windows errors it passes on, as unsigned 32-bit numbers, by what they mean for the user.
_OFFLINE_CODES = frozenset(
    {
        0x8A150008,  # DOWNLOAD_FAILED
        0x8A15000F,  # SOURCE_DATA_MISSING
        0x8A15002E,  # DOWNLOAD_SIZE_MISMATCH
        0x8A150045,  # SOURCE_OPEN_FAILED
        0x8A15004B,  # FAILED_TO_OPEN_ALL_SOURCES
        0x8A15005E,  # PINNED_CERTIFICATE_MISMATCH
        0x8A15006B,  # DOWNLOAD_DEPENDENCIES
        0x8A150086,  # INSTALLER_ZERO_BYTE_FILE
        0x8A150107,  # INSTALL_NO_NETWORK
        0x80072EE2,  # WinINet / WinHTTP: timed out
        0x80072EE7,  # name not resolved
        0x80072EFD,  # cannot connect
        0x80072EFE,  # connection aborted
        0x80072EFF,  # connection reset
        0x80072F7D,  # secure channel error
        0x80072F8F,  # secure failure
    }
)
_DECLINED_CODES = frozenset(
    {
        0x8A15010C,  # INSTALL_CANCELLED_BY_USER
        0x800704C7,  # ERROR_CANCELLED (the permission prompt answered No)
        1223,  # ERROR_CANCELLED, as an installer's own exit code
    }
)
_BUSY_CODES = frozenset({0x8A150102, 0x80070652, 1618})  # another installation is running
_NO_SPACE_CODES = frozenset({0x8A150105, 0x80070070})  # INSTALL_DISK_FULL, ERROR_DISK_FULL
_BLOCKED_CODES = frozenset({0x8A15003A, 0x8A15010F})  # BLOCKED_BY_POLICY, INSTALL_BLOCKED_...
_RESTART_CODES = frozenset({0x8A150109, 0x8A15010A})  # REBOOT_REQUIRED_TO_FINISH / _FOR_INSTALL
# qBittorrent is there already: whether Windows opens torrents with it decides.
_ALREADY_CODES = frozenset(
    {
        0x8A15002B,  # UPDATE_NOT_APPLICABLE
        0x8A150061,  # PACKAGE_ALREADY_INSTALLED
        0x8A15010D,  # INSTALL_ALREADY_INSTALLED
        0x8A15010E,  # INSTALL_DOWNGRADE
    }
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


def install_problem(returncode: int) -> QBittorrentInstallResult | None:
    """Why winget's `returncode` means qBittorrent wasn't installed; None for success (0) and
    for "it is there already", where only a look at the registry tells."""
    code = returncode & 0xFFFFFFFF  # Windows' exit codes may arrive signed or unsigned
    if code == 0 or code in _ALREADY_CODES:
        return None
    for codes, problem in (
        (_OFFLINE_CODES, QBittorrentInstallResult.OFFLINE),
        (_DECLINED_CODES, QBittorrentInstallResult.DECLINED),
        (_BUSY_CODES, QBittorrentInstallResult.BUSY),
        (_NO_SPACE_CODES, QBittorrentInstallResult.NO_SPACE),
        (_BLOCKED_CODES, QBittorrentInstallResult.BLOCKED),
        (_RESTART_CODES, QBittorrentInstallResult.RESTART),
    ):
        if code in codes:
            return problem
    return QBittorrentInstallResult.FAILED


async def install_qbittorrent(
    progress: Callable[[str], None] | None = None,
    find: Callable[[], TorrentApp | None] = find_torrent_app,
) -> QBittorrentInstallResult:
    """Install qBittorrent with winget (Windows shows a UAC prompt). INSTALLED when winget
    succeeds (or finds it there already) and Windows then opens torrents with qBittorrent; else
    why not (`install_problem`; NO_WINGET when winget is missing or won't start, NOT_LINKED when
    qBittorrent is there but isn't the torrent program). Only the exit code is logged."""

    def report(text: str) -> None:
        if progress is not None:
            progress(text)

    winget = shutil.which("winget")
    if sys.platform != "win32" or winget is None:
        log.info("qBittorrent install skipped: winget is not available")
        return QBittorrentInstallResult.NO_WINGET
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
        return QBittorrentInstallResult.NO_WINGET
    try:
        await process.communicate()
    except asyncio.CancelledError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        raise
    returncode = process.returncode if process.returncode is not None else -1
    if (problem := install_problem(returncode)) is not None:
        log.info("qBittorrent install failed: winget exit code %s", process.returncode)
        return problem
    if returncode != 0:
        log.info("winget exit code %s: qBittorrent is there already", process.returncode)
    report(t("Checking the installation…"))
    app = await asyncio.to_thread(find)
    if app is None or app.name != QBITTORRENT:
        log.info("winget finished, but qBittorrent is not registered for torrents")
        return QBittorrentInstallResult.NOT_LINKED
    log.info("qBittorrent installed")
    return QBittorrentInstallResult.INSTALLED


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
    ) -> QBittorrentInstallResult:
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
