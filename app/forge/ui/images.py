"""Posters and backdrops for the queue's cards, through AirGrabber's image cache.

The pictures come from `app.services.images.ImageCache` (its memory LRU, `posters` disk cache and
de-duplicated downloads): the poster as the cache's cover crop of the band's frame.

A queue card's picture (`card`, `CardBake`, a `Finish` step run in a worker thread) is one image
behind a band and its lines: the backdrop composited, fully opaque, over the card's fill, strongest
behind the band, dimmed behind the lines and none at the card's left. `card_strength` and
`baked_pixel` say what any pixel is, so the readability tests check the constants, not a picture.
Its width is fixed (a resize never bakes it again) and its height goes by the card's lines up to a
cap (`CardShape`), so a queue doesn't bake a size per card. A card's picture is big: it isn't kept
in the image cache's memory, only here, under a budget of pixels (CARD_PIXELS).

`BandImages` turns them into Tk images at physical size (sizes are asked in CTk units and scaled by
`ctk.ScalingTracker.get_widget_scaling`) and keeps them in a small LRU, on the loop thread: Tk
images must never be made or freed elsewhere. A picture that can't be had (offline, none at
TMDB) is simply None: the band draws without, and doesn't ask for that one again.
"""

from __future__ import annotations

import logging
import tkinter as tk
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

import customtkinter as ctk
from PIL import Image, ImageChops, ImageDraw, ImageOps, ImageTk

from app.models.domain import TMDB_IMAGE_BASE
from app.ui import theme

if TYPE_CHECKING:
    from app.services.images import Finish, ImageCache

log = logging.getLogger(__name__)

POSTER_SIZE = "w185"
BACKDROP_SIZE = "w780"
FADED_STRENGTH = 0.45  # a done band's poster: this much of the band's colour over the picture

# A queue card's picture, in CTk units unless said otherwise.
CARD_WIDTH = 900  # the picture's width, fixed: a resize never bakes it again
CARD_MAX_LINES = 6  # its height: the band and up to this many lines; a longer card goes on plain
CARD_TYPICAL = 1150  # a card in the window's default size: where the fractions below are measured
CARD_FADE_IN = (0.40, 0.75)  # left to right: nothing until this much of a typical card, full here
CARD_TEXT_LIMIT = 0.55  # a card's title and subtitle stop here (of the card, on a picture)
# From the band's strength to the lines', from here to here (from the band's bottom): the first
# line's old name starts 12 below it, and the band's last 30 have no text on the picture (its
# buttons and status have their own fills, its title and subtitle stop at the limit).
CARD_DROP = (-30, 10)
CARD_TALL_FADE = 80  # a card taller than its picture: gone over this much, by the picture's end
# (band, lines) strength of the picture over the fill, in the light and the dark look: the
# readability tests in test_images.py hold these against the worst pictures.
CARD_STRENGTHS = ((0.45, 0.12), (0.55, 0.18))
CARD_DONE = 0.5  # a done card's picture: this much of the strength, over its faded fill
CARD_PIXELS = 12_000_000  # all the photos of a BandImages together (physical pixels)
# A hovered and a selected line on a card: this much of the text colour over what lies under it
# (light, dark). Clearly visible on every ground, and old names stay readable through it.
HIGHLIGHT_ALPHA = ((0.09, 0.08), (0.13, 0.10))
# (kind, TMDB path, physical width, physical height, fade colour or "")
type Key = tuple[str, str, int, int, str]


def tmdb_image_url(path: str, size: str) -> str:
    """TMDB's URL for the image at `path` ("/abc.jpg") in `size` ("w780")."""
    return f"{TMDB_IMAGE_BASE}{size}/{path.lstrip('/')}"


@dataclass(frozen=True, slots=True)
class Fade:
    """`ImageCache`'s finish step for a done band's poster: the picture faded towards `fade_to`
    (the faded band's colour, "#RRGGBB") by FADED_STRENGTH."""

    fade_to: str

    def __call__(self, image: Image.Image, /) -> Image.Image:
        picture = image.convert("RGB")
        solid = Image.new("RGB", picture.size, self.fade_to)
        return Image.blend(picture, solid, FADED_STRENGTH)


def cover(image: Image.Image, width: int, height: int) -> Image.Image:
    """Scale to fill width × height and crop the middle (CSS's `object-fit: cover`), as the
    image cache does."""
    return ImageOps.fit(image.convert("RGB"), (width, height), Image.Resampling.LANCZOS)


def render_poster(image: Image.Image, width: int, height: int) -> Image.Image:
    """What a band's poster looks like (the pure part, for tests and previews)."""
    return cover(image, width, height)


@dataclass(frozen=True, slots=True)
class CardShape:
    """A card's picture in physical pixels: `width` × `height`, the top `band` of it behind the
    band; `unit` = pixels per CTk unit. `tall`: the card goes on below the picture, so the picture
    fades out before its end."""

    width: int
    height: int
    band: int
    unit: float
    tall: bool = False


def card_strengths(*, dark: bool, done: bool) -> tuple[float, float]:
    """The picture's (band, lines) strength in a look."""
    band, lines = CARD_STRENGTHS[1 if dark else 0]
    return (band * CARD_DONE, lines * CARD_DONE) if done else (band, lines)


def _ramp(value: float, start: float, end: float) -> float:
    """0 up to `start`, 1 from `end` on, in a straight line between."""
    return max(0.0, min(1.0, (value - start) / (end - start)))


def card_across(from_right: float) -> float:
    """How much of the picture shows `from_right` units from the card's right end: left to right,
    0 → 1 over CARD_FADE_IN of a typical card."""
    start, full = ((1 - fraction) * CARD_TYPICAL for fraction in CARD_FADE_IN)
    return _ramp(start - from_right, 0.0, start - full)


def card_down(shape: CardShape, strengths: tuple[float, float], y: float) -> float:
    """The picture's strength at pixel row `y`, before `card_across`: the band's, dropping to the
    lines' around the band's bottom; a tall card's fades out to nothing by the picture's end."""
    band, lines = strengths
    strength = band + (lines - band) * _ramp((y - shape.band) / shape.unit, *CARD_DROP)
    if shape.tall:
        end = shape.height / shape.unit
        strength *= 1.0 - _ramp(y / shape.unit, end - CARD_TALL_FADE, end)
    return strength


def card_strength(shape: CardShape, strengths: tuple[float, float], x: float, y: float) -> float:
    """How much of the picture is in the baked pixel at (x, y) of a card's picture."""
    return card_across((shape.width - x) / shape.unit) * card_down(shape, strengths, y)


def card_text_end(left: int, right: int, unit: float) -> int:
    """Where a title and subtitle on a picture stop, for a card from `left` to `right` (pixels):
    at CARD_TEXT_LIMIT of the card, and on a card narrower than a typical one (the picture is
    anchored at its right end) no further into the picture than on a typical one."""
    return min(
        left + round((right - left) * CARD_TEXT_LIMIT),
        right - round((1 - CARD_TEXT_LIMIT) * CARD_TYPICAL * unit),
    )


def baked_pixel(
    picture: tuple[int, int, int], fill: tuple[int, int, int], strength: float
) -> tuple[int, int, int]:
    """What a baked pixel is: `strength` of the picture over the card's fill, fully opaque."""
    red, green, blue = (
        round(p * strength + f * (1 - strength)) for p, f in zip(picture, fill, strict=True)
    )
    return red, green, blue


@dataclass(frozen=True, slots=True)
class CardBake:
    """`ImageCache`'s finish step for a card's picture: the cover-fitted backdrop composited over
    `fill` ("#RRGGBB"), by `card_strength`."""

    fill: str
    shape: CardShape
    strengths: tuple[float, float]

    def __call__(self, image: Image.Image, /) -> Image.Image:
        picture = image.convert("RGB")
        shape = self.shape
        width, height = picture.size
        across = Image.new("L", (width, 1))
        across.putdata([round(255 * card_across((width - x) / shape.unit)) for x in range(width)])
        down = Image.new("L", (1, height))
        down.putdata([round(255 * card_down(shape, self.strengths, y)) for y in range(height)])
        mask = ImageChops.multiply(across.resize((width, height)), down.resize((width, height)))
        return Image.composite(picture, Image.new("RGB", (width, height), self.fill), mask)


def render_card(
    image: Image.Image, shape: CardShape, fill: str, *, dark: bool, done: bool = False
) -> Image.Image:
    """What a card's picture looks like (the pure part, for tests and previews)."""
    strengths = card_strengths(dark=dark, done=done)
    return CardBake(fill, shape, strengths)(cover(image, shape.width, shape.height))


def line_highlight(
    width: int, height: int, radius: int, colour: str, alpha: float
) -> Image.Image:
    """A card line's see-through highlight: `alpha` of `colour`, as an RGBA strip with rounded
    corners (Tk's canvas blends a photo's alpha over what lies under it; its rectangles can't be
    see-through). Small enough to make on the loop."""
    level = round(255 * alpha)
    mask = Image.new("L", (width, height), level)
    radius = min(radius, width // 2, height // 2)
    if radius > 0:
        side = 2 * radius
        large = Image.new("L", (side * 4, side * 4), 0)
        ImageDraw.Draw(large).ellipse((0, 0, side * 4 - 1, side * 4 - 1), fill=level)
        round_tile = large.resize((side, side), Image.Resampling.BOX)
        for x, y, left, top in (
            (0, 0, 0, 0),
            (width - radius, 0, radius, 0),
            (0, height - radius, 0, radius),
            (width - radius, height - radius, radius, radius),
        ):
            mask.paste(round_tile.crop((left, top, left + radius, top + radius)), (x, y))
    strip = Image.new("RGBA", (width, height), colour)
    strip.putalpha(mask)
    return strip


class BandImages:
    """The bands' Tk images over `ImageCache`; use from the loop thread only.

    `widget` is the one the pictures are shown in: its scaling turns CTk units into physical
    pixels. Kept: the last `limit` photos, as long as they stay under `pixels` physical pixels
    together (cards are big, posters small). The default budget holds about 16 full-size cards at
    150 %, more than any screen shows at once: photos on screen are the most recently asked, so
    never the ones to go.
    """

    def __init__(
        self,
        cache: ImageCache,
        widget: tk.Misc,
        *,
        limit: int = 200,
        pixels: int = CARD_PIXELS,
    ) -> None:
        self._cache = cache
        self._widget = widget
        self._limit = limit
        self._max_pixels = pixels
        self._pixels = 0  # of the photos held now
        self._photos: OrderedDict[Key, ImageTk.PhotoImage] = OrderedDict()
        self._missing: set[Key] = set()

    def poster_key(
        self, path: str | None, width: int, height: int, *, faded: str | None = None
    ) -> Key:
        """`faded`: the faded band's colour a done band's poster fades towards."""
        w, h = self._physical(width, height)
        return ("poster", path or "", w, h, faded or "")

    def cached_poster(
        self, path: str | None, width: int, height: int, *, faded: str | None = None
    ) -> ImageTk.PhotoImage | None:
        return self._get(self.poster_key(path, width, height, faded=faded))

    def card_key(self, path: str | None, shape: CardShape, fill: str, *, done: bool = False) -> Key:
        """A card's picture: its look (the fill), its size, and what its strengths and fades are."""
        kind = f"card{'-done' if done else ''}{'-tall' if shape.tall else ''}"
        return (kind, path or "", shape.width, shape.height, fill)

    def cached_card(
        self, path: str | None, shape: CardShape, fill: str, *, done: bool = False
    ) -> ImageTk.PhotoImage | None:
        return self._get(self.card_key(path, shape, fill, done=done))

    def known_missing(self, key: Key) -> bool:
        return key in self._missing

    async def poster(
        self, path: str | None, width: int, height: int, *, faded: str | None = None
    ) -> ImageTk.PhotoImage | None:
        """The poster at `path`, `width` × `height` CTk units (faded towards `faded`), or None."""
        return await self._load(self.poster_key(path, width, height, faded=faded), POSTER_SIZE)

    async def card(
        self, path: str | None, shape: CardShape, fill: str, *, done: bool = False
    ) -> ImageTk.PhotoImage | None:
        """A card's picture at `path`, baked over `fill` in the current look in a worker thread,
        or None."""
        strengths = card_strengths(dark=theme.is_dark(), done=done)
        finish = CardBake(fill, shape, strengths)
        key = self.card_key(path, shape, fill, done=done)
        return await self._load(key, BACKDROP_SIZE, finish=finish, keep=False)

    def clear(self) -> None:
        """Let every Tk image go (on the loop thread), e.g. when the view closes."""
        self._photos.clear()
        self._pixels = 0
        self._missing.clear()

    def _physical(self, width: int, height: int) -> tuple[int, int]:
        scale = ctk.ScalingTracker.get_widget_scaling(self._widget)
        return max(1, round(width * scale)), max(1, round(height * scale))

    def _get(self, key: Key) -> ImageTk.PhotoImage | None:
        photo = self._photos.get(key)
        if photo is not None:
            self._photos.move_to_end(key)
        return photo

    async def _load(
        self, key: Key, size: str, *, finish: Finish | None = None, keep: bool = True
    ) -> ImageTk.PhotoImage | None:
        """`keep`: the image cache keeps the finished picture in its memory too (not a card's:
        at 2 MB each, its 300 places would hold far more than all of AirGrabber's posters)."""
        cached = self._get(key)
        if cached is not None or not key[1] or key in self._missing:
            return cached
        kind, path, width, height, fade_to = key
        if finish is None and fade_to:
            finish = Fade(fade_to)
        image = await self._cache.get(
            tmdb_image_url(path, size), (width, height), finish=finish, keep=keep
        )
        if image is None:
            log.debug("No %s for a band", kind)
            self._missing.add(key)
            return None
        if not self._widget.winfo_exists():
            return None
        existing = self._get(key)  # a concurrent caller for the same picture finished first
        if existing is not None:
            return existing
        photo = ImageTk.PhotoImage(image, master=self._widget)
        self._photos[key] = photo
        self._pixels += width * height
        while len(self._photos) > 1 and (
            len(self._photos) > self._limit or self._pixels > self._max_pixels
        ):
            (_kind, _path, gone_w, gone_h, _fade), _photo = self._photos.popitem(last=False)
            self._pixels -= gone_w * gone_h
        return photo
