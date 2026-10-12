"""Pure layout rules of the calendar grid: card styles and suggestion quotas."""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from app.ui.views.calendar_view import card_style, suggestion_quota

MONDAY = date(2026, 10, 12)
SUNDAY = MONDAY - timedelta(days=1)


def week(first: date) -> list[date]:
    return [first + timedelta(days=offset) for offset in range(7)]


class CardStyleTests(unittest.TestCase):
    def styles(self, days: list[date], *, today: date, week_start: date) -> set[str]:
        return {card_style(day, today=today, week_start=week_start) for day in days}

    def test_current_and_later_weeks_are_full(self) -> None:
        for today in week(MONDAY):
            with self.subTest(today=today):
                days = week(MONDAY) + week(MONDAY + timedelta(weeks=1))
                self.assertEqual(self.styles(days, today=today, week_start=MONDAY), {"full"})

    def test_previous_week_is_full_on_the_first_two_days_only(self) -> None:
        previous = week(MONDAY - timedelta(weeks=1))
        for offset, expected in [(0, "full"), (1, "full"), (2, "compact"), (6, "compact")]:
            today = MONDAY + timedelta(days=offset)
            with self.subTest(today=today):
                self.assertEqual(
                    self.styles(previous, today=today, week_start=MONDAY), {expected}
                )

    def test_older_weeks_are_compact(self) -> None:
        older = week(MONDAY - timedelta(weeks=2)) + week(MONDAY - timedelta(weeks=4))
        for today in (MONDAY, MONDAY + timedelta(days=5)):
            with self.subTest(today=today):
                self.assertEqual(self.styles(older, today=today, week_start=MONDAY), {"compact"})

    def test_sunday_first_week(self) -> None:
        previous = week(SUNDAY - timedelta(weeks=1))  # Sun 4 Oct - Sat 10 Oct
        # Monday is the second day of a Sunday-first week; Tuesday the third.
        self.assertEqual(self.styles(previous, today=MONDAY, week_start=SUNDAY), {"full"})
        tuesday = MONDAY + timedelta(days=1)
        self.assertEqual(self.styles(previous, today=tuesday, week_start=SUNDAY), {"compact"})
        # On Saturday the week's own Sunday is still the current week...
        saturday = SUNDAY + timedelta(days=6)
        self.assertEqual(card_style(SUNDAY, today=saturday, week_start=SUNDAY), "full")
        # ...whereas in a Monday-first week that Sunday belongs to the previous week.
        self.assertEqual(card_style(SUNDAY, today=saturday, week_start=MONDAY), "compact")


class SuggestionQuotaTests(unittest.TestCase):
    days = week(MONDAY)

    def test_quiet_range_fills_every_day_to_three(self) -> None:
        self.assertEqual(suggestion_quota({}, self.days), dict.fromkeys(self.days, 3))

    def test_target_is_at_least_three(self) -> None:
        quota = suggestion_quota({self.days[0]: 2, self.days[1]: 3}, self.days)
        self.assertEqual(quota[self.days[0]], 1)
        self.assertEqual(quota[self.days[1]], 0)
        self.assertEqual(quota[self.days[2]], 3)

    def test_busiest_day_sets_the_target(self) -> None:
        counts = {self.days[0]: 1, self.days[2]: 5, self.days[4]: 5}
        quota = suggestion_quota(counts, self.days)
        self.assertEqual(
            quota,
            {
                self.days[0]: 4,
                self.days[1]: 5,
                self.days[2]: 0,
                self.days[3]: 5,
                self.days[4]: 0,
                self.days[5]: 5,
                self.days[6]: 5,
            },
        )

    def test_every_day_busy(self) -> None:
        counts = dict.fromkeys(self.days, 4)
        self.assertEqual(suggestion_quota(counts, self.days), dict.fromkeys(self.days, 0))


if __name__ == "__main__":
    unittest.main()
