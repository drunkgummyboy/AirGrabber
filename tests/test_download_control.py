"""The cards' one-click Download row: which look each state gets, and the status-bar text."""

from __future__ import annotations

import gc
import time
import tkinter as tk
import unittest
from datetime import UTC, date, datetime, timedelta

import customtkinter as ctk

from app.models import (
    CalendarEntry,
    Episode,
    GrabOutcome,
    GrabResult,
    MediaType,
    Movie,
    MovieDetails,
    QualityProfile,
    QuickDownloadResult,
    Settings,
    Show,
)
from app.ui import theme
from app.ui.calendar_cards import episode_base
from app.ui.download_control import (
    CHOOSE_TEXT,
    DOWNLOAD_SHORT,
    DOWNLOAD_TEXT,
    DOWNLOAD_TIP,
    GRABBED_TEXT,
    LOOKS_FOR_QUALITY,
    WORKING_SHORT,
    WORKING_TEXT,
    Base,
    DownloadActions,
    DownloadControl,
    DownloadStates,
    Phase,
    RunState,
    already_downloaded_text,
    control_look,
    control_tip,
    download_status_text,
    episode_key,
    fit_text,
    gb_text,
    grabbed_state,
    looks_for_text,
    movie_key,
    nothing_found_text,
    outcome_text,
    picked_text,
    size_range_text,
)
from app.ui import tooltip
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

    def test_already_downloaded_shows_grabbed(self) -> None:
        for base in (DOWNLOAD, Base("grabbed"), Base("search")):
            look = control_look(base, RunState(Phase.DONE))
            self.assertEqual(
                (look.layout, look.text, look.color, look.action),
                ("button", GRABBED_TEXT, theme.GRABBED, "choose"),
            )

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

    def test_already_downloaded(self) -> None:
        self.assertEqual(
            already_downloaded_text("Severance S02E01"), "Severance S02E01 was already downloaded"
        )

    def test_movie_one_seeder_no_quality(self) -> None:
        result = QuickDownloadResult(found=True, release_title="x", seeders=1, grab=self.GRAB)
        self.assertEqual(
            download_status_text("Dune (2025)", result),
            "Dune (2025) · 1 seeder · Saved Severance.S02E01.torrent",
        )


class HoverTipTests(unittest.TestCase):
    GIB = 1024**3

    def test_looks_for_the_series_or_the_movie_settings(self) -> None:
        settings = Settings(
            quality_tv="720p",
            size_tv_min_gb=0.5,
            size_tv_max_gb="1,5",
            quality_movie="1080p",
            size_movie_min_gb=2.5,
            size_movie_max_gb=4,
        )
        self.assertEqual(looks_for_text(settings, MediaType.TV), "Looks for HD (720p), 0.5–1.5 GB")
        self.assertEqual(
            looks_for_text(settings, MediaType.MOVIE), "Looks for Full HD (1080p), 2.5–4 GB"
        )

    def test_without_a_size_range_just_the_quality(self) -> None:
        self.assertEqual(looks_for_text(Settings(), MediaType.MOVIE), "Looks for Full HD (1080p)")
        settings = Settings(quality_tv="Any", quality_movie="x265", size_movie_max_gb=3)
        self.assertEqual(looks_for_text(settings, MediaType.TV), "Looks for any quality")
        self.assertEqual(
            looks_for_text(settings, MediaType.MOVIE),
            "Looks for smaller files (1080p x265), up to 3 GB",
        )

    def test_size_ranges(self) -> None:
        self.assertEqual(size_range_text(2.5, 4.0), "2.5–4 GB")
        self.assertEqual(size_range_text(2.5, None), "at least 2.5 GB")
        self.assertEqual(size_range_text(None, 10.0), "up to 10 GB")
        self.assertEqual(size_range_text(3.0, 3.0), "3 GB")
        self.assertIsNone(size_range_text(None, None))

    def test_quality_names_match_settings(self) -> None:
        from app.ui.settings_dialog import QUALITY_NAMES

        self.assertEqual(set(LOOKS_FOR_QUALITY), set(QualityProfile))
        for profile, name in LOOKS_FOR_QUALITY.items():
            with self.subTest(profile):
                self.assertEqual(name.casefold(), QUALITY_NAMES[profile].casefold())

    def test_picked(self) -> None:
        self.assertEqual(gb_text(round(2.8 * self.GIB)), "2.8")
        self.assertEqual(gb_text(4 * self.GIB), "4")
        self.assertEqual(gb_text(round(0.34 * self.GIB)), "0.3")
        self.assertEqual(gb_text(round(0.05 * self.GIB)), "0.05")
        self.assertEqual(gb_text(round(0.75 * self.GIB)), "0.8")
        self.assertEqual(picked_text("1080p", round(2.8 * self.GIB)), "Picked 1080p · 2.8 GB")
        self.assertEqual(picked_text("720p", None), "Picked 720p")
        self.assertEqual(picked_text(None, 4 * self.GIB), "Picked 4 GB")
        self.assertEqual(picked_text(None, None), "")

    def test_grabbed_state_keeps_what_was_picked(self) -> None:
        grab = GrabResult(outcome=GrabOutcome.SENT, message="Sent to qBittorrent")
        result = QuickDownloadResult(
            found=True, release_title="x", quality="1080p", size_bytes=3 * self.GIB, grab=grab
        )
        state = grabbed_state(result)
        self.assertEqual(
            state, RunState(Phase.DONE, GrabOutcome.SENT, quality="1080p", size_bytes=3 * self.GIB)
        )
        # The look is the same as before: only the tip knows more.
        self.assertEqual(control_look(DOWNLOAD, state), control_look(DOWNLOAD, done(GrabOutcome.SENT)))

    def test_tip(self) -> None:
        looks_for = "Looks for Full HD (1080p)"
        idle = control_look(DOWNLOAD, None)
        self.assertEqual(control_tip(idle, None, looks_for, shortened=False), looks_for)
        self.assertEqual(
            control_tip(idle, None, looks_for, shortened=True), f"{DOWNLOAD_TIP} · {looks_for}"
        )
        self.assertEqual(control_tip(idle, None, "", shortened=True), DOWNLOAD_TIP)
        self.assertEqual(control_tip(idle, None, "", shortened=False), "")
        picked = RunState(Phase.DONE, GrabOutcome.SAVED, quality="720p", size_bytes=self.GIB)
        self.assertEqual(
            control_tip(control_look(DOWNLOAD, picked), picked, looks_for, shortened=False),
            "Picked 720p · 1 GB",
        )
        # Already downloaded, nothing found, working, a disabled button: no tip.
        for base, state in (
            (DOWNLOAD, RunState(Phase.DONE)),
            (DOWNLOAD, NOTHING),
            (DOWNLOAD, WORKING),
            (Base("grabbed"), None),
            (Base("unavailable", "Not Aired"), None),
        ):
            with self.subTest(base=base, state=state):
                look = control_look(base, state)
                self.assertEqual(control_tip(look, state, looks_for, shortened=False), "")

    def test_states_read_the_settings_when_asked(self) -> None:
        current = Settings(quality_tv="2160p")
        states = DownloadStates(settings=lambda: current)
        self.assertEqual(states.looks_for(episode_key(1, 1, 1)), "Looks for 4K (2160p)")
        self.assertEqual(states.looks_for(movie_key(1)), "Looks for Full HD (1080p)")
        current = Settings(quality_tv="720p")
        self.assertEqual(states.looks_for(episode_key(1, 1, 1)), "Looks for HD (720p)")
        self.assertEqual(DownloadStates().looks_for(movie_key(1)), "")


class HoverTipWidgetTests(unittest.TestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        # Tk only delivers Enter/Leave to mapped widgets: show the root, invisible and off-screen.
        self.root.attributes("-alpha", 0.0)
        self.root.geometry("300x100+-3000+-3000")
        self.addCleanup(self._destroy_root)

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    def pump(self, ms: int) -> None:
        end = time.monotonic() + ms / 1000
        while time.monotonic() < end:
            self.root.update()
            time.sleep(0.01)

    def test_the_tip_follows_settings_and_the_run(self) -> None:
        current = Settings(quality_movie="720p", size_movie_max_gb=2)
        states = DownloadStates(settings=lambda: current)
        actions: DownloadActions[int] = DownloadActions(
            download=lambda _item: None, choose=lambda _item: None, states=states
        )
        key = movie_key(5)
        control = DownloadControl(self.root, item=5, key=key, base=DOWNLOAD, actions=actions)
        control.pack()
        self.root.update()
        tip = control._main_tip
        self.assertEqual(tip.text, "Looks for HD (720p), up to 2 GB")
        current = Settings(quality_movie="2160p")  # changed in Settings since
        control._main.event_generate("<Enter>")
        self.assertEqual(tip.text, "Looks for 4K (2160p)")
        self.pump(tooltip.DELAY_MS + 200)
        self.assertTrue(tip.visible)  # shown with the new text
        assert tooltip._manager._window is not None
        self.assertEqual(tooltip._manager._window._label.cget("text"), "Looks for 4K (2160p)")
        control._main.event_generate("<Leave>")
        self.root.update()
        states.set(key, WORKING)
        self.assertEqual(tip.text, "")
        states.set(key, RunState(Phase.DONE, GrabOutcome.OPENED, quality="2160p", size_bytes=None))
        self.assertEqual(tip.text, "Picked 2160p")
        tip.hide()


if __name__ == "__main__":
    unittest.main()
