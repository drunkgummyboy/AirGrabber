"""Movie discovery: the user's watchlist first (stored data, no TMDB needed), then TMDB's
shelves: just dropped digitally, trending, upcoming digital releases."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import customtkinter as ctk

from app.i18n import N_, t
from app.models import Movie
from app.services import DiscoveryService, ImageCache, SettingsService, WatchlistService
from app.ui import theme
from app.ui.card_scale import FULL, CardScale
from app.ui.download_control import DownloadActions
from app.ui.media_cards import MovieCard
from app.ui.shelf_filter import MOVIE_GENRES, Genre, genres_of
from app.ui.views.base import StatusReporter
from app.ui.views.shelves import Shelf, ShelvesView
from app.ui.watchlist_cards import WatchlistShelf
from app.ui.widgets import destroy_tree


class MovieView(ShelvesView[Movie]):
    """The watchlist shelf and TMDB's film shelves under a header with a Refresh button.

    The watchlist shelf stays across reloads (and while TMDB loads or fails) and follows the
    watchlist by itself; it hides while the list is empty. The filter bar leaves it alone: it
    filters TMDB's shelves only, and "Hide what I already have" (offered only while Plex is
    connected: app/services/plex.py, the only thing that counts as had) hides the films in
    Plex (their cards' quiet ✓).
    """

    _body_pady = (0, 5)
    _hide_owned_setting = "hide_owned_movies"
    _hide_owned_tip = N_("Hides the films you have in Plex (your watchlist always stays)")
    _all_owned_text = N_("You already have every film here.")
    # Whether Plex is connected, as the last load found it (the hide switch is offered); read
    # by the base class while it builds the filter bar, before the first load.
    _plex_connected = False

    def __init__(
        self,
        master: Any,
        *,
        discovery: DiscoveryService,
        images: ImageCache,
        status: StatusReporter,
        settings: SettingsService,
        on_open_settings: Callable[[], None],
        movie_actions: DownloadActions[Movie],
        watchlist: WatchlistService,
        card_scale: Callable[[], CardScale] = lambda: FULL,
        on_open_movie: Callable[[Movie], None] | None = None,
        **kwargs: Any,
    ) -> None:
        """`on_open_movie`: what a click on a film card's cover or title does (its info
        window)."""
        super().__init__(
            master,
            images=images,
            status=status,
            settings=settings,
            on_open_settings=on_open_settings,
            card_scale=card_scale,
            **kwargs,
        )
        self._discovery = discovery
        self._movie_actions = movie_actions  # the cards' Download and ⋯ buttons
        self._watchlist = watchlist
        self._on_open_movie = on_open_movie
        self._in_plex: frozenset[int] = frozenset()  # the last load's films in Plex
        self.watchlist_shelf = WatchlistShelf(
            self._body,
            watchlist=watchlist,
            images=images,
            actions=movie_actions,
            card_scale=card_scale,
            on_open=on_open_movie,
            plex=discovery.plex_films,
        )

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=15, pady=5)
        ctk.CTkLabel(
            header, text=t("Movie Discovery"), font=theme.font(22, bold=True), text_color=theme.TEXT
        ).pack(side="left")
        ctk.CTkButton(
            header,
            text=t("↻ Refresh"),
            width=90,
            height=28,
            fg_color="transparent",
            hover_color=theme.SURFACE,
            text_color=theme.BUTTON_TEXT,
            border_width=1,
            border_color=theme.EDGE,
            command=self.reload,
        ).pack(side="right")

    async def _fetch_shelves(self, *, force: bool) -> list[Shelf[Movie]]:
        dashboard = await self._discovery.movie_dashboard(force=force)
        self._plex_connected = dashboard.plex_connected
        self._in_plex = dashboard.in_plex
        failed = set(dashboard.failed)
        return [
            Shelf(
                "📀",
                t("Just Dropped (Digital)"),
                theme.SECTION_NEW,
                dashboard.just_dropped,
                "just_dropped" in failed,
            ),
            Shelf(
                "🔥",
                t("Trending Movies"),
                theme.SECTION_HOT,
                dashboard.trending,
                "trending" in failed,
            ),
            Shelf(
                "📅",
                t("Upcoming Digital"),
                theme.TEXT_MUTED,
                dashboard.upcoming,
                "upcoming" in failed,
            ),
        ]

    async def load(self) -> None:
        self.watchlist_shelf.start()  # shown at once: stored data, no TMDB needed
        await super().load()

    def _genres(self, item: Movie) -> frozenset[Genre]:
        return genres_of(item.genre_ids, MOVIE_GENRES)

    def _is_owned(self, item: Movie) -> bool:
        """In Plex, the only thing that counts as had (app/services/plex.py)."""
        return item.id in self._in_plex

    def _hide_owned_offered(self) -> bool:
        """Only with Plex: without it nothing counts as had, so there is nothing to hide."""
        return self._plex_connected

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
            on_open=self._on_open_movie,
            in_plex=item.id in self._in_plex,
        )
