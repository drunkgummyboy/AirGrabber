"""Settings dialog: plain quality names, which tab a validation error opens, download-mode rows."""

from __future__ import annotations

import unittest
from pathlib import Path

from pydantic import ValidationError

from app.models import DownloadMode, QualityProfile, Settings
from app.ui.settings_dialog import (
    QUALITY_NAMES,
    TAB_ADVANCED,
    TAB_GENERAL,
    DownloadRows,
    _describe,
    _first_tab,
    dialog_height,
    download_dir_to_save,
    download_rows,
    quality_from_name,
    quality_name,
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
                stored = Settings(quality=quality_from_name(quality_name(profile)))
                self.assertEqual(stored.model_dump(mode="json")["quality"], profile.value)

    def test_names_are_unique(self) -> None:
        self.assertEqual(len(set(QUALITY_NAMES.values())), len(QUALITY_NAMES))

    def test_unknown_name(self) -> None:
        with self.assertRaises(ValueError):
            quality_from_name("1080p")


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
    WANTED = 785  # General's content, Save/Cancel and padding at full size

    def test_1080p_at_150_percent(self) -> None:
        self.assertEqual(dialog_height(self.WANTED, 720), 640)

    def test_1366x768_at_100_percent(self) -> None:
        self.assertEqual(dialog_height(self.WANTED, 768), 688)

    def test_never_taller_than_its_content(self) -> None:
        self.assertEqual(dialog_height(self.WANTED, 1067), self.WANTED)
        self.assertEqual(dialog_height(self.WANTED, self.WANTED + 80), self.WANTED)


class ValidationErrorTests(unittest.TestCase):
    def test_general_fields_open_general(self) -> None:
        for fields in (
            {"download_dir": " "},
            {"quality": "720"},
            {"first_day": "Someday"},
            {"download_mode": "cloud"},
            {"remote_url": "nas.local"},  # the NAS / other computer form is on General now
            {"remote_kind": "utorrent"},
            {"remote_verify_tls": "maybe"},
        ):
            with self.subTest(fields=fields):
                self.assertEqual(_first_tab(validation_error(**fields)), TAB_GENERAL)

    def test_advanced_fields_open_advanced(self) -> None:
        for fields in (
            {"prowlarr_url": "localhost:9696"},
            {"excluded_keywords": 5},
            {"preferred_groups": 5},
            {"disabled_indexers": 5},
        ):
            with self.subTest(fields=fields):
                self.assertEqual(_first_tab(validation_error(**fields)), TAB_ADVANCED)

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
        self.assertTrue(_describe(validation_error(quality="x")).startswith("Preferred quality: "))

    def test_the_first_error_decides(self) -> None:
        error = validation_error(prowlarr_url="nas.local", download_dir="")
        self.assertEqual(len(error.errors()), 2)
        first = str(error.errors()[0]["loc"][0])
        expected = TAB_ADVANCED if first == "prowlarr_url" else TAB_GENERAL
        self.assertEqual(_first_tab(error), expected)


if __name__ == "__main__":
    unittest.main()
