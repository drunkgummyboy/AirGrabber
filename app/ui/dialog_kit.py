"""Shared look of the roomy setup dialogs (first-run welcome, TMDB key guide): sizes, buttons,
headings, links, placing a modal dialog over its parent, fully on the screen (and bringing it
back on top after a switch between light and dark), and FitScroll: what scrolls when a dialog
is taller than the screen allows (`max_dialog_height`)."""

from __future__ import annotations

import math
import tkinter as tk
import webbrowser
from collections.abc import Callable
from typing import Any

import customtkinter as ctk

from app.ui import theme
from app.ui.tooltip import work_area

# Logical (unscaled) CTk units.
DIALOG_WIDTH = 640
PAD_X = 32
PAD_Y = 28
CONTENT_WIDTH = DIALOG_WIDTH - 2 * PAD_X
BLOCK_GAP = 24  # between the blocks of a step
LINE_GAP = 8  # between a heading and its muted line
NAV_GAP = 28  # above the Back / Next row
BUTTON_HEIGHT = 40
BUTTON_RADIUS = 8

HEADING_SIZE = 22
BODY_SIZE = 16
DETAIL_SIZE = 15  # muted lines and descriptions
SMALL_SIZE = 13  # "Step 2 of 3", hints

SCREEN_MARGIN = 16  # kept free above and below a dialog that is as tall as the screen allows
WINDOW_FRAME = 40  # a dialog's title bar and borders around its content (Windows 11)

Rect = tuple[int, int, int, int]  # left, top, right, bottom


def primary_button(
    master: Any, text: str, command: Callable[[], None], *, width: int = 140
) -> ctk.CTkButton:
    return ctk.CTkButton(
        master,
        text=text,
        width=width,
        height=BUTTON_HEIGHT,
        corner_radius=BUTTON_RADIUS,
        fg_color=theme.ACCENT,
        hover_color=theme.ACCENT_HOVER,
        font=theme.font(15, bold=True),
        command=command,
    )


def secondary_button(
    master: Any, text: str, command: Callable[[], None], *, width: int = 140
) -> ctk.CTkButton:
    return ctk.CTkButton(
        master,
        text=text,
        width=width,
        height=BUTTON_HEIGHT,
        corner_radius=BUTTON_RADIUS,
        fg_color="transparent",
        border_width=1,
        border_color=theme.EDGE,
        hover_color=theme.SURFACE,
        text_color=theme.TEXT,
        font=theme.font(15),
        command=command,
    )


def heading_row(master: Any, title: str, step: str | None = None) -> ctk.CTkFrame:
    """A step's heading, with "Step N of 3" at the top right when `step` is given."""
    row = ctk.CTkFrame(master, fg_color="transparent")
    row.grid_columnconfigure(0, weight=1)
    ctk.CTkLabel(
        row,
        text=title,
        font=theme.font(HEADING_SIZE, bold=True),
        text_color=theme.TEXT,
        anchor="w",
        justify="left",
    ).grid(row=0, column=0, sticky="w")
    if step is not None:
        ctk.CTkLabel(
            row, text=step, font=theme.font(SMALL_SIZE), text_color=theme.TEXT_MUTED
        ).grid(row=0, column=1, sticky="ne", padx=(16, 0))
    return row


def text_label(
    master: Any,
    text: str,
    *,
    size: int = BODY_SIZE,
    color: theme.Color = theme.TEXT,
    wraplength: int = CONTENT_WIDTH,
    bold: bool = False,
) -> ctk.CTkLabel:
    """Left-aligned wrapping text (CTk scales `wraplength` with the DPI)."""
    return ctk.CTkLabel(
        master,
        text=text,
        font=theme.font(size, bold=bold),
        text_color=color,
        anchor="w",
        justify="left",
        wraplength=wraplength,
    )


def link_label(master: Any, text: str, url: str, *, size: int = DETAIL_SIZE) -> ctk.CTkLabel:
    """Underlined link text that opens `url` in the browser."""
    link = ctk.CTkLabel(
        master,
        text=text,
        font=theme.font(size, underline=True),
        text_color=theme.LINK,
        cursor="hand2",
    )
    link.bind("<Button-1>", lambda _event: webbrowser.open(url))
    return link


def fit_height(height: int, area_height: float) -> int:
    """`height` (a dialog's content, CTk units), at most what fits a work area `area_height`
    tall (CTk units) with the title bar and SCREEN_MARGIN above and below."""
    return max(1, min(height, int(area_height) - WINDOW_FRAME - 2 * SCREEN_MARGIN))


def on_screen(x: int, y: int, width: int, height: int, area: Rect) -> tuple[int, int]:
    """(x, y) moved as little as needed for a window `width` × `height` (title bar included)
    with that top-left corner to lie within `area`; its top-left corner stays on the area
    when the window is larger. Physical pixels."""
    left, top, right, bottom = area
    return max(left, min(x, right - width)), max(top, min(y, bottom - height))


def screen_area(widget: Any) -> Rect:
    """The work area (physical pixels) of the monitor nearest to `widget`'s middle; off Windows
    the primary screen."""
    x = widget.winfo_rootx() + widget.winfo_width() // 2
    y = widget.winfo_rooty() + widget.winfo_height() // 2
    area = work_area(x, y)
    if area is None:
        width, height = widget.winfo_toplevel().wm_maxsize()
        area = (0, 0, int(width), int(height))
    return area


def max_dialog_height(master: Any, scaling: float) -> int:
    """The tallest content (CTk units) a dialog opened over `master` may have to fit on its
    screen (`fit_height`)."""
    _left, top, _right, bottom = screen_area(master)
    return fit_height(10**6, (bottom - top) / scaling)


def place_over(dialog: ctk.CTkToplevel, master: Any) -> None:
    """Centre `dialog` horizontally over `master`, in its upper third (as Settings does), moved
    where needed to lie fully on the screen's work area."""
    dialog.update_idletasks()
    width, height = dialog.winfo_reqwidth(), dialog.winfo_reqheight()
    x = master.winfo_rootx() + (master.winfo_width() - width) // 2
    y = master.winfo_rooty() + (master.winfo_height() - height) // 3
    frame = round(WINDOW_FRAME * ctk.ScalingTracker.get_window_scaling(dialog))
    x, y = on_screen(x, y, width, height + frame, screen_area(master))
    dialog.geometry(f"+{x}+{y}")


class FitScroll(ctk.CTkScrollableFrame):
    """A frame that scrolls only while what it holds is taller than its room: the scrollbar
    shows just then, else it looks like a plain frame. Its content goes in grid row 0, column 0
    (several steps there: the hidden ones grid_remove'd).

    `follow`: its height follows its content, up to `set_limit` (Quick setup). Else the host
    sets its height (`configure(height=…)`) and shorter content is stretched to it (the
    welcome's steps fill the dialog)."""

    def __init__(self, master: Any, *, follow: bool = False) -> None:
        super().__init__(
            master,
            fg_color="transparent",
            corner_radius=0,
            border_width=0,
            scrollbar_button_color=theme.SURFACE,
            scrollbar_button_hover_color=theme.EDGE,
        )
        self._follow = follow
        self._limit: int | None = None  # physical pixels
        self._spacer: ctk.CTkFrame | None = None
        self.grid_columnconfigure(0, weight=1)
        self._scrollbar.grid_remove()
        self.bind("<Configure>", lambda _event: self.refit(), add="+")
        self._parent_canvas.bind("<Configure>", lambda _event: self.refit(), add="+")

    @property
    def scrolls(self) -> bool:
        """The scrollbar shows: the content is taller than the room."""
        return bool(self._scrollbar.winfo_manager())

    def content_height(self) -> int:
        """The content's height (physical pixels; the tallest of what is gridded)."""
        return max((widget.winfo_reqheight() for widget in self.grid_slaves()), default=0)

    def set_limit(self, height: int) -> None:
        """`follow`: at most this tall (physical pixels); taller content scrolls."""
        self._limit = max(1, height)
        self.refit()

    @property
    def reserved(self) -> bool:
        """The scrollbar's room is kept (`reserve_bar`)."""
        return self._spacer is not None

    def reserve_bar(self) -> None:
        """Keep the scrollbar's room beside the content also while it is hidden, so the content
        keeps its width whether it scrolls or not (the host grows by it)."""
        if self._spacer is not None:
            return
        # An empty column takes no room in Tk: a see-through frame under the scrollbar holds it.
        self._spacer = ctk.CTkFrame(
            self._parent_frame,
            width=self._scrollbar.cget("width"),
            height=1,
            fg_color="transparent",
        )
        self._spacer.grid(row=1, column=1, padx=(0, 1))
        self._spacer.lower(self._scrollbar)

    def width_needed(self, scaling: float) -> int:
        """Its width (CTk units) without a scrollbar shown for the moment (while the dialog
        is still being sized): the content's, plus the scrollbar's room when reserved."""
        bar = self._spacer.winfo_reqwidth() + 1 if self._spacer is not None else 0
        return int(math.ceil((self._parent_canvas.winfo_reqwidth() + bar) / scaling))

    def to_top(self) -> None:
        self._parent_canvas.yview_moveto(0)

    def refit(self) -> None:
        canvas = self._parent_canvas
        need = self.content_height()
        if self._follow:
            room = need if self._limit is None else min(need, self._limit)
            if canvas.winfo_reqheight() != room:
                canvas.configure(height=room)
        else:
            room = canvas.winfo_height()
            if room <= 1:  # not on screen yet
                return
            self.grid_rowconfigure(0, minsize=room)
        overflow = need > room
        if overflow != self.scrolls:
            if overflow:
                self._scrollbar.grid()
            else:
                self._scrollbar.grid_remove()
        if not overflow:
            self.to_top()


def activate_later(dialog: ctk.CTkToplevel) -> None:
    """Raise, focus and grab `dialog` once it is mapped: CTkToplevel can open behind its parent
    on Windows. After every switch between light and dark it does so again
    (`reactivate_after_mode_change`)."""

    def activate() -> None:
        if dialog.winfo_exists():
            dialog.lift()
            dialog.focus_force()
            dialog.grab_set()

    dialog.after(150, activate)
    reactivate_after_mode_change(dialog)


# After a switch, CTk shows its windows again 5 ms later and gives the focus back 10 ms later.
REACTIVATE_MS = 80


def reactivate_after_mode_change(dialog: ctk.CTkToplevel) -> None:
    """`reactivate(dialog)` a moment after every switch between light and dark, for as long as
    `dialog` exists. CTk hides and re-shows each window to recolour its title bar; a dialog over
    another window (its owner hidden at the same moment) can stay hidden while it still holds
    the grab, so the window under it no longer answers."""
    theme.on_mode_change(dialog, lambda: dialog.after(REACTIVATE_MS, lambda: reactivate(dialog)))


def _owners(dialog: tk.Toplevel) -> list[tk.Tk | tk.Toplevel]:
    """`dialog` and the windows it sits over, up to the main window: [dialog, owner, …, root]."""
    chain: list[tk.Tk | tk.Toplevel] = [dialog]
    window: tk.Tk | tk.Toplevel = dialog
    while window.master is not None:
        window = window.master.winfo_toplevel()
        chain.append(window)
    return chain


def reactivate(dialog: tk.Toplevel) -> None:
    """Show `dialog` again on top, with the keyboard focus and the grab: first the windows it
    sits over (Settings under Quick setup) and then the dialog itself, each when it is hidden
    while the window under it shows (nothing while the main window is hidden in the tray). A
    dialog opened over this one keeps the grab and stays on top."""
    try:
        if not dialog.winfo_exists():
            return
        chain = _owners(dialog)
        for window, owner in reversed(list(zip(chain, chain[1:]))):
            if window.wm_state() == "withdrawn" and owner.wm_state() in ("normal", "zoomed"):
                window.wm_deiconify()
        if dialog.wm_state() != "normal":
            return
        # The windows with a grab (Tk keeps one per application): none, or this dialog.
        holders = [str(window) for window in dialog.tk.splitlist(dialog.tk.call("grab", "current"))]
        if holders and str(dialog) not in holders:
            return  # a dialog over this one has the grab
        dialog.lift()
        dialog.focus_force()
        dialog.grab_set()
    except tk.TclError:  # gone meanwhile, or not on screen yet
        pass
