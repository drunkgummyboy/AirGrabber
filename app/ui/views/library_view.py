"""The Library page: what is in your library, as a grid of posters (the switch's Library part,
only while there is one: Plex, or your own folders once switched on in Settings → Library,
docs/folder-library/; app/services/plex_library.py gives it its contents).

Two shelves, never mixed (docs/folder-library/2026-11-16-alongside-plex-design.md): "Plex", what
Plex has, and "Downloaded", what your own folders hold. With both on, a two-way switch with
each shelf's count (`ShelfSwitch`) sits under the header, above Continue watching and the
Shows / Films tabs, which are the chosen shelf's; with one source on there is no switch and the
page is that shelf's. The chosen shelf is kept while the app runs (a shelf gone for a while
gives way to the other, and comes back when it is back). Opening the Downloaded shelf (a click
on it, or the page shown on it) has the folders read again soon (`read_downloaded`: cheap,
unchanged files aren't read again), so a file moved by hand leaves it quickly.

At the top (the owner's choice A, mockups/library-filters.html): "Library" with a round search box
at its right; under them the Shows and Films tabs with their counts, a line under the chosen one;
then the filters as chips (shows: All / Not followed / With gaps / Ended; films: All / 4K /
Recently added) and, at the right, Sort ▾ (Recently added / A–Z / Year). "From Plex · checked
08:53" ("From your folders · …" with the folder library) goes to the main window's footer
(`on_checked`). The cards follow the card size (Settings › Looks): the poster
from the Plex server (`posters`: an ImageCache fetching through PlexService.poster), the title
(two lines' room on every card, so a row's cards line up; longer ends in "…"),
"3 seasons · 34 episodes" or the year and quality, an amber "2 gaps" pill and, for a show,
"+ Follow" / "✓ Following" with a round phone button (a drawn phone: an emoji shows as a box
there) while a phone is connected: the same PosterCard as the other pages' cards. A poster or title opens the show's window or the film's info window. PAGE_SIZE
cards at first, "Show more" adds the next ones below those shown (made once; a search, filter,
sort or tab change makes them anew): a library of a thousand films stays quick.

Above the tabs, "Continue watching" (app/ui/continue_watching.py): what Plex says to go on with
(`continue_watching`, read at every load and whenever the page is shown again, and after the
player stopped: `refresh_continue`), a click playing it where Plex left it (`play.resume`).
Hidden when it is empty, when Plex isn't connected or when Plex didn't answer (one quiet log
line). With a `play`, a card also offers "▶ Play" on hover (an accent pill drawn on its cover):
a film plays at once, a show opens the episode picker.

At the bottom, only on the Downloaded shelf and only when there is something in it,
"Couldn't recognise (n)" (mockups/folder-library.html, state 4): the files whose names say no film
or episode (`unrecognised()`, the folder library's last check), each its name and its folder, and
"Tidy up in MediaForge" (`on_tidy(paths)`: the main window hands them to MediaForge's page; this
page never moves or renames anything). Under the cards, above the footer's "From your folders"
line, one muted amber line per folder the last check couldn't read (`reach()`, the Settings
box's words: "Can't reach …" / "Reading … takes a while"). `refresh_unrecognised` draws both
again after a check.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import ntpath
import tkinter as tk
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, Protocol

import customtkinter as ctk
from PIL import ImageTk

from app.i18n import N_, t, tn
from app.models.plex import PlexContinue, PlexTitle
from app.services import ImageCache, SettingsService
from app.services.plex import PlexError, poster_url
from app.services.plex_library import (
    can_upgrade,
    FILM_FILTERS,
    FILTER_NAMES,
    SHOW_FILTERS,
    SORT_NAMES,
    LibraryContents,
    LibraryFilter,
    LibraryKindTab,
    LibraryShow,
    LibrarySort,
    PlexLibraryService,
    Shelf,
    checked_text,
    film_line,
    gaps_text,
    page_slice,
    show_line,
    shown_films,
    shown_shows,
)
from app.ui import theme
from app.ui.card_scale import FULL, CardScale, grid_item_width
from app.ui.cards import RoundButton, phone_icon
from app.ui.continue_watching import ContinueRow
from app.ui.dialog_kit import secondary_button
from app.ui.download_control import CORNER_RADIUS as ROW_CORNER_RADIUS
from app.ui.download_control import GAP as ROW_GAP
from app.ui.missing_control import MISSING_COLOR
from app.ui.play_control import PLAY_TEXT
from app.ui.poster_card import (
    POSTER_CARD_WIDTH,
    PosterCard,
    TrackButton,
    pill_image,
    rgb,
    row_font_size,
    row_height,
)
from app.ui.scrollable import ScrollableFrame
from app.ui.shelf_filter import CHIP_FONT_SIZE, CHIP_GAP, CHIP_HEIGHT, CHIP_PAD, chip_color
from app.ui.tasks import spawn
from app.ui.tooltip import Tooltip
from app.ui.views.base import BaseView, StatusReporter
from app.ui.widgets import FlexGrid, MessagePanel, elided, hairline

log = logging.getLogger(__name__)

TITLE = N_("Library")
# The two shelves' names on the switch (only with both on).
PLEX_SHELF = N_("Plex")
DOWNLOADED_SHELF = N_("Downloaded")
SHELF_NAMES: dict[Shelf, str] = {"plex": PLEX_SHELF, "downloaded": DOWNLOADED_SHELF}
SHOWS_TAB = N_("Shows")
FILMS_TAB = N_("Films")
SEARCH_TEXT = N_("Search your library…")
SORT_TEXT = N_("Sort")
FOLLOWING_TEXT = N_("✓ Following")
ADDING_TEXT = N_("Adding…")
UPGRADE_TEXT = N_("⬆ Upgrade")
UPGRADE_TIP = N_("Find a better copy: 1080p or 4K")
SEND_TIP = N_("Send to phone")
FOLLOW_FAILED = N_("Couldn't follow {name}: TVmaze doesn't know it")
OPEN_FAILED = N_("Couldn't find {name} on TVmaze")
MORE_TEXT = N_("Show more ({n})")
EMPTY_TITLE = N_("Nothing from Plex yet")
EMPTY_TEXT = N_("AirGrabber reads Plex every half hour; Settings → Library → Check now reads it at once.")
EMPTY_FOLDERS_TITLE = N_("Nothing from your folders yet")
EMPTY_FOLDERS_TEXT = N_(
    "AirGrabber reads your folders every 6 hours; Settings → Library → Check now reads them at once."
)
UNRECOGNISED_TITLE = N_("Couldn't recognise ({n})")
# The header's button: Plex or the folders read again now, then the page.
RESYNC_TEXT = N_("↻ Check now")
RESYNCING_TEXT = N_("Checking…")
RESYNC_WIDTH = 130
UNRECOGNISED_TEXT = N_("These files' names don't say which film or episode they are.")
TIDY_TEXT = N_("Tidy up in MediaForge")
TIDY_TIP = N_("Adds these files to MediaForge's queue, where you can name and sort them")
MORE_FILES_ONE = N_("…and {n} more file")
MORE_FILES_MORE = N_("…and {n} more files")
UNRECOGNISED_SHOWN = 50  # files listed; "Tidy up" takes them all
FILE_FONT = "Consolas"
FILE_SIZE = 12
# A row's room (CTk units) for a file's name and for its folder: longer ones are shortened in the
# middle, so a deep NAS path never makes the row wider than the page (narrowest window 1220).
NAME_ROOM = 460
FOLDER_ROOM = 440
NO_MATCH_TEXT = N_("Nothing here with this search or filter.")
PAGE_SIZE = 60  # cards at first and per "Show more"
SEARCH_WAIT_MS = 250
SEARCH_WIDTH = 280
SEARCH_HEIGHT = 34
TAB_FONT_SIZE = 15
COUNT_FONT_SIZE = 12
UNDERLINE = 3  # the chosen tab's line
SHELF_FONT_SIZE = 13
SHELF_RADIUS = 10  # the shelf switch's track
SHELF_PAD = 3  # the track's room around its two shelves
HOVER_CHECK_MS = 60  # the pointer left the card or one of its parts: still on it by then?
PLAY_AT = 0.42  # ▶ Play's place on the cover, from the card's top
PLAY_PAD_X = 16  # ▶ Play's pill: its room left and right of the text (at the card's scale)
PLAY_TAG = "play"  # ▶ Play's canvas items


class LibraryPlay(Protocol):
    """play_control's Play as the page uses it (tests pass fakes)."""

    def resume(self, item: PlexContinue) -> None: ...

    def film(self, title: PlexTitle) -> None: ...

    def pick(self, master: Any, title: PlexTitle) -> object: ...

    def close(self) -> None: ...


ContinueSource = Callable[[Shelf], Awaitable[Sequence[PlexContinue]]]  # a shelf's row


class KindTab(ctk.CTkFrame):
    """Shows or Films: its name, its count in a small pill, and a line under it while chosen."""

    def __init__(self, master: Any, name: str, command: Callable[[], None]) -> None:
        super().__init__(master, fg_color="transparent", cursor="hand2")
        self.selected = False
        self._hover = False
        self.name = ctk.CTkLabel(
            self, text=name, width=0, font=theme.font(TAB_FONT_SIZE, bold=True), cursor="hand2"
        )
        self.name.grid(row=0, column=0, padx=(2, 0), pady=(4, 6))
        self.count = ctk.CTkLabel(
            self, text="", width=0, font=theme.font(COUNT_FONT_SIZE, bold=True), height=20,
            corner_radius=10, cursor="hand2",
        )
        self.count.grid(row=0, column=1, padx=(7, 2), pady=(4, 6))
        self.underline = ctk.CTkFrame(
            self, width=1, height=UNDERLINE, corner_radius=1, fg_color="transparent"
        )
        self.underline.grid(row=1, column=0, columnspan=2, sticky="ew")
        for widget in (self, self.name, self.count):
            widget.bind("<Button-1>", lambda _event: command(), add="+")
            widget.bind("<Enter>", lambda _event: self._set_hover(True), add="+")
            widget.bind("<Leave>", lambda _event: self._set_hover(False), add="+")
        self._paint()

    def set_count(self, n: int) -> None:
        self.count.configure(text=f" {n} ")

    def set_selected(self, selected: bool) -> None:
        if selected != self.selected:
            self.selected = selected
            self._paint()

    def _set_hover(self, inside: bool) -> None:
        if inside != self._hover:
            self._hover = inside
            self._paint()

    def _paint(self) -> None:
        on = self.selected
        self.name.configure(text_color=theme.TEXT if on or self._hover else theme.TEXT_MUTED)
        fill, _hover = chip_color(on)
        self.count.configure(fg_color=fill, text_color=theme.ON_ACCENT if on else theme.TEXT_MUTED)
        self.underline.configure(fg_color=theme.BRAND if on else "transparent")


class ShelfSegment(ctk.CTkFrame):
    """One shelf on the switch: its name and its count in a small pill, filled while chosen."""

    def __init__(self, master: Any, name: str, command: Callable[[], None]) -> None:
        super().__init__(
            master, fg_color="transparent", corner_radius=SHELF_RADIUS - SHELF_PAD, cursor="hand2"
        )
        self.selected = False
        self._hover = False
        self.command = command  # a click on it
        self.name = ctk.CTkLabel(
            self, text=name, width=0, font=theme.font(SHELF_FONT_SIZE, bold=True), cursor="hand2"
        )
        self.name.grid(row=0, column=0, padx=(14, 0), pady=5)
        self.count = ctk.CTkLabel(
            self, text="", width=0, font=theme.font(COUNT_FONT_SIZE, bold=True), height=20,
            corner_radius=10, cursor="hand2",
        )
        self.count.grid(row=0, column=1, padx=(8, 12), pady=5)
        for widget in (self, self.name, self.count):
            widget.bind("<Button-1>", lambda _event: command(), add="+")
            widget.bind("<Enter>", lambda _event: self._set_hover(True), add="+")
            widget.bind("<Leave>", lambda _event: self._set_hover(False), add="+")
        self._paint()

    def set_count(self, n: int) -> None:
        self.count.configure(text=f" {n} ")

    def set_selected(self, selected: bool) -> None:
        if selected != self.selected:
            self.selected = selected
            self._paint()

    def _set_hover(self, inside: bool) -> None:
        if inside != self._hover:
            self._hover = inside
            self._paint()

    def _paint(self) -> None:
        # As the Downloads page's filter bar: the chosen one SELECTED, the other CHIP_HOVER under
        # the pointer; the count's pill stands out from either.
        on, hover = self.selected, self._hover
        if on:
            fill: theme.Color = theme.SELECTED
        else:
            fill = theme.CHIP_HOVER if hover else "transparent"
        self.configure(fg_color=fill)
        self.name.configure(text_color=theme.TEXT if on or hover else theme.TEXT_MUTED)
        self.count.configure(
            fg_color=theme.SURFACE if on or hover else theme.CHIP_HOVER,
            text_color=theme.TEXT if on else theme.TEXT_MUTED,
        )


class ShelfSwitch(ctk.CTkFrame):
    """Plex or Downloaded: the two shelves side by side on one rounded track (as the search
    box: the surface with a quiet edge), each with its count, the chosen one filled."""

    def __init__(self, master: Any, command: Callable[[Shelf], None]) -> None:
        super().__init__(
            master, fg_color=theme.SURFACE, corner_radius=SHELF_RADIUS, border_width=1,
            border_color=theme.CHIP_HOVER,
        )
        self.segments: dict[Shelf, ShelfSegment] = {}
        for column, (shelf, name) in enumerate(SHELF_NAMES.items()):
            segment = ShelfSegment(self, t(name), functools.partial(command, shelf))
            segment.grid(
                row=0, column=column, padx=(SHELF_PAD if column == 0 else 0, SHELF_PAD),
                pady=SHELF_PAD,
            )
            self.segments[shelf] = segment

    def show(self, counts: dict[Shelf, int], chosen: Shelf | None) -> None:
        for shelf, segment in self.segments.items():
            segment.set_count(counts.get(shelf, 0))
            segment.set_selected(shelf == chosen)


class LibraryCard(PosterCard):
    """One show or film, as the other pages' poster cards (the same size, the title and its line
    on the cover's faded bottom): for a show "2 seasons · 34 episodes", an amber "2 gaps" pill in
    the corner (as My shows' "2 missing") and the row of a Discover card, + Follow / ✓ Following
    (TrackButton) with the round phone button beside it; for a film its year and quality, and as
    its row "⬆ Upgrade" (below 1080p, `on_upgrade`) with the round phone beside it, or "Send to
    phone" alone, or no row (the text at the bottom, as Get started's cards)."""

    def __init__(
        self,
        master: Any,
        title: PlexTitle,
        *,
        posters: ImageCache,
        scale: CardScale,
        line: str,
        tag: str = "",
        following: bool | None = None,
        on_open: Callable[[], object],
        on_follow: Callable[[], object] | None = None,
        on_send: Callable[[], object] | None = None,
        on_upgrade: Callable[[], object] | None = None,
        on_play: Callable[[], object] | None = None,
    ) -> None:
        """`on_send`: Send to phone (the round phone beside the main button, or a film's own
        row); None: no phone to send to. `on_upgrade`: a film below 1080p's "⬆ Upgrade".
        `on_play`: "▶ Play" over the cover while the pointer is on the card."""
        has_row = following is not None or on_send is not None or on_upgrade is not None
        url = poster_url(title)
        super().__init__(
            master,
            title=title.title,
            meta=line,
            poster=lambda _width: url,
            images=posters,
            extra_height=0 if has_row else -row_height(scale),
            scale=scale,
            meta_row=True,
        )
        self.title = title
        self._set_open(on_open)
        if tag:
            self._set_badge(tag, MISSING_COLOR)
        self._on_play: Callable[[], object] | None = None
        self.play_items: tuple[int, int] | None = None  # ▶ Play's pill and text on the sheet
        self._play_photo: ImageTk.PhotoImage | None = None
        self._play_hot = False  # the pointer is on the pill
        self._play_cursor = ""
        self._play_on = False  # shown (its items' state)
        if on_play is not None:
            self._hover_play(on_play)
        self.follow_button: TrackButton | None = None
        self.send_button: ctk.CTkButton | RoundButton | None = None
        self.upgrade_button: ctk.CTkButton | None = None
        if not has_row:
            return
        row = ctk.CTkFrame(self, fg_color="transparent", height=row_height(scale))
        row.pack_propagate(False)
        self._pack_action(row)

        def send() -> None:
            if on_send is not None:
                on_send()

        def follow() -> None:
            if on_follow is not None:
                on_follow()

        def upgrade() -> None:
            if on_upgrade is not None:
                on_upgrade()

        if following is not None or on_upgrade is not None:
            if on_send is not None:
                phone = RoundButton(
                    row, icon=phone_icon, size=row_height(scale),
                    font=theme.font(row_font_size(scale)), command=send,
                )
                phone.pack(side="right", padx=(scale.px(ROW_GAP), 0))
                Tooltip(phone, t(SEND_TIP))
                self._follow_scale(phone.rescale)
                self.send_button = phone
            if following is not None:
                self.follow_button = TrackButton(
                    row, busy_text=t(ADDING_TEXT), command=follow, scale=scale
                )
                self.follow_button.pack(side="left", fill="both", expand=True)
                self.set_following(following)
            else:  # a film below 1080p: "do it", the filled accent like Follow
                self.upgrade_button = ctk.CTkButton(
                    row, text=t(UPGRADE_TEXT), height=row_height(scale),
                    corner_radius=scale.px(ROW_CORNER_RADIUS),
                    font=theme.font(row_font_size(scale), bold=True), fg_color=theme.ACCENT,
                    hover_color=theme.ACCENT_HOVER, text_color=theme.ON_ACCENT, command=upgrade,
                )
                self.upgrade_button.pack(side="left", fill="both", expand=True)
                Tooltip(self.upgrade_button, t(UPGRADE_TIP))
        elif on_send is not None:
            self.send_button = ctk.CTkButton(
                row, text=t(SEND_TIP), height=row_height(scale),
                corner_radius=scale.px(ROW_CORNER_RADIUS),
                font=theme.font(row_font_size(scale), bold=True), fg_color="transparent",
                border_width=1, border_color=theme.EDGE, hover_color=theme.HOVER,
                text_color=theme.BUTTON_TEXT, command=send,
            )
            self.send_button.pack(fill="both", expand=True)

    def _hover_play(self, on_play: Callable[[], object]) -> None:
        """"▶ Play" drawn on the cover (an accent pill, as the Download row's filled buttons),
        shown while the pointer is on the card; a click on it plays, not opens."""
        self._on_play = on_play
        canvas = self._sheet
        self._play_font = theme.font(row_font_size(self.scale), bold=True)
        self.play_items = (
            canvas.create_image(0, 0, anchor="center", state="hidden", tags=(PLAY_TAG,)),
            canvas.create_text(
                0, 0, anchor="center", text=t(PLAY_TEXT), state="hidden", tags=(PLAY_TAG,)
            ),
        )
        canvas.tag_bind(PLAY_TAG, "<Button-1>", self._play_pressed)
        canvas.tag_bind(PLAY_TAG, "<Enter>", lambda _event: self._set_play_hot(True))
        canvas.tag_bind(PLAY_TAG, "<Leave>", lambda _event: self._set_play_hot(False))
        self._follow_scale(self._draw_play)  # a new scaling, light ↔ dark
        self._draw_play()
        # The card's own frame: leaving through the action row, or the page scrolled under the
        # pointer, reaches it too.
        tk.Misc.bind(self, "<Enter>", lambda _event: self.hover(True), "+")
        tk.Misc.bind(self, "<Leave>", lambda _event: self.after(HOVER_CHECK_MS, self._left), "+")
        canvas.bind("<Enter>", lambda _event: self.hover(True), add="+")

    def _draw_play(self) -> None:
        """The pill at the card's scaling and in the current light / dark colours."""
        if self.play_items is None:
            return
        canvas, scaling, scale = self._sheet, self.scaling, self.scale
        text = t(PLAY_TEXT)
        width = self._play_font.measure(text) + 2 * scale.px(PLAY_PAD_X)
        size = (max(1, round(width * scaling)), max(1, round(row_height(scale) * scaling)))
        fill = rgb(self, theme.ACCENT_HOVER if self._play_hot else theme.ACCENT)
        self._play_photo = ImageTk.PhotoImage(pill_image(size, 1, fill, fill), master=canvas)
        x = round(self._units[0] * scaling) / 2
        y = round(self._units[1] * scaling * PLAY_AT)
        image, label = self.play_items
        canvas.coords(image, x, y)
        canvas.itemconfigure(image, image=self._play_photo)
        canvas.coords(label, x, y)
        canvas.itemconfigure(
            label, font=self._play_font.create_scaled_tuple(scaling),
            fill=theme.pick(theme.ON_ACCENT),
        )

    @property
    def play_shown(self) -> bool:
        """▶ Play is on the cover (the pointer is on the card)."""
        return self._play_on

    def hover(self, inside: bool) -> None:
        """Show or hide "▶ Play" (the pointer came onto the card or left it)."""
        items = self.play_items
        if items is None:
            return
        if not inside:
            self._set_play_hot(False)
        self._play_on = inside
        for item in items:
            self._sheet.itemconfigure(item, state="normal" if inside else "hidden")
        if inside:
            self._sheet.tag_raise(PLAY_TAG)

    def _set_play_hot(self, hot: bool) -> None:
        """The pointer on the pill: the hand and the accent's hover colour."""
        if hot == self._play_hot:
            return
        self._play_hot = hot
        canvas = self._sheet
        if hot:
            self._play_cursor = str(canvas.cget("cursor"))
            canvas.configure(cursor="hand2")
        else:
            canvas.configure(cursor=self._play_cursor)
        self._draw_play()

    def _over_play(self) -> bool:
        items = self.play_items
        if items is None or not self.play_shown:
            return False
        return bool(set(self._sheet.find_withtag("current")) & set(items))

    def _play_pressed(self, _event: Any = None) -> None:
        if self._on_play is not None and self.play_shown:
            self._on_play()

    def _clicked(self, event: Any) -> None:
        """A click on the cover opens the title, except on ▶ Play (its own tag binding)."""
        if self._over_play():
            return
        super()._clicked(event)

    def _left(self) -> None:
        """The pointer left the card or one of its parts: ▶ Play goes once it is off the card."""
        try:
            if not self.winfo_exists():
                return
            x, y = self.winfo_pointerxy()
            left, top = self.winfo_rootx(), self.winfo_rooty()
            inside = left <= x < left + self.winfo_width() and top <= y < top + self.winfo_height()
        except tk.TclError:
            return
        if not inside:
            self.hover(False)

    def set_following(self, following: bool) -> None:
        if self.follow_button is not None:
            self.follow_button.set_state("tracked" if following else "idle")

    def set_busy(self) -> None:
        """While Track looks the show up and adds it."""
        if self.follow_button is not None:
            self.follow_button.set_state("busy")

    @property
    def following(self) -> bool:
        return self.follow_button is not None and self.follow_button.cget("text") == t(
            FOLLOWING_TEXT
        )


class LibraryView(BaseView):
    def __init__(
        self,
        master: Any,
        *,
        service: PlexLibraryService,
        posters: ImageCache,
        settings: SettingsService,
        status: StatusReporter,
        on_open_show: Callable[[PlexTitle], Awaitable[bool]],
        on_open_film: Callable[[PlexTitle], None],
        on_upgrade_film: Callable[[PlexTitle], None] | None = None,
        on_send: Callable[[PlexTitle], None] | None = None,
        can_send: Callable[[], bool] = lambda: False,
        card_scale: Callable[[], CardScale] = lambda: FULL,
        on_checked: Callable[[str], None] = lambda text: None,
        continue_watching: ContinueSource | None = None,
        play: LibraryPlay | None = None,
        unrecognised: Callable[[], Sequence[str]] | None = None,
        on_tidy: Callable[[Sequence[str]], None] | None = None,
        reach: Callable[[], Sequence[str]] | None = None,
        resync: Callable[[], Awaitable[None]] | None = None,
        read_downloaded: Callable[[], None] | None = None,
        **kwargs: Any,
    ) -> None:
        """`on_open_show(title)`: opens the show's window; False when TVmaze knows none.
        `on_open_film(title)`: the film's info window. `on_send(title)`: the Send to phone
        window, on the cards while `can_send()` (a phone is connected), read at every render.
        `on_checked(text)`: "From Plex · checked 08:53" (the shelf's) after every load (the
        footer's). `continue_watching(shelf)`: the shelf's Continue watching row (None: no
        row; raises PlexError); `play`: ▶ Play (the row's cards, the cards' hover button; None:
        neither). `unrecognised()`: the folder library's files it couldn't recognise (read at
        every render of the Downloaded shelf); `on_tidy(paths)`: "Tidy up in MediaForge"
        (None: no section). `reach()`: the folder library's lines on folders it couldn't read
        (translated; read at every render of the Downloaded shelf). `resync()`: the header's
        "↻ Check now" (Plex's and the folders' checks, whichever are on; None: no button); the
        page loads again after it. `read_downloaded()`: the Downloaded shelf was opened, the
        folders are to be read again soon (FolderLibraryService.check_soon)."""
        super().__init__(master, **kwargs)
        self._service = service
        self._posters = posters
        self._settings = settings
        self._status = status
        self._on_open_show = on_open_show
        self._on_open_film = on_open_film
        self._on_upgrade_film = on_upgrade_film
        self._on_send = on_send
        self._can_send = can_send
        self._card_scale = card_scale
        self._on_checked = on_checked
        self._continue_watching = continue_watching
        self._play = play
        self._unrecognised = unrecognised
        self._on_tidy = on_tidy
        self._reach = reach
        self._resync = resync
        self._read_downloaded = read_downloaded
        self.resync_button: ctk.CTkButton | None = None
        self.reach_labels: list[ctk.CTkLabel] = []
        self.unrecognised_section: ctk.CTkFrame | None = None
        self._file_fonts: tuple[ctk.CTkFont, ctk.CTkFont] | None = None  # made once, as needed
        self._continue_task: asyncio.Task[None] | None = None
        self.checked_text = ""
        # The shelves on at the last load, each its contents, and the one chosen (kept while
        # the app runs; `shelf` is the one shown).
        self._shelves: tuple[Shelf, ...] = ()
        self._by_shelf: dict[Shelf, LibraryContents] = {}
        self._chosen: Shelf | None = None
        self.shelf_switch: ShelfSwitch | None = None  # made once both shelves are on
        self._contents = LibraryContents()  # the shown shelf's
        self._kind = LibraryKindTab.SHOWS
        self._sort = LibrarySort.ADDED
        self._filter = LibraryFilter.ALL
        self._shown = PAGE_SIZE
        self._search_job: str | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self.cards: list[LibraryCard] = []

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(5, weight=1)
        # "Library" and the search box; under them (row 1) the shelf switch, with both shelves.
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=15, pady=(5, 0))
        header.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            header, text=t(TITLE), font=theme.font(20, bold=True), text_color=theme.TEXT
        ).grid(row=0, column=0, sticky="w")
        if resync is not None:
            self.resync_button = secondary_button(
                header, t(RESYNC_TEXT), self.resync, width=RESYNC_WIDTH
            )
            self.resync_button.grid(row=0, column=1, sticky="w", padx=(14, 0))
        self.search = ctk.CTkEntry(
            header, width=SEARCH_WIDTH, height=SEARCH_HEIGHT, corner_radius=SEARCH_HEIGHT // 2,
            placeholder_text="🔍  " + t(SEARCH_TEXT), fg_color=theme.SURFACE,
            border_color=theme.CHIP_HOVER, text_color=theme.TEXT,
        )
        self.search.grid(row=0, column=2, sticky="e")
        self.search.bind("<KeyRelease>", self._search_typed, add="+")

        # Continue watching, above the tabs: hidden until Plex names something.
        self.continue_row = ContinueRow(self, posters=posters, on_play=self._resume)
        self.continue_row.grid(row=2, column=0, sticky="ew", padx=15, pady=(8, 0))
        self.continue_row.grid_remove()

        # The Shows and Films tabs on a thin line.
        tabs = ctk.CTkFrame(self, fg_color="transparent")
        tabs.grid(row=3, column=0, sticky="ew", padx=15, pady=(6, 0))
        self.kind_tabs_row = tabs
        tabs.grid_columnconfigure(2, weight=1)
        self.kind_tabs: dict[LibraryKindTab, KindTab] = {}
        kinds = ((LibraryKindTab.SHOWS, SHOWS_TAB), (LibraryKindTab.FILMS, FILMS_TAB))
        for column, (kind, name) in enumerate(kinds):
            tab = KindTab(tabs, t(name), functools.partial(self.set_kind, kind))
            tab.grid(row=0, column=column, sticky="w", padx=(0, 26))
            self.kind_tabs[kind] = tab
        ctk.CTkFrame(tabs, height=1, corner_radius=0, fg_color=theme.CHIP_HOVER).grid(
            row=1, column=0, columnspan=3, sticky="ew"
        )
        self._show_kinds()

        # The filters as chips, Sort ▾ at the right.
        bar = ctk.CTkFrame(self, fg_color="transparent")
        bar.grid(row=4, column=0, sticky="ew", padx=15, pady=(12, 10))
        bar.grid_columnconfigure(0, weight=1)
        self._chip_font = theme.font(CHIP_FONT_SIZE)
        self._chip_row = ctk.CTkFrame(bar, fg_color="transparent")
        self._chip_row.grid(row=0, column=0, sticky="w")
        self.filter_chips: dict[LibraryFilter, ctk.CTkButton] = {}
        self._make_chips()
        sort = ctk.CTkFrame(bar, fg_color="transparent")
        sort.grid(row=0, column=1, sticky="e")
        ctk.CTkLabel(
            sort, text=t(SORT_TEXT), font=self._chip_font, text_color=theme.TEXT_MUTED
        ).pack(side="left", padx=(0, 8))
        fill, hover = chip_color(False)
        self.sort_menu = ctk.CTkOptionMenu(
            sort, values=[t(name) for name in SORT_NAMES.values()], height=CHIP_HEIGHT,
            corner_radius=8, font=self._chip_font, dropdown_font=self._chip_font,
            fg_color=fill, button_color=fill, button_hover_color=hover,
            text_color=theme.BUTTON_TEXT, command=self._choose_sort,
        )
        self.sort_menu.set(t(SORT_NAMES[self._sort]))
        self.sort_menu.pack(side="left")

        self._body = ScrollableFrame(self, fg_color="transparent")
        self._body.grid(row=5, column=0, sticky="nsew", padx=15, pady=(0, 5))
        self._body.grid_columnconfigure(0, weight=1)
        # The folders the folder library couldn't read: always in sight, right above the footer.
        self._reach_row = ctk.CTkFrame(self, fg_color="transparent")
        self._reach_row.grid(row=6, column=0, sticky="ew", padx=19, pady=(0, 4))
        self._reach_row.grid_remove()
        self._grid: FlexGrid | None = None
        # What the grid shows from (the last render's search, filter and sort) and its size.
        self._items: Sequence[LibraryShow | PlexTitle] = ()
        self._scale = FULL
        self.more_button: ctk.CTkButton | None = None
        self._message: MessagePanel | ctk.CTkLabel | None = None

    def _filters(self) -> tuple[LibraryFilter, ...]:
        return SHOW_FILTERS if self._kind is LibraryKindTab.SHOWS else FILM_FILTERS

    def _make_chips(self) -> None:
        """The chips of the chosen tab's filters (shows' and films' differ)."""
        for chip in self.filter_chips.values():
            chip.destroy()
        self.filter_chips = {}
        for choice in self._filters():
            text = t(FILTER_NAMES[choice])
            chip = ctk.CTkButton(
                self._chip_row, text=text,
                width=round(self._chip_font.measure(text)) + 2 * CHIP_PAD, height=CHIP_HEIGHT,
                corner_radius=CHIP_HEIGHT // 2, font=self._chip_font,
                command=functools.partial(self._choose_filter, choice),
            )
            chip.pack(side="left", padx=(0, CHIP_GAP))
            self.filter_chips[choice] = chip
        self._paint_chips()

    def _paint_chips(self) -> None:
        for choice, chip in self.filter_chips.items():
            on = choice is self._filter
            fill, hover = chip_color(on)
            chip.configure(
                fg_color=fill, hover_color=hover,
                text_color=theme.ON_ACCENT if on else theme.BUTTON_TEXT,
            )

    def _show_kinds(self) -> None:
        """The tabs' counts and which one is chosen."""
        counts = {
            LibraryKindTab.SHOWS: len(self._contents.shows),
            LibraryKindTab.FILMS: len(self._contents.films),
        }
        for kind, tab in self.kind_tabs.items():
            tab.set_count(counts[kind])
            tab.set_selected(kind is self._kind)

    def _shelf_of(self, shelves: Sequence[Shelf]) -> Shelf | None:
        """Of `shelves` (those on), the one shown: the chosen one while it is on, else the
        first."""
        if self._chosen in shelves:
            return self._chosen
        return shelves[0] if shelves else None

    @property
    def shelf(self) -> Shelf | None:
        """The shelf shown (None: no library at the last load)."""
        return self._shelf_of(self._shelves)

    @property
    def shelf_switch_shown(self) -> bool:
        switch = self.shelf_switch
        return switch is not None and bool(switch.winfo_manager())

    def _show_shelves(self) -> None:
        """The switch with both shelves on, each with its count (made at the first time),
        else none."""
        if len(self._shelves) < 2:
            if self.shelf_switch is not None:
                self.shelf_switch.grid_remove()
            return
        if self.shelf_switch is None:
            self.shelf_switch = ShelfSwitch(self, self.set_shelf)
            self.shelf_switch.grid(row=1, column=0, sticky="w", padx=15, pady=(10, 0))
        counts = {
            shelf: len(contents.shows) + len(contents.films)
            for shelf, contents in self._by_shelf.items()
        }
        self.shelf_switch.show(counts, self.shelf)
        self.shelf_switch.grid()

    # -- choices ---------------------------------------------------------------------------

    def set_shelf(self, shelf: Shelf) -> None:
        """Plex or Downloaded, as a click on the switch (kept while the app runs); opening
        Downloaded has the folders read again soon."""
        if shelf not in self._shelves or shelf == self.shelf:
            return
        self._chosen = shelf
        self._shown = PAGE_SIZE
        self._show_shelf()
        self.refresh_continue()
        self._opened(shelf)

    def _opened(self, shelf: Shelf | None) -> None:
        if shelf == "downloaded" and self._read_downloaded is not None:
            self._read_downloaded()

    def set_kind(self, kind: LibraryKindTab) -> None:
        """Shows or Films, as a click on its tab."""
        if kind is self._kind:
            return
        self._kind = kind
        self._filter = LibraryFilter.ALL
        self._show_kinds()
        self._make_chips()
        self._shown = PAGE_SIZE
        self._render()

    def _choose_sort(self, label: str) -> None:
        self._sort = next(s for s, name in SORT_NAMES.items() if t(name) == label)
        self._shown = PAGE_SIZE
        self._render()

    def _choose_filter(self, choice: LibraryFilter) -> None:
        if choice is self._filter:
            return
        self._filter = choice
        self._paint_chips()
        self._shown = PAGE_SIZE
        self._render()

    def _search_typed(self, _event: object = None) -> None:
        if self._search_job is not None:
            self.after_cancel(self._search_job)
        self._search_job = self.after(SEARCH_WAIT_MS, self._searched)

    def _searched(self) -> None:
        self._search_job = None
        self._shown = PAGE_SIZE
        self._render()

    # -- loading and showing -------------------------------------------------------------------

    async def load(self) -> None:
        """Every shelf on, each its own contents (the switch's counts), then the chosen one."""
        profile_id = self._settings.current.current_profile
        shelves = self._service.shelves()
        found = await asyncio.gather(
            *(self._service.contents(profile_id=profile_id, shelf=shelf) for shelf in shelves)
        )
        if not self.winfo_exists():
            return
        self._shelves = shelves
        self._by_shelf = dict(zip(shelves, found, strict=True))
        self._show_shelf()
        self.refresh_continue()

    def _show_shelf(self) -> None:
        """The shown shelf's switch, tabs, cards and footer line ("From Plex · checked …", or
        on Downloaded "From your folders · checked …")."""
        shelf = self.shelf
        self._contents = self._by_shelf.get(shelf, LibraryContents()) if shelf else LibraryContents()
        self._show_shelves()
        self._show_kinds()
        self.checked_text = checked_text(
            self._contents.checked_at, folders=shelf == "downloaded"
        )
        self._on_checked(self.checked_text)
        self._render()

    def refresh_if_stale(self) -> None:
        """The page is shown: loaded when stale (the row with it), else only the row read
        again (what was watched elsewhere meanwhile). Shown on Downloaded, the folders are
        read again soon."""
        if self.is_stale():
            super().refresh_if_stale()
        else:
            self.refresh_continue()
        self._opened(self._shelf_of(self._service.shelves()))

    def refresh_continue(self) -> None:
        """Read the shown shelf's Continue watching again (a read under way is dropped for
        this one)."""
        if self._continue_task is not None:
            self._continue_task.cancel()
        source = self._continue_watching
        shelf = self.shelf
        if source is None or shelf is None:
            self.continue_row.show(())
            return
        self._continue_task = spawn(
            self._load_continue(source, shelf), name="LibraryView.continue"
        )

    async def _load_continue(self, source: ContinueSource, shelf: Shelf) -> None:
        try:
            items: Sequence[PlexContinue] = await source(shelf)
        except PlexError as exc:
            log.info("Library: Continue watching didn't read (%s)", type(exc).__name__)
            items = ()
        if self.winfo_exists():
            self.continue_row.show(items)

    def _resume(self, item: PlexContinue) -> None:
        if self._play is not None:
            self._play.resume(item)

    def _clear(self) -> None:
        for child in self._body.winfo_children():
            child.destroy()
        self.cards = []
        self._grid = None
        self._items = ()
        self.more_button = None
        self._message = None
        self.unrecognised_section = None

    def _render(self) -> None:
        self._clear()
        self._render_cards()
        self.refresh_unrecognised()

    def _render_cards(self) -> None:
        query = self.search.get()
        contents = self._contents
        if not contents.shows and not contents.films:
            folders = self.shelf == "downloaded"
            self._message = MessagePanel(
                self._body,
                t(EMPTY_FOLDERS_TITLE if folders else EMPTY_TITLE),
                t(EMPTY_FOLDERS_TEXT if folders else EMPTY_TEXT),
                height=300,
            )
            self._message.grid(row=0, column=0, sticky="nsew")
            return
        scale = self._card_scale()
        items: Sequence[LibraryShow | PlexTitle]
        if self._kind is LibraryKindTab.SHOWS:
            items = shown_shows(contents.shows, self._filter, self._sort, query)
        else:
            items = shown_films(contents.films, self._filter, self._sort, query)
        if not items:
            self._message = ctk.CTkLabel(
                self._body, text=t(NO_MATCH_TEXT), font=theme.font(14),
                text_color=theme.TEXT_MUTED,
            )
            self._message.grid(row=0, column=0, pady=40)
            return
        item_width = grid_item_width(POSTER_CARD_WIDTH, scale)  # as every page's poster cards
        grid = FlexGrid(self._body, item_width=item_width, pad_x=8, pad_y=10)
        grid.grid(row=0, column=0, sticky="ew")
        self._grid = grid
        self._items = items
        self._scale = scale
        self._add_cards(0)

    def _add_cards(self, start: int) -> None:
        """Cards for the shown items from `start` on, into the grid as it is, and "Show more"
        for the rest (gone when nothing is left)."""
        grid = self._grid
        if grid is None:
            return
        page: Sequence[LibraryShow | PlexTitle]
        page, more = page_slice(self._items, self._shown)
        for item in page[start:]:
            self.cards.append(self._card(grid, item, self._scale))
        grid.relayout()  # re-grids the cards already there, never makes them again
        if not more:
            if self.more_button is not None:
                self.more_button.destroy()
                self.more_button = None
        elif self.more_button is not None:
            self.more_button.configure(text=t(MORE_TEXT, n=more))
        else:
            self.more_button = ctk.CTkButton(
                self._body, text=t(MORE_TEXT, n=more), width=200, fg_color="transparent",
                border_width=1, border_color=theme.EDGE, hover_color=theme.HOVER,
                text_color=theme.BUTTON_TEXT, command=self.show_more,
            )
            self.more_button.grid(row=1, column=0, pady=(6, 16))

    def _card(self, grid: FlexGrid, item: LibraryShow | PlexTitle, scale: CardScale) -> LibraryCard:
        send = self._on_send if self._on_send is not None and self._can_send() else None
        play = self._play
        if isinstance(item, LibraryShow):
            title = item.title
            card = LibraryCard(
                grid, title, posters=self._posters, scale=scale, line=show_line(title),
                tag=gaps_text(item.gaps), following=item.following,
                on_open=lambda: self._open_show(title),
                on_send=(lambda: send(title)) if send is not None else None,
                on_play=(lambda: play.pick(self.winfo_toplevel(), title)) if play else None,
            )
            if card.follow_button is not None:
                card.follow_button.configure(command=lambda: self._follow(card, item))
            return card
        line = film_line(item)
        if item.resolution:
            line = f"{line} · {item.resolution}" if line else item.resolution
        upgrade = self._on_upgrade_film if can_upgrade(item) else None
        return LibraryCard(
            grid, item, posters=self._posters, scale=scale, line=line,
            on_open=lambda: self._on_open_film(item),
            on_send=(lambda: send(item)) if send is not None else None,
            on_upgrade=(lambda: upgrade(item)) if upgrade is not None else None,
            on_play=(lambda: play.film(item)) if play is not None else None,
        )

    def _refresh_reach(self) -> None:
        for label in self.reach_labels:
            label.destroy()
        self.reach_labels = []
        downloaded = self.shelf == "downloaded"  # the folders' lines: on their shelf only
        lines = list(self._reach()) if self._reach is not None and downloaded else []
        if not lines or not self.winfo_exists():
            self._reach_row.grid_remove()
            return
        for line in lines:
            label = ctk.CTkLabel(
                self._reach_row, text=line, font=theme.font(12), text_color=theme.WARNING,
                anchor="w", justify="left",
            )
            label.pack(anchor="w")
            self.reach_labels.append(label)
        self._reach_row.grid()

    def refresh_unrecognised(self) -> None:
        """"Couldn't recognise (n)" under the cards, drawn again from `unrecognised()` (gone when
        nothing is in it), and the lines on folders the last check couldn't read (`reach()`);
        both on the Downloaded shelf only."""
        self._refresh_reach()
        if self.unrecognised_section is not None:
            self.unrecognised_section.destroy()
            self.unrecognised_section = None
        downloaded = self.shelf == "downloaded"
        paths = tuple(self._unrecognised()) if self._unrecognised is not None and downloaded else ()
        if not paths or self._on_tidy is None or not self.winfo_exists():
            return
        on_tidy = self._on_tidy
        section = ctk.CTkFrame(self._body, fg_color="transparent")
        section.grid(row=2, column=0, sticky="ew", padx=4, pady=(18, 16))
        section.grid_columnconfigure(0, weight=1)
        self.unrecognised_section = section
        hairline(section, theme.PANEL_EDGE).grid(row=0, column=0, columnspan=2, sticky="ew")
        words = ctk.CTkFrame(section, fg_color="transparent")
        words.grid(row=1, column=0, sticky="w", pady=(14, 8))
        ctk.CTkLabel(
            words, text=t(UNRECOGNISED_TITLE, n=len(paths)), font=theme.font(15, bold=True),
            text_color=theme.TEXT, anchor="w",
        ).pack(anchor="w")
        ctk.CTkLabel(
            words, text=t(UNRECOGNISED_TEXT), font=theme.font(12), text_color=theme.TEXT_MUTED,
            anchor="w",
        ).pack(anchor="w")
        tidy = ctk.CTkButton(
            section, text=t(TIDY_TEXT), width=0, height=32, corner_radius=8,
            font=theme.font(13, bold=True), fg_color="transparent", border_width=1,
            border_color=theme.EDGE, hover_color=theme.HOVER, text_color=theme.BUTTON_TEXT,
            command=lambda: on_tidy(paths),
        )
        tidy.grid(row=1, column=1, sticky="e", pady=(14, 8))
        Tooltip(tidy, t(TIDY_TIP))
        name_font, folder_font = self._fonts()
        for row, path in enumerate(paths[:UNRECOGNISED_SHOWN], start=2):
            folder, name = ntpath.split(path)
            name = elided(name, lambda text: name_font.measure(text) <= NAME_ROOM)
            folder = elided(folder, lambda text: folder_font.measure(text) <= FOLDER_ROOM)
            line = ctk.CTkFrame(section, fg_color="transparent")
            line.grid(row=row, column=0, columnspan=2, sticky="ew")
            line.grid_columnconfigure(1, weight=1)
            ctk.CTkLabel(line, text=name, font=name_font, text_color=theme.TEXT, anchor="w").grid(
                row=0, column=0, sticky="w", pady=(4, 4)
            )
            ctk.CTkLabel(
                line, text=folder, font=folder_font, text_color=theme.TEXT_MUTED, anchor="e"
            ).grid(row=0, column=1, sticky="e", padx=(14, 0))
            hairline(line, theme.CHIP).grid(row=1, column=0, columnspan=2, sticky="ew")
        rest = len(paths) - UNRECOGNISED_SHOWN
        if rest > 0:
            ctk.CTkLabel(
                section, text=tn(MORE_FILES_ONE, MORE_FILES_MORE, rest), font=theme.font(12),
                text_color=theme.TEXT_MUTED, anchor="w",
            ).grid(row=UNRECOGNISED_SHOWN + 2, column=0, sticky="w", pady=(6, 0))

    def _fonts(self) -> tuple[ctk.CTkFont, ctk.CTkFont]:
        """The file names' Consolas and the folders' font, at the app's text size, made once."""
        if self._file_fonts is None:
            name_font = theme.font(FILE_SIZE)
            name_font.configure(family=FILE_FONT)
            self._file_fonts = (name_font, theme.font(FILE_SIZE))
        return self._file_fonts

    def show_more(self) -> None:
        """The next PAGE_SIZE cards below those shown, which stay as they are (and so does the
        scrolled place). A search, filter, sort or tab change starts over (`_render`)."""
        start = len(self.cards)
        self._shown = start + PAGE_SIZE
        self._add_cards(start)

    # -- actions -------------------------------------------------------------------------------

    def resync(self) -> None:
        """The header's "↻ Check now": the sources read again (both shelves' together), then
        the page (a click while one runs does nothing)."""
        button = self.resync_button
        if self._resync is None or button is None or button.cget("state") == "disabled":
            return
        button.configure(state="disabled", text=t(RESYNCING_TEXT))
        self._start(self._resynced(self._resync), "LibraryView.resync")

    async def _resynced(self, resync: Callable[[], Awaitable[None]]) -> None:
        try:
            await resync()
            if self.winfo_exists():
                await self.load()
        finally:
            button = self.resync_button
            if button is not None and button.winfo_exists():
                button.configure(state="normal", text=t(RESYNC_TEXT))

    def _start(self, coro: Any, name: str) -> None:
        task: asyncio.Task[Any] = spawn(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _open_show(self, title: PlexTitle) -> None:
        async def run() -> None:
            if not await self._on_open_show(title) and self.winfo_exists():
                self._report(t(OPEN_FAILED, name=title.title))

        self._start(run(), "LibraryView.open")

    def _follow(self, card: LibraryCard, show: LibraryShow) -> None:
        if card.follow_button is None or card.following:
            return
        card.set_busy()

        async def run() -> None:
            followed = await self._service.follow(
                show.title, profile_id=self._settings.current.current_profile
            )
            if not card.winfo_exists():
                return
            card.set_following(followed)
            if not followed:
                self._report(t(FOLLOW_FAILED, name=show.title.title))

        self._start(run(), "LibraryView.follow")

    def _report(self, text: str) -> None:
        try:
            self._status(text, clear_after_ms=6000)
        except tk.TclError:
            pass

    def destroy(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._continue_task is not None:
            self._continue_task.cancel()
        if self._play is not None:
            self._play.close()
        if self._search_job is not None:
            self.after_cancel(self._search_job)
            self._search_job = None
        super().destroy()
