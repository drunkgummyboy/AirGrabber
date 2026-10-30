"""Settings → Looks → Card size: a real poster card at the size being chosen, and a dashed
outline of the size saved now ("S (now)") while they differ.

The card is a `PosterCard` (the pages' own drawing code), so it looks exactly as on the pages:
a followed show's card with "✓ Following". Its cover is one of the library's posters (most likely
in the poster cache already) once `use_show` names a show, the app's placeholder card with
"Your show" until then or without one. A click on a size draws the card again at once (its
cover comes from ImageCache's memory then).

The preview keeps the room of the largest card from the start, so the page doesn't jump.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import customtkinter as ctk
from PIL import Image, ImageColor, ImageDraw

from app.i18n import N_, t
from app.models import CardSize, Show
from app.ui import theme
from app.ui.card_scale import CARD_SIZE_LABELS, POSTER_WIDTHS, CardScale
from app.ui.cards import tvmaze_poster_url
from app.ui.poster_card import CORNER_RADIUS, PosterCard, TrackButton, card_size, year_rating_line

if TYPE_CHECKING:
    from app.services import ImageCache

RGB = tuple[int, int, int]

PLACEHOLDER_TITLE = N_("Your show")
NOW_TEXT = N_("{size} (now)")
GAP = 16  # between the card and the outline
LABEL_HEIGHT = 22  # the "S (now)" line under the outline
DASH = 6  # an outline dash and the gap after it, in CTk units
DASH_GAP = 4
DASH_WIDTH = 1.5

_SUPERSAMPLE = 4
_LARGEST = max(POSTER_WIDTHS, key=lambda size: POSTER_WIDTHS[size])


def preview_room() -> tuple[int, int]:
    """The (width, height) the preview keeps, in CTk units: the largest card and an outline as
    wide beside it, with the label under them."""
    width, height = card_size(CardScale.of(_LARGEST))
    return 2 * width + GAP, height + LABEL_HEIGHT


def outline_label(chosen: CardSize, saved: CardSize) -> str | None:
    """"S (now)" under the outline of the saved size while another one is chosen, else None (no
    outline)."""
    if chosen == saved:
        return None
    return t(NOW_TEXT, size=CARD_SIZE_LABELS[saved])


def _rgb(color: str) -> RGB:
    red, green, blue = ImageColor.getrgb(color)[:3]
    return red, green, blue


def dashed_outline(size: tuple[int, int], radius: int, line: float, dash: float, gap: float,
                   color: RGB) -> Image.Image:
    """A rounded rectangle's dashed outline, `size` physical pixels, RGBA (see-through inside
    and out): dashes `dash` long with `gap` between them, `line` wide, along the whole edge."""
    width, height = size
    scale = _SUPERSAMPLE
    big = Image.new("RGBA", (width * scale, height * scale), (*color, 0))
    draw = ImageDraw.Draw(big)
    half = line * scale / 2
    r = max(0.0, radius * scale - half)
    x0, y0 = half, half
    x1, y1 = width * scale - half, height * scale - half
    points = _rounded_path(x0, y0, x1, y1, r, step=1.0)
    period = (dash + gap) * scale
    on = dash * scale
    fill = (*color, 255)
    for distance, (x, y) in points:
        if distance % period < on:
            draw.ellipse((x - half, y - half, x + half, y + half), fill=fill)
    return big.resize(size, Image.Resampling.BOX)


def _rounded_path(
    x0: float, y0: float, x1: float, y1: float, r: float, *, step: float
) -> list[tuple[float, tuple[float, float]]]:
    """Points `step` apart along a rounded rectangle's edge, clockwise from the top-left
    corner's end, each with its distance along the edge."""
    pieces: list[tuple[str, tuple[float, ...]]] = [
        ("line", (x0 + r, y0, x1 - r, y0)),
        ("arc", (x1 - r, y0 + r, -90.0)),
        ("line", (x1, y0 + r, x1, y1 - r)),
        ("arc", (x1 - r, y1 - r, 0.0)),
        ("line", (x1 - r, y1, x0 + r, y1)),
        ("arc", (x0 + r, y1 - r, 90.0)),
        ("line", (x0, y1 - r, x0, y0 + r)),
        ("arc", (x0 + r, y0 + r, 180.0)),
    ]
    points: list[tuple[float, tuple[float, float]]] = []
    travelled = 0.0
    for kind, values in pieces:
        if kind == "line":
            ax, ay, bx, by = values
            length = math.hypot(bx - ax, by - ay)
            count = max(1, int(length / step))
            for i in range(count):
                share = i / count
                points.append((travelled + share * length, (ax + (bx - ax) * share, ay + (by - ay) * share)))
        else:
            cx, cy, start = values
            length = math.pi * r / 2
            count = max(1, int(length / step))
            for i in range(count):
                share = i / count
                angle = math.radians(start + 90 * share)
                points.append((travelled + share * length, (cx + r * math.cos(angle), cy + r * math.sin(angle))))
        travelled += length
    return points


class _PreviewCard(PosterCard):
    """A followed show's poster card ("✓ Following"), as Discover and the search results draw
    it."""

    def __init__(self, master: Any, *, show: Show | None, images: ImageCache, scale: CardScale) -> None:
        if show is not None:
            title = show.name
            meta = year_rating_line(show.year, show.rating)
            poster_show = show

            def poster(width: int) -> str | None:
                return tvmaze_poster_url(poster_show, width)

        else:
            title, meta = t(PLACEHOLDER_TITLE), ""

            def poster(width: int) -> str | None:
                return None

        super().__init__(
            master, title=title, meta=meta, poster=poster, images=images, scale=scale, meta_row=True
        )
        button = TrackButton(self, busy_text="", command=lambda: None, scale=scale)
        button.set_state("tracked")
        self._pack_action(button)


class CardSizePreview(ctk.CTkFrame):
    """The card at the chosen size and the saved size's dashed outline (`show`)."""

    def __init__(self, master: Any, *, images: ImageCache, chosen: CardSize, saved: CardSize) -> None:
        super().__init__(master, fg_color="transparent")
        width, height = preview_room()
        self._room = ctk.CTkFrame(self, fg_color="transparent", width=width, height=height)
        self._room.pack(anchor="w")
        self._room.pack_propagate(False)
        self._room.grid_propagate(False)
        self._images = images
        self._show: Show | None = None
        self._chosen = chosen
        self._saved = saved
        self.card: PosterCard | None = None
        self._outline_image: ctk.CTkImage | None = None
        self.outline = ctk.CTkLabel(self._room, text="")
        self.label = ctk.CTkLabel(
            self._room, text="", font=theme.font(12), text_color=theme.TEXT_FAINT, height=LABEL_HEIGHT
        )
        self._redraw()

    @property
    def chosen(self) -> CardSize:
        return self._chosen

    @property
    def label_text(self) -> str | None:
        return str(self.label.cget("text")) if self.label.winfo_manager() else None

    def use_show(self, show: Show | None) -> None:
        """Draw the card with `show`'s title and poster (None: the placeholder)."""
        if show != self._show:
            self._show = show
            self._redraw()

    def show(self, chosen: CardSize, saved: CardSize | None = None) -> None:
        """The card at `chosen`; the outline of `saved` (default: as it was) when different."""
        saved = self._saved if saved is None else saved
        if (chosen, saved) != (self._chosen, self._saved) or self.card is None:
            self._chosen, self._saved = chosen, saved
            self._redraw()

    def _redraw(self) -> None:
        if self.card is not None:
            self.card.destroy()
        scale = CardScale.of(self._chosen)
        self.card = _PreviewCard(self._room, show=self._show, images=self._images, scale=scale)
        _width, room_height = preview_room()
        card_width, card_height = card_size(scale)
        self.card.place(x=0, y=room_height - LABEL_HEIGHT - card_height)
        label = outline_label(self._chosen, self._saved)
        if label is None:
            self.outline.place_forget()
            self.label.place_forget()
            return
        saved_scale = CardScale.of(self._saved)
        outline_width, outline_height = card_size(saved_scale)
        scaling: float = ctk.ScalingTracker.get_widget_scaling(self)
        physical = (round(outline_width * scaling), round(outline_height * scaling))
        radius = round(saved_scale.px(CORNER_RADIUS) * scaling)

        def bake(color: str) -> Image.Image:
            return dashed_outline(
                physical, radius, DASH_WIDTH * scaling, DASH * scaling, DASH_GAP * scaling, _rgb(color)
            )

        light, dark = theme.TEXT_FAINT
        self._outline_image = ctk.CTkImage(
            light_image=bake(_named(light)), dark_image=bake(_named(dark)), size=(outline_width, outline_height)
        )
        self.outline.configure(image=self._outline_image)
        x = card_width + GAP
        self.outline.place(x=x, y=room_height - LABEL_HEIGHT - outline_height)
        self.label.configure(text=label, width=outline_width)
        self.label.place(x=x, y=room_height - LABEL_HEIGHT)


def _named(color: str) -> str:
    """A Tk colour name ("gray40") as "#rrggbb" for Pillow."""
    if color.startswith("#"):
        return color
    if color.startswith("gray") and color[4:].isdigit():
        level = round(int(color[4:]) * 255 / 100)
        return f"#{level:02x}{level:02x}{level:02x}"
    return color
