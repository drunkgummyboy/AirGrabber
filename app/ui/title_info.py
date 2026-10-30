"""The info window's shared look, for series (app/ui/show_details.py) and films
(app/ui/movie_details.py).

At the top a wide backdrop (TMDB's, or the poster blurred when TMDB has none) fades into the
window's background, with "▶ Watch trailer" over it when TMDB names a trailer (it opens on
YouTube). The poster overlaps the backdrop's lower edge; beside it the title, a meta line,
genre pills, the synopsis (More shows all of it), a muted line the window may use (a series'
next episode), the actions and, without a TMDB key, a plain hint. Under that the cast row:
the first billed actors with a round photo (initials without one), the name and the part.
Each window adds its own part under that (`content`: a series' seasons and episodes).

Tk can't draw a widget see-through over an image, so the backdrop, the poster and the trailer
button are drawn on one canvas (`Masthead`) in physical pixels: the backdrop and the poster are
baked by Pillow at the screen's scaling (in ImageCache's worker thread, `HeroFace`,
`FramedPoster`), the button is an RGBA image with canvas text on it. The text column is a frame
on that canvas, below the line where the fade turns solid. A switch between light and dark, or
another Windows scaling, bakes them again. Without a TMDB key (or while nothing is known yet)
there is no backdrop: the poster and the text start at the top.

The whole window scrolls; it opens about HEIGHT_SHARE (85 %) of the work area's height and up
to WINDOW_WIDTH wide, centred on the screen it opens on, never larger than that screen's work
area less SCREEN_MARGIN (`window_size`); nothing about its size is remembered.
"""

from __future__ import annotations

import asyncio
import logging
import math
import tkinter as tk
import webbrowser
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from typing import TYPE_CHECKING, Any

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageFilter, ImageTk

from app.i18n import N_, month_short, number, t
from app.models import CastMember, MovieDetails
from app.ui import dialog_kit, theme
from app.ui.branding import set_window_icon
from app.ui.poster_card import rgb
from app.ui.tasks import spawn
from app.ui.text_fit import SLACK, break_lines, fit_to_lines, unscaled_width
from app.ui.tooltip import work_area
from app.ui.widgets import FlexGrid

if TYPE_CHECKING:
    from app.services import ImageCache

log = logging.getLogger(__name__)

RGB = tuple[int, int, int]

# Logical (unscaled) CTk units.
WINDOW_WIDTH = 1400  # at most
HEIGHT_SHARE = 0.85  # of the work area's height
SCREEN_MARGIN = 80  # the window stays at least this much narrower than the work area
MIN_WIDTH = 560
MIN_HEIGHT = 360
PAD_X = dialog_kit.PAD_X
PAD_Y = dialog_kit.PAD_Y
HERO_HEIGHT = 240  # the backdrop
POSTER_WIDTH = 160
POSTER_HEIGHT = 240
POSTER_RADIUS = 10
POSTER_BORDER = 2
POSTER_OVERLAP = 96  # how far the poster reaches up into the backdrop
POSTER_GAP = 24  # between the poster and the text beside it
TEXT_RISE = 16  # the text starts this far above the backdrop's lower edge (the fade is solid)
FADE_FROM = 0.30  # the backdrop starts fading into the background this far down
BLOCK_GAP = dialog_kit.BLOCK_GAP  # under the poster / text, above the cast row
TITLE_SIZE = 26
SUMMARY_LINES = 6  # collapsed; "More" shows the rest
BUTTON_GAP = 12  # between the actions
# "▶ Watch trailer": a dark see-through pill over the backdrop, 42 % of the way down.
PILL_HEIGHT = 46
PILL_PAD_LEFT = 8
PILL_CIRCLE = 30
PILL_TEXT_GAP = 10
PILL_PAD_RIGHT = 18
PILL_FONT_SIZE = 14
PILL_TOP = 0.42  # its centre, as a share of the backdrop's height
PILL_FILL = (0, 0, 0, 150)  # see-through black over the picture
PILL_FILL_HOVER = (0, 0, 0, 200)
PILL_EDGE_ALPHA = (90, 160)  # its white outline: normal, hovered
# The hero's stand-in (a poster without a backdrop): blurred, then dimmed towards the background.
BLUR_SHARE = 1 / 45  # the blur radius as a share of the backdrop's width
BLUR_DIM = 0.35
# Genre pills.
PILL_GENRE_HEIGHT = 26
PILL_GENRE_PAD = 10
PILL_GENRE_GAP = 6
GENRE_FONT_SIZE = 12
# The cast row.
CAST_PHOTO = 60
CAST_ITEM_WIDTH = 88
CAST_NAME_SIZE = 12
CAST_PART_SIZE = 11
CAST_LINES = 2
CAST_GAP = 6  # between two cast entries (each side)
# TMDB's backdrop widths, the smallest first.
TMDB_BACKDROP_WIDTHS = (300, 780, 1280)

_SUPERSAMPLE = 4  # rounded shapes are drawn this much larger, then scaled down

# Shown through t().
TRAILER_TEXT = N_("Watch trailer")
NO_KEY_HINT = N_("Add a TMDB key in Settings for posters, trailers and cast")
MORE_TEXT = N_("More")
# TMDB's film statuses in plain words (shown when a film isn't out yet).
_FILM_STATUSES = {
    "Rumored": N_("Rumoured"),
    "Planned": N_("Planned"),
    "In Production": N_("In production"),
    "Post Production": N_("Post-production"),
    "Canceled": N_("Cancelled"),
}


# -- text (pure) -------------------------------------------------------------------------------


def join_meta(parts: Iterable[str | None]) -> str:
    """The parts that are known, joined by " · " ("" for none)."""
    return " · ".join(part for part in parts if part)


def rating_text(rating: float | None) -> str | None:
    """"★ 7.4" ("★ 7,4" in Dutch); None when unknown."""
    return f"★ {number(rating, '.1f')}" if rating is not None else None


def day_text(day: date, today: date) -> str:
    """"24 Dec", with the year when it isn't this year's ("24 Dec 2024")."""
    text = f"{day.day} {month_short(day)}"
    return text if day.year == today.year else f"{text} {day.year}"


def release_text(details: MovieDetails | None, status: str | None, today: date) -> str | None:
    """A film's release in its meta line: "Out digitally since 24 Dec" / "Out digitally on
    3 Nov" when TMDB knows the digital release, else TMDB's status while it isn't out
    ("In production"); None for a released film without a digital date."""
    digital = details.digital_release if details is not None else None
    if digital is not None:
        when = day_text(digital, today)
        if digital <= today:
            return t("Out digitally since {date}", date=when)
        return t("Out digitally on {date}", date=when)
    if status is not None and (name := _FILM_STATUSES.get(status)) is not None:
        return t(name)
    return None


def initials(name: str) -> str:
    """The first letters of the first and the last word ("Maren Holloway" → "MH"; "Bram" →
    "B"); "?" for a name without letters."""
    words = [word for word in name.replace("-", " ").split() if word[:1].isalnum()]
    if not words:
        return "?"
    first, last = words[0][0], words[-1][0] if len(words) > 1 else ""
    return (first + last).upper()


def backdrop_size(width_px: int) -> str:
    """TMDB's smallest backdrop size at least `width_px` wide, at most the largest ("w1280";
    "original" can be a 4K picture of several MB)."""
    for width in TMDB_BACKDROP_WIDTHS:
        if width >= width_px:
            return f"w{width}"
    return f"w{TMDB_BACKDROP_WIDTHS[-1]}"


def flow_rows(widths: Sequence[float], room: float, gap: float) -> list[list[int]]:
    """The indexes of `widths` row by row, as many on a row as fit in `room` with `gap`
    between them (at least one a row, however wide)."""
    rows: list[list[int]] = []
    used = 0.0
    for index, width in enumerate(widths):
        if rows and used + gap + width <= room:
            rows[-1].append(index)
            used += gap + width
        else:
            rows.append([index])
            used = width
    return rows


def collapsed_summary(
    text: str, width: float, measure: Callable[[str], float]
) -> tuple[str, bool]:
    """The synopsis wrapped into at most SUMMARY_LINES lines of `width`, and whether that cut
    it short (then "More" shows the rest)."""
    fitted = fit_to_lines(text, width, SUMMARY_LINES, measure)
    return fitted, fitted != text


@dataclass(frozen=True, slots=True)
class MastheadLayout:
    """Where the masthead's parts go, in physical pixels (`masthead_layout`)."""

    hero_height: int  # 0: no backdrop
    poster: tuple[int, int]  # top-left
    text: tuple[int, int]  # top-left of the text column
    text_width: int
    pill_centre: tuple[int, int]


def masthead_layout(width: int, scaling: float, *, hero: bool) -> MastheadLayout:
    """The masthead at `width` physical pixels and Windows' `scaling`: with a backdrop, the
    poster reaches POSTER_OVERLAP up into it and the text starts TEXT_RISE above its lower
    edge; without one both start at the top."""

    def px(units: float) -> int:
        return round(units * scaling)

    pad_x = px(PAD_X)
    text_x = pad_x + px(POSTER_WIDTH) + px(POSTER_GAP)
    if hero:
        hero_height = px(HERO_HEIGHT)
        poster_y = hero_height - px(POSTER_OVERLAP)
        text_y = hero_height - px(TEXT_RISE)
    else:
        hero_height = 0
        poster_y = text_y = px(PAD_Y)
    return MastheadLayout(
        hero_height=hero_height,
        poster=(pad_x, poster_y),
        text=(text_x, text_y),
        text_width=max(1, width - text_x - pad_x),
        pill_centre=(width // 2, round(hero_height * PILL_TOP)),
    )


def window_size(work_width: float, work_height: float) -> tuple[int, int]:
    """The info window's size in CTk units for a work area of this size (CTk units): about
    HEIGHT_SHARE of its height and up to WINDOW_WIDTH wide, never wider than the area less
    SCREEN_MARGIN nor taller than the area; at least the minimum size where the area allows."""
    width = min(WINDOW_WIDTH, int(work_width) - SCREEN_MARGIN)
    height = round(work_height * HEIGHT_SHARE)
    width = min(max(width, MIN_WIDTH), int(work_width))
    height = min(max(height, MIN_HEIGHT), int(work_height))
    return max(1, width), max(1, height)


# -- baked images (pure; the big ones run in ImageCache's worker thread) -----------------------


def _smooth(x: float) -> float:
    x = min(1.0, max(0.0, x))
    return x * x * (3 - 2 * x)


@dataclass(frozen=True, slots=True)
class HeroFace:
    """The backdrop's finish step (ImageCache): fade the picture into `bg` from `fade_from`
    down to `solid_from` (pixel rows); `blur` > 0 blurs and dims it first (a poster standing
    in for a backdrop). Hashable: part of the image's cache key."""

    bg: RGB
    fade_from: int
    solid_from: int
    blur: float = 0.0

    def __call__(self, image: Image.Image, /) -> Image.Image:
        return compose_hero(image, self)


def hero_face(size: tuple[int, int], bg: RGB, scaling: float, *, blur: bool) -> HeroFace:
    """The face of a backdrop `size` physical pixels big: solid from TEXT_RISE above its lower
    edge, where the text starts."""
    width, height = size
    solid_from = max(1, height - round(TEXT_RISE * scaling))
    return HeroFace(
        bg=bg,
        fade_from=round(height * FADE_FROM),
        solid_from=solid_from,
        blur=round(width * BLUR_SHARE, 1) if blur else 0.0,
    )


def compose_hero(image: Image.Image, face: HeroFace) -> Image.Image:
    """`image` (already the backdrop's size) faded into `face.bg` towards the bottom, in RGB."""
    picture = image.convert("RGB")
    background = Image.new("RGB", picture.size, face.bg)
    if face.blur > 0:
        picture = picture.filter(ImageFilter.GaussianBlur(face.blur))
        picture = Image.blend(picture, background, BLUR_DIM)
    width, height = picture.size
    span = max(1, face.solid_from - face.fade_from)
    column = bytes(
        255 if row >= face.solid_from else round(255 * _smooth((row + 0.5 - face.fade_from) / span))
        if row >= face.fade_from
        else 0
        for row in range(height)
    )
    mask = Image.frombytes("L", (1, height), column).resize((width, height), Image.Resampling.NEAREST)
    return Image.composite(background, picture, mask)


@lru_cache(maxsize=32)
def rounded_mask(size: tuple[int, int], radius: int, inset: int = 0) -> Image.Image:
    """An anti-aliased L mask of a rounded rectangle `inset` pixels inside `size` (a circle
    when square and `radius` is half its side). Shared: read-only."""
    width, height = size
    scale = _SUPERSAMPLE
    big = Image.new("L", (width * scale, height * scale), 0)
    box = (inset * scale, inset * scale, (width - inset) * scale - 1, (height - inset) * scale - 1)
    ImageDraw.Draw(big).rounded_rectangle(box, radius=max(0, radius * scale), fill=255)
    return big.resize(size, Image.Resampling.BOX)


@dataclass(frozen=True, slots=True)
class FramedPoster:
    """The poster's finish step: rounded corners (see-through outside) inside a `border`-pixel
    `edge` line, in RGBA."""

    radius: int
    border: int
    edge: RGB

    def __call__(self, image: Image.Image, /) -> Image.Image:
        return frame_poster(image, self)


def frame_poster(
    image: Image.Image | None,
    face: FramedPoster,
    size: tuple[int, int] = (1, 1),
    fill: RGB = (0, 0, 0),
) -> Image.Image:
    """`image` (None: `fill` of `size`, the placeholder) with `face`'s corners and border."""
    picture = Image.new("RGB", size, fill) if image is None else image.convert("RGB")
    size = picture.size
    framed = Image.new("RGB", size, face.edge)
    framed.paste(picture, (0, 0), rounded_mask(size, max(0, face.radius - face.border), face.border))
    out = framed.convert("RGBA")
    out.putalpha(rounded_mask(size, face.radius))
    return out


@dataclass(frozen=True, slots=True)
class RoundPhoto:
    """A cast photo's finish step: the square from near the top of a 2:3 portrait (where the
    face is), `diameter` pixels, in a circle (see-through outside), RGBA."""

    diameter: int

    def __call__(self, image: Image.Image, /) -> Image.Image:
        return round_photo(image, self.diameter)


def round_photo(image: Image.Image, diameter: int) -> Image.Image:
    width, height = image.size
    side = min(width, height)
    top = min(max(0, round(height * 0.08)), height - side)
    left = (width - side) // 2
    square = image.convert("RGB").crop((left, top, left + side, top + side))
    if square.size != (diameter, diameter):
        square = square.resize((diameter, diameter), Image.Resampling.LANCZOS)
    out = square.convert("RGBA")
    out.putalpha(rounded_mask((diameter, diameter), diameter // 2))
    return out


@lru_cache(maxsize=8)
def trailer_pill(size: tuple[int, int], scaling: float, hover: bool) -> Image.Image:
    """The "▶ Watch trailer" button's picture (the text is canvas text on it): a see-through
    dark pill with a thin white outline, a white circle with a dark ▶ at its left. RGBA."""
    width, height = size
    scale = _SUPERSAMPLE
    big = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
    draw = ImageDraw.Draw(big)
    edge_alpha = PILL_EDGE_ALPHA[1 if hover else 0]
    line = max(1, round(scaling)) * scale
    radius = height * scale // 2
    draw.rounded_rectangle(
        (0, 0, width * scale - 1, height * scale - 1),
        radius=radius,
        fill=PILL_FILL_HOVER if hover else PILL_FILL,
        outline=(255, 255, 255, edge_alpha),
        width=line,
    )
    circle = round(PILL_CIRCLE * scaling) * scale
    left = round(PILL_PAD_LEFT * scaling) * scale
    top = (height * scale - circle) // 2
    draw.ellipse((left, top, left + circle - 1, top + circle - 1), fill=(255, 255, 255, 255))
    # ▶, a little right of the circle's centre (it looks centred so).
    cx, cy, r = left + circle * 0.54, top + circle / 2, circle * 0.22
    points = [
        (cx - r * 0.8, cy - r),
        (cx - r * 0.8, cy + r),
        (cx + r, cy),
    ]
    draw.polygon(points, fill=(17, 17, 17, 255))
    return big.resize(size, Image.Resampling.BOX)


# -- the masthead ------------------------------------------------------------------------------


class _TextColumn(ctk.CTkFrame):
    """The masthead's text column; tells it when Windows' scaling changed."""

    on_scaling: Callable[[], None] | None = None

    def _set_scaling(self, *args: Any, **kwargs: Any) -> None:
        super()._set_scaling(*args, **kwargs)
        if self.on_scaling is not None:
            self.on_scaling()


@dataclass(frozen=True, slots=True)
class _Hero:
    url: str
    blur: bool


class Masthead(tk.Canvas):
    """The backdrop, the poster, the trailer button and the text column (`text`) on one canvas
    (see the module docstring). `poster` gives the poster's url for that many physical pixels
    wide (None: the placeholder). `reserve_hero` keeps the backdrop's room while it may still
    come, so the layout doesn't jump when it does."""

    def __init__(
        self, master: Any, *, images: ImageCache, poster: Callable[[int], str | None]
    ) -> None:
        super().__init__(
            master, highlightthickness=0, borderwidth=0, height=1, background=theme.pick(theme.BG)
        )
        self._images = images
        self._poster_url = poster
        self._hero: _Hero | None = None
        self._hero_room = False  # the backdrop's room is kept (it shows or may still come)
        self._trailer: str | None = None
        self._width = 0  # physical px, from the last <Configure>
        self._layout: MastheadLayout | None = None
        self._hero_photo: ImageTk.PhotoImage | None = None
        self._poster_photo: ImageTk.PhotoImage | None = None
        self._pill_photo: ImageTk.PhotoImage | None = None
        self._pill_hover = False
        self._hero_task: asyncio.Task[None] | None = None
        self._poster_task: asyncio.Task[None] | None = None
        self._hero_job: str | None = None
        self._pill_font = theme.font(PILL_FONT_SIZE, bold=True)

        self._hero_item = self.create_image(0, 0, anchor="nw")
        self._poster_item = self.create_image(0, 0, anchor="nw")
        self._pill_item = self.create_image(0, 0, anchor="center", state="hidden", tags="trailer")
        self._pill_text = self.create_text(
            0, 0, anchor="w", state="hidden", tags="trailer", text=t(TRAILER_TEXT),
            fill=theme.ON_COLOR,
        )
        self.text = _TextColumn(self, fg_color=theme.BG, bg_color=theme.BG, corner_radius=0)
        self.text.on_scaling = self._relayout_all
        self._text_item = self.create_window(0, 0, window=self.text, anchor="nw")
        self.tag_bind("trailer", "<Button-1>", lambda _event: self._open_trailer())
        self.tag_bind("trailer", "<Enter>", lambda _event: self._hover_pill(True))
        self.tag_bind("trailer", "<Leave>", lambda _event: self._hover_pill(False))
        self.bind("<Configure>", self._on_configure, add="+")
        self.text.bind("<Configure>", lambda _event: self._fit_height(), add="+")
        theme.on_mode_change(self, self._repaint)

    # -- state ---------------------------------------------------------------------------------

    @property
    def scaling(self) -> float:
        scaling: float = ctk.ScalingTracker.get_widget_scaling(self.text)
        return scaling

    @property
    def hero_shown(self) -> bool:
        """Whether the backdrop's room is kept (a picture, or one that may still come)."""
        return self._hero_room

    @property
    def trailer_url(self) -> str | None:
        return self._trailer

    def reserve_hero(self, keep: bool) -> None:
        """Keep the backdrop's room (empty until `set_hero`) or give it up."""
        if keep != self._hero_room:
            self._hero_room = keep
            self._relayout()

    def set_hero(self, url: str | None, *, blur: bool = False) -> None:
        """Show `url` as the backdrop (`blur`: a poster standing in for one); None: none at
        all (the poster and the text move up)."""
        hero = _Hero(url, blur) if url else None
        if hero == self._hero and self._hero_room == (hero is not None):
            return
        self._hero = hero
        self._hero_room = hero is not None
        if hero is None:
            self.itemconfigure(self._hero_item, image="")
            self._hero_photo = None
        self._relayout()
        self._load_hero()

    def set_trailer(self, url: str | None) -> None:
        """Show "▶ Watch trailer" over the backdrop, opening `url`; None hides it."""
        self._trailer = url
        self._place_pill()

    # -- layout --------------------------------------------------------------------------------

    def _on_configure(self, event: Any) -> None:
        if event.width <= 1 or event.width == self._width:
            return
        self._width = event.width
        self._relayout()
        if self._hero is not None:
            # Baked again at the new width once the resizing stops.
            if self._hero_job is not None:
                self.after_cancel(self._hero_job)
            self._hero_job = self.after(150, self._load_hero)

    def _relayout_all(self) -> None:
        """Windows' scaling changed: lay out and bake everything again."""
        self._pill_photo = None
        self._relayout()
        self._load_hero()
        self._load_poster()

    def _relayout(self) -> None:
        if self._width <= 1:
            return
        previous = self._layout
        layout = masthead_layout(self._width, self.scaling, hero=self._hero_room)
        self._layout = layout
        self.coords(self._hero_item, 0, 0)
        self.coords(self._poster_item, *layout.poster)
        self.coords(self._text_item, *layout.text)
        self.itemconfigure(self._text_item, width=layout.text_width)
        if previous is None:
            self._load_poster()
        self._place_pill()
        self._fit_height()

    def _fit_height(self) -> None:
        layout = self._layout
        if layout is None:
            return
        scaling = self.scaling
        poster_bottom = layout.poster[1] + round(POSTER_HEIGHT * scaling)
        text_bottom = layout.text[1] + self.text.winfo_reqheight()
        height = max(poster_bottom, text_bottom, layout.hero_height) + round(BLOCK_GAP * scaling)
        if int(self.cget("height")) != height:
            self.configure(height=height)

    # -- the trailer button --------------------------------------------------------------------

    def _place_pill(self) -> None:
        layout = self._layout
        shown = self._trailer is not None and layout is not None and layout.hero_height > 0
        state = "normal" if shown else "hidden"
        self.itemconfigure("trailer", state=state)
        if not shown or layout is None:
            return
        scaling = self.scaling
        font = self._pill_font
        text_width = font.measure(t(TRAILER_TEXT)) * scaling
        left_part = (PILL_PAD_LEFT + PILL_CIRCLE + PILL_TEXT_GAP) * scaling
        width = math.ceil(left_part + text_width + PILL_PAD_RIGHT * scaling)
        height = round(PILL_HEIGHT * scaling)
        image = trailer_pill((width, height), round(scaling, 3), self._pill_hover)
        self._pill_photo = ImageTk.PhotoImage(image, master=self)
        cx, cy = layout.pill_centre
        self.coords(self._pill_item, cx, cy)
        self.itemconfigure(self._pill_item, image=self._pill_photo)
        self.coords(self._pill_text, cx - width / 2 + left_part, cy)
        self.itemconfigure(self._pill_text, font=font.create_scaled_tuple(scaling))
        self.tag_raise("trailer")

    def _hover_pill(self, hover: bool) -> None:
        self._pill_hover = hover
        self.configure(cursor="hand2" if hover else "")
        self._place_pill()

    def _open_trailer(self) -> None:
        if self._trailer is not None:
            webbrowser.open(self._trailer)

    # -- images --------------------------------------------------------------------------------

    def _load_hero(self) -> None:
        self._hero_job = None
        if self._hero_task is not None:
            self._hero_task.cancel()
        layout, hero = self._layout, self._hero
        if layout is None or hero is None or layout.hero_height <= 0:
            return
        size = (self._width, layout.hero_height)
        face = hero_face(size, rgb(self, theme.BG), self.scaling, blur=hero.blur)
        self._hero_task = spawn(self._fetch_hero(hero, size, face), name="Masthead.hero")

    async def _fetch_hero(self, hero: _Hero, size: tuple[int, int], face: HeroFace) -> None:
        image = await self._images.get(hero.url, size, finish=face)
        if image is None or not self.winfo_exists() or hero != self._hero:
            return
        self._hero_photo = ImageTk.PhotoImage(image, master=self)
        self.itemconfigure(self._hero_item, image=self._hero_photo)

    def _poster_face(self) -> FramedPoster:
        scaling = self.scaling
        return FramedPoster(
            radius=round(POSTER_RADIUS * scaling),
            border=max(1, round(POSTER_BORDER * scaling)),
            edge=rgb(self, theme.PANEL_EDGE),
        )

    def _load_poster(self) -> None:
        if self._poster_task is not None:
            self._poster_task.cancel()
        scaling = self.scaling
        size = (round(POSTER_WIDTH * scaling), round(POSTER_HEIGHT * scaling))
        face = self._poster_face()
        placeholder = frame_poster(None, face, size, rgb(self, theme.PLACEHOLDER))
        self._show_poster(placeholder)
        url = self._poster_url(size[0])
        if url is not None:
            self._poster_task = spawn(self._fetch_poster(url, size, face), name="Masthead.poster")

    async def _fetch_poster(self, url: str, size: tuple[int, int], face: FramedPoster) -> None:
        image = await self._images.get(url, size, finish=face)
        if image is not None and self.winfo_exists():
            self._show_poster(image)

    def _show_poster(self, image: Image.Image) -> None:
        self._poster_photo = ImageTk.PhotoImage(image, master=self)
        self.itemconfigure(self._poster_item, image=self._poster_photo)

    def set_poster(self, poster: Callable[[int], str | None]) -> None:
        """Another poster (its url per width)."""
        self._poster_url = poster
        if self._layout is not None:
            self._load_poster()

    def _repaint(self) -> None:
        """Light ↔ dark: the background, the backdrop's fade and the poster's edge."""
        self.configure(background=theme.pick(theme.BG))
        self._load_hero()
        self._load_poster()

    def destroy(self) -> None:
        for task in (self._hero_task, self._poster_task):
            if task is not None:
                task.cancel()
        if self._hero_job is not None:
            try:
                self.after_cancel(self._hero_job)
            except tk.TclError:
                pass
        super().destroy()


# -- genre pills and the cast row -------------------------------------------------------------


class GenrePills(ctk.CTkFrame):
    """Genre names as small outlined pills, wrapped onto as many rows as `room` (logical
    units, `set_room`) needs."""

    def __init__(self, master: Any) -> None:
        super().__init__(master, fg_color="transparent")
        self._pills: list[ctk.CTkFrame] = []
        self._title_labels: tuple[str, ...] = ()
        self._room = 300.0
        self._font = theme.font(GENRE_FONT_SIZE)

    @property
    def names(self) -> tuple[str, ...]:
        return self._title_labels

    def set_names(self, names: Sequence[str]) -> None:
        names = tuple(names)
        if names == self._title_labels:
            return
        self._title_labels = names
        for pill in self._pills:
            pill.destroy()
        self._pills = []
        for name in names:
            pill = ctk.CTkFrame(
                self,
                fg_color="transparent",
                border_width=1,
                border_color=theme.EDGE,
                corner_radius=PILL_GENRE_HEIGHT // 2,
                height=PILL_GENRE_HEIGHT,
            )
            ctk.CTkLabel(
                pill,
                text=name,
                font=self._font,
                text_color=theme.BUTTON_TEXT,
                height=PILL_GENRE_HEIGHT - 4,
            ).pack(padx=PILL_GENRE_PAD, pady=2)
            self._pills.append(pill)
        self._reflow()

    def set_room(self, room: float) -> None:
        if abs(room - self._room) >= 1:
            self._room = room
            self._reflow()

    def _reflow(self) -> None:
        widths = [self._font.measure(name) + 2 * PILL_GENRE_PAD + 2 for name in self._title_labels]
        for row, indexes in enumerate(flow_rows(widths, self._room, PILL_GENRE_GAP)):
            for column, index in enumerate(indexes):
                self._pills[index].grid(
                    row=row,
                    column=column,
                    padx=(0 if column == 0 else PILL_GENRE_GAP, 0),
                    pady=(0 if row == 0 else PILL_GENRE_GAP, 0),
                    sticky="w",
                )


def _tk_rgb(widget: tk.Misc, color: str) -> RGB:
    """A Tk colour ("#rrggbb" or a name like "gray15") as 8-bit RGB."""
    red, green, blue = widget.winfo_rgb(color)
    return red >> 8, green >> 8, blue >> 8


def initials_disc(side: int, fill: RGB) -> Image.Image:
    """A `side`-pixel disc of `fill`, see-through around it (RGBA): behind a cast entry's
    initials."""
    disc = Image.new("RGBA", (side, side), (*fill, 255))
    disc.putalpha(rounded_mask((side, side), side // 2))
    return disc


class _CastEntry(ctk.CTkFrame):
    """One actor: a round photo (initials until or without one), the name and, muted, the
    part."""

    def __init__(self, master: Any, member: CastMember, images: ImageCache) -> None:
        super().__init__(master, fg_color="transparent", width=CAST_ITEM_WIDTH)
        self.member = member
        self._task: asyncio.Task[None] | None = None
        scaling: float = ctk.ScalingTracker.get_widget_scaling(self)
        side = round(CAST_PHOTO * scaling)
        # The initials on a round disc (baked: a CTkLabel's own rounded corners would widen
        # it), in the light and the dark look's chip colour.
        light, dark = (initials_disc(side, _tk_rgb(self, color)) for color in theme.CHIP)
        self._image = ctk.CTkImage(light_image=light, dark_image=dark, size=(CAST_PHOTO, CAST_PHOTO))
        self.photo = ctk.CTkLabel(
            self,
            text=initials(member.name),
            image=self._image,
            compound="center",
            width=CAST_PHOTO,
            height=CAST_PHOTO,
            corner_radius=0,
            fg_color="transparent",
            text_color=theme.TEXT_MUTED,
            font=theme.font(17, bold=True),
        )
        self.photo.pack()
        room = CAST_ITEM_WIDTH - SLACK
        for text, size, color, gap in (
            (member.name, CAST_NAME_SIZE, theme.TEXT, 6),
            (member.character, CAST_PART_SIZE, theme.TEXT_MUTED, 2),
        ):
            if not text:
                continue
            font = theme.font(size)
            # Breaks made explicit, so the entry keeps its width (see PosterCard's title).
            shown = break_lines(fit_to_lines(text, room, CAST_LINES, font.measure), room, font.measure)
            ctk.CTkLabel(
                self,
                text=shown,
                font=font,
                text_color=color,
                justify="center",
                width=CAST_ITEM_WIDTH,
                height=18,
            ).pack(pady=(gap, 0))
        url = member.photo_url()
        if url is not None:
            self._task = spawn(self._load(images, url), name="CastEntry.photo")

    async def _load(self, images: ImageCache, url: str) -> None:
        scaling: float = ctk.ScalingTracker.get_widget_scaling(self)
        side = round(CAST_PHOTO * scaling)
        image = await images.get(url, (side, round(side * 1.5)), finish=RoundPhoto(side))
        if image is None or not self.winfo_exists():
            return
        self._image = ctk.CTkImage(light_image=image, dark_image=image, size=(CAST_PHOTO, CAST_PHOTO))
        self.photo.configure(image=self._image, text="")

    def destroy(self) -> None:
        if self._task is not None:
            self._task.cancel()
        super().destroy()


class CastRow(ctk.CTkFrame):
    """The cast: one entry per actor, wrapping onto more rows when narrow; hidden while
    empty."""

    def __init__(self, master: Any, *, images: ImageCache) -> None:
        super().__init__(master, fg_color="transparent")
        self._images = images
        self._flow = FlexGrid(
            self, item_width=CAST_ITEM_WIDTH, pad_x=CAST_GAP, pad_y=4, center=False
        )
        self._flow.pack(fill="x")
        self._members: tuple[CastMember, ...] = ()
        self.entries: list[_CastEntry] = []

    @property
    def members(self) -> tuple[CastMember, ...]:
        return self._members

    def set_members(self, members: Sequence[CastMember]) -> None:
        members = tuple(members)
        if members == self._members:
            return
        self._members = members
        for entry in self.entries:
            entry.destroy()
        self.entries = [_CastEntry(self._flow, member, self._images) for member in members]
        self._flow.relayout()


# -- the window --------------------------------------------------------------------------------


class TitleInfoWindow(ctk.CTkToplevel):
    """A film's or show's info window (not modal; see the module docstring). Subclasses fill
    the text column (`_set_name`, `_set_meta`, `set_genres`, `_set_summary`, `_set_note`, the
    actions in `buttons`), the backdrop and the trailer (`masthead`), the cast (`cast`) and
    their own part under it (`content`)."""

    def __init__(
        self,
        master: ctk.CTk,
        *,
        title: str,
        images: ImageCache,
        poster: Callable[[int], str | None],
    ) -> None:
        super().__init__(master, fg_color=theme.BG)
        self._images = images
        self._summary_text = ""
        self._summary_expanded = False
        self._text_width = float(
            WINDOW_WIDTH - 2 * PAD_X - POSTER_WIDTH - POSTER_GAP - 16  # a first guess
        )
        self.title(title)
        set_window_icon(self)
        self.transient(master)
        # CTkToplevel only applies its dark Windows title bar reliably from resizable().
        self.resizable(True, True)
        self.minsize(MIN_WIDTH, MIN_HEIGHT)
        self._place_over(master)

        self.body = ctk.CTkScrollableFrame(
            self,
            fg_color="transparent",
            corner_radius=0,
            border_width=0,
            scrollbar_button_color=theme.SCROLLBAR,
            scrollbar_button_hover_color=theme.EDGE,
        )
        self.body.pack(fill="both", expand=True)
        self.masthead = Masthead(self.body, images=images, poster=poster)
        self.masthead.pack(fill="x")
        self._build_text(self.masthead.text, title)
        self.cast = CastRow(self.body, images=images)
        self.content = ctk.CTkFrame(self.body, fg_color="transparent")
        self.content.pack(fill="x", padx=PAD_X, pady=(0, PAD_Y))

        self.bind("<Escape>", lambda _event: self.destroy())
        # CTkToplevel can open behind its parent on Windows; raise it once it is mapped.
        self._activate_job: str | None = self.after(150, self._activate)

    # -- layout ------------------------------------------------------------------------------

    def _place_over(self, master: ctk.CTk) -> None:
        """Open at `window_size` on the work area of the screen `master` is on, centred there
        (CTk scales the size but not the position: see MainWindow._place)."""
        scaling = ctk.ScalingTracker.get_window_scaling(self)
        centre_x = master.winfo_rootx() + master.winfo_width() // 2
        centre_y = master.winfo_rooty() + master.winfo_height() // 2
        area = work_area(centre_x, centre_y)
        if area is None:  # off Windows: the primary screen, physical pixels
            screen_w, screen_h = self.wm_maxsize()
            area = (0, 0, screen_w, screen_h)
        left, top, right, bottom = area
        width, height = window_size((right - left) / scaling, (bottom - top) / scaling)
        width_px, height_px = round(width * scaling), round(height * scaling)
        x = left + max(0, (right - left - width_px) // 2)
        y = top + max(0, (bottom - top - height_px) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _build_text(self, info: ctk.CTkFrame, title: str) -> None:
        info.grid_columnconfigure(0, weight=1)
        self._title_label = dialog_kit.text_label(info, title, size=TITLE_SIZE, bold=True)
        self._title_label.grid(row=0, column=0, sticky="w")
        self._meta = dialog_kit.text_label(
            info, "", size=dialog_kit.DETAIL_SIZE, color=theme.TEXT_MUTED
        )
        self._meta.grid(row=1, column=0, sticky="w", pady=(4, 0))
        self.genres = GenrePills(info)
        self.genres.grid(row=2, column=0, sticky="w", pady=(10, 0))
        self._summary_font = theme.font(dialog_kit.DETAIL_SIZE)
        self._summary = dialog_kit.text_label(info, "", size=dialog_kit.DETAIL_SIZE)
        self._summary.grid(row=3, column=0, sticky="w", pady=(12, 0))
        self._more = ctk.CTkLabel(
            info,
            text=t(MORE_TEXT),
            font=theme.font(dialog_kit.SMALL_SIZE, underline=True),
            text_color=theme.LINK,
            cursor="hand2",
            height=20,
        )
        self._more.grid(row=4, column=0, sticky="w", pady=(2, 0))
        self._more.bind("<Button-1>", lambda _event: self._expand_summary())
        self._note = dialog_kit.text_label(
            info, "", size=dialog_kit.DETAIL_SIZE, color=theme.TEXT_MUTED
        )
        self._note.grid(row=5, column=0, sticky="w", pady=(10, 0))
        self.buttons = ctk.CTkFrame(info, fg_color="transparent")
        self.buttons.grid(row=6, column=0, sticky="w", pady=(16, 0))
        self._hint = dialog_kit.text_label(
            info, t(NO_KEY_HINT), size=dialog_kit.SMALL_SIZE, color=theme.TEXT_MUTED
        )
        self._hint.grid(row=7, column=0, sticky="w", pady=(12, 0))
        self._hint.grid_remove()
        for widget in (self._meta, self.genres, self._summary, self._more, self._note):
            widget.grid_remove()
        info.bind("<Configure>", self._on_info_resize, add="+")

    def _on_info_resize(self, event: Any) -> None:
        if event.width <= 1:
            return
        width = unscaled_width(self, event.width)
        if abs(width - self._text_width) < 1:
            return
        self._text_width = width
        self._fit_text()

    def _fit_text(self) -> None:
        """Wrap the texts at the text column's width; the synopsis at most SUMMARY_LINES lines
        until "More" is clicked."""
        width = max(1.0, self._text_width - SLACK)
        wrap = max(1, round(width))
        for label in (self._title_label, self._meta, self._note, self._hint):
            label.configure(wraplength=wrap)
        self.genres.set_room(width)
        summary = self._summary_text
        if not summary:
            self._summary.grid_remove()
            self._more.grid_remove()
            return
        if self._summary_expanded:
            text, cut = summary, False
        else:
            text, cut = collapsed_summary(summary, width, self._summary_font.measure)
        self._summary.configure(text=text, wraplength=wrap)
        self._summary.grid()
        if cut:
            self._more.grid()
        else:
            self._more.grid_remove()

    def _expand_summary(self) -> None:
        self._summary_expanded = True
        self._fit_text()

    # -- the text column ---------------------------------------------------------------------

    @property
    def summary_text(self) -> str:
        return self._summary_text

    @property
    def hint_shown(self) -> bool:
        return bool(self._hint.winfo_manager())

    def _set_name(self, name: str) -> None:
        self.title(name)
        self._title_label.configure(text=name)

    def _set_meta(self, text: str) -> None:
        self._show_line(self._meta, text)

    def _set_note(self, text: str | None) -> None:
        """The muted line above the actions (a series' next episode)."""
        self._show_line(self._note, text or "")

    def set_genres(self, names: Sequence[str]) -> None:
        self.genres.set_names(names)
        if names:
            self.genres.grid()
        else:
            self.genres.grid_remove()

    def _set_summary(self, text: str) -> None:
        text = text.strip()
        if text != self._summary_text:
            self._summary_text = text
            self._fit_text()

    def show_hint(self, show: bool) -> None:
        """"Add a TMDB key in Settings for posters, trailers and cast"."""
        if show:
            self._hint.grid()
        else:
            self._hint.grid_remove()

    @staticmethod
    def _show_line(label: ctk.CTkLabel, text: str) -> None:
        label.configure(text=text)
        if text:
            label.grid()
        else:
            label.grid_remove()

    def backdrop_size(self) -> str:
        """TMDB's backdrop size for this window: its width now, at least the width it opens
        with (before it is shown its width is unknown)."""
        scaling: float = ctk.ScalingTracker.get_widget_scaling(self)
        return backdrop_size(max(self.masthead.winfo_width(), round(WINDOW_WIDTH * scaling)))

    def show_cast(self, members: Sequence[CastMember]) -> None:
        self.cast.set_members(members)
        if members and not self.cast.winfo_manager():
            self.cast.pack(fill="x", padx=PAD_X - CAST_GAP, pady=(0, BLOCK_GAP), before=self.content)
        elif not members:
            self.cast.pack_forget()

    # -- window ------------------------------------------------------------------------------

    def _activate(self) -> None:
        self._activate_job = None
        if self.winfo_exists():
            self.lift()
            self.focus_force()

    def raise_window(self) -> None:
        """Bring the window to the front (also from minimised)."""
        if self.state() == "iconic":
            self.deiconify()
        self.lift()
        self.focus_force()

    def destroy(self) -> None:
        if self._activate_job is not None:
            try:
                self.after_cancel(self._activate_job)
            except tk.TclError:
                pass
            self._activate_job = None
        super().destroy()


def exists(widget: Any) -> bool:
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:  # the whole interpreter is gone
        return False
