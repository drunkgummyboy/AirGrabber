"""What a rename drops and adds, word by word, and how the queue's lines show it."""

from __future__ import annotations

import unittest

from app.forge.ui.name_diff import name_changes, runs


class NameChangesTests(unittest.TestCase):
    def test_an_episodes_junk_is_dropped(self) -> None:
        old = "Gold.Rush.S17E06.1080p.WEB.H264-CAKES.mkv"
        new = "Gold Rush (2010) - S17E06 - Rivers of Gold.mkv"
        dropped, added = name_changes(old, new)
        self.assertEqual([old[s:e] for s, e in dropped], ["1080p", "WEB", "H264", "CAKES"])
        self.assertEqual([new[s:e] for s, e in added], ["2010", "Rivers", "of"])

    def test_case_and_separators_dont_count(self) -> None:
        dropped, added = name_changes("the.office.s01e01.mkv", "The Office - S01E01.mkv")
        self.assertEqual((dropped, added), ((), ()))

    def test_runs_join_a_dropped_stretch(self) -> None:
        old = "Dune.2021.1080p.WEB.mkv"
        dropped, _added = name_changes(old, "Dune (2021).mkv")
        self.assertEqual(
            runs(old, dropped), [("Dune.2021.", False), ("1080p.WEB", True), (".mkv", False)]
        )

    def test_runs_without_marks_are_the_text(self) -> None:
        self.assertEqual(runs("Dune (2021).mkv", ()), [("Dune (2021).mkv", False)])
        self.assertEqual("".join(p for p, _m in runs("a.b.c", ((0, 1), (4, 5)))), "a.b.c")
