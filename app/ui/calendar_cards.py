"""Cards for the calendar grid: tracked episodes (full and compact) and suggested shows.

All three are drawn at the page's `CardScale` (Settings → Card size). The full card's sizes and
baked images are in app/ui/calendar_face.py; the compact and suggestion cards keep their look,
only their fonts follow the card size (never below the readable minimums).

A missing episode (app.services.missing's rule: aired at least a day ago, not downloaded, not
marked "I already have this"; the view hands the cards `missing`) gets an amber left edge and
an amber "Not downloaded yet" under its code, on the full and the compact cards alike; a new
one (aired less than a day ago, `new`) only a quiet blue "New" there. Clicking a show's
name (full cards: also the poster) opens its window (`on_open_show`), suggestions included.

Several episodes of one show on one day share a stack card: app/ui/calendar_stack.py, built on
EpisodeCard and the compact cards' small buttons (SlimDownload).
"""

from __future__ import annotations

import asyncio
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, cast

import customtkinter as ctk
from PIL import Image, ImageTk

from app.i18n import N_, t
from app.models import CalendarEntry, Show
from app.services.tvmaze import TVMazeError
from app.ui import theme
from app.ui.calendar_face import (
    MARK_WIDTH,
    STACK_BEHIND,
    TITLE_MAX_LINES,
    EpisodeLayout,
    PosterArea,
    PosterFace,
    StackEdges,
    cap_face,
    compose_poster,
    cover_size,
    episode_layout,
    poster_area,
    poster_face,
    shared_image,
    stack_step_px,
    text_width,
)
from app.ui.card_scale import FULL, SMALL_TEXT_MIN, TEXT_MIN, TITLE_MIN, CardScale
from app.ui.download_control import (
    CHOOSE_TEXT,
    DOWNLOAD_SHORT,
    DOWNLOAD_TIP,
    MORE_TEXT,
    MORE_TIP,
    Action,
    Base,
    DownloadActions,
    DownloadControl,
    RunState,
    control_look,
    episode_key,
    fit_text,
)
from app.ui.missing_control import MISSING_COLOR
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

# The texts are shown through t().
NOT_AIRED_TEXT = N_("Not aired")
SHOW_TIP = N_("More about this show")  # a card's show name (it opens the show's window)
# A missing episode's amber note, with shorter texts for when it doesn't fit.
MISSING_TEXTS = (N_("Not downloaded yet"), N_("Not downloaded"), N_("Missing"))
MISSING_TIP = N_(
    "Aired, but not downloaded yet. Already have it? Mark it in the show's window."
)
CHOOSE_TIP = N_("Nothing good enough was found. Choose a download yourself.")
# A new episode's quiet note (aired less than a day ago: not missing yet).
NEW_NOTE_TEXT = N_("New")
NEW_NOTE_TIP = N_(
    "Aired less than a day ago: a release usually turns up within hours. It counts as missing "
    "after a day."
)
WORKING_ICON = "…"
GRABBED_ICON = "✓"
# A full card's width before its first layout (for where the Download row starts meanwhile).
NOMINAL_CARD_WIDTH = 200
REBAKE_MS = 150  # after a column stops changing width, the poster piece is baked again
# TVMaze's "medium" posters are this wide; wider posters come from its "original" ones.
MEDIUM_POSTER_WIDTH = 210

# Compact cards (older days): one slim line, the name and the code with small buttons at the
# right; a missing episode adds the amber note under it and an amber bar on the left edge.
SLIM_LINE = 24  # the line's height
SLIM_ICON = 22  # the square ⬇ / ✓ button and the ⋯ beside it
SLIM_ICON_GAP = 4  # between the two buttons
SLIM_PAD_X = 8  # left of the text
SLIM_PAD_RIGHT = 5  # right of the buttons
SLIM_PAD_Y = 4
SLIM_CODE_GAP = 6  # between the name and the code
SLIM_BUTTONS_GAP = 6  # between the code and the buttons
SLIM_RADIUS = 8
NOTE_HEIGHT = 16  # the amber note's line
_ICON_RADIUS = 6
_ICON_SIZE = 13  # bold
_NOTE_SIZE = 10
_COMPACT_NAME_HEIGHT = 20  # the suggestions' name line
_COMPACT_CODE_HEIGHT = 16  # ... and their code line
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
        return Base("unavailable", t("Not Aired"))
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


def missing_note(room: float, measure: Measure) -> str:
    """A missing episode's amber note: the longest of MISSING_TEXTS that fits in `room`."""
    return fit_text([t(text) for text in MISSING_TEXTS], room, measure)


def new_note(room: float, measure: Measure) -> str:
    """A new episode's blue note ("New"), elided to `room`."""
    return elide_to_width(t(NEW_NOTE_TEXT), room, measure)


def compact_height(missing: bool) -> int:
    """A compact card's height: the slim line, and the note line of a missing (or new)
    episode."""
    return 2 * SLIM_PAD_Y + SLIM_LINE + (NOTE_HEIGHT if missing else 0)


def slim_name_room(row_width: float, code_width: float, buttons_width: float) -> float:
    """The room a compact card's name has in its line, `row_width` units wide: what the
    code (`code_width`), the gaps and the buttons (`buttons_width`) leave."""
    return row_width - SLIM_CODE_GAP - code_width - SLIM_BUTTONS_GAP - buttons_width - SLACK


@dataclass(frozen=True, slots=True)
class IconLook:
    """A compact card's download button: one character (`text`; "" = no button), its hover
    tip and colours, what a click does (None = disabled) and whether the ⋯ goes beside it."""

    text: str
    tip: str = ""
    # The accent's, read when the look is made (the palette is set at startup).
    color: theme.Color = field(default_factory=lambda: theme.ACCENT)
    hover: theme.Color = field(default_factory=lambda: theme.ACCENT_HOVER)
    text_color: theme.Color | None = None  # None = CTk's default (white)
    border: theme.Color | None = None
    action: Action | None = None
    more: bool = False


def buttons_width(look: IconLook) -> int:
    """How wide a compact card's buttons are in `look`: the button, and the ⋯ beside it."""
    width = SLIM_ICON if look.text else 0
    return width + (SLIM_ICON_GAP + SLIM_ICON if look.more else 0)


def icon_look(base: Base, state: RunState | None) -> IconLook:
    """The compact card's button, in the colours of the Download row (`control_look`): ⬇
    with the ⋯ beside it; ✓ once grabbed (a click opens the search window, as ✓ Grabbed
    does); … while a run works; ⋯ alone where the row offers a search ("Choose…" after
    nothing good enough was found, "Search" for a special); none before the episode airs."""
    look = control_look(base, state)
    if look.layout == "label":
        return IconLook(
            WORKING_ICON, look.text, "transparent", theme.SURFACE, theme.TEXT_MUTED
        )
    if look.action is None:
        return IconLook("")
    painted = IconLook(
        "",
        color=look.color,
        hover=look.hover,
        text_color=look.text_color,
        border=look.border,
        action=look.action,
    )
    if look.layout == "split":
        return replace(painted, text=DOWNLOAD_SHORT, tip=t(DOWNLOAD_TIP), more=True)
    if look.text.startswith(GRABBED_ICON):
        return replace(painted, text=GRABBED_ICON, tip=look.text)
    tip = t(CHOOSE_TIP) if look.text == t(CHOOSE_TEXT) else t(MORE_TIP)
    return replace(painted, text=MORE_TEXT, tip=tip)


def _rgb(widget: Any, color: theme.Color) -> RGB:
    red, green, blue = widget.winfo_rgb(theme.pick(color))  # 16 bits per channel
    return (red >> 8, green >> 8, blue >> 8)


# -- cards -----------------------------------------------------------------------------------


class EpisodeCard(ctk.CTkFrame):
    """A tracked episode (recent days and later) as a wide card: the show's poster fills the
    left side and fades into the card; the show's name, the episode code and the bottom row
    (a muted "Not aired", else the Download row) sit on the right, over the faded part.
    Clicking the poster or the name opens the show's window (`on_open_show`). A `missing`
    episode gets an amber left edge and the amber note under its code, a `new` one the blue
    "New" note.

    Everything but the Download row is drawn on one canvas (see app/ui/calendar_face.py): the
    baked poster piece and right cap, the stretch between them, and the texts. Switching
    between light and dark repaints them and bakes the images again (`_set_appearance_mode`).

    A stack card (app/ui/calendar_stack.py) builds on it: its layout has the cards behind it
    (`stack_depth`, past the front card's right and bottom edges) and its own bottom row.
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
        missing: bool = False,
        new: bool = False,
    ) -> None:
        self._missing = missing
        self._new = new and not missing
        layout = self._card_layout(scale, missing or self._new)
        super().__init__(
            master,
            fg_color=theme.SURFACE,
            corner_radius=0,
            border_width=0,
            height=self._card_height(layout),
        )
        self.grid_propagate(False)
        self.pack_propagate(False)
        self._layout = layout
        self._scale = scale
        self._actions = actions
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

        canvas = tk.Canvas(self, highlightthickness=0, borderwidth=0, cursor="")
        canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self._art = canvas
        self._middle = canvas.create_rectangle(0, 0, 0, 0, outline="", width=0)
        self._top_edge = canvas.create_rectangle(0, 0, 0, 0, outline="", width=0)
        self._bottom_edge = canvas.create_rectangle(0, 0, 0, 0, outline="", width=0)
        self._poster = canvas.create_image(0, 0, anchor="nw", tags=("open",))
        self._cap = canvas.create_image(0, 0, anchor="ne")
        self._title = canvas.create_text(0, 0, anchor="nw", text="", tags=("open",))
        self._code = canvas.create_text(0, 0, anchor="nw", text="")
        self._note = canvas.create_text(0, 0, anchor="nw", text="")
        self._status = canvas.create_text(0, 0, anchor="w", text="")
        self._paint_canvas()

        if on_open_show is not None:
            canvas.tag_bind("open", "<Button-1>", self._open)
            canvas.tag_bind("open", "<Enter>", lambda _event: canvas.configure(cursor="hand2"))
            canvas.tag_bind("open", "<Leave>", lambda _event: canvas.configure(cursor=""))

        # The bottom row: before the episode airs a muted line, else the Download row, a
        # widget over the canvas (created after it, so it stays on top).
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self._control: DownloadControl[CalendarEntry] | None = self._make_control(entry)
        if self._control is not None:
            self._grid_control()
        else:
            canvas.itemconfigure(self._status, text=t(NOT_AIRED_TEXT))
        canvas.bind("<Configure>", self._on_resize)

    # -- what a stack card does differently ----------------------------------------------------

    def _card_layout(self, scale: CardScale, missing: bool) -> EpisodeLayout:
        """The card's sizes (a missing or new episode has room for its note)."""
        return episode_layout(scale, linespace=_font_linespace, note=missing)

    def _card_height(self, layout: EpisodeLayout) -> int:
        """The card's height in CTk units: the card, and the cards behind a stack card."""
        return layout.height + layout.stack_depth

    def _make_control(self, entry: CalendarEntry) -> DownloadControl[CalendarEntry] | None:
        """The bottom row (not yet placed); None before the episode airs."""
        if not entry.episode.has_aired():
            return None
        return _episode_control(self, entry, self._actions, self._layout.row_scale)

    def _stack_edges(self, scaling: float) -> StackEdges | None:
        """The cards behind a stack card, in physical pixels (None: one episode)."""
        return None

    def _depth_px(self) -> int:
        """How far the cards behind a stack card reach past it, in physical pixels."""
        return STACK_BEHIND * stack_step_px(self._layout, self._scaling)

    def _front_size(self) -> tuple[int, int]:
        """The front card's size in physical pixels (the cards behind a stack card aside)."""
        depth = self._depth_px()
        return self._size[0] - depth, self._size[1] - depth

    def _paint_canvas(self) -> None:
        """The canvas's colours in the current mode (the baked images: `_bake`)."""
        canvas, pick = self._art, theme.pick
        canvas.configure(background=self._outside)
        canvas.itemconfigure(self._middle, fill=pick(theme.SURFACE))
        for edge in (self._top_edge, self._bottom_edge):
            canvas.itemconfigure(edge, fill=pick(theme.EDGE))
        canvas.itemconfigure(self._title, fill=pick(theme.TEXT))
        canvas.itemconfigure(self._code, fill=pick(theme.TEXT_MUTED))
        canvas.itemconfigure(self._note, fill=pick(theme.LINK if self._new else MISSING_COLOR))
        canvas.itemconfigure(self._status, fill=pick(theme.TEXT_MUTED))

    def _set_appearance_mode(self, mode_string: str) -> None:
        """Light ↔ dark: CTk repaints the Download row; the canvas and the baked images
        follow here."""
        super()._set_appearance_mode(mode_string)
        self._outside = self._apply_appearance_mode(self.cget("bg_color"))
        self._paint_canvas()
        if self._baked_for is not None:
            self._bake()

    def _open(self, _event: Any = None) -> None:
        """A click on the poster or the name: the show's window."""
        if self._on_open_show is not None:
            self._on_open_show(self._show)

    def _grid_control(self) -> None:
        """The Download row along the bottom, from where the text starts."""
        if self._control is not None:
            depth = self._layout.stack_depth
            self._control.grid(
                row=0,
                column=0,
                sticky="sew",
                padx=(self._area.text_x, self._layout.pad_right + depth),
                pady=(0, self._layout.pad_bottom + depth),
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
        front_width, front_height = self._front_size()
        canvas.coords(self._middle, 0, 0, front_width, front_height)
        canvas.coords(self._top_edge, 0, 0, front_width, border)
        canvas.coords(self._bottom_edge, 0, front_height - border, front_width, front_height)
        canvas.coords(self._cap, width, 0)
        self._place_behind()
        # The poster area follows the whole width, a stack's cards behind included: the texts
        # of a day's cards start at one x.
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

    def _place_behind(self) -> None:
        """A stack card's cards behind it (the canvas's straight stretch of them)."""

    def _text_room(self) -> float:
        """The room the texts have, in CTk units: from where they start to the front card's
        right edge, less its padding."""
        front_width = self._front_size()[0]
        return text_width(self._layout, self._area, front_width / self._scaling) - SLACK

    def _place_texts(self) -> None:
        """The name (fitted to the room left of the card's right edge), the code under it, a
        missing episode's note under that and "Not aired" in the bottom row, at the canvas's
        current size and scaling."""
        layout, canvas, scaling = self._layout, self._art, self._scaling
        height = self._front_size()[1]
        room = self._text_room()
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
        code_y = title_bottom + round(layout.code_gap * scaling)
        canvas.coords(self._code, x, code_y)
        if layout.note:
            box = canvas.bbox(self._code)
            code_bottom = box[3] if box else code_y
            note = new_note if self._new else missing_note
            canvas.itemconfigure(
                self._note,
                text=note(room, self._code_font.measure),
                font=self._code_font.create_scaled_tuple(scaling),
            )
            canvas.coords(self._note, x, code_bottom + round(layout.code_gap * scaling))
        if self._control is None:
            row_middle = layout.pad_bottom + layout.row_height / 2
            canvas.itemconfigure(
                self._status,
                text=elide_to_width(t(NOT_AIRED_TEXT), room, self._code_font.measure),
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
        stack = self._stack_edges(scaling)
        front_height = height - (stack.depth if stack is not None else 0)
        cap = cap_face(layout, scaling, front_height, stack=stack, **colors)
        mark = _rgb(self, MISSING_COLOR) if self._missing else None
        face = poster_face(
            layout,
            area,
            scaling,
            front_height,
            placeholder=_rgb(self, theme.PLACEHOLDER),
            mark=mark,
            stack=stack,
            **colors,
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


def _font_linespace(size: int, bold: bool) -> int:
    """The line height of the theme's font at `size` (for `episode_layout`)."""
    return _linespace(theme.font(size, bold=bold))


def _open_on_click(
    label: ctk.CTkLabel, show: Show, on_open_show: OpenShow | None
) -> Callable[[], None] | None:
    """Make a card's show name open the show's window (with a hover tip), when there is one.
    Returns what a click on it does."""
    if on_open_show is None:
        return None
    open_show = on_open_show

    def clicked() -> None:
        open_show(show)

    label.configure(cursor="hand2")
    label.bind("<Button-1>", lambda _event: clicked())
    add_tooltip(label, t(SHOW_TIP))
    return clicked


def _as_control(widget: SlimDownload) -> DownloadControl[Any]:
    """DownloadStates keeps DownloadControls, but only uses their `key`, `repaint()` and
    `winfo_exists()`, which SlimDownload has as well."""
    return cast(DownloadControl[Any], widget)


class SlimDownload(ctk.CTkFrame):
    """A compact card's download buttons (`icon_look`): a small ⬇ with a small ⋯ beside it,
    ✓ once grabbed, … while a run works. Follows the title's run state like the Download row
    (it attaches to the same DownloadStates)."""

    def __init__(self, master: Any, entry: CalendarEntry, actions: EpisodeActions) -> None:
        super().__init__(master, fg_color="transparent", width=SLIM_ICON, height=SLIM_ICON)
        episode = entry.episode
        self.key = episode_key(entry.show.id, episode.season, episode.number)
        self._item = entry
        self._base = episode_base(entry)
        self._actions = actions
        self._look: IconLook | None = None
        self._allow_more = True  # the ⋯ may show (a stack card's narrow row: right-click only)
        self.on_change: Callable[[], None] | None = None  # after the buttons change
        font = theme.font(_ICON_SIZE, bold=True)
        self._more = ctk.CTkButton(
            self,
            text=MORE_TEXT,
            width=SLIM_ICON,
            height=SLIM_ICON,
            corner_radius=_ICON_RADIUS,
            font=font,
            fg_color="transparent",
            hover_color=theme.HOVER,
            text_color=theme.BUTTON_TEXT,
            border_width=1,
            border_color=theme.EDGE,
            command=self.choose,
        )
        self.main = ctk.CTkButton(
            self,
            text="",
            width=SLIM_ICON,
            height=SLIM_ICON,
            corner_radius=_ICON_RADIUS,
            font=font,
            border_width=0,
            text_color_disabled=theme.TEXT_MUTED,
            command=self._click,
        )
        add_tooltip(self._more, MORE_TIP)
        self._main_tip = add_tooltip(self.main, "")
        self._default_text_color = self.main.cget("text_color")
        actions.states.attach(_as_control(self))
        self.repaint()

    @property
    def look(self) -> IconLook | None:
        return self._look

    @property
    def width_units(self) -> int:
        """How wide the buttons are now (`buttons_width`)."""
        if self._look is None:
            return 0
        return buttons_width(replace(self._look, more=self._look.more and self._allow_more))

    @property
    def shows_more(self) -> bool:
        """Whether the ⋯ is showing."""
        return bool(self._more.winfo_manager())

    def allow_more(self, allowed: bool) -> None:
        """Let the ⋯ show beside ⬇, or not (where the row has no room for it; a right-click
        still opens the search window)."""
        if allowed != self._allow_more:
            self._allow_more = allowed
            self._look = None  # paint again
            self.repaint()

    def repaint(self) -> None:
        look = icon_look(self._base, self._actions.states.get(self.key))
        if look == self._look:
            return
        self._look = look
        self._more.pack_forget()
        self.main.pack_forget()
        if look.more and self._allow_more:
            self._more.pack(side="right", padx=(SLIM_ICON_GAP, 0))
        if look.text:
            self.main.pack(side="right")
        self.main.configure(
            text=look.text,
            fg_color=look.color,
            hover_color=look.hover,
            text_color=look.text_color or self._default_text_color,
            border_width=1 if look.border else 0,
            border_color=look.border or theme.EDGE,
            state="normal" if look.action else "disabled",
        )
        self._main_tip.set_text(look.tip)
        if self.on_change is not None:
            self.on_change()

    def _click(self) -> None:
        look = self._look
        if look is None or look.action is None:
            return
        if look.action == "download":
            self._actions.download(self._item)
        else:
            self.choose()

    def choose(self) -> None:
        """The search window, to choose a download yourself (the ⋯, a right-click)."""
        self._actions.choose(self._item)

    def destroy(self) -> None:
        self._actions.states.detach(_as_control(self))
        super().destroy()


class CompactEpisodeCard(ctk.CTkFrame):
    """A tracked episode on one slim line (older days): the show's name and the episode code,
    then small buttons (SlimDownload). A `missing` episode adds the amber note under the line
    and an amber bar on the card's left edge, a `new` one only the blue "New" note. Clicking
    the name opens the show's window
    (`on_open_show`); a right-click anywhere opens the search window, as the ⋯ does."""

    def __init__(
        self,
        master: Any,
        entry: CalendarEntry,
        *,
        actions: EpisodeActions,
        scale: CardScale = FULL,
        on_open_show: OpenShow | None = None,
        missing: bool = False,
        new: bool = False,
    ) -> None:
        new = new and not missing
        height = compact_height(missing or new)
        super().__init__(
            master,
            fg_color=theme.SURFACE,
            border_color=theme.EDGE,
            border_width=1,
            corner_radius=SLIM_RADIUS,
            height=height,
        )
        self.pack_propagate(False)
        show = entry.show
        self.mark: ctk.CTkFrame | None = None
        if missing:
            self.mark = ctk.CTkFrame(
                self,
                width=MARK_WIDTH,
                height=height - 2 * SLIM_RADIUS,
                corner_radius=0,
                fg_color=MISSING_COLOR,
            )
            self.mark.place(x=0, y=SLIM_RADIUS)

        line = ctk.CTkFrame(self, fg_color="transparent", height=SLIM_LINE)
        line.pack(side="top", fill="x", padx=(SLIM_PAD_X, SLIM_PAD_RIGHT), pady=(SLIM_PAD_Y, 0))
        line.pack_propagate(False)
        self.buttons = SlimDownload(line, entry, actions)
        self.buttons.pack(side="right")
        name_font = theme.font(scale.font(_NAME_SIZE, TITLE_MIN), bold=True)
        code_font = theme.font(scale.font(_CODE_SIZE, SMALL_TEXT_MIN))
        self.name_label = ctk.CTkLabel(
            line, text=show.name, font=name_font, text_color=theme.TEXT, height=SLIM_LINE
        )
        self.name_label.pack(side="left")
        self.open_show = _open_on_click(self.name_label, show, on_open_show)
        code = ctk.CTkLabel(
            line,
            text=entry.episode.code,
            font=code_font,
            text_color=theme.TEXT_MUTED,
            height=SLIM_LINE,
        )
        code.pack(side="left", padx=(SLIM_CODE_GAP, 0))

        self.note: ctk.CTkLabel | None = None
        note_font = theme.font(scale.font(_NOTE_SIZE, SMALL_TEXT_MIN))
        if missing or new:
            self.note = ctk.CTkLabel(
                self,
                text=t(NEW_NOTE_TEXT) if new else t(MISSING_TEXTS[0]),
                font=note_font,
                text_color=theme.LINK if new else MISSING_COLOR,
                height=NOTE_HEIGHT,
                anchor="w",
            )
            self.note.pack(side="top", anchor="w", padx=SLIM_PAD_X)
            add_tooltip(self.note, t(NEW_NOTE_TIP) if new else t(MISSING_TIP))
        note_text = new_note if new else missing_note

        line_width: float | None = None  # unscaled, once laid out

        def fit() -> None:
            if line_width is None:
                return
            code_width = code_font.measure(entry.episode.code)
            room = slim_name_room(line_width, code_width, self.buttons.width_units)
            self.name_label.configure(text=elide_to_width(show.name, room, name_font.measure))
            if self.note is not None:
                self.note.configure(text=note_text(line_width - SLACK, note_font.measure))

        def resized(event: Any) -> None:
            nonlocal line_width
            if event.width > 1:
                line_width = unscaled_width(line, event.width)
                fit()

        line.bind("<Configure>", resized)
        self.buttons.on_change = fit  # ✓ needs less room than ⬇ with ⋯
        widgets: list[tk.Misc] = [self, *_descendants(self)]
        for widget in widgets:
            tk.Misc.bind(widget, "<Button-3>", self._right_click, add="+")

    def _right_click(self, _event: Any = None) -> None:
        self.buttons.choose()


def _descendants(widget: tk.Misc) -> list[tk.Misc]:
    found: list[tk.Misc] = []
    stack = list(widget.winfo_children())
    while stack:
        child = stack.pop()
        found.append(child)
        stack.extend(child.winfo_children())
    return found


class SuggestionCard(ctk.CTkFrame):
    """A popular untracked show airing that day, toned down below the tracked episodes, with a
    button to track it. Clicking the name opens the show's window (`on_open_show`)."""

    def __init__(
        self,
        master: Any,
        entry: CalendarEntry,
        *,
        library: LibraryService,
        status: StatusReporter,
        scale: CardScale = FULL,
        on_open_show: OpenShow | None = None,
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
            text=t("Suggested"),
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
        self.open_show = _open_on_click(name, entry.show, on_open_show)
        self.name_label = name

        bottom = ctk.CTkFrame(inner, fg_color="transparent", height=_COMPACT_CODE_HEIGHT)
        bottom.pack(side="bottom", fill="x")
        ctk.CTkLabel(
            bottom,
            text=entry.episode.code,
            font=code_font,
            text_color=theme.SUGGESTION_TEXT,
            height=_COMPACT_CODE_HEIGHT,
        ).pack(side="left")
        self._track_button = ctk.CTkButton(
            bottom,
            text=t("+ Track"),
            height=16,
            width=45,
            font=theme.font(scale.font(_CODE_SIZE, TEXT_MIN)),
            fg_color="transparent",
            border_width=1,
            border_color=theme.SUGGESTION_BUTTON_EDGE,
            text_color=theme.SUGGESTION_BUTTON_TEXT,
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
                self._track_button.configure(text=t("+ Track"), state="normal")
            self._status(
                t("Couldn't track {name}: {error}", name=self._show.name, error=exc),
                clear_after_ms=6000,
            )
