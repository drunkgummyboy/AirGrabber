"""The match picker (app/forge/ui/match_picker.py) on AirGrabber's dialog kit: the answers,
the TMDB search when the lookup had no candidates, thumbnails at physical size, and the pop-up
lessons of MediaForge's tests/ui/test_popups.py."""

from __future__ import annotations

import asyncio
import gc
import tkinter as tk
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import customtkinter as ctk
from PIL import Image

from app.forge.clients.tmdb import TmdbError
from app.forge.core.matcher import MAX_CANDIDATES, Resolution
from app.forge.core.queue import QueueItem
from app.forge.models.media import MatchState, MediaKind, ParsedMedia, ResolvedMedia
from app.forge.ui.match_picker import (
    OVERVIEW_MAX,
    MatchPicker,
    PickResult,
    short_overview,
)
from app.models.tmdb import TMDBTitle
from tests.forge.base import ForgeAsyncTestCase, ForgeTestCase
from tests.forge.tk import press, pump, pump_until

if TYPE_CHECKING:
    from app.forge.services import ForgeServices


class FakeImages:
    """ImageCache's `get`: the asked (url, size), a grey picture of that size; `gate` holds the
    answer back until it is set."""

    def __init__(self) -> None:
        self.asked: list[tuple[str, tuple[int, int]]] = []
        self.gate: asyncio.Event | None = None

    async def get(self, url: str | None, size: tuple[int, int], **_: object) -> Image.Image:
        assert url is not None
        self.asked.append((url, size))
        if self.gate is not None:
            await self.gate.wait()
        return Image.new("RGB", size, (90, 90, 90))


class FakeTmdb:
    def __init__(self, results: list[TMDBTitle] | None = None, *, fail: bool = False) -> None:
        self.results = results or []
        self.fail = fail
        self.searches: list[tuple[str, str, int | None]] = []

    def configured(self) -> bool:
        return True

    async def search_movie(self, query: str, year: int | None) -> list[TMDBTitle]:
        return self._search("movie", query, year)

    async def search_tv(self, query: str, year: int | None) -> list[TMDBTitle]:
        return self._search("tv", query, year)

    def _search(self, kind: str, query: str, year: int | None) -> list[TMDBTitle]:
        self.searches.append((kind, query, year))
        if self.fail:
            raise TmdbError("offline")
        return self.results


def title(n: int, *, poster: str | None = None) -> TMDBTitle:
    return TMDBTitle(
        id=n, title=f"Dune {n}", date="2021-09-15", overview="Sand.", poster_path=poster
    )


def make_item(
    folder: Path, kind: MediaKind = MediaKind.MOVIE, candidates: tuple[TMDBTitle, ...] = ()
) -> QueueItem:
    name = "Dune.2021.mkv" if kind is MediaKind.MOVIE else "Shrinking.S03E01.1080p.mkv"
    parsed = ParsedMedia(
        source_name=name,
        kind=kind,
        title="Dune" if kind is MediaKind.MOVIE else "Shrinking",
        year=2021 if kind is MediaKind.MOVIE else None,
        season=None if kind is MediaKind.MOVIE else 3,
        episode=None if kind is MediaKind.MOVIE else 1,
    )
    item = QueueItem(7, folder / name, None, parsed)
    if candidates:
        media = ResolvedMedia.offline(parsed, MatchState.UNCERTAIN)
        item.resolution = Resolution(media=media, candidates=candidates)
    return item


class PickerCase(ForgeAsyncTestCase):
    def setUp(self) -> None:
        super().setUp()
        gc.collect()
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        self.tmdb = FakeTmdb()
        self.images = FakeImages()
        self.services = cast("ForgeServices", SimpleNamespace(tmdb=self.tmdb, images=self.images))

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0  # Tk work in a test is slow

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    def popups(self) -> list[MatchPicker]:
        return [w for w in self.root.winfo_children() if isinstance(w, MatchPicker)]

    def ask(self, item: QueueItem) -> asyncio.Task[PickResult]:
        return asyncio.create_task(MatchPicker.ask(self.root, item, self.services))

    async def opened(self, rows: int = 0) -> MatchPicker:
        await pump_until(self.root, lambda: self.popups())
        [picker] = self.popups()
        await pump_until(self.root, lambda: len(self.rows(picker)) >= rows)
        picker.focus_force()
        await pump(self.root, 0.05)
        return picker

    def texts(self, widget: tk.Misc) -> list[str]:
        found: list[str] = []
        stack: list[tk.Misc] = [widget]
        while stack:
            current = stack.pop()
            if isinstance(current, ctk.CTkLabel):
                found.append(str(current.cget("text")))
            stack.extend(current.winfo_children())
        return found

    def rows(self, picker: MatchPicker) -> list[tk.Misc]:
        return [w for w in picker.list.winfo_children() if isinstance(w, ctk.CTkFrame)]

    def use_buttons(self, picker: MatchPicker) -> list[ctk.CTkButton]:
        found: list[ctk.CTkButton] = []
        for row in self.rows(picker):
            found += [w for w in row.winfo_children() if isinstance(w, ctk.CTkButton)]
        return found

    async def answer(self, task: asyncio.Task[PickResult]) -> PickResult:
        result = await asyncio.wait_for(task, 5)
        await pump(self.root, 0.02)
        self.assertEqual(self.popups(), [])
        return result


class AnswerTests(PickerCase):
    async def test_use_this_gives_that_candidate(self) -> None:
        candidates = tuple(title(n) for n in range(1, 4))
        task = self.ask(make_item(self.tmp, candidates=candidates))
        picker = await self.opened(rows=3)
        self.assertEqual(picker.title(), "Choose the right match")
        self.use_buttons(picker)[1].invoke()
        self.assertEqual(await self.answer(task), candidates[1])
        self.assertEqual(self.tmdb.searches, [])  # the lookup's candidates were enough

    async def test_at_most_eight_candidates(self) -> None:
        candidates = tuple(title(n) for n in range(1, 12))
        task = self.ask(make_item(self.tmp, candidates=candidates))
        picker = await self.opened(rows=MAX_CANDIDATES)
        self.assertEqual(len(self.rows(picker)), MAX_CANDIDATES)
        picker.top_button.invoke()
        self.assertEqual(await self.answer(task), candidates[0])

    async def test_skip_tmdb_gives_none(self) -> None:
        task = self.ask(make_item(self.tmp, candidates=(title(1),)))
        picker = await self.opened(rows=1)
        picker.skip_button.invoke()
        self.assertIsNone(await self.answer(task))

    async def test_escape_and_closing_cancel(self) -> None:
        task = self.ask(make_item(self.tmp, candidates=(title(1),)))
        picker = await self.opened(rows=1)
        press(picker, "<Escape>")
        self.assertEqual(await self.answer(task), "cancel")
        task = self.ask(make_item(self.tmp, candidates=(title(1),)))
        picker = await self.opened(rows=1)
        picker.destroy()  # gone some other way
        self.assertEqual(await self.answer(task), "cancel")

    async def test_cancelling_the_wait_closes_the_window(self) -> None:
        task = self.ask(make_item(self.tmp, candidates=(title(1),)))
        await self.opened(rows=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await pump(self.root, 0.02)
        self.assertEqual(self.popups(), [])

    async def test_never_transient(self) -> None:
        task = self.ask(make_item(self.tmp, candidates=(title(1),)))
        picker = await self.opened(rows=1)
        self.assertEqual(str(picker.wm_transient()), "")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


class SearchTests(PickerCase):
    async def test_searches_movies_without_candidates(self) -> None:
        self.tmdb.results = [title(5)]
        task = self.ask(make_item(self.tmp))
        picker = await self.opened(rows=1)
        self.assertEqual(self.tmdb.searches, [("movie", "Dune", 2021)])
        self.use_buttons(picker)[0].invoke()
        self.assertEqual(await self.answer(task), title(5))

    async def test_searches_shows_for_an_episode(self) -> None:
        self.tmdb.results = [title(6)]
        task = self.ask(make_item(self.tmp, MediaKind.EPISODE))
        picker = await self.opened(rows=1)
        self.assertEqual(self.tmdb.searches, [("tv", "Shrinking", None)])
        self.assertIn("Which show is this?", self.texts(picker))
        picker.skip_button.invoke()
        self.assertIsNone(await self.answer(task))

    async def test_nothing_found_disables_use_top_result(self) -> None:
        self.tmdb.fail = True  # a failed search is logged and shows as nothing found
        with self.assertLogs("app.forge.ui.match_picker", "WARNING") as logs:
            task = self.ask(make_item(self.tmp))
            picker = await self.opened()
            await pump_until(self.root, lambda: picker.list.winfo_children())
        self.assertNotIn("offline", "".join(logs.output))  # never the exception's text
        self.assertEqual(picker.top_button.cget("state"), "disabled")
        self.assertEqual(self.texts(picker.list)[-1:], ["TMDB found nothing for this file."])
        picker.top_button.invoke()  # does nothing
        await pump(self.root, 0.02)
        self.assertFalse(task.done())
        press(picker, "<Escape>")
        self.assertEqual(await self.answer(task), "cancel")


class ThumbnailTests(PickerCase):
    async def test_thumbnails_at_physical_size(self) -> None:
        candidates = (title(1, poster="/one.jpg"), title(2))
        task = self.ask(make_item(self.tmp, candidates=candidates))
        picker = await self.opened(rows=2)
        await pump_until(self.root, lambda: len(picker._images) == 1)
        scale = ctk.ScalingTracker.get_widget_scaling(picker)
        [(url, size)] = self.images.asked
        self.assertEqual(url, "https://image.tmdb.org/t/p/w154/one.jpg")
        self.assertEqual(size, (round(50 * scale), round(75 * scale)))
        picker.skip_button.invoke()
        await self.answer(task)

    async def test_a_late_thumbnail_after_closing_is_harmless(self) -> None:
        self.images.gate = asyncio.Event()
        task = self.ask(make_item(self.tmp, candidates=(title(1, poster="/one.jpg"),)))
        picker = await self.opened(rows=1)
        picker.skip_button.invoke()
        await self.answer(task)
        self.images.gate.set()
        await pump(self.root, 0.05)


class PopupLessonTests(PickerCase):
    async def test_picker_can_close_before_it_is_shown(self) -> None:
        # Regression (MediaForge): pop-ups destroyed before being shown left Tk in a state where
        # building the next widget crashed the whole process.
        for _ in range(3):
            MatchPicker(self.root, make_item(self.tmp), self.services).destroy()
            label = ctk.CTkLabel(self.root, text="still fine")
            label.pack()
            await pump(self.root, 0.05)
            label.destroy()


class OverviewTests(ForgeTestCase):
    def test_short_overview(self) -> None:
        self.assertEqual(short_overview("  Sand.  "), "Sand.")
        cut = short_overview("x" * 300)
        self.assertEqual(len(cut), OVERVIEW_MAX - 2)
        self.assertTrue(cut.endswith("…"))
