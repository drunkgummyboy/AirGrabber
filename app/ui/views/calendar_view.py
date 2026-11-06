"""Weekly episode calendar: tracked episodes from SQL, plus suggested shows per day. Both are
the selected person's (Settings' `current_profile`, read at every load; app/services/profiles.py).

While Plex is connected (`LibraryService.plex_connected`; without it the page shows nothing of
what you have or miss, owner's decision for 3.1.0), missing episodes (`missing_between`: aired
at least a day ago, neither in Plex nor downloaded) get the cards' amber note; new ones
(`new_between`: aired less than a day ago) a quiet blue "New" note; the stack cards' rows say
which are in Plex (`plex_between`). Several episodes of one show on one day share a
stack card (app/ui/calendar_stack.py: `stack_entries`); the page remembers which stacks are
unfolded for the session, so a reload (a sync, a settings change, a grab) keeps them open.

Suggestions (💡): every day gets its best-ranked one; then each week row's quieter days get
more, best first, as many as fit within the row's tallest day (`extra_suggestions`), so the
grid looks fuller without growing. A show is suggested once a week row (`take_new`), on its
first day there. (Days with few scripted shows, such as Saturdays, are filled with popular
reality and variety shows: `LibraryService.suggestions`.) Week rows that have passed entirely
get none, nor are they fetched (`suggestion_start`): those rows size to their tracked episodes.
The current week keeps its suggestions on its earlier days too, so its row doesn't change
shape midweek.

A library change (a show tracked from a suggestion, say) reloads the page on screen through
`refresh_quietly`: the new grid is built hidden under the one on screen and swapped in when
it is complete, so the page doesn't blank, rebuild day by day or lose its scroll position.
Other loads (another week, the week count, the card size, the first load) build in view.

While the selected person follows no shows (`LibraryService.stats`, read at every load), a
box sits over the grid: "You don't follow any shows yet" with "Pick shows to follow…", which
opens the "Let's get started" window (app/ui/get_started.py, through `on_pick_shows`). The
quiet reload after a show is followed takes it away.

Its controls, on the main window's row under the band (`controls_master`, `show_controls`): in the
middle the visible dates between ❮ and ❯ (a click on them goes back to today), on the right the
number of weeks (1–5) and the 💡 Suggestions toggle. The cards follow Settings → Card size
(`card_scale`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from datetime import date, timedelta
from typing import Any, Literal, NamedTuple

import customtkinter as ctk

from app.i18n import N_, day_short, month_short, t
from app.models import CalendarEntry, Settings
from app.services import ImageCache, LibraryService, SettingsService
from app.services.library import RECENT_DAYS, calendar_range
from app.ui import theme
from app.ui.calendar_cards import (
    CompactEpisodeCard,
    EpisodeActions,
    EpisodeCard,
    OpenShow,
    SuggestionCard,
)
from app.ui.calendar_stack import CompactStackCard, FoldKey, Stack, StackCard, stack_entries
from app.ui.card_scale import FULL, CardScale
from app.ui.dialog_kit import primary_button
from app.ui.scrollable import ScrollableFrame
from app.ui.tasks import spawn
from app.ui.text_fit import SLACK
from app.ui.tooltip import add_tooltip
from app.ui.views.base import BaseView, StatusReporter
from app.ui.widgets import destroy_tree

log = logging.getLogger(__name__)

CardStyle = Literal["full", "compact"]

WEEK_COUNTS = tuple(range(1, 6))  # the choices of Settings.weeks_to_show
# Shown through t().
WEEKS_TIP = N_("How many weeks to show")
SUGGESTIONS_TEXT = N_("💡 Suggestions")
SUGGESTIONS_TIP = N_(
    "Also show one popular episode a day from a show you don't follow yet. "
    "Click + Track on it to start following that show."
)

# The box over the grid while the person follows no shows; shown through t().
EMPTY_ICON = "📺"
EMPTY_TITLE = N_("You don't follow any shows yet")
EMPTY_TEXT = N_("Pick a few series and their new episodes appear here.")
PICK_SHOWS_TEXT = N_("Pick shows to follow…")
EMPTY_BOX_WIDTH = 340
EMPTY_BOX_PAD_X = 24
EMPTY_BOX_PAD_Y = 20
EMPTY_BOX_RELY = 0.45  # its middle, down the page

HEADER_GAP = 22  # below the header row, above the grid
RANGE_FONT_SIZE = 20  # the visible dates, bold
NAV_HEIGHT = 28  # the ◀ / Today / ▶ buttons, the week count and the Suggestions toggle
NAV_FONT_SIZE = 13
NAV_RADIUS = 7
RANGE_PAD_X = 8  # either side of the dates on their button
ARROW_SIZE = 32  # the round ❮ and ❯ buttons
ARROW_FONT_SIZE = 13
CONTROLS_PAD_RIGHT = 20  # the row's right end, level with the band's name button
BACK_TO_TODAY = N_("Back to today")
RANGE_ARROW_GAP = 4  # between the dates and ❮ / ❯
CONTROL_GAP = 10  # between the week count and the Suggestions toggle
WEEK_BUTTON_WIDTH = 30  # each of the week count's 1–5
TOGGLE_PAD_X = 14  # the Suggestions toggle's room left and right of its text
# Inside a day cell, around its header and cards. Horizontally only 8: the poster cards keep
# room for their text and Download row at the narrower windows.
CELL_PAD_X = 8
CELL_PAD_Y = 12
CARD_GAP = 12  # between the cards of a day
# Most suggestions a day: the first, plus extras that fill a week's quieter days up to its
# busiest day's height.
SUGGESTION_CAP = 8
# Suggestions fetched a day: more than a day shows, so a day keeps enough once the shows already
# suggested on another day of its week row are left out (`take_new`).
SUGGESTION_POOL = 2 * SUGGESTION_CAP
TODAY_BORDER_WIDTH = 2
PILL_PAD_X = 10  # the "Today" pill's padding around its text
PILL_PAD_Y = 2

def _day_month(day: date) -> str:
    """ "12 Oct" / "12 okt"."""
    return f"{day.day} {month_short(day)}"


def range_label(start: date, end: date) -> str:
    """The visible dates: "12 – 25 Oct", "28 Sep – 11 Oct", or with the years when the range
    spans two: "29 Dec 2026 – 11 Jan 2027"."""
    if start.year != end.year:
        return f"{_day_month(start)} {start.year} – {_day_month(end)} {end.year}"
    if start.month != end.month:
        return f"{_day_month(start)} – {_day_month(end)}"
    return f"{start.day} – {_day_month(end)}"


def range_slot_width(measure: Callable[[str], int]) -> int:
    """Room for the labels `range_label` gives the grid within one year: the widest, measured.
    The grid holds 1–5 weeks, and one more at the start of a week (`calendar_range`).

    A slot this wide keeps the buttons after the label in place all year; only the longer
    labels with years, of the few ranges that cross into a new year, push them a little.
    """
    first = date(2026, 1, 1)  # any year: the month names are what differ
    labels = (
        range_label(start, end)
        for weeks in range(1, WEEK_COUNTS[-1] + 2)
        for start in (first + timedelta(days=day) for day in range(365))
        if (end := start + timedelta(weeks=weeks, days=-1)).year == start.year
    )
    return max(measure(label) for label in labels) + SLACK


def suggestions_look(shown: bool) -> dict[str, Any]:
    """The Suggestions toggle's colours: filled accent while on, an outline while off."""
    if shown:
        return {
            "fg_color": theme.ACCENT,
            "hover_color": theme.ACCENT_HOVER,
            "border_color": theme.ACCENT,
            "text_color": theme.ON_COLOR,
        }
    return {
        "fg_color": "transparent",
        "hover_color": theme.SURFACE,
        "border_color": theme.EDGE,
        "text_color": theme.TEXT_MUTED,
    }


def day_label(day: date) -> str:
    """A day cell's header: "Mon 12", with the month on its first day: "Mon 1 Nov" (in Dutch
    "ma 12", "ma 1 nov")."""
    text = f"{day_short(day)} {day.day}"
    return f"{text} {month_short(day)}" if day.day == 1 else text


def card_style(day: date, *, today: date) -> CardStyle:
    """Poster cards from RECENT_DAYS days before `today` on, compact cards before."""
    return "full" if day >= today - timedelta(days=RECENT_DAYS) else "compact"


def by_day(entries: Iterable[CalendarEntry]) -> dict[date, list[CalendarEntry]]:
    """`entries` grouped by air date, each day's in their given order (undated ones dropped)."""
    days: dict[date, list[CalendarEntry]] = {}
    for entry in entries:
        if entry.episode.air_date is not None:
            days.setdefault(entry.episode.air_date, []).append(entry)
    return days


def day_suggestions(
    suggestions: Iterable[CalendarEntry], *, cap: int = SUGGESTION_CAP
) -> dict[date, list[CalendarEntry]]:
    """Each day's suggestions, best-ranked first (they arrive best first within each day), at
    most `cap` a day and no show twice on one day."""
    days: dict[date, list[CalendarEntry]] = {}
    for day, entries in by_day(suggestions).items():
        picks: list[CalendarEntry] = []
        seen: set[int] = set()
        for entry in entries:
            if entry.show.id in seen or len(picks) >= cap:
                continue
            seen.add(entry.show.id)
            picks.append(entry)
        days[day] = picks
    return days


def take_new(picks: Iterable[CalendarEntry], shown: set[int], count: int) -> list[CalendarEntry]:
    """The first `count` of a day's `picks` whose show isn't in `shown` (the shows already
    suggested on its week row); their shows join `shown`, so no show is suggested twice in a
    week."""
    taken: list[CalendarEntry] = []
    for entry in picks:
        if len(taken) >= count:
            break
        if entry.show.id not in shown:
            shown.add(entry.show.id)
            taken.append(entry)
    return taken


def extra_suggestions(used: Sequence[int], target: int, slot: int, *, cap: int) -> list[int]:
    """How many more suggestions each day of a week row gets: as many `slot`-high cards (a
    suggestion with its gap) as fit between the day's `used` height (its tracked cards and its
    first suggestion) and the row's `target` (its tallest day), at most `cap`. The row never
    grows: a day already at the target gets none. One unit throughout (e.g. physical pixels)."""
    if slot <= 0 or cap <= 0:
        return [0 for _ in used]
    return [min(cap, max(0, (target - height) // slot)) for height in used]


def suggestion_start(start: date, end: date, *, today: date) -> date | None:
    """The first day that gets suggestions on a grid of week rows from `start` to `end`: the
    first day of the first row whose last day is `today` or later. None when every row has
    passed."""
    first = start
    while first <= end:
        if first + timedelta(days=6) >= today:
            return first
        first += timedelta(weeks=1)
    return None


def kept_scroll(
    top: float, view: float, old_rows: Sequence[int], new_rows: Sequence[int]
) -> float | None:
    """Where to scroll a grid of week rows starting at `new_rows` (pixels from its top) so
    that the row the view showed of the old grid (`old_rows`, scrolled to `top`, `view`
    high) stays where it was: the first row that started in view, else the row the view was
    inside of. None at the very top (it stays there) or without rows."""
    if top <= 0 or not old_rows or len(old_rows) != len(new_rows):
        return None
    in_view = [row for row, start in enumerate(old_rows) if top <= start < top + view]
    if in_view:
        row = in_view[0]
    else:
        row = max((row for row, start in enumerate(old_rows) if start <= top), default=0)
    return max(0.0, top - old_rows[row] + new_rows[row])


class _Shown(NamedTuple):
    """What a complete grid shows; a quiet reload of the same keeps it on screen until the
    new one is built."""

    start: date
    end: date
    today: date
    profile_id: int
    scale: CardScale
    suggestions: bool


def _days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=offset) for offset in range((end - start).days + 1)]


async def _yield_to_ui() -> None:
    """Pause a long build so Tk can draw what was built so far and handle input.

    sleep(0) is not enough: async-tkinter-loop drains Tk events from its own task, which sleeps
    10 ms between drains, and a task that yields with sleep(0) is queued ahead of it again —
    the build would run on and Tk would draw it all in one long freeze. A timed sleep puts this
    task behind the Tk task's expired timer (or, if Tk drained moments ago, costs ~1 ms).
    """
    await asyncio.sleep(0.001)


def _arrow_button(
    master: Any, glyph: str, command: Callable[[], None], tip: str
) -> ctk.CTkButton:
    """❮ or ❯, either side of the dates: a round button on the page's soft fill, without an
    outline."""
    button = ctk.CTkButton(
        master,
        text=glyph,
        width=ARROW_SIZE,
        height=ARROW_SIZE,
        corner_radius=ARROW_SIZE // 2,
        border_width=0,
        border_spacing=0,
        fg_color=theme.SURFACE,
        hover_color=theme.CHIP_HOVER,
        text_color=theme.TEXT,
        font=theme.font(ARROW_FONT_SIZE),
        command=command,
    )
    add_tooltip(button, tip)
    return button


def _cell_header(cell: ctk.CTkFrame, day: date, *, is_today: bool) -> ctk.CTkFrame:
    """The day ("Mon 12") at the left; today's cell adds a "Today" pill at the right."""
    pill_font = theme.font(12)
    height = int(pill_font.metrics("linespace")) + 2 * PILL_PAD_Y
    header = ctk.CTkFrame(cell, fg_color="transparent", height=height)
    ctk.CTkLabel(
        header,
        text=day_label(day),
        font=theme.font(11, bold=True),
        text_color=theme.TEXT if is_today else theme.TEXT_DIM,
        height=height,
        anchor="w",
    ).pack(side="left")
    if is_today:
        today = t("Today")
        ctk.CTkLabel(
            header,
            text=today,
            font=pill_font,
            text_color=theme.ON_ACCENT,
            fg_color=theme.ACCENT,
            corner_radius=6,
            width=round(pill_font.measure(today)) + 2 * PILL_PAD_X,
            height=height,
        ).pack(side="right")
    return header


class CalendarView(BaseView):
    """Weeks of tracked episodes in a 7-column grid, each day of the current and later weeks
    with one suggested show after them (unless suggestions are switched off); ❮ and ❯ move
    through the weeks, a click on the dates goes back to today.

    `card_scale`: the card size (Settings), read at every load. `on_open_show`: what a click on
    a card's poster or name (suggestions included) does, the show's window."""

    def __init__(
        self,
        master: Any,
        *,
        library: LibraryService,
        settings: SettingsService,
        images: ImageCache,
        status: StatusReporter,
        episode_actions: EpisodeActions,
        card_scale: Callable[[], CardScale] = lambda: FULL,
        on_open_show: OpenShow | None = None,
        on_pick_shows: Callable[[], None] | None = None,
        controls_master: Any = None,
        **kwargs: Any,
    ) -> None:
        """`on_pick_shows`: opens "Let's get started"; with it, the page shows its box over the
        grid while the person follows no shows."""
        super().__init__(master, **kwargs)
        self._library = library
        self._settings = settings
        self._images = images
        self._status = status
        self._episode_actions = episode_actions  # the cards' Download and ⋯ buttons
        self._card_scale = card_scale
        self._on_open_show = on_open_show
        self._on_pick_shows = on_pick_shows
        self._empty_box: ctk.CTkFrame | None = None  # made the first time it shows
        self._weeks: ctk.CTkFrame | None = None  # the grid of day cells, rebuilt on every load
        self._pending: ctk.CTkFrame | None = None  # a quiet load's grid, hidden until complete
        self._shown: _Shown | None = None  # what the complete grid on screen shows
        self._rendered_for: date | None = None  # the "today" of the last load
        self._offset = 0  # weeks from the current one; this session only
        self._unfolded: set[FoldKey] = set()  # the stack cards unfolded; this session only
        self._suggestions_shown = settings.current.show_suggestions  # the toggle's state
        # The person whose calendar shows (Settings' `current_profile`, read at every load):
        # the suggestions' "+ Track" adds to their library.
        self._profile_id = settings.current.current_profile

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._build_header(controls_master)
        self._sync_controls(settings.current)

        self._scroll = ScrollableFrame(self, fg_color="transparent")
        self._scroll.grid(row=1, column=0, sticky="nsew", padx=15, pady=(0, 5))

    # -- header --------------------------------------------------------------------------------

    def _build_header(self, master: Any = None) -> None:
        """The dates between ❮ and ❯, centred (a click on them goes back to today), and on the
        right the "Finding suggestions…" note, the week count and the Suggestions toggle: on
        `master` (the main window's row under the band, `show_controls`), else on a row of the
        page's own."""
        header = master
        if header is None:
            header = ctk.CTkFrame(self, fg_color="transparent", height=NAV_HEIGHT)
            header.grid(row=0, column=0, sticky="ew", padx=15, pady=(5, HEADER_GAP))
            header.grid_columnconfigure(1, weight=1)
        self._header = header

        dates = ctk.CTkFrame(header, fg_color="transparent")
        self._dates = dates
        range_font = theme.font(RANGE_FONT_SIZE, bold=True)
        self._previous = _arrow_button(
            dates, "❮", lambda: self._go_to(self._offset - 1), t("Previous week")
        )
        self._previous.pack(side="left")
        self._range = ctk.CTkButton(
            dates,
            text="",
            font=range_font,
            text_color=theme.TEXT,
            fg_color="transparent",
            hover_color=theme.CHIP_HOVER,
            width=range_slot_width(range_font.measure) + 2 * RANGE_PAD_X,
            height=NAV_HEIGHT,
            corner_radius=NAV_RADIUS,
            border_spacing=0,
            command=lambda: self._go_to(0),
        )
        self._range.pack(side="left", padx=RANGE_ARROW_GAP)
        add_tooltip(self._range, t(BACK_TO_TODAY))
        self._next = _arrow_button(
            dates, "❯", lambda: self._go_to(self._offset + 1), t("Next week")
        )
        self._next.pack(side="left")

        right = ctk.CTkFrame(header, fg_color="transparent")
        self._note = ctk.CTkLabel(
            right, text="", font=theme.font(12), text_color=theme.TEXT_MUTED, anchor="e"
        )
        self._note.pack(side="left", padx=(0, CONTROL_GAP))
        self._week_count = ctk.CTkSegmentedButton(
            right,
            values=[str(weeks) for weeks in WEEK_COUNTS],
            width=WEEK_BUTTON_WIDTH * len(WEEK_COUNTS),
            height=NAV_HEIGHT,
            corner_radius=NAV_RADIUS,
            font=theme.font(NAV_FONT_SIZE, bold=True),
            fg_color=theme.SURFACE,
            selected_color=theme.SELECTED,
            selected_hover_color=theme.SELECTED_HOVER,
            unselected_color=theme.SURFACE,
            unselected_hover_color=theme.CHIP_HOVER,
            text_color=theme.BUTTON_TEXT,
            dynamic_resizing=False,
            command=self._save_weeks,
        )
        self._week_count.pack(side="left")
        add_tooltip(self._week_count, t(WEEKS_TIP))
        toggle_font = theme.font(NAV_FONT_SIZE)
        self._suggestions_toggle = ctk.CTkButton(
            right,
            text=t(SUGGESTIONS_TEXT),
            width=round(toggle_font.measure(t(SUGGESTIONS_TEXT))) + 2 * TOGGLE_PAD_X,
            height=NAV_HEIGHT,
            corner_radius=NAV_RADIUS,
            border_width=1,
            font=toggle_font,
            command=self._toggle_suggestions,
        )
        self._suggestions_toggle.pack(side="left", padx=(CONTROL_GAP, 0))
        add_tooltip(self._suggestions_toggle, t(SUGGESTIONS_TIP))
        self._right = right
        if master is None:
            self.show_controls(True)

    def show_controls(self, shown: bool) -> None:
        """The dates in the middle of the row and the rest at its right end, or neither."""
        if shown:
            self._dates.place(relx=0.5, rely=0.5, anchor="center")
            self._right.grid(row=0, column=2, sticky="e", padx=(0, CONTROLS_PAD_RIGHT))
        else:
            self._dates.place_forget()
            self._right.grid_remove()

    def _go_to(self, offset: int) -> None:
        if offset != self._offset:
            self._offset = offset
            self.refresh()

    # No refresh in the two savers: the window's settings listener reloads the visible view.

    def _save_weeks(self, choice: str) -> None:
        spawn(
            self._settings.update(weeks_to_show=int(choice)),
            name="CalendarView.save_weeks",
        )

    def _toggle_suggestions(self) -> None:
        shown = not self._suggestions_shown
        self._show_suggestions_state(shown)
        spawn(
            self._settings.update(show_suggestions=shown),
            name="CalendarView.save_show_suggestions",
        )

    def _show_suggestions_state(self, shown: bool) -> None:
        self._suggestions_shown = shown
        self._suggestions_toggle.configure(**suggestions_look(shown))

    def _sync_controls(self, prefs: Settings) -> None:
        """Show the stored values (the window may reload with settings saved elsewhere)."""
        self._week_count.set(str(prefs.weeks_to_show))
        self._show_suggestions_state(prefs.show_suggestions)

    # -- loading -------------------------------------------------------------------------------

    def is_stale(self) -> bool:
        # After midnight the grid shows the wrong today, range and card styles.
        return super().is_stale() or self._rendered_for != date.today()

    async def load(self) -> None:
        quiet = self.quiet_load  # before the first await (BaseView)
        prefs = self._settings.current
        self._sync_controls(prefs)
        today = date.today()
        self._rendered_for = today
        scale = self._card_scale()
        start, end = calendar_range(
            today, first_day=prefs.first_day, weeks=prefs.weeks_to_show, offset=self._offset
        )
        self._range.configure(text=range_label(start, end))
        profile_id = prefs.current_profile
        shown = _Shown(start, end, today, profile_id, scale, prefs.show_suggestions)
        # Quietly only over a complete grid of the same weeks; else built in view.
        quiet = quiet and self._weeks is not None and self._shown == shown
        self._drop_pending()
        if not quiet:
            self._shown = None
            self._note.configure(text="")

        entries = await self._library.calendar(start, end, profile_id=profile_id)
        # What you have and miss: with Plex only (the calls answer "nothing" without it).
        marks = self._library.plex_connected
        missing = await self._library.missing_between(start, end, profile_id=profile_id)
        new = await self._library.new_between(start, end, profile_id=profile_id)
        in_plex = await self._library.plex_between(start, end)
        follows_none = (
            self._on_pick_shows is not None
            and (await self._library.stats(profile_id=profile_id)).tracked_shows == 0
        )
        if not self.winfo_exists():
            return
        self.show_empty_box(follows_none)
        self._profile_id = profile_id
        weeks = self._new_grid(hidden=quiet)
        cells = await self._render_tracked(
            weeks,
            _days(start, end),
            by_day(entries),
            today=today,
            scale=scale,
            marks=marks,
            missing=missing,
            new=new,
            in_plex=in_plex,
        )

        failed = False
        first = suggestion_start(start, end, today=today) if prefs.show_suggestions else None
        if first is not None:
            if not quiet:
                self._note.configure(text=t("Finding suggestions…"))
            try:
                suggestions = await self._library.suggestions(
                    first, end, profile_id=profile_id, per_day=SUGGESTION_POOL
                )
            except Exception:
                log.exception("Couldn't load calendar suggestions for %s – %s", first, end)
                failed = True
            else:
                if not self.winfo_exists():
                    return
                await self._render_suggestions(
                    {day: cell for day, cell in cells.items() if day >= first},
                    day_suggestions(suggestions, cap=SUGGESTION_POOL),
                    scale=scale,
                )
        if not self.winfo_exists():
            return
        if quiet:
            await self._swap_in(weeks)
        self._shown = shown
        self._note.configure(text=t("Couldn't load suggestions") if failed else "")

    # -- the box while no show is followed ---------------------------------------------------------

    @property
    def empty_box_shown(self) -> bool:
        return self._empty_box is not None and bool(self._empty_box.winfo_manager())

    def show_empty_box(self, shown: bool) -> None:
        """The box over the grid (made the first time), or not."""
        if not shown:
            if self._empty_box is not None:
                self._empty_box.place_forget()
            return
        if self._empty_box is None:
            self._empty_box = self._build_empty_box()
        self._empty_box.place(relx=0.5, rely=EMPTY_BOX_RELY, anchor="center")
        self._empty_box.lift()

    def _build_empty_box(self) -> ctk.CTkFrame:
        box = ctk.CTkFrame(
            self,
            fg_color=theme.SURFACE,
            border_width=1,
            border_color=theme.EDGE,
            corner_radius=12,
        )
        inner = ctk.CTkFrame(box, fg_color="transparent")
        inner.pack(padx=EMPTY_BOX_PAD_X, pady=EMPTY_BOX_PAD_Y)
        wrap = EMPTY_BOX_WIDTH - 2 * EMPTY_BOX_PAD_X
        ctk.CTkLabel(inner, text=EMPTY_ICON, font=theme.font(30), text_color=theme.TEXT).pack()
        ctk.CTkLabel(
            inner,
            text=t(EMPTY_TITLE),
            font=theme.font(17, bold=True),
            text_color=theme.TEXT,
            wraplength=wrap,
            justify="center",
        ).pack(pady=(4, 0))
        ctk.CTkLabel(
            inner,
            text=t(EMPTY_TEXT),
            font=theme.font(13),
            text_color=theme.TEXT_MUTED,
            wraplength=wrap,
            justify="center",
        ).pack(pady=(4, 14))
        text = t(PICK_SHOWS_TEXT)
        button_font = theme.font(15, bold=True)
        self.pick_shows_button = primary_button(
            inner,
            text,
            self._pick_shows,
            width=max(140, round(button_font.measure(text)) + 40),
        )
        self.pick_shows_button.pack()
        return box

    def _pick_shows(self) -> None:
        if self._on_pick_shows is not None:
            self._on_pick_shows()

    def _new_grid(self, *, hidden: bool) -> ctk.CTkFrame:
        """An empty grid of 7 day columns. In view it replaces the grid on screen at once;
        `hidden`, it is placed under that grid (which covers it: the scroll frame is as tall as
        the packed grid and clips the rest) until `_swap_in`. Either way it is laid out, so its
        cards size themselves and bake their images meanwhile."""
        weeks = ctk.CTkFrame(self._scroll, fg_color="transparent")
        for column in range(7):
            weeks.grid_columnconfigure(column, weight=1, uniform="day")
        if hidden and self._weeks is not None:
            weeks.place(x=0, y=0, relwidth=1)
            weeks.tk.call("lower", str(weeks), str(self._weeks))
            self._pending = weeks
        else:
            if self._weeks is not None:
                destroy_tree(self._weeks)
            weeks.pack(fill="both", expand=True)
            self._weeks = weeks
        return weeks

    async def _swap_in(self, weeks: ctk.CTkFrame) -> None:
        """Show the hidden grid in place of the one on screen, in one step, scrolled so the
        week row at the top of the view stays where it was (a row above it may have grown:
        a newly tracked show's earlier episodes)."""
        await _yield_to_ui()  # the last cards' sizes and images
        if not weeks.winfo_exists():
            return
        old, self._weeks, self._pending = self._weeks, weeks, None
        if old is not None and old.winfo_exists():
            self._keep_scroll(old, weeks)
        weeks.place_forget()
        weeks.pack(fill="both", expand=True)
        if old is not None and old.winfo_exists():
            destroy_tree(old)

    def _keep_scroll(self, old: ctk.CTkFrame, new: ctk.CTkFrame) -> None:
        """Scroll so that `new` (laid out, same rows as `old`) shows the week row the view
        shows of `old` where it was: the first row that starts in view, else the row the view
        is inside of. Before the swap, so no frame is drawn in between: both grids sit at the
        scroll frame's top."""
        canvas = self._scroll._parent_canvas  # CTk has no scroll API
        rows = old.grid_size()[1]
        if new.grid_size()[1] != rows:
            return
        target = kept_scroll(
            canvas.canvasy(0),
            canvas.winfo_height(),
            [old.grid_bbox(0, row)[1] for row in range(rows)],
            [new.grid_bbox(0, row)[1] for row in range(rows)],
        )
        if target is None:
            return
        height = max(new.winfo_reqheight(), 1)
        canvas.configure(scrollregion=(0, 0, canvas.winfo_width(), height))
        canvas.yview_moveto(target / height)

    def _drop_pending(self) -> None:
        """A hidden grid an interrupted quiet load left behind."""
        pending, self._pending = self._pending, None
        if pending is not None and pending.winfo_exists():
            destroy_tree(pending)

    # -- rendering -----------------------------------------------------------------------------

    async def _render_tracked(
        self,
        weeks: ctk.CTkFrame,
        days: Sequence[date],
        tracked: Mapping[date, Sequence[CalendarEntry]],
        *,
        today: date,
        scale: CardScale,
        marks: bool = False,
        missing: Collection[int] = frozenset(),
        new: Collection[int] = frozenset(),
        in_plex: Collection[int] = frozenset(),
    ) -> dict[date, ctk.CTkFrame]:
        """Fill the empty grid `weeks` (`_new_grid`): one cell per day with its tracked
        episodes, a show's episodes of one day on one stack card. `marks`: Plex is connected
        (what you have and miss shows); then `missing`: the ids of the episodes that get the
        amber note; `new`: those aired less than a day ago, a blue "New" note; `in_plex`:
        those in Plex. Returns the cells.

        Yields to the event loop after each day: a whole week of poster cards takes Tk ~250 ms
        to draw. Cancelled at a yield, it leaves a partial grid for the next load to destroy.
        """
        cells: dict[date, ctk.CTkFrame] = {}
        for index, day in enumerate(days):
            is_today = day == today
            cell = ctk.CTkFrame(
                weeks,
                corner_radius=6,
                border_width=TODAY_BORDER_WIDTH if is_today else 1,
                fg_color=theme.TODAY_BG if is_today else theme.CELL_BG,
                border_color=theme.ACCENT if is_today else theme.CELL_BORDER,
            )
            # `days` starts on the first weekday, so the column is the weekday's position.
            cell.grid(row=index // 7, column=index % 7, sticky="nsew", padx=3, pady=3)
            _cell_header(cell, day, is_today=is_today).pack(
                fill="x", padx=CELL_PAD_X, pady=CELL_PAD_Y
            )
            cells[day] = cell

            style = card_style(day, today=today)
            for group in stack_entries(tracked.get(day, ())):
                card = self._episode_card(
                    cell,
                    group,
                    style=style,
                    scale=scale,
                    marks=marks,
                    missing=missing,
                    new=new,
                    in_plex=in_plex,
                )
                card.pack(fill="x", padx=CELL_PAD_X, pady=(0, CARD_GAP))
            await _yield_to_ui()
        return cells

    def _episode_card(
        self,
        cell: ctk.CTkFrame,
        group: Sequence[CalendarEntry],
        *,
        style: CardStyle,
        scale: CardScale,
        marks: bool = False,
        missing: Collection[int] = frozenset(),
        new: Collection[int] = frozenset(),
        in_plex: Collection[int] = frozenset(),
    ) -> ctk.CTkFrame:
        """The card of one show's episodes of the day: a single episode's card, or a stack
        card for more. Without Plex (`marks` False) no note: nothing is missing or new."""
        marked = marks and any(entry.episode.id in missing for entry in group)
        fresh = marks and any(entry.episode.id in new for entry in group)
        if len(group) == 1:
            entry = group[0]
            if style == "full":
                return EpisodeCard(
                    cell,
                    entry,
                    images=self._images,
                    actions=self._episode_actions,
                    scale=scale,
                    on_open_show=self._on_open_show,
                    missing=marked,
                    new=fresh,
                )
            return CompactEpisodeCard(
                cell,
                entry,
                actions=self._episode_actions,
                scale=scale,
                on_open_show=self._on_open_show,
                missing=marked,
                new=fresh,
            )
        stack = Stack(tuple(group))
        if style == "full":
            key = stack.fold_key
            return StackCard(
                cell,
                stack,
                images=self._images,
                actions=self._episode_actions,
                scale=scale,
                on_open_show=self._on_open_show,
                unfolded=key in self._unfolded,
                on_fold=lambda unfolded: self._remember_fold(key, unfolded),
                marks=marks,
                in_plex=in_plex,
                new=new,
            )
        return CompactStackCard(
            cell,
            stack,
            actions=self._episode_actions,
            scale=scale,
            on_open_show=self._on_open_show,
            missing=marked,
            marks=marks,
            in_plex=in_plex,
            new=new,
        )

    def _remember_fold(self, key: FoldKey, unfolded: bool) -> None:
        if unfolded:
            self._unfolded.add(key)
        else:
            self._unfolded.discard(key)

    async def _render_suggestions(
        self,
        cells: Mapping[date, ctk.CTkFrame],
        picks: Mapping[date, Sequence[CalendarEntry]],
        *,
        scale: CardScale,
    ) -> None:
        """The suggestions, after each day's tracked episodes, a week row at a time: each day's
        best-ranked one, then the next best on the row's quieter days, as many as fit within
        the row's tallest day (`extra_suggestions`); a show already suggested on the row is
        skipped (`take_new`). Yields to the event loop after each card.

        Heights are compared in physical pixels, as Tk reports them (`winfo_reqheight`, the
        pack gaps as `pack_info` gives them, both already DPI-scaled by CTk)."""
        days = list(cells)
        for first in range(0, len(days), 7):
            week = days[first : first + 7]
            shown: set[int] = set()  # the shows suggested on this row so far
            slot = 0
            for day in week:
                for entry in take_new(picks.get(day, ()), shown, 1):
                    card = self._suggestion_card(cells[day], entry, scale=scale)
                    await _yield_to_ui()
                    if not slot and card.winfo_exists():
                        card.update_idletasks()
                        slot = _packed_height(card)
            if not slot or not all(cells[day].winfo_exists() for day in week):
                continue
            cells[week[0]].update_idletasks()
            used = [cells[day].winfo_reqheight() for day in week]
            extras = extra_suggestions(used, max(used), slot, cap=SUGGESTION_CAP - 1)
            for day, count in zip(week, extras):
                for entry in take_new(picks.get(day, ()), shown, count):
                    if not cells[day].winfo_exists():
                        return
                    self._suggestion_card(cells[day], entry, scale=scale)
                    await _yield_to_ui()

    def _suggestion_card(
        self, cell: ctk.CTkFrame, entry: CalendarEntry, *, scale: CardScale
    ) -> SuggestionCard:
        card = SuggestionCard(
            cell,
            entry,
            library=self._library,
            status=self._status,
            profile_id=self._profile_id,
            scale=scale,
            on_open_show=self._on_open_show,
        )
        card.pack(fill="x", padx=CELL_PAD_X, pady=(0, CARD_GAP))
        return card


def _packed_height(widget: Any) -> int:
    """The height `widget` adds to its packed parent: its requested height plus its pack gaps
    above and below, in physical pixels."""
    pady = widget.pack_info().get("pady", 0)
    parts = pady if isinstance(pady, (tuple, list)) else str(pady).split()
    gaps = [int(part) for part in parts]
    if len(gaps) == 1:
        gaps *= 2
    return int(widget.winfo_reqheight()) + sum(gaps)
