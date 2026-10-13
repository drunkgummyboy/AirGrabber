"""Pure layout rules of the calendar: the visible range, labels, card styles, suggestion picks."""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from app.models import CalendarEntry, Episode, Settings, Show, Weekday
from app.services.library import calendar_range, week_start
from app.ui.main_window import settings_affect_pages
from app.ui.text_fit import SLACK
from app.ui.views.calendar_view import (
    WEEK_CHOICES,
    best_suggestions,
    by_day,
    card_style,
    day_label,
    range_label,
    range_slot_width,
    week_choice,
)

MONDAY = date(2026, 10, 12)
SUNDAY = MONDAY - timedelta(days=1)
TUESDAY = MONDAY + timedelta(days=1)


def week(first: date) -> list[date]:
    return [first + timedelta(days=offset) for offset in range(7)]


def entry(show_id: int, day: date | None, *, name: str = "") -> CalendarEntry:
    return CalendarEntry(
        show=Show(id=show_id, name=name or f"Show {show_id}"),
        episode=Episode(id=show_id * 100, show_id=show_id, season=1, number=1, air_date=day),
        is_tracked=False,
    )


class CalendarRangeTests(unittest.TestCase):
    def test_this_week_monday_first(self) -> None:
        self.assertEqual(
            calendar_range(TUESDAY, first_day=Weekday.MONDAY, weeks=2),
            (date(2026, 10, 12), date(2026, 10, 25)),
        )

    def test_this_week_sunday_first(self) -> None:
        self.assertEqual(
            calendar_range(TUESDAY, first_day=Weekday.SUNDAY, weeks=2),
            (date(2026, 10, 11), date(2026, 10, 24)),
        )

    def test_offsets_move_by_whole_weeks(self) -> None:
        cases = {
            1: (date(2026, 10, 19), date(2026, 11, 8)),
            -1: (date(2026, 10, 5), date(2026, 10, 25)),
            -2: (date(2026, 9, 28), date(2026, 10, 18)),
            0: (date(2026, 10, 12), date(2026, 11, 1)),
        }
        for offset, expected in cases.items():
            with self.subTest(offset=offset):
                self.assertEqual(
                    calendar_range(TUESDAY, first_day=Weekday.MONDAY, weeks=3, offset=offset),
                    expected,
                )

    def test_every_first_day_and_today(self) -> None:
        for first_day in Weekday:
            for today in week(MONDAY):
                for weeks in (1, 3, 5):
                    for offset in (-2, 0, 1):
                        with self.subTest(first=first_day, today=today, weeks=weeks, o=offset):
                            start, end = calendar_range(
                                today, first_day=first_day, weeks=weeks, offset=offset
                            )
                            self.assertEqual(start.weekday(), first_day.weekday)
                            this_week = start - timedelta(weeks=offset)
                            # Offset 0 starts at the week containing today.
                            self.assertTrue(this_week <= today < this_week + timedelta(days=7))
                            self.assertEqual(this_week, week_start(today, first_day))
                            self.assertEqual((end - start).days + 1, 7 * weeks)

    def test_today_on_the_first_day_starts_there(self) -> None:
        self.assertEqual(week_start(MONDAY, Weekday.MONDAY), MONDAY)
        self.assertEqual(week_start(SUNDAY, Weekday.MONDAY), MONDAY - timedelta(weeks=1))
        self.assertEqual(week_start(SUNDAY, Weekday.SUNDAY), SUNDAY)


class LabelTests(unittest.TestCase):
    def test_range_label(self) -> None:
        cases = {
            (date(2026, 10, 12), date(2026, 10, 25)): "12 – 25 October",
            (date(2026, 9, 28), date(2026, 10, 11)): "28 September – 11 October",
            (date(2026, 12, 29), date(2027, 1, 11)): "29 December 2026 – 11 January 2027",
            (date(2026, 11, 30), date(2026, 12, 6)): "30 November – 6 December",
            (date(2026, 10, 1), date(2026, 10, 7)): "1 – 7 October",
        }
        for (start, end), text in cases.items():
            with self.subTest(text):
                self.assertEqual(range_label(start, end), text)

    def test_range_slot_fits_the_longest_label(self) -> None:
        # By characters, the longest labels have years and two-digit days at both ends.
        self.assertEqual(range_slot_width(len), len("31 December 2026 – 27 January 2027") + SLACK)

    def test_day_label(self) -> None:
        self.assertEqual(day_label(MONDAY), "Mon 12")
        self.assertEqual(day_label(SUNDAY), "Sun 11")
        self.assertEqual(day_label(date(2027, 11, 1)), "Mon 1 Nov")
        self.assertEqual(day_label(date(2026, 11, 1)), "Sun 1 Nov")
        self.assertEqual(day_label(date(2027, 1, 1)), "Fri 1 Jan")
        self.assertEqual(day_label(date(2026, 10, 31)), "Sat 31")

    def test_week_choices(self) -> None:
        self.assertEqual(
            list(WEEK_CHOICES.items()),
            [("1 week", 1), ("2 weeks", 2), ("3 weeks", 3), ("4 weeks", 4), ("5 weeks", 5)],
        )
        self.assertEqual(week_choice(Settings().weeks_to_show), "3 weeks")


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


class SuggestionPickTests(unittest.TestCase):
    def test_one_suggestion_per_day_the_best_ranked(self) -> None:
        # Suggestions arrive best first within each day.
        best_monday, second_monday = entry(1, MONDAY), entry(2, MONDAY)
        only_tuesday = entry(3, TUESDAY)
        picks = best_suggestions([best_monday, second_monday, only_tuesday, entry(4, None)])
        self.assertEqual(picks, {MONDAY: best_monday, TUESDAY: only_tuesday})

    def test_no_suggestions(self) -> None:
        self.assertEqual(best_suggestions([]), {})

    def test_by_day_keeps_the_order_and_drops_undated(self) -> None:
        first, second, other = entry(1, MONDAY), entry(2, MONDAY), entry(3, SUNDAY)
        self.assertEqual(
            by_day([first, entry(9, None), other, second]),
            {MONDAY: [first, second], SUNDAY: [other]},
        )


class SettingsRebuildTests(unittest.TestCase):
    def test_calendar_settings_rebuild_the_pages(self) -> None:
        before = Settings()
        for changes in ({"show_suggestions": False}, {"weeks_to_show": 1}):
            with self.subTest(changes):
                self.assertTrue(settings_affect_pages(before, before.model_copy(update=changes)))


if __name__ == "__main__":
    unittest.main()
