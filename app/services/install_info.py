"""This copy's entry in Windows' "Installed apps", kept up to date from the app side.

install.ps1's `Register-Uninstaller` creates the entry when it installs. `ensure_uninstall_entry`
mirrors that function exactly (key path, value names, value types and formats), so copies installed
before the installer did this get the entry on their next start, and an automatic update refreshes
its DisplayVersion. Keep both in step: change one, change the other.

Current user only (HKCU, no administrator permission needed). Stdlib only: main.py calls it after
shutdown. The registry access is injectable so tests never touch the real registry.
"""

from __future__ import annotations

import logging
import math
import ntpath
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

# All of these as in install.ps1 (Register-Uninstaller and the constants at its top).
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\AirGrabber"  # under HKCU
DISPLAY_NAME = "AirGrabber"
PUBLISHER = "drunkgummyboy"
ABOUT_URL = "https://github.com/drunkgummyboy/AirGrabber"
ICON_PATH = r"app\assets\airgrabber.ico"  # $IconPath; also app/ui/branding.py
UNINSTALLER = r"app\uninstall.ps1"
UNINSTALL_COMMAND = "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "

RegistryValue = str | int  # REG_SZ / REG_DWORD


class Registry(Protocol):
    """Values of keys under HKEY_CURRENT_USER."""

    def read(self, key: str) -> dict[str, RegistryValue] | None:
        """The key's string and DWORD values by name; None when the key does not exist."""
        ...

    def write(self, key: str, values: Mapping[str, RegistryValue]) -> None:
        """Create the key when missing and set `values`: str as REG_SZ, int as REG_DWORD."""
        ...


def ensure_uninstall_entry(
    install_dir: Path, version: str, *, registry: Registry | None = None
) -> None:
    """Create the "Installed apps" entry for the copy in `install_dir` when there is none, or set
    its DisplayVersion to `version` when the entry is this copy's. An entry for another folder is
    never touched. Registry errors are logged, not raised.

    Call it for installed copies only (never a development copy; see updates.is_development_copy).
    Blocking (registry + file system): call it through `asyncio.to_thread` from the event loop.
    """
    if registry is None:
        if sys.platform != "win32":
            return
        registry = WindowsRegistry()
    root = _install_location(install_dir)
    try:
        existing = registry.read(UNINSTALL_KEY)
        if existing is None:
            registry.write(UNINSTALL_KEY, entry_values(root, version))
            log.info("Added AirGrabber %s to Installed apps", version)
            return
        location = existing.get("InstallLocation")
        if not isinstance(location, str) or not same_path(location, root):
            log.info("The Installed apps entry belongs to another AirGrabber folder; left alone")
            return
        if existing.get("DisplayVersion") != version:
            registry.write(UNINSTALL_KEY, {"DisplayVersion": version})
            log.info("Installed apps entry now shows version %s", version)
    except OSError as exc:
        log.warning("Could not update the Installed apps entry: %r", exc)


def entry_values(root: str, version: str) -> dict[str, RegistryValue]:
    """Every value Register-Uninstaller writes, for the install folder `root`."""
    uninstall = f'{UNINSTALL_COMMAND}"{ntpath.join(root, UNINSTALLER)}"'
    return {
        "DisplayName": DISPLAY_NAME,
        "DisplayVersion": version,
        "Publisher": PUBLISHER,
        "DisplayIcon": ntpath.join(root, ICON_PATH),
        "InstallLocation": root,
        "URLInfoAbout": ABOUT_URL,
        "UninstallString": uninstall,
        "QuietUninstallString": uninstall + " -Quiet",
        "NoModify": 1,
        "NoRepair": 1,
        "EstimatedSize": math.ceil(_folder_bytes(root) / 1024),  # KB
    }


def same_path(a: str, b: str) -> bool:
    """As uninstall.ps1's Test-SamePath: full paths, no trailing backslash, case-insensitive."""
    if not a or not b:
        return False
    return _normalize(a).casefold() == _normalize(b).casefold()


def _normalize(path: str) -> str:
    return os.path.abspath(path).rstrip("\\/")


def _install_location(install_dir: Path) -> str:
    """The folder as Register-Uninstaller stores it: full path without a trailing backslash."""
    return os.path.abspath(install_dir).rstrip("\\")


def _folder_bytes(root: str) -> int:
    """Total size of every file below `root`, hidden ones included; unreadable ones are skipped."""
    total = 0
    for folder, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(folder, name))
            except OSError:
                pass
    return total


class WindowsRegistry:
    """The real HKEY_CURRENT_USER (Windows only)."""

    def read(self, key: str) -> dict[str, RegistryValue] | None:
        if sys.platform != "win32":
            return None
        import winreg

        try:
            handle = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_READ)
        except FileNotFoundError:
            return None
        values: dict[str, RegistryValue] = {}
        with handle:
            index = 0
            while True:
                try:
                    name, data, kind = winreg.EnumValue(handle, index)
                except OSError:  # no more values
                    break
                index += 1
                if kind in (winreg.REG_SZ, winreg.REG_EXPAND_SZ) and isinstance(data, str):
                    values[name] = data
                elif kind == winreg.REG_DWORD and isinstance(data, int):
                    values[name] = data
        return values

    def write(self, key: str, values: Mapping[str, RegistryValue]) -> None:
        if sys.platform != "win32":
            return
        import winreg

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as handle:
            for name, value in values.items():
                if isinstance(value, int):
                    winreg.SetValueEx(handle, name, 0, winreg.REG_DWORD, value)
                else:
                    winreg.SetValueEx(handle, name, 0, winreg.REG_SZ, value)
