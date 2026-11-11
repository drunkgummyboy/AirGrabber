"""The watchlist buttons: a click puts the film on the watchlist or takes it off again, and every
button follows the watchlist, so a film added or removed anywhere (another card, the watchlist
shelf, a download) repaints each one showing it; the hover tip always says what a click does.

`WatchlistStar`, the film cards' round ★ beside their Download row (a RoundButton): a filled
star in the accent colour while the film is on the list, an outlined one while it isn't
(`star_look`). `WatchlistButton`, the film window's wide button: "+ Watchlist", and "✓ On
watchlist" (green) once the film is on the list; where the full text doesn't fit, only
"+" / "✓".
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import customtkinter as ctk

from app.i18n import N_, t
from app.models import Movie
from app.ui import theme
from app.ui.card_scale import FULL, TEXT_MIN, CardScale
from app.ui.cards import IconPainter, RoundButton, star_icon
from app.ui.download_control import CORNER_RADIUS
from app.ui.download_control import ROW_HEIGHT as STAR_SIZE
from app.ui.tasks import spawn
from app.ui.text_fit import SLACK, Measure, unscaled_width
from app.ui.tooltip import add_tooltip

if TYPE_CHECKING:
    from app.services.watchlist import WatchlistService

WATCH_ROW_HEIGHT = 30  # WatchlistButton's height when none is given
FONT_SIZE = 13

# Shown through t() (`watch_look`).
ADD_TEXT = N_("+ Watchlist")
ADD_SHORT = "+"
ON_TEXT = N_("✓ On watchlist")
ON_SHORT = "✓"
ADD_TIP = N_("Add to your watchlist")
ON_TIP = N_("On your watchlist. Click to take it off.")


@dataclass(frozen=True, slots=True)
class WatchLook:
    texts: tuple[str, ...]  # longest first; the first that fits is shown
    color: theme.Color
    hover: theme.Color
    tip: str


def watch_look(on_watchlist: bool) -> WatchLook:
    """"+ Watchlist" in the accent colour, or "✓ On watchlist" in the downloaded green."""
    if on_watchlist:
        return WatchLook((t(ON_TEXT), ON_SHORT), theme.GRABBED, theme.ACCENT_HOVER, t(ON_TIP))
    return WatchLook((t(ADD_TEXT), ADD_SHORT), theme.ACCENT, theme.ACCENT_HOVER, t(ADD_TIP))


@dataclass(frozen=True, slots=True)
class StarLook:
    filled: bool
    color: theme.Color  # the star, and the ring around it
    tip: str


def star_look(on_watchlist: bool) -> StarLook:
    """A filled star in green, "already yours" as every card's (a click takes the film off), or
    an outlined one in the text colour (a click puts it on)."""
    if on_watchlist:
        return StarLook(True, theme.SUCCESS, t(ON_TIP))
    return StarLook(False, theme.TEXT, t(ADD_TIP))


def watch_text(texts: tuple[str, ...], room: float, measure: Measure) -> str:
    """The first of `texts` that fits in `room`; the shortest (a single sign) when none does,
    never elided."""
    return next((text for text in texts if measure(text) <= room), texts[-1])


def _toggle(movie: Movie, watchlist: WatchlistService) -> None:
    """Put `movie` on the watchlist, or take it off. The list the buttons show (and repaint
    with): the one in memory, the selected person's. Not cancelled with the button: the shelf
    may rebuild it mid-change, and the change itself touches no widget."""
    person = watchlist.profile_id
    if watchlist.contains(movie.id):
        spawn(watchlist.remove(movie.id, profile_id=person), name="watchlist-remove")
    else:
        spawn(watchlist.add(movie, profile_id=person), name="watchlist-add")


class WatchlistStar(RoundButton):
    """The film cards' round ★: puts `movie` on the watchlist or takes it off; repaints on
    every watchlist change. `size` (CTk units): the row's height at `scale` when not given."""

    def __init__(
        self,
        master: Any,
        *,
        movie: Movie,
        watchlist: WatchlistService,
        scale: CardScale = FULL,
        size: int | None = None,
    ) -> None:
        self._movie = movie
        self._watchlist = watchlist
        self._look: StarLook | None = None
        look = star_look(watchlist.contains(movie.id))
        super().__init__(
            master,
            size=size if size is not None else scale.px(STAR_SIZE),
            font=theme.font(scale.font(FONT_SIZE, TEXT_MIN), bold=True),
            command=self._click,
            icon=self._painter(look),
            text_color=look.color,
            edge=look.color if look.filled else None,
        )
        self._tip = add_tooltip(self, look.tip)
        self._look = look
        self._unsubscribe: Callable[[], None] = watchlist.subscribe(self.repaint)

    @staticmethod
    def _painter(look: StarLook) -> IconPainter:
        return star_icon(look.filled)

    @property
    def on_watchlist(self) -> bool:
        return self._watchlist.contains(self._movie.id)

    @property
    def look(self) -> StarLook | None:
        return self._look

    def repaint(self) -> None:
        if not self.winfo_exists():
            return
        look = star_look(self.on_watchlist)
        if look == self._look:
            return
        self._look = look
        self.set_look(
            icon=self._painter(look),
            text_color=look.color,
            edge=look.color if look.filled else theme.EDGE,
        )
        self._tip.set_text(look.tip)

    def _click(self) -> None:
        _toggle(self._movie, self._watchlist)

    def destroy(self) -> None:
        self._unsubscribe()
        super().destroy()


class WatchlistButton(ctk.CTkButton):
    """The film window's wide button: puts `movie` on the watchlist or takes it off; repaints
    on every watchlist change."""

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
        _toggle(self._movie, self._watchlist)

    def destroy(self) -> None:
        self._unsubscribe()
        super().destroy()
