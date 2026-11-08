"""Settings' search: what it looks through, how it matches, and the box in the real window."""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

import customtkinter as ctk

from app.ui.settings_dialog import SECTION_ADVANCED, SECTION_LOOKS
from app.ui.settings_search import Match, Place, places, result_text, search
from tests.test_settings_dialog import DialogCase

PLACES = [
    Place(Match("Looks", "", None), "Looks"),
    Place(Match("Looks", "Theme", "frame-a"), "Looks Theme Follow Windows Dark Light"),
    Place(Match("Advanced", "Skip releases with these words", "frame-b"), "Advanced Skip releases cam"),
    Place(Match("Naming", "", None), "Naming Presets Trailer Poster"),
]


class SearchTests(unittest.TestCase):
    def test_every_word_has_to_be_there_ignoring_case(self) -> None:
        self.assertEqual([m.title for m in search("skip CAM", PLACES)], ["Skip releases with these words"])
        self.assertEqual(search("skip plex", PLACES), [])
        self.assertEqual(search("   ", PLACES), [])

    def test_a_box_comes_before_its_page(self) -> None:
        self.assertEqual([m.title for m in search("looks", PLACES)], ["Theme", ""])

    def test_a_page_not_built_yet_is_found_by_its_words(self) -> None:
        [match] = search("trailer", PLACES)
        self.assertEqual((match.section, match.title, match.frame), ("Naming", "", None))

    def test_the_result_names_its_page(self) -> None:
        self.assertEqual(result_text(Match("Looks", "Theme", None)), "Theme · Looks")
        self.assertEqual(result_text(Match("Looks", "", None)), "Looks")

    def test_places_read_the_words_inside_a_box(self) -> None:
        root = ctk.CTk()
        self.addCleanup(root.destroy)
        box = ctk.CTkFrame(root)
        ctk.CTkCheckBox(box, text="Download trailers").pack()
        ctk.CTkLabel(ctk.CTkFrame(box), text="Nested words").pack()
        everything = places({"Naming": [("Presets", box)]}, {"Subtitles": ["Languages"]})
        words = {p.match.title or p.match.section: p.words for p in everything}
        self.assertIn("Download trailers", words["Presets"])
        self.assertIn("Languages", words["Subtitles"])


class SettingsWindowSearchTests(DialogCase):
    async def test_typing_finds_a_setting_and_a_pick_goes_there(self) -> None:
        dialog, _settings = self.dialog()
        box: Any = dialog.search_box
        box.entry.insert(0, "skip releases")
        box.refresh()
        self.assertTrue(box.matches)
        self.assertEqual(box.matches[0].section, SECTION_ADVANCED)
        box._pick_first()
        await asyncio.sleep(0.05)
        self.assertEqual(dialog.section, SECTION_ADVANCED)

    async def test_a_page_title_is_found_too_and_escape_clears(self) -> None:
        dialog, _settings = self.dialog()
        box: Any = dialog.search_box
        box.entry.insert(0, "looks")
        box.refresh()
        self.assertIn(SECTION_LOOKS, [m.section for m in box.matches])
        box._clear()
        self.assertEqual(box.matches, [])
        self.assertEqual(box.entry.get(), "")

    async def test_nothing_found_says_so(self) -> None:
        dialog, _settings = self.dialog()
        box: Any = dialog.search_box
        box.entry.insert(0, "zzzz nothing like this")
        box.refresh()
        self.assertEqual(box.matches, [])
