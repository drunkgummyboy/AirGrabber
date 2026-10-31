"""The torrent program on this PC: which one Windows opens magnets / .torrent files with,
a one-click qBittorrent install through winget, and qBittorrent set up for AirGrabber (no "Add
new torrent" pop-up for each download, and AirGrabber's look: app/services/qbittorrent_setup.py;
its Web UI for AirGrabber: app/services/qbittorrent_local.py), and qbittorrent.exe's path
(`qbittorrent_executable`, for starting it).

Detection reads the registry (stdlib `winreg`, read access). qBittorrent 5's installer only
registers its ProgIds (`qBittorrent.File.Torrent`, `qBittorrent.Url.Magnet`) and Capabilities
for Windows' Default apps, and qBittorrent 5 no longer offers to become the default on Windows, so
on a fresh PC nothing opens torrents with it. `QBittorrentLinker` then finds the installed
qbittorrent.exe (read-only: its install / uninstall keys in both registry views, its ProgIds, the
Program Files folders) and links it: it writes per-user keys under HKCU\\Software\\Classes only
(`magnet` and `.torrent`, the format qBittorrent 4's own "use qBittorrent for ..." wrote;
`association_values`) and tells the shell. Only when no other torrent program is set: never over
another live program, never over Windows' own choice (UserChoice, which it never touches), never
in HKLM. Linking runs from the service's `find` (the welcome and Settings), after installing
with winget and from main.py `--setup-qbittorrent`. The reader, writer, file check and shell
notification are injectable so tests never touch the real registry. On other platforms nothing
is detected, linked or installed.
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
from collections.abc import Callable, Iterator, Mapping
from enum import StrEnum
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
# Hives are "HKCU", "HKCR", "HKLM" (the 64-bit view) and "HKLM32" (the 32-bit view, where
# qBittorrent's 32-bit NSIS installer writes); "" names a key's default value.
RegistryReader = Callable[[str, str, str], str | None]
# (key path under HKCU\Software\Classes, value name, data): a REG_SZ value, the key created when
# missing. Raises OSError. Nothing else in the registry is ever written.
RegistryWriter = Callable[[str, str, str], None]
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
_USER_CLASSES = r"Software\Classes"

# Where an installed qbittorrent.exe is found (qBittorrent's installer, dist/windows in
# github.com/qbittorrent/qBittorrent: `InstallLocation` and the uninstall entry, written by the
# 32-bit NSIS installer without SetRegView, so in the 32-bit view; its ProgIds in
# HKLM\Software\Classes; 4.x used the ProgId "qBittorrent").
_QBITTORRENT_EXE = "qbittorrent.exe"
_INSTALL_KEY = r"SOFTWARE\qBittorrent"
_APP_PATHS_KEY = r"Software\Microsoft\Windows\CurrentVersion\App Paths\qbittorrent.exe"
_UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\qBittorrent"
_MACHINE_HIVES = ("HKLM", "HKLM32")
TORRENT_PROG_ID = "qBittorrent.File.Torrent"  # qBittorrent 5's installer's .torrent ProgId
LEGACY_PROG_ID = "qBittorrent"  # qBittorrent 4's .torrent ProgId (its installer's format)
_QBITTORRENT_PROG_IDS = (TORRENT_PROG_ID, "qBittorrent.Url.Magnet", LEGACY_PROG_ID)
_ICON_INDEX = re.compile(r",\s*-?\d+\s*$")
SHCNE_ASSOCCHANGED = 0x08000000
SHCNF_IDLIST = 0

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

    roots = {
        "HKCU": (winreg.HKEY_CURRENT_USER, 0),
        "HKCR": (winreg.HKEY_CLASSES_ROOT, 0),
        "HKLM": (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
        "HKLM32": (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
    }
    if hive not in roots:
        return None
    root, view = roots[hive]
    try:
        with winreg.OpenKey(root, key, 0, winreg.KEY_READ | view) as handle:
            data, kind = winreg.QueryValueEx(handle, value)
    except OSError:
        return None
    if kind not in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) or not isinstance(data, str):
        return None
    return data.strip() or None


def _write_user_class(key: str, value: str, data: str) -> None:
    """REG_SZ `value` = `data` under HKCU\\Software\\Classes\\`key`. Raises OSError."""
    if sys.platform != "win32":
        raise OSError("the registry is only written on Windows")
    import winreg

    path = rf"{_USER_CLASSES}\{key}"
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, path, 0, winreg.KEY_SET_VALUE) as handle:
        winreg.SetValueEx(handle, value, 0, winreg.REG_SZ, data)


def _notify_associations_changed() -> None:
    """Tell Explorer the file / link associations changed (as qBittorrent's own code did)."""
    if sys.platform != "win32":
        return
    import ctypes

    ctypes.windll.shell32.SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, None, None)


# -- an installed qBittorrent, and linking it ----------------------------------------------------


def _in_folder(folder: str) -> str:
    return str(PureWindowsPath(os.path.expandvars(folder.strip().strip('"'))) / _QBITTORRENT_EXE)


def _icon_executable(icon: str) -> str:
    """The file in an icon reference such as '"C:\\...\\qbittorrent.exe",0'."""
    return os.path.expandvars(_ICON_INDEX.sub("", icon).strip().strip('"'))


def _installed_candidates(read: RegistryReader, environ: Mapping[str, str]) -> Iterator[str]:
    """Where qbittorrent.exe may be, most reliable first."""
    for hive in _MACHINE_HIVES:
        if folder := read(hive, _INSTALL_KEY, "InstallLocation"):
            yield _in_folder(folder)
    for hive in ("HKCU", *_MACHINE_HIVES):
        if path := read(hive, _APP_PATHS_KEY, ""):
            yield _icon_executable(path)
    for hive in (*_MACHINE_HIVES, "HKCU"):
        if icon := read(hive, _UNINSTALL_KEY, "DisplayIcon"):
            yield _icon_executable(icon)
        if folder := read(hive, _UNINSTALL_KEY, "InstallLocation"):
            yield _in_folder(folder)
        if uninstaller := read(hive, _UNINSTALL_KEY, "UninstallString"):
            if exe := command_executable(uninstaller):
                yield str(PureWindowsPath(exe).parent / _QBITTORRENT_EXE)
    for prog_id in _QBITTORRENT_PROG_IDS:
        if command := read("HKCR", rf"{prog_id}\{_OPEN_COMMAND}", ""):
            if exe := command_executable(command):
                yield exe
    for variable in ("ProgramW6432", "ProgramFiles", "ProgramFiles(x86)"):
        if base := environ.get(variable):
            yield str(PureWindowsPath(base, "qBittorrent", _QBITTORRENT_EXE))
    if local := environ.get("LOCALAPPDATA"):
        yield str(PureWindowsPath(local, "Programs", "qBittorrent", _QBITTORRENT_EXE))


def find_installed_qbittorrent(
    read: RegistryReader, exists: ExistsCheck, environ: Mapping[str, str]
) -> str | None:
    """qbittorrent.exe's path when qBittorrent is installed, whether or not Windows opens
    torrents with it. Read-only; blocking."""
    for exe in _installed_candidates(read, environ):
        if friendly_name(exe) == QBITTORRENT and exists(exe):
            return exe
    return None


def association_values(
    exe: str, torrent_prog_id: str, old_torrent_prog_id: str | None = None
) -> list[tuple[str, str, str]]:
    """What linking writes, as (key under HKCU\\Software\\Classes, value name, data): the magnet
    link handler and the .torrent ProgId exactly as qBittorrent 4 wrote them when you answered
    Yes (Utils::OS::setMagnetLinkAssoc / setTorrentFileAssoc, release-4.6.7); the .torrent
    ProgId is the installer's own `TORRENT_PROG_ID` when that one opens qBittorrent, else
    qBittorrent 4's per-user `LEGACY_PROG_ID` (its installer's values). A different earlier
    .torrent ProgId is kept under OpenWithProgids, as qBittorrent did."""
    command = f'"{exe}" "%1"'
    icon = f'"{exe}",1'
    values = [
        ("magnet", "", "URL:Magnet link"),
        ("magnet", "Content Type", "application/x-magnet"),
        (r"magnet\DefaultIcon", "", icon),
        (r"magnet\shell", "", "open"),
        (rf"magnet\{_OPEN_COMMAND}", "", command),
        ("magnet", "URL Protocol", ""),
    ]
    if torrent_prog_id == LEGACY_PROG_ID:
        values += [
            (LEGACY_PROG_ID, "", "qBittorrent Torrent File"),
            (LEGACY_PROG_ID, "FriendlyTypeName", "qBittorrent Torrent File"),
            (rf"{LEGACY_PROG_ID}\shell", "", "open"),
            (rf"{LEGACY_PROG_ID}\{_OPEN_COMMAND}", "", command),
            (rf"{LEGACY_PROG_ID}\DefaultIcon", "", icon),
        ]
    if old_torrent_prog_id and old_torrent_prog_id not in _QBITTORRENT_PROG_IDS:
        values.append((r".torrent\OpenWithProgids", old_torrent_prog_id, ""))
    values += [
        (".torrent", "", torrent_prog_id),
        (".torrent", "Content Type", "application/x-bittorrent"),
    ]
    return values


class LinkOutcome(StrEnum):
    """What `QBittorrentLinker.link_if_unlinked` did."""

    ALREADY = "already"  # Windows opens torrents with qBittorrent already
    LINKED = "linked"  # it does now
    OTHER = "other"  # another torrent program is set: left alone
    USER_CHOICE = "user_choice"  # Windows' own choice points elsewhere: only Default apps can
    NOT_INSTALLED = "not_installed"  # no qbittorrent.exe on this PC
    FAILED = "failed"  # writing failed, or Windows still doesn't open torrents with it


_LINK_LOG = {
    LinkOutcome.LINKED: "qBittorrent linked for magnet links and .torrent files",
    LinkOutcome.OTHER: "qBittorrent not linked: another torrent program is set",
    LinkOutcome.USER_CHOICE: (
        "qBittorrent not linked: Windows' own choice (Default apps) points elsewhere"
    ),
    LinkOutcome.FAILED: "qBittorrent linked, but Windows still doesn't open torrents with it",
}


def _user_choice_block(read: RegistryReader, exists: ExistsCheck) -> LinkOutcome | None:
    """Why Windows' own choice (UserChoice, for magnet links or .torrent files) rules linking out:
    OTHER when it opens another program that exists, USER_CHOICE when it can't be read as a
    program here (a store app, a removed one): linking can't change what Windows uses then."""
    for key in (_MAGNET_USER_CHOICE, _TORRENT_USER_CHOICE):
        prog_id = read("HKCU", key, "ProgId")
        if not prog_id:
            continue
        command = read("HKCR", rf"{prog_id}\{_OPEN_COMMAND}", "")
        exe = command_executable(command) if command else None
        if exe is None or not exists(exe):
            return LinkOutcome.USER_CHOICE
        if friendly_name(exe) != QBITTORRENT:
            return LinkOutcome.OTHER
    return None


class QBittorrentLinker:
    """Finds an installed qbittorrent.exe and makes Windows open magnet links and .torrent files
    with it, when no other torrent program is set (module docstring). Blocking: call it through
    `asyncio.to_thread`. Everything it touches is injectable (tests: a fake registry)."""

    def __init__(
        self,
        read: RegistryReader | None = None,
        write: RegistryWriter | None = None,
        exists: ExistsCheck = os.path.isfile,
        notify: Callable[[], None] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._windows = read is not None or sys.platform == "win32"
        self._read = read if read is not None else _read_registry
        self._write = write if write is not None else _write_user_class
        self._exists = exists
        self._notify = notify if notify is not None else _notify_associations_changed
        self._environ = environ if environ is not None else os.environ

    def find(self) -> TorrentApp | None:
        return find_torrent_app(self._read, self._exists)

    def installed_executable(self) -> str | None:
        if not self._windows:
            return None
        return find_installed_qbittorrent(self._read, self._exists, self._environ)

    def link_if_unlinked(self) -> LinkOutcome:
        """Link qBittorrent when it's installed and nothing else is set; never raises OSError.
        Only the outcome is logged."""
        app = self.find()
        if app is not None:
            outcome = LinkOutcome.ALREADY if app.name == QBITTORRENT else LinkOutcome.OTHER
        elif (exe := self.installed_executable()) is None:
            outcome = LinkOutcome.NOT_INSTALLED
        elif (block := _user_choice_block(self._read, self._exists)) is not None:
            outcome = block
        else:
            outcome = self._link(exe)
        if outcome in _LINK_LOG:
            log.info(_LINK_LOG[outcome])
        return outcome

    def _link(self, exe: str) -> LinkOutcome:
        torrent_command = self._read("HKCR", rf"{TORRENT_PROG_ID}\{_OPEN_COMMAND}", "")
        installer_exe = command_executable(torrent_command) if torrent_command else None
        usable = (
            installer_exe is not None
            and friendly_name(installer_exe) == QBITTORRENT
            and self._exists(installer_exe)
        )
        prog_id = TORRENT_PROG_ID if usable else LEGACY_PROG_ID
        old = self._read("HKCU", rf"{_USER_CLASSES}\.torrent", "")
        try:
            for key, value, data in association_values(exe, prog_id, old):
                self._write(key, value, data)
        except OSError as exc:
            log.info(
                "qBittorrent not linked: the registry couldn't be written (%s)",
                type(exc).__name__,
            )
            return LinkOutcome.FAILED
        try:
            self._notify()
        except OSError as exc:
            log.info("The shell wasn't told about the change (%s)", type(exc).__name__)
        app = self.find()
        if app is not None and app.name == QBITTORRENT:
            return LinkOutcome.LINKED
        return LinkOutcome.FAILED


def link_after_install(linker: QBittorrentLinker | None = None) -> LinkOutcome:
    """main.py `--setup-qbittorrent` (install.ps1, right after it installed qBittorrent): link it
    under the same rule. Never raises."""
    try:
        return (linker if linker is not None else QBittorrentLinker()).link_if_unlinked()
    except Exception as exc:  # install.ps1 goes on with the setup either way
        log.info("Linking qBittorrent failed (%s)", type(exc).__name__)
        return LinkOutcome.FAILED


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
    linker: QBittorrentLinker | None = None,
) -> QBittorrentInstallResult:
    """Install qBittorrent with winget (Windows shows a UAC prompt). INSTALLED when winget
    succeeds (or finds it there already) and Windows then opens torrents with qBittorrent; else
    why not (`install_problem`; NO_WINGET when winget is missing or won't start, NOT_LINKED when
    qBittorrent is there but isn't the torrent program). With `linker`, an unlinked qBittorrent
    is linked first (no other program set), and when qbittorrent.exe is there already winget
    isn't run at all (no reinstall, no UAC prompt): only linked and checked. Only the exit code
    is logged."""

    def report(text: str) -> None:
        if progress is not None:
            progress(text)

    if linker is not None and await _installed(linker):
        log.info("qBittorrent is installed already: linking it, no winget")
        report(t("Checking the installation…"))
        return await _linked_result(find, linker)
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
    return await _linked_result(find, linker)


async def _installed(linker: QBittorrentLinker) -> bool:
    return await asyncio.to_thread(linker.installed_executable) is not None


async def _linked_result(
    find: Callable[[], TorrentApp | None], linker: QBittorrentLinker | None
) -> QBittorrentInstallResult:
    """INSTALLED when Windows opens torrents with qBittorrent (linked first when nothing is set
    and there's a `linker`), else NOT_LINKED."""
    app = await asyncio.to_thread(find)
    if app is None and linker is not None:
        if await asyncio.to_thread(linker.link_if_unlinked) is LinkOutcome.LINKED:
            app = await asyncio.to_thread(find)
    if app is None or app.name != QBITTORRENT:
        log.info("qBittorrent is there, but Windows doesn't open torrents with it")
        return QBittorrentInstallResult.NOT_LINKED
    log.info("qBittorrent installed")
    return QBittorrentInstallResult.INSTALLED


class TorrentAppService:
    """What the UI uses: which torrent program is set up, installing qBittorrent, and setting
    qBittorrent up for AirGrabber (its settings file: `qbittorrent`, injectable for tests; with
    `settings`, the setup also turns its Web UI on and points AirGrabber at it when it may:
    app/services/qbittorrent_local.py). With `linker` (Services.create: the real registry), an
    installed qBittorrent that nothing opens torrents with yet is linked when looked for or
    installed; without one, nothing is ever written."""

    def __init__(
        self,
        find: Callable[[], TorrentApp | None] = find_torrent_app,
        qbittorrent: QBittorrentSetup | None = None,
        *,
        settings: SettingsService | None = None,
        linker: QBittorrentLinker | None = None,
    ) -> None:
        self._find = find
        self._qbittorrent = qbittorrent if qbittorrent is not None else QBittorrentSetup()
        self._settings = settings
        self._linker = linker

    async def find(self) -> TorrentApp | None:
        """The torrent program; when none is set but qBittorrent is installed, it's linked first
        (with a linker)."""
        app = await asyncio.to_thread(self._find)
        if app is None and self._linker is not None:
            if await asyncio.to_thread(self._linker.link_if_unlinked) is LinkOutcome.LINKED:
                app = await asyncio.to_thread(self._find)
        return app

    async def install_qbittorrent(
        self, progress: Callable[[str], None] | None = None
    ) -> QBittorrentInstallResult:
        return await install_qbittorrent(progress, self._find, self._linker)

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
