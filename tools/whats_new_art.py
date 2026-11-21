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


def _tick(layer: Layer, x: float, y: float) -> None:
    """A green have-it tick in a circle."""
    layer.paint(solid("#2FA572"), Mask().add(circle(x, y, 9)).done())
    layer.paint(
        solid(WHITE),
        Mask().line(((x - 4.5, y), (x - 1, y + 3.5), (x + 5, y - 3.5)), 2.2, round_ends=True).done(),
    )


def draw_folders() -> Image.Image:
    """Your own folder of films on its way into the main window's Library: an open amber folder
    with three posters sticking out, an arrow, and the window's shelf of posters, most of them
    ticked (you have them), one with a play button."""
    layer = _background()
    layer.paint(glow("#E0A23A", (0.12, 0.55), 0.32, 0.30))
    layer.paint(glow(STEEL, (0.68, 0.4), 0.55, 0.32))

    # The folder: its back with the tab, three posters in it, its front over them.
    layer.rect(34, 78, 40, 20, 6, "#a8701c")
    layer.rect(34, 86, 110, 78, 9, "#a8701c")
    for x, y, (colour, dark) in (
        (48, 62, ("#4ac0d8", "#13315a")),
        (74, 54, ("#e06a5a", "#5a1a20")),
        (100, 64, ("#7ad04f", "#1e5a2a")),
    ):
        poster: Box = (x, y, 30, 44)
        layer.rect(*poster, 4, down(poster, ((0, colour, 1), (1, dark, 1))))
    front = ((30, 110), (148, 110), (142, 168), (36, 168))
    layer.paint(down((30, 110, 118, 58), ((0, "#f0bc55", 1), (1, "#d08a2a", 1))), Mask().add(front).done())

    # The arrow.
    layer.paint(solid(ON_ACCENT), Mask().line(((158, 128), (186, 128)), 3.5, round_ends=True).done())
    layer.paint(
        solid(ON_ACCENT), Mask().line(((178, 118), (190, 128), (178, 138)), 3.5, round_ends=True).done()
    )

    # The main window and its switch, Library lit (a shelf of spines).
    layer.rect(204, 18, 494, 206, 14, WINDOW)
    layer.paint(solid("#5f7d9b"), border_mask(204, 18, 494, 206, 14, 1.5))
    layer.paint(solid(STEEL), Mask().add(circle(226, 40, 8)).done())
    layer.rect(242, 35, 58, 9, 4.5, ON_ACCENT)
    layer.rect(388, 25, 296, 31, 15.5, SURFACE)
    layer.paint(solid("#3a3a3a"), border_mask(388, 25, 296, 31, 15.5, 1))
    layer.rect(406, 37, 34, 7, 3.5, MUTED)
    layer.rect(458, 37, 34, 7, 3.5, MUTED)
    layer.rect(508, 28, 80, 25, 12.5, "#5f84a8")
    for index, height in enumerate((13, 11, 13, 9)):
        layer.rect(534 + index * 7, 47 - height, 5, height, 1, WHITE)
    layer.rect(532, 47, 34, 2, 1, WHITE)
    layer.rect(606, 37, 62, 7, 3.5, MUTED)

    # The posters: the folder's three among them, ticked; the third plays.
    colours = (
        ("#4ac0d8", "#13315a"), ("#e06a5a", "#5a1a20"), ("#b46ad8", "#3a1a5a"),
        ("#7ad04f", "#1e5a2a"), ("#e0b44a", "#5a3a12"), ("#6a8ad8", "#1a2a5a"),
    )
    for index, (colour, dark) in enumerate(colours):
        _poster(layer, 235 + index * 74, 72, colour, dark)
    for index in (0, 1, 3, 4):
        _tick(layer, 235 + index * 74 + 54, 82)
    play_x, play_y = 235 + 2 * 74 + 40, 72 + 36
    layer.paint(solid(WHITE, 0.92), Mask().add(circle(play_x, play_y, 15)).done())
    layer.paint(
        solid(WINDOW),
        Mask().add(((play_x - 4.5, play_y - 7.5), (play_x + 7.5, play_y), (play_x - 4.5, play_y + 7.5))).done(),
    )
    layer.rect(235, 196, 432, 14, 7, "#20262d")
    return layer.image


def draw_shelves() -> Image.Image:
    """Two shelves: Plex's amber chevron at the left and an amber folder at the right, each
    feeding its own half of the main window's Library: a two-part switch on top (Plex's part
    with a chevron, the folders' lit with a folder) and two rows of posters, kept apart by a
    gap, the folder row ticked."""
    layer = _background()
    layer.paint(glow("#E5A00D", (0.08, 0.5), 0.3, 0.26))
    layer.paint(glow("#E0A23A", (0.92, 0.5), 0.3, 0.26))
    layer.paint(glow(STEEL, (0.5, 0.42), 0.5, 0.3))

    # Plex's chevron at the left, the folder at the right.
    chevron = ((26, 92), (50, 92), (78, 120), (50, 148), (26, 148), (54, 120))
    layer.paint(solid("#E5A00D"), Mask().add(chevron).done())
    layer.rect(652, 98, 26, 14, 4, "#a8701c")
    layer.rect(652, 104, 46, 40, 6, "#a8701c")
    front = ((648, 116), (702, 116), (698, 148), (652, 148))
    layer.paint(down((648, 116, 54, 32), ((0, "#f0bc55", 1), (1, "#d08a2a", 1))), Mask().add(front).done())

    # The main window.
    layer.rect(104, 16, 512, 208, 14, WINDOW)
    layer.paint(solid("#5f7d9b"), border_mask(104, 16, 512, 208, 14, 1.5))
    layer.paint(solid(STEEL), Mask().add(circle(126, 38, 8)).done())
    layer.rect(142, 33, 58, 9, 4.5, ON_ACCENT)

    # The shelf switch: Plex | your folders (lit).
    layer.rect(122, 56, 156, 26, 13, SURFACE)
    layer.paint(solid("#3a3a3a"), border_mask(122, 56, 156, 26, 13, 1))
    small = ((150, 63), (156, 63), (162, 69), (156, 75), (150, 75), (156, 69))
    layer.paint(solid("#E5A00D"), Mask().add(small).done())
    layer.rect(168, 66, 30, 6, 3, MUTED)
    layer.rect(202, 58, 74, 22, 11, "#5f84a8")
    layer.rect(214, 63, 9, 5, 1.5, WHITE)
    layer.rect(214, 65, 15, 10, 2, WHITE)
    layer.rect(236, 66, 30, 6, 3, WHITE)

    # Plex's row (dim) and the folders' row (ticked), apart.
    plex_colours = (("#4ac0d8", "#13315a"), ("#b46ad8", "#3a1a5a"), ("#6a8ad8", "#1a2a5a"))
    folder_colours = (("#e06a5a", "#5a1a20"), ("#7ad04f", "#1e5a2a"), ("#e0b44a", "#5a3a12"))
    for index, (colour, dark) in enumerate(plex_colours):
        poster: Box = (124 + index * 50, 100, 42, 62)
        layer.rect(*poster, 5, down(poster, ((0, colour, 0.55), (1, dark, 0.55))))
    layer.rect(290, 104, 2, 108, 1, "#3a3a3a")
    for index, (colour, dark) in enumerate(folder_colours):
        x = 308 + index * 98
        poster = (x, 96, 62, 92)
        layer.rect(*poster, 6, down(poster, ((0, colour, 1), (1, dark, 1))))
        layer.paint(solid("#2FA572"), Mask().add(circle(x + 56, 102, 9)).done())
        layer.paint(
            solid(WHITE),
            Mask().line(((x + 51.5, 102), (x + 55, 105.5), (x + 61, 98.5)), 2.2, round_ends=True).done(),
        )
    layer.rect(124, 174, 142, 6, 3, MUTED)
    layer.rect(308, 198, 290, 8, 4, MUTED)
    return layer.image


def _amber_folder(layer: Layer, x: float, y: float, width: float, height: float) -> None:
    """An amber folder: its back with the tab, its front a little lower."""
    layer.rect(x, y, width * 0.4, height * 0.3, 4, "#a8701c")
    layer.rect(x, y + height * 0.14, width, height * 0.86, 6, "#a8701c")
    top = y + height * 0.36
    front = ((x - 3, top), (x + width + 3, top), (x + width - 2, y + height), (x + 2, y + height))
    paint = down((x - 3, top, width + 6, y + height - top), ((0, "#f0bc55", 1), (1, "#d08a2a", 1)))
    layer.paint(paint, Mask().add(front).done())


def draw_rules() -> Image.Image:
    """Rules for watched folders: an amber download folder at the left, fanning out to the
    rows of a rules list (a grip, a folder, two lines, a mode pill and a switch: two on, one
    off), and the two on rows leading on to their own folders at the right, one of films
    (red posters) and one of series (blue ones)."""
    layer = _background()
    layer.paint(glow("#E0A23A", (0.09, 0.55), 0.3, 0.28))
    layer.paint(glow(STEEL, (0.5, 0.45), 0.5, 0.3))
    layer.paint(glow("#E0A23A", (0.9, 0.5), 0.3, 0.22))

    rows = (52, 104, 156)
    # From the download folder to each rule, and on from the two rules that are on.
    for y in rows:
        layer.paint(
            solid(ON_ACCENT, 0.55),
            Mask().line(((122, 126), (146, 126), (162, y + 18), (176, y + 18)), 2.5, round_ends=True).done(),
        )
    for y, to in ((rows[0], 84), (rows[1], 166)):
        layer.paint(
            solid(ON_ACCENT, 0.85),
            Mask().line(((522, y + 18), (548, y + 18), (562, to), (576, to)), 2.5, round_ends=True).done(),
        )
    _amber_folder(layer, 28, 92, 92, 66)

    # The rules list.
    layer.rect(168, 26, 362, 196, 14, WINDOW)
    layer.paint(solid("#5f7d9b"), border_mask(168, 26, 362, 196, 14, 1.5))
    layer.rect(186, 36, 70, 8, 4, ON_ACCENT)
    for index, y in enumerate(rows):
        on = index < 2
        layer.rect(180, y, 338, 40, 8, "#20262d" if on else "#1b1f24")
        for dot_y in (y + 13, y + 20, y + 27):
            for dot_x in (190, 196):
                layer.paint(solid(MUTED, 0.8 if on else 0.4), Mask().add(circle(dot_x, dot_y, 1.6)).done())
        if on:
            _amber_folder(layer, 208, y + 9, 26, 22)
        else:
            layer.rect(208, y + 12, 26, 19, 4, "#4a4f55")
        layer.rect(244, y + 11, (120, 96, 108)[index], 7, 3.5, WHITE if on else MUTED)
        layer.rect(244, y + 24, (84, 112, 70)[index], 5, 2.5, MUTED)
        layer.rect(398, y + 12, 50, 16, 8, "#5f84a8" if on else "#3a3f45")
        knob_x = 498 if on else 480
        layer.rect(470, y + 11, 38, 18, 9, "#2FA572" if on else "#4a4f55")
        layer.paint(solid(WHITE if on else "#9aa3ad"), Mask().add(circle(knob_x, y + 20, 6.5)).done())

    # Where the two on rules lead: films, then series.
    for y, colours in (
        (52, (("#e06a5a", "#5a1a20"), ("#e0b44a", "#5a3a12"))),
        (134, (("#4ac0d8", "#13315a"), ("#6a8ad8", "#1a2a5a"))),
    ):
        _amber_folder(layer, 586, y + 4, 104, 60)
        for index, (colour, dark) in enumerate(colours):
            poster: Box = (604 + index * 34, y - 10, 28, 40)
            layer.rect(*poster, 4, down(poster, ((0, colour, 1), (1, dark, 1))))
        top = y + 4 + 60 * 0.36
        front = ((583, top), (693, top), (688, y + 64), (588, y + 64))
        paint = down((583, top, 110, y + 64 - top), ((0, "#f0bc55", 1), (1, "#d08a2a", 1)))
        layer.paint(paint, Mask().add(front).done())
    return layer.image


def draw_settings() -> Image.Image:
    """The new Settings window: the sliders icon at the left, then the window with its sidebar
    in groups (the first row lit) and the overview's cards, each with a dot (green, amber or
    grey) and two lines, and the "saved, undo" pill at the bottom."""
    layer = _background()
    layer.paint(glow(STEEL, (0.1, 0.5), 0.28, 0.3))
    layer.paint(glow(STEEL, (0.6, 0.42), 0.5, 0.3))
    layer.paint(glow("#2FA572", (0.72, 0.85), 0.25, 0.18))

    # The sliders: three rails, each with its knob somewhere else.
    for y, knob in ((88, 58), (120, 92), (152, 68)):
        layer.paint(solid(ON_ACCENT, 0.8), Mask().line(((34, y), (110, y)), 4, round_ends=True).done())
        layer.paint(solid(STEEL), Mask().add(circle(knob, y, 10)).done())
        layer.paint(solid(WINDOW), Mask().add(circle(knob, y, 5.5)).done())

    # The window.
    layer.rect(142, 16, 552, 208, 14, WINDOW)
    layer.paint(solid("#5f7d9b"), border_mask(142, 16, 552, 208, 14, 1.5))

    # The sidebar: the first row lit, then three groups (a heading and its rows).
    layer.rect(154, 28, 112, 20, 10, "#5f84a8")
    layer.paint(solid(WHITE), Mask().add(circle(167, 38, 4)).done())
    layer.rect(177, 35, 56, 6, 3, WHITE)
    for top, widths in ((58, (62, 48, 70)), (122, (54, 66, 44)), (186, (58, 40))):
        layer.rect(158, top, 36, 4, 2, MUTED)
        for index, width in enumerate(widths):
            y = top + 11 + index * 16
            layer.paint(solid(MUTED, 0.85), Mask().add(circle(167, y + 3, 3.5)).done())
            layer.rect(177, y, width, 6, 3, ON_ACCENT)
    layer.rect(278, 28, 1.5, 184, 0.75, "#2a2f35")

    # The page: a heading, its note, and the cards.
    layer.rect(294, 30, 96, 9, 4.5, ON_ACCENT)
    layer.rect(294, 46, 168, 5, 2.5, MUTED)
    dots = ("#2FA572", "#2FA572", "#E0A23A", "#2FA572", "#6a737d", "#2FA572")
    for index, dot in enumerate(dots):
        x = 294 + (index % 3) * 130
        y = 62 + (index // 3) * 62
        layer.rect(x, y, 120, 52, 9, "#20262d")
        layer.paint(solid("#3a3f45"), border_mask(x, y, 120, 52, 9, 1))
        layer.paint(solid(dot), Mask().add(circle(x + 15, y + 16, 5)).done())
        layer.rect(x + 27, y + 13, (58, 70, 50, 64, 46, 60)[index], 7, 3.5, WHITE)
        layer.rect(x + 12, y + 30, 92, 5, 2.5, MUTED)
        layer.rect(x + 12, y + 39, (64, 80, 72, 56, 76, 68)[index], 5, 2.5, MUTED)

    # Saved, with Undo beside it.
    layer.rect(452, 190, 176, 24, 12, "#26303b")
    layer.paint(solid("#5f7d9b"), border_mask(452, 190, 176, 24, 12, 1))
    layer.paint(solid("#2FA572"), Mask().add(circle(467, 202, 7)).done())
    layer.paint(
        solid(WHITE),
        Mask().line(((463.5, 202), (466.2, 204.8), (470.8, 199.2)), 1.8, round_ends=True).done(),
    )
    layer.rect(481, 199, 64, 6, 3, WHITE)
    layer.paint(solid(MUTED), Mask().add(circle(556, 202, 1.8)).done())
    layer.rect(566, 199, 46, 6, 3, STEEL)
    return layer.image


_POSTERS = (
    ("#e06a5a", "#5a1a20"),
    ("#4ac0d8", "#13315a"),
    ("#e0b44a", "#5a3a12"),
    ("#b07ad8", "#3a1a5a"),
    ("#5ad08a", "#13402a"),
    ("#6a8ad8", "#1a2a5a"),
)


def _tinted_poster(layer: Layer, box: Box, index: int, radius: float = 4) -> None:
    colour, dark = _POSTERS[index % len(_POSTERS)]
    layer.rect(*box, radius, down(box, ((0, colour, 1), (1, dark, 1))))


def draw_phone() -> Image.Image:
    """The phone connection: AirGrabber's window at the left with a grid of posters, a dotted
    link with a signal's arcs over its middle, and a phone at the right showing the same
    library: a big poster with a play button, then a row of small posters and two lines."""
    layer = _background()
    layer.paint(glow(STEEL, (0.3, 0.5), 0.42, 0.32))
    layer.paint(glow("#2FA572", (0.79, 0.5), 0.24, 0.22))

    # The window: a top band and two rows of posters.
    layer.rect(60, 34, 300, 172, 14, WINDOW)
    layer.paint(solid("#5f7d9b"), border_mask(60, 34, 300, 172, 14, 1.5))
    layer.rect(76, 48, 64, 8, 4, ON_ACCENT)
    layer.rect(150, 49, 40, 6, 3, MUTED)
    for dot_x in (330, 342):
        layer.paint(solid(MUTED, 0.8), Mask().add(circle(dot_x, 52, 3)).done())
    for row in range(2):
        for column in range(5):
            _tinted_poster(layer, (76 + column * 55, 68 + row * 66, 46, 58), row * 5 + column)

    # The link: dots from the window to the phone, and a signal's arcs over the middle.
    for step in range(7):
        x = 376 + step * 14
        layer.paint(solid(ON_ACCENT, 0.4 + 0.08 * step), Mask().add(circle(x, 120, 2.6)).done())
    arcs = Mask()
    for radius in (10, 18, 26):
        points = [
            (418 + radius * math.cos(math.radians(a)), 104 + radius * math.sin(math.radians(a)))
            for a in range(-135, -44, 5)
        ]
        arcs.line(points, 3, round_ends=True)
    arcs.add(circle(418, 104, 3.5))
    layer.paint(solid(STEEL), arcs.done())

    # The phone: its body, the screen, a big poster with play, small posters, two lines.
    layer.rect(484, 14, 112, 212, 20, "#0f1216")
    layer.paint(solid("#5f7d9b"), border_mask(484, 14, 112, 212, 20, 1.5))
    screen: Box = (492, 22, 96, 196)
    layer.rect(*screen, 14, WINDOW)
    layer.rect(526, 28, 28, 5, 2.5, "#2a2f35")
    big: Box = (500, 40, 80, 92)
    hero = Layer()
    hero.rect(*big, 0, down(big, ((0, "#e58a5f", 1), (0.6, "#7a5a86", 1), (1, "#2c3d5c", 1))))
    hero.paint(
        solid("#2a2748"),
        Mask().add(((500, 112), (530, 96), (556, 110), (580, 98), (580, 132), (500, 132))).done(),
    )
    layer.over(hero, clip=rect_mask(*big, 8))
    layer.paint(solid(WHITE, 0.95), Mask().add(circle(540, 84, 13)).done())
    layer.paint(solid(WINDOW), Mask().add(((535, 77), (547, 84), (535, 91))).done())
    layer.rect(500, 140, 56, 7, 3.5, WHITE)
    layer.rect(500, 152, 40, 5, 2.5, MUTED)
    for column in range(3):
        _tinted_poster(layer, (500 + column * 28, 166, 24, 34), column + 1, 3)
    layer.rect(524, 209, 32, 3, 1.5, "#5f6a75")
    return layer.image


def draw_floating_bar() -> Image.Image:
    """The player's new look: a film filling the window edge to edge (a sunset over hills), the
    title in a small pill at the top left, a round ✕ at the top right, and one rounded control
    bar floating over the film's bottom: pause and the jumps, the seek line, then the episode
    list, the sleep timer (a moon) and the settings (a gear)."""
    layer = _background()
    layer.paint(glow(STEEL, (0.5, 0.45), 0.55, 0.36))
    layer.paint(glow("#e58a5f", (0.5, 0.35), 0.3, 0.18))

    # The window: the film fills it, no bands.
    box: Box = (96, 14, 528, 212)
    film = Layer()
    film.rect(*box, 0, down(box, ((0, "#e58a5f", 1), (0.55, "#7a5a86", 1), (1, "#2c3d5c", 1))))
    film.paint(solid("#ffe2b0"), Mask().add(circle(360, 92, 28)).done())
    film.paint(
        solid("#46396a"),
        Mask()
        .add(((96, 132), (200, 88), (290, 126), (380, 80), (480, 120), (624, 76), (624, 226), (96, 226)))
        .done(),
    )
    film.paint(
        solid("#2a2748"),
        Mask()
        .add(((96, 170), (230, 124), (340, 158), (460, 118), (624, 152), (624, 226), (96, 226)))
        .done(),
    )
    layer.over(film, clip=rect_mask(*box, 14))
    layer.paint(solid("#5f7d9b"), border_mask(*box, 14, 1.5))

    # The title pill and the round close button.
    layer.rect(110, 26, 132, 26, 13, "#101418", opacity=0.72)
    layer.rect(122, 33, 70, 6, 3, WHITE)
    layer.rect(122, 42, 44, 4, 2, MUTED)
    layer.paint(solid("#101418", 0.72), Mask().add(circle(597, 39, 13)).done())
    layer.paint(
        solid(ON_ACCENT),
        Mask()
        .line(((592, 34), (602, 44)), 2, round_ends=True)
        .line(((602, 34), (592, 44)), 2, round_ends=True)
        .done(),
    )

    # The floating bar.
    bar: Box = (136, 170, 448, 42)
    layer.rect(*bar, 21, "#101418", opacity=0.8)
    layer.paint(solid("#ffffff", 0.12), border_mask(*bar, 21, 1))
    _jump(layer, 164, 193, clockwise=False)
    layer.rect(182, 184, 5, 17, 1.5, WHITE)
    layer.rect(192, 184, 5, 17, 1.5, WHITE)
    _jump(layer, 218, 193, clockwise=True)
    layer.rect(240, 190, 24, 4, 2, MUTED)
    layer.rect(272, 190.5, 196, 3.5, 1.75, MUTED, opacity=0.4)
    layer.rect(272, 190.5, 84, 3.5, 1.75, STEEL)
    layer.paint(solid(WHITE), Mask().add(circle(356, 192.25, 5.5)).done())
    layer.rect(476, 190, 24, 4, 2, MUTED)
    # The episode list: three lines.
    for row in range(3):
        layer.rect(514, 185 + row * 6, 14, 2.5, 1.25, ON_ACCENT)
    # The sleep timer: a crescent moon.
    moon = Mask().add(circle(545, 191, 7.5)).done()
    bite = Mask().add(circle(549, 187.5, 6.5)).done()
    layer.paint(solid(ON_ACCENT), ImageChops.subtract(moon, bite))
    # The settings: a gear (a ring with teeth).
    gear = Mask()
    for tooth in range(8):
        angle = math.radians(tooth * 45)
        gear.line(
            ((571 + 5 * math.cos(angle), 191 + 5 * math.sin(angle)), (571 + 8.5 * math.cos(angle), 191 + 8.5 * math.sin(angle))),
            3,
        )
    gear.add(circle(571, 191, 6))
    hole = Mask().add(circle(571, 191, 2.6)).done()
    layer.paint(solid(ON_ACCENT), ImageChops.subtract(gear.done(), hole))
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
    "player.png": draw_floating_bar,
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
