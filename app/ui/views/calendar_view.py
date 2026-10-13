"""Weekly episode calendar: tracked episodes from SQL, plus one suggested show per day."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import date, timedelta
from typing import Any, Literal

import customtkinter as ctk

from app.models import CalendarEntry, Settings, Weekday
from app.services import ImageCache, LibraryService, SettingsService
from app.services.library import calendar_range, week_start
from app.ui import theme
from app.ui.calendar_cards import (
    CompactEpisodeCard,
    EpisodeActions,
    EpisodeCard,
    SuggestionCard,
)
from app.ui.tasks import spawn
from app.ui.text_fit import SLACK
from app.ui.tooltip import add_tooltip
from app.ui.views.base import BaseView, StatusReporter
from app.ui.widgets import destroy_tree

log = logging.getLogger(__name__)

CardStyle = Literal["full", "compact"]


def week_choice(weeks: int) -> str:
    """The weeks menu's text for `weeks`: "1 week", "3 weeks"."""
    return f"{weeks} week" + ("" if weeks == 1 else "s")


WEEK_CHOICES = {week_choice(weeks): weeks for weeks in range(1, 6)}  # Settings.weeks_to_show

HEADER_GAP = 22  # below the header row, above the grid
NAV_HEIGHT = 36  # the ◀ / Today / ▶ buttons (and the weeks menu beside them)
NAV_RADIUS = 8
NAV_GAP = 6  # between the navigation buttons
RANGE_GAP = 10  # between the ▶ button and the date range
# Inside a day cell, around its header and cards. Horizontally only 8: at the default 1650 px
# window, cards keep room for their posters (EpisodeCard's _POSTER_MIN_CARD_WIDTH).
CELL_PAD_X = 8
CELL_PAD_Y = 12
CARD_GAP = 12  # between the cards of a day
TODAY_BORDER_WIDTH = 2
PILL_PAD_X = 10  # the "Today" pill's padding around its text
PILL_PAD_Y = 2

_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip
_WEEKDAYS = tuple(weekday.value[:3] for weekday in Weekday)  # "Mon" … "Sun", by date.weekday()


def _day_month(day: date) -> str:
    return f"{day.day} {_MONTHS[day.month - 1]}"


def range_label(start: date, end: date) -> str:
    """The visible dates: "12 – 25 October", "28 September – 11 October", or with the years
    when the range spans two: "29 December 2026 – 11 January 2027"."""
    if start.year != end.year:
        return f"{_day_month(start)} {start.year} – {_day_month(end)} {end.year}"
    if start.month != end.month:
        return f"{_day_month(start)} – {_day_month(end)}"
    return f"{start.day} – {_day_month(end)}"


def range_slot_width(measure: Callable[[str], int]) -> int:
    """Room for every label `range_label` gives a grid of 1–5 weeks: the widest, measured.

    The labels with years, of ranges that cross into a new year, are the widest; those ranges
    start in a year's last five weeks. A fixed slot keeps the buttons before it in place.
    """
    year_end = date(2026, 12, 31)
    labels = (
        range_label(start, start + timedelta(weeks=weeks, days=-1))
        for weeks in range(1, 6)
        # The last day is in the next year from 7 * weeks - 2 days before year_end on.
        for start in (year_end - timedelta(days=back) for back in range(7 * weeks - 1))
    )
    return max(measure(label) for label in labels) + SLACK


def day_label(day: date) -> str:
    """A day cell's header: "Mon 12", with the month on its first day: "Mon 1 Nov"."""
    text = f"{_WEEKDAYS[day.weekday()]} {day.day}"
    return f"{text} {_MONTHS[day.month - 1][:3]}" if day.day == 1 else text


def card_style(day: date, *, today: date, week_start: date) -> CardStyle:
    """Poster cards from the current week (starting `week_start`) on, compact cards before.

    The previous week keeps poster cards during the first two days of the current week.
    """
    if day >= week_start:
        return "full"
    if day >= week_start - timedelta(weeks=1) and (today - week_start).days < 2:
        return "full"
    return "compact"


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
        font=theme.font(14),
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
        ctk.CTkLabel(
            header,
            text="Today",
            font=pill_font,
            text_color=theme.TEXT,
            fg_color=theme.ACCENT,
            corner_radius=6,
            width=round(pill_font.measure("Today")) + 2 * PILL_PAD_X,
            height=height,
        ).pack(side="right")
    return header


class CalendarView(BaseView):
    """Weeks of tracked episodes in a 7-column grid, each day with one suggested show after
    them (unless suggestions are switched off); ◀ / Today / ▶ move through the weeks."""

    def __init__(
        self,
        master: Any,
        *,
        library: LibraryService,
        settings: SettingsService,
        images: ImageCache,
        status: StatusReporter,
        episode_actions: EpisodeActions,
        **kwargs: Any,
    ) -> None:
        super().__init__(master, **kwargs)
        self._library = library
        self._settings = settings
        self._images = images
        self._status = status
        self._episode_actions = episode_actions  # the cards' Download and ⋯ buttons
        self._weeks: ctk.CTkFrame | None = None  # the grid of day cells, rebuilt on every load
        self._rendered_for: date | None = None  # the "today" of the last load
        self._offset = 0  # weeks from the current one; this session only

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
        # Equal side columns keep the navigation in the middle.
        header.grid_columnconfigure((0, 2), weight=1, uniform="side")

        left = ctk.CTkFrame(header, fg_color="transparent")
        left.grid(row=0, column=0, sticky="w")
        self._suggestions_switch = ctk.CTkSwitch(
            left,
            text="Show suggestions",
            font=theme.font(14),
            text_color=theme.TEXT_MUTED,
            progress_color=theme.ACCENT,
            command=self._save_show_suggestions,
        )
        self._suggestions_switch.pack(side="left")
        self._note = ctk.CTkLabel(left, text="", font=theme.font(12), text_color=theme.TEXT_MUTED)
        self._note.pack(side="left", padx=(12, 0))

        nav = ctk.CTkFrame(header, fg_color="transparent")
        nav.grid(row=0, column=1)
        _nav_button(
            nav, "◀", NAV_HEIGHT, lambda: self._go_to(self._offset - 1), "Previous week"
        ).pack(side="left")
        _nav_button(nav, "Today", 72, lambda: self._go_to(0)).pack(side="left", padx=NAV_GAP)
        _nav_button(
            nav, "▶", NAV_HEIGHT, lambda: self._go_to(self._offset + 1), "Next week"
        ).pack(side="left")
        range_font = theme.font(16, bold=True)
        self._range = ctk.CTkLabel(
            nav,
            text="",
            font=range_font,
            text_color=theme.TEXT,
            width=range_slot_width(range_font.measure),  # fixed: the buttons never move
            anchor="w",
        )
        self._range.pack(side="left", padx=(RANGE_GAP, 0))

        self._weeks_menu = ctk.CTkOptionMenu(
            header,
            values=list(WEEK_CHOICES),
            width=110,
            height=NAV_HEIGHT,
            corner_radius=NAV_RADIUS,
            fg_color=theme.SURFACE,
            button_color=theme.EDGE,
            button_hover_color=theme.ACCENT_HOVER,
            command=self._save_weeks,
        )
        self._weeks_menu.grid(row=0, column=2, sticky="e")

    def _go_to(self, offset: int) -> None:
        if offset != self._offset:
            self._offset = offset
            self.refresh()

    # No refresh in the two savers: the window's settings listener reloads the visible view.

    def _save_weeks(self, choice: str) -> None:
        spawn(
            self._settings.update(weeks_to_show=WEEK_CHOICES[choice]),
            name="CalendarView.save_weeks",
        )

    def _save_show_suggestions(self) -> None:
        shown = bool(self._suggestions_switch.get())
        spawn(
            self._settings.update(show_suggestions=shown),
            name="CalendarView.save_show_suggestions",
        )

    def _sync_controls(self, prefs: Settings) -> None:
        """Show the stored values (the window may reload with settings saved elsewhere)."""
        self._weeks_menu.set(week_choice(prefs.weeks_to_show))
        if prefs.show_suggestions:
            self._suggestions_switch.select()
        else:
            self._suggestions_switch.deselect()

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
        start, end = calendar_range(
            today, first_day=prefs.first_day, weeks=prefs.weeks_to_show, offset=self._offset
        )
        self._range.configure(text=range_label(start, end))

        entries = await self._library.calendar(start, end)
        if not self.winfo_exists():
            return
        cells = await self._render_tracked(
            _days(start, end),
            by_day(entries),
            today=today,
            this_week=week_start(today, prefs.first_day),
        )

        if not prefs.show_suggestions:
            return
        self._note.configure(text="Finding suggestions…")
        try:
            suggestions = await self._library.suggestions(start, end, per_day=1)
        except Exception:
            log.exception("Couldn't load calendar suggestions for %s – %s", start, end)
            if self.winfo_exists():
                self._note.configure(text="Couldn't load suggestions")
            return
        if not self.winfo_exists():
            return
        await self._render_suggestions(cells, best_suggestions(suggestions))
        self._note.configure(text="")

    # -- rendering -----------------------------------------------------------------------------

    async def _render_tracked(
        self,
        days: Sequence[date],
        tracked: Mapping[date, Sequence[CalendarEntry]],
        *,
        today: date,
        this_week: date,
    ) -> dict[date, ctk.CTkFrame]:
        """Replace the grid: one cell per day with its tracked episodes. Returns the cells.

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

            style = card_style(day, today=today, week_start=this_week)
            for entry in tracked.get(day, ()):
                card: ctk.CTkFrame
                if style == "full":
                    card = EpisodeCard(
                        cell, entry, images=self._images, actions=self._episode_actions
                    )
                else:
                    card = CompactEpisodeCard(cell, entry, actions=self._episode_actions)
                card.pack(fill="x", padx=CELL_PAD_X, pady=(0, CARD_GAP))
            await _yield_to_ui()
        return cells

    async def _render_suggestions(
        self, cells: Mapping[date, ctk.CTkFrame], picks: Mapping[date, CalendarEntry]
    ) -> None:
        """Each day's suggestion, after its tracked episodes. Yields to the event loop after
        each card."""
        for day, entry in picks.items():
            cell = cells.get(day)
            if cell is None:
                continue
            SuggestionCard(cell, entry, library=self._library, status=self._status).pack(
                fill="x", padx=CELL_PAD_X, pady=(0, CARD_GAP)
            )
            await _yield_to_ui()
