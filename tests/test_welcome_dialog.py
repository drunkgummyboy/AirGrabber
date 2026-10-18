"""First-run welcome: when it opens, what each step saves, and the texts of its status lines."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from app.database import Database
from app.models import DownloadMode, Settings
from app.services import SettingsService
from app.ui import theme
from app.ui.tmdb_key_guide import KeyAction, KeyCheck, key_action, key_check_message
from app.ui.torrent_app_status import TorrentStatus, torrent_look
from app.ui.welcome_dialog import (
    FirstRun,
    Page,
    download_changes,
    elide_path,
    finish_changes,
    first_run_action,
    nav_buttons,
    next_page,
    page_step,
    previous_page,
    step_label,
)


class FirstRunActionTests(unittest.TestCase):
    def test_new_library_gets_the_welcome(self) -> None:
        self.assertIs(first_run_action(setup_done=False, tracked_shows=0), FirstRun.WELCOME)

    def test_existing_library_is_marked_done_silently(self) -> None:
        for count in (1, 39):
            with self.subTest(count=count):
                self.assertIs(
                    first_run_action(setup_done=False, tracked_shows=count), FirstRun.MARK_DONE
                )

    def test_done_means_nothing_whatever_the_library(self) -> None:
        for count in (0, 1, 39):
            with self.subTest(count=count):
                self.assertIs(first_run_action(setup_done=True, tracked_shows=count), FirstRun.NOTHING)

    def test_setup_done_defaults_to_false(self) -> None:
        self.assertFalse(Settings().setup_done)
        # Stored settings from before the welcome existed have no such key.
        self.assertFalse(Settings.model_validate({"quality": "1080p"}).setup_done)


class StepTests(unittest.TestCase):
    def test_step_labels(self) -> None:
        self.assertIsNone(step_label(1))
        self.assertEqual(step_label(2), "Step 2 of 4")
        self.assertEqual(step_label(3), "Step 3 of 4")
        self.assertEqual(step_label(4), "Step 4 of 4")

    def test_pages_and_their_steps(self) -> None:
        self.assertEqual(
            [page_step(page) for page in Page], [1, 2, 2, 3, 4]
        )  # the import panel is still step 2
        self.assertEqual(step_label(page_step(Page.AIRDATES_IMPORT)), "Step 2 of 4")
        self.assertEqual(step_label(page_step(Page.DOWNLOAD)), "Step 3 of 4")
        self.assertEqual(step_label(page_step(Page.TMDB)), "Step 4 of 4")

    def test_nav_buttons(self) -> None:
        self.assertEqual(nav_buttons(Page.WELCOME), (None, None))  # "Get started" in the content
        self.assertEqual(nav_buttons(Page.AIRDATES), ("Back", "Next"))
        self.assertEqual(nav_buttons(Page.DOWNLOAD), ("Back", "Next"))
        self.assertEqual(nav_buttons(Page.TMDB), ("Skip for now", "Finish"))

    def test_import_panel_skips_until_imported(self) -> None:
        self.assertEqual(nav_buttons(Page.AIRDATES_IMPORT), ("Back", "Skip"))
        self.assertEqual(nav_buttons(Page.AIRDATES_IMPORT, imported=True), ("Back", "Next"))
        # Only the panel's button depends on an import.
        self.assertEqual(nav_buttons(Page.DOWNLOAD, imported=True), ("Back", "Next"))

    def test_yes_goes_through_the_panel_to_downloads(self) -> None:
        page = next_page(Page.WELCOME, import_shows=True)
        self.assertIs(page, Page.AIRDATES)
        page = next_page(page, import_shows=True)
        self.assertIs(page, Page.AIRDATES_IMPORT)
        page = next_page(page, import_shows=True)
        self.assertIs(page, Page.DOWNLOAD)
        self.assertIs(next_page(page, import_shows=True), Page.TMDB)

    def test_no_goes_straight_to_downloads(self) -> None:
        self.assertIs(next_page(Page.AIRDATES, import_shows=False), Page.DOWNLOAD)
        self.assertIs(next_page(Page.DOWNLOAD, import_shows=False), Page.TMDB)

    def test_back(self) -> None:
        self.assertIs(previous_page(Page.AIRDATES, import_shows=True), Page.WELCOME)
        self.assertIs(previous_page(Page.AIRDATES_IMPORT, import_shows=True), Page.AIRDATES)
        self.assertIs(previous_page(Page.DOWNLOAD, import_shows=True), Page.AIRDATES_IMPORT)
        self.assertIs(previous_page(Page.DOWNLOAD, import_shows=False), Page.AIRDATES)


class ChangesTests(unittest.TestCase):
    def test_download_choice_without_a_new_folder(self) -> None:
        current = Settings(download_dir=Path("C:/Users/me/Downloads"))
        self.assertEqual(
            download_changes(current, DownloadMode.APP, Path("C:/Users/me/Downloads")),
            {"download_mode": DownloadMode.APP},
        )

    def test_download_choice_with_a_new_folder(self) -> None:
        current = Settings(download_dir=Path("C:/Users/me/Downloads"))
        changes = download_changes(current, DownloadMode.FOLDER, Path("Z:/watch"))
        self.assertEqual(
            changes, {"download_mode": DownloadMode.FOLDER, "download_dir": Path("Z:/watch")}
        )
        updated = Settings.model_validate({**current.model_dump(), **changes})
        self.assertIs(updated.download_mode, DownloadMode.FOLDER)
        self.assertEqual(updated.download_dir, Path("Z:/watch"))

    def test_finish_without_a_checked_key(self) -> None:
        self.assertEqual(finish_changes(None), {"setup_done": True})
        self.assertEqual(finish_changes(""), {"setup_done": True})

    def test_finish_with_a_checked_key(self) -> None:
        self.assertEqual(
            finish_changes("0123456789abcdef"),
            {"setup_done": True, "tmdb_api_key": "0123456789abcdef"},
        )


class SavingTests(unittest.IsolatedAsyncioTestCase):
    """The welcome's saves go through SettingsService and survive a restart."""

    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.db = await Database.open(Path(self.tmp.name) / "welcome.db")
        self.addAsyncCleanup(self.db.close)

    async def test_choices_persist(self) -> None:
        settings = await SettingsService.load(self.db.settings)
        self.assertFalse(settings.current.setup_done)
        await settings.update(
            **download_changes(settings.current, DownloadMode.APP, settings.current.download_dir)
        )
        await settings.update(**finish_changes("0123456789abcdef"))
        reloaded = await SettingsService.load(self.db.settings)
        self.assertTrue(reloaded.current.setup_done)
        self.assertIs(reloaded.current.download_mode, DownloadMode.APP)
        self.assertEqual(reloaded.current.tmdb_api_key.get_secret_value(), "0123456789abcdef")

    async def test_skip_keeps_an_existing_key(self) -> None:
        settings = await SettingsService.load(self.db.settings)
        await settings.update(tmdb_api_key="savedkey")
        await settings.update(**finish_changes(None))
        self.assertTrue(settings.current.setup_done)
        self.assertEqual(settings.current.tmdb_api_key.get_secret_value(), "savedkey")


class KeyActionTests(unittest.TestCase):
    """What Finish (welcome) and Save (key dialog) do with the key entry: a pasted key that
    nobody checked must be checked, not lost."""

    KEY = "0123456789abcdef0123456789abcdef"

    def test_empty_entry_saves_nothing(self) -> None:
        for entry in ("", "   ", "\n"):
            with self.subTest(entry=entry):
                self.assertIs(key_action(entry, None), KeyAction.NONE)

    def test_unchecked_key_is_checked_first(self) -> None:
        self.assertIs(key_action(self.KEY, None), KeyAction.CHECK)
        self.assertIs(key_action(f"  {self.KEY}\n", None), KeyAction.CHECK)

    def test_checked_key_is_saved(self) -> None:
        self.assertIs(key_action(self.KEY, self.KEY), KeyAction.SAVE)
        self.assertIs(key_action(f" {self.KEY} ", self.KEY), KeyAction.SAVE)  # pasted with spaces

    def test_a_different_key_than_the_checked_one_is_checked(self) -> None:
        self.assertIs(key_action(self.KEY + "x", self.KEY), KeyAction.CHECK)


class ElidePathTests(unittest.TestCase):
    """The chosen folder stays on one line: drive, "…", then as many trailing folders as fit."""

    def test_fitting_path_is_unchanged(self) -> None:
        self.assertEqual(elide_path("C:/Users/me/Downloads", 40, len), "C:/Users/me/Downloads")

    def test_middle_folders_are_elided(self) -> None:
        path = r"C:\Users\Alex\AppData\Local\Temp\scratchpad\watch folder"
        self.assertEqual(elide_path(path, 30, len), r"C:\…\scratchpad\watch folder")
        self.assertEqual(elide_path(path, 20, len), r"C:\…\watch folder")

    def test_unc_share_is_kept(self) -> None:
        path = r"\\nas\video\tv\incoming\watch"
        self.assertEqual(elide_path(path, 20, len), r"\\nas\video\…\watch")
        self.assertEqual(elide_path(path, 28, len), r"\\nas\video\…\incoming\watch")
        self.assertEqual(elide_path(path, 29, len), path)  # fits as it is

    def test_forward_slashes(self) -> None:
        self.assertEqual(elide_path("Z:/media/tv/incoming/watch", 16, len), "Z:/…/watch")

    def test_too_long_last_folder_is_elided_at_the_end(self) -> None:
        self.assertEqual(elide_path(r"C:\averyveryverylongfoldername", 12, len), r"C:\averyver…")
        self.assertEqual(
            elide_path(r"C:\Users\Alex\averyveryverylongfoldername", 20, len),
            r"C:\Users\Alex\avery…",
        )


class StatusTextTests(unittest.TestCase):
    def test_torrent_program_lines(self) -> None:
        found = torrent_look(TorrentStatus.FOUND, "qBittorrent")
        self.assertEqual((found.text, found.color), ("✓ qBittorrent found", theme.SUCCESS))
        self.assertFalse(found.install_button or found.download_link)

        missing = torrent_look(TorrentStatus.MISSING)
        self.assertEqual((missing.text, missing.color), ("No torrent program found", theme.WARNING))
        self.assertTrue(missing.install_button)
        self.assertFalse(missing.download_link)

        installing = torrent_look(TorrentStatus.INSTALLING)
        self.assertEqual(installing.text, "Installing… Windows may ask for permission")
        self.assertFalse(installing.install_button or installing.download_link)

        installed = torrent_look(TorrentStatus.INSTALLED)
        self.assertEqual((installed.text, installed.color), ("✓ qBittorrent installed", theme.SUCCESS))

        failed = torrent_look(TorrentStatus.FAILED)
        self.assertEqual(failed.text, "Couldn't install it automatically.")
        self.assertTrue(failed.download_link)
        self.assertFalse(failed.install_button)

        self.assertEqual(torrent_look(TorrentStatus.LOOKING).text, "")

    def test_key_check_lines(self) -> None:
        self.assertEqual(key_check_message(KeyCheck.OK), ("✓ The key works", theme.SUCCESS))
        text, color = key_check_message(KeyCheck.REJECTED)
        self.assertEqual(
            text,
            'That key didn\'t work. Check that you copied the "API Key" (not the long token).',
        )
        self.assertEqual(color, theme.DANGER)
        self.assertEqual(key_check_message(KeyCheck.UNREACHABLE)[1], theme.DANGER)
        self.assertEqual(key_check_message(KeyCheck.CHECKING)[1], theme.TEXT_MUTED)


if __name__ == "__main__":
    unittest.main()
