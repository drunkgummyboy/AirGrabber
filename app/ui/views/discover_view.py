"""TV discovery (TMDB): trending this week, currently airing, upcoming premieres."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from app.i18n import N_, t
from app.models import DiscoverShow, DiscoverShowEntry
from app.services import DiscoveryService, ImageCache, SettingsService
from app.services.discovery import NoTVMazeMatch
from app.services.tmdb import TMDBError
from app.services.tvmaze import TVMazeError
from app.ui import theme
from app.ui.card_scale import FULL, CardScale
from app.ui.media_cards import DiscoverShowCard
from app.ui.poster_card import TrackState
from app.ui.shelf_filter import HIDE_FOLLOWED_TEXT, TV_GENRES, Genre, genres_of
from app.ui.views.base import StatusReporter
from app.ui.views.shelves import Shelf, ShelvesView
from app.ui.widgets import FlexGrid


class DiscoverView(ShelvesView[DiscoverShowEntry]):
    """TMDB's TV shelves; "+ Track" adds a show to the library through its TVMaze counterpart.
    "Hide shows I follow" hides the shows the user follows (following, not having: it doesn't
    depend on Plex)."""

    _hide_owned_setting = "hide_owned_discover"
    _hide_owned_text = HIDE_FOLLOWED_TEXT
    _hide_owned_tip = N_("Hides the shows you already follow")
    _all_owned_text = N_("You already follow every show here.")

    def __init__(
        self,
        master: Any,
        *,
        discovery: DiscoveryService,
        images: ImageCache,
        status: StatusReporter,
        settings: SettingsService,
        on_open_settings: Callable[[], None],
        on_open_show: Callable[[DiscoverShow], None] | None = None,
        card_scale: Callable[[], CardScale] = lambda: FULL,
        **kwargs: Any,
    ) -> None:
        """`on_open_show`: what a click on a card's cover does (the show details).
        `card_scale`: the card size (Settings), read at every load."""
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
        self._on_open_show = on_open_show
        # The cards on screen by TMDB id: a show can sit on more than one shelf.
        self._cards: dict[int, list[DiscoverShowCard]] = {}
        self._tracking: set[int] = set()  # TMDB ids being tracked right now
        # The person whose "already following" marks the shelves show (read at every fetch):
        # a card's + Track adds to their library.
        self._profile_id = settings.current.current_profile

    async def _fetch_shelves(self, *, force: bool) -> list[Shelf[DiscoverShowEntry]]:
        profile_id = self._settings.current.current_profile
        dashboard = await self._discovery.tv_dashboard(profile_id=profile_id, force=force)
        self._profile_id = profile_id
        failed = set(dashboard.failed)
        return [
            Shelf(
                "🔥",
                t("Trending This Week"),
                theme.SECTION_HOT,
                dashboard.trending,
                "trending" in failed,
            ),
            Shelf(
                "📺", t("Currently Airing"), theme.TEXT_MUTED, dashboard.on_air, "on_air" in failed
            ),
            Shelf(
                "🌟",
                t("Upcoming Premieres"),
                theme.SECTION_NEW,
                dashboard.premieres,
                "premieres" in failed,
            ),
        ]

    def _genres(self, item: DiscoverShowEntry) -> frozenset[Genre]:
        return genres_of(item.show.genre_ids, TV_GENRES)

    def _is_owned(self, item: DiscoverShowEntry) -> bool:
        return item.is_tracked

    def _item_key(self, item: DiscoverShowEntry) -> object:
        return item.show

    def _owned_changed(self, items: Sequence[DiscoverShowEntry]) -> bool:
        """Followed or no longer followed (a quiet reload): the cards' Track buttons follow;
        with "Hide shows I follow" on, a newly followed show's cards go. False when a
        shelf would be left empty or a card would have to come back."""
        for item in items:
            if self._hide_owned:
                if not item.is_tracked or not self._remove_cards(item.show.id):
                    return False
            elif item.show.id not in self._tracking:
                self._set_track_state(item.show.id, "tracked" if item.is_tracked else "idle")
        return True

    def _remove_cards(self, show_id: int) -> bool:
        """Take a show's cards off their shelves, which close up. False (nothing removed) when
        that would leave a shelf without cards."""
        cards = [card for card in self._cards.get(show_id, ()) if card.winfo_exists()]
        grids = {
            str(card.master): card.master for card in cards if isinstance(card.master, FlexGrid)
        }
        for grid in grids.values():
            left = [child for child in grid.winfo_children() if child not in cards]
            if not left:
                return False
        for card in cards:
            card.destroy()
        self._cards.pop(show_id, None)
        for grid in grids.values():
            grid.relayout()
        return True

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
            on_open_show=self._on_open_show,
            scale=self._scale,
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
            await self._discovery.track(show, profile_id=self._profile_id)
        except NoTVMazeMatch:
            self._set_track_state(show.id, "missing")
            self._report(t("{name} isn't on TVMaze yet", name=show.name), clear_after_ms=6000)
        except (TMDBError, TVMazeError) as exc:
            self._set_track_state(show.id, "idle")
            self._report(
                t("Couldn't track {name}: {error}", name=show.name, error=exc),
                clear_after_ms=8000,
            )
        else:
            self._set_track_state(show.id, "tracked")
            self._report(t("Now tracking {name}", name=show.name), clear_after_ms=4000)
        finally:
            self._tracking.discard(show.id)

    def _set_track_state(self, show_id: int, state: TrackState) -> None:
        for card in self._cards.get(show_id, ()):
            if card.winfo_exists():
                card.set_track_state(state)
