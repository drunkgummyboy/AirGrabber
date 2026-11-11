"""Cards for the calendar grid: tracked episodes (full and compact) and suggested shows.

All three are drawn at the page's `CardScale` (Settings → Card size). The full card's sizes and
baked images are in app/ui/calendar_face.py; the compact and suggestion cards keep their look,
only their fonts (and the suggestion's button) follow the card size (never below the readable
minimums).

The cards share the poster cards' look: one bottom row, a wide main button (filled accent = do
it, the poster cards' row height and corners) with at most one round button beside it (the
full card's ⋯, `RoundButton` as on My shows); the compact cards' round buttons (`RoundIcon`);
notes as pills (`CanvasPill` on the full card's canvas, `PillLabel` on the compact cards: a
small rounded outline with coloured text on the card's ground, `pill_image`).

While Plex is connected (app/services/plex.py; without it nothing is missing or new, owner's
decision for 3.1.0), a missing episode (app.services.missing's rule: aired at least a day ago,
neither in Plex nor downloaded; the view hands the cards `missing`) gets an amber "Not
downloaded yet" pill under its code, on the full and the compact cards alike; a new one (aired
less than a day ago, `new`) a blue "New" pill there instead; one you have (`have`) a green
"✓ In Plex", or a blue "✓ Downloaded" while it hasn't reached Plex yet. These come from the
database, so they outlive a restart, unlike a run's outcome (the ✓ button). Clicking a show's
name (full cards: also the poster) opens its window (`on_open_show`), suggestions included.

Several episodes of one show on one day share a stack card: app/ui/calendar_stack.py, built on
EpisodeCard and the compact cards' small buttons (SlimDownload).
"""

from __future__ import annotations

import asyncio
import tkinter as tk
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, Literal, cast

import customtkinter as ctk
from PIL import Image, ImageTk

from app.i18n import N_, t
from app.models import CalendarEntry, Show
from app.services.tvmaze import TVMazeError
from app.ui import theme
from app.ui.calendar_face import (
    PILL_PAD_X,
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
from app.ui.card_scale import FULL, SMALL_TEXT_MIN, TITLE_MIN, CardScale
from app.ui.cards import MENU_BUTTON_MIN, MENU_BUTTON_SIZE, RoundButton
from app.ui.download_control import (
    CHOOSE_TEXT,
    DOWNLOAD_SHORT,
    DOWNLOAD_TIP,
    GAP,
    MORE_TEXT,
    MORE_TIP,
    Action,
    Base,
    DownloadActions,
    DownloadControl,
    Look,
    RunState,
    control_look,
    episode_key,
    fit_text,
)
from app.ui.missing_control import MISSING_COLOR
from app.ui.poster_card import TrackButton, pill_image, row_font_size, row_height
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
MISSING_TIP = N_("Aired, but not in Plex and not downloaded yet.")
CHOOSE_TIP = N_("Nothing good enough was found. Choose a download yourself.")
# A new episode's quiet note (aired less than a day ago: not missing yet).
NEW_NOTE_TEXT = N_("New")
NEW_NOTE_TIP = N_(
    "Aired less than a day ago: a release usually turns up within hours. It counts as missing "
    "after a day."
)
# An episode you have: in Plex, or downloaded and on its way there (with Plex only).
HaveMark = Literal["plex", "downloaded"]
HAVE_TEXTS: dict[HaveMark, str] = {"plex": N_("✓ In Plex"), "downloaded": N_("✓ Downloaded")}
HAVE_TIPS: dict[HaveMark, str] = {
    "plex": N_("This episode is in your Plex."),
    "downloaded": N_("Downloaded by AirGrabber. Plex doesn't have it yet."),
}


def have_color(have: HaveMark) -> theme.Color:
    """A have-mark's pill: green for in Plex (yours), blue for downloaded (information: on its
    way). Read when shown (the palette is set at startup)."""
    return theme.SUCCESS if have == "plex" else theme.BRAND


def new_color() -> theme.Color:
    """A new episode's pill: blue, information (not something missing). Read when shown."""
    return theme.BRAND


WORKING_ICON = "…"
GRABBED_ICON = "✓"
# A full card's width before its first layout (for where the Download row starts meanwhile).
NOMINAL_CARD_WIDTH = 200
REBAKE_MS = 150  # after a column stops changing width, the poster piece is baked again
# TVMaze's "medium" posters are this wide; wider posters come from its "original" ones.
MEDIUM_POSTER_WIDTH = 210

# Compact cards (older days): one slim line, the name and the code with a round button at the
# right; a note adds its pill under it.
SLIM_LINE = 24  # the line's height
SLIM_ICON = 24  # the round ⬇ / ✓ button (the full cards' round ⋯ at XL), and a row's ⋯
SLIM_ICON_GAP = 4  # between the two buttons (a stack card's unfolded rows)
SLIM_PAD_X = 8  # left of the text
SLIM_PAD_RIGHT = 5  # right of the buttons
SLIM_PAD_Y = 4
SLIM_CODE_GAP = 6  # between the name and the code
SLIM_BUTTONS_GAP = 6  # between the code and the buttons
SLIM_RADIUS = 8
NOTE_HEIGHT = 16  # the note pill's line
SLIM_PILL_PAD_X = 6  # a compact card's pill: its room left and right of its text
PILL_BORDER = 1  # a pill's outline, in CTk units
_ICON_SIZE = 13  # bold
_NOTE_SIZE = 10  # bold
_COMPACT_NAME_HEIGHT = 20  # the suggestions' name line
_COMPACT_CODE_HEIGHT = 16  # ... and their code line
_NAME_SIZE = 11  # bold
_CODE_SIZE = 9
# Suggestions: a "Suggested" line (with the source, `with_source`), the name, the code, then
# the + Follow button (the poster cards' row, `row_height`).
_SUGGESTED_HEIGHT = 16
_SUGGESTED_SIZE = 11
_SUGGESTION_PAD_Y = 6
_SUGGESTION_NAME_GAP = 2  # between the name and the code
_SUGGESTION_ROW_GAP = 6  # between the code and the button


# -- shared parts ----------------------------------------------------------------------------


def with_source(text: str, show: Show) -> str:
    """`text` with where the show airs behind it: "S09E05 · Netflix", the source going first when
    the line is cut. Just `text` for a show without one."""
    return f"{text} · {show.network}" if show.network else text


def _keep_elided(label: ctk.CTkLabel, text: str, font: ctk.CTkFont, *, reserve: float = 0) -> None:
    """Show `text` on one line in `label`, elided to its parent's width less `reserve`."""

    def fit(event: Any) -> None:
        if event.width > 1:
            width = unscaled_width(label, event.width) - reserve - SLACK
            label.configure(text=elide_to_width(text, width, font.measure))

    label.master.bind("<Configure>", fit)


def episode_base(entry: CalendarEntry) -> Base:
    """A disabled Not Aired; Search for an aired special, which has no episode number to
    download by; else the Download control (also for an episode downloaded before: that is
    never shown)."""
    if not entry.episode.has_aired():
        return Base("unavailable", t("Not Aired"))
    if entry.episode.number is None:
        return Base("search")
    return Base("download")


class EpisodeControl(DownloadControl[CalendarEntry]):
    """A full card's Download button: the poster cards' row (its height and corners) without
    its own ⋯; the card puts its round ⋯ beside it while the row offers one (`more_wanted`: the
    split look, "⬇ Download" with "Choose a download yourself"; `on_more` hears a change)."""

    def __init__(
        self, master: Any, entry: CalendarEntry, actions: EpisodeActions, scale: CardScale = FULL
    ) -> None:
        # Read by `current_look`, which the set-up already calls.
        self.more_wanted = False
        self.on_more: Callable[[], None] | None = None
        episode = entry.episode
        super().__init__(
            master,
            item=entry,
            key=episode_key(entry.show.id, episode.season, episode.number),
            base=episode_base(entry),
            actions=actions,
            scale=scale,
        )

    def current_look(self) -> Look:
        """The row's look, a split one as one button (the ⋯ is the card's)."""
        look = super().current_look()
        wanted = look.layout == "split"
        if wanted != self.more_wanted:
            self.more_wanted = wanted
            if self.on_more is not None:
                self.on_more()
        return replace(look, layout="button") if wanted else look


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


def have_mark(
    ids: Iterable[int], in_plex: Collection[int], downloaded: Collection[int]
) -> HaveMark | None:
    """What a card says about the episodes it stands for: "plex" when all are in Plex,
    "downloaded" when all are in Plex or downloaded, else nothing (a stack's rows tell apart)."""
    wanted = set(ids)
    if not wanted:
        return None
    if wanted <= set(in_plex):
        return "plex"
    return "downloaded" if wanted <= set(in_plex) | set(downloaded) else None


@dataclass(frozen=True, slots=True)
class CardNote:
    """The note under a card's code (a pill): the text to start with, how it fits a width, its
    colour (the pill's outline and text) and its tip (not translated yet, like the
    constants)."""

    text: str
    fit: Callable[[float, Measure], str]
    color: theme.Color
    tip: str


def card_note(
    *, missing: bool = False, new: bool = False, have: HaveMark | None = None
) -> CardNote | None:
    """A card's note: missing wins, then new, then what you have."""
    if missing:
        return CardNote(MISSING_TEXTS[0], missing_note, MISSING_COLOR, MISSING_TIP)
    if new:
        return CardNote(NEW_NOTE_TEXT, new_note, new_color(), NEW_NOTE_TIP)
    if have is not None:
        text = HAVE_TEXTS[have]
        return CardNote(
            text,
            lambda room, measure: elide_to_width(t(text), room, measure),
            have_color(have),
            HAVE_TIPS[have],
        )
    return None


def compact_height(noted: bool) -> int:
    """A compact card's height: the slim line, and the line of a note's pill."""
    return 2 * SLIM_PAD_Y + SLIM_LINE + (NOTE_HEIGHT if noted else 0)


def suggestion_height(scale: CardScale = FULL) -> int:
    """A suggestion card's height: the "Suggested" line, the name, the code and the poster
    cards' row for its + Follow button."""
    return (
        2 * _SUGGESTION_PAD_Y
        + _SUGGESTED_HEIGHT
        + _COMPACT_NAME_HEIGHT
        + _SUGGESTION_NAME_GAP
        + _COMPACT_CODE_HEIGHT
        + _SUGGESTION_ROW_GAP
        + row_height(scale)
    )


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
    with the ⋯ beside it; ✓ once a run grabbed it (a click opens the search window, as the
    outcome's button does); … while a run works; ⋯ alone where the row offers a search
    ("Choose…" after nothing good enough was found, "Search" for a special); none before the
    episode airs."""
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


def _scaling(widget: tk.Misc) -> float:
    scaling: float = ctk.ScalingTracker.get_widget_scaling(widget)
    return scaling


# -- pills and round buttons ------------------------------------------------------------------


def pill_width(text: str, measure: Measure, pad_x: float = PILL_PAD_X) -> float:
    """How wide a pill around `text` is, in CTk units."""
    return measure(text) + 2 * pad_x


def _pill_photo(
    widget: tk.Misc,
    size: tuple[int, int],
    scaling: float,
    color: theme.Color,
    ground: theme.Color,
) -> ImageTk.PhotoImage:
    """A pill's outline in `color` on `ground` (inside too), `size` physical pixels."""
    border = max(1, round(PILL_BORDER * scaling))
    image = pill_image(size, border, _rgb(widget, ground), _rgb(widget, color))
    return ImageTk.PhotoImage(image, master=widget)


class CanvasPill:
    """A pill drawn on a card's canvas (the full cards): the outline (`pill_image`: small, made
    on the loop) with its text, a canvas item, on top. `draw` places it (each layout, and after a
    switch between light and dark: the colours are read when drawn)."""

    def __init__(self, canvas: tk.Canvas, *, tags: tuple[str, ...] = ()) -> None:
        self._canvas = canvas
        self.image = canvas.create_image(0, 0, anchor="nw", tags=tags)
        self.text = canvas.create_text(0, 0, anchor="w", text="", tags=tags)
        self._photo: ImageTk.PhotoImage | None = None
        self._drawn: tuple[object, ...] | None = None

    def draw(
        self,
        x: int,
        y: int,
        text: str,
        *,
        font: ctk.CTkFont,
        height: int,
        scaling: float,
        color: theme.Color,
        ground: theme.Color | None = None,
    ) -> tuple[int, int]:
        """The pill with `text` at (`x`, `y`) in physical pixels, `height` CTk units tall.
        Returns its size in physical pixels."""
        canvas = self._canvas
        ground = theme.SURFACE if ground is None else ground
        width = pill_width(text, font.measure)
        size = (max(1, round(width * scaling)), max(1, round(height * scaling)))
        drawn = (size, scaling, theme.pick(color), theme.pick(ground))
        if drawn != self._drawn:
            self._drawn = drawn
            self._photo = _pill_photo(canvas, size, scaling, color, ground)
            canvas.itemconfigure(self.image, image=self._photo)
        canvas.coords(self.image, x, y)
        canvas.itemconfigure(
            self.text,
            text=text,
            font=font.create_scaled_tuple(scaling),
            fill=theme.pick(color),
        )
        canvas.coords(self.text, x + round(PILL_PAD_X * scaling), y + size[1] / 2)
        return size


class PillLabel(tk.Canvas):
    """A pill as a widget of its own (the compact cards' notes and count): the outline
    (`pill_image`) in `color` on the card's `ground`, the text in `color`; `height` CTk units
    tall, as wide as its text. Repaints after a switch between light and dark."""

    def __init__(
        self,
        master: Any,
        *,
        text: str,
        color: theme.Color,
        font: ctk.CTkFont,
        height: int,
        pad_x: int = SLIM_PILL_PAD_X,
        ground: theme.Color | None = None,
    ) -> None:
        super().__init__(master, highlightthickness=0, borderwidth=0)
        self._text = text
        self._color = color
        self._font = font
        self._height = height
        self._pad_x = pad_x
        self._ground = theme.SURFACE if ground is None else ground  # read now: the accent's
        self._photo: ImageTk.PhotoImage | None = None
        self._image = self.create_image(0, 0, anchor="nw")
        self._label = self.create_text(0, 0, anchor="w", text="")
        self.redraw()
        theme.on_mode_change(self, self.redraw)

    @property
    def text(self) -> str:
        return self._text

    @property
    def color(self) -> theme.Color:
        return self._color

    @property
    def font(self) -> ctk.CTkFont:
        return self._font

    def width_of(self, text: str) -> float:
        """How wide the pill is around `text`, in CTk units."""
        return pill_width(text, self._font.measure, self._pad_x)

    def set_text(self, text: str) -> None:
        if text != self._text:
            self._text = text
            self.redraw()

    def redraw(self) -> None:
        """Draw the pill at the current scaling, in the current mode's colours."""
        scaling = _scaling(self.master)
        size = (
            max(1, round(self.width_of(self._text) * scaling)),
            max(1, round(self._height * scaling)),
        )
        self.configure(width=size[0], height=size[1], background=theme.pick(self._ground))
        self._photo = _pill_photo(self, size, scaling, self._color, self._ground)
        self.itemconfigure(self._image, image=self._photo)
        self.itemconfigure(
            self._label,
            text=self._text,
            font=self._font.create_scaled_tuple(scaling),
            fill=theme.pick(self._color),
        )
        self.coords(self._label, round(self._pad_x * scaling), size[1] / 2)


class RoundIcon(tk.Canvas):
    """A small round button on a card's ground (the compact cards' ⬇ / ✓ / …, an unfolded
    row's ⋯), drawn like the poster cards' round buttons (`pill_image`, a circle): a CTkButton
    grows wider than it is tall around any text. `set_look` paints it: `fill` ("transparent" =
    the ground) inside a 1-unit `edge` ring (the fill's own colour without one), `hover` under
    the pointer, all only while enabled. `size` is its diameter in CTk units. Repaints after a
    switch between light and dark."""

    def __init__(
        self,
        master: Any,
        *,
        size: int,
        font: ctk.CTkFont,
        command: Callable[[], None],
        ground: theme.Color | None = None,
    ) -> None:
        super().__init__(master, highlightthickness=0, borderwidth=0)
        self._size = size
        self._font = font
        self._command = command
        self._ground = theme.SURFACE if ground is None else ground  # read now: the accent's
        self._text = ""
        self._fill: theme.Color = theme.ACCENT
        self._hover: theme.Color = theme.ACCENT_HOVER
        self._edge: theme.Color | None = None
        self._text_color: theme.Color | None = None
        self._enabled = True
        self._hovered = False
        self._images: tuple[ImageTk.PhotoImage, ImageTk.PhotoImage] | None = None
        self._ring = self.create_image(0, 0, anchor="nw")
        self._label = self.create_text(0, 0, anchor="center", text="")
        self.bind("<Enter>", lambda _event: self._hover_to(True), add="+")
        self.bind("<Leave>", lambda _event: self._hover_to(False), add="+")
        self.bind("<Button-1>", lambda _event: self.invoke(), add="+")
        self.redraw()
        theme.on_mode_change(self, self.redraw)

    @property
    def text(self) -> str:
        return self._text

    @property
    def fill(self) -> theme.Color:
        return self._fill

    @property
    def edge(self) -> theme.Color | None:
        return self._edge

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_look(
        self,
        text: str,
        *,
        fill: theme.Color,
        hover: theme.Color,
        text_color: theme.Color | None = None,
        edge: theme.Color | None = None,
        enabled: bool = True,
    ) -> None:
        """`text_color` None: the text on an accent button (ON_ACCENT); muted while disabled."""
        look = (text, fill, hover, text_color, edge, enabled)
        if look == (
            self._text, self._fill, self._hover, self._text_color, self._edge, self._enabled
        ):
            return
        self._text, self._fill, self._hover, self._text_color, self._edge, self._enabled = look
        self.redraw()

    def invoke(self) -> None:
        """What a click does (nothing while disabled)."""
        if self._enabled:
            self._command()

    def _colour(self, color: theme.Color) -> theme.Color:
        return self._ground if color == "transparent" else color

    def redraw(self) -> None:
        """Draw the button at the current scaling, in the current mode's colours."""
        scaling = _scaling(self.master)
        diameter = max(1, round(self._size * scaling))
        border = max(1, round(scaling))
        fill = _rgb(self, self._colour(self._fill))
        hover = _rgb(self, self._colour(self._hover))
        edge = _rgb(self, self._edge) if self._edge is not None else fill
        hover_edge = _rgb(self, self._edge) if self._edge is not None else hover
        size = (diameter, diameter)
        self._images = (
            ImageTk.PhotoImage(pill_image(size, border, fill, edge), master=self),
            ImageTk.PhotoImage(pill_image(size, border, hover, hover_edge), master=self),
        )
        if not self._enabled:
            text_color: theme.Color = self._text_color or theme.TEXT_MUTED
        else:
            text_color = self._text_color or theme.ON_ACCENT
        self.configure(
            width=diameter,
            height=diameter,
            background=theme.pick(self._ground),
            cursor="hand2" if self._enabled else "",
        )
        self.coords(self._label, diameter / 2, diameter / 2)
        self.itemconfigure(
            self._label,
            text=self._text,
            font=self._font.create_scaled_tuple(scaling),
            fill=theme.pick(text_color),
        )
        self._paint()

    def _hover_to(self, on: bool) -> None:
        self._hovered = on
        self._paint()

    def _paint(self) -> None:
        if self._images is not None:
            hovered = self._hovered and self._enabled
            self.itemconfigure(self._ring, image=self._images[1 if hovered else 0])


# -- cards -----------------------------------------------------------------------------------


class EpisodeCard(ctk.CTkFrame):
    """A tracked episode (recent days and later) as a wide card: the show's poster fills the
    left side and fades into the card; the show's name, the episode code and the bottom row
    (a muted "Not aired", else the Download row: the poster cards' row, a wide ⬇ Download with
    the round ⋯ beside it) sit on the right, over the faded part. Clicking the poster or the
    name opens the show's window (`on_open_show`). A `missing` episode gets the amber "Not
    downloaded yet" pill under its code, a `new` one the blue "New" pill, one you have `have`'s
    (all only with Plex: the view passes them).

    Everything but the bottom row is drawn on one canvas (see app/ui/calendar_face.py): the
    baked poster piece and right cap, the stretch between them, the texts and the pills.
    Switching between light and dark repaints them and bakes the images again
    (`_set_appearance_mode`).

    A stack card (app/ui/calendar_stack.py) builds on it: its layout has the cards behind it
    (`stack_depth`, past the front card's right and bottom edges), its own bottom row and its
    own ⋯ (`_wants_menu`, `_menu_clicked`).
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
        have: HaveMark | None = None,
        not_aired_text: str | None = None,
    ) -> None:
        """`not_aired_text`: said instead of "Not aired" (the "Coming up" row's date)."""
        self._not_aired = t(NOT_AIRED_TEXT) if not_aired_text is None else not_aired_text
        self._card_note = card_note(missing=missing, new=new, have=have)
        layout = self._card_layout(scale, self._card_note is not None)
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
        self._entry = entry
        self._show = entry.show
        self._code_text = with_source(entry.episode.code, entry.show)
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
        self._note_pill = CanvasPill(canvas)
        self._note = self._note_pill.text
        self._pill_font = theme.font(layout.pill_size or layout.code_size, bold=True)
        self._status = canvas.create_text(0, 0, anchor="w", text="")
        self._paint_canvas()

        if on_open_show is not None:
            canvas.tag_bind("open", "<Button-1>", self._open)
            canvas.tag_bind("open", "<Enter>", lambda _event: canvas.configure(cursor="hand2"))
            canvas.tag_bind("open", "<Leave>", lambda _event: canvas.configure(cursor=""))

        # The bottom row: before the episode airs a muted line, else the Download row with the
        # round ⋯ beside it, widgets over the canvas (made after it, so they stay on top).
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=1)
        self._row = ctk.CTkFrame(self, fg_color="transparent", height=layout.row_height)
        self._row.pack_propagate(False)
        self._control: DownloadControl[CalendarEntry] | None = self._make_control(
            self._row, entry
        )
        self.menu_button: RoundButton | None = None
        if self._control is not None:
            self.menu_button = RoundButton(
                self._row,
                text=MORE_TEXT,
                size=layout.row_scale.px(MENU_BUTTON_SIZE, minimum=MENU_BUTTON_MIN),
                font=theme.font(row_font_size(layout.row_scale), bold=True),
                command=self._menu_clicked,
                bg=theme.SURFACE,
            )
            add_tooltip(self.menu_button, self._menu_tip())
            self._control.pack(side="left", fill="both", expand=True)
            if isinstance(self._control, EpisodeControl):
                self._control.on_more = self._sync_menu
            self._sync_menu()
            self._grid_control()
        else:
            canvas.itemconfigure(self._status, text=self._not_aired)
        canvas.bind("<Configure>", self._on_resize)

    # -- what a stack card does differently ----------------------------------------------------

    def _card_layout(self, scale: CardScale, noted: bool) -> EpisodeLayout:
        """The card's sizes (with room for a note)."""
        return episode_layout(scale, linespace=_font_linespace, note=noted)

    def _card_height(self, layout: EpisodeLayout) -> int:
        """The card's height in CTk units: the card, and the cards behind a stack card."""
        return layout.height + layout.stack_depth

    def _make_control(
        self, master: ctk.CTkFrame, entry: CalendarEntry
    ) -> DownloadControl[CalendarEntry] | None:
        """The bottom row's main button in `master` (not yet placed); None before the episode
        airs."""
        if not entry.episode.has_aired():
            return None
        return EpisodeControl(master, entry, self._actions, self._layout.row_scale)

    def _wants_menu(self) -> bool:
        """Whether the round ⋯ shows beside the main button: while the Download row offers
        a choice of your own."""
        control = self._control
        return isinstance(control, EpisodeControl) and control.more_wanted

    def _menu_tip(self) -> str:
        return t(MORE_TIP)

    def _menu_alone(self) -> bool:
        """Whether the main button beside the round ⋯ shows nothing (a stack whose episodes
        are all had): the ⋯ then sits in the bottom corner, where the row's bottom is."""
        return False

    def _menu_clicked(self) -> None:
        """The ⋯: the search window, to choose a download yourself."""
        self._actions.choose(self._entry)

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
        canvas.itemconfigure(self._status, fill=pick(theme.TEXT_MUTED))

    def _set_appearance_mode(self, mode_string: str) -> None:
        """Light ↔ dark: CTk repaints the Download row (the ⋯ itself); the canvas, its pills
        and the baked images follow here."""
        super()._set_appearance_mode(mode_string)
        self._outside = self._apply_appearance_mode(self.cget("bg_color"))
        self._paint_canvas()
        if self._size[0] > 1:
            self._place_texts()  # the pills, in the new mode's colours
        if self._baked_for is not None:
            self._bake()

    def _set_scaling(self, *args: Any, **kwargs: Any) -> None:
        """Windows' scaling changed (another screen): CTk resizes the widgets, the canvas
        follows at its next layout; the round ⋯ (plain Tk) is drawn again here."""
        super()._set_scaling(*args, **kwargs)
        if self.menu_button is not None:
            self.menu_button.rescale()

    def _open(self, _event: Any = None) -> None:
        """A click on the poster or the name: the show's window."""
        if self._on_open_show is not None:
            self._on_open_show(self._show)

    def _sync_menu(self) -> None:
        """Show the round ⋯ beside the main button, or not (`_wants_menu`)."""
        button, control = self.menu_button, self._control
        if button is None or control is None:
            return
        if self._wants_menu():
            # Its room first. Alone, at the row's bottom, as the main button's bottom would be;
            # else level with the main button's middle.
            if self._menu_alone():
                button.pack(side="right", anchor="s", before=control)
            else:
                button.pack(side="right", anchor="center", before=control)
            control.pack(side="left", fill="both", expand=True, padx=(0, self._scale.px(GAP)))
        elif button.winfo_manager():
            button.pack_forget()
            control.pack(side="left", fill="both", expand=True, padx=0)

    @property
    def shows_menu(self) -> bool:
        """Whether the round ⋯ is showing."""
        return self.menu_button is not None and bool(self.menu_button.winfo_manager())

    def _grid_control(self) -> None:
        """The bottom row along the bottom, from where the text starts."""
        if self._control is not None:
            depth = self._layout.stack_depth
            self._row.grid(
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
        """The name (fitted to the room left of the card's right edge), the code under it, the
        note's pill under that and "Not aired" in the bottom row, at the canvas's current size
        and scaling."""
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
        if self._card_note is not None:
            box = canvas.bbox(self._code)
            code_bottom = box[3] if box else code_y
            note = self._card_note
            self._note_pill.draw(
                x,
                code_bottom + round(layout.pill_gap * scaling),
                note.fit(room - 2 * PILL_PAD_X, self._pill_font.measure),
                font=self._pill_font,
                height=layout.pill_height,
                scaling=scaling,
                color=note.color,
            )
        if self._control is None:
            row_middle = layout.pad_bottom + layout.row_height / 2
            canvas.itemconfigure(
                self._status,
                text=elide_to_width(self._not_aired, room, self._code_font.measure),
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
        face = poster_face(
            layout,
            area,
            scaling,
            front_height,
            placeholder=_rgb(self, theme.PLACEHOLDER),
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

    def note_bottom(self) -> int | None:
        """Where the note's pill ends on the canvas, in physical pixels (None: no note)."""
        if self._card_note is None:
            return None
        box = self._art.bbox(self._note_pill.image)
        return int(box[3]) if box else None

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

    label.configure(cursor="hand2")  # CTk gives it only to the inner tk.Label
    # The label's frame too, and with it the canvas around the text (its cursor "" follows
    # the frame's), which the click binding covers as well.
    tk.Frame.configure(label, cursor="hand2")
    label.bind("<Button-1>", lambda _event: clicked())
    add_tooltip(label, t(SHOW_TIP))
    return clicked


def _as_control(widget: SlimDownload) -> DownloadControl[Any]:
    """DownloadStates keeps DownloadControls, but only uses their `key`, `repaint()` and
    `winfo_exists()`, which SlimDownload has as well."""
    return cast(DownloadControl[Any], widget)


class SlimDownload(ctk.CTkFrame):
    """A compact card's download buttons (`icon_look`), round (`RoundIcon`): ⬇ (filled
    accent) with a ⋯ ring beside it (a stack card's unfolded rows, where there is room; the
    compact card has none: its right-click), ✓ once a run grabbed it, … while a run works.
    Follows the title's run state like the Download row (it attaches to the same
    DownloadStates)."""

    def __init__(self, master: Any, entry: CalendarEntry, actions: EpisodeActions) -> None:
        super().__init__(master, fg_color="transparent", width=SLIM_ICON, height=SLIM_ICON)
        episode = entry.episode
        self.key = episode_key(entry.show.id, episode.season, episode.number)
        self._item = entry
        self._base = episode_base(entry)
        self._actions = actions
        self._look: IconLook | None = None
        self._allow_more = True  # the ⋯ may show (a narrow row, the compact card: right-click)
        self.on_change: Callable[[], None] | None = None  # after the buttons change
        font = theme.font(_ICON_SIZE, bold=True)
        self._more = RoundIcon(self, size=SLIM_ICON, font=font, command=self.choose)
        self._more.set_look(
            MORE_TEXT,
            fill="transparent",
            hover=theme.HOVER,
            text_color=theme.BUTTON_TEXT,
            edge=theme.EDGE,
        )
        self.main = RoundIcon(self, size=SLIM_ICON, font=font, command=self._click)
        add_tooltip(self._more, t(MORE_TIP))
        self._main_tip = add_tooltip(self.main, "")
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
            gap = round(SLIM_ICON_GAP * _scaling(self))  # plain Tk: in physical pixels
            self._more.pack(side="right", padx=(gap, 0))
        if look.text:
            self.main.pack(side="right")
        self.main.set_look(
            look.text,
            fill=look.color,
            hover=look.hover,
            text_color=look.text_color,
            edge=look.border,
            enabled=look.action is not None,
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

    def _set_scaling(self, *args: Any, **kwargs: Any) -> None:
        """Windows' scaling changed: the round buttons (plain Tk) are drawn again."""
        super()._set_scaling(*args, **kwargs)
        self.main.redraw()
        self._more.redraw()
        self._look = None  # packed again, with the gap at the new scaling
        self.repaint()

    def destroy(self) -> None:
        self._actions.states.detach(_as_control(self))
        super().destroy()


def _note_pill(master: Any, note: CardNote, scale: CardScale) -> PillLabel:
    """A compact card's note as a small pill (packed by the caller), with its tip."""
    pill = PillLabel(
        master,
        text=t(note.text),
        color=note.color,
        font=theme.font(scale.font(_NOTE_SIZE, SMALL_TEXT_MIN), bold=True),
        height=NOTE_HEIGHT,
    )
    add_tooltip(pill, t(note.tip))
    return pill


def _pack_note(pill: PillLabel, card: ctk.CTkFrame) -> None:
    """The note's pill on its line under the slim line, from where the name starts."""
    pill.pack(side="top", anchor="w", padx=round(SLIM_PAD_X * _scaling(card)))


def fit_note(pill: PillLabel, note: CardNote, line_width: float) -> None:
    """The note's longest text that fits a compact card's line, `line_width` units wide."""
    room = line_width - 2 * SLIM_PILL_PAD_X - SLACK
    pill.set_text(note.fit(room, pill.font.measure))


class CompactEpisodeCard(ctk.CTkFrame):
    """A tracked episode on one slim line (older days): the show's name and the episode code,
    then the round download button (SlimDownload, without the ⋯). A `missing` episode adds the
    amber "Not downloaded yet" pill under the line, a `new` one the blue "New" pill, one you
    have `have`'s. Clicking the name opens the show's window (`on_open_show`); a right-click
    anywhere opens the search window (what the ⋯ does on the other cards)."""

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
        have: HaveMark | None = None,
    ) -> None:
        noted = card_note(missing=missing, new=new, have=have)
        height = compact_height(noted is not None)
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
        line = ctk.CTkFrame(self, fg_color="transparent", height=SLIM_LINE)
        line.pack(side="top", fill="x", padx=(SLIM_PAD_X, SLIM_PAD_RIGHT), pady=(SLIM_PAD_Y, 0))
        line.pack_propagate(False)
        self.buttons = SlimDownload(line, entry, actions)
        self.buttons.allow_more(False)  # a slim line: the ⋯'s search window is a right-click
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

        self.note: PillLabel | None = None
        if noted is not None:
            self.note = _note_pill(self, noted, scale)
            _pack_note(self.note, self)

        line_width: float | None = None  # unscaled, once laid out

        def fit() -> None:
            if line_width is None:
                return
            code_width = code_font.measure(entry.episode.code)
            room = slim_name_room(line_width, code_width, self.buttons.width_units)
            self.name_label.configure(text=elide_to_width(show.name, room, name_font.measure))
            if self.note is not None and noted is not None:
                fit_note(self.note, noted, line_width)

        def resized(event: Any) -> None:
            nonlocal line_width
            if event.width > 1:
                line_width = unscaled_width(line, event.width)
                fit()

        line.bind("<Configure>", resized)
        self.buttons.on_change = fit  # ✓ needs other room than ⬇
        widgets: list[tk.Misc] = [self, *_descendants(self)]
        for widget in widgets:
            tk.Misc.bind(widget, "<Button-3>", self._right_click, add="+")

    def _right_click(self, _event: Any = None) -> None:
        self.buttons.choose()

    def _set_scaling(self, *args: Any, **kwargs: Any) -> None:
        """Windows' scaling changed: the note's pill (plain Tk) is drawn again."""
        super()._set_scaling(*args, **kwargs)
        if self.note is not None:
            self.note.redraw()


def _descendants(widget: tk.Misc) -> list[tk.Misc]:
    found: list[tk.Misc] = []
    stack = list(widget.winfo_children())
    while stack:
        child = stack.pop()
        found.append(child)
        stack.extend(child.winfo_children())
    return found


class SuggestionCard(ctk.CTkFrame):
    """A popular untracked show airing that day, toned down below the tracked episodes, with
    the poster cards' + Follow button (`TrackButton`: the row's height and corners, filled
    accent; "✓ Following" in green once followed) along its bottom. A click anywhere on it but
    the button opens the show's window (`on_open_show`)."""

    def __init__(
        self,
        master: Any,
        entry: CalendarEntry,
        *,
        library: LibraryService,
        status: StatusReporter,
        profile_id: int,
        scale: CardScale = FULL,
        on_open_show: OpenShow | None = None,
    ) -> None:
        """`profile_id`: the person whose calendar suggests the show; "+ Follow" adds it to
        their library."""
        super().__init__(
            master,
            fg_color=theme.SUGGESTION_BG,
            border_color=theme.SUGGESTION_BORDER,
            border_width=1,
            corner_radius=8,
            height=suggestion_height(scale),
        )
        self.pack_propagate(False)
        self._show = entry.show
        self._library = library
        self._status = status
        self._profile_id = profile_id
        code_font = theme.font(scale.font(_CODE_SIZE, SMALL_TEXT_MIN))

        inner = ctk.CTkFrame(self, fg_color="transparent")
        inner.pack(side="left", fill="both", expand=True, padx=8, pady=_SUGGESTION_PAD_Y)
        suggested_text = with_source(t("Suggested"), entry.show)
        suggested_font = theme.font(scale.font(_SUGGESTED_SIZE, SMALL_TEXT_MIN))
        self.suggested_label = ctk.CTkLabel(
            inner,
            text=suggested_text,
            font=suggested_font,
            text_color=theme.TEXT_MUTED,
            anchor="w",
            height=_SUGGESTED_HEIGHT,
        )
        self.suggested_label.pack(anchor="w")
        _keep_elided(self.suggested_label, suggested_text, suggested_font)
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

        self._track_button = TrackButton(
            inner, busy_text=t("Adding…"), command=self._track, scale=scale
        )
        self._track_button.pack(side="bottom", fill="x")
        ctk.CTkLabel(
            inner,
            text=entry.episode.code,
            font=code_font,
            text_color=theme.SUGGESTION_TEXT,
            anchor="w",
            height=_COMPACT_CODE_HEIGHT,
        ).pack(side="bottom", anchor="w", pady=(_SUGGESTION_NAME_GAP, _SUGGESTION_ROW_GAP))
        if self.open_show is not None:
            self._open_everywhere(self.open_show)

    def _open_everywhere(self, open_show: Callable[[], None]) -> None:
        """The whole card opens the show, with the hand pointer, except "+ Follow" (its own
        action). The name already opens it (`_open_on_click`, its own binding)."""
        own = {str(w) for w in (self._track_button, *_descendants(self._track_button))}
        named = {str(w) for w in _descendants(self.name_label)}
        widgets: list[tk.Misc] = [self, *_descendants(self)]
        for widget in widgets:
            if str(widget) in own:
                continue
            # Tk's own option (a CTk widget's configure() keeps it to its inner label).
            widget.tk.call(str(widget), "configure", "-cursor", "hand2")
            if str(widget) not in named:
                tk.Misc.bind(widget, "<Button-1>", lambda _event: open_show(), add="+")

    def _track(self) -> None:
        self._track_button.set_state("busy")
        spawn(self._track_show(), name="SuggestionCard.track")

    async def _track_show(self) -> None:
        # On success the library notifies its listeners and the window reloads the calendar
        # quietly (CalendarView.load), which replaces this card with the show's tracked
        # episode; until then the button says the show is followed.
        try:
            await self._library.track(self._show.id, profile_id=self._profile_id)
        except TVMazeError as exc:
            if self._track_button.winfo_exists():
                self._track_button.set_state("idle")
            self._status(
                t("Couldn't track {name}: {error}", name=self._show.name, error=exc),
                clear_after_ms=6000,
            )
            return
        if self._track_button.winfo_exists():
            self._track_button.set_state("tracked")
