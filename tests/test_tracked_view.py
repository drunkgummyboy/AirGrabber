"""My shows: the import summary, the sort and filter rules, and the page with its bar and ⋯
menu (on a real hidden Tk root)."""

from __future__ import annotations

import asyncio
import gc
import tkinter as tk
import unittest
from datetime import UTC, date, datetime, timedelta
from typing import Any
from unittest import mock

import customtkinter as ctk

from app.models import Episode, ImportResult, LibraryEntry, Settings, Show, ShowActivity
from app.models.settings import TrackedFilter, TrackedSort
from app.services import SettingsService
from app.ui import poster_card
from app.ui.cards import ShowCard
from app.ui.missing_control import MissingActions, MissingRuns, PopupMenu
from app.ui.views.tracked_view import (
    EMPTY_FILTER_TEXTS,
    FILTER_LABELS,
    SORT_LABELS,
    ShowRow,
    TrackedView,
    arrange,
    empty_filter_text,
    filter_from_label,
    heading_text,
    import_summary,
    matches,
    show_rows,
    sort_from_label,
    sort_rows,
)

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


class ImportSummaryTest(unittest.TestCase):
    def test_nothing(self) -> None:
        self.assertEqual(import_summary(ImportResult()), "Nothing to import.")

    def test_added_plural_and_singular(self) -> None:
        self.assertEqual(import_summary(ImportResult(added=("A", "B", "C"))), "Added 3 shows.")
        self.assertEqual(import_summary(ImportResult(added=("A",))), "Added 1 show.")

    def test_already_tracked(self) -> None:
        self.assertEqual(
            import_summary(ImportResult(already_tracked=("Severance",))),
            "Already tracked: 1 (Severance)",
        )

    def test_not_found(self) -> None:
        self.assertEqual(
            import_summary(ImportResult(not_found=("Foo Show", "999999"))),
            "Not found: Foo Show, 999999",
        )

    def test_failed(self) -> None:
        self.assertEqual(import_summary(ImportResult(failed=("Bar",))), "Failed (network): Bar")

    def test_all_groups(self) -> None:
        result = ImportResult(
            added=("A", "B", "C"),
            already_tracked=("Severance",),
            not_found=("Foo Show", "999999"),
            failed=("Bar",),
        )
        self.assertEqual(
            import_summary(result),
            "Added 3 shows.\n"
            "Already tracked: 1 (Severance)\n"
            "Not found: Foo Show, 999999\n"
            "Failed (network): Bar",
        )

    def test_truncates_long_groups(self) -> None:
        names = tuple(f"S{i}" for i in range(1, 14))
        self.assertEqual(
            import_summary(ImportResult(not_found=names)),
            "Not found: S1, S2, S3, S4, S5, S6, S7, S8, S9, S10 … and 3 more",
        )
        self.assertEqual(
            import_summary(ImportResult(already_tracked=names)),
            "Already tracked: 13 (S1, S2, S3, S4, S5, S6, S7, S8, S9, S10 … and 3 more)",
        )

    def test_exactly_ten_not_truncated(self) -> None:
        names = tuple(f"S{i}" for i in range(10))
        self.assertNotIn("more", import_summary(ImportResult(failed=names)))


# -- sorting and filtering -------------------------------------------------------------------


def _show(show_id: int, name: str, *, status: str = "Running") -> Show:
    return Show(id=show_id, name=name, status=status)


def _episode(show_id: int, when: datetime | None = None, *, day: date | None = None) -> Episode:
    return Episode(
        id=show_id * 100, show_id=show_id, season=1, number=1, airstamp=when, air_date=day
    )


def _names(rows: list[ShowRow]) -> list[str]:
    return [row.show.name for row in rows]


class ShowRowsTests(unittest.TestCase):
    def test_one_row_per_entry_with_what_is_known(self) -> None:
        alpha, beta = _show(1, "Alpha"), _show(2, "Beta")
        upcoming = _episode(1, NOW + timedelta(days=1))
        rows = show_rows(
            [
                LibraryEntry(show=alpha, tracked_at=NOW, auto_download=True),
                LibraryEntry(show=beta, tracked_at=NOW),
            ],
            {1: ShowActivity(show_id=1, next_episode=upcoming)},
            {2: 3},
            {2: NOW - timedelta(days=40)},
        )
        self.assertEqual(
            rows,
            [
                ShowRow(alpha, auto_download=True, missing=0, next_episode=upcoming),
                ShowRow(beta, missing=3, last_download=NOW - timedelta(days=40)),
            ],
        )


class FilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.rows = [
            ShowRow(_show(1, "Alpha"), auto_download=True),
            ShowRow(_show(2, "Beta", status="Ended"), missing=2),
            ShowRow(_show(3, "Gamma")),
        ]

    def test_each_filter(self) -> None:
        cases = {
            TrackedFilter.ALL: ["Alpha", "Beta", "Gamma"],
            TrackedFilter.MISSING: ["Beta"],
            TrackedFilter.AUTO: ["Alpha"],
            TrackedFilter.ENDED: ["Beta"],
        }
        for show_filter, expected in cases.items():
            with self.subTest(show_filter):
                shown = [row for row in self.rows if matches(row, show_filter)]
                self.assertEqual(_names(shown), expected)

    def test_arrange_filters_then_sorts(self) -> None:
        rows = [*self.rows, ShowRow(_show(4, "Aardvark", status="Ended"))]
        self.assertEqual(
            _names(arrange(rows, TrackedSort.NAME, TrackedFilter.ENDED)), ["Aardvark", "Beta"]
        )


class SortTests(unittest.TestCase):
    def test_a_to_z_ignores_case(self) -> None:
        rows = [
            ShowRow(_show(1, "charlie")),
            ShowRow(_show(2, "Bravo")),
            ShowRow(_show(3, "alpha")),
        ]
        self.assertEqual(_names(sort_rows(rows, TrackedSort.NAME)), ["alpha", "Bravo", "charlie"])

    def test_next_episode_soonest_first_then_the_rest_a_to_z(self) -> None:
        in_two_days = (NOW + timedelta(days=2)).date()
        rows = [
            ShowRow(_show(1, "Zulu")),  # nothing coming
            ShowRow(_show(2, "Later"), next_episode=_episode(2, NOW + timedelta(days=5))),
            ShowRow(_show(3, "Soon"), next_episode=_episode(3, NOW + timedelta(hours=3))),
            ShowRow(_show(4, "Alpha")),  # nothing coming
            # No exact time: its air date (midnight UTC) counts.
            ShowRow(_show(5, "Dated"), next_episode=_episode(5, day=in_two_days)),
        ]
        self.assertEqual(
            _names(sort_rows(rows, TrackedSort.NEXT_EPISODE)),
            ["Soon", "Dated", "Later", "Alpha", "Zulu"],
        )

    def test_recently_downloaded_latest_first_never_downloaded_last(self) -> None:
        rows = [
            ShowRow(_show(1, "Never B")),
            ShowRow(_show(2, "Old"), last_download=NOW - timedelta(days=300)),
            ShowRow(_show(3, "Never A")),
            ShowRow(_show(4, "Fresh"), last_download=NOW - timedelta(hours=1)),
        ]
        self.assertEqual(
            _names(sort_rows(rows, TrackedSort.RECENT)), ["Fresh", "Old", "Never A", "Never B"]
        )


class TextTests(unittest.TestCase):
    def test_heading_counts(self) -> None:
        self.assertEqual(heading_text(40, 40, TrackedFilter.ALL), "Tracked TV Shows (40)")
        self.assertEqual(
            heading_text(12, 40, TrackedFilter.MISSING), "Tracked TV Shows (12 of 40)"
        )
        self.assertEqual(heading_text(0, 40, TrackedFilter.ENDED), "Tracked TV Shows (0 of 40)")

    def test_empty_filter_lines(self) -> None:
        self.assertEqual(
            empty_filter_text(TrackedFilter.MISSING), "No shows with missing episodes."
        )
        self.assertEqual(empty_filter_text(TrackedFilter.ENDED), "No ended shows.")
        self.assertEqual(set(EMPTY_FILTER_TEXTS), set(TrackedFilter))

    def test_labels_round_trip(self) -> None:
        self.assertEqual(
            list(SORT_LABELS.values()), ["A–Z", "Next episode", "Recently downloaded"]
        )
        self.assertEqual(list(FILTER_LABELS.values()), ["All", "Has missing", "Auto on", "Ended"])
        for order, label in SORT_LABELS.items():
            self.assertIs(sort_from_label(label), order)
        for choice, label in FILTER_LABELS.items():
            self.assertIs(filter_from_label(label), choice)
        self.assertIsNone(sort_from_label("Newest"))
        self.assertIsNone(filter_from_label("Running"))

    def test_settings_defaults_and_stored_values(self) -> None:
        settings = Settings()
        self.assertIs(settings.tracked_sort, TrackedSort.NAME)
        self.assertIs(settings.tracked_filter, TrackedFilter.ALL)
        stored = Settings.model_validate({"tracked_sort": "recent", "tracked_filter": "ended"})
        self.assertIs(stored.tracked_sort, TrackedSort.RECENT)
        self.assertIs(stored.tracked_filter, TrackedFilter.ENDED)


# -- the page ----------------------------------------------------------------------------------


class _NoImages:
    async def get(self, *args: object, **kwargs: object) -> None:
        return None


class _Repository:
    def __init__(self) -> None:
        self.saved: list[Settings] = []

    async def save(self, settings: Settings) -> None:
        self.saved.append(settings)


class _Library:
    def __init__(self, entries: list[LibraryEntry], missing: dict[int, int]) -> None:
        self.entries = entries
        self.missing = missing
        self.removed_ended = 0

    async def tracked(self) -> list[LibraryEntry]:
        return self.entries

    async def activity(self) -> dict[int, ShowActivity]:
        return {}

    async def missing_counts(self) -> dict[int, int]:
        return self.missing

    async def last_downloads(self) -> dict[int, datetime]:
        return {2: NOW - timedelta(days=3), 3: NOW - timedelta(days=1)}

    async def remove_ended(self) -> int:
        self.removed_ended += 1
        return 1


class TrackedViewWidgetTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        gc.collect()  # see tests/test_card_size.py: no Tk objects collected in a worker thread
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

    def _destroy_root(self) -> None:
        if PopupMenu._open is not None:
            PopupMenu._open.close()
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    async def view(self, *, missing: dict[int, int], **settings: Any) -> TrackedView:
        entries = [
            LibraryEntry(show=_show(1, "Charlie"), tracked_at=NOW, auto_download=True),
            LibraryEntry(show=_show(2, "alpha", status="Ended"), tracked_at=NOW),
            LibraryEntry(show=_show(3, "Bravo"), tracked_at=NOW),
        ]
        self.library = _Library(entries, missing)
        repository: Any = _Repository()
        self.settings = SettingsService(repository, Settings(**settings))
        actions = MissingActions(
            download=lambda show: None,
            mark_have=lambda show, ids: None,
            unmark_have=lambda show, ids: None,
            runs=MissingRuns(),
        )
        images: Any = _NoImages()
        library: Any = self.library
        view = TrackedView(
            self.root,
            library=library,
            settings=self.settings,
            airdates=mock.Mock(),
            images=images,
            status=lambda text, clear_after_ms=None: None,
            missing_actions=actions,
        )
        view.pack(fill="both", expand=True)
        await view.load()
        self.root.update()
        return view

    def shown(self, view: TrackedView) -> list[str]:
        grid = view._grid
        assert grid is not None
        if not grid.winfo_manager():
            return []
        cards = [child for child in grid._shown() if isinstance(child, ShowCard)]
        return [card.title_text for card in cards]

    async def settle(self) -> None:
        for _ in range(3):
            await asyncio.sleep(0)

    async def test_stored_choices_and_changing_them(self) -> None:
        view = await self.view(missing={3: 2}, tracked_sort="recent")
        self.assertEqual(view._sort_choice.get(), "Recently downloaded")
        self.assertEqual(view._filter_choice.get(), "All")
        self.assertEqual(view._title.cget("text"), "Tracked TV Shows (3)")
        self.assertEqual(self.shown(view), ["Bravo", "alpha", "Charlie"])

        view._choose_sort("A–Z")
        self.assertEqual(self.shown(view), ["alpha", "Bravo", "Charlie"])
        view._choose_filter("Has missing")
        self.assertEqual(self.shown(view), ["Bravo"])
        self.assertEqual(view._title.cget("text"), "Tracked TV Shows (1 of 3)")
        await self.settle()
        self.assertIs(self.settings.current.tracked_sort, TrackedSort.NAME)
        self.assertIs(self.settings.current.tracked_filter, TrackedFilter.MISSING)

    async def test_a_filter_without_shows_says_so(self) -> None:
        view = await self.view(missing={}, tracked_filter="missing")
        self.assertEqual(self.shown(view), [])
        no_match = view._no_match
        assert no_match is not None
        self.assertTrue(no_match.winfo_manager())
        self.assertEqual(no_match.cget("text"), "No shows with missing episodes.")
        self.assertEqual(view._title.cget("text"), "Tracked TV Shows (0 of 3)")
        view._choose_filter("All")
        self.assertEqual(self.shown(view), ["alpha", "Bravo", "Charlie"])
        self.assertFalse(no_match.winfo_manager())

    async def test_auto_filter_follows_the_switch_at_the_next_change(self) -> None:
        view = await self.view(missing={}, tracked_filter="auto")
        self.assertEqual(self.shown(view), ["Charlie"])
        view._note_auto(3, True)  # switched on from Bravo's card
        self.assertEqual(self.shown(view), ["Charlie"])  # the card doesn't jump away
        view._choose_sort("Next episode")
        self.assertEqual(self.shown(view), ["Bravo", "Charlie"])

    async def test_remove_ended_shows_from_the_more_menu(self) -> None:
        view = await self.view(missing={})
        view._open_more_menu()
        menu = PopupMenu._open
        assert menu is not None
        self.assertEqual(menu.labels, ("Remove ended shows…",))
        menu.buttons[0].invoke()
        await self.settle()
        self.assertEqual(self.library.removed_ended, 1)
        self.assertEqual(view._more_button.cget("state"), "normal")


if __name__ == "__main__":
    unittest.main()
