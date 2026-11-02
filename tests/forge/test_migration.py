"""Bringing the old MediaForge over (app/forge/migration.py, F-26).

Every old install here is a fake one in a temporary folder: a MediaForge 1.3 database built
from its schema text (tests/forge/db/test_database.py's frozen copy) with made-up rows, a fake
"Installed apps" entry, a fake right-click menu and a fake "is it running" check. The real
registry, %APPDATA% and the old app's mutex are hidden by tests/forge/base.py.
"""

from __future__ import annotations

import ctypes
import json
import os
import sqlite3
import sys
import uuid
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any, cast
from unittest import mock

import aiosqlite
import httpx

from app.config import AppPaths
from app.forge import migration
from app.forge.context_menu import MenuState, menu_state
from app.forge.db.database import SCHEMA_VERSION, db_path
from app.forge.migration import (
    MARKER_NAME,
    UNINSTALL_KEY,
    ImportProblem,
    ImportTarget,
    OldInstall,
    Problem,
    bring_over,
    find_old_install,
    has_own_data,
    import_label,
    mutex_name,
    old_app_running,
)
from app.forge.models.media import TypeHint
from app.forge.models.preset import Preset
from app.forge.runtime import ForgeRuntime
from app.forge.services import ForgeServices
from app.models import Settings
from app.services.images import ImageCache
from app.services.settings import SettingsService
from app.services.tmdb import TMDBClient
from tests.forge.base import ForgeAsyncTestCase, ForgeTestCase
from tests.forge.db.test_database import MEDIAFORGE_1_3_SCHEMA
from tests.forge.test_context_menu import DEV_COPY, FakeRegistry, menu_of, old_mediaforge_menu

TODAY = date(2026, 11, 2)
OLD_KEY = "made-up-old-key"
OLD_SETTINGS = {
    "tmdb_key": OLD_KEY,
    "theme": "Light",
    "accent": "copper",
    "auto_update": False,
    "first_start_done": True,
    "tmdb_language": "nl-NL",
    "window_geometry": "900x600+10+10",
    "last_preset": "Mine",
    "context_menu_presets": ["Mine"],
    "trailer_tools_ok": True,
    "ytdlp_checked_at": 1700000000.0,
    "watch_folders": [r"D:\Downloads\tv", r"D:\Downloads\films"],
    "series_folder": r"\\nas\video\Series",
    "movies_folder": "",
    "automatic": "trial",
    "notify_needs": False,
    "kept_back": [r"D:\Downloads\tv\keep.mkv"],
}
OLD_ROWS = f"""
INSERT INTO settings (key, value) VALUES ('app', '{json.dumps(OLD_SETTINGS).replace("'", "''")}');
INSERT INTO presets (name, position, type, format, options) VALUES
    ('Mine', 0, 'tv', '{{n}} - {{s00e00}}', '{{"series_poster": true}}'),
    ('Auto', 1, 'auto', '{{ny}}', '{{}}');
INSERT INTO remembered_matches (kind, norm_title, year, tmdb_id, label, created_at) VALUES
    ('episode', 'the office', 2005, 2316, 'The Office (2005)', 1700000000.0),
    ('movie', 'home video', 0, NULL, 'Don''t use TMDB', 1700000001.0);
INSERT INTO tmdb_cache (key, payload, fetched_at) VALUES ('tv:the office', '[1, 2]', 4e9);
INSERT INTO runs (id, started_at, finished_at, source, preset_name, kinds) VALUES
    (7, 1700000000.0, 1700000005.0, 'automatic', 'Mine', 'episode'),
    (8, 1700000100.0, 1700000105.0, 'app', 'Auto', 'movie');
INSERT INTO operations (id, run_id, seq, kind, src, dst, size, mtime_ns, state, error,
                        undo_state, undo_note, undone_at) VALUES
    (1, 7, 1, 'rename', 'C:\\m\\a.mkv', 'D:\\Series\\a.mkv', 5, 6, 'done', NULL, NULL, NULL, NULL),
    (2, 8, 1, 'rename', 'C:\\m\\b.mkv', 'D:\\Films\\b.mkv', 5, 6, 'done', NULL, NULL, NULL, NULL);
"""


class FakeUninstall:
    """HKCU's Uninstall key for the old MediaForge, as a dict (None: not registered)."""

    def __init__(self, values: Mapping[str, object] | None) -> None:
        self.values = values
        self.keys: list[str] = []

    def read(self, key: str) -> Mapping[str, object] | None:
        self.keys.append(key)
        return self.values


ENTRY = {
    "DisplayName": "MediaForge",
    "DisplayVersion": "1.3.0",
    "InstallLocation": r"C:\Users\Alex\AppData\Local\Programs\MediaForge",
    "UninstallString": (
        "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "
        r'"C:\Users\Alex\AppData\Local\Programs\MediaForge\src\mediaforge\uninstall.ps1"'
    ),
    "NoModify": 1,
}


def make_old_db(path: Path, rows: str = OLD_ROWS, *, version: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(MEDIAFORGE_1_3_SCHEMA)
        conn.executescript(rows)
        conn.execute(f"PRAGMA user_version={version}")
        conn.commit()
    finally:
        conn.close()


class FindTests(ForgeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.appdata = self.tmp / "Roaming"
        self.appdata.mkdir()

    def test_an_entry_and_its_data(self) -> None:
        make_old_db(self.appdata / "MediaForge" / "mediaforge.db")
        registry = FakeUninstall(ENTRY)
        found = find_old_install(registry=registry, appdata=self.appdata)
        assert found is not None
        self.assertEqual(registry.keys, [UNINSTALL_KEY])
        self.assertEqual(found.db_file, self.appdata / "MediaForge" / "mediaforge.db")
        self.assertEqual(found.version, "1.3.0")
        self.assertEqual(found.install_location, ENTRY["InstallLocation"])
        self.assertEqual(found.uninstall_string, ENTRY["UninstallString"])
        self.assertTrue(found.registered)
        self.assertTrue(found.has_data)

    def test_an_entry_without_data(self) -> None:
        found = find_old_install(registry=FakeUninstall(ENTRY), appdata=self.appdata)
        assert found is not None
        self.assertTrue(found.registered)
        self.assertFalse(found.has_data)

    def test_data_without_an_entry(self) -> None:
        make_old_db(self.appdata / "MediaForge" / "mediaforge.db")
        found = find_old_install(registry=FakeUninstall(None), appdata=self.appdata)
        assert found is not None
        self.assertFalse(found.registered)
        self.assertTrue(found.has_data)
        self.assertEqual(found.version, "")

    def test_nothing_there(self) -> None:
        self.assertIsNone(find_old_install(registry=FakeUninstall(None), appdata=self.appdata))
        self.assertIsNone(find_old_install(registry=FakeUninstall({}), appdata=self.appdata))

    def test_mediaforge_home_is_ignored(self) -> None:
        home = self.tmp / "home"
        make_old_db(home / "mediaforge.db")
        with mock.patch.dict(os.environ, {"MEDIAFORGE_HOME": str(home)}):
            self.assertIsNone(
                find_old_install(registry=FakeUninstall(None), appdata=self.appdata)
            )

    def test_a_registry_that_fails_counts_as_no_entry(self) -> None:
        registry = mock.Mock()
        registry.read.side_effect = PermissionError("denied")
        make_old_db(self.appdata / "MediaForge" / "mediaforge.db")
        found = find_old_install(registry=registry, appdata=self.appdata)
        assert found is not None
        self.assertFalse(found.registered)

    def test_tests_never_see_the_real_install(self) -> None:
        """tests/forge/base.py hides the real entry and %APPDATA%."""
        self.assertIsNone(find_old_install())
        self.assertFalse(old_app_running())

    def test_import_label(self) -> None:
        self.assertEqual(import_label("1.3.0", TODAY), "MediaForge 1.3.0, 2026-11-02")
        self.assertEqual(import_label("", TODAY), "MediaForge, 2026-11-02")


class MutexTests(ForgeTestCase):
    def test_mutex_name_follows_mediaforge(self) -> None:
        self.assertEqual(mutex_name("Alex"), r"Local\MediaForge-Alex")
        self.assertTrue(migration.mutex_name().startswith("Local\\MediaForge-"))

    def test_running_while_the_mutex_is_held(self) -> None:
        if sys.platform != "win32":
            self.skipTest("Windows only")
        name = f"airgrabber-test-{uuid.uuid4().hex}"  # never the owner's real MediaForge
        # The base class swaps the module's name for a fake; the one imported here is real.
        self.assertIsNot(migration.old_app_running, old_app_running)
        self.assertFalse(old_app_running(name))
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = ctypes.c_void_p
        k32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = k32.CreateMutexW(None, False, mutex_name(name))
        self.assertTrue(handle)
        try:
            self.assertTrue(old_app_running(name))
        finally:
            k32.CloseHandle(handle)
        self.assertFalse(old_app_running(name))


class FakeAirGrabberSettings:
    """SettingsService's surface: `current`, `update` (validated)."""

    def __init__(self, key: str = "") -> None:
        self.current = Settings(tmdb_api_key=key)
        self.updates: list[dict[str, Any]] = []

    async def update(self, **changes: Any) -> Settings:
        self.updates.append(changes)
        self.current = Settings.model_validate({**self.current.model_dump(), **changes})
        return self.current


def _no_network(request: httpx.Request) -> httpx.Response:
    return httpx.Response(404)


class ImportCase(ForgeAsyncTestCase):
    """A running Forge on a temporary data folder and a fake old install beside it."""

    airgrabber_key = ""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.paths = AppPaths(self.data_dir)
        http = httpx.AsyncClient(transport=httpx.MockTransport(_no_network))
        self.addAsyncCleanup(http.aclose)
        self.airgrabber = FakeAirGrabberSettings(self.airgrabber_key)
        self.runtime = ForgeRuntime(
            self.paths,
            http,
            TMDBClient(http, api_key=lambda: ""),
            ImageCache(http, self.data_dir / "posters"),
            cast(SettingsService, self.airgrabber),
            parser_ready=lambda: True,
        )
        self.addAsyncCleanup(self.runtime.aclose)
        services = await self.runtime.start()
        assert services is not None
        self.forge: ForgeServices = services
        await self.forge.settings.update(window_geometry="1200x800+40+40")
        self.appdata = self.tmp / "Roaming"
        self.old_db = self.appdata / "MediaForge" / "mediaforge.db"
        self.registry = FakeRegistry()
        self.saved: list[object] = []
        self.forge.settings.subscribe(self.saved.append)

    def old(self, *, version: str = "1.3.0") -> OldInstall:
        return OldInstall(
            db_file=self.old_db,
            install_location=str(ENTRY["InstallLocation"]),
            uninstall_string=str(ENTRY["UninstallString"]),
            version=version,
        )

    async def bring(self, old: OldInstall | None = None, *, running: bool = False) -> Any:
        return await bring_over(
            self.forge,
            old or self.old(),
            registry=self.registry,
            running=lambda: running,
            today=TODAY,
        )

    async def stored_document(self) -> dict[str, Any]:
        async with aiosqlite.connect(db_path(self.paths)) as conn:
            rows = list(await conn.execute_fetchall("SELECT value FROM settings WHERE key='app'"))
        return cast(dict[str, Any], json.loads(rows[0][0]))

    def leftovers(self) -> list[str]:
        return sorted(p.name for p in self.paths.forge.iterdir() if p.name.startswith("mediaforge-"))


class BringOverTests(ImportCase):
    async def test_everything_comes_over(self) -> None:
        make_old_db(self.old_db)
        self.assertIsInstance(self.forge, ImportTarget)
        self.assertFalse(await has_own_data(self.forge))
        result = await self.bring()

        self.assertEqual(result.imported_from, "MediaForge 1.3.0, 2026-11-02")
        self.assertEqual(
            (result.presets, result.folders, result.renames, result.matches), (2, 3, 2, 2)
        )
        self.assertTrue(result.tmdb_key)
        self.assertFalse(result.menu)  # there was no menu

        settings = self.forge.settings.current
        self.assertEqual(settings.imported_from, "MediaForge 1.3.0, 2026-11-02")
        self.assertEqual(settings.watch_folders, OLD_SETTINGS["watch_folders"])
        self.assertEqual(settings.series_folder, r"\\nas\video\Series")
        self.assertEqual(settings.automatic, "trial")
        self.assertEqual(settings.tmdb_language, "nl-NL")
        self.assertFalse(settings.notify_needs)
        self.assertEqual(settings.kept_back, OLD_SETTINGS["kept_back"])
        self.assertEqual(settings.last_preset, "Mine")
        self.assertEqual(settings.context_menu_presets, ["Mine"])
        self.assertFalse(settings.trailer_tools_ok)  # D12: fresh tools
        self.assertEqual(settings.ytdlp_checked_at, 0.0)
        self.assertEqual(settings.window_geometry, "1200x800+40+40")  # the Forge window's own
        self.assertIn(settings, self.saved)  # listeners (the renamer) were told

        self.assertEqual([p.name for p in await self.forge.presets.list()], ["Mine", "Auto"])
        self.assertEqual(len(await self.forge.matches.list()), 2)
        runs = await self.forge.history.list_runs()
        self.assertEqual(sorted(run.id for run in runs), [7, 8])
        self.assertTrue(await has_own_data(self.forge))

        # The TMDB key went to AirGrabber, and nowhere in forge.db.
        self.assertEqual(self.airgrabber.current.tmdb_api_key.get_secret_value(), OLD_KEY)
        document = await self.stored_document()
        for gone in ("tmdb_key", "theme", "accent", "auto_update", "first_start_done"):
            self.assertNotIn(gone, document)
        for file in self.paths.forge.rglob("*"):
            if file.is_file():
                self.assertNotIn(OLD_KEY.encode(), file.read_bytes(), file.name)

        marker = self.paths.forge / MARKER_NAME
        self.assertEqual(marker.read_text(encoding="utf-8"), "MediaForge 1.3.0, 2026-11-02\n")
        self.assertEqual(self.leftovers(), [])

    async def test_the_old_folder_is_never_written(self) -> None:
        make_old_db(self.old_db)
        # The old app's connection still open with changes only in its WAL: they come along.
        live = sqlite3.connect(self.old_db)
        self.addCleanup(live.close)
        live.execute("PRAGMA wal_autocheckpoint=0")
        live.execute(
            "INSERT INTO remembered_matches (kind, norm_title, year, tmdb_id, label, created_at)"
            " VALUES ('movie', 'heat', 1995, 949, 'Heat (1995)', 1700000002.0)"
        )
        live.commit()
        before = self.old_files()
        self.assertIn("mediaforge.db-wal", before)
        result = await self.bring()
        self.assertEqual(result.matches, 3)
        self.assertEqual(self.old_files(), before)

    def old_files(self) -> dict[str, bytes]:
        """The old folder's files (its WAL index, -shm, is shared memory: left out)."""
        return {
            p.name: p.read_bytes()
            for p in self.old_db.parent.iterdir()
            if not p.name.endswith("-shm")
        }

    async def test_airgrabbers_own_key_stays(self) -> None:
        self.airgrabber.current = Settings(tmdb_api_key="mine")
        make_old_db(self.old_db)
        result = await self.bring()
        self.assertFalse(result.tmdb_key)
        self.assertEqual(self.airgrabber.updates, [])
        self.assertEqual(self.airgrabber.current.tmdb_api_key.get_secret_value(), "mine")

    async def test_no_old_key_nothing_taken(self) -> None:
        settings = {k: v for k, v in OLD_SETTINGS.items() if k != "tmdb_key"}
        rows = OLD_ROWS.replace(json.dumps(OLD_SETTINGS), json.dumps(settings))
        make_old_db(self.old_db, rows)
        result = await self.bring(self.old(version=""))
        self.assertFalse(result.tmdb_key)
        self.assertEqual(result.imported_from, "MediaForge, 2026-11-02")

    async def test_the_old_menu_is_taken_over(self) -> None:
        make_old_db(self.old_db)
        self.registry.put(old_mediaforge_menu())
        self.assertIs(menu_state(self.registry), MenuState.OLD_MEDIAFORGE)
        result = await self.bring()
        self.assertTrue(result.menu)
        self.assertIs(menu_state(self.registry), MenuState.ON)
        commands = self.registry.commands()
        self.assertTrue(commands)
        self.assertTrue(all("--forge" in c and '--preset "Mine"' in c for c in commands))

    async def test_another_copys_menu_is_left_alone(self) -> None:
        make_old_db(self.old_db)
        self.registry.put(menu_of(DEV_COPY))
        before = dict(self.registry.keys)
        result = await self.bring()
        self.assertFalse(result.menu)
        self.assertEqual(self.registry.keys, before)

    async def test_a_menu_that_cant_be_written_doesnt_stop_the_import(self) -> None:
        make_old_db(self.old_db)
        self.registry.put(old_mediaforge_menu())
        self.registry.fail_writes = True
        result = await self.bring()
        self.assertFalse(result.menu)
        self.assertEqual(self.forge.settings.current.imported_from, result.imported_from)


class RefusedTests(ImportCase):
    async def assert_untouched(self) -> None:
        self.assertEqual(self.forge.settings.current.imported_from, "")
        self.assertFalse(await has_own_data(self.forge))
        self.assertFalse((self.paths.forge / MARKER_NAME).exists())
        self.assertEqual(self.airgrabber.updates, [])
        self.assertEqual(self.leftovers(), [])

    async def problem(self, **kwargs: Any) -> Problem:
        with self.assertRaises(ImportProblem) as caught:
            await self.bring(**kwargs)
        self.assertTrue(caught.exception.text)
        return caught.exception.problem

    async def test_while_the_old_app_runs(self) -> None:
        make_old_db(self.old_db)
        self.assertIs(await self.problem(running=True), Problem.RUNNING)
        await self.assert_untouched()

    async def test_without_its_database(self) -> None:
        self.assertIs(await self.problem(), Problem.NO_DATA)
        self.assertIs(
            await self.problem(old=OldInstall(db_file=None, uninstall_string="x")),
            Problem.NO_DATA,
        )
        await self.assert_untouched()

    async def test_a_newer_schema_is_said_plainly(self) -> None:
        make_old_db(self.old_db, version=SCHEMA_VERSION + 1)
        self.assertIs(await self.problem(), Problem.TOO_NEW)
        await self.assert_untouched()

    async def test_a_file_that_isnt_a_database(self) -> None:
        self.old_db.parent.mkdir(parents=True)
        self.old_db.write_bytes(b"not a database at all" * 100)
        self.assertIs(await self.problem(), Problem.UNREADABLE)
        await self.assert_untouched()

    async def test_forge_db_that_cant_be_written(self) -> None:
        make_old_db(self.old_db)
        with mock.patch.object(migration, "_write_forge_db", side_effect=sqlite3.OperationalError):
            self.assertIs(await self.problem(), Problem.FAILED)
        await self.assert_untouched()


class OwnDataTests(ImportCase):
    async def test_a_new_forge_has_none(self) -> None:
        self.assertFalse(await has_own_data(self.forge))  # the window's place doesn't count

    async def test_a_chosen_setting_counts(self) -> None:
        await self.forge.settings.update(watch_folders=[r"D:\Downloads"])
        self.assertTrue(await has_own_data(self.forge))

    async def test_a_changed_preset_counts(self) -> None:
        await self.forge.presets.save(Preset(name="Mine", type=TypeHint.TV, format="{n}"))
        self.assertTrue(await has_own_data(self.forge))

    async def test_a_fake_without_the_parts_is_no_target(self) -> None:
        self.assertNotIsInstance(object(), ImportTarget)


class BlockedRenamerTests(ImportCase):
    """While the old MediaForge is still installed, the new renamer stays idle (it starts
    with Windows and would watch the same folders); the stored Trial / On is kept."""

    def test_blocked_by_old_install(self) -> None:
        cases: dict[str, tuple[Any, bool]] = {
            "installed": (lambda: OLD_INSTALLED, True),
            "only its data left": (lambda: OldInstall(db_file=Path("x.db")), False),
            "nothing": (lambda: None, False),
            "a look that fails": (mock.Mock(side_effect=PermissionError("denied")), False),
        }
        for name, (find, blocked) in cases.items():
            with self.subTest(name):
                self.assertIs(migration.blocked_by_old_install(find), blocked)
        self.assertFalse(migration.blocked_by_old_install())  # the base hides the real one

    async def start_again(self, blocked: bool) -> ForgeServices:
        """Forge started anew on the same data folder, the old install there or not."""
        await self.runtime.aclose()
        http = httpx.AsyncClient(transport=httpx.MockTransport(_no_network))
        self.addAsyncCleanup(http.aclose)
        runtime = ForgeRuntime(
            self.paths,
            http,
            TMDBClient(http, api_key=lambda: ""),
            ImageCache(http, self.data_dir / "posters"),
            cast(SettingsService, self.airgrabber),
            parser_ready=lambda: True,
            old_install_blocks=lambda: blocked,
        )
        self.addAsyncCleanup(runtime.aclose)
        services = await runtime.start()
        assert services is not None
        return services

    async def test_the_runtime_starts_the_renamer_blocked(self) -> None:
        await self.forge.settings.update(automatic="trial")
        self.assertTrue(self.forge.auto.running)
        forge = await self.start_again(blocked=True)
        self.assertTrue(forge.auto.blocked_by_old_install)
        self.assertFalse(forge.auto.running)
        self.assertEqual(forge.settings.current.automatic, "trial")  # kept as it is
        self.assertEqual(
            forge.auto.status([]).text, "Not watching until the old MediaForge is removed"
        )
        await forge.settings.update(automatic="on")  # chosen again in Settings: still idle
        self.assertFalse(forge.auto.running)
        # F-27: the old install is gone.
        forge.auto.blocked_by_old_install = False
        forge.auto.reconfigure()
        self.assertTrue(forge.auto.running)

    async def test_not_blocked_without_the_old_install(self) -> None:
        await self.forge.settings.update(automatic="trial")
        forge = await self.start_again(blocked=False)
        self.assertFalse(forge.auto.blocked_by_old_install)
        self.assertTrue(forge.auto.running)

    async def test_an_imported_trial_waits_for_the_removal(self) -> None:
        make_old_db(self.old_db)
        self.assertFalse(self.forge.auto.blocked_by_old_install)
        result = await self.bring()
        self.assertTrue(result.waits_for_removal)
        self.assertEqual(self.forge.settings.current.automatic, "trial")
        self.assertTrue(self.forge.auto.blocked_by_old_install)
        self.assertFalse(self.forge.auto.running)

    async def test_an_import_without_the_old_install_starts_the_renamer(self) -> None:
        make_old_db(self.old_db)
        result = await self.bring(OldInstall(db_file=self.old_db, version="1.3.0"))
        self.assertFalse(result.waits_for_removal)
        self.assertFalse(self.forge.auto.blocked_by_old_install)
        self.assertTrue(self.forge.auto.running)

    async def test_an_imported_off_says_nothing_about_waiting(self) -> None:
        settings = {**OLD_SETTINGS, "automatic": "off"}
        make_old_db(self.old_db, OLD_ROWS.replace(json.dumps(OLD_SETTINGS), json.dumps(settings)))
        result = await self.bring()
        self.assertFalse(result.waits_for_removal)
        self.assertFalse(self.forge.auto.running)


OLD_INSTALLED = OldInstall(
    db_file=None,
    install_location=str(ENTRY["InstallLocation"]),
    uninstall_string=str(ENTRY["UninstallString"]),
)
