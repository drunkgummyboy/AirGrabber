"""The indexer dialog's pure helpers (table text, row ids, sorting, chips, header lines, the
selection bar's text) and its selection bar on a hidden Tk root with fake services."""

from __future__ import annotations

import asyncio
import tkinter as tk
import unittest
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import customtkinter as ctk

from app.models import (
    GrabOutcome,
    GrabResult,
    GrabTarget,
    IndexerState,
    IndexerStatus,
    Movie,
    MovieDetails,
    SearchUpdate,
    Settings,
    Show,
    TorrentRelease,
)
from app.services import (
    DownloadService,
    ImageCache,
    LibraryService,
    SettingsService,
    TorrentSearchService,
)
from app.services.discovery import DiscoveryService
from app.ui import theme
from app.ui.indexer_dialog import (
    DOWNLOAD_TEXT,
    DOWNLOADING_TEXT,
    NO_SELECTION_TEXT,
    IndexerDialog,
    MovieTarget,
    SortColumn,
    action_text,
    chip_style,
    compact_header,
    download_button_text,
    episode_title,
    failure_text,
    format_age,
    format_size,
    hidden_text,
    movie_meta,
    movie_title,
    next_sort,
    quality_button_text,
    release_id,
    searching_text,
    selection_detail,
    show_meta,
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


class CompactHeaderTests(unittest.TestCase):
    def test_short_dialogs_get_the_compact_header(self) -> None:
        self.assertTrue(compact_header(600))  # the minimum size
        self.assertTrue(compact_header(699.5))
        self.assertFalse(compact_header(700))
        self.assertFalse(compact_header(750))  # the default size keeps the full header
        self.assertFalse(compact_header(1000))


class FakeSearch:
    def __init__(self, releases: tuple[TorrentRelease, ...]) -> None:
        self._releases = releases

    def movie_request(self, movie: Movie, details: MovieDetails | None = None) -> object:
        return object()

    async def search(self, request: object) -> AsyncIterator[SearchUpdate]:
        yield SearchUpdate(statuses=(), releases=self._releases, done=True)


class FakeDownloads:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.grabbed: list[str] = []

    async def grabbed_hashes(self) -> frozenset[str]:
        return frozenset()

    async def grab(self, item: TorrentRelease, target: GrabTarget) -> GrabResult:
        self.grabbed.append(item.title)
        await self.release.wait()
        return GrabResult(outcome=GrabOutcome.SAVED, message="Saved the torrent")


class FakeDiscovery:
    async def movie_details(self, movie_id: int) -> MovieDetails:
        return MovieDetails(id=movie_id)


class FakeSettings:
    current = Settings()


def _no_status(text: str, *, clear_after_ms: int | None = None) -> None:
    pass


class SelectionBarTests(unittest.IsolatedAsyncioTestCase):
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

    async def _settle(self) -> None:
        for _ in range(10):
            await asyncio.sleep(0)
        self.root.update()

    async def test_selection_fills_the_bar_and_the_button_grabs(self) -> None:
        big = release("Film.2024.1080p-GRP", size_bytes=4 * GIB, seeders=40)
        small = release("Film.2024.720p-GRP", size_bytes=GIB, seeders=3)
        downloads = FakeDownloads()
        dialog = IndexerDialog(
            self.root,
            target=MovieTarget(Movie(id=7, title="Film")),
            search=cast(TorrentSearchService, FakeSearch((big, small))),
            downloads=cast(DownloadService, downloads),
            library=cast(LibraryService, object()),
            discovery=cast(DiscoveryService, FakeDiscovery()),
            images=cast(ImageCache, object()),  # never used: the film has no poster
            settings=cast(SettingsService, FakeSettings()),
            status=_no_status,
            on_reopen=lambda _target: None,
        )
        self.addCleanup(dialog.destroy)
        bar: Any = dialog  # the bar's widgets are private
        dialog._select_quality("any")
        await self._settle()
        self.assertEqual(dialog._tree.get_children(), (release_id(big), release_id(small)))
        self.assertEqual(bar._bar_caption.cget("text"), NO_SELECTION_TEXT)
        self.assertEqual(bar._download_button.cget("state"), "disabled")

        dialog._tree.selection_set(release_id(small))
        await self._settle()
        self.assertEqual(bar._bar_caption.cget("text"), "Selected")
        self.assertEqual(bar._bar_name.cget("text"), small.title)
        self.assertEqual(bar._bar_detail.cget("text"), "720p · 1.00 GB · 3 seeders")
        self.assertEqual(bar._download_button.cget("state"), "normal")

        dialog._tree.focus_set()
        dialog._tree.focus(release_id(small))
        dialog._tree.event_generate("<Up>")  # keyboard selection moves the bar along
        await self._settle()
        self.assertEqual(bar._bar_name.cget("text"), big.title)

        bar._download_button.invoke()
        await self._settle()
        self.assertEqual(downloads.grabbed, [big.title])
        self.assertEqual(bar._download_button.cget("text"), DOWNLOADING_TEXT)
        self.assertEqual(bar._download_button.cget("state"), "disabled")
        downloads.release.set()
        await self._settle()
        self.assertEqual(bar._download_button.cget("text"), "✓ Saved")
        self.assertEqual(dialog._tree.set(release_id(big), "action"), "✓")

        dialog._tree.selection_set(release_id(small))  # another row: its own state
        await self._settle()
        self.assertEqual(bar._download_button.cget("text"), DOWNLOAD_TEXT)


class HeaderTests(unittest.TestCase):
    show = Show.model_validate(
        {
            "id": 1,
            "name": "Severance",
            "premiered": "2022-02-18",
            "genres": ["Drama", "Mystery"],
            "status": "Running",
        }
    )
    movie = Movie.model_validate(
        {
            "id": 2,
            "title": "Dune",
            "release_date": "2021-09-15",
            "vote_average": 7.8,
            "vote_count": 9,
        }
    )

    def test_titles(self) -> None:
        self.assertEqual(episode_title(self.show, 1, 5), "Severance: S01E05")
        self.assertEqual(episode_title(self.show, 1, None), "Severance: Season 01 (Pack)")
        self.assertEqual(movie_title(self.movie), "Dune (2021)")
        self.assertEqual(movie_title(Movie(id=3, title="Untitled")), "Untitled")

    def test_show_meta(self) -> None:
        self.assertEqual(show_meta(self.show), "2022 • Drama, Mystery • RUNNING")
        self.assertEqual(show_meta(Show(id=4, name="Bare")), "")

    def test_movie_meta(self) -> None:
        self.assertEqual(movie_meta(self.movie, None), "2021 • ★ 7.8")
        details = MovieDetails(id=2, certification="PG-13", runtime=155)
        self.assertEqual(
            movie_meta(self.movie, details), "2021 • ★ 7.8 • Rated PG-13 • 2h 35m"
        )
        self.assertEqual(movie_meta(Movie(id=5, title="New"), MovieDetails(id=5)), "")


if __name__ == "__main__":
    unittest.main()
