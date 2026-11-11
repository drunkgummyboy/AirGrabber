"""Quiet reloads: a library change under the page on screen (a show tracked) reloads it
without a blank or loading state (BaseView.refresh_quietly). The calendar's and My shows' own
cases are in tests/test_calendar_view.py and tests/test_tracked_view.py; here the base, the
TMDB shelves (Discover, Movies) and the main window's hand-over."""

from __future__ import annotations

import asyncio
import gc
import tkinter as tk
import unittest
from types import SimpleNamespace
from typing import Any
from unittest import mock

import customtkinter as ctk

from app.models.plex import PlexShows
from app.models import (
    DiscoverShow,
    DiscoverShowEntry,
    GrabTarget,
    MediaType,
    Movie,
    Settings,
    TVDashboard,
    TVDetails,
)
from app.services import SettingsService
from app.ui import poster_card, theme
from app.ui.main_window import MainWindow
from app.ui.media_cards import DiscoverShowCard
from app.ui.views.base import BaseView
from app.ui.views.discover_view import DiscoverView
from app.ui.views.shelves import Shelf, same_items
from app.ui.widgets import flex_cell
from tests.test_shelf_filter import FakeMoviesView, MemoryRepository, film


def same_widgets(now: list[Any], before: list[Any]) -> bool:
    """The very same widget objects (Tk reuses the names of destroyed ones)."""
    return len(now) == len(before) and all(a is b for a, b in zip(now, before, strict=True))


class _Recorder(BaseView):
    def __init__(self, master: Any) -> None:
        super().__init__(master)
        self.seen: list[bool] = []

    async def load(self) -> None:
        self.seen.append(self.quiet_load)


class _NoImages:
    async def get(self, url: str | None, size: tuple[int, int], **_: Any) -> None:
        return None


class _Discovery:
    """TMDB's TV shelves from lists the test changes; the flags as the library has them."""

    def __init__(self) -> None:
        self.trending = [_show(1), _show(2), _show(3)]
        self.on_air = [_show(4)]
        self.followed: set[int] = set()
        self.fetches = 0

    async def plex_shows(self) -> PlexShows:
        return PlexShows()

    async def tv_dashboard(self, *, profile_id: int, force: bool = False) -> TVDashboard:
        self.fetches += 1

        def entries(shows: list[DiscoverShow]) -> tuple[DiscoverShowEntry, ...]:
            return tuple(
                DiscoverShowEntry(show=show, is_tracked=show.id in self.followed) for show in shows
            )

        return TVDashboard(trending=entries(self.trending), on_air=entries(self.on_air))

    async def tv_details(self, tmdb_id: int) -> TVDetails:
        return TVDetails(id=tmdb_id)


def _show(tmdb_id: int) -> DiscoverShow:
    return DiscoverShow(id=tmdb_id, name=f"Show {tmdb_id}")


class _PageCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        gc.collect()
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

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0  # building widgets is slow
        repository: Any = MemoryRepository()
        self.settings = SettingsService(repository, Settings())

    async def quietly(self, view: BaseView) -> None:
        view.refresh_quietly()
        assert view._load_task is not None
        await view._load_task


class BaseViewTests(_PageCase):
    async def test_the_load_knows_how_it_was_asked_for(self) -> None:
        view = _Recorder(self.root)
        for start in (view.refresh, view.refresh_quietly, view.refresh):
            start()
            assert view._load_task is not None
            await view._load_task
        self.assertEqual(view.seen, [False, True, False])
        view.invalidate()
        view.refresh_if_stale()  # the page shown again: an ordinary load
        assert view._load_task is not None
        await view._load_task
        self.assertEqual(view.seen[-1], False)


class ShelvesTests(_PageCase):
    def test_same_items(self) -> None:
        def key(movie: Movie) -> object:
            return movie.id

        old = [Shelf("🔥", "Trending", theme.TEXT, (film(1), film(2)))]
        recoloured = Shelf("🔥", "Trending", theme.LINK, (film(1), film(2)))
        self.assertTrue(same_items(old, [recoloured], key))
        swapped = Shelf("🔥", "Trending", theme.TEXT, (film(2), film(1)))
        self.assertFalse(same_items(old, [swapped], key))
        self.assertFalse(same_items(old, [Shelf("🔥", "Trending", theme.TEXT, (film(1),))], key))
        self.assertFalse(same_items(old, [*old, Shelf("📅", "Upcoming", theme.TEXT, ())], key))
        failed = Shelf("🔥", "Trending", theme.TEXT, (film(1), film(2)), failed=True)
        self.assertFalse(same_items(old, [failed], key))

    async def page(self) -> FakeMoviesView:
        shelves = [
            Shelf("📀", "Just Dropped", theme.TEXT, (film(1, 28), film(2, 35))),
            Shelf("🔥", "Trending", theme.TEXT, (film(3, 27),)),
        ]
        view = FakeMoviesView(self.root, self.settings, shelves)
        view.pack(fill="both", expand=True)
        await view.load()
        return view

    async def test_nothing_changed_nothing_rebuilt(self) -> None:
        view = await self.page()
        children = view._body.winfo_children()
        with mock.patch("app.ui.views.shelves.LoadingBar") as loading:
            await self.quietly(view)
        loading.assert_not_called()
        self.assertTrue(same_widgets(view._body.winfo_children(), children))
        self.assertEqual(view.shown, [1, 2, 3])

    async def test_an_owned_change_the_page_cant_show_in_place_renders_again(self) -> None:
        view = await self.page()
        children = view._body.winfo_children()

        async def fetch(*, force: bool) -> list[Shelf[Movie]]:
            view.owned = {2}  # as a fetch learns it (Movies: the downloaded films)
            return view.shelves

        view._fetch_shelves = fetch  # type: ignore[method-assign]
        with mock.patch("app.ui.views.shelves.LoadingBar") as loading:
            await self.quietly(view)
        loading.assert_not_called()  # no loading state: rendered from the fetched shelves
        self.assertFalse(same_widgets(view._body.winfo_children(), children))
        self.assertEqual(view.shown, [1, 2, 3])

    async def test_other_items_load_as_usual(self) -> None:
        view = await self.page()
        view.shelves = [Shelf("📀", "Just Dropped", theme.TEXT, (film(5),))]
        with mock.patch("app.ui.views.shelves.LoadingBar") as loading:
            await self.quietly(view)
        loading.assert_called_once()
        self.assertEqual(view.shown, [5])


class DiscoverTests(_PageCase):
    async def page(self, **settings: Any) -> tuple[DiscoverView, _Discovery]:
        if settings:
            await self.settings.update(**settings)
        discovery = _Discovery()
        service: Any = discovery
        images: Any = _NoImages()
        view = DiscoverView(
            self.root,
            discovery=service,
            images=images,
            status=lambda text, *, clear_after_ms=None: None,
            settings=self.settings,
            on_open_settings=lambda: None,
        )
        view.pack(fill="both", expand=True)
        await view.load()
        return view, discovery

    def card(self, view: DiscoverView, show_id: int) -> DiscoverShowCard:
        (card,) = view._cards[show_id]
        return card

    def label(self, card: DiscoverShowCard) -> str:
        return str(card._track_button.cget("text"))

    async def test_a_tracked_show_turns_following_in_place(self) -> None:
        view, discovery = await self.page()
        cards = {show_id: self.card(view, show_id) for show_id in (1, 2, 3, 4)}
        sections = view._body.winfo_children()
        self.assertEqual(self.label(cards[2]), "+ Follow")
        discovery.followed.add(2)  # tracked from the search window, say
        with mock.patch("app.ui.views.shelves.LoadingBar") as loading:
            await self.quietly(view)
        loading.assert_not_called()
        self.assertTrue(same_widgets(view._body.winfo_children(), sections))
        for show_id, card in cards.items():
            self.assertIs(self.card(view, show_id), card)  # the same cards, still there
        self.assertEqual(self.label(cards[2]), "✓ Following")
        self.assertEqual(self.label(cards[1]), "+ Follow")
        discovery.followed.clear()  # untracked again
        await self.quietly(view)
        self.assertEqual(self.label(cards[2]), "+ Follow")

    async def test_hidden_when_owned_the_card_goes_and_its_shelf_closes_up(self) -> None:
        view, discovery = await self.page(hide_owned_discover=True)
        sections = view._body.winfo_children()
        keep = self.card(view, 3)
        discovery.followed.add(2)
        await self.quietly(view)
        self.assertNotIn(2, view._cards)
        self.assertIs(self.card(view, 3), keep)
        self.assertTrue(same_widgets(view._body.winfo_children(), sections))
        grid: Any = keep.master
        cards = grid.winfo_children()
        self.assertEqual(len(cards), 2)  # shows 1 and 3, in the first two places
        places = [(int(c.grid_info()["row"]), int(c.grid_info()["column"])) for c in cards]
        cols = grid._current_cols
        self.assertEqual(places, [flex_cell(n, cols, center=grid.center) for n in range(2)])

    async def test_a_shelf_left_empty_renders_again(self) -> None:
        view, discovery = await self.page(hide_owned_discover=True)
        discovery.followed.add(4)  # the only show of Currently Airing
        await self.quietly(view)
        self.assertNotIn(4, view._cards)
        titles = [
            str(label.cget("text"))
            for section in view._body.winfo_children()
            for label in section.winfo_children()
            if isinstance(label, ctk.CTkLabel)
        ]
        self.assertFalse(any("Currently Airing" in title for title in titles))
        self.assertTrue(any("Trending This Week" in title for title in titles))

    async def test_a_tracked_show_while_its_card_is_busy_keeps_its_own_state(self) -> None:
        view, discovery = await self.page()
        card = self.card(view, 1)
        view._tracking.add(1)  # this page's own + Follow, still running
        card.set_track_state("busy")
        discovery.followed.add(1)
        await self.quietly(view)
        self.assertEqual(self.label(card), "Finding…")  # the page's own run sets it after


class MainWindowTests(unittest.TestCase):
    def test_a_library_change_reloads_the_page_on_screen_quietly(self) -> None:
        visible = mock.Mock()
        other = mock.Mock()
        window: Any = SimpleNamespace(
            _series_views={"calendar": visible, "tracked": other},
            _movie_view=mock.Mock(),
            _library_view=mock.Mock(),
            _visible_view=lambda: visible,
            _band=mock.Mock(),
        )
        MainWindow._reload_pages(window, quiet=True)
        visible.refresh_quietly.assert_called_once_with()
        visible.refresh.assert_not_called()
        other.invalidate.assert_called_once_with()
        other.refresh_quietly.assert_not_called()
        MainWindow._reload_pages(window)  # settings, a grab, another person: as before
        visible.refresh.assert_called_once_with()
        window._reload_pages = lambda **kwargs: window.calls.append(kwargs)
        window.calls = []
        window._show_library_summary = mock.AsyncMock()
        with mock.patch("app.ui.main_window.spawn") as spawn:
            MainWindow._on_library_changed(window)
        self.assertEqual(window.calls, [{"quiet": True}])
        spawn.assert_called_once()
        spawn.call_args.args[0].close()  # the summary coroutine, never run here


    def test_a_grab_updates_the_page_on_screen_in_place_when_it_can(self) -> None:
        visible = mock.Mock()
        visible.take_grab.return_value = True
        other = mock.Mock()
        window: Any = SimpleNamespace(
            _series_views={"calendar": visible, "tracked": other},
            _movie_view=mock.Mock(),
            _library_view=mock.Mock(),
            _visible_view=lambda: visible,
            _band=mock.Mock(),
        )
        target = GrabTarget(media_type=MediaType.TV, media_id=1, season=1, episode=1)
        MainWindow._on_grab(window, target)
        visible.take_grab.assert_called_once_with(target)
        visible.refresh.assert_not_called()
        visible.invalidate.assert_not_called()
        other.invalidate.assert_called_once_with()
        window._band.reload.assert_called_once_with()
        visible.take_grab.return_value = False  # a page that can't: reloaded
        MainWindow._on_grab(window, target)
        visible.refresh.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
