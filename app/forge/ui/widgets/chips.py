"""The preset card's option buttons and the Forge button's progress bar, drawn on small canvases
in the Forge look.

`OptionButton`: an on / off button with a small round light before its label (the owner's style
D, "Buttons with a light", mockups/forge-option-buttons.html `.td`): the same raised surface
whether on or off, a 1-pixel border, a slight inner shadow at the bottom; off = the light and the
label muted; on = an accent light with a soft glow, the label in the text colour and a warmer
border; under the pointer a slightly different surface. The whole look is decided in one place,
`option_style`, so another style is one function away.

`ForgeProgress`: a run's dock, centred under the queue's list: a dark rounded panel on a soft
shadow with a ⚒ badge, the step in bold and its file muted beside it ("Renaming 7 of 12" ·
"Mind The Gap"), the percentage, and a slim bar under them that fills up with a glowing head.
Room is kept at its right end for a button placed on it (Cancel, `action_room`). It wears the
same dark shade in both looks (the dark look's tokens), so its light text stays readable.

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
PROGRESS_WIDTH = 580
PROGRESS_HEIGHT = 72  # the panel and its shadow under it
PROGRESS_SHADOW = 6  # how far the shadow reaches below the panel (and half that at its sides)
PROGRESS_RADIUS = 18
PROGRESS_PAD = 16
PROGRESS_BADGE = 36  # the ⚒ badge's diameter
PROGRESS_TRACK = 6  # the bar's thickness
PROGRESS_FONT = 14
PROGRESS_DETAIL_FONT = 13


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
        self.locked = False  # set_locked: shows its value, takes no click
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

    def set_locked(self, locked: bool) -> None:
        """Whether clicks are ignored: for a value that can't be changed here."""
        self.locked = locked
        self.canvas.configure(cursor="arrow" if locked else "hand2")

    def invoke(self) -> None:
        """As a click: switch, and tell `on_toggle`."""
        if self.locked:
            return
        self.set(not self.value)
        self._on_toggle(self.value)


@dataclass(frozen=True, slots=True)
class ProgressStyle:
    """The run dock's colours ("#RRGGBB"): the dark look's in both looks, but the shadow, which
    falls on the ground around it."""

    panel: str
    edge: str
    text: str
    detail: str
    glow: str  # the badge's glyph, the percentage and the bar's fill
    badge: str
    track: str
    shadow: tuple[str, ...]  # from the outermost ring in


def progress_style(look: theme.Look, ground: Pair) -> ProgressStyle:
    """The one place the run dock's look is decided."""
    panel = look.STACK_FAR[1]
    glow = look.BRAND[1]  # a light tint of the look's colour: it reads on the dark panel
    under = theme.pick(ground)
    depth = 0.35 if theme.is_dark() else 0.16  # how dark the shadow's innermost ring gets
    return ProgressStyle(
        panel=panel,
        edge=mix(panel, glow, 0.28),
        text=theme.ON_COLOR,
        detail=mix(panel, theme.ON_COLOR, 0.62),
        glow=glow,
        badge=mix(panel, glow, 0.16),
        track=mix(panel, theme.ON_COLOR, 0.10),
        shadow=tuple(mix(under, "#000000", depth * k) for k in (0.25, 0.5, 0.75, 1.0)),
    )


class ForgeProgress(_CanvasWidget):
    """A run's dock: `show(fraction, text)`; `text`'s part after its first " · " (the file) is
    drawn muted beside the step. `action_room`: CTk units kept free at the right end, where the
    owner places a button (`action_y`: that button's middle, from the widget's top)."""

    def __init__(
        self,
        master: Any,
        *,
        width: int = PROGRESS_WIDTH,
        look: theme.Look | None = None,
        ground: Pair | None = None,
    ) -> None:
        self.fraction = 0.0
        self.text = ""
        self.action_room = 0
        super().__init__(master, look=look, ground=ground)
        self.configure(width=width, height=PROGRESS_HEIGHT)

    @staticmethod
    def action_y() -> float:
        """Where a button placed on the panel has its middle (CTk units from the top)."""
        return (PROGRESS_HEIGHT - PROGRESS_SHADOW) / 2

    def show(self, fraction: float, text: str) -> None:
        fraction = max(0.0, min(1.0, fraction))
        if (fraction, text) != (self.fraction, self.text):
            self.fraction, self.text = fraction, text
            self._paint()

    def set_action_room(self, units: int) -> None:
        if units != self.action_room:
            self.action_room = units
            self._paint()

    def style(self) -> ProgressStyle:
        return progress_style(self.look, self._ground)

    def _paint(self) -> None:
        c = self.canvas
        c.delete("all")
        width, height = c.winfo_width(), c.winfo_height()
        if width <= 1 or height <= 1:
            return
        style = self.style()
        px = self._px
        side, drop = px(PROGRESS_SHADOW / 2), px(PROGRESS_SHADOW)
        x0, y0, x1, y1 = side, 0, width - 1 - side, height - 1 - drop
        radius = px(PROGRESS_RADIUS)
        # The soft shadow: rings growing outward and down, the outermost the faintest.
        rings = len(style.shadow)
        for i, colour in enumerate(style.shadow):
            reach = (rings - i) / rings
            round_rect(
                c,
                x0 - side * reach,
                y0 + drop * 0.3 * reach,
                x1 + side * reach,
                y1 + drop * reach,
                radius + side * reach,
                fill=colour,
                width=0,
                tags="shadow",
            )
        round_rect(c, x0, y0, x1, y1, radius, fill=style.panel, outline=style.edge, width=1)

        # The ⚒ badge.
        mid = (y0 + y1) / 2
        r = px(PROGRESS_BADGE) / 2
        bx = x0 + px(PROGRESS_PAD) + r
        c.create_oval(bx - r, mid - r, bx + r, mid + r, fill=style.badge, width=0, tags="badge")
        c.create_text(
            bx,
            mid,
            text="⚒",
            fill=style.glow,
            font=_font(self, 17, self.scale, bold=True),
            tags="badge",
        )

        # The words and the percentage on the upper line, the bar on the lower.
        left = bx + r + px(12)
        right = x1 - px(PROGRESS_PAD) - px(self.action_room)
        upper, lower = y0 + (y1 - y0) * 0.38, y0 + (y1 - y0) * 0.72
        bold = _font(self, PROGRESS_FONT, self.scale, bold=True)
        percent = c.create_text(
            right,
            upper,
            text=f"{round(self.fraction * 100)}%",
            anchor="e",
            fill=style.glow,
            font=bold,
            tags="percent",
        )
        pbox = c.bbox(percent)
        room = max(0, round((pbox[0] if pbox else right) - px(14) - left))
        step, _, detail = self.text.partition(" · ")
        step = fit_text(step, bold, room)
        c.create_text(left, upper, text=step, anchor="w", fill=style.text, font=bold, tags="title")
        if detail and step == self.text.partition(" · ")[0]:
            light = _font(self, PROGRESS_DETAIL_FONT, self.scale)
            gap = px(10)
            rest = room - bold.measure(step) - gap
            if rest > light.measure("…") * 3:
                c.create_text(
                    left + bold.measure(step) + gap,
                    upper,
                    text=fit_text(detail, light, rest),
                    anchor="w",
                    fill=style.detail,
                    font=light,
                    tags="detail",
                )

        thick = max(2, px(PROGRESS_TRACK))
        c.create_line(
            left, lower, right, lower, fill=style.track, width=thick, capstyle="round", tags="track"
        )
        head = left + (right - left) * self.fraction
        if self.fraction > 0:
            for reach, strength in ((5, 0.18), (3.5, 0.32), (2, 0.55)):  # the head's glow
                rr = thick / 2 + px(reach)
                colour = mix(style.panel, style.glow, strength)
                c.create_oval(
                    head - rr, lower - rr, head + rr, lower + rr, fill=colour, width=0, tags="glow"
                )
            c.create_line(
                left, lower, head, lower, fill=style.glow, width=thick, capstyle="round", tags="fill"
            )
            hr = thick / 2 + px(1)
            c.create_oval(
                head - hr,
                lower - hr,
                head + hr,
                lower + hr,
                fill=mix(style.glow, theme.ON_COLOR, 0.5),
                width=0,
                tags="fill",
            )
