"""The Forge window: MediaForge's own window inside AirGrabber ("one app, two windows",
docs/forge/INTEGRATION.md 2), rebuilt from MediaForge's ui/main_window.py `MainShell` in the
owner's chosen design (mockups/forge-chosen.html).

A CTkToplevel whose master is AirGrabber's root and which is never `transient`: it shows while
AirGrabber's window is in the tray, and closing it never quits AirGrabber (the host hides it).
Across the top a slim band (`ForgeBand`): on a canvas a dark-copper gradient with a 2-pixel
copper line under it, MediaForge's logo and name ("Forge" in BRAND), the pages as large words with
a copper line under the one shown — Queue (with the "needs you" count as a copper badge), History
and Auto-rename; at its right end, as real buttons on the band's plain colour, the watching chip
(the renamer's light and status, or a quiet "Auto-rename off"; a click shows Auto-rename), the
Activity button, which opens the Activity panel over the top right of the pages (✕ or Escape
closes it), and ⚙ (tooltip "Settings": AirGrabber's Settings at MediaForge › Library & watching,
`on_more_settings`, which shows AirGrabber's window first when it is in the tray).

`ForgeHost` (app/forge/ui/host.py) makes it and fills it: `show_forge(services)` builds the pages
(QueueView, HistoryView, and the Auto-rename page (LibraryPage) in a scrolling frame so "More in
Settings…" stays reachable in a short window; F-33: the band and the queue in one step, then the
minimum width, History and Auto-rename one per later turn of the window, `pages_built` once all
are; a page opened before its turn is built when it shows), or `show_problem(text,
on_try_again)` shows one plain page instead (Forge isn't ready: guessit couldn't be installed,
or forge.db couldn't be opened), and `show_getting_ready()` the same page with "Getting ready…"
while Forge's file-name reader warms up (F-28: the host shows it, then `show_forge` replaces it).
The renamer's status comes through `set_watch_status` (the band and the Auto-rename page).
Explorer drops anywhere on the window go to the queue (`add_paths`; app/forge/ui/file_drops.py,
the queue takes drops on itself too). The window keeps its size and place (`saved_geometry`,
`place_at`: ForgeSettings.window_geometry, kept on the screen), and its minimum width fits the
band and the queue's toolbar and preset card in the language in use.

The pages are a `widgets.Tabview` whose own tab row is never shown: the band's words switch it.
The logo is loaded at physical size in a worker thread; the window's icon is MediaForge's
(app/assets/forge). Build it while the event loop runs.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageTk

from app.forge.core.auto import OLD_INSTALL_STATUS
from app.forge.ui import taskbar
from app.forge.ui.activity_panel import ActivityPanel
from app.forge.ui.file_drops import attach_file_drops
from app.forge.ui.history_view import HistoryView
from app.forge.ui.library_page import LibraryPage, light_colour
from app.forge.ui.look import Pair, forge_look, mix
from app.forge.ui.queue_view import QueueView
from app.forge.ui.widgets.chips import fit_text, round_rect
from app.i18n import N_, number, t
from app.ui import palettes, tasks, theme
from app.ui.branding import set_own_icon
from app.ui.dialog_kit import WINDOW_FRAME, on_screen, primary_button
from app.ui.scrollable import ScrollableFrame
from app.ui.tooltip import add_tooltip, work_area
from app.ui.widgets import Tabview

if TYPE_CHECKING:
    from app.forge.core.auto import WatchStatus
    from app.forge.services import ForgeServices

log = logging.getLogger(__name__)

ASSETS_DIR = Path(__file__).resolve().parents[2] / "assets" / "forge"
LOGO_FILE = ASSETS_DIR / "MediaForge.png"
ICON_FILE = ASSETS_DIR / "MediaForge.ico"
TITLE = "MediaForge"  # a name: the same in every language

type Page = Literal["queue", "history", "auto"]
PAGES: tuple[tuple[Page, str], ...] = (
    ("queue", N_("Queue")),
    ("history", N_("History")),
    ("auto", N_("Auto-rename")),
)
AUTO_OFF = N_("Auto-rename off")  # the watching chip while renaming on its own is off
ACTIVITY = N_("Activity")
SETTINGS = N_("Settings")
TRY_AGAIN = N_("Try again")
GETTING_READY = N_("Getting ready…")

# CTk units.
DEFAULT_WIDTH, DEFAULT_HEIGHT = 1200, 800
MIN_WIDTH, MIN_HEIGHT = 1000, 640  # MediaForge's; wider when the band or the queue needs it
SCREEN_ROOM = 40  # kept free around a new window on a small screen
BAND_HEIGHT = 64  # the band, without its copper line
BAND_LINE = 2  # the copper line under it
BAND_PAD_X = 22
LOGO_SIZE = 36
NAME_SIZE = 21
TAB_SIZE = 17
TAB_GAP = 24
TAB_MARK = 3  # the copper line under the page shown
BADGE_SIZE = 12
CHIP_SIZE = 13
CHIP_HEIGHT = 30
CONTROL_GAP = 10  # between the band's buttons at its right end
LIGHT_SIZE = 10  # the watching chip's light
SETTINGS_SIZE = 15  # the ⚙
STATUS_ROOM = 240  # the band keeps this for the watching chip and its words
GRADIENT_STEPS = 48
GRADIENT_REACH = 0.6  # the gradient runs over this share of the band's width
MESSAGE_WRAP = 520
HISTORY_RETRY_MS = 250  # an undo in History: ask again this often before closing
# Between the window's later building steps (after the band and the queue): Tk draws and takes
# a click in between, so opening the window is no one long step (F-33).
LATER_STEP_MS = 30
PANEL_GAP = 10  # between the band and the Activity panel
_GEOMETRY = re.compile(r"^(\d+)x(\d+)\+(-?\d+)\+(-?\d+)$")


def set_forge_icon(window: ctk.CTkToplevel) -> None:
    """MediaForge's icon in the title bar (as `branding.set_window_icon` does AirGrabber's):
    call it from the window's __init__, before CTk applies its own icon 200 ms later. Read
    from the .ico once (`branding.set_own_icon`), not per window."""
    set_own_icon(window, ICON_FILE)


def load_logo(size: int) -> Image.Image | None:
    """MediaForge's logo, `size` × `size` physical pixels. Pillow work: through a thread."""
    try:
        with Image.open(LOGO_FILE) as image:
            image.load()
            return image.convert("RGBA").resize((size, size), Image.Resampling.LANCZOS)
    except OSError as exc:
        log.warning("Could not load MediaForge's logo (%s)", type(exc).__name__)
        return None


def parse_geometry(text: str | None) -> tuple[int, int, int, int] | None:
    """"1200x800+40+30" → (1200, 800, 40, 30); None for anything else (also no position)."""
    match = _GEOMETRY.match(text or "")
    if match is None:
        return None
    width, height, x, y = (int(part) for part in match.groups())
    return width, height, x, y


def band_gradient(look: theme.Look) -> tuple[Pair, Pair]:
    """The band's gradient, left and right end, from the Forge look's tokens: a dark copper
    fading into the tab colour (a light copper tint in the light look)."""
    left = (mix(look.TAB_BG[0], look.ACCENT[0], 0.16), mix(look.BG[1], look.ACCENT[1], 0.25))
    return left, look.TAB_BG


def light_dot(colour: Pair, size: int = LIGHT_SIZE) -> ctk.CTkImage:
    """The watching chip's light: a round dot with a soft glow, in `colour`'s light and dark
    shade (a CTkImage follows the look by itself). A tiny picture, made on the loop."""

    def draw(fill: str) -> Image.Image:
        scale = 4  # drawn large, scaled down by CTkImage: smooth edges
        side = size * scale
        image = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        red, green, blue = (int(fill[i : i + 2], 16) for i in (1, 3, 5))
        draw_on = ImageDraw.Draw(image)
        for reach, alpha in ((0, 60), (side // 10, 110)):
            box = (reach, reach, side - reach, side - reach)
            draw_on.ellipse(box, fill=(red, green, blue, alpha))
        inner = side // 4
        draw_on.ellipse((inner, inner, side - inner, side - inner), fill=(red, green, blue, 255))
        return image

    light, dark = (palettes.to_hex(c) for c in colour)
    return ctk.CTkImage(light_image=draw(light), dark_image=draw(dark), size=(size, size))


class ForgeBand(ctk.CTkFrame):
    """The window's top band: on a canvas the gradient, logo, name and the pages' words (with
    the "needs you" badge); at its right end, on the band's plain colour, the watching chip, the
    Activity button and ⚙ Settings as real buttons (with a tooltip). `on_page(page)`,
    `on_watch()`, `on_activity()` and `on_settings()` hear the clicks."""

    def __init__(self, master: Any, *, look: theme.Look) -> None:
        self.look = look
        super().__init__(
            master, height=BAND_HEIGHT + BAND_LINE, corner_radius=0, fg_color=look.TAB_BG
        )
        self.on_page: Callable[[Page], None] = lambda page: None
        self.on_watch: Callable[[], None] = lambda: None
        self.on_activity: Callable[[], None] = lambda: None
        self.on_settings: Callable[[], None] = lambda: None
        self.pages: list[tuple[Page, str]] = []  # (page, its word), once the pages are built
        self.current: Page | None = None
        self.needs = 0
        self.status: WatchStatus | None = None
        self.status_text = ""  # the chip's words, whole (the chip may show them cut)
        self.activity_open = False
        self.logo: ImageTk.PhotoImage | None = None
        self._hover: str | None = None
        self._hits: list[tuple[float, float, float, float, str]] = []  # what a click reaches
        self._fonts: dict[tuple[int, bool], tkfont.Font] = {}
        self._fonts_scale = 0.0
        self._lights: dict[Pair, ctk.CTkImage] = {}
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0, takefocus=0)
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.canvas.bind("<Configure>", lambda _event: self.draw())
        self.canvas.bind("<Button-1>", lambda event: self._clicked(event.x, event.y))
        self.canvas.bind("<Motion>", lambda event: self._set_hover(self._hit(event.x, event.y)))
        self.canvas.bind("<Leave>", lambda _event: self._set_hover(None))

        # The right end: real buttons on the band's own colour (the gradient ends before them).
        self.controls = ctk.CTkFrame(self, fg_color=look.TAB_BG, corner_radius=0)
        self.controls.place(relx=1.0, x=-BAND_PAD_X, rely=0.5, y=-BAND_LINE / 2, anchor="e")
        self.watch_chip = self._pill(lambda: self.on_watch())
        self.watch_chip.configure(compound="left")
        self.watch_chip.pack(side="left")
        self.activity_button = self._pill(lambda: self.on_activity(), text="☰  " + t(ACTIVITY))
        self.activity_button.pack(side="left", padx=(CONTROL_GAP, 0))
        self.settings_button = self._pill(
            lambda: self.on_settings(), text="⚙", width=CHIP_HEIGHT + 6
        )
        self.settings_button.configure(font=theme.font(SETTINGS_SIZE))
        self.settings_button.pack(side="left", padx=(CONTROL_GAP, 0))
        add_tooltip(self.settings_button, t(SETTINGS))
        self.controls.bind("<Configure>", lambda _event: self.draw(), add="+")
        theme.on_mode_change(self, self.draw)
        self._show_status()

    def _pill(
        self, command: Callable[[], None], *, text: str = "", width: int = 28
    ) -> ctk.CTkButton:
        look = self.look
        return ctk.CTkButton(
            self.controls,
            text=text,
            width=width,
            height=CHIP_HEIGHT,
            corner_radius=CHIP_HEIGHT // 2,
            border_width=1,
            fg_color=look.BG,
            bg_color=look.TAB_BG,
            hover_color=look.CHIP,
            border_color=look.CHIP_HOVER,
            text_color=theme.TEXT,
            font=theme.font(CHIP_SIZE),
            command=command,
        )

    # --- the canvas's clicks ------------------------------------------------------------------

    def _hit(self, x: float, y: float) -> str | None:
        hits = self._hits
        return next((tag for x0, y0, x1, y1, tag in hits if x0 <= x <= x1 and y0 <= y <= y1), None)

    def _clicked(self, x: float, y: float) -> None:
        tag = self._hit(x, y)
        if tag is not None:
            self.click(tag)

    def _set_hover(self, tag: str | None) -> None:
        if tag != self._hover:
            self._hover = tag
            self.canvas.configure(cursor="hand2" if tag else "")
            self.draw()

    # --- sizes -----------------------------------------------------------------------------

    @property
    def scale(self) -> float:
        return float(self._get_widget_scaling())

    def _px(self, units: float) -> int:
        return round(units * self.scale)

    def _font(self, size: int, *, bold: bool = False) -> tkfont.Font:
        if self._fonts_scale != self.scale:
            self._fonts, self._fonts_scale = {}, self.scale
        key = (size, bold)
        if key not in self._fonts:
            spec = theme.font(size, bold=bold).create_scaled_tuple(self.scale)
            self._fonts[key] = tkfont.Font(root=self, font=spec)
        return self._fonts[key]

    def _set_scaling(self, *args: Any, **kwargs: Any) -> None:
        super()._set_scaling(*args, **kwargs)
        self.draw()

    def _buttons_width(self) -> int:
        """The Activity and Settings buttons with their gaps (pixels)."""
        buttons = int(self.activity_button.winfo_reqwidth())
        buttons += int(self.settings_button.winfo_reqwidth())
        return buttons + 2 * self._px(CONTROL_GAP)

    def needed_width(self) -> int:
        """The width (pixels) the band needs: everything up to the last page's word, room for a
        usual status and the buttons at the right end."""
        return (
            self._tabs_end()
            + self._px(40 + STATUS_ROOM)
            + self._buttons_width()
            + self._px(BAND_PAD_X)
        )

    # --- what it shows ----------------------------------------------------------------------

    def set_pages(self, pages: Sequence[tuple[Page, str]], current: Page | None) -> None:
        self.pages, self.current = list(pages), current
        self.draw()

    def set_current(self, page: Page | None) -> None:
        if page != self.current:
            self.current = page
            self.draw()

    def set_needs(self, count: int) -> None:
        if count != self.needs:
            self.needs = count
            self.draw()

    def set_status(self, status: WatchStatus | None, text: str) -> None:
        """The renamer's status in the chip: its light and words, or the quiet "Auto-rename
        off" (`text`) while it is off."""
        self.status, self.status_text = status, text
        self._show_status()
        self.draw()

    def set_activity_open(self, opened: bool) -> None:
        if opened != self.activity_open:
            self.activity_open = opened
            look = self.look
            self.activity_button.configure(
                fg_color=look.ROW_SELECTED if opened else look.BG,
                border_color=look.ACCENT if opened else look.CHIP_HOVER,
            )

    def set_logo(self, image: ImageTk.PhotoImage) -> None:
        self.logo = image
        self.draw()

    @property
    def chip_text(self) -> str:
        """The words the watching chip shows (cut to fit)."""
        return str(self.watch_chip.cget("text")).strip()

    @property
    def light(self) -> Pair:
        """The chip's light colour (muted while off)."""
        state = self.status.state if self.status is not None else "off"
        return light_colour(state)

    def _show_status(self) -> None:
        look = self.look
        off = self.status is None or self.status.state == "off"
        light = self.light
        if light not in self._lights:
            self._lights[light] = light_dot(light)
        self.watch_chip.configure(
            image=self._lights[light],
            fg_color=look.TAB_BG if off else look.BG,  # quiet while off: only its outline
            text_color=theme.TEXT_MUTED if off else theme.TEXT,
        )
        self._fit_status()

    def _fit_status(self) -> None:
        """The chip's words, cut so the chip never runs into the pages' words."""
        width = self.canvas.winfo_width()
        text = "  " + self.status_text
        if width > 1:
            room = (
                width
                - self._tabs_end()
                - self._px(30 + BAND_PAD_X)
                - self._buttons_width()
                - self._px(LIGHT_SIZE + 2 * 14 + 8)
            )
            text = "  " + fit_text(self.status_text, self._font(CHIP_SIZE), max(room, 0))
        if self.watch_chip.cget("text") != text:
            self.watch_chip.configure(text=text)

    # --- drawing ----------------------------------------------------------------------------

    def _tabs_end(self) -> int:
        font = self._font(NAME_SIZE, bold=True)
        x = self._px(BAND_PAD_X + LOGO_SIZE + 12) + font.measure("Media") + font.measure("Forge")
        x += self._px(30)
        tab_font = self._font(TAB_SIZE, bold=True)
        for page, word in self.pages:
            x += tab_font.measure(word)
            if page == "queue" and self.needs:
                x += self._px(8) + self._badge_width()
            x += self._px(TAB_GAP)
        return x

    def _badge_width(self) -> int:
        font = self._font(BADGE_SIZE, bold=True)
        return max(self._px(20), font.measure(number(self.needs)) + self._px(16))

    def draw(self) -> None:
        c = self.canvas
        c.delete("all")
        self._hits = []
        width, height = c.winfo_width(), c.winfo_height()
        if width <= 1 or height <= 1:
            return
        self._fit_status()
        look, pick = self.look, theme.pick
        left, right = band_gradient(look)
        start, end = pick(left), pick(right)
        controls_x = self.controls.winfo_x() if self.controls.winfo_ismapped() else width
        # The gradient ends before the buttons, which sit on the band's plain colour.
        reach = max(1.0, min(width * GRADIENT_REACH, controls_x - self._px(12)))
        step = reach / GRADIENT_STEPS
        for index in range(GRADIENT_STEPS):
            colour = mix(start, end, index / (GRADIENT_STEPS - 1))
            x0 = round(index * step)
            c.create_rectangle(x0, 0, round(x0 + step) + 1, height, fill=colour, width=0)
        c.create_rectangle(round(reach), 0, width, height, fill=end, width=0)
        line = max(1, self._px(BAND_LINE))
        c.create_rectangle(0, height - line, width, height, fill=pick(look.ACCENT), width=0)
        middle = (height - line) / 2
        # The logo and the name.
        x: float = self._px(BAND_PAD_X)
        if self.logo is not None:
            c.create_image(x, middle, image=self.logo, anchor="w", tags=("logo",))
        x += self._px(LOGO_SIZE + 12)
        name_font = self._font(NAME_SIZE, bold=True)
        c.create_text(x, middle, text="Media", anchor="w", fill=pick(theme.TEXT), font=name_font)
        x += name_font.measure("Media")
        c.create_text(x, middle, text="Forge", anchor="w", fill=pick(look.BRAND), font=name_font)
        x += name_font.measure("Forge") + self._px(30)
        # The pages' words.
        tab_font = self._font(TAB_SIZE, bold=True)
        for page, word in self.pages:
            tag = f"tab-{page}"
            shown = page == self.current
            ink = theme.TEXT if shown or self._hover == tag else theme.TEXT_MUTED
            c.create_text(
                x, middle, text=word, anchor="w", fill=pick(ink), font=tab_font, tags=(tag,)
            )
            word_w = tab_font.measure(word)
            end_x: float = x + word_w
            if page == "queue" and self.needs:
                end_x = self._draw_badge(x + word_w + self._px(8), middle, tag)
            if shown:
                top = middle + tab_font.metrics("linespace") / 2 + self._px(3)
                c.create_rectangle(
                    x, top, end_x, top + self._px(TAB_MARK), fill=pick(look.BRAND), width=0,
                    tags=(tag, "tab-mark"),
                )  # fmt: skip
            # The word's whole box takes the click, not only the letters.
            reach_x = self._px(TAB_GAP) / 2
            self._hits.append((x - reach_x, 0, end_x + reach_x, height, tag))
            x = end_x + self._px(TAB_GAP)

    def _draw_badge(self, x: float, middle: float, tag: str) -> float:
        c, pick = self.canvas, theme.pick
        w, h = self._badge_width(), self._px(20)
        round_rect(
            c, x, middle - h / 2, x + w, middle + h / 2, h / 2, fill=pick(self.look.ACCENT),
            width=0, tags=(tag, "badge"),
        )  # fmt: skip
        c.create_text(
            x + w / 2,
            middle,
            text=number(self.needs),
            fill=theme.ON_COLOR,
            font=self._font(BADGE_SIZE, bold=True),
            tags=(tag, "badge"),
        )
        return x + w

    def item_text(self, tag: str) -> str:
        """The text of the first text item with `tag` (for tests)."""
        canvas = self.canvas
        for item in canvas.find_withtag(tag):
            if str(canvas.type(item)) == "text":
                return str(canvas.tk.call(str(canvas), "itemcget", item, "-text"))
        return ""

    def click(self, tag: str) -> None:
        """A click on `tag`: "watch", "activity", "settings" or "tab-<page>"."""
        if tag == "watch":
            self.on_watch()
        elif tag == "activity":
            self.on_activity()
        elif tag == "settings":
            self.on_settings()
        elif tag.startswith("tab-"):
            page = tag.removeprefix("tab-")
            for known, _word in self.pages:
                if known == page:
                    self.on_page(known)


class ForgeWindow(ctk.CTkToplevel):
    """`ForgeWindow(master, look=, geometry=)`; then `show_forge(services)` or `show_problem`.
    The host sets `on_close` (the window's ✕: it hides the window) and `on_more_settings`."""

    def __init__(
        self,
        master: Any,
        *,
        look: theme.Look | None = None,
        geometry: str | None = None,
    ) -> None:
        self.look: theme.Look = forge_look() if look is None else look
        super().__init__(master, fg_color=self.look.BG)
        self.services: ForgeServices | None = None
        self.on_close: Callable[[], None] = self.withdraw
        self.on_more_settings: Callable[[], None] = lambda: None
        self.queue: QueueView | None = None
        self.history: HistoryView | None = None
        self.library: LibraryPage | None = None  # the Auto-rename page
        self.library_scroll: ctk.CTkScrollableFrame | None = None
        self.activity: ActivityPanel | None = None
        self.tabs: Tabview | None = None
        self.message: ctk.CTkFrame | None = None  # the plain page while Forge is off
        self.message_label: ctk.CTkLabel | None = None
        self.try_again_button: ctk.CTkButton | None = None
        self.drops = False  # Explorer drops on the window reach the queue
        self.status: WatchStatus | None = None
        self._needs = 0
        self._page_names: dict[Page, str] = {key: t(label) for key, label in PAGES}
        self._logo_image: ImageTk.PhotoImage | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._frames: dict[Page, Any] = {}  # each page's frame in the Tabview
        self._later: list[Callable[[], None]] = []  # building steps still to come
        self._later_id: str | None = None

        self.title(TITLE)
        set_forge_icon(self)
        taskbar.attach(self)  # its own taskbar button with MediaForge's icon (D8)
        self.minsize(MIN_WIDTH, MIN_HEIGHT)
        self.protocol("WM_DELETE_WINDOW", lambda: self.on_close())
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self.band = ForgeBand(self, look=self.look)
        self.band.grid(row=0, column=0, sticky="ew")
        self.band.on_page = self.show_page
        self.band.on_watch = self._status_clicked
        self.band.on_activity = self.toggle_activity
        self.band.on_settings = lambda: self.on_more_settings()
        self._hidden = tk.Frame(self)  # holds the Tabview's own tab row, never shown
        self.bind("<Escape>", lambda _event: self.hide_activity(), add="+")
        self.place_at(geometry)
        self._spawn(self._load_logo())

    # --- building ---------------------------------------------------------------------------

    def show_forge(self, services: ForgeServices, status: WatchStatus | None = None) -> None:
        """Build the pages on `services` (once; the plain page goes)."""
        if self.services is not None:
            return
        self.services = services
        self._clear_message()
        look = self.look
        self.tabs = Tabview(
            self, look="pills", pills_master=self._hidden, colors=look, command=self._page_changed
        )
        self.tabs.grid(row=1, column=0, sticky="nsew")
        frames = {key: self.tabs.add(name) for key, name in self._page_names.items()}
        self._frames = frames
        self.band.set_pages([(key, name) for key, name in self._page_names.items()], "queue")

        self.queue = QueueView(frames["queue"], services, look=look)
        self.queue.pack(fill="both", expand=True)
        self.queue.on_counts = self.set_needs
        self.queue.refresh()
        # Drops anywhere on the window (the band, History, Auto-rename) go to the queue; the
        # queue also takes them on itself.
        self.drops = attach_file_drops(self, self.add_paths)
        self.set_watch_status(status or services.auto.status([]))
        self.show_page("queue")
        # That was one step (the band and the queue); the rest comes a step per turn (F-33).
        self._build_later(self._fit_min_width, self._build_history, self._build_library_page)

    def _build_later(self, *steps: Callable[[], None]) -> None:
        self._later.extend(steps)
        if self._later_id is None:
            self._later_id = self.after(LATER_STEP_MS, self._next_later)

    def _next_later(self) -> None:
        self._later_id = None
        if not self._later or not self.winfo_exists():
            return
        step = self._later.pop(0)
        try:
            step()
        finally:
            if self._later:
                self._later_id = self.after(LATER_STEP_MS, self._next_later)

    @property
    def pages_built(self) -> bool:
        """Every page is there (`show_forge` builds the rest of them in later turns of the
        window)."""
        return self.services is not None and not self._later and self._later_id is None

    def _build_history(self) -> None:
        if self.history is None and self.services is not None:
            self.history = HistoryView(self._frames["history"], self.services, look=self.look)
            self.history.pack(fill="both", expand=True)

    def _build_library_page(self) -> None:
        if self.library is None and self.services is not None:
            self.library = self._build_library(self._frames["auto"], self.services)
            if self.status is not None:
                self.library.set_watch_status(self.status)

    def _build_library(self, master: Any, services: ForgeServices) -> LibraryPage:
        look = self.look
        ctk.CTkLabel(
            master, text=t("Auto-rename"), font=theme.font(16, bold=True), text_color=theme.TEXT
        ).pack(anchor="w", padx=16, pady=(14, 8))
        scroll = ScrollableFrame(
            master,
            fg_color=look.BG,
            corner_radius=0,
            scrollbar_button_color=look.SCROLLBAR,
            scrollbar_button_hover_color=look.EDGE,
        )
        scroll.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.library_scroll = scroll
        page = LibraryPage(
            scroll,
            services.settings,
            services.auto,
            lambda: self.on_more_settings(),
            look=look,
            presets=services.presets,
        )
        page.pack(fill="x")
        return page

    def show_problem(self, text: str, on_try_again: Callable[[], None] | None) -> None:
        """One plain page instead of the pages: why Forge is off, and Try again."""
        if self.services is not None:
            return
        self._clear_message()
        frame = ctk.CTkFrame(self, fg_color="transparent", corner_radius=0)
        frame.grid(row=1, column=0, sticky="nsew")
        inner = ctk.CTkFrame(frame, fg_color="transparent")
        inner.place(relx=0.5, rely=0.4, anchor="center")
        self.message_label = ctk.CTkLabel(
            inner,
            text=text,
            font=theme.font(15, bold=True),
            text_color=theme.TEXT,
            wraplength=MESSAGE_WRAP,
            justify="center",
        )
        self.message_label.pack()
        if on_try_again is not None:
            self.try_again_button = primary_button(
                inner, t(TRY_AGAIN), on_try_again, width=160, look=self.look
            )
            self.try_again_button.pack(pady=(18, 0))
        self.message = frame

    def show_getting_ready(self) -> None:
        """The plain page with "Getting ready…" (no button): Forge's file-name reader warms up
        before the pages are built (ForgeHost.open)."""
        self.show_problem(t(GETTING_READY), None)

    def _clear_message(self) -> None:
        if self.message is not None:
            self.message.destroy()
        self.message = None
        self.message_label = None
        self.try_again_button = None

    def _fit_min_width(self) -> None:
        """At least wide enough for the band and the queue's toolbar and preset card in the
        language in use (the card's option line may wrap)."""
        self.update_idletasks()
        scale = float(ctk.ScalingTracker.get_window_scaling(self)) or 1.0
        widths = [self.band.needed_width()]
        if self.queue is not None:
            widths.append(self.queue.winfo_reqwidth())
        needed = math.ceil(max(widths) / scale)
        self.minsize(max(MIN_WIDTH, needed), MIN_HEIGHT)

    # --- the logo ---------------------------------------------------------------------------

    async def _load_logo(self) -> None:
        size = max(1, round(LOGO_SIZE * self.band.scale))
        image = await asyncio.to_thread(load_logo, size)
        if image is None or not self.winfo_exists():
            return
        self._logo_image = ImageTk.PhotoImage(image, master=self.band.canvas)
        self.band.set_logo(self._logo_image)

    def _spawn(self, coro: Coroutine[Any, Any, Any]) -> asyncio.Task[Any]:
        task = tasks.spawn(coro, name="ForgeWindow")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    # --- size and place ---------------------------------------------------------------------

    def place_at(self, geometry: str | None) -> None:
        """The size and place `geometry` remembers (moved onto a screen when that one is gone),
        else DEFAULT_WIDTH × DEFAULT_HEIGHT in the middle of the screen under the pointer."""
        scale = float(ctk.ScalingTracker.get_window_scaling(self)) or 1.0
        frame = round(WINDOW_FRAME * scale)
        saved = parse_geometry(geometry)
        if saved is not None:
            width, height, x, y = saved
            width, height = max(width, MIN_WIDTH), max(height, MIN_HEIGHT)
            w, h = round(width * scale), round(height * scale)
            area = work_area(x + w // 2, y + h // 2) or self._screen()
            x, y = on_screen(x, y, w, h + frame, area)
        else:
            area = work_area(*self.winfo_pointerxy()) or self._screen()
            left, top, right, bottom = area
            width = max(1, min(DEFAULT_WIDTH, int((right - left) / scale) - SCREEN_ROOM))
            height = max(1, min(DEFAULT_HEIGHT, int((bottom - top) / scale) - SCREEN_ROOM))
            w, h = round(width * scale), round(height * scale)
            x, y = left + (right - left - w) // 2, top + (bottom - top - h - frame) // 2
            x, y = on_screen(x, y, w, h + frame, area)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _screen(self) -> tuple[int, int, int, int]:
        width, height = self.wm_maxsize()
        return 0, 0, int(width), int(height)

    def saved_geometry(self) -> str | None:
        """The size and place to remember: only while the window is neither maximized nor
        minimized (a maximized window comes back at its normal size)."""
        try:
            if self.state() != "normal":
                return None
            return str(self.geometry())
        except tk.TclError:
            return None

    # --- pages ------------------------------------------------------------------------------

    @property
    def current_page(self) -> Page | None:
        if self.tabs is None:
            return None
        name = self.tabs.get()
        return next((key for key, text in self._page_names.items() if text == name), None)

    def show_page(self, page: Page) -> None:
        if self.tabs is None:
            return
        name = self._page_names[page]
        if self.tabs.get() == name:
            self._page_changed()
            return
        # The way a click on a tab goes (it calls `_page_changed`). CTk's own `set` hides the
        # other pages 100 ms later, which hid a page shown within those 100 ms for good.
        self.tabs._tab_clicked(name)

    def _page_changed(self) -> None:
        """A page shows (built now if its turn hasn't come yet): History reads its runs again."""
        page = self.current_page
        self.band.set_current(page)
        if page == "history":
            self._build_history()
        elif page == "auto":
            self._build_library_page()
        if page == "history" and self.history is not None:
            self.history.on_show()
        if page == "auto" and self.library is not None:
            self.library.reload_presets()  # Settings › Naming may have changed them

    def refresh_page(self) -> None:
        """The window shows again (it was hidden): the page shown is read again."""
        self._page_changed()

    # --- the badge -------------------------------------------------------------------------------

    def set_needs(self, needs: int) -> None:
        """The queue's "needs you" count: the badge on Queue."""
        self._needs = needs
        self.band.set_needs(needs)

    @property
    def needs(self) -> int:
        return self._needs

    # --- the Activity panel ------------------------------------------------------------------------

    @property
    def activity_open(self) -> bool:
        panel = self.activity
        return panel is not None and bool(panel.winfo_manager())

    def toggle_activity(self) -> None:
        if self.activity_open:
            self.hide_activity()
        else:
            self.show_activity()

    def show_activity(self) -> None:
        """The Activity panel over the top right of the pages (built the first time)."""
        if self.services is None:
            return
        if self.activity is None:
            self.activity = ActivityPanel(
                self, self.services.activity, look=self.look, on_close=self.hide_activity
            )
        self.activity.place(
            relx=1.0,
            x=-16,
            y=BAND_HEIGHT + BAND_LINE + PANEL_GAP,
            anchor="ne",
            relheight=0.55,
        )
        self.activity.lift()
        self.activity.see_end()
        self.band.set_activity_open(True)

    def hide_activity(self) -> None:
        if self.activity is not None and self.activity.winfo_manager():
            self.activity.place_forget()
        self.band.set_activity_open(False)

    # --- the watching chip -----------------------------------------------------------------

    def set_watch_status(self, status: WatchStatus) -> None:
        """The renamer's status: the chip in the band, and the Auto-rename page."""
        if not self.winfo_exists():
            return
        self.status = status
        if self.library is not None:
            self.library.set_watch_status(status)
        if status.state == "off" and status.text != t(OLD_INSTALL_STATUS):
            self.band.set_status(status, t(AUTO_OFF))
        else:  # on, Trial, or off for a reason worth reading (the old MediaForge's guard)
            self.band.set_status(status, status.text)

    def _status_clicked(self) -> None:
        if self.services is not None:
            self.show_page("auto")

    # --- what the host asks -------------------------------------------------------------------

    def add_paths(self, paths: Sequence[Path]) -> None:
        """Queue files and folders (drops, right-click "Open in MediaForge")."""
        if self.queue is not None and paths:
            self.show_page("queue")
            self.queue.add_paths(paths)

    def select_preset(self, name: str) -> None:
        if self.queue is not None:
            self.queue.select_preset(name)

    def may_close(self, retry: Callable[[], None]) -> bool:
        """True if the window may go now. Otherwise `retry` is called later: a Forge run stops
        after its current file first, and an undo in History is allowed to finish."""
        if self.queue is not None and not self.queue.request_close(retry):
            return False
        if self.history is not None and self.history.busy:
            log.info("Waiting for the undo to finish")
            self.after(HISTORY_RETRY_MS, retry)
            return False
        return True

    def destroy(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._later.clear()
        if self._later_id is not None:
            try:
                self.after_cancel(self._later_id)
            except tk.TclError:
                pass  # gone with its interpreter
            self._later_id = None
        super().destroy()
