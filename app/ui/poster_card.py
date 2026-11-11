"""Tall poster cards: the cover fills a 2:3 card and fades into `theme.BG` at the bottom, where
the title, a "2024 · ★ 7.8" line and the action row sit: one row per card, a wide main button
(Download, + Follow / ✓ Following) with at most one round button beside it (cards.RoundButton).
Pills in the cover's top-left corner (`_set_badges`, side by side): amber for something missing,
blue (`theme.BRAND`) for information such as "Auto".

Tk can't draw a see-through widget over an image, so the cover, the fade, the border and the
rounded corners are baked into one image at the card's physical size, in a worker thread
(`compose_card`, cached by `ImageCache` per url and size), and shown on a canvas that fills the
card. The title and the line under it are text on that canvas, over the fade's lower part: the
fade starts high up the cover (FADE_SHARE of it above the text's lower edge), eases in and
turns solid under the text (FADE_STOPS, no visible edge anywhere). The rows of widgets under
the text (buttons, switches) have `theme.BG` as their own background, on the solid part.

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
from app.ui.tooltip import Tooltip

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
# The fade into the card's colour covers the lower FADE_SHARE of the cover above the text's
# lower edge (the "poster" over the solid card body), solid from there down. FADE_STOPS: (share
# of the way down the fade, how much of the card's colour lies over the cover), passed through
# by a smooth curve that starts and ends flat (`fade_alpha`), so it eases in with no line where
# it starts or where it turns solid.
FADE_SHARE = 0.58
FADE_STOPS: tuple[tuple[float, float], ...] = (
    (0.0, 0.0),
    (0.25, 0.18),
    (0.5, 0.5),
    (0.75, 0.82),
    (1.0, 1.0),
)
# Badges in the cover's top-left corner ("3 missing", "Auto" on My shows): pills on `theme.BG`,
# side by side.
BADGE_INSET = 10  # from the card's outer edge
BADGE_HEIGHT = 22
BADGE_PAD = 8  # inside the pill, left and right of the text
BADGE_FONT = 12
BADGE_GAP = 6  # between two pills
CORNER_ICON = 26  # the picture on the cover's top-right corner (`set_corner_icon`), square

# Shown through t() (`track_look`).
FOLLOWING_TEXT = N_("✓ Following")
TRACK_TEXT = N_("+ Follow")
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
    """"2024 · ★ 7.8", leaving out whichever is unknown ("" for neither)."""
    parts: list[str] = []
    if year:
        parts.append(str(year))
    if rating is not None:
        parts.append(f"★ {number(rating, '.1f')}")
    return " · ".join(parts)


# -- the baked image -------------------------------------------------------------------------


def _stop_slopes(stops: Sequence[tuple[float, float]]) -> tuple[float, ...]:
    """The curve's slope at each stop: flat at both ends, inside the harmonic mean of the two
    straight slopes beside it (0 where they turn): it never overshoots a stop."""
    secants = [
        (y1 - y0) / (x1 - x0) for (x0, y0), (x1, y1) in zip(stops, stops[1:], strict=False)
    ]
    slopes = [0.0]
    for before, after in zip(secants, secants[1:], strict=False):
        same_way = before * after > 0
        slopes.append(2 / (1 / before + 1 / after) if same_way else 0.0)
    slopes.append(0.0)
    return tuple(slopes)


_FADE_SLOPES = _stop_slopes(FADE_STOPS)


def fade_alpha(position: float) -> float:
    """The fade's opacity at `position` (0 where it starts, 1 where it turns solid): through
    FADE_STOPS on a smooth curve (cubic between the stops), flat at both ends."""
    if position <= 0:
        return 0.0
    if position >= 1:
        return 1.0
    for index, ((x0, y0), (x1, y1)) in enumerate(zip(FADE_STOPS, FADE_STOPS[1:], strict=False)):
        if position <= x1:
            span = x1 - x0
            t = (position - x0) / span
            m0, m1 = _FADE_SLOPES[index] * span, _FADE_SLOPES[index + 1] * span
            t2, t3 = t * t, t * t * t
            return (
                (2 * t3 - 3 * t2 + 1) * y0
                + (t3 - 2 * t2 + t) * m0
                + (-2 * t3 + 3 * t2) * y1
                + (t3 - t2) * m1
            )
    return 1.0


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
    solid_from: int  # first row of solid `bg` (under the text: the widget rows and their gap)
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
    title and the line under it) sits above widget rows of `rows_height` units (None: one
    action row) on the bottom padding, ROW_GAP above them.

    The fade turns solid at the text's lower edge and starts FADE_SHARE of the cover above it
    (the cover from the card's top down to there), so a card with more rows under its text
    fades higher up. The text's height (`text_height`, CTk units) doesn't move it: one or two
    title lines sit on the same fade. In physical pixels (DPI factor x card scale), so the
    cover stays sharp at every size."""
    units_w, units_h = card_size(card)
    width, height = round(units_w * scale), round(units_h * scale)
    border = border_width(card)
    rows = row_height(card) if rows_height is None else rows_height
    text_bottom = border + card.px(BOTTOM_PAD) + rows + card.px(ROW_GAP)  # from the bottom edge
    solid_from = max(0, height - round(text_bottom * scale))
    return CardFace(
        size=(width, height),
        border=max(1, round(border * scale)),
        radius=round(card.px(CORNER_RADIUS) * scale),
        fade_from=max(0, solid_from - round(solid_from * FADE_SHARE)),
        solid_from=solid_from,
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
    mask = _fade_mask(face.size, face.fade_from, face.solid_from)
    faded = Image.composite(shade, base, mask)
    card = Image.new("RGB", face.size, face.outside)
    card.paste(face.edge, (0, 0, width, height), _rounded_mask(face.size, face.radius, 0))
    inner = _rounded_mask(face.size, max(0, face.radius - face.border), face.border)
    card.paste(faded, (0, 0), inner)
    return card


def fade_row_alpha(row: int, fade_from: int, solid_from: int) -> float:
    """The fade's opacity on pixel row `row` (see `CardFace`): 0 above `fade_from`,
    `fade_alpha` on the way down, 1 from `solid_from`."""
    if row >= solid_from:
        return 1.0
    if row < fade_from:
        return 0.0
    return fade_alpha((row + 0.5 - fade_from) / max(1, solid_from - fade_from))


@lru_cache(maxsize=16)
def _fade_mask(size: tuple[int, int], fade_from: int, solid_from: int) -> Image.Image:
    """An L mask, one value per row (`fade_row_alpha`). Shared between calls: read-only."""
    width, height = size
    column = bytes(
        round(255 * fade_row_alpha(row, fade_from, solid_from)) for row in range(height)
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


# -- the cover's pills --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Badge:
    """A pill in the cover's top-left corner: `text` in `color` (its outline too), `tip` under
    it on hover ("" = none)."""

    text: str
    color: theme.Color = theme.TEXT
    tip: str = ""


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
    """idle → "+ Follow"; busy → `busy_text`; tracked → "✓ Following" in SUCCESS with a SUCCESS
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
    """A show card's action row: "+ Follow", `busy_text` while adding, "✓ Following" once
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
    """A 2:3 card: the cover with the title, the "2024 · ★ 7.8" line (`meta`; left out when
    empty unless `meta_row`, which keeps its room for `_set_meta`) and an action row on its
    faded bottom.

    `poster` gives the cover's url for a card that many physical pixels wide (None: no cover).
    Subclasses create their action row with this card as master and hand it to `_pack_action`;
    `_set_open` sets what a click on the cover or the text does. A subclass with a row between
    the text and the action row (`_pack_above_action`) passes its height, gap included, as
    `extra_height` (already at `scale`), so the fade turns solid behind that row too.
    `_set_badges` puts pills side by side in the cover's top-left corner (`_set_badge`: one),
    each with an optional hover tip; `set_corner_icon` a small picture (see-through around it)
    on its top-right corner, with a hover tip. `_follow_scale(redraw)`: a plain Tk widget on the
    card (a RoundButton) redrawn with it at a new Windows scaling.

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
        self._badges: tuple[Badge, ...] = ()
        self._badge_font = theme.font(scale.font(BADGE_FONT, TEXT_MIN), bold=True)
        self._badge_items: list[tuple[int, int]] = []  # per pill: its image, its text
        self._badge_photos: list[ImageTk.PhotoImage] = []
        self._badge_tip: Tooltip | None = None
        self._rescales: list[Callable[[], None]] = []  # `_follow_scale`
        # The corner picture: how to draw it at a size (physical pixels) and its tip.
        self._corner: tuple[Callable[[int], Image.Image], str] | None = None
        self._corner_item: int | None = None
        self._corner_photo: ImageTk.PhotoImage | None = None
        self._corner_tip: Tooltip | None = None
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
        self._draw_corner()
        for redraw in self._rescales:
            redraw()

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

    def _follow_scale(self, redraw: Callable[[], None]) -> None:
        """Call `redraw` whenever the card is laid out again at a new scaling (a plain Tk widget
        on it, such as a RoundButton, which CTk doesn't scale)."""
        self._rescales.append(redraw)

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
        """The first pill's text; None without pills."""
        return self._badges[0].text if self._badges else None

    @property
    def badges(self) -> tuple[Badge, ...]:
        """The pills in the cover's corner, left to right."""
        return self._badges

    def _set_badge(self, text: str | None, color: theme.Color = theme.TEXT) -> None:
        """One pill with `text` in `color` (outline and text, on `theme.BG`) in the cover's
        top-left corner; None takes the pills away."""
        self._set_badges([Badge(text, color)] if text else [])

    def _set_badges(self, badges: Sequence[Badge]) -> None:
        """Pills side by side in the cover's top-left corner, the first in the corner; none
        takes them away. Unchanged pills aren't drawn again."""
        shown = tuple(badge for badge in badges if badge.text)
        if shown != self._badges:
            self._badges = shown
            self._draw_badge()

    def _draw_badge(self) -> None:
        canvas = self._sheet
        badges = self._badges
        while len(self._badge_items) > len(badges):
            canvas.delete(*self._badge_items.pop())
        while len(self._badge_items) < len(badges):
            index = len(self._badge_items)
            items = (
                canvas.create_image(0, 0, anchor="nw"),
                canvas.create_text(0, 0, anchor="center"),
            )
            for item in items:
                canvas.tag_bind(item, "<Enter>", self._badge_enter(index), add="+")
                canvas.tag_bind(item, "<Leave>", self._badge_left, add="+")
            self._badge_items.append(items)
        scaling = self.scaling
        scale = self.scale
        font = self._badge_font
        height = scale.text_height(BADGE_HEIGHT, int(font.cget("size")))
        x = y = round(scale.px(BADGE_INSET) * scaling)
        gap = round(scale.px(BADGE_GAP) * scaling)
        border, bg = max(1, round(scaling)), rgb(self, theme.BG)
        photos: list[ImageTk.PhotoImage] = []
        for badge, (image_item, text_item) in zip(badges, self._badge_items, strict=True):
            width = font.measure(badge.text) + 2 * scale.px(BADGE_PAD)
            size = (round(width * scaling), round(height * scaling))
            pill = pill_image(size, border, bg, rgb(self, badge.color))
            photo = ImageTk.PhotoImage(pill, master=canvas)
            photos.append(photo)
            canvas.coords(image_item, x, y)
            canvas.itemconfigure(image_item, image=photo)
            canvas.coords(text_item, x + size[0] / 2, y + size[1] / 2)
            canvas.itemconfigure(
                text_item,
                text=badge.text,
                fill=theme.pick(badge.color),
                font=font.create_scaled_tuple(scaling),
            )
            x += size[0] + gap
        self._badge_photos = photos

    def _badge_enter(self, index: int) -> Callable[[Any], None]:
        return lambda _event: self._badge_entered(index)

    def _badge_entered(self, index: int) -> None:
        if index >= len(self._badges) or not self._badges[index].tip:
            return
        canvas = self._sheet
        if self._badge_tip is None:
            self._badge_tip = Tooltip(canvas, "")
        box = canvas.bbox(self._badge_items[index][0])
        if box is None:
            return
        left, top = canvas.winfo_rootx(), canvas.winfo_rooty()
        area = (left + box[0], top + box[1], left + box[2], top + box[3])
        self._badge_tip.rest_on(area, self._badges[index].tip)

    def _badge_left(self, _event: Any) -> None:
        if self._badge_tip is not None:
            self._badge_tip.rest_on(None, "")

    @property
    def corner_tip(self) -> str | None:
        """The corner picture's tip; None without a picture."""
        return self._corner[1] if self._corner is not None else None

    def set_corner_icon(self, icon: Callable[[int], Image.Image] | None, tip: str = "") -> None:
        """`icon(px)`: a picture `px` physical pixels square (RGBA: see-through around it) on the
        cover's top-right corner, `tip` under it on hover; None takes it away."""
        self._corner = (icon, tip) if icon is not None else None
        self._draw_corner()

    def _draw_corner(self) -> None:
        canvas = self._sheet
        if self._corner is None:
            if self._corner_item is not None:
                canvas.delete(self._corner_item)
            self._corner_item = None
            self._corner_photo = None
            return
        icon, _tip = self._corner
        scaling, scale = self.scaling, self.scale
        side = round(scale.px(CORNER_ICON) * scaling)
        inset = round(scale.px(BADGE_INSET) * scaling)
        x = round(self._units[0] * scaling) - inset - side + round(2 * scaling)
        self._corner_photo = ImageTk.PhotoImage(icon(side), master=canvas)
        if self._corner_item is None:
            self._corner_item = canvas.create_image(0, 0, anchor="nw")
            canvas.tag_bind(self._corner_item, "<Enter>", self._corner_entered, add="+")
            canvas.tag_bind(self._corner_item, "<Leave>", self._corner_left, add="+")
            canvas.tag_bind(self._corner_item, "<Button-1>", self._clicked, add="+")
        canvas.coords(self._corner_item, x, inset - round(2 * scaling))
        canvas.itemconfigure(self._corner_item, image=self._corner_photo)

    def _corner_entered(self, _event: Any) -> None:
        if self._corner is None or self._corner_item is None:
            return
        canvas = self._sheet
        if self._corner_tip is None:
            self._corner_tip = Tooltip(canvas, "")
        box = canvas.bbox(self._corner_item)
        if box is None:
            return
        left, top = canvas.winfo_rootx(), canvas.winfo_rooty()
        area = (left + box[0], top + box[1], left + box[2], top + box[3])
        self._corner_tip.rest_on(area, self._corner[1])

    def _corner_left(self, _event: Any) -> None:
        if self._corner_tip is not None:
            self._corner_tip.rest_on(None, "")

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
