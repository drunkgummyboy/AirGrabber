"""The calendar page on a hidden Tk root with fake services: what each day cell holds (the
amber marks of missing episodes, the suggestions' show windows), the top bar (week count,
Suggestions toggle, wrapping) and the week navigation."""

from __future__ import annotations

import asyncio
import tkinter as tk
import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from typing import Any, cast
from unittest import mock

import customtkinter as ctk

from app.models import CalendarEntry, Episode, LibraryStats, Settings, Show
from app.services import ImageCache, LibraryService, SettingsService
from app.services.library import calendar_range
from app.services.tvmaze import TVMazeError
from app.ui import theme
from app.ui.calendar_cards import CompactEpisodeCard, EpisodeCard, SuggestionCard
from app.ui.calendar_stack import CompactStackCard, StackCard
from app.ui.download_control import DownloadActions, DownloadStates
from app.ui.text_fit import SLACK
from app.ui.views.calendar_view import (
    RANGE_FONT_SIZE,
    SUGGESTION_CAP,
    SUGGESTION_POOL,
    CalendarView,
    range_label,
    kept_scroll,
    range_slot_width,
    suggestion_start,
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
    """LibraryService's calendar calls; what you have and miss only while `plex_connected`
    (3.1.0: the real service answers "nothing" without Plex)."""

    def __init__(
        self,
        tracked: list[CalendarEntry],
        suggestions: list[CalendarEntry],
        missing: frozenset[int] = frozenset(),
        in_plex: frozenset[int] = frozenset(),
        new: frozenset[int] = frozenset(),
        *,
        plex: bool = True,
    ) -> None:
        self._tracked = tracked
        self._suggestions = suggestions
        self._missing = missing
        self._in_plex = in_plex
        self._new = new
        self.plex_connected = plex
        self.calendar_calls: list[tuple[date, date]] = []
        self.missing_calls: list[tuple[date, date]] = []
        self.plex_calls: list[tuple[date, date]] = []
        self.suggestion_calls: list[tuple[date, date, int]] = []
        self.people: list[int] = []  # the person each calendar was asked for
        self.gate: asyncio.Event | None = None
        self.reached = asyncio.Event()
        self.tracked_shows: list[int] = []
        self.track_error: Exception | None = None
        self.follows: dict[int, int] = {}  # how many shows each person follows (default 3)
        self.stats_people: list[int] = []

    async def calendar(self, start: date, end: date, *, profile_id: int) -> list[CalendarEntry]:
        self.calendar_calls.append((start, end))
        self.people.append(profile_id)
        return _within(self._tracked, start, end)

    async def missing_between(
        self, start: date, end: date, *, profile_id: int
    ) -> frozenset[int]:
        self.missing_calls.append((start, end))
        return self._missing if self.plex_connected else frozenset()

    async def new_between(self, start: date, end: date, *, profile_id: int) -> frozenset[int]:
        return self._new if self.plex_connected else frozenset()

    async def plex_between(self, start: date, end: date) -> frozenset[int]:
        self.plex_calls.append((start, end))
        return self._in_plex if self.plex_connected else frozenset()

    async def suggestions(
        self, start: date, end: date, *, profile_id: int, per_day: int
    ) -> list[CalendarEntry]:
        self.suggestion_calls.append((start, end, per_day))
        if self.gate is not None:  # a test looks at the page halfway through a load
            self.reached.set()
            await self.gate.wait()
        return _within(self._suggestions, start, end)

    async def stats(self, *, profile_id: int) -> LibraryStats:
        self.stats_people.append(profile_id)
        return LibraryStats(tracked_shows=self.follows.get(profile_id, 3), episodes=0)

    async def track(self, show_id: int, *, profile_id: int) -> Show:
        self.tracked_shows.append(show_id)
        if self.track_error is not None:
            raise self.track_error
        return Show(id=show_id, name=f"Show {show_id}")


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

    def library(self, missing: frozenset[int] = frozenset()) -> FakeLibrary:
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
        return FakeLibrary(tracked, suggestions, missing)

    def view(
        self,
        library: FakeLibrary,
        settings: FakeSettings,
        on_open_show: Any = None,
        on_pick_shows: Any = None,
    ) -> CalendarView:
        return CalendarView(
            self.root,
            library=cast(LibraryService, library),
            settings=cast(SettingsService, settings),
            images=cast(ImageCache, object()),  # never used: the shows have no poster
            status=_no_status,
            episode_actions=DownloadActions(
                download=lambda _entry: None, choose=lambda _entry: None, states=DownloadStates()
            ),
            on_open_show=on_open_show,
            on_pick_shows=on_pick_shows,
        )

    def card_widgets(self, view: CalendarView, day: date) -> list[Any]:
        """The cards in `day`'s cell, in order."""
        assert view._weeks is not None
        index = (day - GRID_START).days
        for cell in view._weeks.grid_slaves():
            info = cell.grid_info()
            if (int(info["row"]), int(info["column"])) == (index // 7, index % 7):
                header, *cards = cell.pack_slaves()
                return list[Any](cards)
        raise AssertionError(f"no cell for {day}")

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
        # Monday: last week too, so the weekend's episodes are still on screen; it has passed,
        # so its suggestions aren't fetched.
        week_end = TODAY + timedelta(days=6)
        self.assertEqual(library.calendar_calls, [(GRID_START, week_end)])
        self.assertEqual(library.suggestion_calls, [(TODAY, week_end, SUGGESTION_POOL)])
        self.assertEqual(self.cards(view, TODAY), ["full", "full", "Show 10"])
        self.assertEqual(self.cards(view, SUNDAY), [])
        self.assertEqual(self.cards(view, GRID_START), ["compact"])
        self.assertEqual(self.cards(view, TODAY + timedelta(days=1)), [])
        self.assertTrue(view._suggestions_shown)
        self.assertEqual(view._range.cget("text"), "12 – 25 Oct")

    async def test_quiet_days_get_more_suggestions_without_a_taller_grid(self) -> None:
        # Today holds four poster cards; its week's other days only suggestions, many each.
        tracked = [entry(show_id, TODAY, tracked=True) for show_id in range(1, 5)]
        quiet = [TODAY + timedelta(days=n) for n in range(1, 7)]
        suggestions = [
            entry(day.day * 100 + n, day, tracked=False) for day in quiet for n in range(12)
        ] + [entry(90 + n, TODAY, tracked=False) for n in range(3)]
        prefs = Settings(weeks_to_show=1)

        async def grid_height(cap: int) -> tuple[int, CalendarView]:
            view = self.view(FakeLibrary(tracked, suggestions), FakeSettings(prefs))
            with mock.patch("app.ui.views.calendar_view.SUGGESTION_CAP", cap):
                await view.load()
            assert view._weeks is not None
            view._weeks.update_idletasks()
            return view._weeks.winfo_reqheight(), view

        single_height, single = await grid_height(1)
        for day in quiet:
            self.assertEqual(len(self.cards(single, day)), 1)
        filled_height, filled = await grid_height(SUGGESTION_CAP)
        self.assertEqual(filled_height, single_height)
        # The busy day keeps its one suggestion; the quiet days get more, best-ranked first.
        self.assertEqual(self.cards(filled, TODAY), ["full"] * 4 + ["Show 90"])
        for day in quiet:
            names = self.cards(filled, day)
            self.assertGreater(len(names), 1)
            self.assertLessEqual(len(names), SUGGESTION_CAP)
            self.assertEqual(names, [f"Show {day.day * 100 + n}" for n in range(len(names))])
        # Last week's row has no suggestions: nothing to fill.
        self.assertEqual(self.cards(filled, GRID_START), [])

    async def test_a_show_is_suggested_once_a_week_row(self) -> None:
        # Today holds four poster cards. A daily show (50) is the best-ranked suggestion of
        # every weekday, each day has one more of its own, and next week's Monday has 50 too.
        tracked = [entry(show_id, TODAY, tracked=True) for show_id in range(1, 5)]
        weekdays = [TODAY + timedelta(days=n) for n in range(5)]
        next_monday = TODAY + timedelta(weeks=1)
        suggestions = [
            suggestion
            for n, day in enumerate(weekdays)
            for suggestion in (entry(50, day, tracked=False), entry(60 + n, day, tracked=False))
        ] + [entry(50, next_monday, tracked=False)]
        view = self.view(FakeLibrary(tracked, suggestions), FakeSettings(Settings(weeks_to_show=2)))
        await view.load()
        self.assertEqual(self.cards(view, TODAY), ["full"] * 4 + ["Show 50"])
        for n, day in enumerate(weekdays[1:], start=1):
            self.assertEqual(self.cards(view, day), [f"Show {60 + n}"])
        self.assertEqual(self.cards(view, next_monday), ["Show 50"])  # another week row

    async def test_past_weeks_get_no_suggestions(self) -> None:
        # Today is a Wednesday: Monday and Tuesday keep theirs (the row doesn't change midweek).
        wednesday = TODAY + timedelta(days=2)

        class Wednesday(date):
            @classmethod
            def today(cls) -> Wednesday:
                return cls(wednesday.year, wednesday.month, wednesday.day)

        tracked = [entry(1, GRID_START, tracked=True)]
        suggestions = [
            entry(10 + n, GRID_START + timedelta(days=n), tracked=False) for n in range(21)
        ]
        library = FakeLibrary(tracked, suggestions)
        prefs = Settings(weeks_to_show=2)
        with mock.patch("app.ui.views.calendar_view.date", Wednesday):
            view = self.view(library, FakeSettings(prefs))
            await view.load()
            self.assertEqual(library.suggestion_calls[-1][:2], (TODAY, TODAY + timedelta(days=13)))
            self.assertEqual(self.cards(view, TODAY), ["Show 17"])  # Monday, before today
            self.assertEqual(self.cards(view, wednesday), ["Show 19"])
            # Back a week: last week's row has passed, this week's still gets them.
            view._offset = -1
            await view.load()
            self.assertEqual(library.calendar_calls[-1], (GRID_START, TODAY + timedelta(days=6)))
            self.assertEqual(library.suggestion_calls[-1][:2], (TODAY, TODAY + timedelta(days=6)))
            self.assertEqual(self.cards(view, GRID_START), ["compact"])
            self.assertEqual(self.cards(view, GRID_START + timedelta(days=1)), [])
            # Back two: every row has passed, nothing is fetched.
            calls = len(library.suggestion_calls)
            view._offset = -2
            await view.load()
            self.assertEqual(len(library.suggestion_calls), calls)
            self.assertEqual(view._note.cget("text"), "")

    async def test_a_quiet_reload_swaps_in_a_complete_grid(self) -> None:
        # Show 10 was suggested today and has just been tracked (a library change).
        library = self.library()
        view = self.view(library, FakeSettings(Settings(weeks_to_show=1)))
        view.pack(fill="both", expand=True)
        await view.load()
        self.assertEqual(self.cards(view, TODAY), ["full", "full", "Show 10"])
        old = view._weeks
        assert old is not None
        library._tracked.append(entry(10, TODAY, tracked=True))
        library._suggestions = [s for s in library._suggestions if s.show.id != 10]
        library.gate = asyncio.Event()
        view.refresh_quietly()
        await library.reached.wait()  # the new grid's episodes are in, its suggestions not yet
        pending = view._pending
        assert pending is not None
        self.assertIs(view._weeks, old)  # still on screen, unchanged
        self.assertEqual(old.winfo_manager(), "pack")
        self.assertEqual(pending.winfo_manager(), "place")
        stacking = [str(w) for w in view._scroll.winfo_children()]  # lowest first
        self.assertLess(stacking.index(str(pending)), stacking.index(str(old)))
        self.assertEqual(view._note.cget("text"), "")  # no "Finding suggestions…"
        library.gate.set()
        assert view._load_task is not None
        await view._load_task
        self.assertIsNot(view._weeks, old)
        self.assertIs(view._weeks, pending)
        self.assertFalse(old.winfo_exists())
        self.assertIsNone(view._pending)
        self.assertEqual(pending.winfo_manager(), "pack")
        # Show 10's episode instead of its suggestion; the next best is suggested now.
        self.assertEqual(self.cards(view, TODAY), ["full", "full", "full", "Show 11"])

    async def test_a_quiet_reload_of_other_weeks_builds_in_view(self) -> None:
        library = self.library()
        view = self.view(library, FakeSettings(Settings(weeks_to_show=1)))
        await view.load()
        old = view._weeks
        view._offset = 1  # another grid than the one on screen: nothing to keep
        library.gate = asyncio.Event()
        view.refresh_quietly()
        await library.reached.wait()
        self.assertIsNone(view._pending)
        assert old is not None
        self.assertFalse(old.winfo_exists())
        self.assertIsNot(view._weeks, old)
        library.gate.set()
        assert view._load_task is not None
        await view._load_task

    async def test_an_interrupted_quiet_reload_leaves_nothing_behind(self) -> None:
        library = self.library()
        view = self.view(library, FakeSettings(Settings(weeks_to_show=1)))
        await view.load()
        library.gate = asyncio.Event()
        view.refresh_quietly()
        await library.reached.wait()
        pending = view._pending
        assert pending is not None
        library.gate = None
        view.refresh()  # e.g. ◀: cancels the quiet load
        assert view._load_task is not None
        await view._load_task
        self.assertFalse(pending.winfo_exists())
        self.assertIsNone(view._pending)
        assert view._weeks is not None
        self.assertEqual(view._weeks.winfo_manager(), "pack")

    async def test_track_on_a_suggestion_says_following_at_once(self) -> None:
        library = self.library()
        view = self.view(library, FakeSettings(Settings(weeks_to_show=1)))
        await view.load()
        suggestion = self.card_widgets(view, TODAY)[-1]
        self.assertIsInstance(suggestion, SuggestionCard)
        button = suggestion._track_button
        button.invoke()
        self.assertEqual((button.cget("text"), button.cget("state")), ("…", "disabled"))
        for _ in range(3):
            await asyncio.sleep(0)
        self.assertEqual(library.tracked_shows, [10])
        self.assertEqual((button.cget("text"), button.cget("state")), ("✓ Following", "disabled"))
        # A failure gives the button back.
        library.track_error = TVMazeError("down")
        button.configure(text="+ Track", state="normal")
        button.invoke()
        for _ in range(3):
            await asyncio.sleep(0)
        self.assertEqual((button.cget("text"), button.cget("state")), ("+ Track", "normal"))

    async def test_missing_episodes_get_the_amber_note(self) -> None:
        # Show 1 today (a poster card) and show 3 a week ago (a compact card) are missing.
        library = self.library(missing=frozenset({100, 300}))
        view = self.view(library, FakeSettings(Settings(weeks_to_show=1)))
        await view.load()
        self.assertEqual(library.missing_calls, library.calendar_calls)
        first, second, _suggestion = self.card_widgets(view, TODAY)
        self.assertTrue(first._layout.note)  # room for the amber note
        self.assertFalse(second._layout.note)
        self.assertGreater(first.cget("height"), second.cget("height"))
        (compact,) = self.card_widgets(view, GRID_START)
        self.assertIsNotNone(compact.note)

    async def test_without_plex_no_notes_and_connecting_brings_them(self) -> None:
        library = self.library(missing=frozenset({100, 300}))
        library.plex_connected = False
        view = self.view(library, FakeSettings(Settings(weeks_to_show=1)))
        await view.load()
        first, second, _suggestion = self.card_widgets(view, TODAY)
        self.assertFalse(first._layout.note)  # no room for a note: none is drawn
        (compact,) = self.card_widgets(view, GRID_START)
        self.assertIsNone(compact.note)  # not even made
        # Plex connected: the pages reload (PlexService's on_change) and the notes come.
        library.plex_connected = True
        view.refresh_quietly()
        assert view._load_task is not None
        await view._load_task
        first, _second, _suggestion = self.card_widgets(view, TODAY)
        self.assertTrue(first._layout.note)
        (compact,) = self.card_widgets(view, GRID_START)
        self.assertIsNotNone(compact.note)
        # Disconnected: gone again.
        library.plex_connected = False
        view.refresh_quietly()
        assert view._load_task is not None
        await view._load_task
        (compact,) = self.card_widgets(view, GRID_START)
        self.assertIsNone(compact.note)

    async def test_a_shows_episodes_of_one_day_share_a_stack_card(self) -> None:
        def episode(show_id: int, number: int, day: date) -> CalendarEntry:
            return CalendarEntry(
                show=Show(id=show_id, name=f"Show {show_id}"),
                episode=Episode(
                    id=show_id * 100 + number,
                    show_id=show_id,
                    season=1,
                    number=number,
                    air_date=day,
                ),
                is_tracked=True,
            )

        tracked = [
            episode(4, 1, TODAY),
            episode(5, 1, TODAY),
            episode(4, 2, TODAY),
            episode(4, 3, TODAY),
            episode(6, 7, GRID_START),
            episode(6, 8, GRID_START),
        ]
        library = FakeLibrary(tracked, [], missing=frozenset({402}), in_plex=frozenset({403}))
        view = self.view(library, FakeSettings(Settings(weeks_to_show=1, show_suggestions=False)))
        await view.load()
        self.assertEqual(library.plex_calls, library.calendar_calls)
        stack, single = self.card_widgets(view, TODAY)
        self.assertIsInstance(stack, StackCard)
        self.assertIsInstance(single, EpisodeCard)
        self.assertNotIsInstance(single, StackCard)
        self.assertEqual(
            [entry.episode.number for entry in stack._stack.entries], [1, 2, 3]
        )
        self.assertEqual((stack._in_plex, stack._marks), ({403}, True))
        (compact,) = self.card_widgets(view, GRID_START)
        self.assertIsInstance(compact, CompactStackCard)
        self.assertEqual(compact.badge.cget("text"), "2")
        self.assertIsNone(compact.note)
        # An unfolded stack stays unfolded when the page reloads.
        self.assertFalse(stack.unfolded)
        stack.toggle()
        await view.load()
        (reloaded, _single) = self.card_widgets(view, TODAY)
        self.assertIsNot(reloaded, stack)
        self.assertTrue(reloaded.unfolded)
        reloaded.toggle()
        await view.load()
        self.assertFalse(self.card_widgets(view, TODAY)[0].unfolded)

    async def test_a_suggestions_name_opens_the_show_window(self) -> None:
        opened: list[Show] = []
        view = self.view(self.library(), FakeSettings(Settings(weeks_to_show=1)), opened.append)
        await view.load()
        suggestion = self.card_widgets(view, TODAY)[-1]
        self.assertIsInstance(suggestion, SuggestionCard)
        self.assertEqual(str(suggestion.name_label.cget("cursor")), "hand2")
        self.assertNotEqual(suggestion.name_label._label.bind("<Button-1>"), "")
        # The hand on all of the clickable name: its frame and the canvas around the text too.
        self.assertEqual(str(tk.Frame.cget(suggestion.name_label, "cursor")), "hand2")
        self.assertNotEqual(suggestion.name_label._canvas.bind("<Button-1>"), "")
        self.assertEqual(str(suggestion.name_label._canvas.cget("cursor")), "hand2")
        self.assertEqual(str(suggestion._track_button.cget("cursor")), "hand2")
        assert suggestion.open_show is not None
        suggestion.open_show()  # as a click on the name does
        self.assertEqual([show.name for show in opened], ["Show 10"])

    async def test_the_whole_suggestion_card_opens_the_show_but_track(self) -> None:
        opened: list[Show] = []
        view = self.view(self.library(), FakeSettings(Settings(weeks_to_show=1)), opened.append)
        await view.load()
        suggestion = self.card_widgets(view, TODAY)[-1]
        self.assertIsInstance(suggestion, SuggestionCard)
        track = suggestion._track_button
        own = {str(w) for w in (track, *_descendants(track))}
        rest = [w for w in (suggestion, *_descendants(suggestion)) if str(w) not in own]
        self.assertGreater(len(rest), 5)  # the card, its frames, texts and their canvases
        for widget in rest:
            with self.subTest(widget=str(widget)):
                self.assertEqual(str(tk.Misc.cget(widget, "cursor")), "hand2")
        # One click, one window: the background, the code, the name's text.
        code = next(
            w for w in _descendants(suggestion)
            if isinstance(w, ctk.CTkLabel) and w is not suggestion.name_label
            and str(w.cget("text")).startswith("S")
            and str(w) not in own
        )
        for widget in (suggestion, code._label, suggestion.name_label._label):
            opened.clear()
            widget.event_generate("<Button-1>", x=1, y=1)
            self.assertEqual([show.name for show in opened], ["Show 10"], str(widget))
        # "+ Track" keeps its own action: its parts don't open the show.
        for widget in (track, *_descendants(track)):
            self.assertNotIn("open_show", str(tk.Misc.bind(widget, "<Button-1>")))
        opened.clear()
        for widget in _descendants(track):
            widget.event_generate("<Button-1>", x=1, y=1)
        self.assertEqual(opened, [])

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
        self.assertEqual(await press("❯"), next_weeks)
        self.assertEqual(view._range.cget("text"), range_label(*weeks_from_now(1)))
        self.assertEqual(await press("❮"), weeks_from_now(0))
        self.assertEqual(await press("❮"), weeks_from_now(-1))
        self.assertNotIn("Today", buttons)  # a click on the dates does that
        view._range.invoke()
        if view._load_task is not None:
            await view._load_task
        self.assertEqual(library.calendar_calls[-1], weeks_from_now(0))
        calls = len(library.calendar_calls)
        view._range.invoke()  # already on this week: no reload
        self.assertEqual(len(library.calendar_calls), calls)


    # -- the box while no show is followed -----------------------------------------------------

    async def test_the_box_shows_while_no_show_is_followed(self) -> None:
        library = self.library()
        library.follows = {1: 0}
        picks: list[str] = []
        view = self.view(
            library,
            FakeSettings(Settings(weeks_to_show=1)),
            on_pick_shows=lambda: picks.append("open"),
        )
        view.pack(fill="both", expand=True)
        await view.load()
        self.assertTrue(view.empty_box_shown)
        self.assertEqual(library.stats_people, [1])
        self.assertEqual(self.cards(view, TODAY), ["full", "full", "Show 10"])  # grid as before
        texts = [
            str(widget.cget("text"))
            for widget in _descendants(view._empty_box)
            if isinstance(widget, ctk.CTkLabel)
        ]
        self.assertIn("You don't follow any shows yet", texts)
        self.assertIn("Pick a few series and their new episodes appear here.", texts)
        self.assertEqual(view.pick_shows_button.cget("text"), "Pick shows to follow…")
        view.pick_shows_button.invoke()
        self.assertEqual(picks, ["open"])
        # A show followed: the quiet reload takes the box away.
        library.follows = {1: 1}
        view.refresh_quietly()
        assert view._load_task is not None
        await view._load_task
        self.assertFalse(view.empty_box_shown)

    async def test_no_box_with_a_show_followed(self) -> None:
        library = self.library()
        library.follows = {1: 1}
        view = self.view(library, FakeSettings(Settings(weeks_to_show=1)), on_pick_shows=list)
        await view.load()
        self.assertFalse(view.empty_box_shown)

    async def test_the_box_is_per_person(self) -> None:
        library = self.library()
        library.follows = {1: 2, 2: 0}
        settings = FakeSettings(Settings(weeks_to_show=1))
        view = self.view(library, settings, on_pick_shows=list)
        await view.load()
        self.assertFalse(view.empty_box_shown)
        await settings.update(current_profile=2)  # another person selected
        await view.load()
        self.assertTrue(view.empty_box_shown)
        self.assertEqual(library.stats_people, [1, 2])
        await settings.update(current_profile=1)
        await view.load()
        self.assertFalse(view.empty_box_shown)

    async def test_no_box_without_the_window_to_open(self) -> None:
        library = self.library()
        library.follows = {1: 0}
        view = self.view(library, FakeSettings(Settings(weeks_to_show=1)))
        await view.load()
        self.assertFalse(view.empty_box_shown)
        self.assertEqual(library.stats_people, [])


class KeptScrollTests(unittest.TestCase):
    def test_the_row_in_view_stays_put(self) -> None:
        old, grown = [0, 450, 900], [0, 636, 1086]  # the first row grew by 186
        # The second row starts 16 px into the view: it does again after the swap.
        self.assertEqual(kept_scroll(434, 900, old, grown), 620)
        # Inside the second row (none starts in view): the same distance into it.
        self.assertEqual(kept_scroll(500, 300, old, grown), 686)
        self.assertIsNone(kept_scroll(0, 900, old, grown))  # at the top: stays
        self.assertIsNone(kept_scroll(434, 900, old, grown[:2]))
        self.assertEqual(kept_scroll(434, 900, old, [0, 300, 700]), 284)  # a row shrank


class SuggestionStartTests(unittest.TestCase):
    def test_the_first_row_that_has_not_passed(self) -> None:
        end = GRID_START + timedelta(weeks=3, days=-1)  # three week rows
        self.assertEqual(suggestion_start(GRID_START, end, today=GRID_START), GRID_START)
        # A row's last day is still its own: today counts.
        self.assertEqual(suggestion_start(GRID_START, end, today=SUNDAY), GRID_START)
        self.assertEqual(suggestion_start(GRID_START, end, today=TODAY), TODAY)
        self.assertEqual(
            suggestion_start(GRID_START, end, today=TODAY + timedelta(days=9)),
            TODAY + timedelta(weeks=1),
        )
        self.assertIsNone(suggestion_start(GRID_START, end, today=end + timedelta(days=1)))
        # A grid in the future starts with suggestions.
        self.assertEqual(suggestion_start(TODAY, end, today=GRID_START), TODAY)


if __name__ == "__main__":
    unittest.main()
