"""Cards for the calendar grid: tracked episodes (full and compact) and suggested shows.

All three are drawn at the page's `CardScale` (Settings → Card size). The full card's sizes and
baked images are in app/ui/calendar_face.py; the compact and suggestion cards keep their look,
only their fonts follow the card size (never below the readable minimums).
"""

from __future__ import annotations

import asyncio
import tkinter as tk
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import customtkinter as ctk
from PIL import Image, ImageTk

from app.models import CalendarEntry, Show
from app.services.tvmaze import TVMazeError
from app.ui import theme
from app.ui.calendar_face import (
    TITLE_MAX_LINES,
    PosterArea,
    PosterFace,
    cap_face,
    compose_poster,
    cover_size,
    episode_layout,
    poster_area,
    poster_face,
    shared_image,
    text_width,
)
from app.ui.card_scale import FULL, SMALL_TEXT_MIN, TEXT_MIN, TITLE_MIN, CardScale
from app.ui.download_control import (
    ROW_SPACE,
    Base,
    DownloadActions,
    DownloadControl,
    episode_key,
)
from app.ui.media_cards import IMDB_ICON_ROOM
from app.ui.tasks import spawn
from app.ui.tooltip import add_tooltip
from app.ui.text_fit import (
    SLACK,
    Measure,
    elide_to_width,
    unscaled_width,
)

if TYPE_CHECKING:
    from app.services import ImageCache, LibraryService
    from app.ui.views.base import StatusReporter

EpisodeActions = DownloadActions[CalendarEntry]
OpenShow = Callable[[Show], None]  # opens AirGrabber's window of a show (ShowWindows.open_show)

RGB = tuple[int, int, int]

NOT_AIRED_TEXT = "Not aired"
SHOW_TIP = "More about this show"  # the compact cards' ⓘ
# A full card's width before its first layout (for where the Download row starts meanwhile).
NOMINAL_CARD_WIDTH = 200
REBAKE_MS = 150  # after a column stops changing width, the poster piece is baked again
# TVMaze's "medium" posters are this wide; wider posters come from its "original" ones.
MEDIUM_POSTER_WIDTH = 210

# Compact cards: a name line and a code line, then the Download row.
_COMPACT_NAME_HEIGHT = 20
_COMPACT_CODE_HEIGHT = 16
_COMPACT_HEIGHT = 4 + _COMPACT_NAME_HEIGHT + _COMPACT_CODE_HEIGHT + ROW_SPACE
_NAME_SIZE = 11  # bold
_CODE_SIZE = 9
# Suggestions: a "Suggested" line, the name, then the code with the + Track button.
_SUGGESTED_HEIGHT = 16
_SUGGESTED_SIZE = 11
_SUGGESTION_PAD_Y = 6
_SUGGESTION_HEIGHT = (
    2 * _SUGGESTION_PAD_Y + _SUGGESTED_HEIGHT + _COMPACT_NAME_HEIGHT + 2 + _COMPACT_CODE_HEIGHT
)


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
    card: ctk.CTkFrame, entry: CalendarEntry, actions: EpisodeActions, scale: CardScale = FULL
) -> DownloadControl[CalendarEntry]:
    """The card's Download row (not yet placed)."""
    episode = entry.episode
    return DownloadControl(
        card,
        item=entry,
        key=episode_key(entry.show.id, episode.season, episode.number),
        base=episode_base(entry),
        actions=actions,
        scale=scale,
    )


def poster_url(show: Show, width_px: int) -> str | None:
    """The show's poster for an area `width_px` physical pixels wide: TVMaze's medium one, the
    original when the medium one would have to be enlarged (and there is one)."""
    if width_px > MEDIUM_POSTER_WIDTH and show.poster_url_original:
        return show.poster_url_original
    return show.poster_url


def fit_words_to_lines(text: str, max_width: float, max_lines: int, measure: Measure) -> str:
    """`text` word-wrapped into at most `max_lines` lines of `max_width`, breaking only at spaces.

    A word too wide for a line of its own is elided with "…" and ends the text; when the words
    need more lines, the last line holds the rest elided with "…".
    """
    words = text.split()
    lines: list[str] = []
    index = 0
    while index < len(words):
        if len(lines) == max_lines - 1:
            lines.append(elide_to_width(" ".join(words[index:]), max_width, measure))
            break
        line = words[index]
        if measure(line) > max_width:
            lines.append(elide_to_width(line, max_width, measure))
            break
        index += 1
        while index < len(words) and measure(f"{line} {words[index]}") <= max_width:
            line = f"{line} {words[index]}"
            index += 1
        lines.append(line)
    return "\n".join(lines)


def card_title(name: str, room: float, measure: Measure) -> str:
    """The show's name in at most TITLE_MAX_LINES lines of `room`, broken only between words
    and with the breaks explicit (canvas text doesn't wrap by itself)."""
    return fit_words_to_lines(name, room, TITLE_MAX_LINES, measure)


def _rgb(widget: Any, color: str) -> RGB:
    red, green, blue = widget.winfo_rgb(color)  # 16 bits per channel
    return (red >> 8, green >> 8, blue >> 8)


# -- cards -----------------------------------------------------------------------------------


class EpisodeCard(ctk.CTkFrame):
    """A tracked episode (recent days and later) as a wide card: the show's poster fills the
    left side and fades into the card; the show's name, the episode code and the bottom row
    (a muted "Not aired", else the Download row) sit on the right, over the faded part.
    Clicking the poster or the name opens the show's window (`on_open_show`).

    Everything but the Download row is drawn on one canvas (see app/ui/calendar_face.py): the
    baked poster piece and right cap, the stretch between them, and the texts.
    """

    def __init__(
        self,
        master: Any,
        entry: CalendarEntry,
        *,
        images: ImageCache,
        actions: EpisodeActions,
        scale: CardScale = FULL,
        on_open_show: OpenShow | None = None,
    ) -> None:
        def linespace(size: int, bold: bool) -> int:
            return _linespace(theme.font(size, bold=bold))

        layout = episode_layout(scale, linespace=linespace)
        super().__init__(
            master, fg_color=theme.SURFACE, corner_radius=0, border_width=0, height=layout.height
        )
        self.grid_propagate(False)
        self.pack_propagate(False)
        self._layout = layout
        self._show = entry.show
        self._code_text = entry.episode.code
        self._images = images
        self._title_font = theme.font(layout.title_size, bold=True)
        self._code_font = theme.font(layout.code_size)
        self._outside = self._apply_appearance_mode(self.cget("bg_color"))
        self._on_open_show = on_open_show
        self._art_task: asyncio.Task[None] | None = None
        self._rebake_job: str | None = None  # a bake waiting for the width to settle
        # What the images were baked for: (height, scaling, poster area).
        self._baked_for: tuple[int, float, PosterArea] | None = None
        self._photos: dict[str, ImageTk.PhotoImage] = {}  # Tk keeps no reference itself
        self._size: tuple[int, int] = (0, 0)
        self._scaling = 1.0
        self._area = poster_area(layout, scale.px(NOMINAL_CARD_WIDTH))

        canvas = tk.Canvas(
            self, highlightthickness=0, borderwidth=0, background=self._outside, cursor=""
        )
        canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self._art = canvas
        surface, edge = theme.SURFACE, theme.EDGE
        self._middle = canvas.create_rectangle(0, 0, 0, 0, fill=surface, outline="", width=0)
        self._top_edge = canvas.create_rectangle(0, 0, 0, 0, fill=edge, outline="", width=0)
        self._bottom_edge = canvas.create_rectangle(0, 0, 0, 0, fill=edge, outline="", width=0)
        self._poster = canvas.create_image(0, 0, anchor="nw", tags=("open",))
        self._cap = canvas.create_image(0, 0, anchor="ne")
        self._title = canvas.create_text(
            0, 0, anchor="nw", text="", fill=theme.TEXT, tags=("open",)
        )
        self._code = canvas.create_text(0, 0, anchor="nw", text="", fill=theme.TEXT_MUTED)
        self._status = canvas.create_text(0, 0, anchor="w", text="", fill=theme.TEXT_MUTED)

        if on_open_show is not None:
            canvas.tag_bind("open", "<Button-1>", self._open)
            canvas.tag_bind("open", "<Enter>", lambda _event: canvas.configure(cursor="hand2"))
            canvas.tag_bind("open", "<Leave>", lambda _event: canvas.configure(cursor=""))

        # The bottom row: before the episode airs a muted line, else the Download row, a
        # widget over the canvas (created after it, so it stays on top).
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        if entry.episode.has_aired():
            self._control: DownloadControl[CalendarEntry] | None = _episode_control(
                self, entry, actions, layout.row_scale
            )
            self._grid_control()
        else:
            self._control = None
            canvas.itemconfigure(self._status, text=NOT_AIRED_TEXT)
        canvas.bind("<Configure>", self._on_resize)

    def _open(self, _event: Any = None) -> None:
        """A click on the poster or the name: the show's window."""
        if self._on_open_show is not None:
            self._on_open_show(self._show)

    def _grid_control(self) -> None:
        """The Download row along the bottom, from where the text starts."""
        if self._control is not None:
            self._control.grid(
                row=0,
                column=0,
                sticky="sew",
                padx=(self._area.text_x, self._layout.pad_right),
                pady=(0, self._layout.pad_bottom),
            )

    # -- layout --------------------------------------------------------------------------------

    def _on_resize(self, event: Any) -> None:
        width, height = int(event.width), int(event.height)
        if width <= 1 or height <= 1:
            return
        scaling: float = ctk.ScalingTracker.get_widget_scaling(self)
        self._size, self._scaling = (width, height), scaling
        layout, canvas = self._layout, self._art
        border = max(1, round(layout.border * scaling))
        canvas.coords(self._middle, 0, 0, width, height)
        canvas.coords(self._top_edge, 0, 0, width, border)
        canvas.coords(self._bottom_edge, 0, height - border, width, height)
        canvas.coords(self._cap, width, 0)
        area = poster_area(layout, width / scaling)
        if area != self._area:
            self._area = area
            self._grid_control()
        self._place_texts()
        wanted = (height, scaling, area)
        if self._baked_for == wanted:
            self._cancel_rebake()
        elif self._baked_for is not None and self._baked_for[:2] == (height, scaling):
            # Only the column's width changed: bake again once it settles (a window being
            # dragged wider changes it many times a second).
            self._cancel_rebake()
            self._rebake_job = self.after(REBAKE_MS, self._settled)
        else:
            self._bake()

    def _settled(self) -> None:
        self._rebake_job = None
        self._bake()

    def _bake(self) -> None:
        """Bake the images for the card's current size."""
        self._cancel_rebake()
        height, scaling, area = self._size[1], self._scaling, self._area
        self._baked_for = (height, scaling, area)
        if self._art_task is not None:
            self._art_task.cancel()
        self._art_task = spawn(self._load_art(height, scaling, area), name="EpisodeCard.art")

    def _cancel_rebake(self) -> None:
        if self._rebake_job is not None:
            self.after_cancel(self._rebake_job)
            self._rebake_job = None

    def _place_texts(self) -> None:
        """The name (fitted to the room left of the card's right edge), the code under it and
        "Not aired" in the bottom row, at the canvas's current size and scaling."""
        layout, canvas, scaling = self._layout, self._art, self._scaling
        width, height = self._size
        room = text_width(layout, self._area, width / scaling) - SLACK
        x = round(self._area.text_x * scaling)
        canvas.itemconfigure(
            self._title,
            text=card_title(self._show.name, room, self._title_font.measure),
            font=self._title_font.create_scaled_tuple(scaling),
        )
        top = round(layout.pad_top * scaling)
        canvas.coords(self._title, x, top)
        box = canvas.bbox(self._title)
        title_bottom = box[3] if box else top
        canvas.itemconfigure(
            self._code,
            text=elide_to_width(self._code_text, room, self._code_font.measure),
            font=self._code_font.create_scaled_tuple(scaling),
        )
        canvas.coords(self._code, x, title_bottom + round(layout.code_gap * scaling))
        if self._control is None:
            row_middle = layout.pad_bottom + layout.row_height / 2
            canvas.itemconfigure(
                self._status,
                text=elide_to_width(NOT_AIRED_TEXT, room, self._code_font.measure),
                font=self._code_font.create_scaled_tuple(scaling),
            )
            canvas.coords(self._status, x, height - round(row_middle * scaling))

    # -- images --------------------------------------------------------------------------------

    def _colors(self) -> dict[str, RGB]:
        return {
            "bg": _rgb(self, theme.SURFACE),
            "edge": _rgb(self, theme.EDGE),
            "outside": _rgb(self, self._outside),
        }

    async def _load_art(self, height: int, scaling: float, area: PosterArea) -> None:
        layout, colors = self._layout, self._colors()
        cap = cap_face(layout, scaling, height, **colors)
        face = poster_face(
            layout, area, scaling, height, placeholder=_rgb(self, theme.PLACEHOLDER), **colors
        )
        self._show_image("cap", self._cap, await shared_image(cap, cap))
        url = poster_url(self._show, face.size[0])
        # A cover baked again for a new width keeps the old one up until it is ready.
        if url is None or "poster" not in self._photos:
            self._show_image("poster", self._poster, await shared_image(face, _placeholder(face)))
        if url is None:
            return
        cover = await self._images.get(url, cover_size(face.size), finish=face)
        if cover is not None:
            self._show_image("poster", self._poster, cover)

    def _show_image(self, name: str, item: int, image: Image.Image) -> None:
        if not self.winfo_exists():
            return
        photo = ImageTk.PhotoImage(image, master=self._art)
        self._art.itemconfigure(item, image=photo)
        self._photos[name] = photo

    def destroy(self) -> None:
        self._cancel_rebake()
        if self._art_task is not None:
            self._art_task.cancel()
        super().destroy()


def _placeholder(face: PosterFace) -> Callable[[], Image.Image]:
    return lambda: compose_poster(None, face)


def _linespace(font: ctk.CTkFont) -> int:
    space: int = font.metrics("linespace")
    return space


class InfoIcon(ctk.CTkLabel):
    """An ⓘ in a card's top-right corner that calls `command` (the show's window)."""

    def __init__(self, card: ctk.CTkFrame, command: Callable[[], object]) -> None:
        super().__init__(
            card,
            text="ⓘ",
            width=24,
            height=24,
            font=theme.font(16),
            text_color=theme.TEXT_MUTED,
            cursor="hand2",
        )
        self._command = command
        add_tooltip(self, SHOW_TIP)
        self.place(relx=1.0, x=-8, y=8, anchor="ne")
        self.bind("<Button-1>", self._clicked)
        self.bind("<Enter>", lambda _event: self.configure(text_color=theme.TEXT))
        self.bind("<Leave>", self._restore)
        card.bind("<Leave>", self._restore)

    def _clicked(self, _event: Any = None) -> None:
        self._command()

    def _restore(self, _event: Any) -> None:
        self.configure(text_color=theme.TEXT_MUTED)


class CompactEpisodeCard(ctk.CTkFrame):
    """A tracked episode in two short lines (older days), name and code, over its Download
    row; its ⓘ opens the show's window (`on_open_show`)."""

    def __init__(
        self,
        master: Any,
        entry: CalendarEntry,
        *,
        actions: EpisodeActions,
        scale: CardScale = FULL,
        on_open_show: OpenShow | None = None,
    ) -> None:
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
        _episode_control(self, entry, actions).pack_row()

        inner = ctk.CTkFrame(self, fg_color="transparent")
        inner.pack(side="left", fill="both", expand=True, padx=8, pady=(4, 0))
        name_font = theme.font(scale.font(_NAME_SIZE, TITLE_MIN), bold=True)
        name = ctk.CTkLabel(
            inner,
            text=show.name,
            font=name_font,
            text_color=theme.TEXT,
            anchor="w",
            height=_COMPACT_NAME_HEIGHT,
        )
        name.pack(anchor="w")
        reserve = IMDB_ICON_ROOM if on_open_show is not None else 0
        _keep_elided(name, show.name, name_font, reserve=reserve)
        ctk.CTkLabel(
            inner,
            text=entry.episode.code,
            font=theme.font(scale.font(_CODE_SIZE, SMALL_TEXT_MIN)),
            text_color=theme.TEXT_MUTED,
            anchor="w",
            height=_COMPACT_CODE_HEIGHT,
        ).pack(anchor="w")

        self.info_icon: InfoIcon | None = None
        if on_open_show is not None:
            open_show = on_open_show
            self.info_icon = InfoIcon(self, lambda: open_show(show))


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
        scale: CardScale = FULL,
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
        code_font = theme.font(scale.font(_CODE_SIZE, SMALL_TEXT_MIN))

        inner = ctk.CTkFrame(self, fg_color="transparent")
        inner.pack(side="left", fill="both", expand=True, padx=8, pady=_SUGGESTION_PAD_Y)
        ctk.CTkLabel(
            inner,
            text="Suggested",
            font=theme.font(scale.font(_SUGGESTED_SIZE, SMALL_TEXT_MIN)),
            text_color=theme.TEXT_MUTED,
            anchor="w",
            height=_SUGGESTED_HEIGHT,
        ).pack(anchor="w")
        name_font = theme.font(scale.font(_NAME_SIZE, TITLE_MIN), bold=True)
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
            font=code_font,
            text_color="gray40",
            height=_COMPACT_CODE_HEIGHT,
        ).pack(side="left")
        self._track_button = ctk.CTkButton(
            bottom,
            text="+ Track",
            height=16,
            width=45,
            font=theme.font(scale.font(_CODE_SIZE, TEXT_MIN)),
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
