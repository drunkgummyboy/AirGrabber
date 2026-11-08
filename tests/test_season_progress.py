"""A show window's season progress: each episode's state, the chip's and the squares' texts, and
the two canvases."""

from __future__ import annotations

import unittest
from datetime import UTC, datetime, timedelta

import customtkinter as ctk

from app.models import Episode, Show, ShowDetails
from app.ui import theme
from app.ui.season_progress import (
    EpisodeSquares,
    SeasonStrip,
    counts_text,
    episode_state,
    had_text,
    progress_shown,
    season_states,
)

NOW = datetime(2026, 11, 7, 12, tzinfo=UTC)
DAY = timedelta(days=1)


def ep(episode_id: int, season: int, number: int | None, *, aired: datetime) -> Episode:
    return Episode(
        id=episode_id, show_id=1, season=season, number=number, airstamp=aired,
        air_date=aired.date(),
    )


def details(**changes: object) -> ShowDetails:
    episodes = (
        ep(11, 1, 1, aired=NOW - 30 * DAY),
        ep(12, 1, 2, aired=NOW - 23 * DAY),
        ep(13, 1, 3, aired=NOW - 16 * DAY),
        ep(14, 1, 4, aired=NOW + 5 * DAY),
        ep(15, 1, None, aired=NOW - 20 * DAY),  # a special: no square
        ep(21, 2, 1, aired=NOW + 30 * DAY),
    )
    values: dict[str, object] = {
        "show": Show(id=1, name="The Ark"),
        "episodes": episodes,
        "tracked": True,
        "plex_connected": True,
        "in_plex": frozenset({11}),
        "downloaded": frozenset({12}),
    }
    values.update(changes)
    return ShowDetails.model_validate(values)


class StateTests(unittest.TestCase):
    def test_each_episode_gets_its_state(self) -> None:
        shown = details()
        states = [state for _e, state in season_states(shown, 1, NOW)]
        self.assertEqual(states, ["plex", "downloaded", "missing", "later"])
        self.assertEqual([s for _e, s in season_states(shown, 2, NOW)], ["later"])

    def test_in_plex_wins_even_before_its_air_date(self) -> None:
        early = ep(16, 1, 5, aired=NOW + DAY)
        shown = details(in_plex=frozenset({16}))
        self.assertEqual(episode_state(shown, early, NOW), "plex")

    def test_only_a_followed_show_with_plex_shows_progress(self) -> None:
        self.assertTrue(progress_shown(details()))
        self.assertFalse(progress_shown(details(plex_connected=False)))
        self.assertFalse(progress_shown(details(tracked=False)))
        self.assertFalse(progress_shown(None))

    def test_the_texts(self) -> None:
        states = ["plex"] * 7 + ["downloaded"] * 2 + ["missing"] + ["later"] * 2
        self.assertEqual(had_text(states), "9 of 12")  # type: ignore[arg-type]
        self.assertEqual(
            counts_text(states),  # type: ignore[arg-type]
            "7 in Plex · 2 downloaded · 1 not downloaded",
        )
        self.assertEqual(counts_text(["later"]), "")


class CanvasTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = ctk.CTk()
        self.addCleanup(self.root.destroy)

    def test_the_strip_paints_one_stretch_per_state(self) -> None:
        strip = SeasonStrip(self.root, 80)
        strip.show(["plex", "plex", "missing", "later"])
        fills = [strip.itemcget(item, "fill") for item in strip.find_all()]
        self.assertEqual(
            fills,
            [theme.pick(theme.SUCCESS), theme.pick(theme.WARNING), theme.pick(theme.CHIP_HOVER)],
        )
        strip.show([])
        self.assertEqual(strip.find_all(), ())

    def test_the_squares_wrap_a_long_season(self) -> None:
        squares = EpisodeSquares(self.root)
        squares.show([(n, "plex") for n in range(1, 23)])  # 22 episodes: two rows
        numbers = [squares.itemcget(i, "text") for i in squares.find_all() if squares.type(i) == "text"]
        self.assertEqual(numbers, [str(n) for n in range(1, 23)])
        one_row = int(squares.cget("height"))
        squares.show([(1, "plex")])
        self.assertLess(int(squares.cget("height")), one_row)
