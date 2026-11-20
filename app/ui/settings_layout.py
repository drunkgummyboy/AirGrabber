"""The layout of the Settings window (app/ui/settings_dialog.py).

A sidebar of pages (a small group label above each part of the list; `fill_sidebar`,
`SidebarItem`; it scrolls on a screen too short for all of them), a 1-pixel line, and one
scrolling page each (`SettingsPage`: a heading, its muted line, then boxes, `SettingsBox`, one
below the other; a `PageSlot` keeps a place among them for boxes made later, MediaForge's);
`build_layout` makes that skeleton in a window, `fit_window` sizes the window (pages scroll on a
short screen; it can be dragged wider and taller, its texts following: app/ui/reflow.py).

It paints with AirGrabber's tokens, read when a widget is made, never copied at import
(theme.apply_accent sets them once at startup).
"""

from __future__ import annotations

import math
import tkinter as tk
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from typing import Any, NamedTuple

import customtkinter as ctk

from app.i18n import N_, t
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
# The page column's content: the boxes' outer width (`page_width()`) as the window opens.
# WIDE_PAGE_WIDTH where the screen has room: with the sidebar a dialog is then about 1175 wide
# (the owner's wish for a wider window, 2026-11-19, and wider still, 2026-11-20), which fits a
# 1920 x 1080 screen at 150 % (1280 wide). On a narrower screen the page narrows to fit
# (`fitting_page_width`), down to NARROW_PAGE_WIDTH, the old width (a dialog about 855 wide:
# 1366 x 768 at 150 % is 910). The window can then be dragged wider (and taller): its texts
# follow (app/ui/reflow.py), never narrower than it opened.
WIDE_PAGE_WIDTH = 900
NARROW_PAGE_WIDTH = 580
PAGE_PAD_LEFT = 22
PAGE_PAD_RIGHT = 8  # the scrollbar has its own inset
SCROLLBAR_ROOM = 24  # the pages' scrollbar beside them (about 23), with a little slack
# What the window holds beside the page and the sidebar: the 1-pixel line, the page's padding
# and its scrollbar.
PAGE_SIDES = 1 + PAGE_PAD_LEFT + PAGE_PAD_RIGHT + SCROLLBAR_ROOM
SCREEN_EDGES = 20  # left free across the screen: the window's own edges
# Slack under the sum of the parts: what Tk / CTk round on other screens and fonts (GitHub's
# 100 % runner came out 1 unit wider than the parts add up to).
FIT_SLACK = 12
# The page (and the sidebar, level with it) starts under the close button (no title bar,
# app/ui/borderless.py): what scrolls never passes under it.
PAGE_TOP = CLOSE_ROOM + CLOSE_GAP
PAGE_BOTTOM = 20  # under a page's last box
MAX_PAGE_HEIGHT = 680  # a taller page scrolls; the dialog stays about as tall as it was
MIN_HEIGHT = 420  # the window dragged shorter: at least this tall (logical)
SCREEN_RESERVE = 80  # left free for the taskbar and the title bar
HEADING_SIZE = 20
BOX_RADIUS = 12  # as the Prowlarr panel's
BOX_PAD_X = 18
BOX_PAD_Y = 16
BOX_GAP = 14  # between boxes
FIRST_BOX_GAP = 18  # under a page's muted line
TEXT_SIZE = SMALL_SIZE + 1  # a box's explanation
BOX_INSET = 2 * (BOX_PAD_X + 1)  # a box's border and padding, both sides
CONTROL_GAP = 12  # between a box's explanation and its controls
REFUSAL_ROW = 999  # a box's refusal line: under everything in it (Tk allows rows < 10000)

_page_width = WIDE_PAGE_WIDTH  # the open window's (`use_page_width`)


def page_width() -> int:
    """The page's content width (logical) of the Settings window open now: what every wrap and
    box width on its pages is made from (WIDE_PAGE_WIDTH when none is open)."""
    return _page_width


def box_inner() -> int:
    """The width inside a box's border and padding (logical), for `page_width()`."""
    return _page_width - BOX_INSET


def use_page_width(width: int) -> None:
    """The page width of the window that opens now (between NARROW_PAGE_WIDTH and
    WIDE_PAGE_WIDTH): set once before its pages are built, and back to WIDE_PAGE_WIDTH when it
    closes."""
    global _page_width
    _page_width = max(NARROW_PAGE_WIDTH, min(WIDE_PAGE_WIDTH, int(width)))


def fitting_page_width(screen_width: float, sidebar: int) -> int:
    """The widest page (logical) with which a window with a `sidebar` wide sidebar fits a screen
    `screen_width` wide (logical) less its edges: WIDE_PAGE_WIDTH where there is room, never
    narrower than NARROW_PAGE_WIDTH."""
    room = math.floor(screen_width) - SCREEN_EDGES - FIT_SLACK - sidebar - PAGE_SIDES
    return max(NARROW_PAGE_WIDTH, min(WIDE_PAGE_WIDTH, room))


TOGGLE_MARK = 48  # a switch's track (a tick's box) and the gap to its text, with slack


def wrap_toggle(toggle: Any, room: int) -> None:
    """Let a switch's (or tick's) text wrap so that the whole toggle stays within `room`
    (logical): a long text (Dutch's, at the narrowest page) goes on to a second line instead of
    being cut off at the box's edge. CTk has no wrap for it: its text label's own."""
    label = getattr(toggle, "_text_label", None)
    if label is None:
        return
    scaling = ctk.ScalingTracker.get_widget_scaling(toggle)
    label.configure(wraplength=max(1, round((room - TOGGLE_MARK) * scaling)), justify="left")


def dialog_height(wanted: int, screen_height: int, *, reserve: int = SCREEN_RESERVE) -> int:
    """The dialog's height (logical units): what its content wants, but never taller than the
    screen minus room for the taskbar and title bar (the pages scroll instead)."""
    return max(1, min(wanted, screen_height - reserve))


# -- pages and boxes -------------------------------------------------------------------------------


class SettingsBox:
    """A rounded block of related settings: a bold name, a short muted explanation, then the
    controls (`add`, one below the other); at its foot, in red, why its last change wasn't
    stored (`refuse`, app/ui/settings_commit.py)."""

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
        self._refusal: ctk.CTkLabel | None = None  # why a change wasn't stored (`refuse`)
        self._refusals: dict[Hashable, str] = {}  # per control (its source), oldest first
        self.heading: ctk.CTkLabel | None = None  # its bold name, when it has one
        self.title = title or ""
        self.search_title = self.title  # what Settings' search names it by (`SettingsPage.box`)
        if title is not None:
            heading = self.heading = ctk.CTkLabel(
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

    @property
    def refusal(self) -> str:
        """Why the box's changes weren't stored ("" when they were), one line per control."""
        label = self._refusal
        return str(label.cget("text")) if label is not None and label.winfo_manager() else ""

    def refuse(self, text: str | None, *, source: Hashable = None) -> None:
        """Say in red, at the box's foot, why a change of `source` (its control: the fields it
        stores) wasn't stored; None takes that one away. Another control's refusal stays: its
        text is still refused."""
        if text:
            self._refusals.pop(source, None)
            self._refusals[source] = text  # the newest last
        else:
            self._refusals.pop(source, None)
        shown = "\n".join(self._refusals.values())
        if not shown:
            if self._refusal is not None:
                self._refusal.grid_remove()
            return
        if self._refusal is None:
            self._refusal = text_label(
                self.body, "", size=TEXT_SIZE, color=theme.DANGER, wraplength=box_inner()
            )
        self._refusal.configure(text=shown)
        self._refusal.grid(row=REFUSAL_ROW, column=0, sticky="w", pady=(CONTROL_GAP - 4, 0))

    def text(self, text: str, *, indent: int = 0) -> ctk.CTkLabel:
        """A muted explanation line in this box (not placed yet)."""
        return text_label(
            self.body, text, size=TEXT_SIZE, color=theme.TEXT_MUTED, wraplength=box_inner() - indent
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

    def subheading(self, text: str, *, gap: int = CONTROL_GAP + 4) -> ctk.CTkLabel:
        """A small bold name inside the box, above a group of its controls (placed)."""
        label = ctk.CTkLabel(
            self.body,
            text=text,
            font=theme.font(DETAIL_SIZE, bold=True),
            text_color=theme.TEXT,
            anchor="w",
        )
        self.add(label, gap=gap, sticky="w")
        self._labels.append((label, theme.TEXT))
        return label


class SettingsPage:
    """One page: a heading, its muted line, then boxes (`box`) and other blocks (`add`), one
    below the other; `slot()` keeps a place for blocks made later."""

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
            self.frame, note, size=DETAIL_SIZE, color=theme.TEXT_MUTED, wraplength=page_width()
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))
        self._first_row = 2
        self._first_gap = FIRST_BOX_GAP  # above the first block
        self._next_row = 2
        self.boxes: list[tuple[str, Any]] = []  # (shown title, frame): Settings' search
        self.settings_boxes: list[SettingsBox] = []  # the same boxes (`find_box`)

    def box(
        self,
        title: str | None = None,
        text: str | None = None,
        *,
        search_title: str | None = None,
    ) -> SettingsBox:
        """A box on the page. `search_title`: what Settings' search names a box without a bold
        name by (its first control's text, translated); a box with neither isn't one to find
        (a "Getting MediaForge ready…" box)."""
        box = SettingsBox(self.frame, title, text)
        box.search_title = title or search_title or ""
        self.add(box.frame)
        self.boxes.append((box.search_title, box.frame))
        self.settings_boxes.append(box)
        return box

    def find_box(self, title: str) -> SettingsBox | None:
        """The box with this (shown, or search) title on the page."""
        return next(
            (box for box in self.settings_boxes if title in (box.title, box.search_title)), None
        )

    def add(self, widget: Any, *, gap: int | None = None) -> None:
        if gap is None:
            gap = self._first_gap if self._next_row == self._first_row else BOX_GAP
        widget.grid(row=self._next_row, column=0, sticky="ew", pady=(gap, 0))
        self._next_row += 1

    def slot(self) -> PageSlot:
        """A place on this page, after what is on it now, for blocks made later (a MediaForge
        part, at the page's first visit): they come between the blocks around it."""
        row = self._next_row
        self._next_row += 1
        return PageSlot(self, row, self._first_gap if row == self._first_row else BOX_GAP)


class PageSlot(SettingsPage):
    """A place on a page (`SettingsPage.slot`), used as a page of its own: its boxes and blocks
    go one below the other there, and Settings' search finds them on the page. It shows only
    once something is put in it (an empty frame would keep its default size)."""

    def __init__(self, page: SettingsPage, row: int, gap: int) -> None:
        self.frame = ctk.CTkFrame(page.frame, fg_color="transparent")
        self.frame.grid_columnconfigure(0, weight=1)
        self._row = row
        self._first_row = 0
        self._first_gap = gap
        self._next_row = 0
        self.boxes = page.boxes  # the page's own lists
        self.settings_boxes = page.settings_boxes

    def add(self, widget: Any, *, gap: int | None = None) -> None:
        if not self.frame.winfo_manager():
            self.frame.grid(row=self._row, column=0, sticky="ew")
        super().add(widget, gap=gap)


# -- folds and links -------------------------------------------------------------------------------

SHOW_TEXT = N_("▸ Show")
HIDE_TEXT = N_("▾ Hide")


class Fold:
    """Something a box shows only once asked (closed at first): a toggle (`button`, placed by
    the caller: "▸ Show" / "▾ Hide", or its own `text` such as "? Codes") and the `body` it
    shows and hides. The body is placed by the caller first (its grid kept while hidden)."""

    def __init__(self, master: Any, body: Any, *, text: str | None = None) -> None:
        self.body = body
        self._text = text
        self.opened = False
        self.button = ctk.CTkButton(
            master,
            text="",
            width=0,
            height=26,
            corner_radius=6,
            font=theme.font(TEXT_SIZE, bold=True),
            fg_color="transparent",
            hover_color=theme.CHIP,
            text_color=theme.LINK,
            command=self.toggle,
        )
        self._show()

    def toggle(self) -> None:
        self.set_open(not self.opened)

    def set_open(self, opened: bool) -> None:
        self.opened = opened
        self._show()

    def _show(self) -> None:
        if self.opened:
            self.body.grid()
        else:
            self.body.grid_remove()
        text = self._text or t(HIDE_TEXT if self.opened else SHOW_TEXT)
        self.button.configure(text=text)


def open_page(widget: tk.Misc, page: str) -> None:
    """Show `page` (a page key of app/ui/settings_pages_registry.py) in the Settings window
    `widget` is in; nothing outside one."""
    try:
        window = widget.winfo_toplevel()
    except tk.TclError:
        return
    show: Callable[[str], None] | None = getattr(window, "show_section", None)
    if show is not None:
        show(page)


class PageLink(ctk.CTkLabel):
    """A pointer inside Settings ("⚠ Choose your Series folder under Folders.") as a link: a
    click shows `page`, where it is fixed (spec §6). Placed by the caller."""

    def __init__(
        self,
        master: Any,
        text: str,
        page: str,
        *,
        color: theme.Color | None = None,
        size: int = TEXT_SIZE,
        wraplength: int | None = None,
    ) -> None:
        """`color`: the text's (the link colour without it; a warning's amber). `wraplength`:
        a box's inner width without it."""
        super().__init__(
            master,
            text=text,
            font=theme.font(size, underline=True),
            text_color=color if color is not None else theme.LINK,
            cursor="hand2",
            anchor="w",
            justify="left",
            wraplength=box_inner() if wraplength is None else wraplength,
        )
        self.page = page
        self.bind("<Button-1>", lambda _event: self.click(), add="+")

    def click(self) -> None:
        open_page(self, self.page)


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
    """The sidebar's width (logical): room for the longest page name (untranslated, shown
    through t()) in bold (the selected one), so the pages don't shift when another page is
    chosen."""
    bold = theme.font(DETAIL_SIZE, bold=True)
    widest = max((int(bold.measure(t(section))) for section in sections), default=0)
    item = sum(ITEM_PAD) + ICON_WIDTH + widest + 4  # + a little slack
    return max(SIDEBAR_WIDTH, item + 2 * SIDEBAR_PAD)


def fill_sidebar(
    sidebar: Any,
    groups: Sequence[tuple[str | None, Sequence[str]]],
    icons: Mapping[str, str],
    on_click: Callable[[str], None],
) -> dict[str, SidebarItem]:
    """A group label (upper case) above each group's pages, in order (a group of None has
    none: Overview, at the top); groups without a page are left out. Names are untranslated
    (shown through t())."""
    items: dict[str, SidebarItem] = {}
    first = True
    for group, sections in groups:
        if not sections:
            continue
        if group is not None:
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
        width=page_width(),
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


def fit_window(window: Any, scroll: ScrollableFrame, height: int) -> int:
    """Give the pages `height` (logical) and size the window as its content wants; on a short
    screen at the screen's height instead, the pages scrolling. It can be made wider and taller
    by hand, never narrower than this (nor shorter than MIN_HEIGHT). Returns its width
    (logical)."""
    scaling = ctk.ScalingTracker.get_window_scaling(window)
    scroll.configure(height=height)
    window.update_idletasks()
    width = int(math.ceil(window.winfo_reqwidth() / scaling))
    wanted = math.ceil(window.winfo_reqheight() / scaling)
    _screen_width, screen_height = window.wm_maxsize()  # physical pixels
    fitted = dialog_height(wanted, int(screen_height / scaling))
    window.geometry(f"{width}x{fitted}")  # CTk scales the size (not the position)
    window.minsize(width, min(fitted, MIN_HEIGHT))  # CTk scales these too
    return width
