"""Movie discovery: the user's watchlist first (stored data, no TMDB needed), then TMDB's
shelves: just dropped digitally, trending, upcoming digital releases."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import customtkinter as ctk

from app.models import Movie
from app.services import DiscoveryService, ImageCache, WatchlistService
from app.ui import theme
from app.ui.card_scale import FULL, CardScale
from app.ui.download_control import DownloadActions
from app.ui.media_cards import MovieCard
from app.ui.views.base import StatusReporter
from app.ui.views.shelves import Shelf, ShelvesView
from app.ui.watchlist_cards import WatchlistShelf
from app.ui.widgets import destroy_tree


class MovieView(ShelvesView[Movie]):
    """The watchlist shelf and TMDB's film shelves under a header with a Refresh button.

    The watchlist shelf stays across reloads (and while TMDB loads or fails) and follows the
    watchlist by itself; it hides while the list is empty.
    """

    _body_pady = (0, 5)

    def __init__(
        self,
        master: Any,
        *,
        discovery: DiscoveryService,
        images: ImageCache,
        status: StatusReporter,
        on_open_settings: Callable[[], None],
        movie_actions: DownloadActions[Movie],
        watchlist: WatchlistService,
        card_scale: Callable[[], CardScale] = lambda: FULL,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            master,
            images=images,
            status=status,
            on_open_settings=on_open_settings,
            card_scale=card_scale,
            **kwargs,
        )
        self._discovery = discovery
        self._movie_actions = movie_actions  # the cards' Download and ⋯ buttons
        self._watchlist = watchlist
        self.watchlist_shelf = WatchlistShelf(
            self._body,
            watchlist=watchlist,
            images=images,
            actions=movie_actions,
            card_scale=card_scale,
        )

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=15, pady=5)
        ctk.CTkLabel(
            header, text="Movie Discovery", font=theme.font(22, bold=True), text_color=theme.TEXT
        ).pack(side="left")
        ctk.CTkButton(
            header,
            text="↻ Refresh",
            width=90,
            height=28,
            fg_color="transparent",
            hover_color=theme.SURFACE,
            border_width=1,
            border_color=theme.EDGE,
            command=self.reload,
        ).pack(side="right")

    async def _fetch_shelves(self, *, force: bool) -> list[Shelf[Movie]]:
        dashboard = await self._discovery.movie_dashboard(force=force)
        failed = set(dashboard.failed)
        return [
            Shelf(
                "📀",
                "Just Dropped (Digital)",
                theme.SECTION_NEW,
                dashboard.just_dropped,
                "just_dropped" in failed,
            ),
            Shelf(
                "🔥", "Trending Movies", theme.SECTION_HOT, dashboard.trending, "trending" in failed
            ),
            Shelf(
                "📅",
                "Upcoming Digital",
                theme.TEXT_MUTED,
                dashboard.upcoming,
                "upcoming" in failed,
            ),
        ]

    async def load(self) -> None:
        self.watchlist_shelf.start()  # shown at once: stored data, no TMDB needed
        await super().load()

    def _clear_body(self) -> None:
        for child in self._body.winfo_children():
            if child is not self.watchlist_shelf:
                destroy_tree(child)

    def _card(self, master: Any, item: Movie) -> None:
        MovieCard(
            master,
            item,
            images=self._images,
            details=self._discovery.movie_details,
            actions=self._movie_actions,
            watchlist=self._watchlist,
            scale=self._scale,
        )
