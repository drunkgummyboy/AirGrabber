"""Weekly episode calendar: tracked episodes from SQL, plus one suggested show per day.

Missing episodes (`LibraryService.missing_between`: aired, not downloaded, not marked "I
already have this") get the cards' amber mark. Several episodes of one show on one day share a
stack card (app/ui/calendar_stack.py: `stack_entries`); the page remembers which stacks are
unfolded for the session, so a reload (a sync, a settings change, a grab) keeps them open.

The top bar, in one line: the visible dates, ◀ Today ▶, and on the right the number of weeks
(1–5) and the 💡 Suggestions toggle; on a window too narrow for one line the right-hand
controls move to a second line. The cards follow Settings → Card size (`card_scale`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from datetime import date, timedelta
from typing import Any, Literal

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

HEADER_GAP = 22  # below the header row, above the grid
RANGE_FONT_SIZE = 20  # the visible dates, bold
NAV_HEIGHT = 28  # the ◀ / Today / ▶ buttons, the week count and the Suggestions toggle
NAV_FONT_SIZE = 13
NAV_RADIUS = 7
NAV_GAP = 4  # between the navigation buttons
TODAY_WIDTH = 60
RANGE_GAP = 12  # between the date range and the ◀ button
CONTROL_GAP = 10  # between the week count and the Suggestions toggle
WEEK_BUTTON_WIDTH = 30  # each of the week count's 1–5
TOGGLE_PAD_X = 14  # the Suggestions toggle's room left and right of its text
HEADER_MIN_GAP = 24  # least room between the left and right groups on one line
WRAP_GAP = 10  # above the right-hand controls when they wrap to a second line
# Inside a day cell, around its header and cards. Horizontally only 8: the poster cards keep
# room for their text and Download row at the narrower windows.
CELL_PAD_X = 8
CELL_PAD_Y = 12
CARD_GAP = 12  # between the cards of a day
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


def header_wraps(width: float, left: float, right: float, gap: float) -> bool:
    """Whether the top bar's right-hand controls (`right` wide) go on a second line: when they
    and the left group (`left` wide) don't fit in `width` with `gap` between them."""
    return left + gap + right > width


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


def best_suggestions(suggestions: Iterable[CalendarEntry]) -> dict[date, CalendarEntry]:
    """Each day's one suggestion: its best-ranked, i.e. the day's first (they arrive best first
    within each day)."""
    return {day: entries[0] for day, entries in by_day(suggestions).items()}


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


def _nav_button(
    master: Any, text: str, width: int, command: Callable[[], None], tip: str = ""
) -> ctk.CTkButton:
    """A secondary button of the header's week navigation (`tip`: its hover tip)."""
    button = ctk.CTkButton(
        master,
        text=text,
        width=width,
        height=NAV_HEIGHT,
        corner_radius=NAV_RADIUS,
        fg_color="transparent",
        border_width=1,
        border_color=theme.EDGE,
        hover_color=theme.SURFACE,
        text_color=theme.TEXT,
        font=theme.font(NAV_FONT_SIZE),
        command=command,
    )
    if tip:
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
            text_color=theme.TEXT,
            fg_color=theme.ACCENT,
            corner_radius=6,
            width=round(pill_font.measure(today)) + 2 * PILL_PAD_X,
            height=height,
        ).pack(side="right")
    return header


class CalendarView(BaseView):
    """Weeks of tracked episodes in a 7-column grid, each day with one suggested show after
    them (unless suggestions are switched off); ◀ / Today / ▶ move through the weeks.

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
        **kwargs: Any,
    ) -> None:
        super().__init__(master, **kwargs)
        self._library = library
        self._settings = settings
        self._images = images
        self._status = status
        self._episode_actions = episode_actions  # the cards' Download and ⋯ buttons
        self._card_scale = card_scale
        self._on_open_show = on_open_show
        self._weeks: ctk.CTkFrame | None = None  # the grid of day cells, rebuilt on every load
        self._rendered_for: date | None = None  # the "today" of the last load
        self._offset = 0  # weeks from the current one; this session only
        self._unfolded: set[FoldKey] = set()  # the stack cards unfolded; this session only
        self._suggestions_shown = settings.current.show_suggestions  # the toggle's state

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self._build_header()
        self._sync_controls(settings.current)

        self._scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._scroll.grid(row=1, column=0, sticky="nsew", padx=15, pady=(0, 5))

    # -- header --------------------------------------------------------------------------------

    def _build_header(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=15, pady=(5, HEADER_GAP))
        header.grid_columnconfigure(1, weight=1)
        self._header = header

        # Left: the dates in a slot of fixed width (the buttons after it never move), then
        # ◀ Today ▶; the "Finding suggestions…" note follows in the stretching middle column.
        left = ctk.CTkFrame(header, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w")
        range_font = theme.font(RANGE_FONT_SIZE, bold=True)
        self._range = ctk.CTkLabel(
            left,
            text="",
            font=range_font,
            text_color=theme.TEXT,
            width=range_slot_width(range_font.measure),
            anchor="w",
        )
        self._range.pack(side="left")
        _nav_button(
            left, "◀", NAV_HEIGHT, lambda: self._go_to(self._offset - 1), t("Previous week")
        ).pack(side="left", padx=(RANGE_GAP, 0))
        _nav_button(left, t("Today"), TODAY_WIDTH, lambda: self._go_to(0)).pack(
            side="left", padx=NAV_GAP
        )
        _nav_button(
            left, "▶", NAV_HEIGHT, lambda: self._go_to(self._offset + 1), t("Next week")
        ).pack(side="left")
        self._left = left
        self._note = ctk.CTkLabel(
            header, text="", font=theme.font(12), text_color=theme.TEXT_MUTED, anchor="w"
        )
        self._note.grid(row=0, column=1, sticky="w", padx=(16, 0))

        # Right: the week count, then the Suggestions toggle.
        right = ctk.CTkFrame(header, fg_color="transparent")
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
        self._wrapped: bool | None = None
        self._place_right(wrapped=False)
        header.bind("<Configure>", self._fit_header, add="+")

    def _fit_header(self, event: Any) -> None:
        """One line while everything fits, else the right-hand controls on a second line."""
        if event.width <= 1:
            return
        gap = round(HEADER_MIN_GAP * ctk.ScalingTracker.get_widget_scaling(self))
        wraps = header_wraps(
            event.width, self._left.winfo_reqwidth(), self._right.winfo_reqwidth(), gap
        )
        if wraps != self._wrapped:
            self._place_right(wrapped=wraps)

    def _place_right(self, *, wrapped: bool) -> None:
        self._wrapped = wrapped
        if wrapped:
            self._right.grid(row=1, column=0, columnspan=3, sticky="w", pady=(WRAP_GAP, 0))
        else:
            self._right.grid(row=0, column=2, columnspan=1, sticky="e", pady=0)

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
        prefs = self._settings.current
        self._sync_controls(prefs)
        self._note.configure(text="")
        today = date.today()
        self._rendered_for = today
        scale = self._card_scale()
        start, end = calendar_range(
            today, first_day=prefs.first_day, weeks=prefs.weeks_to_show, offset=self._offset
        )
        self._range.configure(text=range_label(start, end))

        entries = await self._library.calendar(start, end)
        missing = await self._library.missing_between(start, end)
        had = await self._library.have_between(start, end)
        if not self.winfo_exists():
            return
        cells = await self._render_tracked(
            _days(start, end),
            by_day(entries),
            today=today,
            scale=scale,
            missing=missing,
            had=had,
        )

        if not prefs.show_suggestions:
            return
        self._note.configure(text=t("Finding suggestions…"))
        try:
            suggestions = await self._library.suggestions(start, end, per_day=1)
        except Exception:
            log.exception("Couldn't load calendar suggestions for %s – %s", start, end)
            if self.winfo_exists():
                self._note.configure(text=t("Couldn't load suggestions"))
            return
        if not self.winfo_exists():
            return
        await self._render_suggestions(cells, best_suggestions(suggestions), scale=scale)
        self._note.configure(text="")

    # -- rendering -----------------------------------------------------------------------------

    async def _render_tracked(
        self,
        days: Sequence[date],
        tracked: Mapping[date, Sequence[CalendarEntry]],
        *,
        today: date,
        scale: CardScale,
        missing: Collection[int] = frozenset(),
        had: Collection[int] = frozenset(),
    ) -> dict[date, ctk.CTkFrame]:
        """Replace the grid: one cell per day with its tracked episodes, a show's episodes of
        one day on one stack card (`missing`: the ids of the episodes that get the amber mark;
        `had`: those marked "I already have this"). Returns the cells.

        Yields to the event loop after each day: a whole week of poster cards takes Tk ~250 ms
        to draw. Cancelled at a yield, it leaves a partial grid in `self._weeks` for the next
        load to destroy.
        """
        if self._weeks is not None:
            destroy_tree(self._weeks)
        weeks = ctk.CTkFrame(self._scroll, fg_color="transparent")
        weeks.pack(fill="both", expand=True)
        for column in range(7):
            weeks.grid_columnconfigure(column, weight=1, uniform="day")
        self._weeks = weeks

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
                    cell, group, style=style, scale=scale, missing=missing, had=had
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
        missing: Collection[int],
        had: Collection[int],
    ) -> ctk.CTkFrame:
        """The card of one show's episodes of the day: a single episode's card, or a stack
        card for more."""
        marked = any(entry.episode.id in missing for entry in group)
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
                )
            return CompactEpisodeCard(
                cell,
                entry,
                actions=self._episode_actions,
                scale=scale,
                on_open_show=self._on_open_show,
                missing=marked,
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
                missing=marked,
                had=had,
                unfolded=key in self._unfolded,
                on_fold=lambda unfolded: self._remember_fold(key, unfolded),
            )
        return CompactStackCard(
            cell,
            stack,
            actions=self._episode_actions,
            scale=scale,
            on_open_show=self._on_open_show,
            missing=marked,
            had=had,
        )

    def _remember_fold(self, key: FoldKey, unfolded: bool) -> None:
        if unfolded:
            self._unfolded.add(key)
        else:
            self._unfolded.discard(key)

    async def _render_suggestions(
        self,
        cells: Mapping[date, ctk.CTkFrame],
        picks: Mapping[date, CalendarEntry],
        *,
        scale: CardScale,
    ) -> None:
        """Each day's suggestion, after its tracked episodes. Yields to the event loop after
        each card."""
        for day, entry in picks.items():
            cell = cells.get(day)
            if cell is None:
                continue
            SuggestionCard(
                cell,
                entry,
                library=self._library,
                status=self._status,
                scale=scale,
                on_open_show=self._on_open_show,
            ).pack(fill="x", padx=CELL_PAD_X, pady=(0, CARD_GAP))
            await _yield_to_ui()
