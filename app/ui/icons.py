"""Drawn icons, sharp at any Windows scaling: lines drawn by Pillow at the button's physical size
(a few times larger, then scaled down, so they are anti-aliased), once in the light and once in
the dark look's colour (`CTkImage` shows the one that fits and follows a switch by itself).

`IconButton` is a CTkButton showing such an icon; it draws it again when Windows' scaling
changes (the window moved to another screen). The header's settings button is one ("sliders":
three vertical lines with a knob each).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import customtkinter as ctk
from PIL import Image, ImageColor, ImageDraw

from app.ui import theme

RGB = tuple[int, int, int]
Segment = tuple[tuple[float, float], tuple[float, float]]
DrawIcon = Callable[[int, RGB], Image.Image]

VIEWBOX = 24  # the icons' own units: a 24 × 24 box
STROKE = 2.0  # line width in those units
_SUPERSAMPLE = 4

# "Sliders": three vertical lines, each broken by a short horizontal knob at another height.
SLIDERS: tuple[Segment, ...] = (
    ((4, 21), (4, 14)),
    ((4, 10), (4, 3)),
    ((12, 21), (12, 12)),
    ((12, 8), (12, 3)),
    ((20, 21), (20, 16)),
    ((20, 12), (20, 3)),
    ((1, 14), (7, 14)),
    ((9, 8), (15, 8)),
    ((17, 16), (23, 16)),
)


def draw_lines(segments: tuple[Segment, ...], size_px: int, color: RGB) -> Image.Image:
    """`segments` (in VIEWBOX units) as STROKE-wide lines with round ends in `color`, on a
    transparent `size_px` square (RGBA)."""
    big = size_px * _SUPERSAMPLE
    scale = big / VIEWBOX
    width = max(1.0, STROKE * scale)
    radius = width / 2
    image = Image.new("RGBA", (big, big), (*color, 0))
    draw = ImageDraw.Draw(image)
    fill = (*color, 255)
    for (x1, y1), (x2, y2) in segments:
        start, end = (x1 * scale, y1 * scale), (x2 * scale, y2 * scale)
        draw.line((start, end), fill=fill, width=round(width))
        for x, y in (start, end):  # round ends
            draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill)
    return image.resize((size_px, size_px), Image.Resampling.BOX)


def sliders_icon(size_px: int, color: RGB) -> Image.Image:
    """The settings icon: three sliders (SLIDERS), `size_px` square, RGBA."""
    return draw_lines(SLIDERS, size_px, color)


def _rgb(color: str) -> RGB:
    red, green, blue = ImageColor.getrgb(color)[:3]
    return red, green, blue


class IconButton(ctk.CTkButton):
    """A button showing `draw`'s icon, `icon_size` units square, in `color`'s light and dark
    halves (BUTTON_TEXT: the colour of a button's text without a colour fill)."""

    def __init__(
        self,
        master: Any,
        *,
        draw: DrawIcon,
        icon_size: int = VIEWBOX,
        color: theme.Pair = theme.BUTTON_TEXT,
        **kwargs: Any,
    ) -> None:
        self._draw_icon: DrawIcon | None = None
        super().__init__(master, text="", **kwargs)
        self._draw_icon = draw
        self._icon_size = icon_size
        self._icon_color = color
        self.icon: ctk.CTkImage | None = None
        self._render()

    @property
    def icon_pixels(self) -> int:
        """The icon's side in physical pixels at the current scaling."""
        scaling: float = ctk.ScalingTracker.get_widget_scaling(self)
        return round(self._icon_size * scaling)

    def _render(self) -> None:
        if self._draw_icon is None:
            return
        side = self.icon_pixels
        light, dark = (self._draw_icon(side, _rgb(color)) for color in self._icon_color)
        self.icon = ctk.CTkImage(
            light_image=light, dark_image=dark, size=(self._icon_size, self._icon_size)
        )
        self.configure(image=self.icon)

    def _set_scaling(self, *args: Any, **kwargs: Any) -> None:
        """Windows' scaling changed: CTk resizes the button; the icon is drawn again at the new
        physical size (CTkImage would only stretch the old one)."""
        super()._set_scaling(*args, **kwargs)
        self._render()
