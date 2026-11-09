"""The main window's header: the spotlight band, edge to edge, BAND_HEIGHT units tall.

Its picture is a title's TMDB backdrop, blurred, dimmed and fading into the page's background at
the bottom (as the info window's: app/ui/backdrop.py) under a left-to-right scrim of that
background (darkest behind the logo), or without one a soft glow in the accent colour. Over it,
on the top row: the logo (over a soft shadow) and "AirGrabber" ("Grabber" in BRAND) on the left
(a click on either goes home, `on_home`: Series > Calendar),
the search field, ⬇ Downloads (with its badge while downloads are on their way), the
settings button, 📱 while a phone is connected (`set_phone`, `on_phone`: what's on your phone)
and, at the right end, the name button (the selected person's avatar, name and
▾, `set_person`; a click calls `on_person`, the people menu) on the right. The Series side's
titles are the selected person's (`profile`). Under them the
spotlight: the small poster, a tag ("TONIGHT · 21:00", "TRENDING", …), the title, a muted line
("3 episodes today", a film's "2026 · ★ 7.0") and a dot per title. The titles come from
app/services/spotlight.py: Series and Movies each have their own; the Downloads page keeps the
last side's.

MediaForge's page (`set_compact`): only the top row, plus 🕘 (`set_history_lit`, `on_history`) left
of ⚙, on a quiet ground: the glow a band without a title has, fading into the page, baked once in a
worker thread (per size and look) and kept on its own canvas item, so going in and out only hides
and shows items. No picture, poster, tag, title or dots, and no turning while it shows; back on
another page the full band is there at once, its picture baked again only if something changed
meanwhile (a new width, a look, new titles).

The Series / Movies / MediaForge switch (`ModeSwitch`, below) is a canvas of its own at the right
end of the tabs row under the band: a solid pill over a soft shadow on the page's background, with
MediaForge's count of waiting files on its word (`set_count`).

Every ROTATE_S seconds the next title fades in (FADE_STEPS frames over FADE_MS, blended in a
worker thread), not while the pointer is over the band, not at all with Settings'
`spotlight_motion` off (a dot still jumps, without the fade). A click on the poster, the texts
or the picture opens the title's info window (`on_open`).

Tk can't draw a widget see-through over a picture, so everything but the search field's text
box is drawn on this canvas: the picture and the poster as one image baked in physical pixels,
the field and the buttons as anti-aliased RGBA images with canvas text on them, the
dots likewise. The text box is a flat CTkEntry inside the field's solid pill, in the same
colour, so no rectangle or corner shows. Hover tips for the drawn buttons come from a small tip
window of their own that looks like app/ui/tooltip.py's. A switch between light and dark, a
change of Windows' scaling and a new width (once the resizing stops) bake everything again,
reading the colour tokens at that moment.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
import tkinter as tk
from collections import OrderedDict
from collections.abc import Callable, Coroutine, Hashable
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal, TypeVar

import customtkinter as ctk
from PIL import Image, ImageTk

from app import APP_NAME
from app.i18n import N_, t
from app.models import DownloadsOverview, FreeSpace, Spotlight, SpotlightItem, SpotlightSide
from app.ui import theme, tooltip
from app.ui.backdrop import (
    RGBA,
    backdrop_face,
    compose_scene,
    crossfade_frames,
    drop_shadow,
    glow_layer,
    rounded_box,
    shadowed_box,
)
from app.ui.branding import load_logo
from app.ui.icons import phone_icon, sliders_icon
from app.ui.people import avatar_image
from app.ui.poster_card import rgb
from app.ui.spotlight import (
    BADGE_HEIGHT,
    BAND_HEIGHT,
    BUTTON_RADIUS,
    COMPACT_HEIGHT,
    FADE_MS,
    FADE_STEPS,
    LOGO_SIZE,
    POSTER_HEIGHT,
    POSTER_RADIUS,
    POSTER_TOP,
    POSTER_WIDTH,
    PAD_X,
    PERSON_ARROW,
    PERSON_ARROW_GAP,
    PERSON_FONT_SIZE,
    PERSON_NAME_GAP,
    PERSON_NAME_MAX,
    SEGMENT_FONT_SIZE,
    SEGMENT_PAD_X,
    SWITCH_BADGE_GAP,
    SWITCH_BADGE_PAD,
    WORD_SIZE,
    BandLayout,
    Hit,
    HitKind,
    Rect,
    Rotation,
    SwitchPart,
    band_layout,
    hit_target,
    person_width,
    sub_line,
    tag_text,
    title_text,
)
from app.ui.tasks import spawn
from app.ui.text_fit import elide_to_width
from app.ui.title_info import FramedPoster, backdrop_size, frame_poster
from app.ui.views.downloads_view import badge_text, space_status_text

if TYPE_CHECKING:
    from app.services import DownloadStatusService, ImageCache, SpotlightService

log = logging.getLogger(__name__)

T = TypeVar("T")

PART_LABELS = {
    SwitchPart.SERIES: N_("Series"),
    SwitchPart.MOVIES: N_("Movies"),
    SwitchPart.LIBRARY: N_("Library"),
    SwitchPart.FORGE: N_("MediaForge"),
}
# The parts on the switch without and with Plex (the Library part).
PARTS = (SwitchPart.SERIES, SwitchPart.MOVIES, SwitchPart.FORGE)
PARTS_WITH_LIBRARY = (SwitchPart.SERIES, SwitchPart.MOVIES, SwitchPart.LIBRARY, SwitchPart.FORGE)
SEARCH_PLACEHOLDER = N_("Search shows & movies…")
SEARCH_TIP = N_("Search")
HISTORY_TIP = N_("History")
DOWNLOADS_TIP = N_("Downloads")
SETTINGS_TIP = N_("Settings")
PHONE_TIP = N_("What's on your phone")
_TIPS = {
    HitKind.SEARCH: SEARCH_TIP,
    HitKind.HISTORY: HISTORY_TIP,
    HitKind.DOWNLOADS: DOWNLOADS_TIP,
    HitKind.SETTINGS: SETTINGS_TIP,
    HitKind.PHONE: PHONE_TIP,
}
_CONTROLS = frozenset(
    {
        HitKind.PART, HitKind.SEARCH,
        HitKind.FIELD,
        HitKind.HISTORY,
        HitKind.DOWNLOADS,
        HitKind.SETTINGS,
        HitKind.PERSON,
        HitKind.PHONE,
    }
)

SPACE_NOTICE_MS = 15_000  # how long the "almost full" notice stays in the status bar
TICK_MS = 250  # how often the band checks whether the next title is due
REBAKE_MS = 150  # a new width bakes the picture again once the resizing stopped this long
RELOAD_MS = 600  # reloads asked for in a burst (a sync) fetch the titles once
TIP_DELAY_MS = 500
SCENE_CACHE = 8  # baked pictures kept (a side's titles and the next one's)
SHADOW_BLUR = 5  # the poster's shadow (units)
SHADOW_OFFSET = 3
ICON_SIZE = 22  # the settings button's sliders
# The switch (ModeSwitch) stands on a solid pill: SURFACE, nearly opaque, a
# CHIP_HOVER edge, over a soft shadow; the part under the pointer (not the lit one) a CHIP_HOVER
# fill.
SWITCH_ALPHA = 235  # the pill's SURFACE (of 255)
# Soft shadows (blur and offset in units, alpha (light, dark)): the switch's and the logo's.
SWITCH_SHADOW = (5.0, 2, (0.2, 0.5))
LOGO_SHADOW = (4.0, 2, (0.25, 0.6))
DOT_ALPHA = (90, 170, 255)  # a dot: others, under the pointer, the one showing


class SpotlightBand(tk.Canvas):
    """The header (see the module docstring). MainWindow calls `start` once the loop runs,
    `select` when the page changes (whose titles show; None: the Downloads page), `set_compact`
    for MediaForge's page (`set_history_lit`: History shows there; a click on 🕘 calls
    `on_history`), `reload` when the library, a download or a setting changed, and `set_motion`
    for Settings' `spotlight_motion`. `set_person` puts the selected person on the name button
    (none until then); a click on it calls `on_person`. A click on the logo or "AirGrabber" calls
    `on_home`. `entry` is the search field's text box."""

    def __init__(
        self,
        master: Any,
        *,
        spotlight: SpotlightService,
        downloads: DownloadStatusService,
        images: ImageCache,
        motion: bool = True,
        on_downloads: Callable[[], None],
        on_history: Callable[[], None],
        on_settings: Callable[[], None],
        on_search: Callable[[], None],
        on_open: Callable[[SpotlightItem], None],
        report: Callable[[str, int], None],
        profile: Callable[[], int],
        on_person: Callable[[], None] | None = None,
        on_phone: Callable[[], None] | None = None,
        on_home: Callable[[], None] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
        on_part: Callable[[SwitchPart], None] | None = None,
    ) -> None:
        """`profile`: the selected person, read at every load (the Series side's episodes are
        theirs). `clock`: the local time now, aware (the tag's day and time; tests set it)."""
        super().__init__(
            master,
            highlightthickness=0,
            borderwidth=0,
            width=1,
            height=1,
            background=theme.pick(theme.BG),
        )
        self._service = spotlight
        self._images = images
        self._on_downloads = on_downloads
        self._on_history = on_history
        self._on_settings = on_settings
        self._on_search = on_search
        self._on_open = on_open
        self._report = report
        self._profile = profile
        self._on_person = on_person
        self._on_phone = on_phone
        self._phone_shown = False  # 📱: a phone is connected (`set_phone`)
        self._on_home = on_home
        self._on_part = on_part  # a click on the main menu's switch (none: no switch)
        self._part: SwitchPart | None = SwitchPart.SERIES  # lit; None: the Downloads page
        self._parts: tuple[SwitchPart, ...] = PARTS  # on the switch, left to right
        self._forge_count = 0  # MediaForge's waiting files, on its part
        self._switch_images: dict[Hashable, Image.Image] = {}
        self._switch_pads: dict[Hashable, int] = {}
        self._home_hover = False  # the pointer over the logo or "AirGrabber"
        self._home_pressed = False
        # The name button's person: (name, initial, colour); None: no button.
        self._person: tuple[str, str, theme.Pair] | None = None
        self._clock = clock
        self._started = False
        self._closed = False
        self._fresh_picture = False  # bake the title showing again once its side reloaded
        self._side = SpotlightSide.SERIES  # whose titles show
        self._selected: SpotlightSide | None = SpotlightSide.SERIES  # None: ⬇ lit
        self._compact = False  # MediaForge's page: only the top row (`set_compact`)
        self._history_lit = False  # 🕘 lit: History shows there
        self._scene_stale = False  # a picture was asked for while compact: baked when back
        self._ground_key: Hashable | None = None  # what the ground on the canvas was baked for
        self._ground_task: asyncio.Task[None] | None = None
        self._spotlights: dict[SpotlightSide, Spotlight] = {}
        self._stale: set[SpotlightSide] = set(SpotlightSide)
        self._indexes = dict.fromkeys(SpotlightSide, 0)
        self._rotation = Rotation(motion=motion)
        latest = downloads.latest
        self._downloading = latest.in_progress if latest is not None else 0
        self._scale = float(ctk.ScalingTracker.get_widget_scaling(self))
        self._logo_pad = 0  # the logo's image reaches this far past it (its shadow), px
        self._width = 0  # physical px, from the last <Configure>
        self._layout: BandLayout | None = None
        self._hover: Hit | None = None
        self._pressed: Hit | None = None
        self._entry_focused = False
        self._shown: Image.Image | None = None  # the picture on the canvas now
        self._scenes: OrderedDict[Hashable, Image.Image] = OrderedDict()
        self._photos: dict[str, ImageTk.PhotoImage] = {}
        self._photo_sources: dict[str, Image.Image] = {}
        self._load_tasks: dict[SpotlightSide, asyncio.Task[None]] = {}
        self._show_task: asyncio.Task[None] | None = None
        self._warm_task: asyncio.Task[Image.Image | None] | None = None
        self._logo_task: asyncio.Task[None] | None = None
        self._tick_job: str | None = None
        self._bake_job: str | None = None
        self._tip_job: str | None = None
        self._reload_job: str | None = None
        self._tip = _Tip(self)

        self._fonts = {
            "word": theme.font(WORD_SIZE, bold=True),
            "tag": theme.font(11, bold=True),
            "title": theme.font(18, bold=True),
            "sub": theme.font(13),
            "magnifier": theme.font(15),
            "glyph": theme.font(20),
            "badge": theme.font(10, bold=True),
            "person": theme.font(PERSON_FONT_SIZE),
            "arrow": theme.font(12),
            "switch": theme.font(SEGMENT_FONT_SIZE, bold=True),
        }

        def text(anchor: Literal["nw", "w", "center"] = "nw") -> int:
            return self.create_text(0, 0, anchor=anchor, text="")

        self._scene_item = self.create_image(0, 0, anchor="nw")
        self._ground_item = self.create_image(0, 0, anchor="nw", state="hidden")  # compact's
        self._logo_item = self.create_image(0, 0, anchor="nw")
        self._word_items = (text("w"), text("w"))
        self._tag_item, self._title_item, self._sub_item = text(), text(), text()
        self._search_item = self.create_image(0, 0, anchor="nw")
        self._magnifier_item = self.create_text(0, 0, anchor="center", text="🔍")
        self._history_item = self.create_image(0, 0, anchor="nw", state="hidden")
        self._history_glyph = self.create_text(0, 0, anchor="center", text="🕘", state="hidden")
        self._downloads_item = self.create_image(0, 0, anchor="nw")
        self._downloads_glyph = self.create_text(0, 0, anchor="center", text="⬇")
        self._settings_item = self.create_image(0, 0, anchor="nw")
        self._settings_icon = self.create_image(0, 0, anchor="center")
        self._phone_item = self.create_image(0, 0, anchor="nw", state="hidden")
        self._phone_glyph = self.create_image(0, 0, anchor="center", state="hidden")
        self._badge_item = self.create_image(0, 0, anchor="nw", state="hidden")
        self._badge_text = self.create_text(0, 0, anchor="center", text="", state="hidden")
        self._person_item = self.create_image(0, 0, anchor="nw", state="hidden")
        self._avatar_item = self.create_image(0, 0, anchor="nw", state="hidden")
        self._person_name = self.create_text(0, 0, anchor="w", text="", state="hidden")
        self._person_arrow = self.create_text(0, 0, anchor="w", text="", state="hidden")
        self._switch_track = self.create_image(0, 0, anchor="nw")
        self._switch_hover = self.create_image(0, 0, anchor="nw", state="hidden")
        self._switch_active = self.create_image(0, 0, anchor="nw")
        self._switch_texts = {part: text("center") for part in SwitchPart}
        self._switch_badge = self.create_image(0, 0, anchor="nw", state="hidden")
        self._switch_badge_text = self.create_text(0, 0, anchor="center", text="", state="hidden")
        self._dot_items: list[int] = []

        # The search field's text box: flat, in the field's own colour.
        self.entry = ctk.CTkEntry(
            self,
            placeholder_text=t(SEARCH_PLACEHOLDER),
            placeholder_text_color=theme.TEXT_MUTED,
            text_color=theme.TEXT,
            fg_color=theme.SURFACE,
            bg_color=theme.SURFACE,
            border_width=0,
            corner_radius=0,
            height=28,
            width=200,
            font=theme.font(14),
        )
        self._entry_window = self.create_window(0, 0, window=self.entry, anchor="nw")
        self._entry_size = (200.0, 28.0)
        self.entry.bind("<Return>", lambda _event: on_search())
        self.entry.bind("<FocusIn>", lambda _event: self._focus_entry(True))
        self.entry.bind("<FocusOut>", lambda _event: self._focus_entry(False))

        self.bind("<Configure>", self._on_configure, add="+")
        self.bind("<Motion>", self._on_motion, add="+")
        self.bind("<Leave>", self._on_leave, add="+")
        self.bind("<ButtonPress-1>", self._on_press, add="+")
        self.bind("<ButtonRelease-1>", self._on_release, add="+")
        theme.on_mode_change(self, self._repaint)
        ctk.ScalingTracker.add_widget(self._rescaled, self)
        self._unsubscribe = downloads.subscribe(self._on_overview)
        self._unsubscribe_space = downloads.subscribe_space(self._on_low_space)
        self.configure(height=self._px(BAND_HEIGHT))

    # -- what MainWindow tells it -------------------------------------------------------------

    def start(self) -> None:
        """Load both sides' titles and start turning; the loop runs (MainWindow._start)."""
        if self._started:
            return
        self._started = True
        self._logo_task = self._spawn(self._load_logo(), "SpotlightBand.logo")
        self._load(self._side)
        self._load(_other(self._side))
        self._show(fade=False)  # the glow until the titles are known
        self._tick_job = self._later(TICK_MS, self._tick)

    def select(self, side: SpotlightSide | None) -> None:
        """Show `side`'s titles; None: the Downloads page (⬇ lit, the band keeps the last
        side's titles)."""
        self._selected = side
        if side is None or side is self._side:
            self._paint_controls()
            return
        self._indexes[self._side] = self._rotation.index
        self._side = side
        spotlight = self._spotlights.get(side)
        count = len(spotlight.items) if spotlight is not None else 0
        self._rotation.reset(count, self._indexes[side], time.monotonic())
        self._relayout(texts=False)
        self._show(fade=True)
        if side in self._stale:
            self._load(side)

    def reload(self) -> None:
        """Fetch both sides' titles again soon (the library, a grab or a setting changed)."""
        self._stale = set(SpotlightSide)
        if not self._started:
            return
        if self._reload_job is not None:
            self._cancel_job(self._reload_job)
        self._reload_job = self._later(RELOAD_MS, self._reload_now)

    def set_motion(self, on: bool) -> None:
        """Settings' `spotlight_motion`: turn to the next title by itself or stay put."""
        self._rotation.set_motion(on, time.monotonic())

    def set_compact(self, compact: bool) -> None:
        """MediaForge's page: only the top row with 🕘 on its quiet ground, no turning. False:
        the full band again, at once (see the module docstring)."""
        if compact == self._compact:
            return
        self._compact = compact
        self._hide_tip()
        self.configure(height=self._band_height())
        if compact:
            if self._show_task is not None and not self._show_task.done():
                self._show_task.cancel()
                self._scene_stale = True  # that picture hadn't come yet
            if self._warm_task is not None:
                self._warm_task.cancel()
            self.itemconfigure(self._scene_item, state="hidden")
            self._relayout()
            self._show_ground()
            return
        self._history_lit = False
        if self._ground_task is not None:
            self._ground_task.cancel()
        self.itemconfigure(self._ground_item, state="hidden")
        self.itemconfigure(self._scene_item, state="normal")
        rotation = self._rotation
        rotation.reset(rotation.count, rotation.index, time.monotonic())  # a full interval
        self._relayout()
        if self._scene_stale:
            self._scene_stale = False
            self._show(fade=False)

    def set_history_lit(self, lit: bool) -> None:
        """Whether History shows on MediaForge's page: 🕘 lit."""
        if lit != self._history_lit:
            self._history_lit = lit
            self._paint_controls()

    @property
    def compact(self) -> bool:
        return self._compact

    def set_person(self, name: str, initial: str, colour: theme.Pair) -> None:
        """The selected person on the name button: their `name`, the `initial` on their
        avatar, their `colour` (a theme.PERSON_* token)."""
        person = (name, initial, colour)
        if person != self._person:
            self._person = person
            self._relayout(texts=False)

    def set_phone(self, shown: bool) -> None:
        """📱 beside the name button, or not (a phone connected or not)."""
        shown = shown and self._on_phone is not None
        if shown != self._phone_shown:
            self._phone_shown = shown
            self._relayout(texts=False)

    def phone_box(self) -> tuple[int, int, int, int] | None:
        """📱 on the screen (as `person_box`): where the phones' menu opens under."""
        layout = self._layout
        return None if layout is None or layout.phone.w <= 0 else self._screen_box(layout.phone)

    def person_box(self) -> tuple[int, int, int, int] | None:
        """The name button on the screen (x, y, width, height in physical pixels): where its
        menu opens under; None without one."""
        layout = self._layout
        if layout is None or layout.person.w <= 0:
            return None
        return self._screen_box(layout.person)

    def _screen_box(self, rect: Rect) -> tuple[int, int, int, int]:
        return (
            self.winfo_rootx() + self._px(rect.x),
            self.winfo_rooty() + self._px(rect.y),
            self._px(rect.w),
            self._px(rect.h),
        )

    def jump(self, index: int) -> None:
        """Show the side's `index`th title (its dot)."""
        if self._rotation.jump(index, time.monotonic()):
            self._index_changed(fade=True)

    @property
    def current(self) -> SpotlightItem | None:
        """The title showing now."""
        spotlight = self._spotlights.get(self._side)
        if spotlight is None or not spotlight.items:
            return None
        return spotlight.items[min(self._rotation.index, len(spotlight.items) - 1)]

    @property
    def side(self) -> SpotlightSide:
        return self._side

    @property
    def rotation(self) -> Rotation:
        return self._rotation

    @property
    def layout(self) -> BandLayout | None:
        return self._layout

    def _later(self, ms: int, callback: Callable[[], object]) -> str:
        """A timer on the window, not on the band: MainWindow.destroy() cancels (and deletes)
        every pending timer of the window before its widgets go."""
        return self.winfo_toplevel().after(ms, callback)

    def _cancel_job(self, job: str) -> None:
        try:
            self.winfo_toplevel().after_cancel(job)
        except tk.TclError:
            pass  # closing

    def _spawn(self, coro: Coroutine[Any, Any, T], name: str) -> asyncio.Task[T] | None:
        """`spawn` while the band is there and the loop runs (a timer can fire as the window
        closes, after the loop stopped)."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return None
        if self._closed or not _exists(self):
            coro.close()
            return None
        return spawn(coro, name=name)

    # -- titles --------------------------------------------------------------------------------

    def _reload_now(self) -> None:
        self._reload_job = None
        if self._closed:
            return
        self._scenes.clear()  # a new TMDB key may bring pictures where the glow was
        self._fresh_picture = True  # … also for the title showing now
        self._load(self._side)
        self._load(_other(self._side))

    def _load(self, side: SpotlightSide) -> None:
        if not self._started:
            return
        task = self._load_tasks.get(side)
        if task is not None and not task.done():
            task.cancel()
        task = self._spawn(self._fetch(side), "SpotlightBand.load")
        if task is not None:
            self._load_tasks[side] = task

    async def _fetch(self, side: SpotlightSide) -> None:
        spotlight = await self._service.spotlight(side, profile_id=self._profile())
        if not self._closed and _exists(self):
            self._apply(spotlight)

    def _apply(self, spotlight: Spotlight) -> None:
        """New titles for a side: keep showing the same title where it still is."""
        side = spotlight.side
        self._stale.discard(side)
        old = self._spotlights.get(side)
        self._spotlights[side] = spotlight
        index = self._rotation.index if side is self._side else self._indexes[side]
        old_key = old.items[index].key if old is not None and index < len(old.items) else None
        keys = [item.key for item in spotlight.items]
        index = keys.index(old_key) if old_key in keys else 0
        self._indexes[side] = index
        if side is not self._side:
            return
        same_place = old_key is not None and old_key in keys and self._rotation.index == index
        self._rotation.count = len(keys)
        fresh, self._fresh_picture = self._fresh_picture, False
        if same_place and old is not None and len(old.items) == len(keys):
            self._rotation.index = index
            self._paint_texts()  # the same titles: perhaps another count in the muted line
            if fresh:
                self._show(fade=True)  # its picture baked again (a crossfade if it changed)
            return
        self._rotation.reset(len(keys), index, time.monotonic())
        self._relayout(texts=False)
        if same_place and not fresh:
            self._paint_texts()
        else:
            self._show(fade=True)

    def _index_changed(self, *, fade: bool) -> None:
        self._indexes[self._side] = self._rotation.index
        self._relayout(texts=False)  # the dots
        self._show(fade=fade)

    def _tick(self) -> None:
        self._tick_job = None
        if self._closed:
            return
        try:
            if self.winfo_viewable() and not self._compact:
                now = time.monotonic()
                self._rotation.hover(self._pointer_inside(), now)
                if self._rotation.due(now):
                    self._rotation.advance(now)
                    self._index_changed(fade=True)
        except tk.TclError:
            return  # closing
        self._tick_job = self._later(TICK_MS, self._tick)

    def _pointer_inside(self) -> bool:
        try:
            x, y = self.winfo_pointerxy()
            path = str(self.tk.call("winfo", "containing", x, y))
        except tk.TclError:
            return False
        own = str(self)
        return path == own or path.startswith(own + ".")

    # -- the picture ---------------------------------------------------------------------------

    def _show(self, *, fade: bool) -> None:
        """Bake the current title's picture and show it (crossfading with `fade`); while compact
        only the ground, the picture when the full band is back."""
        if not self._started:
            return
        if self._compact:
            self._scene_stale = True
            self._show_ground()
            return
        if self._show_task is not None:
            self._show_task.cancel()
        self._show_task = self._spawn(self._transition(fade), "SpotlightBand.show")

    async def _transition(self, fade: bool) -> None:
        item = self.current
        size = self._scene_size()
        if size is None:
            return  # not laid out yet: <Configure> bakes it
        scene = await self._bake(item, size)
        if scene is None or self._closed or not _exists(self):
            return
        shown = self._shown
        if scene is shown:
            self._paint_texts()  # the same picture: nothing to fade
        elif fade and shown is not None and self._rotation.motion:
            frames = await asyncio.to_thread(crossfade_frames, shown, scene, FADE_STEPS)
            middle = max(1, len(frames) // 2)
            for number, frame in enumerate(frames, 1):
                if self._closed or not _exists(self):
                    return
                self._put_scene(frame)
                if number == middle:
                    self._paint_texts()
                if number < len(frames):
                    await asyncio.sleep(FADE_MS / FADE_STEPS / 1000)
        else:
            self._put_scene(scene)
            self._paint_texts()
        self._warm_next()

    def _put_scene(self, image: Image.Image) -> None:
        self._set_image(self._scene_item, "scene", image)
        self._shown = image

    def _warm_next(self) -> None:
        """Bake the next title's picture ahead, so its fade starts at once."""
        spotlight = self._spotlights.get(self._side)
        size = self._scene_size()
        if spotlight is None or len(spotlight.items) < 2 or size is None:
            return
        following = spotlight.items[(self._rotation.index + 1) % len(spotlight.items)]
        if self._warm_task is not None:
            self._warm_task.cancel()
        self._warm_task = self._spawn(self._bake(following, size), "SpotlightBand.warm")

    def _scene_size(self) -> tuple[int, int] | None:
        if self._width <= 1:
            return None
        return self._width, self._px(BAND_HEIGHT)

    def _band_height(self) -> int:
        return self._px(COMPACT_HEIGHT if self._compact else BAND_HEIGHT)

    def _show_ground(self) -> None:
        """The compact band's ground: the glow a band without a title has, at this size and in
        this look (kept while it stays the same: going in again only shows the item)."""
        if self._width <= 1:
            return
        size = (self._width, self._band_height())
        palette = self._palette()
        key = (size, palette, theme.is_dark())
        if key == self._ground_key:
            self.itemconfigure(self._ground_item, state="normal")
            return
        if self._ground_task is not None:
            self._ground_task.cancel()
        self._ground_task = self._spawn(self._bake_ground(key, size, palette), "SpotlightBand.ground")

    async def _bake_ground(
        self,
        key: Hashable,
        size: tuple[int, int],
        palette: tuple[tuple[int, int, int], ...],
    ) -> None:
        bg, accent = palette[0], palette[1]
        image = await asyncio.to_thread(glow_layer, size, bg, accent, theme.is_dark())
        if self._closed or not _exists(self):
            return
        self._set_image(self._ground_item, "ground", image)
        self._ground_key = key
        if self._compact:
            self.itemconfigure(self._ground_item, state="normal")

    def _palette(self) -> tuple[tuple[int, int, int], ...]:
        """The colours a picture is baked in, read now (the theme may have changed)."""
        return (
            rgb(self, theme.BG),
            rgb(self, theme.ACCENT),
            rgb(self, theme.PANEL_EDGE),
            rgb(self, theme.PLACEHOLDER),
        )

    async def _bake(
        self, item: SpotlightItem | None, size: tuple[int, int]
    ) -> Image.Image | None:
        """`item`'s picture at `size` physical pixels: its backdrop (else the glow) with its
        poster. Kept for the next time (SCENE_CACHE). None once the band is gone."""
        if self._closed or not _exists(self):
            return None
        palette = self._palette()
        dark = theme.is_dark()
        key = (item.key if item else None, item.poster_url if item else None, size, palette, dark)
        cached = self._scenes.get(key)
        if cached is not None:
            self._scenes.move_to_end(key)
            return cached
        bg, accent, edge, placeholder = palette
        layer: Image.Image | None = None
        if item is not None:
            url = await self._service.backdrop_url(item, backdrop_size(size[0]))
            if url is not None:
                layer = await self._images.get(url, size, finish=backdrop_face(size, bg))
        if layer is None:
            layer = await asyncio.to_thread(glow_layer, size, bg, accent, dark)
        poster: Image.Image | None = None
        if item is not None:
            poster_size = (self._px(POSTER_WIDTH), self._px(POSTER_HEIGHT))
            face = FramedPoster(
                radius=self._px(POSTER_RADIUS), border=max(1, round(self._scale)), edge=edge
            )
            if item.poster_url:
                poster = await self._images.get(item.poster_url, poster_size, finish=face)
            if poster is None:
                poster = frame_poster(None, face, poster_size, placeholder)
        scene = await asyncio.to_thread(
            compose_scene,
            layer,
            poster,
            (self._px(PAD_X), self._px(POSTER_TOP)),
            radius=self._px(POSTER_RADIUS),
            shadow_blur=max(1, self._px(SHADOW_BLUR)),
            shadow_offset=self._px(SHADOW_OFFSET),
        )
        self._scenes[key] = scene
        while len(self._scenes) > SCENE_CACHE:
            self._scenes.popitem(last=False)
        return scene

    async def _load_logo(self) -> None:
        """The logo over its soft shadow (LOGO_SHADOW, the current look's alpha)."""
        blur, offset, alphas = LOGO_SHADOW
        alpha = alphas[theme.is_dark()]
        size, scale = self._px(LOGO_SIZE), self._scale
        logo = await asyncio.to_thread(
            _shadowed_logo, size, blur * scale, round(offset * scale), alpha
        )
        if logo is None or self._closed or not _exists(self):
            return
        image, self._logo_pad = logo
        self._set_image(self._logo_item, "logo", image)
        layout = self._layout
        if layout is not None:
            self._place_logo(layout)

    def _place_logo(self, layout: BandLayout) -> None:
        pad = self._logo_pad
        self.coords(self._logo_item, self._px(layout.logo.x) - pad, self._px(layout.logo.y) - pad)

    # -- layout and painting -------------------------------------------------------------------

    def _px(self, units: float) -> int:
        return round(units * self._scale)

    def _scaled(self, name: str) -> Any:
        return self._fonts[name].create_scaled_tuple(self._scale)

    def _on_configure(self, event: Any) -> None:
        if self._closed or event.width <= 1 or event.width == self._width:
            return
        self._width = event.width
        self._relayout()
        if not self._started:
            return
        if self._bake_job is not None:
            self._cancel_job(self._bake_job)
        self._bake_job = self._later(REBAKE_MS, self._rebake)

    def _rebake(self) -> None:
        self._bake_job = None
        if not self._closed:
            self._show(fade=False)

    def _relayout(self, *, texts: bool = True) -> None:
        """Place everything for the current width; `texts`: also the tag, title and line (not
        while a fade is about to change them)."""
        if self._width <= 1:
            return
        spotlight = self._spotlights.get(self._side)
        count = len(spotlight.items) if spotlight is not None else 0
        layout = band_layout(
            self._width / self._scale,
            word_width=self._fonts["word"].measure(APP_NAME),
            dots=0 if self._compact else count,
            current=self._rotation.index,
            person_width=self._person_width(),
            history=self._compact,
            phone=self._phone_shown,
            part_widths=self._part_widths() if self._on_part is not None else (),
        )
        self._layout = layout
        px = self._px
        self._place_logo(layout)
        first, second = APP_NAME[:3], APP_NAME[3:]  # "Air" + "Grabber"
        middle = px(layout.row_middle)
        self.coords(self._word_items[0], px(layout.word_x), middle)
        self.coords(
            self._word_items[1], px(layout.word_x + self._fonts["word"].measure(first)), middle
        )
        for item, part in zip(self._word_items, (first, second), strict=True):
            self.itemconfigure(item, text=part, font=self._scaled("word"))
        for item, name in (
            (self._tag_item, "tag"),
            (self._title_item, "title"),
            (self._sub_item, "sub"),
            (self._magnifier_item, "magnifier"),
            (self._history_glyph, "glyph"),
            (self._downloads_glyph, "glyph"),
            (self._badge_text, "badge"),
            (self._person_name, "person"),
            (self._person_arrow, "arrow"),
            (self._switch_badge_text, "badge"),
            *((item, "switch") for item in self._switch_texts.values()),
        ):
            self.itemconfigure(item, font=self._scaled(name))
        entry = layout.entry
        self.coords(self._entry_window, px(entry.x), px(entry.y))
        if self._entry_size != (entry.w, entry.h):
            self._entry_size = (entry.w, entry.h)
            self.entry.configure(width=entry.w, height=entry.h)
        self._paint_controls()
        self._paint_dots()
        if texts:
            self._paint_texts()

    def _set_image(self, item: int, name: str, image: Image.Image) -> None:
        if self._photo_sources.get(name) is image:
            return  # the same (cached) picture is there already
        self._photos[name] = ImageTk.PhotoImage(image, master=self)
        self._photo_sources[name] = image
        self.itemconfigure(item, image=self._photos[name])

    def _box(self, rect: Rect, radius: float, fill: RGBA, edge: RGBA | None = None) -> Image.Image:
        border = max(1, round(self._scale)) if edge is not None else 0
        return rounded_box((self._px(rect.w), self._px(rect.h)), radius, fill, edge, border)

    def _rgba(self, color: theme.Color, alpha: int = 255) -> RGBA:
        return (*rgb(self, color), alpha)

    def _hovered(self, kind: HitKind) -> bool:
        return self._hover is not None and self._hover.kind is kind

    # -- the main menu's switch -----------------------------------------------------------------

    @property
    def part(self) -> SwitchPart | None:
        """The switch's lit part (None: the Downloads page)."""
        return self._part

    @property
    def forge_count(self) -> int:
        """MediaForge's waiting files on the switch."""
        return self._forge_count

    def select_part(self, part: SwitchPart | None) -> None:
        """Light `part` of the main menu (None: none, the Downloads page)."""
        if part is not self._part:
            self._part = part
            self._paint_switch()

    @property
    def parts(self) -> tuple[SwitchPart, ...]:
        """The switch's parts, left to right."""
        return self._parts

    def set_library_shown(self, shown: bool) -> None:
        """The Library part on the switch (while Plex is connected) or not."""
        parts = PARTS_WITH_LIBRARY if shown else PARTS
        if parts != self._parts:
            self._parts = parts
            self._relayout()

    def set_forge_count(self, count: int) -> None:
        """MediaForge's waiting files: the amber badge on its word (hidden at 0)."""
        count = max(0, count)
        if count != self._forge_count:
            self._forge_count = count
            self._paint_switch()

    def _part_widths(self) -> tuple[float, ...]:
        """Series, Movies (and Library) as wide as each other, MediaForge by its own word."""
        font = self._fonts["switch"]
        names = {part: font.measure(t(label)) for part, label in PART_LABELS.items()}
        even = [part for part in self._parts if part is not SwitchPart.FORGE]
        side = max(names[part] for part in even) + 2 * SEGMENT_PAD_X
        return tuple(
            names[part] + 2 * SEGMENT_PAD_X if part is SwitchPart.FORGE else side
            for part in self._parts
        )

    def _switch_box(self, rect: Rect, fill: RGBA) -> Image.Image:
        """A part's or the badge's round box, the same picture for the same look."""
        size = (self._px(rect.w), self._px(rect.h))
        key = (size, fill)
        image = self._switch_images.get(key)
        if image is None:
            image = self._switch_images[key] = rounded_box(size, size[1] / 2, fill, None, 0)
        return image

    def _switch_pill(self, pill: Rect) -> tuple[Image.Image, int]:
        """The solid pill over its soft shadow, and how far that reaches past it."""
        px = self._px
        blur, offset, alphas = SWITCH_SHADOW
        fill, edge = self._rgba(theme.SURFACE, SWITCH_ALPHA), self._rgba(theme.CHIP_HOVER)
        key = ("track", px(pill.w), px(pill.h), fill, edge, theme.is_dark(), self._scale)
        cached = self._switch_images.get(key)
        pad_key = ("pad", key)
        if cached is None:
            cached, pad = shadowed_box(
                (px(pill.w), px(pill.h)),
                px(pill.h) / 2,
                fill,
                edge,
                max(1, round(self._scale)),
                (blur * self._scale, px(offset), alphas[theme.is_dark()]),
            )
            self._switch_images[key] = cached
            self._switch_pads[pad_key] = pad
        return cached, self._switch_pads[pad_key]

    def _paint_switch(self) -> None:
        layout = self._layout
        if layout is None or not layout.parts:
            return
        px = self._px
        parts = dict(zip(self._parts, layout.parts, strict=True))
        track, pad = self._switch_pill(layout.pill)
        self.coords(self._switch_track, px(layout.pill.x) - pad, px(layout.pill.y) - pad)
        self._set_image(self._switch_track, "switch-track", track)
        lit = self._part
        hit = self._hover
        hovered = self._parts[hit.dot] if hit is not None and hit.kind is HitKind.PART else None
        if hovered is not None and hovered is not lit:
            part = parts[hovered]
            self.coords(self._switch_hover, px(part.x), px(part.y))
            box = self._switch_box(part, self._rgba(theme.CHIP_HOVER))
            self._set_image(self._switch_hover, "switch-hover", box)
            self.itemconfigure(self._switch_hover, state="normal")
        else:
            self.itemconfigure(self._switch_hover, state="hidden")
        if lit is not None and lit in parts:
            part = parts[lit]
            self.coords(self._switch_active, px(part.x), px(part.y))
            box = self._switch_box(part, self._rgba(theme.ACCENT))
            self._set_image(self._switch_active, "switch-active", box)
            self.itemconfigure(self._switch_active, state="normal")
        else:
            self.itemconfigure(self._switch_active, state="hidden")
        for key, item in self._switch_texts.items():
            if key not in parts:
                self.itemconfigure(item, state="hidden")
                continue
            self.itemconfigure(item, state="normal")
            part = parts[key]
            colour = theme.ON_COLOR if key is lit else theme.TEXT
            self.coords(item, px(part.x + part.w / 2), px(part.y + part.h / 2))
            self.itemconfigure(item, text=t(PART_LABELS[key]), fill=theme.pick(colour))
        self._paint_switch_badge(parts[SwitchPart.FORGE])

    def _paint_switch_badge(self, part: Rect) -> None:
        """The amber badge (theme.WARNING, whatever the accent) right after MediaForge's word."""
        text = badge_text(self._forge_count)
        if not text:
            self.itemconfigure(self._switch_badge, state="hidden")
            self.itemconfigure(self._switch_badge_text, state="hidden")
            return
        px = self._px
        word = self._fonts["switch"].measure(t(PART_LABELS[SwitchPart.FORGE]))
        width = max(BADGE_HEIGHT, self._fonts["badge"].measure(text) + SWITCH_BADGE_PAD)
        x = part.x + part.w / 2 + word / 2 + SWITCH_BADGE_GAP
        rect = Rect(x, part.y + (part.h - BADGE_HEIGHT) / 2, width, BADGE_HEIGHT)
        self.coords(self._switch_badge, px(rect.x), px(rect.y))
        box = self._switch_box(rect, self._rgba(theme.WARNING))
        self._set_image(self._switch_badge, "switch-badge", box)
        self.coords(self._switch_badge_text, px(rect.x + rect.w / 2), px(rect.y + rect.h / 2))
        self.itemconfigure(self._switch_badge_text, text=text, fill=theme.pick(theme.ON_WARNING))
        self.itemconfigure(self._switch_badge, state="normal")
        self.itemconfigure(self._switch_badge_text, state="normal")

    def _paint_controls(self) -> None:
        layout = self._layout
        if layout is None:
            return
        self._paint_switch()
        px = self._px
        # The search field: a solid pill (the text box inside is the same colour).
        search = layout.search
        edge = theme.ACCENT if self._entry_focused else theme.EDGE
        self.coords(self._search_item, px(search.x), px(search.y))
        field = self._box(search, px(search.h) / 2, self._rgba(theme.SURFACE), self._rgba(edge))
        self._set_image(self._search_item, "search", field)
        magnifier = layout.magnifier
        self.coords(
            self._magnifier_item,
            px(magnifier.x + magnifier.w / 2),
            px(magnifier.y + magnifier.h / 2),
        )
        glass = theme.TEXT if self._hovered(HitKind.SEARCH) else theme.TEXT_MUTED
        self.itemconfigure(self._magnifier_item, fill=theme.pick(glass))
        # 🕘 (MediaForge's page, lit while History shows), ⬇ Downloads (lit while its page shows)
        # and the settings button.
        on_page = self._selected is None and not self._compact
        history = layout.history
        lit = {HitKind.HISTORY: self._history_lit, HitKind.DOWNLOADS: on_page}
        buttons = [
            (HitKind.DOWNLOADS, layout.downloads, self._downloads_item, "downloads"),
            (HitKind.SETTINGS, layout.settings, self._settings_item, "settings"),
        ]
        if history.w > 0:
            buttons.append((HitKind.HISTORY, history, self._history_item, "history"))
            self.coords(
                self._history_glyph, px(history.x + history.w / 2), px(history.y + history.h / 2)
            )
            glyph = theme.ON_ACCENT if self._history_lit else theme.BUTTON_TEXT
            self.itemconfigure(self._history_glyph, fill=theme.pick(glyph))
        for item in (self._history_item, self._history_glyph):
            self.itemconfigure(item, state="normal" if history.w > 0 else "hidden")
        mobile = layout.phone
        if mobile.w > 0:
            buttons.append((HitKind.PHONE, mobile, self._phone_item, "phone"))
            self.coords(
                self._phone_glyph, px(mobile.x + mobile.w / 2), px(mobile.y + mobile.h / 2)
            )
            colour = rgb(self, theme.BUTTON_TEXT)
            self._set_image(self._phone_glyph, "phone-icon", _phone(px(ICON_SIZE), colour))
        for item in (self._phone_item, self._phone_glyph):
            self.itemconfigure(item, state="normal" if mobile.w > 0 else "hidden")
        for kind, rect, item, name in buttons:
            lit_button = lit.get(kind, False)
            fill = theme.ACCENT if lit_button else theme.HOVER if self._hovered(kind) else theme.SURFACE
            border = theme.ACCENT if lit_button else theme.EDGE
            self.coords(item, px(rect.x), px(rect.y))
            image = self._box(rect, px(BUTTON_RADIUS), self._rgba(fill), self._rgba(border))
            self._set_image(item, name, image)
        downloads, settings = layout.downloads, layout.settings
        self.coords(
            self._downloads_glyph,
            px(downloads.x + downloads.w / 2),
            px(downloads.y + downloads.h / 2),
        )
        glyph = theme.ON_ACCENT if on_page else theme.BUTTON_TEXT
        self.itemconfigure(self._downloads_glyph, fill=theme.pick(glyph))
        self.coords(
            self._settings_icon, px(settings.x + settings.w / 2), px(settings.y + settings.h / 2)
        )
        self._set_image(
            self._settings_icon, "sliders", _sliders(px(ICON_SIZE), rgb(self, theme.BUTTON_TEXT))
        )
        self._paint_badge()
        self._paint_person()

    def _person_width(self) -> float:
        """The name button's width (units); 0 without a person."""
        if self._person is None or self._on_person is None:
            return 0.0
        name = self._fonts["person"].measure(self._person[0])
        return person_width(name, self._fonts["arrow"].measure(PERSON_ARROW))

    def _paint_person(self) -> None:
        """The name button: a pill like the other buttons, the person's avatar at its left
        end, their name (cut short past PERSON_NAME_MAX) and ▾."""
        layout, person = self._layout, self._person
        items = (self._person_item, self._avatar_item, self._person_name, self._person_arrow)
        if layout is None or person is None or layout.person.w <= 0:
            for item in items:
                self.itemconfigure(item, state="hidden")
            return
        name, initial, colour = person
        px = self._px
        rect, disc = layout.person, layout.avatar
        fill = theme.HOVER if self._hovered(HitKind.PERSON) else theme.SURFACE
        self.coords(self._person_item, px(rect.x), px(rect.y))
        image = self._box(rect, px(rect.h) / 2, self._rgba(fill), self._rgba(theme.EDGE))
        self._set_image(self._person_item, "person", image)
        self.coords(self._avatar_item, px(disc.x), px(disc.y))
        picture = avatar_image(initial, rgb(self, colour), px(disc.w))
        self._set_image(self._avatar_item, "avatar", picture)
        shown = elide_to_width(name, PERSON_NAME_MAX, self._fonts["person"].measure)
        name_x = disc.right + PERSON_NAME_GAP
        middle = px(rect.y + rect.h / 2)
        self.coords(self._person_name, px(name_x), middle)
        self.itemconfigure(self._person_name, text=shown, fill=theme.pick(theme.BUTTON_TEXT))
        arrow_x = name_x + min(self._fonts["person"].measure(name), PERSON_NAME_MAX)
        self.coords(self._person_arrow, px(arrow_x + PERSON_ARROW_GAP), middle)
        self.itemconfigure(
            self._person_arrow, text=PERSON_ARROW, fill=theme.pick(theme.TEXT_MUTED)
        )
        for item in items:
            self.itemconfigure(item, state="normal")

    def _paint_badge(self) -> None:
        """⬇'s green badge: how many downloads are on their way."""
        layout = self._layout
        corner = layout.badge if layout is not None else None
        self._paint_count(
            self._badge_item, self._badge_text, "badge", corner, self._downloading, theme.SUCCESS
        )

    def _paint_count(
        self,
        item: int,
        text_item: int,
        name: str,
        corner: tuple[float, float] | None,
        count: int,
        colour: theme.Color,
    ) -> None:
        """A button's badge: `count` (badge_text: "99+" above 99) in ON_COLOR on a `colour` pill
        with a BG ring, its top-right at `corner`; hidden for none."""
        text = badge_text(count)
        if corner is None or not text:
            self.itemconfigure(item, state="hidden")
            self.itemconfigure(text_item, state="hidden")
            return
        px = self._px
        width = max(BADGE_HEIGHT, self._fonts["badge"].measure(text) + 10)
        right, top = corner
        rect = Rect(right - width, top, width, BADGE_HEIGHT)
        border = max(1, round(2 * self._scale))
        image = rounded_box(
            (px(rect.w), px(rect.h)),
            px(rect.h) / 2,
            self._rgba(colour),
            self._rgba(theme.BG),
            border,
        )
        self.coords(item, px(rect.x), px(rect.y))
        self._set_image(item, name, image)
        self.coords(text_item, px(rect.x + rect.w / 2), px(rect.y + rect.h / 2))
        self.itemconfigure(text_item, text=text, fill=theme.pick(theme.ON_COLOR))
        self.itemconfigure(item, state="normal")
        self.itemconfigure(text_item, state="normal")

    def _paint_dots(self) -> None:
        for item in self._dot_items:
            self.delete(item)
        self._dot_items = []
        layout = self._layout
        if layout is None:
            return
        colour = rgb(self, theme.TEXT)
        hover = self._hover
        hovered = hover.dot if hover is not None and hover.kind is HitKind.DOT else -1
        for index, rect in enumerate(layout.dots):
            alpha = DOT_ALPHA[2 if index == self._rotation.index else 1 if index == hovered else 0]
            item = self.create_image(self._px(rect.x), self._px(rect.y), anchor="nw")
            image = self._box(rect, self._px(rect.h) / 2, (*colour, alpha))
            self._photos[f"dot{index}"] = ImageTk.PhotoImage(image, master=self)
            self._photo_sources.pop(f"dot{index}", None)
            self.itemconfigure(item, image=self._photos[f"dot{index}"])
            self._dot_items.append(item)

    def _paint_texts(self) -> None:
        """The tag, the title and the muted line of the title showing (none without one)."""
        layout = self._layout
        if layout is None:
            return
        item = self.current
        if item is None or self._compact:
            tag = title = line = ""
        else:
            tag = tag_text(item, self._clock())
            title = title_text(item)
            line = sub_line(self._spotlights.get(self._side), item)
        room = max(1.0, layout.text_right - layout.text_x)
        title = elide_to_width(title, room, self._fonts["title"].measure)
        line = elide_to_width(line, room, self._fonts["sub"].measure)
        heights = {
            name: float(self._fonts[name].metrics("linespace")) for name in ("tag", "title", "sub")
        }
        bottom = layout.poster.bottom + 2  # the line's descent sits level with the poster
        sub_y = bottom - (heights["sub"] if line else 0.0)
        title_y = sub_y - heights["title"]
        tag_y = title_y - heights["tag"]
        px = self._px
        for canvas_item, text, y, colour in (
            (self._tag_item, tag, tag_y, theme.LINK),
            (self._title_item, title, title_y, theme.TEXT),
            (self._sub_item, line, sub_y, theme.TEXT_MUTED),
        ):
            self.coords(canvas_item, px(layout.text_x), px(y))
            self.itemconfigure(canvas_item, text=text, fill=theme.pick(colour))
        self.itemconfigure(self._word_items[0], fill=theme.pick(theme.TEXT))
        self.itemconfigure(self._word_items[1], fill=theme.pick(theme.BRAND))

    # -- the pointer ---------------------------------------------------------------------------

    def _hit_at(self, x: int, y: int) -> Hit | None:
        layout = self._layout
        if layout is None:
            return None
        spotlight = self._spotlights.get(self._side)
        items = len(spotlight.items) if spotlight is not None and not self._compact else 0
        return hit_target(layout, x / self._scale, y / self._scale, items=items)

    def _on_home_at(self, x: int, y: int) -> bool:
        """Whether (x, y) px is on the logo or "AirGrabber" (with an `on_home` to call)."""
        layout = self._layout
        if layout is None or self._on_home is None:
            return False
        return layout.brand.contains(x / self._scale, y / self._scale)

    def _on_motion(self, event: Any) -> None:
        home = self._on_home_at(event.x, event.y)
        self._set_home_hover(home)
        self._set_hover(None if home else self._hit_at(event.x, event.y))

    def _on_leave(self, _event: Any) -> None:
        self._set_home_hover(False)
        self._set_hover(None)

    def _set_home_hover(self, on: bool) -> None:
        if on != self._home_hover:
            self._home_hover = on
            self._show_cursor()

    def _show_cursor(self) -> bool:
        """The pointer's look for what it is over; False once the band is gone."""
        hit = self._hover
        if self._home_hover:
            cursor = "hand2"
        else:
            cursor = "" if hit is None else "xterm" if hit.kind is HitKind.FIELD else "hand2"
        try:
            self.configure(cursor=cursor)
        except tk.TclError:
            return False
        return True

    def _set_hover(self, hit: Hit | None) -> None:
        if hit == self._hover:
            return
        previous, self._hover = self._hover, hit
        if not self._show_cursor():
            return
        self._hide_tip()
        if hit is not None and hit.kind in _TIPS:
            self._tip_job = self._later(TIP_DELAY_MS, lambda: self._show_tip(hit))
        kinds = {h.kind for h in (previous, hit) if h is not None}
        if kinds & _CONTROLS:
            self._paint_controls()
        if HitKind.DOT in kinds:
            self._paint_dots()

    def _on_press(self, event: Any) -> None:
        self._hide_tip()
        self._home_pressed = self._on_home_at(event.x, event.y)
        self._pressed = None if self._home_pressed else self._hit_at(event.x, event.y)

    def _on_release(self, event: Any) -> None:
        if self._home_pressed:
            self._home_pressed = False
            if self._on_home is not None and self._on_home_at(event.x, event.y):
                self._on_home()
            return
        hit, pressed = self._hit_at(event.x, event.y), self._pressed
        self._pressed = None
        if hit is not None and hit == pressed:
            self._activate(hit)

    def _activate(self, hit: Hit) -> None:
        kind = hit.kind
        if kind is HitKind.PART:
            if self._on_part is not None:
                self._on_part(self._parts[hit.dot])
            return
        if kind is HitKind.SEARCH:
            self._on_search()
        elif kind is HitKind.FIELD:
            self.entry.focus_set()
        elif kind is HitKind.HISTORY:
            self._on_history()
        elif kind is HitKind.DOWNLOADS:
            self._on_downloads()
        elif kind is HitKind.SETTINGS:
            self._on_settings()
        elif kind is HitKind.PERSON:
            if self._on_person is not None:
                self._on_person()
        elif kind is HitKind.PHONE:
            if self._on_phone is not None:
                self._on_phone()
        elif kind is HitKind.DOT:
            self.jump(hit.dot)
        elif (item := self.current) is not None:
            self._on_open(item)

    def _focus_entry(self, focused: bool) -> None:
        if focused != self._entry_focused:
            self._entry_focused = focused
            self._paint_controls()

    def _show_tip(self, hit: Hit) -> None:
        self._tip_job = None
        layout = self._layout
        if layout is None or hit != self._hover or not self.winfo_ismapped():
            return
        rect = {
            HitKind.SEARCH: layout.magnifier,
            HitKind.HISTORY: layout.history,
            HitKind.DOWNLOADS: layout.downloads,
            HitKind.SETTINGS: layout.settings,
            HitKind.PHONE: layout.phone,
        }[hit.kind]
        left, top = self.winfo_rootx() + self._px(rect.x), self.winfo_rooty() + self._px(rect.y)
        screen = (left, top, left + self._px(rect.w), top + self._px(rect.h))
        self._tip.show(screen, t(_TIPS[hit.kind]), self._px(tooltip.GAP))

    def _hide_tip(self) -> None:
        if self._tip_job is not None:
            self._cancel_job(self._tip_job)
            self._tip_job = None
        self._tip.hide()

    # -- the downloads -------------------------------------------------------------------------

    def _on_overview(self, overview: DownloadsOverview) -> None:
        if not _exists(self):
            return
        count = overview.in_progress
        if count != self._downloading:
            self._downloading = count
            self._paint_badge()

    def _on_low_space(self, space: FreeSpace) -> None:
        self._report(space_status_text(space), SPACE_NOTICE_MS)

    # -- light / dark, scaling, closing --------------------------------------------------------

    def _repaint(self) -> None:
        """Light ↔ dark: the background, the controls, the texts and the picture again."""
        self.configure(background=theme.pick(theme.BG))
        self._tip.destroy()  # made again in the new colours
        self._scenes.clear()
        self._relayout()
        if self._started:  # the logo's shadow is lighter in the light look
            self._logo_task = self._spawn(self._load_logo(), "SpotlightBand.logo")
        self._show(fade=False)

    def _rescaled(self, _widget_scaling: float, _window_scaling: float) -> None:
        if not _exists(self):
            return
        self._scale = float(ctk.ScalingTracker.get_widget_scaling(self))
        self.configure(height=self._band_height())
        self._photo_sources.clear()
        self._relayout()
        if self._started:
            self._logo_task = self._spawn(self._load_logo(), "SpotlightBand.logo")
            self._show(fade=False)

    def destroy(self) -> None:
        self._closed = True
        tasks = (
            *self._load_tasks.values(),
            self._show_task,
            self._warm_task,
            self._logo_task,
            self._ground_task,
        )
        for task in tasks:
            if task is not None:
                task.cancel()
        for job in (self._tick_job, self._bake_job, self._tip_job, self._reload_job):
            if job is not None:
                try:
                    self._cancel_job(job)
                except tk.TclError:
                    pass
        self._unsubscribe()
        self._unsubscribe_space()
        ctk.ScalingTracker.remove_widget(self._rescaled, self)
        self._tip.destroy()
        super().destroy()


class _Tip:
    """A hover tip for the band's drawn buttons (they are no widgets of their own), looking
    like app/ui/tooltip.py's: a rounded SURFACE box with one line, under the button."""

    def __init__(self, owner: tk.Misc) -> None:
        self._owner = owner
        self._top: tk.Toplevel | None = None
        self._label: ctk.CTkLabel | None = None

    def show(self, rect: tuple[int, int, int, int], text: str, gap: int) -> None:
        if self._top is None:
            self._build()
        top, label = self._top, self._label
        assert top is not None and label is not None
        label.configure(text=text)
        top.update_idletasks()
        size = (top.winfo_reqwidth(), top.winfo_reqheight())
        middle = ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
        screen = tooltip.work_area(*middle)
        if screen is None:
            width, height = self._owner.winfo_toplevel().wm_maxsize()
            screen = (0, 0, width, height)
        x, y = tooltip.tip_position(rect, size, screen, gap)
        top.geometry(f"+{x}+{y}")
        top.deiconify()
        top.lift()

    def _build(self) -> None:
        bg = theme.pick(theme.BG)
        top = tk.Toplevel(self._owner, bg=bg, takefocus=False)
        top.withdraw()
        top.overrideredirect(True)
        try:
            top.attributes("-topmost", True)
            if sys.platform == "win32":
                top.attributes("-transparentcolor", bg)
        except tk.TclError:
            pass
        box = ctk.CTkFrame(
            top,
            fg_color=theme.SURFACE,
            border_width=1,
            border_color=theme.EDGE,
            corner_radius=tooltip.CORNER_RADIUS,
        )
        box.pack()
        self._label = ctk.CTkLabel(
            box, text="", height=1, font=theme.font(tooltip.FONT_SIZE), text_color=theme.TEXT
        )
        self._label.pack(padx=tooltip.PAD_X, pady=tooltip.PAD_Y)
        self._top = top

    def hide(self) -> None:
        if self._top is not None:
            try:
                self._top.withdraw()
            except tk.TclError:
                pass

    def destroy(self) -> None:
        if self._top is not None:
            try:
                self._top.destroy()
            except tk.TclError:
                pass
        self._top = self._label = None


def _shadowed_logo(
    size: int, blur: float, offset: int, alpha: float
) -> tuple[Image.Image, int] | None:
    """The logo `size` pixels tall over its `drop_shadow`, and how far that reaches past it
    (Pillow work: call it in a worker thread). None without the logo."""
    logo = load_logo(size)
    return None if logo is None else drop_shadow(logo, blur, offset, alpha)


_sliders_cache: dict[tuple[int, tuple[int, int, int]], Image.Image] = {}
_phone_cache: dict[tuple[int, tuple[int, int, int]], Image.Image] = {}


def _phone(size: int, colour: tuple[int, int, int]) -> Image.Image:
    """📱's icon, drawn once per size and colour."""
    key = (size, colour)
    image = _phone_cache.get(key)
    if image is None:
        image = _phone_cache[key] = phone_icon(size, colour)
    return image


def _sliders(size: int, colour: tuple[int, int, int]) -> Image.Image:
    """The settings icon, drawn once per size and colour (tiny: fine on the event loop)."""
    key = (size, colour)
    image = _sliders_cache.get(key)
    if image is None:
        image = _sliders_cache[key] = sliders_icon(size, colour)
    return image


def _other(side: SpotlightSide) -> SpotlightSide:
    return SpotlightSide.MOVIES if side is SpotlightSide.SERIES else SpotlightSide.SERIES


def _exists(widget: tk.Misc) -> bool:
    try:
        return bool(widget.winfo_exists())
    except tk.TclError:
        return False
