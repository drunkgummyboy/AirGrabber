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
    """`by_query` answers a query with its own results; `gates` holds a query's answer back
    until its event is set."""

    def __init__(self, results: list[TMDBTitle] | None = None, *, fail: bool = False) -> None:
        self.results = results or []
        self.fail = fail
        self.ready = True
        self.by_query: dict[str, list[TMDBTitle]] = {}
        self.gates: dict[str, asyncio.Event] = {}
        self.searches: list[tuple[str, str, int | None]] = []

    def configured(self) -> bool:
        return self.ready

    async def search_movie(self, query: str, year: int | None) -> list[TMDBTitle]:
        return await self._search("movie", query, year)

    async def search_tv(self, query: str, year: int | None) -> list[TMDBTitle]:
        return await self._search("tv", query, year)

    async def _search(self, kind: str, query: str, year: int | None) -> list[TMDBTitle]:
        self.searches.append((kind, query, year))
        gate = self.gates.get(query)
        if gate is not None:
            await gate.wait()
        if self.fail:
            raise TmdbError("offline")
        return self.by_query.get(query, self.results)


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


def ark(n: int) -> TMDBTitle:
    return TMDBTitle(id=100 + n, title=f"The Ark {n}", date="2023-02-01", overview="Space.")


def ark_item(folder: Path, kind: MediaKind = MediaKind.EPISODE) -> QueueItem:
    """The owner's case: a wrong parsed year and four unrelated candidates."""
    name = "The.Ark.2026.S03E01.WEB-DL.1080p.mkv"
    parsed = ParsedMedia(
        source_name=name,
        kind=kind,
        title="The Ark",
        year=2026,
        season=3 if kind is MediaKind.EPISODE else None,
        episode=1 if kind is MediaKind.EPISODE else None,
    )
    item = QueueItem(9, folder / name, None, parsed)
    media = ResolvedMedia.offline(parsed, MatchState.UNCERTAIN)
    item.resolution = Resolution(media=media, candidates=tuple(title(n) for n in range(1, 5)))
    return item


class ManualSearchTests(PickerCase):
    async def search(self, picker: MatchPicker, query: str | None = None) -> None:
        entry = picker.search_entry
        assert entry is not None
        if query is not None:
            entry.delete(0, "end")
            entry.insert(0, query)
        entry.focus_force()
        entry._entry.event_generate("<Return>")

    async def settled(self, picker: MatchPicker) -> None:
        """Every search started so far has finished."""
        searches = [t for t in picker._tasks if t.get_name() == "MatchPicker.search"]
        self.assertTrue(searches)
        await pump_until(self.root, lambda: all(t.done() for t in searches))
        await pump(self.root, 0.02)

    async def test_prefilled_without_the_year_and_enter_searches(self) -> None:
        self.tmdb.results = [ark(1), ark(2)]
        task = self.ask(ark_item(self.tmp))
        picker = await self.opened(rows=4)
        assert picker.search_entry is not None
        self.assertEqual(picker.search_entry.get(), "The Ark")
        await self.search(picker)
        await self.settled(picker)
        self.assertEqual(self.tmdb.searches, [("tv", "The Ark", None)])
        self.assertEqual(len(self.rows(picker)), 2)  # replaced, not added
        self.assertIn("The Ark 1 (2023)", self.texts(picker.list))
        self.assertNotIn("Dune 1 (2021)", self.texts(picker.list))
        self.assertEqual(picker.candidates, [ark(1), ark(2)])
        self.use_buttons(picker)[1].invoke()
        self.assertEqual(await self.answer(task), ark(2))

    async def test_a_year_at_the_end_is_passed_as_the_year(self) -> None:
        self.tmdb.results = [ark(1)]
        task = self.ask(ark_item(self.tmp))
        picker = await self.opened(rows=4)
        await self.search(picker, "The Ark 2023")
        await self.settled(picker)
        await self.search(picker, "  The Ark (2023) ")
        await self.settled(picker)
        await self.search(picker, "Blade Runner 2049 x")
        await self.settled(picker)
        self.assertEqual(
            self.tmdb.searches,
            [
                ("tv", "The Ark", 2023),
                ("tv", "The Ark", 2023),
                ("tv", "Blade Runner 2049 x", None),
            ],
        )
        picker.skip_button.invoke()
        await self.answer(task)

    async def test_a_movie_searches_movies_and_the_button_searches_too(self) -> None:
        self.tmdb.results = [ark(3)]
        task = self.ask(ark_item(self.tmp, MediaKind.MOVIE))
        picker = await self.opened(rows=4)
        assert picker.search_button is not None
        picker.search_button.invoke()
        await self.settled(picker)
        self.assertEqual(self.tmdb.searches, [("movie", "The Ark", None)])
        picker.top_button.invoke()  # the new top
        self.assertEqual(await self.answer(task), ark(3))

    async def test_searching_shows_and_disables(self) -> None:
        gate = asyncio.Event()
        self.tmdb.gates["The Ark"] = gate
        self.tmdb.results = [ark(1)]
        task = self.ask(ark_item(self.tmp))
        picker = await self.opened(rows=4)
        await self.search(picker)
        await pump_until(self.root, lambda: "Searching…" in self.texts(picker.list))
        assert picker.search_button is not None
        self.assertEqual(picker.search_button.cget("state"), "disabled")
        self.assertEqual(picker.search_button.cget("text"), "Searching…")
        self.assertEqual(picker.top_button.cget("state"), "disabled")
        self.assertEqual(self.rows(picker), [])
        gate.set()
        await self.settled(picker)
        self.assertEqual(picker.search_button.cget("text"), "🔍 Search")
        self.assertEqual(picker.top_button.cget("state"), "normal")
        picker.skip_button.invoke()
        await self.answer(task)

    async def test_nothing_found(self) -> None:
        task = self.ask(ark_item(self.tmp))
        picker = await self.opened(rows=4)
        await self.search(picker, "Zzz")
        await self.settled(picker)
        self.assertEqual(self.rows(picker), [])
        self.assertEqual(self.texts(picker.list), ['Nothing found for "Zzz".'])
        self.assertEqual(picker.top_button.cget("state"), "disabled")
        self.assertEqual(picker.candidates, [])
        press(picker, "<Escape>")
        self.assertEqual(await self.answer(task), "cancel")

    async def test_tmdb_error(self) -> None:
        task = self.ask(ark_item(self.tmp))
        picker = await self.opened(rows=4)
        self.tmdb.fail = True
        with self.assertLogs("app.forge.ui.match_picker", "WARNING") as logs:
            await self.search(picker, "Secret Query")
            await self.settled(picker)
        self.assertNotIn("offline", "".join(logs.output))
        self.assertNotIn("Secret", "".join(logs.output))
        self.assertEqual(self.texts(picker.list), ["TMDB couldn't be reached. Try again."])
        self.assertEqual(picker.top_button.cget("state"), "disabled")
        press(picker, "<Escape>")
        await self.answer(task)

    async def test_an_empty_query_does_nothing(self) -> None:
        task = self.ask(ark_item(self.tmp))
        picker = await self.opened(rows=4)
        await self.search(picker, "   ")
        await pump(self.root, 0.05)
        self.assertEqual(self.tmdb.searches, [])
        self.assertEqual(len(self.rows(picker)), 4)
        picker.skip_button.invoke()
        await self.answer(task)

    async def test_a_newer_search_wins(self) -> None:
        slow = asyncio.Event()
        self.tmdb.gates["Old"] = slow
        self.tmdb.by_query = {"Old": [title(9)], "New": [ark(1)]}
        task = self.ask(ark_item(self.tmp))
        picker = await self.opened(rows=4)
        await self.search(picker, "Old")
        await pump_until(self.root, lambda: len(self.tmdb.searches) == 1)
        await self.search(picker, "New")  # the button is disabled meanwhile; Enter still works
        await pump_until(self.root, lambda: picker.candidates == [ark(1)])
        slow.set()
        await pump(self.root, 0.1)
        self.assertEqual(picker.candidates, [ark(1)])
        self.assertIn("The Ark 1 (2023)", self.texts(picker.list))
        self.assertNotIn("Dune 9 (2021)", self.texts(picker.list))
        picker.top_button.invoke()
        self.assertEqual(await self.answer(task), ark(1))

    async def test_no_search_row_without_tmdb(self) -> None:
        self.tmdb.ready = False
        task = self.ask(ark_item(self.tmp))
        picker = await self.opened(rows=4)
        self.assertIsNone(picker.search_entry)
        self.assertIsNone(picker.search_button)
        stack: list[tk.Misc] = [picker]
        while stack:
            current = stack.pop()
            self.assertNotIsInstance(current, ctk.CTkEntry)
            stack.extend(current.winfo_children())
        picker.skip_button.invoke()
        await self.answer(task)


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
