"""The calendar page on a hidden Tk root with fake services: what each day cell holds, the
top bar (week count, Suggestions toggle, wrapping) and the week navigation."""

from __future__ import annotations

import asyncio
import tkinter as tk
import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import customtkinter as ctk

from app.models import CalendarEntry, Episode, Settings, Show
from app.services import ImageCache, LibraryService, SettingsService
from app.services.library import calendar_range
from app.ui import theme
from app.ui.calendar_cards import CompactEpisodeCard, EpisodeCard, SuggestionCard
from app.ui.download_control import DownloadActions, DownloadStates
from app.ui.text_fit import SLACK
from app.ui.views.calendar_view import (
    RANGE_FONT_SIZE,
    CalendarView,
    range_label,
    range_slot_width,
)

TODAY = date(2026, 10, 19)  # a Monday: the grid starts a week earlier (Settings: Monday first)
GRID_START = TODAY - timedelta(weeks=1)
SUNDAY = TODAY - timedelta(days=1)


class FixedDate(date):
    @classmethod
    def today(cls) -> FixedDate:
        return cls(TODAY.year, TODAY.month, TODAY.day)


def entry(show_id: int, day: date, *, tracked: bool) -> CalendarEntry:
    return CalendarEntry(
        show=Show(id=show_id, name=f"Show {show_id}"),
        episode=Episode(id=show_id * 100, show_id=show_id, season=1, number=1, air_date=day),
        is_tracked=tracked,
    )


def _within(entries: list[CalendarEntry], start: date, end: date) -> list[CalendarEntry]:
    return [e for e in entries if e.episode.air_date and start <= e.episode.air_date <= end]


class FakeLibrary:
    def __init__(self, tracked: list[CalendarEntry], suggestions: list[CalendarEntry]) -> None:
        self._tracked = tracked
        self._suggestions = suggestions
        self.calendar_calls: list[tuple[date, date]] = []
        self.suggestion_calls: list[tuple[date, date, int]] = []

    async def calendar(self, start: date, end: date) -> list[CalendarEntry]:
        self.calendar_calls.append((start, end))
        return _within(self._tracked, start, end)

    async def suggestions(self, start: date, end: date, *, per_day: int) -> list[CalendarEntry]:
        self.suggestion_calls.append((start, end, per_day))
        return _within(self._suggestions, start, end)


class FakeSettings:
    def __init__(self, settings: Settings) -> None:
        self.current = settings
        self.updates: list[dict[str, Any]] = []

    async def update(self, **changes: Any) -> Settings:
        self.updates.append(changes)
        self.current = Settings.model_validate({**self.current.model_dump(), **changes})
        return self.current


def _no_status(text: str, *, clear_after_ms: int | None = None) -> None:
    pass


def _descendants(widget: Any) -> list[Any]:
    found: list[Any] = []
    stack = list(widget.winfo_children())
    while stack:
        child = stack.pop()
        found.append(child)
        stack.extend(child.winfo_children())
    return found


class CalendarViewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        patcher = mock.patch("app.ui.views.calendar_view.date", FixedDate)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def asyncSetUp(self) -> None:
        # The test loop runs in debug mode, which logs every step over 0.1 s; building a page
        # of Tk widgets between two awaits may take about that long.
        asyncio.get_running_loop().slow_callback_duration = 1.0

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()

    def library(self) -> FakeLibrary:
        tracked = [
            entry(1, TODAY, tracked=True),
            entry(2, TODAY, tracked=True),
            entry(3, GRID_START, tracked=True),  # a week ago: compact
        ]
        suggestions = [
            entry(10, TODAY, tracked=False),  # today's best-ranked
            entry(11, TODAY, tracked=False),
            entry(12, SUNDAY, tracked=False),
        ]
        return FakeLibrary(tracked, suggestions)

    def view(self, library: FakeLibrary, settings: FakeSettings) -> CalendarView:
        return CalendarView(
            self.root,
            library=cast(LibraryService, library),
            settings=cast(SettingsService, settings),
            images=cast(ImageCache, object()),  # never used: the shows have no poster
            status=_no_status,
            episode_actions=DownloadActions(
                download=lambda _entry: None, choose=lambda _entry: None, states=DownloadStates()
            ),
        )

    def cards(self, view: CalendarView, day: date) -> list[str]:
        """The cards in `day`'s cell, in order: "full", "compact" or the suggested show's
        name."""
        assert view._weeks is not None
        cells: dict[tuple[int, int], Any] = {}
        for cell in view._weeks.grid_slaves():
            info = cell.grid_info()
            cells[int(info["row"]), int(info["column"])] = cell
        index = (day - GRID_START).days
        header, *cards = cells[index // 7, index % 7].pack_slaves()
        names: list[str] = []
        for card in cards:
            if isinstance(card, SuggestionCard):
                names.append(cast(Any, card)._show.name)
            elif isinstance(card, CompactEpisodeCard):
                names.append("compact")
            else:
                self.assertIsInstance(card, EpisodeCard)
                names.append("full")
        return names

    async def test_tracked_episodes_first_then_one_suggestion(self) -> None:
        library = self.library()
        view = self.view(library, FakeSettings(Settings(weeks_to_show=1)))
        await view.load()
        # Monday: last week too, so the weekend's episodes are still on screen.
        week_end = TODAY + timedelta(days=6)
        self.assertEqual(library.calendar_calls, [(GRID_START, week_end)])
        self.assertEqual(library.suggestion_calls, [(GRID_START, week_end, 1)])
        self.assertEqual(self.cards(view, TODAY), ["full", "full", "Show 10"])
        self.assertEqual(self.cards(view, SUNDAY), ["Show 12"])
        self.assertEqual(self.cards(view, GRID_START), ["compact"])
        self.assertEqual(self.cards(view, TODAY + timedelta(days=1)), [])
        self.assertTrue(view._suggestions_shown)
        self.assertEqual(view._range.cget("text"), "12 – 25 Oct")

    async def test_switched_off_suggestions_are_not_fetched(self) -> None:
        library = self.library()
        prefs = Settings(weeks_to_show=1, show_suggestions=False)
        view = self.view(library, FakeSettings(prefs))
        await view.load()
        self.assertEqual(library.suggestion_calls, [])
        self.assertEqual(self.cards(view, TODAY), ["full", "full"])
        self.assertEqual(self.cards(view, SUNDAY), [])
        self.assertFalse(view._suggestions_shown)
        self.assertEqual(view._suggestions_toggle.cget("fg_color"), "transparent")

    async def test_header_controls_save_the_settings(self) -> None:
        settings = FakeSettings(Settings())
        view = self.view(self.library(), settings)
        self.assertEqual(view._week_count.get(), "3")
        self.assertEqual(view._week_count.cget("values"), ["1", "2", "3", "4", "5"])
        self.assertEqual(view._suggestions_toggle.cget("fg_color"), theme.ACCENT)
        view._suggestions_toggle.invoke()  # as a click does
        self.assertEqual(view._suggestions_toggle.cget("fg_color"), "transparent")
        view._week_count.cget("command")("1")  # as clicking "1" does
        await asyncio.sleep(0)
        self.assertEqual(settings.updates, [{"show_suggestions": False}, {"weeks_to_show": 1}])
        view._suggestions_toggle.invoke()
        await asyncio.sleep(0)
        self.assertEqual(settings.updates[-1], {"show_suggestions": True})
        self.assertEqual(view._suggestions_toggle.cget("fg_color"), theme.ACCENT)

    async def test_reload_shows_settings_saved_elsewhere(self) -> None:
        settings = FakeSettings(Settings())
        view = self.view(self.library(), settings)
        settings.current = Settings(weeks_to_show=5, show_suggestions=False)
        await view.load()
        self.assertEqual(view._week_count.get(), "5")
        self.assertFalse(view._suggestions_shown)

    async def test_right_hand_controls_wrap_when_narrow(self) -> None:
        view = self.view(self.library(), FakeSettings(Settings()))
        self.root.update_idletasks()
        self.assertEqual(int(view._right.grid_info()["row"]), 0)
        view._fit_header(SimpleNamespace(width=200))
        self.assertEqual(int(view._right.grid_info()["row"]), 1)
        view._fit_header(SimpleNamespace(width=20000))
        info = view._right.grid_info()
        self.assertEqual((int(info["row"]), int(info["column"])), (0, 2))

    def test_range_slot_is_the_widest_label_within_a_year(self) -> None:
        font = theme.font(RANGE_FONT_SIZE, bold=True)
        first = date(2027, 1, 1)
        widest = max(
            font.measure(range_label(start, end))
            for start in (first + timedelta(days=n) for n in range(365))
            for weeks in range(1, 7)  # 1–5 weeks, and one more early in a week
            if (end := start + timedelta(weeks=weeks, days=-1)).year == start.year
        )
        self.assertEqual(range_slot_width(font.measure), widest + SLACK)
        # Close to the usual labels: the buttons sit right after the dates.
        self.assertLess(range_slot_width(font.measure) - font.measure("12 Oct – 8 Nov"), 30)

    async def test_range_label_keeps_its_width(self) -> None:
        library = self.library()
        view = self.view(library, FakeSettings(Settings(weeks_to_show=1, show_suggestions=False)))
        widths = set()
        for start, end in (
            (date(2026, 10, 12), date(2026, 10, 18)),
            (date(2026, 10, 19), date(2026, 11, 8)),
            (date(2027, 3, 31), date(2027, 5, 11)),
        ):
            view._range.configure(text=range_label(start, end))
            self.root.update_idletasks()
            widths.add(view._range.winfo_reqwidth())
        self.assertEqual(len(widths), 1, widths)
        # A label with years, around New Year, may push the buttons.
        view._range.configure(text=range_label(date(2026, 12, 28), date(2027, 1, 31)))
        self.root.update_idletasks()
        self.assertGreater(view._range.winfo_reqwidth(), widths.pop())

    async def test_week_navigation(self) -> None:
        library = self.library()
        prefs = Settings(weeks_to_show=2, show_suggestions=False)
        view = self.view(library, FakeSettings(prefs))
        buttons = {
            str(widget.cget("text")): widget
            for widget in _descendants(view)
            if isinstance(widget, ctk.CTkButton)
        }

        async def press(text: str) -> tuple[date, date]:
            buttons[text].invoke()
            if view._load_task is not None:
                await view._load_task
            return library.calendar_calls[-1]

        def weeks_from_now(offset: int) -> tuple[date, date]:
            return calendar_range(TODAY, first_day=prefs.first_day, weeks=2, offset=offset)

        await view.load()
        self.assertEqual(library.calendar_calls, [weeks_from_now(0)])
        self.assertEqual(weeks_from_now(0), (GRID_START, TODAY + timedelta(days=13)))
        self.assertEqual(view._range.cget("text"), range_label(*weeks_from_now(0)))
        # Away from today, whole weeks without the extra one.
        next_weeks = (TODAY + timedelta(weeks=1), TODAY + timedelta(days=20))
        self.assertEqual(await press("▶"), next_weeks)
        self.assertEqual(view._range.cget("text"), range_label(*weeks_from_now(1)))
        self.assertEqual(await press("◀"), weeks_from_now(0))
        self.assertEqual(await press("◀"), weeks_from_now(-1))
        self.assertEqual(await press("Today"), weeks_from_now(0))
        calls = len(library.calendar_calls)
        buttons["Today"].invoke()  # already on this week: no reload
        self.assertEqual(len(library.calendar_calls), calls)


if __name__ == "__main__":
    unittest.main()
