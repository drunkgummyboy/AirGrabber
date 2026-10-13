"""The cards' one-click Download row: which look each state gets, and the status-bar text."""

from __future__ import annotations

import unittest
from datetime import UTC, date, datetime, timedelta

from app.models import (
    CalendarEntry,
    Episode,
    GrabOutcome,
    GrabResult,
    MediaType,
    Movie,
    MovieDetails,
    QuickDownloadResult,
    Show,
)
from app.ui import theme
from app.ui.calendar_cards import episode_base
from app.ui.download_control import (
    CHOOSE_TEXT,
    DOWNLOAD_SHORT,
    DOWNLOAD_TEXT,
    GRABBED_TEXT,
    WORKING_SHORT,
    WORKING_TEXT,
    Base,
    DownloadStates,
    Phase,
    RunState,
    control_look,
    download_status_text,
    episode_key,
    fit_text,
    movie_key,
    nothing_found_text,
    outcome_text,
)
from app.ui.media_cards import movie_base

DOWNLOAD = Base("download")
WORKING = RunState(Phase.WORKING)
NOTHING = RunState(Phase.NOTHING)


def done(outcome: GrabOutcome) -> RunState:
    return RunState(Phase.DONE, outcome)


class ControlLookTests(unittest.TestCase):
    def test_idle_is_the_split_control(self) -> None:
        look = control_look(DOWNLOAD, None)
        self.assertEqual(
            (look.layout, look.text, look.action), ("split", DOWNLOAD_TEXT, "download")
        )
        self.assertEqual((look.color, look.hover), (theme.ACCENT, theme.ACCENT_HOVER))
        # Narrow: just the arrow (the "⋯" stays).
        self.assertEqual(look.texts, (DOWNLOAD_TEXT, DOWNLOAD_SHORT))
        self.assertEqual(DOWNLOAD_SHORT, "⬇")

    def test_working_is_one_muted_label(self) -> None:
        for base in (DOWNLOAD, Base("grabbed"), Base("search")):
            look = control_look(base, WORKING)
            self.assertEqual((look.layout, look.text, look.action), ("label", WORKING_TEXT, None))
            self.assertEqual(look.text_color, theme.TEXT_MUTED)
            self.assertEqual(look.texts, (WORKING_TEXT, WORKING_SHORT))

    def test_done_by_outcome(self) -> None:
        for outcome, text in (
            (GrabOutcome.OPENED, "✓ Opened"),
            (GrabOutcome.SAVED, "✓ Saved"),
            (GrabOutcome.SENT, "✓ Sent"),
        ):
            with self.subTest(outcome):
                self.assertEqual(outcome_text(outcome), text)
                look = control_look(DOWNLOAD, done(outcome))
                self.assertEqual((look.layout, look.text), ("button", text))
                self.assertEqual(look.color, theme.GRABBED)
                # Like an already-grabbed episode: the button opens the search window.
                self.assertEqual(look.action, "choose")
        # A card rebuilt after the grab (flagged as downloaded now) keeps showing the outcome.
        self.assertEqual(control_look(Base("grabbed"), done(GrabOutcome.SENT)).text, "✓ Sent")

    def test_every_outcome_has_a_text(self) -> None:
        for outcome in GrabOutcome:
            self.assertTrue(outcome_text(outcome).startswith("✓ "))

    def test_nothing_found_is_an_amber_outline_that_opens_the_search_window(self) -> None:
        look = control_look(DOWNLOAD, NOTHING)
        self.assertEqual((look.layout, look.text, look.action), ("button", "Choose…", "choose"))
        self.assertEqual(look.text, CHOOSE_TEXT)
        self.assertEqual(
            (look.color, look.border, look.text_color),
            ("transparent", theme.PENDING, theme.PENDING),
        )

    def test_grabbed_from_the_search_window_wins_over_nothing_found(self) -> None:
        look = control_look(Base("grabbed"), NOTHING)
        self.assertEqual(
            (look.text, look.color, look.action), (GRABBED_TEXT, theme.GRABBED, "choose")
        )

    def test_unavailable_is_disabled(self) -> None:
        for state in (None, NOTHING):
            look = control_look(Base("unavailable", "Not Aired"), state)
            self.assertEqual((look.layout, look.text, look.action), ("button", "Not Aired", None))
            self.assertEqual((look.color, look.hover), (theme.NOT_AIRED, theme.NOT_AIRED))

    def test_search_base(self) -> None:
        look = control_look(Base("search"), None)
        self.assertEqual((look.layout, look.text, look.action), ("button", "Search", "choose"))


class FitTextTests(unittest.TestCase):
    @staticmethod
    def measure(text: str) -> float:
        return 10.0 * len(text)

    def test_the_first_text_that_fits(self) -> None:
        texts = (DOWNLOAD_TEXT, DOWNLOAD_SHORT)  # 100 and 10 wide
        self.assertEqual(fit_text(texts, 100, self.measure), DOWNLOAD_TEXT)
        self.assertEqual(fit_text(texts, 99, self.measure), DOWNLOAD_SHORT)
        self.assertEqual(fit_text(texts, 10, self.measure), DOWNLOAD_SHORT)
        self.assertEqual(fit_text((WORKING_TEXT, WORKING_SHORT), 120, self.measure), WORKING_SHORT)

    def test_elides_the_last_text_when_nothing_fits(self) -> None:
        self.assertEqual(fit_text(("abcdef",), 40, self.measure), "abc…")


class BaseTests(unittest.TestCase):
    SHOW = Show(id=7, name="Severance")

    def entry(
        self, *, number: int | None = 1, aired: bool = True, downloaded: bool = False
    ) -> CalendarEntry:
        stamp = datetime.now(UTC) + (timedelta(days=-1) if aired else timedelta(days=1))
        episode = Episode(id=1, show_id=7, season=2, number=number, airstamp=stamp)
        return CalendarEntry(
            show=self.SHOW, episode=episode, is_tracked=True, is_downloaded=downloaded
        )

    def test_episode_bases(self) -> None:
        self.assertEqual(episode_base(self.entry()), Base("download"))
        self.assertEqual(episode_base(self.entry(aired=False)), Base("unavailable", "Not Aired"))
        self.assertEqual(episode_base(self.entry(downloaded=True)), Base("grabbed"))
        self.assertEqual(episode_base(self.entry(number=None)), Base("search"))

    def test_movie_bases(self) -> None:
        today = date(2026, 10, 12)
        out = Movie(id=1, title="Dune", release_date=date(2026, 1, 1))
        self.assertEqual(movie_base(out, None, today), Base("download"))
        later = MovieDetails(id=1, digital_release=date(2026, 10, 15))
        self.assertEqual(movie_base(out, later, today), Base("unavailable", "Digital 15 Oct"))
        unreleased = Movie(id=2, title="Dune 3")
        self.assertEqual(movie_base(unreleased, None, today), Base("unavailable", "Not Released"))

    def test_keys(self) -> None:
        self.assertEqual(episode_key(7, 2, 1), (MediaType.TV, 7, 2, 1))
        self.assertEqual(movie_key(9), (MediaType.MOVIE, 9, None, None))
        self.assertNotEqual(episode_key(9, 1, 1), movie_key(9))


class DownloadStatesTests(unittest.TestCase):
    def test_states(self) -> None:
        states = DownloadStates()
        key = episode_key(7, 2, 1)
        self.assertIsNone(states.get(key))
        self.assertFalse(states.is_working(key))
        states.set(key, WORKING)
        self.assertTrue(states.is_working(key))
        self.assertFalse(states.is_working(episode_key(7, 2, 2)))
        states.set(key, done(GrabOutcome.SAVED))
        self.assertFalse(states.is_working(key))
        self.assertEqual(states.get(key), done(GrabOutcome.SAVED))
        states.set(key, None)
        self.assertIsNone(states.get(key))


class StatusTextTests(unittest.TestCase):
    GRAB = GrabResult(outcome=GrabOutcome.SAVED, message="Saved Severance.S02E01.torrent")

    def test_episode(self) -> None:
        result = QuickDownloadResult(
            found=True, release_title="x", quality="1080p", seeders=412, grab=self.GRAB
        )
        self.assertEqual(
            download_status_text("Severance S02E01", result),
            "Severance S02E01 · 1080p · 412 seeders · Saved Severance.S02E01.torrent",
        )

    def test_nothing_found(self) -> None:
        self.assertEqual(
            nothing_found_text("Severance S02E01"),
            "No good download found for Severance S02E01. Choose one yourself.",
        )

    def test_movie_one_seeder_no_quality(self) -> None:
        result = QuickDownloadResult(found=True, release_title="x", seeders=1, grab=self.GRAB)
        self.assertEqual(
            download_status_text("Dune (2025)", result),
            "Dune (2025) · 1 seeder · Saved Severance.S02E01.torrent",
        )


if __name__ == "__main__":
    unittest.main()
