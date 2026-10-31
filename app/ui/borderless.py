"""Pop-up windows without the Windows title bar: a round close button (✕) in the top-right
corner instead, and the window moves when dragged by its own background.

    make_borderless(window, drag_widgets=(body, heading), on_close=None) -> Borderless | None

Call it on a CTkToplevel (or any Tk Toplevel) once its content is built and its Escape key
bound, before placing it (`dialog_kit.place_over`). On Windows the title bar goes; elsewhere
nothing changes and it returns None (the normal title bar stays, no close button). Keep
`window.title(...)`: the taskbar and Alt+Tab still show it, so a title the dialog shows
nowhere else also goes in a heading inside it.

- The close button: a round ✕ CLOSE_SIZE wide, CLOSE_INSET from the top and right edges,
  red on hover, with the tip "Close (Esc)" ("Close" when Escape doesn't close the window). A
  click takes the window's own way out: `on_close`, else its WM_DELETE_WINDOW protocol
  handler, else `destroy()`. The corner it sits in (CLOSE_ROOM square) is the dialog's to
  keep free: `clearance(pad)` is the extra room a top heading needs at its right. Around the
  disc it shows the window's background, or, over a picture (the info window's backdrop),
  that picture's piece (`CloseButton.set_backdrop`).
- Dragging: a left-button press on one of `drag_widgets` (`Borderless.add_drag` adds more
  later) moves the window, through Windows' own move loop (Aero Snap included). Any Tk
  widget can be one: a CTk widget is bound on its inner canvas and label, a Toplevel only
  where nothing else is (its own background), a Canvas not on an item with a click binding of
  its own or the NO_DRAG tag. A drag widget's own labels (direct children, unless they have
  the hand cursor of a link) drag too. Buttons, entries and the other controls never start a
  drag.

How the title bar goes (Windows 10 / 11): the window keeps every style Tk gave it, so its
taskbar and Alt+Tab behaviour, keyboard focus, `grab_set`, its shadow, Windows 11's rounded
corners and (for a resizable window) resizing by its left, right and bottom edges stay as they
were. Only the title bar's room is taken away: Tk's wrapper window (the real top-level window,
`GetParent(winfo_id())`) is subclassed, and its WM_NCCALCSIZE answer lets the client area
reach up to the window's top edge; the window gets shorter by as much, so Tk's content keeps
its size. (`overrideredirect` was rejected: Tk then makes a dialog a child window of the
desktop, without a taskbar entry, a shadow or reliable keyboard focus. Removing WS_CAPTION
leaves Tk sizing the window for a title bar it no longer has.)

Tk replaces a wrapper with a new one now and then: on `wm resizable` / `wm transient`, and
whenever the window it sits over is shown again (the main window after a switch between light
and dark, or back from the tray). The old wrapper's WM_NCDESTROY finds the new one, still
hidden, and takes its title bar off before Tk shows it. CTk's own title-bar recolouring on a
switch between light and dark (which hides and re-shows the window) is switched off for these
windows (unless `hide_to_recolour`: the info window); the dark or light window border follows
the switch instead (`set_dark`).

Tk sizes the window for the title bar it thinks is there; it corrects the height itself after
each size request (it takes the extra room for a wrapped menu bar). Where it pins the size of a
window that can't be resized (during a drag), WM_GETMINMAXINFO takes the title bar off its
number (`fitted_height`), or the dialog would grow by it.

A drag runs Windows' move loop inside a Tk binding. Tk is not told about that loop
(WM_ENTERSIZEMOVE is kept from it): it would run Tcl events, and with them Python callbacks,
from inside a Python callback, which _tkinter can't take. The window's content is drawn again
once the button is let go. For the same reason the window procedure (a ctypes callback that
Tk's own code calls) only ever calls Win32, never tkinter.

The Win32 / DWM calls sit behind `Native` (`set_native`): tests use a fake, the real one
(`WindowsNative`) only runs in the app.
"""

from __future__ import annotations

import ctypes
import logging
import sys
import tkinter as tk
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

import customtkinter as ctk
from PIL import Image, ImageDraw, ImageTk

from app.i18n import N_, t
from app.ui import theme
from app.ui.tooltip import add_tooltip

log = logging.getLogger(__name__)

# Logical (unscaled) CTk units.
CLOSE_SIZE = 34
CLOSE_INSET = 12  # from the window's top and right edges
CLOSE_ROOM = CLOSE_INSET + CLOSE_SIZE  # the top-right square the button takes
CLOSE_GAP = 8  # kept between the button and the content beside or under it
CLOSE_GLYPH = "✕"
CLOSE_FONT_SIZE = 15
CLOSE_TIP = N_("Close (Esc)")
CLOSE_TIP_NO_ESCAPE = N_("Close")

NO_DRAG = "no-drag"  # a canvas item with this tag never starts a drag (the info window's poster)

_SUPERSAMPLE = 4  # the disc is drawn this much larger, then scaled down: smooth edges
_LABELS: tuple[type, ...] = (ctk.CTkLabel, tk.Label)
_LINK_CURSOR = "hand2"


def clearance(pad: int) -> int:
    """The room (CTk units) a heading at the top of a dialog keeps free at its right for the
    close button, beyond the dialog's own right padding `pad`."""
    return max(0, CLOSE_ROOM + CLOSE_GAP - pad)


# -- the Win32 / DWM seam ------------------------------------------------------------------------


class Native(Protocol):
    """What the window system does for a borderless window. `window_id` is the Tk window's own
    id (`winfo_id()`), which stays the same for as long as the window exists."""

    def attach(self, window_id: int, *, dark: bool) -> bool:
        """Take the title bar off (and keep it off); False when that isn't possible."""
        ...

    def start_drag(self, window_id: int) -> None:
        """Move the window with the pointer until the mouse button is let go."""
        ...

    def set_dark(self, window_id: int, dark: bool) -> None:
        """Give the window's border its dark or light look."""
        ...


_native: Native | None = None


def set_native(native: Native | None) -> Native | None:
    """Use `native` from now on (None: no window becomes borderless); returns the one before.
    Tests put a fake in."""
    global _native
    previous, _native = _native, native
    return previous


# -- making a window borderless ----------------------------------------------------------------


def make_borderless(
    window: tk.Toplevel,
    *,
    drag_widgets: Iterable[tk.Misc] = (),
    on_close: Callable[[], object] | None = None,
    hide_to_recolour: bool = False,
) -> Borderless | None:
    """Take `window`'s title bar off and give it the close button (see the module's notes);
    None where that isn't done (not on Windows): the window keeps its title bar.

    `hide_to_recolour`: keep CTk's own hiding and re-showing of the window around its title-bar
    recolouring (right after it opens and after a switch between light and dark). The info
    window needs it: its "tabs" layout (a tab view in a scrolling frame) only settles while the
    window is hidden; laid out while showing, its height swings between two sizes for good."""
    native = _native
    if native is None:
        return None
    window_id = window.winfo_id()
    if not native.attach(window_id, dark=theme.is_dark()):
        log.warning("Couldn't take the title bar off a window; it keeps its own")
        return None
    # CTk hides and re-shows a window after a switch between light and dark to recolour its
    # title bar: there is none to recolour, and that would only make the window blink.
    setattr(window, "_deactivate_windows_window_header_manipulation", not hide_to_recolour)
    theme.on_mode_change(window, lambda: native.set_dark(window_id, theme.is_dark()), early=True)
    borderless = Borderless(window, native, on_close=on_close)
    borderless.add_drag(*drag_widgets)
    return borderless


class Borderless:
    """A window without its title bar: its close button and the widgets it is dragged by."""

    def __init__(
        self,
        window: tk.Toplevel,
        native: Native,
        *,
        on_close: Callable[[], object] | None = None,
    ) -> None:
        self.window = window
        self._native = native
        self._window_id = window.winfo_id()
        self._on_close = on_close
        self.close_button = CloseButton(window, command=self.close)
        tip = CLOSE_TIP if window.bind("<Escape>") else CLOSE_TIP_NO_ESCAPE
        add_tooltip(self.close_button, t(tip))

    def close(self) -> None:
        """The close button: the window's own way out."""
        if self._on_close is not None:
            self._on_close()
            return
        handler = str(self.window.protocol("WM_DELETE_WINDOW"))
        if handler:
            self.window.tk.eval(handler)
        else:
            self.window.destroy()

    def add_drag(self, *widgets: tk.Misc) -> None:
        """Let a press on these widgets (and their own labels) drag the window."""
        for widget in widgets:
            for area in (widget, *_own_labels(widget)):
                _bind(area, "<ButtonPress-1>", self._on_press(area))
        self.close_button.keep_on_top()

    def _on_press(self, area: tk.Misc) -> Callable[[Any], None]:
        def press(event: Any) -> None:
            if starts_drag(area, event.widget):
                self._native.start_drag(self._window_id)

        return press


def starts_drag(area: tk.Misc, target: object) -> bool:
    """Whether a press on `target` (the event's widget) drags the window by drag widget `area`:
    not on a control, a Toplevel only on its own background, a canvas not on an item that has a
    click binding of its own (the info window's trailer button) or the NO_DRAG tag (its
    poster)."""
    if not isinstance(target, tk.Misc):
        return False  # a widget gone meanwhile
    if isinstance(area, (tk.Tk, tk.Toplevel)) and str(target) != str(area):
        return False  # on something inside it
    owner = getattr(target, "master", None)  # a CTk widget's inner canvas or label
    if isinstance(target, _CONTROLS) or isinstance(owner, _CONTROLS):
        return False
    return not (isinstance(target, tk.Canvas) and _on_clickable_item(target))


def _on_clickable_item(canvas: tk.Canvas) -> bool:
    for item in canvas.find_withtag("current"):
        if NO_DRAG in canvas.gettags(item):
            return True
        for tag in (str(item), *canvas.gettags(item)):
            sequences = canvas.tk.splitlist(canvas.tag_bind(tag))
            if any("Button" in str(sequence) for sequence in sequences):
                return True
    return False


def _own_labels(widget: tk.Misc) -> list[tk.Misc]:
    """`widget`'s labels (its direct children) that aren't links."""
    labels: list[tk.Misc] = []
    for child in widget.winfo_children():
        if not isinstance(child, _LABELS):
            continue
        try:
            cursor = str(child.cget("cursor"))
        except (tk.TclError, ValueError):
            cursor = ""
        if cursor != _LINK_CURSOR:
            labels.append(child)
    return labels


def _bind(widget: tk.Misc, sequence: str, command: Callable[[Any], object]) -> None:
    """`widget.bind` with add="+": a CTk widget binds its inner canvas and label (where the
    clicks land), plain Tk the widget itself."""
    try:
        widget.bind(sequence, command, "+")
    except NotImplementedError:  # a CTk widget without its own bind
        tk.Misc.bind(widget, sequence, command, "+")


# What lies under the close button over a picture: an RGB image of the button's size (physical
# pixels), or None for the window's own background.
Backdrop = Callable[[], Image.Image | None]


class CloseButton(tk.Canvas):
    """The round ✕ in a borderless window's top-right corner: a quiet disc (CLOSE_BUTTON) with
    the ✕ in CLOSE_GLYPH, red (CLOSE_BUTTON_HOVER) with a white ✕ under the pointer; a click
    (let go on it) calls `command`. Drawn on its own canvas, anti-aliased at the screen's
    scaling, because a CTkButton grows wider than it is tall around any text. Around the disc
    the window's background, or what `set_backdrop` gives (a picture under the button). Follows
    a switch between light and dark and a change of Windows' scaling."""

    def __init__(self, window: tk.Misc, *, command: Callable[[], object]) -> None:
        super().__init__(window, highlightthickness=0, borderwidth=0)
        self._window = window
        self._command = command
        self._hovered = False
        self._pressed = False
        self._backdrop: Backdrop | None = None
        self._under_photo: ImageTk.PhotoImage | None = None
        self._discs: tuple[ImageTk.PhotoImage, ImageTk.PhotoImage] | None = None
        self._under = self.create_image(0, 0, anchor="nw")  # the picture under it, lowest
        self._disc = self.create_image(0, 0, anchor="nw")
        self._glyph = self.create_text(0, 0, text=CLOSE_GLYPH, anchor="center")
        self._font = theme.font(CLOSE_FONT_SIZE)
        self.bind("<Enter>", lambda _event: self._hover(True), add="+")
        self.bind("<Leave>", lambda _event: self._hover(False), add="+")
        self.bind("<ButtonPress-1>", self._press, add="+")
        self.bind("<ButtonRelease-1>", self._release, add="+")
        self.redraw()
        theme.on_mode_change(self, self.redraw, early=True)  # with the window's background
        ctk.ScalingTracker.add_widget(self._rescaled, self)
        self.bind("<Destroy>", self._gone, add="+")
        self.keep_on_top()

    @property
    def hovered(self) -> bool:
        return self._hovered

    def invoke(self) -> None:
        self._command()

    def keep_on_top(self) -> None:
        """Above the window's other content (what was made after it included)."""
        tk.Misc.tkraise(self)  # a Canvas's own lift() raises canvas items

    def set_backdrop(self, backdrop: Backdrop | None) -> None:
        """Sit on a picture: `backdrop()` gives what lies under the button now (None: the
        window's background). Call `repaint_backdrop` whenever that changes (the picture, or
        where it is under the button)."""
        self._backdrop = backdrop
        self.repaint_backdrop()

    @property
    def over_picture(self) -> bool:
        """Whether a picture shows around the disc (else the window's background)."""
        return self._under_photo is not None

    def repaint_backdrop(self) -> None:
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:  # the whole interpreter is gone
            return
        picture = self._backdrop() if self._backdrop is not None else None
        if picture is None:
            self._under_photo = None
            self.itemconfigure(self._under, image="")
            return
        self._under_photo = ImageTk.PhotoImage(picture.convert("RGB"), master=self)
        self.itemconfigure(self._under, image=self._under_photo)

    def redraw(self) -> None:
        """Draw it at the current scaling in the current mode's colours, in its corner."""
        scaling = float(ctk.ScalingTracker.get_widget_scaling(self))
        diameter = max(1, round(CLOSE_SIZE * scaling))
        background = theme.pick(_background(self._window))
        self.configure(width=diameter, height=diameter, background=background)
        self._discs = (
            ImageTk.PhotoImage(disc_image(diameter, self._rgb(theme.CLOSE_BUTTON)), master=self),
            ImageTk.PhotoImage(
                disc_image(diameter, self._rgb(theme.CLOSE_BUTTON_HOVER)), master=self
            ),
        )
        self.coords(self._glyph, diameter / 2, diameter / 2)
        self.itemconfigure(self._glyph, font=self._font.create_scaled_tuple(scaling))
        inset = round(CLOSE_INSET * scaling)
        self.place(relx=1.0, x=-inset, y=inset, anchor="ne")
        self.repaint_backdrop()
        self._paint()

    def _paint(self) -> None:
        if self._discs is not None:
            self.itemconfigure(self._disc, image=self._discs[1 if self._hovered else 0])
        glyph = theme.ON_COLOR if self._hovered else theme.CLOSE_GLYPH
        self.itemconfigure(self._glyph, fill=theme.pick(glyph))

    def _hover(self, on: bool) -> None:
        self._hovered = on
        self._paint()

    def _press(self, _event: Any) -> None:
        self._pressed = True

    def _release(self, event: Any) -> None:
        pressed, self._pressed = self._pressed, False
        inside = 0 <= event.x < self.winfo_width() and 0 <= event.y < self.winfo_height()
        if pressed and inside:
            self._command()

    def _rgb(self, color: theme.Color) -> tuple[int, int, int]:
        red, green, blue = self.winfo_rgb(theme.pick(color))  # 16 bits per channel
        return red >> 8, green >> 8, blue >> 8

    def _rescaled(self, _widget_scaling: float, _window_scaling: float) -> None:
        if self.winfo_exists():
            self.redraw()

    def _gone(self, event: Any) -> None:
        if event.widget is self:
            ctk.ScalingTracker.remove_widget(self._rescaled, self)


def disc_image(diameter: int, fill: tuple[int, int, int]) -> Image.Image:
    """A `fill`-coloured disc `diameter` physical pixels wide, transparent around it,
    anti-aliased. Tiny: fine on the event loop."""
    big = diameter * _SUPERSAMPLE
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, big - 1, big - 1), fill=255)
    disc = Image.new("RGBA", (diameter, diameter), (*fill, 255))
    disc.putalpha(mask.resize((diameter, diameter), Image.Resampling.LANCZOS))
    return disc


def _background(window: tk.Misc) -> theme.Color:
    """The colour the close button sits on: the window's own."""
    try:
        color = window.cget("fg_color")  # a CTk window's (light, dark) pair
    except (tk.TclError, ValueError):
        return str(window.cget("background"))
    if isinstance(color, (list, tuple)) and len(color) == 2:
        return (str(color[0]), str(color[1]))
    return str(color) if color and color != "transparent" else str(window.cget("background"))


# The controls a press never drags the window from (nor their inner canvas and label).
_CONTROLS: tuple[type, ...] = (
    CloseButton,
    tk.Button,
    tk.Entry,
    tk.Text,
    tk.Listbox,
    tk.Scrollbar,
    tk.Spinbox,
    tk.Scale,
    tk.Checkbutton,
    tk.Radiobutton,
    tk.Menubutton,
    ctk.CTkButton,
    ctk.CTkEntry,
    ctk.CTkTextbox,
    ctk.CTkCheckBox,
    ctk.CTkSwitch,
    ctk.CTkRadioButton,
    ctk.CTkSegmentedButton,
    ctk.CTkOptionMenu,
    ctk.CTkComboBox,
    ctk.CTkSlider,
    ctk.CTkScrollbar,
)


# -- Windows ---------------------------------------------------------------------------------------

WM_GETMINMAXINFO = 0x0024
WM_WINDOWPOSCHANGED = 0x0047
WM_NCDESTROY = 0x0082
WM_NCCALCSIZE = 0x0083
WM_NCLBUTTONDOWN = 0x00A1
WM_ENTERSIZEMOVE = 0x0231
WM_EXITSIZEMOVE = 0x0232
HTCAPTION = 2
GWLP_WNDPROC = -4
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010
SWP_FRAMECHANGED = 0x0020
SWP_NOOWNERZORDER = 0x0200
DWMWA_USE_IMMERSIVE_DARK_MODE_BEFORE_20H1 = 19
DWMWA_USE_IMMERSIVE_DARK_MODE = 20
DWMWA_WINDOW_CORNER_PREFERENCE = 33  # Windows 11; refused on Windows 10
DWMWCP_ROUND = 2
TK_WRAPPER_CLASS = "TkTopLevel"


def caption_free_top(window_top: int, window_left: int, client_left: int, *, zoomed: bool) -> int:
    """Where a window's client area starts (screen pixels) without a title bar: at the window's
    own top. A maximized window hangs its border (`client_left - window_left` thick) over the
    screen's edges, so there it starts that much lower."""
    return window_top + (client_left - window_left) if zoomed else window_top


def fitted_height(limit: int, *, client: int, border: int, title_bar: int) -> int:
    """A window height Tk pinned its smallest and largest size to (a dialog that can't be
    resized, while it is dragged), for the window without its title bar: Tk adds the border
    it believes in to the content's height, and until it measures the window again that
    border still holds the title bar. `client`: the content's height now, `border`: the
    window's real border (the window's height less `client`)."""
    if limit - border == client:
        return limit  # Tk knows the real border
    return limit - title_bar


def point_lparam(x: int, y: int) -> int:
    """A screen point as a mouse message's lParam (two 16-bit halves; negative on a monitor
    left of or above the main one)."""
    return (x & 0xFFFF) | ((y & 0xFFFF) << 16)


@dataclass
class _Wrapper:
    """A subclassed Tk wrapper window."""

    window_id: int  # Tk's own window inside it
    original: int  # Tk's window procedure
    dark: bool
    quiet: bool = False  # keep WM_WINDOWPOSCHANGED from Tk (our own size change)
    title_bar: int = 0  # the height taken off (the title bar and the border above it)


class WindowsNative:
    """The Win32 / DWM side (Windows 10 and 11). One per process (`_windows_native`): its window
    procedure must outlive every window it was given to."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("Windows only")
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        lresult = ctypes.c_ssize_t
        hwnd, uint = wintypes.HWND, wintypes.UINT
        wparam, lparam = wintypes.WPARAM, wintypes.LPARAM
        user32.SetWindowLongPtrW.argtypes = (hwnd, ctypes.c_int, ctypes.c_ssize_t)
        user32.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.CallWindowProcW.argtypes = (ctypes.c_ssize_t, hwnd, uint, wparam, lparam)
        user32.CallWindowProcW.restype = lresult
        user32.DefWindowProcW.argtypes = (hwnd, uint, wparam, lparam)
        user32.DefWindowProcW.restype = lresult
        user32.SendMessageW.argtypes = (hwnd, uint, wparam, lparam)
        user32.SendMessageW.restype = lresult
        user32.GetParent.argtypes = (hwnd,)
        user32.GetParent.restype = hwnd
        user32.SetWindowPos.argtypes = (
            hwnd, hwnd, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, uint
        )
        user32.SetWindowPos.restype = wintypes.BOOL
        user32.GetWindowRect.argtypes = (hwnd, ctypes.POINTER(wintypes.RECT))
        user32.GetClientRect.argtypes = (hwnd, ctypes.POINTER(wintypes.RECT))
        user32.ClientToScreen.argtypes = (hwnd, ctypes.POINTER(wintypes.POINT))
        user32.GetCursorPos.argtypes = (ctypes.POINTER(wintypes.POINT),)
        user32.GetClassNameW.argtypes = (hwnd, wintypes.LPWSTR, ctypes.c_int)
        for name in ("IsWindow", "IsWindowVisible", "IsZoomed", "IsIconic"):
            getattr(user32, name).argtypes = (hwnd,)
            getattr(user32, name).restype = wintypes.BOOL
        user32.ReleaseCapture.argtypes = ()
        dwmapi = ctypes.WinDLL("dwmapi")
        dwmapi.DwmSetWindowAttribute.argtypes = (
            hwnd, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD
        )
        dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
        self._user32 = user32
        self._dwmapi = dwmapi
        self._wintypes = wintypes

        class NcCalcSizeParams(ctypes.Structure):
            _fields_ = [("rgrc", wintypes.RECT * 3), ("lppos", ctypes.c_void_p)]

        class MinMaxInfo(ctypes.Structure):
            _fields_ = [
                ("ptReserved", wintypes.POINT),
                ("ptMaxSize", wintypes.POINT),
                ("ptMaxPosition", wintypes.POINT),
                ("ptMinTrackSize", wintypes.POINT),
                ("ptMaxTrackSize", wintypes.POINT),
            ]

        self._params_type = NcCalcSizeParams
        self._minmax_type = MinMaxInfo
        wndproc = ctypes.WINFUNCTYPE(lresult, hwnd, uint, wparam, lparam)
        self._proc = wndproc(self._window_proc)  # alive as long as this object (the process)
        self._proc_address = int(ctypes.cast(self._proc, ctypes.c_void_p).value or 0)
        self._wrappers: dict[int, _Wrapper] = {}
        self._dragging = 0  # the wrapper whose move loop runs

    # -- Native ------------------------------------------------------------------------------

    def attach(self, window_id: int, *, dark: bool) -> bool:
        wrapper = int(self._user32.GetParent(window_id) or 0)
        if not wrapper or not self._is_wrapper(wrapper):
            return False  # not wrapped yet (never mapped)
        return self._attach(wrapper, window_id, dark=dark)

    def start_drag(self, window_id: int) -> None:
        wrapper = int(self._user32.GetParent(window_id) or 0)
        if not wrapper:
            return
        point = self._wintypes.POINT()
        self._user32.GetCursorPos(ctypes.byref(point))
        self._user32.ReleaseCapture()
        self._dragging = wrapper
        try:
            self._user32.SendMessageW(
                wrapper, WM_NCLBUTTONDOWN, HTCAPTION, point_lparam(point.x, point.y)
            )
        finally:
            self._dragging = 0

    def set_dark(self, window_id: int, dark: bool) -> None:
        wrapper = int(self._user32.GetParent(window_id) or 0)
        frame = self._wrappers.get(wrapper)
        if frame is None:
            return
        frame.dark = dark
        self._set_dark(wrapper, dark)
        self._redraw_frame(wrapper, frame)

    # -- the wrapper's window procedure ------------------------------------------------------

    def _window_proc(self, hwnd: int | None, msg: int, wparam: int, lparam: int) -> int:
        wrapper = int(hwnd or 0)
        frame = self._wrappers.get(wrapper)
        if frame is None:  # not ours (any more): shouldn't happen
            return int(self._user32.DefWindowProcW(hwnd, msg, wparam, lparam))
        try:
            if msg == WM_NCCALCSIZE:
                return self._calc_size(wrapper, frame, wparam, lparam)
            if msg == WM_GETMINMAXINFO:
                return self._limits(wrapper, frame, wparam, lparam)
            if msg == WM_NCDESTROY:
                return self._destroyed(wrapper, frame, msg, wparam, lparam)
            if (msg == WM_WINDOWPOSCHANGED and frame.quiet) or (
                msg in (WM_ENTERSIZEMOVE, WM_EXITSIZEMOVE) and wrapper == self._dragging
            ):
                return int(self._user32.DefWindowProcW(hwnd, msg, wparam, lparam))
        except Exception:  # never out of a window procedure: Tk's own handling instead
            log.exception("Borderless window: message %#x failed", msg)
            if wrapper not in self._wrappers:
                return 0
        return int(self._user32.CallWindowProcW(frame.original, hwnd, msg, wparam, lparam))

    def _calc_size(self, wrapper: int, frame: _Wrapper, wparam: int, lparam: int) -> int:
        """The client area as Windows works it out (borders around it), then reaching up to
        the window's top: no title bar."""
        if wparam:
            rect = self._params_type.from_address(lparam).rgrc[0]
        else:
            rect = self._wintypes.RECT.from_address(lparam)
        top, left = int(rect.top), int(rect.left)
        result = int(
            self._user32.CallWindowProcW(frame.original, wrapper, WM_NCCALCSIZE, wparam, lparam)
        )
        if self._user32.IsIconic(wrapper):
            return result
        zoomed = bool(self._user32.IsZoomed(wrapper))
        rect.top = caption_free_top(top, left, int(rect.left), zoomed=zoomed)
        return 0

    def _limits(self, wrapper: int, frame: _Wrapper, wparam: int, lparam: int) -> int:
        """Tk's size limits, where it pinned them, fitted to the window without a title bar
        (`fitted_height`): a dialog would otherwise grow by its old title bar's height as soon
        as it is dragged."""
        result = int(
            self._user32.CallWindowProcW(frame.original, wrapper, WM_GETMINMAXINFO, wparam, lparam)
        )
        info = self._minmax_type.from_address(lparam)
        least, most = info.ptMinTrackSize, info.ptMaxTrackSize
        if least.y != most.y or not frame.title_bar:
            return result  # not pinned: at most a title bar's height off, in the window's favour
        window, client = self._wintypes.RECT(), self._wintypes.RECT()
        self._user32.GetWindowRect(wrapper, ctypes.byref(window))
        self._user32.GetClientRect(wrapper, ctypes.byref(client))
        inside = int(client.bottom - client.top)
        height = fitted_height(
            int(least.y),
            client=inside,
            border=int(window.bottom - window.top) - inside,
            title_bar=frame.title_bar,
        )
        least.y = most.y = height
        return result

    def _destroyed(self, wrapper: int, frame: _Wrapper, msg: int, wparam: int, lparam: int) -> int:
        user32 = self._user32
        user32.SetWindowLongPtrW(wrapper, GWLP_WNDPROC, frame.original)
        del self._wrappers[wrapper]
        result = int(user32.CallWindowProcW(frame.original, wrapper, msg, wparam, lparam))
        # Tk making a new wrapper: its own window already sits in it, still hidden.
        try:
            if user32.IsWindow(frame.window_id):
                new = int(user32.GetParent(frame.window_id) or 0)
                if new and new != wrapper and self._is_wrapper(new):
                    self._attach(new, frame.window_id, dark=frame.dark)
        except Exception:  # never out of a window procedure
            log.exception("Borderless window: couldn't follow Tk's new wrapper")
        return result

    # -- helpers -----------------------------------------------------------------------------

    def _attach(self, wrapper: int, window_id: int, *, dark: bool) -> bool:
        if wrapper in self._wrappers:
            self._wrappers[wrapper].window_id = window_id
            return True
        original = int(self._user32.SetWindowLongPtrW(wrapper, GWLP_WNDPROC, self._proc_address))
        if not original:
            log.warning("Borderless window: subclassing failed (error %d)", ctypes.get_last_error())
            return False
        frame = _Wrapper(window_id=window_id, original=original, dark=dark)
        self._wrappers[wrapper] = frame
        self._set_attribute(wrapper, DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND)
        self._set_dark(wrapper, dark)
        self._drop_caption(wrapper, frame)
        return True

    def _drop_caption(self, wrapper: int, frame: _Wrapper) -> None:
        """Work the frame out again without the title bar, the window that much shorter: Tk's
        content keeps its size (and Tk needn't hear of it)."""
        user32 = self._user32
        flags = SWP_FRAMECHANGED | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_NOOWNERZORDER
        if user32.IsIconic(wrapper) or user32.IsZoomed(wrapper):
            # The client area grows by the title bar: Tk hears of it, unless it is hidden (a hidden
            # window's news would make Tk think it withdrawn; it measures it when shown).
            frame.quiet = not user32.IsWindowVisible(wrapper)
            try:
                user32.SetWindowPos(wrapper, None, 0, 0, 0, 0, flags | SWP_NOSIZE)
            finally:
                frame.quiet = False
            return
        window = self._wintypes.RECT()
        user32.GetWindowRect(wrapper, ctypes.byref(window))
        origin = self._wintypes.POINT(0, 0)
        user32.ClientToScreen(wrapper, ctypes.byref(origin))
        title_bar = max(0, int(origin.y) - int(window.top))  # and the border above it
        if title_bar:
            frame.title_bar = title_bar
        width, height = window.right - window.left, window.bottom - window.top
        frame.quiet = True
        try:
            user32.SetWindowPos(wrapper, None, 0, 0, width, max(1, height - title_bar), flags)
        finally:
            frame.quiet = False

    def _redraw_frame(self, wrapper: int, frame: _Wrapper) -> None:
        """Draw the border again (its new colour); nothing changes size."""
        flags = (
            SWP_FRAMECHANGED
            | SWP_NOMOVE
            | SWP_NOSIZE
            | SWP_NOZORDER
            | SWP_NOACTIVATE
            | SWP_NOOWNERZORDER
        )
        frame.quiet = True
        try:
            self._user32.SetWindowPos(wrapper, None, 0, 0, 0, 0, flags)
        finally:
            frame.quiet = False

    def _set_dark(self, wrapper: int, dark: bool) -> None:
        if not self._set_attribute(wrapper, DWMWA_USE_IMMERSIVE_DARK_MODE, int(dark)):
            self._set_attribute(wrapper, DWMWA_USE_IMMERSIVE_DARK_MODE_BEFORE_20H1, int(dark))

    def _set_attribute(self, wrapper: int, attribute: int, value: int) -> bool:
        data = ctypes.c_int(value)
        result = self._dwmapi.DwmSetWindowAttribute(
            wrapper, attribute, ctypes.byref(data), ctypes.sizeof(data)
        )
        return int(result) == 0

    def _is_wrapper(self, hwnd: int) -> bool:
        name = ctypes.create_unicode_buffer(64)
        self._user32.GetClassNameW(hwnd, name, len(name))
        return name.value == TK_WRAPPER_CLASS


_windows_native: WindowsNative | None = None


def _default_native() -> Native | None:
    global _windows_native
    if sys.platform != "win32":
        return None
    try:
        _windows_native = WindowsNative()
    except (OSError, AttributeError):
        log.warning("Borderless windows unavailable; pop-ups keep their title bar", exc_info=True)
        return None
    return _windows_native


_native = _default_native()
