"""Card size (Settings → General): the scale maths, font minimums and grid widths (pure), the
setting itself, and the poster / My shows cards drawn at a scale on a real (hidden) Tk root."""

from __future__ import annotations

import asyncio
import gc
import tkinter as tk
import unittest
from collections.abc import Callable
from unittest import mock
from types import SimpleNamespace
from typing import Any

import customtkinter as ctk

from app.models import CardSize, Movie, MovieDetails, Settings, Show
from app.ui.card_scale import (
    CARD_SIZE_LABELS,
    FULL,
    POSTER_WIDTHS,
    SMALL_TEXT_MIN,
    TEXT_MIN,
    TITLE_MIN,
    CardScale,
    grid_item_width,
    line_height,
    scale_factor,
    scaled_font_size,
    size_from_label,
)
from app.ui.cards import AUTO_SWITCH_TEXT, CARD_WIDTH, ShowCard, show_card_layout
from app.ui.download_control import DOWNLOAD_SHORT, DOWNLOAD_TEXT, DownloadActions, DownloadStates
from app.ui import poster_card
from app.ui.main_window import settings_affect_pages
from app.ui.media_cards import MovieCard
from app.ui.poster_card import (
    POSTER_CARD_HEIGHT,
    POSTER_CARD_WIDTH,
    TEXT_WIDTH,
    card_face,
    card_size,
    row_font_size,
    row_height,
    text_width,
)
from app.ui.text_fit import ELLIPSIS
from app.ui.watchlist_cards import WatchlistShelf
from app.ui.widgets import FlexGrid

SIZES = list(CardSize)  # smallest first
RGB = (0, 0, 0)


class ScaleMathTests(unittest.TestCase):
    def test_sizes_go_from_small_to_the_original(self) -> None:
        self.assertEqual(SIZES, [CardSize.XS, CardSize.S, CardSize.M, CardSize.L, CardSize.XL])
        factors = [scale_factor(size) for size in SIZES]
        self.assertEqual(factors, sorted(factors))
        self.assertEqual(len(set(factors)), 5)
        self.assertEqual(scale_factor(CardSize.XL), 1.0)
        self.assertAlmostEqual(scale_factor(CardSize.XS), 2 / 3)
        for size, expected in zip(SIZES, (0.68, 0.76, 0.84, 0.92, 1.0), strict=True):
            self.assertAlmostEqual(scale_factor(size), expected, delta=0.015)

    def test_poster_cards_stay_whole_pixels_at_common_windows_scaling(self) -> None:
        for size in SIZES:
            width, height = card_size(CardScale.of(size))
            with self.subTest(size=size):
                self.assertEqual(width, POSTER_WIDTHS[size])
                self.assertEqual(width % 16, 0)
                self.assertEqual(height * 2, width * 3)  # 2:3
                for dpi in (1.0, 1.25, 1.5, 1.75, 2.0):
                    self.assertEqual(width * dpi, int(width * dpi))
                    self.assertEqual(height * dpi, int(height * dpi))

    def test_xl_is_todays_card(self) -> None:
        self.assertEqual(CardScale.of(CardSize.XL), FULL)
        self.assertEqual(card_size(FULL), (POSTER_CARD_WIDTH, POSTER_CARD_HEIGHT))
        self.assertEqual(text_width(FULL), TEXT_WIDTH)
        self.assertEqual((row_height(FULL), row_font_size(FULL)), (36, 14))

    def test_px(self) -> None:
        scale = CardScale(0.5)
        self.assertEqual(scale.px(36), 18)
        self.assertEqual(scale.px(1), 0)
        self.assertEqual(scale.px(1, minimum=1), 1)
        self.assertEqual(FULL.px(245), 245)

    def test_fonts_shrink_but_never_below_their_minimum(self) -> None:
        self.assertEqual(scaled_font_size(15, 1.0, TITLE_MIN), 15)
        self.assertEqual(scaled_font_size(15, 0.84, TITLE_MIN), 13)
        self.assertEqual(scaled_font_size(15, 2 / 3, TITLE_MIN), TITLE_MIN)
        self.assertEqual(scaled_font_size(12, 2 / 3, SMALL_TEXT_MIN), SMALL_TEXT_MIN)
        self.assertEqual(scaled_font_size(14, 2 / 3, TEXT_MIN), TEXT_MIN)
        self.assertEqual(scaled_font_size(9, 2 / 3, SMALL_TEXT_MIN), 9)  # already small: kept
        self.assertEqual(scaled_font_size(14, 1.5, TEXT_MIN), 14)  # never above the original
        for size in SIZES:
            scale = CardScale.of(size)
            for base in range(8, 25):
                for minimum in (SMALL_TEXT_MIN, TEXT_MIN, TITLE_MIN):
                    font = scale.font(base, minimum)
                    self.assertLessEqual(font, base)
                    self.assertGreaterEqual(font, min(base, minimum))

    def test_text_rows_fit_their_font(self) -> None:
        self.assertEqual(line_height(10), 14)
        xs = CardScale.of(CardSize.XS)
        self.assertEqual(xs.text_height(18, 10), 14)  # 12 would cut a 10 px line
        self.assertEqual(xs.text_height(36, 11), 24)
        self.assertEqual(FULL.text_height(18, 13), 18)  # XL keeps today's rows exactly

    def test_grid_item_width_follows_the_card(self) -> None:
        for size in SIZES:
            scale = CardScale.of(size)
            self.assertEqual(grid_item_width(POSTER_CARD_WIDTH, scale), card_size(scale)[0])
            self.assertEqual(grid_item_width(CARD_WIDTH, scale), show_card_layout(scale).width)
        self.assertEqual(grid_item_width(CARD_WIDTH, FULL), 245)

    def test_labels(self) -> None:
        self.assertEqual([CARD_SIZE_LABELS[size] for size in SIZES], ["XS", "S", "M", "L", "XL"])
        for size in SIZES:
            self.assertIs(size_from_label(CARD_SIZE_LABELS[size]), size)
        with self.assertRaises(ValueError):
            size_from_label("XXL")


class ShowCardLayoutTests(unittest.TestCase):
    def test_xl_is_todays_card(self) -> None:
        layout = show_card_layout(FULL)
        self.assertEqual((layout.width, layout.library_height, layout.poster), (245, 120, (68, 100)))
        self.assertEqual((layout.info_height, layout.switch_height, layout.missing_height), (18, 24, 26))
        self.assertEqual(layout.auto_height, 4 + 18 + 24 + 10)
        self.assertEqual(layout.missing_room, 26 + 8)
        self.assertEqual(
            (layout.title_font, layout.status_font, layout.info_font, layout.switch_font), (12, 9, 13, 14)
        )
        self.assertEqual(layout.height(with_switch=True, with_missing=True), 120 + 56 + 34)
        self.assertEqual(layout.height(with_switch=False, with_missing=True), 120)

    def test_every_size_is_smaller_and_readable(self) -> None:
        layouts = [show_card_layout(CardScale.of(size)) for size in SIZES]
        widths = [layout.width for layout in layouts]
        heights = [layout.height(with_switch=True, with_missing=True) for layout in layouts]
        self.assertEqual(widths, sorted(widths))
        self.assertEqual(heights, sorted(heights))
        for layout in layouts[:-1]:  # the smaller sizes (XL is today's card, tested above)
            self.assertGreaterEqual(layout.title_font, TEXT_MIN)
            self.assertGreaterEqual(layout.info_font, SMALL_TEXT_MIN)
            self.assertGreaterEqual(layout.switch_font, TEXT_MIN)
            self.assertGreaterEqual(layout.missing_font, TEXT_MIN)
            self.assertGreaterEqual(layout.info_height, line_height(layout.info_font))
            self.assertGreaterEqual(layout.library_height, layout.poster[1] + 2 * layout.row_pad)
            self.assertGreater(layout.switch_text_room, 0)
            self.assertGreater(layout.title_room, 0)


class CardFaceScaleTests(unittest.TestCase):
    def test_the_cover_is_rendered_at_dpi_times_card_scale(self) -> None:
        for size in SIZES:
            scale = CardScale.of(size)
            width, height = card_size(scale)
            for dpi in (1.0, 1.5):
                face = card_face(
                    dpi, 80, bg=RGB, edge=RGB, outside=RGB, placeholder=RGB, card=scale
                )
                self.assertEqual(face.size, (round(width * dpi), round(height * dpi)))
                self.assertGreaterEqual(face.border, 1)
                self.assertLessEqual(face.fade_from, face.solid_from)

    def test_faces_of_different_sizes_differ(self) -> None:
        # ImageCache keys a finished cover by its size and face: each card size gets its own.
        faces = {
            card_face(1.5, 80, bg=RGB, edge=RGB, outside=RGB, placeholder=RGB, card=CardScale.of(s))
            for s in SIZES
        }
        self.assertEqual(len(faces), len(SIZES))


class SettingTests(unittest.TestCase):
    def test_existing_settings_load_as_the_largest(self) -> None:
        self.assertIs(Settings().card_size, CardSize.XL)
        self.assertIs(Settings.model_validate({"first_day": "Sunday"}).card_size, CardSize.XL)

    def test_stored_by_value(self) -> None:
        settings = Settings(card_size="s")
        self.assertIs(settings.card_size, CardSize.S)
        self.assertEqual(settings.model_dump(mode="json")["card_size"], "s")
        self.assertEqual(Settings.model_validate(settings.model_dump(mode="json")), settings)

    def test_a_change_reloads_the_pages(self) -> None:
        self.assertTrue(settings_affect_pages(Settings(), Settings(card_size="xs")))


# -- widgets -----------------------------------------------------------------------------------


class RecordingImages:
    def __init__(self) -> None:
        self.sizes: list[tuple[int, int]] = []

    async def get(self, url: str, size: tuple[int, int], **_kwargs: object) -> None:
        self.sizes.append(size)
        return None


def _fake_watchlist() -> Any:
    return SimpleNamespace(subscribe=lambda listener: (lambda: None), contains=lambda _id: False)


async def _details(movie_id: int) -> MovieDetails:
    return MovieDetails(id=movie_id)


async def until(condition: Callable[[], bool]) -> None:
    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), timeout=5)


class CardWidgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        # Tk objects an earlier test left for the garbage collector must go now, on this
        # thread: collected in a cover's worker thread, their Tcl calls would wait for a Tk
        # loop that isn't running here, holding the cover back for seconds.
        gc.collect()
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        # Placeholders composed in an earlier test's event loop can't be awaited in this one.
        for cache in (poster_card._placeholders, poster_card._placeholder_tasks):
            patcher = mock.patch.dict(cache, clear=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.actions: DownloadActions[Movie] = DownloadActions(
            download=lambda _m: None, choose=lambda _m: None, states=DownloadStates()
        )

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    def movie_card(self, master: Any, scale: CardScale, images: Any) -> MovieCard:
        film = Movie.model_validate(
            {"id": 1, "title": "Mission: Impossible – The Final Reckoning", "poster_path": "/p.jpg",
             "release_date": "2025-05-21", "vote_average": 7.4, "vote_count": 900}
        )
        return MovieCard(
            master, film, images=images, details=_details, actions=self.actions,
            watchlist=_fake_watchlist(), scale=scale,
        )

    async def test_a_movie_card_scales_as_a_whole(self) -> None:
        images = RecordingImages()
        xs, xl = CardScale.of(CardSize.XS), FULL
        small = self.movie_card(self.root, xs, images)
        big = self.movie_card(self.root, xl, images)
        for card in (small, big):
            card.pack(side="left")
        self.root.update()
        await until(lambda: len(images.sizes) == 2)
        dpi = ctk.ScalingTracker.get_widget_scaling(small)
        self.assertEqual(small.cget("width"), 128)
        self.assertEqual(big.cget("width"), POSTER_CARD_WIDTH)
        # Rendered at physical size for each card: DPI factor x card scale.
        self.assertEqual(
            sorted(images.sizes), sorted([(round(128 * dpi), round(192 * dpi)), (round(192 * dpi), round(288 * dpi))])
        )
        small_row, big_row = small._control, big._control
        self.assertLess(small_row.cget("height"), big_row.cget("height"))
        self.assertGreaterEqual(small.title_font.cget("size"), TITLE_MIN)
        self.assertLess(small.title_font.cget("size"), big.title_font.cget("size"))
        # Narrow: the Download button falls back to its icon (with a hover tip) or fits whole.
        self.root.update()
        self.assertIn(small_row._main.cget("text"), (DOWNLOAD_TEXT, DOWNLOAD_SHORT))
        self.assertEqual(big_row._main.cget("text"), DOWNLOAD_TEXT)
        self.assertTrue(small.watch_button.cget("text"))

    async def test_the_grid_reflows_at_the_scaled_width(self) -> None:
        columns = {}
        for size in (CardSize.XS, CardSize.M, CardSize.XL):
            scale = CardScale.of(size)
            grid = FlexGrid(self.root, item_width=grid_item_width(POSTER_CARD_WIDTH, scale))
            dpi = ctk.ScalingTracker.get_widget_scaling(grid)
            columns[size] = grid._columns(round(1100 * dpi))  # a 1100-unit wide page
            grid.destroy()
        self.assertEqual(columns, {CardSize.XS: 7, CardSize.M: 6, CardSize.XL: 5})

    async def test_the_watchlist_shelf_rebuilds_for_a_new_size(self) -> None:
        from tests.test_watchlist_ui import entry

        current = {"scale": FULL}
        watchlist: Any = SimpleNamespace(
            entries=lambda: (entry(),),
            subscribe=lambda _listener: (lambda: None),
            contains=lambda _id: True,
        )
        body = ctk.CTkFrame(self.root)
        body.pack(fill="both", expand=True)
        shelf = WatchlistShelf(
            body, watchlist=watchlist, images=RecordingImages(), actions=self.actions,  # type: ignore[arg-type]
            card_scale=lambda: current["scale"],
        )
        shelf.start()
        first = shelf.cards[1]
        self.assertEqual(first.cget("width"), POSTER_CARD_WIDTH)
        shelf.sync()
        self.assertIs(shelf.cards[1], first)  # same films, same size: repainted in place
        current["scale"] = CardScale.of(CardSize.M)
        shelf.sync()  # the page's reload after the setting changed
        rebuilt = shelf.cards[1]
        self.assertIsNot(rebuilt, first)
        self.assertEqual(rebuilt.cget("width"), 160)
        assert shelf._grid is not None
        self.assertEqual(shelf._grid.item_width, 160)

    async def test_a_my_shows_card_scales_as_a_whole(self) -> None:
        images: Any = RecordingImages()
        show = Show(id=7, name="The Long and Winding Title of a Show", status="Returning Series")
        cards = {
            size: ShowCard(
                self.root, show, images=images, on_auto_download=lambda s, on: None,
                info_line="Downloaded S02E08 · yesterday", scale=CardScale.of(size),
            )
            for size in (CardSize.XS, CardSize.XL)
        }
        for card in cards.values():
            card.pack(side="left")
        self.root.update()
        xs, xl = cards[CardSize.XS], cards[CardSize.XL]
        self.assertEqual(xl.cget("width"), CARD_WIDTH)
        self.assertEqual(xs.cget("width"), show_card_layout(CardScale.of(CardSize.XS)).width)
        self.assertLess(xs.cget("height"), xl.cget("height"))
        assert xs._auto_switch is not None and xl._auto_switch is not None
        self.assertEqual(xl._auto_switch.cget("text"), AUTO_SWITCH_TEXT)
        text = xs._auto_switch.cget("text")
        self.assertTrue(text == AUTO_SWITCH_TEXT or text.endswith(ELLIPSIS))


if __name__ == "__main__":
    unittest.main()
