"""Cards for the calendar grid: tracked episodes (full and compact) and suggested shows."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import customtkinter as ctk

from app.models import CalendarEntry
from app.services.tvmaze import TVMazeError
from app.ui import theme
from app.ui.download_control import (
    ROW_SPACE,
    Base,
    DownloadActions,
    DownloadControl,
    episode_key,
)
from app.ui.media_cards import IMDB_ICON_ROOM, add_imdb_icon
from app.ui.tasks import spawn
from app.ui.text_fit import SLACK, elide_to_width, fit_to_lines, unscaled_width
from app.ui.widgets import PosterImage

if TYPE_CHECKING:
    from app.services import ImageCache, LibraryService
    from app.ui.views.base import StatusReporter

EpisodeActions = DownloadActions[CalendarEntry]

_NAME_MAX_LINES = 3  # full cards: room for three lines of name above the code
_POSTER_HEIGHT = 100
# Full cards: the poster (or the name and code) with 10 above it, then the Download row.
_CARD_HEIGHT = 10 + _POSTER_HEIGHT + ROW_SPACE
# Compact cards: a name line and a code line, then the Download row.
_COMPACT_NAME_HEIGHT = 20
_COMPACT_CODE_HEIGHT = 16
_COMPACT_HEIGHT = 4 + _COMPACT_NAME_HEIGHT + _COMPACT_CODE_HEIGHT + ROW_SPACE
# Suggestions: a "Suggested" line, the name, then the code with the + Track button.
_SUGGESTED_HEIGHT = 16
_SUGGESTION_PAD_Y = 6
_SUGGESTION_HEIGHT = (
    2 * _SUGGESTION_PAD_Y + _SUGGESTED_HEIGHT + _COMPACT_NAME_HEIGHT + 2 + _COMPACT_CODE_HEIGHT
)
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


def episode_base(entry: CalendarEntry) -> Base:
    """A disabled Not Aired; ✓ Grabbed (aired and downloaded); Search for an aired special,
    which has no episode number to download by; else the Download control."""
    if not entry.episode.has_aired():
        return Base("unavailable", "Not Aired")
    if entry.is_downloaded:
        return Base("grabbed")
    if entry.episode.number is None:
        return Base("search")
    return Base("download")


def _episode_control(
    card: ctk.CTkFrame, entry: CalendarEntry, actions: EpisodeActions
) -> DownloadControl[CalendarEntry]:
    """The card's Download row, packed at its bottom across its whole width (call this before
    packing anything else into the card)."""
    episode = entry.episode
    control = DownloadControl(
        card,
        item=entry,
        key=episode_key(entry.show.id, episode.season, episode.number),
        base=episode_base(entry),
        actions=actions,
    )
    control.pack_row()
    return control


# -- cards -----------------------------------------------------------------------------------


class EpisodeCard(ctk.CTkFrame):
    """A tracked episode with its show's poster (the current weeks)."""

    def __init__(
        self,
        master: Any,
        entry: CalendarEntry,
        *,
        images: ImageCache,
        actions: EpisodeActions,
    ) -> None:
        super().__init__(
            master,
            fg_color=theme.SURFACE,
            border_color=theme.EDGE,
            border_width=1,
            corner_radius=8,
            height=_CARD_HEIGHT,
        )
        self.pack_propagate(False)
        show = entry.show
        _episode_control(self, entry, actions)

        poster = PosterImage(self, width=68, height=_POSTER_HEIGHT)
        poster.pack(side="left", anchor="n", padx=10, pady=(10, 0))
        poster.show(images, show.poster_url)

        info = ctk.CTkFrame(self, fg_color="transparent")
        info.pack(side="left", fill="both", expand=True, padx=(0, 5), pady=(10, 0))

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
            self._poster.pack(side="left", anchor="n", padx=10, pady=(10, 0), before=self._info)
            self._info.pack_configure(padx=(0, 5))
        else:
            self._poster.pack_forget()
            self._info.pack_configure(padx=(10, 5))


class CompactEpisodeCard(ctk.CTkFrame):
    """A tracked episode in two short lines (older weeks), name and code, over its Download
    row."""

    def __init__(self, master: Any, entry: CalendarEntry, *, actions: EpisodeActions) -> None:
        super().__init__(
            master,
            fg_color=theme.SURFACE,
            border_color=theme.EDGE,
            border_width=1,
            corner_radius=8,
            height=_COMPACT_HEIGHT,
        )
        self.pack_propagate(False)
        show = entry.show
        _episode_control(self, entry, actions)

        inner = ctk.CTkFrame(self, fg_color="transparent")
        inner.pack(side="left", fill="both", expand=True, padx=8, pady=(4, 0))
        name_font = theme.font(11, bold=True)
        name = ctk.CTkLabel(
            inner,
            text=show.name,
            font=name_font,
            text_color=theme.TEXT,
            anchor="w",
            height=_COMPACT_NAME_HEIGHT,
        )
        name.pack(anchor="w")
        _keep_elided(name, show.name, name_font, reserve=IMDB_ICON_ROOM if show.imdb_url else 0)
        ctk.CTkLabel(
            inner,
            text=entry.episode.code,
            font=theme.font(9),
            text_color=theme.TEXT_MUTED,
            anchor="w",
            height=_COMPACT_CODE_HEIGHT,
        ).pack(anchor="w")

        add_imdb_icon(self, show.imdb_url)


class SuggestionCard(ctk.CTkFrame):
    """A popular untracked show airing that day, toned down below the tracked episodes, with a
    button to track it."""

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
            height=_SUGGESTION_HEIGHT,
        )
        self.pack_propagate(False)
        self._show = entry.show
        self._library = library
        self._status = status

        inner = ctk.CTkFrame(self, fg_color="transparent")
        inner.pack(side="left", fill="both", expand=True, padx=8, pady=_SUGGESTION_PAD_Y)
        ctk.CTkLabel(
            inner,
            text="Suggested",
            font=theme.font(11),
            text_color=theme.TEXT_MUTED,
            anchor="w",
            height=_SUGGESTED_HEIGHT,
        ).pack(anchor="w")
        name_font = theme.font(11, bold=True)
        name = ctk.CTkLabel(
            inner,
            text=entry.show.name,
            font=name_font,
            text_color=theme.TEXT_DIM,
            anchor="w",
            height=_COMPACT_NAME_HEIGHT,
        )
        name.pack(anchor="w")
        _keep_elided(name, entry.show.name, name_font)

        bottom = ctk.CTkFrame(inner, fg_color="transparent", height=_COMPACT_CODE_HEIGHT)
        bottom.pack(side="bottom", fill="x")
        ctk.CTkLabel(
            bottom,
            text=entry.episode.code,
            font=theme.font(9),
            text_color="gray40",
            height=_COMPACT_CODE_HEIGHT,
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
