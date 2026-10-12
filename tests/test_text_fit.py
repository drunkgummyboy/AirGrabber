"""Fitting text into a width: eliding, word wrap within a line limit, title breaks."""

from __future__ import annotations

import unittest

from app.ui.text_fit import elide_to_width, fit_to_lines, title_lines


def measure(text: str) -> float:
    """A fake font: 7 px per character, the ellipsis included."""
    return 7.0 * len(text)


class ElideToWidthTests(unittest.TestCase):
    def test_text_that_fits_is_unchanged(self) -> None:
        self.assertEqual(elide_to_width("Severance", 63, measure), "Severance")  # exactly 63
        self.assertEqual(elide_to_width("Severance", 500, measure), "Severance")
        self.assertEqual(elide_to_width("", 0, measure), "")

    def test_longest_prefix_that_fits_with_the_ellipsis(self) -> None:
        self.assertEqual(elide_to_width("Severance", 62, measure), "Severan…")  # 8 chars = 56
        self.assertEqual(elide_to_width("Severance", 56, measure), "Severan…")
        self.assertEqual(elide_to_width("Severance", 55, measure), "Severa…")

    def test_never_ends_in_a_space_before_the_ellipsis(self) -> None:
        # "Ted " + "…" would fit (35), but the trailing space is dropped.
        self.assertEqual(elide_to_width("Ted Lasso", 35, measure), "Ted…")

    def test_ellipsis_alone_when_no_character_fits(self) -> None:
        self.assertEqual(elide_to_width("Severance", 7, measure), "…")
        self.assertEqual(elide_to_width("Severance", 13.9, measure), "…")

    def test_empty_when_not_even_the_ellipsis_fits(self) -> None:
        self.assertEqual(elide_to_width("Severance", 6.9, measure), "")
        self.assertEqual(elide_to_width("Severance", 0, measure), "")
        self.assertEqual(elide_to_width("Severance", -10, measure), "")

    def test_binary_search_measures_few_candidates(self) -> None:
        calls: list[str] = []

        def counting(text: str) -> float:
            calls.append(text)
            return measure(text)

        result = elide_to_width("x" * 1000, 70, counting)
        self.assertEqual(result, "x" * 9 + "…")
        self.assertLess(len(calls), 20)


class FitToLinesTests(unittest.TestCase):
    def test_text_that_wraps_into_the_lines_is_unchanged(self) -> None:
        # 49 px = 7 characters a line: "aaa bbb" / "ccc ddd" / "eee"
        self.assertEqual(fit_to_lines("aaa bbb ccc ddd eee", 49, 3, measure), "aaa bbb ccc ddd eee")
        self.assertEqual(fit_to_lines("Lost", 49, 1, measure), "Lost")

    def test_last_line_is_elided_when_more_lines_are_needed(self) -> None:
        self.assertEqual(fit_to_lines("aaa bbb ccc ddd eee", 49, 2, measure), "aaa bbb\nccc dd…")
        self.assertEqual(fit_to_lines("aaa bbb ccc ddd eee", 49, 1, measure), "aaa bb…")

    def test_long_word_is_broken_across_lines(self) -> None:
        self.assertEqual(fit_to_lines("abcdefghijkl", 35, 2, measure), "abcde\nfghi…")
        self.assertEqual(fit_to_lines("abcdefghij", 35, 2, measure), "abcdefghij")

    def test_real_name_on_three_lines(self) -> None:
        name = "Law & Order: Special Victims Unit"
        # 98 px = 14 characters: "Law & Order:" / "Special" (+" Victims" = 15) / "Victims Unit"
        self.assertEqual(fit_to_lines(name, 98, 3, measure), name)
        self.assertEqual(fit_to_lines(name, 98, 2, measure), "Law & Order:\nSpecial Victi…")

    def test_empty_text(self) -> None:
        self.assertEqual(fit_to_lines("", 49, 2, measure), "")
        self.assertEqual(fit_to_lines("", 49, 1, measure), "")


class FitToLinesWithBreaksTests(unittest.TestCase):
    def test_existing_breaks_are_kept_when_everything_fits(self) -> None:
        self.assertEqual(fit_to_lines("Mission\nImpossible", 98, 2, measure), "Mission\nImpossible")

    def test_each_source_line_wraps_on_its_own(self) -> None:
        # "aaa" / "bbb ccc" / "ddd": "aaa bbb" would fit one line, but the break comes first.
        self.assertEqual(fit_to_lines("aaa\nbbb ccc ddd", 49, 3, measure), "aaa\nbbb ccc ddd")

    def test_breaks_count_towards_the_line_limit(self) -> None:
        # Two short source lines need two lines even though together they'd fit on one.
        self.assertEqual(fit_to_lines("aaa\nbbb ccc ddd", 49, 2, measure), "aaa\nbbb cc…")
        self.assertEqual(fit_to_lines("a\nb\nc", 49, 3, measure), "a\nb\nc")
        self.assertEqual(fit_to_lines("a\nb\nc", 49, 2, measure), "a\nb c")

    def test_last_line_joins_the_remaining_source_lines(self) -> None:
        # "aaa bbb" / "ccc" + "ddd eee" share the last line, elided.
        self.assertEqual(fit_to_lines("aaa bbb ccc\nddd eee", 49, 2, measure), "aaa bbb\nccc dd…")
        self.assertEqual(fit_to_lines("aaa\nbbb", 49, 1, measure), "aaa bbb")
        self.assertEqual(fit_to_lines("aaa\nbbb ccc", 49, 1, measure), "aaa bb…")

    def test_source_lines_are_stripped(self) -> None:
        self.assertEqual(fit_to_lines("aaa \n  bbb ccc ddd", 49, 2, measure), "aaa\nbbb cc…")

    def test_a_blank_source_line_takes_a_line(self) -> None:
        self.assertEqual(fit_to_lines("aaa\n\nbbb", 49, 3, measure), "aaa\n\nbbb")
        self.assertEqual(fit_to_lines("aaa\n\nbbb ccc", 49, 2, measure), "aaa\nbbb ccc")


class TitleLinesTests(unittest.TestCase):
    def test_colon_becomes_a_line_break(self) -> None:
        self.assertEqual(
            title_lines("Mission: Impossible – Dead Reckoning"),
            "Mission\nImpossible – Dead Reckoning",
        )

    def test_spaced_hyphen_becomes_a_line_break(self) -> None:
        self.assertEqual(
            title_lines("Spider-Man - Across the Spider-Verse"),
            "Spider-Man\nAcross the Spider-Verse",
        )

    def test_only_the_first_separator(self) -> None:
        self.assertEqual(
            title_lines("Star Wars: Episode IV - A New Hope"), "Star Wars\nEpisode IV - A New Hope"
        )
        self.assertEqual(title_lines("Foo - Bar: Baz"), "Foo\nBar: Baz")
        self.assertEqual(title_lines("A: B: C"), "A\nB: C")

    def test_titles_without_a_separator_are_unchanged(self) -> None:
        for title in ("Dune", "Spider-Man", "Mission – Impossible", "Re:Zero", "10:30 PM", ""):
            with self.subTest(title=title):
                self.assertEqual(title_lines(title), title)

    def test_an_empty_side_keeps_the_title(self) -> None:
        for title in ("Re: ", ": Origins", " - Foo", "Foo - "):
            with self.subTest(title=title):
                self.assertEqual(title_lines(title), title)

    def test_fits_a_long_movie_title_into_two_lines(self) -> None:
        title = title_lines("Mission: Impossible – Dead Reckoning")
        # 196 px = 28 characters: the subtitle (27) fits on the second line.
        self.assertEqual(fit_to_lines(title, 196, 2, measure), title)
        # 140 px = 20 characters: the subtitle is elided.
        self.assertEqual(fit_to_lines(title, 140, 2, measure), "Mission\nImpossible – Dead R…")


if __name__ == "__main__":
    unittest.main()
