"""Posters and backdrops for the bands (app/forge/ui/images.py) and the executor's posters
(app/forge/clients/posters.py): through AirGrabber's ImageCache, fetched once, never an error,
and the backdrop keeps the band's text readable in both looks of the copper Forge window."""

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
    BACKDROP_SIZE,
    FADED_STRENGTH,
    POSTER_SIZE,
    BandImages,
    Fade,
    Wash,
    render_backdrop,
)
from app.forge.ui.images import render_poster
from app.forge.ui.look import forge_look
from app.services.images import ImageCache
from app.ui import theme
from tests.forge.base import ForgeAsyncTestCase, ForgeTestCase
from tests.test_accent import contrast

LIGHT, DARK = 0, 1


def picture(color: tuple[int, int, int], size: tuple[int, int] = (300, 170)) -> Image.Image:
    return Image.new("RGB", size, color)


def encoded(color: tuple[int, int, int], size: tuple[int, int] = (300, 170)) -> bytes:
    out = io.BytesIO()
    picture(color, size).save(out, format="PNG")
    return out.getvalue()


def hex_of(pixel: object) -> str:
    assert isinstance(pixel, tuple)
    red, green, blue = pixel[:3]
    return f"#{red:02X}{green:02X}{blue:02X}"


class RenderTests(ForgeTestCase):
    def test_backdrop_fades_into_the_band_colour(self) -> None:
        # A strip for the band's right end: the band's own colour at its left edge, then the
        # picture as a soft wash.
        image = render_backdrop(picture((200, 30, 30)), 240, 64, fade_to="#242424")
        self.assertEqual(image.size, (240, 64))
        left = image.getpixel((0, 32))
        right = image.getpixel((239, 32))
        assert isinstance(left, tuple) and isinstance(right, tuple)
        self.assertTrue(all(abs(c - 0x24) <= 2 for c in left[:3]))  # joins the band seamlessly
        self.assertTrue(right[0] > 60 and right[0] > right[1] + 30)  # the picture shows

    def test_band_text_stays_readable_over_any_backdrop(self) -> None:
        # MediaForge's review: in light mode the band's dark text sat on a darkened picture
        # (contrast 1.8-3.5). Now the band is the copper look's SURFACE, its text theme.TEXT.
        look = forge_look()
        shades = ((0, 0, 0), (255, 255, 255), (128, 128, 128), (255, 220, 0), (0, 0, 160))
        for name, half in (("light", LIGHT), ("dark", DARK)):
            surface, text, muted = look.SURFACE[half], theme.TEXT[half], theme.TEXT_MUTED[half]
            for shade in shades:
                strip = render_backdrop(picture(shade), 400, 60, fade_to=surface)
                for x in range(0, 400, 8):
                    with self.subTest(look=name, shade=shade, x=x):
                        colour = hex_of(strip.getpixel((x, 30)))
                        self.assertGreaterEqual(contrast(text, colour), 4.5)
                        self.assertGreaterEqual(contrast(muted, colour), 3)

    def test_poster_is_cropped_to_its_frame(self) -> None:
        image = render_poster(picture((10, 120, 200), (500, 300)), 34, 50)
        self.assertEqual(image.size, (34, 50))

    def test_wash_is_a_finish_step_keyed_by_value(self) -> None:
        self.assertEqual(Wash("#242424"), Wash("#242424"))
        self.assertEqual(hash(Wash("#242424")), hash(Wash("#242424")))
        self.assertNotEqual(Wash("#242424"), Wash("#FFFFFF"))
        source = picture((200, 30, 30), (40, 10))
        washed = Wash("#FFFFFF")(source)
        self.assertEqual(source.getpixel((0, 0)), (200, 30, 30))  # its argument left alone
        self.assertEqual(washed.size, (40, 10))

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
        self.serve("/b.jpg", BACKDROP_SIZE, (40, 90, 160))
        self.serve("/p.jpg", POSTER_SIZE, (40, 90, 160))
        band = BandImages(self.cache, self.root)
        self.assertIsNone(band.cached_backdrop("/b.jpg", 240, 64))
        first = await band.backdrop("/b.jpg", 240, 64)
        second = await band.backdrop("/b.jpg", 240, 64)
        assert first is not None
        self.assertIs(first, second)
        self.assertIs(band.cached_backdrop("/b.jpg", 240, 64), first)
        self.assertEqual((first.width(), first.height()), (360, 96))  # 240 x 64 units at 150 %
        poster = await band.poster("/p.jpg", 34, 50)
        assert poster is not None
        self.assertEqual((poster.width(), poster.height()), (51, 75))
        self.assertEqual(
            self.server.requests,
            [
                "https://image.tmdb.org/t/p/w780/b.jpg",
                "https://image.tmdb.org/t/p/w185/p.jpg",
            ],
        )

    async def test_wash_follows_the_look_at_request_time(self) -> None:
        self.serve("/b.jpg", BACKDROP_SIZE, (200, 30, 30))
        surface = forge_look().SURFACE
        band = BandImages(self.cache, self.root)
        dark_key = band.backdrop_key("/b.jpg", 240, 64)
        self.assertEqual(dark_key[4], surface[DARK])
        dark = await band.backdrop("/b.jpg", 240, 64)
        self.mode = "light"
        self.assertIsNone(band.cached_backdrop("/b.jpg", 240, 64))  # asks again after a switch
        self.assertEqual(band.backdrop_key("/b.jpg", 240, 64)[4], surface[LIGHT])
        light = await band.backdrop("/b.jpg", 240, 64)
        assert dark is not None and light is not None
        self.assertIsNot(dark, light)
        self.assertEqual(len(self.server.requests), 1)  # the download is shared
        washed = await self.cache.get(
            images.tmdb_image_url("/b.jpg", BACKDROP_SIZE), (360, 96), finish=Wash(surface[LIGHT])
        )
        assert washed is not None
        self.assertEqual(hex_of(washed.getpixel((0, 48))), surface[LIGHT].upper())

    async def test_missing_image_gives_none_and_is_not_asked_again(self) -> None:
        band = BandImages(self.cache, self.root)
        self.assertIsNone(await band.poster(None, 34, 50))
        self.assertEqual(self.server.requests, [])
        self.assertIsNone(await band.backdrop("/gone.jpg", 240, 64))
        self.assertTrue(band.known_missing(band.backdrop_key("/gone.jpg", 240, 64)))
        self.assertIsNone(await band.backdrop("/gone.jpg", 240, 64))
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
