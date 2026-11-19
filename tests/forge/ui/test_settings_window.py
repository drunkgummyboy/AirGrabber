"""The one Settings window of AirGrabber and MediaForge (app/ui/settings_dialog.py;
docs/settings/2026-11-19-settings-redesign.md): MediaForge's parts on their pages, each built by its
own builder at its page's first visit (Forge not started before then); one look, AirGrabber's; each
change of every part stored when it is made, through the window's one Committer (a refusal in one
part stores nothing of it and stops nothing else; one Undo bar); ✕ / Escape close and leave no
listeners; a change of Forge's reloads no pages; freed when closed; Dutch.
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
from app.forge.ui.settings_menu import MenuSection, carry_presets
from app.forge.ui.settings_naming import NamingSection, PresetsChange
from app.forge.ui.settings_pages import FORGE_PARTS, LibrarySection
from app.forge.ui.settings_subtitles import SubtitlesSection
from app.i18n import Language
from app.models import Settings
from app.ui import main_window as main_window_module
from app.ui import theme
from app.ui.main_window import MainWindow, settings_affect_pages
from app.ui.settings_dialog import SettingsDialog
from app.ui.settings_pages_registry import (
    PAGE_ACCOUNTS,
    PAGE_FOLDERS,
    PAGE_LOOKS,
    PAGE_NAMING,
    PAGE_ORDER,
    PAGE_OVERVIEW,
    PAGE_PAGES,
    PAGE_SORTING,
    PAGE_SUBTITLES,
    PAGE_WINDOWS,
    PAGES,
)

# The pages a MediaForge part is built for (its own; Folders' and Accounts & keys' come with them).
FORGE_OWN = [page for page in PAGE_ORDER if page in FORGE_PARTS and FORGE_PARTS[page].owner is None]
from app.ui.widgets import install_window_release
from tests.forge.base import ForgeAsyncTestCase
from tests.forge.tk import press, pump, pump_until
from tests.forge.ui.settings_kit import airgrabber_settings, forge_settings
from tests.forge.ui.test_settings_library import READY, FakeForge, FakeSettings, labels
from tests.test_settings_dialog import FakeProwlarr, find, settle


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
        self, settings: ForgeSettings = READY, *, section: str | None = PAGE_SORTING
    ) -> tuple[SettingsDialog, FakeForge]:
        forge = FakeForge(FakeSettings(settings))
        window = forge_settings(self.main_window(), forge, section=section)
        window.attributes("-alpha", 0.0)
        return window, forge

    async def ready(
        self, settings: ForgeSettings = READY, *, section: str | None = PAGE_SORTING
    ) -> tuple[SettingsDialog, FakeForge]:
        """The window with all of MediaForge's pages visited, on `section`'s."""
        window, forge = self.make(settings, section=section)
        for name in FORGE_PARTS:
            window.show_section(name)
        naming = window.parts[PAGE_NAMING]
        assert isinstance(naming, NamingSection)
        await pump_until(self.root, lambda: naming.ready)
        if section is not None:
            window.show_section(section)
        return window, forge

    @staticmethod
    def library(window: SettingsDialog) -> LibrarySection:
        part = window.parts[PAGE_SORTING]
        assert isinstance(part, LibrarySection)
        return part

    @staticmethod
    def stored(forge: FakeForge) -> FakeSettings:
        assert forge.services is not None
        return cast(FakeSettings, forge.services.settings)


class SectionsTests(SettingsWindowCase):
    async def test_mediaforges_pages_in_the_sidebar(self) -> None:
        window, _forge = self.make(section=None)
        self.assertEqual(window.sections, [page for page in PAGE_ORDER if page in window.sections])
        for page in FORGE_PARTS:
            self.assertIn(page, window.sections)
        shown = labels(window._sidebar)
        groups = [text for text in shown if text.isupper() and len(text) > 2]
        self.assertEqual(groups, ["DOWNLOADING", "YOUR FILES", "APP"])  # no MediaForge group
        # Your files' label, then an icon and a name per page, in the registry's order.
        at = shown.index("YOUR FILES") + 1
        for page in (PAGE_FOLDERS, PAGE_SORTING, PAGE_NAMING, PAGE_SUBTITLES):
            self.assertEqual(shown[at : at + 2], [PAGES[page].icon, page])
            at += 2
        self.assertEqual(shown[at], "APP")

    async def test_the_parts_are_built_by_their_builders(self) -> None:
        window, _forge = await self.ready()
        self.assertEqual(set(window.parts), set(FORGE_PARTS))
        self.assertIsInstance(window.parts[PAGE_SORTING], LibrarySection)
        self.assertNotIn(PAGE_FOLDERS, window.parts)  # AirGrabber's page (app/ui/folders_page.py)
        self.assertIsInstance(window.parts[PAGE_NAMING], NamingSection)
        self.assertIsInstance(window.parts[PAGE_SUBTITLES], SubtitlesSection)
        self.assertIs(window.parts[PAGE_ACCOUNTS], window.parts[PAGE_SUBTITLES])
        self.assertIsInstance(window.parts[PAGE_WINDOWS], MenuSection)
        for name in FORGE_PARTS:
            with self.subTest(page=name):
                window.show_section(name)
                texts = labels(window.page(name))
                self.assertEqual(texts[:2], [name, i18n.t(PAGES[name].note)])
                self.assertGreater(len(texts), 2)  # the part's own boxes

    async def test_a_page_is_built_on_its_first_visit_and_forge_starts_then(self) -> None:
        forge = FakeForge(FakeSettings(READY), started=False)
        with mock.patch.dict(
            FORGE_PARTS,
            {
                name: part._replace(build=mock.Mock(wraps=part.build))
                for name, part in FORGE_PARTS.items()
                if part.build is not None
            },
        ):
            window = forge_settings(self.main_window(), forge)
            window.attributes("-alpha", 0.0)
            self.assertEqual(window.section, PAGE_OVERVIEW)
            await pump(self.root, 0.05)
            self.assertEqual(window.parts, {})
            self.assertEqual(forge.starts, 0)
            for name in FORGE_OWN:
                cast(mock.Mock, FORGE_PARTS[name].build).assert_not_called()
            window._items[PAGE_NAMING].click()  # the sidebar
            await pump(self.root, 0.05)
            self.assertEqual(list(window.parts), [PAGE_NAMING])
            self.assertEqual(forge.starts, 1)
            window.show_section(PAGE_LOOKS)  # AirGrabber's pages are as they were
            window.show_section(PAGE_NAMING)  # the second visit builds nothing
            self.assertEqual(window.section, PAGE_NAMING)
            cast(mock.Mock, FORGE_PARTS[PAGE_NAMING].build).assert_called_once()
            cast(mock.Mock, FORGE_PARTS[PAGE_SORTING].build).assert_not_called()
            window.show_section(PAGE_FOLDERS)  # AirGrabber's page: it builds no part
            cast(mock.Mock, FORGE_PARTS[PAGE_SORTING].build).assert_not_called()
            self.assertNotIn(PAGE_FOLDERS, window.parts)

    async def test_opens_at_the_asked_section_and_switches(self) -> None:
        window, _forge = self.make(section=PAGE_WINDOWS)
        self.assertEqual(window.section, PAGE_WINDOWS)
        self.assertTrue(window._items[PAGE_WINDOWS].selected)
        window.show_section(PAGE_NAMING)
        self.assertEqual(window.section, PAGE_NAMING)
        self.assertTrue(window._items[PAGE_NAMING].selected)
        self.assertFalse(window._items[PAGE_WINDOWS].selected)
        window.show_section("Nothing like it")  # not here: nothing changes
        self.assertEqual(window.section, PAGE_NAMING)
        window._items[PAGE_SORTING].click()  # the sidebar
        self.assertEqual(window.section, PAGE_SORTING)
        self.assertEqual(window.page(PAGE_NAMING).winfo_manager(), "")
        window._items[PAGE_OVERVIEW].click()
        self.assertEqual(window.page(PAGE_SORTING).winfo_manager(), "")

    async def test_the_window_belongs_to_the_window_it_opened_over(self) -> None:
        window, _forge = self.make()
        self.assertEqual(window.title(), "Settings")
        self.assertEqual(str(window.transient()), str(self.main_window()))
        self.assertIs(window.master, self.main_window())

    async def test_one_look(self) -> None:
        window, _forge = await self.ready()
        look = app_look()
        self.assertEqual(tuple(window.cget("fg_color")), theme.BG)
        bar = window.committer.bar  # the one Undo bar, for MediaForge's parts too
        assert bar is not None
        self.assertEqual(tuple(bar.cget("fg_color")), theme.SURFACE)
        self.assertEqual(tuple(bar.undo_link.cget("text_color")), theme.LINK)
        part = self.library(window)
        self.assertEqual(tuple(part.watching.cget("progress_color")), look.ACCENT)
        self.assertEqual(tuple(part.rule_rows[0].switch.cget("progress_color")), look.ACCENT)
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
        self.assertEqual(labels(window.page(PAGE_SORTING))[0], "Sorteren")
        sidebar = labels(window._sidebar)
        for group in ("DOWNLOADEN", "JE BESTANDEN", "APP"):
            self.assertIn(group, sidebar)
        self.assertNotIn("MEDIAFORGE", sidebar)


class CommitTests(SettingsWindowCase):
    async def test_a_refusal_in_one_part_stores_nothing_of_it_and_stops_nothing_else(
        self,
    ) -> None:
        settings = airgrabber_settings()
        forge = FakeForge(FakeSettings(READY))
        window = forge_settings(self.main_window(), forge, section=PAGE_WINDOWS, settings=settings)
        window.attributes("-alpha", 0.0)
        for name in FORGE_PARTS:
            window.show_section(name)
        naming = window.parts[PAGE_NAMING]
        assert isinstance(naming, NamingSection)
        await pump_until(self.root, lambda: naming.ready)
        library = self.library(window)
        naming._name.set("  ")  # a preset without a name
        naming.name_field.commit_now()
        find(window.page(PAGE_SORTING), library._needs).toggle()  # a fine change elsewhere
        spotlight = not settings.current.spotlight_motion
        window._items[PAGE_LOOKS].click()
        find(window.page(PAGE_PAGES), window._spotlight_motion).toggle()  # and in AirGrabber's
        await settle()
        self.assertEqual(naming._presets_box.refusal, "Give every preset a name.")
        self.assertEqual(self.stored(forge).updates, [{"notify_needs": False}])
        self.assertEqual(settings.current.spotlight_motion, spotlight)
        self.assertTrue(window.winfo_exists())

    async def test_only_built_parts_take_part(self) -> None:
        window, forge = self.make(section=PAGE_OVERVIEW)
        self.assertEqual(window.parts, {})
        await settle()
        self.assertEqual(forge.starts, 0)
        self.assertEqual(self.stored(forge).updates, [])
        window.show_section(PAGE_SORTING)
        self.assertEqual(list(window.parts), [PAGE_SORTING])
        find(window.page(PAGE_SORTING), self.library(window)._needs).toggle()
        await settle()
        self.assertEqual(self.stored(forge).updates, [{"notify_needs": False}])

    async def test_each_change_stores_in_the_order_made_and_the_window_stays(self) -> None:
        settings = airgrabber_settings()
        forge = FakeForge(FakeSettings(READY))
        window = forge_settings(self.main_window(), forge, section=PAGE_NAMING, settings=settings)
        window.attributes("-alpha", 0.0)
        window.show_section(PAGE_SORTING)
        order: list[str] = []
        settings.subscribe(lambda _settings: order.append("airgrabber"))
        self.stored(forge).subscribe(lambda _settings: order.append("forge"))
        library = self.library(window)
        await library.set_watching(True)
        find(window.page(PAGE_PAGES), window._spotlight_motion).toggle()
        await settle()
        find(window.page(PAGE_SORTING), library._needs).toggle()
        await settle()
        self.assertEqual(order, ["forge", "airgrabber", "forge"])
        self.assertEqual(
            self.stored(forge).updates, [{"automatic": "on"}, {"notify_needs": False}]
        )
        self.assertTrue(window.winfo_exists())

    async def test_a_failed_store_says_why_in_its_box_and_the_window_stays(self) -> None:
        window, forge = await self.ready()
        library = self.library(window)
        self.stored(forge).fail = True
        with self.assertLogs("app.forge.ui.settings_pages", "ERROR"):
            find(window.page(PAGE_SORTING), library._summary).toggle()
            await settle()
        self.assertTrue(window.winfo_exists())
        self.assertIn("couldn't be saved", library._options_box.refusal)
        self.assertEqual(window.section, PAGE_SORTING)

    async def test_a_change_of_forges_reloads_no_pages(self) -> None:
        settings = airgrabber_settings()
        before = settings.current
        heard: list[Settings] = []
        settings.subscribe(heard.append)  # what MainWindow's reload listens to
        forge = FakeForge(FakeSettings(READY))
        window = forge_settings(
            self.main_window(), forge, section=PAGE_SORTING, settings=settings
        )
        window.attributes("-alpha", 0.0)
        find(window.page(PAGE_SORTING), self.library(window)._needs).toggle()
        await settle()
        self.assertEqual(self.stored(forge).updates, [{"notify_needs": False}])
        self.assertEqual(heard, [])
        self.assertFalse(settings_affect_pages(before, settings.current))

    async def test_close_and_escape_keep_what_was_stored_and_leave_no_listeners(self) -> None:
        for how in ("close", "escape"):
            with self.subTest(how=how):
                window, forge = await self.ready()
                library = self.library(window)
                await library.toggle_rule(0, False)
                if how == "close":
                    window.tk.eval(str(window.protocol("WM_DELETE_WINDOW")))  # ✕
                else:
                    press(window, "<Escape>")
                await pump(self.root, 0.05)
                self.assertFalse(window.winfo_exists())
                self.assertEqual(len(self.stored(forge).listeners), 0)  # nothing listens on
                assert forge.services is not None
                self.assertFalse(forge.services.rules.stored[0].on)  # stored before closing


class PresetsWithoutTheMenuPageTests(SettingsWindowCase):
    """Naming's Save while the Right-click menu's page was never visited: what that page would do
    for it (`MenuSection._presets_saved`) still happens."""

    async def test_the_chosen_presets_follow_and_the_menu_is_refreshed(self) -> None:
        forge = FakeForge(FakeSettings(READY.model_copy(update={"context_menu_presets": ["Auto"]})))
        assert forge.services is not None
        forge.services.presets = SimpleNamespace(
            list=mock.AsyncMock(return_value=list(DEFAULT_PRESETS)), replace_all=mock.AsyncMock()
        )
        window = forge_settings(self.main_window(), forge, section=PAGE_NAMING)
        window.attributes("-alpha", 0.0)
        naming = window.parts[PAGE_NAMING]
        assert isinstance(naming, NamingSection)
        await pump_until(self.root, lambda: naming.ready)
        self.assertNotIn(PAGE_WINDOWS, window.parts)
        naming.select_preset(naming._drafts[0])  # "Auto"
        naming._name.set("Everything")
        with mock.patch.object(
            context_menu, "refresh_from", mock.AsyncMock(return_value=True)
        ) as refresh:
            naming.name_field.commit_now()  # Enter, or leaving the field
            await settle()
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
        window.close()
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
            folder_library=None,
            problem_reports=None,
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
        MainWindow._open_settings(window, PAGE_SORTING)
        dialog = window._settings_dialog
        self.assertEqual(dialog.section, PAGE_SORTING)
        self.assertEqual(str(dialog.transient()), str(self.main_window()))
        await pump_until(self.root, lambda: dialog.grab_current() is not None)  # `activate_later`
        self.assertEqual(str(dialog.grab_current()), str(dialog))

    async def test_airgrabbers_own_ways_open_on_overview(self) -> None:
        window = self.stand_in(FakeForge(FakeSettings(READY)))
        MainWindow._open_settings(window)
        self.assertEqual(window._settings_dialog.section, PAGE_OVERVIEW)

    async def test_an_open_one_is_raised_and_switched(self) -> None:
        window = self.stand_in(FakeForge(FakeSettings(READY)))
        MainWindow._open_settings(window, PAGE_WINDOWS)
        dialog = window._settings_dialog
        MainWindow._open_settings(window)  # ⚙ again: the page it is on
        self.assertEqual(dialog.section, PAGE_WINDOWS)
        MainWindow._open_settings(window, PAGE_SORTING)
        self.assertEqual(dialog.section, PAGE_SORTING)
        MainWindow._open_settings(window, PAGE_LOOKS)
        self.assertEqual(dialog.section, PAGE_LOOKS)
        self.assertIs(window._settings_dialog, dialog)
        self.assertEqual(self.made, [dialog])

    async def test_the_main_windows_close_closes_settings_over_it_first(self) -> None:
        calls: list[str] = []
        window: Any = SimpleNamespace(
            tray=SimpleNamespace(close_window=lambda: calls.append("tray"))
        )
        window._settings_dialog = SimpleNamespace(close=lambda: calls.append("close"))
        MainWindow._close_window(window)
        self.assertEqual(calls, ["close", "tray"])  # hidden with it, it would keep the grab

    async def test_an_update_or_going_back_closes_settings_as_its_cross_does(self) -> None:
        # Text typed but not stored yet is stored first (`close` flushes), then the restart.
        for start in (MainWindow._start_update, MainWindow._start_going_back):
            calls: list[str] = []
            window: Any = SimpleNamespace(
                _update_task=SimpleNamespace(done=lambda: False),  # one already running: stop
                _settings_dialog=SimpleNamespace(
                    close=lambda: calls.append("close"), winfo_exists=lambda: True
                ),
            )
            start(window)  # an update under way: nothing happens
            self.assertEqual(calls, [])
            window._update_task = None
            window._close_settings = lambda w=window: MainWindow._close_settings(w)
            window._run_update = window._run_going_back = lambda: asyncio.sleep(0)
            start(window)
            self.assertIsNone(window._settings_dialog)
            await window._update_task
            self.assertEqual(calls, ["close"])
        self.assertIsNone(window._settings_dialog)
