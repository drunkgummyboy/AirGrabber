"""Settings dialog: plain quality names, the sidebar's sections, which section a validation error
opens, download-mode rows, the size entries' texts, and the dialog itself (sections, the quality
page, the TMDB key box) on a real (hidden) Tk root."""

from __future__ import annotations

import asyncio
import gc
import tempfile
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import customtkinter as ctk
from pydantic import ValidationError

from app.models import DownloadMode, ProwlarrState, ProwlarrStatus, QualityProfile, Settings
from app.services import SettingsService
from app.services.tmdb import TMDBError
from app.ui import theme
from app.ui.settings_dialog import (
    KEY_GUIDE_LINK,
    MOVIES_TEXT,
    QUALITY_NAMES,
    QUALITY_NOTE,
    SECTION_ADVANCED,
    SECTION_AUTOMATIC,
    SECTION_GROUPS,
    SECTION_INFO,
    SECTION_LOOKS,
    SECTION_QUALITY,
    SECTION_SUBTITLES,
    SECTION_UPDATES,
    SECTION_WHERE,
    SECTIONS,
    SERIES_TEXT,
    TMDB_KEY_LABEL,
    TMDB_KEY_TEXT,
    DownloadRows,
    SettingsDialog,
    _describe,
    _first_section,
    dialog_height,
    download_dir_to_save,
    download_rows,
    quality_from_name,
    quality_name,
    section_for_field,
    sidebar_width,
    size_text,
)


def validation_error(**fields: object) -> ValidationError:
    try:
        Settings.model_validate(fields)
    except ValidationError as exc:
        return exc
    raise AssertionError(f"{fields} validated")


class QualityNameTests(unittest.TestCase):
    def test_plain_names(self) -> None:
        self.assertEqual(quality_name(QualityProfile.FULL_HD), "Full HD (1080p)")
        self.assertEqual(quality_name(QualityProfile.HD), "HD (720p)")
        self.assertEqual(quality_name(QualityProfile.UHD), "4K (2160p)")
        self.assertEqual(quality_name(QualityProfile.HEVC), "Smaller files (1080p x265)")
        self.assertEqual(quality_name(QualityProfile.ANY), "Any quality")

    def test_menu_order(self) -> None:
        self.assertEqual(
            list(QUALITY_NAMES),
            [
                QualityProfile.FULL_HD,
                QualityProfile.HD,
                QualityProfile.UHD,
                QualityProfile.HEVC,
                QualityProfile.ANY,
            ],
        )

    def test_round_trip_keeps_the_stored_values(self) -> None:
        self.assertEqual(set(QUALITY_NAMES), set(QualityProfile))
        for profile in QualityProfile:
            with self.subTest(profile=profile):
                self.assertIs(quality_from_name(quality_name(profile)), profile)
                chosen = quality_from_name(quality_name(profile))
                stored = Settings(quality_tv=chosen, quality_movie=chosen).model_dump(mode="json")
                self.assertEqual(stored["quality_tv"], profile.value)
                self.assertEqual(stored["quality_movie"], profile.value)

    def test_names_are_unique(self) -> None:
        self.assertEqual(len(set(QUALITY_NAMES.values())), len(QUALITY_NAMES))

    def test_unknown_name(self) -> None:
        with self.assertRaises(ValueError):
            quality_from_name("1080p")


class SizeTextTests(unittest.TestCase):
    def test_what_the_entries_show(self) -> None:
        self.assertEqual(size_text(None), "")
        self.assertEqual(size_text(2.5), "2.5")
        self.assertEqual(size_text(4.0), "4")
        self.assertEqual(size_text(15), "15")

    def test_shown_text_validates_back(self) -> None:
        for value in (None, 0.7, 2.5, 4.0, 40.0):
            with self.subTest(value=value):
                settings = Settings(size_tv_min_gb=size_text(value))
                self.assertEqual(settings.size_tv_min_gb, value)


class SectionTests(unittest.TestCase):
    def test_the_sidebar_order(self) -> None:
        self.assertEqual(
            [(group, list(names)) for group, names in SECTION_GROUPS],
            [
                ("Downloading", ["Where downloads go", "Quality", "Automatic", "Subtitles"]),
                ("App", ["Movie & show info", "Looks", "Updates", "Advanced"]),
            ],
        )
        self.assertEqual(SECTIONS[0], SECTION_WHERE)

    def test_every_setting_has_a_section(self) -> None:
        expected = {
            "download_mode": SECTION_WHERE,
            "download_dir": SECTION_WHERE,
            "remote_url": SECTION_WHERE,
            "remote_kind": SECTION_WHERE,
            "quality_tv": SECTION_QUALITY,
            "quality_movie": SECTION_QUALITY,
            "size_tv_min_gb": SECTION_QUALITY,
            "size_movie_max_gb": SECTION_QUALITY,
            "keep_in_tray": SECTION_AUTOMATIC,
            "start_with_windows": SECTION_AUTOMATIC,
            "auto_download_new_shows": SECTION_AUTOMATIC,
            "subtitles_folder": SECTION_SUBTITLES,
            "opensubtitles_own_key": SECTION_SUBTITLES,
            "tmdb_api_key": SECTION_INFO,
            "card_size": SECTION_LOOKS,
            "first_day": SECTION_LOOKS,
            "auto_update": SECTION_UPDATES,
            "prowlarr_url": SECTION_ADVANCED,
            "excluded_keywords": SECTION_ADVANCED,
            "preferred_groups": SECTION_ADVANCED,
            "disabled_indexers": SECTION_ADVANCED,
        }
        for name, section in expected.items():
            with self.subTest(name=name):
                self.assertIn(name, Settings.model_fields)
                self.assertEqual(section_for_field(name), section)


class DownloadRowsTests(unittest.TestCase):
    def test_torrent_program_shows_its_status_only(self) -> None:
        self.assertEqual(
            download_rows(DownloadMode.APP),
            DownloadRows(torrent_status=True, remote_form=False, folder=False),
        )

    def test_remote_program_shows_its_form_only(self) -> None:
        self.assertEqual(
            download_rows(DownloadMode.REMOTE),
            DownloadRows(torrent_status=False, remote_form=True, folder=False),
        )

    def test_folder_shows_the_folder_only(self) -> None:
        self.assertEqual(
            download_rows(DownloadMode.FOLDER),
            DownloadRows(torrent_status=False, remote_form=False, folder=True),
        )


class DownloadDirTests(unittest.TestCase):
    STORED = Path(r"D:\Watch")

    def test_folder_mode_saves_the_entry(self) -> None:
        self.assertEqual(
            download_dir_to_save(DownloadMode.FOLDER, r"  E:\New  ", self.STORED), r"E:\New"
        )

    def test_folder_mode_still_validates_an_empty_entry(self) -> None:
        self.assertEqual(download_dir_to_save(DownloadMode.FOLDER, "  ", self.STORED), "")

    def test_torrent_program_mode_keeps_the_stored_folder(self) -> None:
        for entry in ("", "   ", r"E:\Typed but hidden"):
            with self.subTest(entry=entry):
                kept = download_dir_to_save(DownloadMode.APP, entry, self.STORED)
                self.assertEqual(kept, self.STORED)
                # A hidden, emptied entry no longer blocks saving.
                settings = Settings(download_mode=DownloadMode.APP, download_dir=kept)
                self.assertEqual(settings.download_dir, self.STORED)


class DialogHeightTests(unittest.TestCase):
    WANTED = 785  # the tallest page, Save/Cancel and padding at full size

    def test_1080p_at_150_percent(self) -> None:
        self.assertEqual(dialog_height(self.WANTED, 720), 640)

    def test_1366x768_at_100_percent(self) -> None:
        self.assertEqual(dialog_height(self.WANTED, 768), 688)

    def test_1366x768_at_150_percent(self) -> None:
        self.assertEqual(dialog_height(self.WANTED, 512), 432)

    def test_never_taller_than_its_content(self) -> None:
        self.assertEqual(dialog_height(self.WANTED, 1067), self.WANTED)
        self.assertEqual(dialog_height(self.WANTED, self.WANTED + 80), self.WANTED)


class ValidationErrorTests(unittest.TestCase):
    def test_errors_open_their_section(self) -> None:
        cases: list[tuple[dict[str, object], str]] = [
            ({"download_dir": " "}, SECTION_WHERE),
            ({"download_mode": "cloud"}, SECTION_WHERE),
            ({"remote_url": "nas.local"}, SECTION_WHERE),  # the NAS / other computer form
            ({"remote_kind": "utorrent"}, SECTION_WHERE),
            ({"remote_verify_tls": "maybe"}, SECTION_WHERE),
            ({"quality_tv": "720"}, SECTION_QUALITY),
            ({"quality_movie": "720"}, SECTION_QUALITY),
            ({"size_tv_min_gb": "lots"}, SECTION_QUALITY),
            ({"size_movie_min_gb": 9, "size_movie_max_gb": 3}, SECTION_QUALITY),
            ({"first_day": "Someday"}, SECTION_LOOKS),
            ({"card_size": "xxl"}, SECTION_LOOKS),
            ({"prowlarr_url": "localhost:9696"}, SECTION_ADVANCED),
            ({"excluded_keywords": 5}, SECTION_ADVANCED),
            ({"preferred_groups": 5}, SECTION_ADVANCED),
            ({"disabled_indexers": 5}, SECTION_ADVANCED),
            ({"keep_in_tray": "maybe"}, SECTION_AUTOMATIC),
            ({"auto_update": "maybe"}, SECTION_UPDATES),
            ({"subtitles_enabled": "maybe"}, SECTION_SUBTITLES),
        ]
        for fields, section in cases:
            with self.subTest(fields=fields):
                self.assertEqual(_first_section(validation_error(**fields)), section)

    def test_plain_labels(self) -> None:
        self.assertEqual(
            _describe(validation_error(download_dir="")),
            "Save .torrent files to a folder: Choose a folder",
        )
        self.assertEqual(
            _describe(validation_error(remote_url="nas.local")),
            "Address: Use a full address like https://nas.local:5001",
        )
        self.assertTrue(_describe(validation_error(remote_kind="x")).startswith("Program: "))
        self.assertTrue(
            _describe(validation_error(excluded_keywords=5)).startswith(
                "Skip releases with these words: "
            )
        )
        self.assertTrue(
            _describe(validation_error(quality_tv="x")).startswith("Series quality: ")
        )
        self.assertTrue(
            _describe(validation_error(quality_movie="x")).startswith("Movies quality: ")
        )
        self.assertEqual(
            _describe(validation_error(size_tv_min_gb="lots")),
            "Series size: Use a number, like 2.5",
        )
        self.assertEqual(
            _describe(validation_error(size_movie_min_gb=9, size_movie_max_gb="3")),
            "Movies size: The first number can't be bigger than the second",
        )
        self.assertTrue(
            _describe(validation_error(tmdb_api_key=5)).startswith(f"{TMDB_KEY_LABEL}: ")
        )

    def test_the_first_error_decides(self) -> None:
        error = validation_error(prowlarr_url="nas.local", download_dir="")
        self.assertEqual(len(error.errors()), 2)
        first = str(error.errors()[0]["loc"][0])
        expected = SECTION_ADVANCED if first == "prowlarr_url" else SECTION_WHERE
        self.assertEqual(_first_section(error), expected)


# -- the dialog --------------------------------------------------------------------------------


class FakeRepository:
    async def save(self, settings: Settings) -> None:
        pass


class FakeProwlarr:
    async def status(self) -> ProwlarrStatus:
        return ProwlarrStatus(state=ProwlarrState.NOT_INSTALLED)


class FakeShortcut:
    def is_enabled(self) -> bool:
        return False

    async def set_enabled(self, on: bool) -> None:
        raise AssertionError("not changed here")


def labels(widget: Any) -> list[str]:
    found: list[str] = []
    for child in widget.winfo_children():
        if isinstance(child, ctk.CTkLabel):
            found.append(str(child.cget("text")))
        found += labels(child)
    return found


class DialogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.folder = Path(tmp.name)
        self.discovery = mock.MagicMock(check_tmdb_key=mock.AsyncMock(return_value=True))

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0  # building a dialog is slow

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()  # collect the dialog's Tk variables on the main thread

    def dialog(self, **values: Any) -> tuple[SettingsDialog, SettingsService]:
        settings = SettingsService(
            FakeRepository(),  # type: ignore[arg-type]
            Settings(download_dir=self.folder, **values),
        )
        dialog = SettingsDialog(
            self.root,
            settings,
            search=mock.MagicMock(),
            station=mock.MagicMock(),
            updates=SimpleNamespace(current="2.0.4"),  # type: ignore[arg-type]
            torrent_app=mock.MagicMock(find=mock.AsyncMock(return_value=None)),
            discovery=self.discovery,
            prowlarr=FakeProwlarr(),  # type: ignore[arg-type]
            startup_shortcut=FakeShortcut(),
        )
        self.addCleanup(self._close, dialog)
        return dialog, settings

    @staticmethod
    def _close(dialog: SettingsDialog) -> None:
        if dialog.winfo_exists():
            dialog.destroy()

    async def test_opens_on_where_downloads_go_and_switches_pages(self) -> None:
        dialog, _settings = self.dialog()
        self.assertEqual(dialog.section, SECTION_WHERE)
        self.assertTrue(dialog.page(SECTION_WHERE).winfo_manager())
        # Without the subtitle service there is no Subtitles section.
        self.assertEqual(
            list(dialog._items), [s for s in SECTIONS if s != SECTION_SUBTITLES]
        )
        dialog._items[SECTION_LOOKS].click()
        self.assertEqual(dialog.section, SECTION_LOOKS)
        self.assertTrue(dialog._items[SECTION_LOOKS].selected)
        self.assertFalse(dialog._items[SECTION_WHERE].selected)
        self.assertTrue(dialog.page(SECTION_LOOKS).winfo_manager())
        self.assertFalse(dialog.page(SECTION_WHERE).winfo_manager())

    async def test_every_page_has_a_heading_and_a_muted_line(self) -> None:
        dialog, _settings = self.dialog()
        for section in dialog._items:
            with self.subTest(section=section):
                texts = labels(dialog.page(section))
                self.assertEqual(texts[0], section)
                self.assertTrue(texts[1])

    async def test_fits_a_1366x768_screen_at_150_percent(self) -> None:
        dialog, _settings = self.dialog()
        dialog.update_idletasks()
        scaling = ctk.ScalingTracker.get_window_scaling(dialog)
        width = dialog.winfo_reqwidth() / scaling
        self.assertLessEqual(width, 1366 / 1.5 - 20)  # room for the window's own edges
        self.assertGreaterEqual(sidebar_width(), 220)

    async def test_the_quality_page(self) -> None:
        dialog, settings = self.dialog(
            quality_tv="720p", quality_movie="2160p", size_movie_min_gb=6, size_movie_max_gb=15
        )
        texts = labels(dialog.page(SECTION_QUALITY))
        for text in ("Series", SERIES_TEXT, "Movies", MOVIES_TEXT, QUALITY_NOTE, "between", "GB"):
            self.assertIn(text, texts)
        self.assertEqual(dialog._quality_tv.get(), "HD (720p)")
        self.assertEqual(dialog._quality_movie.get(), "4K (2160p)")
        self.assertEqual((dialog._size_tv_min.get(), dialog._size_tv_max.get()), ("", ""))
        self.assertEqual((dialog._size_movie_min.get(), dialog._size_movie_max.get()), ("6", "15"))
        dialog._quality_tv.set("Full HD (1080p)")
        dialog._size_tv_min.set("2,5")
        dialog._size_tv_max.set("4")
        dialog._size_movie_max.set("")
        await dialog._save()
        current = settings.current
        self.assertIs(current.quality_tv, QualityProfile.FULL_HD)
        self.assertIs(current.quality_movie, QualityProfile.UHD)
        self.assertEqual((current.size_tv_min_gb, current.size_tv_max_gb), (2.5, 4.0))
        self.assertEqual((current.size_movie_min_gb, current.size_movie_max_gb), (6.0, None))
        self.assertFalse(dialog.winfo_exists())

    async def test_a_bad_size_opens_the_quality_page(self) -> None:
        dialog, settings = self.dialog()
        dialog._size_tv_min.set("5")
        dialog._size_tv_max.set("3")
        await dialog._save()
        self.assertTrue(dialog.winfo_exists())
        self.assertEqual(dialog.section, SECTION_QUALITY)
        self.assertEqual(
            dialog._error.cget("text"),
            "Series size: The first number can't be bigger than the second",
        )
        self.assertIsNone(settings.current.size_tv_max_gb)

    async def test_the_tmdb_key_box(self) -> None:
        dialog, settings = self.dialog(tmdb_api_key="old")
        texts = labels(dialog.page(SECTION_INFO))
        for text in (TMDB_KEY_LABEL, TMDB_KEY_TEXT, KEY_GUIDE_LINK):
            self.assertIn(text, texts)
        self.assertEqual(dialog._tmdb_check.cget("text"), "Check")
        self.assertNotIn("Movies key", texts)
        self.assertEqual(dialog._tmdb_entry.cget("show"), "•")
        dialog._tmdb_key.set("  newkey  ")
        dialog._check_tmdb_key()
        self.assertEqual(dialog._tmdb_result.cget("text"), "Checking…")
        await dialog._tests["tmdb"]
        self.discovery.check_tmdb_key.assert_awaited_once_with("newkey")
        self.assertEqual(dialog._tmdb_result.cget("text"), "✓ The key works")
        self.assertEqual(dialog._tmdb_result.cget("text_color"), theme.SUCCESS)
        dialog._tmdb_key.set("other")  # an edit forgets the result
        self.assertFalse(dialog._tmdb_result.winfo_manager())
        await dialog._save()
        self.assertEqual(settings.current.tmdb_api_key.get_secret_value(), "other")

    async def test_a_refused_or_unreachable_key(self) -> None:
        dialog, _settings = self.dialog()
        self.discovery.check_tmdb_key.return_value = False
        dialog._tmdb_key.set("wrong")
        dialog._check_tmdb_key()
        await dialog._tests["tmdb"]
        self.assertIn("didn't work", dialog._tmdb_result.cget("text"))
        self.discovery.check_tmdb_key.side_effect = TMDBError("TMDB did not answer.")
        dialog._tmdb_key.set("again")
        with self.assertLogs("app.ui.settings_dialog", "INFO") as logs:
            dialog._check_tmdb_key()
            await dialog._tests["tmdb"]
        self.assertIn("Couldn't reach", dialog._tmdb_result.cget("text"))
        self.assertNotIn("again", "\n".join(logs.output))  # never the key

    async def test_an_empty_key_is_not_checked(self) -> None:
        dialog, _settings = self.dialog()
        dialog._check_tmdb_key()
        self.assertNotIn("tmdb", dialog._tests)
        self.discovery.check_tmdb_key.assert_not_awaited()

    async def test_a_key_saved_by_the_guide_shows_as_working(self) -> None:
        dialog, _settings = self.dialog()
        dialog._key_saved("fromguide")
        self.assertEqual(dialog._tmdb_key.get(), "fromguide")
        self.assertEqual(dialog._tmdb_result.cget("text"), "✓ The key works")

    async def test_the_card_size_and_week_on_looks(self) -> None:
        dialog, settings = self.dialog()
        self.assertEqual(dialog._card_size.get(), "M")  # the default
        texts = labels(dialog.page(SECTION_LOOKS))
        self.assertLess(texts.index("Card size"), texts.index("Week starts on"))
        dialog._card_size.set("XL")
        dialog._first_day.set("Sunday")
        await dialog._save()
        self.assertEqual(settings.current.card_size.value, "xl")
        self.assertEqual(settings.current.first_day.value, "Sunday")

    async def test_updates_page_shows_the_version(self) -> None:
        dialog, settings = self.dialog(auto_update=True)
        self.assertIn("Version 2.0.4", labels(dialog.page(SECTION_UPDATES)))
        dialog._auto_update.set(False)
        await dialog._save()
        self.assertFalse(settings.current.auto_update)


if __name__ == "__main__":
    unittest.main()
