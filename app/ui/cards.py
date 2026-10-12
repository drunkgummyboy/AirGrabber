"""Show cards for the Tracked grid and search results."""

from __future__ import annotations

import webbrowser
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal

import customtkinter as ctk

from app.models import Show
from app.ui import theme
from app.ui.widgets import PosterImage

if TYPE_CHECKING:
    from app.services.images import ImageCache

CardMode = Literal["library", "search"]
TrackState = Literal["idle", "busy", "tracked"]

CARD_WIDTH = 245
_TITLE_MAX = 20


def _truncate(text: str) -> str:
    return text if len(text) <= _TITLE_MAX else text[: _TITLE_MAX - 3] + "…"


class ShowCard(ctk.CTkFrame):
    """Poster + title + status. Library mode: IMDb / remove buttons; search mode: a Track button."""

    def __init__(
        self,
        master: Any,
        show: Show,
        *,
        images: ImageCache,
        mode: CardMode,
        is_tracked: bool = False,
        on_remove: Callable[[Show], None] | None = None,
        on_track: Callable[[Show], None] | None = None,
    ) -> None:
        search = mode == "search"
        super().__init__(
            master,
            fg_color=theme.SURFACE,
            border_color=theme.EDGE,
            border_width=1,
            corner_radius=8,
            width=CARD_WIDTH,
            height=135 if search else 120,
        )
        self.grid_propagate(False)
        self.pack_propagate(False)
        self._show = show
        self._mode = mode
        self._on_remove = on_remove
        self._on_track = on_track
        self._track_button: ctk.CTkButton | None = None

        poster = PosterImage(self, width=68, height=100)
        poster.pack(side="left", padx=10 if search else 8, pady=10)
        poster.show(images, show.poster_url)

        info = ctk.CTkFrame(self, fg_color="transparent")
        info.pack(side="left", fill="both", expand=True, padx=(0, 5), pady=10)

        ctk.CTkLabel(
            info,
            text=_truncate(show.name),
            font=theme.font(12, bold=True),
            text_color=theme.TEXT,
            anchor="w",
        ).pack(fill="x")
        if search:
            year = show.year or "Unknown"
            rating = f"{show.rating:.1f}" if show.rating is not None else "N/A"
            ctk.CTkLabel(
                info,
                text=f"{year} | ★ {rating}",
                font=theme.font(9),
                text_color=theme.TEXT_MUTED,
                anchor="w",
            ).pack(fill="x")
        ctk.CTkLabel(
            info,
            text=f"Status: {show.status or 'Unknown'}",
            font=theme.font(9),
            text_color="gray50",
            anchor="w",
        ).pack(fill="x")

        bottom = ctk.CTkFrame(info, fg_color="transparent")
        bottom.pack(side="bottom", fill="x")
        if search:
            self._track_button = ctk.CTkButton(
                bottom,
                height=22,
                font=theme.font(10, bold=True),
                corner_radius=4,
                command=self._track,
            )
            self._track_button.pack(fill="x")
            self.set_track_state("tracked" if is_tracked else "idle")
        else:
            ctk.CTkButton(
                bottom,
                text="remove",
                width=40,
                height=20,
                font=theme.font(10, bold=True),
                fg_color=theme.DANGER_BUTTON,
                hover_color=theme.DANGER_BUTTON_HOVER,
                border_width=0,
                command=self._remove,
            ).pack(side="right")
            if imdb_url := show.imdb_url:
                ctk.CTkButton(
                    bottom,
                    text="IMDb",
                    width=40,
                    height=20,
                    font=theme.font(10, bold=True),
                    fg_color=theme.IMDB,
                    text_color="black",
                    hover_color=theme.IMDB_HOVER,
                    border_width=0,
                    command=lambda: webbrowser.open(imdb_url),
                ).pack(side="right", padx=(0, 5))

    def set_track_state(self, state: TrackState) -> None:
        """Search mode only: idle → "+ Track", busy → "Adding…", tracked → "Tracking"."""
        button = self._track_button
        if button is None:
            raise RuntimeError("set_track_state() is only available on search-mode cards")
        if state == "idle":
            button.configure(
                text="+ Track",
                state="normal",
                fg_color=theme.ACCENT,
                hover_color=theme.ACCENT_HOVER,
            )
        elif state == "busy":
            button.configure(
                text="Adding…", state="disabled", fg_color=theme.ACCENT, hover_color=theme.ACCENT_HOVER
            )
        else:
            button.configure(text="Tracking", state="disabled", fg_color="transparent")

    def _remove(self) -> None:
        if self._on_remove is not None:
            self._on_remove(self._show)

    def _track(self) -> None:
        if self._on_track is not None:
            self._on_track(self._show)
