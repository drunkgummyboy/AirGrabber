"""The accent colours (Settings → Looks → Accent colour): ten calm palettes, Slate first.

Each palette gives the buttons, chosen items, tabs, links and highlights its colour, and the
backgrounds a very slight tint of it, in the light and the dark look alike. Slate is the slate
blue AirGrabber always had: its colours are app/ui/theme.py's as they are written there.

The other palettes' colours come from the design (mockups/accent-colours.html): per palette the
accent, its hover, and per look the window background, the cards' surface, the quiet chips, the
quiet lines (PLACEHOLDER, PANEL_EDGE), the borders, the chosen table row, today's calendar cell
and the links (`Shades`). Every other accent-dependent token is Slate's value moved the way a
related token moves (`shifted`): its hue turned and its lightness changed as much, its
saturation scaled by as much (a grey without a hue of its own takes the palette's tint).

Green (downloaded), amber (missing) and red (remove, a close button under the pointer) keep
their meaning in every palette; the text colours stay as they are (the design keeps them).

`tokens(accent, slate)` is pure: theme.apply_accent sets the result as theme's tokens, once at
startup before the first window (a change takes a restart).
"""

from __future__ import annotations

import colorsys
from collections.abc import Mapping
from dataclasses import dataclass

from app.i18n import N_
from app.models import Accent

Pair = tuple[str, str]  # (light, dark), as app/ui/theme.py

# The theme tokens a palette sets, each a (light, dark) pair.
ACCENT_TOKENS: tuple[str, ...] = (
    "BG",
    "SURFACE",
    "TAB_BG",
    "EDGE",
    "ACCENT",
    "ACCENT_HOVER",
    "LINK",
    "PLACEHOLDER",
    "HOVER",
    "MENU_HOVER",
    "SELECTED",
    "SELECTED_HOVER",
    "SCROLLBAR",
    "CELL_BG",
    "CELL_BORDER",
    "TODAY_BG",
    "STACK_NEAR",
    "STACK_NEAR_EDGE",
    "STACK_FAR",
    "STACK_FAR_EDGE",
    "CHIP",
    "CHIP_HOVER",
    "ROW_SELECTED",
    "NOT_AIRED",
)


@dataclass(frozen=True, slots=True)
class Shades:
    """One look's (light or dark) colours of a palette, as the design gives them."""

    bg: str  # BG
    surface: str  # SURFACE
    chip: str  # CHIP
    line: str  # PLACEHOLDER (and PANEL_EDGE)
    edge: str  # EDGE
    rowsel: str  # ROW_SELECTED
    todaybg: str  # TODAY_BG
    link: str  # LINK


@dataclass(frozen=True, slots=True)
class Palette:
    name: str  # N_(): shown through t()
    accent: str  # ACCENT, both looks
    hover: str  # ACCENT_HOVER, both looks
    light: Shades
    dark: Shades


# In the order Settings shows them. Slate's shades are the design's; the tokens themselves are
# theme.py's (where the two differ, theme.py's win: Slate looks as it always did). They are the
# anchors the other palettes' derived tokens move from.
PALETTES: dict[Accent, Palette] = {
    Accent.SLATE: Palette(
        N_("Slate"),
        "#4E6A85",
        "#61809D",
        light=Shades(
            "#F2F4F7", "#FFFFFF", "#E6EBF1", "#DCE2E9", "#768DA4", "#D7E3F0", "#DEE8F3", "#2D6390"
        ),
        dark=Shades(
            "#161616", "#222324", "#2A2B2D", "#2E3033", "#4E6A85", "#2A3A4D", "#182133", "#7FA3C4"
        ),
    ),
    Accent.OCEAN: Palette(
        N_("Ocean"),
        "#3A6EA5",
        "#4C82BA",
        light=Shades(
            "#F1F4F8", "#FFFFFF", "#E2EAF3", "#D5DFEA", "#7B9DC4", "#D3E2F3", "#DDE8F5", "#2C5F95"
        ),
        dark=Shades(
            "#141619", "#1F2329", "#272D35", "#2C323A", "#3A6EA5", "#1F3550", "#162438", "#86B1E0"
        ),
    ),
    Accent.PETROL: Palette(
        N_("Petrol"),
        "#2F6676",
        "#3D7B8C",
        light=Shades(
            "#F0F4F5", "#FFFFFF", "#E0EAEC", "#D3E0E3", "#7099A4", "#CFE2E7", "#DBEBEF", "#275868"
        ),
        dark=Shades(
            "#131617", "#1E2325", "#262D30", "#2B3336", "#3F7585", "#1E363D", "#15262B", "#79B3C3"
        ),
    ),
    Accent.TEAL: Palette(
        N_("Teal"),
        "#2E7A76",
        "#3B918C",
        light=Shades(
            "#F0F5F4", "#FFFFFF", "#E0ECEA", "#D3E2DF", "#6E9E99", "#CFE6E3", "#DCEFEC", "#23706B"
        ),
        dark=Shades(
            "#131716", "#1E2423", "#26302E", "#2A3432", "#3E7C77", "#1E3937", "#142826", "#6FBDB6"
        ),
    ),
    Accent.SAGE: Palette(
        N_("Sage"),
        "#5E7A66",
        "#718E79",
        light=Shades(
            "#F2F5F2", "#FFFFFF", "#E4EBE5", "#D8E1D9", "#8DA593", "#DAE6DC", "#E3EDE5", "#4A6652"
        ),
        dark=Shades(
            "#151715", "#212421", "#2A2E2A", "#2F332F", "#5E7A66", "#2C3A30", "#1D2820", "#A3C2AB"
        ),
    ),
    Accent.INDIGO: Palette(
        N_("Indigo"),
        "#5A5FA3",
        "#6D72B6",
        light=Shades(
            "#F3F3F8", "#FFFFFF", "#E6E6F2", "#DADAEA", "#8A8DC2", "#DCDDF2", "#E4E5F6", "#4A4F98"
        ),
        dark=Shades(
            "#151519", "#212128", "#2A2A34", "#2F2F3A", "#5A5FA3", "#2C2D4A", "#1C1D33", "#9EA2E0"
        ),
    ),
    Accent.PLUM: Palette(
        N_("Plum"),
        "#7A5786",
        "#8D6A99",
        light=Shades(
            "#F6F2F7", "#FFFFFF", "#EDE4F0", "#E2D6E6", "#A585AF", "#E8DAEC", "#F0E4F3", "#6E4A7B"
        ),
        dark=Shades(
            "#171518", "#242026", "#2E2830", "#332C36", "#7A5786", "#3A2B40", "#271D2B", "#C29ACF"
        ),
    ),
    Accent.ROSE: Palette(
        N_("Rose"),
        "#9E5A6C",
        "#B16D7F",
        light=Shades(
            "#F8F2F3", "#FFFFFF", "#F0E3E6", "#E8D6DB", "#C08A98", "#F1DCE2", "#F5E4E8", "#8C4A5C"
        ),
        dark=Shades(
            "#181516", "#262022", "#30282B", "#362D30", "#9E5A6C", "#432A33", "#2C1C22", "#D79AAB"
        ),
    ),
    Accent.MOCHA: Palette(
        N_("Mocha"),
        "#7A6555",
        "#8D7868",
        light=Shades(
            "#F6F3F1", "#FFFFFF", "#EDE6E1", "#E2D9D2", "#A6907F", "#EADFD7", "#F1E7DF", "#6A5546"
        ),
        dark=Shades(
            "#171615", "#242120", "#2E2A28", "#34302D", "#7A6555", "#3A3029", "#29211C", "#CDB4A0"
        ),
    ),
    Accent.GRAPHITE: Palette(
        N_("Graphite"),
        "#5D6670",
        "#717B86",
        light=Shades(
            "#F3F4F5", "#FFFFFF", "#E7E9EB", "#DCDFE2", "#8E969E", "#DDE1E5", "#E6E9EC", "#4A535C"
        ),
        dark=Shades(
            "#151515", "#222222", "#2B2B2B", "#303030", "#5D6670", "#33383F", "#22262B", "#A9B3BE"
        ),
    ),
}

# The tokens the design gives per palette, by their Shades field (both looks).
_FROM_SHADES: dict[str, str] = {
    "BG": "bg",
    "SURFACE": "surface",
    "CHIP": "chip",
    "PLACEHOLDER": "line",
    "EDGE": "edge",
    "ROW_SELECTED": "rowsel",
    "TODAY_BG": "todaybg",
    "LINK": "link",
}

# Every other token, per look (light, dark): "=accent" / "=hover" / "=surface" = the palette's
# own colour (as in Slate, where the token is that colour); a Shades field = Slate's value
# moved the way that field moves from Slate to the palette.
_DERIVED: dict[str, tuple[str, str]] = {
    "TAB_BG": ("bg", "bg"),
    "HOVER": ("todaybg", "=hover"),
    "MENU_HOVER": ("todaybg", "=accent"),
    "SELECTED": ("rowsel", "=accent"),
    "SELECTED_HOVER": ("rowsel", "=hover"),
    "SCROLLBAR": ("line", "=surface"),
    "CELL_BG": ("chip", "todaybg"),
    "CELL_BORDER": ("line", "todaybg"),
    "STACK_NEAR": ("bg", "todaybg"),
    "STACK_NEAR_EDGE": ("edge", "edge"),
    "STACK_FAR": ("bg", "todaybg"),
    "STACK_FAR_EDGE": ("edge", "edge"),
    "CHIP_HOVER": ("chip", "chip"),
    "NOT_AIRED": ("chip", "chip"),
}

NEUTRAL = 0.02  # a saturation below this is a grey: no hue of its own to turn


def accent_name(accent: Accent) -> str:
    """The palette's name as N_() marked it (show it through t())."""
    return PALETTES[accent].name


def swatch_color(accent: Accent) -> str:
    """The palette's accent: its swatch in Settings and Quick setup."""
    return PALETTES[accent].accent


def tokens(accent: Accent, slate: Mapping[str, Pair]) -> dict[str, Pair]:
    """Every ACCENT_TOKENS token for `accent`, given Slate's (theme.py's own values). Slate
    gives `slate` back unchanged."""
    if accent is Accent.SLATE:
        return {name: slate[name] for name in ACCENT_TOKENS}
    palette = PALETTES[accent]
    anchors = PALETTES[Accent.SLATE]
    out: dict[str, Pair] = {}
    for name in ACCENT_TOKENS:
        if name == "ACCENT":
            out[name] = (palette.accent, palette.accent)
        elif name == "ACCENT_HOVER":
            out[name] = (palette.hover, palette.hover)
        elif name in _FROM_SHADES:
            field = _FROM_SHADES[name]
            out[name] = (getattr(palette.light, field), getattr(palette.dark, field))
        else:
            light_rule, dark_rule = _DERIVED[name]
            base_light, base_dark = slate[name]
            out[name] = (
                _derive(light_rule, base_light, palette, palette.light, anchors.light),
                _derive(dark_rule, base_dark, palette, palette.dark, anchors.dark),
            )
    return out


def _derive(rule: str, base: str, palette: Palette, own: Shades, anchors: Shades) -> str:
    if rule == "=accent":
        return palette.accent
    if rule == "=hover":
        return palette.hover
    if rule == "=surface":
        return own.surface
    return shifted(base, getattr(anchors, rule), getattr(own, rule))


# -- colour arithmetic (pure) -------------------------------------------------------------------


def to_hex(color: str) -> str:
    """"#rrggbb" (upper case) for "#rrggbb" or a Tk grey ("gray25")."""
    text = color.strip()
    if text.startswith("#") and len(text) == 7:
        int(text[1:], 16)  # a bad digit raises ValueError
        return text.upper()
    lowered = text.lower()
    for prefix in ("gray", "grey"):
        if lowered.startswith(prefix) and lowered[len(prefix) :].isdigit():
            percent = int(lowered[len(prefix) :])
            if 0 <= percent <= 100:
                level = (percent * 255 + 50) // 100
                return f"#{level:02X}{level:02X}{level:02X}"
    raise ValueError(f"not a colour this module reads: {color!r}")


def _hls(color: str) -> tuple[float, float, float]:
    hexed = to_hex(color)
    red, green, blue = (int(hexed[i : i + 2], 16) / 255 for i in (1, 3, 5))
    return colorsys.rgb_to_hls(red, green, blue)


def _from_hls(hue: float, lightness: float, saturation: float) -> str:
    red, green, blue = colorsys.hls_to_rgb(hue % 1.0, lightness, saturation)
    return "#" + "".join(f"{max(0, min(255, round(c * 255))):02X}" for c in (red, green, blue))


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def shifted(color: str, before: str, after: str) -> str:
    """`color` moved the way `before` moves to `after`: its lightness changed by as much; its
    hue turned by as much and its saturation scaled by as much, or, where `color` or `before`
    is a grey (no hue to turn), the hue of `after` and its saturation changed by as much."""
    hue, lightness, saturation = _hls(color)
    hue_before, light_before, sat_before = _hls(before)
    hue_after, light_after, sat_after = _hls(after)
    lightness = _clamp(lightness + light_after - light_before)
    if saturation < NEUTRAL or sat_before < NEUTRAL:
        saturation = _clamp(saturation + sat_after - sat_before)
        if sat_after >= NEUTRAL:
            hue = hue_after
    else:
        if sat_after >= NEUTRAL:
            hue = hue + hue_after - hue_before
        saturation = _clamp(saturation * sat_after / sat_before)
    return _from_hls(hue, lightness, saturation)
