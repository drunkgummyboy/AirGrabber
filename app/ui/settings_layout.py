"""The layout of the Settings window (app/ui/settings_dialog.py).

A sidebar of sections (a small group label above each part of the list; `fill_sidebar`,
`SidebarItem`; it scrolls on a screen too short for all of them), a 1-pixel line, and one scrolling page per section (`SettingsPage`: a heading,
its muted line, then boxes, `SettingsBox`, one below the other); `build_layout` makes that
skeleton in a window, `fit_window` sizes the window (pages scroll on a short screen).

It paints with AirGrabber's tokens, read when a widget is made, never copied at import
(theme.apply_accent sets them once at startup).
"""

from __future__ import annotations

import math
import tkinter as tk
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, NamedTuple

import customtkinter as ctk

from app.i18n import t
from app.ui import theme
from app.ui.borderless import CLOSE_GAP, CLOSE_ROOM
from app.ui.dialog_kit import DETAIL_SIZE, SMALL_SIZE, FitScroll, text_label
from app.ui.scrollable import ScrollableFrame

# -- sizes (logical, unscaled CTk units) ---------------------------------------------------------

SIDEBAR_WIDTH = 220  # at least; wider when a section's name in bold needs it
SIDEBAR_PAD = 12
ITEM_HEIGHT = 36
ICON_WIDTH = 30
ITEM_PAD = (8, 4, 10)  # left of the icon, between icon and name, right of the name
# The page column's content: the boxes' outer width. With the sidebar a dialog stays about
# 880 wide, so it fits a 1366 x 768 screen at 150 % (910 wide).
PAGE_WIDTH = 580
PAGE_PAD_LEFT = 22
PAGE_PAD_RIGHT = 8  # the scrollbar has its own inset
# The page (and the sidebar, level with it) starts under the close button (no title bar,
# app/ui/borderless.py): what scrolls never passes under it.
PAGE_TOP = CLOSE_ROOM + CLOSE_GAP
PAGE_BOTTOM = 20  # under a page's last box
MAX_PAGE_HEIGHT = 680  # a taller page scrolls; the dialog stays about as tall as it was
SCREEN_RESERVE = 80  # left free for the taskbar and the title bar
HEADING_SIZE = 20
BOX_RADIUS = 12  # as the Prowlarr panel's
BOX_PAD_X = 18
BOX_PAD_Y = 16
BOX_GAP = 14  # between boxes
FIRST_BOX_GAP = 18  # under a page's muted line
TEXT_SIZE = SMALL_SIZE + 1  # a box's explanation
BOX_INNER = PAGE_WIDTH - 2 * (BOX_PAD_X + 1)  # inside a box's border and padding
CONTROL_GAP = 12  # between a box's explanation and its controls


def dialog_height(wanted: int, screen_height: int, *, reserve: int = SCREEN_RESERVE) -> int:
    """The dialog's height (logical units): what its content wants, but never taller than the
    screen minus room for the taskbar and title bar (the pages scroll instead)."""
    return max(1, min(wanted, screen_height - reserve))


# -- pages and boxes -------------------------------------------------------------------------------


class SettingsBox:
    """A rounded block of related settings: a bold name, a short muted explanation, then the
    controls (`add`, one below the other)."""

    def __init__(
        self,
        master: Any,
        title: str | None = None,
        text: str | None = None,
    ) -> None:
        self.frame = ctk.CTkFrame(
            master,
            fg_color=theme.TAB_BG,
            border_width=1,
            border_color=theme.PANEL_EDGE,
            corner_radius=BOX_RADIUS,
        )
        self.body = ctk.CTkFrame(self.frame, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=BOX_PAD_X, pady=BOX_PAD_Y)
        self.body.grid_columnconfigure(0, weight=1)
        self._next_row = 0
        self._labels: list[tuple[ctk.CTkLabel, theme.Color]] = []  # greyed by `set_enabled`
        if title is not None:
            heading = ctk.CTkLabel(
                self.body,
                text=title,
                font=theme.font(16, bold=True),
                text_color=theme.TEXT,
                anchor="w",
            )
            self.add(heading, gap=0, sticky="w")
            self._labels.append((heading, theme.TEXT))
        if text is not None:
            line = self.text(text)
            self.add(line, gap=2, sticky="w")
            self._labels.append((line, theme.TEXT_MUTED))

    def set_enabled(self, enabled: bool) -> None:
        """Its name and explanation in their colours, or greyed (its controls are disabled by
        the caller)."""
        for label, color in self._labels:
            label.configure(text_color=color if enabled else theme.TEXT_FAINT)

    def text(self, text: str, *, indent: int = 0) -> ctk.CTkLabel:
        """A muted explanation line in this box (not placed yet)."""
        return text_label(
            self.body, text, size=TEXT_SIZE, color=theme.TEXT_MUTED, wraplength=BOX_INNER - indent
        )

    def add(
        self, widget: Any, *, gap: int = CONTROL_GAP, sticky: str = "ew", padx: int = 0
    ) -> int:
        """Place `widget` (a child of `body`) on the next row, `gap` below the previous one.
        Returns its row."""
        row = self.take_row()
        widget.grid(
            row=row, column=0, sticky=sticky, padx=(padx, 0), pady=(0 if row == 0 else gap, 0)
        )
        return row

    def frame_row(self, *, gap: int = CONTROL_GAP) -> ctk.CTkFrame:
        """A transparent frame on the next row, for a line of controls."""
        row = ctk.CTkFrame(self.body, fg_color="transparent")
        self.add(row, gap=gap)
        return row

    def take_row(self) -> int:
        row = self._next_row
        self._next_row += 1
        return row


class SettingsPage:
    """One section's page: a heading, its muted line, then boxes (`box`) and other blocks
    (`add`), one below the other."""

    def __init__(self, master: Any, title: str, note: str) -> None:
        self.frame = ctk.CTkFrame(master, fg_color="transparent")
        self.frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            self.frame,
            text=title,
            font=theme.font(HEADING_SIZE, bold=True),
            text_color=theme.TEXT,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        text_label(
            self.frame, note, size=DETAIL_SIZE, color=theme.TEXT_MUTED, wraplength=PAGE_WIDTH
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))
        self._next_row = 2

    def box(self, title: str | None = None, text: str | None = None) -> SettingsBox:
        box = SettingsBox(self.frame, title, text)
        self.add(box.frame)
        return box

    def add(self, widget: Any, *, gap: int | None = None) -> None:
        if gap is None:
            gap = FIRST_BOX_GAP if self._next_row == 2 else BOX_GAP
        widget.grid(row=self._next_row, column=0, sticky="ew", pady=(gap, 0))
        self._next_row += 1


# -- the sidebar -----------------------------------------------------------------------------------


class SidebarItem(ctk.CTkFrame):
    """One section in the sidebar: its icon and name; a click shows its page."""

    def __init__(
        self,
        master: Any,
        section: str,
        icon: str,
        on_click: Callable[[str], None],
    ) -> None:
        super().__init__(master, fg_color="transparent", corner_radius=8, cursor="hand2")
        self.section = section
        self._on_click = on_click
        self._selected = False
        self._hovered = False
        self._icon = ctk.CTkLabel(
            self,
            text=icon,
            width=ICON_WIDTH,
            height=ITEM_HEIGHT,
            font=theme.font(DETAIL_SIZE),
            text_color=theme.TEXT_MUTED,
        )
        self._icon.pack(side="left", padx=(ITEM_PAD[0], ITEM_PAD[1]))
        self._name = ctk.CTkLabel(
            self,
            text=t(section),
            height=ITEM_HEIGHT,
            anchor="w",
            font=theme.font(DETAIL_SIZE),
            text_color=theme.TEXT,
        )
        self._name.pack(side="left", fill="x", expand=True, padx=(0, ITEM_PAD[2]))
        for widget in (self, self._icon, self._name):
            widget.bind("<Button-1>", lambda _event: self.click(), add="+")
            widget.bind("<Enter>", lambda _event: self._hover(True), add="+")
            widget.bind("<Leave>", self._on_leave, add="+")

    @property
    def selected(self) -> bool:
        return self._selected

    def click(self) -> None:
        self._on_click(self.section)

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self._paint()

    def _on_leave(self, event: tk.Event[Any]) -> None:
        # Moving between the item's own labels is not leaving it.
        try:
            inside = self.winfo_containing(event.x_root, event.y_root)
        except (KeyError, tk.TclError):
            inside = None
        while inside is not None:
            if inside is self:
                return
            inside = inside.master
        self._hover(False)

    def _hover(self, hovered: bool) -> None:
        self._hovered = hovered
        self._paint()

    def _paint(self) -> None:
        color: theme.Color
        if self._selected:
            color = theme.ROW_SELECTED
        elif self._hovered:
            color = theme.CHIP
        else:
            color = "transparent"
        self.configure(fg_color=color)
        self._name.configure(font=theme.font(DETAIL_SIZE, bold=self._selected))


def sidebar_width(sections: Iterable[str]) -> int:
    """The sidebar's width (logical): room for the longest section name (untranslated, shown
    through t()) in bold (the selected one), so the pages don't shift when another section is
    chosen."""
    bold = theme.font(DETAIL_SIZE, bold=True)
    widest = max((int(bold.measure(t(section))) for section in sections), default=0)
    item = sum(ITEM_PAD) + ICON_WIDTH + widest + 4  # + a little slack
    return max(SIDEBAR_WIDTH, item + 2 * SIDEBAR_PAD)


def fill_sidebar(
    sidebar: Any,
    groups: Sequence[tuple[str, Sequence[str]]],
    icons: Mapping[str, str],
    on_click: Callable[[str], None],
) -> dict[str, SidebarItem]:
    """A group label (upper case) above each group's sections, in order; groups without a
    section are left out. Names are untranslated (shown through t())."""
    items: dict[str, SidebarItem] = {}
    first = True
    for group, sections in groups:
        if not sections:
            continue
        ctk.CTkLabel(
            sidebar,
            text=t(group).upper(),
            font=theme.font(12, bold=True),
            text_color=theme.TEXT_MUTED,
            anchor="w",
        ).pack(fill="x", padx=(10, 0), pady=(0 if first else 14, 4))
        first = False
        for section in sections:
            item = SidebarItem(sidebar, section, icons[section], on_click)
            item.pack(fill="x", pady=(0, 2))
            items[section] = item
    return items


# -- the window --------------------------------------------------------------------------------------


class SidebarScroll(FitScroll):
    """What holds the sidebar: a plain frame until the window is too short for all its sections
    (13 of them at 150 % on a 1080p screen); then it scrolls, its bar taking its room from the
    list so that the pages beside it keep their width."""

    def __init__(self, master: Any, width: int) -> None:
        super().__init__(master)
        self._full_width = width
        self._width_now = width
        self.configure(width=width)

    def refit(self) -> None:
        super().refit()
        width = self._full_width
        if self.scrolls:
            width -= int(self._scrollbar.cget("width"))
        if width != self._width_now:
            self._width_now = width
            self.configure(width=width)


class SettingsLayout(NamedTuple):
    """What `build_layout` made: the window's body (grid: the sidebar in column 0, the line in
    column 1, the pages in column 2 of row 0; row 1, column 2 is the caller's, for its footer),
    the sidebar (what `fill_sidebar` fills) and what scrolls it, the pages' scrolling frame."""

    body: ctk.CTkFrame
    sidebar: ctk.CTkFrame
    scroll: ScrollableFrame
    sidebar_scroll: SidebarScroll


def build_layout(window: Any, sidebar: int) -> SettingsLayout:
    """The sidebar (`sidebar` logical units wide at least), a 1-pixel line beside it and the
    pages' scrolling frame, packed into `window`."""
    body = ctk.CTkFrame(window, fg_color="transparent")
    body.pack(fill="both", expand=True)
    scaling = ctk.ScalingTracker.get_widget_scaling(body)
    # Plain Tk grid options are not DPI-scaled by CTk.
    body.grid_columnconfigure(0, minsize=round(sidebar * scaling))
    body.grid_columnconfigure(2, weight=1)
    body.grid_rowconfigure(0, weight=1)

    side_scroll = SidebarScroll(body, sidebar - 2 * SIDEBAR_PAD)
    side_scroll.grid(
        row=0, column=0, rowspan=2, sticky="nsew", padx=SIDEBAR_PAD, pady=(PAGE_TOP, 12)
    )
    side = ctk.CTkFrame(side_scroll, fg_color="transparent")
    side.grid(row=0, column=0, sticky="nsew")
    # A 1-pixel line between the sidebar and the page (a CTkFrame that thin draws nothing).
    line = tk.Frame(body, width=1, highlightthickness=0)
    line.grid(row=0, column=1, rowspan=2, sticky="ns")

    def paint_line() -> None:
        line.configure(background=theme.pick(theme.PANEL_EDGE))

    paint_line()
    theme.on_mode_change(line, paint_line)
    scroll = ScrollableFrame(
        body,
        width=PAGE_WIDTH,
        height=200,
        fg_color="transparent",
        scrollbar_button_color=theme.SCROLLBAR,
        scrollbar_button_hover_color=theme.EDGE,
    )
    scroll.grid(
        row=0,
        column=2,
        sticky="nsew",
        padx=(PAGE_PAD_LEFT, PAGE_PAD_RIGHT),
        pady=(PAGE_TOP, 0),
    )
    scroll.grid_columnconfigure(0, weight=1)
    return SettingsLayout(body, side, scroll, side_scroll)


def page_height(content: int, scaling: float) -> int:
    """The pages' height (logical): the tallest page's (`content`, pixels) and the room under
    it, at most MAX_PAGE_HEIGHT (a taller page scrolls)."""
    return min(math.ceil(content / scaling) + PAGE_BOTTOM, MAX_PAGE_HEIGHT)


def fit_window(window: Any, scroll: ScrollableFrame, height: int) -> None:
    """Give the pages `height` (logical) and fix the window at the size its content wants; on
    a short screen at the screen's height instead, the pages scrolling."""
    scaling = ctk.ScalingTracker.get_window_scaling(window)
    scroll.configure(height=height)
    window.update_idletasks()
    width = math.ceil(window.winfo_reqwidth() / scaling)
    wanted = math.ceil(window.winfo_reqheight() / scaling)
    _screen_width, screen_height = window.wm_maxsize()  # physical pixels
    fitted = dialog_height(wanted, int(screen_height / scaling))
    window.geometry(f"{width}x{fitted}")  # CTk scales the size (not the position)
