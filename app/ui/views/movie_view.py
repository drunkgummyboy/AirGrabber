"""Movie discovery (TMDB): just dropped digitally, trending, upcoming digital releases."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import customtkinter as ctk

from app.models import Movie
from app.services import DiscoveryService, ImageCache
from app.ui import theme
from app.ui.media_cards import MovieCard
from app.ui.views.base import StatusReporter
from app.ui.views.shelves import Shelf, ShelvesView


class MovieView(ShelvesView[Movie]):
    """TMDB's film shelves under a header with a Refresh button."""

    _body_pady = (0, 5)

    def __init__(
        self,
        master: Any,
        *,
        discovery: DiscoveryService,
        images: ImageCache,
        status: StatusReporter,
        on_open_settings: Callable[[], None],
        on_grab: Callable[[Movie], None],
        **kwargs: Any,
    ) -> None:
        super().__init__(
            master, images=images, status=status, on_open_settings=on_open_settings, **kwargs
        )
        self._discovery = discovery
        self._on_grab = on_grab

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

    def _card(self, master: Any, item: Movie) -> None:
        MovieCard(
            master,
            item,
            images=self._images,
            details=self._discovery.movie_details,
            on_grab=self._on_grab,
        )
