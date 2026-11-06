"""The spotlight band's pure parts (the widget: app/ui/spotlight_band.py): its texts (the tag,
the title, the muted line), when it turns to the next title (`Rotation`) and where everything
goes (`band_layout`, `hit_target`; the Series / Movies / MediaForge switch beside the logo:
`switch_layout`), all in CTk's unscaled units."""

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
SEARCH_MIN_WIDTH = 140
SWITCH_GAP = 18  # "AirGrabber" → the switch
SEARCH_GAP = 14  # the search field → the first button
MAGNIFIER_LEFT = 8  # the 🔍 inside the search field
MAGNIFIER_WIDTH = 30
ENTRY_HEIGHT = 28
ENTRY_PAD_RIGHT = 16
BUTTON_SIZE = 40
BUTTON_GAP = 10
BUTTON_RADIUS = 10
BADGE_HEIGHT = 18
BADGE_OVERHANG = 6  # ⬇'s badge reaches this far past its corner
# The name button at the right end (app/services/profiles.py): a pill as tall as the buttons,
# the person's avatar in its left end, their name, then ▾.
PERSON_AVATAR = 30
PERSON_PAD_LEFT = 5  # the avatar's inset (it is centred top to bottom)
PERSON_NAME_GAP = 8  # the avatar → the name
PERSON_ARROW_GAP = 6  # the name → ▾
PERSON_PAD_RIGHT = 12
PERSON_NAME_MAX = 120  # a longer name is cut short
PERSON_FONT_SIZE = 14
PERSON_ARROW = "▾"
POSTER_TOP = ROW_TOP + ROW_HEIGHT + 22  # the row → the spotlight
POSTER_WIDTH = 44
POSTER_HEIGHT = 64
POSTER_RADIUS = 6
BAND_BOTTOM = 18  # the poster → the band's lower edge (the page tabs sit right under it)
BAND_HEIGHT = POSTER_TOP + POSTER_HEIGHT + BAND_BOTTOM
# MediaForge's page: only the top row, where it is on every page; so the list gets the room.
COMPACT_BOTTOM = 10  # the row → the band's lower edge
COMPACT_HEIGHT = ROW_TOP + ROW_HEIGHT + COMPACT_BOTTOM
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
    downloads: Rect
    settings: Rect
    badge: tuple[float, float]  # the ⬇ button's badge's top-right corner
    poster: Rect
    text_x: float  # the tag, title and muted line start here
    text_right: float  # … and end before this
    dots: tuple[Rect, ...]
    spot: Rect  # a click here opens the title (not on the controls or the dots)
    person: Rect = Rect(0, 0, 0, 0)  # the name button, at the right end (none: 0 wide)
    history: Rect = Rect(0, 0, 0, 0)  # 🕘, just left of ⚙: MediaForge's page only (else 0 wide)
    pill: Rect = Rect(0, 0, 0, 0)  # the main menu's switch (else 0 wide)
    parts: tuple[Rect, ...] = ()  # its parts: Series, Movies, MediaForge

    @property
    def row_middle(self) -> float:
        return ROW_TOP + ROW_HEIGHT / 2

    @property
    def avatar(self) -> Rect:
        """The person's avatar inside the name button."""
        person = self.person
        return Rect(
            person.x + PERSON_PAD_LEFT,
            person.y + (person.h - PERSON_AVATAR) / 2,
            PERSON_AVATAR,
            PERSON_AVATAR,
        )


def person_width(name_width: float, arrow_width: float) -> float:
    """The name button's width for a name (cut to PERSON_NAME_MAX) `name_width` wide."""
    return (
        PERSON_PAD_LEFT
        + PERSON_AVATAR
        + PERSON_NAME_GAP
        + min(name_width, PERSON_NAME_MAX)
        + PERSON_ARROW_GAP
        + arrow_width
        + PERSON_PAD_RIGHT
    )


def band_layout(
    width: float,
    *,
    word_width: float,
    dots: int = 0,
    current: int = 0,
    person_width: float = 0.0,
    history: bool = False,
    part_widths: Sequence[float] = (),
) -> BandLayout:
    """The band at `width` units: the logo and "AirGrabber" (`word_width` wide) on the left, then
    the main menu's switch (a part per `part_widths`), the search field, ⬇, the settings button
    and the name button (`person_width` wide; none at 0) on the right; under them the poster and
    the texts, the `dots` (the `current` one wide) on the right. The search field narrows (to
    SEARCH_MIN_WIDTH) before it would come too close to what is on its left. `history`: 🕘
    (MediaForge's page) goes right beside the settings button, with ⬇ and the search field moving
    over for it; the settings button and the name stay where they are on every page and no other
    page has a gap."""
    right = width - PAD_X
    logo = Rect(PAD_X, ROW_TOP, LOGO_SIZE, LOGO_SIZE)
    word_x = logo.right + WORD_GAP
    left_end = word_x + word_width
    person = Rect(right - person_width, ROW_TOP, person_width, BUTTON_SIZE)
    buttons_right = person.x - BUTTON_GAP if person_width > 0 else right
    settings = Rect(buttons_right - BUTTON_SIZE, ROW_TOP, BUTTON_SIZE, BUTTON_SIZE)
    clock = Rect(settings.x - BUTTON_GAP - BUTTON_SIZE, ROW_TOP, BUTTON_SIZE, BUTTON_SIZE)
    beside = clock if history else settings
    downloads = Rect(beside.x - BUTTON_GAP - BUTTON_SIZE, ROW_TOP, BUTTON_SIZE, BUTTON_SIZE)
    search_right = downloads.x - SEARCH_GAP
    switch = switch_layout(segment_widths=part_widths, x=left_end + SWITCH_GAP) if part_widths else None
    room = search_right - ((switch.pill.right if switch else left_end) + CLUSTER_GAP)
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
        downloads=downloads,
        settings=settings,
        badge=(downloads.right + BADGE_OVERHANG, downloads.y - BADGE_OVERHANG),
        poster=poster,
        text_x=text_x,
        text_right=text_right,
        dots=tuple(dot_rects),
        spot=spot,
        person=person if person_width > 0 else Rect(0, 0, 0, 0),
        history=clock if history else Rect(0, 0, 0, 0),
        pill=switch.pill if switch else Rect(0, 0, 0, 0),
        parts=switch.segments if switch else (),
    )


class HitKind(StrEnum):
    SEARCH = "search"  # the 🔍
    FIELD = "field"  # the search field around its text box: focuses it
    HISTORY = "history"  # 🕘, on MediaForge's page
    DOWNLOADS = "downloads"
    SETTINGS = "settings"
    PERSON = "person"  # the name button: who is selected, and the people menu
    DOT = "dot"
    SPOT = "spot"  # the title: opens its info window
    PART = "part"  # a part of the main menu's switch


class Hit(NamedTuple):
    kind: HitKind
    dot: int = 0  # which dot, or which part of the switch


def hit_target(layout: BandLayout, x: float, y: float, *, items: int) -> Hit | None:
    """What a pointer at (x, y) units is over (None: the band's background up top, the logo,
    or the spotlight without titles)."""
    for index, part in enumerate(layout.parts):
        if part.contains(x, y):
            return Hit(HitKind.PART, index)
    if layout.magnifier.contains(x, y):
        return Hit(HitKind.SEARCH)
    if layout.search.contains(x, y):
        return Hit(HitKind.FIELD)
    if layout.history.w > 0 and layout.history.contains(x, y):
        return Hit(HitKind.HISTORY)
    if layout.downloads.contains(x, y):
        return Hit(HitKind.DOWNLOADS)
    if layout.settings.contains(x, y):
        return Hit(HitKind.SETTINGS)
    if layout.person.w > 0 and layout.person.contains(x, y):
        return Hit(HitKind.PERSON)
    for index, dot in enumerate(layout.dots):
        if dot.grown(DOT_HIT).contains(x, y):
            return Hit(HitKind.DOT, index)
    if items and layout.spot.contains(x, y):
        return Hit(HitKind.SPOT)
    return None


def controls(layout: BandLayout) -> Sequence[Rect]:
    """The top row's parts that must never overlap (tests check it at every width)."""
    parts: tuple[Rect, ...] = (layout.brand, layout.search, layout.downloads, layout.settings)
    if layout.pill.w > 0:
        parts = (*parts, layout.pill)
    if layout.history.w > 0:
        parts = (*parts, layout.history)
    return (*parts, layout.person) if layout.person.w > 0 else parts


# -- the Series / Movies / MediaForge switch (in the band's top row, beside the logo) ----------

PILL_PAD = 4  # the switch's solid pill around its parts
SEGMENT_PAD_X = 24  # either side of "Series" / "Movies" / "MediaForge"
SEGMENT_FONT_SIZE = 15
SWITCH_BADGE_GAP = 5  # MediaForge's word → its "needs you" badge, in the part's own padding
SWITCH_BADGE_PAD = 6  # the badge's width around its number


class SwitchPart(StrEnum):
    """The switch's parts, left to right."""

    SERIES = "series"
    MOVIES = "movies"
    FORGE = "forge"


@dataclass(frozen=True, slots=True)
class SwitchLayout:
    """The switch in the band's top row, in unscaled units: the pill and its parts (Series,
    Movies, MediaForge)."""

    pill: Rect
    segments: tuple[Rect, ...]


def switch_layout(*, segment_widths: Sequence[float], x: float) -> SwitchLayout:
    """A part per width (a name's width plus SEGMENT_PAD_X either side), on a pill PILL_PAD around
    them, starting at `x`, as tall as the top row."""
    pill = Rect(x, ROW_TOP, 2 * PILL_PAD + sum(segment_widths), ROW_HEIGHT)
    segments: list[Rect] = []
    left = pill.x + PILL_PAD
    for width in segment_widths:
        segments.append(Rect(left, pill.y + PILL_PAD, width, ROW_HEIGHT - 2 * PILL_PAD))
        left += width
    return SwitchLayout(pill=pill, segments=tuple(segments))
