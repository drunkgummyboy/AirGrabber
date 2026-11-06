"""The rolling week's dates and picture, and a season pack's target size."""

from __future__ import annotations

import unittest
from datetime import date
from typing import Any

from app.models import CalendarLayout, Settings, Weekday
from app.models.domain import SearchKind, SearchRequest
from app.services.library import rolling_range
from app.services.picking import (
    GIB,
    MIB,
    is_likely_fake,
    pack_episodes,
    pick_best,
    size_range,
    size_rank,
)
from app.ui.calendar_layout_picture import cells
from tests.test_picking import rel

FRIDAY = date(2026, 11, 6)
TV = Settings(size_tv_min_gb=1, size_tv_max_gb=2)


def season(episodes: int | None = 10) -> SearchRequest:
    return SearchRequest(kind=SearchKind.SEASON, title="Show", season=2, episode_count=episodes)


class RollingRangeTests(unittest.TestCase):
    def test_today_is_in_the_middle(self) -> None:
        start, end = rolling_range(FRIDAY, first_day=Weekday.MONDAY)
        self.assertEqual((start, end), (date(2026, 11, 3), date(2026, 11, 9)))
        self.assertEqual(FRIDAY, date.fromordinal((start.toordinal() + end.toordinal()) // 2))

    def test_the_next_arrow_starts_on_the_first_monday_to_come(self) -> None:
        start, end = rolling_range(FRIDAY, first_day=Weekday.MONDAY, offset=1)
        self.assertEqual((start, end), (date(2026, 11, 9), date(2026, 11, 15)))
        self.assertEqual(start.weekday(), 0)
        middle = date.fromordinal(start.toordinal() + 3)
        self.assertEqual(middle.strftime("%A"), "Thursday")

    def test_it_goes_on_a_week_at_a_time(self) -> None:
        self.assertEqual(
            rolling_range(FRIDAY, first_day=Weekday.MONDAY, offset=2)[0], date(2026, 11, 16)
        )

    def test_the_back_arrow_shows_the_whole_week_before(self) -> None:
        start, end = rolling_range(FRIDAY, first_day=Weekday.MONDAY, offset=-1)
        self.assertEqual((start, end), (date(2026, 10, 26), date(2026, 11, 1)))

    def test_it_follows_the_first_day_of_the_week(self) -> None:
        self.assertEqual(
            rolling_range(FRIDAY, first_day=Weekday.SUNDAY, offset=1)[0], date(2026, 11, 8)
        )

    def test_on_a_monday_the_next_arrow_goes_to_next_monday(self) -> None:
        monday = date(2026, 11, 9)
        self.assertEqual(
            rolling_range(monday, first_day=Weekday.MONDAY, offset=1)[0], date(2026, 11, 16)
        )

    def test_weeks_stays_the_default(self) -> None:
        self.assertIs(Settings().calendar_layout, CalendarLayout.WEEKS)


class PictureTests(unittest.TestCase):
    def test_rolling_is_one_row_with_today_in_the_middle(self) -> None:
        days = cells(CalendarLayout.ROLLING, 154, 62, 2)
        self.assertEqual(len(days), 7)
        self.assertEqual([index for index, day in enumerate(days) if day[4]], [3])

    def test_weeks_is_a_grid_with_one_today(self) -> None:
        days = cells(CalendarLayout.WEEKS, 154, 62, 2)
        self.assertEqual(len(days), 21)
        self.assertEqual(sum(1 for day in days if day[4]), 1)


class PackSizeTests(unittest.TestCase):
    def test_the_range_is_the_episode_range_times_the_episodes(self) -> None:
        release = rel("Show.S02.1080p.WEB")
        self.assertEqual(size_range(release, season(10), TV), (10, 20))

    def test_a_range_set_on_one_side_stays_open_on_the_other(self) -> None:
        release = rel("Show.S02.1080p.WEB")
        self.assertEqual(size_range(release, season(10), Settings(size_tv_max_gb=2)), (None, 20))

    def test_without_a_count_or_a_range_nothing_is_judged(self) -> None:
        release = rel("Show.S02.1080p.WEB")
        self.assertIsNone(size_range(release, season(None), TV))
        self.assertIsNone(size_range(release, season(10), Settings()))

    def test_the_pack_that_fits_beats_a_bigger_one_with_more_seeders(self) -> None:
        fits = rel("Show.S02.1080p.WEB-FIT", seeders=5, size=15 * GIB)
        huge = rel("Show.S02.1080p.WEB-HUGE", seeders=500, size=60 * GIB)
        self.assertIs(pick_best([huge, fits], season(10), TV), fits)

    def test_outside_the_range_the_closest_wins_and_nothing_is_filtered(self) -> None:
        near = rel("Show.S02.1080p.WEB-NEAR", seeders=5, size=25 * GIB)
        far = rel("Show.S02.1080p.WEB-FAR", seeders=500, size=60 * GIB)
        self.assertIs(pick_best([far, near], season(10), TV), near)

    def test_a_pack_that_names_its_episodes_is_judged_by_them(self) -> None:
        release = rel("Show.S02E01-E04.1080p.WEB")
        self.assertEqual(pack_episodes(release, season(10)), 4)
        self.assertEqual(size_range(release, season(10), TV), (4, 8))

    def test_a_year_or_a_resolution_is_no_episode_span(self) -> None:
        self.assertEqual(pack_episodes(rel("Show.S02.2024.1080p"), season(10)), 10)
        self.assertEqual(pack_episodes(rel("Show.S02E01.2024.1080p"), season(10)), 10)

    def test_several_seasons_are_not_judged_and_rank_below_a_fit(self) -> None:
        for title in ("Show.S01-S03.1080p", "Show.Seasons.1-3.1080p", "Show.Complete.Series.1080p"):
            with self.subTest(title):
                release = rel(title, size=90 * GIB)
                self.assertIsNone(pack_episodes(release, season(10)))
                self.assertIsNone(size_range(release, season(10), TV))
                self.assertEqual(size_rank(release, season(10), TV), (1, 0.0))

    def test_a_series_range_still_means_one_episode_for_one_episode(self) -> None:
        request: Any = SearchRequest(kind=SearchKind.EPISODE, title="Show", season=2, episode=1)
        self.assertEqual(size_range(rel("Show.S02E01.1080p"), request, TV), (1, 2))


class PackFakeCheckTests(unittest.TestCase):
    def test_a_tiny_pack_for_its_episodes_counts_as_fake(self) -> None:
        # 1080p: an episode's floor is 250 MiB; half of that times ten is 1250.
        self.assertTrue(is_likely_fake(rel("Show.S02.1080p", size=800 * MIB), season(10)))
        self.assertFalse(is_likely_fake(rel("Show.S02.1080p", size=1500 * MIB), season(10)))

    def test_without_a_count_one_episodes_floor_stays(self) -> None:
        self.assertFalse(is_likely_fake(rel("Show.S02.1080p", size=300 * MIB), season(None)))
        self.assertTrue(is_likely_fake(rel("Show.S02.1080p", size=200 * MIB), season(None)))

    def test_a_season_of_one_episode_is_not_held_to_less_than_an_episode(self) -> None:
        self.assertTrue(is_likely_fake(rel("Show.S02.1080p", size=200 * MIB), season(1)))


if __name__ == "__main__":
    unittest.main()
