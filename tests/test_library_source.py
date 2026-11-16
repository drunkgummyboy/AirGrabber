"""Which source is the library (app/services/library_source.py): Plex with a token, else the
folder library once switched on with a folder set, else none; the settings it reads and the
words for what you have."""

from __future__ import annotations

import unittest
from typing import cast

from app.models import Settings
from app.services.backup import MACHINE_FIELDS
from app.services.library_source import has_library, have_text, library_source
from app.services.settings import SettingsService
from app.ui import library_words

FOLDERS = {"film_folders": ("D:\\Films",), "series_folders": ("\\\\NAS\\Series",)}


class LibrarySourceTests(unittest.TestCase):
    def test_plex_wins(self) -> None:
        settings = Settings(plex_token="made-up-token", folder_library_on=True, **FOLDERS)
        self.assertEqual(library_source(settings), "plex")
        self.assertTrue(has_library(settings))

    def test_folders_need_switching_on(self) -> None:
        settings = Settings(folder_library_on=False, **FOLDERS)
        self.assertIsNone(library_source(settings))
        self.assertFalse(has_library(settings))

    def test_folders_on_without_any_folder_is_none(self) -> None:
        for folders in ({}, {"film_folders": ("  ",), "series_folders": ("",)}):
            with self.subTest(folders=folders):
                settings = Settings(folder_library_on=True, **folders)
                self.assertIsNone(library_source(settings))
                self.assertFalse(has_library(settings))

    def test_folders_on(self) -> None:
        for folders in (FOLDERS, {"film_folders": ("D:\\Films",)}, {"series_folders": ("S:\\",)}):
            with self.subTest(folders=folders):
                settings = Settings(folder_library_on=True, **folders)
                self.assertEqual(library_source(settings), "folders")
                self.assertTrue(has_library(settings))

    def test_nothing_set_is_no_library(self) -> None:
        self.assertIsNone(library_source(Settings()))
        self.assertFalse(has_library(Settings()))

    def test_have_text_names_the_source(self) -> None:
        self.assertEqual(have_text(Settings(plex_token="made-up-token")), "In Plex")
        self.assertEqual(
            have_text(Settings(folder_library_on=True, **FOLDERS)), "In your library"
        )


class _Settings:
    def __init__(self, settings: Settings) -> None:
        self.current = settings


class LibraryWordsTests(unittest.TestCase):
    """The UI's words for what you have follow the source (app/ui/library_words.py)."""

    def follow(self, settings: Settings) -> None:
        library_words.follow(cast(SettingsService, _Settings(settings)))
        self.addCleanup(library_words.follow, None)

    def test_plexs_words_without_settings(self) -> None:
        self.assertFalse(library_words.folders())
        self.assertEqual(library_words.have_tip(), "In Plex")
        self.assertEqual(library_words.by_source("plex", "folders"), "plex")

    def test_plexs_words_with_plex(self) -> None:
        self.follow(Settings(plex_token="made-up-token", folder_library_on=True, **FOLDERS))
        self.assertEqual(library_words.have_tip(), "In Plex")
        self.assertEqual(library_words.by_source("plex", "folders"), "plex")

    def test_the_folder_librarys_words(self) -> None:
        self.follow(Settings(folder_library_on=True, **FOLDERS))
        self.assertTrue(library_words.folders())
        self.assertEqual(library_words.have_tip(), "In your library")
        self.assertEqual(library_words.by_source("plex", "folders"), "folders")

    def test_no_library_keeps_plexs_words(self) -> None:
        self.follow(Settings(**FOLDERS))  # set up, not switched on
        self.assertEqual(library_words.have_tip(), "In Plex")


class FolderSettingsTests(unittest.TestCase):
    def test_defaults_are_off_and_empty(self) -> None:
        settings = Settings()
        self.assertFalse(settings.folder_library_on)
        self.assertEqual(settings.film_folders, ())
        self.assertEqual(settings.series_folders, ())

    def test_they_round_trip(self) -> None:
        settings = Settings(folder_library_on=True, **FOLDERS)
        self.assertEqual(Settings.model_validate(settings.model_dump(mode="json")), settings)

    def test_they_are_this_pcs_own(self) -> None:
        # A backup loaded on another PC keeps that PC's folders.
        for name in ("folder_library_on", "film_folders", "series_folders"):
            self.assertIn(name, MACHINE_FIELDS)


if __name__ == "__main__":
    unittest.main()
