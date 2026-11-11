"""Where Discover's shows air: on their cards, and a menu that keeps the page to one service."""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from typing import Any

from app.models import TVDetails
from app.models.tmdb import TMDBTVDetails
from app.services.tmdb import TMDBError
from app.ui.media_cards import DiscoverShowCard, source_lines
from app.ui.views.discover_view import DiscoverView, service_names
from tests.test_quiet_reload import _Discovery, _NoImages, _PageCase

FIXTURES = Path(__file__).parent / "fixtures" / "tmdb"
NETWORKS = {1: ("Netflix",), 2: ("Netflix", "Hulu"), 3: ("Disney+",)}  # 4: TMDB can't tell


class SourcedDiscovery(_Discovery):
    async def tv_details(self, tmdb_id: int) -> TVDetails:
        if tmdb_id not in NETWORKS:
            raise TMDBError("no details")
        return TVDetails(id=tmdb_id, networks=NETWORKS[tmdb_id])


class PureTests(unittest.TestCase):
    def test_tmdbs_networks(self) -> None:
        raw = json.loads((FIXTURES / "tv_details.json").read_text(encoding="utf-8"))
        self.assertEqual(TMDBTVDetails.model_validate(raw).to_details().networks[0], "bilibili")

    def test_the_cards_lines_longest_first(self) -> None:
        self.assertEqual(
            source_lines(2024, 7.8, "Netflix"),
            ("2024 · ★ 7.8 · Netflix", "2024 · Netflix", "2024 · ★ 7.8"),
        )
        self.assertEqual(source_lines(None, None, "Netflix"), ("Netflix", "Netflix", ""))

    def test_the_most_shows_first_then_by_name(self) -> None:
        self.assertEqual(
            service_names([("Netflix",), ("Netflix", "Hulu"), ("Disney+",)]),
            ["Netflix", "Disney+", "Hulu"],
        )


class DiscoverSourceTests(_PageCase):
    async def page(self) -> DiscoverView:
        discovery: Any = SourcedDiscovery()
        images: Any = _NoImages()
        view = DiscoverView(
            self.root,
            discovery=discovery,
            images=images,
            status=lambda text, *, clear_after_ms=None: None,
            settings=self.settings,
            on_open_settings=lambda: None,
        )
        view.pack(fill="both", expand=True)
        await view.load()
        await self.settle(view)
        return view

    async def settle(self, view: DiscoverView) -> None:
        for _ in range(10):
            await asyncio.gather(*view._tasks, return_exceptions=True)
            await asyncio.sleep(0.01)
            self.root.update()

    def shown(self, view: DiscoverView) -> set[int]:
        return {
            key for key, cards in view._cards.items() if any(c.winfo_exists() for c in cards)
        }

    async def test_a_card_says_where_it_airs_and_the_menu_lists_the_services(self) -> None:
        view = await self.page()
        card: DiscoverShowCard = view._cards[1][0]
        self.assertIn("Netflix", card._meta_shown[0])
        menu = view.filter_bar.service_menu
        assert menu is not None
        self.assertEqual(list(menu.cget("values")), ["All services", "Netflix", "Disney+", "Hulu"])
        self.assertEqual(menu.get(), "All services")

    async def test_the_menu_keeps_the_page_to_one_service_and_back(self) -> None:
        view = await self.page()
        self.assertEqual(self.shown(view), {1, 2, 3, 4})
        view.filter_bar._service_picked("Hulu")
        await self.settle(view)
        self.assertEqual(self.shown(view), {2})  # any of a show's networks counts
        menu = view.filter_bar.service_menu
        assert menu is not None
        self.assertEqual(menu.get(), "Hulu")
        view.filter_bar._service_picked("All services")
        await self.settle(view)
        self.assertEqual(self.shown(view), {1, 2, 3, 4})  # 4, without a source, comes back

    async def test_the_service_survives_a_reload(self) -> None:
        view = await self.page()
        view.filter_bar._service_picked("Disney+")
        await self.settle(view)
        await view.load()
        await self.settle(view)
        self.assertEqual(self.shown(view), {3})

    async def test_nothing_from_that_service_says_so(self) -> None:
        view = await self.page()
        view._service = "Apple TV"
        self.assertIn("Nothing from Apple TV", view._empty_line(None))
        view._service = None
        self.assertNotIn("Nothing from", view._empty_line(None))
