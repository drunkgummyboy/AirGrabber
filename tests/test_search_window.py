"""The search results window's own search field: a new search in the same window, Ctrl+F and
Esc (app/ui/search.py)."""

from __future__ import annotations

import asyncio
import gc
import tkinter as tk
import unittest
from typing import Any

import customtkinter as ctk

from app import i18n
from app.i18n import Language
from app.models import Movie, ShowSearchResult
from app.ui.download_control import DownloadActions
from app.ui.search import SearchResultsWindow


class FakeLibrary:
    """Each search waits until the test lets it finish (`finish`)."""

    def __init__(self) -> None:
        self.queries: list[str] = []
        self.gate = asyncio.Event()

    async def search(self, query: str, *, profile_id: int, limit: int) -> list[ShowSearchResult]:
        self.queries.append(query)
        await self.gate.wait()
        return []


class FakeDiscovery:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.gate = asyncio.Event()

    async def search_movies(self, query: str, *, limit: int) -> list[Movie]:
        self.queries.append(query)
        await self.gate.wait()
        return []


class SearchWindowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        gc.collect()
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        self.addCleanup(i18n.set_language, i18n.current_language())
        i18n.set_language(Language.EN)

    def _destroy_root(self) -> None:
        for child in self.root.winfo_children():
            if isinstance(child, tk.Toplevel):
                child.destroy()
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.tk.call("after", "cancel", job)
        self.root.destroy()
        gc.collect()

    async def asyncSetUp(self) -> None:
        asyncio.get_running_loop().slow_callback_duration = 2.0  # building windows is slow
        self.library = FakeLibrary()
        self.discovery = FakeDiscovery()

    def window(self, query: str = "dune") -> SearchResultsWindow:
        actions: DownloadActions[Movie] = DownloadActions(
            download=lambda movie: None,
            choose=lambda movie: None,
            states=None,  # type: ignore[arg-type]  # no film cards here
        )
        window = SearchResultsWindow(
            self.root,
            library=self.library,  # type: ignore[arg-type]
            images=None,  # type: ignore[arg-type]
            discovery=self.discovery,  # type: ignore[arg-type]
            status=lambda text, clear_after_ms=0: None,
            query=query,
            movie_actions=actions,
            watchlist=None,  # type: ignore[arg-type]
            on_open_settings=lambda: None,
            profile_id=1,
        )
        self.addCleanup(self._close, window)
        return window

    @staticmethod
    def _close(window: SearchResultsWindow) -> None:
        if window.winfo_exists():
            window.destroy()

    async def settle(self, rounds: int = 8) -> None:
        for _ in range(rounds):
            self.root.update()
            await asyncio.sleep(0.01)

    async def test_the_field_shows_the_query(self) -> None:
        window = self.window("dune")
        await self.settle()
        self.assertEqual(window.field.get(), "dune")
        self.assertEqual(window.title(), "Search Results: dune")
        self.assertEqual((self.library.queries, self.discovery.queries), (["dune"], ["dune"]))

    async def test_searching_again_in_the_same_window(self) -> None:
        window = self.window("dune")
        await self.settle()
        old = list(window._searches)
        self.assertEqual(len(old), 2)
        window.field.delete(0, "end")
        window.field.insert(0, "  alien ")
        window.field._entry.event_generate("<Return>")
        await self.settle()
        self.assertTrue(all(task.cancelled() for task in old))
        self.assertEqual(window.query, "alien")
        self.assertEqual(window.title(), "Search Results: alien")
        self.assertEqual(window.field.get(), "alien")
        self.assertEqual(self.library.queries, ["dune", "alien"])
        self.assertEqual(self.discovery.queries, ["dune", "alien"])
        self.assertIsNotNone(window._shows_loading)  # loading again
        self.assertIsNotNone(window._movies_loading)
        self.library.gate.set()
        self.discovery.gate.set()
        await self.settle()
        self.assertIsNone(window._shows_loading)  # the new searches finished
        self.assertIsNone(window._movies_loading)
        texts = [
            child.cget("text")
            for section in (window._shows_section, window._movies_section)
            for child in section.winfo_children()
            if isinstance(child, ctk.CTkLabel)
        ]
        self.assertEqual(texts, ["📺 TV Shows", "No TV shows found.", "🎬 Movies", "No movies found."])

    async def test_a_picked_suggestion_searches_in_the_window(self) -> None:
        window = self.window("dune")
        await self.settle()
        window._suggestions._choose("Dune: Prophecy")  # the field emptied, then the search
        await self.settle()
        self.assertEqual(window.query, "Dune: Prophecy")
        self.assertEqual(window.field.get(), "Dune: Prophecy")
        self.assertEqual(self.library.queries, ["dune", "Dune: Prophecy"])

    async def test_an_empty_query_is_ignored(self) -> None:
        window = self.window("dune")
        await self.settle()
        searches = list(window._searches)
        for query in ("", "   "):
            window.search(query)
        await self.settle()
        self.assertEqual(window._searches, searches)
        self.assertFalse(any(task.done() for task in searches))
        self.assertEqual(window.query, "dune")
        self.assertEqual(window.title(), "Search Results: dune")
        self.assertEqual(self.library.queries, ["dune"])

    async def test_ctrl_f_focuses_the_field(self) -> None:
        window = self.window("dune")
        await self.settle(30)  # shown and activated
        window.focus_force()
        await self.settle()
        window.field.select_clear()
        self.assertNotEqual(str(window.focus_get()), str(window.field._entry))
        self.assertFalse(window.field._entry.selection_present())
        window.event_generate("<Control-f>")
        await self.settle()
        self.assertEqual(str(window.focus_get()), str(window.field._entry))
        self.assertTrue(window.field._entry.selection_present())
        self.assertEqual(window.field._entry.selection_get(), "dune")

    async def test_esc_closes_the_suggestions_first(self) -> None:
        window = self.window("dune")
        await self.settle()
        hidden: list[bool] = []
        suggestions: Any = window._suggestions
        suggestions._visible = True
        suggestions.hide = lambda: hidden.append(True)
        self.assertEqual(window._on_field_escape(None), "break")  # type: ignore[arg-type]
        self.assertEqual(hidden, [True])
        self.assertTrue(window.winfo_exists())
        suggestions._visible = False
        self.assertIsNone(window._on_field_escape(None))  # type: ignore[arg-type]

    async def test_closing_cancels_the_searches(self) -> None:
        window = self.window("dune")
        await self.settle()
        searches = list(window._searches)
        window.destroy()
        await self.settle()
        self.assertTrue(all(task.cancelled() for task in searches))


if __name__ == "__main__":
    unittest.main()
