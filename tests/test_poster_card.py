"""Poster cards: the year / rating line, the fade, the baked card image and the Track looks."""

from __future__ import annotations

import asyncio
import random
import unittest
from unittest import mock

from PIL import Image

from app.ui import theme
from app.ui.poster_card import (
    FOLLOWING_TEXT,
    POSTER_CARD_HEIGHT,
    POSTER_CARD_WIDTH,
    RGB,
    CardFace,
    card_face,
    compose_card,
    fade_alpha,
    placeholder_image,
    track_look,
    year_rating_line,
)

BG: RGB = (22, 22, 22)
EDGE: RGB = (78, 106, 133)
OUTSIDE: RGB = (28, 28, 28)
PLACEHOLDER: RGB = (51, 51, 51)


def face(scale: float = 1.5, text_height: float = 110, **colors: RGB) -> CardFace:
    return card_face(
        scale,
        text_height,
        bg=colors.get("bg", BG),
        edge=colors.get("edge", EDGE),
        outside=colors.get("outside", OUTSIDE),
        placeholder=colors.get("placeholder", PLACEHOLDER),
    )


def noise(size: tuple[int, int], seed: int = 1) -> Image.Image:
    width, height = size
    data = random.Random(seed).randbytes(width * height * 3)
    return Image.frombytes("RGB", size, data)


class YearRatingLineTests(unittest.TestCase):
    def test_year_and_rating(self) -> None:
        self.assertEqual(year_rating_line(2024, 7.84), "2024 | ★ 7.8")
        self.assertEqual(year_rating_line(1999, 10.0), "1999 | ★ 10.0")

    def test_unknown_parts_are_left_out(self) -> None:
        self.assertEqual(year_rating_line(None, 8.1), "★ 8.1")
        self.assertEqual(year_rating_line(2024, None), "2024")
        self.assertEqual(year_rating_line(None, None), "")

    def test_a_zero_rating_is_still_a_rating(self) -> None:
        self.assertEqual(year_rating_line(2020, 0.0), "2020 | ★ 0.0")


class FadeAlphaTests(unittest.TestCase):
    def test_ends(self) -> None:
        self.assertEqual(fade_alpha(-0.5), 0.0)
        self.assertEqual(fade_alpha(0.0), 0.0)
        self.assertEqual(fade_alpha(1.0), 1.0)
        self.assertEqual(fade_alpha(1.5), 1.0)

    def test_85_percent_a_little_under_half_way(self) -> None:
        self.assertAlmostEqual(fade_alpha(0.45), 0.85)

    def test_soft_start(self) -> None:
        self.assertLess(fade_alpha(0.02), 0.01)

    def test_never_decreases(self) -> None:
        values = [fade_alpha(step / 200) for step in range(201)]
        self.assertEqual(values, sorted(values))


class CardFaceTests(unittest.TestCase):
    def test_physical_size_and_rows(self) -> None:
        card = face(1.5, text_height=110)
        self.assertEqual(card.size, (POSTER_CARD_WIDTH * 3 // 2, POSTER_CARD_HEIGHT * 3 // 2))
        self.assertEqual((card.border, card.radius), (3, 21))
        height = card.size[1]
        self.assertEqual(card.fade_from, round(height * 0.45))
        # Solid from 4 units above the text: border 2 + bottom padding 12 + text 110.
        self.assertEqual(card.solid_from, height - round((2 + 12 + 110 + 4) * 1.5))

    def test_whole_pixels_at_common_scales(self) -> None:
        for scale in (1.0, 1.25, 1.5, 1.75, 2.0):
            with self.subTest(scale=scale):
                width, height = POSTER_CARD_WIDTH * scale, POSTER_CARD_HEIGHT * scale
                self.assertEqual(face(scale).size, (int(width), int(height)))
                self.assertEqual((width, height), (int(width), int(height)))

    def test_text_taller_than_the_fade_leaves_no_fade(self) -> None:
        card = face(1.0, text_height=250)
        self.assertEqual(card.fade_from, card.solid_from)
        self.assertGreaterEqual(card.solid_from, 0)

    def test_faces_compare_by_value(self) -> None:
        self.assertEqual(face(), face())
        self.assertEqual(hash(face()), hash(face()))
        self.assertNotEqual(face(), face(outside=(0, 0, 0)))


class ComposeCardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.face = face()
        self.width, self.height = self.face.size
        self.cover = noise(self.face.size)
        self.card = compose_card(self.cover, self.face)

    def test_size_and_mode(self) -> None:
        self.assertEqual(self.card.size, self.face.size)
        self.assertEqual(self.card.mode, "RGB")

    def test_a_cover_of_another_size_is_fitted(self) -> None:
        card = compose_card(noise((185, 278)), self.face)
        self.assertEqual(card.size, self.face.size)

    def test_rows_from_solid_down_are_the_background(self) -> None:
        inset = self.face.radius
        for y in range(self.face.solid_from, self.height - inset):
            for x in range(inset, self.width - inset, 7):
                self.assertEqual(self.card.getpixel((x, y)), BG, (x, y))

    def test_rows_above_the_fade_are_the_cover(self) -> None:
        border = self.face.border
        for y in range(self.face.radius + 1, self.face.fade_from, 5):
            for x in range(border, self.width - border):
                self.assertEqual(self.card.getpixel((x, y)), self.cover.getpixel((x, y)), (x, y))

    def test_the_fade_darkens_gradually(self) -> None:
        white = Image.new("RGB", self.face.size, (255, 255, 255))
        card = compose_card(white, self.face)
        x = self.width // 2
        column = [
            card.getpixel((x, y))[0]  # type: ignore[index]
            for y in range(self.face.fade_from, self.face.solid_from)
        ]
        self.assertEqual(column, sorted(column, reverse=True))
        self.assertGreater(column[0], 245)
        self.assertLess(column[-1], 40)

    def test_border_and_corners(self) -> None:
        width, height = self.face.size
        for point in ((width // 2, 0), (width // 2, height - 1), (0, height // 2)):
            self.assertEqual(self.card.getpixel(point), EDGE, point)
        for point in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
            self.assertEqual(self.card.getpixel(point), OUTSIDE, point)

    def test_without_a_cover_the_placeholder_fades_too(self) -> None:
        card = compose_card(None, self.face)
        x = self.width // 2
        self.assertEqual(card.getpixel((x, self.face.radius)), PLACEHOLDER)
        self.assertEqual(card.getpixel((x, self.face.solid_from)), BG)

    def test_transparent_cover_parts_show_the_placeholder(self) -> None:
        cover = Image.new("RGBA", self.face.size, (200, 0, 0, 0))
        card = compose_card(cover, self.face)
        self.assertEqual(card.getpixel((self.width // 2, self.face.radius)), PLACEHOLDER)

    def test_the_face_composes_when_called_and_leaves_the_cover_alone(self) -> None:
        before = self.cover.tobytes()
        self.assertEqual(self.face(self.cover).tobytes(), self.card.tobytes())
        self.assertEqual(self.cover.tobytes(), before)


class TrackLookTests(unittest.TestCase):
    def test_idle(self) -> None:
        look = track_look("idle", "Adding…")
        self.assertEqual((look.text, look.color, look.enabled), ("+ Track", theme.ACCENT, True))
        self.assertIsNone(look.border)

    def test_busy(self) -> None:
        look = track_look("busy", "Adding…")
        self.assertEqual((look.text, look.enabled), ("Adding…", False))

    def test_tracked_follows_in_success_green(self) -> None:
        look = track_look("tracked", "Adding…")
        self.assertEqual(look.text, FOLLOWING_TEXT)
        self.assertEqual(look.text, "✓ Following")
        self.assertEqual((look.text_color, look.border), (theme.SUCCESS, theme.SUCCESS))
        self.assertEqual((look.color, look.enabled), ("transparent", False))

    def test_missing(self) -> None:
        look = track_look("missing", "Finding…")
        self.assertEqual((look.text, look.color), ("Not on TVMaze", theme.DANGER_BUTTON))
        self.assertFalse(look.enabled)


class PlaceholderImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_composed_once_for_all_cards_with_one_face(self) -> None:
        card = face(placeholder=(1, 2, 3))  # a face no other test uses: the cache is global
        with mock.patch("app.ui.poster_card.compose_card", wraps=compose_card) as composed:
            images = await asyncio.gather(*(placeholder_image(card) for _ in range(5)))
            again = await placeholder_image(card)
        self.assertEqual(composed.call_count, 1)
        self.assertTrue(all(image is again for image in images))
        self.assertEqual(again.tobytes(), compose_card(None, card).tobytes())


if __name__ == "__main__":
    unittest.main()
