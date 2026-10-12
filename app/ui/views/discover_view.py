"""TV discovery (TMDB): trending this week, currently airing, upcoming premieres."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.models import DiscoverShow, DiscoverShowEntry
from app.services import DiscoveryService, ImageCache
from app.services.discovery import NoTVMazeMatch
from app.services.tmdb import TMDBError
from app.services.tvmaze import TVMazeError
from app.ui import theme
from app.ui.media_cards import DiscoverShowCard, TrackState
from app.ui.views.base import StatusReporter
from app.ui.views.shelves import Shelf, ShelvesView


class DiscoverView(ShelvesView[DiscoverShowEntry]):
    """TMDB's TV shelves; "+ Track" adds a show to the library through its TVMaze counterpart."""

    def __init__(
        self,
        master: Any,
        *,
        discovery: DiscoveryService,
        images: ImageCache,
        status: StatusReporter,
        on_open_settings: Callable[[], None],
        **kwargs: Any,
    ) -> None:
        super().__init__(
            master, images=images, status=status, on_open_settings=on_open_settings, **kwargs
        )
        self._discovery = discovery
        # The cards on screen by TMDB id: a show can sit on more than one shelf.
        self._cards: dict[int, list[DiscoverShowCard]] = {}
        self._tracking: set[int] = set()  # TMDB ids being tracked right now

    async def _fetch_shelves(self, *, force: bool) -> list[Shelf[DiscoverShowEntry]]:
        dashboard = await self._discovery.tv_dashboard(force=force)
        failed = set(dashboard.failed)
        return [
            Shelf(
                "🔥",
                "Trending This Week",
                theme.SECTION_HOT,
                dashboard.trending,
                "trending" in failed,
            ),
            Shelf("📺", "Currently Airing", theme.TEXT_MUTED, dashboard.on_air, "on_air" in failed),
            Shelf(
                "🌟",
                "Upcoming Premieres",
                theme.SECTION_NEW,
                dashboard.premieres,
                "premieres" in failed,
            ),
        ]

    def _clear_body(self) -> None:
        super()._clear_body()
        self._cards.clear()

    def _card(self, master: Any, item: DiscoverShowEntry) -> None:
        show = item.show
        card = DiscoverShowCard(
            master,
            show,
            images=self._images,
            details=self._discovery.tv_details,
            is_tracked=item.is_tracked,
            on_track=self._track,
        )
        if show.id in self._tracking and not item.is_tracked:
            card.set_track_state("busy")
        self._cards.setdefault(show.id, []).append(card)

    # -- tracking ------------------------------------------------------------------------------

    def _track(self, show: DiscoverShow) -> None:
        if show.id in self._tracking:
            return
        self._tracking.add(show.id)
        self._set_track_state(show.id, "busy")
        self._start(self._track_show(show), "DiscoverView.track")

    async def _track_show(self, show: DiscoverShow) -> None:
        # On success the library notifies its listeners and the window reloads this page,
        # whose cards then come back flagged as tracked.
        try:
            await self._discovery.track(show)
        except NoTVMazeMatch:
            self._set_track_state(show.id, "missing")
            self._report(f"{show.name} isn't on TVMaze yet", clear_after_ms=6000)
        except (TMDBError, TVMazeError) as exc:
            self._set_track_state(show.id, "idle")
            self._report(f"Couldn't track {show.name}: {exc}", clear_after_ms=8000)
        else:
            self._set_track_state(show.id, "tracked")
            self._report(f"Now tracking {show.name}", clear_after_ms=4000)
        finally:
            self._tracking.discard(show.id)

    def _set_track_state(self, show_id: int, state: TrackState) -> None:
        for card in self._cards.get(show_id, ()):
            if card.winfo_exists():
                card.set_track_state(state)
