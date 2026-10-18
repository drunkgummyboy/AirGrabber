"""The airdates.tv import panel's decisions and texts (pure helpers; no Tk)."""

from __future__ import annotations

import unittest

from app.models import AirdatesPreview, ImportMatch, ImportResult
from app.ui.airdates_import import (
    from_note,
    import_label,
    import_notes,
    imported_text,
    importing_text,
    looking_up_text,
    nothing_reached,
    review_heading,
    review_notes,
    ticked_by_default,
)

FOUND = ImportMatch(source_name="Severance", show_id=1, show_name="Severance", year=2022)
TRACKED = ImportMatch(
    source_name="Ghosts", show_id=2, show_name="Ghosts", year=2021, already_tracked=True
)
MISSING = ImportMatch(source_name="Nothing Matches")


class TickTests(unittest.TestCase):
    def test_found_shows_start_ticked(self) -> None:
        self.assertTrue(ticked_by_default(FOUND))

    def test_already_followed_and_missing_ones_do_not(self) -> None:
        self.assertFalse(ticked_by_default(TRACKED))
        self.assertFalse(ticked_by_default(MISSING))


class FromNoteTests(unittest.TestCase):
    def note(self, source: str, tvmaze: str) -> str | None:
        return from_note(ImportMatch(source_name=source, show_id=1, show_name=tvmaze))

    def test_same_name_has_no_note(self) -> None:
        self.assertIsNone(self.note("Severance", "Severance"))

    def test_case_and_punctuation_are_ignored(self) -> None:
        self.assertIsNone(
            self.note(
                "Re:Zero − Starting Life in Another World",
                "Re:ZERO -Starting Life in Another World-",
            )
        )
        self.assertIsNone(self.note("Greys Anatomy", "Grey's Anatomy"))
        self.assertIsNone(self.note("shakespeare & hathaway", "Shakespeare & Hathaway"))

    def test_different_name_shows_the_source(self) -> None:
        self.assertEqual(
            self.note("The Traitors (US)", "The Traitors"), ' · from "The Traitors (US)"'
        )
        self.assertEqual(self.note("Monk", "Mr. Monk"), ' · from "Monk"')

    def test_no_note_without_a_match(self) -> None:
        self.assertIsNone(from_note(MISSING))


class TextTests(unittest.TestCase):
    def test_heading_counts_everything_in_the_export(self) -> None:
        preview = AirdatesPreview(
            matches=(FOUND, TRACKED, MISSING), unknown_ids=(7, 8), failed=1
        )
        self.assertEqual(review_heading(preview), "Found 2 of 6 shows")
        single = AirdatesPreview(matches=(FOUND,))
        self.assertEqual(review_heading(single), "Found 1 of 1 show")

    def test_notes(self) -> None:
        other = ImportMatch(source_name="Also Missing")
        preview = AirdatesPreview(
            matches=(FOUND, MISSING, other), unknown_ids=(7, 8), failed=3
        )
        self.assertEqual(
            review_notes(preview),
            [
                "Couldn't find: Nothing Matches, Also Missing",
                "airdates.tv didn't recognise 2 shows",
                "3 couldn't be looked up; try again later",
            ],
        )
        self.assertEqual(
            review_notes(AirdatesPreview(matches=(FOUND,), unknown_ids=(7,))),
            ["airdates.tv didn't recognise 1 show"],
        )
        self.assertEqual(review_notes(AirdatesPreview(matches=(FOUND, TRACKED))), [])

    def test_nothing_reached(self) -> None:
        self.assertTrue(nothing_reached(AirdatesPreview(failed=4)))
        self.assertFalse(nothing_reached(AirdatesPreview(matches=(FOUND,), failed=3)))
        self.assertFalse(nothing_reached(AirdatesPreview(unknown_ids=(1,), failed=3)))
        self.assertFalse(nothing_reached(AirdatesPreview()))

    def test_looking_up_counts_shows_not_lookups(self) -> None:
        # find() reports two lookups per show (airdates.tv, then TVMaze): total = 2 x 37.
        self.assertEqual(looking_up_text(0, 74), "Looking up 1 of 37…")
        self.assertEqual(looking_up_text(23, 74), "Looking up 12 of 37…")
        self.assertEqual(looking_up_text(74, 74), "Looking up 37 of 37…")

    def test_importing(self) -> None:
        self.assertEqual(importing_text(0, 34), "Importing 1 of 34…")
        self.assertEqual(importing_text(2, 34), "Importing 3 of 34…")
        self.assertEqual(importing_text(34, 34), "Importing 34 of 34…")

    def test_import_button_label(self) -> None:
        self.assertEqual(import_label(0), "Import 0 shows")
        self.assertEqual(import_label(1), "Import 1 show")
        self.assertEqual(import_label(34), "Import 34 shows")

    def test_after_import(self) -> None:
        self.assertEqual(imported_text(3), "Imported 3 shows")
        self.assertEqual(imported_text(1), "Imported 1 show")
        self.assertEqual(import_notes(ImportResult(added=("A",))), "")
        self.assertEqual(
            import_notes(ImportResult(added=("A",), failed=("2", "3"))),
            "2 couldn't be imported; try again later",
        )


if __name__ == "__main__":
    unittest.main()
