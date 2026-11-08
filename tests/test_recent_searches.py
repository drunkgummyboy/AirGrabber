"""Recent searches: kept newest first and once each, listed while the search box is empty."""

from __future__ import annotations

import unittest

from app.models import Settings
from app.services.backup import MACHINE_FIELDS
from app.ui.search import CLEAR_RECENT, RECENT_MAX, recent_rows, remember_search


class RememberTests(unittest.TestCase):
    def test_newest_first_once_each_and_not_too_many(self) -> None:
        recent = remember_search((), "Reacher")
        recent = remember_search(recent, "The Ark")
        recent = remember_search(recent, "reacher ")  # the same search again
        self.assertEqual(recent, ("reacher", "The Ark"))
        for n in range(20):
            recent = remember_search(recent, f"show {n}")
        self.assertEqual(len(recent), RECENT_MAX)
        self.assertEqual(recent[0], "show 19")
        self.assertEqual(remember_search(recent, "   "), recent)

    def test_the_rows_end_with_clear(self) -> None:
        rows = recent_rows(("Reacher", "The Ark"))
        self.assertEqual([target for _text, target in rows[:2]], ["Reacher", "The Ark"])
        self.assertIn("Reacher", rows[0][0])
        self.assertEqual(rows[-1][0], CLEAR_RECENT)
        self.assertEqual(recent_rows(()), [])

    def test_they_stay_on_this_pc(self) -> None:
        self.assertIn("recent_searches", MACHINE_FIELDS)
        self.assertEqual(Settings().recent_searches, ())
