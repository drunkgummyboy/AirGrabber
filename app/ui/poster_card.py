"""Tall poster cards: the cover fills a 2:3 card and fades into `theme.BG` at the bottom, where
the title, a "2024 | ★ 7.8" line and an action row (Download, + Track) sit.

Tk can't draw a see-through widget over an image, so the cover, the fade, the border and the
rounded corners are baked into one image at the card's physical size, in a worker thread
(`compose_card`, cached by `ImageCache` per url and size). The fade turns solid just above the
text, so the labels and buttons, whose own background is `theme.BG`, blend in.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Literal

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageOps

from app.ui import theme
from app.ui.download_control import CORNER_RADIUS as ROW_CORNER_RADIUS
from app.ui.download_control import FONT_SIZE as ROW_FONT_SIZE
from app.ui.download_control import ROW_HEIGHT
from app.ui.tasks import spawn
from app.ui.text_fit import SLACK, elide_to_width, fit_to_lines, title_lines

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
TEXT_WIDTH = POSTER_CARD_WIDTH - 2 * (BORDER + SIDE_PAD)
TITLE_SIZE = 15
TITLE_MAX_LINES = 2
META_SIZE = 12
# The fade covers the bottom 55 % of the cover: soft at first, 85 % opaque a little under half
# way, solid SOLID_MARGIN above the text.
FADE_FROM = 0.45
FADE_KNEE = (0.45, 0.85)
SOLID_MARGIN = 4

FOLLOWING_TEXT = "✓ Following"
TRACK_TEXT = "+ Track"
MISSING_TEXT = "Not on TVMaze"

_SUPERSAMPLE = 4  # the rounded corners are drawn this much larger, then scaled down


# -- text ------------------------------------------------------------------------------------


def year_rating_line(year: int | None, rating: float | None) -> str:
    """"2024 | ★ 7.8", leaving out whichever is unknown ("" for neither)."""
    parts: list[str] = []
    if year:
        parts.append(str(year))
    if rating is not None:
        parts.append(f"★ {rating:.1f}")
    return " | ".join(parts)


# -- the baked image -------------------------------------------------------------------------


def fade_alpha(position: float) -> float:
    """The fade's opacity at `position`: 0 where it starts, 1 where it turns solid. Smooth from
    0 up to 85 % at 0.45 (no visible edge where it starts), then straight on to 100 %."""
    if position <= 0:
        return 0.0
    if position >= 1:
        return 1.0
    knee, knee_alpha = FADE_KNEE
    if position < knee:
        t = position / knee
        return knee_alpha * t * t * (3 - 2 * t)
    return knee_alpha + (1 - knee_alpha) * (position - knee) / (1 - knee)


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
    solid_from: int  # first row of solid `bg`
    bg: RGB
    edge: RGB  # the border
    outside: RGB  # behind the rounded corners: what the card sits on
    placeholder: RGB  # the cover until it has loaded, or for a title without one

    def __call__(self, cover: Image.Image, /) -> Image.Image:
        return compose_card(cover, self)


def card_face(
    scale: float, text_height: float, *, bg: RGB, edge: RGB, outside: RGB, placeholder: RGB
) -> CardFace:
    """The face of a card at widget scaling `scale` whose text and action row take
    `text_height` CTk units above the bottom padding: the fade starts at 45 % of the card's
    height and is solid from SOLID_MARGIN above the text down."""
    width, height = round(POSTER_CARD_WIDTH * scale), round(POSTER_CARD_HEIGHT * scale)
    solid_from = height - round((BORDER + BOTTOM_PAD + text_height + SOLID_MARGIN) * scale)
    solid_from = max(0, solid_from)
    return CardFace(
        size=(width, height),
        border=max(1, round(BORDER * scale)),
        radius=round(CORNER_RADIUS * scale),
        fade_from=min(round(height * FADE_FROM), solid_from),
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
    faded = Image.composite(shade, base, _fade_mask(face.size, face.fade_from, face.solid_from))
    card = Image.new("RGB", face.size, face.outside)
    card.paste(face.edge, (0, 0, width, height), _rounded_mask(face.size, face.radius, 0))
    inner = _rounded_mask(face.size, max(0, face.radius - face.border), face.border)
    card.paste(faded, (0, 0), inner)
    return card


@lru_cache(maxsize=16)
def _fade_mask(size: tuple[int, int], fade_from: int, solid_from: int) -> Image.Image:
    """An L mask, one value per row: 0 above `fade_from`, `fade_alpha` down to `solid_from`,
    255 from there. Shared between calls: read-only."""
    width, height = size
    span = max(1, solid_from - fade_from)
    column = bytes(
        255 if row >= solid_from else round(255 * fade_alpha((row + 0.5 - fade_from) / span))
        for row in range(height)
    )
    return Image.frombytes("L", (1, height), column).resize(size, Image.Resampling.NEAREST)


@lru_cache(maxsize=16)
def _rounded_mask(size: tuple[int, int], radius: int, inset: int) -> Image.Image:
    """An anti-aliased L mask of a rounded rectangle `inset` pixels inside `size`. Shared
    between calls: read-only."""
    width, height = size
    scale = _SUPERSAMPLE
    big = Image.new("L", (width * scale, height * scale), 0)
    box = (inset * scale, inset * scale, (width - inset) * scale - 1, (height - inset) * scale - 1)
    ImageDraw.Draw(big).rounded_rectangle(box, radius=radius * scale, fill=255)
    return big.resize(size, Image.Resampling.BOX)


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
    color: str
    hover: str
    enabled: bool
    border: str | None = None  # a 1-unit border in this colour
    text_color: str | None = None  # None = the button's default; disabled text included


def track_look(state: TrackState, busy_text: str) -> TrackLook:
    """idle → "+ Track"; busy → `busy_text`; tracked → "✓ Following" in SUCCESS with a SUCCESS
    border; missing → "Not on TVMaze"."""
    if state == "idle":
        return TrackLook(TRACK_TEXT, theme.ACCENT, theme.ACCENT_HOVER, enabled=True)
    if state == "busy":
        return TrackLook(busy_text, theme.ACCENT, theme.ACCENT_HOVER, enabled=False)
    if state == "tracked":
        return TrackLook(
            FOLLOWING_TEXT,
            "transparent",
            theme.ACCENT_HOVER,
            enabled=False,
            border=theme.SUCCESS,
            text_color=theme.SUCCESS,
        )
    return TrackLook(MISSING_TEXT, theme.DANGER_BUTTON, theme.DANGER_BUTTON_HOVER, enabled=False)


class TrackButton(ctk.CTkButton):
    """A show card's action row: "+ Track", `busy_text` while adding, "✓ Following" once
    tracked (see `track_look`)."""

    def __init__(self, master: Any, *, busy_text: str, command: Callable[[], None]) -> None:
        super().__init__(
            master,
            text="",
            height=ROW_HEIGHT,
            corner_radius=ROW_CORNER_RADIUS,
            font=theme.font(ROW_FONT_SIZE, bold=True),
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


def _rgb(widget: Any, color: str) -> RGB:
    red, green, blue = widget.winfo_rgb(color)  # 16 bits per channel
    return (red >> 8, green >> 8, blue >> 8)


class PosterCard(ctk.CTkFrame):
    """A 2:3 card: the cover with the title, the "2024 | ★ 7.8" line (`meta`, left out when
    empty) and an action row on its faded bottom.

    `poster` gives the cover's url for a card that many physical pixels wide (None: no cover).
    Subclasses create their action row with this card as master and hand it to `_pack_action`;
    `_set_open` sets what a click on the cover or the text does.
    """

    def __init__(
        self,
        master: Any,
        *,
        title: str,
        meta: str,
        poster: Callable[[int], str | None],
        images: ImageCache,
    ) -> None:
        super().__init__(
            master,
            fg_color=theme.BG,
            border_color=theme.EDGE,
            border_width=BORDER,
            corner_radius=CORNER_RADIUS,
            width=POSTER_CARD_WIDTH,
            height=POSTER_CARD_HEIGHT,
        )
        self.grid_propagate(False)
        self.pack_propagate(False)
        self._open: Callable[[], object] | None = None
        self._cover_image: ctk.CTkImage | None = None

        # Created first so the text and buttons stack above it; placed once it has an image.
        self._cover = ctk.CTkLabel(
            self, text="", width=POSTER_CARD_WIDTH, height=POSTER_CARD_HEIGHT
        )
        self._text = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0, border_width=0)
        self._text.pack(side="bottom", fill="x", padx=BORDER + SIDE_PAD, pady=(0, ROW_GAP))
        title_font = theme.font(TITLE_SIZE, bold=True)
        fitted = fit_to_lines(
            title_lines(title), TEXT_WIDTH - SLACK, TITLE_MAX_LINES, title_font.measure
        )
        self._title = ctk.CTkLabel(
            self._text,
            text=fitted,
            height=0,
            font=title_font,
            text_color=theme.TEXT,
            justify="center",
        )
        self._title.pack(fill="x")
        clickable: list[Any] = [self._cover, self._text, self._title]
        text_height = ROW_HEIGHT + ROW_GAP + (fitted.count("\n") + 1) * _linespace(title_font)
        if meta:
            meta_font = theme.font(META_SIZE)
            line = ctk.CTkLabel(
                self._text,
                text=elide_to_width(meta, TEXT_WIDTH - SLACK, meta_font.measure),
                height=0,
                font=meta_font,
                text_color=theme.TEXT_MUTED,
            )
            line.pack(fill="x", pady=(META_GAP, 0))
            clickable.append(line)
            text_height += META_GAP + _linespace(meta_font)
        for widget in clickable:
            widget.bind("<Button-1>", self._clicked, add="+")

        scale: float = ctk.ScalingTracker.get_widget_scaling(self)
        self._face = card_face(
            scale,
            text_height,
            bg=_rgb(self, theme.BG),
            edge=_rgb(self, theme.EDGE),
            outside=_rgb(self, self._apply_appearance_mode(self.cget("bg_color"))),
            placeholder=_rgb(self, theme.PLACEHOLDER),
        )
        url = poster(self._face.size[0])
        self._cover_task = spawn(self._load_cover(images, url), name="PosterCard.cover")

    # -- action row and opening ----------------------------------------------------------------

    def _pack_action(self, row: Any) -> None:
        """Pack `row` (created with this card as master) as the card's bottom row."""
        row.pack(
            side="bottom",
            fill="x",
            padx=BORDER + SIDE_PAD,
            pady=(0, BORDER + BOTTOM_PAD),
            before=self._text,
        )

    def _set_open(self, action: Callable[[], object] | None) -> None:
        """What a click on the cover or the text does (None: nothing)."""
        self._open = action
        cursor = "hand2" if action is not None else ""
        self._cover.configure(cursor=cursor)  # the image fills the cover label's inner label
        self._text.configure(cursor=cursor)  # the title and year lines inherit it

    def _clicked(self, _event: Any) -> None:
        if self._open is not None:
            self._open()

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
        self._cover_image = ctk.CTkImage(
            light_image=image, dark_image=image, size=(POSTER_CARD_WIDTH, POSTER_CARD_HEIGHT)
        )
        self._cover.configure(image=self._cover_image)
        if not self._cover.winfo_manager():
            self._cover.place(x=0, y=0, relwidth=1, relheight=1)

    def destroy(self) -> None:
        self._cover_task.cancel()
        super().destroy()


def _linespace(font: ctk.CTkFont) -> int:
    space: int = font.metrics("linespace")
    return space
