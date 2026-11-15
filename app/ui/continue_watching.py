"""The Library page's "Continue watching" row (app/ui/views/library_view.py, above its tabs):
what Plex says to go on with (PlexLibraryService.continue_watching), at most CONTINUE_SIZE cards
in Plex's order, side by side in a strip that scrolls sideways (its scrollbar only while they
don't fit; Shift + the wheel scrolls it too).

A card is the page's poster card at the smallest card size (ROW_SIZE, so the row leaves the
grid room): the show's or film's poster (PlexService.poster through the page's ImageCache, at
physical pixels like every PosterCard), its title, an episode's "S02E03 · Title" or a film's
year, and the episode picker's thin progress line (`watched_share`: begun and below 90 %). A
click plays it where Plex left it, without the Resume prompt (`on_play`: play_control's
`Play.resume`). The row hides itself when it has nothing to show (`show([])`).
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Sequence
from typing import Any

import customtkinter as ctk

from app.i18n import N_, t
from app.models import CardSize
from app.models.plex import PlexContinue
from app.services import ImageCache
from app.services.plex import CONTINUE_SIZE, poster_url
from app.services.plex_library import continue_line
from app.ui import theme
from app.ui.card_scale import CardScale
from app.ui.play_control import PROGRESS_HEIGHT, watched_share
from app.ui.poster_card import PosterCard, card_size, row_height
from app.ui.scrollable import ScrollableFrame

TITLE = N_("Continue watching")
ROW_SIZE = CardSize.XS  # the row's cards: the smallest, so the grid below keeps its room
CARD_GAP = 8  # between two cards
STRIP_SLACK = 4  # the strip's height beyond a card's


class ContinueCard(PosterCard):
    """One thing to go on with (see the module docstring); `progress` is its progress line
    (None when Plex has no place in it)."""

    def __init__(
        self,
        master: Any,
        item: PlexContinue,
        *,
        posters: ImageCache,
        scale: CardScale,
        on_play: Callable[[PlexContinue], object],
    ) -> None:
        url = poster_url(item.title)
        super().__init__(
            master,
            title=item.title.title,
            meta=continue_line(item),
            poster=lambda _width: url,
            images=posters,
            # No action row: the progress line's room instead, so every card's text lines up.
            extra_height=PROGRESS_HEIGHT - row_height(scale),
            scale=scale,
            meta_row=True,
        )
        self.item = item
        self._set_open(lambda: on_play(item))
        self.progress: ctk.CTkProgressBar | None = None
        share = watched_share(item.video)
        if share is not None:  # the episode picker's line
            bar = ctk.CTkProgressBar(
                self,
                height=PROGRESS_HEIGHT,
                corner_radius=1,
                border_width=0,
                fg_color=theme.PLACEHOLDER,
                progress_color=theme.ACCENT,
            )
            bar.set(share)
            self._pack_action(bar)
            self.progress = bar


class ContinueRow(ctk.CTkFrame):
    """The heading and the strip of cards (see the module docstring). Placed by its page;
    `show` fills it, and hides it (grid_remove) when there is nothing."""

    def __init__(
        self,
        master: Any,
        *,
        posters: ImageCache,
        on_play: Callable[[PlexContinue], object],
    ) -> None:
        super().__init__(master, fg_color="transparent")
        self._posters = posters
        self._on_play = on_play
        self._scale = CardScale.of(ROW_SIZE)
        self._items: tuple[PlexContinue, ...] = ()
        self.cards: list[ContinueCard] = []
        self.grid_columnconfigure(0, weight=1)
        self.heading = ctk.CTkLabel(
            self, text=t(TITLE), font=theme.font(14, bold=True), text_color=theme.TEXT, anchor="w"
        )
        self.heading.grid(row=0, column=0, sticky="w", padx=3, pady=(0, 4))
        self._strip = ScrollableFrame(
            self,
            orientation="horizontal",
            height=card_size(self._scale)[1] + STRIP_SLACK,
            corner_radius=0,
            fg_color="transparent",
            scrollbar_button_color=theme.SURFACE,
            scrollbar_button_hover_color=theme.EDGE,
        )
        self._strip.grid(row=1, column=0, sticky="ew")
        self._strip.bind("<Configure>", lambda _event: self._fit_scrollbar(), add="+")
        self._strip._parent_canvas.bind(
            "<Configure>", lambda _event: self._fit_scrollbar(), add="+"
        )
        self._strip._scrollbar.grid_remove()

    @property
    def shown(self) -> bool:
        """The row is on its page (it has cards)."""
        return bool(self.winfo_manager())

    def show(self, items: Sequence[PlexContinue]) -> None:
        """Cards for `items` (the first CONTINUE_SIZE, in their order), made anew only when
        they changed; none hides the row."""
        items = tuple(items[:CONTINUE_SIZE])
        if not items:
            self._clear()
            self.grid_remove()
            return
        if items != self._items or not self.cards:
            self._clear()
            self._items = items
            for column, item in enumerate(items):
                card = ContinueCard(
                    self._strip, item, posters=self._posters, scale=self._scale,
                    on_play=self._on_play,
                )
                card.grid(row=0, column=column, padx=(0 if column == 0 else CARD_GAP, 0))
                self.cards.append(card)
        self.grid()
        self.after_idle(self._fit_scrollbar)

    def _clear(self) -> None:
        for card in self.cards:
            card.destroy()
        self.cards = []
        self._items = ()

    def _fit_scrollbar(self) -> None:
        """The strip's scrollbar only while its cards don't fit (as the release panel's rows)."""
        try:
            strip = self._strip
            width = strip._parent_canvas.winfo_width()
            overflow = width > 1 and strip.winfo_reqwidth() > width
            bar = strip._scrollbar
            if overflow and not bar.winfo_manager():
                bar.grid()
            elif not overflow and bar.winfo_manager():
                bar.grid_remove()
        except tk.TclError:
            pass  # going
