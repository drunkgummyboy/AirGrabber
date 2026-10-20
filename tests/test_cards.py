"""Show cards: the My shows info line, run line, menu and switch, the search cards' poster, and
the My shows card itself on a real (hidden) Tk root."""

from __future__ import annotations

import gc
import tkinter as tk
import unittest
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any
from unittest import mock

import customtkinter as ctk

from app.models import (
    CardSize,
    DownloadRecord,
    Episode,
    MediaType,
    MissingRunReport,
    Show,
    ShowActivity,
)
from app.ui import poster_card, theme
from app.ui.card_scale import CardScale
from app.ui.cards import (
    AUTO_SWITCH_TEXT,
    AUTO_SWITCH_TIP,
    SearchShowCard,
    ShowCard,
    auto_switch_color,
    card_info_line,
    card_menu,
    run_line,
    tvmaze_poster_url,
)
from app.ui.missing_control import (
    MissingActions,
    MissingRuns,
    MissingRunState,
    PopupMenu,
    outcome_texts,
)
from app.ui.text_fit import ELLIPSIS

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
        self.assertEqual(line, "✓ S02E08 · today")

    def test_downloaded_yesterday(self) -> None:
        line = card_info_line(grabbed(NOW - timedelta(hours=13)), NOW)
        self.assertEqual(line, "✓ S02E08 · yesterday")

    def test_downloaded_earlier_this_week_names_the_weekday(self) -> None:
        self.assertEqual(
            card_info_line(grabbed(NOW - timedelta(days=2)), NOW), "✓ S02E08 · Sun"
        )
        self.assertEqual(
            card_info_line(grabbed(NOW - timedelta(days=6)), NOW), "✓ S02E08 · Wed"
        )

    def test_a_week_old_grab_is_not_mentioned(self) -> None:
        self.assertIsNone(card_info_line(grabbed(NOW - timedelta(days=7)), NOW))

    def test_season_pack(self) -> None:
        line = card_info_line(grabbed(NOW, season=3, number=None), NOW)
        self.assertEqual(line, "✓ S03 · today")

    def test_days_follow_the_given_time_zone(self) -> None:
        # 23:30 UTC on the 12th is already the 13th in UTC+2: "today" there.
        local = NOW.astimezone(timezone(timedelta(hours=2)))
        when = datetime(2026, 10, 12, 23, 30, tzinfo=UTC)
        self.assertEqual(card_info_line(grabbed(when), local), "✓ S02E08 · today")
        self.assertEqual(card_info_line(grabbed(when), NOW), "✓ S02E08 · yesterday")

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
        self.assertEqual(card_info_line(activity, NOW), "✓ S02E08 · yesterday")

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


def run_report(**fields: object) -> MissingRunReport:
    return MissingRunReport.model_validate({"show_id": 7, "total": 3, **fields})


class AutoSwitchTextTests(unittest.TestCase):
    def test_short_name_and_its_hover_tip(self) -> None:
        self.assertEqual(AUTO_SWITCH_TEXT, "Auto")
        self.assertEqual(
            AUTO_SWITCH_TIP,
            "Auto-download: new episodes of this show download by themselves, "
            "about 2 hours after they air.",
        )


class RunLineTests(unittest.TestCase):
    def test_no_run_keeps_the_info_line(self) -> None:
        self.assertIsNone(run_line(None))

    def test_progress_in_muted_text(self) -> None:
        line = run_line(MissingRunState(2, 3))
        assert line is not None
        texts, color = line
        self.assertEqual(texts[0], "Downloading 2 of 3…")
        self.assertEqual(color, theme.TEXT_MUTED)
        starting = run_line(MissingRunState())
        assert starting is not None
        self.assertEqual(starting[0][0], "Finding missing episodes…")

    def test_outcomes_and_their_colours(self) -> None:
        cases = {
            "downloaded": (run_report(downloaded=("a", "b"), not_found=("c",)), theme.SUCCESS),
            "error": (run_report(error="The NAS didn't answer"), theme.DANGER),
            "not found": (run_report(not_found=("a",)), theme.PENDING),
            "nothing": (run_report(total=0), theme.TEXT_MUTED),
        }
        for name, (report, color) in cases.items():
            with self.subTest(name):
                line = run_line(MissingRunState(3, 3, report))
                assert line is not None
                self.assertEqual(line, (outcome_texts(report), color))
        line = run_line(MissingRunState(3, 3, cases["downloaded"][0]))
        assert line is not None
        self.assertEqual(line[0][0], "✓ 2 downloaded · 1 not found yet")


class CardMenuTests(unittest.TestCase):
    def test_everything(self) -> None:
        self.assertEqual(
            card_menu(has_imdb=True, missing=3, working=False, removable=True),
            [
                ("Open on IMDb", "imdb"),
                ("⬇ Download missing (3)", "download_missing"),
                ("Remove show…", "remove"),
            ],
        )

    def test_download_missing_only_with_missing_episodes_and_no_run_working(self) -> None:
        labels = [
            [label for label, _key in card_menu(has_imdb=True, missing=missing, working=working, removable=True)]
            for missing, working in ((0, False), (3, True))
        ]
        self.assertEqual(labels, [["Open on IMDb", "Remove show…"]] * 2)

    def test_imdb_only_when_known(self) -> None:
        self.assertEqual(
            card_menu(has_imdb=False, missing=0, working=False, removable=True),
            [("Remove show…", "remove")],
        )
        self.assertEqual(card_menu(has_imdb=False, missing=0, working=False, removable=False), [])


class _NoImages:
    async def get(self, *args: object, **kwargs: object) -> None:
        return None


class ShowCardWidgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        gc.collect()  # see tests/test_card_size.py: no Tk objects collected in a worker thread
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        for cache in (poster_card._placeholders, poster_card._placeholder_tasks):
            patcher = mock.patch.dict(cache, clear=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.started: list[int] = []
        self.removed: list[int] = []
        self.switched: list[tuple[int, bool]] = []
        self.runs = MissingRuns()
        self.actions = MissingActions(
            download=lambda show: self.started.append(show.id),
            mark_have=lambda show, ids: None,
            unmark_have=lambda show, ids: None,
            runs=self.runs,
        )

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    def card(self, show: Show, **kwargs: Any) -> ShowCard:
        images: Any = _NoImages()
        card = ShowCard(
            self.root,
            show,
            images=images,
            on_remove=lambda s: self.removed.append(s.id),
            on_auto_download=lambda s, on: self.switched.append((s.id, on)),
            missing_actions=self.actions,
            **kwargs,
        )
        card.pack(side="left")
        self.root.update()
        return card

    async def test_a_show_with_missing_episodes(self) -> None:
        show = Show(id=7, name="Severance", imdb_id="tt11280740")
        card = self.card(show, missing=3, info_line="Next: S02E01 · 20 Jan")
        self.assertEqual(card.badge_text, "3 missing")
        self.assertEqual(card.title_text, "Severance")
        self.assertEqual((card.meta_text, card.meta_color), ("Next: S02E01 · 20 Jan", theme.TEXT_MUTED))
        labels = [label for label, _action in card.menu_items()]
        self.assertEqual(labels, ["Open on IMDb", "⬇ Download missing (3)", "Remove show…"])
        card.menu_items()[1][1]()
        self.assertEqual(self.started, [7])  # the same action as the old ⬇ Download missing
        card.menu_items()[2][1]()
        self.assertEqual(self.removed, [7])

    async def test_a_download_missing_run_takes_the_info_line(self) -> None:
        card = self.card(Show(id=7, name="Severance"), missing=3, info_line="Next: S02E01 · 20 Jan")
        self.runs.set(7, MissingRunState(2, 3))
        self.assertEqual(card.meta_text, "Downloading 2 of 3…")
        self.assertNotIn("⬇ Download missing (3)", [label for label, _ in card.menu_items()])
        done = MissingRunState(3, 3, run_report(downloaded=("a", "b", "c")))
        self.runs.set(7, done)
        self.assertEqual((card.meta_text, card.meta_color), ("✓ 3 downloaded", theme.SUCCESS))
        self.runs.expire(7, done)
        self.assertEqual(card.meta_text, "Next: S02E01 · 20 Jan")
        card.destroy()
        self.runs.set(7, MissingRunState(1, 3))  # a destroyed card is no longer followed

    async def test_a_card_rebuilt_mid_run_shows_the_run(self) -> None:
        self.runs.set(8, MissingRunState(1, 2))
        card = self.card(Show(id=8, name="Silo"), missing=2, info_line="Next: S03E01 · Fri")
        self.assertEqual(card.meta_text, "Downloading 1 of 2…")

    async def test_nothing_missing_no_badge(self) -> None:
        card = self.card(Show(id=8, name="Silo"), info_line=None)
        self.assertIsNone(card.badge_text)
        self.assertEqual(card.meta_text, "")  # the line keeps its place, so cards line up
        self.assertEqual([label for label, _ in card.menu_items()], ["Remove show…"])

    async def test_info_lines_fit_every_card_size(self) -> None:
        lines = ("✓ S12E10 · yesterday", "✓ S12 · yesterday", "Next: S12E10 · 20 Dec")
        for size in CardSize:
            for line in lines:
                with self.subTest(size=size, line=line):
                    card = self.card(Show(id=8, name="Silo"), info_line=line, scale=CardScale.of(size))
                    self.assertEqual(card.meta_text, line)
                    card.destroy()

    async def test_a_line_too_long_is_shortened(self) -> None:
        line = "Next: S02E01 · " + "a very long day name " * 3
        card = self.card(Show(id=8, name="Silo"), info_line=line, scale=CardScale.of(CardSize.XS))
        assert card.meta_text is not None
        self.assertTrue(card.meta_text.startswith("Next: S02E01"))
        self.assertTrue(card.meta_text.endswith(ELLIPSIS))
        self.assertLessEqual(card._meta_font.measure(card.meta_text), card._room)

    async def test_the_auto_switch(self) -> None:
        card = self.card(Show(id=9, name="Andor"), auto_download=False)
        assert card._auto_switch is not None
        self.assertEqual(card._auto_switch.cget("text"), "Auto")
        card._auto_switch.toggle()
        self.assertEqual(self.switched, [(9, True)])
        self.assertTrue(card.auto_download)
        card.set_auto_download(False)  # e.g. undoing a failed change: no call back
        self.assertEqual(self.switched, [(9, True)])
        self.assertEqual(card._auto_switch.cget("text_color"), theme.TEXT_MUTED)

    async def test_the_round_menu_button_opens_the_menu(self) -> None:
        card = self.card(Show(id=7, name="Severance", imdb_id="tt11280740"), missing=1)
        menu = card.menu_button
        self.assertEqual(menu.winfo_reqwidth(), menu.winfo_reqheight())  # round, not a pill
        menu.invoke()
        opened = PopupMenu._open
        assert opened is not None
        self.addCleanup(opened.close)
        self.assertEqual(
            opened.labels, ("Open on IMDb", "⬇ Download missing (1)", "Remove show…")
        )

    async def test_a_new_windows_scaling_redraws_the_card(self) -> None:
        card = self.card(Show(id=7, name="Severance"), missing=3, info_line="Next: S02E01 · Fri")
        before = card._face
        with mock.patch.object(ctk.ScalingTracker, "get_widget_scaling", return_value=2.0):
            card._set_scaling(2.0, 2.0)  # what CTk calls when the window moves to a 200 % screen
            self.assertEqual(card.menu_button.winfo_reqwidth(), 48)
        self.assertNotEqual(card._face, before)
        self.assertEqual(card._face.size, (192 * 2, 288 * 2))
        self.assertEqual(card.meta_text, "Next: S02E01 · Fri")

    async def test_search_cards_keep_their_track_row(self) -> None:
        images: Any = _NoImages()
        card = SearchShowCard(
            self.root, Show(id=3, name="Silo", premiered=date(2023, 5, 5)), images=images, is_tracked=True,
            on_track=lambda s: None,
        )
        self.assertEqual(card._track_button.cget("text"), "✓ Following")
        self.assertEqual(card.meta_text, "2023")


if __name__ == "__main__":
    unittest.main()
