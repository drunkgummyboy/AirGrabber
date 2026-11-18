"""The Movies page's See more: under each TMDB shelf with more, the next page added below its
cards (app/ui/views/shelves.py, app/ui/views/movie_view.py)."""

from __future__ import annotations

import asyncio
import gc
import tkinter as tk
import unittest
from collections.abc import Iterator, Sequence
from typing import Any
from unittest import mock

import customtkinter as ctk

from app.models import Movie, MovieDashboard, MoviePage, Settings
from app.services import SettingsService
from app.services.tmdb import TMDBError
from app.ui import theme
from app.ui.shelf_filter import MOVIE_GENRES, Genre, genres_of
from app.ui.views.movie_view import MovieView
from app.ui.views.shelves import (
    SEE_MORE_FAILED,
    SEE_MORE_LOADING,
    SEE_MORE_TEXT,
    Shelf,
    ShelvesView,
)

ACTION, COMEDY, HORROR = 28, 35, 27


def film(movie_id: int, *genre_ids: int) -> Movie:
    return Movie(id=movie_id, title=f"Film {movie_id}", genre_ids=genre_ids)


def ids(movies: Sequence[Movie]) -> list[int]:
    return [movie.id for movie in movies]


class MemoryRepository:
    async def save(self, settings: Settings) -> None:
        pass


class FakeWatchlistShelf(ctk.CTkFrame):
    """The watchlist shelf: stored data, never See more."""

    def __init__(self, master: Any, **_: Any) -> None:
        super().__init__(master)

    def start(self) -> None:
        pass


class FakeDiscovery:
    """The dashboard and its next pages, as DiscoveryService gives them."""

    def __init__(self) -> None:
        self.dashboard = MovieDashboard(
            just_dropped=(film(1, ACTION), film(2, COMEDY)),
            trending=(film(3, ACTION), film(4, HORROR)),
            upcoming=(film(5, COMEDY),),
            more=frozenset({"just_dropped", "trending"}),
            listing=7,
        )
        self.pages: dict[tuple[str, int], MoviePage | Exception] = {}
        self.asked: list[tuple[str, int]] = []
        self.forced: list[bool] = []
        self.also: list[list[int]] = []
        self.listings: list[int] = []
        self.gate: asyncio.Event | None = None
        self.dashboard_gate: asyncio.Event | None = None

    async def movie_dashboard(self, *, force: bool = False, also: Sequence[int] = ()) -> MovieDashboard:
        self.forced.append(force)
        self.also.append(list(also))
        if self.dashboard_gate is not None:
            await self.dashboard_gate.wait()
        return self.dashboard

    async def plex_films(self) -> Any:  # the watchlist shelf's (not built here)
        raise AssertionError("not asked")

    async def more_movies(self, shelf: str, page: int, *, listing: int = 0) -> MoviePage:
        self.asked.append((shelf, page))
        self.listings.append(listing)
        if self.gate is not None:
            await self.gate.wait()
        result = self.pages[(shelf, page)]
        if isinstance(result, Exception):
            raise result
        return result


class CardsMovieView(MovieView):
    """The Movies page with a label per card (its film's id)."""

    def _card(self, master: Any, item: Movie) -> None:
        ctk.CTkLabel(master, text=str(item.id))


class TkCase(unittest.IsolatedAsyncioTestCase):
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


class SeeMoreTests(TkCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        repository: Any = MemoryRepository()
        self.settings = SettingsService(repository, Settings())
        self.discovery = FakeDiscovery()
        self.reports: list[str] = []
        patcher = mock.patch("app.ui.views.movie_view.WatchlistShelf", FakeWatchlistShelf)
        patcher.start()
        self.addCleanup(patcher.stop)
        discovery: Any = self.discovery
        unused: Any = None
        self.view = CardsMovieView(
            self.root,
            discovery=discovery,
            images=unused,
            status=lambda text, *, clear_after_ms=None: self.reports.append(text),
            settings=self.settings,
            on_open_settings=lambda: None,
            movie_actions=unused,
            watchlist=unused,
        )
        self.view.pack(fill="both", expand=True)
        await self.view.load()

    async def settle(self) -> None:
        while self.view._tasks:
            await asyncio.gather(*self.view._tasks, return_exceptions=True)
        self.root.update()

    async def settle_render(self) -> None:
        """Until the filter's render has got to its end (See more still waiting); a render that
        never ends fails the test instead of hanging it."""

        async def ended() -> None:
            while self.view._rendered != self.view._render_generation:
                await asyncio.sleep(0.001)

        await asyncio.wait_for(ended(), timeout=10)
        self.root.update()

    def grid(self, key: str) -> Any:
        return self.view._parts[key].grid

    def cards(self, key: str) -> list[int]:
        """The films with a card on shelf `key`, in order."""
        return [int(child.cget("text")) for child in self.grid(key).winfo_children()]

    def button(self, key: str) -> ctk.CTkButton | None:
        return self.view._parts[key].button

    def see_more_buttons(self) -> list[ctk.CTkButton]:
        def walk(widget: Any) -> Iterator[Any]:
            for child in widget.winfo_children():
                yield child
                yield from walk(child)

        return [
            widget
            for widget in walk(self.view._body)
            if isinstance(widget, ctk.CTkButton) and widget.cget("text") == SEE_MORE_TEXT
        ]

    async def see_more(self, key: str) -> None:
        button = self.button(key)
        assert button is not None
        button.invoke()
        await self.settle()

    async def test_a_see_more_under_each_tmdb_shelf_with_more(self) -> None:
        self.assertEqual(set(self.view._parts), {"just_dropped", "trending", "upcoming"})
        for key in ("just_dropped", "trending"):
            button = self.button(key)
            assert button is not None
            self.assertEqual((button.cget("text"), button.cget("state")), ("See more", "normal"))
            self.assertIs(button.master, self.view._parts[key].section)  # under its own cards
        self.assertIsNone(self.button("upcoming"))  # TMDB's list has nothing more
        # Only those two: the watchlist shelf has none.
        self.assertEqual(len(self.see_more_buttons()), 2)

    async def test_no_button_under_a_shelf_that_failed(self) -> None:
        self.discovery.dashboard = self.discovery.dashboard.model_copy(
            update={"trending": (), "failed": ("trending",)}
        )
        await self.view.load()
        self.assertNotIn("trending", self.view._parts)
        self.assertEqual(len(self.see_more_buttons()), 1)

    async def test_the_next_page_is_added_below_the_cards_shown(self) -> None:
        self.discovery.pages[("trending", 2)] = MoviePage(
            movies=(film(4, HORROR), film(6, ACTION), film(7, COMEDY)), more=True
        )
        self.discovery.pages[("trending", 3)] = MoviePage(movies=(film(8, ACTION),), more=False)
        grid = self.grid("trending")
        first = grid.winfo_children()
        self.discovery.gate = asyncio.Event()
        button = self.button("trending")
        assert button is not None
        button.invoke()
        await asyncio.sleep(0.01)
        self.assertEqual((button.cget("text"), button.cget("state")), ("Loading…", "disabled"))
        self.assertEqual(SEE_MORE_LOADING, "Loading…")
        button.invoke()  # a second click meanwhile asks nothing more
        self.discovery.gate.set()
        await self.settle()

        self.assertEqual(self.discovery.asked, [("trending", 2)])
        self.assertEqual(self.discovery.listings, [7])  # goes on from the dashboard's lists
        self.assertIs(self.grid("trending"), grid)  # the same grid: nothing rebuilt
        self.assertEqual(grid.winfo_children()[:2], first)
        self.assertTrue(all(card.winfo_exists() for card in first))
        self.assertEqual(self.cards("trending"), [3, 4, 6, 7])  # 4 was there already
        self.assertFalse(grid.whole_rows)  # every card asked for shows
        self.assertIs(self.button("trending"), button)
        self.assertEqual((button.cget("text"), button.cget("state")), ("See more", "normal"))
        self.assertEqual(self.cards("just_dropped"), [1, 2])  # the other shelves as they were

        await self.see_more("trending")
        self.assertEqual(self.discovery.asked, [("trending", 2), ("trending", 3)])
        self.assertEqual(self.cards("trending"), [3, 4, 6, 7, 8])
        self.assertIsNone(self.button("trending"))  # the list's end
        self.assertFalse(button.winfo_exists())

    async def test_a_page_with_nothing_new_ends_it(self) -> None:
        self.discovery.pages[("trending", 2)] = MoviePage(
            movies=(film(3, ACTION), film(4, HORROR)), more=True
        )
        await self.see_more("trending")
        self.assertEqual(self.cards("trending"), [3, 4])
        self.assertIsNone(self.button("trending"))

    async def test_a_failure_brings_the_button_back(self) -> None:
        self.discovery.pages[("trending", 2)] = TMDBError("TMDB /trending/movie/week: HTTP 500")
        await self.see_more("trending")
        button = self.button("trending")
        assert button is not None
        self.assertEqual((button.cget("text"), button.cget("state")), ("See more", "normal"))
        self.assertEqual(self.reports, ["Couldn't load more of Trending Movies right now."])
        self.assertEqual(SEE_MORE_FAILED, "Couldn't load more of {title} right now.")
        self.assertEqual(self.cards("trending"), [3, 4])

        self.discovery.pages[("trending", 2)] = MoviePage(movies=(film(6, ACTION),), more=True)
        await self.see_more("trending")  # tried again: the same page
        self.assertEqual(self.discovery.asked, [("trending", 2), ("trending", 2)])
        self.assertEqual(self.cards("trending"), [3, 4, 6])

    async def test_a_reload_starts_every_shelf_at_page_1(self) -> None:
        self.discovery.pages[("trending", 2)] = MoviePage(movies=(film(6, ACTION),), more=True)
        self.discovery.pages[("just_dropped", 2)] = MoviePage(movies=(film(9, ACTION),), more=False)
        await self.see_more("trending")
        await self.see_more("just_dropped")
        self.assertIsNone(self.button("just_dropped"))

        self.view.reload()  # ↻ Refresh
        assert self.view._load_task is not None
        await self.view._load_task
        await self.settle()
        self.assertEqual(self.discovery.forced[-1], True)
        self.assertEqual(self.discovery.also[-1], [])
        self.assertEqual(self.cards("trending"), [3, 4])
        self.assertEqual(self.cards("just_dropped"), [1, 2])
        self.assertTrue(self.grid("trending").whole_rows)
        self.assertIsNotNone(self.button("just_dropped"))  # back, as the dashboard says
        await self.see_more("trending")
        self.assertEqual(self.discovery.asked[-1], ("trending", 2))

    async def test_a_late_page_after_a_load_is_dropped(self) -> None:
        self.discovery.pages[("trending", 2)] = MoviePage(
            movies=(film(6, ACTION),), more=True, in_plex=frozenset({6})
        )
        gate = self.discovery.gate = asyncio.Event()
        button = self.button("trending")
        assert button is not None
        button.invoke()
        await asyncio.sleep(0.01)
        late = list(self.view._tasks)
        await self.view.load()  # while the page was on its way
        gate.set()
        await asyncio.gather(*late)
        self.root.update()
        self.assertEqual(self.cards("trending"), [3, 4])  # nothing added
        new_button = self.button("trending")
        assert new_button is not None
        self.assertIsNot(new_button, button)
        self.assertEqual((new_button.cget("text"), new_button.cget("state")), ("See more", "normal"))
        self.assertFalse(self.view._is_owned(film(6)))  # what it said of its films is dropped too

    async def test_the_filter_applies_to_the_added_cards(self) -> None:
        self.discovery.dashboard = self.discovery.dashboard.model_copy(
            update={"plex_connected": True}
        )
        await self.view.load()
        self.view.filter_bar._pick(Genre.ACTION)
        await self.settle()
        self.assertEqual(self.cards("trending"), [3])
        self.discovery.pages[("trending", 2)] = MoviePage(
            movies=(film(6, ACTION), film(7, COMEDY), film(8, ACTION)),
            more=True,
            in_plex=frozenset({8}),
        )
        self.view._set_hide_owned(True)
        await self.settle()
        await self.see_more("trending")
        self.assertEqual(self.cards("trending"), [3, 6])  # 7: a comedy; 8: had
        self.assertIn(Genre.COMEDY, self.view.filter_bar.genres)  # the new film's genre is offered
        # The filter changed: the added films are there as the first ones are.
        self.view.filter_bar._pick(None)
        await self.settle()
        self.assertEqual(self.cards("trending"), [3, 4, 6, 7])
        self.view._set_hide_owned(False)
        await self.settle()
        self.assertEqual(self.cards("trending"), [3, 4, 6, 7, 8])
        self.assertIsNotNone(self.button("trending"))
        self.assertEqual(genres_of(film(8, ACTION).genre_ids, MOVIE_GENRES), {Genre.ACTION})

    async def test_a_quiet_reload_keeps_what_was_added(self) -> None:
        self.discovery.dashboard = self.discovery.dashboard.model_copy(
            update={"plex_connected": True}
        )
        await self.view.load()
        self.discovery.pages[("trending", 2)] = MoviePage(
            movies=(film(6, ACTION), film(7, COMEDY)), more=True
        )
        await self.see_more("trending")
        # The library changed: 7 is had now (the dashboard says so for the added films too).
        self.discovery.dashboard = self.discovery.dashboard.model_copy(
            update={"in_plex": frozenset({7})}
        )
        self.view._set_hide_owned(True)
        await self.settle()
        self.view.refresh_quietly()
        assert self.view._load_task is not None
        await self.view._load_task
        await self.settle()
        self.assertEqual(self.discovery.also[-1], [6, 7])
        self.assertEqual(self.cards("trending"), [3, 4, 6])
        self.discovery.pages[("trending", 3)] = MoviePage(movies=(film(10, ACTION),), more=False)
        await self.see_more("trending")
        self.assertEqual(self.discovery.asked[-1], ("trending", 3))  # it goes on from there


    async def test_a_page_landing_while_the_filter_renders_again(self) -> None:
        self.discovery.pages[("trending", 2)] = MoviePage(
            movies=(film(6, ACTION), film(7, COMEDY)), more=True
        )
        gate = self.discovery.gate = asyncio.Event()
        button = self.button("trending")
        assert button is not None
        button.invoke()
        await asyncio.sleep(0.01)
        self.view.filter_bar._pick(Genre.ACTION)  # renders the shelves again, one at a time
        async def first_shelf_only() -> None:
            while set(self.view._parts) != {"just_dropped"}:
                await asyncio.sleep(0)

        await asyncio.wait_for(first_shelf_only(), timeout=5)
        self.assertNotIn("trending", self.view._parts)  # not reached yet
        before = self.view._render_generation
        gate.set()
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertEqual(self.view._render_generation, before + 1)  # it starts again
        await self.settle()
        self.assertEqual(self.cards("trending"), [3, 6])  # 7: a comedy
        self.view.filter_bar._pick(None)
        await self.settle()
        self.assertEqual(self.cards("trending"), [3, 4, 6, 7])

    async def test_a_page_for_a_shelf_the_filter_has_hidden(self) -> None:
        self.discovery.pages[("just_dropped", 2)] = MoviePage(
            movies=(film(6, HORROR), film(7, COMEDY)), more=True
        )
        gate = self.discovery.gate = asyncio.Event()
        button = self.button("just_dropped")
        assert button is not None
        button.invoke()
        await asyncio.sleep(0.01)
        self.view.filter_bar._pick(Genre.HORROR)  # nothing of Just Dropped is Horror
        await self.settle_render()
        self.assertNotIn("just_dropped", self.view._parts)
        gate.set()
        await self.settle()
        self.assertEqual(self.cards("just_dropped"), [6])  # back, with the Horror film
        self.assertEqual(self.cards("trending"), [4])

    async def test_a_page_for_a_hidden_shelf_that_the_filter_still_hides(self) -> None:
        self.discovery.pages[("just_dropped", 2)] = MoviePage(
            movies=(film(6, COMEDY), film(7, 16)), more=True  # 16: Animation
        )
        gate = self.discovery.gate = asyncio.Event()
        button = self.button("just_dropped")
        assert button is not None
        button.invoke()
        await asyncio.sleep(0.01)
        self.view.filter_bar._pick(Genre.HORROR)
        await self.settle_render()
        before = self.view._render_generation
        gate.set()
        await self.settle()
        self.assertEqual(self.view._render_generation, before)  # nothing to show: no render
        self.assertNotIn("just_dropped", self.view._parts)
        self.assertIn(Genre.ANIMATION, self.view.filter_bar.genres)  # its genre is offered
        self.view.filter_bar._pick(None)
        await self.settle()
        self.assertEqual(self.cards("just_dropped"), [1, 2, 6, 7])

    async def test_a_page_landing_during_a_quiet_reload_stays(self) -> None:
        self.discovery.dashboard = self.discovery.dashboard.model_copy(
            update={"plex_connected": True}
        )
        await self.view.load()
        self.discovery.pages[("trending", 2)] = MoviePage(
            movies=(film(6, ACTION), film(7, COMEDY)), more=True
        )
        await self.see_more("trending")
        grid = self.grid("trending")
        cards = grid.winfo_children()
        # A Plex check finishes: the quiet reload waits on the dashboard...
        gate = self.discovery.dashboard_gate = asyncio.Event()
        self.view.refresh_quietly()
        await asyncio.sleep(0.01)
        self.assertEqual(self.discovery.also[-1], [6, 7])
        # ...while See more brings page 3 (one of its films had).
        self.discovery.pages[("trending", 3)] = MoviePage(
            movies=(film(8, ACTION),), more=True, in_plex=frozenset({8})
        )
        await self.see_more("trending")
        self.assertEqual(self.cards("trending"), [3, 4, 6, 7, 8])
        gate.set()
        assert self.view._load_task is not None
        await self.view._load_task
        await self.settle()
        # Kept as it was: the same grid and cards, nothing loaded again, the page goes on.
        self.assertEqual(self.discovery.forced, [False, False, False])
        self.assertIs(self.grid("trending"), grid)
        self.assertEqual(grid.winfo_children(), [*cards, *grid.winfo_children()[len(cards):]])
        self.assertTrue(all(card.winfo_exists() for card in cards))
        self.assertEqual(self.cards("trending"), [3, 4, 6, 7, 8])
        self.assertTrue(self.view._is_owned(film(8)))  # its page said so; kept
        self.discovery.pages[("trending", 4)] = MoviePage(movies=(film(9, ACTION),), more=False)
        await self.see_more("trending")
        self.assertEqual(self.discovery.asked[-1], ("trending", 4))


class NoSeeMoreView(ShelvesView[Movie]):
    """A page without See more (Discover's, for now), even with shelves that could page."""

    _hide_owned_setting = "hide_owned_movies"
    _hide_owned_tip = "tip"
    _all_owned_text = "all"

    async def _fetch_shelves(self, *, force: bool) -> list[Shelf[Movie]]:
        return [Shelf("🔥", "Trending", theme.SECTION_HOT, (film(1),), key="trending", more=True)]

    def _card(self, master: Any, item: Movie) -> None:
        ctk.CTkLabel(master, text=str(item.id))

    def _genres(self, item: Movie) -> frozenset[Genre]:
        return frozenset()

    def _is_owned(self, item: Movie) -> bool:
        return False


class OptInTests(TkCase):
    async def test_a_page_without_see_more_shows_no_button(self) -> None:
        repository: Any = MemoryRepository()
        unused: Any = None
        view = NoSeeMoreView(
            self.root,
            images=unused,
            status=lambda text, *, clear_after_ms=None: None,
            settings=SettingsService(repository, Settings()),
            on_open_settings=lambda: None,
        )
        await view.load()
        self.assertEqual(view._parts, {})
        buttons = [
            widget
            for section in view._body.winfo_children()
            for widget in section.winfo_children()
            if isinstance(widget, ctk.CTkButton)
        ]
        self.assertEqual(buttons, [])


if __name__ == "__main__":
    unittest.main()
