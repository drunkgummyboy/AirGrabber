"""Cards for the calendar grid: tracked episodes (full and compact) and suggested shows."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import customtkinter as ctk

from app.models import CalendarEntry
from app.services.tvmaze import TVMazeError
from app.ui import theme
from app.ui.media_cards import IMDB_ICON_ROOM, add_imdb_icon
from app.ui.tasks import spawn
from app.ui.text_fit import SLACK, elide_to_width, fit_to_lines, unscaled_width
from app.ui.widgets import PosterImage

if TYPE_CHECKING:
    from app.services import ImageCache, LibraryService
    from app.ui.views.base import StatusReporter

SearchEpisode = Callable[[CalendarEntry], None]

_NAME_MAX_LINES = 3  # full cards: room for three lines of name above the code and the button
# Full cards narrower than this (small windows) drop the poster so the name, code and button fit.
_POSTER_MIN_CARD_WIDTH = 190


# -- shared parts ----------------------------------------------------------------------------


def _keep_elided(label: ctk.CTkLabel, text: str, font: ctk.CTkFont, *, reserve: float = 0) -> None:
    """Show `text` on one line in `label`, elided to its parent's width less `reserve`."""

    def fit(event: Any) -> None:
        if event.width > 1:
            width = unscaled_width(label, event.width) - reserve - SLACK
            label.configure(text=elide_to_width(text, width, font.measure))

    label.master.bind("<Configure>", fit)


def _episode_button(
    master: Any, entry: CalendarEntry, on_search: SearchEpisode, **options: Any
) -> ctk.CTkButton:
    """Search (aired), ✓ Grabbed (aired and downloaded) or a disabled Not Aired.

    `options` override the size and font (default: height 22, font 10 bold).
    """
    if not entry.episode.has_aired():
        text, color, hover, state = "Not Aired", theme.NOT_AIRED, theme.NOT_AIRED, "disabled"
    elif entry.is_downloaded:
        text, color, hover, state = "✓ Grabbed", theme.GRABBED, theme.ACCENT_HOVER, "normal"
    else:
        text, color, hover, state = "Search", theme.ACCENT, theme.ACCENT_HOVER, "normal"
    style: dict[str, Any] = {
        "height": 22,
        "font": theme.font(10, bold=True),
        "corner_radius": 4,
        "border_width": 0,
        **options,
    }
    return ctk.CTkButton(
        master,
        text=text,
        fg_color=color,
        hover_color=hover,
        state=state,
        command=lambda: on_search(entry),
        **style,
    )


# -- cards -----------------------------------------------------------------------------------


class EpisodeCard(ctk.CTkFrame):
    """A tracked episode with its show's poster (the current weeks)."""

    def __init__(
        self,
        master: Any,
        entry: CalendarEntry,
        *,
        images: ImageCache,
        on_search: SearchEpisode,
    ) -> None:
        super().__init__(
            master,
            fg_color=theme.SURFACE,
            border_color=theme.EDGE,
            border_width=1,
            corner_radius=8,
            height=120,
        )
        self.pack_propagate(False)
        show = entry.show

        poster = PosterImage(self, width=68, height=100)
        poster.pack(side="left", padx=10, pady=10)
        poster.show(images, show.poster_url)

        info = ctk.CTkFrame(self, fg_color="transparent")
        info.pack(side="left", fill="both", expand=True, padx=(0, 5), pady=10)
        _episode_button(info, entry, on_search).pack(side="bottom", fill="x")

        text = ctk.CTkFrame(info, fg_color="transparent")
        text.pack(side="top", fill="both", expand=True, padx=(0, 25))
        name_font = theme.font(12, bold=True)
        name = ctk.CTkLabel(
            text,
            text=show.name,
            font=name_font,
            text_color=theme.TEXT,
            wraplength=100,  # until the first layout reports the real width
            justify="left",
            anchor="nw",
        )
        name.pack(anchor="nw")
        ctk.CTkLabel(
            text,
            text=entry.episode.code,
            font=theme.font(9),
            text_color=theme.TEXT_MUTED,
        ).pack(anchor="nw", pady=(2, 0))

        def wrap(event: Any) -> None:
            # Wrap at the width the column really leaves; a name needing more than three
            # lines gets its last line elided so the code and the button still fit.
            if event.width > 1:
                width = unscaled_width(name, event.width) - SLACK
                name.configure(
                    wraplength=max(1, round(width)),
                    text=fit_to_lines(show.name, width, _NAME_MAX_LINES, name_font.measure),
                )

        text.bind("<Configure>", wrap)
        add_imdb_icon(self, show.imdb_url)

        self._poster = poster
        self._info = info
        self._poster_shown = True
        self.bind("<Configure>", self._fit_poster, add="+")

    def _fit_poster(self, event: Any) -> None:
        if event.width <= 1:
            return
        wide = unscaled_width(self, event.width) >= _POSTER_MIN_CARD_WIDTH
        if wide == self._poster_shown:
            return
        self._poster_shown = wide
        if wide:
            self._poster.pack(side="left", padx=10, pady=10, before=self._info)
            self._info.pack_configure(padx=(0, 5))
        else:
            self._poster.pack_forget()
            self._info.pack_configure(padx=(10, 5))


class CompactEpisodeCard(ctk.CTkFrame):
    """A tracked episode in two short lines (older weeks): name; code and button."""

    def __init__(self, master: Any, entry: CalendarEntry, *, on_search: SearchEpisode) -> None:
        super().__init__(
            master,
            fg_color=theme.SURFACE,
            border_color=theme.EDGE,
            border_width=1,
            corner_radius=8,
            height=55,
        )
        self.pack_propagate(False)
        show = entry.show

        inner = ctk.CTkFrame(self, fg_color="transparent")
        inner.pack(side="left", fill="both", expand=True, padx=8, pady=4)
        name_font = theme.font(11, bold=True)
        name = ctk.CTkLabel(
            inner, text=show.name, font=name_font, text_color=theme.TEXT, anchor="w"
        )
        name.pack(anchor="w")
        _keep_elided(name, show.name, name_font, reserve=IMDB_ICON_ROOM if show.imdb_url else 0)

        bottom = ctk.CTkFrame(inner, fg_color="transparent")
        bottom.pack(side="bottom", fill="x")
        ctk.CTkLabel(
            bottom, text=entry.episode.code, font=theme.font(9), text_color=theme.TEXT_MUTED
        ).pack(side="left")
        _episode_button(
            bottom, entry, on_search, width=52, height=18, font=theme.font(9, bold=True)
        ).pack(side="right")

        add_imdb_icon(self, show.imdb_url)


class SuggestionCard(ctk.CTkFrame):
    """A popular untracked show airing that day, with a button to track it."""

    def __init__(
        self,
        master: Any,
        entry: CalendarEntry,
        *,
        library: LibraryService,
        status: StatusReporter,
    ) -> None:
        super().__init__(
            master,
            fg_color=theme.SUGGESTION_BG,
            border_color=theme.SUGGESTION_BORDER,
            border_width=1,
            corner_radius=8,
            height=55,
        )
        self.pack_propagate(False)
        self._show = entry.show
        self._library = library
        self._status = status

        inner = ctk.CTkFrame(self, fg_color="transparent")
        inner.pack(side="left", fill="both", expand=True, padx=8, pady=4)
        name_font = theme.font(11, bold=True)
        name = ctk.CTkLabel(
            inner, text=entry.show.name, font=name_font, text_color="gray50", anchor="w"
        )
        name.pack(anchor="w")
        _keep_elided(name, entry.show.name, name_font)

        bottom = ctk.CTkFrame(inner, fg_color="transparent")
        bottom.pack(side="bottom", fill="x")
        ctk.CTkLabel(
            bottom, text=entry.episode.code, font=theme.font(9), text_color="gray40"
        ).pack(side="left")
        self._track_button = ctk.CTkButton(
            bottom,
            text="+ Track",
            height=16,
            width=45,
            font=theme.font(9),
            fg_color="transparent",
            border_width=1,
            border_color="gray30",
            text_color="gray60",
            hover_color=theme.SUGGESTION_BORDER,
            command=self._track,
        )
        self._track_button.pack(side="right")

    def _track(self) -> None:
        self._track_button.configure(text="…", state="disabled")
        spawn(self._track_show(), name="SuggestionCard.track")

    async def _track_show(self) -> None:
        # On success the library notifies its listeners and the window reloads the calendar,
        # which replaces this card with the show's tracked episode.
        try:
            await self._library.track(self._show.id)
        except TVMazeError as exc:
            if self._track_button.winfo_exists():
                self._track_button.configure(text="+ Track", state="normal")
            self._status(f"Couldn't track {self._show.name}: {exc}", clear_after_ms=6000)
