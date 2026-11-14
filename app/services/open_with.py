"""Explorer's "Open with" AirGrabber player: any video file opened in AirGrabber's player
(docs/player/2026-11-14-open-with-design.md).

Written under HKEY_CURRENT_USER\\Software\\Classes only (no administrator rights), never as a
default and never into UserChoice: a ProgID `AirGrabber.Video` whose command starts this copy,

    "<pythonw.exe>" "<install>\\main.py" --play "%1"

(main.py hands the file to the running AirGrabber, app/single_instance.py), named in
`Application\\ApplicationName` ("AirGrabber player", in the app's language) with the app's
icon, and listed in each video extension's `OpenWithProgids` (local_files.VIDEO_SUFFIXES).
Windows then offers it in the "Open with" list; making it the default stays the user's own
choice in Windows.

Settings' `open_with_player` (Settings → Plex, on to start with) decides: `refresh` runs at each
start and when the switch changes. Whose it is: the registration "starts this copy" when its
command holds this install's quoted main.py; `refresh` only writes when there is none or it is
this copy's, and only removes this copy's, so a development copy never takes the installed one's.
app/uninstall.ps1 removes it when it starts that install.

The registry is reached through `Registry` (tests pass a fake); the real one goes through the
module's `_read_value` / `_write_key` / `_delete_tree` / `_delete_value` / `_assoc_changed`,
which tests/__init__.py replaces (writes raise, reads find nothing). Everything here blocks: call
it through `asyncio.to_thread`. Stdlib only. Logs say what was done, never a path.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from app.i18n import N_, t
from app.services.local_files import VIDEO_SUFFIXES

log = logging.getLogger(__name__)

INSTALL_DIR = Path(__file__).resolve().parents[2]
MAIN_PY = INSTALL_DIR / "main.py"
ICON = INSTALL_DIR / "app" / "assets" / "airgrabber.ico"

CLASSES = r"Software\Classes"
PROG_ID = "AirGrabber.Video"
PROG_KEY = rf"{CLASSES}\{PROG_ID}"
COMMAND_KEY = rf"{PROG_KEY}\shell\open\command"
PLAY_FLAG = "--play"
NAME_TEXT = N_("AirGrabber player")  # its name in the "Open with" list


class OpenWithState(StrEnum):
    OFF = "off"  # not registered
    OURS = "ours"  # it starts this copy
    OTHER = "other"  # it starts another copy: left alone


@dataclass(frozen=True, slots=True)
class RegEntry:
    key: str
    values: dict[str, str]  # "" is the key's default value


class Registry(Protocol):
    """What the registration needs of HKEY_CURRENT_USER."""

    def read(self, key: str, name: str = "") -> str | None:
        """A string value ("" the key's default); None when the key or value is missing."""
        ...

    def write(self, key: str, values: Mapping[str, str]) -> None:
        """Create the key (and its parents) and set these string values."""
        ...

    def delete_tree(self, key: str) -> None:
        """Delete the key with everything under it; nothing when it doesn't exist."""
        ...

    def delete_value(self, key: str, name: str) -> None:
        """Delete one value; nothing when it doesn't exist."""
        ...

    def associations_changed(self) -> None:
        """Tell Explorer the file types changed (it reads them again)."""
        ...


class WindowsRegistry:
    """The real registry, through the module's guarded functions (looked up at each call)."""

    def read(self, key: str, name: str = "") -> str | None:
        return _read_value(key, name)

    def write(self, key: str, values: Mapping[str, str]) -> None:
        _write_key(key, values)

    def delete_tree(self, key: str) -> None:
        _delete_tree(key)

    def delete_value(self, key: str, name: str) -> None:
        _delete_value(key, name)

    def associations_changed(self) -> None:
        _assoc_changed()


def _read_value(key: str, name: str) -> str | None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as handle:
            value, _kind = winreg.QueryValueEx(handle, name)
    except OSError:
        return None
    return value if isinstance(value, str) else None


def _write_key(key: str, values: Mapping[str, str]) -> None:
    import winreg

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key) as handle:
        for name, value in values.items():
            winreg.SetValueEx(handle, name, 0, winreg.REG_SZ, value)


def _delete_tree(key: str) -> None:
    import winreg

    try:
        handle = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_ALL_ACCESS)
    except FileNotFoundError:
        return
    with handle:
        while True:
            try:
                child = winreg.EnumKey(handle, 0)
            except OSError:
                break
            _delete_tree(rf"{key}\{child}")
    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)


def _delete_value(key: str, name: str) -> None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key, 0, winreg.KEY_SET_VALUE) as handle:
            winreg.DeleteValue(handle, name)
    except FileNotFoundError:
        return


def _assoc_changed() -> None:
    import ctypes

    if sys.platform == "win32":
        # SHChangeNotify(SHCNE_ASSOCCHANGED, SHCNF_IDLIST, NULL, NULL)
        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0x0000, None, None)


async def apply(on: bool) -> None:
    """`refresh` from the loop (in a thread: the registry blocks), in the app's language; a
    registry that refuses is logged, never raised."""
    name = t(NAME_TEXT)
    try:
        await asyncio.to_thread(refresh, on, name=name)
    except OSError as exc:
        log.warning("Open with: the registry refused (%s)", type(exc).__name__)


def _extension_key(suffix: str) -> str:
    return rf"{CLASSES}\{suffix}\OpenWithProgids"


def python_runner(python: Path | None = None) -> Path:
    """The pythonw.exe beside `python` (sys.executable), so no console flashes; that Python
    itself when there is none."""
    exe = Path(python if python is not None else sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return pythonw if pythonw.is_file() else exe


def command(python: Path, main_py: Path) -> str:
    return f'"{python}" "{main_py}" {PLAY_FLAG} "%1"'


def entries(*, name: str, python: Path, main_py: Path, icon: str) -> list[RegEntry]:
    """What the registration holds."""
    found = [
        RegEntry(PROG_KEY, {"": name, "FriendlyTypeName": name}),
        RegEntry(rf"{PROG_KEY}\DefaultIcon", {"": icon}),
        RegEntry(rf"{PROG_KEY}\Application", {"ApplicationName": name, "ApplicationIcon": icon}),
        RegEntry(rf"{PROG_KEY}\shell\open", {"FriendlyAppName": name}),
        RegEntry(COMMAND_KEY, {"": command(python, main_py)}),
    ]
    found += [RegEntry(_extension_key(suffix), {PROG_ID: ""}) for suffix in sorted(VIDEO_SUFFIXES)]
    return found


def state(registry: Registry, main_py: Path = MAIN_PY) -> OpenWithState:
    """Whose registration there is."""
    current = registry.read(COMMAND_KEY)
    if not current:
        return OpenWithState.OFF
    if f'"{main_py}"'.casefold() in current.casefold():
        return OpenWithState.OURS
    return OpenWithState.OTHER


def refresh(
    on: bool,
    registry: Registry | None = None,
    *,
    name: str,
    main_py: Path = MAIN_PY,
    python: Path | None = None,
    icon: str = str(ICON),
) -> bool:
    """Write this copy's registration (`on`) or remove it; another copy's is left alone.
    Whether anything changed. Raises OSError when the registry refuses."""
    registry = registry if registry is not None else WindowsRegistry()
    whose = state(registry, main_py)
    if whose is OpenWithState.OTHER:
        log.info("Open with: another copy's registration is left alone")
        return False
    if not on:
        if whose is OpenWithState.OFF:
            return False
        registry.delete_tree(PROG_KEY)
        for suffix in sorted(VIDEO_SUFFIXES):
            registry.delete_value(_extension_key(suffix), PROG_ID)
        registry.associations_changed()
        log.info("Open with: AirGrabber's player removed")
        return True
    changed = False
    for entry in entries(name=name, python=python_runner(python), main_py=main_py, icon=icon):
        if any(registry.read(entry.key, key) != value for key, value in entry.values.items()):
            registry.write(entry.key, entry.values)
            changed = True
    if changed:
        registry.associations_changed()
        log.info("Open with: AirGrabber's player registered")
    return changed
