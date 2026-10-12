"""Weekly episode calendar: tracked episodes from SQL, suggested shows on quiet days."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import date, timedelta
from typing import Any, Literal

import customtkinter as ctk

from app.models import CalendarEntry, Settings, Weekday
from app.services import ImageCache, LibraryService, SettingsService
from app.services.library import calendar_range
from app.ui import theme
from app.ui.calendar_cards import CompactEpisodeCard, EpisodeCard, SuggestionCard
from app.ui.tasks import spawn
from app.ui.views.base import BaseView, StatusReporter
from app.ui.widgets import destroy_tree

log = logging.getLogger(__name__)

CardStyle = Literal["full", "compact"]

PREV_WEEKS_CHOICES = [str(n) for n in range(5)]  # Settings.prev_weeks_to_show
TOTAL_WEEKS_CHOICES = [str(n) for n in range(1, 6)]  # Settings.weeks_to_show
MIN_DAY_TARGET = 3  # every day is filled up to at least this many cards with suggestions


def card_style(day: date, *, today: date, week_start: date) -> CardStyle:
    """Poster cards from the current week (starting `week_start`) on, compact cards before.

    The previous week keeps poster cards during the first two days of the current week.
    """
    if day >= week_start:
        return "full"
    if day >= week_start - timedelta(weeks=1) and (today - week_start).days < 2:
        return "full"
    return "compact"


def _day_target(counts: Mapping[date, int], days: Sequence[date]) -> int:
    return max(MIN_DAY_TARGET, max((counts.get(day, 0) for day in days), default=0))


def suggestion_quota(counts: Mapping[date, int], days: Sequence[date]) -> dict[date, int]:
    """Suggestions per day: enough to match the busiest day's tracked count (at least 3)."""
    target = _day_target(counts, days)
    return {day: max(0, target - counts.get(day, 0)) for day in days}


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


class CalendarView(BaseView):
    """Weeks of tracked episodes in a 7-column grid; quiet days get popular untracked shows."""

    def __init__(
        self,
        master: Any,
        *,
        library: LibraryService,
        settings: SettingsService,
        images: ImageCache,
        status: StatusReporter,
        on_search_episode: Callable[[CalendarEntry], None],
        **kwargs: Any,
    ) -> None:
        super().__init__(master, **kwargs)
        self._library = library
        self._settings = settings
        self._images = images
        self._status = status
        self._on_search_episode = on_search_episode
        self._weeks: ctk.CTkFrame | None = None  # the grid of day cells, rebuilt on every load
        self._rendered_for: date | None = None  # the "today" of the last load

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        toolbar = ctk.CTkFrame(self, fg_color="transparent")
        toolbar.grid(row=0, column=0, sticky="ew", padx=15, pady=5)
        toolbar.grid_columnconfigure(0, weight=1)
        self._note = ctk.CTkLabel(
            toolbar, text="", font=theme.font(12), text_color=theme.TEXT_MUTED
        )
        self._note.grid(row=0, column=0, sticky="w")
        menus = ctk.CTkFrame(toolbar, fg_color="transparent")
        menus.grid(row=0, column=1, sticky="e")
        self._prev_weeks = self._week_menu(
            menus, "Prev Weeks:", PREV_WEEKS_CHOICES, "prev_weeks_to_show", padx=(0, 15)
        )
        self._total_weeks = self._week_menu(
            menus, "Total Weeks:", TOTAL_WEEKS_CHOICES, "weeks_to_show", padx=0
        )
        self._sync_menus(settings.current)

        self._day_names = ctk.CTkFrame(self, fg_color="transparent")
        self._day_names.grid(row=1, column=0, sticky="ew", padx=15)
        for column in range(7):
            self._day_names.grid_columnconfigure(column, weight=1, uniform="day")

        self._scroll = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._scroll.grid(row=2, column=0, sticky="nsew", padx=15, pady=(0, 5))

    # -- toolbar -------------------------------------------------------------------------------

    def _week_menu(
        self, master: Any, label: str, values: list[str], field: str, *, padx: Any
    ) -> ctk.CTkOptionMenu:
        ctk.CTkLabel(master, text=label, text_color="gray70", font=theme.font(12)).pack(
            side="left", padx=(0, 5)
        )
        menu = ctk.CTkOptionMenu(
            master,
            values=values,
            width=60,
            height=28,
            fg_color=theme.SURFACE,
            button_color=theme.EDGE,
            command=lambda value: self._save_weeks(field, value),
        )
        menu.pack(side="left", padx=padx)
        return menu

    def _save_weeks(self, field: str, value: str) -> None:
        # No refresh here: the window's settings listener reloads the visible view.
        spawn(self._settings.update(**{field: int(value)}), name="CalendarView.save_weeks")

    def _sync_menus(self, prefs: Settings) -> None:
        """Show the stored values (the Settings dialog can change them too)."""
        self._prev_weeks.set(str(prefs.prev_weeks_to_show))
        self._total_weeks.set(str(prefs.weeks_to_show))

    # -- loading -------------------------------------------------------------------------------

    def is_stale(self) -> bool:
        # After midnight the grid shows the wrong today, range and card styles.
        return super().is_stale() or self._rendered_for != date.today()

    async def load(self) -> None:
        prefs = self._settings.current
        self._sync_menus(prefs)
        self._note.configure(text="")
        today = date.today()
        self._rendered_for = today
        start, end = calendar_range(
            today,
            first_day=prefs.first_day,
            weeks_before=prefs.prev_weeks_to_show,
            weeks_total=prefs.weeks_to_show,
        )
        week_start, _ = calendar_range(
            today, first_day=prefs.first_day, weeks_before=0, weeks_total=1
        )
        days = _days(start, end)

        entries = await self._library.calendar(start, end)
        if not self.winfo_exists():
            return
        by_day: dict[date, list[CalendarEntry]] = {}
        for entry in entries:
            if entry.episode.air_date is not None:
                by_day.setdefault(entry.episode.air_date, []).append(entry)
        self._render_day_names(prefs.first_day)
        cells = await self._render_tracked(days, by_day, today=today, week_start=week_start)

        counts = {day: len(day_entries) for day, day_entries in by_day.items()}
        quota = suggestion_quota(counts, days)
        if not any(quota.values()):
            return
        self._note.configure(text="Finding suggestions…")
        try:
            suggestions = await self._library.suggestions(
                start, end, per_day=_day_target(counts, days)
            )
        except Exception:
            log.exception("Couldn't load calendar suggestions for %s – %s", start, end)
            if self.winfo_exists():
                self._note.configure(text="Couldn't load suggestions")
            return
        if not self.winfo_exists():
            return
        await self._render_suggestions(cells, suggestions, quota)
        self._note.configure(text="")

    # -- rendering -----------------------------------------------------------------------------

    def _render_day_names(self, first_day: Weekday) -> None:
        for child in self._day_names.winfo_children():
            child.destroy()
        weekdays = list(Weekday)
        ordered = weekdays[first_day.weekday :] + weekdays[: first_day.weekday]
        for column, weekday in enumerate(ordered):
            ctk.CTkLabel(
                self._day_names,
                text=weekday.value,
                text_color="gray60",
                font=theme.font(bold=True),
            ).grid(row=0, column=column)

    async def _render_tracked(
        self,
        days: Sequence[date],
        by_day: Mapping[date, Sequence[CalendarEntry]],
        *,
        today: date,
        week_start: date,
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
                border_width=1,
                fg_color=theme.TODAY_BG if is_today else theme.CELL_BG,
                border_color=theme.TODAY_BORDER if is_today else theme.CELL_BORDER,
            )
            # `days` starts on the first weekday, so the column is the weekday's position.
            cell.grid(row=index // 7, column=index % 7, sticky="nsew", padx=3, pady=3)
            ctk.CTkLabel(
                cell,
                text=f"{day:%b %d}",
                font=theme.font(11, bold=True),
                text_color=theme.ACCENT if is_today else "gray60",
            ).pack(anchor="e", padx=4, pady=2)
            cells[day] = cell

            style = card_style(day, today=today, week_start=week_start)
            for entry in by_day.get(day, ()):
                card: ctk.CTkFrame
                if style == "full":
                    card = EpisodeCard(
                        cell, entry, images=self._images, on_search=self._on_search_episode
                    )
                else:
                    card = CompactEpisodeCard(cell, entry, on_search=self._on_search_episode)
                card.pack(fill="x", padx=6, pady=4)
            await _yield_to_ui()
        return cells

    async def _render_suggestions(
        self,
        cells: Mapping[date, ctk.CTkFrame],
        suggestions: Sequence[CalendarEntry],
        quota: Mapping[date, int],
    ) -> None:
        """Each day's first `quota[day]` suggestions (they arrive best first, grouped by day).

        Yields to the event loop after each day's cards.
        """
        by_day: dict[date, list[CalendarEntry]] = {}
        for entry in suggestions:
            if entry.episode.air_date is not None:
                by_day.setdefault(entry.episode.air_date, []).append(entry)
        for day, wanted in quota.items():
            picks = by_day.get(day, [])[:wanted]
            if not picks or day not in cells:
                continue
            for entry in picks:
                SuggestionCard(
                    cells[day], entry, library=self._library, status=self._status
                ).pack(fill="x", padx=6, pady=4)
            await _yield_to_ui()
