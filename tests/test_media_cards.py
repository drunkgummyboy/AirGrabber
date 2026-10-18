"""The Movies and Discover cards: ratings, poster sizes, button states, IMDb links."""

from __future__ import annotations

import unittest
from datetime import date

from app.models import Movie, MovieDetails, TVDetails
from app.ui.media_cards import (
    format_runtime,
    imdb_search_url,
    movie_action,
    show_imdb_url,
    tmdb_poster_size,
    tmdb_rating,
)

TODAY = date(2026, 10, 12)


def movie(**fields: object) -> Movie:
    return Movie.model_validate({"id": 1, "title": "Dune: Part Two", **fields})


def details(**fields: object) -> MovieDetails:
    return MovieDetails.model_validate({"id": 1, **fields})


class FormatRuntimeTests(unittest.TestCase):
    def test_hours_and_minutes(self) -> None:
        self.assertEqual(format_runtime(145), "2h 25m")
        self.assertEqual(format_runtime(61), "1h 1m")

    def test_whole_hours(self) -> None:
        self.assertEqual(format_runtime(120), "2h")

    def test_under_an_hour(self) -> None:
        self.assertEqual(format_runtime(45), "45m")


class MovieActionTests(unittest.TestCase):
    def test_digital_release_out(self) -> None:
        for day in ("2026-10-12", "2026-01-01"):
            with self.subTest(day=day):
                result = movie_action(movie(), details(digital_release=day), TODAY)
                self.assertEqual(result, ("Search / Grab", True))

    def test_digital_release_ahead(self) -> None:
        # Even when the film is already in cinemas.
        film = movie(release_date="2026-09-01")
        result = movie_action(film, details(digital_release="2026-10-30"), TODAY)
        self.assertEqual(result, ("Digital 30 Oct", False))

    def test_unknown_digital_release_and_not_released(self) -> None:
        self.assertEqual(movie_action(movie(), None, TODAY), ("Not Released", False))
        future = movie(release_date="2026-12-25")
        self.assertEqual(movie_action(future, None, TODAY), ("Not Released", False))
        self.assertEqual(movie_action(future, details(), TODAY), ("Not Released", False))

    def test_unknown_digital_release_but_released(self) -> None:
        for day in ("2026-10-12", "2025-06-01"):
            with self.subTest(day=day):
                film = movie(release_date=day)
                self.assertEqual(movie_action(film, None, TODAY), ("Search / Grab", True))
                self.assertEqual(movie_action(film, details(), TODAY), ("Search / Grab", True))


class ImdbSearchUrlTests(unittest.TestCase):
    def test_title_and_year_are_quoted(self) -> None:
        self.assertEqual(
            imdb_search_url(movie(release_date="2024-02-27")),
            "https://www.imdb.com/find/?q=Dune%3A%20Part%20Two%202024",
        )

    def test_title_only_without_a_year(self) -> None:
        self.assertEqual(
            imdb_search_url(movie(title="Face/Off & Co")),
            "https://www.imdb.com/find/?q=Face%2FOff%20%26%20Co",
        )


class ShowImdbUrlTests(unittest.TestCase):
    def test_imdb_url(self) -> None:
        self.assertEqual(
            show_imdb_url(TVDetails(id=7, imdb_id="tt11280740")),
            "https://www.imdb.com/title/tt11280740/",
        )
        self.assertIsNone(show_imdb_url(TVDetails(id=7)))


class TmdbRatingTests(unittest.TestCase):
    def test_average_with_votes(self) -> None:
        self.assertEqual(tmdb_rating(7.84, 1200), 7.84)

    def test_unknown_without_votes(self) -> None:
        self.assertIsNone(tmdb_rating(0.0, 0))
        self.assertIsNone(tmdb_rating(6.0, 0))


class TmdbPosterSizeTests(unittest.TestCase):
    def test_smallest_size_at_least_as_wide(self) -> None:
        self.assertEqual(tmdb_poster_size(185), "w185")
        self.assertEqual(tmdb_poster_size(186), "w342")
        self.assertEqual(tmdb_poster_size(288), "w342")  # a poster card at 150 %
        self.assertEqual(tmdb_poster_size(384), "w500")  # ... at 200 %
        self.assertEqual(tmdb_poster_size(50), "w92")

    def test_original_beyond_the_largest(self) -> None:
        self.assertEqual(tmdb_poster_size(781), "original")


if __name__ == "__main__":
    unittest.main()
