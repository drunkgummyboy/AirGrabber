"""Forge's look: AirGrabber's own accent tokens as a value views can hold on to.

`app_look()` gives every palettes.ACCENT_TOKENS token as a (light, dark) pair, as theme.py has
them when it is called: read it when a view is built, never at import (main.py applies the saved
accent at startup, before the first window). Forge code reads `look.ACCENT` and the like here;
the theme-independent tokens (TEXT, TEXT_MUTED, SUCCESS, DANGER, WARNING, ON_COLOR, ...) stay
theme.py's. Shared widgets that take a `look` (dialog_kit's buttons, popup_menu) or `colors`
(widgets.Tabview) paint with it. Canvas items, plain Tk and Pillow take `theme.pick(look.X)` and
repaint on `theme.on_mode_change`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.ui import palettes, theme

if TYPE_CHECKING:
    from app.forge.core.auto import WatchState

Pair = tuple[str, str]  # (light, dark)


@dataclass(frozen=True, slots=True)
class ForgeLook:
    """A window's accent tokens (theme.Look), each a (light, dark) pair (BRAND is the "Forge" in
    the window's title text)."""

    BG: Pair
    SURFACE: Pair
    TAB_BG: Pair
    EDGE: Pair
    ACCENT: Pair
    ACCENT_HOVER: Pair
    LINK: Pair
    BRAND: Pair
    PLACEHOLDER: Pair
    HOVER: Pair
    MENU_HOVER: Pair
    SELECTED: Pair
    SELECTED_HOVER: Pair
    SCROLLBAR: Pair
    CELL_BG: Pair
    CELL_BORDER: Pair
    TODAY_BG: Pair
    STACK_NEAR: Pair
    STACK_NEAR_EDGE: Pair
    STACK_FAR: Pair
    STACK_FAR_EDGE: Pair
    CHIP: Pair
    CHIP_HOVER: Pair
    ROW_SELECTED: Pair
    NOT_AIRED: Pair


def app_look() -> ForgeLook:
    """AirGrabber's accent tokens as a look, as theme.py has them now (the saved accent is
    applied once at startup, before the first widget: don't call this at import)."""
    return ForgeLook(**{name: getattr(theme, name) for name in palettes.ACCENT_TOKENS})


def mix(first: str, second: str, amount: float) -> str:
    """`amount` (0…1) of `second` over `first`, as "#RRGGBB": the in-between colours the
    redesigned window draws (the band's gradient, faded rows, a light's glow) come from tokens
    this way, never from new hex values."""
    a, b = palettes.to_hex(first), palettes.to_hex(second)
    parts = (
        round(int(a[i : i + 2], 16) * (1 - amount) + int(b[i : i + 2], 16) * amount)
        for i in (1, 3, 5)
    )
    return "#" + "".join(f"{max(0, min(255, part)):02X}" for part in parts)


def mix_pair(first: Pair, second: Pair, amount: float) -> Pair:
    """`mix` for each look of two (light, dark) pairs."""
    return mix(first[0], second[0], amount), mix(first[1], second[1], amount)


def light_colour(state: WatchState) -> Pair:
    """The renamer's light (the queue's status line, Settings › Auto-rename): green watching, amber
    trial, red unreachable, else muted."""
    return {
        "watching": theme.SUCCESS,
        "trial": theme.WARNING,
        "unreachable": theme.DANGER,
    }.get(state, theme.TEXT_MUTED)
