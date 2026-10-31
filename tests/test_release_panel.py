"""The release panel's pure helpers (table text, row ids, sorting, chips, the selection bar's
text) and the panel itself on a hidden Tk root with fake services: start(), set_target(), the
chips, on_count / on_target_change / on_fix_match, the selection bar and destroy()."""

from __future__ import annotations

import asyncio
import tkinter as tk
import unittest
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from unittest import mock

import customtkinter as ctk

from app.models import (
    Episode,
    GrabOutcome,
    GrabResult,
    GrabTarget,
    IndexerState,
    IndexerStatus,
    LibraryEntry,
    Movie,
    MovieDetails,
    QualityProfile,
    SearchUpdate,
    Settings,
    Show,
    TorrentRelease,
)
from app.services import (
    DownloadService,
    LibraryService,
    SettingsService,
    TorrentSearchService,
)
from app.services.discovery import DiscoveryService
from app.ui import theme
from app.ui.release_panel import (
    DOWNLOAD_TEXT,
    DOWNLOADING_TEXT,
    NO_SELECTION_TEXT,
    IndexerTarget,
    MovieTarget,
    ReleasePanel,
    SortColumn,
    TvTarget,
    action_text,
    chip_style,
    default_quality,
    download_button_text,
    failure_text,
    format_age,
    format_size,
    hidden_text,
    next_sort,
    quality_button_text,
    release_id,
    searching_text,
    selection_detail,
    sort_releases,
)

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
GIB = 1024**3


def release(title: str = "Show.S01E01.1080p-GRP", **fields: object) -> TorrentRelease:
    data: dict[str, object] = {"title": title, "source": "apibay", "magnet_uri": None}
    data.update(fields)
    if not (data.get("info_hash") or data.get("magnet_uri") or data.get("download_url")):
        data["download_url"] = "https://example.org/x.torrent"
    return TorrentRelease.model_validate(data)


def status(state: IndexerState, **fields: object) -> IndexerStatus:
    return IndexerStatus.model_validate({"name": "eztv", "label": "EZTV", "state": state, **fields})


class FormatSizeTests(unittest.TestCase):
    def test_binary_units_with_two_decimals(self) -> None:
        self.assertEqual(format_size(round(3.69 * GIB)), "3.69 GB")
        self.assertEqual(format_size(1536), "1.50 KB")
        self.assertEqual(format_size(700 * 1024 * 1024), "700.00 MB")
        self.assertEqual(format_size(2 * 1024 * GIB), "2.00 TB")

    def test_bytes_and_unknown(self) -> None:
        self.assertEqual(format_size(512), "512 B")
        self.assertEqual(format_size(0), "—")


class FormatAgeTests(unittest.TestCase):
    def test_units(self) -> None:
        self.assertEqual(format_age(NOW - timedelta(minutes=45), NOW), "45m")
        self.assertEqual(format_age(NOW - timedelta(hours=5, minutes=59), NOW), "5h")
        self.assertEqual(format_age(NOW - timedelta(days=3, hours=2), NOW), "3d")
        self.assertEqual(format_age(NOW - timedelta(days=364), NOW), "364d")
        self.assertEqual(format_age(NOW - timedelta(days=800), NOW), "2y")

    def test_unknown_and_future(self) -> None:
        self.assertEqual(format_age(None, NOW), "—")
        self.assertEqual(format_age(NOW + timedelta(hours=2), NOW), "0m")  # clock skew


class ReleaseIdTests(unittest.TestCase):
    def test_info_hash_when_known(self) -> None:
        self.assertEqual(release_id(release(info_hash="AB" * 20)), "ab" * 20)

    def test_title_and_size_otherwise(self) -> None:
        self.assertEqual(release_id(release("A b", size_bytes=10)), "A b|10")

    def test_stable_across_merged_copies(self) -> None:
        first = release(info_hash="cd" * 20, seeders=3)
        merged = first.model_copy(update={"seeders": 9, "source": "apibay+eztv"})
        self.assertEqual(release_id(first), release_id(merged))


class SortTests(unittest.TestCase):
    def setUp(self) -> None:
        self.small = release("b small", size_bytes=1 * GIB, seeders=50, leechers=1,
                             published_at=NOW - timedelta(days=2))
        self.big = release("A big", size_bytes=4 * GIB, seeders=5, leechers=9,
                           published_at=NOW - timedelta(hours=1))
        self.unknown = release("c unknown", size_bytes=0, seeders=20, source="eztv")

    def titles(self, column: SortColumn, descending: bool) -> list[str]:
        items = [self.small, self.big, self.unknown]
        return [r.title for r in sort_releases(items, column, descending=descending)]

    def test_size_unknown_last_both_ways(self) -> None:
        self.assertEqual(self.titles("size", True), ["A big", "b small", "c unknown"])
        self.assertEqual(self.titles("size", False), ["b small", "A big", "c unknown"])

    def test_counts(self) -> None:
        self.assertEqual(self.titles("seeds", True), ["b small", "c unknown", "A big"])
        self.assertEqual(self.titles("leech", True), ["A big", "b small", "c unknown"])

    def test_name_ignores_case(self) -> None:
        self.assertEqual(self.titles("name", False), ["A big", "b small", "c unknown"])

    def test_age_ascending_is_newest_first_unknown_last(self) -> None:
        self.assertEqual(self.titles("age", False), ["A big", "b small", "c unknown"])
        self.assertEqual(self.titles("age", True), ["b small", "A big", "c unknown"])

    def test_source_and_stability(self) -> None:
        self.assertEqual(self.titles("source", False), ["b small", "A big", "c unknown"])
        twins = [release("x", seeders=1), release("y", seeders=1), release("z", seeders=1)]
        self.assertEqual(
            [r.title for r in sort_releases(twins, "seeds", descending=True)], ["x", "y", "z"]
        )

    def test_next_sort(self) -> None:
        self.assertEqual(next_sort("size", True, "size"), ("size", False))
        self.assertEqual(next_sort("size", False, "size"), ("size", True))
        self.assertEqual(next_sort("size", True, "seeds"), ("seeds", True))
        self.assertEqual(next_sort("size", True, "name"), ("name", False))
        self.assertEqual(next_sort("name", False, "age"), ("age", False))


class LabelTests(unittest.TestCase):
    def test_quality_buttons(self) -> None:
        self.assertEqual(quality_button_text("1080p", 12), "1080P (12)")
        self.assertEqual(quality_button_text("2160p", 0), "4K")
        self.assertEqual(quality_button_text("any", 3), "ANY (3)")

    def test_chips(self) -> None:
        self.assertEqual(chip_style(status(IndexerState.SEARCHING)), ("EZTV", theme.PENDING))
        self.assertEqual(
            chip_style(status(IndexerState.FOUND, count=7)), ("EZTV (7)", theme.SUCCESS)
        )
        self.assertEqual(chip_style(status(IndexerState.EMPTY)), ("EZTV", theme.DANGER_BUTTON))
        self.assertEqual(chip_style(status(IndexerState.FAILED)), ("EZTV ✗", theme.DANGER))
        self.assertEqual(chip_style(status(IndexerState.SKIPPED)), ("EZTV", theme.TEXT_FAINT))

    def test_failures(self) -> None:
        statuses = [
            status(IndexerState.FAILED, message="timed out"),
            status(IndexerState.FOUND, name="apibay", label="APIBay", count=2),
            status(
                IndexerState.FAILED,
                name="yts",
                label="YTS",
                message="YTS: /api/v2/list_movies.json answered HTTP 503",
            ),
        ]
        self.assertEqual(
            failure_text(statuses),
            "EZTV: timed out · YTS: /api/v2/list_movies.json answered HTTP 503",
        )
        self.assertEqual(failure_text([status(IndexerState.EMPTY)]), "")

    def test_actions(self) -> None:
        self.assertEqual(action_text(None, grabbed=False), "⬇")
        self.assertEqual(action_text(None, grabbed=True), "✓")
        self.assertEqual(action_text("grabbing", grabbed=True), "⏳")
        self.assertEqual(action_text("opened", grabbed=False), "✓")
        self.assertEqual(action_text("saved", grabbed=False), "✓")
        self.assertEqual(action_text("sent", grabbed=False), "✓")
        self.assertEqual(action_text("failed", grabbed=True), "✗")

    def test_hidden_hint(self) -> None:
        self.assertEqual(
            hidden_text(4, showing=False),
            "4 hidden by name/episode matching or your filters — Show",
        )
        self.assertTrue(hidden_text(4, showing=True).endswith("— Hide"))

    def test_searching(self) -> None:
        self.assertEqual(searching_text(2, 1), "Searching indexers for S02E01…")
        self.assertEqual(searching_text(2, None), "Searching indexers for Season 02 pack…")
        self.assertEqual(searching_text(None, None), "Searching indexers…")

    def test_default_quality_per_kind(self) -> None:
        settings = Settings(quality_tv=QualityProfile.HD, quality_movie=QualityProfile.UHD)
        self.assertEqual(default_quality(TvTarget(SHOW, 1, 1), settings), "720p")
        self.assertEqual(default_quality(MovieTarget(FILM), settings), "2160p")


class SelectionBarTextTests(unittest.TestCase):
    def test_detail_line(self) -> None:
        item = release("Show.S01E01.1080p.WEB-GRP", size_bytes=round(2.1 * GIB), seeders=412)
        self.assertEqual(selection_detail(item), "1080p · 2.10 GB · 412 seeders")

    def test_detail_line_partial(self) -> None:
        self.assertEqual(selection_detail(release("Show.S01E01-GRP", seeders=1)), "1 seeder")
        hevc = release("Show.S01E01.2160p.x265-GRP", size_bytes=GIB, seeders=0)
        self.assertEqual(selection_detail(hevc), "2160p x265 · 1.00 GB · 0 seeders")
        self.assertEqual(selection_detail(release("Show.S01E01.HEVC-GRP")), "x265 · 0 seeders")

    def test_no_selection_and_button(self) -> None:
        self.assertEqual(NO_SELECTION_TEXT, "Click a release above to select it")
        self.assertEqual(download_button_text(None), DOWNLOAD_TEXT)
        self.assertEqual(download_button_text("failed"), DOWNLOAD_TEXT)
        self.assertEqual(download_button_text("grabbing"), DOWNLOADING_TEXT)
        self.assertEqual(download_button_text("opened"), "✓ Opened")
        self.assertEqual(download_button_text("saved"), "✓ Saved")
        self.assertEqual(download_button_text("sent"), "✓ Sent")


# -- fakes (also used by test_info_layouts) ------------------------------------------------

SHOW = Show(id=1, name="Severance")
FILM = Movie(id=7, title="Film")


class FakeSearch:
    """Records each search as ("movie",) / ("season", s) / ("episode", s, e) and answers with
    one finished update of `releases`; with `block`, an unfinished one and then waits until
    cancelled (`cancelled` tells)."""

    def __init__(self, releases: tuple[TorrentRelease, ...] = (), *, block: bool = False) -> None:
        self._releases = releases
        self._block = block
        self.requests: list[tuple[object, ...]] = []
        self.cancelled = False

    def movie_request(self, movie: Movie, details: MovieDetails | None = None) -> object:
        return ("movie",)

    def season_request(self, show: Show, season: int) -> object:
        return ("season", season)

    def episode_request(self, show: Show, season: int, episode: int) -> object:
        return ("episode", season, episode)

    async def search(self, request: object) -> AsyncIterator[SearchUpdate]:
        self.requests.append(cast(tuple[object, ...], request))
        yield SearchUpdate(statuses=(), releases=self._releases, done=not self._block)
        if self._block:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise


class FakeDownloads:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.grabbed: list[str] = []
        self.hash_calls = 0
        self.cancelled = False

    async def grabbed_hashes(self) -> frozenset[str]:
        self.hash_calls += 1
        return frozenset()

    async def grab(self, item: TorrentRelease, target: GrabTarget) -> GrabResult:
        self.grabbed.append(item.title)
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        return GrabResult(outcome=GrabOutcome.SAVED, message="Saved the torrent")


class FakeDiscovery:
    def __init__(self) -> None:
        self.calls = 0

    async def movie_details(self, movie_id: int) -> MovieDetails:
        self.calls += 1
        return MovieDetails(id=movie_id)


class FakeLibrary:
    def __init__(self, episodes: dict[int, list[Episode]] | None = None) -> None:
        self._episodes = episodes or {}
        self.episode_calls: list[int] = []
        self.tracked_ids: list[int] = []
        self.track_calls: list[int] = []

    async def episodes(self, show_id: int) -> list[Episode]:
        self.episode_calls.append(show_id)
        return self._episodes.get(show_id, [])

    async def tracked(self) -> list[LibraryEntry]:
        return [
            LibraryEntry(show=Show(id=show_id, name=f"Show {show_id}"), tracked_at=NOW)
            for show_id in self.tracked_ids
        ]

    async def track(self, show_id: int) -> Show:
        self.track_calls.append(show_id)
        self.tracked_ids.append(show_id)
        return Show(id=show_id, name="The Right Show")

    async def untrack(self, show_id: int) -> None:
        self.tracked_ids.remove(show_id)


class FakeSettings:
    current = Settings()


def episodes(show_id: int, *pairs: tuple[int, int]) -> list[Episode]:
    return [
        Episode(id=index, show_id=show_id, season=season, number=number)
        for index, (season, number) in enumerate(pairs, start=1)
    ]


def no_status(text: str, *, clear_after_ms: int | None = None) -> None:
    pass


class TkTestCase(unittest.IsolatedAsyncioTestCase):
    """A hidden CTk root per test; `settle` lets the tasks and Tk catch up."""

    def setUp(self) -> None:
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 1.0  # building Tk widgets is slow

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()

    async def settle(self) -> None:
        for _ in range(10):
            await asyncio.sleep(0)
        self.root.update()

    @staticmethod
    def destroy_widget(widget: tk.Misc) -> None:
        if widget.winfo_exists():  # a test may have destroyed it already
            widget.destroy()


class PanelTests(TkTestCase):
    def panel(
        self,
        target: IndexerTarget,
        *,
        search: FakeSearch | None = None,
        downloads: FakeDownloads | None = None,
        library: FakeLibrary | None = None,
        discovery: FakeDiscovery | None = None,
        on_target_change: Callable[[IndexerTarget], None] | None = None,
        on_count: Callable[[int], None] | None = None,
        on_fix_match: Callable[[TvTarget], None] | None = None,
        shown: bool = False,
    ) -> ReleasePanel:
        master: tk.Misc = self.root
        if shown:  # in a window on screen: the table then takes keyboard focus
            master = ctk.CTkToplevel(self.root)
            self.addCleanup(self.destroy_widget, master)
        panel = ReleasePanel(
            master,
            target=target,
            search=cast(TorrentSearchService, search or FakeSearch()),
            downloads=cast(DownloadService, downloads or FakeDownloads()),
            library=cast(LibraryService, library or FakeLibrary()),
            discovery=cast(DiscoveryService, discovery or FakeDiscovery()),
            settings=cast(SettingsService, FakeSettings()),
            status=no_status,
            on_target_change=on_target_change,
            on_count=on_count,
            on_fix_match=on_fix_match,
        )
        panel.pack(fill="both", expand=True)
        self.addCleanup(self.destroy_widget, panel)
        return panel

    async def test_nothing_is_loaded_or_searched_before_start(self) -> None:
        search, downloads, library = FakeSearch(), FakeDownloads(), FakeLibrary()
        counts: list[int] = []
        panel = self.panel(
            TvTarget(SHOW, 1, 2),
            search=search,
            downloads=downloads,
            library=library,
            on_count=counts.append,
        )
        await self.settle()
        self.assertEqual(search.requests, [])
        self.assertEqual(library.episode_calls, [])
        self.assertEqual(downloads.hash_calls, 0)
        self.assertEqual(counts, [])
        state: Any = panel
        self.assertIsNone(state._search_task)
        self.assertEqual(state._tasks, set())
        # The target's own chips are there already.
        self.assertEqual(set(state._season_buttons), {1})
        self.assertEqual(set(state._episode_buttons), {None, 1, 2})

        panel.start()
        await self.settle()
        self.assertEqual(search.requests, [("episode", 1, 2)])
        self.assertEqual(library.episode_calls, [1])
        self.assertEqual(downloads.hash_calls, 1)
        panel.start()  # once only
        await self.settle()
        self.assertEqual(len(search.requests), 1)

    async def test_film_waits_for_its_details_only_after_start(self) -> None:
        discovery, search = FakeDiscovery(), FakeSearch()
        panel = self.panel(MovieTarget(FILM), search=search, discovery=discovery)
        await self.settle()
        self.assertEqual(discovery.calls, 0)
        state: Any = panel
        self.assertEqual(state._season_row.winfo_manager(), "")  # no season / episode rows
        self.assertEqual(state._episode_row.winfo_manager(), "")
        panel.start()
        await self.settle()
        self.assertEqual(discovery.calls, 1)
        self.assertEqual(search.requests, [("movie",)])

    async def test_on_count_follows_the_version(self) -> None:
        releases = (
            release("Film.2024.1080p-A", size_bytes=2 * GIB),
            release("Film.2024.1080p-B", size_bytes=3 * GIB),
            release("Film.2024.720p-C", size_bytes=GIB),
        )
        counts: list[int] = []
        panel = self.panel(
            MovieTarget(FILM), search=FakeSearch(releases), on_count=counts.append
        )
        panel.start()
        await self.settle()
        self.assertEqual(counts, [0, 2])  # searching, then the 1080p releases
        state: Any = panel
        self.assertEqual(state._results_title.cget("text"), "Releases (2)")
        state._select_quality("any")
        state._select_quality("720p")
        state._select_quality("720p")  # unchanged: not told again
        self.assertEqual(counts, [0, 2, 3, 1])

    async def test_set_target_and_the_chips(self) -> None:
        search = FakeSearch()
        library = FakeLibrary({1: episodes(1, (1, 1), (1, 2), (1, 3), (2, 1), (2, 2))})
        changes: list[IndexerTarget] = []
        panel = self.panel(
            TvTarget(SHOW, 1, 2), search=search, library=library, on_target_change=changes.append
        )
        panel.start()
        await self.settle()
        state: Any = panel
        self.assertEqual(set(state._season_buttons), {1, 2})
        self.assertEqual(set(state._episode_buttons), {None, 1, 2, 3})

        panel.set_target(TvTarget(SHOW, 2, 1))
        await self.settle()
        self.assertEqual(panel.target, TvTarget(SHOW, 2, 1))
        self.assertEqual(changes, [TvTarget(SHOW, 2, 1)])
        self.assertEqual(search.requests[-1], ("episode", 2, 1))
        self.assertEqual(set(state._episode_buttons), {None, 1, 2})  # season 2's episodes
        self.assertEqual(library.episode_calls, [1])  # the same show: not loaded again

        state._episode_buttons[None].invoke()  # Pack
        await self.settle()
        self.assertEqual(changes[-1], TvTarget(SHOW, 2, None))
        self.assertEqual(search.requests[-1], ("season", 2))

        state._season_buttons[1].invoke()  # a pack stays a pack in another season
        await self.settle()
        self.assertEqual(changes[-1], TvTarget(SHOW, 1, None))
        state._episode_buttons[3].invoke()
        await self.settle()
        self.assertEqual(changes[-1], TvTarget(SHOW, 1, 3))
        self.assertEqual(search.requests[-1], ("episode", 1, 3))
        state._season_buttons[2].invoke()  # an episode becomes episode 1 of the new season
        await self.settle()
        self.assertEqual(changes[-1], TvTarget(SHOW, 2, 1))

        searches, told = len(search.requests), len(changes)
        panel.set_target(TvTarget(SHOW, 2, 1))  # the same target: nothing happens
        state._episode_buttons[1].invoke()
        await self.settle()
        self.assertEqual((len(search.requests), len(changes)), (searches, told))

    async def test_set_target_before_start_only_moves_the_chips(self) -> None:
        search = FakeSearch()
        changes: list[IndexerTarget] = []
        panel = self.panel(TvTarget(SHOW, 1, 1), search=search, on_target_change=changes.append)
        panel.set_target(TvTarget(SHOW, 3, 4))
        await self.settle()
        self.assertEqual(search.requests, [])
        self.assertEqual(changes, [TvTarget(SHOW, 3, 4)])
        state: Any = panel
        self.assertEqual(set(state._season_buttons), {3})  # the episodes aren't loaded yet
        self.assertEqual(set(state._episode_buttons), {None, 1, 2, 3, 4})
        panel.start()
        await self.settle()
        self.assertEqual(search.requests, [("episode", 3, 4)])

    async def test_set_target_to_another_show_and_to_a_film(self) -> None:
        search, discovery = FakeSearch(), FakeDiscovery()
        other = Show(id=2, name="Other")
        library = FakeLibrary({1: episodes(1, (1, 1)), 2: episodes(2, (1, 1), (5, 1))})
        panel = self.panel(
            TvTarget(SHOW, 1, 1), search=search, library=library, discovery=discovery
        )
        panel.start()
        await self.settle()
        state: Any = panel

        panel.set_target(TvTarget(other, 5, 1))
        await self.settle()
        self.assertEqual(library.episode_calls, [1, 2])
        self.assertEqual(set(state._season_buttons), {1, 5})

        panel.set_target(MovieTarget(FILM))
        await self.settle()
        self.assertEqual(search.requests[-1], ("movie",))
        self.assertEqual(discovery.calls, 1)
        self.assertEqual(state._season_buttons, {})
        self.assertEqual(state._season_row.winfo_manager(), "")
        self.assertEqual(state._quality, "1080p")  # the film quality

        panel.set_target(TvTarget(SHOW, 1, 1))  # and back: the rows return
        await self.settle()
        self.assertEqual(state._season_row.winfo_manager(), "grid")
        self.assertEqual(search.requests[-1], ("episode", 1, 1))

    async def test_destroy_cancels_the_search_and_the_grabs(self) -> None:
        film = release("Film.2024.1080p-GRP", size_bytes=GIB)
        search, downloads = FakeSearch((film,), block=True), FakeDownloads()
        panel = self.panel(MovieTarget(FILM), search=search, downloads=downloads)
        panel.start()
        await self.settle()
        state: Any = panel
        panel.show_message("kept")
        state._tree.selection_set(release_id(film))
        await self.settle()
        state._download_button.invoke()
        await self.settle()
        self.assertEqual(downloads.grabbed, [film.title])
        tasks = [state._search_task, *state._grab_tasks.values()]
        self.assertTrue(all(not task.done() for task in tasks))

        panel.destroy()
        await self.settle()
        self.assertTrue(all(task.cancelled() for task in tasks))
        self.assertTrue(search.cancelled)
        self.assertTrue(downloads.cancelled)

    async def test_fix_match_hands_the_host_a_target(self) -> None:
        library = FakeLibrary()
        library.tracked_ids = [SHOW.id]
        fixed: list[TvTarget] = []
        panel = self.panel(TvTarget(SHOW, 2, 3), library=library, on_fix_match=fixed.append)
        state: Any = panel
        labels = [
            child.cget("text")
            for child in state._season_row.winfo_children()
            if isinstance(child, ctk.CTkButton)
        ]
        self.assertEqual(labels, ["✎ Fix Match"])
        with mock.patch.object(panel, "_ask_tvmaze_id", mock.AsyncMock(return_value=" 42 ")):
            state._fix_match()
            await self.settle()
        self.assertEqual(library.track_calls, [42])
        self.assertEqual(library.tracked_ids, [42])  # the wrong show is untracked
        self.assertEqual(fixed, [TvTarget(Show(id=42, name="The Right Show"), 2, 3)])

    async def test_no_fix_match_link_without_a_host_callback(self) -> None:
        state: Any = self.panel(TvTarget(SHOW, 1, 1))
        buttons = [c for c in state._season_row.winfo_children() if isinstance(c, ctk.CTkButton)]
        self.assertEqual(buttons, [])

    async def test_selection_fills_the_bar_and_the_button_grabs(self) -> None:
        big = release("Film.2024.1080p-GRP", size_bytes=4 * GIB, seeders=40)
        small = release("Film.2024.720p-GRP", size_bytes=GIB, seeders=3)
        downloads = FakeDownloads()
        panel = self.panel(
            MovieTarget(FILM), search=FakeSearch((big, small)), downloads=downloads, shown=True
        )
        panel.start()
        bar: Any = panel  # the bar's widgets are private
        bar._select_quality("any")
        await self.settle()
        self.assertEqual(bar._tree.get_children(), (release_id(big), release_id(small)))
        self.assertEqual(bar._bar_caption.cget("text"), NO_SELECTION_TEXT)
        self.assertEqual(bar._download_button.cget("state"), "disabled")

        bar._tree.selection_set(release_id(small))
        await self.settle()
        self.assertEqual(bar._bar_caption.cget("text"), "Selected")
        self.assertEqual(bar._bar_name.cget("text"), small.title)
        self.assertEqual(bar._bar_detail.cget("text"), "720p · 1.00 GB · 3 seeders")
        self.assertEqual(bar._download_button.cget("state"), "normal")

        panel.focus_table()
        bar._tree.focus(release_id(small))
        bar._tree.event_generate("<Up>")  # keyboard selection moves the bar along
        await self.settle()
        self.assertEqual(bar._bar_name.cget("text"), big.title)

        bar._download_button.invoke()
        await self.settle()
        self.assertEqual(downloads.grabbed, [big.title])
        self.assertEqual(bar._download_button.cget("text"), DOWNLOADING_TEXT)
        self.assertEqual(bar._download_button.cget("state"), "disabled")
        downloads.release.set()
        await self.settle()
        self.assertEqual(bar._download_button.cget("text"), "✓ Saved")
        self.assertEqual(bar._tree.set(release_id(big), "action"), "✓")

        bar._tree.selection_set(release_id(small))  # another row: its own state
        await self.settle()
        self.assertEqual(bar._download_button.cget("text"), DOWNLOAD_TEXT)


if __name__ == "__main__":
    unittest.main()
