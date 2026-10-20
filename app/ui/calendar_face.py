"""The calendar's full episode card (`EpisodeCard` in app/ui/calendar_cards.py): its sizes and
its baked images.

The card is wide and short: the show's poster fills its left half (`poster_area`: half the
card's width, at most as wide as the card is tall), cropped to the card's height near the
poster's top, and fades to the right into the card's background. The show's name, the
episode code and "Not aired" are canvas text over the faded part (Tk draws canvas text
without a box behind it, so the poster shows through). The fade reaches FADE_MAX where the
text starts, then turns solid at the poster's right edge, so the poster has no hard edge.
Under the Download row, a widget that can't be see-through, the poster fades out completely.

Two images are baked in a worker thread at physical pixels (DPI factor x card size): the
poster piece (`PosterFace`: the border and rounded left corners around the faded cover; also
the `ImageCache` finish step) and the right cap (`CapFace`: the border and rounded right
corners). The card's canvas fills the stretch between them with the background and the top
and bottom border lines. The poster area's width snaps to POSTER_STEP, so a column changing
width needs a new poster piece only now and then.

A card is drawn at a `CardScale` (Settings → Card size): every size below is the XL card's in
CTk units and scales with it; the fonts never go below their readable minimums.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Hashable
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from PIL import Image, ImageChops, ImageDraw, ImageOps

from app.ui.card_scale import FULL, SMALL_TEXT_MIN, TITLE_MIN, CardScale
from app.ui.download_control import ROW_HEIGHT
from app.ui.poster_card import FADE_MAX  # the poster cards' fade cap: where the text starts
from app.ui.tasks import spawn

RGB = tuple[int, int, int]

# At XL, in CTk units.
CARD_HEIGHT = 112  # at least; taller when the fonts need it
POSTER_MIN = 48  # the poster area's least width (narrow windows)
POSTER_STEP = 4  # the poster area's width is a multiple of this
PAD_RIGHT = 6
PAD_TOP = 9
PAD_BOTTOM = 9
CODE_GAP = 2  # between the name and the episode code
ROW_GAP = 6  # at least, between the code and the Download row
BORDER = 1
CORNER_RADIUS = 8
SOLID_MARGIN = 2  # around the Download row, the poster has faded out completely
ROW_FADE = 16  # over this distance next to the Download row the poster fades out
TITLE_SIZE = 14  # bold
TITLE_MAX_LINES = 2
CODE_SIZE = 12  # the episode code and "Not aired"
# The Download row is drawn at this share of the card's scale (36 units tall at XL is too big
# for a short card); never smaller than ROW_MIN_FACTOR, so its buttons stay easy to click.
ROW_FACTOR = 0.75
ROW_MIN_FACTOR = 2 / 3

# The poster area: this share of the card's width, at most POSTER_MAX_RATIO x its height (a
# wider area would crop the poster to a thin strip). The fade starts at FADE_SHARE of the
# area's width; the text and the Download row start at TEXT_SHARE of it.
POSTER_SHARE = 0.5
POSTER_MAX_RATIO = 1.0
FADE_SHARE = 0.5
TEXT_SHARE = 0.75
COVER_TOP = 0.2  # the crop's vertical centring: near the poster's top, where faces and titles are
POSTER_ASPECT = 1.5  # height / width of a poster

_SUPERSAMPLE = 4  # the rounded corners are drawn this much larger, then scaled down


# -- sizes -----------------------------------------------------------------------------------


def row_scale(scale: CardScale = FULL) -> CardScale:
    """The scale the card's Download row is drawn at."""
    return CardScale(max(ROW_MIN_FACTOR, scale.factor * ROW_FACTOR))


def title_size(scale: CardScale = FULL) -> int:
    return scale.font(TITLE_SIZE, TITLE_MIN)


def code_size(scale: CardScale = FULL) -> int:
    return scale.font(CODE_SIZE, SMALL_TEXT_MIN)


@dataclass(frozen=True, slots=True)
class EpisodeLayout:
    """The card's sizes in CTk units at one card size."""

    height: int
    poster_min: int
    poster_max: int
    pad_right: int
    pad_top: int
    pad_bottom: int
    code_gap: int
    row_height: int
    row_scale: CardScale
    border: int
    radius: int
    title_size: int
    code_size: int


@dataclass(frozen=True, slots=True)
class PosterArea:
    """Where the poster goes on a card of one width, in CTk units from its left edge."""

    width: int  # the poster area
    fade_from: int  # where the poster starts to fade
    text_x: int  # where the text and the Download row start, over the faded poster


def poster_area(layout: EpisodeLayout, card_width: float) -> PosterArea:
    """The poster area of a card `card_width` units wide: POSTER_SHARE of it, snapped to
    POSTER_STEP, between the layout's least and greatest width."""
    snapped = POSTER_STEP * round(card_width * POSTER_SHARE / POSTER_STEP)
    width = max(layout.poster_min, min(layout.poster_max, snapped))
    return PosterArea(
        width=width, fade_from=round(width * FADE_SHARE), text_x=round(width * TEXT_SHARE)
    )


def text_width(layout: EpisodeLayout, area: PosterArea, card_width: float) -> float:
    """The room the name, the code and the Download row have on a card `card_width` wide."""
    return card_width - area.text_x - layout.pad_right


def episode_layout(
    scale: CardScale = FULL, *, linespace: Callable[[int, bool], int]
) -> EpisodeLayout:
    """The card's sizes at `scale`. `linespace(size, bold)` is a font's line height: the card
    is tall enough for TITLE_MAX_LINES lines of name, the code and the Download row."""
    rows = row_scale(scale)
    titles, codes = title_size(scale), code_size(scale)
    pad_top, pad_bottom = scale.px(PAD_TOP), scale.px(PAD_BOTTOM)
    code_gap, row_height = scale.px(CODE_GAP), rows.px(ROW_HEIGHT)
    needed = (
        pad_top
        + TITLE_MAX_LINES * linespace(titles, True)
        + code_gap
        + linespace(codes, False)
        + scale.px(ROW_GAP)
        + row_height
        + pad_bottom
    )
    height = max(scale.px(CARD_HEIGHT), needed)
    return EpisodeLayout(
        height=height,
        poster_min=POSTER_STEP * -(-scale.px(POSTER_MIN) // POSTER_STEP),  # rounded up
        poster_max=POSTER_STEP * int(height * POSTER_MAX_RATIO // POSTER_STEP),  # rounded down
        pad_right=scale.px(PAD_RIGHT),
        pad_top=pad_top,
        pad_bottom=pad_bottom,
        code_gap=code_gap,
        row_height=row_height,
        row_scale=rows,
        border=scale.px(BORDER, minimum=1),
        radius=scale.px(CORNER_RADIUS),
        title_size=titles,
        code_size=codes,
    )


def cover_size(size: tuple[int, int]) -> tuple[int, int]:
    """The size a poster is scaled to so it covers an area of `size` (2:3, one side exact)."""
    width, height = size
    if round(width * POSTER_ASPECT) >= height:
        return width, round(width * POSTER_ASPECT)
    return round(height / POSTER_ASPECT), height


# -- the fade --------------------------------------------------------------------------------


def fade_alpha(x: float, *, start: float, knee: float, end: float) -> float:
    """The fade's opacity at `x` across the poster: 0 up to `start`, smoothly up to FADE_MAX
    at `knee` (where the text starts), then straight on to 1 at `end` (the poster's right
    edge) and beyond."""
    if x <= start:
        return 0.0
    if x >= end:
        return 1.0
    if x < knee:
        t = (x - start) / (knee - start)
        return FADE_MAX * t * t * (3 - 2 * t)
    return FADE_MAX + (1 - FADE_MAX) * (x - knee) / (end - knee)


def _ramp(position: float, solid_at: float, length: float) -> float:
    """0 up to `length` before `solid_at`, rising smoothly to 1 at `solid_at`."""
    if position >= solid_at:
        return 1.0
    if length <= 0 or position <= solid_at - length:
        return 0.0
    t = (position - (solid_at - length)) / length
    return t * t * (3 - 2 * t)


# -- the baked images ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PosterFace:
    """What `compose_poster` bakes around a cover, in physical pixels.

    Hashable and callable, so it serves as the `ImageCache` finish step that turns a cover
    (scaled to `cover_size(size)`) into the card's poster piece, and as part of its cache key.
    """

    size: tuple[int, int]  # the poster area: its width x the card's height
    border: int
    radius: int  # outer corner radius
    fade_from: int  # x where the fade starts
    fade_knee: int  # x where it reaches FADE_MAX: the text's left edge
    solid_left: int  # the area right of this and below `solid_top` is solid `bg` ...
    solid_top: int
    row_fade: int  # ... after fading out over this distance
    bg: RGB
    edge: RGB  # the border
    outside: RGB  # behind the rounded corners: what the card sits on
    placeholder: RGB  # the cover until it has loaded, or for a show without one

    def __call__(self, cover: Image.Image, /) -> Image.Image:
        return compose_poster(cover, self)


@dataclass(frozen=True, slots=True)
class CapFace:
    """The card's right end in physical pixels: the border and the rounded right corners.
    Hashable; calling it bakes the image."""

    size: tuple[int, int]
    border: int
    radius: int
    bg: RGB
    edge: RGB
    outside: RGB

    def __call__(self) -> Image.Image:
        return compose_cap(self)


def poster_face(
    layout: EpisodeLayout,
    area: PosterArea,
    scaling: float,
    height: int,
    *,
    bg: RGB,
    edge: RGB,
    outside: RGB,
    placeholder: RGB,
) -> PosterFace:
    """The poster piece of a card `height` physical pixels tall at widget scaling `scaling`
    (DPI), with `layout`'s sizes and the poster in `area`."""
    width = round(area.width * scaling)
    row_top = layout.pad_bottom + layout.row_height + SOLID_MARGIN
    return PosterFace(
        size=(width, height),
        border=max(1, round(layout.border * scaling)),
        radius=round(layout.radius * scaling),
        fade_from=round(area.fade_from * scaling),
        fade_knee=round(area.text_x * scaling),
        solid_left=round((area.text_x - SOLID_MARGIN) * scaling),
        solid_top=height - round(row_top * scaling),
        row_fade=round(ROW_FADE * scaling),
        bg=bg,
        edge=edge,
        outside=outside,
        placeholder=placeholder,
    )


def cap_face(
    layout: EpisodeLayout, scaling: float, height: int, *, bg: RGB, edge: RGB, outside: RGB
) -> CapFace:
    """The right cap of a card `height` physical pixels tall: as wide as its corners."""
    border = max(1, round(layout.border * scaling))
    radius = round(layout.radius * scaling)
    return CapFace(
        size=(radius + border + 1, height),
        border=border,
        radius=radius,
        bg=bg,
        edge=edge,
        outside=outside,
    )


def compose_poster(cover: Image.Image | None, face: PosterFace) -> Image.Image:
    """The poster piece in RGB: `cover` (None: the placeholder colour) cropped to fill it near
    its top, faded into `face.bg` to the right and around the Download row, inside a border
    with rounded left corners on `face.outside`.

    CPU-heavy: call it in a worker thread. Pure; `cover` is left alone.
    """
    base = Image.new("RGB", face.size, face.placeholder)
    if cover is not None:
        fitted = cover
        if fitted.size != face.size:
            fitted = ImageOps.fit(
                fitted, face.size, Image.Resampling.LANCZOS, centering=(0.5, COVER_TOP)
            )
        if fitted.mode == "RGBA":
            base.paste(fitted, (0, 0), fitted.getchannel("A"))
        else:
            base.paste(fitted.convert("RGB"), (0, 0))
    shade = Image.new("RGB", face.size, face.bg)
    faded = Image.composite(shade, base, _fade_mask(face))
    return _framed(faded, face.size, face.border, face.radius, face.edge, face.outside, "right")


def compose_cap(face: CapFace) -> Image.Image:
    """The right cap in RGB: `face.bg` inside a border with rounded right corners on
    `face.outside`. Pure."""
    inside = Image.new("RGB", face.size, face.bg)
    return _framed(inside, face.size, face.border, face.radius, face.edge, face.outside, "left")


def _framed(
    inside: Image.Image,
    size: tuple[int, int],
    border: int,
    radius: int,
    edge: RGB,
    outside: RGB,
    open_side: Literal["left", "right"],
) -> Image.Image:
    """`inside` within a border of `border` pixels whose corners are rounded except on
    `open_side`, where the card goes on."""
    width, height = size
    card = Image.new("RGB", size, outside)
    card.paste(edge, (0, 0, width, height), _rounded_mask(size, radius, 0, open_side))
    card.paste(inside, (0, 0), _rounded_mask(size, max(0, radius - border), border, open_side))
    return card


@lru_cache(maxsize=16)
def _fade_mask(face: PosterFace) -> Image.Image:
    """An L mask of the fade: `fade_alpha` across, and towards the Download row's corner a
    second fade laid over it (their opacities combined, so it rises smoothly from the first to
    solid). Shared between calls: read-only."""
    width, height = face.size
    across = bytes(
        round(255 * fade_alpha(x + 0.5, start=face.fade_from, knee=face.fade_knee, end=width))
        for x in range(width)
    )
    row_x = bytes(
        round(255 * _ramp(x + 0.5, face.solid_left, face.row_fade)) for x in range(width)
    )
    row_y = bytes(
        round(255 * _ramp(y + 0.5, face.solid_top, face.row_fade)) for y in range(height)
    )
    nearest = Image.Resampling.NEAREST
    mask = Image.frombytes("L", (width, 1), across).resize(face.size, nearest)
    corner = ImageChops.multiply(
        Image.frombytes("L", (width, 1), row_x).resize(face.size, nearest),
        Image.frombytes("L", (1, height), row_y).resize(face.size, nearest),
    )
    return ImageChops.screen(mask, corner)  # 1 - (1 - a) (1 - b)


@lru_cache(maxsize=32)
def _rounded_mask(
    size: tuple[int, int], radius: int, inset: int, open_side: Literal["left", "right"]
) -> Image.Image:
    """An anti-aliased L mask of a rectangle `inset` pixels inside `size` with rounded corners,
    running on past the `open_side` edge (no corners there). Shared between calls: read-only."""
    width, height = size
    scale = _SUPERSAMPLE
    beyond = (radius + inset + 1) * scale  # far enough past the edge to hide those corners
    left = -beyond if open_side == "left" else inset * scale
    right = width * scale + beyond if open_side == "right" else (width - inset) * scale - 1
    big = Image.new("L", (width * scale, height * scale), 0)
    box = (left, inset * scale, right, (height - inset) * scale - 1)
    ImageDraw.Draw(big).rounded_rectangle(box, radius=radius * scale, fill=255)
    return big.resize(size, Image.Resampling.BOX)


# Images shared between cards (placeholders, caps) and the bakes still running; touched on the
# event-loop thread only. The images are read-only.
_shared: dict[Hashable, Image.Image] = {}
_baking: dict[Hashable, asyncio.Task[Image.Image]] = {}
_SHARED_MAX = 64


async def shared_image(key: Hashable, make: Callable[[], Image.Image]) -> Image.Image:
    """`make()`, run in a worker thread once for every card asking with `key`."""
    image = _shared.get(key)
    if image is not None:
        return image
    task = _baking.get(key)
    if task is None:
        task = spawn(asyncio.to_thread(make), name="calendar_face.shared_image")
        _baking[key] = task
        task.add_done_callback(lambda done: _baked(key, done))
    # A card destroyed meanwhile must not cancel the bake the other cards are waiting for.
    return await asyncio.shield(task)


def _baked(key: Hashable, task: asyncio.Task[Image.Image]) -> None:
    if _baking.get(key) is task:
        del _baking[key]
    if not task.cancelled() and task.exception() is None:
        if len(_shared) >= _SHARED_MAX:
            _shared.clear()  # sizes or colours from an earlier card size / DPI
        _shared[key] = task.result()
