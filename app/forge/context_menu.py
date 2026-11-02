"""The Explorer right-click menu: a "MediaForge" submenu with one "Forge: <preset>" entry per
preset, for files and folders (ported from MediaForge's `platform/context_menu.py`).

Written under HKEY_CURRENT_USER (no administrator rights), at the same keys MediaForge used, so
Explorer never shows two "MediaForge" menus. Each entry starts this install:

    "<pythonw.exe>" "<install>\\main.py" --forge --confirm --preset "<preset>" "%1"

Explorer starts one process per selected file (`MultiSelectModel=Player`); main.py hands each
request to the running AirGrabber (app/single_instance.py, INTEGRATION.md 3.11).

Whose menu it is: the menu "starts this copy" when its first command holds this install's quoted
main.py. `refresh` (every Forge start, a preset change) only rewrites a menu that starts this
copy, so a development copy never takes over the installed one's menu, and neither ever takes
over the old MediaForge's (the import, F-26, is the one exception). `--forge-menu on|off`
(`menu_action`, install.ps1 on a new install, D8) likewise leaves another program's menu alone.
app/uninstall.ps1 removes the keys when their first command starts that install's main.py.

The registry is reached through `MenuRegistry` (tests pass a fake). The real one,
`WindowsRegistry`, goes through the module's `_read_value` / `_first_subkey` / `_key_exists`
(reads) and `_write_key` / `_delete_tree` (writes, HKCU only): tests/forge/base.py patches exactly
these names, so no test can change the real menu. Everything here blocks: call it through
`asyncio.to_thread` from the loop. Stdlib only.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Protocol

from app.i18n import N_, t

if TYPE_CHECKING:
    from app.config import AppPaths
    from app.forge.models.preset import Preset
    from app.forge.models.settings import ForgeSettings

log = logging.getLogger(__name__)

INSTALL_DIR = Path(__file__).resolve().parents[2]
MAIN_PY = INSTALL_DIR / "main.py"
ICON = INSTALL_DIR / "app" / "assets" / "forge" / "MediaForge.ico"

# Under HKEY_CURRENT_USER: every file, and folders (the same keys as MediaForge's).
BASES = (
    r"Software\Classes\*\shell\MediaForge",
    r"Software\Classes\Directory\shell\MediaForge",
)
# MediaForge's very first menu, under HKEY_LOCAL_MACHINE: only a .reg file run by an
# administrator removes it (`legacy_removal_reg`).
LEGACY_HKLM_KEYS = (
    r"SOFTWARE\Classes\*\shell\MediaForgeCommands",
    r"SOFTWARE\Classes\Folder\shell\MediaForgeCommands",
)
_LEGACY_COMMAND_STORE = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\CommandStore\shell"

MENU_NAME = "MediaForge"  # the submenu's name (a name, never translated)
ENTRY_TEXT = N_("Forge: {preset}")  # one entry per preset, in the current language
FORGE_FLAG = "--forge"  # main.py's Forge request; MediaForge's own commands don't have it

# `menu_action`'s exit codes (`main.py --forge-menu on|off`).
EXIT_OK = 0  # done, or nothing of this copy's to change (another program's menu is left alone)
EXIT_FAILED = 1  # the registry couldn't be changed (the log says why)
EXIT_USAGE = 2  # neither "on" nor "off"

Hive = Literal["HKCU", "HKLM"]


class MenuState(StrEnum):
    """Whose right-click menu Explorer has at BASES (judged by the files menu's first command)."""

    OFF = "off"  # no menu
    ON = "on"  # it starts this copy, with this Python
    STALE = "stale"  # it starts this copy's main.py, but with a Python that moved
    OTHER_COPY = "other_copy"  # it starts another AirGrabber (another folder)
    OLD_MEDIAFORGE = "old_mediaforge"  # it starts the old MediaForge (no --forge)

    @property
    def ours(self) -> bool:
        """The menu starts this copy (refresh and "off" may change it)."""
        return self in (MenuState.ON, MenuState.STALE)


@dataclass(frozen=True)
class RegEntry:
    key: str
    values: dict[str, str]  # "" is the key's default value


# --- the registry ------------------------------------------------------------------------------


class MenuRegistry(Protocol):
    """What the menu needs of the registry. Writes are HKEY_CURRENT_USER only."""

    def read(self, hive: Hive, key: str, name: str = "") -> str | None:
        """A string value ("" is the key's default); None when the key or value is missing."""
        ...

    def first_subkey(self, hive: Hive, key: str) -> str | None:
        """The name of the key's first subkey; None when it has none or doesn't exist."""
        ...

    def exists(self, hive: Hive, key: str) -> bool: ...

    def write(self, key: str, values: Mapping[str, str]) -> None:
        """Create the HKCU key (and its parents) and set these string values."""
        ...

    def delete_tree(self, key: str) -> None:
        """Delete the HKCU key with everything under it; nothing when it doesn't exist."""
        ...


class WindowsRegistry:
    """The real registry, through the module's guarded functions (looked up at each call)."""

    def read(self, hive: Hive, key: str, name: str = "") -> str | None:
        return _read_value(hive, key, name)

    def first_subkey(self, hive: Hive, key: str) -> str | None:
        return _first_subkey(hive, key)

    def exists(self, hive: Hive, key: str) -> bool:
        return bool(_key_exists(hive, key))

    def write(self, key: str, values: Mapping[str, str]) -> None:
        _write_key(key, values)

    def delete_tree(self, key: str) -> None:
        _delete_tree(key)


def _root(hive: Hive) -> int:
    import winreg

    return winreg.HKEY_CURRENT_USER if hive == "HKCU" else winreg.HKEY_LOCAL_MACHINE


def _read_value(hive: Hive, key: str, name: str) -> str | None:
    import winreg

    try:
        with winreg.OpenKey(_root(hive), key) as handle:
            value, _kind = winreg.QueryValueEx(handle, name)
    except OSError:
        return None
    return value if isinstance(value, str) else None


def _first_subkey(hive: Hive, key: str) -> str | None:
    import winreg

    try:
        with winreg.OpenKey(_root(hive), key) as handle:
            return winreg.EnumKey(handle, 0)
    except OSError:
        return None


def _key_exists(hive: Hive, key: str) -> bool:
    import winreg

    try:
        winreg.OpenKey(_root(hive), key).Close()
    except OSError:
        return False
    return True


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


def _registry(registry: MenuRegistry | None) -> MenuRegistry:
    return registry if registry is not None else WindowsRegistry()


# --- what the menu holds -----------------------------------------------------------------------


def python_runner(python: Path | None = None) -> Path:
    """The pythonw.exe beside `python` (sys.executable), so no console flashes; that Python
    itself when there is none (as install.ps1 and the Startup shortcut do)."""
    exe = Path(python if python is not None else sys.executable)
    pythonw = exe.with_name("pythonw.exe")
    return pythonw if pythonw.is_file() else exe


def command_prefix(python: Path | None = None, main_py: Path = MAIN_PY) -> list[str]:
    """How Explorer starts this copy: its Python and its main.py."""
    return [str(python_runner(python)), str(main_py)]


def default_icon() -> str:
    """MediaForge's icon, inside this install."""
    return str(ICON)


def menu_presets(all_names: Sequence[str], chosen: Sequence[str]) -> list[str]:
    """The presets the menu shows, in the presets' order: the chosen ones that still exist;
    none chosen (or none of them left) means every preset, so presets added later appear too."""
    kept = [name for name in all_names if name in chosen]
    return kept or list(all_names)


def follow_presets(
    chosen: Sequence[str],
    names: Sequence[str],
    renamed: Mapping[str, str],
    removed: Sequence[str] = (),
) -> list[str]:
    """The chosen presets after a Save renamed or deleted some (`names`: the presets now, in
    order): a renamed one stays chosen under its new name, a deleted one goes. [] (every
    preset) stays []; when none is left it becomes [] too (every preset, as `menu_presets`)."""
    if not chosen:
        return []
    moved = {renamed.get(name, name) for name in chosen if name not in removed}
    return [name for name in names if name in moved]


def _quote(part: str) -> str:
    return '"' + part.replace('"', "") + '"'


def _launcher(prefix: Sequence[str]) -> str:
    return " ".join(_quote(part) for part in prefix)


def build_entries(presets: Sequence[str], prefix: Sequence[str], icon: str) -> list[RegEntry]:
    """The keys and values of the whole menu, for files and for folders."""
    launcher = _launcher(prefix)
    entries: list[RegEntry] = []
    for base in BASES:
        entries.append(RegEntry(base, {"MUIVerb": MENU_NAME, "SubCommands": "", "Icon": icon}))
        for index, preset in enumerate(presets, start=1):
            safe = re.sub(r"[^A-Za-z0-9]", "", preset) or "Preset"
            item = rf"{base}\shell\{index:02d}_{safe}"
            entries.append(
                RegEntry(
                    item,
                    {"MUIVerb": t(ENTRY_TEXT, preset=preset), "MultiSelectModel": "Player"},
                )
            )
            command = f'{launcher} {FORGE_FLAG} --confirm --preset {_quote(preset)} "%1"'
            entries.append(RegEntry(rf"{item}\command", {"": command}))
    return entries


def _reg_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def to_reg_file(entries: Sequence[RegEntry], hive: str = "HKEY_CURRENT_USER") -> str:
    """A .reg file that adds these entries (save it as UTF-16, as regedit writes them)."""
    lines = ["Windows Registry Editor Version 5.00", ""]
    for entry in entries:
        lines.append(f"[{hive}\\{entry.key}]")
        for name, value in entry.values.items():
            label = "@" if name == "" else _reg_string(name)
            lines.append(f"{label}={_reg_string(value)}")
        lines.append("")
    return "\r\n".join(lines) + "\r\n"


# --- reading and writing the menu --------------------------------------------------------------


def first_command(base: str, registry: MenuRegistry | None = None) -> str | None:
    """The command line of the first entry under `base`; None when there is none."""
    reg = _registry(registry)
    shell = rf"{base}\shell"
    first = reg.first_subkey("HKCU", shell)
    if first is None:
        return None
    return reg.read("HKCU", rf"{shell}\{first}\command")


def installed_command(registry: MenuRegistry | None = None) -> str | None:
    """The files menu's first command line (to tell whose menu it is); None when it is off."""
    return first_command(BASES[0], registry)


def starts_copy(command: str, main_py: Path = MAIN_PY) -> bool:
    """Whether a menu command starts the copy whose main.py is `main_py` (its quoted path in
    the command, letter case aside; as `Test-MenuOwned` in app/uninstall.ps1)."""
    return f'"{main_py}"'.casefold() in command.casefold()


def command_state(
    command: str | None, *, python: Path | None = None, main_py: Path = MAIN_PY
) -> MenuState:
    """Whose menu a first command belongs to."""
    if command is None:
        return MenuState.OFF
    if starts_copy(command, main_py):
        expected = f"{_launcher(command_prefix(python, main_py))} {FORGE_FLAG} "
        return MenuState.ON if command.casefold().startswith(expected.casefold()) else MenuState.STALE
    if FORGE_FLAG in command.split():
        return MenuState.OTHER_COPY
    return MenuState.OLD_MEDIAFORGE


def menu_state(
    registry: MenuRegistry | None = None, *, python: Path | None = None, main_py: Path = MAIN_PY
) -> MenuState:
    return command_state(installed_command(registry), python=python, main_py=main_py)


def install(entries: Sequence[RegEntry], registry: MenuRegistry | None = None) -> None:
    """Replace the menu (whoever's it was) with `entries`. Raises OSError."""
    reg = _registry(registry)
    uninstall(reg)
    for entry in entries:
        reg.write(entry.key, entry.values)


def uninstall(registry: MenuRegistry | None = None) -> None:
    """Remove the menu, whoever's it is. Raises OSError."""
    reg = _registry(registry)
    for base in BASES:
        reg.delete_tree(base)


def remove_own(registry: MenuRegistry | None = None, *, main_py: Path = MAIN_PY) -> bool:
    """Remove each menu key whose first command starts this copy (another program's stays).
    True when something was removed. Raises OSError."""
    reg = _registry(registry)
    removed = False
    for base in BASES:
        command = first_command(base, reg)
        if command is not None and starts_copy(command, main_py):
            reg.delete_tree(base)
            removed = True
    return removed


def write_menu(
    all_names: Sequence[str],
    chosen: Sequence[str],
    registry: MenuRegistry | None = None,
    *,
    python: Path | None = None,
    main_py: Path = MAIN_PY,
) -> bool:
    """Put the menu for this copy in place with the presets it shows (`menu_presets`). False
    (nothing written) when there is no preset at all. Raises OSError."""
    names = menu_presets(all_names, chosen)
    if not names:
        return False
    install(build_entries(names, command_prefix(python, main_py), default_icon()), registry)
    return True


def refresh(
    all_names: Sequence[str],
    chosen: Sequence[str],
    registry: MenuRegistry | None = None,
    *,
    python: Path | None = None,
    main_py: Path = MAIN_PY,
) -> bool:
    """Rewrite the menu with the current presets (and this Python, in the current language) if
    it is on and starts this copy. A menu that is off, or starts another copy or the old
    MediaForge, is never taken over. True when it was rewritten. Raises OSError."""
    reg = _registry(registry)
    if not menu_state(reg, python=python, main_py=main_py).ours:
        return False
    return write_menu(all_names, chosen, reg, python=python, main_py=main_py)


# --- MediaForge's first (HKLM) menu ----------------------------------------------------------


def legacy_installed(registry: MenuRegistry | None = None) -> bool:
    """Whether MediaForge's very first menu (HKEY_LOCAL_MACHINE) is still there."""
    return _registry(registry).exists("HKLM", LEGACY_HKLM_KEYS[0])


def _legacy_subcommands(registry: MenuRegistry) -> list[str]:
    value = registry.read("HKLM", LEGACY_HKLM_KEYS[0], "SubCommands")
    return [name for name in (value or "").split(";") if name]


def legacy_removal_reg(registry: MenuRegistry | None = None) -> str:
    """A .reg file that removes the old HKLM menu (an administrator has to run it)."""
    hive = "HKEY_LOCAL_MACHINE"
    lines = ["Windows Registry Editor Version 5.00", ""]
    lines += [f"[-{hive}\\{key}]" for key in LEGACY_HKLM_KEYS]
    names = _legacy_subcommands(_registry(registry))
    lines += [f"[-{hive}\\{_LEGACY_COMMAND_STORE}\\{name}]" for name in names]
    return "\r\n".join(lines) + "\r\n"


# --- on the loop -------------------------------------------------------------------------------


class _Presets(Protocol):
    async def list(self) -> list[Preset]: ...


class _Settings(Protocol):
    @property
    def current(self) -> ForgeSettings: ...


class MenuSources(Protocol):
    """What `refresh_from` reads of ForgeServices."""

    @property
    def presets(self) -> _Presets: ...

    @property
    def settings(self) -> _Settings: ...


async def refresh_from(
    services: MenuSources, registry: MenuRegistry | None = None, *, main_py: Path = MAIN_PY
) -> bool:
    """`refresh` with Forge's presets and settings: at every Forge start and after a preset
    change. Never raises (a registry that can't be written is logged)."""
    try:
        names = [preset.name for preset in await services.presets.list()]
        chosen = list(services.settings.current.context_menu_presets)
        done = await asyncio.to_thread(refresh, names, chosen, registry, main_py=main_py)
    except Exception as exc:
        # The type only: a message could name a path with the user's name in it.
        log.warning("Couldn't refresh the right-click menu (%s)", type(exc).__name__)
        return False
    if done:
        log.debug("Right-click menu refreshed")
    return done


async def _stored_presets(paths: AppPaths) -> tuple[list[str], list[str]]:
    """The preset names and the menu's chosen ones from forge.db; the default presets when
    Forge has no database yet (a new install: none is made here)."""
    from app.forge.db.database import Database, db_path
    from app.forge.db.presets import PresetRepo
    from app.forge.db.settings import SettingsRepo
    from app.forge.models.preset import DEFAULT_PRESETS

    path = db_path(paths)
    if not path.is_file():
        return [preset.name for preset in DEFAULT_PRESETS], []
    db = await Database.open(path)
    try:
        names = [preset.name for preset in await PresetRepo(db).list()]
        chosen = list((await SettingsRepo(db).load()).context_menu_presets)
    finally:
        await db.close()
    return (names or [preset.name for preset in DEFAULT_PRESETS]), chosen


async def menu_action(
    paths: AppPaths,
    action: str,
    registry: MenuRegistry | None = None,
    *,
    python: Path | None = None,
    main_py: Path = MAIN_PY,
) -> int:
    """`main.py --forge-menu on|off` (no window): turn this copy's menu on (every preset, or
    the ones chosen in Settings) or off. A menu that starts another copy or the old MediaForge
    is left alone either way. Returns one of the EXIT_ codes; never raises."""
    if action not in ("on", "off"):
        log.error("--forge-menu takes on or off")
        return EXIT_USAGE
    reg = _registry(registry)
    try:
        state = await asyncio.to_thread(menu_state, reg, python=python, main_py=main_py)
        if action == "off":
            if state.ours:
                await asyncio.to_thread(remove_own, reg, main_py=main_py)
                log.info("Right-click menu removed")
            return EXIT_OK
        if state in (MenuState.OTHER_COPY, MenuState.OLD_MEDIAFORGE):
            log.info("Right-click menu left alone: it starts another program (%s)", state)
            return EXIT_OK
        names, chosen = await _stored_presets(paths)
        await asyncio.to_thread(write_menu, names, chosen, reg, python=python, main_py=main_py)
    except Exception as exc:
        log.error("Couldn't change the right-click menu (%s)", type(exc).__name__)
        return EXIT_FAILED
    log.info("Right-click menu on")
    return EXIT_OK
