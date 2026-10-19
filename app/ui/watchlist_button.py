"""The movie cards' watchlist button: "+ Watchlist", and "✓ On watchlist" (green) once the film
is on the list; a click puts it on or takes it off again. Every button follows the watchlist,
so a film added or removed anywhere (another card, the watchlist shelf, a download) repaints
each card showing it. Where the full text doesn't fit, the button shows only "+" / "✓"; its
hover tip always says what a click does.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import customtkinter as ctk

from app.models import Movie
from app.ui import theme
from app.ui.card_scale import FULL, TEXT_MIN, CardScale
from app.ui.download_control import CORNER_RADIUS
from app.ui.tasks import spawn
from app.ui.text_fit import SLACK, Measure, unscaled_width
from app.ui.tooltip import add_tooltip

if TYPE_CHECKING:
    from app.services.watchlist import WatchlistService

WATCH_ROW_HEIGHT = 30  # a little lower than the Download row, so the cover keeps its room
WATCH_ROW_GAP = 8  # between the watchlist row and the Download row under it
FONT_SIZE = 13

ADD_TEXT = "+ Watchlist"
ADD_SHORT = "+"
ON_TEXT = "✓ On watchlist"
ON_SHORT = "✓"
ADD_TIP = "Add to your watchlist"
ON_TIP = "On your watchlist. Click to take it off."


@dataclass(frozen=True, slots=True)
class WatchLook:
    texts: tuple[str, ...]  # longest first; the first that fits is shown
    color: str
    hover: str
    tip: str


def watch_look(on_watchlist: bool) -> WatchLook:
    """"+ Watchlist" in the accent colour, or "✓ On watchlist" in the downloaded green."""
    if on_watchlist:
        return WatchLook((ON_TEXT, ON_SHORT), theme.GRABBED, theme.ACCENT_HOVER, ON_TIP)
    return WatchLook((ADD_TEXT, ADD_SHORT), theme.ACCENT, theme.ACCENT_HOVER, ADD_TIP)


def watch_text(texts: tuple[str, ...], room: float, measure: Measure) -> str:
    """The first of `texts` that fits in `room`; the shortest (a single sign) when none does,
    never elided."""
    return next((text for text in texts if measure(text) <= room), texts[-1])


class WatchlistButton(ctk.CTkButton):
    """Puts `movie` on the watchlist or takes it off; repaints on every watchlist change."""

    def __init__(
        self,
        master: Any,
        *,
        movie: Movie,
        watchlist: WatchlistService,
        width: int = 0,
        height: int | None = None,
        scale: CardScale = FULL,
    ) -> None:
        """`width`: a fixed width (a narrow one shows only "+" / "✓"); 0 = as packed. `height`:
        None = WATCH_ROW_HEIGHT at `scale` (the card size); `width` and `height` are taken as
        given."""
        self._font = theme.font(scale.font(FONT_SIZE, TEXT_MIN), bold=True)
        self._corner = scale.px(CORNER_RADIUS)
        super().__init__(
            master,
            text="",
            width=width or 28,
            height=height if height is not None else scale.px(WATCH_ROW_HEIGHT),
            corner_radius=self._corner,
            font=self._font,
            border_width=0,
            command=self._click,
        )
        self._movie = movie
        self._watchlist = watchlist
        self._look: WatchLook | None = None
        self._width: float | None = float(width) if width else None
        self._tip = add_tooltip(self, "")
        self.bind("<Configure>", self._on_resize, add="+")
        self._unsubscribe: Callable[[], None] = watchlist.subscribe(self.repaint)
        self.repaint()

    @property
    def on_watchlist(self) -> bool:
        return self._watchlist.contains(self._movie.id)

    def repaint(self) -> None:
        if not self.winfo_exists():
            return
        look = watch_look(self.on_watchlist)
        if look == self._look:
            return
        self._look = look
        self.configure(fg_color=look.color, hover_color=look.hover)
        self._tip.set_text(look.tip)
        self._fit_text()

    def _on_resize(self, event: Any) -> None:
        if event.width > 1:
            self._width = unscaled_width(self, event.width)
            self._fit_text()

    def _fit_text(self) -> None:
        look = self._look
        if look is None:
            return
        text = look.texts[0]
        if self._width is not None:
            room = self._width - 2 * self._corner - SLACK
            text = watch_text(look.texts, room, self._font.measure)
        self.configure(text=text)

    def _click(self) -> None:
        movie, watchlist = self._movie, self._watchlist
        # Not cancelled with the button: the shelf may rebuild it mid-change, and the change
        # itself touches no widget.
        if watchlist.contains(movie.id):
            spawn(watchlist.remove(movie.id), name="watchlist-remove")
        else:
            spawn(watchlist.add(movie), name="watchlist-add")

    def destroy(self) -> None:
        self._unsubscribe()
        super().destroy()
