"""Instant saving in MediaForge's parts of Settings (spec §3; app/ui/settings_commit.py): a rule's
switch stores through the real RuleService (so a rule switched back on still waits once: nothing
moves unseen), a preset renamed in Naming is stored and the right-click menu's presets follow, the
menu on with no preset ticked is refused in its box, and an own OpenSubtitles key OpenSubtitles
refuses is never stored."""

from __future__ import annotations

import asyncio
import gc
import tkinter as tk
from typing import Any, cast

import customtkinter as ctk

from app import i18n
from app.forge.core.rules_service import RuleService
from app.forge.core.settings import ForgeSettingsService
from app.forge.db.rules import RuleRepo
from app.forge.db.settings import SettingsRepo
from app.forge.models.rules import Rule, RuleTarget
from app.forge.models.settings import ForgeSettings
from app.forge.models.subtitles import KeyRejected
from app.forge.ui.settings_menu import PICK_ONE, MenuSection
from app.forge.ui.settings_naming import NamingSection
from app.forge.ui.settings_pages import LibrarySection
from app.forge.ui.settings_subtitles import SubtitlesSection
from app.i18n import Language
from app.ui.settings_pages_registry import (
    PAGE_ACCOUNTS,
    PAGE_NAMING,
    PAGE_SORTING,
    PAGE_SUBTITLES,
    PAGE_WINDOWS,
)
from tests.forge.db.base import ForgeDbTestCase
from tests.forge.tk import pump_until
from tests.forge.ui.settings_kit import airgrabber_settings, forge_settings
from tests.forge.ui.test_settings_library import FakeForge, FakeRules, FakeSettings
from tests.forge.ui.test_settings_naming import FakeForge as NamingForge
from tests.forge.ui.test_settings_naming import FakePresets
from app.ui.tasks import _error_listeners
from tests.test_settings_dialog import settle


class TkMixin:
    root: ctk.CTk

    def make_root(self, case: Any) -> None:
        gc.collect()
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            case.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        case.addCleanup(self._destroy_root)
        case.addCleanup(i18n.set_language, i18n.current_language())
        i18n.set_language(Language.EN)

    def _destroy_root(self) -> None:
        for child in list(self.root.winfo_children()):
            child.destroy()
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()


class RuleSwitchTests(TkMixin, ForgeDbTestCase):
    """The Sorting page on a real forge.db: RuleService, ForgeSettingsService."""

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        asyncio.get_running_loop().slow_callback_duration = 2.0
        self.make_root(self)
        self.settings = await ForgeSettingsService.load(SettingsRepo(self.db))
        await self.settings.update(series_folder=r"\\nas\Series", movies_folder=r"\\nas\Movies")
        self.rules = RuleService(RuleRepo(self.db), self.settings)
        await self.rules.load()
        mine = Rule(
            folder=str(self.tmp / "in"),
            mode="auto",
            on=False,
            shows=RuleTarget(preset="Series (Folder)"),
            films=RuleTarget(preset="Movie (Folder)"),
        )
        await self.rules.replace_all([mine, *self.rules.current])
        await self.rules.first_look_done([rule.id for rule in self.rules.current])

    async def test_rule_switch_stores_through_rule_service(self) -> None:
        self.assertFalse(any(rule.fresh for rule in self.rules.current))
        forge = FakeForge(cast(FakeSettings, self.settings), rules=cast(FakeRules, self.rules))
        dialog = forge_settings(self.root, forge, section=PAGE_SORTING)
        part = dialog.parts[PAGE_SORTING]
        assert isinstance(part, LibrarySection)
        self.assertFalse(part.rule_rows[0].switch.get())
        part.rule_rows[0].switch.toggle()  # clicked: on, stored at once
        await settle()
        stored = await RuleRepo(self.db).list()
        self.assertTrue(stored[0].on)
        # Switched back on: its first look queues what is already there as Manual once
        # (`manual_once`): nothing moves unseen.
        self.assertTrue(stored[0].fresh)
        self.assertTrue(self.rules.current[0].fresh)
        bar = dialog.committer.bar
        assert bar is not None
        self.assertTrue(bar.shown)
        # Undo stores it off again, the same way.
        self.assertTrue(await dialog.committer.run_undo())
        self.assertFalse((await RuleRepo(self.db).list())[0].on)
        self.assertFalse(part.rule_rows[0].switch.get())
        dialog.destroy()


class NamingAndMenuTests(TkMixin, ForgeDbTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        asyncio.get_running_loop().slow_callback_duration = 2.0
        self.make_root(self)

    async def test_preset_rename_commits_and_the_menu_follows(self) -> None:
        settings = FakeSettings(ForgeSettings(context_menu_presets=["Movie (Flat)"]))
        forge = NamingForge(settings, FakePresets())
        dialog = forge_settings(self.root, forge, section=PAGE_WINDOWS)
        menu = dialog.parts[PAGE_WINDOWS]
        assert isinstance(menu, MenuSection)
        await pump_until(self.root, lambda: menu.ready)
        dialog.show_section(PAGE_NAMING)
        naming = dialog.parts[PAGE_NAMING]
        assert isinstance(naming, NamingSection)
        await pump_until(self.root, lambda: naming.ready)
        naming.select_preset(next(d for d in naming._drafts if d.preset.name == "Movie (Flat)"))
        naming._name.set("Films")
        naming.name_field.commit_now()  # Enter, or the keyboard leaving the field
        await settle()
        assert forge.parts is not None
        self.assertIn("Films", cast(FakePresets, forge.parts.presets).names)
        self.assertEqual(settings.current.context_menu_presets, ["Films"])  # the menu followed
        self.assertIn("Films", menu.ticked())
        dialog.destroy()

    async def test_menu_on_without_presets_is_refused_inline(self) -> None:
        settings = FakeSettings(ForgeSettings())
        forge = NamingForge(settings, FakePresets())
        dialog = forge_settings(self.root, forge, section=PAGE_WINDOWS)
        menu = dialog.parts[PAGE_WINDOWS]
        assert isinstance(menu, MenuSection)
        await pump_until(self.root, lambda: menu.ready)
        menu.set_ticks([])  # every preset unticked (shown only)
        task = menu.set_on(True)
        assert task is not None
        self.assertFalse(await task)
        self.assertEqual(menu._menu_box.refusal, i18n.t(PICK_ONE))
        self.assertFalse(menu.on)  # the tick goes back
        self.assertEqual(settings.updates, [])
        dialog.destroy()


class OwnKeyTests(TkMixin, ForgeDbTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        asyncio.get_running_loop().slow_callback_duration = 2.0
        self.make_root(self)
        self.errors: list[BaseException] = []
        _error_listeners.append(self.errors.append)
        self.addCleanup(_error_listeners.remove, self.errors.append)

    async def accounts(self, source: Any) -> tuple[Any, SubtitlesSection, Any]:
        stored = airgrabber_settings()
        forge = FakeForge(FakeSettings(ForgeSettings()))
        assert forge.services is not None
        forge.services.subtitles = source
        forge.services.airgrabber_settings = stored
        dialog = forge_settings(self.root, forge, section=PAGE_ACCOUNTS, settings=stored)
        part = dialog.parts[PAGE_SUBTITLES]
        assert isinstance(part, SubtitlesSection)
        await pump_until(self.root, lambda: part.ready)
        return dialog, part, stored

    async def test_closing_with_an_own_key_typed_stores_it_after_the_check(self) -> None:
        from tests.forge.ui.subtitle_kit import FakeSubtitleSource

        source = FakeSubtitleSource()
        dialog, part, stored = await self.accounts(source)
        assert part.own_key is not None
        part.own_key.entry.insert(0, "GOODKEY")
        dialog.close()  # ✕ / Escape: the typed key is stored, the row already gone
        await settle()
        self.assertEqual(source.checked, ["GOODKEY"])
        self.assertEqual(stored.current.opensubtitles_own_key.get_secret_value(), "GOODKEY")
        self.assertEqual(self.errors, [])  # no task failed, no error report

    async def test_a_key_typed_during_an_older_ones_check_wins(self) -> None:
        from tests.forge.ui.subtitle_kit import FakeSubtitleSource

        gate = asyncio.get_running_loop().create_future()

        class Slow(FakeSubtitleSource):
            async def check_key(self, key: str) -> None:
                if key == "FIRST":
                    await gate
                await super().check_key(key)

        source = Slow()
        dialog, part, stored = await self.accounts(source)
        assert part.own_key is not None and part.key_field is not None
        part.own_key.entry.insert(0, "FIRST")
        part.key_field.commit_now()
        await asyncio.sleep(0.05)  # FIRST's check is out
        part.own_key.entry.delete(0, "end")
        part.own_key.entry.insert(0, "SECOND")
        part.key_field.commit_now()
        await asyncio.sleep(0.05)
        gate.set_result(None)
        await settle()
        self.assertEqual(stored.current.opensubtitles_own_key.get_secret_value(), "SECOND")
        self.assertEqual(part.own_key.value, "SECOND")
        self.assertEqual(self.errors, [])
        dialog.destroy()

    async def test_own_key_refused_is_not_stored(self) -> None:
        from tests.forge.ui.subtitle_kit import FakeSubtitleSource

        source = FakeSubtitleSource()
        source.key_error = KeyRejected("OpenSubtitles refused your own API key")
        stored = airgrabber_settings()
        forge = FakeForge(FakeSettings(ForgeSettings()))
        assert forge.services is not None
        forge.services.subtitles = source
        forge.services.airgrabber_settings = stored
        dialog = forge_settings(self.root, forge, section=PAGE_ACCOUNTS, settings=stored)
        part = dialog.parts[PAGE_SUBTITLES]
        assert isinstance(part, SubtitlesSection)
        await pump_until(self.root, lambda: part.ready)
        assert part.own_key is not None and part.key_field is not None
        part.own_key.entry.insert(0, "BADKEY")
        part.key_field.left(dialog)  # the keyboard went to another control of the window
        await settle()
        self.assertEqual(stored.current.opensubtitles_own_key.get_secret_value(), "")
        box = dialog.box(PAGE_ACCOUNTS, "OpenSubtitles")
        self.assertEqual(box.refusal, "Own API key: OpenSubtitles refused your own API key")
        self.assertEqual(part.own_key.value, "BADKEY")  # kept, to be corrected
        bar = dialog.committer.bar
        assert bar is not None
        self.assertFalse(bar.shown)
        dialog.destroy()

