"""Hover tips: a short line of text under a widget after the pointer rests on it.

`add_tooltip(widget, text)` shows `text` about half a second after the pointer enters
`widget` (anywhere on it: CTk widgets are a frame over an inner canvas and label, and Tk sends
the frame its own Enter/Leave as the pointer crosses into or out of any of them). The tip goes
on leave, on any click and when the widget is destroyed; one tip is on screen at a time. It
never takes focus or clicks: it sits next to the widget, never under the pointer.

All tips share one borderless window per Tk root, shown and withdrawn as needed.
"""

from __future__ import annotations

import sys
import tkinter as tk
import weakref
from typing import Any

import customtkinter as ctk

from app.ui import theme

DELAY_MS = 500  # hover time before a tip shows
GAP = 8  # between the widget and the tip (unscaled units)
PAD_X = 12
PAD_Y = 8
FONT_SIZE = 13
CORNER_RADIUS = 6

Rect = tuple[int, int, int, int]  # left, top, right, bottom (physical pixels)


def tip_position(
    widget: Rect, tip_size: tuple[int, int], screen: Rect, gap: int
) -> tuple[int, int]:
    """Where a tip of `tip_size` goes for a widget at `widget`, all in physical pixels.

    Centred under the widget, `gap` below it; above it when there is no room below; moved
    left or right to stay on `screen` (and down to its top if it fits neither way).
    """
    left, top, right, bottom = widget
    tip_w, tip_h = tip_size
    s_left, s_top, s_right, s_bottom = screen
    x = (left + right) // 2 - tip_w // 2
    x = max(s_left, min(x, s_right - tip_w))
    y = bottom + gap
    if y + tip_h > s_bottom:
        y = top - gap - tip_h
    return x, max(s_top, y)


def add_tooltip(widget: tk.Misc, text: str) -> Tooltip:
    """Give `widget` a hover tip; change it later with `Tooltip.set_text` ("" = no tip)."""
    return Tooltip(widget, text)


class Tooltip:
    """The hover tip of one widget. Binds with add="+", so the widget's own bindings stay."""

    def __init__(self, widget: tk.Misc, text: str) -> None:
        self.widget = widget
        self.text = text
        self._job: str | None = None
        # tk.Misc.bind, not CTk's override: that one binds the inner canvas and label instead,
        # missing a text label CTk creates later (a button whose text is set after creation).
        tk.Misc.bind(widget, "<Enter>", self._on_enter, add="+")
        tk.Misc.bind(widget, "<Leave>", self._on_leave, add="+")
        tk.Misc.bind(widget, "<Destroy>", self._on_destroy, add="+")
        _bind_clicks(widget)

    def set_text(self, text: str) -> None:
        """Show `text` from now on; an empty text switches the tip off."""
        if text == self.text:
            return
        self.text = text
        if _manager.owner is self and _manager.showing:
            if text:
                self._show()
            else:
                self.hide()

    @property
    def visible(self) -> bool:
        return _manager.owner is self and _manager.showing

    def hide(self) -> None:
        self._cancel()
        if _manager.owner is self:
            _manager.hide()

    def _on_enter(self, _event: Any) -> None:
        if not self.text or _manager.owner is self:
            return  # no tip, or already pending / showing
        _manager.take(self)
        # On the root, not the widget: the job's Tcl command is then the root's, which
        # MainWindow.destroy() cancels (and deletes) with every other pending job.
        self._job = _root_of(self.widget).after(DELAY_MS, self._show)

    def _on_leave(self, _event: Any) -> None:
        # Moving between the widget's own parts (canvas → label) is no leave.
        if not self._pointer_inside():
            self.hide()

    def _on_destroy(self, event: Any) -> None:
        if event.widget is self.widget:
            self.hide()

    def _cancel(self) -> None:
        if self._job is not None:
            try:
                _root_of(self.widget).after_cancel(self._job)
            except tk.TclError:
                pass  # the interpreter is gone
            self._job = None

    def _show(self) -> None:
        self._job = None
        try:
            if not self.text or not self.widget.winfo_ismapped():
                return
        except tk.TclError:
            return
        _manager.show(self, self.widget, self.text)

    def _pointer_inside(self) -> bool:
        try:
            x, y = self.widget.winfo_pointerxy()
            path = str(self.widget.tk.call("winfo", "containing", x, y))
        except tk.TclError:
            return False
        own = str(self.widget)
        return path == own or path.startswith(own + ".")


class _Manager:
    """Which tooltip is pending or showing, and the shared tip window."""

    def __init__(self) -> None:
        self.owner: Tooltip | None = None
        self.showing = False
        self._window: _TipWindow | None = None

    def take(self, tooltip: Tooltip) -> None:
        """`tooltip` becomes the one tip pending or on screen; the previous one goes."""
        previous = self.owner
        if previous is not None and previous is not tooltip:
            previous.hide()
        self.owner = tooltip

    def show(self, tooltip: Tooltip, widget: tk.Misc, text: str) -> None:
        self.take(tooltip)
        root = _root_of(widget)
        if self._window is None or not self._window.belongs_to(root):
            if self._window is not None:
                self._window.destroy()
            self._window = _TipWindow(root)
        self._window.show(widget, text)
        self.showing = True

    def hide(self) -> None:
        self.owner = None
        self.showing = False
        if self._window is not None:
            self._window.hide()

    def hide_any(self, _event: Any = None) -> None:
        if self.owner is not None:
            self.owner.hide()


_manager = _Manager()
_click_roots: weakref.WeakSet[tk.Tk] = weakref.WeakSet()  # roots with the hide-on-click binding


def _root_of(widget: tk.Misc) -> tk.Tk:
    root: tk.Tk = widget.nametowidget(".")
    return root


def _bind_clicks(widget: tk.Misc) -> None:
    """Any click anywhere hides the tip: one "all" binding per Tk interpreter."""
    root = _root_of(widget)
    if root not in _click_roots:
        _click_roots.add(root)
        # Tk's own "all" tag has a <Button-1> binding, which wins over a bare <ButtonPress>
        # for the left button: add to both.
        for sequence in ("<ButtonPress>", "<ButtonPress-1>"):
            tk.Misc.bind_all(root, sequence, _manager.hide_any, add="+")


class _TipWindow:
    """A borderless, always-on-top window with a rounded SURFACE box and one line of text."""

    def __init__(self, root: tk.Tk) -> None:
        self._root = root
        # The window's own colour is keyed out (where supported), leaving the rounded box.
        top = tk.Toplevel(root, bg=theme.BG, takefocus=False)
        top.withdraw()
        top.overrideredirect(True)
        try:
            top.attributes("-topmost", True)
            if sys.platform == "win32":
                top.attributes("-transparentcolor", theme.BG)
        except tk.TclError:
            pass
        box = ctk.CTkFrame(
            top,
            fg_color=theme.SURFACE,
            border_width=1,
            border_color=theme.EDGE,
            corner_radius=CORNER_RADIUS,
        )
        box.pack()
        self._label = ctk.CTkLabel(
            box, text="", height=1, font=theme.font(FONT_SIZE), text_color=theme.TEXT
        )
        self._label.pack(padx=PAD_X, pady=PAD_Y)
        self._top = top

    def belongs_to(self, root: tk.Tk) -> bool:
        try:
            return self._root is root and bool(self._top.winfo_exists())
        except tk.TclError:
            return False

    def show(self, widget: tk.Misc, text: str) -> None:
        top = self._top
        if self._label.cget("text") != text:
            self._label.configure(text=text)
        top.update_idletasks()
        size = (top.winfo_reqwidth(), top.winfo_reqheight())
        left, top_y = widget.winfo_rootx(), widget.winfo_rooty()
        rect = (left, top_y, left + widget.winfo_width(), top_y + widget.winfo_height())
        gap = round(GAP * ctk.ScalingTracker.get_widget_scaling(widget))
        x, y = tip_position(rect, size, _screen_rect(widget, rect), gap)
        top.geometry(f"+{x}+{y}")
        top.deiconify()
        top.lift()

    def hide(self) -> None:
        try:
            self._top.withdraw()
        except tk.TclError:
            pass  # gone with its root

    def destroy(self) -> None:
        try:
            self._top.destroy()
        except tk.TclError:
            pass


def _screen_rect(widget: tk.Misc, rect: Rect) -> Rect:
    """The screen the widget is on, in physical pixels: its monitor on Windows (so a window on
    a second screen keeps its tips there), else the primary screen from wm_maxsize() (which,
    unlike winfo_screenwidth(), is physical pixels under CTk's DPI awareness)."""
    monitor = _monitor_rect((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
    if monitor is not None:
        return monitor
    width, height = _root_of(widget).wm_maxsize()
    return 0, 0, width, height


def _monitor_rect(x: int, y: int) -> Rect | None:
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes

    class MonitorInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("rcMonitor", wintypes.RECT),
            ("rcWork", wintypes.RECT),
            ("dwFlags", wintypes.DWORD),
        ]

    try:
        user32 = ctypes.windll.user32
        user32.MonitorFromPoint.restype = wintypes.HMONITOR
        user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
        user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MonitorInfo)]
        monitor = user32.MonitorFromPoint(wintypes.POINT(x, y), 2)  # MONITOR_DEFAULTTONEAREST
        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(MonitorInfo)
        if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
    except (AttributeError, OSError):
        return None
    r = info.rcMonitor
    return int(r.left), int(r.top), int(r.right), int(r.bottom)
