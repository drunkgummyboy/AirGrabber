"""Show cards: the Tracked info line and switch colours, the search cards' poster."""

from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta, timezone

from app.models import DownloadRecord, Episode, MediaType, Show, ShowActivity
from app.ui import theme
from app.ui.cards import auto_switch_color, card_info_line, tvmaze_poster_url

# Tuesday 13 October 2026, 12:00 UTC.
NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def grabbed(when: datetime, *, season: int = 2, number: int | None = 8) -> ShowActivity:
    record = DownloadRecord(
        media_type=MediaType.TV,
        media_id=1,
        season=season,
        episode=number,
        release_title="x",
        downloaded_at=when,
    )
    return ShowActivity(show_id=1, last_grab=record)


def upcoming(airs: datetime | None = None, *, air_date: date | None = None) -> ShowActivity:
    episode = Episode(id=4, show_id=1, season=2, number=4, airstamp=airs, air_date=air_date)
    return ShowActivity(show_id=1, next_episode=episode)


class InfoLineTests(unittest.TestCase):
    def test_downloaded_today(self) -> None:
        line = card_info_line(grabbed(NOW - timedelta(hours=11)), NOW)
        self.assertEqual(line, "Downloaded S02E08 · today")

    def test_downloaded_yesterday(self) -> None:
        line = card_info_line(grabbed(NOW - timedelta(hours=13)), NOW)
        self.assertEqual(line, "Downloaded S02E08 · yesterday")

    def test_downloaded_earlier_this_week_names_the_weekday(self) -> None:
        self.assertEqual(
            card_info_line(grabbed(NOW - timedelta(days=2)), NOW), "Downloaded S02E08 · Sun"
        )
        self.assertEqual(
            card_info_line(grabbed(NOW - timedelta(days=6)), NOW), "Downloaded S02E08 · Wed"
        )

    def test_a_week_old_grab_is_not_mentioned(self) -> None:
        self.assertIsNone(card_info_line(grabbed(NOW - timedelta(days=7)), NOW))

    def test_season_pack(self) -> None:
        line = card_info_line(grabbed(NOW, season=3, number=None), NOW)
        self.assertEqual(line, "Downloaded S03 · today")

    def test_days_follow_the_given_time_zone(self) -> None:
        # 23:30 UTC on the 12th is already the 13th in UTC+2: "today" there.
        local = NOW.astimezone(timezone(timedelta(hours=2)))
        when = datetime(2026, 10, 12, 23, 30, tzinfo=UTC)
        self.assertEqual(card_info_line(grabbed(when), local), "Downloaded S02E08 · today")
        self.assertEqual(card_info_line(grabbed(when), NOW), "Downloaded S02E08 · yesterday")

    def test_next_episode_within_six_days_names_the_weekday(self) -> None:
        self.assertEqual(
            card_info_line(upcoming(NOW + timedelta(days=2)), NOW), "Next: S02E04 · Thu"
        )
        self.assertEqual(
            card_info_line(upcoming(NOW + timedelta(days=6)), NOW), "Next: S02E04 · Mon"
        )

    def test_next_episode_further_away_names_the_date(self) -> None:
        self.assertEqual(
            card_info_line(upcoming(NOW + timedelta(days=10)), NOW), "Next: S02E04 · 23 Oct"
        )
        self.assertEqual(
            card_info_line(upcoming(air_date=date(2026, 11, 2)), NOW), "Next: S02E04 · 2 Nov"
        )

    def test_next_episode_by_air_date(self) -> None:
        self.assertEqual(
            card_info_line(upcoming(air_date=date(2026, 10, 15)), NOW), "Next: S02E04 · Thu"
        )

    def test_a_recent_grab_comes_before_the_next_episode(self) -> None:
        activity = grabbed(NOW - timedelta(days=1)).model_copy(
            update={"next_episode": upcoming(NOW + timedelta(days=2)).next_episode}
        )
        self.assertEqual(card_info_line(activity, NOW), "Downloaded S02E08 · yesterday")

    def test_nothing(self) -> None:
        self.assertIsNone(card_info_line(None, NOW))
        self.assertIsNone(card_info_line(ShowActivity(show_id=1), NOW))
        self.assertIsNone(card_info_line(upcoming(), NOW))  # no date known
        self.assertIsNone(card_info_line(upcoming(NOW - timedelta(days=2)), NOW))  # stale


class SwitchColorTests(unittest.TestCase):
    def test_text_when_on_muted_when_off(self) -> None:
        self.assertEqual(auto_switch_color(True), theme.TEXT)
        self.assertEqual(auto_switch_color(False), theme.TEXT_MUTED)


MEDIUM = "https://static.tvmaze.com/uploads/images/medium_portrait/1/1.jpg"
ORIGINAL = "https://static.tvmaze.com/uploads/images/original_untouched/1/1.jpg"


class TvmazePosterUrlTests(unittest.TestCase):
    def test_medium_while_it_is_wide_enough(self) -> None:
        show = Show(id=1, name="x", poster_url=MEDIUM, poster_url_original=ORIGINAL)
        self.assertEqual(tvmaze_poster_url(show, 192), MEDIUM)
        self.assertEqual(tvmaze_poster_url(show, 210), MEDIUM)

    def test_original_for_wider_cards(self) -> None:
        show = Show(id=1, name="x", poster_url=MEDIUM, poster_url_original=ORIGINAL)
        self.assertEqual(tvmaze_poster_url(show, 288), ORIGINAL)

    def test_whichever_exists(self) -> None:
        self.assertEqual(tvmaze_poster_url(Show(id=1, name="x", poster_url=MEDIUM), 288), MEDIUM)
        only_original = Show(id=1, name="x", poster_url_original=ORIGINAL)
        self.assertEqual(tvmaze_poster_url(only_original, 192), ORIGINAL)
        self.assertIsNone(tvmaze_poster_url(Show(id=1, name="x"), 288))


if __name__ == "__main__":
    unittest.main()
