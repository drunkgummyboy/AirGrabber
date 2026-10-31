"""The info window's shared look, for series (app/ui/show_details.py) and films
(app/ui/movie_details.py), and where it shows the releases (app/ui/release_panel.py). Two layouts
(Settings' `info_layout`, read when a window opens):

"side" (InfoLayout.SIDE, the default): a backdrop band across the top (TMDB's backdrop, or the
poster blurred when TMDB has none) with "▶ Watch trailer" centred on it (it opens on YouTube);
under it on the left a column with the poster reaching up into the band, then the title, a meta
line, genre pills, the synopsis (More shows all of it), a muted line the window may use (a
series' next episode), the actions (wrapping), without a TMDB key a plain hint, and a compact
cast (three actors, "+N more" unfolds the rest). The column scrolls on its own when it is too
tall. On the right a film's releases, or a series' episodes (`content`), which "☰ Choose…"
swaps for that episode's releases under a "← Episodes" bar.

"tabs" (InfoLayout.TABS): a taller backdrop; under it the poster beside the text column, the
poster's top level with the title's and its bottom with the actions' (the actions sit at the
bottom of the column; the synopsis gets the lines that fit). Then tabs: "Releases (18)" | "Cast"
for a film, "Episodes" | "Releases" | "Cast" for a series ("Releases · S03E05" once an episode
was chosen). The whole window scrolls; the Releases tab is as tall as the window, so its table
fills it once scrolled there. The Cast tab is there while there is a cast.

A film's releases start searching when its window opens; a series' when "☰ Choose…" picks an
episode (`open_releases`). The panel's own chips can pick another episode: the "← Episodes"
bar's label or the tab follows.

Tk can't draw a widget see-through over an image, so the backdrop, the poster and the trailer
button are drawn on a canvas (`Masthead`) in physical pixels: the backdrop and the poster are
baked by Pillow at the screen's scaling (in ImageCache's worker thread, `HeroFace`,
`FramedPoster`), the button is an RGBA image with canvas text on it. In "tabs" the text column is
a frame on that canvas, below the line where the fade turns solid; in "side" the poster's lower
part is drawn on a second canvas at the top of the column (`Masthead.foot`), right under the
band. A switch between light and dark, or another Windows scaling, bakes them again. Without a
TMDB key (or while nothing is known yet) there is no backdrop: the poster and the text start at
the top.

The window opens about HEIGHT_SHARE (85 %) of the work area's height and up to WINDOW_WIDTH
("tabs") or SIDE_WINDOW_WIDTH ("side") wide, centred on the screen it opens on, never larger
than that screen's work area less SCREEN_MARGIN (`window_size`); nothing about its size is
remembered.

It has no Windows title bar (app/ui/borderless.py): it is dragged by the backdrop band (the
Masthead canvas; never from the trailer button or the poster, tagged NO_DRAG), and its round ✕
sits in the backdrop's top-right corner, which the layouts keep free. Over the picture the ✕
shows that picture's piece around its disc (`Masthead.under`, repainted when the picture, the
layout or the "tabs" scroll position changes). Without a backdrop "side" keeps a plain band
SIDE_STRIP tall for it (the poster starts where it did) and "tabs" narrows the text column by
the ✕'s clearance; "tabs" starts its scrollbar under the ✕. Escape closes it as before.
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
from app.models import CastMember, InfoLayout, MovieDetails
from app.ui import dialog_kit, theme
from app.ui.borderless import CLOSE_GAP, CLOSE_ROOM, NO_DRAG, Borderless, clearance, make_borderless
from app.ui.branding import set_window_icon
from app.ui.poster_card import rgb
from app.ui.release_panel import IndexerTarget, PanelServices, ReleasePanel, TvTarget
from app.ui.tasks import spawn
from app.ui.text_fit import SLACK, break_lines, elide_to_width, fit_to_lines, unscaled_width
from app.ui.tooltip import work_area
from app.ui.widgets import FlexGrid, Tabview

if TYPE_CHECKING:
    from app.services import ImageCache

log = logging.getLogger(__name__)

RGB = tuple[int, int, int]

# Logical (unscaled) CTk units.
WINDOW_WIDTH = 1400  # at most ("tabs")
SIDE_WINDOW_WIDTH = 1280  # at most ("side")
HEIGHT_SHARE = 0.85  # of the work area's height
SCREEN_MARGIN = 80  # the window stays at least this much narrower than the work area
MIN_WIDTH = 560
MIN_HEIGHT = 360
SIDE_MIN_WIDTH = 800  # "side": the info column and a usable releases table (where it fits)
PAD_X = dialog_kit.PAD_X
PAD_Y = dialog_kit.PAD_Y
BLOCK_GAP = dialog_kit.BLOCK_GAP  # under the poster / text
BUTTON_GAP = 12  # between the actions
TEXT_RISE = 16  # the text starts this far above the backdrop's lower edge (the fade is solid)
FADE_FROM = 0.30  # the backdrop starts fading into the background this far down
SUMMARY_LINES = 6  # collapsed at most; "More" shows the rest
MIN_SUMMARY_LINES = 2  # "tabs": at least this many, even when the text then ends below the poster
POSTER_RADIUS = 10
POSTER_BORDER = 2
# "tabs": about 230 units of backdrop above where the poster and the text start.
HERO_HEIGHT = 246
POSTER_WIDTH = 176
POSTER_HEIGHT = 264
POSTER_GAP = 24  # between the poster and the text beside it
PILL_TOP = 0.42  # the trailer button's centre, as a share of the backdrop's height
TAB_CHROME = 56  # the tab bar and the tab view's margins above and below a page
TAB_BOTTOM = 12  # under the tab view
RELEASES_MIN_HEIGHT = 320  # the Releases tab's page at least
TAB_SETTLE_MS = 150  # after CTk's own 100 ms tidy-up of the pages it doesn't show
# "side".
SIDE_BAND = 130  # the backdrop band across the top
SIDE_STRIP = CLOSE_ROOM  # the band without a backdrop: the close button's corner stays free
SIDE_LEFT_WIDTH = 320  # the info column
SIDE_PAD = 20  # the info column's left side
SIDE_POSTER_WIDTH = 112
SIDE_POSTER_HEIGHT = 168
SIDE_OVERLAP = 56  # how far the poster reaches up into the band
SIDE_TOP = 20  # without a backdrop the poster starts this far down
SIDE_POSTER_GAP = 14  # under the poster, above the title
SIDE_PILL_TOP = 0.45
SIDE_SUMMARY_LINES = 4
SIDE_CAST_SHOWN = 3  # the compact cast's actors before "+N more"
RIGHT_PAD = 16  # around the right side's content
# "▶ Watch trailer": a dark see-through pill over the backdrop.
PILL_HEIGHT = 46
PILL_PAD_LEFT = 8
PILL_CIRCLE = 30
PILL_TEXT_GAP = 10
PILL_PAD_RIGHT = 18
PILL_FONT_SIZE = 14
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
# The cast: the Cast tab's grid ("tabs") and the compact list ("side").
CAST_PHOTO = 56
CAST_ITEM_WIDTH = 88
CAST_NAME_SIZE = 12
CAST_PART_SIZE = 11
CAST_LINES = 2  # the name's
CAST_PART_LINES = 1
CAST_GAP = 6  # between two cast entries (each side)
CAST_LIST_PHOTO = 28
CAST_LIST_GAP = 8  # between the photo and the text, and under each actor
# TMDB's backdrop widths, the smallest first.
TMDB_BACKDROP_WIDTHS = (300, 780, 1280)

_SUPERSAMPLE = 4  # rounded shapes are drawn this much larger, then scaled down

# Shown through t().
TRAILER_TEXT = N_("Watch trailer")
NO_KEY_HINT = N_("Add a TMDB key in Settings for posters, trailers and cast")
MORE_TEXT = N_("More")
EPISODES_TAB = N_("Episodes")
RELEASES_TAB = N_("Releases")
CAST_TAB = N_("Cast")
BACK_TEXT = N_("← Episodes")
CHOOSE_FIRST_TEXT = N_("Click ☰ Choose… on an episode to see its releases here.")
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
    text: str, width: float, measure: Callable[[str], float], lines: int = SUMMARY_LINES
) -> tuple[str, bool]:
    """The synopsis wrapped into at most `lines` lines of `width`, and whether that cut it
    short (then "More" shows the rest)."""
    fitted = fit_to_lines(text, width, lines, measure)
    return fitted, fitted != text


def target_code(target: TvTarget) -> str:
    """"S03E05", or "S03" for a season pack."""
    if target.episode is None:
        return f"S{target.season:02d}"
    return f"S{target.season:02d}E{target.episode:02d}"


def releases_tab_text(target: IndexerTarget | None, count: int | None) -> str:
    """The Releases tab's name: "Releases · S03E05" for an episode (or "· S03" for a pack),
    "Releases (18)" for a film once its search counted, else "Releases"."""
    if isinstance(target, TvTarget):
        return t("Releases · {code}", code=target_code(target))
    if target is not None and count is not None:
        return t("Releases ({n})", n=count)
    return t(RELEASES_TAB)


def more_text(hidden: int) -> str:
    """The compact cast's "+5 more"."""
    return t("+{n} more", n=hidden)


@dataclass(frozen=True, slots=True)
class MastheadLayout:
    """Where the masthead's parts go, in physical pixels (`masthead_layout`)."""

    hero_height: int  # 0: no backdrop
    poster: tuple[int, int]  # top-left (in "side" from the band's top; it reaches below it)
    poster_size: tuple[int, int]
    text: tuple[int, int]  # top-left of the text column ("side": where it starts under the poster)
    text_width: int
    pill_centre: tuple[int, int]
    band_height: int  # "side": the band's (SIDE_STRIP without a backdrop); "tabs": hero_height


def masthead_layout(
    width: int, scaling: float, *, hero: bool, layout: InfoLayout = InfoLayout.TABS
) -> MastheadLayout:
    """The masthead at `width` physical pixels and Windows' `scaling`.

    "tabs": with a backdrop, the poster and the text start TEXT_RISE above its lower edge (the
    poster beside the text, their tops level); without one both start at the top, the text
    column narrower by the close button's clearance. "side": the band is SIDE_BAND tall and the
    poster reaches SIDE_OVERLAP up into it; without a backdrop the band is a plain SIDE_STRIP
    (the close button's corner) and the poster starts SIDE_TOP down. The text column starts
    under the poster."""

    def px(units: float) -> int:
        return round(units * scaling)

    if layout is InfoLayout.SIDE:
        hero_height = px(SIDE_BAND) if hero else 0
        poster_y = hero_height - px(SIDE_OVERLAP) if hero else px(SIDE_TOP)
        poster_size = (px(SIDE_POSTER_WIDTH), px(SIDE_POSTER_HEIGHT))
        return MastheadLayout(
            hero_height=hero_height,
            poster=(px(SIDE_PAD), poster_y),
            poster_size=poster_size,
            text=(px(SIDE_PAD), poster_y + poster_size[1] + px(SIDE_POSTER_GAP)),
            text_width=max(1, px(SIDE_LEFT_WIDTH) - 2 * px(SIDE_PAD)),
            pill_centre=(width // 2, round(hero_height * SIDE_PILL_TOP)),
            band_height=hero_height if hero else px(SIDE_STRIP),
        )
    pad_x = px(PAD_X)
    text_x = pad_x + px(POSTER_WIDTH) + px(POSTER_GAP)
    if hero:
        hero_height = px(HERO_HEIGHT)
        top = hero_height - px(TEXT_RISE)
        corner = 0
    else:
        hero_height = 0
        top = px(PAD_Y)
        corner = px(clearance(PAD_X))  # the title stays clear of the close button
    return MastheadLayout(
        hero_height=hero_height,
        poster=(pad_x, top),
        poster_size=(px(POSTER_WIDTH), px(POSTER_HEIGHT)),
        text=(text_x, top),
        text_width=max(1, width - text_x - pad_x - corner),
        pill_centre=(width // 2, round(hero_height * PILL_TOP)),
        band_height=hero_height,
    )


def window_size(
    work_width: float, work_height: float, max_width: int = WINDOW_WIDTH
) -> tuple[int, int]:
    """The info window's size in CTk units for a work area of this size (CTk units): about
    HEIGHT_SHARE of its height and up to `max_width` wide, never wider than the area less
    SCREEN_MARGIN nor taller than the area; at least the minimum size where the area allows."""
    width = min(max_width, int(work_width) - SCREEN_MARGIN)
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
    """The text column; tells the masthead when Windows' scaling changed."""

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
    """The backdrop, the poster and the trailer button on one canvas (see the module
    docstring); in "tabs" also the text column (`text`), in "side" the poster's lower part on
    `foot` (a canvas in `foot_master`, which the window places under the band). `poster` gives
    the poster's url for that many physical pixels wide (None: the placeholder).
    `reserve_hero` keeps the backdrop's room while it may still come, so the layout doesn't
    jump when it does."""

    def __init__(
        self,
        master: Any,
        *,
        images: ImageCache,
        poster: Callable[[int], str | None],
        layout: InfoLayout = InfoLayout.TABS,
        foot_master: Any = None,
    ) -> None:
        super().__init__(
            master, highlightthickness=0, borderwidth=0, height=1, background=theme.pick(theme.BG)
        )
        self.layout = layout
        self._images = images
        self._poster_url = poster
        self._hero: _Hero | None = None
        self._hero_room = False  # the backdrop's room is kept (it shows or may still come)
        self._trailer: str | None = None
        self._width = 0  # physical px, from the last <Configure>
        self._layout: MastheadLayout | None = None
        self._hero_photo: ImageTk.PhotoImage | None = None
        self._hero_image: Image.Image | None = None  # what `_hero_photo` shows (`under`)
        # Told when what lies under the window's close button may have changed (`under`).
        self.on_backdrop_change: Callable[[], None] | None = None
        self._poster_photo: ImageTk.PhotoImage | None = None
        self._pill_photo: ImageTk.PhotoImage | None = None
        self._pill_hover = False
        self._hero_task: asyncio.Task[None] | None = None
        self._poster_task: asyncio.Task[None] | None = None
        self._hero_job: str | None = None
        self._pill_font = theme.font(PILL_FONT_SIZE, bold=True)
        self._probe: ctk.CTkBaseClass | None = None  # a CTk widget that knows the scaling

        self._hero_item = self.create_image(0, 0, anchor="nw")
        self._poster_item = self.create_image(0, 0, anchor="nw", tags=(NO_DRAG,))
        self._pill_item = self.create_image(0, 0, anchor="center", state="hidden", tags="trailer")
        self._pill_text = self.create_text(
            0, 0, anchor="w", state="hidden", tags="trailer", text=t(TRAILER_TEXT),
            fill=theme.ON_COLOR,
        )
        self.text: _TextColumn | None = None
        self.foot: tk.Canvas | None = None
        self._text_item: int | None = None
        self._foot_poster: int | None = None
        if layout is InfoLayout.SIDE:
            self.foot = tk.Canvas(
                foot_master if foot_master is not None else master,
                highlightthickness=0,
                borderwidth=0,
                height=1,
                background=theme.pick(theme.BG),
            )
            self._foot_poster = self.foot.create_image(0, 0, anchor="nw")
        else:
            self.text = _TextColumn(self, fg_color=theme.BG, bg_color=theme.BG, corner_radius=0)
            self.watch_scaling(self.text)
            self._text_item = self.create_window(0, 0, window=self.text, anchor="nw")
            self.text.bind("<Configure>", lambda _event: self._fit_height(), add="+")
        self.tag_bind("trailer", "<Button-1>", lambda _event: self._open_trailer())
        self.tag_bind("trailer", "<Enter>", lambda _event: self._hover_pill(True))
        self.tag_bind("trailer", "<Leave>", lambda _event: self._hover_pill(False))
        self.bind("<Configure>", self._on_configure, add="+")
        self.bind("<Configure>", lambda _event: self._backdrop_changed(), add="+")
        theme.on_mode_change(self, self._repaint)
        if layout is InfoLayout.SIDE:
            self._relayout()  # the poster's place doesn't wait for the band's width

    # -- state ---------------------------------------------------------------------------------

    def placed(self) -> None:
        """"side": the window has gridded the band; it hides while there is no backdrop."""
        self._fit_height()

    def watch_scaling(self, widget: _TextColumn) -> None:
        """Lay out and bake again when Windows' scaling changes (`widget` hears it)."""
        widget.on_scaling = self._relayout_all
        self._probe = widget

    @property
    def scaling(self) -> float:
        if self._probe is not None:
            scaling: float = ctk.ScalingTracker.get_widget_scaling(self._probe)
            return scaling
        window: float = ctk.ScalingTracker.get_window_scaling(self.winfo_toplevel())
        return window

    @property
    def hero_shown(self) -> bool:
        """Whether the backdrop's room is kept (a picture, or one that may still come)."""
        return self._hero_room

    @property
    def trailer_url(self) -> str | None:
        return self._trailer

    @property
    def poster_size(self) -> tuple[int, int]:
        """The poster's size in physical pixels."""
        return masthead_layout(1, self.scaling, hero=False, layout=self.layout).poster_size

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
            self._hero_image = None
        self._relayout()
        self._load_hero()
        self._backdrop_changed()

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
        side = self.layout is InfoLayout.SIDE
        if self._width <= 1 and not side:
            return
        previous = self._layout
        layout = masthead_layout(
            max(1, self._width), self.scaling, hero=self._hero_room, layout=self.layout
        )
        self._layout = layout
        self.coords(self._hero_item, 0, 0)
        self.coords(self._poster_item, *layout.poster)
        if self._text_item is not None:
            self.coords(self._text_item, *layout.text)
            self.itemconfigure(self._text_item, width=layout.text_width)
        if self.foot is not None and self._foot_poster is not None:
            x, y = layout.poster
            self.foot.coords(self._foot_poster, x, y - layout.band_height)
        if previous is None:
            self._load_poster()
        self._place_pill()
        self._fit_height()

    def _fit_height(self) -> None:
        layout = self._layout
        if layout is None:
            return
        poster_bottom = layout.poster[1] + layout.poster_size[1]
        if self.foot is not None:
            # "side": the band as tall as the backdrop (without one the plain strip for the
            # close button); the poster's lower part on the foot, with room under it.
            band = layout.band_height
            if band > 0:
                if not self.winfo_manager():
                    self.grid()
                if int(self.cget("height")) != band:
                    self.configure(height=band)
            elif self.winfo_manager():
                self.grid_remove()
            foot = poster_bottom - band + round(SIDE_POSTER_GAP * self.scaling)
            if int(self.foot.cget("height")) != foot:
                self.foot.configure(height=foot)
            return
        assert self.text is not None
        text_bottom = layout.text[1] + self.text.winfo_reqheight()
        height = max(poster_bottom, text_bottom, layout.hero_height) + round(
            BLOCK_GAP * self.scaling
        )
        if int(self.cget("height")) != height:
            self.configure(height=height)

    # -- the trailer button --------------------------------------------------------------------

    def _place_pill(self) -> None:
        layout = self._layout
        shown = (
            self._trailer is not None
            and layout is not None
            and layout.hero_height > 0
            and self._width > 1
        )
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
        if layout is None or hero is None or layout.hero_height <= 0 or self._width <= 1:
            return
        size = (self._width, layout.hero_height)
        face = hero_face(size, rgb(self, theme.BG), self.scaling, blur=hero.blur)
        self._hero_task = spawn(self._fetch_hero(hero, size, face), name="Masthead.hero")

    async def _fetch_hero(self, hero: _Hero, size: tuple[int, int], face: HeroFace) -> None:
        image = await self._images.get(hero.url, size, finish=face)
        if image is None or not self.winfo_exists() or hero != self._hero:
            return
        self._hero_photo = ImageTk.PhotoImage(image, master=self)
        self._hero_image = image
        self.itemconfigure(self._hero_item, image=self._hero_photo)
        self._backdrop_changed()

    # -- under the window's close button ---------------------------------------------------------

    def under(self, widget: tk.Misc) -> Image.Image | None:
        """What of the backdrop lies under `widget` (the window's close button, over the band's
        top-right corner), as big as it is (physical pixels), RGB, the background where the
        backdrop doesn't reach; None when no backdrop shows under it."""
        image = self._hero_image
        if image is None or not exists(widget) or not exists(self):
            return None
        try:
            width, height = widget.winfo_width(), widget.winfo_height()
            if width <= 1 or height <= 1:  # not laid out yet
                width, height = widget.winfo_reqwidth(), widget.winfo_reqheight()
            x = widget.winfo_rootx() - self.winfo_rootx()
            y = widget.winfo_rooty() - self.winfo_rooty()
        except tk.TclError:  # going with its window
            return None
        left, top = max(0, x), max(0, y)
        right, bottom = min(image.width, x + width), min(image.height, y + height)
        if right <= left or bottom <= top:
            return None
        piece = Image.new("RGB", (width, height), rgb(self, theme.BG))
        piece.paste(image.crop((left, top, right, bottom)), (left - x, top - y))
        return piece

    def _backdrop_changed(self) -> None:
        if self.on_backdrop_change is not None:
            self.on_backdrop_change()

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
        size = self.poster_size
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
        if self.foot is not None and self._foot_poster is not None:
            self.foot.itemconfigure(self._foot_poster, image=self._poster_photo)

    def set_poster(self, poster: Callable[[int], str | None]) -> None:
        """Another poster (its url per width)."""
        self._poster_url = poster
        if self._layout is not None:
            self._load_poster()

    def _repaint(self) -> None:
        """Light ↔ dark: the background, the backdrop's fade and the poster's edge."""
        self.configure(background=theme.pick(theme.BG))
        if self.foot is not None:
            self.foot.configure(background=theme.pick(theme.BG))
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


# -- genre pills, the actions, the cast ---------------------------------------------------------


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


class FlowRow(ctk.CTkFrame):
    """The actions: widgets (children of this frame) side by side, wrapping onto another row
    when `room` (CTk units, `set_room`) runs out. `set_items` says which show, in order."""

    def __init__(self, master: Any, *, gap: int = BUTTON_GAP) -> None:
        super().__init__(master, fg_color="transparent", corner_radius=0)
        self._gap = gap
        self._room = 10_000.0
        self._items: list[tk.Widget] = []
        self._rows: list[ctk.CTkFrame] = []
        self._shape: tuple[tuple[str, ...], tuple[tuple[int, ...], ...]] | None = None
        self._watched: set[str] = set()
        self._job: str | None = None

    @property
    def items(self) -> tuple[tk.Widget, ...]:
        return tuple(self._items)

    def set_items(self, widgets: Sequence[tk.Widget], *, destroy_old: bool = False) -> None:
        """Show `widgets` in this order; the ones shown before and not now are taken away
        (destroyed with `destroy_old`)."""
        keep = {str(widget) for widget in widgets}
        for widget in self._items:
            if str(widget) in keep or not exists(widget):
                continue
            if destroy_old:
                widget.destroy()
            else:
                widget.pack_forget()
        self._items = list(widgets)
        for widget in widgets:
            if str(widget) not in self._watched:
                self._watched.add(str(widget))
                widget.bind("<Configure>", lambda _event: self._later(), add="+")
        self._shape = None
        self._reflow()

    def set_room(self, room: float) -> None:
        if abs(room - self._room) >= 1:
            self._room = room
            self._reflow()

    def _later(self) -> None:
        if self._job is None:
            self._job = self.after_idle(self._reflow)

    def _reflow(self) -> None:
        self._job = None
        items = [widget for widget in self._items if exists(widget)]
        scaling: float = ctk.ScalingTracker.get_widget_scaling(self)
        widths = [widget.winfo_reqwidth() / scaling for widget in items]
        rows = flow_rows(widths, self._room, self._gap)
        shape = (tuple(str(widget) for widget in items), tuple(tuple(row) for row in rows))
        if shape == self._shape:
            return
        self._shape = shape
        while len(self._rows) < len(rows):
            frame = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0, height=1)
            # Under the widgets it holds (they are this frame's siblings, made before it).
            self.tk.call("lower", frame._w)
            self._rows.append(frame)
        for widget in items:
            widget.pack_forget()
        for index, frame in enumerate(self._rows):
            if index < len(rows):
                frame.pack(anchor="w", pady=(0 if index == 0 else self._gap, 0))
            else:
                frame.pack_forget()
        for index, row in enumerate(rows):
            for column, item in enumerate(row):
                items[item].pack(
                    in_=self._rows[index], side="left", padx=(0 if column == 0 else self._gap, 0)
                )

    def destroy(self) -> None:
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None
        super().destroy()


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


class _CastPhoto(ctk.CTkLabel):
    """An actor's round photo, `size` units across: the initials on a disc until (or without)
    one."""

    def __init__(
        self, master: Any, member: CastMember, images: ImageCache, *, size: int, font_size: int
    ) -> None:
        self._units = size
        self._photo_task: asyncio.Task[None] | None = None
        scaling: float = ctk.ScalingTracker.get_widget_scaling(master)
        side = round(size * scaling)
        # The initials on a round disc (baked: a CTkLabel's own rounded corners would widen
        # it), in the light and the dark look's chip colour.
        light, dark = (initials_disc(side, _tk_rgb(master, color)) for color in theme.CHIP)
        self._picture = ctk.CTkImage(light_image=light, dark_image=dark, size=(size, size))
        super().__init__(
            master,
            text=initials(member.name),
            image=self._picture,
            compound="center",
            width=size,
            height=size,
            corner_radius=0,
            fg_color="transparent",
            text_color=theme.TEXT_MUTED,
            font=theme.font(font_size, bold=True),
        )
        url = member.photo_url()
        if url is not None:
            self._photo_task = spawn(self._load(images, url), name="CastPhoto.load")

    async def _load(self, images: ImageCache, url: str) -> None:
        scaling: float = ctk.ScalingTracker.get_widget_scaling(self)
        side = round(self._units * scaling)
        image = await images.get(url, (side, round(side * 1.5)), finish=RoundPhoto(side))
        if image is None or not self.winfo_exists():
            return
        size = (self._units, self._units)
        self._picture = ctk.CTkImage(light_image=image, dark_image=image, size=size)
        self.configure(image=self._picture, text="")

    def destroy(self) -> None:
        if self._photo_task is not None:
            self._photo_task.cancel()
        super().destroy()


class _CastEntry(ctk.CTkFrame):
    """One actor in the Cast tab's grid: a round photo, the name and, muted, the part."""

    def __init__(self, master: Any, member: CastMember, images: ImageCache) -> None:
        super().__init__(master, fg_color="transparent", width=CAST_ITEM_WIDTH)
        self.member = member
        self.photo = _CastPhoto(self, member, images, size=CAST_PHOTO, font_size=16)
        self.photo.pack()
        room = CAST_ITEM_WIDTH - SLACK
        for text, size, color, lines, gap in (
            (member.name, CAST_NAME_SIZE, theme.TEXT, CAST_LINES, 6),
            (member.character, CAST_PART_SIZE, theme.TEXT_MUTED, CAST_PART_LINES, 2),
        ):
            if not text:
                continue
            font = theme.font(size)
            # Breaks made explicit, so the entry keeps its width (see PosterCard's title).
            shown = break_lines(fit_to_lines(text, room, lines, font.measure), room, font.measure)
            ctk.CTkLabel(
                self,
                text=shown,
                font=font,
                text_color=color,
                justify="center",
                width=CAST_ITEM_WIDTH,
                height=18,
            ).pack(pady=(gap, 0))


class CastRow(ctk.CTkFrame):
    """The Cast tab: one entry per actor, wrapping onto more rows when narrow."""

    def __init__(self, master: Any, *, images: ImageCache) -> None:
        super().__init__(master, fg_color="transparent")
        self._images = images
        self._flow = FlexGrid(
            self, item_width=CAST_ITEM_WIDTH, pad_x=CAST_GAP, pad_y=8, center=False
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

    def set_room(self, room: float) -> None:
        """The grid fits itself to its width."""


class _CastLine(ctk.CTkFrame):
    """One actor in the compact cast: a small round photo, the name and, muted, the part, each
    on one line."""

    def __init__(self, master: Any, member: CastMember, images: ImageCache) -> None:
        super().__init__(master, fg_color="transparent", corner_radius=0)
        self.member = member
        self.photo = _CastPhoto(self, member, images, size=CAST_LIST_PHOTO, font_size=10)
        self.photo.pack(side="left", anchor="n")
        words = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        words.pack(side="left", fill="x", expand=True, padx=(CAST_LIST_GAP, 0))
        self._lines: list[tuple[ctk.CTkLabel, str, ctk.CTkFont]] = []
        for text, size, color in (
            (member.name, CAST_NAME_SIZE + 1, theme.TEXT),
            (member.character, CAST_PART_SIZE + 1, theme.TEXT_MUTED),
        ):
            if not text:
                continue
            font = theme.font(size)
            label = ctk.CTkLabel(
                words, text=text, font=font, text_color=color, anchor="w", height=1
            )
            label.pack(fill="x")
            self._lines.append((label, text, font))

    def set_room(self, room: float) -> None:
        """Each line elided to `room` (the column's width) less the photo."""
        width = room - CAST_LIST_PHOTO - CAST_LIST_GAP - SLACK
        for label, text, font in self._lines:
            label.configure(text=elide_to_width(text, width, font.measure))


class CastList(ctk.CTkFrame):
    """The "side" layout's cast: the first SIDE_CAST_SHOWN actors one under another, then
    "+N more", which unfolds the rest in the same style."""

    def __init__(self, master: Any, *, images: ImageCache) -> None:
        super().__init__(master, fg_color="transparent", corner_radius=0)
        self._images = images
        self._members: tuple[CastMember, ...] = ()
        self._room = 280.0
        self.entries: list[_CastLine] = []
        self._more = ctk.CTkLabel(
            self,
            text="",
            font=theme.font(CAST_NAME_SIZE + 1),
            text_color=theme.TEXT_MUTED,
            cursor="hand2",
            anchor="w",
            height=1,
        )
        self._more.bind("<Button-1>", lambda _event: self.unfold())

    @property
    def members(self) -> tuple[CastMember, ...]:
        return self._members

    @property
    def folded(self) -> bool:
        """Some actors wait behind "+N more"."""
        return len(self.entries) < len(self._members)

    def set_members(self, members: Sequence[CastMember]) -> None:
        members = tuple(members)
        if members == self._members:
            return
        self._members = members
        for entry in self.entries:
            entry.destroy()
        self.entries = []
        self._add(members[:SIDE_CAST_SHOWN])
        self._paint_more()

    def unfold(self) -> None:
        """Show every actor."""
        self._add(self._members[len(self.entries) :])
        self._paint_more()

    def set_room(self, room: float) -> None:
        if abs(room - self._room) >= 1:
            self._room = room
            for entry in self.entries:
                entry.set_room(room)

    def _add(self, members: Sequence[CastMember]) -> None:
        for member in members:
            entry = _CastLine(self, member, self._images)
            entry.set_room(self._room)
            entry.pack(fill="x", pady=(0, CAST_LIST_GAP))
            self.entries.append(entry)

    def _paint_more(self) -> None:
        hidden = len(self._members) - len(self.entries)
        if hidden > 0:
            self._more.configure(text=more_text(hidden))
            self._more.pack(fill="x")
            self._more.lift()
        else:
            self._more.pack_forget()


# -- the window --------------------------------------------------------------------------------


class ScrollColumn(ctk.CTkScrollableFrame):
    """A column that scrolls on its own while what it holds (in grid row 0, column 0) is taller
    than its room. The scrollbar keeps its place and only shows then: nothing moves when it
    comes or goes (no width that flips back and forth), and the change waits for Tk to be idle
    (showing or hiding a bar from inside <Configure> can leave it on screen)."""

    def __init__(self, master: Any) -> None:
        super().__init__(
            master,
            fg_color="transparent",
            corner_radius=0,
            border_width=0,
            scrollbar_button_color=theme.BG,
            scrollbar_button_hover_color=theme.BG,
        )
        self.grid_columnconfigure(0, weight=1)
        self._overflow: bool | None = None
        self._job: str | None = None
        self.bind("<Configure>", lambda _event: self._later(), add="+")
        self._parent_canvas.bind("<Configure>", lambda _event: self._later(), add="+")

    @property
    def scrolls(self) -> bool:
        """What it holds is taller than its room (the scrollbar shows)."""
        return bool(self._overflow)

    @property
    def shown(self) -> bool:
        """It is packed / gridded (CTk places the column itself in a frame of its own)."""
        return bool(self._parent_frame.winfo_manager())

    def _later(self) -> None:
        if self._job is None:
            self._job = self.after_idle(self._check)

    def _check(self) -> None:
        self._job = None
        if not exists(self):
            return
        room = self._parent_canvas.winfo_height()
        if room <= 1:
            return
        overflow = self.winfo_reqheight() > room
        if overflow == self._overflow:
            return
        self._overflow = overflow
        if overflow:
            self.configure(
                scrollbar_button_color=theme.SCROLLBAR, scrollbar_button_hover_color=theme.EDGE
            )
        else:
            self.configure(scrollbar_button_color=theme.BG, scrollbar_button_hover_color=theme.BG)
            self._parent_canvas.yview_moveto(0)

    def destroy(self) -> None:
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except tk.TclError:
                pass
            self._job = None
        super().destroy()


NBSP = "\u00a0"  # a no-break space
NBSP_DOT = NBSP + "· "  # " · " that breaks only after the dot


def meta_wrapping(text: str) -> str:
    """A " · "-joined meta line that wraps only between its parts: "2026 · 2h 25m · ★ 8.5 ·"
    then "Out digitally since 6 Oct", never "… since 6" then "Oct" (no-break spaces inside a
    part and before each dot)."""
    return NBSP_DOT.join(part.replace(" ", NBSP) for part in text.split(" · "))


@dataclass(frozen=True, slots=True)
class _TextLook:
    """The text column's sizes in a layout (CTk units)."""

    title: int
    detail: int  # meta line, synopsis, note
    small: int  # More, the hint
    # Above: the meta line, the genres, the synopsis, More, the note, the actions, the hint.
    gaps: tuple[int, int, int, int, int, int, int]
    summary_lines: int


_TABS_LOOK = _TextLook(
    title=26, detail=15, small=13, gaps=(2, 8, 8, 0, 6, 10, 10), summary_lines=SUMMARY_LINES
)
_SIDE_LOOK = _TextLook(
    title=22, detail=14, small=12, gaps=(4, 10, 10, 2, 8, 14, 10), summary_lines=SIDE_SUMMARY_LINES
)
# The text column's grid rows.
_TITLE_ROW, _META_ROW, _GENRES_ROW, _SUMMARY_ROW, _MORE_ROW, _NOTE_ROW = range(6)
_SPACER_ROW, _BUTTONS_ROW, _HINT_ROW, _CAST_ROW = range(6, 10)


def _text(
    master: Any, text: str, size: int, color: theme.Color = theme.TEXT, *, bold: bool = False
) -> ctk.CTkLabel:
    """Left-aligned wrapping text, as tall as its lines."""
    return ctk.CTkLabel(
        master,
        text=text,
        font=theme.font(size, bold=bold),
        text_color=color,
        anchor="w",
        justify="left",
        height=1,
    )


class TitleInfoWindow(ctk.CTkToplevel):
    """A film's or show's info window (not modal; see the module docstring). Subclasses fill
    the text column (`_set_name`, `_set_meta`, `set_genres`, `_set_summary`, `_set_note`, the
    actions in `buttons` (`buttons.set_items`)), the backdrop and the trailer (`masthead`), the
    cast (`show_cast`), a series its episodes (`content`), and show releases with
    `open_releases` (needs `releases`, the panel's services)."""

    def __init__(
        self,
        master: ctk.CTk,
        *,
        title: str,
        images: ImageCache,
        poster: Callable[[int], str | None],
        layout: InfoLayout = InfoLayout.SIDE,
        releases: PanelServices | None = None,
        episodes: bool = False,
        on_fix_match: Callable[[TvTarget], None] | None = None,
    ) -> None:
        super().__init__(master, fg_color=theme.BG)
        self.layout = layout
        self._images = images
        self._services = releases
        self._has_episodes = episodes
        self._on_fix_match = on_fix_match
        self._look = _TABS_LOOK if layout is InfoLayout.TABS else _SIDE_LOOK
        self._summary_text = ""
        self._summary_expanded = False
        self._summary_lines = self._look.summary_lines
        self._summary_shown = 0  # the synopsis' lines showing
        self._text_width = float(SIDE_LEFT_WIDTH - 2 * SIDE_PAD)  # a first guess
        self.panel: ReleasePanel | None = None
        self._count: int | None = None  # the panel's last count
        self._releases_tab: str | None = None  # the Releases tab's name now ("tabs")
        self._episodes_tab: str | None = None
        self._cast_tab: str | None = None
        self._choose_first: ctk.CTkLabel | None = None
        self._block_job: str | None = None
        self._scroll_job: str | None = None
        self._reshow_job: str | None = None  # the Releases tab shown again after a rename
        self._scroll_tries = 0
        self.body: ctk.CTkScrollableFrame | None = None
        self.tabs: Tabview | None = None
        self.cast: CastList | CastRow | None = None
        self._episodes_view: ScrollColumn | None = None  # "side", a series
        self._releases_view: ctk.CTkFrame | None = None
        self._target_label: ctk.CTkLabel | None = None
        self.title(title)
        set_window_icon(self)
        self.transient(master)
        # CTkToplevel only applies its dark Windows title bar reliably from resizable().
        self.resizable(True, True)
        width = self._place_over(master)
        min_width = min(SIDE_MIN_WIDTH, width) if layout is InfoLayout.SIDE else MIN_WIDTH
        self.minsize(min_width, MIN_HEIGHT)

        if layout is InfoLayout.TABS:
            self._build_tabs(title, poster)
        else:
            self._build_side(title, poster)

        self.bind("<Escape>", lambda _event: self.destroy())
        # No title bar: dragged by the backdrop band, the round ✕ in its top-right corner.
        # CTk's own hide-and-show stays (as it was with a title bar): "tabs" settles while hidden.
        self.borderless: Borderless | None = make_borderless(
            self, drag_widgets=(self.masthead,), hide_to_recolour=True
        )
        if self.borderless is not None:
            self._sit_close_button(self.borderless)
        # CTkToplevel can open behind its parent on Windows; raise it once it is mapped.
        self._activate_job: str | None = self.after(150, self._activate)
        self._show_job: str | None = None
        theme.on_mode_change(self, self._after_mode_change)

    def _sit_close_button(self, borderless: Borderless) -> None:
        """The ✕ shows the backdrop's piece around it; in "tabs" (everything scrolls) also as
        the window scrolls, and the scrollbar starts under it."""
        button = borderless.close_button
        masthead = self.masthead
        button.set_backdrop(lambda: masthead.under(button))
        masthead.on_backdrop_change = button.repaint_backdrop
        body = self.body
        if body is None:
            return
        scrollbar = body._scrollbar
        scrollbar.grid(pady=(CLOSE_ROOM + CLOSE_GAP, 0))

        def scrolled(first: str, last: str) -> None:
            scrollbar.set(first, last)
            if exists(button):
                button.repaint_backdrop()

        body._parent_canvas.configure(yscrollcommand=scrolled)

    # -- layout ------------------------------------------------------------------------------

    @property
    def _widest(self) -> int:
        return SIDE_WINDOW_WIDTH if self.layout is InfoLayout.SIDE else WINDOW_WIDTH

    def _place_over(self, master: ctk.CTk) -> int:
        """Open at `window_size` on the work area of the screen `master` is on, centred there
        (CTk scales the size but not the position: see MainWindow._place). Returns the width
        (CTk units)."""
        scaling = ctk.ScalingTracker.get_window_scaling(self)
        centre_x = master.winfo_rootx() + master.winfo_width() // 2
        centre_y = master.winfo_rooty() + master.winfo_height() // 2
        area = work_area(centre_x, centre_y)
        if area is None:  # off Windows: the primary screen, physical pixels
            screen_w, screen_h = self.wm_maxsize()
            area = (0, 0, screen_w, screen_h)
        left, top, right, bottom = area
        width, height = window_size(
            (right - left) / scaling, (bottom - top) / scaling, self._widest
        )
        width_px, height_px = round(width * scaling), round(height * scaling)
        x = left + max(0, (right - left - width_px) // 2)
        y = top + max(0, (bottom - top - height_px) // 2)
        self.geometry(f"{width}x{height}+{x}+{y}")
        return width

    def _build_side(self, title: str, poster: Callable[[int], str | None]) -> None:
        """The band across the top; the info column on the left (the poster's lower part, then
        the scrolling text); the releases or the episodes on the right."""
        self.grid_columnconfigure(2, weight=1)
        self.grid_rowconfigure(1, weight=1)
        left = ctk.CTkFrame(self, width=SIDE_LEFT_WIDTH, fg_color="transparent", corner_radius=0)
        left.pack_propagate(False)
        self.masthead = Masthead(
            self, images=self._images, poster=poster, layout=InfoLayout.SIDE, foot_master=left
        )
        self.masthead.grid(row=0, column=0, columnspan=3, sticky="ew")
        self.masthead.placed()
        left.grid(row=1, column=0, sticky="ns")
        # A quiet line between the column and the right side (plain Tk: repainted on a switch).
        scaling: float = ctk.ScalingTracker.get_window_scaling(self)
        divider = tk.Frame(
            self, width=max(1, round(scaling)), bg=theme.pick(theme.PANEL_EDGE), borderwidth=0
        )
        divider.grid(row=1, column=1, sticky="ns")
        theme.on_mode_change(divider, lambda: divider.configure(bg=theme.pick(theme.PANEL_EDGE)))
        self.right = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        self.right.grid(row=1, column=2, sticky="nsew")
        assert self.masthead.foot is not None
        self.masthead.foot.pack(fill="x")
        scroll = ScrollColumn(left)
        scroll.pack(fill="both", expand=True)
        info = _TextColumn(scroll, fg_color=theme.BG, bg_color=theme.BG, corner_radius=0)
        info.grid(row=0, column=0, sticky="new", padx=(SIDE_PAD, 6), pady=(0, SIDE_PAD))
        self.masthead.watch_scaling(info)
        self._build_text(info, title)
        self.cast = CastList(info, images=self._images)

        if self._has_episodes:
            view = ScrollColumn(self.right)
            view.pack(fill="both", expand=True)
            self._episodes_view = view
            self.content = ctk.CTkFrame(view, fg_color="transparent", corner_radius=0)
            self.content.grid(row=0, column=0, sticky="new", padx=(RIGHT_PAD, 4), pady=RIGHT_PAD)
            releases = ctk.CTkFrame(self.right, fg_color="transparent", corner_radius=0)
            self._releases_view = releases
            bar = ctk.CTkFrame(releases, fg_color="transparent", corner_radius=0)
            bar.pack(fill="x", padx=RIGHT_PAD, pady=(RIGHT_PAD - 4, 8))
            self.back_button = ctk.CTkButton(
                bar,
                text=t(BACK_TEXT),
                width=110,
                height=30,
                corner_radius=6,
                fg_color="transparent",
                hover_color=theme.HOVER,
                text_color=theme.BUTTON_TEXT,
                border_width=1,
                border_color=theme.EDGE,
                font=theme.font(13, bold=True),
                command=self.show_episodes,
            )
            self.back_button.pack(side="left")
            self._target_label = ctk.CTkLabel(
                bar, text="", font=theme.font(15, bold=True), text_color=theme.TEXT, anchor="w"
            )
            self._target_label.pack(side="left", fill="x", expand=True, padx=(12, 0))
            self._panel_holder = ctk.CTkFrame(releases, fg_color="transparent", corner_radius=0)
            self._panel_holder.pack(fill="both", expand=True, padx=RIGHT_PAD, pady=(0, RIGHT_PAD))
        else:
            self.content = ctk.CTkFrame(self.right, fg_color="transparent", corner_radius=0)
            self._panel_holder = ctk.CTkFrame(self.right, fg_color="transparent", corner_radius=0)
            self._panel_holder.pack(
                fill="both", expand=True, padx=RIGHT_PAD, pady=(RIGHT_PAD - 4, RIGHT_PAD)
            )

    def _build_tabs(self, title: str, poster: Callable[[int], str | None]) -> None:
        """The masthead with the text column beside the poster, then the tabs; all of it
        scrolls."""
        self.body = ctk.CTkScrollableFrame(
            self,
            fg_color="transparent",
            corner_radius=0,
            border_width=0,
            scrollbar_button_color=theme.SCROLLBAR,
            scrollbar_button_hover_color=theme.EDGE,
        )
        self.body.pack(fill="both", expand=True)
        self.masthead = Masthead(
            self.body, images=self._images, poster=poster, layout=InfoLayout.TABS
        )
        self.masthead.pack(fill="x")
        info = self.masthead.text
        assert info is not None
        self._build_text(info, title)
        info.bind("<Configure>", lambda _event: self._schedule_block(), add="+")
        self.tabs = Tabview(
            self.body,
            height=10,
            corner_radius=10,
            border_width=0,
            fg_color=theme.BG,
            anchor="w",
            segmented_button_fg_color=theme.SURFACE,
            segmented_button_selected_color=theme.SELECTED,
            segmented_button_selected_hover_color=theme.SELECTED_HOVER,
            segmented_button_unselected_color=theme.SURFACE,
            segmented_button_unselected_hover_color=theme.HOVER,
            segmented_button_font=theme.font(14, bold=True),
            text_color=theme.BUTTON_TEXT,
        )
        self.tabs.pack(fill="x", padx=PAD_X - 8, pady=(0, TAB_BOTTOM))
        if self._has_episodes:
            self._episodes_tab = t(EPISODES_TAB)
            page = self.tabs.add(self._episodes_tab)
            self.content = ctk.CTkFrame(page, fg_color="transparent", corner_radius=0)
            self.content.pack(fill="x", padx=8, pady=(4, 8))
        else:
            self.content = ctk.CTkFrame(self.body, fg_color="transparent", corner_radius=0)
        self._releases_tab = releases_tab_text(None, None)
        page = self.tabs.add(self._releases_tab)
        self._panel_holder = ctk.CTkFrame(
            page, fg_color="transparent", corner_radius=0, height=RELEASES_MIN_HEIGHT
        )
        self._panel_holder.pack_propagate(False)
        self._panel_holder.pack(fill="x", padx=4, pady=(4, 0))
        if self._has_episodes:
            self._choose_first = _text(
                self._panel_holder, t(CHOOSE_FIRST_TEXT), 14, theme.TEXT_MUTED
            )
            self._choose_first.pack(anchor="w", padx=8, pady=12)
            self.tabs.set(self._episodes_tab or self._releases_tab)
        else:
            self.tabs.set(self._releases_tab)
        self.body._parent_canvas.bind("<Configure>", lambda _event: self._fit_tabs(), add="+")

    def _build_text(self, info: ctk.CTkFrame, title: str) -> None:
        look = self._look
        meta_gap, genres_gap, summary_gap, more_gap, note_gap, buttons_gap, hint_gap = look.gaps
        self._info = info
        info.grid_columnconfigure(0, weight=1)
        self._title_label = _text(info, title, look.title, bold=True)
        self._title_label.grid(row=_TITLE_ROW, column=0, sticky="w")
        self._meta = _text(info, "", look.detail, theme.TEXT_MUTED)
        self._meta.grid(row=_META_ROW, column=0, sticky="w", pady=(meta_gap, 0))
        self.genres = GenrePills(info)
        self.genres.grid(row=_GENRES_ROW, column=0, sticky="w", pady=(genres_gap, 0))
        self._summary_font = theme.font(look.detail)
        self._summary = _text(info, "", look.detail)
        self._summary.grid(row=_SUMMARY_ROW, column=0, sticky="w", pady=(summary_gap, 0))
        self._more = ctk.CTkLabel(
            info,
            text=t(MORE_TEXT),
            font=theme.font(look.small, underline=True),
            text_color=theme.LINK,
            cursor="hand2",
            height=1,
        )
        self._more.grid(row=_MORE_ROW, column=0, sticky="w", pady=(more_gap, 0))
        self._more.bind("<Button-1>", lambda _event: self._expand_summary())
        self._note = _text(info, "", look.detail, theme.TEXT_MUTED)
        self._note.grid(row=_NOTE_ROW, column=0, sticky="w", pady=(note_gap, 0))
        self.buttons = FlowRow(info)
        self.buttons.grid(row=_BUTTONS_ROW, column=0, sticky="w", pady=(buttons_gap, 0))
        self._hint = _text(info, t(NO_KEY_HINT), look.small, theme.TEXT_MUTED)
        self._hint.grid(row=_HINT_ROW, column=0, sticky="w", pady=(hint_gap, 0))
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
        self._summary_lines = self._look.summary_lines  # fitted to the poster again ("tabs")
        self._fit_text()

    def _fit_text(self) -> None:
        """Wrap the texts at the text column's width; the synopsis at most the layout's lines
        (in "tabs" the lines that fit beside the poster) until "More" is clicked."""
        width = max(1.0, self._text_width - SLACK)
        wrap = max(1, round(width))
        for label in (self._title_label, self._meta, self._note, self._hint):
            label.configure(wraplength=wrap)
        self.genres.set_room(width)
        self.buttons.set_room(width)
        if isinstance(self.cast, CastList):
            self.cast.set_room(width)
        summary = self._summary_text
        if not summary:
            self._summary.grid_remove()
            self._more.grid_remove()
            self._summary_shown = 0
            return
        if self._summary_expanded:
            text, cut = summary, False
        else:
            text, cut = collapsed_summary(
                summary, width, self._summary_font.measure, self._summary_lines
            )
        self._summary_shown = text.count("\n") + 1
        self._summary.configure(text=text, wraplength=wrap)
        self._summary.grid()
        if cut:
            self._more.grid()
        else:
            self._more.grid_remove()

    def _expand_summary(self) -> None:
        self._summary_expanded = True
        self._fit_text()

    def _schedule_block(self) -> None:
        if self._block_job is None:
            self._block_job = self.after_idle(self._fit_block)

    def _fit_block(self) -> None:
        """"tabs": the text column as tall as the poster (the actions at its bottom), the
        synopsis cut to the lines that fit (at least MIN_SUMMARY_LINES)."""
        self._block_job = None
        if self.layout is not InfoLayout.TABS or not exists(self):
            return
        info = self._info
        poster = self.masthead.poster_size[1]
        spacer = 0
        natural = 0
        for _ in range(SUMMARY_LINES + 1):
            info.update_idletasks()
            spacer = int(info.grid_rowconfigure(_SPACER_ROW)["minsize"])
            natural = info.winfo_reqheight() - spacer
            if (
                natural <= poster
                or self._summary_expanded
                or self._summary_shown <= MIN_SUMMARY_LINES
            ):
                break
            self._summary_lines = max(MIN_SUMMARY_LINES, self._summary_shown - 1)
            self._fit_text()
        room = max(0, poster - natural)
        if room != spacer:
            info.grid_rowconfigure(_SPACER_ROW, minsize=room)

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
        self._show_line(self._meta, meta_wrapping(text))

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
            self._summary_lines = self._look.summary_lines
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
        return backdrop_size(max(self.masthead.winfo_width(), round(self._widest * scaling)))

    def show_cast(self, members: Sequence[CastMember]) -> None:
        """"side": the compact list in the info column; "tabs": the Cast tab (there only while
        there is a cast)."""
        if self.layout is InfoLayout.SIDE:
            assert self.cast is not None
            self.cast.set_members(members)
            if members and not self.cast.winfo_manager():
                self.cast.grid(row=_CAST_ROW, column=0, sticky="ew", pady=(18, 0))
            elif not members:
                self.cast.grid_remove()
            return
        tabs = self.tabs
        assert tabs is not None
        if members and self._cast_tab is None:
            self._cast_tab = t(CAST_TAB)
            page = tabs.add(self._cast_tab)
            self.cast = CastRow(page, images=self._images)
            self.cast.pack(fill="x", padx=4, pady=(8, 8))
        elif not members and self._cast_tab is not None:
            if tabs.get() == self._cast_tab:
                tabs.set(self._episodes_tab or self._releases_tab or self._cast_tab)
            tabs.delete(self._cast_tab)
            self._cast_tab = None
            self.cast = None
        if self.cast is not None:
            self.cast.set_members(members)

    # -- the releases ------------------------------------------------------------------------

    @property
    def releases_tab(self) -> str | None:
        """"tabs": the Releases tab's name now ("Releases (18)", "Releases · S03E05")."""
        return self._releases_tab

    @property
    def showing_releases(self) -> bool:
        """The releases are on screen (or their tab is chosen)."""
        if self.tabs is not None:
            return bool(self.tabs.get() == self._releases_tab)
        if self._releases_view is not None:
            return bool(self._releases_view.winfo_manager())
        return self.panel is not None

    def open_releases(self, target: IndexerTarget, *, reveal: bool = True) -> ReleasePanel | None:
        """Show `target`'s releases: the panel is made (or set to `target`) and started.
        "side": a series' right side swaps to it; "tabs": its tab is chosen and, with
        `reveal`, the window scrolls down to it. None without the panel's services."""
        services = self._services
        if services is None:
            return None
        panel = self.panel
        if panel is None or not exists(panel):
            if self._choose_first is not None:
                self._choose_first.destroy()
                self._choose_first = None
            panel = ReleasePanel(
                self._panel_holder,
                target=target,
                search=services.search,
                downloads=services.downloads,
                library=services.library,
                discovery=services.discovery,
                settings=services.settings,
                status=services.status,
                on_target_change=self._paint_releases_title,
                on_count=self._counted,
                on_fix_match=self._on_fix_match if isinstance(target, TvTarget) else None,
            )
            panel.pack(fill="both", expand=True)
            self.panel = panel
            self._count = None
        else:
            panel.set_target(target)
        panel.start()
        self._paint_releases_title(panel.target)
        self._show_releases(reveal=reveal)
        return panel

    def show_episodes(self) -> None:
        """Back to the episodes: "side" puts the episodes back on the right (the panel goes,
        and its searches and grabs with it); "tabs" chooses the Episodes tab."""
        if self.tabs is not None:
            if self._episodes_tab is not None:
                self.tabs.set(self._episodes_tab)
            return
        if self._episodes_view is None or self._releases_view is None:
            return
        if self.panel is not None and exists(self.panel):
            self.panel.destroy()
        self.panel = None
        self._releases_view.pack_forget()
        if not self._episodes_view.shown:
            self._episodes_view.pack(fill="both", expand=True)

    def _show_releases(self, *, reveal: bool) -> None:
        if self.tabs is not None and self._releases_tab is not None:
            self.tabs.set(self._releases_tab)
            self._fit_tabs()
            if reveal:
                self._scroll_tries = 0
                self._scroll_later()
            return
        if self._episodes_view is not None and self._releases_view is not None:
            self._episodes_view.pack_forget()
            if not self._releases_view.winfo_manager():
                self._releases_view.pack(fill="both", expand=True)

    def _counted(self, count: int) -> None:
        self._count = count
        if self.panel is not None:
            self._paint_releases_title(self.panel.target)

    def _paint_releases_title(self, target: IndexerTarget) -> None:
        """The "← Episodes" bar's label ("side"), the Releases tab's name ("tabs")."""
        if self._target_label is not None:
            self._target_label.configure(text=self.target_text(target))
        tabs, old = self.tabs, self._releases_tab
        if tabs is None or old is None:
            return
        name = releases_tab_text(target, self._count)
        if name == old:
            return
        current = tabs.get() == old
        tabs.rename(old, name)
        self._releases_tab = name
        if current:
            tabs.set(name)
            # CTk hides every page but the chosen one 100 ms after a `set`, by name: a `set`
            # under the old name just before (a count that came quickly) would hide this page.
            if self._reshow_job is not None:
                self.after_cancel(self._reshow_job)
            self._reshow_job = self.after(TAB_SETTLE_MS, lambda: self._reshow_tab(name))

    def _reshow_tab(self, name: str) -> None:
        self._reshow_job = None
        tabs = self.tabs
        if tabs is not None and exists(tabs) and tabs.get() == name:
            tabs.set(name)

    def target_text(self, target: IndexerTarget) -> str:
        """The "← Episodes" bar's label for `target` (a series names the episode)."""
        if isinstance(target, TvTarget):
            return target_code(target)
        return target.movie.title

    def _fit_tabs(self) -> None:
        """"tabs": the Releases page as tall as the window less the tab bar, so scrolled down
        the table fills the window."""
        body = self.body
        if body is None:
            return
        view = body._parent_canvas.winfo_height()
        if view <= 1:
            return
        scaling: float = ctk.ScalingTracker.get_widget_scaling(self._panel_holder)
        height = max(RELEASES_MIN_HEIGHT, round(view / scaling) - TAB_CHROME - TAB_BOTTOM)
        if int(self._panel_holder.cget("height")) != height:
            self._panel_holder.configure(height=height)

    def _scroll_later(self) -> None:
        if self._scroll_job is not None:
            self.after_cancel(self._scroll_job)
        self._scroll_job = self.after(160, self._scroll_to_tabs)

    def _scroll_to_tabs(self) -> None:
        """"tabs": scroll so the tab bar is at the top (as far as the page goes)."""
        self._scroll_job = None
        body, tabs = self.body, self.tabs
        if body is None or tabs is None or not exists(self):
            return
        total = body.winfo_height()
        if total <= 1 or tabs.winfo_y() <= 0:
            self._scroll_tries += 1
            if self._scroll_tries < 10:  # not laid out yet
                self._scroll_later()
            return
        body._parent_canvas.yview_moveto(tabs.winfo_y() / total)

    # -- window ------------------------------------------------------------------------------

    def _activate(self) -> None:
        self._activate_job = None
        if self.winfo_exists():
            self.lift()
            self.focus_force()
            if self.panel is not None and exists(self.panel) and self.showing_releases:
                self.panel.focus_table()  # Up / Down / Enter work at once

    def _after_mode_change(self) -> None:
        """A switch between light and dark: CTk hides the main window to recolour its title
        bar, so this window (its transient) reads itself as hidden and stays so. Shown again
        once that is over (not while the main window is in the tray; never grabbed: not
        modal)."""
        if self._show_job is None:
            self._show_job = self.after(dialog_kit.REACTIVATE_MS, self._show_again)

    def _show_again(self) -> None:
        self._show_job = None
        if not exists(self) or self.state() != "withdrawn":
            return
        if str(self.master.state()) == "withdrawn":
            return  # in the tray: it comes back with the main window
        self.deiconify()

    def raise_window(self) -> None:
        """Bring the window to the front (also from minimised)."""
        if self.state() == "iconic":
            self.deiconify()
        self.lift()
        self.focus_force()

    def destroy(self) -> None:
        jobs = (
            self._activate_job,
            self._block_job,
            self._scroll_job,
            self._show_job,
            self._reshow_job,
        )
        for job in jobs:
            if job is not None:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
        self._activate_job = self._block_job = self._scroll_job = self._show_job = None
        self._reshow_job = None
        super().destroy()  # the panel too, which cancels its own tasks


def exists(widget: Any) -> bool:
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:  # the whole interpreter is gone
        return False

