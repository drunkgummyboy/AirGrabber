"""Settings' pages (app/ui/settings_pages_registry.py; docs/settings/2026-11-19-settings-redesign.md
§2): Overview, then the four groups and their pages in order, a page left out without its service;
every box of the window before the regroup on its new page (found there once the page shows,
MediaForge's parts built then); the window opening on Overview; and MediaForge's openers (the
status line, "choose folders", a rule's label) reaching Sorting and Folders.

The window runs on fakes (tests/test_settings_dialog.py's, tests/forge/ui/'s for MediaForge,
tests/test_plex_ui.py's and tests/test_folder_library_settings.py's); nothing real is asked."""

from __future__ import annotations

import tkinter as tk
from types import SimpleNamespace
from typing import Any
from unittest import mock

import customtkinter as ctk

from app import i18n
from app.forge.ui.settings_menu import MenuSection
from app.forge.ui.settings_naming import NamingSection
from app.forge.ui.settings_pages import RULE_SPOT, LibrarySection
from app.forge.ui.settings_subtitles import CREDIT_TEXT, SubtitlesSection
from app.i18n import Language
from app.models import Settings
from app.services import SettingsService
from app.ui import settings_dialog
from app.ui.settings_dialog import (
    APP_CHOICE,
    AUTO_UPDATE_SWITCH,
    FOLDER_CHOICE,
    QUALITY_NOTE,
    REMOTE_CHOICE,
    SOURCES_TEXT,
    SettingsDialog,
)
from app.ui.settings_pages_registry import (
    FOLDERS_SPOT,
    GROUPS,
    PAGE_ACCOUNTS,
    PAGE_AUTOMATIC,
    PAGE_FOLDERS,
    PAGE_LOOKS,
    PAGE_NAMING,
    PAGE_ORDER,
    PAGE_OVERVIEW,
    PAGE_PAGES,
    PAGE_PHONE,
    PAGE_PLAYING,
    PAGE_PLEX,
    PAGE_QUALITY,
    PAGE_SORTING,
    PAGE_SUBTITLES,
    PAGE_TORRENT,
    PAGE_UPDATES,
    PAGE_WINDOWS,
    PAGES,
)
from tests.forge.base import ForgeAsyncTestCase
from tests.forge.tk import pump_until
from tests.forge.ui.test_settings_library import READY, FakeForge, FakeSettings
from tests.test_backup_dialog import FakeBackup
from tests.test_folder_library_settings import FakeFolders
from tests.test_plex_ui import FakePlex
from tests.test_settings_dialog import FakeProwlarr, FakeRepository, FakeShortcut

PHONE_BOX = "The phone box (a stand-in)"

# Each box of the window before the regroup (its title, or a text in it for an untitled one) →
# the page it is on now and what it is found by there (a new title where boxes were merged).
OLD_BOXES: tuple[tuple[str, str, str], ...] = (
    # Where downloads go
    ("Open in my torrent program on this PC", PAGE_TORRENT, APP_CHOICE),
    ("Send to a torrent program on my NAS or another computer", PAGE_TORRENT, REMOTE_CHOICE),
    ("Save .torrent files to a folder", PAGE_TORRENT, FOLDER_CHOICE),
    ("Where finished downloads end up", PAGE_FOLDERS, "Finished downloads"),
    # Quality
    ("Series", PAGE_QUALITY, "Series"),
    ("Movies", PAGE_QUALITY, "Movies"),
    (QUALITY_NOTE, PAGE_QUALITY, QUALITY_NOTE),
    # Automatic
    ("In the background", PAGE_WINDOWS, "In the background"),
    ("New shows", PAGE_AUTOMATIC, "For new shows and films"),
    ("New films on your watchlist", PAGE_AUTOMATIC, "For new shows and films"),
    ("Wait after an episode airs", PAGE_AUTOMATIC, "Wait after an episode airs"),
    ("Swap a stuck download after", PAGE_AUTOMATIC, "Swap a stuck download after"),
    # Library
    ("Plex", PAGE_PLEX, "Plex"),
    ("Your own folders", PAGE_FOLDERS, "Count what's in them"),
    ("Play in", PAGE_PLAYING, "Play in"),
    (
        "Offer AirGrabber's player in Explorer's \"Open with\" for any video",
        PAGE_PLAYING,
        "Explorer",
    ),
    ("With and without a library", PAGE_PLEX, "What does a library add?"),
    # Phone connection
    (PHONE_BOX, PAGE_PHONE, PHONE_BOX),
    # MediaForge › Auto-rename
    ("Watching", PAGE_SORTING, "Watch folders"),
    ("Rules", PAGE_SORTING, "Rules"),
    ("Ignored", PAGE_SORTING, "Ignored"),
    ("AirGrabber's downloads", PAGE_SORTING, "AirGrabber's downloads"),
    ("Library folders", PAGE_FOLDERS, "Series"),  # one list per kind, the destination marked
    ("Options", PAGE_SORTING, "When sorting"),
    # MediaForge › Naming
    ("Presets", PAGE_NAMING, "Presets"),
    ("Library presets", PAGE_NAMING, "For new rules"),
    ("Names language", PAGE_NAMING, "Names language"),
    ("Remembered matches", PAGE_NAMING, "Remembered matches"),
    # MediaForge › Subtitles
    ("Languages", PAGE_SUBTITLES, "Languages"),
    ("Hearing impaired", PAGE_SUBTITLES, "Hearing impaired"),
    (CREDIT_TEXT, PAGE_SUBTITLES, CREDIT_TEXT),
    ("OpenSubtitles account", PAGE_ACCOUNTS, "OpenSubtitles"),
    ("Own API key", PAGE_ACCOUNTS, "Own API key"),
    # MediaForge › Right-click menu
    ("In the right-click menu", PAGE_WINDOWS, "Explorer's right-click menu"),
    ("Presets in the menu", PAGE_WINDOWS, "Presets in the menu"),
    ("Registry files", PAGE_WINDOWS, "Registry files"),
    # Movie & show info
    ("TMDB key", PAGE_ACCOUNTS, "TMDB"),
    ("Where this information comes from", PAGE_ACCOUNTS, SOURCES_TEXT),
    # Looks
    ("Quick setup", PAGE_OVERVIEW, "Quick setup"),
    ("Language", PAGE_PAGES, "Language"),
    ("Theme", PAGE_LOOKS, "Theme"),
    ("Accent colour", PAGE_LOOKS, "Accent colour"),
    ("Text size", PAGE_LOOKS, "Text size"),
    ("Card size", PAGE_LOOKS, "Card size"),
    ("Info window layout", PAGE_PAGES, "Info window"),
    ("Spotlight", PAGE_PAGES, "Spotlight"),
    ("Calendar view", PAGE_PAGES, "Calendar"),
    ("Week starts on", PAGE_PAGES, "Week starts on"),
    # Updates
    ("Automatic updates", PAGE_UPDATES, AUTO_UPDATE_SWITCH),
    ("Version 3.2.0", PAGE_UPDATES, "Version 3.2.0"),
    ("Not happy with this version?", PAGE_UPDATES, "Not happy with this version?"),
    ("Something not working?", PAGE_UPDATES, "Something not working?"),
    # Backup
    ("Backup", PAGE_UPDATES, "Backup"),
    # Advanced
    ("Built-in sites", PAGE_QUALITY, "Sites"),
    ("Use Prowlarr", PAGE_QUALITY, "Use Prowlarr"),
    ("Skip releases with these words", PAGE_QUALITY, "Skip releases with these words"),
    ("Prefer releases from", PAGE_QUALITY, "Prefer releases from"),
    ("Never use releases from", PAGE_QUALITY, "Never use releases from"),
)


def shown_texts(widget: tk.Misc) -> list[str]:
    """The texts on screen in `widget`: labels, ticks, switches, radio buttons."""
    found: list[str] = []
    for child in widget.winfo_children():
        if not child.winfo_manager():
            continue
        if isinstance(child, (ctk.CTkLabel, ctk.CTkCheckBox, ctk.CTkSwitch, ctk.CTkRadioButton)):
            found.append(str(child.cget("text")))
        found += shown_texts(child)
    return found


def stand_in_phone_box(page: Any, _phone: Any) -> None:
    page.box(PHONE_BOX)


class RegistryCase(ForgeAsyncTestCase):
    def setUp(self) -> None:
        super().setUp()
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        self.addCleanup(i18n.set_language, i18n.current_language())
        i18n.set_language(Language.EN)
        patcher = mock.patch.object(settings_dialog, "PhoneBox", stand_in_phone_box)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _destroy_root(self) -> None:
        for child in list(self.root.winfo_children()):
            child.destroy()
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()

    def window(
        self,
        *,
        forge: bool = True,
        plex: bool = True,
        folders: bool = True,
        phone: bool = True,
        section: str | None = None,
    ) -> tuple[SettingsDialog, FakeForge]:
        settings = SettingsService(FakeRepository(), Settings())  # type: ignore[arg-type]
        fake_forge = FakeForge(FakeSettings(READY))
        dialog = SettingsDialog(
            self.root,
            settings,
            search=mock.MagicMock(),
            station=mock.MagicMock(),
            updates=SimpleNamespace(current="3.2.0", previous_version="3.1.0"),  # type: ignore[arg-type]
            torrent_app=mock.MagicMock(find=mock.AsyncMock(return_value=None)),
            discovery=mock.MagicMock(check_tmdb_key=mock.AsyncMock(return_value=True)),
            prowlarr=FakeProwlarr(),  # type: ignore[arg-type]
            startup_shortcut=FakeShortcut(),
            backup=FakeBackup(),  # type: ignore[arg-type]
            problem_reports=mock.MagicMock(),
            plex=FakePlex() if plex else None,  # type: ignore[arg-type]
            folder_library=FakeFolders(settings) if folders else None,  # type: ignore[arg-type]
            phone=SimpleNamespace(available=True, can_switch=False) if phone else None,  # type: ignore[arg-type]
            forge=fake_forge if forge else None,  # type: ignore[arg-type]
            section=section,
        )
        dialog.attributes("-alpha", 0.0)
        self.addCleanup(lambda: dialog.destroy() if dialog.winfo_exists() else None)
        return dialog, fake_forge

    async def every_page_shown(self, dialog: SettingsDialog) -> None:
        """Each page once, MediaForge's parts built and ready."""
        for page in dialog.sections:
            dialog.show_section(page)
        for page in (PAGE_SORTING, PAGE_NAMING, PAGE_SUBTITLES, PAGE_WINDOWS):
            part: Any = dialog.parts[page]
            await pump_until(self.root, lambda: bool(part.ready))  # awaited here: this part


class PagesTests(RegistryCase):
    def test_overview_then_four_groups_of_sixteen_pages(self) -> None:
        self.assertEqual(GROUPS[0], (None, (PAGE_OVERVIEW,)))
        self.assertEqual(
            [(group, list(pages)) for group, pages in GROUPS[1:]],
            [
                ("Downloading", ["Torrent program", "Quality & sources", "Automatic"]),
                ("Your files", ["Folders", "Sorting", "Naming", "Subtitles"]),
                ("Watching", ["Plex & library", "Playing", "Phone connection"]),
                ("App", ["Accounts & keys", "Looks", "Pages", "Windows", "Updates & backup"]),
            ],
        )
        self.assertEqual(len(PAGE_ORDER), 16)
        self.assertEqual(set(PAGES), set(PAGE_ORDER))

    async def test_groups_and_pages_in_order(self) -> None:
        dialog, _forge = self.window()
        self.assertEqual(dialog.sections, list(PAGE_ORDER))
        shown = shown_texts(dialog._sidebar)
        groups = [text for text in shown if text.isupper() and len(text) > 2]
        self.assertEqual(groups, ["DOWNLOADING", "YOUR FILES", "WATCHING", "APP"])
        # Overview above the groups, without a label; then a group's label, its pages' icons
        # and names.
        self.assertEqual(shown[:2], [PAGES[PAGE_OVERVIEW].icon, PAGE_OVERVIEW])
        at = shown.index("YOUR FILES") + 1
        for page in (PAGE_FOLDERS, PAGE_SORTING, PAGE_NAMING, PAGE_SUBTITLES):
            self.assertEqual(shown[at : at + 2], [PAGES[page].icon, page])
            at += 2

    async def test_a_page_without_its_service_is_left_out(self) -> None:
        dialog, _forge = self.window(phone=False)
        self.assertNotIn(PAGE_PHONE, dialog.sections)
        dialog, _forge = self.window(plex=False, folders=False)
        self.assertNotIn(PAGE_PLEX, dialog.sections)
        self.assertNotIn(PAGE_PLAYING, dialog.sections)
        dialog, _forge = self.window(plex=False)  # the folders alone: no Play in yet
        self.assertIn(PAGE_PLEX, dialog.sections)
        self.assertNotIn(PAGE_PLAYING, dialog.sections)
        dialog, _forge = self.window(forge=False, plex=False, folders=False, phone=False)
        for page in (PAGE_SORTING, PAGE_NAMING, PAGE_SUBTITLES, PAGE_PLEX, PAGE_PHONE):
            self.assertNotIn(page, dialog.sections)
        # Its other pages stay, without MediaForge's boxes.
        for page in (PAGE_FOLDERS, PAGE_ACCOUNTS, PAGE_WINDOWS):
            self.assertIn(page, dialog.sections)
        dialog.show_section(PAGE_WINDOWS)
        self.assertNotIn("Explorer's right-click menu", shown_texts(dialog.page(PAGE_WINDOWS)))
        self.assertEqual(
            [text for text in shown_texts(dialog._sidebar) if text.isupper() and len(text) > 2],
            ["DOWNLOADING", "YOUR FILES", "APP"],  # Watching has no page left
        )

    async def test_opens_on_overview(self) -> None:
        dialog, forge = self.window()
        self.assertEqual(dialog.section, PAGE_OVERVIEW)
        self.assertTrue(dialog._items[PAGE_OVERVIEW].selected)
        texts = shown_texts(dialog.page(PAGE_OVERVIEW))
        self.assertEqual(texts[:2], [PAGE_OVERVIEW, PAGES[PAGE_OVERVIEW].note])
        # The cards (app/ui/settings_overview.py, tests/test_settings_overview.py), then Quick setup.
        self.assertEqual(texts[3], PAGE_TORRENT)  # the first card: its dot, then its page's name
        self.assertIn("Quick setup", texts[texts.index(PAGE_WINDOWS) :])
        self.assertEqual(dialog.parts, {})  # nothing of MediaForge's built
        self.assertEqual(forge.starts, 0)
        dialog, _forge = self.window(section=PAGE_PAGES)
        self.assertEqual(dialog.section, PAGE_PAGES)
        dialog, _forge = self.window(section="Advanced")  # gone: Overview
        self.assertEqual(dialog.section, PAGE_OVERVIEW)

    async def test_every_old_box_has_a_page(self) -> None:
        dialog, _forge = self.window()
        await self.every_page_shown(dialog)
        for old, page, found_by in OLD_BOXES:
            with self.subTest(box=old):
                dialog.show_section(page)
                self.assertIn(found_by, shown_texts(dialog.page(page)))

    async def test_mediaforges_parts_and_the_pages_they_fill(self) -> None:
        dialog, _forge = self.window()
        await self.every_page_shown(dialog)
        self.assertIsInstance(dialog.parts[PAGE_SORTING], LibrarySection)
        self.assertNotIn(PAGE_FOLDERS, dialog.parts)  # AirGrabber's page now
        self.assertIsInstance(dialog.parts[PAGE_NAMING], NamingSection)
        self.assertIsInstance(dialog.parts[PAGE_SUBTITLES], SubtitlesSection)
        self.assertIs(dialog.parts[PAGE_ACCOUNTS], dialog.parts[PAGE_SUBTITLES])
        self.assertIsInstance(dialog.parts[PAGE_WINDOWS], MenuSection)
        # One part each, whichever of its pages: four parts in all.
        self.assertEqual(
            len({id(part) for part in dialog.parts.values()}), 4
        )
        # The Folders page: Series, Movies, Finished downloads, Count what's in them.
        texts = shown_texts(dialog.page(PAGE_FOLDERS))
        order = [
            texts.index(text)
            for text in ("Series", "Movies", "Finished downloads", "Count what's in them")
        ]
        self.assertEqual(order, sorted(order))
        # Accounts & keys: TMDB, OpenSubtitles (MediaForge's), Plex.
        texts = shown_texts(dialog.page(PAGE_ACCOUNTS))
        order = [texts.index(text) for text in ("TMDB", "OpenSubtitles", "Plex")]
        self.assertEqual(order, sorted(order))

    async def test_visiting_folders_builds_no_mediaforge_part(self) -> None:
        dialog, forge = self.window()
        dialog.show_section(PAGE_FOLDERS)
        self.assertEqual(forge.starts, 0)  # FakeForge starts as started: nothing to start
        self.assertEqual(dialog.parts, {})
        page = dialog.folders_page
        assert page is not None
        self.assertEqual(page.destination("series"), READY.series_folder)  # the marks are there

    async def test_search_finds_a_mediaforge_box_before_its_page_is_built(self) -> None:
        dialog, _forge = self.window()
        box = dialog.search_box
        box.entry.insert(0, "opensubtitles")
        box.refresh()
        self.assertIn(PAGE_ACCOUNTS, [match.section for match in box.matches])
        box.entry.delete(0, "end")
        box.entry.insert(0, "when sorting")
        box.refresh()
        self.assertIn(PAGE_SORTING, [match.section for match in box.matches])


class OpenersTests(RegistryCase):
    async def test_forge_openers_reach_sorting_and_folders(self) -> None:
        from app.forge.ui.page import ForgePage

        asked: list[tuple[Any, ...]] = []
        page = SimpleNamespace(on_settings=lambda *args: asked.append(args))
        ForgePage._status_clicked(page)  # type: ignore[arg-type]
        ForgePage._folders_clicked(page)  # type: ignore[arg-type]
        self.assertEqual(asked, [(PAGE_SORTING,), (PAGE_FOLDERS, FOLDERS_SPOT)])

    async def test_the_folders_spot_is_on_the_folders_page(self) -> None:
        dialog, _forge = self.window()
        dialog.go_to_spot(PAGE_FOLDERS, FOLDERS_SPOT)
        self.assertEqual(dialog.section, PAGE_FOLDERS)
        page = dialog.folders_page
        assert page is not None
        await pump_until(self.root, lambda: page.spots[FOLDERS_SPOT].winfo_ismapped())

    async def test_a_rules_spot_is_on_sorting(self) -> None:
        dialog, _forge = self.window()
        dialog.show_section(PAGE_SORTING)
        part = dialog.parts[PAGE_SORTING]
        assert isinstance(part, LibrarySection)
        rule = next(rule for rule in part.rule_drafts if rule.id)
        self.assertIn(RULE_SPOT.format(id=rule.id), part.spots)
