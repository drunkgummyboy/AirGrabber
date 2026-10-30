"""The show details window's pure helpers: meta line, next line, seasons, row states, summary."""

from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta, timezone

from app.models import Episode, Show, ShowDetails
from app.ui.show_details import (
    collapsed_summary,
    date_text,
    default_season,
    episode_row_state,
    mark_color,
    mark_slot_width,
    mark_text,
    meta_line,
    new_text,
    next_line,
    row_mark,
    season_episodes,
    season_marks,
    season_numbers,
    upcoming_text,
)
from app.ui import theme

NOW = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)  # a Sunday


def show(**fields: object) -> Show:
    return Show.model_validate({"id": 1, "name": "Severance", **fields})


def episode(
    episode_id: int,
    season: int,
    number: int | None,
    *,
    airs: datetime | None = None,
    air_date: date | None = None,
) -> Episode:
    return Episode(
        id=episode_id,
        show_id=1,
        season=season,
        number=number,
        airstamp=airs,
        air_date=air_date if air_date is not None else (airs.date() if airs else None),
    )


def details(*episodes: Episode, next_episode: Episode | None = None) -> ShowDetails:
    return ShowDetails(show=show(), episodes=episodes, next_episode=next_episode)


class MetaLineTests(unittest.TestCase):
    """Year · network · seasons · rating · status (the info window's order)."""

    def test_everything_known(self) -> None:
        full = show(premiered="2022-02-18", network="Apple TV", status="Running", rating=8.4)
        self.assertEqual(meta_line(full, 3), "2022 · Apple TV · 3 seasons · ★ 8.4 · Running")
        self.assertEqual(meta_line(full, 1), "2022 · Apple TV · 1 season · ★ 8.4 · Running")

    def test_unknown_parts_are_left_out(self) -> None:
        self.assertEqual(meta_line(show(status="Ended", rating=7.0)), "★ 7.0 · Ended")
        self.assertEqual(meta_line(show(premiered="2022-02-18")), "2022")
        self.assertEqual(meta_line(show(), 0), "")
        self.assertEqual(meta_line(show()), "")


class NextLineTests(unittest.TestCase):
    def test_code_weekday_and_date(self) -> None:
        upcoming = episode(1, 2, 4, airs=datetime(2026, 10, 16, 1, 0, tzinfo=UTC))
        self.assertEqual(next_line(upcoming, NOW), "Next: S02E04 · Fri 16 Oct")

    def test_the_airstamp_decides_the_day_in_nows_time_zone(self) -> None:
        # 01:00 UTC on the 16th is still the 15th in New York (UTC-4).
        upcoming = episode(1, 2, 4, airs=datetime(2026, 10, 16, 1, 0, tzinfo=UTC))
        new_york = NOW.astimezone(timezone(timedelta(hours=-4)))
        self.assertEqual(next_line(upcoming, new_york), "Next: S02E04 · Thu 15 Oct")

    def test_air_date_without_an_airstamp(self) -> None:
        upcoming = episode(1, 2, 4, air_date=date(2026, 10, 22))
        self.assertEqual(next_line(upcoming, NOW), "Next: S02E04 · Thu 22 Oct")

    def test_unknown_date_and_no_episode(self) -> None:
        self.assertEqual(next_line(episode(1, 3, 1), NOW), "Next: S03E01")
        self.assertIsNone(next_line(None, NOW))


class SeasonTests(unittest.TestCase):
    def test_seasons_leave_out_specials(self) -> None:
        episodes = [episode(1, 0, 1), episode(2, 2, 1), episode(3, 1, 1), episode(4, 2, 2)]
        self.assertEqual(season_numbers(episodes), [1, 2])

    def test_default_is_the_next_episodes_season(self) -> None:
        aired = episode(1, 2, 1, airs=NOW - timedelta(days=400))
        upcoming = episode(2, 3, 1, airs=NOW + timedelta(days=3))
        self.assertEqual(default_season(details(aired, upcoming, next_episode=upcoming), NOW), 3)

    def test_default_is_else_the_latest_season_with_an_aired_episode(self) -> None:
        episodes = (
            episode(1, 1, 1, airs=NOW - timedelta(days=800)),
            episode(2, 2, 1, airs=NOW - timedelta(days=400)),
            episode(3, 3, 1),  # announced, no date: no next episode either
            episode(4, 0, 1, airs=NOW - timedelta(days=1)),  # a special doesn't count
        )
        self.assertEqual(default_season(details(*episodes), NOW), 2)

    def test_default_is_the_latest_season_while_it_has_missing_episodes(self) -> None:
        aired = episode(1, 1, 1, airs=NOW - timedelta(days=400))
        upcoming = episode(2, 2, 1, airs=NOW + timedelta(days=3))
        missing = ShowDetails(
            show=show(),
            episodes=(aired, upcoming),
            next_episode=upcoming,
            latest_season=1,
            missing=frozenset({1}),
        )
        self.assertEqual(default_season(missing, NOW), 1)

    def test_default_without_aired_episodes_is_the_first_season(self) -> None:
        self.assertEqual(default_season(details(episode(1, 2, 1), episode(2, 1, 1)), NOW), 1)

    def test_no_seasons(self) -> None:
        self.assertIsNone(default_season(details(), NOW))
        self.assertIsNone(default_season(details(episode(1, 0, 1)), NOW))

    def test_season_episodes_by_number_then_unnumbered_by_date(self) -> None:
        episodes = [
            episode(1, 1, None, air_date=date(2026, 3, 1)),
            episode(2, 1, 2),
            episode(3, 2, 1),
            episode(4, 1, 1),
            episode(5, 1, None, air_date=date(2026, 1, 1)),
        ]
        self.assertEqual([e.id for e in season_episodes(episodes, 1)], [4, 2, 5, 1])


class RowStateTests(unittest.TestCase):
    def test_aired_and_downloaded(self) -> None:
        aired = episode(1, 1, 1, airs=NOW - timedelta(hours=1))
        self.assertEqual(episode_row_state(aired, downloaded=True, now=NOW), "downloaded")

    def test_aired_not_downloaded(self) -> None:
        aired = episode(1, 1, 1, air_date=NOW.date())  # no airstamp: aired from its date on
        self.assertEqual(episode_row_state(aired, downloaded=False, now=NOW), "downloadable")

    def test_not_aired_is_upcoming_even_when_a_pack_covers_it(self) -> None:
        later = episode(1, 1, 1, airs=NOW + timedelta(hours=1))
        self.assertEqual(episode_row_state(later, downloaded=True, now=NOW), "upcoming")
        self.assertEqual(episode_row_state(episode(2, 1, 2), downloaded=False, now=NOW), "upcoming")

    def test_upcoming_text_counts_the_days(self) -> None:
        def text(day: date) -> str:
            return upcoming_text(episode(1, 1, 1, air_date=day), NOW)

        self.assertEqual(text(date(2026, 10, 18)), "airs today")
        self.assertEqual(text(date(2026, 10, 19)), "airs tomorrow")
        self.assertEqual(text(date(2026, 10, 25)), "airs in 7 days")
        self.assertEqual(text(date(2027, 1, 1)), "airs in 75 days")
        self.assertEqual(upcoming_text(episode(1, 1, 1), NOW), "")  # the date column says TBA
        later_today = episode(1, 1, 1, airs=NOW + timedelta(hours=3))
        self.assertEqual(upcoming_text(later_today, NOW), "airs today")

    def test_date_text_aired_or_not(self) -> None:
        self.assertEqual(date_text(episode(1, 1, 1, air_date=date(2026, 1, 17)), NOW), "17 Jan")
        self.assertEqual(date_text(episode(1, 1, 1, air_date=date(2026, 10, 25)), NOW), "25 Oct")
        self.assertEqual(date_text(episode(1, 1, 1), NOW), "TBA")


class MarkTests(unittest.TestCase):
    """The latest season's chips: amber Missing, blue New (aired less than a day ago), muted
    ✓ Have it."""

    def details(self) -> ShowDetails:
        return ShowDetails(
            show=show(),
            episodes=(
                episode(1, 1, 1, airs=NOW - 3 * timedelta(days=1)),
                episode(2, 1, 2, airs=NOW - timedelta(hours=3)),
                episode(3, 1, 3, airs=NOW - 2 * timedelta(days=1)),
                episode(4, 1, 4, airs=NOW + timedelta(days=1)),
            ),
            tracked=True,
            latest_season=1,
            missing=frozenset({1}),
            new=frozenset({2}),
            have=frozenset({3}),
        )

    def test_row_marks(self) -> None:
        details = self.details()
        marks = [row_mark(details, e) for e in details.episodes]
        self.assertEqual(marks, ["missing", "new", "have", None])
        self.assertEqual(season_marks(details, 1), ([1], [3]))  # new ones aren't missing

    def test_new_text_today_or_yesterday(self) -> None:
        self.assertEqual(new_text(episode(1, 1, 1, airs=NOW - timedelta(hours=3)), NOW), "New · aired today")
        late_yesterday = episode(1, 1, 1, airs=NOW - timedelta(hours=14))  # 22:00 the day before
        self.assertEqual(new_text(late_yesterday, NOW), "New · aired yesterday")
        self.assertEqual(new_text(episode(1, 1, 1, air_date=NOW.date()), NOW), "New · aired today")
        # The day in `now`'s time zone: 22:00 UTC on the 17th is the 18th in Amsterdam.
        amsterdam = NOW.astimezone(timezone(timedelta(hours=2)))
        self.assertEqual(new_text(late_yesterday, amsterdam), "New · aired today")
        self.assertEqual(new_text(episode(1, 1, 1), NOW), "New")

    def test_texts_and_colours(self) -> None:
        aired = episode(1, 1, 1, airs=NOW - timedelta(hours=1))
        self.assertEqual(mark_text("missing", aired, NOW), "Missing")
        self.assertEqual(mark_text("have", aired, NOW), "✓ Have it")
        self.assertEqual(mark_text("new", aired, NOW), "New · aired today")
        self.assertEqual(mark_color("missing"), theme.WARNING)
        self.assertEqual(mark_color("new"), theme.LINK)
        self.assertEqual(mark_color("have"), theme.TEXT_MUTED)

    def test_the_chip_slot_fits_the_longest_text(self) -> None:
        width = mark_slot_width(lambda text: 7.0 * len(text))
        self.assertEqual(width, 7 * len("New · aired yesterday") + 24)
        self.assertEqual(mark_slot_width(lambda text: 1.0), 92)  # never below MARK_WIDTH


class SummaryTests(unittest.TestCase):
    @staticmethod
    def measure(text: str) -> float:
        return float(len(text))  # one unit per character

    def test_short_summary_is_kept_whole(self) -> None:
        self.assertEqual(collapsed_summary("A short one.", 40, self.measure), ("A short one.", False))

    def test_long_summary_is_cut_at_six_lines(self) -> None:
        text = " ".join(f"word{n:02d}" for n in range(40))  # 7 characters a word
        fitted, cut = collapsed_summary(text, 15, self.measure)  # two words a line
        self.assertTrue(cut)
        lines = fitted.split("\n")
        self.assertEqual(len(lines), 6)
        self.assertEqual(lines[0], "word00 word01")
        self.assertTrue(lines[-1].endswith("…"))


if __name__ == "__main__":
    unittest.main()
