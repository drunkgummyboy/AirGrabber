"""Posters and backdrops for the Inbox's bands, through AirGrabber's image cache.

The pictures come from `app.services.images.ImageCache` (its memory LRU, `posters` disk cache and
de-duplicated downloads): the poster as the cache's cover crop of the band's frame, the backdrop
as a soft wash over the band's colour (`Wash`, a `Finish` step, for a strip of fixed width at the
band's right end). The wash's colour is `theme.pick(look.SURFACE)` at request time and part of
the key, so after a light/dark switch the band asks again and gets the other look's wash.

`BandImages` turns them into Tk images at physical size (sizes are asked in CTk units and scaled by
`ctk.ScalingTracker.get_widget_scaling`) and keeps them in a small LRU, on the loop thread: Tk
images must never be made or freed elsewhere. A picture that can't be had (offline, none at
TMDB) is simply None: the band draws without, and doesn't ask for that one again.
"""

from __future__ import annotations

import logging
import tkinter as tk
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

import customtkinter as ctk
from PIL import Image, ImageOps, ImageTk

from app.forge.ui.look import Pair, forge_look
from app.models.domain import TMDB_IMAGE_BASE
from app.ui import theme

if TYPE_CHECKING:
    from app.services.images import ImageCache

log = logging.getLogger(__name__)

POSTER_SIZE = "w185"
BACKDROP_SIZE = "w780"
# How strongly a backdrop shows over the band's colour: low enough that the band's text and quiet
# text stay readable on any picture, in light and dark (see the readability test in test_images).
BACKDROP_STRENGTH = 0.22
BACKDROP_FADE = 0.4  # the strip's left part, where the picture fades in from the band's colour
FADED_STRENGTH = 0.45  # a done band's poster: this much of the band's colour over the picture
# (kind, TMDB path, physical width, physical height, wash colour or "")
type Key = tuple[str, str, int, int, str]


def tmdb_image_url(path: str, size: str) -> str:
    """TMDB's URL for the image at `path` ("/abc.jpg") in `size` ("w780")."""
    return f"{TMDB_IMAGE_BASE}{size}/{path.lstrip('/')}"


@dataclass(frozen=True, slots=True)
class Wash:
    """`ImageCache`'s finish step for a backdrop strip: the picture as a soft wash over `fade_to`
    (the band's colour, "#RRGGBB"): none at the left edge, so the strip joins the band seamlessly,
    rising to BACKDROP_STRENGTH over its first BACKDROP_FADE."""

    fade_to: str

    def __call__(self, image: Image.Image, /) -> Image.Image:
        picture = image.convert("RGB")
        width, height = picture.size
        solid = Image.new("RGB", (width, height), self.fade_to)
        ramp = Image.new("L", (width, 1))
        stop = max(1, round(width * BACKDROP_FADE))
        ramp.putdata([round(255 * BACKDROP_STRENGTH * min(1.0, x / stop)) for x in range(width)])
        return Image.composite(picture, solid, ramp.resize((width, height)))


@dataclass(frozen=True, slots=True)
class Fade:
    """`ImageCache`'s finish step for a done band's poster: the picture faded towards `fade_to`
    (the faded band's colour, "#RRGGBB") by FADED_STRENGTH."""

    fade_to: str

    def __call__(self, image: Image.Image, /) -> Image.Image:
        picture = image.convert("RGB")
        solid = Image.new("RGB", picture.size, self.fade_to)
        return Image.blend(picture, solid, FADED_STRENGTH)


def cover(image: Image.Image, width: int, height: int) -> Image.Image:
    """Scale to fill width × height and crop the middle (CSS's `object-fit: cover`), as the
    image cache does."""
    return ImageOps.fit(image.convert("RGB"), (width, height), Image.Resampling.LANCZOS)


def render_poster(image: Image.Image, width: int, height: int) -> Image.Image:
    """What a band's poster looks like (the pure part, for tests and previews)."""
    return cover(image, width, height)


def render_backdrop(image: Image.Image, width: int, height: int, fade_to: str) -> Image.Image:
    """What a band's backdrop strip looks like (the pure part, for tests and previews)."""
    return Wash(fade_to)(cover(image, width, height))


class BandImages:
    """The bands' Tk images over `ImageCache`; use from the loop thread only.

    `widget` is the one the pictures are shown in: its scaling turns CTk units into physical
    pixels. `ground` is the band colour a backdrop is washed over, picked at request time.
    """

    def __init__(
        self, cache: ImageCache, widget: tk.Misc, *, ground: Pair | None = None, limit: int = 200
    ) -> None:
        self._cache = cache
        self._widget = widget
        self._ground = ground if ground is not None else forge_look().SURFACE
        self._limit = limit
        self._photos: OrderedDict[Key, ImageTk.PhotoImage] = OrderedDict()
        self._missing: set[Key] = set()

    def poster_key(
        self, path: str | None, width: int, height: int, *, faded: str | None = None
    ) -> Key:
        """`faded`: the faded band's colour a done band's poster fades towards."""
        w, h = self._physical(width, height)
        return ("poster", path or "", w, h, faded or "")

    def backdrop_key(self, path: str | None, width: int, height: int) -> Key:
        w, h = self._physical(width, height)
        return ("backdrop", path or "", w, h, theme.pick(self._ground))

    def cached_poster(
        self, path: str | None, width: int, height: int, *, faded: str | None = None
    ) -> ImageTk.PhotoImage | None:
        return self._get(self.poster_key(path, width, height, faded=faded))

    def cached_backdrop(
        self, path: str | None, width: int, height: int
    ) -> ImageTk.PhotoImage | None:
        return self._get(self.backdrop_key(path, width, height))

    def known_missing(self, key: Key) -> bool:
        return key in self._missing

    async def poster(
        self, path: str | None, width: int, height: int, *, faded: str | None = None
    ) -> ImageTk.PhotoImage | None:
        """The poster at `path`, `width` × `height` CTk units (faded towards `faded`), or None."""
        return await self._load(self.poster_key(path, width, height, faded=faded), POSTER_SIZE)

    async def backdrop(
        self, path: str | None, width: int, height: int
    ) -> ImageTk.PhotoImage | None:
        """The backdrop strip at `path`, `width` × `height` CTk units, washed over the band's
        colour in the current look, or None."""
        return await self._load(self.backdrop_key(path, width, height), BACKDROP_SIZE)

    def clear(self) -> None:
        """Let every Tk image go (on the loop thread), e.g. when the view closes."""
        self._photos.clear()
        self._missing.clear()

    def _physical(self, width: int, height: int) -> tuple[int, int]:
        scale = ctk.ScalingTracker.get_widget_scaling(self._widget)
        return max(1, round(width * scale)), max(1, round(height * scale))

    def _get(self, key: Key) -> ImageTk.PhotoImage | None:
        photo = self._photos.get(key)
        if photo is not None:
            self._photos.move_to_end(key)
        return photo

    async def _load(self, key: Key, size: str) -> ImageTk.PhotoImage | None:
        cached = self._get(key)
        if cached is not None or not key[1] or key in self._missing:
            return cached
        kind, path, width, height, fade_to = key
        finish: Wash | Fade | None = None
        if fade_to:
            finish = Wash(fade_to) if kind == "backdrop" else Fade(fade_to)
        image = await self._cache.get(tmdb_image_url(path, size), (width, height), finish=finish)
        if image is None:
            log.debug("No %s for a band", kind)
            self._missing.add(key)
            return None
        if not self._widget.winfo_exists():
            return None
        existing = self._get(key)  # a concurrent caller for the same picture finished first
        if existing is not None:
            return existing
        photo = ImageTk.PhotoImage(image, master=self._widget)
        self._photos[key] = photo
        while len(self._photos) > self._limit:
            self._photos.popitem(last=False)
        return photo
