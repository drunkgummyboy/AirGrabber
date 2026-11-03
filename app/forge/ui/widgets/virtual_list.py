"""A fast list for thousands of rows: only the rows on screen are drawn, on a canvas (ported from
MediaForge's ui/widgets/virtual_list.py).

Colours: the Forge look's (light, dark) pairs and theme's text colours, `theme.pick`ed when
drawn; a switch between light and dark draws the list again (`theme.on_mode_change`), as does a
change of Windows' scaling (`_set_scaling`: row heights and fonts follow). Fonts (D10):
AirGrabber's font, monospace (as the release list's table) for old file names (a line's `faint`,
an item's subtitle).

Smooth with 1,000 files while their lookups land (F-33): rows equal to those shown draw nothing,
the scroll region and CTk's scroll bar (which redraws itself and runs the window's pending idle
work on every set) are set only when they change, and text widths are remembered.

The queue's bands (app/forge/ui/queue_view.py, mockups/forge-chosen.html 3C): a rounded band with
a status-coloured edge, the poster, the title and its subtitle with the destination in the link
colour, the status as a dot and a word, and the band's buttons, which show only while the pointer
is over the band or one of its lines (`hover_actions`; a band that needs the owner always shows
them). A done band is drawn quieter (`Row.faded`). A band's `chip` (the queue's preset, always
shown) sits under its status, the status moving up to the title's line; a click on it is the
band's action "preset" (`chip_box` gives where it is, `action_box` any button's). A click on a
button stops there: CTk's click on "all" would take back the focus from a menu it opened. `select_bands`: a click on a band selects
its first line. Sizes come from `Sizes` (the queue's are bigger
than the confirm window's and History's).
"""

from __future__ import annotations

import bisect
import math
import tkinter as tk
import tkinter.font as tkfont
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import customtkinter as ctk
from pydantic import BaseModel, ConfigDict

from app.forge.ui.look import forge_look, mix
from app.ui import palettes, theme

MONO_FAMILY = "Consolas"  # old file names, as the release list's table
BACKDROP_WIDTH = 420  # a band's picture strip, in unscaled pixels
CHIP_MAX = 190  # a band's preset chip at most (CTk units; a longer name is cut with "…")
CHIP_HEIGHT = 26
_MEASURE_CACHE = 4000  # remembered text widths / fitted texts before starting over
_FADED = 0.45  # a done band's fill: this much of the list's ground over the band colour
_ARC_STEPS = 6  # points on a rounded corner's quarter circle
# A line's current file name: this much of the ground over the muted colour (light, dark), about
# 60 % of the muted colour's contrast and still readable (4:1 and more on the band colours).
_WASHED = (0.2, 0.3)


@dataclass(frozen=True, slots=True)
class Sizes:
    """Font sizes and band measures in CTk units. The defaults suit the confirm window and History;
    the queue passes bigger ones (`poster` = the band's poster, else 2/3 of the band's height)."""

    item_title: int = 12
    band_title: int = 14
    band_sub: int = 11
    button: int = 11
    note: int = 10
    line: int = 12
    code: int = 12
    mono: int = 10
    state: int = 11
    button_height: int = 26
    button_pad: int = 11  # inside a band's button, either side of its label
    band_gap: int = 3  # above a band (and as much below when `band_gap_below`)
    band_gap_below: bool = True
    band_radius: int = 0
    band_edge: int = 3  # the band's coloured left edge
    band_inset: int = 8  # the poster's distance from the band's left
    poster: tuple[int, int] | None = None


def thumb_colours() -> tuple[str, ...]:
    """The calm tints behind a poster's initials when there is no picture (the same in both
    looks): AirGrabber's ten accent colours, which all carry white text."""
    return tuple(palette.accent for palette in palettes.PALETTES.values())


class Row(BaseModel):
    """One line of the list (frozen: a tick box's click puts a changed copy in its place)."""

    model_config = ConfigDict(frozen=True)

    key: int
    # group: a quiet heading; section: an upper-case heading ("NEEDS YOU · 1"); band: a show's
    # or movie's band (backdrop, poster, title, buttons); item: a file line.
    kind: Literal["group", "item", "section", "band"]
    title: str
    subtitle: str = ""
    badge: str = ""
    status: str = ""
    highlight: tuple[int, int] | None = None  # the SxxEyy part of the title
    checked: bool | None = None  # None: no tick box
    check_disabled: bool = False
    art: str | None = None  # a band's poster (TMDB path)
    backdrop: str | None = None  # a band's backdrop (TMDB path)
    actions: tuple[tuple[str, str], ...] = ()  # a band's buttons: (id, label), the last filled
    note: str = ""  # a band's quiet text on the right (a time)
    chip: str = ""  # a band's preset chip under its status ("Series (Folder) ▾"): action "preset"
    code: str = ""  # an episode line's code ("S04E03"), in the link colour (readable accent)
    faint: str = ""  # a line's current (old) file name, washed out on the right
    faint_label: str = ""  # a quiet word before it ("Now:"), translated
    new: bool = False  # the line's title is the file's new name: a copper "→ " before it
    band: int | None = None  # an item line's band: the pointer over either shows its buttons
    state: Literal["", "needs", "ready", "would", "done"] = ""  # a band's status (dot + word)
    state_text: str = ""  # that status's word, translated
    dest: str = ""  # a band's destination ("→ Series › …"), after its subtitle in the link colour
    faded: bool = False  # done today: drawn quieter


def faded_fill(look: theme.Look) -> str:
    """A done band's fill in the current look: the band colour, quieter."""
    return mix(theme.pick(look.SURFACE), theme.pick(look.BG), _FADED)


def _fit(text: str, font: tkfont.Font, width: int) -> str:
    """Cut text with an ellipsis so it fits in width pixels."""
    if width <= 0:
        return ""
    if font.measure(text) <= width:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if font.measure(text[:mid] + "…") <= width:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo] + "…"


def _initials(text: str) -> str:
    return "".join(word[0] for word in text.split()[:2]).upper()


class _Colours:
    """The colours of one redraw, picked for the current look."""

    def __init__(self, look: theme.Look) -> None:
        pick = theme.pick
        self.main = pick(look.BG)
        self.surface = pick(look.SURFACE)
        self.raised = pick(look.CHIP)
        self.border = pick(look.PLACEHOLDER)
        self.edge = pick(look.EDGE)
        self.accent = pick(look.ACCENT)
        self.link = pick(look.LINK)
        self.text = pick(theme.TEXT)
        self.muted = pick(theme.TEXT_MUTED)
        self.accent_text = pick(theme.ON_ACCENT)
        self.on_color = pick(theme.ON_COLOR)
        self.line = pick(look.CHIP_HOVER)  # a ghost button's outline
        # A line's current file name: the muted colour washed towards the ground, still readable.
        self.washed = mix(self.muted, self.main, _WASHED[1 if theme.is_dark() else 0])
        self.success = pick(theme.SUCCESS)
        self.warning = pick(theme.WARNING)
        self.faded = faded_fill(look)
        self.states: dict[str, str] = {
            "needs": self.warning,
            "ready": self.success,
            "would": self.link,
            "done": mix(self.success, self.faded, 0.35),
        }
        self.status: dict[str, tuple[str, str]] = {
            "looking_up": ("…", self.muted),
            "ready": ("●", self.success),
            "needs_choice": ("?", self.warning),
            "working": ("⟳", self.link),
            "done": ("✓", pick(theme.SUCCESS)),
            "error": ("!", pick(theme.DANGER)),
            "skipped": ("–", self.muted),
        }


class VirtualList(ctk.CTkFrame):
    """Rows of four kinds on a canvas; only those on screen are drawn. Heights are CTk units
    (scaled with Windows' scaling)."""

    def __init__(
        self,
        master: Any,
        row_height: int = 46,
        group_height: int = 26,
        *,
        look: theme.Look | None = None,
        removable: bool = True,
        thumbnails: bool = True,
        band_height: int = 64,
        section_height: int = 30,
        sizes: Sizes | None = None,
        hover_actions: bool = False,
    ) -> None:
        """`band_height` is a band's row with its gaps; `hover_actions`: a band's buttons show
        only while the pointer is over the band or one of its lines (or the band needs the
        owner)."""
        self.look: theme.Look = forge_look() if look is None else look
        self.sizes = Sizes() if sizes is None else sizes
        self.hover_actions = hover_actions
        super().__init__(master, fg_color=self.look.BG, corner_radius=0)
        self.on_select: Callable[[int | None], None] = lambda key: None
        self.on_remove: Callable[[int], None] = lambda key: None
        self.on_toggle: Callable[[int, bool], None] = lambda key, value: None
        self.on_action: Callable[[int, str], None] = lambda key, action: None
        # A band's (poster, backdrop) Tk images, asked with `backdrop_size` (one size whatever the
        # window's, so a resize never needs new pictures), or None while not (yet) loaded.
        self.images: Callable[[Row, int, int], tuple[Any, Any]] = lambda row, w, h: (None, None)
        self._hits: list[tuple[int, int, int, int, int, str]] = []  # buttons, canvas coordinates
        self.select_bands = False  # a click on a band selects its first line (the queue)
        self.removable = removable
        self.thumbnails = thumbnails
        self.selected: int | None = None
        self._rows: list[Row] = []
        self._tops: list[int] = []
        self._total = 0
        self._visible: list[int] = []
        self._hover: int | None = None  # the line under the pointer
        self._hover_band: int | None = None  # the band under the pointer (or of its line)
        self._redraw_pending = False
        # What the scroll bar shows: CTk's starts at (0, 1), so a list that needs no scrolling
        # never sets it (the first set ran the new window's whole first draw in one step).
        self._scroll_view: tuple[float, float] = (0.0, 1.0)
        # Text widths and fitted texts of earlier draws (a redraw measures the same texts again;
        # each measure is a call into Tk). Emptied when the fonts change.
        self._widths: dict[tuple[str, str], int] = {}
        self._fitted: dict[tuple[str, str, int], str] = {}
        self._units = (row_height, group_height, band_height, section_height)
        self._apply_scale()

        self.canvas = tk.Canvas(
            self,
            bg=theme.pick(self.look.BG),
            highlightthickness=0,
            bd=0,
            yscrollincrement=1,
            takefocus=1,
        )
        self.scrollbar = ctk.CTkScrollbar(
            self,
            command=self._on_scrollbar,
            button_color=self.look.SCROLLBAR,
            button_hover_color=self.look.EDGE,
        )
        self.canvas.configure(yscrollcommand=self._on_yscroll)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.bind("<Configure>", lambda _event: self._schedule_redraw())
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", lambda _event: self._set_hover(None))
        theme.on_mode_change(self, self._repaint)

    # --- sizes -----------------------------------------------------------------------------

    @property
    def scale(self) -> float:
        """The widget's scaling now (CTk's own, updated before `_set_scaling` comes here)."""
        return float(self._get_widget_scaling())

    def _px(self, units: float) -> int:
        return round(units * self.scale)

    def _font(self, size: int, *, bold: bool = False) -> tkfont.Font:
        """AirGrabber's font at physical size (negative = pixels, like CTk's own fonts)."""
        return tkfont.Font(root=self, font=theme.font(size, bold=bold).create_scaled_tuple(self.scale))

    def _apply_scale(self) -> None:
        """Row heights, spacing and fonts for the current scaling."""
        row_height, group_height, band_height, section_height = self._units
        sizes = self.sizes
        self._row_h = self._px(row_height)
        self._group_h = self._px(group_height)
        self._band_h = self._px(band_height)
        self._section_h = self._px(section_height)
        self._band_top = self._px(sizes.band_gap)
        self._band_bottom = self._band_top if sizes.band_gap_below else 0
        box_h = self._band_h - self._band_top - self._band_bottom
        self.backdrop_size = (self._px(BACKDROP_WIDTH), box_h)
        if sizes.poster is not None:
            self.poster_size = (self._px(sizes.poster[0]), self._px(sizes.poster[1]))
        else:
            poster_h = box_h - 2 * self._px(6)
            self.poster_size = (round(poster_h * 2 / 3), poster_h)
        self._pad = self._px(8)
        self._box = self._px(16)
        self._title_font = self._font(sizes.item_title, bold=True)
        self._sub_font = tkfont.Font(root=self, family=MONO_FAMILY, size=-self._px(sizes.mono))
        self._group_font = self._font(10, bold=True)
        self._status_font = self._font(13)
        self._thumb_font = self._font(max(9, sizes.band_title // 2), bold=True)
        self._band_font = self._font(sizes.band_title, bold=True)
        self._band_sub_font = self._font(sizes.band_sub)
        self._note_font = self._font(sizes.note)
        self._button_font = self._font(sizes.button, bold=sizes.button > 12)
        self._state_font = self._font(sizes.state, bold=True)
        self._line_font = self._font(sizes.line)
        self._label_font = self._font(max(10, sizes.mono - 1))
        self._code_font = self._font(sizes.code, bold=True)
        self._widths.clear()
        self._fitted.clear()
        # Episode lines start where a band's text starts: under the title, past the poster.
        self._line_x = (
            self._pad // 2 + self._px(sizes.band_inset) + self.poster_size[0] + self._px(12)
        )

    def _set_scaling(self, *args: Any, **kwargs: Any) -> None:
        """Windows' scaling changed (the window moved to another screen): CTk resizes the
        widgets; the rows, their fonts and the drawing follow here."""
        super()._set_scaling(*args, **kwargs)
        self._apply_scale()
        self._layout()
        self._redraw()

    def _repaint(self) -> None:
        """Light ↔ dark: the canvas's background and every row again."""
        self.canvas.configure(bg=theme.pick(self.look.BG))
        self._redraw()

    # --- data ------------------------------------------------------------------------------

    def set_rows(self, rows: Sequence[Row]) -> None:
        rows = list(rows)
        if rows == self._rows:
            return  # nothing to draw again (the Inbox refreshes while lookups land)
        self._rows = rows
        self._layout()
        if self.selected is not None and not any(r.key == self.selected for r in self._rows):
            self.selected = None
        self._redraw()

    def _layout(self) -> None:
        self._tops = []
        y = 0
        for row in self._rows:
            self._tops.append(y)
            y += self._height(row)
        if y != self._total or not self._tops:
            self.canvas.configure(scrollregion=(0, 0, 1, max(y, 1)))
        self._total = y

    def _on_yscroll(self, first: str | float, last: str | float) -> None:
        """The canvas's view changed: the scroll bar follows, only when what it shows changed
        (CTk's scroll bar redraws itself and runs the window's pending idle work on every set:
        tens of ms with a busy window)."""
        view = (float(first), float(last))
        if view != self._scroll_view:
            self._scroll_view = view
            self.scrollbar.set(*view)

    @property
    def rows(self) -> list[Row]:
        return list(self._rows)

    def item_keys(self) -> list[int]:
        return [r.key for r in self._rows if r.kind == "item"]

    def select(self, key: int | None, *, notify: bool = True) -> None:
        self.selected = key
        if key is not None:
            self._scroll_into_view(key)
        self._redraw()
        if notify:
            self.on_select(key)

    def visible_keys(self) -> list[int]:
        return list(self._visible)

    def fit_height(self, max_height: int) -> None:
        """Ask for just enough height to show every row, but no more than max_height."""
        cap = self._px(max_height)
        self.canvas.configure(height=max(min(self._total, cap), self._row_h))

    def _height(self, row: Row) -> int:
        return {"group": self._group_h, "section": self._section_h, "band": self._band_h}.get(
            row.kind, self._row_h
        )

    def action_point(self, key: int, action: str) -> tuple[int, int]:
        """The middle of a band's button in window coordinates (for tests)."""
        for x0, y0, x1, y1, hit_key, hit_action in self._hits:
            if hit_key == key and hit_action == action:
                return (x0 + x1) // 2, round((y0 + y1) / 2 - self.canvas.canvasy(0))
        raise KeyError((key, action))

    def _index(self, key: int) -> int | None:
        return next((i for i, r in enumerate(self._rows) if r.key == key), None)

    def check_point(self, key: int) -> int:
        """An x inside the row's tick box (for tests and keyboard helpers)."""
        return self._pad + 4 + self._box // 2

    def row_middle(self, key: int) -> int:
        """The row's vertical middle in window coordinates."""
        index = self._index(key)
        if index is None:
            raise KeyError(key)
        row_h = self._height(self._rows[index])
        return round(self._tops[index] - self.canvas.canvasy(0) + row_h / 2)

    # --- scrolling -------------------------------------------------------------------------

    def _on_scrollbar(self, *args: Any) -> None:
        self.canvas.yview(*args)
        self._redraw()

    def _on_wheel(self, event: tk.Event[Any]) -> str | None:
        self.canvas.yview_scroll(int(-event.delta / 120 * self._row_h), "units")
        self._redraw()
        # Inside a scrolling page, only this list moves while it has rows left to show.
        return "break" if self._total > self.canvas.winfo_height() else None

    def scroll_to_fraction(self, fraction: float) -> None:
        self.canvas.yview_moveto(fraction)
        self._redraw()

    def _scroll_into_view(self, key: int) -> None:
        index = self._index(key)
        if index is None or not self._total:
            return
        top = self._tops[index]
        bottom = top + self._row_h
        view_top = self.canvas.canvasy(0)
        view_h = self.canvas.winfo_height()
        if top < view_top:
            self.canvas.yview_moveto(top / self._total)
        elif bottom > view_top + view_h:
            self.canvas.yview_moveto(max(bottom - view_h, 0) / self._total)

    # --- drawing ---------------------------------------------------------------------------

    def _schedule_redraw(self) -> None:
        if not self._redraw_pending:
            self._redraw_pending = True
            self.after_idle(self._redraw)

    def _row_at(self, y: float) -> int | None:
        index = bisect.bisect_right(self._tops, y) - 1
        if 0 <= index < len(self._rows):
            return index
        return None

    def _redraw(self) -> None:
        self._redraw_pending = False
        if not self.winfo_exists():
            return
        c = self.canvas
        c.delete("all")
        self._visible = []
        self._hits = []
        if not self._rows:
            return
        colours = _Colours(self.look)
        width = max(c.winfo_width(), 200)
        top = c.canvasy(0)
        height = max(c.winfo_height(), self._row_h)
        first = self._row_at(top) or 0
        for index in range(first, len(self._rows)):
            y = self._tops[index]
            if y > top + height:
                break
            row = self._rows[index]
            self._visible.append(row.key)
            if row.kind in ("group", "section"):
                self._draw_heading(colours, row, y, width)
            elif row.kind == "band":
                self._draw_band(colours, row, y, width)
            elif row.code or row.faint:
                self._draw_line(colours, row, y, width)
            else:
                self._draw_item(colours, row, y, width)

    def refresh_images(self) -> None:
        """Draw again once pictures have arrived."""
        self._schedule_redraw()

    def _measure(self, font: tkfont.Font, text: str) -> int:
        key = (str(font), text)
        width = self._widths.get(key)
        if width is None:
            if len(self._widths) >= _MEASURE_CACHE:
                self._widths.clear()
            width = self._widths[key] = font.measure(text)
        return width

    def _fit(self, text: str, font: tkfont.Font, width: int) -> str:
        """`_fit`, remembered: the same texts are fitted to the same widths on every redraw."""
        key = (str(font), text, width)
        fitted = self._fitted.get(key)
        if fitted is None:
            if len(self._fitted) >= _MEASURE_CACHE:
                self._fitted.clear()
            fitted = self._fitted[key] = _fit(text, font, width)
        return fitted

    def _draw_heading(self, p: _Colours, row: Row, y: int, width: int) -> None:
        """A group (quiet heading) or a section ("NEEDS YOU   ·   1")."""
        pad = self._pad
        text = row.title.upper() + (f"   ·   {row.subtitle}" if row.subtitle else "")
        bottom = y + self._group_h - 7 if row.kind == "group" else y + self._section_h - 8
        self.canvas.create_text(
            pad + 4,
            bottom,
            text=self._fit(text, self._group_font, width - 2 * pad),
            anchor="sw",
            fill=p.muted,
            font=self._group_font,
        )

    def actions_shown(self, row: Row) -> bool:
        """Whether a band's buttons are drawn: always, or (`hover_actions`) while the pointer is
        over the band or one of its lines, and always for a band that needs the owner."""
        return not self.hover_actions or row.state == "needs" or self._hover_band == row.key

    def _corners(self, x0: int, y0: int, x1: int, y1: int, radius: int, ground: str) -> None:
        """Round a band's corners: the list's ground over what lies outside a quarter circle at
        each corner (drawn after the band's pictures, so they are rounded too)."""
        if radius <= 1:
            return
        c = self.canvas
        for cx, cy, sx, sy in ((x0, y0, 1, 1), (x1, y0, -1, 1), (x0, y1, 1, -1), (x1, y1, -1, -1)):
            points: list[float] = [cx, cy]
            for step in range(_ARC_STEPS + 1):
                angle = math.pi / 2 * step / _ARC_STEPS
                points += [
                    cx + sx * radius * (1 - math.sin(angle)),
                    cy + sy * radius * (1 - math.cos(angle)),
                ]
            c.create_polygon(points, fill=ground, outline=ground, width=1, tags=("band-corner",))

    def round_rect(
        self, x0: float, y0: float, x1: float, y1: float, radius: int, **options: Any
    ) -> int:
        """A rounded rectangle on the canvas (a smoothed polygon)."""
        r = min(radius, (x1 - x0) / 2, (y1 - y0) / 2)
        points = [
            x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1,
            x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0,
        ]  # fmt: skip
        return int(self.canvas.create_polygon(points, smooth=True, **options))

    def _band_buttons(
        self, row: Row, right: int, middle: float
    ) -> list[tuple[int, int, int, int, str, str]]:
        """Where a band's buttons go, right to left from `right`: (x0, y0, x1, y1, id, label);
        the same whether they show or not, so the status beside them never moves."""
        sizes = self.sizes
        placed: list[tuple[int, int, int, int, str, str]] = []
        bh = self._px(sizes.button_height)
        for action, label in reversed(row.actions):
            w = self._measure(self._button_font, label) + 2 * self._px(sizes.button_pad)
            by0, by1 = round(middle - bh / 2), round(middle + bh / 2)
            placed.append((right - w, by0, right, by1, action, label))
            right -= w + self._px(8)
        return placed

    def _draw_band(self, p: _Colours, row: Row, y: int, width: int) -> None:
        c, pad, sizes = self.canvas, self._pad, self.sizes
        x0, x1 = pad // 2, width - pad // 2
        y0, y1 = y + self._band_top, y + self._band_h - self._band_bottom
        poster_w, poster_h = self.poster_size
        faded = row.faded
        fill = p.faded if faded else p.surface
        poster, backdrop = self.images(row, *self.backdrop_size)
        tints = thumb_colours()
        tint = tints[zlib.crc32((row.badge or row.title).encode()) % len(tints)]
        edge = p.states.get(row.state, tint)
        c.create_rectangle(x0, y0, x1, y1, fill=fill, width=0, tags=("band-fill",))
        if sizes.band_edge:
            edge_x = x0 + self._px(sizes.band_edge)
            c.create_rectangle(x0, y0, edge_x, y1, fill=edge, width=0, tags=("band-edge",))
        if backdrop is not None and not faded and x1 - x0 - 3 >= self.backdrop_size[0]:
            c.create_image(x1, y0, image=backdrop, anchor="ne", tags=("band-image",))
        self._corners(x0, y0, x1, y1, self._px(sizes.band_radius), p.main)
        px = x0 + self._px(sizes.band_inset)
        py = round((y0 + y1 - poster_h) / 2)
        if poster is not None:
            c.create_image(px, py, image=poster, anchor="nw", tags=("band-image",))
        else:
            colour = mix(tint, fill, 0.5) if faded else tint
            c.create_rectangle(px, py, px + poster_w, py + poster_h, fill=colour, width=0)
            c.create_text(
                px + poster_w / 2,
                py + poster_h / 2,
                text=_initials(row.badge or row.title),
                fill=p.on_color,
                font=self._thumb_font,
                tags=("band-initials",),
            )
        right = x1 - self._px(14)
        middle = (y0 + y1) / 2
        buttons = self._band_buttons(row, right, middle)
        if self.actions_shown(row):
            self._draw_buttons(p, row, buttons, middle, fill)
        if buttons:
            right = buttons[-1][0] - self._px(14)
        if row.chip:
            right = self._draw_state_and_chip(p, row, right, y0, y1, fill)
        elif row.state_text:
            right = self._draw_state(p, row, right, middle)
        if row.note:
            c.create_text(
                right, middle, text=row.note, anchor="e", fill=p.muted, font=self._note_font
            )
            right -= self._measure(self._note_font, row.note) + self._px(12)
        tx = px + poster_w + self._px(12)
        avail = right - tx
        box_h = y1 - y0
        title_y, sub_y = (0.36, 0.72) if sizes.poster is None else (0.40, 0.64)
        c.create_text(
            tx,
            y0 + box_h * title_y,
            text=self._fit(row.title, self._band_font, avail),
            anchor="w",
            fill=p.muted if faded else p.text,
            font=self._band_font,
            tags=("band-title",),
        )
        sub_y_px = y0 + box_h * sub_y
        subtitle = self._fit(row.subtitle, self._band_sub_font, avail)
        c.create_text(
            tx, sub_y_px, text=subtitle, anchor="w", fill=p.muted, font=self._band_sub_font
        )
        if row.dest and subtitle == row.subtitle:
            used = self._measure(self._band_sub_font, row.subtitle + " ") if row.subtitle else 0
            dest = self._fit(row.dest, self._band_sub_font, avail - used)
            if dest:
                c.create_text(
                    tx + used,
                    sub_y_px,
                    text=dest,
                    anchor="w",
                    fill=p.muted if faded else p.link,
                    font=self._band_sub_font,
                    tags=("band-dest",),
                )

    def _draw_buttons(
        self,
        p: _Colours,
        row: Row,
        buttons: Sequence[tuple[int, int, int, int, str, str]],
        middle: float,
        fill: str,
    ) -> None:
        """A band's buttons (the rightmost filled with the accent) and their click areas."""
        c = self.canvas
        rounded = self._px(8) if self.sizes.band_radius else 0
        for index, (bx0, by0, bx1, by1, action, label) in enumerate(buttons):
            primary = index == 0
            if rounded:
                self.round_rect(
                    bx0,
                    by0,
                    bx1,
                    by1,
                    rounded,
                    fill=p.accent if primary else fill,
                    outline=p.accent if primary else p.line,
                    width=1,
                    tags=("band-button",),
                )
            else:
                c.create_rectangle(
                    bx0,
                    by0,
                    bx1,
                    by1,
                    fill=p.accent if primary else "",
                    outline=p.accent if primary else p.edge,
                    width=1,
                    tags=("band-button",),
                )
            c.create_text(
                (bx0 + bx1) / 2,
                middle,
                text=label,
                fill=p.accent_text if primary else p.text,
                font=self._button_font,
                tags=("band-button-text",),
            )
            self._hits.append((bx0, by0, bx1, by1, row.key, action))

    def _draw_state_and_chip(
        self, p: _Colours, row: Row, right: int, y0: int, y1: int, fill: str
    ) -> int:
        """The status on the title's line and the preset chip under it, both ending at `right`
        (one column as wide as the wider); returns the x left of them."""
        c, box_h = self.canvas, y1 - y0
        title_y, sub_y = (0.36, 0.72) if self.sizes.poster is None else (0.40, 0.64)
        state_left = right
        if row.state_text:
            state_left = self._draw_state(p, row, right, y0 + box_h * title_y) + self._px(14)
        pad = self._px(10)
        text = self._fit(row.chip, self._note_font, self._px(CHIP_MAX) - 2 * pad)
        width = self._measure(self._note_font, text) + 2 * pad
        middle = y0 + box_h * sub_y
        height = self._px(CHIP_HEIGHT)
        cx0, cy0, cy1 = right - width, round(middle - height / 2), round(middle + height / 2)
        self.round_rect(
            cx0, cy0, right, cy1, self._px(8), fill=fill, outline=p.line, width=1, tags=("chip",)
        )
        c.create_text(
            right - pad, middle, text=text, anchor="e", fill=p.muted, font=self._note_font,
            tags=("chip-text",),
        )  # fmt: skip
        self._hits.append((cx0, cy0, right, cy1, row.key, "preset"))
        return min(state_left, cx0) - self._px(14)

    def chip_box(self, key: int) -> tuple[int, int, int, int] | None:
        """Where band `key`'s preset chip was drawn last (canvas coordinates), or None."""
        return self.action_box(key, "preset")

    def action_box(self, key: int, action: str) -> tuple[int, int, int, int] | None:
        """Where band `key`'s button `action` was drawn last (canvas coordinates), or None."""
        for x0, y0, x1, y1, hit_key, hit_action in self._hits:
            if hit_key == key and hit_action == action:
                return x0, y0, x1, y1
        return None

    def screen_box(self, box: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        """A canvas box as (x, y, width, height) on the screen: where a menu opens under it."""
        x0, y0, x1, y1 = box
        canvas = self.canvas
        left = round(canvas.winfo_rootx() + x0 - canvas.canvasx(0))
        top = round(canvas.winfo_rooty() + y0 - canvas.canvasy(0))
        return left, top, x1 - x0, y1 - y0

    def _draw_state(self, p: _Colours, row: Row, right: int, middle: float) -> int:
        """A band's status as a coloured dot and word ending at `right`; returns the x left of
        it."""
        c = self.canvas
        colour = p.states.get(row.state, p.muted)
        c.create_text(
            right,
            middle,
            text=row.state_text,
            anchor="e",
            fill=colour,
            font=self._state_font,
            tags=("band-state",),
        )
        dot = self._px(5)
        dx = right - self._measure(self._state_font, row.state_text) - self._px(7) - dot
        c.create_oval(
            dx - dot, middle - dot, dx + dot, middle + dot, fill=colour, width=0, tags=("band-dot",)
        )
        return dx - dot - self._px(14)

    def _draw_selection(self, p: _Colours, row: Row, y: int, width: int, inset: int) -> None:
        """The hover / selected background of a line or an item (`inset` from its edges)."""
        c, pad, h = self.canvas, self._pad, self._row_h
        selected = row.key == self.selected
        if selected or row.key == self._hover:
            c.create_rectangle(
                pad // 2,
                y + inset,
                width - pad // 2,
                y + h - inset,
                fill=p.raised if selected else p.surface,
                width=0,
            )
        if selected:
            c.create_rectangle(
                pad // 2, y + inset, pad // 2 + 3, y + h - inset, fill=p.accent, width=0
            )

    def _draw_status(self, p: _Colours, row: Row, middle: float, width: int) -> None:
        """The status glyph at the right, or the remove cross under the pointer."""
        x = width - self._pad - self._px(12)
        if row.key == self._hover and self.removable:
            glyph, colour = "✕", p.muted
        else:
            glyph, colour = p.status.get(row.status, ("", p.muted))
        self.canvas.create_text(x, middle, text=glyph, fill=colour, font=self._status_font)

    def _draw_line(self, p: _Colours, row: Row, y: int, width: int) -> None:
        """A file line under a band: code, name, the old name faint on the right."""
        c, pad, h = self.canvas, self._pad, self._row_h
        self._draw_selection(p, row, y, width, 1)
        middle = y + h / 2
        x = self._line_x
        if row.checked is not None:
            self._draw_box(p, row, pad + 4, round(middle - self._box / 2))
        right = width - pad - self._px(30)
        if row.faint:
            label = row.faint_label
            label_w = self._measure(self._label_font, label) + self._px(6) if label else 0
            room = round((right - x) * 0.45) - label_w
            faint_w = min(self._measure(self._sub_font, row.faint), room)
            c.create_text(
                right,
                middle,
                text=self._fit(row.faint, self._sub_font, faint_w),
                anchor="e",
                fill=p.washed,
                font=self._sub_font,
                tags=("line-old",),
            )
            right -= faint_w
            if label:
                c.create_text(
                    right - self._px(6),
                    middle,
                    text=label,
                    anchor="e",
                    fill=p.muted,
                    font=self._label_font,
                    tags=("line-now",),
                )
                right -= label_w
            right -= self._px(16)
        if row.new:
            arrow = "→ "
            c.create_text(
                x,
                middle,
                text=arrow,
                anchor="w",
                fill=p.muted if row.faded else p.link,
                font=self._line_font,
                tags=("line-arrow",),
            )
            x += self._measure(self._line_font, arrow)
        if row.code:
            c.create_text(
                x,
                middle,
                text=row.code,
                anchor="w",
                fill=p.muted if row.faded else p.link,
                font=self._code_font,
            )
            widest = self._measure(self._code_font, "S00E00-E00")
            x += max(widest, self._measure(self._code_font, row.code)) + self._px(10)
        c.create_text(
            x,
            middle,
            text=self._fit(row.title, self._line_font, right - x),
            anchor="w",
            fill=p.muted if row.faded else p.text,
            font=self._line_font,
        )
        self._draw_status(p, row, middle, width)

    def _draw_item(self, p: _Colours, row: Row, y: int, width: int) -> None:
        c, pad, h = self.canvas, self._pad, self._row_h
        self._draw_selection(p, row, y, width, 2)
        tx = pad + 4
        if row.checked is not None:
            self._draw_box(p, row, tx, y + (h - self._box) // 2)
            tx += self._box + 10
        if self.thumbnails:
            thumb_w, thumb_h = round(h * 0.55), h - 2 * round(h * 0.13)
            ty = y + (h - thumb_h) // 2
            tints = thumb_colours()
            colour = tints[zlib.crc32(row.badge.encode()) % len(tints)]
            c.create_rectangle(tx, ty, tx + thumb_w, ty + thumb_h, fill=colour, width=0)
            c.create_text(
                tx + thumb_w / 2,
                ty + thumb_h / 2,
                text=_initials(row.badge),
                fill=p.on_color,
                font=self._thumb_font,
            )
            tx += thumb_w + 10

        right = width - pad - 30
        x = tx
        avail = right - x
        title = self._fit(row.title, self._title_font, avail)
        title_y = y + h * 0.36
        if row.highlight and title == row.title:
            start, end = row.highlight
            for part, colour in (
                (title[:start], p.text),
                (title[start:end], p.link),
                (title[end:], p.text),
            ):
                if part:
                    c.create_text(
                        x, title_y, text=part, anchor="w", fill=colour, font=self._title_font
                    )
                    x += self._measure(self._title_font, part)
        else:
            c.create_text(x, title_y, text=title, anchor="w", fill=p.text, font=self._title_font)
        c.create_text(
            tx,
            y + h * 0.70,
            text=self._fit(row.subtitle, self._sub_font, avail),
            anchor="w",
            fill=p.muted,
            font=self._sub_font,
        )
        self._draw_status(p, row, y + h / 2, width)

    def _draw_box(self, p: _Colours, row: Row, x: int, y: int) -> None:
        c, b = self.canvas, self._box
        if row.checked:
            fill = p.border if row.check_disabled else p.accent
            c.create_rectangle(x, y, x + b, y + b, fill=fill, outline=fill, width=1)
            c.create_line(
                [x + b * 0.22, y + b * 0.52, x + b * 0.42, y + b * 0.72, x + b * 0.78, y + b * 0.3],
                fill=p.muted if row.check_disabled else p.accent_text,
                width=max(2, b // 8),
            )
        else:
            c.create_rectangle(x, y, x + b, y + b, outline=p.border, width=max(1, b // 10))

    # --- mouse -----------------------------------------------------------------------------

    def _set_hover(self, key: int | None, band: int | None = None) -> None:
        if key != self._hover or band != self._hover_band:
            self._hover = key
            self._hover_band = band
            self._redraw()

    def _on_motion(self, event: tk.Event[Any]) -> None:
        index = self._row_at(self.canvas.canvasy(event.y))
        row = self._rows[index] if index is not None else None
        if row is not None and row.kind == "band":
            self._set_hover(None, row.key)
        elif row is not None and row.kind == "item":
            self._set_hover(row.key, row.band)
        else:
            self._set_hover(None)

    def hover_band(self, key: int | None) -> None:
        """As the pointer over band `key` (None: over nothing): for tests."""
        self._set_hover(None, key)

    def _on_click(self, event: tk.Event[Any]) -> str | None:
        self.canvas.focus_set()
        cy = self.canvas.canvasy(event.y)
        for x0, y0, x1, y1, key, action in self._hits:
            if x0 <= event.x <= x1 and y0 <= cy <= y1:
                self.on_action(key, action)
                # A button's menu (Ignore, the chip) took the focus: CTk's own click on "all"
                # (focus to the clicked widget) would pull it back and close the menu at once.
                return "break"
        index = self._row_at(cy)
        if index is not None and self.select_bands and self._rows[index].kind == "band":
            band = self._rows[index].key
            first = next((r.key for r in self._rows if r.band == band), None)
            if first is not None:
                self.select(first)
            return None
        if index is None or self._rows[index].kind != "item":
            return None
        row = self._rows[index]
        box_right = self._pad + 4 + self._box + 6
        if row.checked is not None and event.x <= box_right:
            if not row.check_disabled:
                row = row.model_copy(update={"checked": not row.checked})
                self._rows[index] = row
                self._redraw()
                self.on_toggle(row.key, bool(row.checked))
        elif self.removable and event.x > self.canvas.winfo_width() - self._pad - 28:
            self.on_remove(row.key)
        else:
            self.select(row.key)
        return None
