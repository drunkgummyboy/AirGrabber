"""Draw the What's new window's pictures into app/assets/whats_new/ (Pillow, not shipped: an
installed copy only has app/, so it gets the finished PNGs).

    python -m tools.whats_new_art

A picture stands for a big feature (`Feature` in app/whats_new.py) and has no words, so the one
file works in both languages and both looks. Each is 3:1, 1464 x 488, RGBA, twice the size it
shows at on a 100 % screen, with its top corners rounded transparently to fit the dialog's card.
A drawing is written in a 720 x 240 view, as an SVG's coordinates, and supersampled. A new
release adds its drawing to PICTURES and removes the ones of the Features it removed.
"""

from __future__ import annotations

import argparse
import math
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path

from PIL import Image, ImageChops, ImageColor, ImageDraw

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "app" / "assets" / "whats_new"

WIDTH, HEIGHT = 1464, 488
VIEW_WIDTH = 720  # a drawing's coordinates: 720 x 240
SCALE = WIDTH / VIEW_WIDTH
SUPERSAMPLE = 4
# The card's corner radius (12 units) where the picture is 732 units wide.
CORNER_RADIUS = 24

Point = tuple[float, float]
Box = tuple[float, float, float, float]  # x, y, width, height in the view
Stop = tuple[float, str, float]  # offset, colour, opacity
Rgba = tuple[int, int, int, int]

# The view's accents and the app's dark colours.
SKY_TOP, SKY_BOTTOM = "#16222f", "#34526f"
STEEL = "#8FB0D0"
ON_ACCENT = "#DCE4EE"
MUTED = "#A4B2C6"
WINDOW = "#161616"
SURFACE = "#242424"
WHITE = "#ffffff"


# -- paints: full-size images, sampled at the pixels' middles ------------------------------------


def _rgba(colour: str, opacity: float = 1.0) -> Rgba:
    red, green, blue = ImageColor.getrgb(colour)[:3]
    return red, green, blue, round(255 * opacity)


def _between(stops: Sequence[Stop], at: float) -> Rgba:
    """The colour at offset `at` (as SVG: the first and last stops carry on beyond their ends)."""
    if at <= stops[0][0]:
        return _rgba(stops[0][1], stops[0][2])
    for (start, colour_a, opacity_a), (end, colour_b, opacity_b) in zip(stops, stops[1:]):
        if at <= end:
            part = (at - start) / (end - start)
            a, b = ImageColor.getrgb(colour_a), ImageColor.getrgb(colour_b)
            mixed = [a[i] + (b[i] - a[i]) * part for i in range(3)]
            opacity = opacity_a + (opacity_b - opacity_a) * part
            return round(mixed[0]), round(mixed[1]), round(mixed[2]), round(255 * opacity)
    return _rgba(stops[-1][1], stops[-1][2])


def solid(colour: str, opacity: float = 1.0) -> Image.Image:
    return Image.new("RGBA", (WIDTH, HEIGHT), _rgba(colour, opacity))


def across(box: Box, stops: Sequence[Stop]) -> Image.Image:
    """A linear gradient from `box`'s left to its right."""
    left, width = box[0] * SCALE, box[2] * SCALE
    row = bytearray()
    for x in range(WIDTH):
        row.extend(_between(stops, (x + 0.5 - left) / width))
    strip = Image.frombytes("RGBA", (WIDTH, 1), bytes(row))
    return strip.resize((WIDTH, HEIGHT), Image.Resampling.NEAREST)


def down(box: Box, stops: Sequence[Stop]) -> Image.Image:
    """A linear gradient from `box`'s top to its bottom."""
    top, height = box[1] * SCALE, box[3] * SCALE
    column = bytearray()
    for y in range(HEIGHT):
        column.extend(_between(stops, (y + 0.5 - top) / height))
    strip = Image.frombytes("RGBA", (1, HEIGHT), bytes(column))
    return strip.resize((WIDTH, HEIGHT), Image.Resampling.NEAREST)


def corner_to_corner(stops: Sequence[Stop]) -> Image.Image:
    """A linear gradient over the whole view, x1=0 y1=0 x2=1 y2=1 in its own (3:1) proportions."""
    pixels = bytearray()
    for y in range(HEIGHT):
        down_part = (y + 0.5) / HEIGHT
        for x in range(WIDTH):
            pixels.extend(_between(stops, ((x + 0.5) / WIDTH + down_part) / 2))
    return Image.frombytes("RGBA", (WIDTH, HEIGHT), bytes(pixels))


def glow(colour: str, centre: Point, radius: float, opacity: float) -> Image.Image:
    """A radial gradient over the whole view, in its own proportions (an ellipse): `opacity` at
    `centre` (fractions of the view), nothing at `radius`."""
    red, green, blue = ImageColor.getrgb(colour)[:3]
    pixels = bytearray()
    for y in range(HEIGHT):
        dy = ((y + 0.5) / HEIGHT - centre[1]) ** 2
        for x in range(WIDTH):
            at = math.sqrt(((x + 0.5) / WIDTH - centre[0]) ** 2 + dy) / radius
            pixels.extend((red, green, blue, round(255 * opacity * max(0.0, 1 - at))))
    return Image.frombytes("RGBA", (WIDTH, HEIGHT), bytes(pixels))


# -- shapes, as points in the view ---------------------------------------------------------------


def _arc(centre: Point, radius: float, start: float, steps: int = 24) -> list[Point]:
    """A quarter of a circle, clockwise on screen, from `start` degrees."""
    x, y = centre
    return [
        (
            x + radius * math.cos(math.radians(start + 90 * step / steps)),
            y + radius * math.sin(math.radians(start + 90 * step / steps)),
        )
        for step in range(steps + 1)
    ]


def round_rect(x: float, y: float, width: float, height: float, radius: float = 0) -> list[Point]:
    """A rectangle's outline, as an SVG <rect> runs: from the top edge's start, clockwise."""
    radius = min(radius, width / 2, height / 2)
    if radius <= 0:
        return [(x, y), (x + width, y), (x + width, y + height), (x, y + height)]
    return [
        (x + radius, y),
        *_arc((x + width - radius, y + radius), radius, -90),
        *_arc((x + width - radius, y + height - radius), radius, 0),
        *_arc((x + radius, y + height - radius), radius, 90),
        *_arc((x + radius, y + radius), radius, 180),
    ]


def circle(x: float, y: float, radius: float) -> list[Point]:
    return [
        (x + radius * math.cos(2 * math.pi * step / 180), y + radius * math.sin(2 * math.pi * step / 180))
        for step in range(180)
    ]


def _dashes(outline: Sequence[Point], on: float, off: float) -> Iterator[list[Point]]:
    """A closed outline cut into dashes, from its first point onwards."""
    closed = [*outline, outline[0]]
    dash: list[Point] = [closed[0]]
    left, drawing = on, True
    for (x1, y1), (x2, y2) in zip(closed, closed[1:]):
        length = math.hypot(x2 - x1, y2 - y1)
        walked = 0.0
        while length - walked > left:
            walked += left
            point = (x1 + (x2 - x1) * walked / length, y1 + (y2 - y1) * walked / length)
            if drawing:
                yield [*dash, point]
                dash = []
            else:
                dash = [point]
            drawing = not drawing
            left = on if drawing else off
        left -= length - walked
        if drawing:
            dash.append((x2, y2))
    if drawing and len(dash) > 1:
        yield dash


def hammers(x: float, y: float, scale: float) -> Iterator[list[Point]]:
    """The crossed hammers: two handles and two heads, as polygons."""
    for degrees in (42.0, -42.0):
        turn = math.radians(degrees)
        cos, sin = math.cos(turn), math.sin(turn)
        for part in (round_rect(-1.6, -9, 3.2, 19, 1.4), round_rect(-6, -11, 12, 6, 1.6)):
            yield [
                (x + scale * (px * cos - py * sin), y + scale * (px * sin + py * cos))
                for px, py in part
            ]


# -- masks and layers ----------------------------------------------------------------------------


def _big(point: Point) -> tuple[float, float]:
    """A point on the large mask. Pillow counts a pixel's middle as its whole number: half off."""
    return point[0] * SCALE * SUPERSAMPLE - 0.5, point[1] * SCALE * SUPERSAMPLE - 0.5


class Mask:
    """What a shape covers: drawn SUPERSAMPLE times larger, scaled down when done."""

    def __init__(self) -> None:
        self._image = Image.new("L", (WIDTH * SUPERSAMPLE, HEIGHT * SUPERSAMPLE), 0)
        self._draw = ImageDraw.Draw(self._image)

    def add(self, *polygons: Iterable[Point]) -> Mask:
        for polygon in polygons:
            self._draw.polygon([_big(point) for point in polygon], fill=255)
        return self

    def take(self, polygon: Iterable[Point]) -> Mask:
        self._draw.polygon([_big(point) for point in polygon], fill=0)
        return self

    def line(self, points: Sequence[Point], width: float, *, round_ends: bool = False) -> Mask:
        thick = round(width * SCALE * SUPERSAMPLE)
        self._draw.line([_big(point) for point in points], fill=255, width=thick, joint="curve")
        if round_ends:
            for point in (points[0], points[-1]):
                self.add(circle(point[0], point[1], width / 2))
        return self

    def done(self) -> Image.Image:
        return self._image.reduce(SUPERSAMPLE)


def rect_mask(x: float, y: float, width: float, height: float, radius: float = 0) -> Image.Image:
    return Mask().add(round_rect(x, y, width, height, radius)).done()


def border_mask(
    x: float, y: float, width: float, height: float, radius: float, thick: float
) -> Image.Image:
    """An SVG stroke on a rounded rectangle: half outside its edge, half inside."""
    half = thick / 2
    outer = round_rect(x - half, y - half, width + thick, height + thick, radius + half)
    inner = round_rect(x + half, y + half, width - thick, height - thick, radius - half)
    return Mask().add(outer).take(inner).done()





def _with_alpha(image: Image.Image, opacity: float) -> Image.Image:
    faded = image.copy()
    faded.putalpha(image.getchannel("A").point(lambda value: round(value * opacity)))
    return faded


class Layer:
    """A drawing's stack: what is painted goes over what is there, as in an SVG."""

    def __init__(self) -> None:
        self.image = Image.new("RGBA", (WIDTH, HEIGHT), (0, 0, 0, 0))

    def paint(self, paint: Image.Image, mask: Image.Image | None = None) -> None:
        if mask is None:
            self.image = Image.alpha_composite(self.image, paint)
            return
        piece = paint.copy()
        piece.putalpha(ImageChops.multiply(paint.getchannel("A"), mask))
        self.image = Image.alpha_composite(self.image, piece)

    def rect(
        self,
        x: float,
        y: float,
        width: float,
        height: float,
        radius: float,
        fill: str | Image.Image,
        *,
        opacity: float = 1.0,
    ) -> None:
        paint = solid(fill, opacity) if isinstance(fill, str) else fill
        self.paint(paint, rect_mask(x, y, width, height, radius))

    def over(self, other: Layer, *, opacity: float = 1.0, clip: Image.Image | None = None) -> None:
        """A finished group on top, at a group's opacity and through a clip path."""
        piece = other.image if opacity >= 1 else _with_alpha(other.image, opacity)
        self.paint(piece, clip)


def _background() -> Layer:
    """The dark blue diagonal both pictures stand on."""
    layer = Layer()
    layer.paint(corner_to_corner(((0, SKY_TOP, 1), (1, SKY_BOTTOM, 1))))
    return layer


# -- the pictures --------------------------------------------------------------------------------


def _file_card(layer: Layer, y: float, colour: str, dark: str) -> None:
    """A card in the picture of the main window: its tint at the right, poster, two lines."""
    box: Box = (270, y, 388, 44)
    layer.rect(*box, 7, "#262c33")
    layer.rect(*box, 7, across(box, ((0.45, colour, 0), (1, colour, 0.75))))
    poster: Box = (278, y + 7, 21, 30)
    layer.rect(*poster, 3, down(poster, ((0, colour, 1), (1, dark, 1))))
    layer.rect(308, y + 12, 92, 7, 3.5, WHITE)
    layer.rect(308, y + 25, 62, 5, 2.5, MUTED)


def draw_mediaforge_page() -> Image.Image:
    """The separate window on its way into the main window, whose switch has a third part with
    a hammer and a badge."""
    layer = _background()
    layer.paint(glow(STEEL, (0.66, 0.42), 0.55, 0.38))

    # The old separate window, on its way in.
    old = Layer()
    old.rect(46, 74, 122, 92, 10, "#1b2733")
    dashes = Mask()
    for dash in _dashes(round_rect(46, 74, 122, 92, 10), 7, 6):
        dashes.line(dash, 2)
    old.paint(solid(STEEL), dashes.done())
    old.paint(solid(WHITE), Mask().add(*hammers(107, 120, 1.5)).done())
    layer.over(old, opacity=0.7)

    # The arrow.
    layer.paint(solid(ON_ACCENT), Mask().line(((178, 120), (232, 120)), 3.5, round_ends=True).done())
    layer.paint(
        solid(ON_ACCENT), Mask().line(((224, 110), (236, 120), (224, 130)), 3.5, round_ends=True).done()
    )

    # The main window.
    layer.rect(254, 26, 420, 190, 14, WINDOW)
    layer.paint(solid("#5f7d9b"), border_mask(254, 26, 420, 190, 14, 1.5))
    layer.paint(solid(STEEL), Mask().add(circle(276, 48, 8)).done())
    layer.rect(292, 43, 58, 9, 4.5, ON_ACCENT)

    # The switch.
    layer.rect(436, 33, 226, 31, 15.5, SURFACE)
    layer.paint(solid("#3a3a3a"), border_mask(436, 33, 226, 31, 15.5, 1))
    layer.rect(456, 45, 36, 7, 3.5, MUTED)
    layer.rect(512, 45, 36, 7, 3.5, MUTED)
    layer.rect(566, 36, 93, 25, 12.5, "#5f84a8")
    layer.paint(solid(WHITE), Mask().add(*hammers(596, 48.5, 0.82)).done())
    layer.paint(solid("#E0A030"), Mask().add(circle(637, 48.5, 8.5)).done())

    _file_card(layer, 80, "#6b8f4e", "#1d3a24")
    _file_card(layer, 132, "#b4566b", "#3b1a28")
    layer.rect(270, 184, 388, 22, 7, "#262c33")
    return layer.image


def _folder() -> list[Point]:
    """The open-in-Explorer button's folder, as its SVG path runs."""
    return [
        (596, 52),
        (604, 52),
        (607, 55),
        (618, 55),
        *_arc((618, 56.5), 1.5, -90),
        (619.5, 67.5),
        *_arc((618, 67.5), 1.5, 0),
        (596, 69),
        *_arc((596, 67.5), 1.5, 90),
        (594.5, 53.5),
        *_arc((596, 53.5), 1.5, 180),
    ]


def draw_one_card() -> Image.Image:
    """One card with a landscape behind its title and all its files, nothing between them."""
    layer = _background()
    box: Box = (70, 22, 580, 196)
    card = Layer()
    card.rect(
        *box, 0, down(box, ((0, "#e58a5f", 1), (0.62, "#7a5a86", 1), (1, "#2c3d5c", 1)))
    )
    card.paint(solid("#ffe2b0"), Mask().add(circle(540, 82, 30)).done())
    card.paint(
        solid("#46396a"),
        Mask()
        .add(((330, 170), (430, 92), (500, 146), (570, 78), (650, 150), (650, 218), (330, 218)))
        .done(),
    )
    card.paint(
        solid("#2a2748"),
        Mask()
        .add(((300, 200), (420, 140), (520, 186), (610, 134), (650, 160), (650, 218), (300, 218)))
        .done(),
    )
    card.rect(*box, 0, across(box, ((0.3, SURFACE, 1), (0.72, SURFACE, 0))))
    card.rect(*box, 0, down(box, ((0.3, SURFACE, 0), (1, SURFACE, 0.62))))

    # The title.
    poster: Box = (90, 40, 50, 74)
    card.rect(*poster, 6, down(poster, ((0, "#7c6fd6", 1), (1, "#34205c", 1))))
    card.rect(156, 56, 166, 14, 7, WHITE)
    card.rect(156, 82, 112, 8, 4, MUTED)

    # Its open-in-Explorer button.
    card.rect(584, 42, 46, 36, 8, SURFACE)
    card.paint(solid("#5a5a5a"), border_mask(584, 42, 46, 36, 8, 1))
    folder = _folder()
    card.paint(solid(WHITE), Mask().line([*folder, folder[0], folder[1]], 2).done())

    # Its files, no line between them and the title.
    for y, name_width in ((130, 96), (158, 118), (186, 84)):
        card.rect(156, y, 42, 8, 4, STEEL)
        card.rect(214, y, name_width, 8, 4, WHITE)
        card.rect(470, y + 1, 146, 6, 3, ON_ACCENT, opacity=0.75)
        card.paint(solid(STEEL), Mask().add(circle(630, y + 4, 4)).done())

    layer.over(card, clip=rect_mask(*box, 16))
    return layer.image


def _poster(layer: Layer, x: float, y: float, colour: str, dark: str) -> None:
    """A poster card of the Library page: the picture with a ring, a title and a line under it."""
    layer.rect(x, y, 62, 112, 7, "#20262d")
    poster: Box = (x + 5, y + 5, 52, 74)
    layer.rect(*poster, 5, down(poster, ((0, colour, 1), (1, dark, 1))))
    layer.paint(solid(dark), Mask().add(circle(x + 40, y + 36, 11)).done())
    layer.paint(solid(colour), Mask().add(circle(x + 40, y + 36, 5)).done())
    layer.rect(x + 11, y + 86, 40, 6, 3, WHITE)
    layer.rect(x + 16, y + 97, 30, 4, 2, MUTED)


def draw_library() -> Image.Image:
    """The main window with a Library part lit on its switch (a shelf of spines) and Plex's
    shows and films under it as posters, an amber Plex chevron glowing beside them; one poster
    followed (a green tick), one to follow (a plus)."""
    layer = _background()
    layer.paint(glow("#E5A00D", (0.12, 0.55), 0.35, 0.30))
    layer.paint(glow(STEEL, (0.7, 0.4), 0.55, 0.32))

    # Plex's chevron, beside the window.
    chevron = ((52, 82), (84, 82), (120, 120), (84, 158), (52, 158), (88, 120))
    layer.paint(solid("#E5A00D"), Mask().add(chevron).done())

    # The main window and its switch: Series, Movies, Library (lit), MediaForge.
    layer.rect(150, 18, 540, 206, 14, WINDOW)
    layer.paint(solid("#5f7d9b"), border_mask(150, 18, 540, 206, 14, 1.5))
    layer.paint(solid(STEEL), Mask().add(circle(172, 40, 8)).done())
    layer.rect(188, 35, 58, 9, 4.5, ON_ACCENT)
    layer.rect(380, 25, 296, 31, 15.5, SURFACE)
    layer.paint(solid("#3a3a3a"), border_mask(380, 25, 296, 31, 15.5, 1))
    layer.rect(398, 37, 34, 7, 3.5, MUTED)
    layer.rect(450, 37, 34, 7, 3.5, MUTED)
    layer.rect(500, 28, 80, 25, 12.5, "#5f84a8")
    for index, height in enumerate((13, 11, 13, 9)):  # a shelf of spines
        layer.rect(526 + index * 7, 47 - height, 5, height, 1, WHITE)
    layer.rect(530 - 6, 47, 34, 2, 1, WHITE)
    layer.rect(598, 37, 62, 7, 3.5, MUTED)

    # The posters.
    colours = (
        ("#4ac0d8", "#13315a"), ("#7ad04f", "#1e5a2a"), ("#e0b44a", "#5a3a12"),
        ("#b46ad8", "#3a1a5a"), ("#e06a5a", "#5a1a20"), ("#4ad8b0", "#13504a"),
        ("#6a8ad8", "#1a2a5a"),
    )
    for index, (colour, dark) in enumerate(colours):
        _poster(layer, 170 + index * 72, 72, colour, dark)
    # Followed: a green tick on the second; to follow: a plus on the fifth.
    layer.paint(solid("#2FA572"), Mask().add(circle(276, 82, 9)).done())
    layer.paint(
        solid(WHITE), Mask().line(((271.5, 82), (275, 85.5), (281, 78.5)), 2.2, round_ends=True).done()
    )
    layer.paint(solid("#5f84a8"), Mask().add(circle(492, 82, 9)).done())
    layer.paint(solid(WHITE), Mask().line(((487, 82), (497, 82)), 2.2, round_ends=True).done())
    layer.paint(solid(WHITE), Mask().line(((492, 77), (492, 87)), 2.2, round_ends=True).done())
    layer.rect(170, 196, 500, 14, 7, "#20262d")
    return layer.image


def _jump(layer: Layer, x: float, y: float, clockwise: bool) -> None:
    """The player's jump back / forward button: three quarters of a circle, ending at the top in
    an arrow head that points the way it turns."""
    sign = 1 if clockwise else -1
    points = [
        (x + 8 * math.sin(math.radians(sign * degrees)), y - 8 * math.cos(math.radians(sign * degrees)))
        for degrees in range(90, 361, 10)
    ]
    layer.paint(solid(ON_ACCENT), Mask().line(points, 2, round_ends=True).done())
    head = ((x - sign * 0.5, y - 13), (x + sign * 5, y - 8), (x - sign * 0.5, y - 3))
    layer.paint(solid(ON_ACCENT), Mask().add(head).done())


def draw_player() -> Image.Image:
    """AirGrabber's own player: a window playing a film (a sunset over hills), its seek bar with
    the played part lit, the controls under it, and a Skip intro pill over the video."""
    layer = _background()
    layer.paint(glow(STEEL, (0.5, 0.45), 0.55, 0.36))
    layer.paint(glow("#e58a5f", (0.5, 0.35), 0.3, 0.16))

    # The window and the film in it.
    layer.rect(120, 14, 480, 212, 14, WINDOW)
    layer.paint(solid("#5f7d9b"), border_mask(120, 14, 480, 212, 14, 1.5))
    box: Box = (128, 22, 464, 196)
    film = Layer()
    film.rect(*box, 0, down(box, ((0, "#e58a5f", 1), (0.55, "#7a5a86", 1), (1, "#2c3d5c", 1))))
    film.paint(solid("#ffe2b0"), Mask().add(circle(360, 104, 26)).done())
    film.paint(
        solid("#46396a"),
        Mask()
        .add(((128, 140), (220, 98), (300, 134), (380, 92), (470, 128), (592, 86), (592, 218), (128, 218)))
        .done(),
    )
    film.paint(
        solid("#2a2748"),
        Mask()
        .add(((128, 176), (240, 132), (340, 164), (450, 126), (592, 160), (592, 218), (128, 218)))
        .done(),
    )
    # The controls' shade over the film's bottom and top.
    film.rect(*box, 0, down(box, ((0.5, "#000000", 0), (1, "#000000", 0.78))))
    film.rect(*box, 0, down(box, ((0, "#000000", 0.5), (0.2, "#000000", 0))))
    layer.over(film, clip=rect_mask(*box, 8))

    # The top band: a title, and full screen and close at the right.
    layer.rect(144, 34, 120, 8, 4, WHITE)
    layer.rect(144, 47, 70, 5, 2.5, MUTED)
    corners = Mask()
    for (cx, cy), (dx, dy) in (
        ((542, 35), (1, 1)), ((556, 35), (-1, 1)), ((542, 49), (1, -1)), ((556, 49), (-1, -1))
    ):
        corners.line(((cx, cy + 5 * dy), (cx, cy), (cx + 5 * dx, cy)), 2)
    layer.paint(solid(ON_ACCENT), corners.done())
    layer.paint(
        solid(ON_ACCENT),
        Mask()
        .line(((570, 36), (582, 48)), 2, round_ends=True)
        .line(((582, 36), (570, 48)), 2, round_ends=True)
        .done(),
    )

    # Skip intro: a pill over the film's lower right, a skip mark (two arrow heads and a bar).
    layer.rect(484, 134, 92, 28, 14, "#f2f2f2", opacity=0.95)
    layer.paint(
        solid(WINDOW),
        Mask().add(((498, 141), (507, 148), (498, 155)), ((507, 141), (516, 148), (507, 155))).done(),
    )
    layer.rect(517, 141, 3, 14, 1, WINDOW)
    layer.rect(530, 145, 34, 6, 3, "#5a5a5a")

    # The seek bar: played up to the knob, the rest dim.
    layer.rect(144, 178, 30, 5, 2.5, MUTED)
    layer.rect(546, 178, 30, 5, 2.5, MUTED)
    layer.rect(184, 179, 352, 3.5, 1.75, MUTED, opacity=0.4)
    layer.rect(184, 179, 128, 3.5, 1.75, STEEL)
    layer.paint(solid(WHITE), Mask().add(circle(312, 180.75, 6)).done())

    # The controls: jump back, pause, jump forward in the middle; sound at the left, subtitles
    # at the right.
    _jump(layer, 330, 202, clockwise=False)
    layer.rect(352, 194, 5, 16, 1.5, WHITE)
    layer.rect(363, 194, 5, 16, 1.5, WHITE)
    _jump(layer, 390, 202, clockwise=True)
    layer.paint(
        solid(ON_ACCENT),
        Mask().add(((146, 198), (151, 198), (158, 192), (158, 212), (151, 206), (146, 206))).done(),
    )
    layer.rect(166, 200, 34, 4, 2, ON_ACCENT, opacity=0.5)
    layer.rect(166, 200, 22, 4, 2, ON_ACCENT)
    layer.paint(solid(ON_ACCENT), border_mask(554, 194, 22, 16, 3, 1.6))
    layer.rect(558, 203, 6, 2.5, 1.25, ON_ACCENT)
    layer.rect(566, 203, 6, 2.5, 1.25, ON_ACCENT)
    return layer.image


def round_top_corners(image: Image.Image) -> Image.Image:
    """The top corners transparent: the card's own corners show there."""
    big = Image.new("L", (WIDTH * SUPERSAMPLE, HEIGHT * SUPERSAMPLE), 0)
    radius = CORNER_RADIUS * SUPERSAMPLE
    ImageDraw.Draw(big).rounded_rectangle(
        (0, 0, big.width - 1, big.height - 1),
        radius,
        fill=255,
        corners=(True, True, False, False),
    )
    rounded = image.copy()
    rounded.putalpha(ImageChops.multiply(image.getchannel("A"), big.reduce(SUPERSAMPLE)))
    return rounded


# File name in app/assets/whats_new -> its drawing.
PICTURES: dict[str, Callable[[], Image.Image]] = {
    "player.png": draw_player,
}


def write_all(folder: Path = OUTPUT_DIR) -> list[Path]:
    """Draw every picture into `folder`; the files written."""
    folder.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, draw in PICTURES.items():
        path = folder / name
        round_top_corners(draw()).save(path, "PNG", optimize=True)
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Draw the What's new window's pictures.")
    parser.add_argument("--out", type=Path, default=OUTPUT_DIR, help="the folder to write them to")
    args = parser.parse_args(argv)
    for path in write_all(args.out):
        print(f"{path.name}: {path.stat().st_size / 1024:.0f} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
