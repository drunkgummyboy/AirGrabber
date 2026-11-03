"""The main window's header: the spotlight band, edge to edge, BAND_HEIGHT units tall.

Its picture is a title's TMDB backdrop, blurred, dimmed and fading into the page's background at
the bottom (as the info window's: app/ui/backdrop.py) under a left-to-right scrim of that
background (darkest behind the logo), or without one a soft glow in the accent colour. Over it,
on the top row: the logo (over a soft shadow) and "AirGrabber" ("Grabber" in BRAND) on the left
(a click on either goes home, `on_home`: Series > Calendar),
the search field, 🔨 MediaForge (with a copper badge while MediaForge's renamer needs the owner:
`set_forge_count`), ⬇ Downloads (with its badge while downloads are on their way), the
settings button and, at the right end, the name button (the selected person's avatar, name and
▾, `set_person`; a click calls `on_person`, the people menu) on the right. The Series side's
titles are the selected person's (`profile`). Under them the
spotlight: the small poster, a tag ("TONIGHT · 21:00", "TRENDING", …), the title, a muted line
("3 episodes today", a film's "2026 · ★ 7.0") and a dot per title. The titles come from
app/services/spotlight.py: Series and Movies each have their own; the Downloads page keeps the
last side's.

The Series / Movies switch (`ModeSwitch`, below) is a canvas of its own at the right end of the
tabs row under the band: a solid pill over a soft shadow on the page's background.

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
from app.forge.ui.look import forge_look
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
from app.ui.icons import sliders_icon
from app.ui.people import avatar_image
from app.ui.poster_card import rgb
from app.ui.spotlight import (
    BADGE_HEIGHT,
    BAND_HEIGHT,
    BUTTON_RADIUS,
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
    WORD_SIZE,
    BandLayout,
    Hit,
    HitKind,
    Rect,
    Rotation,
    SwitchLayout,
    band_layout,
    hit_target,
    person_width,
    sub_line,
    switch_hit,
    switch_layout,
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

SIDE_LABELS = {SpotlightSide.SERIES: N_("Series"), SpotlightSide.MOVIES: N_("Movies")}
SEARCH_PLACEHOLDER = N_("Search shows & movies…")
SEARCH_TIP = N_("Search")
FORGE_TIP = N_("MediaForge")
DOWNLOADS_TIP = N_("Downloads")
SETTINGS_TIP = N_("Settings")
_TIPS = {
    HitKind.SEARCH: SEARCH_TIP,
    HitKind.FORGE: FORGE_TIP,
    HitKind.DOWNLOADS: DOWNLOADS_TIP,
    HitKind.SETTINGS: SETTINGS_TIP,
}
_CONTROLS = frozenset(
    {
        HitKind.SEARCH,
        HitKind.FIELD,
        HitKind.FORGE,
        HitKind.DOWNLOADS,
        HitKind.SETTINGS,
        HitKind.PERSON,
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
# The Series / Movies switch (ModeSwitch) stands on a solid pill: SURFACE, nearly opaque, a
# CHIP_HOVER edge, over a soft shadow; the part under the pointer (not the lit one) a CHIP_HOVER
# fill.
SWITCH_ALPHA = 235  # the pill's SURFACE (of 255)
# Soft shadows (blur and offset in units, alpha (light, dark)): the switch's and the logo's.
SWITCH_SHADOW = (5.0, 2, (0.2, 0.5))
LOGO_SHADOW = (4.0, 2, (0.25, 0.6))
DOT_ALPHA = (90, 170, 255)  # a dot: others, under the pointer, the one showing


class SpotlightBand(tk.Canvas):
    """The header (see the module docstring). MainWindow calls `start` once the loop runs,
    `select` when the page changes (whose titles show; None: the Downloads page), `reload` when
    the library, a download or a setting changed, and `set_motion` for Settings'
    `spotlight_motion` and `set_forge_count` with MediaForge's "needs you" count; a click on
    🔨 calls `on_forge`. `set_person` puts the selected person on the name button (none until
    then); a click on it calls `on_person`. A click on the logo or "AirGrabber" calls
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
        on_forge: Callable[[], None],
        on_settings: Callable[[], None],
        on_search: Callable[[], None],
        on_open: Callable[[SpotlightItem], None],
        report: Callable[[str, int], None],
        profile: Callable[[], int],
        on_person: Callable[[], None] | None = None,
        on_home: Callable[[], None] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
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
        self._on_forge = on_forge
        self._on_settings = on_settings
        self._on_search = on_search
        self._on_open = on_open
        self._report = report
        self._profile = profile
        self._on_person = on_person
        self._on_home = on_home
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
        self._spotlights: dict[SpotlightSide, Spotlight] = {}
        self._stale: set[SpotlightSide] = set(SpotlightSide)
        self._indexes = dict.fromkeys(SpotlightSide, 0)
        self._rotation = Rotation(motion=motion)
        latest = downloads.latest
        self._downloading = latest.in_progress if latest is not None else 0
        self._forge_needs = 0  # MediaForge's "needs you" count: the 🔨 button's badge
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
        }

        def text(anchor: Literal["nw", "w", "center"] = "nw") -> int:
            return self.create_text(0, 0, anchor=anchor, text="")

        self._scene_item = self.create_image(0, 0, anchor="nw")
        self._logo_item = self.create_image(0, 0, anchor="nw")
        self._word_items = (text("w"), text("w"))
        self._tag_item, self._title_item, self._sub_item = text(), text(), text()
        self._search_item = self.create_image(0, 0, anchor="nw")
        self._magnifier_item = self.create_text(0, 0, anchor="center", text="🔍")
        self._forge_item = self.create_image(0, 0, anchor="nw")
        self._forge_glyph = self.create_text(0, 0, anchor="center", text="🔨")
        self._downloads_item = self.create_image(0, 0, anchor="nw")
        self._downloads_glyph = self.create_text(0, 0, anchor="center", text="⬇")
        self._settings_item = self.create_image(0, 0, anchor="nw")
        self._settings_icon = self.create_image(0, 0, anchor="center")
        self._badge_item = self.create_image(0, 0, anchor="nw", state="hidden")
        self._badge_text = self.create_text(0, 0, anchor="center", text="", state="hidden")
        self._forge_badge_item = self.create_image(0, 0, anchor="nw", state="hidden")
        self._forge_badge_text = self.create_text(0, 0, anchor="center", text="", state="hidden")
        self._person_item = self.create_image(0, 0, anchor="nw", state="hidden")
        self._avatar_item = self.create_image(0, 0, anchor="nw", state="hidden")
        self._person_name = self.create_text(0, 0, anchor="w", text="", state="hidden")
        self._person_arrow = self.create_text(0, 0, anchor="w", text="", state="hidden")
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

    def set_forge_count(self, count: int) -> None:
        """MediaForge's "needs you" count (ForgeHost.on_needs): the 🔨 button's copper badge
        (hidden at 0)."""
        count = max(0, count)
        if count != self._forge_needs:
            self._forge_needs = count
            self._paint_forge_badge()

    def set_person(self, name: str, initial: str, colour: theme.Pair) -> None:
        """The selected person on the name button: their `name`, the `initial` on their
        avatar, their `colour` (a theme.PERSON_* token)."""
        person = (name, initial, colour)
        if person != self._person:
            self._person = person
            self._relayout(texts=False)

    def person_box(self) -> tuple[int, int, int, int] | None:
        """The name button on the screen (x, y, width, height in physical pixels): where its
        menu opens under; None without one."""
        layout = self._layout
        if layout is None or layout.person.w <= 0:
            return None
        rect = layout.person
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
            if self.winfo_viewable():
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
        """Bake the current title's picture and show it (crossfading with `fade`)."""
        if not self._started:
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
            dots=count,
            current=self._rotation.index,
            person_width=self._person_width(),
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
            (self._forge_glyph, "glyph"),
            (self._downloads_glyph, "glyph"),
            (self._badge_text, "badge"),
            (self._forge_badge_text, "badge"),
            (self._person_name, "person"),
            (self._person_arrow, "arrow"),
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

    def _paint_controls(self) -> None:
        layout = self._layout
        if layout is None:
            return
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
        # 🔨 MediaForge, ⬇ Downloads (lit while its page shows) and the settings button.
        on_page = self._selected is None
        for kind, rect, item, name in (
            (HitKind.FORGE, layout.forge, self._forge_item, "forge"),
            (HitKind.DOWNLOADS, layout.downloads, self._downloads_item, "downloads"),
            (HitKind.SETTINGS, layout.settings, self._settings_item, "settings"),
        ):
            lit_button = kind is HitKind.DOWNLOADS and on_page
            fill = theme.ACCENT if lit_button else theme.HOVER if self._hovered(kind) else theme.SURFACE
            border = theme.ACCENT if lit_button else theme.EDGE
            self.coords(item, px(rect.x), px(rect.y))
            image = self._box(rect, px(BUTTON_RADIUS), self._rgba(fill), self._rgba(border))
            self._set_image(item, name, image)
        forge, downloads, settings = layout.forge, layout.downloads, layout.settings
        self.coords(self._forge_glyph, px(forge.x + forge.w / 2), px(forge.y + forge.h / 2))
        self.itemconfigure(self._forge_glyph, fill=theme.pick(theme.BUTTON_TEXT))
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
        self._paint_forge_badge()
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

    def _paint_forge_badge(self) -> None:
        """🔨's copper badge (the Forge look's ACCENT): how many files MediaForge needs the
        owner for."""
        layout = self._layout
        corner = layout.forge_badge if layout is not None else None
        self._paint_count(
            self._forge_badge_item,
            self._forge_badge_text,
            "forge_badge",
            corner,
            self._forge_needs,
            forge_look().ACCENT,
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
        """A button's badge: `count` (badge_text: "99+" above 99) in white ON_COLOR on a
        `colour` pill with a BG ring, its top-right at `corner`; hidden for none."""
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
        if item is None:
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
        items = len(spotlight.items) if spotlight is not None else 0
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
        if kind is HitKind.SEARCH:
            self._on_search()
        elif kind is HitKind.FIELD:
            self.entry.focus_set()
        elif kind is HitKind.FORGE:
            self._on_forge()
        elif kind is HitKind.DOWNLOADS:
            self._on_downloads()
        elif kind is HitKind.SETTINGS:
            self._on_settings()
        elif kind is HitKind.PERSON:
            if self._on_person is not None:
                self._on_person()
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
            HitKind.FORGE: layout.forge,
            HitKind.DOWNLOADS: layout.downloads,
            HitKind.SETTINGS: layout.settings,
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
        self.configure(height=self._px(BAND_HEIGHT))
        self._photo_sources.clear()
        self._relayout()
        if self._started:
            self._logo_task = self._spawn(self._load_logo(), "SpotlightBand.logo")
            self._show(fade=False)

    def destroy(self) -> None:
        self._closed = True
        tasks = (*self._load_tasks.values(), self._show_task, self._warm_task, self._logo_task)
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


class ModeSwitch(tk.Canvas):
    """The Series / Movies switch at the right end of the tabs row under the band: a solid pill
    (SURFACE, nearly opaque, a CHIP_HOVER edge) over a soft shadow on the page's background,
    the lit part in the accent colour with white text, the other one in TEXT (a CHIP_HOVER fill
    under the pointer). A click on a part calls `on_side`; MainWindow lights the page's part
    with `select` (None: the Downloads page, neither lit). Its size follows the font and
    Windows' scaling (`switch_layout`); a switch between light and dark paints it again."""

    def __init__(self, master: Any, *, on_side: Callable[[SpotlightSide], None]) -> None:
        super().__init__(
            master,
            highlightthickness=0,
            borderwidth=0,
            width=1,
            height=1,
            background=theme.pick(theme.BG),
        )
        self._on_side = on_side
        self._scale = float(ctk.ScalingTracker.get_widget_scaling(self))
        self._selected: SpotlightSide | None = SpotlightSide.SERIES
        self._hover: SpotlightSide | None = None
        self._pressed: SpotlightSide | None = None
        self._font = theme.font(SEGMENT_FONT_SIZE, bold=True)
        self._photos: dict[str, ImageTk.PhotoImage] = {}
        self._photo_sources: dict[str, Image.Image] = {}
        self._track_item = self.create_image(0, 0, anchor="nw")
        self._hover_item = self.create_image(0, 0, anchor="nw", state="hidden")
        self._active_item = self.create_image(0, 0, anchor="nw")
        self._text_items = {
            side: self.create_text(0, 0, anchor="center", text="") for side in SpotlightSide
        }
        self._layout = self._measure()
        self.bind("<Motion>", self._on_motion, add="+")
        self.bind("<Leave>", lambda _event: self._set_hover(None), add="+")
        self.bind("<ButtonPress-1>", self._on_press, add="+")
        self.bind("<ButtonRelease-1>", self._on_release, add="+")
        theme.on_mode_change(self, self._repaint)
        ctk.ScalingTracker.add_widget(self._rescaled, self)
        self._relayout()

    @property
    def layout(self) -> SwitchLayout:
        return self._layout

    @property
    def selected(self) -> SpotlightSide | None:
        return self._selected

    def select(self, side: SpotlightSide | None) -> None:
        """Light `side`'s part (None: neither, the Downloads page)."""
        if side is not self._selected:
            self._selected = side
            self._paint()

    def _px(self, units: float) -> int:
        return round(units * self._scale)

    def _measure(self) -> SwitchLayout:
        name = max(self._font.measure(t(label)) for label in SIDE_LABELS.values())
        return switch_layout(
            segment_width=name + 2 * SEGMENT_PAD_X,
            line_height=float(self._font.metrics("linespace")),
        )

    def _relayout(self) -> None:
        self._layout = self._measure()
        width, height = self._layout.size
        self.configure(width=self._px(width), height=self._px(height))
        font = self._font.create_scaled_tuple(self._scale)
        for item in self._text_items.values():
            self.itemconfigure(item, font=font)
        self._paint()

    def _set_image(self, item: int, name: str, image: Image.Image) -> None:
        if self._photo_sources.get(name) is image:
            return
        self._photos[name] = ImageTk.PhotoImage(image, master=self)
        self._photo_sources[name] = image
        self.itemconfigure(item, image=self._photos[name])

    def _rgba(self, color: theme.Color, alpha: int = 255) -> RGBA:
        return (*rgb(self, color), alpha)

    def _box(self, rect: Rect, fill: RGBA) -> Image.Image:
        size = (self._px(rect.w), self._px(rect.h))
        return rounded_box(size, size[1] / 2, fill, None, 0)

    def _paint(self) -> None:
        px, layout = self._px, self._layout
        pill = layout.pill
        blur, offset, alphas = SWITCH_SHADOW
        track, pad = shadowed_box(
            (px(pill.w), px(pill.h)),
            px(pill.h) / 2,
            self._rgba(theme.SURFACE, SWITCH_ALPHA),
            self._rgba(theme.CHIP_HOVER),
            max(1, round(self._scale)),
            (blur * self._scale, px(offset), alphas[theme.is_dark()]),
        )
        self.coords(self._track_item, px(pill.x) - pad, px(pill.y) - pad)
        self._set_image(self._track_item, "track", track)
        lit, hovered = self._selected, self._hover
        parts = dict(zip(SpotlightSide, layout.segments, strict=True))
        if hovered is not None and hovered is not lit:
            part = parts[hovered]
            self.coords(self._hover_item, px(part.x), px(part.y))
            self._set_image(self._hover_item, "hover", self._box(part, self._rgba(theme.CHIP_HOVER)))
            self.itemconfigure(self._hover_item, state="normal")
        else:
            self.itemconfigure(self._hover_item, state="hidden")
        if lit is not None:
            part = parts[lit]
            self.coords(self._active_item, px(part.x), px(part.y))
            self._set_image(self._active_item, "active", self._box(part, self._rgba(theme.ACCENT)))
            self.itemconfigure(self._active_item, state="normal")
        else:
            self.itemconfigure(self._active_item, state="hidden")
        for side, item in self._text_items.items():
            part = parts[side]
            colour = theme.ON_COLOR if side is lit else theme.TEXT
            self.coords(item, px(part.x + part.w / 2), px(part.y + part.h / 2))
            self.itemconfigure(item, text=t(SIDE_LABELS[side]), fill=theme.pick(colour))

    def _side_at(self, x: int, y: int) -> SpotlightSide | None:
        index = switch_hit(self._layout, x / self._scale, y / self._scale)
        return None if index is None else tuple(SpotlightSide)[index]

    def _on_motion(self, event: Any) -> None:
        self._set_hover(self._side_at(event.x, event.y))

    def _set_hover(self, side: SpotlightSide | None) -> None:
        if side is self._hover:
            return
        self._hover = side
        try:
            self.configure(cursor="" if side is None else "hand2")
        except tk.TclError:
            return
        self._paint()

    def _on_press(self, event: Any) -> None:
        self._pressed = self._side_at(event.x, event.y)

    def _on_release(self, event: Any) -> None:
        side, pressed = self._side_at(event.x, event.y), self._pressed
        self._pressed = None
        if side is not None and side is pressed:
            self._on_side(side)

    def _repaint(self) -> None:
        """Light ↔ dark: the background and the pill in the new colours."""
        self.configure(background=theme.pick(theme.BG))
        self._paint()

    def _rescaled(self, _widget_scaling: float, _window_scaling: float) -> None:
        if not _exists(self):
            return
        self._scale = float(ctk.ScalingTracker.get_widget_scaling(self))
        self._photo_sources.clear()
        self._relayout()

    def destroy(self) -> None:
        ctk.ScalingTracker.remove_widget(self._rescaled, self)
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
