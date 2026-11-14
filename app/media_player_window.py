"""The video player's window (app/media_player.py's process; `start_player` / `run_player`).

A borderless window in AirGrabber's dark look, like the trailer window: the video in a thin
frame (MARGIN; none in full screen), libVLC drawing into `video` (`EngineSeam.attach`), and
the controls floating over the picture.

How the controls float: libVLC draws with Direct3D into a child window of its own inside
`video`, and Tk widgets placed over that child aren't reliably drawn above it (VLC's own
interface keeps its full-screen controls in a separate window for the same reason). So the
controls live in `overlay`, a borderless top-level window owned by this one (above it, hidden
and shown with it, stay-on-top with it) that always covers the video exactly (`_follow`). Its
background is a colour key (KEY_COLOUR): Windows shows the video through it and passes clicks
there on to the video, so only the bands, buttons and cards show, at OVERLAY_ALPHA. It hides
while the window is dragged, then comes back where the video went. libVLC leaves the mouse
and keys to us (`VlcEngine.attach`), so the pointer moving over the picture and a double click
reach `video`.

What shows (the spec's "The window"): the top band (title line, stay on top in window mode,
full screen, ✕), the bottom band (the seek bar with elapsed / remaining time, a time under the
pointer, play / pause, back 10 s / forward 30 s, mute and volume, the Audio and Subtitles
menus with Find subtitles… and Always pick: …), shown on any movement or key and hidden after
IDLE_S without (`controls_shown`: not while paused or with the pointer on them). Floating on
their own: the resume prompt (when the app asks and the video doesn't start at the
beginning), Skip intro (inside Plex's intro marker), the next episode countdown (from
`countdown_from`, counted in video time, so it waits while paused) and a Notice line.

libVLC's end event counts as the end only near it (`played_to_the_end`): it also comes when a
read fails part-way (a share or Wi-Fi gone, a cut-off file), and that video pauses where it
stopped instead (`Position` paused, no `Ended`: it isn't marked watched); Play opens it again
there. After a start past the beginning, a position of 0 is never believed until libVLC has
said another (`_zero_wait`).

The mini player (Ctrl+H as VLC's minimal interface, the top band's button; `toggle_mini`) is
this same window made small, borderless all round and always on top, so the video plays on:
only the picture, and on hover a round play / pause, back to the window, ✕ and a thin seek
strip. It starts in the work area's bottom-right corner (`first_mini`), then where it was last
left; dragged by its picture it tucks into a corner it is let go near (`tuck`), and resized by
its edges it goes back to 16:9 once the mouse button is up (`reshape`). Esc, Ctrl+H, the button
or a double click go back to the window; F goes to full screen and from there to the window.

Threads: every engine call runs on the EngineWorker's thread; the stdin reader and libVLC's
end event hand their news over through one queue this window drains every DRAIN_MS. Nothing
off Tk's thread touches Tk. Remembered in the settings file: the window's place and size, the mini
player's (`mini`), stay on top and the volume.
"""

from __future__ import annotations

import ctypes
import functools
import queue
import re
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import customtkinter as ctk

from app.media_player import (
    EXIT_CLOSED,
    LANGUAGES,
    RELEASE_WAIT_S,
    EngineSeam,
    EngineWorker,
    Event,
    Incoming,
    InputEnded,
    Look,
    Rect,
    Tick,
    TrackLists,
    VideoEnded,
    load_settings,
    read_input,
    save_settings,
    work_area_under_pointer,
)
from app.media_player_logic import (
    COUNTDOWN_S,
    JUMP_BACK_MS,
    JUMP_FORWARD_MS,
    KEYS,
    MINI_LEAST,
    OFF,
    SUBTITLES_OFF,
    Track,
    clock,
    controls_shown,
    countdown_from,
    first_geometry,
    first_mini,
    intro_button,
    is_mini_key,
    language_key,
    mini_shape,
    pick_track,
    played_to_the_end,
    tucked,
)
from app.media_protocol import (
    AlwaysPick,
    Ended,
    FindSubtitles,
    LoadSubtitle,
    Message,
    Notice,
    Play,
    Position,
    Ready,
    Stopped,
    Tracks,
    WantNext,
    encode,
)
from app.ui import borderless, theme
from app.ui.borderless import CLOSE_GAP, CLOSE_ROOM, CloseButton
from app.ui.branding import set_window_icon
from app.ui.missing_control import MenuEntry, PopupMenu
from app.ui.tooltip import Tooltip, add_tooltip

DRAIN_MS = 50  # how often the queue from the other threads is emptied
REFRESH_MS = 250  # how often the controls' fading and the notice are checked
REPORT_S = 10.0  # a Position this often while playing (and on every pause and seek)
NOTICE_S = 5.0  # a notice shows this long
LOOKING_S = 60.0  # "Looking for subtitles…" until the app answers, at most this long
VOLUME_STEP = 5
END_ROOM_MS = 1_000  # a jump forward stops this far before the end
MIN_WIDTH, MIN_HEIGHT = 480, 270
SHAPE_MS = 300  # the mini player's shape put back this long after its last resize
DRAG_START_PX = 4  # a press on the mini player's picture moved this far: a drag

# CTk units (scaled by Windows' scaling, like the rest of the app).
MARGIN = 12  # the frame around the video in a window
BAND_PAD = 16
FLOAT_INSET = 24  # Skip intro and the countdown from the video's right and bottom edges
MINI_INSET = 8  # the mini player's controls from its edges
OVERLAY_ALPHA = 0.92
KEY_COLOUR = "#010203"  # the overlay's see-through colour (nothing else uses it)

# Segoe Fluent Icons (Windows 11) / Segoe MDL2 Assets (Windows 10): the media glyphs.
ICON_FONTS = ("Segoe Fluent Icons", "Segoe MDL2 Assets")
PLAY_GLYPH = ""
PAUSE_GLYPH = ""
BACK_GLYPH = ""  # SkipBack10
FORWARD_GLYPH = ""  # SkipForward30
VOLUME_GLYPH = ""
MUTED_GLYPH = ""
FULL_SCREEN_GLYPH = ""
WINDOW_GLYPH = ""
PIN_GLYPH = ""
PINNED_GLYPH = ""
MINI_GLYPH = "\uee49"  # the mini player
BIG_GLYPH = "\uee47"  # back to the window
CLOSE_GLYPH = "\ue711"
MENU_ARROW = "▾"
MINUS = "−"

_GEOMETRY = re.compile(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)")
TrackKind = Literal["audio", "subtitles"]


def _mix(first: str, second: str, share: float) -> str:
    """`first` moved `share` of the way to `second` (#rrggbb)."""
    a = [int(first[i : i + 2], 16) for i in (1, 3, 5)]
    b = [int(second[i : i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * share):02x}" for x, y in zip(a, b, strict=True))


def _place_of(geometry: str) -> list[int] | None:
    """A Tk geometry ("WxH+X+Y") as the settings file keeps it: [x, y, width, height]."""
    match = _GEOMETRY.fullmatch(geometry or "")
    if match is None:
        return None
    width, height, x, y = (int(part) for part in match.groups())
    return [x, y, width, height]


def _saved_place(saved: object, least: tuple[int, int]) -> list[int] | None:
    """A place from the settings file: four whole numbers, at least `least` big; else None."""
    if not (isinstance(saved, list) and len(saved) == 4):
        return None
    if not all(isinstance(n, int) and not isinstance(n, bool) for n in saved):
        return None
    if saved[2] < least[0] or saved[3] < least[1]:
        return None
    return [int(n) for n in saved]


def _left_button_down() -> bool:
    """Whether the left mouse button is held (a resize by the window's edge still going)."""
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.WinDLL("user32").GetAsyncKeyState(0x01) & 0x8000)  # VK_LBUTTON
    except (OSError, AttributeError):
        return False


def _icon_family(widget: tk.Misc) -> str:
    try:
        families = set(tkfont.families(widget))
    except tk.TclError:
        families = set()
    return next((name for name in ICON_FONTS if name in families), ICON_FONTS[-1])


_GWLP_HWNDPARENT = -8
_GWL_EXSTYLE, _WS_EX_TOPMOST = -20, 0x0008
_GW_HWNDPREV = 3
_SWP_NOSIZE, _SWP_NOMOVE, _SWP_NOACTIVATE = 0x0001, 0x0002, 0x0010
_DWMWA_WINDOW_CORNER_PREFERENCE = 33  # Windows 11
_DWMWCP_DONOTROUND, _DWMWCP_ROUND = 1, 2


def _wrapper(window: tk.Misc) -> int:
    """Tk's wrapper (the real top-level window) of a toplevel; 0 when there is none."""
    try:
        return int(window.wm_frame(), 16)  # type: ignore[attr-defined]
    except (tk.TclError, ValueError, AttributeError):
        return 0


def _own(window: tk.Misc, owner: tk.Misc | None) -> None:
    """Make `owner`'s wrapper the owner of `window`'s (None: no owner): Windows then keeps the
    overlay above the player and minimises and restores it with it. Tk gives an override-redirect
    window no owner (`wm transient` doesn't). Windows destroys an owned window with its owner, and
    Tk replaces the player's wrapper when full screen switches: the overlay is let go first
    (`toggle_full_screen`) and owned again on the next `_follow`."""
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.WinDLL("user32")
        user32.SetWindowLongPtrW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t)
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.GetWindowLongPtrW.argtypes = (ctypes.c_void_p, ctypes.c_int)
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        hwnd = _wrapper(window)
        wanted = _wrapper(owner) if owner is not None else 0
        if hwnd and user32.GetWindowLongPtrW(hwnd, _GWLP_HWNDPARENT) != wanted:
            user32.SetWindowLongPtrW(hwnd, _GWLP_HWNDPARENT, wanted)
            if wanted:
                _just_above(user32, hwnd, wanted)
    except (OSError, AttributeError):
        pass


def _just_above(user32: Any, hwnd: int, owner: int) -> None:
    """`hwnd` put right above `owner` without activating it: a new owner doesn't move a window,
    and Tk's new wrapper (full screen switched) is drawn on top of the overlay it lets go."""
    user32.GetWindow.argtypes = (ctypes.c_void_p, ctypes.c_uint)
    user32.GetWindow.restype = ctypes.c_void_p
    user32.SetWindowPos.argtypes = (
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ctypes.c_uint,
    )

    def topmost(window: int) -> bool:
        return bool(user32.GetWindowLongPtrW(window, _GWL_EXSTYLE) & _WS_EX_TOPMOST)

    after = user32.GetWindow(owner, _GW_HWNDPREV) or 0
    if after == hwnd:
        return
    if after and topmost(after) != topmost(owner):
        after = 0  # HWND_TOP: the top of the owner's own band, not into the topmost ones
    user32.SetWindowPos(hwnd, after, 0, 0, 0, 0, _SWP_NOSIZE | _SWP_NOMOVE | _SWP_NOACTIVATE)


def _round_corners(window: tk.Misc, on: bool) -> None:
    """Windows 11's rounded corners on (a window) or off (full screen: square to the screen)."""
    if sys.platform != "win32":
        return
    try:
        value = ctypes.c_int(_DWMWCP_ROUND if on else _DWMWCP_DONOTROUND)
        ctypes.WinDLL("dwmapi").DwmSetWindowAttribute(
            ctypes.c_void_p(_wrapper(window)),
            _DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(value),
            ctypes.sizeof(value),
        )
    except (OSError, AttributeError):
        pass


class _Overlay(tk.Toplevel):
    """The controls' window. It holds CTk widgets, so customtkinter treats it as one of its
    windows when Windows' scaling changes (a move to another monitor): it rescales the widgets
    (good) and dims the window and blocks its size events meanwhile, which a plain Toplevel can't
    take; here those are no-ops, and only `see_through` sets how see-through it is."""

    def block_update_dimensions_event(self) -> None:
        pass

    def unblock_update_dimensions_event(self) -> None:
        pass

    def see_through(self, alpha: float) -> None:
        tk.Toplevel.wm_attributes(self, "-alpha", alpha)

    def wm_attributes(self, *args: Any, **kwargs: Any) -> Any:
        if len(args) == 2 and args[0] == "-alpha":
            return None  # customtkinter's dimming around a rescale
        return tk.Toplevel.wm_attributes(self, *args, **kwargs)

    attributes = wm_attributes


class PlayerWindow(ctk.CTkToplevel):
    """The player's window (see the module's notes). Driven by `handle` (the queue's events),
    the keys (`press`) and its controls; it says what happens through `send` (one line each)."""

    def __init__(
        self,
        master: tk.Misc,
        engine: EngineSeam,
        look: Look,
        *,
        send: Callable[[str], None],
        settings_path: Path,
        work_area: Callable[[], Rect] = work_area_under_pointer,
        on_screen: Callable[[Rect], bool] = lambda _rect: True,
        pointer: Callable[[], tuple[int, int]] | None = None,
        now: Callable[[], float] = time.monotonic,
        button_down: Callable[[], bool] = _left_button_down,
    ) -> None:
        super().__init__(master, fg_color=look.background)
        self.look = look
        self.events: queue.Queue[Event] = queue.Queue()
        self.worker = EngineWorker(engine, self.events)
        self._send = send
        self._settings_path = settings_path
        self._now = now
        self._pointer = pointer or self.winfo_pointerxy
        self._jobs: dict[str, str] = {}
        self._closed = False

        # The video and its state.
        self._play: Play | None = None
        self._generation = 0
        self._position = 0
        self._duration = 0
        self._started = False  # playing (or paused) since the prompt or the start
        self._paused = True
        self._ended = False
        self._pending = 0  # the engine job a tick must have seen (the last seek or start)
        self._reported_at: float | None = None
        self._audio: tuple[Track, ...] = ()
        self._subtitles: tuple[Track, ...] = ()
        self._audio_id: int | None = None  # None: the file's own choice, untouched
        self._subtitle_id: int | None = None
        self._picked_audio: str | None = None  # a language picked by hand in this video
        self._picked_subtitles: str | None = None
        self._always: dict[TrackKind, str] = {"audio": "", "subtitles": ""}
        self._applied: set[TrackKind] = set()
        self._new_subtitle = False  # a found subtitle loading: it becomes the current track
        self._finding = ""  # the language Find subtitles asked for
        self._countdown_at: int | None = None  # where the countdown began (video time)
        self._countdown_cancelled = False
        self._want_sent = False
        self._sound_set = False  # the volume given again once the video plays (libVLC's output)
        # After a start past the beginning libVLC says 0 until it has opened the file and applied
        # the start time (long, for 4K on a sleeping NAS; for ever when it can't open it): ticks
        # saying 0 are passed over until a real position comes, so 0 never reaches Plex.
        self._zero_wait = False
        # libVLC's end event before the video's end (a read that failed part-way): where it
        # stopped. Paused there; Play opens the file again there (`_reopen`).
        self._cut: int | None = None
        self._prompt = False
        self._skip = False
        self._countdown = False
        self._dragging_seek: float | None = None

        settings = load_settings(settings_path)
        volume = settings.get("volume")
        self._volume = volume if isinstance(volume, int) and 0 <= volume <= 100 else 100
        self._muted = False
        self._on_top = settings.get("on_top") is True
        self._full_screen = False
        self._window_geometry = ""  # the window's, kept while full screen
        # The mini player (`toggle_mini`): the window's geometry kept meanwhile, and the mini
        # player's last place ([x, y, width, height] as `geometry` is saved).
        self._mini = False
        self._normal_geometry = ""
        self._mini_place = _saved_place(settings.get("mini"), MINI_LEAST)
        self._work_area = work_area
        self._on_screen = on_screen
        self._button_down = button_down
        self._mini_size: tuple[int, int] | None = None  # its last shape put right (pixels)
        self._press: tuple[int, int] | None = None  # a press on its picture (a drag to come?)
        self._last_activity = now()
        self._last_pointer: tuple[int, int] | None = None
        self._controls_visible = False
        self._notice = ""
        self._notice_until = 0.0
        self._menu: PopupMenu | None = None
        self._native: borderless.Native | None = None
        self._win32 = False  # the real borderless window (tests' stand-in: no Win32 calls here)

        self.title(look.title)
        set_window_icon(self)  # AirGrabber's, not customtkinter's
        self.minsize(MIN_WIDTH, MIN_HEIGHT)
        self._place_window(settings.get("geometry"), work_area, on_screen)
        self.video = tk.Frame(self, bg="#000000", highlightthickness=0, takefocus=0)
        self._place_video()
        self._build_overlay()
        self._go_borderless()
        if self._on_top:
            self.attributes("-topmost", True)
            self.overlay.attributes("-topmost", True)

        self.protocol("WM_DELETE_WINDOW", self.close)
        # Alt+F4 with the controls active closes the player, not just its controls; the overlay
        # gone some other way does too (`_on_overlay_destroy`).
        self.overlay.protocol("WM_DELETE_WINDOW", self.close)
        self.overlay.bind("<Destroy>", self._on_overlay_destroy, add="+")
        for target in (self, self.overlay):
            target.bind("<KeyPress>", self._on_key, add="+")
            target.bind("<Motion>", self._on_motion, add="+")
            target.bind("<MouseWheel>", self._on_wheel, add="+")
        self.video.bind("<Double-Button-1>", self._on_double)
        self.video.bind("<ButtonPress-1>", self._on_video_press, add="+")
        self.video.bind("<B1-Motion>", self._on_video_drag, add="+")
        self.bind("<ButtonPress-1>", self._on_press_frame, add="+")
        self.bind("<Configure>", self._on_configure, add="+")
        self.bind("<Unmap>", self._on_unmap, add="+")
        self.bind("<Map>", self._on_map, add="+")
        self.bind("<Destroy>", self._on_destroy, add="+")
        self._show_controls(True)
        self._schedule("drain", DRAIN_MS, self._drain)
        self._schedule("refresh", REFRESH_MS, self._refresh_loop)

    # -- what tests and the controls read -----------------------------------------------------

    @property
    def position_ms(self) -> int:
        return self._position

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def volume(self) -> int:
        return self._volume

    @property
    def full_screen(self) -> bool:
        return self._full_screen

    @property
    def on_top(self) -> bool:
        return self._on_top

    @property
    def mini(self) -> bool:
        return self._mini

    @property
    def controls_visible(self) -> bool:
        return self._controls_visible

    @property
    def prompt_shown(self) -> bool:
        return self._prompt

    @property
    def skip_shown(self) -> bool:
        return self._skip

    @property
    def countdown_shown(self) -> bool:
        return self._countdown

    @property
    def notice_text(self) -> str:
        return self._notice

    @property
    def audio_tracks(self) -> tuple[Track, ...]:
        return self._audio

    @property
    def subtitle_tracks(self) -> tuple[Track, ...]:
        return self._subtitles

    # -- building ------------------------------------------------------------------------------

    def _scale(self) -> float:
        return float(ctk.ScalingTracker.get_window_scaling(self)) or 1.0

    def _px(self, units: float) -> int:
        return round(units * self._scale())

    def _place_window(
        self, saved: object, work_area: Callable[[], Rect], on_screen: Callable[[Rect], bool]
    ) -> None:
        scale = self._scale()
        if isinstance(saved, list) and len(saved) == 4 and all(isinstance(n, int) for n in saved):
            x, y, width, height = saved
            physical = (x, y, round(width * scale), round(height * scale))
            if width >= MIN_WIDTH and height >= MIN_HEIGHT and on_screen(physical):
                self.geometry(f"{width}x{height}+{x}+{y}")
                return
        x, y, width, height = first_geometry(work_area())
        self.geometry(f"{round(width / scale)}x{round(height / scale)}+{x}+{y}")

    def _place_video(self) -> None:
        margin = 0 if self._full_screen or self._mini else self._px(MARGIN)
        self.video.place(
            x=margin, y=margin, relwidth=1.0, relheight=1.0, width=-2 * margin, height=-2 * margin
        )

    def _go_borderless(self) -> None:
        """Windows' title bar off (borderless.py's way, without its close button: the ✕ is in
        the top band); the window keeps its taskbar entry, rounded corners and resizing."""
        native = borderless.current_native()
        if native is None:
            return
        self.update_idletasks()
        if native.attach(self.winfo_id(), dark=True):
            setattr(self, "_deactivate_windows_window_header_manipulation", True)
            self._native = native
            self._win32 = isinstance(native, borderless.WindowsNative)

    def _build_overlay(self) -> None:
        look = self.look
        surface = look.surface
        self._hover = _mix(surface, look.foreground, 0.14)
        self._quiet = _mix(surface, look.foreground, 0.08)
        self._track = _mix(surface, look.foreground, 0.22)
        self._progress = _mix(look.accent, look.foreground, 0.35)  # the accent, lit for the dark
        self._icon_font = ctk.CTkFont(family=_icon_family(self), size=17)
        overlay = self.overlay = _Overlay(self, bg=KEY_COLOUR, highlightthickness=0)
        overlay.withdraw()
        overlay.overrideredirect(True)  # owned by this window in `_follow` (`_own`)
        try:
            overlay.attributes("-transparentcolor", KEY_COLOUR)
            overlay.see_through(OVERLAY_ALPHA)
        except tk.TclError:
            pass  # not on Windows: the overlay is opaque
        self._build_top_band()
        self._build_bottom_band()
        self._build_mini_controls()
        self._build_floating()

    def _icon_button(
        self, master: Any, glyph: str, command: Callable[[], None], tip: str, *, size: int = 40
    ) -> tuple[ctk.CTkButton, Tooltip]:
        button = ctk.CTkButton(
            master,
            text=glyph,
            font=self._icon_font,
            width=size,
            height=36,
            corner_radius=8,
            fg_color="transparent",
            hover_color=self._hover,
            text_color=self.look.foreground,
            command=command,
        )
        return button, add_tooltip(button, tip)

    def _text_button(
        self, master: Any, text: str, command: Callable[[], None], *, primary: bool = False
    ) -> ctk.CTkButton:
        look = self.look
        return ctk.CTkButton(
            master,
            text=text,
            font=theme.font(15, bold=primary),
            width=40,
            height=40,
            corner_radius=8,
            fg_color=look.accent if primary else self._quiet,
            hover_color=look.accent_hover if primary else self._hover,
            text_color=look.foreground,
            command=command,
        )

    def _build_top_band(self) -> None:
        look = self.look
        band = self.top_band = ctk.CTkFrame(self.overlay, fg_color=look.surface, corner_radius=0)
        self.close_button = CloseButton(band, command=self.close)
        add_tooltip(self.close_button, look.text("close_tip"))
        buttons = ctk.CTkFrame(band, fg_color="transparent")
        buttons.pack(side="right", padx=(0, CLOSE_ROOM + CLOSE_GAP - 4), pady=11)
        self.full_screen_button, self._full_screen_tip = self._icon_button(
            buttons, FULL_SCREEN_GLYPH, self.toggle_full_screen, look.text("full_screen_tip")
        )
        self.full_screen_button.pack(side="right")
        self.on_top_button, _tip = self._icon_button(
            buttons, PINNED_GLYPH if self._on_top else PIN_GLYPH, self.toggle_on_top,
            look.text("on_top_tip"),
        )
        self.on_top_button.pack(side="right", padx=(0, 4))
        self.mini_button, _tip = self._icon_button(
            buttons, MINI_GLYPH, self.toggle_mini, look.text("mini_tip")
        )
        self.mini_button.pack(side="right", padx=(0, 4))
        self.title_label = ctk.CTkLabel(
            band, text=look.title, font=theme.font(17, bold=True), text_color=look.foreground
        )
        self.title_label.pack(side="left", padx=(BAND_PAD + 4, 10))
        self.line_label = ctk.CTkLabel(
            band, text="", font=theme.font(15), text_color=look.muted
        )
        self.line_label.pack(side="left", padx=(0, 10))
        for widget in (band, self.title_label, self.line_label):
            widget.bind("<ButtonPress-1>", self._on_press_band, add="+")

    def _build_bottom_band(self) -> None:
        look = self.look
        band = self.bottom_band = ctk.CTkFrame(
            self.overlay, fg_color=look.surface, corner_radius=0
        )
        self._time_tip = tk.Label(  # the time under the pointer on the seek bar
            self.overlay,
            bg=look.surface,
            fg=look.foreground,
            font=theme.font(13).create_scaled_tuple(self._scale()),
            padx=self._px(6),
            pady=self._px(2),
        )
        seek_row = ctk.CTkFrame(band, fg_color="transparent")
        seek_row.pack(fill="x", padx=BAND_PAD, pady=(10, 0))
        label: dict[str, Any] = {"font": theme.font(14), "text_color": look.muted, "width": 64}
        self.elapsed_label = ctk.CTkLabel(seek_row, text="0:00", anchor="w", **label)
        self.elapsed_label.pack(side="left")
        self.remaining_label = ctk.CTkLabel(seek_row, text="", anchor="e", **label)
        self.remaining_label.pack(side="right")
        self.seek_bar = self._seek_canvas(seek_row, height=22)
        self.seek_bar.pack(side="left", fill="x", expand=True, padx=8)

        row = ctk.CTkFrame(band, fg_color="transparent")
        row.pack(fill="x", padx=BAND_PAD - 6, pady=(2, 10))
        self.back_button, _tip = self._icon_button(
            row, BACK_GLYPH, lambda: self.jump(-JUMP_BACK_MS), look.text("back_tip")
        )
        self.back_button.pack(side="left")
        self.play_button, self._play_tip = self._icon_button(
            row, PLAY_GLYPH, self.toggle_pause, look.text("play_tip"), size=48
        )
        self.play_button.configure(font=ctk.CTkFont(family=self._icon_font.cget("family"), size=22))
        self.play_button.pack(side="left", padx=2)
        self.forward_button, _tip = self._icon_button(
            row, FORWARD_GLYPH, lambda: self.jump(JUMP_FORWARD_MS), look.text("forward_tip")
        )
        self.forward_button.pack(side="left")
        self.mute_button, self._mute_tip = self._icon_button(
            row, VOLUME_GLYPH, self.toggle_mute, look.text("mute_tip")
        )
        self.mute_button.pack(side="left", padx=(18, 0))
        self.volume_slider = ctk.CTkSlider(
            row,
            from_=0,
            to=100,
            number_of_steps=100,
            width=110,
            height=16,
            fg_color=self._track,
            progress_color=look.foreground,
            button_color=look.foreground,
            button_hover_color=look.foreground,
            command=lambda value: self.set_volume(round(float(value))),
        )
        self.volume_slider.set(self._volume)
        self.volume_slider.pack(side="left", padx=(2, 0))
        self.subtitles_button = self._menu_button(
            row, look.text("subtitles"), self.open_subtitle_menu
        )
        self.subtitles_button.pack(side="right")
        self.audio_button = self._menu_button(row, look.text("audio"), self.open_audio_menu)
        self.audio_button.pack(side="right", padx=(0, 4))

    def _build_mini_controls(self) -> None:
        """The mini player's few controls, straight on the overlay (the picture between them):
        play / pause in the middle, back to the window and ✕ top right, the time and a thin
        seek line along the bottom (`_layout_controls` places them)."""
        look = self.look
        overlay = self.overlay
        family = self._icon_font.cget("family")

        def floating(glyph: str, command: Callable[[], None], tip: str) -> ctk.CTkButton:
            button, _tip = self._icon_button(overlay, glyph, command, tip, size=36)
            button.configure(fg_color=look.surface)
            return button

        self.mini_play_button, self._mini_play_tip = self._icon_button(
            overlay, PLAY_GLYPH, self.toggle_pause, look.text("play_tip"), size=56
        )
        self.mini_play_button.configure(
            height=56,
            corner_radius=28,
            fg_color=look.surface,
            font=ctk.CTkFont(family=family, size=24),
        )
        self.mini_window_button = floating(
            BIG_GLYPH, self.toggle_mini, look.text("back_to_window_tip")
        )
        self.mini_close_button = floating(CLOSE_GLYPH, self.close, look.text("close_mini_tip"))
        strip = self.mini_strip = ctk.CTkFrame(overlay, fg_color=look.surface, corner_radius=0)
        self.mini_time_label = ctk.CTkLabel(
            strip, text="0:00", font=theme.font(13), text_color=look.muted, height=20
        )
        self.mini_time_label.pack(side="left", padx=(10, 6), pady=4)
        self.mini_seek_bar = self._seek_canvas(strip, height=18)
        self.mini_seek_bar.pack(side="left", fill="x", expand=True, padx=(0, 10))

    def _seek_canvas(self, master: Any, *, height: int) -> tk.Canvas:
        """A seek bar (the window's or the mini player's): click or drag to jump."""
        bar = tk.Canvas(
            master,
            height=self._px(height),
            bg=self.look.surface,
            highlightthickness=0,
            cursor="hand2",
        )
        bar.bind("<Configure>", lambda _event: self._draw_seek(), add="+")
        bar.bind("<ButtonPress-1>", self._seek_press)
        bar.bind("<B1-Motion>", self._seek_drag)
        bar.bind("<ButtonRelease-1>", self._seek_release)
        bar.bind("<Motion>", self._seek_hover, add="+")
        bar.bind("<Leave>", lambda _event: self._time_tip.place_forget())
        return bar

    def _menu_button(self, master: Any, text: str, command: Callable[[], None]) -> ctk.CTkButton:
        return ctk.CTkButton(
            master,
            text=f"{text} {MENU_ARROW}",
            font=theme.font(15),
            width=40,
            height=36,
            corner_radius=8,
            fg_color="transparent",
            hover_color=self._hover,
            text_color=self.look.foreground,
            command=command,
        )

    def _build_floating(self) -> None:
        look = self.look
        overlay = self.overlay
        self.skip_button = ctk.CTkButton(
            overlay,
            text=look.text("skip_intro"),
            font=theme.font(16, bold=True),
            height=44,
            corner_radius=8,
            border_width=1,
            border_color=_mix(look.surface, look.foreground, 0.4),
            fg_color=look.surface,
            hover_color=look.accent,
            text_color=look.foreground,
            command=self.skip_intro,
        )

        card = self.countdown_card = ctk.CTkFrame(overlay, fg_color=look.surface, corner_radius=12)
        self.countdown_label = ctk.CTkLabel(
            card, text="", font=theme.font(16, bold=True), text_color=look.foreground
        )
        self.countdown_label.pack(anchor="w", padx=18, pady=(14, 10))
        buttons = ctk.CTkFrame(card, fg_color="transparent")
        buttons.pack(fill="x", padx=18, pady=(0, 16))
        self.play_now_button = self._text_button(
            buttons, look.text("play_now"), self.want_next, primary=True
        )
        self.play_now_button.configure(width=110)
        self.play_now_button.pack(side="left")
        self.cancel_button = self._text_button(buttons, look.text("cancel"), self.cancel_countdown)
        self.cancel_button.configure(width=110)
        self.cancel_button.pack(side="left", padx=(8, 0))

        prompt = self.prompt_card = ctk.CTkFrame(overlay, fg_color=look.surface, corner_radius=14)
        self.resume_button = self._text_button(
            prompt, look.text("resume", time="0:00"), self.resume, primary=True
        )
        self.resume_button.configure(width=260, height=46)
        self.resume_button.pack(fill="x", padx=20, pady=(20, 10))
        self.start_over_button = self._text_button(
            prompt, look.text("start_over"), self.start_over
        )
        self.start_over_button.configure(width=260, height=46)
        self.start_over_button.pack(fill="x", padx=20, pady=(0, 20))

        self.notice_label = ctk.CTkLabel(
            overlay,
            text="",
            font=theme.font(15),
            text_color=look.foreground,
            fg_color=look.surface,
            corner_radius=8,
            height=38,
        )

    # -- the overlay following the video -------------------------------------------------------

    def _follow_later(self) -> None:
        if "follow" not in self._jobs:
            self._jobs["follow"] = self.after_idle(self._follow)

    def _follow(self) -> None:
        self._jobs.pop("follow", None)
        if self._closed or not self.winfo_exists():
            return
        try:
            if self.state() in ("withdrawn", "iconic"):
                return
            video = self.video
            width, height = max(1, video.winfo_width()), max(1, video.winfo_height())
            if self._win32:  # first: put above its owner after the move, it undid the move
                _own(self.overlay, self)
            self.overlay.geometry(f"{width}x{height}+{video.winfo_rootx()}+{video.winfo_rooty()}")
            if self.overlay.state() == "withdrawn":
                self.overlay.deiconify()
        except tk.TclError:
            pass

    def _on_map(self, event: Any) -> None:
        if event.widget is self:
            self._follow_later()

    def _on_unmap(self, event: Any) -> None:
        if event.widget is self and not self._closed:
            try:
                self.overlay.withdraw()
            except tk.TclError:
                pass

    def _on_press_frame(self, event: Any) -> None:
        """A press on the frame around the video (the window's own background) drags it."""
        if event.widget is self:
            self._drag()

    def _on_press_band(self, _event: Any) -> None:
        self._drag()

    def _drag(self) -> None:
        if self._native is None or self._full_screen:
            return
        self.overlay.withdraw()  # it would lag behind: back once the window is let go
        try:
            self._native.start_drag(self.winfo_id())
        finally:
            self._follow_later()

    def _on_configure(self, event: Any) -> None:
        self._follow_later()
        if event.widget is self and self._mini:
            job = self._jobs.pop("shape", None)
            if job is not None:
                self.after_cancel(job)
            self._schedule("shape", SHAPE_MS, self.reshape)

    def _on_double(self, _event: Any) -> None:
        """A double click on the picture: the mini player back to the window; else full screen."""
        if self._mini:
            self.toggle_mini()
        else:
            self.toggle_full_screen()

    def _on_video_press(self, event: Any) -> None:
        self._press = (int(event.x_root), int(event.y_root)) if self._mini else None

    def _on_video_drag(self, event: Any) -> None:
        """The mini player's picture pressed and moved: the window follows (Windows' own move),
        then tucks into a corner it was let go near (`tuck`). A press alone stays a click."""
        if self._press is None or not self._mini:
            return
        x, y = self._press
        if max(abs(int(event.x_root) - x), abs(int(event.y_root) - y)) < DRAG_START_PX:
            return
        self._press = None
        self._drag()
        self.tuck()

    def tuck(self) -> None:
        """The mini player let go near a corner of the work area: into that corner."""
        if not self._mini or self._closed:
            return
        try:
            self.update_idletasks()
            place = _place_of(self.geometry())
            video = self.video
            shown = (
                video.winfo_rootx(),
                video.winfo_rooty(),
                video.winfo_width(),
                video.winfo_height(),
            )
        except tk.TclError:
            return
        if place is None:
            return
        target = tucked(shown, self._work_area(), self._scale())
        if target != shown:
            x, y = place[0] + target[0] - shown[0], place[1] + target[1] - shown[1]
            self.geometry(f"+{x}+{y}")

    def reshape(self) -> None:
        """The mini player resized by its edges: back to 16:9 (`mini_shape`), once the mouse
        button is let go."""
        self._jobs.pop("shape", None)
        if not self._mini or self._closed:
            return
        if self._button_down():
            self._schedule("shape", SHAPE_MS, self.reshape)
            return
        try:
            size = (self.winfo_width(), self.winfo_height())
        except tk.TclError:
            return
        scale = self._scale()
        least = (round(MINI_LEAST[0] * scale), round(MINI_LEAST[1] * scale))
        width, height = mini_shape(size, self._mini_size or size, least)
        if abs(width - size[0]) > 1 or abs(height - size[1]) > 1:
            self.geometry(f"{round(width / scale)}x{round(height / scale)}")
        self._mini_size = (width, height)

    # -- the queue -----------------------------------------------------------------------------

    def _schedule(self, name: str, delay_ms: int, action: Callable[[], None]) -> None:
        if not self._closed:
            self._jobs[name] = self.after(delay_ms, action)

    def _drain(self) -> None:
        self._jobs.pop("drain", None)
        while not self._closed:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            self.handle(event)
        self._schedule("drain", DRAIN_MS, self._drain)

    def handle(self, event: Event) -> None:
        """One piece of news from the other threads."""
        if self._closed:
            return
        if isinstance(event, Incoming):
            self._on_message(event.message)
        elif isinstance(event, InputEnded):
            self.close()
        elif isinstance(event, Tick):
            self._on_tick(event)
        elif isinstance(event, TrackLists):
            self._on_tracks(event)
        elif isinstance(event, VideoEnded):
            self._on_ended(event.generation)

    def _say(self, message: Message) -> None:
        self._send(encode(message))

    def _on_message(self, message: Message) -> None:
        if isinstance(message, Play):
            self.play(message)
        elif isinstance(message, LoadSubtitle):
            self._load_subtitle(message.path)
        elif isinstance(message, Notice):
            self.show_notice(message.text)

    # -- a video -------------------------------------------------------------------------------

    def play(self, message: Play) -> None:
        """Show `message`'s video (the one before reported Stopped first)."""
        self._report_stopped()
        self._play = message
        self._generation += 1
        self._position = message.start_ms  # also what a Stopped says before it starts
        self._duration = 0
        self._started = False
        self._paused = True
        self._ended = False
        self._pending = 0
        self._reported_at = None
        self._audio = self._subtitles = ()
        self._audio_id = self._subtitle_id = None
        self._picked_audio = self._picked_subtitles = None
        self._always = {"audio": message.always_audio, "subtitles": message.always_subtitles}
        self._applied = set()
        self._new_subtitle = False
        self._countdown_at = None
        self._countdown_cancelled = False
        self._want_sent = False
        self._sound_set = False
        self._zero_wait = False
        self._cut = None
        self.title(message.title or self.look.title)
        self.title_label.configure(text=message.title)
        self.line_label.configure(text=message.line)
        path, generation = message.file, self._generation
        self.worker.submit(lambda engine: engine.load(path))
        self.worker.watch(generation)
        self._show_skip(False)
        self._show_countdown(False)
        if message.ask_resume and message.start_ms > 0:
            self.resume_button.configure(text=self.look.text("resume", time=clock(message.start_ms)))
            self._show_prompt(True)
        else:
            self._show_prompt(False)
            self._begin(message.start_ms, seek=message.start_ms > 0)
        self._update_play_button()
        self._draw_seek()
        self._update_times()
        self.activity()
        try:
            self.focus_force()
        except tk.TclError:
            pass

    def resume(self) -> None:
        if self._play is not None and self._prompt:
            self._begin(self._play.start_ms, seek=True)

    def start_over(self) -> None:
        if self._play is not None and self._prompt:
            self._begin(0, seek=True)

    def _begin(self, start_ms: int, *, seek: bool) -> None:
        self._show_prompt(False)
        if seek:
            self.worker.submit(lambda engine: engine.seek(start_ms))
        self._pending = self.worker.submit(lambda engine: engine.play())
        self._started = True
        self._paused = False
        self._position = start_ms
        self._zero_wait = start_ms > 0
        self._update_play_button()
        self._apply_tracks()
        self.activity()

    def _report_stopped(self) -> None:
        if self._play is not None and not self._ended:
            self._say(Stopped(ms=self._position, duration_ms=self._duration))

    def _report(self) -> None:
        state: Literal["playing", "paused"] = "paused" if self._paused else "playing"
        self._say(Position(ms=self._position, duration_ms=self._duration, state=state))
        self._reported_at = self._now()

    def _on_tick(self, tick: Tick) -> None:
        if tick.generation != self._generation or tick.done < self._pending or self._ended:
            return
        if tick.duration_ms > 0:
            self._duration = tick.duration_ms
        if not self._started:
            # Loaded, not playing yet (the resume prompt): libVLC has no position, and the
            # one to keep (a Stopped now) is where the video would start.
            self._update_times()
            self._draw_seek()
            return
        if self._cut is not None:
            return  # stopped early: libVLC's ended player says nothing that counts
        if self._zero_wait:
            if tick.ms == 0:
                return  # libVLC hasn't opened it or applied the start time yet
            self._zero_wait = False
        self._position = tick.ms
        if self._started and not self._sound_set and tick.ms > 0:
            # libVLC has no sound output before a video plays: the volume and mute again now.
            self._sound_set = True
            volume, muted = self._volume, self._muted
            self.worker.submit(lambda engine: engine.volume(volume))
            self.worker.submit(lambda engine: engine.mute(muted))
        if self._started and not self._paused and self._duration > 0:
            if self._reported_at is None or self._now() - self._reported_at >= REPORT_S:
                self._report()
        self._draw_seek()
        self._update_times()
        self._update_markers()

    def _on_ended(self, generation: int) -> None:
        if (
            generation != self._generation
            or self._ended
            or self._play is None
            or self._cut is not None
        ):
            return
        if not played_to_the_end(self._position, self._duration, self._play.credits_ms):
            self._stopped_early()
            return
        countdown_running = self._countdown and not self._countdown_cancelled
        if self._duration > 0:
            self._position = self._duration
        self._ended = True
        self._paused = True
        self._say(Ended(duration_ms=self._duration))
        if countdown_running:
            self.want_next()
        self._show_skip(False)
        self._show_countdown(False)
        self._update_play_button()
        self._draw_seek()
        self._update_times()
        self.activity()

    def _stopped_early(self) -> None:
        """libVLC's end event far from the end: a read failed part-way (the share or Wi-Fi gone,
        a cut-off file). No Ended (it would be marked watched): a pause at the last real
        position, the window as it was."""
        self._cut = self._position
        self._paused = True
        self._countdown_at = None
        self._show_skip(False)
        self._show_countdown(False)
        self._report()
        self._update_play_button()
        self._draw_seek()
        self._update_times()
        self.activity()

    def _reopen(self) -> None:
        """Play after a video stopped early: the file opened again where it stopped, its
        tracks picked again (a language picked by hand in it first) once its fresh track lists
        come: the old ones would be picked from before libVLC has opened it, and lost."""
        cut, self._cut = self._cut, None
        if self._play is None or cut is None:
            return
        path = self._play.file
        self.worker.submit(lambda engine: engine.load(path))
        self._audio = self._subtitles = ()
        self.worker.watch(self._generation)  # the lists reported again, even when unchanged
        self._applied = set()
        self._sound_set = False
        self._begin(cut, seek=cut > 0)

    # -- playing -------------------------------------------------------------------------------

    def toggle_pause(self) -> None:
        if self._prompt:
            self.resume()
            return
        if not self._started:
            return
        if self._paused and self._cut is not None:
            self._reopen()
            self._report()
            return
        if self._paused:
            self._pending = self.worker.submit(lambda engine: engine.play())
            if self._ended:  # played to the end: from the start again
                self._ended = False
                self._position = 0
                self._countdown_at = None
                self._want_sent = False
            self._paused = False
        else:
            self.worker.submit(lambda engine: engine.pause())
            self._paused = True
        self._report()
        self._update_play_button()
        self.activity()

    def jump(self, ms: int) -> None:
        self._seek(self._position + ms)

    def seek_to_fraction(self, fraction: float) -> None:
        if self._duration > 0:
            self._seek(round(max(0.0, min(1.0, fraction)) * self._duration))

    def skip_intro(self) -> None:
        if self._play is not None and self._play.intro is not None:
            self._seek(self._play.intro[1])

    def _seek(self, ms: int) -> None:
        if not self._started or self._ended:
            return
        if self._duration > 0:
            ms = min(ms, max(0, self._duration - END_ROOM_MS))
        target = max(0, ms)
        if self._cut is not None:
            self._cut = target  # stopped early: where Play opens it again
        else:
            self._pending = self.worker.submit(lambda engine: engine.seek(target))
        if target == 0:
            self._zero_wait = False  # the start asked for: 0 is real now
        self._position = target
        if target < (self._countdown_at or 0):
            self._countdown_at = None
        self._report()
        self._draw_seek()
        self._update_times()
        self._update_markers()
        self.activity()

    def set_volume(self, level: int) -> None:
        self._volume = max(0, min(100, level))
        volume = self._volume
        self.worker.submit(lambda engine: engine.volume(volume))
        self.volume_slider.set(volume)
        self.activity()

    def toggle_mute(self) -> None:
        self._muted = not self._muted
        muted = self._muted
        self.worker.submit(lambda engine: engine.mute(muted))
        self.mute_button.configure(text=MUTED_GLYPH if muted else VOLUME_GLYPH)
        self._mute_tip.set_text(self.look.text("unmute_tip" if muted else "mute_tip"))
        self.activity()

    def _update_play_button(self) -> None:
        playing = self._started and not self._paused
        tip = self.look.text("pause_tip" if playing else "play_tip")
        for button, button_tip in (
            (self.play_button, self._play_tip),
            (self.mini_play_button, self._mini_play_tip),
        ):
            button.configure(text=PAUSE_GLYPH if playing else PLAY_GLYPH)
            button_tip.set_text(tip)

    # -- the markers: Skip intro and the countdown ----------------------------------------------

    def _update_markers(self) -> None:
        message = self._play
        if message is None or not self._started or self._ended or self._cut is not None:
            self._show_skip(False)
            self._show_countdown(False)
            return
        self._show_skip(intro_button(self._position, message.intro))
        if (
            message.next_label is None
            or self._duration <= 0
            or self._countdown_cancelled
            or self._want_sent
        ):
            self._show_countdown(False)
            return
        start = countdown_from(self._duration, message.credits_ms)
        if self._position < start:
            self._countdown_at = None
            self._show_countdown(False)
            return
        if self._countdown_at is None:
            self._countdown_at = self._position
        left = COUNTDOWN_S - (self._position - self._countdown_at) // 1000
        if left <= 0:
            self.want_next()
            return
        self.countdown_label.configure(
            text=self.look.text("next_episode", label=message.next_label, n=left)
        )
        self._show_countdown(True)

    def want_next(self) -> None:
        """The countdown reached 0, or Play now: the app sends the next episode."""
        if self._want_sent or self._play is None:
            return
        self._want_sent = True
        self._say(WantNext())
        self._show_countdown(False)

    def cancel_countdown(self) -> None:
        self._countdown_cancelled = True
        self._show_countdown(False)

    # -- tracks ----------------------------------------------------------------------------------

    def _on_tracks(self, lists: TrackLists) -> None:
        if lists.generation != self._generation:
            return
        before = {track.id for track in self._subtitles}
        self._audio, self._subtitles = lists.audio, lists.subtitles
        if self._new_subtitle:
            added = [track.id for track in self._subtitles if track.id not in before]
            if added:
                self._subtitle_id = added[-1]
                self._new_subtitle = False
        self._apply_tracks()

    def _apply_tracks(self) -> None:
        """At a video's start, once its tracks are known: the carried language (one picked by hand
        in this video first, when it is opened again), else Always pick."""
        message = self._play
        if message is None or not self._started:
            return
        if "audio" not in self._applied and self._audio:
            self._applied.add("audio")
            carried = self._picked_audio if self._picked_audio is not None else message.carry_audio
            found = pick_track(self._audio, message.always_audio, carried)
            if found is not None:
                self._set_audio(found)
        if "subtitles" not in self._applied and self._subtitles:
            self._applied.add("subtitles")
            found = pick_track(
                self._subtitles,
                message.always_subtitles,
                (
                    self._picked_subtitles
                    if self._picked_subtitles is not None
                    else message.carry_subtitles
                ),
                subtitles=True,
            )
            if found is not None:
                self._set_subtitle(found)

    def _set_audio(self, track_id: int) -> None:
        self._audio_id = track_id
        self.worker.submit(lambda engine: engine.set_audio(track_id))

    def _set_subtitle(self, track_id: int) -> None:
        self._subtitle_id = track_id
        self.worker.submit(lambda engine: engine.set_subtitle(track_id))

    def choose_audio(self, track_id: int) -> None:
        """An audio track picked by hand: for this video, and carried to the next episode."""
        self._applied.add("audio")
        self._set_audio(track_id)
        track = next((t for t in self._audio if t.id == track_id), None)
        self._picked_audio = self._language_of(track) if track is not None else ""
        if track is not None:
            self.show_notice(f"{self.look.text('audio')}: {self._track_label(track)}")
        self._say_tracks()

    def choose_subtitle(self, track_id: int) -> None:
        """A subtitle track (or off) picked by hand: as `choose_audio`."""
        self._applied.add("subtitles")
        self._new_subtitle = False
        self._set_subtitle(track_id)
        if track_id == SUBTITLES_OFF:
            self._picked_subtitles = OFF
            label = self.look.text("subtitles_off")
        else:
            track = next((t for t in self._subtitles if t.id == track_id), None)
            self._picked_subtitles = self._language_of(track) if track is not None else ""
            label = self._track_label(track) if track is not None else ""
        self.show_notice(f"{self.look.text('subtitles')}: {label}")
        self._say_tracks()

    def _say_tracks(self) -> None:
        message = self._play
        carried_audio = message.carry_audio if message is not None else ""
        carried_subtitles = message.carry_subtitles if message is not None else ""
        audio = self._picked_audio if self._picked_audio is not None else carried_audio
        subtitles = (
            self._picked_subtitles if self._picked_subtitles is not None else carried_subtitles
        )
        self._say(Tracks(audio=audio, subtitles=subtitles))

    def next_subtitle(self) -> None:
        if not self._started:
            return
        options = [SUBTITLES_OFF, *(track.id for track in self._subtitles)]
        current = self._subtitle_id if self._subtitle_id is not None else SUBTITLES_OFF
        index = options.index(current) if current in options else 0
        self.choose_subtitle(options[(index + 1) % len(options)])

    def next_audio(self) -> None:
        if not self._started or not self._audio:
            return
        options = [track.id for track in self._audio]
        current = self._audio_id if self._audio_id is not None else options[0]
        index = options.index(current) if current in options else 0
        self.choose_audio(options[(index + 1) % len(options)])

    def always_pick(self, kind: TrackKind, language: str) -> None:
        """Always pick `language` for `kind` (the app stores it); switched to now when the video
        has it. No hand pick: nothing to carry."""
        self._always[kind] = language
        self._say(AlwaysPick(kind=kind, language=language))
        if not language or not self._started:
            return
        if kind == "audio":
            found = pick_track(self._audio, language, "")
            if found is not None:
                self._set_audio(found)
        else:
            found = pick_track(self._subtitles, language, "", subtitles=True)
            if found is not None:
                self._set_subtitle(found)

    def find_subtitles(self, language: str) -> None:
        """Ask the app for subtitles in `language`; it answers LoadSubtitle or a Notice."""
        self._say(FindSubtitles(language=language))
        self._finding = language
        self.show_notice(self.look.text("looking"), seconds=LOOKING_S)

    def _load_subtitle(self, path: str) -> None:
        self.hide_notice()
        if self._play is None:
            return
        self.worker.submit(lambda engine: engine.add_subtitle(path))
        self._applied.add("subtitles")
        self._new_subtitle = True
        self._picked_subtitles = self._finding
        self._say_tracks()

    def _language_of(self, track: Track) -> str:
        key = language_key(track.language)
        if key:
            return key
        for code, _name in LANGUAGES:
            if pick_track([track], code, "") == track.id:
                return code
        return ""

    def _track_label(self, track: Track) -> str:
        if track.name.strip():
            return track.name.strip()
        number = next((i for i, t in enumerate(self._all_tracks(track), 1) if t == track), 1)
        return self.look.text("track", number=number)

    def _all_tracks(self, track: Track) -> tuple[Track, ...]:
        return self._audio if track in self._audio else self._subtitles

    def _language_name(self, language: str, kind: TrackKind) -> str:
        if not language:
            return self.look.text("video_default")
        if language == OFF and kind == "subtitles":
            return self.look.text("subtitles_off")
        names = dict(LANGUAGES)
        key = language_key(language)
        return self.look.text(names[key]) if key in names else language.upper()

    # -- the menus -----------------------------------------------------------------------------

    def audio_menu_entries(self) -> list[MenuEntry]:
        entries = [
            MenuEntry(
                self._track_label(track),
                functools.partial(self.choose_audio, track.id),
                checked=track.id == self._audio_id,
            )
            for track in self._audio
        ]
        if entries:
            entries.append(MenuEntry("", None, separator=True))
        entries.append(self._always_entry("audio"))
        return entries

    def subtitle_menu_entries(self) -> list[MenuEntry]:
        current = self._subtitle_id
        entries = [
            MenuEntry(
                self.look.text("subtitles_off"),
                lambda: self.choose_subtitle(SUBTITLES_OFF),
                checked=current == SUBTITLES_OFF,
            )
        ]
        entries += [
            MenuEntry(
                self._track_label(track),
                functools.partial(self.choose_subtitle, track.id),
                checked=track.id == current,
            )
            for track in self._subtitles
        ]
        entries.append(MenuEntry("", None, separator=True))
        if self._play is None or self._play.find_subtitles:  # not for a file from Explorer
            entries.append(MenuEntry(self.look.text("find_subtitles"), self._open_find_menu))
        entries.append(self._always_entry("subtitles"))
        return entries

    def _always_entry(self, kind: TrackKind) -> MenuEntry:
        name = self._language_name(self._always[kind], kind)
        return MenuEntry(
            self.look.text("always_pick", language=name), lambda: self._open_always_menu(kind)
        )

    def audio_menu_labels(self) -> list[str]:
        return [entry.label for entry in self.audio_menu_entries() if not entry.separator]

    def subtitle_menu_labels(self) -> list[str]:
        return [entry.label for entry in self.subtitle_menu_entries() if not entry.separator]

    def _languages(self, kind: TrackKind) -> list[str]:
        """Always pick's and Find subtitles' languages: LANGUAGES, then the video's others."""
        codes = [code for code, _name in LANGUAGES]
        tracks = self._audio if kind == "audio" else self._subtitles
        for track in tracks:
            key = self._language_of(track)
            if key and key not in codes:
                codes.append(key)
        return codes

    def _open_always_menu(self, kind: TrackKind) -> None:
        current = self._always[kind]
        choices = ["", *([OFF] if kind == "subtitles" else []), *self._languages(kind)]
        entries = [
            MenuEntry(
                self._language_name(code, kind),
                functools.partial(self.always_pick, kind, code),
                checked=code == current,
            )
            for code in choices
        ]
        anchor = self.audio_button if kind == "audio" else self.subtitles_button
        self._open_menu(anchor, entries)

    def _open_find_menu(self) -> None:
        entries = [
            MenuEntry(
                self._language_name(code, "subtitles"), functools.partial(self.find_subtitles, code)
            )
            for code in self._languages("subtitles")
        ]
        self._open_menu(self.subtitles_button, entries)

    def open_audio_menu(self) -> None:
        self._open_menu(self.audio_button, self.audio_menu_entries())

    def open_subtitle_menu(self) -> None:
        self._open_menu(self.subtitles_button, self.subtitle_menu_entries())

    def _open_menu(self, anchor: tk.Misc, entries: list[MenuEntry]) -> None:
        self.activity()
        self._menu = PopupMenu(anchor, entries)

    def _menu_open(self) -> bool:
        return self._menu is not None and self._menu.is_open

    # -- the window ------------------------------------------------------------------------------

    def toggle_full_screen(self) -> None:
        self._close_menu()
        if self._mini and not self._full_screen:
            self.toggle_mini()  # full screen from the mini player: back to the window after it
        if not self._full_screen:
            self._window_geometry = self.geometry()
        self._full_screen = not self._full_screen
        if self._win32:
            _own(self.overlay, None)  # Tk makes a new wrapper: Windows would take the overlay along
        try:
            self.attributes("-fullscreen", self._full_screen)
        except tk.TclError:
            pass
        if self._win32:
            _round_corners(self, not self._full_screen)
        self._place_video()
        full = self._full_screen
        self.full_screen_button.configure(text=WINDOW_GLYPH if full else FULL_SCREEN_GLYPH)
        self._full_screen_tip.set_text(self.look.text("window_tip" if full else "full_screen_tip"))
        if full:
            self.on_top_button.pack_forget()
        else:
            self.on_top_button.pack(side="right", padx=(0, 4), before=self.mini_button)
        self._follow_later()
        self.activity()

    def toggle_on_top(self) -> None:
        self._on_top = not self._on_top
        if not self._mini:  # the mini player is always on top
            self._topmost(self._on_top)
        self.on_top_button.configure(text=PINNED_GLYPH if self._on_top else PIN_GLYPH)
        self.activity()

    def _topmost(self, on: bool) -> None:
        try:
            self.attributes("-topmost", on)
            self.overlay.attributes("-topmost", on)
        except tk.TclError:
            pass

    def toggle_mini(self) -> None:
        """To the mini player (from full screen too) and back to the window: the same window,
        so the video plays on. The mini player is always on top and starts where it was last
        left (`_mini_geometry`); the window comes back where it was."""
        self._close_menu()
        if self._full_screen:
            self.toggle_full_screen()
        if not self._mini:
            self._normal_geometry = self.geometry()
            self._mini = True
            self.minsize(*MINI_LEAST)
            self.geometry(self._mini_geometry())
        else:
            self._mini_place = _place_of(self.geometry())
            self._mini = False
            self.minsize(MIN_WIDTH, MIN_HEIGHT)
            if self._normal_geometry:
                self.geometry(self._normal_geometry)
        try:
            # The new place applied first: Tk's -topmost puts back a place still pending.
            self.update_idletasks()
        except tk.TclError:
            pass
        self._topmost(self._mini or self._on_top)
        if self._mini:
            self._mini_size = (self.winfo_width(), self.winfo_height())
        self._place_video()
        self._layout_controls()
        self._follow_later()
        self.activity()

    def _mini_geometry(self) -> str:
        """Where the mini player was last left, while that is on a screen; else in the bottom
        right corner of the work area (`first_mini`)."""
        scale = self._scale()
        if self._mini_place is not None:
            x, y, width, height = self._mini_place
            if self._on_screen((x, y, round(width * scale), round(height * scale))):
                return f"{width}x{height}+{x}+{y}"
        x, y, width, height = first_mini(self._work_area(), scale)
        return f"{round(width / scale)}x{round(height / scale)}+{x}+{y}"

    def press(self, keysym: str) -> None:
        """A key, as in VLC (`KEYS`)."""
        action = KEYS.get(keysym)
        if action is None:
            return
        self.activity()
        if action == "pause":
            self.toggle_pause()
        elif action == "fullscreen":
            self.toggle_full_screen()
        elif action == "escape":
            if self._full_screen:
                self.toggle_full_screen()
            elif self._mini:
                self.toggle_mini()
            else:
                self.close()
        elif action == "back":
            self.jump(-JUMP_BACK_MS)
        elif action == "forward":
            self.jump(JUMP_FORWARD_MS)
        elif action == "louder":
            self.set_volume(self._volume + VOLUME_STEP)
        elif action == "softer":
            self.set_volume(self._volume - VOLUME_STEP)
        elif action == "mute":
            self.toggle_mute()
        elif action == "subtitle_track":
            self.next_subtitle()
        elif action == "audio_track":
            self.next_audio()

    def _on_key(self, event: Any) -> str | None:
        state = getattr(event, "state", 0)
        if is_mini_key(str(event.keysym), state if isinstance(state, int) else 0):
            self.activity()
            self.toggle_mini()
            return "break"
        if event.keysym in KEYS:
            self.press(event.keysym)
            return "break"
        return None

    def _on_wheel(self, event: Any) -> str:
        """The mouse wheel over the player: the volume up or down, a step a notch."""
        delta = int(getattr(event, "delta", 0))
        if delta:
            self.set_volume(self._volume + (VOLUME_STEP if delta > 0 else -VOLUME_STEP))
        return "break"

    def _on_motion(self, event: Any) -> None:
        where = (int(event.x_root), int(event.y_root))
        if where != self._last_pointer:
            self._last_pointer = where
            self.activity()

    # -- the controls showing and fading -------------------------------------------------------

    def activity(self) -> None:
        """The pointer moved or a key was pressed: the controls show (again)."""
        self._last_activity = self._now()
        if not self._controls_visible and not self._closed:
            self._show_controls(True)

    def refresh_controls(self) -> None:
        """Show or hide the controls (IDLE_S without activity), and end a notice's time."""
        if self._closed:
            return
        idle = self._now() - self._last_activity
        paused = self._paused or not self._started or self._prompt
        on_them = self._pointer_on_controls() or self._menu_open()
        self._show_controls(controls_shown(idle, paused, on_them))
        if self._notice and self._now() >= self._notice_until:
            self.hide_notice()

    def _refresh_loop(self) -> None:
        self._jobs.pop("refresh", None)
        self.refresh_controls()
        self._schedule("refresh", REFRESH_MS, self._refresh_loop)

    def _pointer_on_controls(self) -> bool:
        if not self._controls_visible:
            return False
        try:
            x, y = self._pointer()
        except tk.TclError:
            return False
        for band in self._current_controls():
            try:
                left, top = band.winfo_rootx(), band.winfo_rooty()
                width, height = band.winfo_width(), band.winfo_height()
            except tk.TclError:
                continue
            if left <= x < left + width and top <= y < top + height:
                return True
        return False

    def _current_controls(self) -> tuple[tk.Misc, ...]:
        """The controls of the way it is watched: the two bands, or the mini player's."""
        if self._mini:
            return (
                self.mini_play_button,
                self.mini_window_button,
                self.mini_close_button,
                self.mini_strip,
            )
        return (self.top_band, self.bottom_band)

    def _show_controls(self, show: bool) -> None:
        if show == self._controls_visible:
            return
        self._controls_visible = show
        self.video.configure(cursor="" if show else "none")
        self._layout_controls()

    def _layout_controls(self) -> None:
        """The current controls placed while they show (the others never), then the floating
        ones clear of them."""
        for widget in (
            self.top_band,
            self.bottom_band,
            self.mini_play_button,
            self.mini_window_button,
            self.mini_close_button,
            self.mini_strip,
        ):
            widget.place_forget()
        self._time_tip.place_forget()
        if self._controls_visible and self._mini:
            self.mini_play_button.place(relx=0.5, rely=0.5, anchor="center")
            self.mini_close_button.place(relx=1.0, x=-MINI_INSET, y=MINI_INSET, anchor="ne")
            self.mini_window_button.place(
                relx=1.0, x=-(MINI_INSET + 36 + 6), y=MINI_INSET, anchor="ne"
            )
            self.mini_strip.place(relx=0, rely=1.0, relwidth=1.0, anchor="sw")
        elif self._controls_visible:
            self.top_band.place(relx=0, rely=0, relwidth=1.0, anchor="nw")
            self.bottom_band.place(relx=0, rely=1.0, relwidth=1.0, anchor="sw")
        self._place_floating()

    def _place_floating(self) -> None:
        """Skip intro, the countdown and the notice: clear of the bands (or the mini player's
        controls) when they show; nearer the edges in the mini player."""
        scale = self._scale()
        inset = MINI_INSET if self._mini else FLOAT_INSET
        bottom = inset
        top = MINI_INSET if self._mini else 16
        if self._controls_visible:
            self.update_idletasks()
            lower = self.mini_strip if self._mini else self.bottom_band
            upper = self.mini_close_button if self._mini else self.top_band
            bottom += round(lower.winfo_reqheight() / scale)
            top += round(upper.winfo_reqheight() / scale) + (MINI_INSET if self._mini else 0)
        if self._skip:
            self.skip_button.place(relx=1.0, rely=1.0, x=-inset, y=-bottom, anchor="se")
        if self._countdown:
            self.countdown_card.place(relx=1.0, rely=1.0, x=-inset, y=-bottom, anchor="se")
        if self._notice:
            self.notice_label.place(relx=0.5, y=top, anchor="n")

    def _show_skip(self, show: bool) -> None:
        if show == self._skip:
            return
        self._skip = show
        if not show:
            self.skip_button.place_forget()
        self._place_floating()

    def _show_countdown(self, show: bool) -> None:
        if show == self._countdown:
            return
        self._countdown = show
        if not show:
            self.countdown_card.place_forget()
        self._place_floating()

    def _show_prompt(self, show: bool) -> None:
        self._prompt = show
        if show:
            self.prompt_card.place(relx=0.5, rely=0.5, anchor="center")
            self.prompt_card.lift()
        else:
            self.prompt_card.place_forget()

    def show_notice(self, text: str, *, seconds: float = NOTICE_S) -> None:
        self._notice = text
        self._notice_until = self._now() + seconds
        self.notice_label.configure(text=f"  {text}  ")
        self._place_floating()

    def hide_notice(self) -> None:
        self._notice = ""
        self.notice_label.place_forget()

    # -- the seek bar --------------------------------------------------------------------------

    def _update_times(self) -> None:
        self.elapsed_label.configure(text=clock(self._position))
        remaining = MINUS + clock(self._duration - self._position) if self._duration > 0 else ""
        self.remaining_label.configure(text=remaining)
        both = clock(self._position)
        if self._duration > 0:
            both += f" / {clock(self._duration)}"
        self.mini_time_label.configure(text=both)

    def _draw_seek(self) -> None:
        for bar in (self.seek_bar, self.mini_seek_bar):
            self._draw_bar(bar)

    def _draw_bar(self, bar: tk.Canvas) -> None:
        try:
            width, height = bar.winfo_width(), bar.winfo_height()
        except tk.TclError:
            return
        bar.delete("all")
        radius = self._px(6)
        thick = self._px(4)
        left, right = radius, max(radius, width - radius)
        middle = height / 2
        if self._dragging_seek is not None:
            fraction = self._dragging_seek
        elif self._duration > 0:
            fraction = max(0.0, min(1.0, self._position / self._duration))
        else:
            fraction = 0.0
        x = left + (right - left) * fraction
        bar.create_line(left, middle, right, middle, fill=self._track, width=thick, capstyle="round")
        if fraction > 0:
            bar.create_line(
                left, middle, x, middle, fill=self._progress, width=thick, capstyle="round"
            )
        bar.create_oval(
            x - radius, middle - radius, x + radius, middle + radius,
            fill=self.look.foreground, outline="",
        )

    def _fraction_at(self, bar: tk.Misc, x: int) -> float:
        radius = self._px(6)
        width = max(1, bar.winfo_width() - 2 * radius)
        return max(0.0, min(1.0, (x - radius) / width))

    def _seek_press(self, event: Any) -> None:
        if self._duration > 0 and self._started:
            self._dragging_seek = self._fraction_at(event.widget, int(event.x))
            self._draw_seek()

    def _seek_drag(self, event: Any) -> None:
        if self._dragging_seek is not None:
            self._dragging_seek = self._fraction_at(event.widget, int(event.x))
            self._draw_seek()
            self._seek_hover(event)

    def _seek_release(self, event: Any) -> None:
        if self._dragging_seek is None:
            return
        fraction = self._fraction_at(event.widget, int(event.x))
        self._dragging_seek = None
        self.seek_to_fraction(fraction)

    def _seek_hover(self, event: Any) -> None:
        if self._duration <= 0:
            return
        bar = event.widget
        ms = round(self._fraction_at(bar, int(event.x)) * self._duration)
        tip = self._time_tip
        tip.configure(text=clock(ms))
        x = bar.winfo_rootx() - self.overlay.winfo_rootx() + int(event.x)
        y = bar.winfo_rooty() - self.overlay.winfo_rooty() - self._px(6)
        tip.place(x=x, y=y, anchor="s")
        tip.lift()

    # -- closing -------------------------------------------------------------------------------

    def _close_menu(self) -> None:
        if self._menu is not None:
            self._menu.close()
            self._menu = None

    def close(self) -> None:
        """✕, Escape in a window, or stdin's end: Stopped for the video, the settings saved,
        the window gone. Never waits for the engine."""
        if self._closed:
            return
        self._report_stopped()
        self._save()
        self._closed = True
        self._close_menu()
        for job in self._jobs.values():
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
        self._jobs.clear()
        self.worker.stop()
        self._cancel_own_jobs()
        try:
            self.overlay.destroy()
        except tk.TclError:
            pass
        try:
            self.destroy()
        except tk.TclError:
            pass

    def _cancel_own_jobs(self) -> None:
        """customtkinter's own `after` jobs on this window (its icon, 200 ms after it opened):
        they would run for a window that's gone."""
        own = set(getattr(self, "_tclCommands", None) or ())
        try:
            pending = self.tk.splitlist(self.tk.call("after", "info"))
        except tk.TclError:
            return
        for job in pending:
            try:
                script = self.tk.splitlist(self.tk.call("after", "info", job))[0]
            except (tk.TclError, IndexError):
                continue
            if str(script) in own:
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass

    def _on_destroy(self, event: Any) -> None:
        if event.widget is self and not self._closed:
            self._closed = True
            for job in self._jobs.values():
                try:
                    self.after_cancel(job)
                except tk.TclError:
                    pass
            self._jobs.clear()
            self.worker.stop()

    def _on_overlay_destroy(self, event: Any) -> None:
        """The controls' window gone without `close` (closed on its own): the player closes
        too, once Tk is done destroying (when the whole window is going, `_on_destroy` comes
        first and this finds it closed)."""
        if event.widget is self.overlay and not self._closed and "overlay" not in self._jobs:
            self._jobs["overlay"] = self.after_idle(self._overlay_gone)

    def _overlay_gone(self) -> None:
        self._jobs.pop("overlay", None)
        self.close()

    def _save(self) -> None:
        try:
            current = self.geometry()
        except tk.TclError:
            return  # the window is already gone: nothing to measure
        if self._full_screen:
            geometry = self._window_geometry
        elif self._mini:
            geometry = self._normal_geometry
            self._mini_place = _place_of(current)
        else:
            geometry = current
        data: dict[str, Any] = {"on_top": self._on_top, "volume": self._volume}
        place = _place_of(geometry)
        if place is not None:
            data["geometry"] = place
        if self._mini_place is not None:
            data["mini"] = self._mini_place
        save_settings(self._settings_path, data)


def start_player(
    master: tk.Misc,
    engine: EngineSeam,
    look: Look,
    *,
    stdin: Any,
    send: Callable[[str], None],
    settings_path: Path,
    work_area: Callable[[], Rect] = work_area_under_pointer,
    on_screen: Callable[[Rect], bool] = lambda _rect: True,
    pointer: Callable[[], tuple[int, int]] | None = None,
    now: Callable[[], float] = time.monotonic,
    button_down: Callable[[], bool] = _left_button_down,
) -> PlayerWindow:
    """The window, its engine thread and its stdin reader running; Ready said."""
    window = PlayerWindow(
        master,
        engine,
        look,
        send=send,
        settings_path=settings_path,
        work_area=work_area,
        on_screen=on_screen,
        pointer=pointer,
        now=now,
        button_down=button_down,
    )
    worker, events = window.worker, window.events
    engine.on_end(lambda: events.put(VideoEnded(worker.generation)))
    video_id, volume = window.video.winfo_id(), window.volume
    worker.submit(lambda engine: engine.attach(video_id))
    worker.submit(lambda engine: engine.volume(volume))
    worker.start()
    send(encode(Ready()))
    threading.Thread(
        target=read_input, args=(stdin, events), name="media-stdin", daemon=True
    ).start()
    return window


def run_player(
    master: tk.Misc,
    engine: EngineSeam,
    look: Look,
    *,
    stdin: Any,
    send: Callable[[str], None],
    settings_path: Path,
    work_area: Callable[[], Rect] = work_area_under_pointer,
    on_screen: Callable[[Rect], bool] = lambda _rect: True,
    pointer: Callable[[], tuple[int, int]] | None = None,
) -> int:
    """The player until its window closes; the exit code. libVLC gets RELEASE_WAIT_S to let
    go; one stuck on a stalled share is left behind (the process ends anyway)."""
    window = start_player(
        master,
        engine,
        look,
        stdin=stdin,
        send=send,
        settings_path=settings_path,
        work_area=work_area,
        on_screen=on_screen,
        pointer=pointer,
    )
    worker = window.worker
    window.wait_window()
    worker.join(RELEASE_WAIT_S)
    return EXIT_CLOSED
