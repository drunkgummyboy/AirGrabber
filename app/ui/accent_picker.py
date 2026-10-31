"""The accent colour's swatches (Settings → Looks → Accent colour, and Quick setup's Look): a row
of round swatches, one per palette in app/ui/palettes.py's order, the chosen one ringed, each
one's name as its hover tip, and the chosen colour's name after the row.

A swatch is drawn on its own canvas, anti-aliased at Windows' scaling (a CTkButton grows wider
than it is tall), on the colour of what it sits on (`background`); it follows a switch between
light and dark and a change of scaling. Choosing only changes the row and calls `command`: the
host saves it, and the colours change at the next start (theme.apply_accent).
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from typing import Any

import customtkinter as ctk
from PIL import Image, ImageColor, ImageDraw, ImageTk

from app.i18n import t
from app.models import Accent
from app.ui import theme
from app.ui.palettes import PALETTES, accent_name, swatch_color
from app.ui.tooltip import add_tooltip

# Logical (unscaled) CTk units.
DISC = 26  # a swatch's colour
RING_GAP = 2  # between the disc and the chosen one's ring
RING = 2  # the ring's width
SWATCH = DISC + 2 * (RING_GAP + RING)  # a swatch's square, the ring included
SWATCH_GAP = 2  # between two swatches' squares (the discs 10 apart)
NAME_GAP = 12  # before the chosen colour's name
NAME_SIZE = 13

_SUPERSAMPLE = 4


def row_width() -> int:
    """The swatches' width (logical units), the name not included."""
    count = len(PALETTES)
    return count * SWATCH + (count - 1) * SWATCH_GAP


def _rgb(color: str) -> tuple[int, int, int]:
    red, green, blue = ImageColor.getrgb(color)[:3]
    return red, green, blue


def swatch_image(size: int, fill: str, ring: str | None, scaling: float) -> Image.Image:
    """A swatch `size` physical pixels square, RGBA, see-through around it: a `fill` disc and,
    when chosen, a `ring`-coloured ring around it with a gap."""
    big = size * _SUPERSAMPLE
    unit = scaling * _SUPERSAMPLE
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    centre = big / 2
    if ring is not None:  # the whole square's disc in the ring's colour, its inside cleared
        inner = centre - RING * unit
        draw.ellipse((0, 0, big - 1, big - 1), fill=(*_rgb(ring), 255))
        draw.ellipse(
            (centre - inner, centre - inner, centre + inner - 1, centre + inner - 1),
            fill=(0, 0, 0, 0),
        )
    radius = DISC / 2 * unit
    draw.ellipse(
        (centre - radius, centre - radius, centre + radius - 1, centre + radius - 1),
        fill=(*_rgb(fill), 255),
    )
    return image.resize((size, size), Image.Resampling.LANCZOS)


class _Swatch(tk.Canvas):
    """One palette's round swatch; a click (let go on it) chooses it."""

    def __init__(
        self,
        master: Any,
        accent: Accent,
        *,
        background: theme.Color,
        command: Callable[[Accent], None],
    ) -> None:
        super().__init__(master, highlightthickness=0, borderwidth=0, cursor="hand2")
        self.accent = accent
        self._background = background
        self._command = command
        self._chosen = False
        self._pressed = False
        self._photo: ImageTk.PhotoImage | None = None
        self._item = self.create_image(0, 0, anchor="nw")
        self.bind("<ButtonPress-1>", self._press, add="+")
        self.bind("<ButtonRelease-1>", self._release, add="+")
        add_tooltip(self, t(accent_name(accent)))
        self.redraw()
        theme.on_mode_change(self, self.redraw)
        ctk.ScalingTracker.add_widget(self._rescaled, self)
        self.bind("<Destroy>", self._gone, add="+")

    @property
    def chosen(self) -> bool:
        return self._chosen

    def set_chosen(self, chosen: bool) -> None:
        if chosen != self._chosen:
            self._chosen = chosen
            self.redraw()

    def redraw(self) -> None:
        scaling = float(ctk.ScalingTracker.get_widget_scaling(self))
        size = max(1, round(SWATCH * scaling))
        self.configure(width=size, height=size, background=theme.pick(self._background))
        ring = theme.pick(theme.TEXT) if self._chosen else None
        image = swatch_image(size, swatch_color(self.accent), ring, scaling)
        self._photo = ImageTk.PhotoImage(image, master=self)
        self.itemconfigure(self._item, image=self._photo)

    def invoke(self) -> None:
        self._command(self.accent)

    def _press(self, _event: Any) -> None:
        self._pressed = True

    def _release(self, event: Any) -> None:
        pressed, self._pressed = self._pressed, False
        inside = 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height()
        if pressed and inside:
            self.invoke()

    def _rescaled(self, _widget_scaling: float, _window_scaling: float) -> None:
        if self.winfo_exists():
            self.redraw()

    def _gone(self, event: Any) -> None:
        if event.widget is self:
            ctk.ScalingTracker.remove_widget(self._rescaled, self)


class AccentSwatches(ctk.CTkFrame):
    """The swatches and the chosen colour's name (see the module docstring). `background`: the
    colour the row sits on; `command(accent)` after a click chose another one."""

    def __init__(
        self,
        master: Any,
        *,
        value: Accent,
        background: theme.Color,
        command: Callable[[Accent], None] | None = None,
    ) -> None:
        super().__init__(master, fg_color="transparent")
        self._value = value
        self._command = command
        scaling: float = ctk.ScalingTracker.get_widget_scaling(self)
        self.swatches: dict[Accent, _Swatch] = {}
        for index, accent in enumerate(PALETTES):
            swatch = _Swatch(self, accent, background=background, command=self._chosen)
            # Plain Tk geometry isn't scaled by CTk.
            swatch.pack(side="left", padx=(0 if index == 0 else round(SWATCH_GAP * scaling), 0))
            self.swatches[accent] = swatch
        self.name = ctk.CTkLabel(
            self,
            text="",
            font=theme.font(NAME_SIZE),
            text_color=theme.TEXT_MUTED,
            anchor="w",
        )
        self.name.pack(side="left", padx=(NAME_GAP, 0))
        self._paint()

    @property
    def value(self) -> Accent:
        return self._value

    def set(self, accent: Accent) -> None:
        """Show `accent` as the chosen one (without calling `command`)."""
        self._value = accent
        self._paint()

    def _chosen(self, accent: Accent) -> None:
        changed = accent is not self._value
        self.set(accent)
        if changed and self._command is not None:
            self._command(accent)

    def _paint(self) -> None:
        for accent, swatch in self.swatches.items():
            swatch.set_chosen(accent is self._value)
        self.name.configure(text=t(accent_name(self._value)))
