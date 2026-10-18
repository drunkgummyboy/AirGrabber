"""ImageCache: memory LRU over disk over HTTP, exercised against httpx.MockTransport."""

from __future__ import annotations

import asyncio
import hashlib
import io
import os
import tempfile
import unittest
import warnings
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from app.services.images import MAX_DOWNLOAD_BYTES, ImageCache

POSTER = "https://static.tvmaze.com/uploads/images/medium_portrait/1/1.jpg"
SIZE = (102, 150)


def encode(
    mode: str = "RGB", size: tuple[int, int] = (210, 295), fmt: str = "JPEG", **params: Any
) -> bytes:
    if mode == "P":
        rgb = Image.new("RGB", size, (200, 30, 30))
        image = rgb.convert("P", palette=Image.Palette.ADAPTIVE, colors=16)
    else:
        image = Image.new(mode, size)
    buffer = io.BytesIO()
    image.save(buffer, fmt, **params)
    return buffer.getvalue()


def url_for(n: int, suffix: str = ".jpg") -> str:
    return f"https://image.tmdb.org/t/p/w342/poster{n}{suffix}"


@dataclass(frozen=True)
class Tint:
    """A finish step: the crop's size, all in one colour."""

    color: tuple[int, int, int]

    def __call__(self, image: Image.Image) -> Image.Image:
        return Image.new("RGB", image.size, self.color)


@dataclass(frozen=True)
class Broken:
    def __call__(self, image: Image.Image) -> Image.Image:
        raise RuntimeError("bug in a finish step")


class FakeServer:
    """MockTransport handler serving fixed bodies (404 otherwise) and recording every request."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, bytes]] = {}
        self.requests: list[str] = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.release.set()

    def serve(self, url: str, body: bytes, status: int = 200) -> None:
        self.routes[url] = (status, body)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(str(request.url))
        self.started.set()
        await self.release.wait()
        status, body = self.routes.get(str(request.url), (404, b"not found"))
        return httpx.Response(status, content=body)


class ImageCacheTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        temp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name) / "data" / "posters"
        self.server = FakeServer()
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.server.handle))
        self.addAsyncCleanup(self.http.aclose)

    def make_cache(self, memory_items: int = 300) -> ImageCache:
        return ImageCache(self.http, self.directory, memory_items=memory_items)

    def cache_file(self, url: str, suffix: str = ".jpg") -> Path:
        return self.directory / (hashlib.sha1(url.encode()).hexdigest() + suffix)

    def stored_files(self) -> list[str]:
        return sorted(os.listdir(self.directory))

    async def test_returns_exact_size_in_rgb(self) -> None:
        self.server.serve(POSTER, encode(size=(400, 120)))  # far wider than the target
        image = await self.make_cache().get(POSTER, SIZE)
        assert image is not None
        self.assertEqual(image.size, SIZE)
        self.assertEqual(image.mode, "RGB")
        self.assertEqual(self.stored_files(), [self.cache_file(POSTER).name])

    async def test_second_get_is_served_from_memory(self) -> None:
        self.server.serve(POSTER, encode())
        cache = self.make_cache()
        first = await cache.get(POSTER, SIZE)
        second = await cache.get(POSTER, SIZE)
        self.assertIsNotNone(first)
        self.assertIs(second, first)
        self.assertEqual(len(self.server.requests), 1)

    async def test_other_size_reuses_the_download(self) -> None:
        self.server.serve(POSTER, encode())
        cache = self.make_cache()
        large = await cache.get(POSTER, SIZE)
        small = await cache.get(POSTER, (51, 75))
        assert large is not None and small is not None
        self.assertEqual((large.size, small.size), (SIZE, (51, 75)))
        self.assertEqual(len(self.server.requests), 1)

    async def test_new_instance_is_served_from_disk(self) -> None:
        self.server.serve(POSTER, encode())
        self.assertIsNotNone(await self.make_cache().get(POSTER, SIZE))
        self.server.requests.clear()
        image = await self.make_cache().get(POSTER, SIZE)
        assert image is not None
        self.assertEqual(image.size, SIZE)
        self.assertEqual(self.server.requests, [])

    async def test_disk_hit_refreshes_mtime(self) -> None:
        self.server.serve(POSTER, encode())
        await self.make_cache().get(POSTER, SIZE)
        path = self.cache_file(POSTER)
        os.utime(path, (1_000_000, 1_000_000))
        await self.make_cache().get(POSTER, SIZE)
        self.assertGreater(path.stat().st_mtime, 1_000_000)

    async def test_concurrent_gets_share_one_download(self) -> None:
        self.server.serve(POSTER, encode())
        self.server.release.clear()
        cache = self.make_cache()
        sizes = [SIZE, SIZE, (51, 75), SIZE, (34, 50)]
        gathered = asyncio.gather(*(cache.get(POSTER, size) for size in sizes))
        await self.server.started.wait()
        await asyncio.sleep(0.1)  # time for any duplicate request to reach the server too
        self.server.release.set()
        images = await gathered
        self.assertEqual([image.size if image else None for image in images], sizes)
        self.assertIs(images[0], images[1])
        self.assertEqual(len(self.server.requests), 1)

    async def test_cancelled_waiter_does_not_break_the_other(self) -> None:
        self.server.serve(POSTER, encode())
        self.server.release.clear()
        cache = self.make_cache()
        cancelled = asyncio.create_task(cache.get(POSTER, SIZE))
        survivor = asyncio.create_task(cache.get(POSTER, (51, 75)))
        await self.server.started.wait()
        cancelled.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await cancelled
        self.server.release.set()
        image = await survivor
        assert image is not None
        self.assertEqual(image.size, (51, 75))
        self.assertEqual(len(self.server.requests), 1)
        self.assertTrue(self.cache_file(POSTER).exists())

    async def test_download_outlives_its_only_cancelled_waiter(self) -> None:
        self.server.serve(POSTER, encode())
        self.server.release.clear()
        cache = self.make_cache()
        waiter = asyncio.create_task(cache.get(POSTER, SIZE))
        await self.server.started.wait()
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.server.release.set()
        self.assertIsNotNone(await cache.get(POSTER, SIZE))
        self.assertEqual(len(self.server.requests), 1)

    async def test_http_error_returns_none(self) -> None:
        self.server.serve(POSTER, encode(), status=404)
        self.assertIsNone(await self.make_cache().get(POSTER, SIZE))
        self.assertEqual(self.stored_files(), [])

    async def test_transport_error_returns_none(self) -> None:
        def refuse(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as http:
            self.assertIsNone(await ImageCache(http, self.directory).get(POSTER, SIZE))

    async def test_non_image_body_returns_none_and_stores_nothing(self) -> None:
        self.server.serve(POSTER, b"<html>definitely not a poster</html>")
        self.assertIsNone(await self.make_cache().get(POSTER, SIZE))
        self.assertEqual(self.stored_files(), [])

    async def test_oversized_body_is_rejected(self) -> None:
        too_big = b"\0" * (MAX_DOWNLOAD_BYTES + 1)

        async def chunks() -> AsyncIterator[bytes]:  # no Content-Length: caught while reading
            for start in range(0, len(too_big), 1024 * 1024):
                yield too_big[start : start + 1024 * 1024]

        def respond(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("streamed.jpg"):
                return httpx.Response(200, content=chunks())
            return httpx.Response(200, content=too_big)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            cache = ImageCache(http, self.directory)
            for name in ("declared.jpg", "streamed.jpg"):
                with self.subTest(name):
                    self.assertIsNone(await cache.get(f"https://example.test/{name}", SIZE))
        self.assertEqual(self.stored_files(), [])

    async def test_missing_url_returns_none_without_request(self) -> None:
        cache = self.make_cache()
        self.assertIsNone(await cache.get(None, SIZE))
        self.assertIsNone(await cache.get("", SIZE))
        self.assertEqual(self.server.requests, [])

    async def test_memory_is_lru_bounded(self) -> None:
        a, b, c = url_for(1), url_for(2), url_for(3)
        for url in (a, b, c):
            self.server.serve(url, encode())
        cache = self.make_cache(memory_items=2)
        first_a = await cache.get(a, SIZE)
        first_b = await cache.get(b, SIZE)
        self.assertIs(await cache.get(a, SIZE), first_a)  # hit: a becomes most recent
        await cache.get(c, SIZE)  # evicts b, the least recently used
        self.assertIs(await cache.get(a, SIZE), first_a)
        second_b = await cache.get(b, SIZE)
        self.assertIsNotNone(second_b)
        self.assertIsNot(second_b, first_b)
        self.assertEqual(len(self.server.requests), 3)  # b came back from disk

    async def test_finish_runs_on_the_crop(self) -> None:
        self.server.serve(POSTER, encode(size=(400, 120)))
        image = await self.make_cache().get(POSTER, SIZE, finish=Tint((10, 20, 30)))
        assert image is not None
        self.assertEqual(image.size, SIZE)
        self.assertEqual(image.getpixel((0, 0)), (10, 20, 30))

    async def test_finished_images_are_cached_per_finish(self) -> None:
        self.server.serve(POSTER, encode())
        cache = self.make_cache()
        tinted = await cache.get(POSTER, SIZE, finish=Tint((1, 2, 3)))
        # An equal finish step is the same cache entry; another one or none is not.
        self.assertIs(await cache.get(POSTER, SIZE, finish=Tint((1, 2, 3))), tinted)
        other = await cache.get(POSTER, SIZE, finish=Tint((4, 5, 6)))
        plain = await cache.get(POSTER, SIZE)
        assert other is not None and plain is not None
        self.assertEqual(other.getpixel((0, 0)), (4, 5, 6))
        self.assertEqual(plain.getpixel((0, 0)), (0, 0, 0))  # encode() is black
        self.assertEqual(len(self.server.requests), 1)

    async def test_failing_finish_returns_none(self) -> None:
        self.server.serve(POSTER, encode())
        self.assertIsNone(await self.make_cache().get(POSTER, SIZE, finish=Broken()))

    async def test_corrupt_disk_file_is_downloaded_again(self) -> None:
        body = encode()
        self.server.serve(POSTER, body)
        self.directory.mkdir(parents=True)
        path = self.cache_file(POSTER)
        path.write_bytes(b"\xff\xd8\xff garbage")
        image = await self.make_cache().get(POSTER, SIZE)
        assert image is not None
        self.assertEqual(image.size, SIZE)
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(path.read_bytes(), body)

    async def test_other_source_modes_are_converted(self) -> None:
        sources = {
            "P": encode("P", fmt="PNG"),
            "L": encode("L"),
            "CMYK": encode("CMYK"),
        }
        cache = self.make_cache()
        for n, (mode, body) in enumerate(sources.items()):
            with self.subTest(mode):
                url = url_for(n, ".png")
                self.server.serve(url, body)
                image = await cache.get(url, SIZE)
                assert image is not None
                self.assertEqual((image.size, image.mode), (SIZE, "RGB"))

    async def test_transparency_is_kept_as_rgba(self) -> None:
        palette_alpha = encode("P", fmt="PNG", transparency=bytes(range(0, 256, 16)))
        rgba = encode("RGBA", fmt="PNG")
        cache = self.make_cache()
        for n, body in enumerate((palette_alpha, rgba)):
            url = url_for(n, ".png")
            self.server.serve(url, body)
            with warnings.catch_warnings():
                warnings.simplefilter("error")
                image = await cache.get(url, SIZE)
            assert image is not None
            self.assertEqual((image.size, image.mode), (SIZE, "RGBA"))

    async def test_file_suffix_follows_the_url(self) -> None:
        cases = {url_for(1, ".png"): ".png", url_for(2, ".WEBP"): ".webp", url_for(3, ""): ".jpg"}
        cache = self.make_cache()
        for url in cases:
            self.server.serve(url, encode())
            await cache.get(url, SIZE)
        expected = sorted(self.cache_file(url, suffix).name for url, suffix in cases.items())
        self.assertEqual(self.stored_files(), expected)

    async def test_prune_deletes_oldest_first(self) -> None:
        cache = self.make_cache()
        mtimes = {"a.jpg": 4_000_000, "b.jpg": 1_000_000, "c.jpg": 3_000_000, "d.jpg": 2_000_000}
        for name, mtime in mtimes.items():
            path = self.directory / name
            path.write_bytes(b"x" * 100)
            os.utime(path, (mtime, mtime))
        self.assertEqual(await cache.prune(max_bytes=400), 0)
        self.assertEqual(await cache.prune(max_bytes=250), 2)
        self.assertEqual(self.stored_files(), ["a.jpg", "c.jpg"])


if __name__ == "__main__":
    unittest.main()
