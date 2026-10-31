"""The film and series info windows without the Windows title bar (app/ui/title_info.py with
app/ui/borderless.py): the round ✕ in the backdrop's top-right corner, showing the backdrop's
piece around its disc (and the background without one), dragging by the band but never from the
poster or the trailer button, the plain band "side" keeps without a backdrop, the "tabs" text
column clear of the ✕, its scrollbar under it and the ✕ following the scroll, Escape. The Win32
side is a recording fake; hidden Tk root, fake services."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest import mock

import customtkinter as ctk
from PIL import Image

from app.models import IndexerState, IndexerStatus, InfoLayout
from app.ui import borderless, theme
from app.ui.borderless import CLOSE_GAP, CLOSE_ROOM, NO_DRAG, clearance, starts_drag
from app.ui.title_info import (
    PAD_X,
    SIDE_STRIP,
    SIDE_TOP,
    Masthead,
    TitleInfoWindow,
    masthead_layout,
)
from tests.test_borderless import FakeNative
from tests.test_info_layouts import FILM, FOUND, SHOW, WindowsTestCase
from tests.test_release_panel import FakeSearch


class LayoutTests(WindowsTestCase):
    def test_side_keeps_a_plain_band_for_the_close_button(self) -> None:
        bare = masthead_layout(1800, 1.5, hero=False, layout=InfoLayout.SIDE)
        self.assertEqual(bare.hero_height, 0)  # no backdrop, no trailer button
        self.assertEqual(bare.band_height, round(SIDE_STRIP * 1.5))
        self.assertEqual(bare.poster[1], round(SIDE_TOP * 1.5))  # where it always started
        self.assertGreaterEqual(SIDE_STRIP, CLOSE_ROOM)
        full = masthead_layout(1800, 1.5, hero=True, layout=InfoLayout.SIDE)
        self.assertEqual(full.band_height, full.hero_height)

    def test_tabs_text_stays_clear_of_the_close_button_without_a_backdrop(self) -> None:
        bare = masthead_layout(1245, 1.5, hero=False, layout=InfoLayout.TABS)
        room = 1245 - bare.text[0] - round(PAD_X * 1.5)
        self.assertEqual(bare.text_width, room - round(clearance(PAD_X) * 1.5))
        full = masthead_layout(1245, 1.5, hero=True, layout=InfoLayout.TABS)
        self.assertEqual(full.text_width, 1245 - full.text[0] - full.poster[0])


class BorderlessWindowTests(WindowsTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.native = FakeNative()
        previous = borderless.set_native(self.native)
        self.addCleanup(borderless.set_native, previous)

    async def film(self, layout: InfoLayout) -> TitleInfoWindow:
        _shows, movies, _search = self.make(layout)
        movies.open_movie(FILM)
        await self.settle()
        return self.film_window(movies)

    async def series(self, layout: InfoLayout) -> TitleInfoWindow:
        shows, _movies, _search = self.make(layout)
        shows.open_show(SHOW)
        await self.settle()
        return self.show_window(shows)

    @staticmethod
    def picture(masthead: Masthead, color: tuple[int, int, int]) -> None:
        """A backdrop, as the masthead shows one once ImageCache baked it."""
        width = max(masthead.winfo_width(), 400)
        height = max(int(masthead.cget("height")), 100)
        masthead._hero_image = Image.new("RGB", (width, height), color)
        masthead._backdrop_changed()

    async def test_every_window_has_the_close_button_and_escape(self) -> None:
        for layout in InfoLayout:
            for make in (self.film, self.series):
                window = await make(layout)
                with self.subTest(layout=layout, window=type(window).__name__):
                    assert window.borderless is not None
                    self.assertIn(window.winfo_id(), [w for w, _dark in self.native.attached])
                    self.assertTrue(window.bind("<Escape>"))
                    button = window.borderless.close_button
                    self.assertEqual(button.place_info()["anchor"], "ne")
                    button.invoke()
                    self.root.update()
                    self.assertFalse(window.winfo_exists())

    async def test_dragged_by_the_band_not_by_the_poster_or_the_trailer(self) -> None:
        for layout in InfoLayout:
            window = await self.film(layout)
            masthead = window.masthead
            with self.subTest(layout=layout):
                with mock.patch.object(masthead, "find_withtag", return_value=()):
                    self.assertTrue(starts_drag(masthead, masthead))  # the band itself
                poster = masthead._poster_item
                self.assertIn(NO_DRAG, masthead.gettags(poster))
                with mock.patch.object(masthead, "find_withtag", return_value=(poster,)):
                    self.assertFalse(starts_drag(masthead, masthead))
                trailer = masthead._pill_item
                with mock.patch.object(masthead, "find_withtag", return_value=(trailer,)):
                    self.assertFalse(starts_drag(masthead, masthead))
                self.native.drags.clear()
                with mock.patch.object(masthead, "find_withtag", return_value=()):
                    masthead.event_generate("<ButtonPress-1>", x=600, y=5)
                self.assertEqual(self.native.drags, [window.winfo_id()])
                window.destroy()

    async def test_the_close_button_shows_the_backdrop_around_it(self) -> None:
        for layout in InfoLayout:
            window = await self.series(layout)
            assert window.borderless is not None
            button = window.borderless.close_button
            masthead = window.masthead
            with self.subTest(layout=layout):
                masthead.set_hero(None)
                self.assertFalse(button.over_picture)  # no backdrop: the window's background
                self.assertEqual(button.cget("background"), theme.pick(theme.BG))
                self.picture(masthead, (200, 30, 40))
                self.assertTrue(button.over_picture)
                piece = masthead.under(button)
                assert piece is not None
                self.assertEqual(piece.size, (button.winfo_width(), button.winfo_height()))
                self.assertEqual(piece.getpixel((0, 0)), (200, 30, 40))
                button.redraw()  # a switch between light and dark, another scaling: kept
                self.assertTrue(button.over_picture)
                masthead.set_hero("https://image.tmdb.org/t/p/w1280/b.jpg")
                masthead.set_hero(None)  # the backdrop gone
                self.assertFalse(button.over_picture)
                window.destroy()

    async def test_side_without_a_backdrop_keeps_the_strip(self) -> None:
        window = await self.film(InfoLayout.SIDE)
        masthead = window.masthead
        masthead.set_hero(None)
        self.root.update()
        scaling = ctk.ScalingTracker.get_widget_scaling(window)
        self.assertTrue(masthead.winfo_manager())  # the band stays, as a plain strip
        self.assertEqual(int(masthead.cget("height")), round(SIDE_STRIP * scaling))

    async def test_tabs_scrollbar_under_the_close_button_and_the_button_follows_the_scroll(self) -> None:
        window = await self.series(InfoLayout.TABS)
        assert window.borderless is not None and window.body is not None
        button = window.borderless.close_button
        scrollbar: Any = window.body._scrollbar
        scaling = ctk.ScalingTracker.get_widget_scaling(window)
        pady = scrollbar.grid_info()["pady"]
        top = pady[0] if isinstance(pady, tuple) else str(pady).split()[0]
        self.assertEqual(int(top), round((CLOSE_ROOM + CLOSE_GAP) * scaling))
        window.geometry("900x420")  # shorter than its content: it scrolls
        self.root.update()
        canvas = window.body._parent_canvas
        with mock.patch.object(button, "repaint_backdrop") as repaint:
            canvas.yview_moveto(1.0)
            self.root.update()
        self.assertGreater(canvas.yview()[0], 0.0)
        repaint.assert_called()


class TabsPageTests(BorderlessWindowTests):
    async def test_the_info_window_keeps_ctks_hide_and_show(self) -> None:
        window = await self.film(InfoLayout.TABS)
        # Its "tabs" layout settles while hidden (see make_borderless' hide_to_recolour).
        self.assertFalse(window._deactivate_windows_window_header_manipulation)

    async def test_a_quick_count_keeps_the_releases_page_showing(self) -> None:
        """CTk hides the pages it doesn't show 100 ms after a `set`, by name: the count (and so
        the tab's new name) arriving before that must not hide the Releases page."""
        window = await self.film(InfoLayout.TABS)
        assert window.tabs is not None
        for _ in range(3):  # past CTk's 100 ms and the window's own re-show
            await asyncio.sleep(0.12)
            self.root.update()
        self.assertEqual(window.tabs.get(), "Releases (0)")  # renamed by the quick count
        self.assertEqual(window.tabs.tab("Releases (0)").winfo_manager(), "grid")  # still showing


class PanelRowsTests(WindowsTestCase):
    """The releases panel's rows stay as tall as what they hold; only the table grows."""

    async def test_the_title_row_is_compact_without_and_with_indexer_chips(self) -> None:
        for search in (FakeSearch(FOUND), ChipSearch(FOUND)):
            for layout in InfoLayout:
                _shows, movies, _search = self.make(layout, search=search)
                movies.open_movie(FILM)
                await self.settle()
                window = self.film_window(movies)
                window.geometry("1200x586")
                await self.settle()
                panel = window.panel
                assert panel is not None
                scaling = ctk.ScalingTracker.get_widget_scaling(panel)
                top = panel._results_title.master
                with self.subTest(search=type(search).__name__, layout=layout):
                    self.assertLessEqual(top.winfo_height(), round(40 * scaling))
                    self.assertEqual(bool(panel._chips), isinstance(search, ChipSearch))
                window.destroy()
                movies.close()


class ChipSearch(FakeSearch):
    """FakeSearch with the indexers' statuses (the chips beside "Releases (n)")."""

    async def search(self, request: object) -> Any:
        async for update in super().search(request):
            statuses = (
                IndexerStatus(name="apibay", label="APIBay", state=IndexerState.FOUND, count=2),
                IndexerStatus(name="yts", label="YTS", state=IndexerState.EMPTY),
            )
            yield update.model_copy(update={"statuses": statuses})
