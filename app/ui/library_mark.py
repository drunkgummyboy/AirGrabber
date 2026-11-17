"""The library mark: a small drawn shelf of books on a card's cover, top-right, while its film or
show is in the user's Plex libraries or folder library (only while there is a library: without
one nothing counts as had). On every poster card that can tell: Movies, the watchlist shelf, My
shows, Discover and the search results window. Its tip says where: "In Plex", beside Plex
"Downloaded…" for what only the user's own folders have (`from_folders`), "In your library" with
the folder library alone (app/ui/library_words.py).

Just the icon, no box behind it (the owner's wish): white books with a soft dark shadow, drawn on
the cover's canvas (`PosterCard.set_corner_icon`) so they read on bright and dark covers alike.
Drawn with Pillow at the card's physical size, once per size.
"""

from __future__ import annotations

import math
from functools import cache
from typing import TYPE_CHECKING

from PIL import Image, ImageDraw, ImageFilter

from app.ui import library_words, theme

if TYPE_CHECKING:
    from app.ui.poster_card import PosterCard

_DRAWN = 96  # the books are drawn this large, then scaled down
_INK = 0.80  # the books' share of the picture (the rest is room for the shadow)


@cache
def books(colour: str) -> Image.Image:
    """Three books on a shelf, the last one leaning, in `colour` ("#RRGGBB") on transparent."""
    size = _DRAWN
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    rgb = tuple(int(colour[i : i + 2], 16) for i in (1, 3, 5))
    ink = (*rgb, 255)
    clear = (0, 0, 0, 0)
    shelf_top = 80
    # Two upright books: their spines with a band near the top cut out.
    for left, top in ((14, 18), (32, 28)):
        draw.rounded_rectangle((left, top, left + 15, shelf_top), radius=3, fill=ink)
        draw.rectangle((left + 3, top + 8, left + 12, top + 11), fill=clear)
    # The third leans against the second, pivoting on its foot.
    angle = math.radians(20)
    foot_x, width, height = 50.0, 15.0, 52.0

    def turn(x: float, y: float) -> tuple[float, float]:
        return (
            foot_x + x * math.cos(angle) + y * math.sin(angle),
            shelf_top - y * math.cos(angle) + x * math.sin(angle),
        )

    draw.polygon([turn(0, 0), turn(width, 0), turn(width, height), turn(0, height)], fill=ink)
    band = [turn(3, height - 9), turn(width - 3, height - 9), turn(width - 3, height - 12)]
    draw.polygon([*band, turn(3, height - 12)], fill=clear)
    # The shelf.
    draw.rounded_rectangle((8, shelf_top + 2, size - 8, shelf_top + 8), radius=3, fill=ink)
    return image


@cache
def mark_image(side: int) -> Image.Image:
    """The mark `side` physical pixels square: white books on a soft shadow, see-through around."""
    side = max(8, side)
    inner = max(6, round(side * _INK))
    picture = books(theme.ON_COLOR).resize((inner, inner), Image.Resampling.LANCZOS)
    at = ((side - inner) // 2, (side - inner) // 2)
    alpha = Image.new("L", (side, side), 0)
    alpha.paste(picture.getchannel("A"), (at[0], at[1] + max(1, side // 24)))
    shadow = alpha.filter(ImageFilter.GaussianBlur(max(1.0, side / 14)))
    shadow = shadow.point(lambda value: min(255, round(value * 1.6)))
    result = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    result.putalpha(shadow)
    result.alpha_composite(picture, at)
    return result


class LibraryMark:
    """The mark on one card (`PosterCard.set_corner_icon`); `destroy` takes it away.
    `from_folders`: beside Plex, only the user's own folders have it (its tip says so)."""

    def __init__(self, card: PosterCard, *, from_folders: bool = False) -> None:
        self.card = card
        self.from_folders = from_folders
        self.text = library_words.have_tip(library_words.have_source(from_folders=from_folders))
        card.set_corner_icon(mark_image, self.text)

    def destroy(self) -> None:
        if self.card.winfo_exists():
            self.card.set_corner_icon(None)


def library_mark(card: PosterCard, *, from_folders: bool = False) -> LibraryMark:
    """Put the mark on `card`'s cover (`from_folders`: as LibraryMark's)."""
    return LibraryMark(card, from_folders=from_folders)
