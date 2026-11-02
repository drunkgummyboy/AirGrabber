"""MediaForge's own Settings window (app/forge/ui/settings_window.py; INTEGRATION.md 3.7, the
owner's decision of 2026-11-02): its sections from FORGE_SECTIONS in order with their icons,
built by their own builders; title, icon, transient to the Forge window, the copper look; Save
(D9: every section's problem first, nothing stored on one; then each saves and the window
closes); Cancel / ✕ / Escape store nothing; the footer's shared line and "Open AirGrabber's
Settings"; freed when closed; the light / dark switch; Dutch. And AirGrabber's Settings without
the MediaForge group.

The sections run on the fakes of tests/forge/ui/test_settings_library.py (no forge.db); the
right-click menu's registry is tests/forge/base.py's guard (reads as not installed)."""

from __future__ import annotations

import asyncio
import gc
import tkinter as tk
import weakref
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import customtkinter as ctk

from app import i18n
from app.forge.models.settings import ForgeSettings
from app.forge.ui import settings_window
from app.forge.ui.look import forge_look
from app.forge.ui.settings_menu import SECTION_MENU
from app.forge.ui.settings_naming import SECTION_NAMING, NamingSection
from app.forge.ui.settings_pages import FORGE_SECTIONS, SECTION_LIBRARY, LibrarySection
from app.forge.ui.settings_window import ForgeSettingsWindow
from app.forge.ui.window import set_forge_icon
from app.i18n import Language
from app.models import Settings
from app.services import SettingsService
from app.ui import theme
from app.ui.settings_dialog import SECTION_GROUPS, SECTIONS, SettingsDialog
from app.ui.widgets import install_window_release
from tests.forge.base import ForgeAsyncTestCase
from tests.forge.tk import press, pump, pump_until
from tests.forge.ui.test_settings_library import READY, FakeForge, FakeSettings, labels
from tests.test_settings_dialog import FakeProwlarr, FakeRepository, FakeShortcut


def descendants(widget: tk.Misc) -> list[tk.Misc]:
    found: list[tk.Misc] = []
    for child in widget.winfo_children():
        found += [child, *descendants(child)]
    return found


class SettingsWindowCase(ForgeAsyncTestCase):
    def setUp(self) -> None:
        super().setUp()
        gc.collect()
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        install_window_release(self.root)  # as MainWindow: a closed window is forgotten
        self.addCleanup(self._destroy_root)
        self.addCleanup(i18n.set_language, i18n.current_language())
        i18n.set_language(Language.EN)
        self.addCleanup(ctk.set_appearance_mode, ctk.get_appearance_mode())
        self.opened: list[int] = []  # "Open AirGrabber's Settings"

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0  # building a window is slow

    def _destroy_root(self) -> None:
        for child in list(self.root.winfo_children()):
            child.destroy()
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()  # the window's Tk variables, on the main thread

    def forge_window(self) -> ctk.CTkToplevel:
        """A stand-in for the Forge window: mapped, but see-through."""
        if not hasattr(self, "_forge_window"):
            self._forge_window = ctk.CTkToplevel(self.root)
            self._forge_window.attributes("-alpha", 0.0)
        return self._forge_window

    def make(
        self, settings: ForgeSettings = READY, *, section: str | None = None
    ) -> tuple[ForgeSettingsWindow, FakeForge]:
        forge = FakeForge(FakeSettings(settings))
        window = ForgeSettingsWindow(
            self.forge_window(),
            cast(Any, forge),
            section=section,
            on_open_airgrabber=lambda: self.opened.append(1),
        )
        window.attributes("-alpha", 0.0)
        return window, forge

    async def ready(
        self, settings: ForgeSettings = READY, *, section: str | None = None
    ) -> tuple[ForgeSettingsWindow, FakeForge]:
        window, forge = self.make(settings, section=section)
        naming = window.parts[SECTION_NAMING]
        assert isinstance(naming, NamingSection)
        await pump_until(self.root, lambda: naming.ready)
        return window, forge

    @staticmethod
    def library(window: ForgeSettingsWindow) -> LibrarySection:
        part = window.parts[SECTION_LIBRARY]
        assert isinstance(part, LibrarySection)
        return part

    @staticmethod
    def stored(forge: FakeForge) -> FakeSettings:
        assert forge.services is not None
        return cast(FakeSettings, forge.services.settings)


class SectionsTests(SettingsWindowCase):
    async def test_the_sections_are_built_in_order_with_their_icons(self) -> None:
        window, _forge = await self.ready()
        self.assertEqual(window.sections, list(FORGE_SECTIONS))
        self.assertEqual(list(window.parts), list(FORGE_SECTIONS))
        self.assertIsInstance(window.parts[SECTION_LIBRARY], LibrarySection)
        self.assertIsInstance(window.parts[SECTION_NAMING], NamingSection)
        # The group's label, then an icon and a name per section, in FORGE_SECTIONS' order.
        expected = ["MEDIAFORGE"]
        for name, section in FORGE_SECTIONS.items():
            expected += [section.icon, name]
        self.assertEqual(labels(window._sidebar), expected)
        for name, section in FORGE_SECTIONS.items():
            with self.subTest(section=name):
                texts = labels(window.page(name))
                self.assertEqual(texts[:2], [name, i18n.t(section.note)])
                self.assertGreater(len(texts), 2)  # the section's own boxes

    async def test_opens_at_the_first_section_or_the_asked_one(self) -> None:
        window, _forge = self.make()
        self.assertEqual(window.section, next(iter(FORGE_SECTIONS)))
        self.assertTrue(window._items[window.section].selected)
        window.destroy()
        window, _forge = self.make(section=SECTION_MENU)
        self.assertEqual(window.section, SECTION_MENU)
        window.show_section(SECTION_NAMING)
        self.assertEqual(window.section, SECTION_NAMING)
        self.assertTrue(window._items[SECTION_NAMING].selected)
        self.assertFalse(window._items[SECTION_MENU].selected)
        window.show_section("Where downloads go")  # not here: nothing changes
        self.assertEqual(window.section, SECTION_NAMING)
        window._items[SECTION_LIBRARY].click()  # the sidebar
        self.assertEqual(window.section, SECTION_LIBRARY)
        self.assertEqual(window.page(SECTION_NAMING).winfo_manager(), "")

    async def test_title_icon_transient_and_the_copper_look(self) -> None:
        forge_window = ctk.CTkToplevel(self.root)  # the Forge window
        forge_window.attributes("-alpha", 0.0)
        with mock.patch.object(
            settings_window, "set_forge_icon", wraps=set_forge_icon
        ) as icon:
            window = ForgeSettingsWindow(forge_window, cast(Any, FakeForge(FakeSettings(READY))))
        icon.assert_called_once_with(window)  # MediaForge's icon, from the .ico read once
        self.assertEqual(window.title(), "MediaForge Settings")
        self.assertEqual(str(window.transient()), str(forge_window))
        self.assertIs(window.master, forge_window)
        look = forge_look()
        self.assertIs(window.look, look)
        self.assertEqual(tuple(window.cget("fg_color")), look.BG)
        self.assertEqual(tuple(window._save_button.cget("fg_color")), look.ACCENT)
        page = window._pages[SECTION_LIBRARY]
        self.assertIs(page.look, look)  # the sections paint with it
        part = self.library(window)
        self.assertEqual(tuple(part.use_box.cget("fg_color")), look.ACCENT)
        self.assertEqual(tuple(window._items[window.section].cget("fg_color")), look.ROW_SELECTED)

    async def test_the_footer_says_what_is_shared_and_opens_airgrabbers_settings(self) -> None:
        window, _forge = self.make()
        self.assertEqual(
            window.shared_label.cget("text"),
            "Language, theme, TMDB key and updates are shared with AirGrabber.",
        )
        self.assertEqual(window.airgrabber_button.cget("text"), "Open AirGrabber's Settings")
        window.airgrabber_button.invoke()
        self.assertEqual(self.opened, [1])
        self.assertTrue(window.winfo_exists())  # it stays open

    async def test_dutch(self) -> None:
        i18n.set_language(Language.NL)
        window, _forge = self.make()
        self.assertEqual(window.title(), "MediaForge-instellingen")
        self.assertEqual(
            window.shared_label.cget("text"),
            "Taal, thema, TMDB-sleutel en updates deel je met AirGrabber.",
        )
        self.assertEqual(window.airgrabber_button.cget("text"), "AirGrabber-instellingen openen")
        self.assertEqual(labels(window.page(SECTION_LIBRARY))[0], "Bibliotheek & bewaken")


class SaveTests(SettingsWindowCase):
    async def test_a_problem_in_one_section_stores_nothing_anywhere(self) -> None:
        window, forge = await self.ready(section=SECTION_MENU)
        library = self.library(window)
        library._needs.set(False)  # a fine change in the first section
        naming = window.parts[SECTION_NAMING]
        assert isinstance(naming, NamingSection)
        naming._name.set("  ")  # a preset without a name in the second
        self.assertFalse(await window._save_all())
        self.assertEqual(self.stored(forge).updates, [])  # Library's change wasn't stored either
        self.assertEqual(window.section, SECTION_NAMING)  # the page with the problem shows
        self.assertEqual(str(window._error.cget("text")), "Give every preset a name.")
        self.assertTrue(window.winfo_exists())

    async def test_save_stores_and_closes(self) -> None:
        window, forge = await self.ready()
        library = self.library(window)
        library._needs.set(False)
        library.set_mode("trial")
        window._save_button.invoke()  # async_command: Save runs on the loop
        await pump_until(self.root, lambda: not window.winfo_exists())
        self.assertEqual(
            self.stored(forge).updates, [{"automatic": "trial", "notify_needs": False}]
        )

    async def test_a_failed_save_says_why_and_stays(self) -> None:
        window, forge = await self.ready()
        self.library(window)._summary.set(False)
        self.stored(forge).fail = True
        with self.assertLogs("app.forge.ui.settings_pages", "ERROR"):
            await window._save()
        self.assertTrue(window.winfo_exists())
        self.assertIn("couldn't be saved", str(window._error.cget("text")))
        self.assertEqual(str(window._save_button.cget("state")), "normal")
        self.assertEqual(window.section, SECTION_LIBRARY)

    async def test_cancel_close_and_escape_store_nothing(self) -> None:
        for how in ("cancel", "close", "escape"):
            with self.subTest(how=how):
                window, forge = await self.ready()
                library = self.library(window)
                library.remove_watch_folder(READY.watch_folders[0])
                library._needs.set(False)
                if how == "cancel":
                    window.cancel_button.invoke()
                elif how == "close":
                    window.tk.eval(str(window.protocol("WM_DELETE_WINDOW")))  # ✕
                else:
                    press(window, "<Escape>")
                await pump(self.root, 0.05)
                self.assertFalse(window.winfo_exists())
                self.assertEqual(self.stored(forge).updates, [])
                self.assertEqual(self.stored(forge).listeners, [])  # nothing listens on


class HousekeepingTests(SettingsWindowCase):
    async def test_a_closed_window_is_freed(self) -> None:
        window, _forge = await self.ready()
        ref = weakref.ref(window)
        window.cancel()
        del window
        await pump(self.root, 0.05)
        gc.collect()
        self.assertIsNone(ref())
        tracked = ctk.ScalingTracker.window_widgets_dict
        self.assertEqual([w for w in tracked if isinstance(w, ForgeSettingsWindow)], [])

    async def test_the_light_dark_switch_repaints_the_line(self) -> None:
        window, _forge = self.make()
        line = next(w for w in descendants(window) if type(w) is tk.Frame)  # the 1-pixel line
        for mode in ("light", "dark", "light"):
            ctk.set_appearance_mode(mode)
            await pump(self.root, 0.05)
            with self.subTest(mode=mode):
                self.assertEqual(
                    str(line.cget("background")).upper(),
                    theme.pick(forge_look().PLACEHOLDER).upper(),
                )


class AirGrabbersSettingsTests(SettingsWindowCase):
    async def test_airgrabbers_settings_no_longer_list_mediaforge(self) -> None:
        self.assertNotIn("MediaForge", [group for group, _names in SECTION_GROUPS])
        for name in FORGE_SECTIONS:
            self.assertNotIn(name, SECTIONS)
        dialog = SettingsDialog(
            self.root,
            SettingsService(FakeRepository(), Settings(download_dir=self.tmp)),  # type: ignore[arg-type]
            search=mock.MagicMock(),
            station=mock.MagicMock(),
            updates=SimpleNamespace(current="3.0.0"),  # type: ignore[arg-type]
            torrent_app=mock.MagicMock(find=mock.AsyncMock(return_value=None)),
            discovery=mock.MagicMock(check_tmdb_key=mock.AsyncMock(return_value=True)),
            prowlarr=FakeProwlarr(),  # type: ignore[arg-type]
            startup_shortcut=FakeShortcut(),
        )
        sidebar = labels(dialog._sidebar)
        self.assertIn("DOWNLOADING", sidebar)
        self.assertNotIn("MEDIAFORGE", sidebar)
        for name in FORGE_SECTIONS:
            self.assertNotIn(name, sidebar)
        dialog.destroy()
