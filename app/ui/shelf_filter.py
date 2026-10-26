"""The filter bar above the Discover and Movies shelves: genre chips and "Hide what I already have".

TMDB tags every title in its lists with genre ids, and films and shows use different ids for
the same kind of story. `MOVIE_GENRES` and `TV_GENRES` map both onto one short list of plain
names (`Genre`); ids outside it (Thriller, Mystery, Reality, News...) get no chip. The bar only
offers the genres that at least one title on the page has (`chip_genres`).

Picking a chip or flipping the switch only filters what the page already loaded: nothing is
fetched again. The page remembers its chip for the session and its switch in Settings.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from enum import StrEnum
from typing import Any

import customtkinter as ctk

from app.i18n import N_, t
from app.ui import theme
from app.ui.tooltip import add_tooltip


class Genre(StrEnum):
    """The genre chips, in the order the bar shows them; the value is the chip's text in
    English (`genre_name`: as shown)."""

    ACTION = "Action"
    COMEDY = "Comedy"
    DRAMA = "Drama"
    ANIMATION = "Animation"
    DOCUMENTARY = "Documentary"
    FAMILY = "Kids & Family"
    CRIME = "Crime"
    SCI_FI = "Sci-Fi & Fantasy"
    HORROR = "Horror"


# TMDB's film genres (/genre/movie/list).
MOVIE_GENRES: Mapping[int, Genre] = {
    28: Genre.ACTION,
    35: Genre.COMEDY,
    18: Genre.DRAMA,
    16: Genre.ANIMATION,
    99: Genre.DOCUMENTARY,
    10751: Genre.FAMILY,
    80: Genre.CRIME,
    878: Genre.SCI_FI,  # Science Fiction
    14: Genre.SCI_FI,  # Fantasy
    27: Genre.HORROR,
}

# TMDB's TV genres (/genre/tv/list); TV has no horror genre.
TV_GENRES: Mapping[int, Genre] = {
    10759: Genre.ACTION,  # Action & Adventure
    35: Genre.COMEDY,
    18: Genre.DRAMA,
    16: Genre.ANIMATION,
    99: Genre.DOCUMENTARY,
    10751: Genre.FAMILY,
    10762: Genre.FAMILY,  # Kids
    80: Genre.CRIME,
    10765: Genre.SCI_FI,  # Sci-Fi & Fantasy
}

# The chips' texts, shown through t() (`genre_name`).
_GENRE_NAMES: Mapping[Genre, str] = {
    Genre.ACTION: N_("Action"),
    Genre.COMEDY: N_("Comedy"),
    Genre.DRAMA: N_("Drama"),
    Genre.ANIMATION: N_("Animation"),
    Genre.DOCUMENTARY: N_("Documentary"),
    Genre.FAMILY: N_("Kids & Family"),
    Genre.CRIME: N_("Crime"),
    Genre.SCI_FI: N_("Sci-Fi & Fantasy"),
    Genre.HORROR: N_("Horror"),
}

ALL_TEXT = N_("All")
HIDE_OWNED_TEXT = N_("Hide what I already have")

CHIP_HEIGHT = 28
CHIP_FONT_SIZE = 13
CHIP_PAD = 12  # inside a chip, left and right of its text
CHIP_GAP = 6  # between two chips
SWITCH_GAP = 16  # between the chips and the switch when they share a line
SWITCH_SIZE = (36, 18)  # the switch's track


def genre_name(genre: Genre) -> str:
    """The chip's text: "Comedy" / "Komedie"."""
    return t(_GENRE_NAMES[genre])


def genres_of(genre_ids: Iterable[int], table: Mapping[int, Genre]) -> frozenset[Genre]:
    """The chips a title with TMDB's `genre_ids` belongs to (`table`: MOVIE_GENRES or
    TV_GENRES)."""
    return frozenset(genre for genre_id in genre_ids if (genre := table.get(genre_id)))


def chip_genres(titles: Iterable[frozenset[Genre]], selected: Genre | None) -> tuple[Genre, ...]:
    """The genres the bar offers, in `Genre` order: those at least one title has, and the
    selected one even when no title has it any more (so it can be switched off again)."""
    present = {genre for genres in titles for genre in genres}
    if selected is not None:
        present.add(selected)
    return tuple(genre for genre in Genre if genre in present)


def empty_text(genre: Genre | None, hide_owned: bool, all_owned: str) -> str:
    """The page's one line when the filter left every shelf empty. `all_owned`: what the page
    says when only "Hide what I already have" did it ("You already follow every show here.")."""
    if genre is None:
        return all_owned
    if hide_owned:
        return t(
            "Nothing new in {genre} here right now. Pick another genre or {all}.",
            genre=genre_name(genre),
            all=t(ALL_TEXT),
        )
    return t(
        "Nothing in {genre} here right now. Pick another genre or {all}.",
        genre=genre_name(genre),
        all=t(ALL_TEXT),
    )


def chip_color(selected: bool) -> tuple[theme.Color, theme.Color]:
    """A chip's fill and hover colours: the accent when selected (as the show window's season
    chips)."""
    if selected:
        return theme.ACCENT, theme.ACCENT_HOVER
    return theme.CHIP, theme.CHIP_HOVER


class FilterBar(ctk.CTkFrame):
    """"All" and the page's genre chips on the left, the "Hide what I already have" switch on
    the right (under the chips when the line is too narrow for both).

    A click on a chip selects it at once and calls `on_genre` (None = All); the switch calls
    `on_hide_owned`. The page hands the bar its chips with `show_genres`.
    """

    def __init__(
        self,
        master: Any,
        *,
        hide_owned: bool,
        hide_tip: str,
        on_genre: Callable[[Genre | None], None],
        on_hide_owned: Callable[[bool], None],
    ) -> None:
        super().__init__(master, fg_color="transparent")
        self._on_genre = on_genre
        self._on_hide_owned = on_hide_owned
        self._genres: tuple[Genre, ...] | None = None
        self._selected: Genre | None = None
        self._chips: dict[Genre | None, ctk.CTkButton] = {}
        self._chip_font = theme.font(CHIP_FONT_SIZE)
        self._wrapped: bool | None = None  # the switch is on a line of its own

        self.grid_columnconfigure(1, weight=1)
        self._chip_row = ctk.CTkFrame(self, fg_color="transparent")
        self._chip_row.grid(row=0, column=0, sticky="w")
        self._hide_var = ctk.BooleanVar(value=hide_owned)
        switch_w, switch_h = SWITCH_SIZE
        self._switch = ctk.CTkSwitch(
            self,
            text=t(HIDE_OWNED_TEXT),
            variable=self._hide_var,
            font=theme.font(CHIP_FONT_SIZE),
            text_color=theme.TEXT_MUTED,
            switch_width=switch_w,
            switch_height=switch_h,
            progress_color=theme.ACCENT,
            command=self._hide_toggled,
        )
        add_tooltip(self._switch, hide_tip)
        self._place_switch(wrapped=False)
        self.bind("<Configure>", self._on_resize, add="+")

    @property
    def genres(self) -> tuple[Genre, ...]:
        """The genre chips on show (after "All")."""
        return self._genres or ()

    @property
    def selected(self) -> Genre | None:
        return self._selected

    @property
    def hide_owned(self) -> bool:
        return bool(self._hide_var.get())

    def show_genres(self, genres: Sequence[Genre], selected: Genre | None) -> None:
        """Offer "All" + `genres`, with `selected` (None = All) highlighted."""
        genres = tuple(genres)
        if genres != self._genres:
            for chip in self._chips.values():
                chip.destroy()
            self._chips = {None: self._chip(t(ALL_TEXT), None)}
            for genre in genres:
                self._chips[genre] = self._chip(genre_name(genre), genre)
            self._genres = genres
        self._selected = selected
        self._paint()
        self._fit()

    def _chip(self, text: str, genre: Genre | None) -> ctk.CTkButton:
        button = ctk.CTkButton(
            self._chip_row,
            text=text,
            width=round(self._chip_font.measure(text)) + 2 * CHIP_PAD,
            height=CHIP_HEIGHT,
            corner_radius=CHIP_HEIGHT // 2,
            font=self._chip_font,
            command=lambda: self._pick(genre),
        )
        button.pack(side="left", padx=(0, CHIP_GAP))
        return button

    def _paint(self) -> None:
        for genre, chip in self._chips.items():
            selected = genre == self._selected
            fill, hover = chip_color(selected)
            text = theme.ON_ACCENT if selected else theme.BUTTON_TEXT
            chip.configure(fg_color=fill, hover_color=hover, text_color=text)

    def _pick(self, genre: Genre | None) -> None:
        if genre == self._selected:
            return
        self._selected = genre
        self._paint()
        self._on_genre(genre)

    def _hide_toggled(self) -> None:
        self._on_hide_owned(self.hide_owned)

    # -- layout --------------------------------------------------------------------------------

    def _on_resize(self, event: Any) -> None:
        if event.width > 1:
            self._fit(event.width)

    def _fit(self, width: int | None = None) -> None:
        """The switch beside the chips when both fit on one line, else under them."""
        if width is None:
            width = self.winfo_width()
        if width <= 1:
            return
        scaling = ctk.ScalingTracker.get_widget_scaling(self)
        needed = (
            self._chip_row.winfo_reqwidth()
            + self._switch.winfo_reqwidth()
            + round(SWITCH_GAP * scaling)
        )
        self._place_switch(wrapped=needed > width)

    def _place_switch(self, *, wrapped: bool) -> None:
        if wrapped == self._wrapped:
            return
        self._wrapped = wrapped
        if wrapped:
            self._switch.grid(row=1, column=0, columnspan=2, sticky="w", pady=(8, 0))
        else:
            self._switch.grid(row=0, column=1, sticky="e", padx=(SWITCH_GAP, 0))
