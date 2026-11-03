"""The "Let's get started" window (app/ui/get_started.py): its pure helpers, and the window on
a hidden Tk root with a fake library and discovery service (no live API: the shows have no
posters)."""

from __future__ import annotations

import asyncio
import gc
import tkinter as tk
import unittest
from datetime import date, timedelta
from typing import Any
from unittest import mock

import customtkinter as ctk

from app import i18n
from app.i18n import Language
from app.models import (
    CalendarEntry,
    DiscoverShow,
    DiscoverShowEntry,
    Episode,
    Show,
    ShowSearchResult,
    TVDashboard,
)
from app.services.tvmaze import TVMazeError
from app.ui import get_started, poster_card, theme
from app.ui.get_started import (
    ADD_MARK,
    CHOSEN_MARK,
    GetStartedDialog,
    ListKind,
    Pick,
    available_lists,
    chosen_text,
    dashboard_lists,
    failed_text,
    follow_text,
    get_started_due,
    meta_line,
    schedule_picks,
    search_picks,
    tmdb_picks,
)

TODAY = date(2026, 11, 2)  # a Monday


def tmdb(show_id: int, name: str = "", *, year: int | None = 2024) -> DiscoverShow:
    first = date(year, 1, 5) if year else None
    return DiscoverShow(id=show_id, name=name or f"TMDB {show_id}", first_air_date=first)


def shelf(*shows: DiscoverShow, tracked: frozenset[int] = frozenset()) -> tuple[DiscoverShowEntry, ...]:
    return tuple(DiscoverShowEntry(show=show, is_tracked=show.id in tracked) for show in shows)


def maze(show_id: int, name: str = "", network: str | None = "NBC") -> Show:
    return Show(id=show_id, name=name or f"Maze {show_id}", network=network)


def airing(show_id: int, day: date, *, network: str | None = "NBC") -> CalendarEntry:
    return CalendarEntry(
        show=maze(show_id, network=network),
        episode=Episode(id=show_id * 100 + day.day, show_id=show_id, season=1, number=1, air_date=day),
        is_tracked=False,
    )


# -- pure helpers --------------------------------------------------------------------------------


class HelperTests(unittest.TestCase):
    def test_meta_line(self) -> None:
        self.assertEqual(meta_line("NBC", "Mon", 2024), "NBC · Mon")
        self.assertEqual(meta_line("HBO", None, 2019), "HBO · 2019")
        self.assertEqual(meta_line(None, None, 2024), "2024")
        self.assertEqual(meta_line(None, "Fri", None), "Fri")
        self.assertEqual(meta_line(None, None, None), "")

    def test_keys_are_stable_per_source(self) -> None:
        self.assertEqual(Pick(tmdb(7)).key, ("tmdb", 7))
        self.assertEqual(Pick(maze(7)).key, ("tvmaze", 7))
        self.assertNotEqual(Pick(tmdb(7)).key, Pick(maze(7)).key)

    def test_tmdb_lists_leave_followed_shows_out(self) -> None:
        picks = tmdb_picks(shelf(tmdb(1), tmdb(2), tmdb(3), tracked=frozenset({2})))
        self.assertEqual([pick.key for pick in picks], [("tmdb", 1), ("tmdb", 3)])
        self.assertEqual(picks[0].meta, "2024")

    def test_tmdb_lists_hold_eighteen_at_most(self) -> None:
        self.assertEqual(len(tmdb_picks(shelf(*(tmdb(n) for n in range(30))))), 18)

    def test_dashboard_lists_and_failed_shelves(self) -> None:
        dashboard = TVDashboard(
            trending=shelf(tmdb(1)), on_air=shelf(tmdb(2)), premieres=(), failed=("premieres",)
        )
        lists, failed = dashboard_lists(dashboard)
        self.assertEqual([p.key for p in lists[ListKind.POPULAR]], [("tmdb", 1)])
        self.assertEqual([p.key for p in lists[ListKind.ON_AIR]], [("tmdb", 2)])
        self.assertEqual(failed, frozenset({ListKind.COMING}))

    def test_schedule_takes_each_days_best_first_once_per_show(self) -> None:
        tuesday = TODAY + timedelta(days=1)
        entries = [
            airing(1, TODAY),  # Monday's best
            airing(2, TODAY),
            airing(3, tuesday),  # Tuesday's best
            airing(1, tuesday),  # again on Tuesday: kept once, as Monday's
            airing(4, tuesday),
        ]
        picks = schedule_picks(entries)
        self.assertEqual([pick.source.id for pick in picks], [1, 3, 2, 4])
        self.assertEqual(picks[0].meta, "NBC · Mon")
        self.assertEqual(picks[1].meta, "NBC · Tue")
        self.assertEqual(len(schedule_picks([airing(n, TODAY) for n in range(40)])), 18)

    def test_search_leaves_followed_shows_out(self) -> None:
        results = [
            ShowSearchResult(show=maze(1), is_tracked=False),
            ShowSearchResult(show=maze(2), is_tracked=True),
        ]
        self.assertEqual([pick.key for pick in search_picks(results)], [("tvmaze", 1)])

    def test_chips_with_and_without_a_tmdb_key(self) -> None:
        self.assertEqual(
            available_lists(True), (ListKind.POPULAR, ListKind.ON_AIR, ListKind.COMING)
        )
        self.assertEqual(available_lists(False), (ListKind.ON_AIR,))

    def test_texts(self) -> None:
        self.assertEqual(chosen_text(0), "Nothing chosen yet")
        self.assertEqual(chosen_text(1), "1 show chosen")
        self.assertEqual(chosen_text(3), "3 shows chosen")
        self.assertEqual(follow_text(1), "Follow 1 show")
        self.assertEqual(follow_text(3), "Follow 3 shows")
        self.assertEqual(failed_text(["A", "B"]), "Couldn't follow: A, B")

    def test_texts_in_dutch(self) -> None:
        i18n.set_language(Language.NL)
        self.addCleanup(i18n.set_language, Language.EN)
        self.assertEqual(chosen_text(3), "3 series gekozen")
        self.assertEqual(follow_text(1), "1 serie volgen")
        self.assertEqual(follow_text(3), "3 series volgen")

    def test_opens_by_itself_only_once_and_only_without_shows(self) -> None:
        self.assertTrue(get_started_due(False, 0))
        self.assertFalse(get_started_due(False, 1))
        self.assertFalse(get_started_due(True, 0))


# -- the window ----------------------------------------------------------------------------------


class FakeLibrary:
    def __init__(self) -> None:
        self.schedule: list[CalendarEntry] = []
        self.results: list[ShowSearchResult] = []
        self.search_error: Exception | None = None
        self.schedule_error: Exception | None = None
        self.track_errors: dict[int, Exception] = {}
        self.tracked: list[tuple[int, int]] = []  # (TVMaze id, person)
        self.searches: list[tuple[str, int]] = []
        self.suggestion_calls: list[tuple[date, date, int]] = []

    async def suggestions(
        self, start: date, end: date, *, profile_id: int, per_day: int
    ) -> list[CalendarEntry]:
        self.suggestion_calls.append((start, end, profile_id))
        if self.schedule_error is not None:
            raise self.schedule_error
        return self.schedule

    async def search(
        self, query: str, *, profile_id: int, limit: int = 12
    ) -> list[ShowSearchResult]:
        self.searches.append((query, profile_id))
        if self.search_error is not None:
            raise self.search_error
        return self.results

    async def track(self, show_id: int, *, profile_id: int) -> Show:
        if show_id in self.track_errors:
            raise self.track_errors[show_id]
        self.tracked.append((show_id, profile_id))
        return maze(show_id)


class FakeDiscovery:
    def __init__(self, *, key: bool = True) -> None:
        self.key = key
        self.dashboard = TVDashboard(
            trending=shelf(tmdb(1, "The Ark"), tmdb(2, "Harbor"), tmdb(3), tracked=frozenset({3})),
            on_air=shelf(tmdb(4, "Night Shift"), tmdb(1, "The Ark")),
            premieres=shelf(tmdb(5, "Orbit")),
        )
        self.dashboard_error: Exception | None = None
        self.track_errors: dict[int, Exception] = {}
        self.tracked: list[tuple[int, int]] = []  # (TMDB id, person)
        self.dashboard_people: list[int] = []

    @property
    def has_tmdb_key(self) -> bool:
        return self.key

    async def tv_dashboard(self, *, profile_id: int, force: bool = False) -> TVDashboard:
        self.dashboard_people.append(profile_id)
        if self.dashboard_error is not None:
            raise self.dashboard_error
        return self.dashboard

    async def track(self, show: DiscoverShow, *, profile_id: int) -> Show:
        if show.id in self.track_errors:
            raise self.track_errors[show.id]
        self.tracked.append((show.id, profile_id))
        return maze(show.id + 1000)


class FixedDate(date):
    @classmethod
    def today(cls) -> FixedDate:
        return cls(TODAY.year, TODAY.month, TODAY.day)


class GetStartedDialogTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        gc.collect()
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        for cache in (poster_card._placeholders, poster_card._placeholder_tasks):
            patcher = mock.patch.dict(cache, clear=True)
            patcher.start()
            self.addCleanup(patcher.stop)
        ring_cache = mock.patch.dict(get_started._rings, clear=True)
        ring_cache.start()
        self.addCleanup(ring_cache.stop)
        today = mock.patch("app.ui.get_started.date", FixedDate)
        today.start()
        self.addCleanup(today.stop)
        self.library = FakeLibrary()
        self.discovery = FakeDiscovery()

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 1.0

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    async def settle(self, turns: int = 5) -> None:
        for _ in range(turns):
            await asyncio.sleep(0.01)
            if self.root.winfo_exists():
                self.root.update()

    async def dialog(self, *, profile_id: int = 2) -> GetStartedDialog:
        library: Any = self.library
        discovery: Any = self.discovery
        images: Any = object()  # never used: no show has a poster
        dialog = GetStartedDialog(
            self.root, library=library, discovery=discovery, images=images, profile_id=profile_id
        )

        def close() -> None:
            if dialog.winfo_exists():
                dialog.destroy()

        self.addCleanup(close)
        await self.settle()
        return dialog

    def names(self, dialog: GetStartedDialog) -> list[str]:
        return [card.pick.name for card in dialog.cards.values()]

    async def test_the_three_lists_leave_followed_shows_out(self) -> None:
        dialog = await self.dialog()
        self.assertEqual(dialog.chips, (ListKind.POPULAR, ListKind.ON_AIR, ListKind.COMING))
        self.assertIs(dialog.current, ListKind.POPULAR)
        self.assertEqual(self.names(dialog), ["The Ark", "Harbor"])  # TMDB 3 is followed
        self.assertEqual(self.discovery.dashboard_people, [2])
        dialog.show_list(ListKind.ON_AIR)
        self.assertEqual(self.names(dialog), ["Night Shift", "The Ark"])
        dialog.show_list(ListKind.COMING)
        self.assertEqual(self.names(dialog), ["Orbit"])
        self.assertEqual(self.library.suggestion_calls, [])  # the schedule only without a key

    async def test_loading_then_the_list(self) -> None:
        gate = asyncio.Event()
        original = self.discovery.tv_dashboard

        async def slow(*, profile_id: int, force: bool = False) -> TVDashboard:
            await gate.wait()
            return await original(profile_id=profile_id)

        self.discovery.tv_dashboard = slow  # type: ignore[method-assign]
        dialog = await self.dialog()
        self.assertEqual(dialog.area_texts(), ["Loading…"])
        gate.set()
        await self.settle()
        self.assertEqual(self.names(dialog), ["The Ark", "Harbor"])

    async def test_without_a_tmdb_key_only_this_weeks_schedule(self) -> None:
        self.discovery.key = False
        self.library.schedule = [airing(7, TODAY), airing(8, TODAY + timedelta(days=2))]
        dialog = await self.dialog()
        self.assertEqual(dialog.chips, (ListKind.ON_AIR,))
        self.assertIs(dialog.current, ListKind.ON_AIR)
        self.assertEqual(self.names(dialog), ["Maze 7", "Maze 8"])
        self.assertEqual(
            self.library.suggestion_calls, [(TODAY, TODAY + timedelta(days=6), 2)]
        )
        self.assertEqual(self.discovery.dashboard_people, [])
        card = dialog.cards[("tvmaze", 8)]
        self.assertEqual(card.meta_text, "NBC · Wed")

    async def test_a_failed_list(self) -> None:
        self.discovery.dashboard_error = TVMazeError("boom")
        dialog = await self.dialog()
        self.assertEqual(dialog.area_texts(), ["Couldn't load this list."])

    async def test_a_failed_schedule(self) -> None:
        self.discovery.key = False
        self.library.schedule_error = TVMazeError("boom")
        dialog = await self.dialog()
        self.assertEqual(dialog.area_texts(), ["Couldn't load this list."])

    async def test_a_click_chooses_and_unchooses(self) -> None:
        dialog = await self.dialog()
        self.assertEqual(dialog.count_text, "Nothing chosen yet")
        self.assertEqual(dialog.follow_button.cget("state"), "disabled")
        card = dialog.cards[("tmdb", 1)]
        self.assertEqual(card.mark_text, ADD_MARK)
        self.assertFalse(card.ring_shown)
        card._clicked(None)  # a click anywhere on the poster
        await self.settle()
        self.assertTrue(card.chosen)
        self.assertEqual(card.mark_text, CHOSEN_MARK)
        self.assertTrue(card.ring_shown)
        self.assertEqual(dialog.count_text, "1 show chosen")
        self.assertEqual(dialog.follow_button.cget("text"), "Follow 1 show")
        self.assertEqual(dialog.follow_button.cget("state"), "normal")
        card._clicked(None)
        await self.settle()
        self.assertFalse(card.chosen)
        self.assertFalse(card.ring_shown)
        self.assertEqual(dialog.count_text, "Nothing chosen yet")
        self.assertEqual(dialog.follow_button.cget("state"), "disabled")

    async def test_chosen_shows_stay_chosen_across_chips_and_a_search(self) -> None:
        self.library.results = [
            ShowSearchResult(show=maze(50, "Fargo"), is_tracked=False),
            ShowSearchResult(show=maze(51, "Followed"), is_tracked=True),
        ]
        dialog = await self.dialog()
        dialog.toggle(dialog.cards[("tmdb", 1)].pick)  # The Ark
        dialog.toggle(dialog.cards[("tmdb", 2)].pick)
        dialog.show_list(ListKind.ON_AIR)
        self.assertTrue(dialog.cards[("tmdb", 1)].chosen)  # The Ark again, still chosen
        self.assertFalse(dialog.cards[("tmdb", 4)].chosen)
        dialog.search("  fargo ")
        self.assertIsNone(dialog.current)
        await self.settle()
        self.assertEqual(self.library.searches, [("fargo", 2)])
        self.assertEqual(dialog.area_texts()[0], 'Results for "fargo"')
        self.assertEqual(self.names(dialog), ["Fargo"])  # the followed one left out
        for chip in dialog._chips.values():  # none selected while the results show
            self.assertNotEqual(chip.cget("fg_color"), theme.ACCENT)
        dialog.toggle(dialog.cards[("tvmaze", 50)].pick)
        self.assertEqual(dialog.count_text, "3 shows chosen")
        dialog.show_list(ListKind.POPULAR)  # a chip goes back
        self.assertIs(dialog.current, ListKind.POPULAR)
        self.assertEqual(dialog._chips[ListKind.POPULAR].cget("fg_color"), theme.ACCENT)
        self.assertTrue(dialog.cards[("tmdb", 1)].chosen)
        self.assertTrue(dialog.cards[("tmdb", 2)].chosen)
        self.assertEqual(
            [pick.key for pick in dialog.chosen], [("tmdb", 1), ("tmdb", 2), ("tvmaze", 50)]
        )
        self.assertEqual(dialog.follow_button.cget("text"), "Follow 3 shows")

    async def test_a_search_that_fails_or_finds_nothing(self) -> None:
        dialog = await self.dialog()
        dialog.search("nothing")
        await self.settle()
        self.assertEqual(dialog.area_texts(), ['Results for "nothing"', "No TV shows found."])
        self.library.search_error = TVMazeError("down")
        dialog.search("again")
        await self.settle()
        self.assertEqual(dialog.area_texts(), ['Results for "again"', "Couldn't load this list."])
        dialog.search("   ")  # empty: nothing happens
        self.assertEqual(self.library.searches, [("nothing", 2), ("again", 2)])

    async def test_follow_tracks_each_chosen_show_for_the_person_and_closes(self) -> None:
        self.library.results = [ShowSearchResult(show=maze(50, "Fargo"), is_tracked=False)]
        dialog = await self.dialog(profile_id=3)
        dialog.toggle(dialog.cards[("tmdb", 1)].pick)
        dialog.toggle(dialog.cards[("tmdb", 2)].pick)
        dialog.search("fargo")
        await self.settle()
        dialog.toggle(dialog.cards[("tvmaze", 50)].pick)
        dialog.follow()
        self.assertEqual(dialog.follow_button.cget("text"), "Following…")
        self.assertEqual(dialog.follow_button.cget("state"), "disabled")
        await self.settle()
        self.assertEqual(self.discovery.tracked, [(1, 3), (2, 3)])
        self.assertEqual(self.library.tracked, [(50, 3)])
        self.assertFalse(dialog.winfo_exists())

    async def test_a_show_that_fails_is_named_and_the_rest_go_through(self) -> None:
        self.discovery.track_errors[2] = TVMazeError("no match")
        dialog = await self.dialog()
        dialog.toggle(dialog.cards[("tmdb", 1)].pick)
        dialog.toggle(dialog.cards[("tmdb", 2)].pick)
        dialog.follow()
        await self.settle()
        self.assertTrue(dialog.winfo_exists())
        self.assertEqual(self.discovery.tracked, [(1, 2)])
        self.assertEqual(dialog.message_text, "Couldn't follow: Harbor")
        self.assertEqual(self.names(dialog), ["Harbor"])  # The Ark is followed: it left
        self.assertEqual([pick.key for pick in dialog.chosen], [("tmdb", 2)])
        self.assertEqual(dialog.count_text, "1 show chosen")
        self.assertEqual(dialog.follow_button.cget("text"), "Follow 1 show")
        self.assertEqual(dialog.follow_button.cget("state"), "normal")

    async def test_not_now_and_escape_change_nothing(self) -> None:
        dialog = await self.dialog()
        dialog.toggle(dialog.cards[("tmdb", 1)].pick)
        dialog.not_now_button.invoke()
        await self.settle()
        self.assertFalse(dialog.winfo_exists())
        dialog = await self.dialog()
        dialog.toggle(dialog.cards[("tmdb", 1)].pick)
        dialog.event_generate("<Escape>")
        await self.settle()
        self.assertFalse(dialog.winfo_exists())
        self.assertEqual(self.discovery.tracked, [])
        self.assertEqual(self.library.tracked, [])

    async def test_the_window_in_dutch(self) -> None:
        i18n.set_language(Language.NL)
        self.addCleanup(i18n.set_language, Language.EN)
        dialog = await self.dialog()
        self.assertEqual(dialog.title(), "Aan de slag")
        self.assertEqual(dialog.count_text, "Nog niets gekozen")
        self.assertEqual(dialog._chips[ListKind.ON_AIR].cget("text"), "Nu op tv")


if __name__ == "__main__":
    unittest.main()
