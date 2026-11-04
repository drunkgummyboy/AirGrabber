"""Settings › Right-click menu (app/forge/ui/settings_menu.py): on / off, which presets, whose menu Explorer has, the .reg files, changes kept
until Save (D9), Naming's saved presets carried along. The registry is a fake
(tests/forge/test_context_menu.py's); the guard in tests/forge/base.py keeps the real one out."""

from __future__ import annotations

import asyncio
import gc
import tkinter as tk
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import customtkinter as ctk

from app import i18n
from app.forge import context_menu
from app.forge.context_menu import MAIN_PY, MenuState, RegEntry, build_entries
from app.forge.models.preset import DEFAULT_PRESETS, Preset
from app.forge.models.settings import ForgeSettings
from app.forge.ui.settings_menu import (
    GETTING_READY,
    PICK_ONE,
    READ_FAILED,
    SAVE_FAILED,
    SECTION_MENU,
    STATUS,
    MenuSection,
)
from app.forge.ui.settings_naming import PresetsChange
from app.forge.ui.settings_pages import FORGE_SECTIONS
from app.i18n import Language, t
from app.ui.settings_dialog import SettingsDialog
from tests.forge.ui.settings_kit import forge_settings
from tests.forge.base import ForgeAsyncTestCase
from tests.forge.test_context_menu import FakeRegistry, old_mediaforge_menu
from tests.forge.tk import pump

NAMES = ["Auto", "Series (Folder)", "Movie (Folder)"]
NOT_READY_TEXT = "MediaForge isn't ready yet (test)."


def presets(names: list[str]) -> list[Preset]:
    return [DEFAULT_PRESETS[0].model_copy(update={"name": name}) for name in names]


class FakePresets:
    def __init__(self, names: list[str]) -> None:
        self.names = list(names)
        self.fail = False

    async def list(self) -> list[Preset]:
        await asyncio.sleep(0)
        if self.fail:
            raise OSError("forge.db is locked")
        return presets(self.names)

    async def replace_all(self, stored: Sequence[Preset]) -> None:
        self.names = [preset.name for preset in stored]


class FakeSettings:
    def __init__(self, current: ForgeSettings) -> None:
        self.current = current
        self.updates: list[dict[str, Any]] = []
        self.listeners: list[Callable[[ForgeSettings], None]] = []

    async def update(self, **changes: Any) -> ForgeSettings:
        await asyncio.sleep(0)
        self.updates.append(changes)
        self.current = ForgeSettings.model_validate({**self.current.model_dump(), **changes})
        for listener in list(self.listeners):
            listener(self.current)
        return self.current

    def subscribe(self, listener: Callable[[ForgeSettings], None]) -> Callable[[], None]:
        self.listeners.append(listener)
        return lambda: self.listeners.remove(listener)


class FakeMatches:
    async def list(self) -> list[Any]:
        return []

    async def forget(self, _match: Any) -> None:
        pass


class FakeForge:
    """ForgeRuntime's surface: `services` (None until started), `start()`, `reason`."""

    def __init__(self, parts: Any, *, started: bool = True) -> None:
        self._parts = parts
        self.services = parts if started else None
        self.reason = ""
        self.gate: asyncio.Future[None] | None = None

    async def start(self) -> Any:
        if self.gate is not None:
            await asyncio.shield(self.gate)
        if self._parts is None:
            self.reason = NOT_READY_TEXT
        self.services = self._parts
        return self._parts


def this_copys_menu(names: list[str], **kwargs: Any) -> list[RegEntry]:
    prefix = context_menu.command_prefix(**kwargs)
    return build_entries(names, prefix, context_menu.default_icon())


def labels(widget: tk.Misc) -> list[str]:
    found: list[str] = []
    for child in widget.winfo_children():
        if isinstance(child, ctk.CTkLabel) and child.winfo_manager():
            found.append(str(child.cget("text")))
        if child.winfo_manager():
            found += labels(child)
    return found


class MenuSettingsTests(ForgeAsyncTestCase):
    def setUp(self) -> None:
        super().setUp()
        gc.collect()
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        self.addCleanup(i18n.set_language, i18n.current_language())
        i18n.set_language(Language.EN)
        self.registry = FakeRegistry()
        # Every default registry in the section is this fake (the real one stays guarded).
        self.enterContext(mock.patch.object(context_menu, "WindowsRegistry", lambda: self.registry))
        self.saved: list[tuple[str, str]] = []  # what the fake save dialog was asked
        self.save_to: str = ""

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0  # building a dialog is slow

    def _destroy_root(self) -> None:
        for child in list(self.root.winfo_children()):
            child.destroy()
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    def parts(self, settings: ForgeSettings | None = None, names: list[str] = NAMES) -> Any:
        return SimpleNamespace(
            presets=FakePresets(names),
            settings=FakeSettings(settings or ForgeSettings()),
            matches=FakeMatches(),
        )

    async def dialog(self, forge: FakeForge) -> tuple[SettingsDialog, MenuSection]:
        dialog = forge_settings(self.root, forge, section=SECTION_MENU)
        part = dialog.parts[SECTION_MENU]
        assert isinstance(part, MenuSection)
        part.ask_save = self._ask_save
        for _ in range(50):
            if part.ready or not part.waiting_label.winfo_exists():
                break
            await pump(self.root, 0.02)
        return dialog, part

    async def ready(
        self, settings: ForgeSettings | None = None
    ) -> tuple[SettingsDialog, MenuSection, Any]:
        parts = self.parts(settings)
        dialog, part = await self.dialog(FakeForge(parts))
        self.assertTrue(part.ready)
        return dialog, part, parts

    def _ask_save(self, title: str, name: str) -> str:
        self.saved.append((title, name))
        return self.save_to

    def verbs(self) -> list[str]:
        """The files menu's entries, in order."""
        base = context_menu.BASES[0].casefold() + "\\shell\\"
        return [
            values["MUIVerb"]
            for (_hive, key), values in sorted(self.registry.keys.items())
            if key.startswith(base) and "\\command" not in key[len(base) :]
        ]

    # --- the section ---------------------------------------------------------------------------

    async def test_the_section_comes_last_in_the_mediaforge_group(self) -> None:
        dialog, _part, _parts = await self.ready()
        self.assertEqual(list(FORGE_SECTIONS)[-1], SECTION_MENU)
        self.assertEqual(dialog.section, SECTION_MENU)
        self.assertEqual(labels(dialog.page(SECTION_MENU))[0], "Right-click menu")

    async def test_off_shows_every_preset_ticked(self) -> None:
        dialog, part, _parts = await self.ready()
        self.assertFalse(part.on)
        self.assertEqual(part.state, MenuState.OFF)
        self.assertEqual(part.ticked(), NAMES)
        texts = labels(dialog.page(SECTION_MENU))
        self.assertIn(t(STATUS[MenuState.OFF]), texts)
        self.assertNotIn(t(GETTING_READY), texts)
        self.assertFalse(part.legacy_button.winfo_manager())  # no old HKLM menu here
        self.assertEqual(part.changes(), {})

    async def test_waits_for_forge_and_says_when_it_cant_start(self) -> None:
        forge = FakeForge(self.parts(), started=False)
        forge.gate = asyncio.get_running_loop().create_future()
        dialog, part = await self.dialog(forge)
        self.assertFalse(part.ready)
        self.assertIn(t(GETTING_READY), labels(dialog.page(SECTION_MENU)))
        self.assertTrue(await dialog._save_all())  # nothing to save yet: Save still works
        forge.gate.set_result(None)
        await pump(self.root, 0.2)
        self.assertTrue(part.ready)
        dialog.destroy()
        _dialog, part = await self.dialog(FakeForge(None, started=False))
        self.assertFalse(part.ready)
        self.assertEqual(str(part.waiting_label.cget("text")), NOT_READY_TEXT)

    async def test_presets_that_cant_be_read_are_said_plainly(self) -> None:
        parts = self.parts()
        parts.presets.fail = True
        with self.assertLogs("app.forge.ui.settings_menu", "ERROR") as logs:
            _dialog, part = await self.dialog(FakeForge(parts))
        self.assertNotIn("locked", "\n".join(logs.output))
        self.assertEqual(str(part.waiting_label.cget("text")), t(READ_FAILED))

    # --- on / off, on Save only ------------------------------------------------------------------

    async def test_turning_it_on_waits_for_save(self) -> None:
        dialog, part, parts = await self.ready()
        part.set_on(True)
        self.assertEqual(self.registry.keys, {})  # nothing written yet
        self.assertEqual(part.changes(), {"on": True})
        self.assertTrue(await dialog._save_all())
        self.assertEqual(self.verbs(), [f"Forge: {name}" for name in NAMES])
        self.assertTrue(all(f'"{MAIN_PY}" --forge --confirm' in c for c in self.registry.commands()))
        self.assertEqual(parts.settings.updates, [])  # every preset: [] stays []
        self.assertEqual(part.state, MenuState.ON)
        self.assertIn(t(STATUS[MenuState.ON]), labels(dialog.page(SECTION_MENU)))
        self.assertEqual(part.changes(), {})

    async def test_cancel_changes_nothing(self) -> None:
        dialog, part, parts = await self.ready()
        part.set_on(True)
        part.set_ticked(["Auto"])
        dialog.destroy()
        await pump(self.root, 0.1)
        self.assertEqual(self.registry.keys, {})
        self.assertEqual(parts.settings.updates, [])

    async def test_chosen_presets_are_stored_and_shown_in_the_menu(self) -> None:
        dialog, part, parts = await self.ready()
        part.set_on(True)
        part.set_ticked(["Movie (Folder)", "Auto"])
        self.assertTrue(await dialog._save_all())
        self.assertEqual(parts.settings.updates, [{"context_menu_presets": ["Auto", "Movie (Folder)"]}])
        self.assertEqual(self.verbs(), ["Forge: Auto", "Forge: Movie (Folder)"])
        # Ticking every one again stores [] (presets added later appear too).
        part.set_ticked(NAMES)
        self.assertTrue(await dialog._save_all())
        self.assertEqual(parts.settings.current.context_menu_presets, [])
        self.assertEqual(len(self.verbs()), 3)

    async def test_on_with_no_preset_ticked_cant_be_saved(self) -> None:
        dialog, part, _parts = await self.ready()
        part.set_on(True)
        part.set_ticked([])
        self.assertEqual(part.problem(), t(PICK_ONE))
        self.assertFalse(await dialog._save_all())
        self.assertEqual(self.registry.keys, {})
        part.set_on(False)  # off: no preset needed, and nothing stored
        self.assertIsNone(part.problem())
        self.assertEqual(part.changes(), {})

    async def test_turning_it_off_removes_this_copys_menu(self) -> None:
        self.registry.put(this_copys_menu(["Auto"]))
        dialog, part, _parts = await self.ready(ForgeSettings(context_menu_presets=["Auto"]))
        self.assertEqual(part.state, MenuState.ON)
        self.assertTrue(part.on)
        self.assertEqual(part.ticked(), ["Auto"])
        part.set_on(False)
        self.assertTrue(await dialog._save_all())
        self.assertEqual(self.registry.keys, {})
        self.assertEqual(part.state, MenuState.OFF)

    async def test_an_unchanged_form_writes_nothing(self) -> None:
        self.registry.put(this_copys_menu(NAMES))
        dialog, part, parts = await self.ready()
        self.assertTrue(await dialog._save_all())
        self.assertEqual(self.registry.writes, 0)
        self.assertEqual(parts.settings.updates, [])

    async def test_a_stale_menu_is_put_right_on_save(self) -> None:
        self.registry.put(this_copys_menu(["Auto"], python=Path(r"C:\Old\Python312\python.exe")))
        dialog, part, _parts = await self.ready()
        self.assertEqual(part.state, MenuState.STALE)
        self.assertTrue(part.on)
        self.assertIn(t(STATUS[MenuState.STALE]), labels(dialog.page(SECTION_MENU)))
        self.assertTrue(await dialog._save_all())
        self.assertEqual(part.state, MenuState.ON)
        self.assertNotIn(r"C:\Old", " ".join(self.registry.commands()))

    async def test_another_programs_menu_is_kept_until_the_box_is_ticked(self) -> None:
        self.registry.put(old_mediaforge_menu())
        dialog, part, _parts = await self.ready()
        self.assertEqual(part.state, MenuState.OLD_MEDIAFORGE)
        self.assertFalse(part.on)
        self.assertIn(t(STATUS[MenuState.OLD_MEDIAFORGE]), labels(dialog.page(SECTION_MENU)))
        part.set_ticked(["Auto"])  # a preset change alone never touches its menu
        self.assertTrue(await dialog._save_all())
        self.assertEqual(self.registry.writes, 0)
        self.assertEqual(part.state, MenuState.OLD_MEDIAFORGE)
        part.set_on(True)  # the owner's own choice: it is replaced
        self.assertTrue(await dialog._save_all())
        self.assertEqual(part.state, MenuState.ON)

    async def test_a_registry_that_cant_be_written(self) -> None:
        dialog, part, _parts = await self.ready()
        self.registry.fail_writes = True
        part.set_on(True)
        with self.assertLogs("app.forge.ui.settings_menu", "ERROR") as logs:
            self.assertFalse(await dialog._save_all())
        self.assertNotIn("access denied", "\n".join(logs.output))
        self.assertEqual(str(dialog._error.cget("text")), t(SAVE_FAILED))

    # --- the .reg files -----------------------------------------------------------------------------

    async def test_saves_the_menu_as_a_reg_file(self) -> None:
        _dialog, part, _parts = await self.ready()
        part.set_ticked(["Auto"])
        self.save_to = str(self.tmp / "menu.reg")
        part.save_reg_file()
        self.assertEqual(self.saved[0][1], "MediaForge menu.reg")
        raw = (self.tmp / "menu.reg").read_bytes()
        self.assertTrue(raw.startswith(b"\xff\xfe"))  # UTF-16, as regedit writes it
        text = raw.decode("utf-16")
        self.assertTrue(text.startswith("Windows Registry Editor Version 5.00\r\n"))
        self.assertNotIn("\r\r\n", text)
        self.assertIn('"MUIVerb"="Forge: Auto"', text)
        self.assertNotIn("Forge: Series", text)
        self.assertEqual(self.registry.keys, {})  # only the file

    async def test_the_old_menus_removal_file_shows_only_while_it_is_there(self) -> None:
        self.registry.keys[("HKLM", context_menu.LEGACY_HKLM_KEYS[0].casefold())] = {
            "SubCommands": "MediaForge_Auto"
        }
        dialog, part, _parts = await self.ready()
        self.assertTrue(part.legacy)
        self.assertTrue(part.legacy_button.winfo_manager())
        self.save_to = str(self.tmp / "remove.reg")
        part.save_legacy_file()
        self.assertEqual(self.saved[0][1], "Remove old MediaForge menu.reg")
        text = (self.tmp / "remove.reg").read_text(encoding="utf-16")
        self.assertIn(r"[-HKEY_LOCAL_MACHINE\SOFTWARE\Classes\*\shell\MediaForgeCommands]", text)
        self.save_to = str(self.tmp / "no such folder" / "x.reg")
        with self.assertLogs("app.forge.ui.settings_menu", "WARNING"):
            part.save_legacy_file()
        self.assertTrue(part.file_label.winfo_manager())
        self.save_to = ""  # Cancel
        part.save_reg_file()
        self.assertEqual(len(self.saved), 3)
        dialog.destroy()

    # --- Naming's saved presets ------------------------------------------------------------------

    async def test_a_preset_renamed_in_naming_is_carried_along(self) -> None:
        self.registry.put(this_copys_menu(["Auto", "Series (Folder)"]))
        settings = ForgeSettings(context_menu_presets=["Auto", "Series (Folder)"])
        dialog, part, parts = await self.ready(settings)
        part.set_ticked(["Auto"])  # not saved yet: kept
        change = PresetsChange(
            names=("Auto", "Plex Series", "New one"),
            renamed={"Series (Folder)": "Plex Series"},
            removed=("Movie (Folder)",),
        )
        await part._presets_saved(change)
        self.assertEqual(parts.settings.current.context_menu_presets, ["Auto", "Plex Series"])
        self.assertEqual(self.verbs(), ["Forge: Auto", "Forge: Plex Series"])
        self.assertEqual(part.ticked(), ["Auto"])  # the unsaved untick stays
        parts.presets.names = list(change.names)
        self.assertTrue(await dialog._save_all())
        self.assertEqual(self.verbs(), ["Forge: Auto"])

    async def test_naming_changes_leave_another_programs_menu_alone(self) -> None:
        self.registry.put(old_mediaforge_menu())
        _dialog, part, parts = await self.ready()
        await part._presets_saved(PresetsChange(names=("Auto",), renamed={}, removed=()))
        self.assertEqual(self.registry.writes, 0)
        self.assertEqual(parts.settings.updates, [])
        self.assertEqual(part.ticked(), ["Auto"])

    async def test_it_listens_to_naming_only_while_open(self) -> None:
        from app.forge.ui import settings_naming

        dialog, part, _parts = await self.ready()
        self.assertIn(part._presets_saved, settings_naming._listeners)
        dialog.destroy()
        await pump(self.root, 0.1)
        self.assertNotIn(part._presets_saved, settings_naming._listeners)
