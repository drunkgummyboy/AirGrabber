"""Posters for the bands (app/forge/ui/images.py; a card's picture: test_cards.py) and the
executor's posters (app/forge/clients/posters.py): through AirGrabber's ImageCache, fetched once,
never an error."""

from __future__ import annotations

import gc
import io
import tkinter as tk
from pathlib import Path
from unittest import mock

import customtkinter as ctk
import httpx
from PIL import Image

from app.forge.clients.posters import CachedPosters, image_url
from app.forge.clients.tmdb import TmdbError
from app.forge.ui import images
from app.forge.ui.images import (
    FADED_STRENGTH,
    POSTER_SIZE,
    BandImages,
    Fade,
)
from app.forge.ui.images import render_poster
from app.services.images import ImageCache
from app.ui import theme
from tests.forge.base import ForgeAsyncTestCase, ForgeTestCase


def picture(color: tuple[int, int, int], size: tuple[int, int] = (300, 170)) -> Image.Image:
    return Image.new("RGB", size, color)


def encoded(color: tuple[int, int, int], size: tuple[int, int] = (300, 170)) -> bytes:
    out = io.BytesIO()
    picture(color, size).save(out, format="PNG")
    return out.getvalue()


class RenderTests(ForgeTestCase):
    def test_poster_is_cropped_to_its_frame(self) -> None:
        image = render_poster(picture((10, 120, 200), (500, 300)), 34, 50)
        self.assertEqual(image.size, (34, 50))

    def test_a_done_bands_poster_fades_towards_its_colour(self) -> None:
        self.assertEqual(Fade("#202020"), Fade("#202020"))  # a finish step keyed by value
        faded = Fade("#000000")(picture((200, 100, 0), (4, 4)))
        red, green, blue = faded.getpixel((1, 1))  # type: ignore[misc]
        self.assertEqual(
            (red, green, blue),
            tuple(round(c * (1 - FADED_STRENGTH)) for c in (200, 100, 0)),
        )


class FakeServer:
    """MockTransport handler serving fixed bodies (404 otherwise), recording the urls asked."""

    def __init__(self) -> None:
        self.routes: dict[str, bytes] = {}
        self.requests: list[str] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        self.requests.append(url)
        body = self.routes.get(url)
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404)


class CacheCase(ForgeAsyncTestCase):
    async def asyncSetUp(self) -> None:
        self.server = FakeServer()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.server.handle))
        self.addAsyncCleanup(self.http.aclose)
        self.cache = ImageCache(self.http, Path(self.tmp) / "posters")


class CachedPostersTests(CacheCase):
    async def test_poster_bytes_come_through_the_cache(self) -> None:
        body = encoded((40, 90, 160))
        self.server.routes["https://image.tmdb.org/t/p/w780/abc.jpg"] = body
        posters = CachedPosters(self.cache)
        self.assertEqual(await posters.image("/abc.jpg", "w780"), body)
        self.assertEqual(await posters.image("/abc.jpg", "w780"), body)  # from disk now
        self.assertEqual(self.server.requests, ["https://image.tmdb.org/t/p/w780/abc.jpg"])

    async def test_nothing_raises_the_poster_error_without_the_url(self) -> None:
        posters = CachedPosters(self.cache)
        for path in ("/missing.jpg", "", "/"):
            with self.subTest(path=path):
                with self.assertRaises(TmdbError) as caught:
                    await posters.image(path, "w780")
                self.assertNotIn("http", str(caught.exception))
                self.assertNotIn("missing", str(caught.exception))
        self.assertEqual(self.server.requests, ["https://image.tmdb.org/t/p/w780/missing.jpg"])

    def test_image_url(self) -> None:
        self.assertEqual(image_url("/x.jpg", "w185"), "https://image.tmdb.org/t/p/w185/x.jpg")
        self.assertEqual(images.tmdb_image_url("/x.jpg", "w185"), image_url("/x.jpg", "w185"))


class BandImagesTests(CacheCase):
    def setUp(self) -> None:
        super().setUp()
        gc.collect()
        try:
            self.root = ctk.CTk()
        except tk.TclError as exc:  # no display
            self.skipTest(f"Tk unavailable: {exc}")
        self.root.withdraw()
        self.addCleanup(self._destroy_root)
        self.scale = 1.5
        self.enterContext(
            mock.patch.object(
                ctk.ScalingTracker, "get_widget_scaling", side_effect=lambda _w: self.scale
            )
        )
        self.mode = "dark"
        self.enterContext(
            mock.patch.object(theme, "is_dark", side_effect=lambda: self.mode == "dark")
        )

    def _destroy_root(self) -> None:
        for job in self.root.tk.splitlist(self.root.tk.call("after", "info")):
            self.root.after_cancel(job)
        self.root.destroy()
        gc.collect()

    def serve(self, path: str, size: str, color: tuple[int, int, int]) -> None:
        self.server.routes[f"https://image.tmdb.org/t/p/{size}{path}"] = encoded(color)

    async def test_images_are_fetched_once_and_cached_at_physical_size(self) -> None:
        self.serve("/p.jpg", POSTER_SIZE, (40, 90, 160))
        band = BandImages(self.cache, self.root)
        self.assertIsNone(band.cached_poster("/p.jpg", 34, 50))
        poster = await band.poster("/p.jpg", 34, 50)
        again = await band.poster("/p.jpg", 34, 50)
        assert poster is not None
        self.assertIs(poster, again)
        self.assertIs(band.cached_poster("/p.jpg", 34, 50), poster)
        self.assertEqual((poster.width(), poster.height()), (51, 75))  # at 150 %
        self.assertEqual(self.server.requests, ["https://image.tmdb.org/t/p/w185/p.jpg"])

    async def test_missing_image_gives_none_and_is_not_asked_again(self) -> None:
        band = BandImages(self.cache, self.root)
        self.assertIsNone(await band.poster(None, 34, 50))
        self.assertEqual(self.server.requests, [])
        self.assertIsNone(await band.poster("/gone.jpg", 34, 50))
        self.assertTrue(band.known_missing(band.poster_key("/gone.jpg", 34, 50)))
        self.assertIsNone(await band.poster("/gone.jpg", 34, 50))
        self.assertEqual(len(self.server.requests), 1)

    async def test_offline_gives_none(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as http:
            cache = ImageCache(http, Path(self.tmp) / "offline")
            self.assertIsNone(await BandImages(cache, self.root).poster("/p.jpg", 34, 50))

    async def test_lru_is_bounded(self) -> None:
        for n in range(3):
            self.serve(f"/p{n}.jpg", POSTER_SIZE, (10 * n, 90, 160))
        band = BandImages(self.cache, self.root, limit=2)
        for n in range(3):
            self.assertIsNotNone(await band.poster(f"/p{n}.jpg", 34, 50))
        self.assertIsNone(band.cached_poster("/p0.jpg", 34, 50))
        self.assertIsNotNone(band.cached_poster("/p2.jpg", 34, 50))
