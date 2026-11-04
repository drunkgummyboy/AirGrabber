"""Bringing an old MediaForge 1.x install's data over, once (docs/forge/INTEGRATION.md 3.10, D4).

Finding it: the old install's "Installed apps" entry (HKCU
`Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\MediaForge`: `InstallLocation`,
`UninstallString`, `DisplayVersion`) and its database `%APPDATA%\\MediaForge\\mediaforge.db`.
Both are injectable (`find_old_install(registry=, appdata=)`); `MEDIAFORGE_HOME` (MediaForge's
own development override) is ignored. The real registry is read only through
`_read_uninstall_values` and %APPDATA% only through `_appdata`: tests/forge/base.py patches
both, so no test ever sees the owner's real install.

Bringing it over (`bring_over`), refused while the old MediaForge runs (its mutex
`Local\\MediaForge-<user>`, `old_app_running`, ported from MediaForge's
`platform/instance_name.instance_running`):
1. a snapshot of the old database through SQLite's backup API from a read-only connection
   (`mode=ro`: the old file is never written, and the snapshot is consistent even if the old
   app wrote meanwhile) into a temporary folder under `AppPaths.forge`;
2. the snapshot opened as a Forge database (app/forge/db: MediaForge's schema v1 / v2 are
   Forge's, later migrations run on the copy; a schema newer than this build is refused and
   said so plainly), its settings rewritten as `ForgeSettings` (the fields AirGrabber owns, the
   TMDB key among them, vanish; the trailer tools are downloaded fresh, D12:
   `trailer_tools_ok` reset; `imported_from` set; `window_geometry`, now unread, stays);
3. forge.db written from it with SQLite's backup API while Forge runs (the Forge database's
   other connection sees the new rows at its next statement), under Forge's `run_lock` so no
   rename run writes meanwhile; then Forge's settings service is told (`settings.update` with
   the imported settings: the renamer is reconfigured as after a Settings save);
4. the old TMDB key goes into AirGrabber's settings only when AirGrabber has none (never
   logged); an old right-click menu (it starts the old MediaForge) is rewritten to start this
   AirGrabber (app/forge/context_menu.py: the one time another program's menu is taken over);
5. a marker file `imported-from-mediaforge` beside forge.db (phase 3's farewell release looks
   for it).

While the old install is still registered, an imported Manual / On is stored as it is but the new
renamer stays idle (`blocked_by_old_install`, asked by ForgeRuntime before the renamer starts;
the import sets AutoRenamer's flag itself; the result says so with `waits_for_removal`).

Forge's data is replaced as a whole: the caller asks first when Forge already has data of its
own (`has_own_data`). Presets, folders, Off / Manual / On, notifications, `kept_back`,
remembered matches, history and the TMDB cache come over as they are. MediaForge 1.x's
single-file JSON import is not ported (MediaForge 1.3's database already holds that data).

Never logs paths, the TMDB key or exception messages (they could name the user's folders).
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import getpass
import logging
import os
import re
import sqlite3
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import aiosqlite

from app.i18n import N_, t

if TYPE_CHECKING:
    from app.config import AppPaths
    from app.forge.context_menu import MenuRegistry
    from app.forge.models.history import RunSummary
    from app.forge.models.match import RememberedMatch
    from app.forge.models.preset import Preset
    from app.forge.models.settings import ForgeSettings
    from app.models import Settings

log = logging.getLogger(__name__)

UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\MediaForge"  # under HKCU
DATA_FOLDER = "MediaForge"  # under %APPDATA%
OLD_DB_NAME = "mediaforge.db"
MARKER_NAME = "imported-from-mediaforge"  # beside forge.db
_SNAPSHOT_NAME = "mediaforge.db"
_TEMP_PREFIX = "mediaforge-import-"
_SYNCHRONIZE = 0x00100000

# Forge's settings the owner chooses: any of them away from its default is "data of its own".
OWN_FIELDS: tuple[str, ...] = (
    "tmdb_language",
    "scan_subfolders",
    "context_menu_presets",
    "watch_folders",
    "series_folder",
    "movies_folder",
    "series_preset",
    "movies_preset",
    "automatic",
    "clean_downloads",
    "notify_needs",
    "daily_summary",
    "kept_back",
)


# --- finding the old install -------------------------------------------------------------------


class UninstallReader(Protocol):
    """Values of a key under HKEY_CURRENT_USER (tests pass a fake)."""

    def read(self, key: str) -> Mapping[str, object] | None:
        """The key's values by name; None when the key doesn't exist."""
        ...


def _read_uninstall_values(key: str) -> dict[str, object] | None:
    """The real registry (HKCU). Tests patch this name (tests/forge/base.py)."""
    if sys.platform != "win32":
        return None
    import winreg

    try:
        handle = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key)
    except OSError:
        return None
    values: dict[str, object] = {}
    with handle:
        index = 0
        while True:
            try:
                name, value, _kind = winreg.EnumValue(handle, index)
            except OSError:
                break
            values[name] = value
            index += 1
    return values


def _appdata() -> Path | None:
    """%APPDATA% (the roaming folder MediaForge kept its database in). Tests patch this name."""
    value = os.environ.get("APPDATA")
    return Path(value) if value else None


class WindowsUninstallReader:
    def read(self, key: str) -> Mapping[str, object] | None:
        return _read_uninstall_values(key)


@dataclass(frozen=True, slots=True)
class OldInstall:
    """What was found of the old MediaForge: its "Installed apps" entry and its data."""

    db_file: Path | None  # mediaforge.db, when it exists
    install_location: str = ""
    uninstall_string: str = ""
    version: str = ""  # DisplayVersion, e.g. "1.3.0"

    @property
    def registered(self) -> bool:
        """Whether Windows still lists it under Installed apps."""
        return bool(self.install_location or self.uninstall_string)

    @property
    def has_data(self) -> bool:
        """Whether there is a database to bring over."""
        return self.db_file is not None


def _text(values: Mapping[str, object] | None, name: str) -> str:
    value = (values or {}).get(name)
    return value.strip() if isinstance(value, str) else ""


def find_old_install(
    *, registry: UninstallReader | None = None, appdata: Path | None = None
) -> OldInstall | None:
    """The old MediaForge, or None when neither its entry nor its database is there.
    `appdata`: the folder MediaForge's data folder is in (default %APPDATA%). Quick but
    blocking (the registry, one file check): from the loop, through `asyncio.to_thread`."""
    try:
        values = (registry or WindowsUninstallReader()).read(UNINSTALL_KEY)
    except OSError as exc:
        log.warning("Couldn't read the old MediaForge's entry (%s)", type(exc).__name__)
        values = None
    base = appdata if appdata is not None else _appdata()
    db_file: Path | None = None
    if base is not None:
        candidate = base / DATA_FOLDER / OLD_DB_NAME
        with contextlib.suppress(OSError):
            if candidate.is_file():
                db_file = candidate
    found = OldInstall(
        db_file=db_file,
        install_location=_text(values, "InstallLocation"),
        uninstall_string=_text(values, "UninstallString"),
        version=_text(values, "DisplayVersion"),
    )
    if not found.registered and not found.has_data:
        return None
    return found


def blocked_by_old_install(find: Callable[[], OldInstall | None] | None = None) -> bool:
    """Whether the new renamer must stay idle: the old MediaForge is still installed (it starts
    with Windows and would watch the same folders). Blocking like `find_old_install`; never
    raises (a failed look counts as not installed)."""
    try:
        old = (find or find_old_install)()
    except Exception as exc:
        log.warning("Couldn't look for the old MediaForge (%s)", type(exc).__name__)
        return False
    return old is not None and old.registered


def user_tag() -> str:
    """As MediaForge's `instance_name.user_tag`."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", getpass.getuser()) or "user"


def mutex_name(name: str | None = None) -> str:
    """The old MediaForge's single-instance mutex."""
    return f"Local\\MediaForge-{name or user_tag()}"


def old_app_running(name: str | None = None) -> bool:
    """True while an old MediaForge holds its lock (ported from MediaForge's
    `platform/instance_name.instance_running`; stdlib ctypes, only opens the mutex to look)."""
    if sys.platform != "win32":
        return False
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenMutexW.restype = ctypes.c_void_p
    k32.OpenMutexW.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_wchar_p]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = k32.OpenMutexW(_SYNCHRONIZE, False, mutex_name(name))
    if not handle:
        return False
    k32.CloseHandle(handle)
    return True


# --- what the import says ----------------------------------------------------------------------


class Problem(StrEnum):
    RUNNING = "running"  # the old MediaForge is open
    NO_DATA = "no_data"  # its database is gone
    TOO_NEW = "too_new"  # its database is newer than this build reads
    UNREADABLE = "unreadable"  # it couldn't be read
    FAILED = "failed"  # forge.db couldn't be written


PROBLEM_TEXTS: dict[Problem, str] = {
    Problem.RUNNING: N_(
        "Your old MediaForge is still open. Close it (also from the tray), then try again."
    ),
    Problem.NO_DATA: N_("Your old MediaForge's settings and history weren't found."),
    Problem.TOO_NEW: N_(
        "Your old MediaForge's data comes from a newer version than this AirGrabber can read. "
        "Update AirGrabber, then try again."
    ),
    Problem.UNREADABLE: N_(
        "Your old MediaForge's settings and history couldn't be read. The log has the details."
    ),
    Problem.FAILED: N_(
        "MediaForge's settings couldn't be written here. The log has the details."
    ),
}


class ImportProblem(Exception):
    """Nothing was brought over; `text` says why (translated)."""

    def __init__(self, problem: Problem) -> None:
        super().__init__(problem.value)
        self.problem = problem

    @property
    def text(self) -> str:
        return t(PROBLEM_TEXTS[self.problem])


@dataclass(frozen=True, slots=True)
class ImportResult:
    """What came over (the result step reports it)."""

    imported_from: str  # e.g. "MediaForge 1.3.0, 2026-11-02"
    presets: int
    folders: int  # watched download folders plus the Series / Movies library folders set
    renames: int  # past renames (History's runs)
    matches: int  # remembered matches
    tmdb_key: bool  # the old TMDB key went into AirGrabber's settings
    menu: bool  # the old right-click menu now starts AirGrabber
    # Manual / On came over while the old MediaForge is still installed: the renamer waits.
    waits_for_removal: bool = False


def import_label(version: str, day: date) -> str:
    """ForgeSettings.imported_from: "MediaForge 1.3.0, 2026-11-02" (stored, never translated)."""
    name = f"MediaForge {version}" if version else "MediaForge"
    return f"{name}, {day.isoformat()}"


# --- what it needs of Forge ----------------------------------------------------------------------


class _ForgeSettings(Protocol):
    @property
    def current(self) -> ForgeSettings: ...

    async def update(self, **changes: Any) -> ForgeSettings: ...


class _Presets(Protocol):
    async def list(self) -> list[Preset]: ...


class _History(Protocol):
    async def list_runs(self) -> list[RunSummary]: ...


class _Matches(Protocol):
    async def list(self) -> list[RememberedMatch]: ...


class _AirGrabberSettings(Protocol):
    @property
    def current(self) -> Settings: ...

    async def update(self, **changes: Any) -> Settings: ...


@runtime_checkable
class ImportTarget(Protocol):
    """What the import uses of ForgeServices (app/forge/services.py)."""

    @property
    def paths(self) -> AppPaths: ...

    @property
    def settings(self) -> _ForgeSettings: ...

    @property
    def presets(self) -> _Presets: ...

    @property
    def history(self) -> _History: ...

    @property
    def matches(self) -> _Matches: ...

    @property
    def airgrabber_settings(self) -> _AirGrabberSettings: ...

    @property
    def run_lock(self) -> asyncio.Lock: ...


async def has_own_data(forge: ImportTarget) -> bool:
    """Whether Forge already has data of its own that an import would replace: a past
    rename, a remembered match, presets other than the defaults or a chosen setting."""
    from app.forge.models.preset import DEFAULT_PRESETS
    from app.forge.models.settings import ForgeSettings

    if await forge.history.list_runs() or await forge.matches.list():
        return True
    if list(await forge.presets.list()) != list(DEFAULT_PRESETS):
        return True
    current, default = forge.settings.current, ForgeSettings()
    return any(getattr(current, name) != getattr(default, name) for name in OWN_FIELDS)


def marker_path(paths: AppPaths) -> Path:
    """The file phase 3's farewell MediaForge looks for: `<forge>\\imported-from-mediaforge`."""
    return paths.forge / MARKER_NAME


# --- bringing it over --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Prepared:
    settings: ForgeSettings
    tmdb_key: str
    presets: int
    renames: int
    matches: int


async def _snapshot(source: Path, target: Path) -> None:
    """A consistent copy of `source` through SQLite's backup API from a read-only connection
    (the old folder is never written)."""
    uri = source.as_uri() + "?mode=ro"
    async with (
        aiosqlite.connect(uri, uri=True) as old,
        aiosqlite.connect(target, check_same_thread=False) as copy,
    ):
        await old.backup(copy)


async def _prepare(copy: Path, current: ForgeSettings, label: str) -> _Prepared:
    """Open the snapshot as a Forge database (migrating it), count what it holds and store its
    settings as Forge's own (dropped fields gone, tools reset, the import marked)."""
    from app.forge.db.database import Database
    from app.forge.db.history import HistoryRepo
    from app.forge.db.matches import MatchRepo
    from app.forge.db.presets import PresetRepo
    from app.forge.db.settings import SettingsRepo

    try:
        db = await Database.open(copy)
    except RuntimeError as exc:  # a schema newer than this build (app/forge/db/database.py)
        log.warning("The old MediaForge's database is newer than this build (%s)", type(exc).__name__)
        raise ImportProblem(Problem.TOO_NEW) from exc
    try:
        repo = SettingsRepo(db)
        document = await repo.load_document()
        old = await repo.load()
        key = document.get("tmdb_key")
        settings = old.model_copy(
            update={
                "trailer_tools_ok": False,  # D12: downloaded fresh on first use
                "ytdlp_checked_at": 0.0,
                "window_geometry": current.window_geometry,  # unread, as it was
                "imported_from": label,
            }
        )
        await repo.save(settings)  # the copy (and so forge.db) never holds the old key
        return _Prepared(
            settings=settings,
            tmdb_key=key.strip() if isinstance(key, str) else "",
            presets=len(await PresetRepo(db).list()),
            renames=len(await HistoryRepo(db).list_runs()),
            matches=len(await MatchRepo(db).list()),
        )
    finally:
        await db.close()


async def _write_forge_db(copy: Path, forge_db: Path) -> None:
    """forge.db := the prepared copy, through SQLite's backup API (Forge's own connection may
    stay open: it reads the new rows at its next statement)."""
    forge_db.parent.mkdir(parents=True, exist_ok=True)
    async with (
        aiosqlite.connect(copy) as source,
        aiosqlite.connect(forge_db, check_same_thread=False) as target,
    ):
        await source.backup(target)


async def _take_tmdb_key(forge: ImportTarget, key: str) -> bool:
    """The old TMDB key into AirGrabber's settings, only when AirGrabber has none."""
    if not key:
        return False
    settings = forge.airgrabber_settings
    if settings.current.tmdb_api_key.get_secret_value().strip():
        return False
    try:
        await settings.update(tmdb_api_key=key)
    except Exception as exc:  # the type only: never the key
        log.warning("The old TMDB key wasn't taken over (%s)", type(exc).__name__)
        return False
    return True


async def _take_over_menu(
    forge: ImportTarget, chosen: list[str], registry: MenuRegistry | None
) -> bool:
    """When the right-click menu starts the old MediaForge, rewrite it to start AirGrabber."""
    from app.forge import context_menu

    try:
        state = await asyncio.to_thread(context_menu.menu_state, registry)
        if state is not context_menu.MenuState.OLD_MEDIAFORGE:
            return False
        names = [preset.name for preset in await forge.presets.list()]
        return await asyncio.to_thread(context_menu.write_menu, names, chosen, registry)
    except Exception as exc:
        log.warning("The old right-click menu wasn't taken over (%s)", type(exc).__name__)
        return False


def _write_marker(paths: AppPaths, label: str) -> None:
    try:
        marker_path(paths).write_text(label + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning("The import marker wasn't written (%s)", type(exc).__name__)


async def bring_over(
    forge: ImportTarget,
    old: OldInstall,
    *,
    registry: MenuRegistry | None = None,
    running: Callable[[], bool] | None = None,
    today: date | None = None,
) -> ImportResult:
    """Bring the old MediaForge's data over (see the module's notes), replacing Forge's.
    Raises ImportProblem when nothing came over. `registry`: the right-click menu's (tests
    pass a fake); `running`: whether the old app runs (default `old_app_running`)."""
    is_running = running if running is not None else old_app_running
    if await asyncio.to_thread(is_running):
        raise ImportProblem(Problem.RUNNING)
    source = old.db_file
    if source is None or not await asyncio.to_thread(source.is_file):
        raise ImportProblem(Problem.NO_DATA)
    paths = forge.paths
    label = import_label(old.version, today or date.today())
    forge_dir = paths.forge
    try:
        forge_dir.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(
            prefix=_TEMP_PREFIX, dir=forge_dir, ignore_cleanup_errors=True
        )
    except OSError as exc:
        log.error("No room for the import's copy (%s)", type(exc).__name__)
        raise ImportProblem(Problem.FAILED) from exc
    with temp as folder:
        copy = Path(folder) / _SNAPSHOT_NAME
        try:
            await _snapshot(source, copy)
            prepared = await _prepare(copy, forge.settings.current, label)
        except ImportProblem:
            raise
        except (sqlite3.Error, OSError, ValueError) as exc:
            log.error("The old MediaForge's database couldn't be read (%s)", type(exc).__name__)
            raise ImportProblem(Problem.UNREADABLE) from exc
        from app.forge.db.database import db_path

        if old.registered:
            # The old MediaForge still starts with Windows: the new renamer must not start on
            # the imported Manual / On (AutoRenamer.blocked_by_old_install; F-27 clears it).
            auto = getattr(forge, "auto", None)
            if auto is not None:
                auto.blocked_by_old_install = True
        async with forge.run_lock:  # no rename run writes while forge.db is replaced
            try:
                await _write_forge_db(copy, db_path(paths))
            except (sqlite3.Error, OSError) as exc:
                log.error("forge.db couldn't be written (%s)", type(exc).__name__)
                raise ImportProblem(Problem.FAILED) from exc
            # Forge's settings service holds them in memory: tell it (the renamer follows).
            await forge.settings.update(**prepared.settings.model_dump())
    tmdb_key = await _take_tmdb_key(forge, prepared.tmdb_key)
    menu = await _take_over_menu(forge, list(prepared.settings.context_menu_presets), registry)
    await asyncio.to_thread(_write_marker, paths, label)
    settings = prepared.settings
    folders = len(settings.watch_folders) + bool(settings.series_folder) + bool(
        settings.movies_folder
    )
    result = ImportResult(
        imported_from=label,
        presets=prepared.presets,
        folders=folders,
        renames=prepared.renames,
        matches=prepared.matches,
        tmdb_key=tmdb_key,
        menu=menu,
        waits_for_removal=old.registered and settings.automatic != "off",
    )
    log.info(
        "Brought the old MediaForge's data over: %d presets, %d folders, %d past renames, "
        "%d remembered matches, TMDB key %s, menu %s",
        result.presets,
        result.folders,
        result.renames,
        result.matches,
        "taken" if tmdb_key else "not taken",
        "taken over" if menu else "unchanged",
    )
    return result
