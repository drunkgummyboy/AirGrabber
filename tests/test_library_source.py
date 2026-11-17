"""Which sources the library has (app/services/library_source.py): Plex with a token, the
folders once switched on with a folder set (beside Plex too), the main source; the settings it
reads and the words for what you have."""

from __future__ import annotations

import unittest
from typing import cast

from app.models import Settings
from app.services.backup import MACHINE_FIELDS
from app.services.library_source import (
    folders_on,
    folders_only,
    has_library,
    have_text,
    library_source,
)
from app.services.settings import SettingsService
from app.ui import library_words

FOLDERS = {"film_folders": ("D:\\Films",), "series_folders": ("\\\\NAS\\Series",)}


class LibrarySourceTests(unittest.TestCase):
    def test_plex_is_the_main_source_and_the_folders_count_beside_it(self) -> None:
        # Two shelves (docs/folder-library/2026-11-16-alongside-plex-design.md): with a token
        # Plex stays the main source, and folders switched on count beside it.
        settings = Settings(plex_token="made-up-token", folder_library_on=True, **FOLDERS)
        self.assertEqual(library_source(settings), "plex")
        self.assertTrue(has_library(settings))
        self.assertTrue(folders_on(settings))
        self.assertFalse(folders_only(settings))
        plex_alone = Settings(plex_token="made-up-token", **FOLDERS)  # set, never switched on
        self.assertEqual(library_source(plex_alone), "plex")
        self.assertTrue(has_library(plex_alone))
        self.assertFalse(folders_on(plex_alone))
        self.assertFalse(folders_only(plex_alone))

    def test_folders_need_switching_on(self) -> None:
        settings = Settings(folder_library_on=False, **FOLDERS)
        self.assertIsNone(library_source(settings))
        self.assertFalse(has_library(settings))
        self.assertFalse(folders_on(settings))
        self.assertFalse(folders_only(settings))

    def test_folders_on_without_any_folder_is_none(self) -> None:
        for folders in ({}, {"film_folders": ("  ",), "series_folders": ("",)}):
            for token in ("", "made-up-token"):
                with self.subTest(folders=folders, token=token):
                    settings = Settings(plex_token=token, folder_library_on=True, **folders)
                    self.assertFalse(folders_on(settings))
                    self.assertFalse(folders_only(settings))
                    self.assertEqual(has_library(settings), bool(token))
                    self.assertEqual(library_source(settings), "plex" if token else None)

    def test_folders_on(self) -> None:
        for folders in (FOLDERS, {"film_folders": ("D:\\Films",)}, {"series_folders": ("S:\\",)}):
            with self.subTest(folders=folders):
                settings = Settings(folder_library_on=True, **folders)
                self.assertEqual(library_source(settings), "folders")
                self.assertTrue(has_library(settings))
                self.assertTrue(folders_on(settings))
                self.assertTrue(folders_only(settings))

    def test_nothing_set_is_no_library(self) -> None:
        self.assertIsNone(library_source(Settings()))
        self.assertFalse(has_library(Settings()))
        self.assertFalse(folders_on(Settings()))
        self.assertFalse(folders_only(Settings()))

    def test_have_text_names_the_source(self) -> None:
        self.assertEqual(have_text(Settings(plex_token="made-up-token")), "In Plex")
        self.assertEqual(
            have_text(Settings(folder_library_on=True, **FOLDERS)), "In your library"
        )
        # Beside Plex, what Plex has keeps Plex's words.
        self.assertEqual(
            have_text(Settings(plex_token="made-up-token", folder_library_on=True, **FOLDERS)),
            "In Plex",
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
        self.assertEqual(library_words.by_library("plex", "library"), "plex")

    def test_both_shelves_say_where(self) -> None:
        # Two shelves: Plex's stays "In Plex", the folders' own beside it "Downloaded…"; texts
        # about the whole library take its words whenever the folders count.
        self.follow(Settings(plex_token="made-up-token", folder_library_on=True, **FOLDERS))
        self.assertFalse(library_words.folders())  # part 1's words: the folders alone only
        self.assertEqual(library_words.have_source(), "plex")
        self.assertEqual(library_words.have_source(from_folders=True), "downloaded")
        self.assertEqual(
            library_words.have_tip("downloaded"), "Downloaded. Plex doesn't have it yet."
        )
        self.assertEqual(library_words.by_library("plex", "library"), "library")
        self.follow(Settings(plex_token="made-up-token"))
        self.assertEqual(library_words.by_library("plex", "library"), "plex")
        self.follow(Settings(folder_library_on=True, **FOLDERS))
        self.assertEqual(library_words.have_source(from_folders=True), "library")
        self.assertEqual(library_words.have_tip("library"), "In your library")
        self.assertEqual(library_words.by_library("plex", "library"), "library")


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
