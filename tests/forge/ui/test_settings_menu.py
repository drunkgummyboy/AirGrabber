"""Settings › Windows › Explorer's right-click menu (app/forge/ui/settings_menu.py, one box): on /
off, which presets, whose menu Explorer has, the .reg files, each click stored at once (the menu
on with no preset ticked refused in the box; Undo puts it back), Naming's stored presets carried
along. The registry is a fake (tests/forge/test_context_menu.py's); the guard in
tests/forge/base.py keeps the real one out."""

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
    STATUS,
    MenuSection,
)
from app.forge.ui.settings_naming import PresetsChange
from app.forge.ui.settings_pages import FORGE_PARTS
from app.i18n import Language, t
from app.ui.settings_dialog import SettingsDialog
from app.ui.settings_pages_registry import PAGE_ORDER, PAGE_WINDOWS
from tests.forge.ui.settings_kit import forge_settings
from tests.forge.base import ForgeAsyncTestCase
from tests.forge.test_context_menu import FakeRegistry, old_mediaforge_menu
from tests.forge.tk import pump
from tests.test_settings_dialog import settle


async def done(task: "asyncio.Task[bool] | None") -> bool:
    """A click's change, stored."""
    assert task is not None
    return await task

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
        dialog = forge_settings(self.root, forge, section=PAGE_WINDOWS)
        part = dialog.parts[PAGE_WINDOWS]
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

    async def test_one_box_on_windows_the_last_mediaforge_part(self) -> None:
        dialog, part, _parts = await self.ready()
        forge_pages = [page for page in PAGE_ORDER if page in FORGE_PARTS]
        self.assertEqual(forge_pages[-1], PAGE_WINDOWS)
        self.assertEqual(dialog.section, PAGE_WINDOWS)
        texts = labels(dialog.page(PAGE_WINDOWS))
        self.assertEqual(texts[0], "Windows")
        order = [
            texts.index(text)
            for text in (
                "In the background",
                "Explorer's right-click menu",
                "Presets in the menu",
                "Registry files",
            )
        ]
        self.assertEqual(order, sorted(order))
        self.assertEqual(len(part._content), 1)  # the tick, the presets and the files together
        for widget in (part.on_box, part.presets_frame, part.reg_button):
            self.assertIs(widget.master, part._content[0].body)

    async def test_off_shows_every_preset_ticked(self) -> None:
        dialog, part, _parts = await self.ready()
        self.assertFalse(part.on)
        self.assertEqual(part.state, MenuState.OFF)
        self.assertEqual(part.ticked(), NAMES)
        texts = labels(dialog.page(PAGE_WINDOWS))
        self.assertIn(t(STATUS[MenuState.OFF]), texts)
        self.assertNotIn(t(GETTING_READY), texts)
        self.assertFalse(part.legacy_button.winfo_manager())  # no old HKLM menu here

    async def test_waits_for_forge_and_says_when_it_cant_start(self) -> None:
        forge = FakeForge(self.parts(), started=False)
        forge.gate = asyncio.get_running_loop().create_future()
        dialog, part = await self.dialog(forge)
        self.assertFalse(part.ready)
        self.assertIn(t(GETTING_READY), labels(dialog.page(PAGE_WINDOWS)))
        self.assertIsNone(part.set_on(True))  # nothing to change yet
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

    # --- on / off, at once ------------------------------------------------------------------------

    async def test_turning_it_on_writes_it_at_once(self) -> None:
        dialog, part, parts = await self.ready()
        task = part.set_on(True)
        self.assertEqual(self.registry.keys, {})  # in a moment
        assert task is not None
        self.assertTrue(await task)
        self.assertEqual(self.verbs(), [f"Forge: {name}" for name in NAMES])
        self.assertTrue(all(f'"{MAIN_PY}" --forge --confirm' in c for c in self.registry.commands()))
        self.assertEqual(parts.settings.updates, [])  # every preset: [] stays []
        self.assertEqual(part.state, MenuState.ON)
        self.assertIn(t(STATUS[MenuState.ON]), labels(dialog.page(PAGE_WINDOWS)))
        bar = dialog.committer.bar
        assert bar is not None
        self.assertTrue(bar.shown)

    async def test_undo_takes_it_off_again(self) -> None:
        dialog, part, _parts = await self.ready()
        task = part.set_on(True)
        assert task is not None
        await task
        self.assertTrue(await dialog.committer.run_undo())
        self.assertEqual(self.registry.keys, {})
        self.assertEqual(part.state, MenuState.OFF)
        self.assertFalse(part.on)

    async def test_closing_leaves_what_was_stored(self) -> None:
        dialog, part, parts = await self.ready()
        task = part.set_on(True)
        dialog.destroy()
        assert task is not None
        await task  # stored all the same; nothing of the closed page touched
        await pump(self.root, 0.1)
        self.assertEqual(len(self.verbs()), 3)
        self.assertEqual(parts.settings.updates, [])

    async def test_chosen_presets_are_stored_and_shown_in_the_menu(self) -> None:
        _dialog, part, parts = await self.ready()
        await done(part.set_on(True))
        await done(part.set_ticked(["Movie (Folder)", "Auto"]))
        self.assertEqual(parts.settings.updates, [{"context_menu_presets": ["Auto", "Movie (Folder)"]}])
        self.assertEqual(self.verbs(), ["Forge: Auto", "Forge: Movie (Folder)"])
        # Ticking every one again stores [] (presets added later appear too).
        await done(part.set_ticked(NAMES))
        self.assertEqual(parts.settings.current.context_menu_presets, [])
        self.assertEqual(len(self.verbs()), 3)

    async def test_on_with_no_preset_ticked_is_refused_in_its_box(self) -> None:
        _dialog, part, parts = await self.ready()
        await done(part.set_on(True))
        writes = self.registry.writes
        self.assertFalse(await done(part.set_ticked([])))
        self.assertEqual(part._menu_box.refusal, t(PICK_ONE))
        self.assertEqual(self.registry.writes, writes)  # nothing stored
        self.assertEqual(parts.settings.updates, [])
        self.assertEqual(part.ticked(), NAMES)  # the ticks go back
        self.assertTrue(await done(part.set_on(False)))  # off needs no preset
        self.assertEqual(part._menu_box.refusal, "")

    async def test_turning_it_off_removes_this_copys_menu(self) -> None:
        self.registry.put(this_copys_menu(["Auto"]))
        _dialog, part, _parts = await self.ready(ForgeSettings(context_menu_presets=["Auto"]))
        self.assertEqual(part.state, MenuState.ON)
        self.assertTrue(part.on)
        self.assertEqual(part.ticked(), ["Auto"])
        await done(part.set_on(False))
        self.assertEqual(self.registry.keys, {})
        self.assertEqual(part.state, MenuState.OFF)

    async def test_opening_writes_nothing(self) -> None:
        self.registry.put(this_copys_menu(NAMES))
        _dialog, _part, parts = await self.ready()
        await settle()
        self.assertEqual(self.registry.writes, 0)
        self.assertEqual(parts.settings.updates, [])

    async def test_a_stale_menu_is_put_right_by_ticking_it_again(self) -> None:
        self.registry.put(this_copys_menu(["Auto"], python=Path(r"C:\Old\Python312\python.exe")))
        dialog, part, _parts = await self.ready()
        self.assertEqual(part.state, MenuState.STALE)
        self.assertTrue(part.on)
        self.assertIn(t(STATUS[MenuState.STALE]), labels(dialog.page(PAGE_WINDOWS)))
        await done(part.set_on(False))
        await done(part.set_on(True))
        self.assertEqual(part.state, MenuState.ON)
        self.assertNotIn(r"C:\Old", " ".join(self.registry.commands()))

    async def test_another_programs_menu_is_kept_until_the_box_is_ticked(self) -> None:
        self.registry.put(old_mediaforge_menu())
        dialog, part, _parts = await self.ready()
        self.assertEqual(part.state, MenuState.OLD_MEDIAFORGE)
        self.assertFalse(part.on)
        self.assertIn(t(STATUS[MenuState.OLD_MEDIAFORGE]), labels(dialog.page(PAGE_WINDOWS)))
        await done(part.set_ticked(["Auto"]))  # a preset change alone: not its menu
        self.assertEqual(self.registry.writes, 0)
        self.assertEqual(part.state, MenuState.OLD_MEDIAFORGE)
        await done(part.set_on(True))  # the owner's own choice: it is replaced
        self.assertEqual(part.state, MenuState.ON)

    async def test_a_registry_that_cant_be_written(self) -> None:
        _dialog, part, _parts = await self.ready()
        self.registry.fail_writes = True
        with self.assertLogs("app.forge.ui.settings_menu", "ERROR") as logs:
            self.assertFalse(await done(part.set_on(True)))
        self.assertNotIn("access denied", "\n".join(logs.output))
        self.assertEqual(part._menu_box.refusal, t(SAVE_FAILED))
        self.assertFalse(part.on)  # the tick goes back

    # --- the .reg files -----------------------------------------------------------------------------

    async def test_saves_the_menu_as_a_reg_file(self) -> None:
        _dialog, part, _parts = await self.ready()
        part.set_ticks(["Auto"])  # shown, not stored
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
        await settle()
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
        _dialog, part, parts = await self.ready(settings)
        change = PresetsChange(
            names=("Auto", "Plex Series", "New one"),
            renamed={"Series (Folder)": "Plex Series"},
            removed=("Movie (Folder)",),
        )
        await part._presets_saved(change)
        self.assertEqual(parts.settings.current.context_menu_presets, ["Auto", "Plex Series"])
        self.assertEqual(self.verbs(), ["Forge: Auto", "Forge: Plex Series"])
        self.assertEqual(part.ticked(), ["Auto", "Plex Series"])
        parts.presets.names = list(change.names)
        await done(part.set_ticked(["Auto"]))
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
