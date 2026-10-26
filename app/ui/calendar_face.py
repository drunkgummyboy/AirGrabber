"""The calendar's full episode card (`EpisodeCard` in app/ui/calendar_cards.py): its sizes and
its baked images.

The card is wide and short: the show's poster fills its left half (`poster_area`: half the
card's width, at most as wide as the card is tall), cropped to the card's height near the
poster's top, and fades to the right into the card's background. The show's name, the
episode code and "Not aired" are canvas text over the faded part (Tk draws canvas text
without a box behind it, so the poster shows through). The fade is one smooth curve
(`fade_alpha`, smootherstep) from FADE_START of the poster area to solid at its right edge:
no knee, so no hard line; where the text starts it is about 0.84, so the text stays readable.
Under the Download row, a widget that can't be see-through, the poster fades out completely.

A missing episode (aired, not downloaded, not marked "I already have this") adds an amber line
under the code (`episode_layout(note=True)` makes room for it) and an amber left edge on the
poster piece (`PosterFace.mark`).

Several episodes of one show on one day share a stack card (app/ui/calendar_stack.py):
`episode_layout(stack=True)` makes room for the "3 episodes ▾" pill under the code, and two
card edges peek out behind the card at its bottom right (`StackEdges`, STACK_BEHIND cards each
`stack_step` further right and down), baked into the poster piece and the right cap; the
canvas draws their straight stretch along the bottom. The pill is baked too (`PillFace`).

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
ROW_GAP = 6  # at least, between the code (or the amber note under it) and the Download row
BORDER = 1
MARK_WIDTH = 3  # a missing episode's amber left edge
CORNER_RADIUS = 8
SOLID_MARGIN = 2  # around the Download row, the poster has faded out completely
ROW_FADE = 16  # over this distance next to the Download row the poster fades out
TITLE_SIZE = 14  # bold
TITLE_MAX_LINES = 2
CODE_SIZE = 12  # the episode code, "Not aired" and a missing episode's amber note
# The Download row is drawn at this share of the card's scale (36 units tall at XL is too big
# for a short card); never smaller than ROW_MIN_FACTOR, so its buttons stay easy to click.
ROW_FACTOR = 0.75
ROW_MIN_FACTOR = 2 / 3
# A stack card (several episodes of one show on one day): the "3 episodes ▾" pill under the
# code, and the cards peeking out behind it.
PILL_SIZE = 11  # bold
PILL_PAD_X = 7  # the pill's room left and right of its text
PILL_PAD_Y = 1  # ... and above and below it
PILL_GAP = 4  # between the code (the episodes' range) and the pill
STACK_STEP = 3  # each card behind sits this much further right and down
STACK_BEHIND = 2  # how many cards peek out

# The poster area: this share of the card's width, at most POSTER_MAX_RATIO x its height (a
# wider area would crop the poster to a thin strip). The fade starts at FADE_START of the
# area's width and turns solid at its right edge; the text and the Download row start at
# TEXT_SHARE of it.
POSTER_SHARE = 0.5
POSTER_MAX_RATIO = 1.0
FADE_START = 0.15
TEXT_SHARE = 0.75
# The fade's shape, part of the poster piece's cache key: a new curve never reuses old bakes.
FADE_CURVE = "smootherstep"
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
    mark_width: int  # a missing episode's amber left edge
    note: bool = False  # room for the amber note under the code
    # A stack card: the pill's font size and height and the gap above it (all 0 otherwise),
    # and how much further right and down each card behind sits (0: not a stack).
    pill_size: int = 0
    pill_height: int = 0
    pill_gap: int = 0
    stack_step: int = 0

    @property
    def stack_depth(self) -> int:
        """How far the cards behind a stack card reach past it, right and down (0 for one
        episode)."""
        return STACK_BEHIND * self.stack_step


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
        width=width, fade_from=round(width * FADE_START), text_x=round(width * TEXT_SHARE)
    )


def text_width(layout: EpisodeLayout, area: PosterArea, card_width: float) -> float:
    """The room the name, the code and the Download row have on a card `card_width` wide."""
    return card_width - area.text_x - layout.pad_right


def pill_size(scale: CardScale = FULL) -> int:
    return scale.font(PILL_SIZE, SMALL_TEXT_MIN)


def episode_layout(
    scale: CardScale = FULL,
    *,
    linespace: Callable[[int, bool], int],
    note: bool = False,
    stack: bool = False,
) -> EpisodeLayout:
    """The card's sizes at `scale`. `linespace(size, bold)` is a font's line height: the card
    is tall enough for TITLE_MAX_LINES lines of name, the code and the Download row, with
    `note` (a missing episode) for the amber note under the code as well, and with `stack`
    (several episodes) for the pill under the code instead. Neither changes the poster area:
    the texts of a day's cards start at one x. A stack's cards behind it come on top of
    `height` (`stack_depth`)."""
    rows = row_scale(scale)
    titles, codes = title_size(scale), code_size(scale)
    pad_top, pad_bottom = scale.px(PAD_TOP), scale.px(PAD_BOTTOM)
    code_gap, row_height = scale.px(CODE_GAP), rows.px(ROW_HEIGHT)
    code_line = code_gap + linespace(codes, False)
    needed = (
        pad_top
        + TITLE_MAX_LINES * linespace(titles, True)
        + code_line
        + scale.px(ROW_GAP)
        + row_height
        + pad_bottom
    )
    plain = max(scale.px(CARD_HEIGHT), needed)  # without the note: what sizes the poster
    height = max(plain, needed + code_line) if note else plain
    pills = pill_size(scale) if stack else 0
    pill_height = linespace(pills, True) + 2 * scale.px(PILL_PAD_Y, minimum=1) if stack else 0
    pill_gap = scale.px(PILL_GAP) if stack else 0
    if stack:
        height = max(plain, needed + pill_gap + pill_height)
    return EpisodeLayout(
        height=height,
        poster_min=POSTER_STEP * -(-scale.px(POSTER_MIN) // POSTER_STEP),  # rounded up
        poster_max=POSTER_STEP * int(plain * POSTER_MAX_RATIO // POSTER_STEP),  # rounded down
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
        mark_width=scale.px(MARK_WIDTH, minimum=2),
        note=note,
        pill_size=pills,
        pill_height=pill_height,
        pill_gap=pill_gap,
        stack_step=scale.px(STACK_STEP, minimum=2) if stack else 0,
    )


def cover_size(size: tuple[int, int]) -> tuple[int, int]:
    """The size a poster is scaled to so it covers an area of `size` (2:3, one side exact)."""
    width, height = size
    if round(width * POSTER_ASPECT) >= height:
        return width, round(width * POSTER_ASPECT)
    return round(height / POSTER_ASPECT), height


# -- the fade --------------------------------------------------------------------------------


def fade_alpha(x: float, *, start: float, end: float) -> float:
    """The fade's opacity at `x` across the poster: 0 up to `start`, then one smooth curve
    (smootherstep, 6u⁵ − 15u⁴ + 10u³: flat at both ends, so neither end shows as a line) up
    to 1 at `end` (the poster's right edge) and beyond."""
    if x <= start:
        return 0.0
    if x >= end:
        return 1.0
    u = (x - start) / (end - start)
    return u * u * u * (u * (6 * u - 15) + 10)


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
class StackEdges:
    """The cards peeking out behind a stack card, in physical pixels: STACK_BEHIND of them,
    each `step` further right and down than the one in front, the nearest first in
    `fills` / `edges`."""

    step: int
    fills: tuple[RGB, ...]
    edges: tuple[RGB, ...]

    @property
    def depth(self) -> int:
        """How far the last card reaches past the front one."""
        return self.step * len(self.fills)


@dataclass(frozen=True, slots=True)
class PosterFace:
    """What `compose_poster` bakes around a cover, in physical pixels.

    Hashable and callable, so it serves as the `ImageCache` finish step that turns a cover
    (scaled to `cover_size(size)`) into the card's poster piece, and as part of its cache key.
    """

    size: tuple[int, int]  # the poster area: its width x the (front) card's height
    border: int
    radius: int  # outer corner radius
    fade_from: int  # x where the fade starts (solid at the area's right edge)
    solid_left: int  # the area right of this and below `solid_top` is solid `bg` ...
    solid_top: int
    row_fade: int  # ... after fading out over this distance
    bg: RGB
    edge: RGB  # the border
    outside: RGB  # behind the rounded corners: what the card sits on
    placeholder: RGB  # the cover until it has loaded, or for a show without one
    mark: RGB | None = None  # a missing episode: an amber left edge ...
    mark_width: int = 0  # ... this wide, following the rounded corners
    stack: StackEdges | None = None  # a stack card: the cards behind it (the piece is taller)
    fade: str = FADE_CURVE  # the fade's shape: a new one never reuses an old bake

    def __call__(self, cover: Image.Image, /) -> Image.Image:
        return compose_poster(cover, self)


@dataclass(frozen=True, slots=True)
class CapFace:
    """The card's right end in physical pixels: the border and the rounded right corners.
    Hashable; calling it bakes the image."""

    size: tuple[int, int]  # the front card's part
    border: int
    radius: int
    bg: RGB
    edge: RGB
    outside: RGB
    stack: StackEdges | None = None  # a stack card: the cards behind it (the cap is larger)

    def __call__(self) -> Image.Image:
        return compose_cap(self)


@dataclass(frozen=True, slots=True)
class PillFace:
    """A stack card's "3 episodes ▾" pill without its text (canvas text goes on top), in
    physical pixels: fully rounded ends, see-through around them. Hashable; calling it bakes
    the image."""

    size: tuple[int, int]
    fill: RGB

    def __call__(self) -> Image.Image:
        return compose_pill(self)


def stack_step_px(layout: EpisodeLayout, scaling: float) -> int:
    """`layout.stack_step` in physical pixels at widget scaling `scaling` (0: not a stack)."""
    return max(2, round(layout.stack_step * scaling)) if layout.stack_step > 0 else 0


def stack_edges(
    layout: EpisodeLayout, scaling: float, *, fills: tuple[RGB, ...], edges: tuple[RGB, ...]
) -> StackEdges | None:
    """The cards behind a stack card at widget scaling `scaling`, nearest first, STACK_BEHIND
    of them (None for a card of one episode)."""
    step = stack_step_px(layout, scaling)
    if step <= 0:
        return None
    if len(fills) != STACK_BEHIND or len(edges) != STACK_BEHIND:
        raise ValueError(f"a stack has {STACK_BEHIND} cards behind it")
    return StackEdges(step=step, fills=fills, edges=edges)


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
    mark: RGB | None = None,
    stack: StackEdges | None = None,
) -> PosterFace:
    """The poster piece of a card `height` physical pixels tall (the front card of a stack) at
    widget scaling `scaling` (DPI), with `layout`'s sizes and the poster in `area`; `mark`: the
    colour of a missing episode's left edge; `stack`: the cards behind a stack card."""
    width = round(area.width * scaling)
    row_top = layout.pad_bottom + layout.row_height + SOLID_MARGIN
    return PosterFace(
        size=(width, height),
        border=max(1, round(layout.border * scaling)),
        radius=round(layout.radius * scaling),
        fade_from=round(area.fade_from * scaling),
        solid_left=round((area.text_x - SOLID_MARGIN) * scaling),
        solid_top=height - round(row_top * scaling),
        row_fade=round(ROW_FADE * scaling),
        bg=bg,
        edge=edge,
        outside=outside,
        placeholder=placeholder,
        mark=mark,
        mark_width=max(1, round(layout.mark_width * scaling)) if mark is not None else 0,
        stack=stack,
    )


def cap_face(
    layout: EpisodeLayout,
    scaling: float,
    height: int,
    *,
    bg: RGB,
    edge: RGB,
    outside: RGB,
    stack: StackEdges | None = None,
) -> CapFace:
    """The right cap of a card `height` physical pixels tall (the front card of a stack): as
    wide as its corners; with `stack`, the ends of the cards behind it as well."""
    border = max(1, round(layout.border * scaling))
    radius = round(layout.radius * scaling)
    return CapFace(
        size=(radius + border + 1, height),
        border=border,
        radius=radius,
        bg=bg,
        edge=edge,
        outside=outside,
        stack=stack,
    )


def compose_poster(cover: Image.Image | None, face: PosterFace) -> Image.Image:
    """The poster piece in RGB: `cover` (None: the placeholder colour) cropped to fill it near
    its top, faded into `face.bg` to the right and around the Download row, inside a border
    with rounded left corners on `face.outside`; with `face.mark`, a left edge in that colour;
    with `face.stack`, the cards behind it below (the piece is `stack.depth` taller).

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
    piece = _framed(faded, face.size, face.border, face.radius, face.edge, face.outside, "right")
    if face.mark is not None and face.mark_width > 0:
        edge_mask = _mark_mask(face.size, face.radius, face.mark_width)
        piece.paste(face.mark, (0, 0, *face.size), edge_mask)
    if face.stack is not None:
        piece = _stacked(piece, face.border, face.radius, face.outside, face.stack, "right")
    return piece


def compose_cap(face: CapFace) -> Image.Image:
    """The right cap in RGB: `face.bg` inside a border with rounded right corners on
    `face.outside`; with `face.stack`, the right ends of the cards behind it (the cap is
    `stack.depth` wider and taller). Pure."""
    inside = Image.new("RGB", face.size, face.bg)
    cap = _framed(inside, face.size, face.border, face.radius, face.edge, face.outside, "left")
    if face.stack is not None:
        cap = _stacked(cap, face.border, face.radius, face.outside, face.stack, "left")
    return cap


def compose_pill(face: PillFace) -> Image.Image:
    """The pill in RGBA: `face.fill` with fully rounded ends, see-through around them. Pure."""
    width, height = face.size
    scale = _SUPERSAMPLE
    big = Image.new("L", (width * scale, height * scale), 0)
    ImageDraw.Draw(big).rounded_rectangle(
        (0, 0, width * scale - 1, height * scale - 1), radius=height * scale // 2, fill=255
    )
    pill = Image.new("RGBA", face.size, (*face.fill, 0))
    pill.putalpha(big.resize(face.size, Image.Resampling.BOX))
    return pill


def _stacked(
    front: Image.Image,
    border: int,
    radius: int,
    outside: RGB,
    stack: StackEdges,
    open_side: Literal["left", "right"],
) -> Image.Image:
    """`front` (a piece of the front card, open on `open_side`) with the cards behind it: each
    `stack.step` further right and down, peeking out below it (and right of it, for the cap:
    `open_side` "left"). The image grows by `stack.depth` down (and right, for the cap)."""
    width, height = front.size
    depth = stack.depth
    size = (width + (depth if open_side == "left" else 0), height + depth)
    card = Image.new("RGB", size, outside)
    behind = list(zip(stack.fills, stack.edges, strict=True))
    for index in reversed(range(len(behind))):  # the farthest first
        fill, edge = behind[index]
        shift = (index + 1) * stack.step
        box = (shift, shift, width + shift, height + shift)
        card.paste(edge, (0, 0, *size), _box_mask(size, box, radius, open_side))
        inner = (box[0] + border, box[1] + border, box[2] - border, box[3] - border)
        card.paste(fill, (0, 0, *size), _box_mask(size, inner, max(0, radius - border), open_side))
    card.paste(front, (0, 0), _rounded_mask(front.size, radius, 0, open_side))
    return card


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
        round(255 * fade_alpha(x + 0.5, start=face.fade_from, end=width)) for x in range(width)
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


@lru_cache(maxsize=8)
def _mark_mask(size: tuple[int, int], radius: int, width: int) -> Image.Image:
    """An L mask of the card's left `width` pixels within its rounded left corners. Shared
    between calls: read-only."""
    column = Image.new("L", size, 0)
    column.paste(255, (0, 0, width, size[1]))
    return ImageChops.multiply(_rounded_mask(size, radius, 0, "right"), column)


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


@lru_cache(maxsize=32)
def _box_mask(
    size: tuple[int, int],
    box: tuple[int, int, int, int],
    radius: int,
    open_side: Literal["left", "right"],
) -> Image.Image:
    """An anti-aliased L mask, `size` large, of the rectangle `box` (left, top, right, bottom)
    with rounded corners, running on past its `open_side` edge and the image's (no corners
    there). Shared between calls: read-only."""
    width, height = size
    scale = _SUPERSAMPLE
    left, top, right, bottom = box
    beyond = (radius + 1) * scale  # far enough past the edge to hide those corners
    x0 = min(left, 0) * scale - beyond if open_side == "left" else left * scale
    x1 = max(right, width) * scale + beyond if open_side == "right" else right * scale - 1
    big = Image.new("L", (width * scale, height * scale), 0)
    ImageDraw.Draw(big).rounded_rectangle(
        (x0, top * scale, x1, bottom * scale - 1), radius=radius * scale, fill=255
    )
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
