"""Settings → Overview and the polish of docs/settings/2026-11-19-settings-redesign.md §5–6: the
cards (pure `overview_cards`, one test per card's states), a card's click, the Folders card amber
while a destination is missing, the Overview never starting MediaForge; the release groups as
chips (the same tuple as typing them), the learned groups' line, "? Codes" and "What does a
library add?" folded at first, every on/off a switch, the in-window pointers as links, and every
box found by Settings' search. Fakes throughout (tests/test_settings_dialog.py's,
tests/test_settings_registry.py's window, tests/forge/ui's MediaForge); nothing real is asked."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest import mock

import customtkinter as ctk

from app.forge.models.settings import ForgeSettings
from app.forge.ui.settings_pages import LibrarySection, overview_facts
from app.models import DownloadMode, RemoteKind, Settings
from app.models.folder_library import FolderLibraryStatus
from app.models.plex import PlexProblem, PlexStatus
from app.services import SettingsService
from app.services.qbittorrent_webui import USERNAME
from app.ui import theme
from app.ui.settings_chips import added
from app.ui.settings_dialog import LEARNED_LINE, SettingsDialog
from app.ui.settings_layout import PageLink
from app.ui.settings_overview import Card, ForgeFacts, overview_cards
from app.ui.settings_pages_registry import (
    PAGE_ACCOUNTS,
    PAGE_AUTOMATIC,
    PAGE_FOLDERS,
    PAGE_NAMING,
    PAGE_ORDER,
    PAGE_OVERVIEW,
    PAGE_PLEX,
    PAGE_QUALITY,
    PAGE_SORTING,
    PAGE_SUBTITLES,
    PAGE_TORRENT,
    PAGE_UPDATES,
    PAGE_WINDOWS,
)
from app.ui.settings_search import search
from app.ui.torrent_app_status import TorrentStatus
from tests.forge.tk import pump_until
from tests.forge.ui.test_settings_library import READY, FakeForge, FakeSettings
from tests.test_backup_dialog import FakeBackup
from tests.test_folder_library_settings import FakeFolders
from tests.test_plex_ui import FakePlex
from tests.test_settings_dialog import (
    DialogCase,
    FakeProwlarr,
    FakeRepository,
    FakeShortcut,
    settle,
)
from tests.test_settings_registry import RegistryCase

NOW = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
FACTS = ForgeFacts(
    watching=True,
    rules_on=2,
    series_folder=r"D:\Series",
    movies_folder=r"D:\Movies",
    needs_series=True,
    needs_movies=True,
)


def card(page: str, settings: Settings | None = None, **facts: Any) -> Card:
    """The card for `page` with every page shown."""
    found = {
        c.page: c
        for c in overview_cards(
            settings or Settings(),
            pages=PAGE_ORDER,
            version=facts.pop("version", "3.20.0"),
            start_with_windows=facts.pop("start_with_windows", False),
            now=NOW,
            **facts,
        )
    }
    return found[page]


class CardsTests(unittest.TestCase):
    def test_cards_follow_the_settings_in_order_for_the_pages_shown(self) -> None:
        pages = [c.page for c in overview_cards(
            Settings(), pages=PAGE_ORDER, version="1", start_with_windows=False
        )]
        self.assertEqual(
            pages,
            [
                PAGE_TORRENT,
                PAGE_QUALITY,
                PAGE_AUTOMATIC,
                PAGE_FOLDERS,
                PAGE_SORTING,
                PAGE_SUBTITLES,
                PAGE_PLEX,
                PAGE_ACCOUNTS,
                PAGE_UPDATES,
                PAGE_WINDOWS,
            ],
        )
        without = overview_cards(
            Settings(),
            pages=[p for p in PAGE_ORDER if p not in (PAGE_SORTING, PAGE_SUBTITLES, PAGE_PLEX)],
            version="1",
            start_with_windows=False,
        )
        self.assertNotIn(PAGE_SORTING, [c.page for c in without])

    def test_cards_follow_the_settings_torrent_program(self) -> None:
        app = Settings(download_mode=DownloadMode.APP)
        self.assertEqual(  # not looked yet: no verdict
            card(PAGE_TORRENT, app),
            Card(PAGE_TORRENT, "off", "On this PC: looking for the torrent program…"),
        )
        self.assertEqual(
            card(PAGE_TORRENT, app, torrent=TorrentStatus.READY, torrent_name="qBittorrent").line,
            "qBittorrent on this PC, ready",
        )
        self.assertEqual(
            card(PAGE_TORRENT, app, torrent=TorrentStatus.FOUND, torrent_name="Transmission").line,
            "Transmission on this PC",
        )
        self.assertEqual(card(PAGE_TORRENT, app, torrent=TorrentStatus.MISSING).state, "warn")
        self.assertEqual(card(PAGE_TORRENT, app, torrent=TorrentStatus.SETUP_FAILED).state, "warn")
        # As the page has it: qBittorrent still asking about each download, or waiting to close.
        self.assertEqual(
            card(PAGE_TORRENT, app, torrent=TorrentStatus.SETUP_NEEDED),
            Card(PAGE_TORRENT, "warn", "qBittorrent on this PC still asks about each download"),
        )
        self.assertEqual(
            card(PAGE_TORRENT, app, torrent=TorrentStatus.SETUP_WAITING).line,
            "On this PC: close qBittorrent to finish setting it up",
        )
        self.assertNotIn("on this PC on this PC", card(PAGE_TORRENT, app, torrent=TorrentStatus.FOUND).line)
        # Round 2, 10: installing or setting up says so, not "looking for" (still no verdict).
        for status, line in (
            (TorrentStatus.INSTALLING, "On this PC: installing qBittorrent…"),
            (TorrentStatus.SETTING_UP, "On this PC: setting up qBittorrent…"),
            (TorrentStatus.REINSTALLING, "On this PC: reinstalling qBittorrent…"),
        ):
            with self.subTest(status=status):
                self.assertEqual(card(PAGE_TORRENT, app, torrent=status), Card(PAGE_TORRENT, "off", line))
        remote = Settings(download_mode=DownloadMode.REMOTE)
        self.assertEqual(
            card(PAGE_TORRENT, remote),
            Card(PAGE_TORRENT, "warn", "A NAS or another computer, not set up yet"),
        )
        ready = Settings(
            download_mode=DownloadMode.REMOTE,
            remote_kind=RemoteKind.TRANSMISSION,
            remote_url="http://192.168.1.20:9091",
        )
        self.assertEqual(card(PAGE_TORRENT, ready).state, "ok")
        self.assertIn("192.168.1.20", card(PAGE_TORRENT, ready).line)
        folder = Settings(download_mode=DownloadMode.FOLDER)
        self.assertEqual(card(PAGE_TORRENT, folder).line, ".torrent files to a folder")

    def test_cards_follow_the_settings_airgrabbers_own_qbittorrent(self) -> None:
        # The NAS / other computer choice pointed at AirGrabber's qBittorrent (a new install's):
        # its own look on this PC decides (review I1).
        local = Settings(
            download_mode=DownloadMode.REMOTE,
            remote_kind=RemoteKind.QBITTORRENT,
            remote_url="http://127.0.0.1:8181",
            remote_username=USERNAME,
        )
        self.assertEqual(card(PAGE_TORRENT, local).state, "off")  # not looked yet
        self.assertEqual(
            card(PAGE_TORRENT, local, local_torrent=TorrentStatus.READY),
            Card(PAGE_TORRENT, "ok", "qBittorrent on this PC (set up by AirGrabber)"),
        )
        self.assertEqual(
            card(PAGE_TORRENT, local, local_torrent=TorrentStatus.MISSING),
            Card(PAGE_TORRENT, "warn", "On this PC: no torrent program found yet"),
        )
        for status in (TorrentStatus.SETUP_FAILED, TorrentStatus.SETUP_WAITING, TorrentStatus.FAILED):
            with self.subTest(status=status):
                self.assertEqual(card(PAGE_TORRENT, local, local_torrent=status).state, "warn")
        self.assertEqual(
            card(PAGE_TORRENT, local, local_torrent=TorrentStatus.SETTING_UP).line,
            "On this PC: setting up qBittorrent…",
        )

    def test_cards_follow_the_settings_quality_and_sources(self) -> None:
        found = card(PAGE_QUALITY, Settings(quality_tv="1080p", quality_movie="1080p"))
        self.assertEqual(found, Card(PAGE_QUALITY, "ok", "Full HD (1080p) · 4 sites"))  # no Prowlarr set up
        prowlarr = {"prowlarr_url": "http://127.0.0.1:9696", "prowlarr_api_key": "key"}
        self.assertEqual(
            card(PAGE_QUALITY, Settings(quality_movie="1080p", **prowlarr)).line,
            "Full HD (1080p) · 4 sites + Prowlarr",
        )
        sites_off = ("apibay", "eztv", "bitsearch", "yts")
        ticked_only = Settings(disabled_indexers=sites_off)  # Prowlarr ticked, not set up
        self.assertEqual(card(PAGE_QUALITY, ticked_only), Card(PAGE_QUALITY, "warn", "No site to search"))
        only = card(PAGE_QUALITY, Settings(quality_movie="1080p", disabled_indexers=sites_off, **prowlarr))
        self.assertEqual(only, Card(PAGE_QUALITY, "ok", "Full HD (1080p) · Prowlarr"))
        two = card(
            PAGE_QUALITY,
            Settings(quality_movie="2160p", disabled_indexers=("eztv", "yts", "prowlarr")),
        )
        self.assertEqual(two.line, "Series Full HD (1080p), movies 4K (2160p) · 2 sites")
        none = Settings(disabled_indexers=("apibay", "eztv", "bitsearch", "yts", "prowlarr"))
        self.assertEqual(card(PAGE_QUALITY, none), Card(PAGE_QUALITY, "warn", "No site to search"))

    def test_cards_follow_the_settings_automatic(self) -> None:
        both = Settings(
            auto_download_new_shows=True, auto_download_new_films=True, auto_wait_hours=4
        )
        self.assertEqual(card(PAGE_AUTOMATIC, both).line, "On for new shows and films · waits 4 h")
        shows = Settings(auto_download_new_shows=True, auto_download_new_films=False)
        self.assertTrue(card(PAGE_AUTOMATIC, shows).line.startswith("On for new shows ·"))
        off = Settings(auto_download_new_shows=False, auto_download_new_films=False)
        self.assertEqual(card(PAGE_AUTOMATIC, off), Card(PAGE_AUTOMATIC, "off", "Off for new shows and films"))

    def test_cards_follow_the_settings_folders(self) -> None:
        self.assertEqual(card(PAGE_FOLDERS), Card(PAGE_FOLDERS, "off", "No folders chosen yet"))
        lists = Settings(series_folders=(r"D:\Series", r"E:\Old"), film_folders=(r"D:\Movies",))
        self.assertEqual(
            card(PAGE_FOLDERS, lists), Card(PAGE_FOLDERS, "ok", "1 film folder · 2 series folders")
        )
        self.assertEqual(card(PAGE_FOLDERS, lists, forge=FACTS).state, "ok")

    def test_amber_folders_when_a_destination_is_missing(self) -> None:
        no_movies = FACTS._replace(movies_folder="")
        self.assertEqual(
            card(PAGE_FOLDERS, forge=no_movies),
            Card(PAGE_FOLDERS, "warn", "Your Movies folder isn't chosen yet"),
        )
        neither = FACTS._replace(series_folder="", movies_folder="")
        self.assertEqual(
            card(PAGE_FOLDERS, forge=neither).line,
            "Your Series and Movies folders aren't chosen yet",
        )
        # Nothing sends films there: no folder is needed, nothing amber.
        unneeded = no_movies._replace(needs_movies=False)
        self.assertNotEqual(card(PAGE_FOLDERS, forge=unneeded).state, "warn")

    def test_cards_follow_the_settings_sorting(self) -> None:
        self.assertEqual(card(PAGE_SORTING), Card(PAGE_SORTING, "off", "Getting MediaForge ready…"))
        self.assertEqual(card(PAGE_SORTING, forge_reason="It can't.").state, "warn")
        self.assertEqual(card(PAGE_SORTING, forge=FACTS), Card(PAGE_SORTING, "ok", "Watching · 2 rules on"))
        none_on = FACTS._replace(rules_on=0)  # watching with nothing to do it with
        self.assertEqual(
            card(PAGE_SORTING, forge=none_on), Card(PAGE_SORTING, "warn", "Watching, but no rule is on")
        )
        paused = FACTS._replace(watching=False, rules_on=1)
        self.assertEqual(card(PAGE_SORTING, forge=paused), Card(PAGE_SORTING, "off", "Paused · 1 rule on"))

    def test_cards_follow_the_settings_subtitles(self) -> None:
        both = Settings(subtitle_languages=("en", "nl"))
        self.assertEqual(card(PAGE_SUBTITLES, both), Card(PAGE_SUBTITLES, "ok", "English, Dutch"))

    def test_cards_follow_the_settings_plex_and_library(self) -> None:
        self.assertEqual(card(PAGE_PLEX).state, "off")
        plex = Settings(plex_token="token", plex_server_name="Alex-NAS")
        self.assertEqual(card(PAGE_PLEX, plex), Card(PAGE_PLEX, "ok", "Connected to Alex-NAS"))
        trouble = PlexStatus(connected=True, problem=PlexProblem.SIGNED_OUT)
        self.assertEqual(card(PAGE_PLEX, plex, plex=trouble).state, "warn")
        on = FolderLibraryStatus(on=True, films=2, shows=1)
        self.assertEqual(
            card(PAGE_PLEX, folders=on), Card(PAGE_PLEX, "ok", "Your own folders: 2 films · 1 series")
        )
        self.assertEqual(
            card(PAGE_PLEX, plex, folders=on).line, "Connected to Alex-NAS · your folders too"
        )

    def test_cards_follow_the_settings_accounts(self) -> None:
        self.assertEqual(
            card(PAGE_ACCOUNTS),
            Card(PAGE_ACCOUNTS, "warn", "No TMDB key yet · OpenSubtitles without an account"),
        )
        keyed = Settings(tmdb_api_key="abc", opensubtitles_username="alex")
        self.assertEqual(
            card(PAGE_ACCOUNTS, keyed),
            Card(PAGE_ACCOUNTS, "ok", "TMDB key set · OpenSubtitles account: alex"),  # what is stored
        )
        [accounts] = [
            c
            for c in overview_cards(
                keyed, pages=[PAGE_ACCOUNTS], version="1", start_with_windows=False
            )
        ]
        self.assertEqual(accounts.line, "TMDB key set")  # no Subtitles page: no OpenSubtitles

    def test_cards_follow_the_settings_updates_and_windows(self) -> None:
        self.assertEqual(
            card(PAGE_UPDATES, Settings(auto_update=True)),
            Card(PAGE_UPDATES, "ok", "Version 3.20.0 · updates by themselves"),
        )
        self.assertEqual(card(PAGE_UPDATES, Settings(auto_update=False)).state, "off")
        self.assertEqual(
            card(PAGE_WINDOWS, Settings(keep_in_tray=True)),
            Card(PAGE_WINDOWS, "off", "In the tray · doesn't start with Windows"),
        )
        self.assertEqual(
            card(PAGE_WINDOWS, Settings(keep_in_tray=True), start_with_windows=True).state, "ok"
        )

    def test_forge_facts_read_the_rules_without_starting_it(self) -> None:
        forge = FakeForge(FakeSettings(READY.model_copy(update={"movies_folder": ""})))
        facts = overview_facts(forge)
        assert facts is not None
        self.assertEqual((facts.series_folder, facts.movies_folder), (READY.series_folder, ""))
        self.assertTrue(facts.needs_series and facts.needs_movies)  # the rules move to library
        self.assertEqual(forge.starts, 0)
        # Presets don't count (the controller's ruling): no rule on, nothing is needed.
        fake = FakeSettings(READY.model_copy(update={"watch_folders": [], "use_airgrabber_downloads": False}))
        idle = FakeForge(fake)
        facts = overview_facts(idle)
        assert facts is not None
        self.assertFalse(facts.needs_series or facts.needs_movies)
        self.assertIsNone(overview_facts(FakeForge(FakeSettings(READY), started=False)))

    def test_chips_add_names_once(self) -> None:
        self.assertEqual(added(["ntb"], " FLUX, ntb ,NTB, , evo"), ["ntb", "FLUX", "evo"])


class OverviewWindowTests(DialogCase):
    async def test_a_card_click_opens_its_page(self) -> None:
        dialog, _settings = self.dialog()
        self.assertEqual(dialog.section, PAGE_OVERVIEW)
        overview = dialog.overview
        assert overview is not None
        self.assertIn(PAGE_QUALITY, overview.views)
        overview.views[PAGE_QUALITY].click()
        self.assertEqual(dialog.section, PAGE_QUALITY)
        dialog.show_section(PAGE_OVERVIEW)
        overview.views[PAGE_WINDOWS].click()
        self.assertEqual(dialog.section, PAGE_WINDOWS)

    async def test_opening_draws_the_cards_for_no_status_only_shown_to_measure(self) -> None:
        # Final review Minor 5: `_fit_size` shows SETUP_NEEDED only to measure the page.
        seen: list[TorrentStatus] = []
        real = SettingsDialog.draw_overview

        def draw(dialog: SettingsDialog) -> None:
            seen.append(dialog._torrent.status)
            real(dialog)

        with mock.patch.object(SettingsDialog, "draw_overview", autospec=True, side_effect=draw):
            dialog, _settings = self.dialog()
        self.assertNotIn(TorrentStatus.SETUP_NEEDED, seen)
        self.assertTrue(seen)
        dialog._torrent.show(TorrentStatus.READY, "qBittorrent")  # from here on: redrawn
        self.assertIs(seen[-1], TorrentStatus.READY)

    async def test_the_torrent_card_knows_airgrabbers_own_qbittorrent_without_a_visit(
        self,
    ) -> None:
        # Round 2, 10: at the window's start, and when the form, an Undo or the setup points
        # AirGrabber at its own qBittorrent later (the download choice staying the same).
        own = {
            "remote_kind": RemoteKind.QBITTORRENT,
            "remote_username": USERNAME,
            "remote_url": "http://127.0.0.1:8181",
        }
        dialog, _settings = self.dialog(download_mode=DownloadMode.REMOTE, **own)
        await settle()
        self.assertEqual(dialog.section, PAGE_OVERVIEW)
        looking = "On this PC: looking for the torrent program…"
        cards = {c.page: c for c in dialog.overview_now()}
        self.assertNotEqual(cards[PAGE_TORRENT].line, looking)
        self.assertIs(dialog._local_torrent.status, TorrentStatus.MISSING)  # the fake finds none
        dialog, settings = self.dialog(
            download_mode=DownloadMode.REMOTE,
            remote_kind=RemoteKind.QBITTORRENT,
            remote_username="me",
            remote_url="http://192.168.1.20:8080",
        )
        await settle()
        await settings.update(**own)
        await settle()
        cards = {c.page: c for c in dialog.overview_now()}
        self.assertEqual(cards[PAGE_TORRENT].line, "On this PC: no torrent program found yet")

    async def test_the_cards_follow_a_change(self) -> None:
        dialog, settings = self.dialog(auto_download_new_shows=False, auto_download_new_films=False)
        overview = dialog.overview
        assert overview is not None
        self.assertEqual(overview.card(PAGE_AUTOMATIC), Card(PAGE_AUTOMATIC, "off", "Off for new shows and films"))
        views = dict(overview.views)
        await settings.update(auto_download_new_shows=True)
        await settle()
        found = overview.card(PAGE_AUTOMATIC)
        assert found is not None
        self.assertEqual(found.state, "ok")
        # Drawn again in place, the other cards untouched (no rebuild of all ten per change).
        self.assertEqual(overview.views, views)
        changed = overview.views[PAGE_AUTOMATIC]
        self.assertIs(changed.card, found)
        self.assertEqual(changed.line.cget("text"), found.line)
        self.assertEqual(tuple(changed.dot.cget("text_color")), tuple(theme.SUCCESS))
        # Drawn again while the mouse is on it: it keeps the hover edge (review M5).
        changed._hover(True)
        await settings.update(auto_download_new_films=True)
        await settle()
        self.assertEqual(tuple(changed.cget("border_color")), tuple(theme.EDGE))
        changed._hover(False)
        self.assertEqual(tuple(changed.cget("border_color")), tuple(theme.PANEL_EDGE))

    async def test_release_groups_as_chips_store_the_same_tuple(self) -> None:
        dialog, settings = self.dialog(preferred_groups="ntb")
        chips = dialog.group_chips["preferred_groups"]
        self.assertEqual(chips.names, ["ntb"])
        chips.open_field()
        chips.entry.insert(0, "FLUX, ntb")
        chips.field.commit_now()  # Enter
        await settle()
        typed = Settings(preferred_groups="ntb, FLUX").preferred_groups  # as the old field
        self.assertEqual(settings.current.preferred_groups, typed)
        self.assertEqual(settings.current.preferred_groups, ("ntb", "flux"))
        self.assertEqual(chips.names, ["ntb", "flux"])  # the stored form
        chips.chips["ntb"].invoke()  # its ×
        await settle()
        self.assertEqual(settings.current.preferred_groups, ("flux",))
        await dialog.committer.run_undo()
        await settle()
        self.assertEqual(settings.current.preferred_groups, ("ntb", "flux"))
        self.assertEqual(chips.names, ["ntb", "flux"])
        # Never use releases from: the same; what the field holds is stored when Settings closes.
        blocked = dialog.group_chips["blocked_groups"]
        blocked.open_field()
        blocked.entry.insert(0, "YIFY")
        dialog.close()
        await settle()
        self.assertEqual(settings.current.blocked_groups, ("yify",))

    async def test_the_learned_groups_show_under_prefer(self) -> None:
        dialog, _settings = self.dialog(learned_groups=("evo", "ntb"))
        line = dialog.learned_line
        assert line is not None
        self.assertEqual(line.cget("text"), LEARNED_LINE.format(groups="evo, ntb"))
        self.assertEqual(line.winfo_manager(), "grid")
        plain, _settings = self.dialog()
        self.assertIsNone(plain.learned_line)

    async def test_every_on_off_is_a_switch(self) -> None:
        dialog, _settings = self.dialog()
        for page in (PAGE_AUTOMATIC, PAGE_WINDOWS, PAGE_UPDATES):
            stack: list[Any] = [dialog.page(page)]
            while stack:
                widget = stack.pop()
                self.assertNotIsInstance(widget, ctk.CTkCheckBox, page)
                stack.extend(widget.winfo_children())

    async def test_the_prowlarr_panel_is_no_box_in_a_box(self) -> None:
        dialog, _settings = self.dialog()
        self.assertEqual(dialog._prowlarr_panel.cget("border_width"), 0)

    async def test_the_update_badge_opens_updates(self) -> None:
        from app.ui.main_window import MainWindow

        asked: list[Any] = []
        window: Any = SimpleNamespace(_open_settings=lambda *args: asked.append(args))
        MainWindow._open_updates(window)
        self.assertEqual(asked, [(PAGE_UPDATES,)])


class WholeWindowTests(RegistryCase):
    def open(
        self,
        forge_settings: ForgeSettings = READY,
        *,
        started: bool = True,
        forge: FakeForge | None = None,
    ) -> tuple[SettingsDialog, FakeForge]:
        settings = SettingsService(FakeRepository(), Settings())  # type: ignore[arg-type]
        if forge is None:
            forge = FakeForge(FakeSettings(forge_settings), started=started)
        dialog = SettingsDialog(
            self.root,
            settings,
            search=mock.MagicMock(),
            station=mock.MagicMock(),
            updates=SimpleNamespace(current="3.2.0", previous_version=None),  # type: ignore[arg-type]
            torrent_app=mock.MagicMock(find=mock.AsyncMock(return_value=None)),
            discovery=mock.MagicMock(check_tmdb_key=mock.AsyncMock(return_value=True)),
            prowlarr=FakeProwlarr(),  # type: ignore[arg-type]
            startup_shortcut=FakeShortcut(),
            backup=FakeBackup(),  # type: ignore[arg-type]
            problem_reports=mock.MagicMock(),
            plex=FakePlex(),  # type: ignore[arg-type]
            folder_library=FakeFolders(settings),  # type: ignore[arg-type]
            phone=SimpleNamespace(available=True, can_switch=False),  # type: ignore[arg-type]
            forge=forge,  # type: ignore[arg-type]
        )
        dialog.attributes("-alpha", 0.0)
        self.addCleanup(lambda: dialog.destroy() if dialog.winfo_exists() else None)
        return dialog, forge

    async def test_the_overview_never_starts_mediaforge(self) -> None:
        dialog, forge = self.open(started=False)
        await settle()
        self.assertEqual(forge.starts, 0)
        overview = dialog.overview
        assert overview is not None
        self.assertEqual(overview.card(PAGE_SORTING), Card(PAGE_SORTING, "off", "Getting MediaForge ready…"))
        # Started meanwhile (a page that needs it): the cards say more when the page shows again.
        dialog.show_section(PAGE_SORTING)
        await pump_until(self.root, lambda: bool(dialog.parts[PAGE_SORTING].ready))
        dialog.show_section(PAGE_OVERVIEW)
        await settle()
        found = overview.card(PAGE_SORTING)
        assert found is not None
        self.assertTrue(found.line.startswith("Paused · "), found)  # READY's watching is off

    async def test_the_folders_card_is_the_same_before_and_after_visiting_sorting(self) -> None:
        # The controller's ruling (review I3): only rules count, so which pages were visited
        # changes nothing. No rule on, no Movies destination: never amber, the same both times.
        # Only AirGrabber's rule, switched off (a rule list always holds it).
        stored = FakeSettings(
            READY.model_copy(
                update={"movies_folder": "", "watch_folders": [], "use_airgrabber_downloads": False}
            )
        )
        dialog, _forge = self.open(forge=FakeForge(stored))
        await settle()
        overview = dialog.overview
        assert overview is not None
        before = overview.card(PAGE_FOLDERS)
        assert before is not None
        self.assertNotEqual(before.state, "warn")
        dialog.show_section(PAGE_SORTING)
        await pump_until(self.root, lambda: bool(dialog.parts[PAGE_SORTING].ready))
        await settle()
        dialog.show_section(PAGE_OVERVIEW)
        await settle()
        self.assertEqual(overview.card(PAGE_FOLDERS), before)

    async def test_a_start_that_fails_shows_on_sorting_and_a_closed_window_is_forgotten(self) -> None:
        # Review M2 / M3: the window hears the start end (ready or not) and forgets that when
        # it closes, so a start that never comes keeps no closed window.
        forge = EndingForge(FakeSettings(READY), started=False)
        dialog, _forge = self.open(forge=forge)
        await settle()
        self.assertEqual(len(forge.ends), 1)
        overview = dialog.overview
        assert overview is not None
        forge.end(reason="MediaForge couldn't start. The log has the details.")
        await settle()
        self.assertEqual(
            overview.card(PAGE_SORTING),
            Card(PAGE_SORTING, "warn", "MediaForge couldn't start. The log has the details."),
        )
        other = EndingForge(FakeSettings(READY), started=False)
        closed, _forge = self.open(forge=other)
        self.assertEqual(len(other.ends), 1)
        closed.destroy()
        self.assertEqual(other.ends, [])

    async def test_a_start_that_fails_without_a_reason_says_mediaforge_isnt_available(
        self,
    ) -> None:
        # Never "Getting MediaForge ready…" for ever once the start has ended.
        forge = EndingForge(FakeSettings(READY), started=False)
        dialog, _forge = self.open(forge=forge)
        await settle()
        overview = dialog.overview
        assert overview is not None
        forge.end(reason="")
        await settle()
        self.assertEqual(
            overview.card(PAGE_SORTING),
            Card(PAGE_SORTING, "warn", "MediaForge isn't available."),
        )

    async def test_amber_folders_card_with_mediaforge_running(self) -> None:
        dialog, _forge = self.open(READY.model_copy(update={"movies_folder": ""}))
        await settle()
        overview = dialog.overview
        assert overview is not None
        self.assertEqual(overview.card(PAGE_FOLDERS), Card(PAGE_FOLDERS, "warn", "Your Movies folder isn't chosen yet"))
        self.assertEqual(overview.views[PAGE_FOLDERS].cget("border_color"), theme.WARNING)
        overview.views[PAGE_FOLDERS].click()
        self.assertEqual(dialog.section, PAGE_FOLDERS)

    async def test_codes_fold_starts_closed(self) -> None:
        dialog, _forge = self.open()
        dialog.show_section(PAGE_NAMING)
        part: Any = dialog.parts[PAGE_NAMING]
        await pump_until(self.root, lambda: bool(part.ready))
        self.assertFalse(part.codes_fold.opened)
        self.assertEqual(part.codes_area.winfo_manager(), "")
        self.assertEqual(part.codes_fold.button.cget("text"), "? Codes")
        part.codes_fold.button.invoke()
        self.assertEqual(part.codes_area.winfo_manager(), "grid")
        # "What does a library add?" on Plex & library is folded too.
        compare = dialog.plex_compare
        assert compare is not None
        self.assertFalse(compare.fold.opened)
        self.assertEqual(compare.table.winfo_manager(), "")

    async def test_pointer_links_open_their_page(self) -> None:
        no_folders = READY.model_copy(update={"series_folder": "", "movies_folder": ""})
        dialog, _forge = self.open(no_folders)
        dialog.show_section(PAGE_SORTING)
        part = dialog.parts[PAGE_SORTING]
        assert isinstance(part, LibrarySection)
        await pump_until(self.root, lambda: part.ready)
        links = [n for row in part.rule_rows for n in row.notes if isinstance(n, PageLink)]
        self.assertTrue(links)  # "⚠ Choose your Series folder under Folders."
        self.assertIn("⚠ Choose your Series folder under Folders.", [str(n.cget("text")) for n in links])
        links[0].click()
        self.assertEqual(dialog.section, PAGE_FOLDERS)

    async def test_mediaforges_on_offs_are_switches(self) -> None:
        dialog, _forge = self.open()
        await self.every_page_shown(dialog)
        naming: Any = dialog.parts[PAGE_NAMING]
        self.assertTrue(all(isinstance(b, ctk.CTkSwitch) for b in naming.option_boxes.values()))
        menu: Any = dialog.parts[PAGE_WINDOWS]
        self.assertIsInstance(menu.on_box, ctk.CTkSwitch)
        sorting = dialog.parts[PAGE_SORTING]
        assert isinstance(sorting, LibrarySection)
        stack: list[Any] = [sorting._options_box.frame]
        while stack:
            widget = stack.pop()
            self.assertNotIsInstance(widget, ctk.CTkCheckBox)
            stack.extend(widget.winfo_children())

    async def test_search_finds_every_box(self) -> None:
        dialog, _forge = self.open()
        await self.every_page_shown(dialog)
        everything = dialog._search_places()
        for section, page in dialog._pages.items():
            dialog.show_section(section)
            for title, frame in page.boxes:
                if not frame.winfo_manager():
                    continue  # "Getting MediaForge ready…", or a box that waits for its moment
                with self.subTest(section=section, title=title):
                    self.assertTrue(title, "a box on screen without a search title")
                    found = search(title, everything, limit=1000)
                    self.assertIn((section, title), [(m.section, m.title) for m in found])
        # The old names find their new place.
        for old, where in (("library folders", PAGE_FOLDERS), ("with and without", PAGE_PLEX)):
            with self.subTest(old=old):
                self.assertIn(where, [m.section for m in search(old, everything)])


class EndingForge(FakeForge):
    """FakeForge with ForgeRuntime's `when_start_ends`: `end()` ends a start (ready or not)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.ends: list[Any] = []

    def when_start_ends(self, callback: Any) -> Any:
        self.ends.append(callback)
        return lambda: self.ends.remove(callback) if callback in self.ends else None

    def end(self, *, reason: str = "") -> None:
        self.reason = reason
        callbacks, self.ends = self.ends, []
        for callback in callbacks:
            callback()


if __name__ == "__main__":
    unittest.main()
