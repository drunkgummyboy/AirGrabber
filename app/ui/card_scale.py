"""How big the cards are drawn: Settings → Looks → Card size (`Settings.card_size`).

The poster cards (Movies, Discover, search results, the watchlist shelf), the My shows cards
and the calendar's cards scale as a whole, like zooming: picture, buttons, gaps and text (the
calendar's compact and suggestion cards only their text). The main window reads the setting
(`CardScale.of`) when a page loads and hands the `CardScale` to the page, which hands it to its
cards and its `FlexGrid` (`scale.px(width)`).

Fonts shrink with the card but never below a readable minimum (`scaled_font_size`); text that
then no longer fits is shortened by the cards' own fitting (text_fit, the Download row's
shorter texts). The poster cards' widths are multiples of 16 units, so they and their 2:3
heights stay whole physical pixels at 125, 150 and 175 % Windows scaling.

All of this is pure (no Tk): tested in tests/test_card_scale.py.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.i18n import N_
from app.models import CardSize

# The poster card's width per size, in CTk units; XL is the original 192.
POSTER_WIDTHS: dict[CardSize, int] = {
    CardSize.XS: 128,
    CardSize.S: 144,
    CardSize.M: 160,
    CardSize.L: 176,
    CardSize.XL: 192,
}
FULL_WIDTH = POSTER_WIDTHS[CardSize.XL]

# Settings' segmented control: one short label per size, smallest first.
CARD_SIZE_LABELS: dict[CardSize, str] = {
    CardSize.XS: "XS",
    CardSize.S: "S",
    CardSize.M: "M",
    CardSize.L: "L",
    CardSize.XL: "XL",
}
# Shown through t() (Settings → Looks).
CARD_SIZE_LABEL = N_("Card size")
CARD_SIZE_TIP = N_(
    "How big the cards are on the calendar, Movies, Discover, My shows and in search results. "
    "Smaller cards fit more on the screen."
)

# Font sizes never go below these (unless the original size is smaller already).
SMALL_TEXT_MIN = 10  # muted lines: year / rating, status, info line
TEXT_MIN = 11  # buttons, switches, chips
TITLE_MIN = 12  # poster card titles
LINE_FACTOR = 1.4  # a text row's height per unit of font size (room for ascenders/descenders)


def scale_factor(size: CardSize) -> float:
    """1.0 for XL, smaller for the smaller sizes (128 / 192 for XS)."""
    return POSTER_WIDTHS[size] / FULL_WIDTH


def scaled_font_size(base: int, factor: float, minimum: int) -> int:
    """`base` scaled by `factor`, never above `base` and never below `minimum` (nor below
    `base` itself when that is smaller than `minimum`: such text is kept as it was)."""
    floor = min(base, minimum)
    return max(floor, min(base, round(base * factor)))


def line_height(font_size: int) -> int:
    """The height a single line of text at `font_size` needs."""
    return math.ceil(font_size * LINE_FACTOR)


def size_from_label(label: str) -> CardSize:
    """The size for a segmented-control label ("M" → CardSize.M)."""
    for size, shown in CARD_SIZE_LABELS.items():
        if shown == label:
            return size
    raise ValueError(f"unknown card size label: {label!r}")


@dataclass(frozen=True, slots=True)
class CardScale:
    """One card size's arithmetic. `factor` 1.0 = the original (XL) cards."""

    factor: float = 1.0

    @classmethod
    def of(cls, size: CardSize) -> CardScale:
        return cls(scale_factor(size))

    def px(self, units: float, *, minimum: int = 0) -> int:
        """`units` (CTk units at XL) at this size, rounded, at least `minimum`."""
        return max(minimum, round(units * self.factor))

    def font(self, base: int, minimum: int = TEXT_MIN) -> int:
        """A font size at this size (see `scaled_font_size`)."""
        return scaled_font_size(base, self.factor, minimum)

    def text_height(self, units: float, font_size: int) -> int:
        """A text row's height: `units` scaled, but tall enough for a line at `font_size`."""
        return max(self.px(units), line_height(font_size)) if self.factor < 1 else round(units)


FULL = CardScale()


def grid_item_width(width: int, scale: CardScale) -> int:
    """A `FlexGrid`'s item width for cards `width` units wide at XL."""
    return scale.px(width)
