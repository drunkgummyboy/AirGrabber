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

from app.models import FollowRecord, Episode, ImportResult, LibraryEntry, Settings, Show, ShowActivity
from app.models.plex import PlexShows
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
    return Show(id=show_id, name=name, status=status, tvdb_id=300 + show_id)


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
    def __init__(
        self, entries: list[LibraryEntry], missing: dict[int, int], *, plex: bool = True
    ) -> None:
        self.entries = entries
        self.missing = missing
        self.plex_connected = plex  # missing episodes exist only with Plex (3.1.0)
        self.removed_ended = 0
        self.removed_for: list[int] = []  # whose ended shows went
        self.people: list[int] = []  # whose shows each load asked for
        self.untracked: list[int] = []
        self.restored: list[FollowRecord] = []

    async def tracked(self, *, profile_id: int) -> list[LibraryEntry]:
        self.people.append(profile_id)
        return self.entries

    async def activity(self, *, profile_id: int) -> dict[int, ShowActivity]:
        return {}

    async def missing_counts(self, *, profile_id: int) -> dict[int, int]:
        return self.missing if self.plex_connected else {}  # as LibraryService answers

    async def last_downloads(self) -> dict[int, datetime]:
        return {2: NOW - timedelta(days=3), 3: NOW - timedelta(days=1)}

    async def plex_shows(self) -> PlexShows:
        # As LibraryService answers: none without Plex. Bravo is in Plex by its TheTVDB id.
        return PlexShows(tvdb=frozenset({303})) if self.plex_connected else PlexShows()

    async def untrack(self, show_id: int, *, profile_id: int) -> FollowRecord | None:
        self.untracked.append(show_id)
        return FollowRecord(
            show_id=show_id, profile_id=profile_id, tracked_at="2026-01-01T00:00:00Z",
            auto_download=True,
        )

    async def restore(self, record: FollowRecord) -> bool:
        self.restored.append(record)
        return True

    async def remove_ended(self, *, profile_id: int) -> int:
        self.removed_ended += 1
        self.removed_for.append(profile_id)
        return 1


class UndoRemoveTests(unittest.IsolatedAsyncioTestCase):
    """Removing a show offers "Undo" for a while, which puts the follow back as it was."""

    def setUp(self) -> None:
        gc.collect()
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self.root.destroy)

    def page(self) -> tuple[TrackedView, _Library]:
        library = _Library([], {})
        any_library: Any = library
        images: Any = _NoImages()
        repository: Any = _Repository()
        view = TrackedView(
            self.root,
            library=any_library,
            settings=SettingsService(repository, Settings()),
            airdates=mock.Mock(),
            images=images,
            status=lambda text, clear_after_ms=None: None,
        )
        self.addCleanup(view.destroy)
        return view, library

    async def test_undo_puts_the_show_back(self) -> None:
        view, library = self.page()
        await view._untrack(_show(1, "Charlie"))
        self.assertTrue(view._undo_bar.winfo_manager())
        self.assertEqual(view._undo_label.cget("text"), "Removed Charlie")
        view.undo_button.invoke()
        await asyncio.gather(*view._tasks)
        self.assertEqual([r.show_id for r in library.restored], [1])
        self.assertTrue(library.restored[0].auto_download)  # its switch comes back too
        self.assertFalse(view._undo_bar.winfo_manager())

    async def test_the_offer_goes_after_a_while(self) -> None:
        view, library = self.page()
        await view._untrack(_show(1, "Charlie"))
        view._hide_undo()  # what the timer does
        self.assertFalse(view._undo_bar.winfo_manager())
        view._undo()  # too late: nothing comes back
        self.assertEqual(library.restored, [])

    async def test_a_second_removal_is_the_one_undone(self) -> None:
        view, library = self.page()
        await view._untrack(_show(1, "Charlie"))
        await view._untrack(_show(3, "Bravo"))
        self.assertEqual(view._undo_label.cget("text"), "Removed Bravo")
        view._undo()
        await asyncio.gather(*view._tasks)
        self.assertEqual([r.show_id for r in library.restored], [3])


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

    async def view(
        self,
        *,
        missing: dict[int, int],
        plex: bool = True,
        on_pick_shows: Any = None,
        **settings: Any,
    ) -> TrackedView:
        entries = [
            LibraryEntry(show=_show(1, "Charlie"), tracked_at=NOW, auto_download=True),
            LibraryEntry(show=_show(2, "alpha", status="Ended"), tracked_at=NOW),
            LibraryEntry(show=_show(3, "Bravo"), tracked_at=NOW),
        ]
        self.library = _Library(entries, missing, plex=plex)
        repository: Any = _Repository()
        self.settings = SettingsService(repository, Settings(**settings))
        actions = MissingActions(download=lambda show: None, runs=MissingRuns())
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
            on_pick_shows=on_pick_shows,
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

    async def test_a_show_tracked_elsewhere_comes_in_quietly(self) -> None:
        view = await self.view(missing={})
        old = view._grid
        assert old is not None
        on_screen: list[bool] = []
        tracked = self.library.tracked

        async def still_showing(*, profile_id: int) -> list[LibraryEntry]:
            on_screen.append(view._grid is old and old.winfo_manager() == "pack")
            return await tracked(profile_id=profile_id)

        self.library.tracked = still_showing  # type: ignore[method-assign]
        self.library.entries.append(LibraryEntry(show=_show(4, "Delta"), tracked_at=NOW))
        with mock.patch("app.ui.views.tracked_view.LoadingBar") as loading:
            view.refresh_quietly()
            assert view._load_task is not None
            await view._load_task
        loading.assert_not_called()
        self.assertEqual(on_screen, [True])  # the cards stayed while the library was read
        self.assertFalse(old.winfo_exists())
        new = view._grid
        assert new is not None and new is not old
        self.assertEqual(new.winfo_manager(), "pack")
        self.assertEqual(self.shown(view), ["alpha", "Bravo", "Charlie", "Delta"])
        self.assertEqual(view._title.cget("text"), heading_text(4, 4, TrackedFilter.ALL))
        # Under a filter that matches nothing there are no cards to keep: an ordinary load.
        view._filter = TrackedFilter.MISSING
        view._arrange()
        with mock.patch("app.ui.views.tracked_view.LoadingBar") as loading:
            view.refresh_quietly()
            assert view._load_task is not None
            await view._load_task
        loading.assert_called_once()

    async def test_without_plex_no_missing_filter_nor_counts(self) -> None:
        view = await self.view(missing={3: 2}, plex=False, tracked_filter="missing")
        self.assertEqual(view._filter_choice.cget("values"), ["All", "Auto on", "Ended"])
        self.assertEqual(view._filter_choice.get(), "All")  # the stored choice counts as All
        self.assertEqual(self.shown(view), ["alpha", "Bravo", "Charlie"])
        # No "2 missing" pill (a show downloading by itself still wears its blue "Auto").
        pills = [badge.text for card in view._cards.values() for badge in card.badges]
        self.assertFalse([text for text in pills if text != "Auto"])
        labels = [label for label, _ in view._cards[3].menu_items()]
        self.assertNotIn("⬇ Download missing (2)", labels)
        self.assertIs(self.settings.current.tracked_filter, TrackedFilter.MISSING)  # kept
        # Plex connected: the next load (the pages reload) offers it again, and its counts.
        self.library.plex_connected = True
        await view.load()
        self.assertEqual(
            view._filter_choice.cget("values"), ["All", "Has missing", "Auto on", "Ended"]
        )
        self.assertEqual(view._filter_choice.get(), "Has missing")
        self.assertEqual(self.shown(view), ["Bravo"])
        self.assertEqual(view._cards[3].badge_text, "2 missing")
        # Disconnected again: gone again.
        self.library.plex_connected = False
        await view.load()
        self.assertEqual(view._filter_choice.cget("values"), ["All", "Auto on", "Ended"])
        self.assertEqual(self.shown(view), ["alpha", "Bravo", "Charlie"])

    async def test_a_show_in_plex_wears_the_library_mark(self) -> None:
        view = await self.view(missing={})
        marks = {show_id: card.plex_mark is not None for show_id, card in view._cards.items()}
        self.assertEqual(marks, {1: False, 2: False, 3: True})
        self.library.plex_connected = False  # without Plex nothing counts as had
        await view.load()
        self.assertFalse(any(card.plex_mark for card in view._cards.values()))

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

    async def test_pick_shows_opens_get_started(self) -> None:
        opened: list[str] = []
        view = await self.view(missing={}, on_pick_shows=lambda: opened.append("open"))
        button = view.pick_shows_button
        assert button is not None
        self.assertEqual(button.cget("text"), "+ Pick shows…")
        # Between Import Shows and the ⋯.
        header = [str(widget) for widget in button.master.pack_slaves()]
        self.assertLess(header.index(str(view._import_button)), header.index(str(button)))
        self.assertLess(header.index(str(button)), header.index(str(view._more_button)))
        button.invoke()
        self.assertEqual(opened, ["open"])

    async def test_no_pick_shows_without_the_window(self) -> None:
        view = await self.view(missing={})
        self.assertIsNone(view.pick_shows_button)


if __name__ == "__main__":
    unittest.main()
