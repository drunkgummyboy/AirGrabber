"""The Explorer right-click menu (app/forge/context_menu.py), ported from MediaForge's
tests/platform/test_context_menu.py: the keys and commands, the .reg files, whose menu it is,
refresh never taking over another copy's menu, and `main.py --forge-menu on|off`. Every test
uses a fake registry; the guard in tests/forge/base.py makes the real one refuse writes."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

from app import i18n
from app.config import AppPaths
from app.forge import context_menu
from app.forge.context_menu import (
    BASES,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    Hive,
    MenuState,
    RegEntry,
    build_entries,
    legacy_removal_reg,
    to_reg_file,
)
from app.forge.models.preset import DEFAULT_PRESETS, Preset
from app.forge.models.settings import ForgeSettings
from app.i18n import Language
from tests.forge.base import ForgeAsyncTestCase, ForgeTestCase, RealContextMenuWrite

PYTHONW = r"C:\Users\Jürgen Smit\AppData\Local\Programs\Python\Python313\pythonw.exe"
INSTALLED = Path(r"C:\Users\Jürgen Smit\AppData\Local\Programs\AirGrabber\main.py")
DEV_COPY = Path(r"D:\work\AirGrabber\main.py")
OLD_MEDIAFORGE = Path(r"C:\Users\Jürgen Smit\AppData\Local\Programs\MediaForge\main.py")


class FakeRegistry:
    """HKCU and HKLM as dicts: key → {value name: text}. Keys sort like regedit's."""

    def __init__(self) -> None:
        self.keys: dict[tuple[Hive, str], dict[str, str]] = {}
        self.writes = 0
        self.fail_writes = False

    def read(self, hive: Hive, key: str, name: str = "") -> str | None:
        return self.keys.get((hive, key.casefold()), {}).get(name)

    def first_subkey(self, hive: Hive, key: str) -> str | None:
        prefix = key.casefold() + "\\"
        children = sorted(
            {k[len(prefix) :].split("\\")[0] for h, k in self.keys if h == hive and k.startswith(prefix)}
        )
        return children[0] if children else None

    def exists(self, hive: Hive, key: str) -> bool:
        return (hive, key.casefold()) in self.keys

    def write(self, key: str, values: Mapping[str, str]) -> None:
        if self.fail_writes:
            raise PermissionError("access denied")
        self.writes += 1
        self.keys.setdefault(("HKCU", key.casefold()), {}).update(values)

    def delete_tree(self, key: str) -> None:
        if self.fail_writes:
            raise PermissionError("access denied")
        gone = key.casefold()
        for hive, name in list(self.keys):
            if hive == "HKCU" and (name == gone or name.startswith(gone + "\\")):
                del self.keys[(hive, name)]

    def put(self, entries: list[RegEntry], hive: Hive = "HKCU") -> None:
        for entry in entries:
            self.keys[(hive, entry.key.casefold())] = dict(entry.values)

    def commands(self) -> list[str]:
        return [
            values[""]
            for (hive, key), values in sorted(self.keys.items())
            if hive == "HKCU" and key.endswith("\\command")
        ]


def menu_of(main_py: Path, *, presets: tuple[str, ...] = ("Auto",), python: str = PYTHONW) -> list[RegEntry]:
    return build_entries(list(presets), [python, str(main_py)], "x.ico")


def old_mediaforge_menu() -> list[RegEntry]:
    """MediaForge 1.x's own menu at the same keys: no --forge."""
    entries = menu_of(OLD_MEDIAFORGE)
    return [
        RegEntry(e.key, {k: v.replace(" --forge", "") for k, v in e.values.items()}) for e in entries
    ]


class EntriesTests(ForgeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(i18n.set_language, i18n.current_language())
        i18n.set_language(Language.EN)

    def test_build_entries_structure(self) -> None:
        entries = build_entries(["Auto", "Series (Folder)"], [PYTHONW, str(INSTALLED)], "x.ico")
        keys = [e.key for e in entries]
        base = BASES[0]
        self.assertEqual(
            keys[:5],
            [
                base,
                rf"{base}\shell\01_Auto",
                rf"{base}\shell\01_Auto\command",
                rf"{base}\shell\02_SeriesFolder",
                rf"{base}\shell\02_SeriesFolder\command",
            ],
        )
        self.assertEqual(len(entries), 10)  # the same five keys for folders
        self.assertEqual(keys[5], BASES[1])
        by_key = {e.key: e.values for e in entries}
        self.assertEqual(by_key[base], {"MUIVerb": "MediaForge", "SubCommands": "", "Icon": "x.ico"})
        self.assertEqual(
            by_key[rf"{base}\shell\02_SeriesFolder"],
            {"MUIVerb": "Forge: Series (Folder)", "MultiSelectModel": "Player"},
        )
        # F-20's hand-over flags; non-ASCII letters and spaces in both paths, quoted.
        self.assertEqual(
            by_key[rf"{base}\shell\02_SeriesFolder\command"],
            {"": f'"{PYTHONW}" "{INSTALLED}" --forge --confirm --preset "Series (Folder)" "%1"'},
        )
        self.assertEqual(
            BASES,
            (r"Software\Classes\*\shell\MediaForge", r"Software\Classes\Directory\shell\MediaForge"),
        )

    def test_the_entries_are_in_the_current_language(self) -> None:
        def translated(text: str, **fmt: object) -> str:
            return "NL " + text.format(**fmt)

        with mock.patch.object(context_menu, "t", side_effect=translated):
            entries = build_entries(["Auto"], [PYTHONW, str(INSTALLED)], "x.ico")
        self.assertEqual(entries[1].values["MUIVerb"], "NL Forge: Auto")
        self.assertEqual(entries[0].values["MUIVerb"], "MediaForge")  # a name: never translated

    def test_odd_preset_names(self) -> None:
        entries = build_entries(['Plex "4K"', "§§"], ["p", "m"], "x.ico")
        self.assertEqual(entries[1].key, rf"{BASES[0]}\shell\01_Plex4K")
        self.assertEqual(entries[2].values[""], '"p" "m" --forge --confirm --preset "Plex 4K" "%1"')
        self.assertEqual(entries[3].key, rf"{BASES[0]}\shell\02_Preset")

    def test_reg_file_escaping(self) -> None:
        text = to_reg_file([RegEntry(r"Software\X", {"": r'"C:\a b\c.exe" "%1"', "Name": "v"})])
        self.assertTrue(text.startswith("Windows Registry Editor Version 5.00\r\n"))
        self.assertIn(r"[HKEY_CURRENT_USER\Software\X]", text)
        self.assertIn(r'@="\"C:\\a b\\c.exe\" \"%1\""', text)
        self.assertIn('"Name"="v"', text)

    def test_legacy_removal_reg_uses_minus_keys(self) -> None:
        registry = FakeRegistry()
        registry.keys[("HKLM", context_menu.LEGACY_HKLM_KEYS[0].casefold())] = {
            "SubCommands": "MediaForge_Auto;MediaForge_Movie;"
        }
        self.assertTrue(context_menu.legacy_installed(registry))
        text = legacy_removal_reg(registry)
        self.assertIn(r"[-HKEY_LOCAL_MACHINE\SOFTWARE\Classes\*\shell\MediaForgeCommands]", text)
        self.assertIn(r"[-HKEY_LOCAL_MACHINE\SOFTWARE\Classes\Folder\shell\MediaForgeCommands]", text)
        store = r"[-HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\CommandStore\shell"
        self.assertIn(store + r"\MediaForge_Auto]", text)
        self.assertIn(store + r"\MediaForge_Movie]", text)
        self.assertFalse(context_menu.legacy_installed(FakeRegistry()))

    def test_command_prefix_uses_pythonw_and_main_py(self) -> None:
        python = self.tmp / "Python313" / "python.exe"
        python.parent.mkdir()
        python.touch()
        # No pythonw.exe beside it: that Python itself.
        self.assertEqual(context_menu.command_prefix(python, INSTALLED), [str(python), str(INSTALLED)])
        (python.parent / "pythonw.exe").touch()
        self.assertEqual(
            context_menu.command_prefix(python, INSTALLED),
            [str(python.parent / "pythonw.exe"), str(INSTALLED)],
        )
        prefix = context_menu.command_prefix()
        self.assertTrue(prefix[0].lower().endswith(("pythonw.exe", "python.exe")))
        self.assertEqual(Path(prefix[1]).name, "main.py")
        self.assertTrue(Path(prefix[1]).is_file())  # this copy's own main.py

    def test_the_icon_is_mediaforges_inside_this_install(self) -> None:
        icon = Path(context_menu.default_icon())
        self.assertEqual(icon.name, "MediaForge.ico")
        self.assertEqual(icon.parent, context_menu.INSTALL_DIR / "app" / "assets" / "forge")

    def test_menu_presets_empty_means_all_and_drops_deleted(self) -> None:
        names = ["Auto", "Movie (Flat)", "Series (Folder)"]
        self.assertEqual(context_menu.menu_presets(names, []), names)
        self.assertEqual(context_menu.menu_presets(names, ["Series (Folder)", "Gone"]), ["Series (Folder)"])
        self.assertEqual(context_menu.menu_presets(names, ["Gone"]), names)  # none left: all
        self.assertEqual(context_menu.menu_presets(names, ["Series (Folder)", "Auto"]), ["Auto", "Series (Folder)"])


class StateTests(ForgeTestCase):
    def state(self, registry: FakeRegistry) -> MenuState:
        return context_menu.menu_state(registry, python=Path(PYTHONW), main_py=INSTALLED)

    def test_whose_menu_it_is(self) -> None:
        cases: dict[str, tuple[list[RegEntry], MenuState]] = {
            "none": ([], MenuState.OFF),
            "this copy": (menu_of(INSTALLED), MenuState.ON),
            "this copy, letter case aside": (menu_of(Path(str(INSTALLED).upper())), MenuState.ON),
            "this copy, a Python that moved": (
                menu_of(INSTALLED, python=r"C:\Old\Python312\pythonw.exe"),
                MenuState.STALE,
            ),
            "a development copy": (menu_of(DEV_COPY), MenuState.OTHER_COPY),
            "the old MediaForge": (old_mediaforge_menu(), MenuState.OLD_MEDIAFORGE),
        }
        for name, (entries, expected) in cases.items():
            with self.subTest(name):
                registry = FakeRegistry()
                registry.put(entries)
                self.assertEqual(self.state(registry), expected)
        self.assertTrue(MenuState.ON.ours and MenuState.STALE.ours)
        self.assertFalse(MenuState.OTHER_COPY.ours or MenuState.OLD_MEDIAFORGE.ours or MenuState.OFF.ours)

    def test_a_main_py_inside_another_path_is_not_this_copy(self) -> None:
        # The quoted path must match whole: "...\AirGrabber\main.py" isn't "...\AirGrabber 2\main.py".
        command = f'"{PYTHONW}" "C:\\Apps\\AirGrabber 2\\main.py" --forge --confirm "%1"'
        self.assertFalse(context_menu.starts_copy(command, Path(r"C:\Apps\AirGrabber\main.py")))
        self.assertFalse(context_menu.starts_copy(command, Path(r"Apps\AirGrabber 2\main.py")))
        self.assertTrue(context_menu.starts_copy(command, Path(r"c:\apps\airgrabber 2\MAIN.PY")))


class WriteTests(ForgeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.registry = FakeRegistry()
        self.python = Path(PYTHONW)

    def refresh(self, names: list[str], chosen: list[str], main_py: Path = INSTALLED) -> bool:
        return context_menu.refresh(names, chosen, self.registry, python=self.python, main_py=main_py)

    def test_refresh_rewrites_the_menu_of_this_copy(self) -> None:
        self.registry.put(menu_of(INSTALLED, python=r"C:\Old\pythonw.exe"))  # stale, one preset
        self.assertTrue(self.refresh(["Auto", "New one"], []))
        verbs = [values.get("MUIVerb") for values in self.registry.keys.values()]
        self.assertIn("Forge: Auto", verbs)
        self.assertIn("Forge: New one", verbs)
        self.assertNotIn(r"C:\Old", " ".join(self.registry.commands()))
        self.assertEqual(
            context_menu.menu_state(self.registry, python=self.python, main_py=INSTALLED), MenuState.ON
        )

    def test_refresh_drops_entries_of_deleted_presets(self) -> None:
        self.registry.put(menu_of(INSTALLED, presets=("Auto", "Gone")))
        self.assertTrue(self.refresh(["Auto"], ["Auto", "Gone"]))
        self.assertEqual(len(self.registry.commands()), 2)  # one entry, for files and folders

    def test_refresh_never_takes_over_another_copys_menu(self) -> None:
        # MediaForge's review I4: a development copy used to rewrite the installed copy's menu.
        for name, entries in {
            "another copy": menu_of(DEV_COPY),
            "the old MediaForge": old_mediaforge_menu(),
        }.items():
            with self.subTest(name):
                self.registry = FakeRegistry()
                self.registry.put(entries)
                before = dict(self.registry.keys)
                self.assertFalse(self.refresh(["Auto", "Plex"], []))
                self.assertEqual(self.registry.keys, before)
                self.assertEqual(self.registry.writes, 0)

    def test_refresh_leaves_a_menu_that_is_off(self) -> None:
        self.assertFalse(self.refresh(["Auto"], []))
        self.assertEqual(self.registry.keys, {})

    def test_install_replaces_the_whole_menu(self) -> None:
        self.registry.put(menu_of(INSTALLED, presets=("A", "B", "C")))
        context_menu.install(menu_of(INSTALLED, presets=("Auto",)), self.registry)
        self.assertEqual(len(self.registry.commands()), 2)
        context_menu.uninstall(self.registry)
        self.assertEqual(self.registry.keys, {})

    def test_remove_own_leaves_another_programs_menu(self) -> None:
        self.registry.put(old_mediaforge_menu())
        self.assertFalse(context_menu.remove_own(self.registry, main_py=INSTALLED))
        self.assertEqual(len(self.registry.commands()), 2)
        self.registry = FakeRegistry()
        self.registry.put(menu_of(INSTALLED))
        self.assertTrue(context_menu.remove_own(self.registry, main_py=INSTALLED))
        self.assertEqual(self.registry.keys, {})

    def test_the_real_registry_is_guarded(self) -> None:
        # Without a fake, the real registry's writes refuse and it reads as empty.
        real = context_menu.WindowsRegistry()
        self.assertEqual(context_menu.menu_state(real), MenuState.OFF)
        self.assertFalse(context_menu.legacy_installed())
        with self.assertRaises(RealContextMenuWrite):
            context_menu.install(menu_of(INSTALLED))
        with self.assertRaises(RealContextMenuWrite):
            real.write(r"Software\Classes\x", {"": "y"})
        self.assertFalse(context_menu.refresh(["Auto"], []))  # off: nothing written


class FakePresets:
    def __init__(self, names: list[str], *, fail: bool = False) -> None:
        self.names = names
        self.fail = fail

    async def list(self) -> list[Preset]:
        await asyncio.sleep(0)
        if self.fail:
            raise OSError("forge.db is locked")
        return [DEFAULT_PRESETS[0].model_copy(update={"name": name}) for name in self.names]


def sources(names: list[str], chosen: list[str], *, fail: bool = False) -> Any:
    return SimpleNamespace(
        presets=FakePresets(names, fail=fail),
        settings=SimpleNamespace(current=ForgeSettings(context_menu_presets=chosen)),
    )


class RefreshFromTests(ForgeAsyncTestCase):
    async def test_refreshes_with_forges_presets_and_choice(self) -> None:
        registry = FakeRegistry()
        registry.put(menu_of(INSTALLED, python=str(context_menu.python_runner())))
        done = await context_menu.refresh_from(
            sources(["Auto", "Plex", "Kodi"], ["Kodi", "Plex"]), registry, main_py=INSTALLED
        )
        self.assertTrue(done)
        self.assertEqual(
            [c.split("--preset ")[1] for c in registry.commands()][:2], ['"Plex" "%1"', '"Kodi" "%1"']
        )

    async def test_never_raises(self) -> None:
        registry = FakeRegistry()
        registry.put(menu_of(INSTALLED))
        with self.assertLogs("app.forge.context_menu", "WARNING") as logs:
            self.assertFalse(await context_menu.refresh_from(sources([], [], fail=True), registry, main_py=INSTALLED))
        self.assertNotIn("locked", "\n".join(logs.output))  # the type only
        registry.fail_writes = True
        with self.assertLogs("app.forge.context_menu", "WARNING"):
            self.assertFalse(await context_menu.refresh_from(sources(["Auto"], []), registry, main_py=INSTALLED))

    async def test_the_real_menu_is_left_alone_in_tests(self) -> None:
        self.assertFalse(await context_menu.refresh_from(sources(["Auto"], [])))


class MenuActionTests(ForgeAsyncTestCase):
    """`main.py --forge-menu on|off` (D8: install.ps1 turns the menu on for a new install)."""

    def setUp(self) -> None:
        super().setUp()
        self.paths = AppPaths.default()
        self.registry = FakeRegistry()
        self.python = Path(PYTHONW)

    async def action(self, action: str) -> int:
        return await context_menu.menu_action(
            self.paths, action, self.registry, python=self.python, main_py=INSTALLED
        )

    async def test_on_for_a_new_install_shows_every_default_preset(self) -> None:
        self.assertEqual(await self.action("on"), EXIT_OK)
        commands = self.registry.commands()
        self.assertEqual(len(commands), 2 * len(DEFAULT_PRESETS))
        self.assertTrue(all(c.startswith(f'"{PYTHONW}" "{INSTALLED}" --forge --confirm') for c in commands))
        self.assertFalse((self.paths.forge / "forge.db").exists())  # no database made for it

    async def test_on_uses_forges_presets_and_the_choice(self) -> None:
        from app.forge.db.database import Database, db_path
        from app.forge.db.presets import PresetRepo
        from app.forge.db.settings import SettingsRepo

        db = await Database.open(db_path(self.paths))
        try:
            await PresetRepo(db).seed_defaults_if_empty()
            await PresetRepo(db).save(DEFAULT_PRESETS[0].model_copy(update={"name": "Plex"}))
            await SettingsRepo(db).save(ForgeSettings(context_menu_presets=["Plex", "Auto"]))
        finally:
            await db.close()
        self.assertEqual(await self.action("on"), EXIT_OK)
        verbs = sorted(v["MUIVerb"] for v in self.registry.keys.values() if "MultiSelectModel" in v)
        self.assertEqual(verbs, ["Forge: Auto", "Forge: Auto", "Forge: Plex", "Forge: Plex"])

    async def test_on_leaves_another_programs_menu_alone(self) -> None:
        self.registry.put(old_mediaforge_menu())
        before = dict(self.registry.keys)
        self.assertEqual(await self.action("on"), EXIT_OK)
        self.assertEqual(self.registry.keys, before)

    async def test_off_removes_only_this_copys_menu(self) -> None:
        self.registry.put(menu_of(DEV_COPY))
        self.assertEqual(await self.action("off"), EXIT_OK)
        self.assertEqual(len(self.registry.commands()), 2)
        self.registry = FakeRegistry()
        self.registry.put(menu_of(INSTALLED))
        self.assertEqual(await self.action("off"), EXIT_OK)
        self.assertEqual(self.registry.keys, {})

    async def test_a_failure_and_a_bad_word(self) -> None:
        self.registry.fail_writes = True
        with self.assertLogs("app.forge.context_menu", "ERROR") as logs:
            self.assertEqual(await self.action("on"), EXIT_FAILED)
        self.assertNotIn("access denied", "\n".join(logs.output))
        with self.assertLogs("app.forge.context_menu", "ERROR"):
            self.assertEqual(await self.action("maybe"), EXIT_USAGE)
