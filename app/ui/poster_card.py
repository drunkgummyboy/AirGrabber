"""Tall poster cards: the cover fills a 2:3 card and fades into `theme.BG` at the bottom, where
the title, a "2024 | ★ 7.8" line and an action row (Download, + Track) sit.

Tk can't draw a see-through widget over an image, so the cover, the fade, the border and the
rounded corners are baked into one image at the card's physical size, in a worker thread
(`compose_card`, cached by `ImageCache` per url and size), and shown on a canvas that fills the
card. The title and the line under it are text on that canvas, so the cover shows through
behind them: the fade stops at FADE_MAX there. The rows of widgets under the text (buttons,
switches) have `theme.BG` as their own background, so the fade turns solid where they start.

A card is drawn at a `CardScale` (Settings, Card size: app/ui/card_scale.py): every size, gap
and font below is the XL card's and scales with it; the cover is rendered at the scaled card's
physical size (and again when Windows' scaling changes, `_set_scaling`).

Switching between light and dark (`_set_appearance_mode`) repaints the canvas and bakes the
cover again: the face's colours are part of its cache key, so light and dark covers don't mix.
"""

from __future__ import annotations

import asyncio
import logging
import tkinter as tk
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Literal

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageOps, ImageTk

from app.i18n import N_, number, t
from app.ui import theme
from app.ui.card_scale import FULL, SMALL_TEXT_MIN, TEXT_MIN, TITLE_MIN, CardScale
from app.ui.download_control import CORNER_RADIUS as ROW_CORNER_RADIUS
from app.ui.download_control import FONT_SIZE as ROW_FONT_SIZE
from app.ui.download_control import ROW_HEIGHT, fit_text
from app.ui.tasks import spawn
from app.ui.text_fit import SLACK, break_lines, fit_to_lines, title_lines

if TYPE_CHECKING:
    from app.services.images import ImageCache

log = logging.getLogger(__name__)

RGB = tuple[int, int, int]
TrackState = Literal["idle", "busy", "tracked", "missing"]

# 2:3, and whole physical pixels at 125, 150 and 175 % (190 x 285 would round unevenly).
POSTER_CARD_WIDTH = 192
POSTER_CARD_HEIGHT = 288
BORDER = 2
CORNER_RADIUS = 14
SIDE_PAD = 12  # the text and the action row, from the border
BOTTOM_PAD = 12  # under the action row
ROW_GAP = 10  # between the text and the action row
META_GAP = 2  # between the title and the year / rating line
TEXT_WIDTH = POSTER_CARD_WIDTH - 2 * (BORDER + SIDE_PAD)  # at XL; see `text_width`
TITLE_SIZE = 15
TITLE_MAX_LINES = 2
META_SIZE = 12
# The fade starts at 45 % of the card's height: soft at first, then on up to FADE_MAX, which it
# reaches TEXT_MARGIN above the text and keeps behind it; under the text it turns solid by the
# top of the widget rows. Above tall text (two title lines over two rows of buttons) it starts
# higher, so its soft part keeps at least FADE_MIN_SPAN of the card's height.
FADE_FROM = 0.45
FADE_MIN_SPAN = 0.12
FADE_KNEE = (0.45, 0.85)  # the soft part's shape: 85 % of the way up a little under half way
FADE_MAX = 0.80  # the fade's opacity behind the text: the cover shows through at 20 %
TEXT_MARGIN = 4
# A badge in the cover's top-left corner ("3 missing" on My shows): a pill on `theme.BG`.
BADGE_INSET = 10  # from the card's outer edge
BADGE_HEIGHT = 22
BADGE_PAD = 8  # inside the pill, left and right of the text
BADGE_FONT = 12

# Shown through t() (`track_look`).
FOLLOWING_TEXT = N_("✓ Following")
TRACK_TEXT = N_("+ Track")
MISSING_TEXT = N_("Not on TVMaze")

_SUPERSAMPLE = 4  # the rounded corners are drawn this much larger, then scaled down


# -- sizes -----------------------------------------------------------------------------------


def card_size(scale: CardScale = FULL) -> tuple[int, int]:
    """The card's (width, height) in CTk units at `scale`."""
    return scale.px(POSTER_CARD_WIDTH), scale.px(POSTER_CARD_HEIGHT)


def border_width(scale: CardScale = FULL) -> int:
    return scale.px(BORDER, minimum=1)


def side_pad(scale: CardScale = FULL) -> int:
    """From the card's edge to the text and the action row (border included)."""
    return border_width(scale) + scale.px(SIDE_PAD)


def text_width(scale: CardScale = FULL) -> int:
    """The room the title, the meta line and the rows under them have, in CTk units."""
    return card_size(scale)[0] - 2 * side_pad(scale)


def row_height(scale: CardScale = FULL) -> int:
    """The action row's height (the Download row, Track)."""
    return scale.px(ROW_HEIGHT)


def row_font_size(scale: CardScale = FULL) -> int:
    return scale.font(ROW_FONT_SIZE, TEXT_MIN)


# -- text ------------------------------------------------------------------------------------


def year_rating_line(year: int | None, rating: float | None) -> str:
    """"2024 | ★ 7.8", leaving out whichever is unknown ("" for neither)."""
    parts: list[str] = []
    if year:
        parts.append(str(year))
    if rating is not None:
        parts.append(f"★ {number(rating, '.1f')}")
    return " | ".join(parts)


# -- the baked image -------------------------------------------------------------------------


def fade_alpha(position: float, maximum: float = FADE_MAX) -> float:
    """The fade's opacity at `position`: 0 where it starts, `maximum` where it reaches the text.
    Smooth from 0 up to 85 % of `maximum` at 0.45 (no visible edge where it starts), then
    straight on to `maximum`."""
    if position <= 0:
        return 0.0
    if position >= 1:
        return maximum
    knee, knee_share = FADE_KNEE
    if position < knee:
        t = position / knee
        share = knee_share * t * t * (3 - 2 * t)
    else:
        share = knee_share + (1 - knee_share) * (position - knee) / (1 - knee)
    return maximum * share


@dataclass(frozen=True, slots=True)
class CardFace:
    """What `compose_card` bakes around a cover, in physical pixels.

    Hashable and callable, so it serves as the `ImageCache` finish step that turns a cropped
    cover into the card's image (and as part of that image's cache key).
    """

    size: tuple[int, int]
    border: int
    radius: int  # outer corner radius
    fade_from: int  # first row of the fade
    full_from: int  # first row at `fade_max` (TEXT_MARGIN above the text)
    ramp_from: int  # first row under the text: from `fade_max` on to solid
    solid_from: int  # first row of solid `bg` (the widget rows)
    fade_max: float  # the fade's opacity behind the text
    bg: RGB
    edge: RGB  # the border
    outside: RGB  # behind the rounded corners: what the card sits on
    placeholder: RGB  # the cover until it has loaded, or for a title without one

    def __call__(self, cover: Image.Image, /) -> Image.Image:
        return compose_card(cover, self)


def card_face(
    scale: float,
    text_height: float,
    *,
    rows_height: float | None = None,
    bg: RGB,
    edge: RGB,
    outside: RGB,
    placeholder: RGB,
    card: CardScale = FULL,
) -> CardFace:
    """The face of a card at widget scaling `scale` (DPI) and card size `card` whose text (the
    title and the line under it) is `text_height` CTk units tall, above widget rows of
    `rows_height` units (None: one action row) on the bottom padding.

    The fade starts at 45 % of the card's height (higher above tall text: FADE_MIN_SPAN),
    reaches FADE_MAX TEXT_MARGIN above the text, and turns solid between the text and the
    widget rows (ROW_GAP). In physical pixels (DPI
    factor x card scale), so the cover stays sharp at every size."""
    units_w, units_h = card_size(card)
    width, height = round(units_w * scale), round(units_h * scale)
    border = border_width(card)
    rows = row_height(card) if rows_height is None else rows_height
    rows_top = border + card.px(BOTTOM_PAD) + rows  # all from the card's bottom edge
    text_bottom = rows_top + card.px(ROW_GAP)
    text_top = text_bottom + text_height

    def row(units: float) -> int:
        """The pixel row `units` above the card's bottom edge."""
        return max(0, height - round(units * scale))

    solid_from = row(rows_top)
    ramp_from = min(row(text_bottom), solid_from)
    full_from = min(row(text_top + card.px(TEXT_MARGIN)), ramp_from)
    return CardFace(
        size=(width, height),
        border=max(1, round(border * scale)),
        radius=round(card.px(CORNER_RADIUS) * scale),
        fade_from=max(0, min(round(height * FADE_FROM), full_from - round(height * FADE_MIN_SPAN))),
        full_from=full_from,
        ramp_from=ramp_from,
        solid_from=solid_from,
        fade_max=FADE_MAX,
        bg=bg,
        edge=edge,
        outside=outside,
        placeholder=placeholder,
    )


def compose_card(cover: Image.Image | None, face: CardFace) -> Image.Image:
    """The card's whole image in RGB: `cover` (None: the placeholder colour) filling it, faded
    into `face.bg` towards the bottom, inside a rounded border on `face.outside`.

    CPU-heavy: call it in a worker thread. Pure; `cover` is left alone.
    """
    width, height = face.size
    base = Image.new("RGB", face.size, face.placeholder)
    if cover is not None:
        fitted = cover
        if fitted.size != face.size:
            fitted = ImageOps.fit(fitted, face.size, Image.Resampling.LANCZOS)
        if fitted.mode == "RGBA":
            base.paste(fitted, (0, 0), fitted.getchannel("A"))
        else:
            base.paste(fitted.convert("RGB"), (0, 0))
    shade = Image.new("RGB", face.size, face.bg)
    mask = _fade_mask(
        face.size, face.fade_from, face.full_from, face.ramp_from, face.solid_from, face.fade_max
    )
    faded = Image.composite(shade, base, mask)
    card = Image.new("RGB", face.size, face.outside)
    card.paste(face.edge, (0, 0, width, height), _rounded_mask(face.size, face.radius, 0))
    inner = _rounded_mask(face.size, max(0, face.radius - face.border), face.border)
    card.paste(faded, (0, 0), inner)
    return card


def fade_row_alpha(
    row: int, fade_from: int, full_from: int, ramp_from: int, solid_from: int, fade_max: float
) -> float:
    """The fade's opacity on pixel row `row` (see `CardFace`): 0 above `fade_from`,
    `fade_alpha` up to `fade_max` at `full_from`, `fade_max` behind the text, then straight on
    to 1 at `solid_from`."""
    if row >= solid_from:
        return 1.0
    middle = row + 0.5
    if row >= ramp_from:
        return fade_max + (1 - fade_max) * (middle - ramp_from) / max(1, solid_from - ramp_from)
    if row >= full_from:
        return fade_max
    return fade_alpha((middle - fade_from) / max(1, full_from - fade_from), fade_max)


@lru_cache(maxsize=16)
def _fade_mask(
    size: tuple[int, int],
    fade_from: int,
    full_from: int,
    ramp_from: int,
    solid_from: int,
    fade_max: float,
) -> Image.Image:
    """An L mask, one value per row (`fade_row_alpha`). Shared between calls: read-only."""
    width, height = size
    column = bytes(
        round(255 * fade_row_alpha(row, fade_from, full_from, ramp_from, solid_from, fade_max))
        for row in range(height)
    )
    return Image.frombytes("L", (1, height), column).resize(size, Image.Resampling.NEAREST)


@lru_cache(maxsize=32)
def _rounded_mask(size: tuple[int, int], radius: int, inset: int) -> Image.Image:
    """An anti-aliased L mask of a rounded rectangle `inset` pixels inside `size`. Shared
    between calls: read-only."""
    width, height = size
    scale = _SUPERSAMPLE
    big = Image.new("L", (width * scale, height * scale), 0)
    box = (inset * scale, inset * scale, (width - inset) * scale - 1, (height - inset) * scale - 1)
    ImageDraw.Draw(big).rounded_rectangle(box, radius=radius * scale, fill=255)
    return big.resize(size, Image.Resampling.BOX)


@lru_cache(maxsize=32)
def pill_image(size: tuple[int, int], border: int, fill: RGB, edge: RGB) -> Image.Image:
    """A pill (a circle when square) of `size` physical pixels in RGBA: `fill` inside a
    `border`-pixel `edge` line, transparent around its rounded ends, anti-aliased. Small: fine
    on the event loop. Shared between calls: read-only."""
    width, height = size
    radius = min(width, height) // 2
    colour = Image.new("RGB", size, edge)
    colour.paste(fill, (0, 0, width, height), _rounded_mask(size, max(0, radius - border), border))
    pill = colour.convert("RGBA")
    pill.putalpha(_rounded_mask(size, radius, 0))
    return pill


# Placeholder images by face, and the composes still running; touched on the event-loop thread
# only. The images are shared between cards: read-only.
_placeholders: dict[CardFace, Image.Image] = {}
_placeholder_tasks: dict[CardFace, asyncio.Task[Image.Image]] = {}


async def placeholder_image(face: CardFace) -> Image.Image:
    """`compose_card(None, face)`, composed in a worker thread once for all cards with `face`."""
    image = _placeholders.get(face)
    if image is not None:
        return image
    task = _placeholder_tasks.get(face)
    if task is None:
        task = spawn(asyncio.to_thread(compose_card, None, face), name="placeholder_image")
        _placeholder_tasks[face] = task
        task.add_done_callback(lambda done: _placeholder_done(face, done))
    # A card destroyed meanwhile must not cancel the compose the other cards are waiting for.
    return await asyncio.shield(task)


def _placeholder_done(face: CardFace, task: asyncio.Task[Image.Image]) -> None:
    if _placeholder_tasks.get(face) is task:
        del _placeholder_tasks[face]
    if not task.cancelled() and task.exception() is None:
        _placeholders[face] = task.result()


# -- the action row's Track button -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrackLook:
    text: str
    color: theme.Color
    hover: theme.Color
    enabled: bool
    border: theme.Color | None = None  # a 1-unit border in this colour
    text_color: theme.Color | None = None  # None = the button's default; disabled text included


def track_look(state: TrackState, busy_text: str) -> TrackLook:
    """idle → "+ Track"; busy → `busy_text`; tracked → "✓ Following" in SUCCESS with a SUCCESS
    border; missing → "Not on TVMaze"."""
    if state == "idle":
        return TrackLook(t(TRACK_TEXT), theme.ACCENT, theme.ACCENT_HOVER, enabled=True)
    if state == "busy":
        return TrackLook(busy_text, theme.ACCENT, theme.ACCENT_HOVER, enabled=False)
    if state == "tracked":
        return TrackLook(
            t(FOLLOWING_TEXT),
            "transparent",
            theme.ACCENT_HOVER,
            enabled=False,
            border=theme.SUCCESS,
            text_color=theme.SUCCESS,
        )
    return TrackLook(
        t(MISSING_TEXT), theme.DANGER_BUTTON, theme.DANGER_BUTTON_HOVER, enabled=False
    )


class TrackButton(ctk.CTkButton):
    """A show card's action row: "+ Track", `busy_text` while adding, "✓ Following" once
    tracked (see `track_look`)."""

    def __init__(
        self,
        master: Any,
        *,
        busy_text: str,
        command: Callable[[], None],
        scale: CardScale = FULL,
    ) -> None:
        super().__init__(
            master,
            text="",
            height=row_height(scale),
            corner_radius=scale.px(ROW_CORNER_RADIUS),
            font=theme.font(row_font_size(scale), bold=True),
            border_width=0,
            command=command,
        )
        self._busy_text = busy_text
        self._default_text_color = self.cget("text_color")
        self._default_disabled_color = self.cget("text_color_disabled")
        self.set_state("idle")

    def set_state(self, state: TrackState) -> None:
        look = track_look(state, self._busy_text)
        self.configure(
            text=look.text,
            fg_color=look.color,
            hover_color=look.hover,
            border_width=1 if look.border else 0,
            border_color=look.border or theme.EDGE,
            text_color=look.text_color or self._default_text_color,
            text_color_disabled=look.text_color or self._default_disabled_color,
            state="normal" if look.enabled else "disabled",
        )


# -- the card --------------------------------------------------------------------------------


def rgb(widget: tk.Misc, color: theme.Color) -> RGB:
    """`color` (a name or "#rrggbb"; a pair: the current mode's) as 8-bit RGB."""
    red, green, blue = widget.winfo_rgb(theme.pick(color))  # 16 bits per channel
    return (red >> 8, green >> 8, blue >> 8)


class PosterCard(ctk.CTkFrame):
    """A 2:3 card: the cover with the title, the "2024 | ★ 7.8" line (`meta`; left out when
    empty unless `meta_row`, which keeps its room for `_set_meta`) and an action row on its
    faded bottom.

    `poster` gives the cover's url for a card that many physical pixels wide (None: no cover).
    Subclasses create their action row with this card as master and hand it to `_pack_action`;
    `_set_open` sets what a click on the cover or the text does. A subclass with a row between
    the text and the action row (`_pack_above_action`) passes its height, gap included, as
    `extra_height` (already at `scale`), so the fade turns solid behind that row too.
    `_set_badge` puts a pill in the cover's top-left corner.

    `scale` sizes the whole card (`self.scale`; subclasses size their rows with it).
    """

    def __init__(
        self,
        master: Any,
        *,
        title: str,
        meta: str,
        poster: Callable[[int], str | None],
        images: ImageCache,
        extra_height: float = 0,
        scale: CardScale = FULL,
        meta_row: bool = False,
    ) -> None:
        self.scale = scale
        width, height = card_size(scale)
        super().__init__(
            master,
            fg_color=theme.BG,
            border_color=theme.EDGE,
            border_width=border_width(scale),
            corner_radius=scale.px(CORNER_RADIUS),
            width=width,
            height=height,
        )
        self.grid_propagate(False)
        self.pack_propagate(False)
        self._units = (width, height)
        self._poster = poster
        self._images = images
        self._open: Callable[[], object] | None = None
        self._cover_task: asyncio.Task[None] | None = None
        self._photo: ImageTk.PhotoImage | None = None
        self._pad_x = side_pad(scale)
        self._bottom_pad = border_width(scale) + scale.px(BOTTOM_PAD)
        self._rows_height = row_height(scale) + extra_height
        self._room = text_width(scale) - SLACK

        self.title_font = theme.font(scale.font(TITLE_SIZE, TITLE_MIN), bold=True)
        # Breaks made explicit: a title that only fits word-wrapped would otherwise stay one
        # line (cut off at both sides on a small card) and its height would be counted wrong.
        self._title_text = break_lines(
            fit_to_lines(title_lines(title), self._room, TITLE_MAX_LINES, self.title_font.measure),
            self._room,
            self.title_font.measure,
        )
        self._meta_font = theme.font(scale.font(META_SIZE, SMALL_TEXT_MIN))
        self._meta_gap = scale.px(META_GAP)
        with_meta = bool(meta) or meta_row
        self._text_height: float = (self._title_text.count("\n") + 1) * _linespace(self.title_font)
        if with_meta:
            self._text_height += self._meta_gap + _linespace(self._meta_font)

        # Placed once it has an image; the action rows, created later, stack above it.
        self._sheet = tk.Canvas(
            self, highlightthickness=0, borderwidth=0, background=theme.pick(theme.BG)
        )
        self._cover_item = self._sheet.create_image(0, 0, anchor="nw")
        self._title_item = self._sheet.create_text(
            0, 0, text=self._title_text, fill=theme.pick(theme.TEXT), anchor="s", justify="center"
        )
        self._meta_item: int | None = None
        self._meta_shown: tuple[str, theme.Color] = ("", theme.TEXT_MUTED)  # text, colour
        if with_meta:
            self._meta_item = self._sheet.create_text(0, 0, text="", anchor="n")
            self._set_meta(meta)
        self._badge: tuple[str, theme.Color] | None = None  # text, colour
        self._badge_font = theme.font(scale.font(BADGE_FONT, TEXT_MIN), bold=True)
        self._badge_items: tuple[int, int] | None = None  # pill image, text
        self._badge_photo: ImageTk.PhotoImage | None = None
        self._sheet.bind("<Button-1>", self._clicked, add="+")
        self._apply_scale()

    @property
    def scaling(self) -> float:
        """Windows' scaling of this card (physical pixels per CTk unit)."""
        scaling: float = ctk.ScalingTracker.get_widget_scaling(self)
        return scaling

    # -- layout --------------------------------------------------------------------------------

    def _apply_scale(self) -> None:
        """Lay the canvas out at the current scaling and (re)load the cover at that size."""
        scaling = self.scaling
        width, height = self._units
        canvas = self._sheet
        text_bottom = height - (self._bottom_pad + self._rows_height + self.scale.px(ROW_GAP))
        centre = round(width * scaling) / 2
        title_bottom: float = text_bottom
        if self._meta_item is not None:
            meta_top = text_bottom - _linespace(self._meta_font)
            canvas.coords(self._meta_item, centre, round(meta_top * scaling))
            canvas.itemconfigure(
                self._meta_item, font=self._meta_font.create_scaled_tuple(scaling)
            )
            title_bottom = meta_top - self._meta_gap
        canvas.coords(self._title_item, centre, round(title_bottom * scaling))
        canvas.itemconfigure(self._title_item, font=self.title_font.create_scaled_tuple(scaling))
        self._draw_badge()

        self._face = card_face(
            scaling,
            self._text_height,
            rows_height=self._rows_height,
            card=self.scale,
            bg=rgb(self, theme.BG),
            edge=rgb(self, theme.EDGE),
            outside=rgb(self, self._apply_appearance_mode(self.cget("bg_color"))),
            placeholder=rgb(self, theme.PLACEHOLDER),
        )
        if self._cover_task is not None:
            self._cover_task.cancel()
        url = self._poster(self._face.size[0])
        self._cover_task = spawn(self._load_cover(self._images, url), name="PosterCard.cover")

    def _set_scaling(self, *args: Any, **kwargs: Any) -> None:
        """Windows' scaling changed (the window moved to another screen): CTk resizes the
        widgets; the canvas follows here."""
        super()._set_scaling(*args, **kwargs)
        self._apply_scale()

    def _set_appearance_mode(self, mode_string: str) -> None:
        """Light ↔ dark: CTk repaints the widgets; the canvas, its texts, the badge and the
        baked cover follow here."""
        super()._set_appearance_mode(mode_string)
        self._sheet.configure(background=theme.pick(theme.BG))
        self._sheet.itemconfigure(self._title_item, fill=theme.pick(theme.TEXT))
        if self._meta_item is not None:
            self._sheet.itemconfigure(self._meta_item, fill=theme.pick(self._meta_shown[1]))
        self._apply_scale()

    # -- action row and opening ----------------------------------------------------------------

    def _pack_action(self, row: Any) -> None:
        """Pack `row` (created with this card as master) as the card's bottom row."""
        row.pack(side="bottom", fill="x", padx=self._pad_x, pady=(0, self._bottom_pad))

    def _pack_above_action(self, row: Any, gap: int) -> None:
        """Pack `row` (created with this card as master) between the text and the action row,
        `gap` (at the card's scale) above the action row. Call after `_pack_action`."""
        row.pack(side="bottom", fill="x", padx=self._pad_x, pady=(0, gap))

    def _set_open(self, action: Callable[[], object] | None) -> None:
        """What a click on the cover or the text does (None: nothing)."""
        self._open = action
        self._sheet.configure(cursor="hand2" if action is not None else "")

    def _clicked(self, _event: Any) -> None:
        if self._open is not None:
            self._open()

    # -- text and badge --------------------------------------------------------------------------

    @property
    def title_text(self) -> str:
        """The title as shown (fitted to two lines)."""
        return self._title_text

    @property
    def meta_text(self) -> str | None:
        """The line under the title as shown; None for a card without one."""
        return self._meta_shown[0] if self._meta_item is not None else None

    @property
    def meta_color(self) -> theme.Color | None:
        return self._meta_shown[1] if self._meta_item is not None else None

    def _set_meta(self, text: str | Sequence[str], color: theme.Color = theme.TEXT_MUTED) -> None:
        """Show `text` under the title (the first of several, longest first, that fits; else
        shortened), in `color`. Only on a card with that line (`meta` or `meta_row`)."""
        if self._meta_item is None:
            return
        texts = (text,) if isinstance(text, str) else tuple(text)
        fitted = fit_text(texts, self._room, self._meta_font.measure) if texts else ""
        self._sheet.itemconfigure(self._meta_item, text=fitted, fill=theme.pick(color))
        self._meta_shown = (fitted, color)

    @property
    def badge_text(self) -> str | None:
        return self._badge[0] if self._badge is not None else None

    def _set_badge(self, text: str | None, color: theme.Color = theme.TEXT) -> None:
        """A pill with `text` in `color` (outline and text, on `theme.BG`) in the cover's
        top-left corner; None takes it away."""
        self._badge = (text, color) if text else None
        self._draw_badge()

    def _draw_badge(self) -> None:
        canvas = self._sheet
        if self._badge is None:
            if self._badge_items is not None:
                canvas.delete(*self._badge_items)
            self._badge_items = None
            self._badge_photo = None
            return
        text, color = self._badge
        scaling = self.scaling
        scale = self.scale
        font = self._badge_font
        height = scale.text_height(BADGE_HEIGHT, int(font.cget("size")))
        width = font.measure(text) + 2 * scale.px(BADGE_PAD)
        size = (round(width * scaling), round(height * scaling))
        x = y = round(scale.px(BADGE_INSET) * scaling)
        pill = pill_image(size, max(1, round(scaling)), rgb(self, theme.BG), rgb(self, color))
        self._badge_photo = ImageTk.PhotoImage(pill, master=canvas)
        if self._badge_items is None:
            self._badge_items = (
                canvas.create_image(0, 0, anchor="nw"),
                canvas.create_text(0, 0, anchor="center"),
            )
        image_item, text_item = self._badge_items
        canvas.coords(image_item, x, y)
        canvas.itemconfigure(image_item, image=self._badge_photo)
        canvas.coords(text_item, x + size[0] / 2, y + size[1] / 2)
        canvas.itemconfigure(
            text_item, text=text, fill=theme.pick(color), font=font.create_scaled_tuple(scaling)
        )

    # -- cover -----------------------------------------------------------------------------------

    async def _load_cover(self, images: ImageCache, url: str | None) -> None:
        face = self._face
        self._show_cover(await placeholder_image(face))
        if url is None:
            return
        cover = await images.get(url, face.size, finish=face)
        if cover is not None:
            self._show_cover(cover)

    def _show_cover(self, image: Image.Image) -> None:
        if not self.winfo_exists():
            return
        self._photo = ImageTk.PhotoImage(image, master=self._sheet)
        self._sheet.itemconfigure(self._cover_item, image=self._photo)
        if not self._sheet.winfo_manager():
            self._sheet.place(x=0, y=0, relwidth=1, relheight=1)

    def destroy(self) -> None:
        if self._cover_task is not None:
            self._cover_task.cancel()
        super().destroy()


def _linespace(font: ctk.CTkFont) -> int:
    space: int = font.metrics("linespace")
    return space
