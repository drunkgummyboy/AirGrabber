"""The calendar's full episode card: its sizes per card size, the baked poster piece and right
cap (pure Pillow), and the card itself on a hidden Tk root."""

from __future__ import annotations

import asyncio
import tkinter as tk
import unittest
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import customtkinter as ctk
from PIL import Image

from app.models import CalendarEntry, CardSize, Episode, Show
from app.services import ImageCache
from app.ui.calendar_cards import (
    NOT_AIRED_TEXT,
    CompactEpisodeCard,
    EpisodeCard,
    card_title,
    poster_url,
)
from app.ui.calendar_face import (
    CARD_HEIGHT,
    POSTER_SHARE,
    ROW_MIN_FACTOR,
    CapFace,
    PosterFace,
    compose_cap,
    compose_poster,
    cover_size,
    episode_layout,
    fade_alpha,
    poster_area,
    poster_face,
    row_scale,
    text_width,
)
from app.ui.card_scale import FULL, CardScale, line_height
from app.ui.download_control import DownloadActions, DownloadControl, DownloadStates
from app.ui.poster_card import FADE_MAX

RED = (200, 0, 0)
BG = (36, 36, 36)
EDGE = (78, 106, 133)
OUTSIDE = (18, 22, 32)
PLACEHOLDER = (51, 51, 51)


def linespace(size: int, bold: bool) -> int:
    return line_height(size)


def close(actual: Any, expected: tuple[int, ...], tolerance: int = 3) -> bool:
    """`actual` (a pixel) within `tolerance` of `expected` in every channel."""
    return all(abs(a - e) <= tolerance for a, e in zip(actual, expected, strict=True))


def item_text(card: EpisodeCard, item: int) -> str:
    """The text of a canvas item on `card`."""
    canvas: Any = card._art
    return str(canvas.itemcget(item, "text"))


class LayoutTests(unittest.TestCase):
    def test_xl_is_the_designed_card(self) -> None:
        layout = episode_layout(FULL, linespace=linespace)
        self.assertEqual(layout.height, CARD_HEIGHT)
        area = poster_area(layout, 200)
        self.assertEqual(area.width, 100)  # the left half
        self.assertLess(area.fade_from, area.text_x)
        self.assertLess(area.text_x, area.width)  # the text starts over the fade
        self.assertEqual(text_width(layout, area, 200), 200 - area.text_x - layout.pad_right)

    def test_poster_fills_about_half_the_card(self) -> None:
        for size in CardSize:
            layout = episode_layout(CardScale.of(size), linespace=linespace)
            for width in (120, 160, 200, 240):
                with self.subTest(size=size, width=width):
                    area = poster_area(layout, width)
                    self.assertEqual(area.width % 4, 0)
                    # Half the card, unless that would crop the poster to a strip: then at
                    # most as wide as the card is tall.
                    expected = min(width * POSTER_SHARE, layout.height)
                    self.assertLessEqual(abs(area.width - expected), 4)
                    self.assertLessEqual(area.width, layout.height)

    def test_poster_area_snaps_so_resizing_rarely_rebakes(self) -> None:
        layout = episode_layout(FULL, linespace=linespace)
        # Twenty widths of a column being dragged: four poster sizes, not twenty.
        widths = {poster_area(layout, width).width for width in range(180, 200)}
        self.assertLessEqual(len(widths), 4)

    def test_download_row_fits_its_full_text_on_the_usual_card(self) -> None:
        # About 200 units wide at the default window: "⬇ Download" (71 units at 11 pt bold)
        # + its corners and slack, a gap and "⋯" need 118 at XL.
        layout = episode_layout(FULL, linespace=linespace)
        self.assertGreaterEqual(text_width(layout, poster_area(layout, 200), 200), 118)

    def test_every_size_fits_two_name_lines_the_code_and_the_row(self) -> None:
        for size in CardSize:
            scale = CardScale.of(size)
            with self.subTest(size=size):
                layout = episode_layout(scale, linespace=linespace)
                needed = (
                    layout.pad_top
                    + 2 * line_height(layout.title_size)
                    + layout.code_gap
                    + line_height(layout.code_size)
                    + layout.row_height
                    + layout.pad_bottom
                )
                self.assertGreaterEqual(layout.height, needed)
                self.assertGreaterEqual(layout.title_size, 12)
                self.assertGreaterEqual(layout.code_size, 10)

    def test_smaller_sizes_make_smaller_cards(self) -> None:
        heights = [
            episode_layout(CardScale.of(size), linespace=linespace).height for size in CardSize
        ]
        self.assertEqual(heights, sorted(heights))
        self.assertLess(heights[0], heights[-1])

    def test_row_scale(self) -> None:
        self.assertAlmostEqual(row_scale(FULL).factor, 0.75)
        self.assertAlmostEqual(row_scale(CardScale.of(CardSize.XS)).factor, ROW_MIN_FACTOR)
        self.assertEqual(row_scale(CardScale.of(CardSize.XS)).px(36), 24)

    def test_cover_size_keeps_the_poster_shape(self) -> None:
        self.assertEqual(cover_size((92, 112)), (92, 138))
        self.assertEqual(cover_size((60, 200)), (133, 200))


class FadeTests(unittest.TestCase):
    def test_profile(self) -> None:
        def alpha(x: float) -> float:
            return fade_alpha(x, start=30, knee=60, end=90)

        self.assertEqual(alpha(0), 0.0)
        self.assertEqual(alpha(30), 0.0)
        self.assertAlmostEqual(alpha(60), FADE_MAX)
        self.assertEqual(alpha(90), 1.0)
        self.assertEqual(alpha(200), 1.0)
        values = [alpha(x) for x in range(0, 100)]
        self.assertEqual(values, sorted(values))
        self.assertTrue(all(alpha(x) <= FADE_MAX for x in range(0, 61)))


def face(**changes: Any) -> PosterFace:
    values: dict[str, Any] = {
        "size": (138, 168),
        "border": 2,
        "radius": 12,
        "fade_from": 42,
        "fade_knee": 93,
        "solid_left": 90,
        "solid_top": 100,
        "row_fade": 15,
        "bg": BG,
        "edge": EDGE,
        "outside": OUTSIDE,
        "placeholder": PLACEHOLDER,
    }
    values.update(changes)
    return PosterFace(**values)


class ComposeTests(unittest.TestCase):
    def test_poster_piece(self) -> None:
        image = compose_poster(Image.new("RGB", cover_size((138, 168)), RED), face())
        self.assertEqual(image.size, (138, 168))
        self.assertEqual(image.mode, "RGB")
        pixel = image.getpixel
        self.assertTrue(close(pixel((0, 0)), OUTSIDE))  # outside the rounded corner
        self.assertTrue(close(pixel((0, 84)), EDGE))  # the left border
        self.assertTrue(close(pixel((120, 0)), EDGE))  # the top border runs on to the right
        self.assertTrue(close(pixel((10, 50)), RED))  # the poster, before the fade
        mixed = tuple(round(FADE_MAX * b + (1 - FADE_MAX) * r) for b, r in zip(BG, RED))
        self.assertTrue(close(pixel((93, 50)), mixed, 6))  # where the text starts
        self.assertTrue(close(pixel((137, 50)), BG, 6))  # solid at its right edge
        self.assertTrue(close(pixel((110, 130)), BG))  # under the Download row
        self.assertTrue(close(pixel((137, 84)), BG, 6))  # no right border: the card goes on

    def test_poster_is_cropped_near_its_top(self) -> None:
        cover = Image.new("RGB", (138, 207), RED)
        cover.paste((0, 0, 200), (0, 0, 138, 30))  # blue top band, inside the kept part
        cover.paste((0, 200, 0), (0, 177, 138, 207))  # green bottom band, cropped away
        image = compose_poster(cover, face())
        self.assertTrue(close(image.getpixel((10, 12)), (0, 0, 200)))
        self.assertTrue(close(image.getpixel((10, 160)), RED, 8))

    def test_placeholder(self) -> None:
        image = compose_poster(None, face())
        self.assertTrue(close(image.getpixel((10, 50)), PLACEHOLDER))

    def test_right_cap(self) -> None:
        cap = CapFace(size=(15, 168), border=2, radius=12, bg=BG, edge=EDGE, outside=OUTSIDE)
        image = compose_cap(cap)
        self.assertEqual(image.size, (15, 168))
        self.assertTrue(close(image.getpixel((14, 0)), OUTSIDE))  # outside the rounded corner
        self.assertTrue(close(image.getpixel((14, 84)), EDGE))  # the right border
        self.assertTrue(close(image.getpixel((0, 0)), EDGE))  # the top border comes in
        self.assertTrue(close(image.getpixel((5, 84)), BG))

    def test_face_from_layout(self) -> None:
        layout = episode_layout(FULL, linespace=linespace)
        area = poster_area(layout, 200)
        made = poster_face(
            layout, area, 1.5, 168, bg=BG, edge=EDGE, outside=OUTSIDE, placeholder=PLACEHOLDER
        )
        self.assertEqual(made.size, (round(area.width * 1.5), 168))
        self.assertEqual(made.fade_knee, round(area.text_x * 1.5))
        self.assertLess(made.solid_left, made.fade_knee)
        self.assertLess(made.solid_top, 168 - round(layout.row_height * 1.5))


class TextTests(unittest.TestCase):
    def test_poster_url(self) -> None:
        show = Show(id=1, name="X", poster_url="medium.jpg", poster_url_original="original.jpg")
        self.assertEqual(poster_url(show, 138), "medium.jpg")
        self.assertEqual(poster_url(show, 230), "original.jpg")
        self.assertEqual(poster_url(Show(id=2, name="Y", poster_url="m.jpg"), 230), "m.jpg")
        self.assertIsNone(poster_url(Show(id=3, name="Z"), 100))

    def test_title_in_two_lines_with_explicit_breaks(self) -> None:
        self.assertEqual(card_title("The Ark", 10, len), "The Ark")
        self.assertEqual(
            card_title("Georgie & Mandy's First Marriage", 10, len), "Georgie &\nMandy's F…"
        )


class FakeImages:
    def __init__(self) -> None:
        self.sizes: list[tuple[int, int]] = []

    async def get(
        self, url: str | None, size: tuple[int, int], *, finish: Any = None
    ) -> Image.Image | None:
        self.sizes.append(size)
        cover = Image.new("RGB", size, RED)
        return cover if finish is None else cast(Image.Image, finish(cover))


def calendar_entry(*, aired: bool, imdb: str | None = "tt0944947") -> CalendarEntry:
    now = datetime.now(UTC)
    airstamp = now - timedelta(days=1) if aired else now + timedelta(days=2)
    return CalendarEntry(
        show=Show(id=7, name="Severance", imdb_id=imdb, poster_url="https://img/p.jpg"),
        episode=Episode(
            id=70,
            show_id=7,
            season=2,
            number=1,
            air_date=airstamp.date(),
            airstamp=airstamp,
        ),
        is_tracked=True,
    )


class EpisodeCardTests(unittest.IsolatedAsyncioTestCase):
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

    def card(
        self,
        entry: CalendarEntry,
        images: FakeImages,
        scale: CardScale = FULL,
        on_open_show: Any = None,
    ) -> EpisodeCard:
        actions: DownloadActions[CalendarEntry] = DownloadActions(
            download=lambda _entry: None, choose=lambda _entry: None, states=DownloadStates()
        )
        return EpisodeCard(
            self.root,
            entry,
            images=cast(ImageCache, images),
            actions=actions,
            scale=scale,
            on_open_show=on_open_show,
        )

    def controls(self, card: EpisodeCard) -> list[DownloadControl[Any]]:
        return [child for child in card.winfo_children() if isinstance(child, DownloadControl)]

    async def test_not_aired_is_a_muted_line(self) -> None:
        card = self.card(calendar_entry(aired=False), FakeImages())
        self.assertEqual(self.controls(card), [])
        self.assertEqual(item_text(card, card._status), NOT_AIRED_TEXT)

    async def test_aired_has_the_download_row(self) -> None:
        card = self.card(calendar_entry(aired=True), FakeImages())
        self.assertEqual(len(self.controls(card)), 1)
        self.assertEqual(item_text(card, card._status), "")

    async def test_follows_the_card_size(self) -> None:
        small = self.card(calendar_entry(aired=True), FakeImages(), CardScale.of(CardSize.XS))
        large = self.card(calendar_entry(aired=True), FakeImages())
        self.assertLess(small.cget("height"), large.cget("height"))

    async def test_poster_and_title_open_the_show_window(self) -> None:
        opened: list[Show] = []
        entry = calendar_entry(aired=True)
        card = self.card(entry, FakeImages(), on_open_show=opened.append)
        canvas = card._art
        self.assertEqual(set(canvas.find_withtag("open")), {card._poster, card._title})
        self.assertNotEqual(canvas.tag_bind("open", "<Button-1>"), "")
        card._open()
        self.assertEqual(opened, [entry.show])

    async def test_without_a_show_window_nothing_is_clickable(self) -> None:
        card = self.card(calendar_entry(aired=True), FakeImages())
        self.assertEqual(card._art.tag_bind("open", "<Button-1>"), "")

    async def test_compact_card_info_icon_opens_the_show_window(self) -> None:
        opened: list[Show] = []
        entry = calendar_entry(aired=True)
        actions: DownloadActions[CalendarEntry] = DownloadActions(
            download=lambda _entry: None, choose=lambda _entry: None, states=DownloadStates()
        )
        card = CompactEpisodeCard(self.root, entry, actions=actions, on_open_show=opened.append)
        assert card.info_icon is not None
        self.assertEqual(card.info_icon.cget("text"), "ⓘ")
        card.info_icon._clicked()  # as a click does
        self.assertEqual(opened, [entry.show])
        bare = CompactEpisodeCard(self.root, entry, actions=actions)
        self.assertIsNone(bare.info_icon)

    async def test_bakes_the_poster_at_physical_size_and_lays_out_the_text(self) -> None:
        images = FakeImages()
        card = self.card(calendar_entry(aired=True), images)
        scaling = ctk.ScalingTracker.get_widget_scaling(card)
        height = round(card.cget("height") * scaling)

        def resize(units: int) -> None:
            card._on_resize(SimpleNamespace(width=round(units * scaling), height=height))

        resize(200)
        assert card._art_task is not None
        await card._art_task
        poster_width = round(100 * scaling)  # the left half
        self.assertEqual(images.sizes, [cover_size((poster_width, height))])
        self.assertIn("poster", card._photos)
        self.assertIn("cap", card._photos)
        self.assertEqual(card._photos["poster"].width(), poster_width)
        self.assertEqual(item_text(card, card._title), "Severance")
        self.assertEqual(item_text(card, card._code), "S02E01")
        # The code sits under the name, both from where the text starts, over the poster.
        title_x, title_y = card._art.coords(card._title)
        code_x, code_y = card._art.coords(card._code)
        self.assertEqual(title_x, code_x)
        self.assertGreater(code_y, title_y)
        self.assertLess(title_x, poster_width)
        # A slightly different width only moves things: the same images.
        task = card._art_task
        resize(202)
        self.assertIs(card._art_task, task)
        self.assertIsNone(card._rebake_job)
        self.assertEqual(card._art.coords(card._cap), [round(202 * scaling), 0.0])
        # A narrower column: the text moves at once, the poster is baked again once it settles.
        resize(160)
        self.assertIs(card._art_task, task)
        self.assertIsNotNone(card._rebake_job)
        self.assertLess(card._art.coords(card._title)[0], title_x)
        card._bake()  # as the settle timer does
        self.assertIsNot(card._art_task, task)
        await card._art_task
        self.assertEqual(card._photos["poster"].width(), round(80 * scaling))
        self.assertEqual(images.sizes[-1], cover_size((round(80 * scaling), height)))


if __name__ == "__main__":
    unittest.main()
