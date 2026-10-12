"""What the Movies and Discover cards say: meta and details lines, button states, IMDb links."""

from __future__ import annotations

import unittest
from datetime import date

from app.models import DiscoverShow, Movie, MovieDetails, TVDetails
from app.ui.media_cards import (
    format_runtime,
    imdb_search_url,
    movie_action,
    movie_details_line,
    movie_meta,
    show_details_line,
    show_imdb_url,
    show_meta,
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


class MovieMetaTests(unittest.TestCase):
    def test_release_date_and_rating_from_the_list(self) -> None:
        film = movie(release_date="2026-03-01", vote_average=8.24, vote_count=900)
        self.assertEqual(movie_meta(film, None), "01 Mar 2026 | ★ 8.2")

    def test_digital_release_wins_once_known(self) -> None:
        film = movie(release_date="2026-03-01", vote_average=8.0, vote_count=10)
        self.assertEqual(
            movie_meta(film, details(digital_release="2026-04-16")), "16 Apr 2026 | ★ 8.0"
        )
        # Details without a digital date keep the release date.
        self.assertEqual(movie_meta(film, details(runtime=120)), "01 Mar 2026 | ★ 8.0")

    def test_no_date(self) -> None:
        self.assertEqual(movie_meta(movie(vote_average=6.5, vote_count=3), None), "TBA | ★ 6.5")

    def test_no_votes(self) -> None:
        film = movie(release_date="2026-03-01", vote_average=0.0, vote_count=0)
        self.assertEqual(movie_meta(film, None), "01 Mar 2026 | ★ N/A")


class MovieDetailsLineTests(unittest.TestCase):
    def test_both_parts(self) -> None:
        line = movie_details_line(details(certification="PG-13", runtime=145))
        self.assertEqual(line, "Rated PG-13 · 2h 25m")

    def test_one_part(self) -> None:
        self.assertEqual(movie_details_line(details(certification="R")), "Rated R")
        self.assertEqual(movie_details_line(details(runtime=95)), "1h 35m")

    def test_nothing_known(self) -> None:
        self.assertEqual(movie_details_line(details()), "")
        self.assertEqual(movie_details_line(None), "")


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


def show(**fields: object) -> DiscoverShow:
    return DiscoverShow.model_validate({"id": 7, "name": "Severance", **fields})


class ShowTextTests(unittest.TestCase):
    def test_meta(self) -> None:
        self.assertEqual(
            show_meta(show(first_air_date="2022-02-17", vote_average=8.36, vote_count=2000)),
            "2022 | ★ 8.4",
        )
        self.assertEqual(show_meta(show()), "TBA | ★ N/A")

    def test_details_line(self) -> None:
        line = show_details_line(
            TVDetails(id=7, status="Returning Series", number_of_seasons=2)
        )
        self.assertEqual(line, "Returning Series • 2 Seasons")
        self.assertEqual(show_details_line(TVDetails(id=7, number_of_seasons=1)), "1 Season")
        self.assertEqual(show_details_line(TVDetails(id=7, status="Ended")), "Ended")
        self.assertEqual(show_details_line(TVDetails(id=7, number_of_seasons=0)), "")
        self.assertEqual(show_details_line(None), "")

    def test_imdb_url(self) -> None:
        self.assertEqual(
            show_imdb_url(TVDetails(id=7, imdb_id="tt11280740")),
            "https://www.imdb.com/title/tt11280740/",
        )
        self.assertIsNone(show_imdb_url(TVDetails(id=7)))


if __name__ == "__main__":
    unittest.main()
