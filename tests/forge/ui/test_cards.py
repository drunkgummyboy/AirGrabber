"""The queue's cards: a band and its lines drawn as one card on the fast list (Sizes.cards,
app/forge/ui/widgets/virtual_list.py); what a card's picture is, baked (app/forge/ui/images.py),
held against the worst pictures; Open in Explorer (app/forge/ui/file_browser.py and the queue's
band action, with a fake at its seam)."""

from __future__ import annotations

import asyncio
import dataclasses
import gc
import io
import logging
import tkinter as tk
from pathlib import Path
from typing import cast
from unittest import mock

import customtkinter as ctk
import httpx
from PIL import Image, ImageTk

from app.forge.core.matcher import Resolution
from app.forge.ui import file_browser
from app.forge.ui.images import (
    BACKDROP_SIZE,
    CARD_DROP,
    CARD_MAX_LINES,
    CARD_STRENGTHS,
    CARD_TEXT_LIMIT,
    CARD_TYPICAL,
    CARD_WIDTH,
    HIGHLIGHT_ALPHA,
    BandImages,
    CardBake,
    CardShape,
    baked_pixel,
    card_across,
    card_down,
    card_strength,
    card_strengths,
    card_text_end,
    line_highlight,
    render_card,
)
from app.forge.ui.look import app_look, mix
from app.forge.ui.queue_rows import EXPLORER_ACTION
from app.forge.ui.widgets.virtual_list import Row, Sizes, VirtualList, faded_fill
from app.models import Accent
from app.services.images import ImageCache
from app.ui import theme
from app.ui.palettes import to_hex
from tests.forge.base import ForgeAsyncTestCase, ForgeTestCase
from tests.forge.tk import pump, pump_until
from tests.forge.ui.test_queue_view import FakeImages, QueueCase, make_files
from tests.test_accent import contrast

REAL_START = file_browser._start  # before the guard in tests/forge/base.py patches it
LIGHT, DARK = 0, 1
SIZES = Sizes(
    band_title=21, band_sub=14, button=14, line=16, code=16, mono=13, state=13,
    button_height=34, band_gap=10, band_gap_below=False, band_radius=10,
    band_inset=16, poster=(58, 86), cards=True,
)  # fmt: skip


def rgb(colour: str) -> tuple[int, int, int]:
    text = to_hex(colour)
    red, green, blue = (int(text[i : i + 2], 16) for i in (1, 3, 5))
    return red, green, blue


def hex_of(pixel: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*pixel)


def cget(canvas: tk.Canvas, item: int, option: str) -> str:
    return str(canvas.tk.call(str(canvas), "itemcget", item, f"-{option}"))


def band(key: int, *, title: str = "Show", backdrop: str | None = "/b.jpg", faded: bool = False) -> Row:
    return Row(
        key=key,
        kind="band",
        title=title,
        subtitle="Season 1 · 2 episodes",
        dest="→ Series › Show",
        badge=title,
        backdrop=backdrop,
        actions=((EXPLORER_ACTION, "📂"), ("rename", "Rename")),
        tips=((EXPLORER_ACTION, "Open in Explorer"),),
        state="done" if faded else "ready",
        state_text="Renamed" if faded else "Ready",
        faded=faded,
    )


def line(key: int, owner: int, *, faded: bool = False) -> Row:
    return Row(
        key=key,
        kind="item",
        title=f"Episode {key}",
        code="S01E01",
        faint=f"show.{key}.mkv",
        faint_label="Now:",
        status="ready",
        band=owner,
        faded=faded,
    )


def card(owner: int, first: int, lines: int, **options: object) -> list[Row]:
    faded = bool(options.get("faded"))
    made = band(owner, **options)  # type: ignore[arg-type]
    return [made, *(line(first + n, owner, faded=faded) for n in range(lines))]


class Shape:
    """A card's picture at 150 %: 900 units wide, a band of 110, lines of 40, a foot of 10."""

    UNIT = 1.5

    @classmethod
    def of(cls, lines: int) -> CardShape:
        height = 110 + min(lines, CARD_MAX_LINES) * 40 + 10
        return CardShape(
            round(CARD_WIDTH * cls.UNIT),
            round(height * cls.UNIT),
            round(110 * cls.UNIT),
            cls.UNIT,
            lines > CARD_MAX_LINES,
        )


class CardStrengthTests(ForgeTestCase):
    SHAPE = Shape.of(4)

    def test_nothing_at_the_left_full_at_the_right(self) -> None:
        self.assertEqual(card_across((1 - 0.40) * CARD_TYPICAL + 1), 0.0)
        self.assertEqual(card_across((1 - 0.75) * CARD_TYPICAL - 1), 1.0)
        self.assertEqual(card_across(0), 1.0)
        self.assertEqual(card_strength(self.SHAPE, CARD_STRENGTHS[1], 0, 0), 0.0)
        self.assertEqual(card_strength(self.SHAPE, CARD_STRENGTHS[1], self.SHAPE.width, 0), 0.55)

    def test_strongest_behind_the_band_dropping_to_the_lines_below_it(self) -> None:
        strengths = CARD_STRENGTHS[DARK]
        shape = self.SHAPE
        self.assertEqual(card_down(shape, strengths, 0), strengths[0])
        self.assertEqual(card_down(shape, strengths, shape.band - 31 * shape.unit), strengths[0])
        drop_end = shape.band + CARD_DROP[1] * shape.unit
        self.assertEqual(card_down(shape, strengths, drop_end), strengths[1])
        self.assertEqual(card_down(shape, strengths, shape.height), strengths[1])
        self.assertLessEqual(CARD_DROP[1] - CARD_DROP[0], 40)  # about 40 units, no hard line
        between = card_down(shape, strengths, shape.band)
        self.assertLess(strengths[1], between)
        self.assertLess(between, strengths[0])

    def test_a_tall_card_fades_out_before_the_pictures_end(self) -> None:
        tall = Shape.of(16)
        self.assertEqual(tall.height, Shape.of(CARD_MAX_LINES).height)  # capped
        strengths = CARD_STRENGTHS[DARK]
        self.assertEqual(card_down(tall, strengths, tall.height), 0.0)
        self.assertEqual(card_down(tall, strengths, tall.band + 60 * tall.unit), strengths[1])
        self.assertEqual(
            card_down(Shape.of(CARD_MAX_LINES), strengths, tall.height), strengths[1]
        )  # not when it ends with the card

    def test_a_done_card_at_about_half_the_strength(self) -> None:
        for dark in (False, True):
            full = card_strengths(dark=dark, done=False)
            done = card_strengths(dark=dark, done=True)
            self.assertEqual(done, (full[0] / 2, full[1] / 2))

    def test_baked_pixel(self) -> None:
        self.assertEqual(baked_pixel((255, 255, 255), (0, 0, 0), 0.5), (128, 128, 128))
        self.assertEqual(baked_pixel((9, 9, 9), (36, 36, 36), 0.0), (36, 36, 36))

    def test_the_bake_is_what_the_pure_function_says(self) -> None:
        shape = self.SHAPE
        fill = "#242424"
        strengths = card_strengths(dark=True, done=False)
        baked = render_card(Image.new("RGB", (300, 170), (250, 120, 30)), shape, fill, dark=True)
        self.assertEqual(baked.mode, "RGB")  # fully opaque
        self.assertEqual(baked.size, (shape.width, shape.height))
        for x, y in ((0, 0), (shape.width - 1, 0), (shape.width - 1, shape.height - 1), (900, 100)):
            wanted = baked_pixel((250, 120, 30), rgb(fill), card_strength(shape, strengths, x, y))
            got = baked.getpixel((x, y))
            assert isinstance(got, tuple)
            for have, want in zip(got, wanted, strict=True):
                self.assertLessEqual(abs(have - want), 2, (x, y))
        self.assertEqual(hex_of(baked.getpixel((0, 0))), fill)  # type: ignore[arg-type]

    def test_the_finish_step_compares_by_value(self) -> None:
        shape = self.SHAPE
        self.assertEqual(CardBake("#242424", shape, (0.5, 0.1)), CardBake("#242424", shape, (0.5, 0.1)))
        self.assertEqual(
            hash(CardBake("#242424", shape, (0.5, 0.1))), hash(CardBake("#242424", shape, (0.5, 0.1)))
        )
        self.assertNotEqual(CardBake("#242424", shape, (0.5, 0.1)), CardBake("#FFFFFF", shape, (0.5, 0.1)))

    def test_the_text_stops_at_the_limit_and_further_in_on_a_narrow_card(self) -> None:
        self.assertEqual(card_text_end(0, 2000, 1.0), 1100)  # a wide card: 55 %
        self.assertEqual(card_text_end(0, 1150, 1.0), round(1150 * CARD_TEXT_LIMIT))
        narrow = card_text_end(0, 960, 1.0)  # the same place in the picture as on a typical card
        self.assertEqual(960 - narrow, round((1 - CARD_TEXT_LIMIT) * CARD_TYPICAL))
        self.assertEqual(card_text_end(0, 1725, 1.5), round(1725 * CARD_TEXT_LIMIT))  # at 150 %

    def test_the_highlight_strip(self) -> None:
        strip = line_highlight(300, 40, 8, "#FFFFFF", 0.1)
        self.assertEqual(strip.mode, "RGBA")
        self.assertEqual(strip.size, (300, 40))
        self.assertEqual(strip.getpixel((150, 20)), (255, 255, 255, round(25.5)))
        corner = strip.getpixel((0, 0))
        assert isinstance(corner, tuple)
        self.assertEqual(corner[3], 0)  # rounded


class ReadabilityTests(ForgeTestCase):
    """On the worst pictures, at the limits the text has: pure white in dark, pure black in light,
    every accent. Mid-grey for the link colour, whose worst ground isn't an extreme."""

    def setUp(self) -> None:
        super().setUp()
        self.addCleanup(theme.apply_accent, Accent.SLATE)
        self.shape = Shape.of(4)

    def each(self) -> list[tuple[str, int, tuple[int, int, int]]]:
        cases: list[tuple[str, int, tuple[int, int, int]]] = []
        for accent in Accent:
            theme.apply_accent(accent)
            cases.append((accent.name, DARK, (255, 255, 255)))
            cases.append((accent.name, LIGHT, (0, 0, 0)))
        return cases

    def baked(self, half: int, picture: tuple[int, int, int], x: float, y: float, *, done: bool = False) -> str:
        look = app_look()
        fill = to_hex(look.SURFACE[half])
        if done:
            fill = mix(fill, look.BG[half], 0.45)
        strengths = card_strengths(dark=half == DARK, done=done)
        strength = card_strength(self.shape, strengths, x, y)
        return hex_of(baked_pixel(picture, rgb(fill), strength))

    def _limit_x(self) -> float:
        """Where a title and subtitle on a typical card stop, in the picture."""
        return self.shape.width - (1 - CARD_TEXT_LIMIT) * CARD_TYPICAL * self.shape.unit

    def _first_line_y(self) -> float:
        """The first line's old name starts here (its row 40, its text 16 high): the worst line."""
        return self.shape.band + 12 * self.shape.unit

    def test_title_and_subtitle_at_the_limit(self) -> None:
        y = 40 * self.shape.unit  # behind the band: the picture's full strength
        for name in [c for c in self.each()]:
            accent, half, picture = name
            theme.apply_accent(Accent[accent])
            ground = self.baked(half, picture, self._limit_x(), y)
            with self.subTest(accent=accent, half=half):
                self.assertGreaterEqual(contrast(theme.TEXT[half], ground), 4.5)
                self.assertGreaterEqual(contrast(theme.TEXT_MUTED[half], ground), 3.0)

    def test_a_lines_text_and_old_name_at_the_right_end(self) -> None:
        x = self.shape.width - 1
        for accent, half, picture in self.each():
            theme.apply_accent(Accent[accent])
            for y in (self._first_line_y(), self.shape.band + 40 * self.shape.unit):
                ground = self.baked(half, picture, x, y)
                with self.subTest(accent=accent, half=half, y=y):
                    self.assertGreaterEqual(contrast(theme.TEXT[half], ground), 4.5)
                    self.assertGreaterEqual(contrast(theme.TEXT_MUTED[half], ground), 3.0)

    def test_and_through_a_hovered_or_selected_lines_highlight(self) -> None:
        x = self.shape.width - 1
        for accent, half, picture in self.each():
            theme.apply_accent(Accent[accent])
            ground = rgb(self.baked(half, picture, x, self._first_line_y()))
            text = rgb(theme.TEXT[half])
            for selected in (False, True):
                lit = hex_of(baked_pixel(text, ground, HIGHLIGHT_ALPHA[selected][half]))
                with self.subTest(accent=accent, half=half, selected=selected):
                    self.assertGreaterEqual(contrast(theme.TEXT[half], lit), 4.5)
                    self.assertGreaterEqual(contrast(theme.TEXT_MUTED[half], lit), 3.0)

    def test_the_highlight_is_clearly_visible_on_every_ground(self) -> None:
        x = self.shape.width - 1
        for accent, half, picture in self.each():
            theme.apply_accent(Accent[accent])
            text = rgb(theme.TEXT[half])
            for ground in (
                to_hex(app_look().SURFACE[half]),  # no picture
                self.baked(half, picture, x, self._first_line_y()),  # the worst one
                self.baked(half, (128, 128, 128), x, self._first_line_y()),
            ):
                for selected in (False, True):
                    lit = hex_of(baked_pixel(text, rgb(ground), HIGHLIGHT_ALPHA[selected][half]))
                    with self.subTest(accent=accent, half=half, ground=ground):
                        self.assertGreaterEqual(contrast(lit, ground), 1.15)

    def test_the_link_colour_on_a_mid_grey_picture(self) -> None:
        y = 40 * self.shape.unit
        for accent, half, _picture in self.each():
            theme.apply_accent(Accent[accent])
            link = app_look().LINK[half]
            at_limit = self.baked(half, (128, 128, 128), self._limit_x(), y)
            on_lines = self.baked(half, (128, 128, 128), self.shape.width - 1, self._first_line_y())
            with self.subTest(accent=accent, half=half):
                self.assertGreaterEqual(contrast(link, at_limit), 3.0)
                self.assertGreaterEqual(contrast(link, on_lines), 3.0)

    def test_a_done_cards_quiet_text(self) -> None:
        y = 40 * self.shape.unit
        for accent, half, picture in self.each():
            theme.apply_accent(Accent[accent])
            muted = theme.TEXT_MUTED[half]
            title = self.baked(half, picture, self._limit_x(), y, done=True)
            note = self.baked(half, picture, self.shape.width - 1, y, done=True)
            line_ground = self.baked(half, picture, self.shape.width - 1, self._first_line_y(), done=True)
            with self.subTest(accent=accent, half=half):
                self.assertGreaterEqual(contrast(muted, title), 4.5)
                self.assertGreaterEqual(contrast(muted, note), 3.0)
                self.assertGreaterEqual(contrast(muted, line_ground), 3.0)


class CardListCase(ForgeAsyncTestCase):
    def setUp(self) -> None:
        super().setUp()
        gc.collect()
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        self.addCleanup(ctk.set_appearance_mode, ctk.get_appearance_mode())

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    async def card_list(self, rows: list[Row], *, width: int = 1000, height: int = 600) -> VirtualList:
        holder = ctk.CTkFrame(self.root, width=width, height=height)
        holder.place(x=0, y=0)
        holder.pack_propagate(False)
        self.addCleanup(holder.destroy)
        vlist = VirtualList(holder, row_height=40, band_height=120, sizes=SIZES, hover_actions=True)
        vlist.pack(fill="both", expand=True)
        self.root.deiconify()
        self.root.geometry(f"{width + 50}x{height + 50}+20+20")
        vlist.set_rows(rows)
        await pump(self.root, 0.05)
        return vlist

    def picture(self, vlist: VirtualList, lines: int = 2, colour: str = "red") -> ImageTk.PhotoImage:
        shape = vlist.card_shape(lines)
        return ImageTk.PhotoImage(Image.new("RGB", (shape.width, shape.height), colour), master=self.root)


class OneCardTests(CardListCase):
    async def test_a_band_and_its_lines_are_one_piece(self) -> None:
        rows = [*card(-10, 1, 2), Row(key=-5, kind="section", title="Done"), *card(-11, 3, 1, faded=True)]
        vlist = await self.card_list(rows)
        c = vlist.canvas
        look = app_look()
        fills = c.find_withtag("card-fill")
        self.assertEqual(len(fills), 2)
        self.assertEqual(
            [cget(c, i, "fill").upper() for i in fills],
            [theme.pick(look.SURFACE).upper(), faded_fill(look).upper()],
        )
        scale = vlist.scale
        x0, y0, _x1, y1 = c.coords(fills[0])
        self.assertEqual(y0, round(10 * scale))  # the gap above stays the list's ground
        # The fill runs under the band and both lines, and a little past them.
        self.assertEqual(y1, vlist._tops[2] + vlist._row_h + vlist._foot)
        self.assertEqual(vlist._tops[3], y1)  # the heading sits outside the cards
        self.assertEqual(c.find_withtag("band-fill"), ())
        self.assertEqual(c.find_withtag("band-edge"), ())
        self.assertEqual(x0, vlist._pad // 2)

    async def test_rounded_only_at_the_cards_top_and_bottom(self) -> None:
        vlist = await self.card_list(card(-10, 1, 3))
        c = vlist.canvas
        _x0, y0, _x1, y1 = c.coords(c.find_withtag("card-fill")[0])
        corners = c.find_withtag("band-corner")
        self.assertEqual(len(corners), 4)
        tops = {c.coords(i)[1] for i in corners}
        self.assertEqual(tops, {y0, y1})

    async def test_the_edge_is_gone_for_every_list(self) -> None:
        self.assertNotIn("band_edge", {f.name for f in dataclasses.fields(Sizes)})

    async def test_only_bands_with_a_picture_ask_for_one(self) -> None:
        vlist = await self.card_list([*card(-10, 1, 2), *card(-11, 3, 7, backdrop=None)])
        asked: list[tuple[str, CardShape, str]] = []

        def ask(row: Row, shape: CardShape, fill: str) -> None:
            asked.append((row.title, shape, fill))

        vlist.card_picture = ask
        vlist._redraw()
        [(_title, shape, fill)] = asked
        self.assertEqual(fill.upper(), theme.pick(app_look().SURFACE).upper())
        self.assertEqual(shape, vlist.card_shape(2))

    async def test_heights_go_by_lines_up_to_a_cap(self) -> None:
        vlist = await self.card_list(card(-10, 1, 1))
        shapes = [vlist.card_shape(n) for n in (1, 2, 6, 7, 16)]
        self.assertEqual({s.width for s in shapes}, {round(CARD_WIDTH * vlist.scale)})
        self.assertEqual(shapes[3], shapes[4])  # every longer card: one size
        self.assertEqual(shapes[2].height, shapes[3].height)
        self.assertFalse(shapes[2].tall)
        self.assertTrue(shapes[3].tall)
        self.assertLess(shapes[0].height, shapes[1].height)
        # One line's card is exactly as tall as its picture.
        c = vlist.canvas
        _x0, y0, _x1, y1 = c.coords(c.find_withtag("card-fill")[0])
        self.assertEqual(y1 - y0, shapes[0].height)

    async def test_the_picture_lies_beneath_the_rows_with_only_part_of_the_card_in_view(self) -> None:
        rows = [*card(-10, 1, 30), *card(-11, 100, 2)]
        vlist = await self.card_list(rows)
        picture = self.picture(vlist, 30)
        asked: list[str] = []

        def ask(row: Row, shape: CardShape, fill: str) -> ImageTk.PhotoImage:
            asked.append(row.title)
            return picture

        vlist.card_picture = ask
        c = vlist.canvas
        c.yview_moveto(vlist._tops[10] / vlist._total)  # the band far above the canvas
        vlist._redraw()
        await pump(self.root, 0.02)
        self.assertNotIn(-10, vlist.visible_keys())
        self.assertTrue(asked)
        [image] = c.find_withtag("card-image")
        self.assertLess(c.coords(image)[1], c.canvasy(0))  # it starts above the canvas
        items = list(c.find_all())
        others = [i for i in items if "card" not in c.gettags(i)]
        self.assertTrue(others)
        self.assertLess(max(items.index(i) for i in c.find_withtag("card")), items.index(others[0]))
        self.assertEqual([r.key for r in vlist.cards_in_view()], [-10])

    async def test_nothing_is_drawn_again_for_a_card_that_didnt_change(self) -> None:
        vlist = await self.card_list(card(-10, 1, 2))
        picture = self.picture(vlist)
        vlist.card_picture = lambda row, shape, fill: picture
        c = vlist.canvas
        vlist._redraw()
        before = c.find_withtag("card")
        vlist.hover_band(-10)  # the pointer: only the row's items change
        vlist._redraw()
        self.assertEqual(c.find_withtag("card"), before)
        # Rows equal to those shown draw nothing at all (F-33).
        with mock.patch.object(vlist, "_redraw") as redraw:
            vlist.set_rows(card(-10, 1, 2))
        redraw.assert_not_called()

    async def test_a_changed_card_is_drawn_again_and_a_gone_one_goes(self) -> None:
        vlist = await self.card_list(card(-10, 1, 2))
        c = vlist.canvas
        before = c.find_withtag("card")
        vlist.set_rows(card(-10, 1, 3))
        after = c.find_withtag("card")
        self.assertEqual(len(after), len(before))
        self.assertNotEqual(after, before)
        vlist.set_rows([])
        self.assertEqual(c.find_all(), ())

    async def test_a_shown_picture_outlives_the_cache(self) -> None:
        vlist = await self.card_list(card(-10, 1, 2))
        picture = self.picture(vlist)
        name = str(picture)
        vlist.card_picture = lambda row, shape, fill: picture
        vlist._redraw()
        del picture
        vlist.card_picture = lambda row, shape, fill: None
        gc.collect()
        self.assertIn(name, self.root.tk.splitlist(self.root.tk.call("image", "names")))

    async def test_a_picture_wider_than_the_card_is_cut_at_its_left(self) -> None:
        vlist = await self.card_list(card(-10, 1, 2), width=500)
        picture = self.picture(vlist)
        vlist.card_picture = lambda row, shape, fill: picture
        vlist._redraw()
        c = vlist.canvas
        self.assertGreater(picture.width(), c.winfo_width())
        [clip] = c.find_withtag("card-clip")
        self.assertEqual(c.coords(clip)[2], vlist._pad // 2)
        self.assertEqual(cget(c, clip, "fill").upper(), theme.pick(app_look().BG).upper())
        self.assertEqual(c.coords(c.find_withtag("card-image")[0])[0], c.winfo_width() - vlist._pad // 2)

    async def test_light_and_dark_draw_the_cards_again(self) -> None:
        look = app_look()
        ctk.set_appearance_mode("light")
        vlist = await self.card_list(card(-10, 1, 1))
        c = vlist.canvas
        fill = c.find_withtag("card-fill")[0]
        self.assertEqual(cget(c, fill, "fill").upper(), theme.pick(look.SURFACE, dark=False).upper())
        ctk.set_appearance_mode("dark")
        await pump(self.root, 0.05)
        fill = c.find_withtag("card-fill")[0]
        self.assertEqual(cget(c, fill, "fill").upper(), theme.pick(look.SURFACE, dark=True).upper())


class CardRowsTests(CardListCase):
    async def test_a_hovered_or_selected_line_gets_the_strip_without_a_bar(self) -> None:
        vlist = await self.card_list(card(-10, 1, 2))
        c = vlist.canvas
        self.assertEqual(c.find_withtag("line-highlight"), ())
        rectangles = len([i for i in c.find_all() if str(c.type(i)) == "rectangle"])
        vlist.select(1)
        [strip] = c.find_withtag("line-highlight")
        self.assertEqual(str(c.type(strip)), "image")
        self.assertEqual(len([i for i in c.find_all() if str(c.type(i)) == "rectangle"]), rectangles)
        c.event_generate("<Motion>", x=300, y=vlist.row_middle(2))
        await pump(self.root, 0.05)
        self.assertEqual(len(c.find_withtag("line-highlight")), 2)
        # Strips are remembered by size and look: the same photo on the next draw.
        name = cget(c, c.find_withtag("line-highlight")[0], "image")
        vlist._redraw()
        self.assertEqual(cget(c, c.find_withtag("line-highlight")[0], "image"), name)

    async def test_strips_stay_few(self) -> None:
        vlist = await self.card_list(card(-10, 1, 2))
        for width in range(100, 130):
            vlist._highlight(width, 30, False)
        self.assertLessEqual(len(vlist._strips), 8)

    async def test_the_lists_without_cards_keep_their_selection(self) -> None:
        holder = ctk.CTkFrame(self.root, width=700, height=400)
        holder.place(x=0, y=0)
        holder.pack_propagate(False)
        self.addCleanup(holder.destroy)
        vlist = VirtualList(holder)
        vlist.pack(fill="both", expand=True)
        self.root.deiconify()
        self.root.geometry("750x450+20+20")
        vlist.set_rows([Row(key=1, kind="item", title="A file", subtitle="a.mkv", badge="A")])
        await pump(self.root, 0.05)
        vlist.select(1)
        c = vlist.canvas
        self.assertEqual(c.find_withtag("line-highlight"), ())
        fills = {cget(c, i, "fill").upper() for i in c.find_all() if str(c.type(i)) == "rectangle"}
        self.assertIn(theme.pick(app_look().ACCENT).upper(), fills)  # its bar

    async def test_a_hover_on_any_line_or_the_foot_counts_as_over_the_card(self) -> None:
        vlist = await self.card_list([*card(-10, 1, 2), *card(-11, 3, 1)])
        c = vlist.canvas
        c.event_generate("<Motion>", x=300, y=vlist.row_middle(2))
        await pump(self.root, 0.05)
        self.assertEqual((vlist._hover, vlist._hover_band), (2, -10))
        foot = vlist._tops[2] + vlist._row_h + vlist._foot // 2
        c.event_generate("<Motion>", x=300, y=foot)
        await pump(self.root, 0.05)
        self.assertEqual((vlist._hover, vlist._hover_band), (None, -10))
        # Past the last card: nothing.
        c.event_generate("<Motion>", x=300, y=vlist._total + 20)
        await pump(self.root, 0.05)
        self.assertEqual((vlist._hover, vlist._hover_band), (None, None))

    async def test_a_click_in_the_foot_selects_nothing(self) -> None:
        vlist = await self.card_list(card(-10, 1, 2))
        vlist.select(1, notify=False)
        c = vlist.canvas
        foot = vlist._tops[2] + vlist._row_h + vlist._foot // 2
        c.event_generate("<Button-1>", x=300, y=foot)
        await pump(self.root, 0.05)
        self.assertEqual(vlist.selected, 1)
        c.event_generate("<Button-1>", x=300, y=vlist.row_middle(2))
        await pump(self.root, 0.05)
        self.assertEqual(vlist.selected, 2)

    def title_end(self, vlist: VirtualList) -> int:
        c = vlist.canvas
        return int(c.bbox(c.find_withtag("band-title")[0])[2])

    async def test_on_a_picture_the_title_stops_at_the_limit(self) -> None:
        long = "A very long title that goes on and on and on and on and on and on and on and on and on"
        rows = card(-10, 1, 1, title=long)
        vlist = await self.card_list(rows)
        c = vlist.canvas
        pad = vlist._pad // 2
        end = card_text_end(pad, c.winfo_width() - pad, vlist.scale)
        self.assertLessEqual(self.title_end(vlist), end)
        self.assertLess(self.title_end(vlist), c.winfo_width() // 2 + vlist._px(100))
        # Without a picture only the status stops it.
        plain = card(-10, 1, 1, title=long, backdrop=None)
        vlist.set_rows(plain)
        self.assertGreater(self.title_end(vlist), end)
        state = c.find_withtag("band-state-pill")[0]
        self.assertLess(self.title_end(vlist), c.bbox(state)[0])

    async def test_the_status_sits_on_a_solid_pill_and_the_buttons_are_solid(self) -> None:
        vlist = await self.card_list(card(-10, 1, 1))
        c = vlist.canvas
        vlist.hover_band(-10)
        surface = theme.pick(app_look().SURFACE).upper()
        [pill] = c.find_withtag("band-state-pill")
        self.assertEqual(cget(c, pill, "fill").upper(), surface)
        pill_box = c.bbox(pill)
        word_box = c.bbox(c.find_withtag("band-state")[0])
        self.assertLessEqual(pill_box[0], c.bbox(c.find_withtag("band-dot")[0])[0])
        self.assertGreaterEqual(pill_box[2], word_box[2])
        items = list(c.find_all())
        self.assertLess(items.index(pill), items.index(c.find_withtag("band-state")[0]))
        for button in c.find_withtag("band-button"):
            self.assertNotEqual(cget(c, button, "fill"), "")

    async def test_a_buttons_own_tip_under_the_pointer(self) -> None:
        vlist = await self.card_list(card(-10, 1, 1))
        vlist.hover_band(-10)
        box = vlist.action_box(-10, EXPLORER_ACTION)
        assert box is not None
        vlist._point_at((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        self.assertEqual(vlist.tip_shown, "Open in Explorer")
        vlist._point_at(1, 1)
        self.assertEqual(vlist.tip_shown, "")

    async def test_a_ready_status_in_the_link_colour_needs_you_amber(self) -> None:
        rows = card(-10, 1, 1)
        rows[0] = rows[0].model_copy(update={"state": "needs", "state_text": "Needs you"})
        vlist = await self.card_list([*rows, *card(-11, 3, 1)])
        c = vlist.canvas
        words = {cget(c, i, "text"): cget(c, i, "fill").upper() for i in c.find_withtag("band-state")}
        self.assertEqual(words["Needs you"], theme.pick(theme.WARNING).upper())
        self.assertEqual(words["Ready"], theme.pick(app_look().LINK).upper())

    async def test_a_line_not_laid_out_yet_doesnt_scroll_the_list(self) -> None:
        vlist = await self.card_list(card(-10, 1, 2))
        with mock.patch.object(vlist.canvas, "winfo_height", return_value=1):
            vlist.select(1, notify=False)
        self.assertEqual(vlist.canvas.canvasy(0), 0)


def _encoded(colour: tuple[int, int, int]) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (300, 170), colour).save(out, format="PNG")
    return out.getvalue()


class CardBakingTests(CardListCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        body = _encoded((200, 40, 40))

        def serve(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/gone.jpg"):
                return httpx.Response(404)
            return httpx.Response(200, content=body)

        self.http = httpx.AsyncClient(transport=httpx.MockTransport(serve))
        self.addAsyncCleanup(self.http.aclose)
        self.cache = ImageCache(self.http, Path(self.tmp) / "posters")
        self.images = BandImages(self.cache, self.root)

    async def test_a_bake_is_kept_here_not_in_the_caches_memory(self) -> None:
        shape = Shape.of(1)
        with mock.patch("app.services.images._fit", wraps=__import__("app.services.images", fromlist=["_fit"])._fit) as fit:
            photo = await self.images.card("/a.jpg", shape, "#242424")
        assert photo is not None
        self.assertEqual((photo.width(), photo.height()), (shape.width, shape.height))
        fit.assert_called_once()
        self.assertEqual(len(self.cache._memory), 0)
        self.assertIs(self.images.cached_card("/a.jpg", shape, "#242424"), photo)
        self.assertIs(await self.images.card("/a.jpg", shape, "#242424"), photo)

    async def test_the_key_tells_looks_done_cards_and_tall_cards_apart(self) -> None:
        shape, tall = Shape.of(CARD_MAX_LINES), Shape.of(CARD_MAX_LINES + 1)
        keys = {
            self.images.card_key("/a.jpg", shape, "#242424"),
            self.images.card_key("/a.jpg", shape, "#FFFFFF"),
            self.images.card_key("/a.jpg", shape, "#242424", done=True),
            self.images.card_key("/a.jpg", tall, "#242424"),
        }
        self.assertEqual(len(keys), 4)

    async def test_a_picture_tmdb_lacks_isnt_asked_again(self) -> None:
        shape = Shape.of(1)
        self.assertIsNone(await self.images.card("/gone.jpg", shape, "#242424"))
        self.assertTrue(self.images.known_missing(self.images.card_key("/gone.jpg", shape, "#242424")))

    async def test_the_photos_stay_under_their_budget_and_the_newest_stay(self) -> None:
        shape = Shape.of(1)
        one = shape.width * shape.height
        images = BandImages(self.cache, self.root, pixels=3 * one)
        for n in range(5):
            self.assertIsNotNone(await images.card(f"/a{n}.jpg", shape, "#242424"))
        self.assertLessEqual(images._pixels, 3 * one)
        self.assertEqual(len(images._photos), 3)
        self.assertIsNone(images.cached_card("/a0.jpg", shape, "#242424"))
        self.assertIsNotNone(images.cached_card("/a4.jpg", shape, "#242424"))
        images.clear()
        self.assertEqual(images._pixels, 0)

    async def test_the_url_is_tmdbs_backdrop(self) -> None:
        fake = FakeImages()
        images = BandImages(cast(ImageCache, fake), self.root)
        await images.card("/b.jpg", Shape.of(1), "#242424")
        self.assertTrue(fake.asked[0].endswith(f"{BACKDROP_SIZE}/b.jpg"))
        self.assertEqual(fake.keeps, [False])
        self.assertIsInstance(fake.finishes[0], CardBake)

    async def test_the_cache_keeps_a_picture_unless_told_not_to(self) -> None:
        kept = await self.cache.get("https://image.tmdb.org/t/p/w780/a.jpg", (10, 10))
        self.assertIsNotNone(kept)
        self.assertEqual(len(self.cache._memory), 1)
        await self.cache.get("https://image.tmdb.org/t/p/w780/b.jpg", (10, 10), keep=False)
        self.assertEqual(len(self.cache._memory), 1)


class FileBrowserTests(ForgeTestCase):
    def test_one_file_is_selected(self) -> None:
        path = Path(r"D:\Series\The Ark\The Ark - S01E01.mkv")
        self.assertEqual(file_browser.explorer_target([path]), (path, True))

    def test_several_show_the_first_ones_folder(self) -> None:
        first = Path(r"D:\Series\The Ark\a.mkv")
        same = [first, Path(r"D:\Series\The Ark\b.mkv")]
        self.assertEqual(file_browser.explorer_target(same), (first.parent, False))
        apart = [first, Path(r"E:\Other\b.mkv")]
        self.assertEqual(file_browser.explorer_target(apart), (first.parent, False))

    def test_the_commands(self) -> None:
        path = Path(r"D:\My Series\a, b.mkv")
        self.assertEqual(
            file_browser.explorer_command(path, select=True), f'explorer /select,"{path}"'
        )
        self.assertEqual(
            file_browser.explorer_command(path.parent, select=False), f'explorer "{path.parent}"'
        )

    def test_a_forgotten_test_cant_start_the_real_one(self) -> None:
        with self.assertRaises(AssertionError):
            file_browser.open_explorer(Path("D:/a.mkv"), select=True)

    def test_a_string_without_a_shell_never_waited_for(self) -> None:
        with mock.patch("subprocess.Popen") as popen:
            REAL_START('explorer /select,"D:\\a.mkv"')
        popen.assert_called_once_with('explorer /select,"D:\\a.mkv"')
        popen.return_value.wait.assert_not_called()
        popen.return_value.communicate.assert_not_called()

    def test_explorer_not_starting_is_logged_without_the_path(self) -> None:
        with (
            mock.patch.object(file_browser, "_start", side_effect=OSError("D:\\secret")),
            self.assertLogs("app.forge.ui.file_browser", logging.WARNING) as logs,
        ):
            file_browser.open_explorer(Path("D:/secret/a.mkv"), select=True)
        self.assertNotIn("secret", "\n".join(logs.output))


class QueueCardsTests(QueueCase):
    async def files(self, *names: str) -> Path:
        folder = self.tmp / "in"
        make_files(folder, names)
        await self.add_and_settle(folder)
        return folder

    def band_key(self) -> int:
        return next(r.key for r in self.view.list.rows if r.kind == "band")

    def ids(self, key: int) -> list[int]:
        return list(self.view._bands[key])

    async def test_one_file_opens_with_it_selected_and_logs_no_path(self) -> None:
        folder = await self.files("Dune.2021.mkv")
        started: list[str] = []
        with (
            mock.patch.object(file_browser, "_start", started.append),
            self.assertLogs("app.forge.ui.queue_view", logging.INFO) as logs,
        ):
            await self.view.open_in_explorer(self.ids(self.band_key()))
        self.assertEqual(started, [f'explorer /select,"{folder / "Dune.2021.mkv"}"'])
        self.assertNotIn("Dune", "\n".join(logs.output))

    async def test_several_open_the_folder(self) -> None:
        folder = await self.files("Ted.Lasso.S04E03.mkv", "Ted.Lasso.S04E05.mkv")
        started: list[str] = []
        with mock.patch.object(file_browser, "_start", started.append):
            await self.view.open_in_explorer(self.ids(self.band_key()))
        self.assertEqual(started, [f'explorer "{folder}"'])

    async def test_it_follows_a_rename(self) -> None:
        folder = await self.files("Dune.2021.mkv")
        [item] = self.services.queue.items
        moved = folder / "Dune (2021)" / "Dune (2021).mkv"
        moved.parent.mkdir()
        (folder / "Dune.2021.mkv").rename(moved)
        item.path = moved
        started: list[str] = []
        with mock.patch.object(file_browser, "_start", started.append):
            await self.view.open_in_explorer([item.id])
        self.assertEqual(started, [f'explorer /select,"{moved}"'])

    async def test_a_file_thats_gone_says_so_and_starts_nothing(self) -> None:
        folder = await self.files("Dune.2021.mkv")
        (folder / "Dune.2021.mkv").unlink()
        said: list[str] = []
        with (
            mock.patch.object(file_browser, "_start", side_effect=AssertionError),
            mock.patch.object(self.view, "_say", side_effect=lambda text, **_: said.append(text)),
        ):
            await self.view.open_in_explorer(self.ids(self.band_key()))
        self.assertEqual(said, ["That file isn't there any more."])

    async def test_a_view_gone_during_the_check_starts_nothing(self) -> None:
        await self.files("Dune.2021.mkv")
        ids = self.ids(self.band_key())
        with (
            mock.patch.object(file_browser, "_start", side_effect=AssertionError),
            mock.patch.object(self.view, "winfo_exists", return_value=False),
        ):
            await self.view.open_in_explorer(ids)

    async def test_the_band_action_opens_it(self) -> None:
        await self.files("Dune.2021.mkv")
        key = self.band_key()
        started: list[str] = []
        with mock.patch.object(file_browser, "_start", started.append):
            self.view.list.hover_band(key)
            self.view._on_band_action(key, EXPLORER_ACTION)
            await pump_until(self.root, lambda: bool(started))
        self.assertEqual(len(started), 1)

    async def with_pictures(self, *names: str) -> FakeImages:
        fake = FakeImages()
        self.view.images = BandImages(cast(ImageCache, fake), self.view.list)
        await self.files(*names)
        for item in self.services.queue.items:
            media = item.media.model_copy(
                update={"poster_path": "/p.jpg", "backdrop_path": f"/{item.media.title}.jpg"}
            )
            item.resolution = Resolution(media=media)
        self.view.refresh()
        await pump(self.root, 0.3)
        return fake

    async def test_a_card_gets_one_picture_baked_once_and_kept_when_nothing_changed(self) -> None:
        fake = await self.with_pictures("Ted.Lasso.S04E03.mkv", "Ted.Lasso.S04E04.mkv")
        c = self.view.list.canvas
        [image] = c.find_withtag("card-image")
        self.assertEqual(sum(1 for f in fake.finishes if isinstance(f, CardBake)), 1)
        self.view.list._redraw()
        self.assertEqual(c.find_withtag("card-image"), (image,))
        self.assertEqual(sum(1 for f in fake.finishes if isinstance(f, CardBake)), 1)

    async def test_a_light_dark_switch_bakes_it_again(self) -> None:
        fake = await self.with_pictures("Dune.2021.mkv")
        self.addCleanup(ctk.set_appearance_mode, ctk.get_appearance_mode())
        for mode in ("light", "dark"):
            ctk.set_appearance_mode(mode)
            await pump(self.root, 0.3)
        fills = {f.fill for f in fake.finishes if isinstance(f, CardBake)}
        self.assertEqual(fills, {theme.pick(self.view.look.SURFACE, dark=d) for d in (False, True)})

    async def test_a_card_scrolled_out_of_sight_isnt_baked(self) -> None:
        names = [f"Show{i:02d}.S01E01.mkv" for i in range(30)]
        fake = await self.with_pictures(*names)
        baked = sum(1 for f in fake.finishes if isinstance(f, CardBake))
        self.assertLess(baked, 30)
        # Waiting ones of cards gone from view are left out, and asked again when they show.
        view = self.view
        view._bakes.clear()
        shape = view.list.card_shape(1)
        calls: list[int] = []

        async def bake() -> None:
            calls.append(1)

        key = ("card", "/Not In View.jpg", shape.width, shape.height, "#000000")
        view._loading.add(key)
        view._bakes[key] = ("/Not In View.jpg", bake)
        view._bakers += 1
        await view._bake_cards()
        self.assertEqual(calls, [])
        self.assertNotIn(key, view._loading)

    async def test_a_done_card_still_has_its_picture_at_half_strength(self) -> None:
        fake = await self.with_pictures("Dune.2021.mkv")
        [item] = self.services.queue.items
        self.services.queue.mark_finished(item.id, item.path)
        self.view.refresh()
        await pump(self.root, 0.3)
        bakes = [f for f in fake.finishes if isinstance(f, CardBake)]
        self.assertEqual(bakes[-1].strengths, card_strengths(dark=theme.is_dark(), done=True))
        self.assertEqual(bakes[-1].fill, faded_fill(self.view.look))
        self.assertEqual(len(self.view.list.canvas.find_withtag("card-image")), 1)


class DefaultLookTests(CardListCase):
    async def test_a_list_defaults_to_the_accent_applied_at_startup(self) -> None:
        self.addCleanup(theme.apply_accent, Accent.SLATE)
        theme.apply_accent(Accent.TEAL)
        holder = ctk.CTkFrame(self.root)
        self.addCleanup(holder.destroy)
        vlist = VirtualList(holder)
        self.assertEqual(vlist.look.ACCENT, theme.ACCENT)
