"""The preset card's option buttons and the Forge button's progress bar, drawn on small canvases
in the Forge look.

`OptionButton`: an on / off button with a small round light before its label (the owner's style
D, "Buttons with a light", mockups/forge-option-buttons.html `.td`): the same raised surface
whether on or off, a 1-pixel border, a slight inner shadow at the bottom; off = the light and the
label muted; on = an accent light with a soft glow, the label in the text colour and a warmer
border; under the pointer a slightly different surface. The whole look is decided in one place,
`option_style`, so another style is one function away.

`ForgeProgress`: the Forge button while a run goes: "Renaming 7 of 12 · Mind The Gap" over a
accent bar that fills up. It wears the same dark shade in both looks, so its white text stays
readable over the filled and the empty part alike.

Canvas colours are `theme.pick`ed and repainted on a light/dark switch; sizes follow Windows'
scaling (CTk's `_set_scaling`).
"""

from __future__ import annotations

import math
import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import customtkinter as ctk

from app.forge.ui.look import Pair, app_look, mix
from app.ui import theme

# CTk units (mockups/forge-option-buttons.html `.td`).
OPTION_HEIGHT = 34
OPTION_RADIUS = 8
OPTION_PAD_X = 12
OPTION_LIGHT = 8  # the light's diameter
OPTION_GAP = 8  # between the light and the label
OPTION_FONT = 14
OPTION_SHADOW = 2  # the inner shadow at the bottom
PROGRESS_HEIGHT = 38
PROGRESS_RADIUS = 10
PROGRESS_FONT = 15


@dataclass(frozen=True, slots=True)
class OptionStyle:
    """One state's colours of an option button ("#RRGGBB", picked for the current look)."""

    surface: str
    border: str
    text: str
    light: str
    glow: bool  # a soft halo around the light
    shadow: str  # the inner shadow along the bottom edge


def option_style(look: theme.Look, *, on: bool, hover: bool) -> OptionStyle:
    """Style D, "Buttons with a light": the one place the option buttons' look is decided."""
    pick, dark = theme.pick, theme.is_dark()
    surface = pick(look.STACK_FAR)
    if hover:  # lighter in dark, a touch deeper in light
        surface = pick(look.STACK_NEAR) if dark else pick(look.CHIP)
    off_border = pick(look.CHIP_HOVER)
    # Off, the light wears the border's colour; on a light surface that would barely show.
    off_light = off_border if dark else mix(pick(theme.TEXT_MUTED), surface, 0.5)
    return OptionStyle(
        surface=surface,
        border=pick(look.STACK_FAR_EDGE) if on else off_border,
        text=pick(theme.TEXT) if on else pick(theme.TEXT_MUTED),
        light=pick(look.BRAND) if on else off_light,
        glow=on,
        shadow=mix(surface, theme.TEXT[0], 0.35 if dark else 0.1),
    )


def _font(widget: tk.Misc, size: int, scale: float, *, bold: bool = False) -> tkfont.Font:
    """AirGrabber's font at physical size (negative = pixels, like CTk's own fonts)."""
    return tkfont.Font(root=widget, font=theme.font(size, bold=bold).create_scaled_tuple(scale))


def round_rect(
    canvas: tk.Canvas, x0: float, y0: float, x1: float, y1: float, radius: float, **options: Any
) -> int:
    """A rounded rectangle on `canvas` (a smoothed polygon)."""
    r = max(0.0, min(radius, (x1 - x0) / 2, (y1 - y0) / 2))
    points = [
        x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
        x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
    ]  # fmt: skip
    return int(canvas.create_polygon(points, smooth=True, **options))


def fit_text(text: str, font: tkfont.Font, width: int) -> str:
    """Cut `text` with an ellipsis so it fits in `width` pixels."""
    if font.measure(text) <= width:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid] + "…") <= width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + "…" if width > 0 else ""


class _CanvasWidget(ctk.CTkFrame):
    """A CTkFrame holding one canvas on `ground` (the colour around it), drawn by `_paint` on a
    light/dark switch, a resize and a change of scaling."""

    def __init__(self, master: Any, *, look: theme.Look | None, ground: Pair | None) -> None:
        self.look: theme.Look = app_look() if look is None else look
        self._ground: Pair = self.look.BG if ground is None else ground
        super().__init__(master, fg_color=self._ground, corner_radius=0)
        self.canvas = tk.Canvas(
            self,
            bg=theme.pick(self._ground),
            width=1,
            height=1,
            highlightthickness=0,
            bd=0,
            takefocus=0,
        )
        # Placed, not packed: the frame keeps the size it is given, whatever the canvas asks.
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.canvas.bind("<Configure>", lambda _event: self._paint())
        theme.on_mode_change(self, self._repaint)

    @property
    def scale(self) -> float:
        return float(self._get_widget_scaling())

    def _px(self, units: float) -> int:
        return round(units * self.scale)

    def _repaint(self) -> None:
        self.canvas.configure(bg=theme.pick(self._ground))
        self._paint()

    def _set_scaling(self, *args: Any, **kwargs: Any) -> None:
        super()._set_scaling(*args, **kwargs)
        self._fit()
        self._paint()

    def _fit(self) -> None:
        """Size the frame for its content (CTk units)."""

    def _paint(self) -> None:
        raise NotImplementedError


class OptionButton(_CanvasWidget):
    """`text` is shown as given (translate it first); `on_toggle` hears every click's new value.
    `ground`: the colour the button sits on (the preset card's)."""

    def __init__(
        self,
        master: Any,
        text: str,
        on_toggle: Callable[[bool], None],
        value: bool = False,
        *,
        look: theme.Look | None = None,
        ground: Pair | None = None,
    ) -> None:
        self.text = text
        self.value = value
        self._on_toggle = on_toggle
        self._hover = False
        super().__init__(master, look=look, ground=ground)
        self.canvas.configure(cursor="hand2")
        self.canvas.bind("<Button-1>", lambda _event: self.invoke())
        self.canvas.bind("<Enter>", lambda _event: self._set_hover(True), add="+")
        self.canvas.bind("<Leave>", lambda _event: self._set_hover(False), add="+")
        self._fit()

    def _fit(self) -> None:
        scale = self.scale or 1.0
        text = _font(self, OPTION_FONT, scale).measure(self.text) / scale  # as it is drawn
        width = OPTION_PAD_X * 2 + OPTION_LIGHT + OPTION_GAP + math.ceil(text)
        self.configure(width=width, height=OPTION_HEIGHT)

    def style(self) -> OptionStyle:
        return option_style(self.look, on=self.value, hover=self._hover)

    def _paint(self) -> None:
        c = self.canvas
        c.delete("all")
        width, height = c.winfo_width(), c.winfo_height()
        if width <= 1 or height <= 1:
            return
        style = self.style()
        x0, y0, x1, y1 = 0, 0, width - 1, height - 1
        radius = self._px(OPTION_RADIUS)
        round_rect(c, x0, y0, x1, y1, radius, fill=style.shadow, outline=style.border, width=1)
        shadow = max(1, self._px(OPTION_SHADOW))
        round_rect(
            c, x0 + 1, y0 + 1, x1 - 1, y1 - 1 - shadow, radius - 1, fill=style.surface, width=0
        )
        cx = self._px(OPTION_PAD_X) + self._px(OPTION_LIGHT) / 2
        cy = height / 2
        if style.glow:
            for reach, strength in ((5, 0.18), (3.5, 0.32), (2, 0.55)):
                r = self._px(OPTION_LIGHT / 2 + reach)
                colour = mix(style.surface, style.light, strength)
                c.create_oval(cx - r, cy - r, cx + r, cy + r, fill=colour, width=0, tags="glow")
        half = self._px(OPTION_LIGHT) / 2
        c.create_oval(
            cx - half, cy - half, cx + half, cy + half, fill=style.light, width=0, tags="light"
        )
        c.create_text(
            cx + half + self._px(OPTION_GAP),
            cy,
            text=self.text,
            anchor="w",
            fill=style.text,
            font=_font(self, OPTION_FONT, self.scale),
            tags="label",
        )

    def _set_hover(self, inside: bool) -> None:
        if inside != self._hover:
            self._hover = inside
            self._paint()

    def set(self, value: bool) -> None:
        """Show `value` without telling `on_toggle`."""
        if value != self.value:
            self.value = value
            self._paint()

    def invoke(self) -> None:
        """As a click: switch, and tell `on_toggle`."""
        self.set(not self.value)
        self._on_toggle(self.value)


class ForgeProgress(_CanvasWidget):
    """The run's progress in the Forge button's place: `show(fraction, text)`."""

    def __init__(
        self,
        master: Any,
        *,
        width: int = 340,
        look: theme.Look | None = None,
        ground: Pair | None = None,
    ) -> None:
        self.fraction = 0.0
        self.text = ""
        super().__init__(master, look=look, ground=ground)
        self.configure(width=width, height=PROGRESS_HEIGHT)

    def show(self, fraction: float, text: str) -> None:
        fraction = max(0.0, min(1.0, fraction))
        if (fraction, text) != (self.fraction, self.text):
            self.fraction, self.text = fraction, text
            self._paint()

    def _paint(self) -> None:
        c = self.canvas
        c.delete("all")
        width, height = c.winfo_width(), c.winfo_height()
        if width <= 1 or height <= 1:
            return
        look = self.look
        dark = look.STACK_FAR[1]  # the same dark shade in both looks: white text on all of it
        accent = theme.pick(look.ACCENT)
        radius = self._px(PROGRESS_RADIUS)
        round_rect(c, 0, 0, width - 1, height - 1, radius, fill=dark, outline=accent, width=1)
        filled = round((width - 2) * self.fraction)
        if filled > 2:
            round_rect(
                c, 1, 1, 1 + filled, height - 2, radius - 1, fill=accent, width=0, tags="fill"
            )
            c.create_line(
                1 + radius / 2,
                2,
                max(1 + radius / 2, filled - radius / 2),
                2,
                fill=theme.pick(look.ACCENT_HOVER),
                tags="fill",
            )
        font = _font(self, PROGRESS_FONT, self.scale, bold=True)
        c.create_text(
            width / 2,
            height / 2,
            text=fit_text(self.text, font, width - 2 * radius),
            fill=theme.ON_COLOR,
            font=font,
            tags="label",
        )
