"""Pure layout rules of the calendar: the visible range, labels, the top bar, card styles,
suggestion picks."""

from __future__ import annotations

import unittest
from datetime import date, timedelta

from app.models import CalendarEntry, CardSize, Episode, Settings, Show, Weekday
from app.services.library import RECENT_DAYS, calendar_range, week_start
from app.ui import theme
from app.ui.calendar_cards import fit_words_to_lines
from app.ui.main_window import settings_affect_pages
from app.ui.text_fit import SLACK
from app.ui.views.calendar_view import (
    WEEK_COUNTS,
    best_suggestions,
    by_day,
    card_style,
    day_label,
    header_wraps,
    range_label,
    range_slot_width,
    suggestions_look,
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
    def test_start_of_the_week_shows_the_previous_week_too(self) -> None:
        # Tuesday is one day into a Monday-first week: the weekend must stay on screen.
        self.assertEqual(
            calendar_range(TUESDAY, first_day=Weekday.MONDAY, weeks=2),
            (date(2026, 10, 5), date(2026, 10, 25)),
        )
        # In a Sunday-first week Tuesday is the third day: Saturday is in the previous week.
        self.assertEqual(
            calendar_range(TUESDAY, first_day=Weekday.SUNDAY, weeks=2),
            (date(2026, 10, 4), date(2026, 10, 24)),
        )

    def test_later_in_the_week_starts_at_the_week(self) -> None:
        thursday = MONDAY + timedelta(days=3)
        self.assertEqual(
            calendar_range(thursday, first_day=Weekday.MONDAY, weeks=2),
            (date(2026, 10, 12), date(2026, 10, 25)),
        )

    def test_offsets_move_by_whole_weeks_without_the_extra_week(self) -> None:
        cases = {
            1: (date(2026, 10, 19), date(2026, 11, 8)),
            -1: (date(2026, 10, 5), date(2026, 10, 25)),
            -2: (date(2026, 9, 28), date(2026, 10, 18)),
            0: (date(2026, 10, 5), date(2026, 11, 1)),
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
                this_week = week_start(today, first_day)
                into_week = (today - this_week).days
                for weeks in (1, 3, 5):
                    for offset in (-2, -1, 0, 1):
                        with self.subTest(first=first_day, today=today, weeks=weeks, o=offset):
                            start, end = calendar_range(
                                today, first_day=first_day, weeks=weeks, offset=offset
                            )
                            self.assertEqual(start.weekday(), first_day.weekday)
                            moved = this_week + timedelta(weeks=offset)
                            # The chosen weeks from the (moved) current week on ...
                            self.assertEqual(end, moved + timedelta(weeks=weeks, days=-1))
                            # ... and at the current position, early in the week, one before.
                            extra = int(offset == 0 and into_week < RECENT_DAYS)
                            self.assertEqual(start, moved - timedelta(weeks=extra))
                            self.assertEqual((end - start).days + 1, 7 * (weeks + extra))

    def test_the_last_three_days_are_always_on_the_grid(self) -> None:
        for first_day in Weekday:
            for today in week(MONDAY):
                with self.subTest(first=first_day, today=today):
                    start, end = calendar_range(today, first_day=first_day, weeks=1)
                    self.assertLessEqual(start, today - timedelta(days=RECENT_DAYS))
                    self.assertLessEqual(today, end)
                    # No more than needed: a week earlier would not show a recent day.
                    self.assertGreater(start + timedelta(weeks=1), today - timedelta(days=3))

    def test_today_on_the_first_day_starts_there(self) -> None:
        self.assertEqual(week_start(MONDAY, Weekday.MONDAY), MONDAY)
        self.assertEqual(week_start(SUNDAY, Weekday.MONDAY), MONDAY - timedelta(weeks=1))
        self.assertEqual(week_start(SUNDAY, Weekday.SUNDAY), SUNDAY)


class LabelTests(unittest.TestCase):
    def test_range_label(self) -> None:
        cases = {
            (date(2026, 10, 12), date(2026, 10, 25)): "12 – 25 Oct",
            (date(2026, 9, 28), date(2026, 10, 11)): "28 Sep – 11 Oct",
            (date(2026, 10, 19), date(2026, 11, 8)): "19 Oct – 8 Nov",
            (date(2026, 12, 29), date(2027, 1, 11)): "29 Dec 2026 – 11 Jan 2027",
            (date(2026, 11, 30), date(2026, 12, 6)): "30 Nov – 6 Dec",
            (date(2026, 10, 1), date(2026, 10, 7)): "1 – 7 Oct",
        }
        for (start, end), text in cases.items():
            with self.subTest(text):
                self.assertEqual(range_label(start, end), text)

    def test_range_slot_fits_the_longest_label_within_a_year(self) -> None:
        # By characters, the longest have two months and two-digit days at both ends.
        self.assertEqual(range_slot_width(len), len("28 Sep – 11 Oct") + SLACK)

    def test_range_slot_leaves_out_labels_with_years(self) -> None:
        # Those few are allowed to push the buttons: the slot stays close to the usual labels.
        def measure(text: str) -> int:
            return 1000 if any(char.isdigit() for char in text.split()[-1]) else len(text)

        self.assertEqual(range_slot_width(measure), len("28 Sep – 11 Oct") + SLACK)

    def test_range_slot_covers_six_weeks(self) -> None:
        # Five weeks and the extra one: only six weeks from 30 March end on 10 May.
        def measure(text: str) -> int:
            return 100 if text == "30 Mar – 10 May" else 0

        self.assertEqual(range_slot_width(measure), 100 + SLACK)

    def test_day_label(self) -> None:
        self.assertEqual(day_label(MONDAY), "Mon 12")
        self.assertEqual(day_label(SUNDAY), "Sun 11")
        self.assertEqual(day_label(date(2027, 11, 1)), "Mon 1 Nov")
        self.assertEqual(day_label(date(2026, 11, 1)), "Sun 1 Nov")
        self.assertEqual(day_label(date(2027, 1, 1)), "Fri 1 Jan")
        self.assertEqual(day_label(date(2026, 10, 31)), "Sat 31")

    def test_week_counts(self) -> None:
        self.assertEqual(WEEK_COUNTS, (1, 2, 3, 4, 5))
        self.assertIn(Settings().weeks_to_show, WEEK_COUNTS)


class HeaderTests(unittest.TestCase):
    def test_wraps_only_when_both_groups_do_not_fit(self) -> None:
        self.assertFalse(header_wraps(1000, 500, 300, 24))
        self.assertFalse(header_wraps(824, 500, 300, 24))
        self.assertTrue(header_wraps(823, 500, 300, 24))

    def test_suggestions_toggle_filled_when_on_outlined_when_off(self) -> None:
        on, off = suggestions_look(True), suggestions_look(False)
        self.assertEqual(on["fg_color"], theme.ACCENT)
        self.assertEqual(off["fg_color"], "transparent")
        self.assertEqual(off["border_color"], theme.EDGE)
        self.assertEqual(set(on), set(off))


class CardStyleTests(unittest.TestCase):
    def test_full_cards_from_three_days_ago(self) -> None:
        for today in week(MONDAY) + week(MONDAY + timedelta(weeks=1)):
            with self.subTest(today=today):
                recent = today - timedelta(days=RECENT_DAYS)
                self.assertEqual(card_style(recent, today=today), "full")
                self.assertEqual(card_style(today, today=today), "full")
                self.assertEqual(card_style(today + timedelta(weeks=4), today=today), "full")
                self.assertEqual(card_style(recent - timedelta(days=1), today=today), "compact")
                self.assertEqual(card_style(today - timedelta(weeks=3), today=today), "compact")

    def test_monday_shows_the_weekend_in_full(self) -> None:
        start, _ = calendar_range(MONDAY, first_day=Weekday.MONDAY, weeks=1)
        styles = [card_style(day, today=MONDAY) for day in week(start)]
        # Mon–Thu of the previous week compact, Fri–Sun full.
        self.assertEqual(styles, ["compact"] * 4 + ["full"] * 3)

    def test_every_first_day(self) -> None:
        for first_day in Weekday:
            for today in week(MONDAY):
                with self.subTest(first=first_day, today=today):
                    start, end = calendar_range(today, first_day=first_day, weeks=2)
                    days = [start + timedelta(days=n) for n in range((end - start).days + 1)]
                    full = [day for day in days if card_style(day, today=today) == "full"]
                    # Every day from three days ago on is on the grid, in full ...
                    self.assertEqual(full[0], today - timedelta(days=RECENT_DAYS))
                    self.assertEqual(full[-1], end)
                    self.assertEqual(len(full), (end - full[0]).days + 1)
                    # ... and only days before it are compact, never a whole week of them.
                    compact = [day for day in days if day not in full]
                    self.assertTrue(all(day < full[0] for day in compact))
                    self.assertLess(len(compact), 7)


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
        for changes in (
            {"show_suggestions": False},
            {"weeks_to_show": 1},
            {"card_size": CardSize.S},
        ):
            with self.subTest(changes):
                self.assertTrue(settings_affect_pages(before, before.model_copy(update=changes)))


class WordFitTests(unittest.TestCase):
    """The full cards' names: two lines, broken only between words (1 unit per character)."""

    def fit(self, text: str, width: float) -> str:
        return fit_words_to_lines(text, width, 2, len)

    def test_breaks_at_spaces_and_elides_the_rest(self) -> None:
        self.assertEqual(
            self.fit("Georgie & Mandy's First Marriage", 10), "Georgie &\nMandy's F…"
        )

    def test_never_splits_a_word(self) -> None:
        self.assertEqual(self.fit("Avatar: Seven Havens", 8), "Avatar:\nSeven H…")
        self.assertEqual(self.fit("Avatar: Seven Havens", 14), "Avatar: Seven\nHavens")
        self.assertEqual(self.fit("Shrinking", 8), "Shrinki…")

    def test_a_word_wider_than_a_line_is_elided(self) -> None:
        self.assertEqual(self.fit("Supercalifragilistic", 8), "Superca…")
        self.assertEqual(self.fit("Big Supercalifragilistic Show", 8), "Big\nSuperca…")

    def test_fits_unchanged(self) -> None:
        self.assertEqual(self.fit("The Ark", 10), "The Ark")


if __name__ == "__main__":
    unittest.main()
