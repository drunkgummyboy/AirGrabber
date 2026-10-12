"""What the header search dropdown lists: show and movie rows and the query each row opens."""

from __future__ import annotations

import unittest

from app.models import Movie, Show, ShowSearchResult
from app.ui.search import movie_label, show_label, suggestion_rows


def show(**fields: object) -> Show:
    return Show.model_validate({"id": 1, "name": "Dune: Prophecy", **fields})


def movie(**fields: object) -> Movie:
    return Movie.model_validate({"id": 2, "title": "Dune: Part Two", **fields})


class LabelTests(unittest.TestCase):
    def test_show_with_and_without_year(self) -> None:
        self.assertEqual(show_label(show(premiered="2024-11-17")), "📺 Dune: Prophecy (2024)")
        self.assertEqual(show_label(show()), "📺 Dune: Prophecy")

    def test_movie_with_and_without_year(self) -> None:
        self.assertEqual(movie_label(movie(release_date="2024-02-27")), "🎬 Dune: Part Two (2024)")
        self.assertEqual(movie_label(movie()), "🎬 Dune: Part Two")


class SuggestionRowsTests(unittest.TestCase):
    def test_shows_first_then_movies_with_their_queries(self) -> None:
        rows = suggestion_rows(
            [
                ShowSearchResult(show=show(premiered="2024-11-17"), is_tracked=True),
                ShowSearchResult(show=show(id=3, name="Dune"), is_tracked=False),
            ],
            [movie(release_date="2024-02-27")],
        )
        self.assertEqual(
            rows,
            [
                ("📺 Dune: Prophecy (2024)  ✓", "Dune: Prophecy"),
                ("📺 Dune", "Dune"),
                ("🎬 Dune: Part Two (2024)", "Dune: Part Two"),
            ],
        )

    def test_either_side_may_be_empty(self) -> None:
        self.assertEqual(suggestion_rows([], [movie()]), [("🎬 Dune: Part Two", "Dune: Part Two")])
        self.assertEqual(suggestion_rows([], []), [])


if __name__ == "__main__":
    unittest.main()
