"""The main window's header: the spotlight band, edge to edge, BAND_HEIGHT units tall.

Its picture is a title's TMDB backdrop, blurred, dimmed and fading into the page's background at
the bottom (as the info window's: app/ui/backdrop.py), or without one a soft glow in the accent
colour. Over it, on the top row: the logo and "AirGrabber", the Series / Movies switch in the
middle, the search field, ⬇ Downloads (with its badge while downloads are on their way) and the
settings button on the right. Under them the spotlight: the small poster, a tag ("TONIGHT ·
21:00", "TRENDING", …), the title, a muted line ("3 episodes today · 1 downloading") and a dot
per title. The titles come from app/services/spotlight.py: Series and Movies each have their
own; the Downloads page keeps the last side's.

Every ROTATE_S seconds the next title fades in (FADE_STEPS frames over FADE_MS, blended in a
worker thread), not while the pointer is over the band, not at all with Settings'
`spotlight_motion` off (a dot still jumps, without the fade). A click on the poster, the texts
or the picture opens the title's info window (`on_open`).

Tk can't draw a widget see-through over a picture, so everything but the search field's text
box is drawn on this canvas: the picture and the poster as one image baked in physical pixels,
the switch, the field and the buttons as anti-aliased RGBA images with canvas text on them, the
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
    band_face,
    compose_scene,
    crossfade_frames,
    glow_layer,
    rounded_box,
)
from app.ui.branding import load_logo
from app.ui.icons import sliders_icon
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
    SEGMENT_FONT_SIZE,
    SEGMENT_PAD_X,
    WORD_SIZE,
    BandLayout,
    Hit,
    HitKind,
    Rect,
    Rotation,
    band_layout,
    hit_target,
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

SIDE_LABELS = {SpotlightSide.SERIES: N_("Series"), SpotlightSide.MOVIES: N_("Movies")}
SEARCH_PLACEHOLDER = N_("Search shows & movies…")
SEARCH_TIP = N_("Search")
DOWNLOADS_TIP = N_("Downloads")
SETTINGS_TIP = N_("Settings")
_TIPS = {HitKind.SEARCH: SEARCH_TIP, HitKind.DOWNLOADS: DOWNLOADS_TIP, HitKind.SETTINGS: SETTINGS_TIP}
_CONTROLS = frozenset(
    {HitKind.SERIES, HitKind.MOVIES, HitKind.SEARCH, HitKind.FIELD, HitKind.DOWNLOADS, HitKind.SETTINGS}
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
# The switch's see-through track over the picture: (light, dark).
TRACK_FILL: tuple[RGBA, RGBA] = ((255, 255, 255, 150), (0, 0, 0, 90))
TRACK_EDGE: tuple[RGBA, RGBA] = ((0, 0, 0, 24), (255, 255, 255, 28))
DOT_ALPHA = (90, 170, 255)  # a dot: others, under the pointer, the one showing


class SpotlightBand(tk.Canvas):
    """The header (see the module docstring). MainWindow calls `start` once the loop runs,
    `select` when the page changes (None: the Downloads page), `reload` when the library, a
    download or a setting changed, and `set_motion` for Settings' `spotlight_motion`. `entry`
    is the search field's text box."""

    def __init__(
        self,
        master: Any,
        *,
        spotlight: SpotlightService,
        downloads: DownloadStatusService,
        images: ImageCache,
        motion: bool = True,
        on_side: Callable[[SpotlightSide], None],
        on_downloads: Callable[[], None],
        on_settings: Callable[[], None],
        on_search: Callable[[], None],
        on_open: Callable[[SpotlightItem], None],
        report: Callable[[str, int], None],
        clock: Callable[[], datetime] = lambda: datetime.now().astimezone(),
    ) -> None:
        """`clock`: the local time now, aware (the tag's day and time; tests set it)."""
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
        self._on_side = on_side
        self._on_downloads = on_downloads
        self._on_settings = on_settings
        self._on_search = on_search
        self._on_open = on_open
        self._report = report
        self._clock = clock
        self._started = False
        self._closed = False
        self._fresh_picture = False  # bake the title showing again once its side reloaded
        self._side = SpotlightSide.SERIES  # whose titles show
        self._selected: SpotlightSide | None = SpotlightSide.SERIES  # the switch's lit part
        self._spotlights: dict[SpotlightSide, Spotlight] = {}
        self._stale: set[SpotlightSide] = set(SpotlightSide)
        self._indexes = dict.fromkeys(SpotlightSide, 0)
        self._rotation = Rotation(motion=motion)
        latest = downloads.latest
        self._downloading = latest.in_progress if latest is not None else 0
        self._scale = float(ctk.ScalingTracker.get_widget_scaling(self))
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
            "segment": theme.font(SEGMENT_FONT_SIZE, bold=True),
            "tag": theme.font(11, bold=True),
            "title": theme.font(18, bold=True),
            "sub": theme.font(13),
            "magnifier": theme.font(15),
            "glyph": theme.font(20),
            "badge": theme.font(10, bold=True),
        }

        def text(anchor: Literal["nw", "w", "center"] = "nw") -> int:
            return self.create_text(0, 0, anchor=anchor, text="")

        self._scene_item = self.create_image(0, 0, anchor="nw")
        self._logo_item = self.create_image(0, 0, anchor="nw")
        self._word_items = (text("w"), text("w"))
        self._tag_item, self._title_item, self._sub_item = text(), text(), text()
        self._track_item = self.create_image(0, 0, anchor="nw")
        self._active_item = self.create_image(0, 0, anchor="nw")
        self._segment_items = {side: text("center") for side in SpotlightSide}
        self._search_item = self.create_image(0, 0, anchor="nw")
        self._magnifier_item = self.create_text(0, 0, anchor="center", text="🔍")
        self._downloads_item = self.create_image(0, 0, anchor="nw")
        self._downloads_glyph = self.create_text(0, 0, anchor="center", text="⬇")
        self._settings_item = self.create_image(0, 0, anchor="nw")
        self._settings_icon = self.create_image(0, 0, anchor="center")
        self._badge_item = self.create_image(0, 0, anchor="nw", state="hidden")
        self._badge_text = self.create_text(0, 0, anchor="center", text="", state="hidden")
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
        self.bind("<Leave>", lambda _event: self._set_hover(None), add="+")
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
        """Light `side` on the switch and show its titles; None: the Downloads page (⬇ lit,
        the band keeps the last side's titles)."""
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
        spotlight = await self._service.spotlight(side)
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
                layer = await self._images.get(url, size, finish=band_face(size, bg))
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
        logo = await asyncio.to_thread(load_logo, self._px(LOGO_SIZE))
        if logo is not None and not self._closed and _exists(self):
            self._set_image(self._logo_item, "logo", logo)

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
        segment = max(self._fonts["segment"].measure(t(label)) for label in SIDE_LABELS.values())
        layout = band_layout(
            self._width / self._scale,
            word_width=self._fonts["word"].measure(APP_NAME),
            segment_width=segment + 2 * SEGMENT_PAD_X,
            dots=count,
            current=self._rotation.index,
        )
        self._layout = layout
        px = self._px
        self.coords(self._logo_item, px(layout.logo.x), px(layout.logo.y))
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
            (self._downloads_glyph, "glyph"),
            (self._badge_text, "badge"),
        ):
            self.itemconfigure(item, font=self._scaled(name))
        for item in self._segment_items.values():
            self.itemconfigure(item, font=self._scaled("segment"))
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
        px, dark = self._px, theme.is_dark()
        # The Series / Movies switch: a see-through track, the lit part in the accent colour.
        pill = layout.pill
        self.coords(self._track_item, px(pill.x), px(pill.y))
        track = self._box(pill, px(pill.h) / 2, TRACK_FILL[dark], TRACK_EDGE[dark])
        self._set_image(self._track_item, "track", track)
        lit = self._selected
        if lit is not None:
            part = layout.segments[0 if lit is SpotlightSide.SERIES else 1]
            self.coords(self._active_item, px(part.x), px(part.y))
            self._set_image(
                self._active_item, "active", self._box(part, px(part.h) / 2, self._rgba(theme.ACCENT))
            )
            self.itemconfigure(self._active_item, state="normal")
        else:
            self.itemconfigure(self._active_item, state="hidden")
        for (side, item), part in zip(self._segment_items.items(), layout.segments, strict=True):
            kind = HitKind.SERIES if side is SpotlightSide.SERIES else HitKind.MOVIES
            colour = (
                theme.ON_ACCENT
                if side is lit
                else theme.TEXT
                if self._hovered(kind)
                else theme.TEXT_MUTED
            )
            self.coords(item, px(part.x + part.w / 2), px(part.y + part.h / 2))
            self.itemconfigure(item, text=t(SIDE_LABELS[side]), fill=theme.pick(colour))
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
        # ⬇ Downloads (lit while its page shows) and the settings button.
        on_page = self._selected is None
        for kind, rect, item, name in (
            (HitKind.DOWNLOADS, layout.downloads, self._downloads_item, "downloads"),
            (HitKind.SETTINGS, layout.settings, self._settings_item, "settings"),
        ):
            lit_button = kind is HitKind.DOWNLOADS and on_page
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

    def _paint_badge(self) -> None:
        layout = self._layout
        text = badge_text(self._downloading)
        if layout is None or not text:
            self.itemconfigure(self._badge_item, state="hidden")
            self.itemconfigure(self._badge_text, state="hidden")
            return
        px = self._px
        width = max(BADGE_HEIGHT, self._fonts["badge"].measure(text) + 10)
        right, top = layout.badge
        rect = Rect(right - width, top, width, BADGE_HEIGHT)
        border = max(1, round(2 * self._scale))
        image = rounded_box(
            (px(rect.w), px(rect.h)),
            px(rect.h) / 2,
            self._rgba(theme.SUCCESS),
            self._rgba(theme.BG),
            border,
        )
        self.coords(self._badge_item, px(rect.x), px(rect.y))
        self._set_image(self._badge_item, "badge", image)
        self.coords(self._badge_text, px(rect.x + rect.w / 2), px(rect.y + rect.h / 2))
        self.itemconfigure(self._badge_text, text=text, fill=theme.pick(theme.ON_COLOR))
        self.itemconfigure(self._badge_item, state="normal")
        self.itemconfigure(self._badge_text, state="normal")

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
            line = sub_line(self._spotlights.get(self._side), self._downloading)
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
        self.itemconfigure(self._word_items[1], fill=theme.pick(theme.LINK))

    # -- the pointer ---------------------------------------------------------------------------

    def _hit_at(self, x: int, y: int) -> Hit | None:
        layout = self._layout
        if layout is None:
            return None
        spotlight = self._spotlights.get(self._side)
        items = len(spotlight.items) if spotlight is not None else 0
        return hit_target(layout, x / self._scale, y / self._scale, items=items)

    def _on_motion(self, event: Any) -> None:
        self._set_hover(self._hit_at(event.x, event.y))

    def _set_hover(self, hit: Hit | None) -> None:
        if hit == self._hover:
            return
        previous, self._hover = self._hover, hit
        cursor = "" if hit is None else "xterm" if hit.kind is HitKind.FIELD else "hand2"
        try:
            self.configure(cursor=cursor)
        except tk.TclError:
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
        self._pressed = self._hit_at(event.x, event.y)

    def _on_release(self, event: Any) -> None:
        hit, pressed = self._hit_at(event.x, event.y), self._pressed
        self._pressed = None
        if hit is not None and hit == pressed:
            self._activate(hit)

    def _activate(self, hit: Hit) -> None:
        kind = hit.kind
        if kind is HitKind.SERIES:
            self._on_side(SpotlightSide.SERIES)
        elif kind is HitKind.MOVIES:
            self._on_side(SpotlightSide.MOVIES)
        elif kind is HitKind.SEARCH:
            self._on_search()
        elif kind is HitKind.FIELD:
            self.entry.focus_set()
        elif kind is HitKind.DOWNLOADS:
            self._on_downloads()
        elif kind is HitKind.SETTINGS:
            self._on_settings()
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
            self._paint_texts()

    def _on_low_space(self, space: FreeSpace) -> None:
        self._report(space_status_text(space), SPACE_NOTICE_MS)

    # -- light / dark, scaling, closing --------------------------------------------------------

    def _repaint(self) -> None:
        """Light ↔ dark: the background, the controls, the texts and the picture again."""
        self.configure(background=theme.pick(theme.BG))
        self._tip.destroy()  # made again in the new colours
        self._scenes.clear()
        self._relayout()
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
