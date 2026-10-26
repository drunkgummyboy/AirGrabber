"""Colour palette and fonts: a light and a dark look (Settings → Looks → Theme).

Every colour is a (light, dark) pair, the way customtkinter takes colours: CTk widgets repaint
themselves when the mode changes. The dark values are the 1.x look carried over. A few brand
colours (IMDb's yellow) are one colour in both modes.

Plain Tk widgets, canvas items, ttk styles and images baked with Pillow take one colour: they
`pick` the current mode's colour, and repaint when the mode changes (`on_mode_change`, or a CTk
widget's own `_set_appearance_mode`).
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from typing import Any

import customtkinter as ctk

from app.models import ThemeMode

Pair = tuple[str, str]  # (light, dark)
Color = str | Pair  # a colour as CTk takes it: one for both modes, or a pair

BG: Pair = ("#F2F4F7", "#161616")  # window background
SURFACE: Pair = ("#FFFFFF", "#242424")  # cards and inputs
TAB_BG: Pair = ("#F8F9FB", "#1C1C1C")  # tab view body
EDGE: Pair = ("#768DA4", "#4E6A85")  # slate-blue borders
ACCENT: Pair = ("#4E6A85", "#4E6A85")  # primary buttons
ACCENT_HOVER: Pair = ("#427839", "#427839")  # button hover
TEXT: Pair = ("#1B2530", "#FFFFFF")
TEXT_MUTED: Pair = ("#4D5B6A", "#A4B2C6")
TEXT_DIM: Pair = ("#596573", "gray60")
DANGER: Pair = ("#B5302A", "#E74C3C")
SUCCESS: Pair = ("#1A6F47", "#2FA572")  # positive results (connection test passed)
LINK: Pair = ("#2D6390", "#5D8AA8")
DANGER_BUTTON: Pair = ("#C0392B", "#C0392B")  # destructive buttons (remove)
DANGER_BUTTON_HOVER: Pair = ("#922B21", "#922B21")
IMDB = "#F5C518"  # IMDb brand yellow, both modes
IMDB_HOVER = "#D4A710"
PLACEHOLDER: Pair = ("#DCE2E9", "gray20")  # poster placeholder

# Buttons that aren't filled with a colour (transparent, SURFACE or CHIP behind the text):
# their text (CTk's own default in dark) and hover. Accent-filled buttons keep light text
# (ON_ACCENT, the CTkButton default after `apply_appearance`) and ACCENT_HOVER.
BUTTON_TEXT: Pair = ("#1B2530", "#DCE4EE")
ON_ACCENT: Pair = ("#FFFFFF", "#DCE4EE")  # a button's text on ACCENT (or GRABBED, DANGER...)
ON_COLOR = "#FFFFFF"  # a badge's text on a coloured fill, both modes
HOVER: Pair = ("#DEE6EF", "#427839")
MENU_HOVER: Pair = ("#DEE6EF", "#4E6A85")  # a popup menu's item under the pointer
# The chosen part of a segmented control (tabs, Card size, week count): one text colour serves
# both parts, so in light it is a soft tint with dark text rather than the accent.
SELECTED: Pair = ("#C9D8E8", "#4E6A85")
SELECTED_HOVER: Pair = ("#B8CADD", "#427839")
SCROLLBAR: Pair = ("#D5DCE4", "#242424")  # a scrollbar's handle on the page background

# Calendar grid
CELL_BG: Pair = ("#ECF0F5", "#121620")  # day cell
CELL_BORDER: Pair = ("#D5DDE7", "#1C222E")
TODAY_BG: Pair = ("#DEE8F3", "#182133")  # today's cell (its border is ACCENT)
SUGGESTION_BG: Pair = ("#F5F3F9", "#14121A")  # untracked-show suggestion card
SUGGESTION_BORDER: Pair = ("#DAD3E6", "#2A2438")
SUGGESTION_TEXT: Pair = ("#6C6878", "gray40")  # a suggestion's episode code
SUGGESTION_BUTTON_EDGE: Pair = ("#B9B3C6", "gray30")  # its + Track button
SUGGESTION_BUTTON_TEXT: Pair = ("#5A5666", "gray60")
# A stack card (several episodes of one show on one day): the two cards peeking out behind it,
# the nearer one first; their edges a little lighter (light) / darker (dark) than EDGE.
STACK_NEAR: Pair = ("#F6F8FB", "#212830")
STACK_NEAR_EDGE: Pair = ("#9AABBD", "#46607A")
STACK_FAR: Pair = ("#EEF2F6", "#1E2329")
STACK_FAR_EDGE: Pair = ("#B9C6D3", "#3A4E63")
GRABBED: Pair = ("#2E7D4F", "#2E7D4F")  # episode button once downloaded
NOT_AIRED: Pair = ("#E1E6EC", "gray25")  # disabled episode button before broadcast
NOT_AIRED_TEXT: Pair = ("#56616D", "gray60")  # its text

# Discover / Movies shelf titles
SECTION_HOT: Pair = ("#9C5100", "#F39C12")  # trending
SECTION_NEW: Pair = ("#186E3D", "#2ECC71")  # premieres, fresh digital releases
WARNING = SECTION_HOT  # amber: something is missing rather than broken (no torrent program)
PANEL_EDGE = PLACEHOLDER  # a quiet 1 px border around a group of settings (Prowlarr)

# Indexer search dialog
CHIP: Pair = ("#E6EBF1", "gray15")  # unselected season / episode / version button
CHIP_HOVER: Pair = ("#D5DDE6", "gray25")
PENDING: Pair = ("#765D00", "#F1C40F")  # indexer still searching
ROW_SELECTED: Pair = ("#D7E3F0", "#2A3A4D")  # selected results-table row
TEXT_FAINT: Pair = ("#7D8792", "gray40")  # skipped indexers, results hidden by the filters
TEXT_QUIET: Pair = ("#58636F", "gray50")  # "No movies found." and other empty results

# CTk's own defaults that only suit dark: (widget, option) → the light value; the dark value
# stays CTk's. White text on the accent reads better than CTk's light grey.
_LIGHT_DEFAULTS: dict[tuple[str, str], str] = {
    ("CTkButton", "text_color"): ON_ACCENT[0],
}

_CTK_MODES = {ThemeMode.SYSTEM: "system", ThemeMode.DARK: "dark", ThemeMode.LIGHT: "light"}


def font(size: int = 13, *, bold: bool = False, underline: bool = False) -> ctk.CTkFont:
    return ctk.CTkFont(size=size, weight="bold" if bold else "normal", underline=underline)


def is_dark() -> bool:
    """Whether the app is in its dark look now (Windows' choice under "Follow Windows")."""
    return bool(ctk.get_appearance_mode() == "Dark")


def pick(color: Color, *, dark: bool | None = None) -> str:
    """The colour `color` stands for in the current mode (`dark` overrides it): for what
    takes one colour (plain Tk, canvas items, ttk, Pillow)."""
    if isinstance(color, str):
        return color
    light_color, dark_color = color
    return dark_color if (is_dark() if dark is None else dark) else light_color


def choice_colors(selected: bool) -> dict[str, Color]:
    """A choice button's CTk colours (season and genre chips, the search window's season,
    episode and version buttons): the accent when chosen, else a quiet chip."""
    if selected:
        return {"fg_color": ACCENT, "hover_color": ACCENT_HOVER, "text_color": ON_ACCENT}
    return {"fg_color": CHIP, "hover_color": CHIP_HOVER, "text_color": BUTTON_TEXT}


def apply_appearance(mode: ThemeMode = ThemeMode.SYSTEM) -> None:
    """Light, dark, or Windows' app mode (SYSTEM: CTk follows a change while running). Call
    before the first window is created, and again when Settings changes it: CTk widgets and
    window title bars repaint themselves, the rest through `on_mode_change`."""
    _tune_ctk_defaults()
    ctk.set_appearance_mode(_CTK_MODES[mode])
    if mode is ThemeMode.SYSTEM:
        # Now rather than at CTk's next check, so a switch from DARK / LIGHT shows at once.
        ctk.AppearanceModeTracker.init_appearance_mode()


def _tune_ctk_defaults() -> None:
    """CTk's light defaults where they don't suit AirGrabber (widgets created afterwards)."""
    for (widget, option), light in _LIGHT_DEFAULTS.items():
        defaults: dict[str, Any] = ctk.ThemeManager.theme[widget]
        current = defaults[option]
        if isinstance(current, (list, tuple)) and len(current) == 2:
            defaults[option] = [light, current[1]]


def on_mode_change(widget: tk.Misc, repaint: Callable[[], object]) -> None:
    """Call `repaint()` after every switch between light and dark while `widget` exists: for
    what CTk doesn't repaint itself (plain Tk widgets, canvas items, ttk styles). `repaint`
    runs while CTk goes through its widgets: it must not destroy any (schedule that)."""

    def changed(_mode: str) -> None:
        try:
            if widget.winfo_exists():
                repaint()
        except tk.TclError:
            pass  # gone with its interpreter

    ctk.AppearanceModeTracker.add(changed)

    def gone(event: tk.Event[Any]) -> None:
        if event.widget is widget:  # a Toplevel also hears its children go
            ctk.AppearanceModeTracker.remove(changed)

    tk.Misc.bind(widget, "<Destroy>", gone, add="+")
