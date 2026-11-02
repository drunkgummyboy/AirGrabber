"""Posters for the executor (`PosterFetcher`, core/executor.py) from AirGrabber's image cache.

MediaForge's TMDB client fetched and cached images itself; here a TMDB image path and size
become TMDB's image URL, read through `ImageCache.raw` (AirGrabber's shared `posters` disk
cache, its download de-duplication and size limit). No key is needed for TMDB's images.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.forge.clients.tmdb import TmdbError
from app.models.domain import TMDB_IMAGE_BASE

if TYPE_CHECKING:
    from app.services.images import ImageCache

__all__ = ["CachedPosters", "image_url"]


def image_url(path: str, size: str) -> str:
    """TMDB's URL for the image at `path` ("/abc.jpg") in `size` ("w780")."""
    return f"{TMDB_IMAGE_BASE}{size}/{path.lstrip('/')}"


class CachedPosters:
    """The executor's `PosterFetcher` on AirGrabber's `ImageCache`."""

    def __init__(self, images: ImageCache) -> None:
        self._images = images

    async def image(self, path: str, size: str) -> bytes:
        """The image's original bytes; `TmdbError` when there are none (no path, offline, not at
        TMDB, too large or not a picture). The message holds neither the URL nor the path."""
        data = await self._images.raw(image_url(path, size)) if path.strip("/") else None
        if data is None:
            raise TmdbError("Poster not available")
        return data
