"""The spotlight band's pure parts (the widget: app/ui/spotlight_band.py): its texts (the tag,
the title, the muted line), when it turns to the next title (`Rotation`) and where everything
goes (`band_layout`, `hit_target`; the Series / Movies switch on the tabs row: `switch_layout`,
`switch_hit`), all in CTk's unscaled units."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import NamedTuple

from app.i18n import N_, day_short, month_short, number, t, tn
from app.models import Movie, Spotlight, SpotlightItem, SpotlightReason

# -- texts -------------------------------------------------------------------------------------

TRENDING_TAG = N_("Trending")
NEW_TO_STREAM_TAG = N_("New to stream")
TONIGHT = N_("Tonight")
TODAY = N_("Today")
TOMORROW = N_("Tomorrow")
EPISODE_TITLE = N_("{show} — {code} “{name}”")
EVENING_HOUR = 17  # from this hour on, today's episode airs "tonight"


def tag_text(item: SpotlightItem, now: datetime) -> str:
    """The small upper-case line above the title: "TONIGHT · 21:00", "TODAY · 14:00",
    "TOMORROW · 20:00", "FRI 7 NOV" (later days, without the time), "TRENDING", "NEW TO
    STREAM". `now` is aware: its time zone decides the day and the time."""
    if item.reason is SpotlightReason.TRENDING:
        return t(TRENDING_TAG).upper()
    if item.reason is SpotlightReason.NEW_TO_STREAM:
        return t(NEW_TO_STREAM_TAG).upper()
    local = item.airs_at.astimezone(now.tzinfo) if item.airs_at is not None else None
    day = local.date() if local is not None else item.air_day
    if day is None:
        return ""
    today = now.date()
    if day == today:
        evening = local is not None and local.hour >= EVENING_HOUR
        word = t(TONIGHT) if evening else t(TODAY)
    elif day == today + timedelta(days=1):
        word = t(TOMORROW)
    else:
        return f"{day_short(day)} {day.day} {month_short(day)}".upper()
    return (f"{word} · {local:%H:%M}" if local is not None else word).upper()


def title_text(item: SpotlightItem) -> str:
    """ "Show — S03E05 “Episode title”" ("Show — S03E05" without a name), else the title."""
    episode = item.episode
    if episode is None:
        return item.title
    if episode.name:
        return t(EPISODE_TITLE, show=item.title, code=episode.code, name=episode.name)
    return f"{item.title} — {episode.code}"


def sub_line(spotlight: Spotlight | None, item: SpotlightItem | None = None) -> str:
    """The muted line under the title: for a film (`item`) its year and rating ("2026 · ★ 7.0",
    `film_line`); else what airs, "3 episodes today" / "4 episodes in the next 7 days" ("" for
    trending shows and without any). How many downloads are on their way is the ⬇ button's
    badge, not this line's."""
    if item is not None and item.movie is not None:
        return film_line(item.movie)
    if spotlight is None or not spotlight.airing:
        return ""
    count = spotlight.airing
    if spotlight.reason is SpotlightReason.TODAY:
        return tn("{n} episode today", "{n} episodes today", count)
    if spotlight.reason is SpotlightReason.COMING:
        return tn("{n} episode in the next 7 days", "{n} episodes in the next 7 days", count)
    return ""


def film_line(movie: Movie) -> str:
    """ "2026 · ★ 7.0", leaving out whichever is unknown (no votes: no rating); "" for
    neither."""
    parts: list[str] = []
    if movie.year:
        parts.append(str(movie.year))
    if movie.vote_count:
        parts.append(f"★ {number(movie.vote_average, '.1f')}")
    return " · ".join(parts)


# -- turning to the next title -----------------------------------------------------------------

ROTATE_S = 8.0  # a title shows this long before the next one
FADE_MS = 400  # the crossfade between two titles
FADE_STEPS = 8  # its frames


class Rotation:
    """Which title shows and when the next is due: every `interval` seconds while `motion` is
    on, the pointer is off the band (`hovering`) and there is more than one title. A jump (a
    dot) or the pointer leaving starts a full interval again. Seconds come from the caller's
    monotonic clock."""

    def __init__(self, *, interval: float = ROTATE_S, motion: bool = True) -> None:
        self.interval = interval
        self.motion = motion
        self.count = 0
        self.index = 0
        self.hovering = False
        self._since = 0.0

    def reset(self, count: int, index: int, now: float) -> None:
        """Another list of `count` titles, showing `index` (0 when out of range) from `now`."""
        self.count = max(0, count)
        self.index = index if 0 <= index < self.count else 0
        self._since = now

    def due(self, now: float) -> bool:
        return (
            self.motion
            and not self.hovering
            and self.count > 1
            and now - self._since >= self.interval
        )

    def advance(self, now: float) -> int:
        self.index = (self.index + 1) % max(1, self.count)
        self._since = now
        return self.index

    def jump(self, index: int, now: float) -> bool:
        """Show `index` (a dot); False when it already shows or there is no such title."""
        if not 0 <= index < self.count or index == self.index:
            return False
        self.index = index
        self._since = now
        return True

    def hover(self, on: bool, now: float) -> None:
        if self.hovering and not on:
            self._since = now  # a full interval after the pointer leaves
        self.hovering = on

    def set_motion(self, on: bool, now: float) -> None:
        if on and not self.motion:
            self._since = now
        self.motion = on


# -- layout --------------------------------------------------------------------------------------

PAD_X = 20  # the band's left and right margin
ROW_TOP = 28  # the controls' row
ROW_HEIGHT = 40
LOGO_SIZE = 40
WORD_GAP = 10  # the logo → "AirGrabber"
WORD_SIZE = 18
CLUSTER_GAP = 24  # at least between "AirGrabber" and the search field
SEARCH_WIDTH = 300
SEARCH_MIN_WIDTH = 200
SEARCH_GAP = 14  # the search field → the 🔨 MediaForge button
MAGNIFIER_LEFT = 8  # the 🔍 inside the search field
MAGNIFIER_WIDTH = 30
ENTRY_HEIGHT = 28
ENTRY_PAD_RIGHT = 16
BUTTON_SIZE = 40
BUTTON_GAP = 10
BUTTON_RADIUS = 10
BADGE_HEIGHT = 18
BADGE_OVERHANG = 6  # a button's badge (⬇, 🔨) reaches this far past its corner
POSTER_TOP = ROW_TOP + ROW_HEIGHT + 22  # the row → the spotlight
POSTER_WIDTH = 44
POSTER_HEIGHT = 64
POSTER_RADIUS = 6
BAND_BOTTOM = 18  # the poster → the band's lower edge (the page tabs sit right under it)
BAND_HEIGHT = POSTER_TOP + POSTER_HEIGHT + BAND_BOTTOM
POSTER_GAP = 14  # the poster → the texts
TITLE_GAP = 24  # the texts → the dots
DOT = 7
DOT_WIDE = 20
DOT_GAP = 6
DOT_HIT = 6  # a dot's click area reaches this far around it
SPOT_TOP = ROW_TOP + ROW_HEIGHT + 8  # the spotlight's click area starts here


@dataclass(frozen=True, slots=True)
class Rect:
    x: float
    y: float
    w: float
    h: float

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    def contains(self, x: float, y: float) -> bool:
        return self.x <= x < self.right and self.y <= y < self.bottom

    def grown(self, by: float) -> Rect:
        return Rect(self.x - by, self.y - by, self.w + 2 * by, self.h + 2 * by)

    def overlaps(self, other: Rect) -> bool:
        return (
            self.x < other.right
            and other.x < self.right
            and self.y < other.bottom
            and other.y < self.bottom
        )


@dataclass(frozen=True, slots=True)
class BandLayout:
    """Where the band's parts go at one width (unscaled units; see `band_layout`)."""

    width: float
    logo: Rect
    word_x: float  # "AirGrabber" starts here, centred on the row
    brand: Rect  # the logo and "AirGrabber" together
    search: Rect  # the search field
    magnifier: Rect  # its 🔍 (the search button)
    entry: Rect  # the text box inside it
    forge: Rect  # the 🔨 MediaForge button, just left of ⬇
    downloads: Rect
    settings: Rect
    badge: tuple[float, float]  # the ⬇ button's badge's top-right corner
    forge_badge: tuple[float, float]  # the 🔨 button's badge's top-right corner
    poster: Rect
    text_x: float  # the tag, title and muted line start here
    text_right: float  # … and end before this
    dots: tuple[Rect, ...]
    spot: Rect  # a click here opens the title (not on the controls or the dots)

    @property
    def row_middle(self) -> float:
        return ROW_TOP + ROW_HEIGHT / 2


def band_layout(
    width: float, *, word_width: float, dots: int = 0, current: int = 0
) -> BandLayout:
    """The band at `width` units: the logo and "AirGrabber" (`word_width` wide) on the left,
    the search field, 🔨 MediaForge, ⬇ and the settings button on the right (the Series /
    Movies switch is on the tabs row under the band: `switch_layout`); under them the poster
    and the texts, the `dots` (the `current` one wide) on the right. The search field narrows (to
    SEARCH_MIN_WIDTH) before it would come too close to "AirGrabber"."""
    right = width - PAD_X
    logo = Rect(PAD_X, ROW_TOP, LOGO_SIZE, LOGO_SIZE)
    word_x = logo.right + WORD_GAP
    left_end = word_x + word_width
    settings = Rect(right - BUTTON_SIZE, ROW_TOP, BUTTON_SIZE, BUTTON_SIZE)
    downloads = Rect(settings.x - BUTTON_GAP - BUTTON_SIZE, ROW_TOP, BUTTON_SIZE, BUTTON_SIZE)
    forge = Rect(downloads.x - BUTTON_GAP - BUTTON_SIZE, ROW_TOP, BUTTON_SIZE, BUTTON_SIZE)
    search_right = forge.x - SEARCH_GAP
    room = search_right - (left_end + CLUSTER_GAP)
    search_width = max(SEARCH_MIN_WIDTH, min(SEARCH_WIDTH, room))
    search = Rect(search_right - search_width, ROW_TOP, search_width, ROW_HEIGHT)
    magnifier = Rect(search.x + MAGNIFIER_LEFT, ROW_TOP, MAGNIFIER_WIDTH, ROW_HEIGHT)
    entry_x = magnifier.right
    entry = Rect(
        entry_x,
        ROW_TOP + (ROW_HEIGHT - ENTRY_HEIGHT) / 2,
        max(1.0, search.right - ENTRY_PAD_RIGHT - entry_x),
        ENTRY_HEIGHT,
    )
    poster = Rect(PAD_X, POSTER_TOP, POSTER_WIDTH, POSTER_HEIGHT)
    dot_rects: list[Rect] = []
    if dots > 1:
        total = DOT_WIDE + (dots - 1) * (DOT + DOT_GAP)
        x = right - total
        y = poster.y + (poster.h - DOT) / 2
        for index in range(dots):
            wide = DOT_WIDE if index == current else DOT
            dot_rects.append(Rect(x, y, wide, DOT))
            x += wide + DOT_GAP
    text_x = poster.right + POSTER_GAP
    dots_left = dot_rects[0].x if dot_rects else right + TITLE_GAP
    text_right = dots_left - TITLE_GAP
    spot_right = dot_rects[0].x - DOT_HIT if dot_rects else width
    spot = Rect(0, SPOT_TOP, spot_right, BAND_HEIGHT - SPOT_TOP)
    return BandLayout(
        width=width,
        logo=logo,
        word_x=word_x,
        brand=Rect(logo.x, logo.y, left_end - logo.x, logo.h),
        search=search,
        magnifier=magnifier,
        entry=entry,
        forge=forge,
        downloads=downloads,
        settings=settings,
        badge=(downloads.right + BADGE_OVERHANG, downloads.y - BADGE_OVERHANG),
        forge_badge=(forge.right + BADGE_OVERHANG, forge.y - BADGE_OVERHANG),
        poster=poster,
        text_x=text_x,
        text_right=text_right,
        dots=tuple(dot_rects),
        spot=spot,
    )


class HitKind(StrEnum):
    SEARCH = "search"  # the 🔍
    FIELD = "field"  # the search field around its text box: focuses it
    FORGE = "forge"  # the 🔨 MediaForge button
    DOWNLOADS = "downloads"
    SETTINGS = "settings"
    DOT = "dot"
    SPOT = "spot"  # the title: opens its info window


class Hit(NamedTuple):
    kind: HitKind
    dot: int = 0  # which dot


def hit_target(layout: BandLayout, x: float, y: float, *, items: int) -> Hit | None:
    """What a pointer at (x, y) units is over (None: the band's background up top, the logo,
    or the spotlight without titles)."""
    if layout.magnifier.contains(x, y):
        return Hit(HitKind.SEARCH)
    if layout.search.contains(x, y):
        return Hit(HitKind.FIELD)
    if layout.forge.contains(x, y):
        return Hit(HitKind.FORGE)
    if layout.downloads.contains(x, y):
        return Hit(HitKind.DOWNLOADS)
    if layout.settings.contains(x, y):
        return Hit(HitKind.SETTINGS)
    for index, dot in enumerate(layout.dots):
        if dot.grown(DOT_HIT).contains(x, y):
            return Hit(HitKind.DOT, index)
    if items and layout.spot.contains(x, y):
        return Hit(HitKind.SPOT)
    return None


def controls(layout: BandLayout) -> Sequence[Rect]:
    """The top row's parts that must never overlap (tests check it at every width)."""
    return (
        layout.brand,
        layout.search,
        layout.forge,
        layout.downloads,
        layout.settings,
    )


# -- the Series / Movies switch (on the tabs row, under the band) --------------------------------

PILL_PAD = 4  # the switch's solid pill around its two parts
SEGMENT_PAD_X = 30  # either side of "Series" / "Movies"
SEGMENT_PAD_Y = 9  # … above and under it
SEGMENT_FONT_SIZE = 17
SWITCH_MARGIN = 7  # the switch's own canvas around the pill: room for its shadow


@dataclass(frozen=True, slots=True)
class SwitchLayout:
    """The switch on its own canvas (`size`), in unscaled units: the pill and its Series and
    Movies parts."""

    size: tuple[float, float]
    pill: Rect
    segments: tuple[Rect, Rect]


def switch_layout(*, segment_width: float, line_height: float) -> SwitchLayout:
    """Two `segment_width` parts (each name's width plus SEGMENT_PAD_X either side), each a
    `line_height` line tall plus SEGMENT_PAD_Y above and under it, on a pill PILL_PAD around
    them, SWITCH_MARGIN in from the canvas's edges."""
    part_height = line_height + 2 * SEGMENT_PAD_Y
    pill = Rect(
        SWITCH_MARGIN, SWITCH_MARGIN, 2 * PILL_PAD + 2 * segment_width, 2 * PILL_PAD + part_height
    )
    series = Rect(pill.x + PILL_PAD, pill.y + PILL_PAD, segment_width, part_height)
    movies = Rect(series.right, series.y, segment_width, part_height)
    size = (pill.right + SWITCH_MARGIN, pill.bottom + SWITCH_MARGIN)
    return SwitchLayout(size=size, pill=pill, segments=(series, movies))


def switch_hit(layout: SwitchLayout, x: float, y: float) -> int | None:
    """Which part a pointer at (x, y) units is over: 0 = Series, 1 = Movies, None = neither."""
    for index, part in enumerate(layout.segments):
        if part.contains(x, y):
            return index
    return None
