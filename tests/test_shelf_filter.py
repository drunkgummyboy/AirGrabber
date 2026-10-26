"""The Discover / Movies filter: genre chips (TMDB ids → plain names), "Hide what I already
have", and the shelves they leave."""

from __future__ import annotations

import asyncio
import gc
import json
import tkinter as tk
import unittest
from pathlib import Path
from typing import Any

import customtkinter as ctk

from app.models import Movie, Settings
from app.models.tmdb import TMDBMovieItem, TMDBTVItem
from app.services import SettingsService
from app.ui import theme
from app.ui.shelf_filter import (
    ALL_TEXT,
    HIDE_OWNED_TEXT,
    MOVIE_GENRES,
    TV_GENRES,
    Genre,
    chip_color,
    chip_genres,
    empty_text,
    genres_of,
)
from app.ui.views.shelves import Shelf, ShelvesView, filter_shelves, has_items

FIXTURES = Path(__file__).parent / "fixtures" / "tmdb"


def shelf(title: str, *items: int, failed: bool = False) -> Shelf[int]:
    return Shelf("🔥", title, theme.SECTION_HOT, items, failed)


class GenreTests(unittest.TestCase):
    def test_movie_and_tv_ids_share_the_plain_names(self) -> None:
        self.assertEqual(genres_of((28, 12), MOVIE_GENRES), {Genre.ACTION})  # Adventure: none
        self.assertEqual(genres_of((10759,), TV_GENRES), {Genre.ACTION})  # Action & Adventure
        self.assertEqual(genres_of((878, 14), MOVIE_GENRES), {Genre.SCI_FI})
        self.assertEqual(genres_of((10765,), TV_GENRES), {Genre.SCI_FI})
        self.assertEqual(genres_of((10751,), MOVIE_GENRES), {Genre.FAMILY})
        self.assertEqual(genres_of((10751, 10762), TV_GENRES), {Genre.FAMILY})  # Family, Kids
        for shared in (35, 18, 16, 99, 80):  # Comedy, Drama, Animation, Documentary, Crime
            with self.subTest(shared):
                self.assertEqual(genres_of((shared,), MOVIE_GENRES), genres_of((shared,), TV_GENRES))
                self.assertTrue(genres_of((shared,), MOVIE_GENRES))

    def test_ids_without_a_chip_are_ignored(self) -> None:
        self.assertEqual(genres_of((53, 9648, 10402), MOVIE_GENRES), frozenset())  # Thriller...
        self.assertEqual(genres_of((10764, 10767), TV_GENRES), frozenset())  # Reality, Talk
        self.assertEqual(genres_of((27,), TV_GENRES), frozenset())  # a film id, not a TV one
        self.assertEqual(genres_of((), MOVIE_GENRES), frozenset())

    def test_every_genre_has_a_film_id(self) -> None:
        self.assertEqual(set(MOVIE_GENRES.values()), set(Genre))
        self.assertEqual(set(Genre) - set(TV_GENRES.values()), {Genre.HORROR})

    def test_recorded_lists_have_genres(self) -> None:
        def results(name: str) -> list[dict[str, Any]]:
            payload: dict[str, Any] = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
            return list(payload["results"])

        shows = [TMDBTVItem.model_validate(item).to_discover_show() for item in results("tv_trending.json")]
        films = [TMDBMovieItem.model_validate(item).to_movie() for item in results("movie_trending.json")]
        self.assertEqual(genres_of(shows[0].genre_ids, TV_GENRES), {
            Genre.ANIMATION, Genre.SCI_FI, Genre.DRAMA,  # LINK CLICK; Mystery has no chip
        })
        self.assertTrue(any(genres_of(film.genre_ids, MOVIE_GENRES) for film in films))

    def test_chips_are_the_genres_on_the_page_in_order(self) -> None:
        titles = [frozenset({Genre.HORROR, Genre.COMEDY}), frozenset(), frozenset({Genre.ACTION})]
        self.assertEqual(
            chip_genres(titles, None), (Genre.ACTION, Genre.COMEDY, Genre.HORROR)
        )
        # The selected genre stays offered when no title has it (any more).
        self.assertEqual(chip_genres(titles, Genre.DRAMA)[:3], (Genre.ACTION, Genre.COMEDY, Genre.DRAMA))
        self.assertEqual(chip_genres([], None), ())

    def test_texts(self) -> None:
        self.assertEqual(Genre.FAMILY.value, "Kids & Family")
        self.assertEqual(
            empty_text(Genre.HORROR, False, "unused"),
            f"Nothing in Horror here right now. Pick another genre or {ALL_TEXT}.",
        )
        self.assertEqual(
            empty_text(Genre.HORROR, True, "unused"),
            f"Nothing new in Horror here right now. Pick another genre or {ALL_TEXT}.",
        )
        self.assertEqual(empty_text(None, True, "You already have every film here."),
                         "You already have every film here.")
        self.assertEqual(chip_color(True), (theme.ACCENT, theme.ACCENT_HOVER))
        self.assertEqual(chip_color(False), (theme.CHIP, theme.CHIP_HOVER))


class FilterShelvesTests(unittest.TestCase):
    def test_keeps_the_accepted_items_and_hides_emptied_shelves(self) -> None:
        shelves = [shelf("A", 1, 2, 3), shelf("B", 4, 5), shelf("C")]
        shown = filter_shelves(shelves, lambda item: item % 2 == 1)
        self.assertEqual([(s.title, tuple(s.items)) for s in shown], [("A", (1, 3)), ("B", (5,))])
        shown = filter_shelves(shelves, lambda item: item < 3)
        self.assertEqual([(s.title, tuple(s.items)) for s in shown], [("A", (1, 2))])
        # Keeping everything only drops the shelves that were empty already.
        self.assertEqual([s.title for s in filter_shelves(shelves, lambda _: True)], ["A", "B"])

    def test_a_failed_shelf_stays(self) -> None:
        shelves = [shelf("A", 1), shelf("B", failed=True)]
        shown = filter_shelves(shelves, lambda _: False)
        self.assertEqual([(s.title, s.failed) for s in shown], [("B", True)])
        self.assertFalse(has_items(shown))
        self.assertTrue(has_items(shelves))


# -- the page --------------------------------------------------------------------------------


class MemoryRepository:
    def __init__(self) -> None:
        self.saved: list[Settings] = []

    async def save(self, settings: Settings) -> None:
        self.saved.append(settings)


def film(movie_id: int, *genre_ids: int) -> Movie:
    return Movie(id=movie_id, title=f"Film {movie_id}", genre_ids=genre_ids)


class FakeMoviesView(ShelvesView[Movie]):
    _hide_owned_setting = "hide_owned_movies"
    _hide_owned_tip = "Hides the films you've already downloaded"
    _all_owned_text = "You already have every film here."

    def __init__(self, master: Any, settings: SettingsService, shelves: list[Shelf[Movie]]) -> None:
        images: Any = None
        super().__init__(
            master,
            images=images,
            status=lambda text, *, clear_after_ms=None: None,
            settings=settings,
            on_open_settings=lambda: None,
        )
        self.shelves = shelves
        self.owned: set[int] = set()
        self.shown: list[int] = []  # the films with a card, in order

    async def _fetch_shelves(self, *, force: bool) -> list[Shelf[Movie]]:
        return self.shelves

    def _card(self, master: Any, item: Movie) -> None:
        ctk.CTkLabel(master, text=item.title)
        self.shown.append(item.id)

    def _clear_body(self) -> None:
        super()._clear_body()
        self.shown = []

    def _genres(self, item: Movie) -> frozenset[Genre]:
        return genres_of(item.genre_ids, MOVIE_GENRES)

    def _is_owned(self, item: Movie) -> bool:
        return item.id in self.owned


class PageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0  # building widgets is slow
        self.repository = MemoryRepository()
        repository: Any = self.repository
        self.settings = SettingsService(repository, Settings())
        shelves = [
            Shelf("📀", "Just Dropped", theme.SECTION_NEW, (film(1, 28), film(2, 35, 18))),
            Shelf("🔥", "Trending", theme.SECTION_HOT, (film(3, 27), film(4, 28, 878))),
            Shelf("📅", "Upcoming", theme.TEXT_MUTED, (), failed=True),
        ]
        self.view = FakeMoviesView(self.root, self.settings, shelves)
        self.view.pack(fill="both", expand=True)

    async def settle(self) -> None:
        while self.view._tasks:
            await asyncio.gather(*self.view._tasks)
        self.root.update()

    def body_texts(self) -> list[str]:
        """The texts of the labels directly in the page's body (the empty line)."""
        return [
            child.cget("text")
            for child in self.view._body.winfo_children()
            if isinstance(child, ctk.CTkLabel)
        ]

    async def test_chips_filter_every_shelf(self) -> None:
        await self.view.load()
        bar = self.view.filter_bar
        self.assertTrue(bar.winfo_manager())
        self.assertEqual(
            bar.genres,
            (Genre.ACTION, Genre.COMEDY, Genre.DRAMA, Genre.SCI_FI, Genre.HORROR),
        )
        self.assertIsNone(bar.selected)
        self.assertEqual(self.view.shown, [1, 2, 3, 4])

        bar._pick(Genre.ACTION)
        await self.settle()
        self.assertEqual(self.view.shown, [1, 4])
        self.assertEqual(bar.selected, Genre.ACTION)
        bar._pick(Genre.HORROR)  # the Just Dropped shelf hides
        await self.settle()
        self.assertEqual(self.view.shown, [3])
        bar._pick(None)
        await self.settle()
        self.assertEqual(self.view.shown, [1, 2, 3, 4])
        self.assertEqual(self.settings.current, Settings())  # the chip isn't a setting

    async def test_the_chip_survives_a_reload(self) -> None:
        await self.view.load()
        self.view.filter_bar._pick(Genre.COMEDY)
        await self.settle()
        await self.view.load()
        self.assertEqual(self.view.shown, [2])
        self.assertEqual(self.view.filter_bar.selected, Genre.COMEDY)

    async def test_hide_what_i_already_have_is_saved(self) -> None:
        self.view.owned = {1, 4}
        await self.view.load()
        self.assertFalse(self.view.filter_bar.hide_owned)
        self.assertEqual(self.view.shown, [1, 2, 3, 4])

        self.view.filter_bar._switch.toggle()
        await self.settle()
        self.assertEqual(self.view.shown, [2, 3])
        self.assertTrue(self.settings.current.hide_owned_movies)
        self.assertEqual(len(self.repository.saved), 1)
        # Only the films still shown offer their genres.
        self.assertNotIn(Genre.ACTION, self.view.filter_bar.genres)
        self.assertNotIn(Genre.SCI_FI, self.view.filter_bar.genres)

    async def test_the_switch_starts_from_settings(self) -> None:
        await self.settings.update(hide_owned_movies=True)
        view = FakeMoviesView(self.root, self.settings, self.view.shelves)
        view.owned = {1, 2, 3, 4}
        await view.load()
        self.assertTrue(view.filter_bar.hide_owned)
        self.assertEqual(view.shown, [])
        texts = [
            child.cget("text")
            for child in view._body.winfo_children()
            if isinstance(child, ctk.CTkLabel)
        ]
        self.assertEqual(texts, ["You already have every film here."])
        self.assertEqual(view.filter_bar._switch.cget("text"), HIDE_OWNED_TEXT)

    async def test_a_friendly_line_when_nothing_is_left(self) -> None:
        self.view.owned = {3}
        await self.view.load()
        self.view.filter_bar._pick(Genre.HORROR)
        await self.settle()
        self.assertEqual(self.view.shown, [3])
        self.view._set_hide_owned(True)
        await self.settle()
        self.assertEqual(self.view.shown, [])
        self.assertEqual(self.body_texts(), [empty_text(Genre.HORROR, True, "")])
        # Horror stays offered while selected, so it can be switched off.
        self.assertIn(Genre.HORROR, self.view.filter_bar.genres)
        self.view._set_hide_owned(False)
        await self.settle()
        self.assertEqual(self.view.shown, [3])
        self.assertEqual(self.body_texts(), [])


if __name__ == "__main__":
    unittest.main()
