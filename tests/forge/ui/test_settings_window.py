"""The one Settings window of AirGrabber and MediaForge (app/ui/settings_dialog.py;
INTEGRATION.md 3.7, the owner's decision of 2026-11-04): the sidebar's groups in order, MediaForge's
sections with their icons, each built by its own builder on its first visit (Forge not started
before then); one look, AirGrabber's; one Save (every built section's problem first, nothing stored
on one; then AirGrabber's settings and each section, and the window closes); Cancel / ✕ / Escape
store nothing and leave no listeners; a save of Forge's reloads no pages; freed when closed; Dutch.
And every way in: MainWindow._open_settings (on a stand-in, with the real dialog) over the Forge
window with the main window left in the tray, an open one raised and switched, closed with the
window it belongs to.

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
from app.forge import context_menu
from app.forge.models.preset import DEFAULT_PRESETS
from app.forge.models.settings import ForgeSettings
from app.forge.ui.look import app_look
from app.forge.ui.settings_menu import SECTION_MENU, MenuSection, carry_presets
from app.forge.ui.settings_naming import SECTION_NAMING, NamingSection, PresetsChange
from app.forge.ui.settings_pages import (
    FORGE_GROUP,
    FORGE_SECTIONS,
    SECTION_LIBRARY,
    LibrarySection,
)
from app.i18n import Language
from app.models import Settings
from app.ui import main_window as main_window_module
from app.ui import theme
from app.ui.main_window import MainWindow, settings_affect_pages
from app.ui.settings_dialog import SECTION_GROUPS, SECTION_LOOKS, SECTION_WHERE, SettingsDialog
from app.ui.widgets import install_window_release
from tests.forge.base import ForgeAsyncTestCase
from tests.forge.tk import press, pump, pump_until
from tests.forge.ui.settings_kit import airgrabber_settings, forge_settings
from tests.forge.ui.test_settings_library import READY, FakeForge, FakeSettings, labels
from tests.test_settings_dialog import FakeProwlarr


def descendants(widget: tk.Misc) -> list[tk.Misc]:
    found: list[tk.Misc] = []
    for child in widget.winfo_children():
        found += [child, *descendants(child)]
    return found


def colours(widget: tk.Misc) -> set[str]:
    """The colours a CTk widget was given (its `_…color…` attributes)."""
    found: set[str] = set()
    for name, value in vars(widget).items():
        if "color" not in name:
            continue
        if isinstance(value, str):
            found.add(value.upper())
        elif isinstance(value, (tuple, list)):
            found.update(part.upper() for part in value if isinstance(part, str))
    return found


class SettingsWindowCase(ForgeAsyncTestCase):
    def setUp(self) -> None:
        super().setUp()
        gc.collect()
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()  # the main window, in the tray
        install_window_release(self.root)  # as MainWindow: a closed window is forgotten
        self.addCleanup(self._destroy_root)
        self.addCleanup(i18n.set_language, i18n.current_language())
        i18n.set_language(Language.EN)
        self.addCleanup(ctk.set_appearance_mode, ctk.get_appearance_mode())

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0  # building a window is slow

    def _destroy_root(self) -> None:
        for child in list(self.root.winfo_children()):
            child.destroy()
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()  # the window's Tk variables, on the main thread

    def main_window(self) -> ctk.CTkToplevel:
        """A stand-in for the main window Settings opens over: mapped, but see-through."""
        if not hasattr(self, "_main_window"):
            self._main_window = ctk.CTkToplevel(self.root)
            self._main_window.attributes("-alpha", 0.0)
        return self._main_window

    async def settled_main_window(self) -> ctk.CTkToplevel:
        """The stand-in as a window with a ⚙ to click is: on screen for a while, CTk's
        hiding and showing of a new window over (what is transient to a window hidden just then
        can stay hidden)."""
        window = self.main_window()
        await pump_until(self.root, lambda: window.state() == "normal")
        await pump(self.root, 0.3)
        return window

    def make(
        self, settings: ForgeSettings = READY, *, section: str | None = SECTION_LIBRARY
    ) -> tuple[SettingsDialog, FakeForge]:
        forge = FakeForge(FakeSettings(settings))
        window = forge_settings(self.main_window(), forge, section=section)
        window.attributes("-alpha", 0.0)
        return window, forge

    async def ready(
        self, settings: ForgeSettings = READY, *, section: str | None = SECTION_LIBRARY
    ) -> tuple[SettingsDialog, FakeForge]:
        """The window with all of MediaForge's pages visited, on `section`'s."""
        window, forge = self.make(settings, section=section)
        for name in FORGE_SECTIONS:
            window.show_section(name)
        naming = window.parts[SECTION_NAMING]
        assert isinstance(naming, NamingSection)
        await pump_until(self.root, lambda: naming.ready)
        if section is not None:
            window.show_section(section)
        return window, forge

    @staticmethod
    def library(window: SettingsDialog) -> LibrarySection:
        part = window.parts[SECTION_LIBRARY]
        assert isinstance(part, LibrarySection)
        return part

    @staticmethod
    def stored(forge: FakeForge) -> FakeSettings:
        assert forge.services is not None
        return cast(FakeSettings, forge.services.settings)


class SectionsTests(SettingsWindowCase):
    async def test_the_sidebars_groups_in_order(self) -> None:
        window, _forge = self.make(section=None)
        downloading, app = SECTION_GROUPS
        self.assertEqual(FORGE_GROUP, "MediaForge")
        self.assertEqual(
            window.sections,
            [
                *[n for n in downloading[1] if n in window.sections],
                *FORGE_SECTIONS,
                *[n for n in app[1] if n in window.sections],
            ],
        )
        shown = labels(window._sidebar)
        groups = [text for text in shown if text in ("DOWNLOADING", "MEDIAFORGE", "APP")]
        self.assertEqual(groups, ["DOWNLOADING", "MEDIAFORGE", "APP"])
        # The group's label, then an icon and a name per section, in FORGE_SECTIONS' order.
        at = shown.index("MEDIAFORGE") + 1
        for name, section in FORGE_SECTIONS.items():
            self.assertEqual(shown[at : at + 2], [section.icon, name])
            at += 2
        self.assertEqual(shown[at], "APP")

    async def test_the_sections_are_built_by_their_builders(self) -> None:
        window, _forge = await self.ready()
        self.assertEqual(list(window.parts), list(FORGE_SECTIONS))
        self.assertIsInstance(window.parts[SECTION_LIBRARY], LibrarySection)
        self.assertIsInstance(window.parts[SECTION_NAMING], NamingSection)
        self.assertIsInstance(window.parts[SECTION_MENU], MenuSection)
        for name, section in FORGE_SECTIONS.items():
            with self.subTest(section=name):
                window.show_section(name)
                texts = labels(window.page(name))
                self.assertEqual(texts[:2], [name, i18n.t(section.note)])
                self.assertGreater(len(texts), 2)  # the section's own boxes

    async def test_a_page_is_built_on_its_first_visit_and_forge_starts_then(self) -> None:
        forge = FakeForge(FakeSettings(READY), started=False)
        with mock.patch.dict(
            FORGE_SECTIONS,
            {
                name: section._replace(build=mock.Mock(wraps=section.build))
                for name, section in FORGE_SECTIONS.items()
            },
        ):
            window = forge_settings(self.main_window(), forge)
            window.attributes("-alpha", 0.0)
            self.assertEqual(window.section, SECTION_WHERE)
            await pump(self.root, 0.05)
            self.assertEqual(window.parts, {})
            self.assertEqual(forge.starts, 0)
            for section in FORGE_SECTIONS.values():
                cast(mock.Mock, section.build).assert_not_called()
            window._items[SECTION_NAMING].click()  # the sidebar
            await pump(self.root, 0.05)
            self.assertEqual(list(window.parts), [SECTION_NAMING])
            self.assertEqual(forge.starts, 1)
            window.show_section(SECTION_LOOKS)  # AirGrabber's pages are as they were
            window.show_section(SECTION_NAMING)  # the second visit builds nothing
            self.assertEqual(window.section, SECTION_NAMING)
            cast(mock.Mock, FORGE_SECTIONS[SECTION_NAMING].build).assert_called_once()
            cast(mock.Mock, FORGE_SECTIONS[SECTION_LIBRARY].build).assert_not_called()

    async def test_opens_at_the_asked_section_and_switches(self) -> None:
        window, _forge = self.make(section=SECTION_MENU)
        self.assertEqual(window.section, SECTION_MENU)
        self.assertTrue(window._items[SECTION_MENU].selected)
        window.show_section(SECTION_NAMING)
        self.assertEqual(window.section, SECTION_NAMING)
        self.assertTrue(window._items[SECTION_NAMING].selected)
        self.assertFalse(window._items[SECTION_MENU].selected)
        window.show_section("Nothing like it")  # not here: nothing changes
        self.assertEqual(window.section, SECTION_NAMING)
        window._items[SECTION_LIBRARY].click()  # the sidebar
        self.assertEqual(window.section, SECTION_LIBRARY)
        self.assertEqual(window.page(SECTION_NAMING).winfo_manager(), "")
        window._items[SECTION_WHERE].click()
        self.assertEqual(window.page(SECTION_LIBRARY).winfo_manager(), "")

    async def test_the_window_belongs_to_the_window_it_opened_over(self) -> None:
        window, _forge = self.make()
        self.assertEqual(window.title(), "Settings")
        self.assertEqual(str(window.transient()), str(self.main_window()))
        self.assertIs(window.master, self.main_window())

    async def test_one_look(self) -> None:
        window, _forge = await self.ready()
        look = app_look()
        self.assertEqual(tuple(window.cget("fg_color")), theme.BG)
        self.assertEqual(tuple(window._save_button.cget("fg_color")), theme.ACCENT)
        part = self.library(window)
        self.assertEqual(tuple(part.use_box.cget("fg_color")), look.ACCENT)
        self.assertEqual(tuple(part.mode.cget("selected_color")), look.SELECTED)
        self.assertEqual(tuple(window._items[window.section].cget("fg_color")), theme.ROW_SELECTED)

    async def test_on_a_short_screen_the_sidebar_scrolls_and_the_pages_keep_their_width(
        self,
    ) -> None:
        await self.settled_main_window()
        window, _forge = self.make(section=None)
        await pump_until(self.root, lambda: window.state() == "normal")
        await pump(self.root, 0.05)
        sidebar = window._sidebar_scroll
        self.assertFalse(sidebar.scrolls)  # it all fits: a plain frame
        page_width = window._scroll.winfo_width()
        scaling = ctk.ScalingTracker.get_window_scaling(window)
        width = round(window.winfo_width() / scaling)
        window.geometry(f"{width}x500")  # too short for all of them
        await pump_until(self.root, lambda: sidebar.scrolls)
        await pump(self.root, 0.05)
        self.assertAlmostEqual(window._scroll.winfo_width(), page_width, delta=2)  # rounding

    async def test_gone_are_the_shared_line_and_the_link(self) -> None:
        window, _forge = self.make()
        texts = labels(window)
        self.assertNotIn("Language, theme, TMDB key and updates are shared with AirGrabber.", texts)
        buttons = [w for w in descendants(window) if isinstance(w, ctk.CTkButton)]
        self.assertNotIn("Open AirGrabber's Settings", [b.cget("text") for b in buttons])

    async def test_dutch(self) -> None:
        i18n.set_language(Language.NL)
        window, _forge = self.make()
        self.assertEqual(window.title(), "Instellingen")
        self.assertEqual(labels(window.page(SECTION_LIBRARY))[0], "Automatisch hernoemen")
        self.assertIn("MEDIAFORGE", labels(window._sidebar))


class SaveTests(SettingsWindowCase):
    async def test_a_problem_in_one_section_stores_nothing_anywhere(self) -> None:
        settings = airgrabber_settings()
        heard: list[Settings] = []
        settings.subscribe(heard.append)
        forge = FakeForge(FakeSettings(READY))
        window = forge_settings(self.main_window(), forge, section=SECTION_MENU, settings=settings)
        window.attributes("-alpha", 0.0)
        for name in FORGE_SECTIONS:
            window.show_section(name)
        naming = window.parts[SECTION_NAMING]
        assert isinstance(naming, NamingSection)
        await pump_until(self.root, lambda: naming.ready)
        window._items[SECTION_LOOKS].click()
        self.library(window)._needs.set(False)  # a fine change in the first section
        window._spotlight_motion.set(not settings.current.spotlight_motion)  # and in AirGrabber's
        naming._name.set("  ")  # a preset without a name in the second
        self.assertFalse(await window._save_all())
        self.assertEqual(self.stored(forge).updates, [])  # Library's change wasn't stored either
        self.assertEqual(heard, [])  # nor AirGrabber's
        self.assertEqual(window.section, SECTION_NAMING)  # the page with the problem shows
        self.assertEqual(str(window._error.cget("text")), "Give every preset a name.")
        self.assertEqual(str(window._save_button.cget("state")), "normal")
        self.assertTrue(window.winfo_exists())

    async def test_only_built_sections_take_part(self) -> None:
        window, forge = self.make(section=SECTION_WHERE)
        self.assertEqual(window.parts, {})
        self.assertTrue(await window._save_all())
        self.assertEqual(forge.starts, 0)
        self.assertEqual(self.stored(forge).updates, [])
        window.show_section(SECTION_LIBRARY)
        self.assertEqual(list(window.parts), [SECTION_LIBRARY])
        self.library(window)._needs.set(False)
        self.assertTrue(await window._save_all())
        self.assertEqual(self.stored(forge).updates, [{"notify_needs": False}])

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

    async def test_airgrabbers_settings_first_then_each_section_in_order(self) -> None:
        settings = airgrabber_settings()
        forge = FakeForge(FakeSettings(READY))
        window = forge_settings(
            self.main_window(), forge, section=SECTION_NAMING, settings=settings
        )
        window.attributes("-alpha", 0.0)
        window.show_section(SECTION_LIBRARY)  # visited after Naming: stored in the sidebar's order
        order: list[str] = []
        spotlight = not settings.current.spotlight_motion
        window._spotlight_motion.set(spotlight)
        settings.subscribe(lambda _settings: order.append("airgrabber"))

        def part(name: str) -> Any:
            async def save() -> str | None:
                order.append(name)
                return None

            return SimpleNamespace(problem=lambda: None, save=save)

        window.parts = {name: part(name) for name in window.parts}
        self.assertTrue(await window._save_all())
        self.assertEqual(order, ["airgrabber", SECTION_LIBRARY, SECTION_NAMING])
        self.assertEqual(settings.current.spotlight_motion, spotlight)

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

    async def test_a_save_of_forges_reloads_no_pages(self) -> None:
        settings = airgrabber_settings()
        before = settings.current
        heard: list[Settings] = []
        settings.subscribe(heard.append)  # what MainWindow's reload listens to
        forge = FakeForge(FakeSettings(READY))
        window = forge_settings(
            self.main_window(), forge, section=SECTION_LIBRARY, settings=settings
        )
        window.attributes("-alpha", 0.0)
        self.library(window)._needs.set(False)
        self.assertTrue(await window._save_all())
        self.assertEqual(self.stored(forge).updates, [{"notify_needs": False}])
        self.assertEqual(heard, [])
        self.assertFalse(settings_affect_pages(before, settings.current))

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


class PresetsWithoutTheMenuPageTests(SettingsWindowCase):
    """Naming's Save while the Right-click menu's page was never visited: what that page would do
    for it (`MenuSection._presets_saved`) still happens."""

    async def test_the_chosen_presets_follow_and_the_menu_is_refreshed(self) -> None:
        forge = FakeForge(FakeSettings(READY.model_copy(update={"context_menu_presets": ["Auto"]})))
        assert forge.services is not None
        forge.services.presets = SimpleNamespace(
            list=mock.AsyncMock(return_value=list(DEFAULT_PRESETS)), replace_all=mock.AsyncMock()
        )
        window = forge_settings(self.main_window(), forge, section=SECTION_NAMING)
        window.attributes("-alpha", 0.0)
        naming = window.parts[SECTION_NAMING]
        assert isinstance(naming, NamingSection)
        await pump_until(self.root, lambda: naming.ready)
        self.assertNotIn(SECTION_MENU, window.parts)
        naming.select_preset(naming._drafts[0])  # "Auto"
        naming._name.set("Everything")
        with mock.patch.object(
            context_menu, "refresh_from", mock.AsyncMock(return_value=True)
        ) as refresh:
            self.assertTrue(await window._save_all())
        refresh.assert_awaited_once()
        self.assertEqual(
            self.stored(forge).current.context_menu_presets, ["Everything"]
        )

    async def test_carry_presets_drops_a_deleted_one(self) -> None:
        settings = FakeSettings(
            READY.model_copy(update={"context_menu_presets": ["Auto", "Old"]})
        )
        parts: Any = SimpleNamespace(settings=settings)
        with mock.patch.object(context_menu, "refresh_from", mock.AsyncMock()) as refresh:
            await carry_presets(
                parts, PresetsChange(names=("Auto",), renamed={}, removed=("Old",))
            )
        refresh.assert_awaited_once_with(parts)
        self.assertEqual(settings.current.context_menu_presets, ["Auto"])


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
        self.assertEqual([w for w in tracked if isinstance(w, SettingsDialog)], [])

    async def test_the_light_dark_switch_repaints_the_line(self) -> None:
        window, _forge = self.make()
        line = next(w for w in descendants(window) if type(w) is tk.Frame)  # the 1-pixel line
        for mode in ("light", "dark", "light"):
            ctk.set_appearance_mode(mode)
            await pump(self.root, 0.05)
            with self.subTest(mode=mode):
                self.assertEqual(
                    str(line.cget("background")).upper(),
                    theme.pick(theme.PANEL_EDGE).upper(),
                )


class WaysInTests(SettingsWindowCase):
    """MainWindow._open_settings on a stand-in, with the real dialog over the real Tk windows."""

    def stand_in(self, forge: FakeForge) -> Any:
        services = SimpleNamespace(
            settings=airgrabber_settings(),
            search=mock.MagicMock(),
            station=mock.MagicMock(),
            updates=SimpleNamespace(current="3.0.0", previous_version=None),
            torrent_app=mock.MagicMock(find=mock.AsyncMock(return_value=None)),
            discovery=mock.MagicMock(check_tmdb_key=mock.AsyncMock(return_value=True)),
            prowlarr_setup=FakeProwlarr(),
            subtitles=None,
            images=None,
            library=None,
            backup=None,
            plex=None,
            phone=None,
            forge=forge,
        )
        window: Any = SimpleNamespace(
            _settings_dialog=None,
            _services=services,
            _update_info=None,
            _start_update=lambda: None,
            _start_going_back=lambda: None,
            restart=lambda: None,
        )
        self.made: list[SettingsDialog] = []
        real = SettingsDialog
        over = self.main_window()

        def make(master: Any, *args: Any, **kwargs: Any) -> SettingsDialog:
            self.assertIs(master, window)  # always over the main window
            dialog = real(over, *args, **kwargs)
            dialog.attributes("-alpha", 0.0)
            self.made.append(dialog)
            return dialog

        patch = mock.patch.object(main_window_module, "SettingsDialog", make)
        patch.start()
        self.addCleanup(patch.stop)
        return window

    async def test_the_status_lines_way_opens_at_its_section(self) -> None:
        window = self.stand_in(FakeForge(FakeSettings(READY)))
        await self.settled_main_window()
        MainWindow._open_settings(window, SECTION_LIBRARY)
        dialog = window._settings_dialog
        self.assertEqual(dialog.section, SECTION_LIBRARY)
        self.assertEqual(str(dialog.transient()), str(self.main_window()))
        await pump_until(self.root, lambda: dialog.grab_current() is not None)  # `activate_later`
        self.assertEqual(str(dialog.grab_current()), str(dialog))

    async def test_airgrabbers_own_ways_keep_their_usual_page(self) -> None:
        window = self.stand_in(FakeForge(FakeSettings(READY)))
        MainWindow._open_settings(window)
        self.assertEqual(window._settings_dialog.section, SECTION_WHERE)

    async def test_an_open_one_is_raised_and_switched(self) -> None:
        window = self.stand_in(FakeForge(FakeSettings(READY)))
        MainWindow._open_settings(window, SECTION_MENU)
        dialog = window._settings_dialog
        MainWindow._open_settings(window)  # ⚙ again: the page it is on
        self.assertEqual(dialog.section, SECTION_MENU)
        MainWindow._open_settings(window, SECTION_LIBRARY)
        self.assertEqual(dialog.section, SECTION_LIBRARY)
        MainWindow._open_settings(window, SECTION_LOOKS)
        self.assertEqual(dialog.section, SECTION_LOOKS)
        self.assertIs(window._settings_dialog, dialog)
        self.assertEqual(self.made, [dialog])

    async def test_the_main_windows_close_cancels_settings_over_it_first(self) -> None:
        calls: list[str] = []
        window: Any = SimpleNamespace(
            tray=SimpleNamespace(close_window=lambda: calls.append("tray"))
        )
        window._settings_dialog = SimpleNamespace(cancel=lambda: calls.append("cancel"))
        MainWindow._close_window(window)
        self.assertEqual(calls, ["cancel", "tray"])  # hidden with it, it would keep the grab
        self.assertIsNone(window._settings_dialog)
