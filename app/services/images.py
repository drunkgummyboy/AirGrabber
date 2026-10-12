"""Poster cache: decoded, resized images in memory over the raw downloaded bytes on disk."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import logging
import os
import tempfile
from collections import OrderedDict
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import httpx
from PIL import Image, ImageOps

log = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"})
_DEFAULT_SUFFIX = ".jpg"

_MemoryKey = tuple[str, tuple[int, int]]


class ImageCache:
    """Two-level image cache: an LRU of decoded, resized images over a directory of raw bytes.

    `get` looks in memory (keyed by url and size), then on disk, then downloads. Concurrent
    calls for one url, whatever sizes they ask for, share a single disk read or download.

    Thread safety: all file I/O and Pillow work runs in worker threads via `asyncio.to_thread`,
    but those functions are pure (bytes/path in, result out) and never see the cache's state.
    The LRU `OrderedDict` and the in-flight table are only ever touched on the event-loop
    thread, so this confinement is what makes the LRU thread-safe without locks. Use one
    instance from one event loop only.

    Returned images are shared between callers and must be treated as read-only.
    """

    def __init__(
        self,
        http: httpx.AsyncClient,
        directory: Path,
        *,
        memory_items: int = 300,
        max_concurrent: int = 6,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        self._http = http
        self._directory = directory
        self._memory_items = max(0, memory_items)
        self._memory: OrderedDict[_MemoryKey, Image.Image] = OrderedDict()
        self._inflight: dict[str, asyncio.Task[Image.Image | None]] = {}
        self._downloads = asyncio.Semaphore(max_concurrent)
        directory.mkdir(parents=True, exist_ok=True)

    async def get(self, url: str | None, size: tuple[int, int]) -> Image.Image | None:
        """The image at `url`, cropped to fill exactly `size` pixels, in RGB or RGBA mode.

        None without a url, or when the image cannot be downloaded or decoded (logged at DEBUG).
        Never raises for network or image problems; a non-positive size is a ValueError.
        """
        if not url:
            return None
        width, height = size
        if width < 1 or height < 1:
            raise ValueError(f"invalid image size {size!r}")
        key: _MemoryKey = (url, (width, height))
        cached = self._memory.get(key)
        if cached is not None:
            self._memory.move_to_end(key)
            return cached

        source = await self._source(url)
        if source is None:
            return None
        try:
            fitted = await asyncio.to_thread(_fit, source, key[1])
        except Exception as exc:
            log.debug("Could not resize image %s to %s: %r", url, key[1], exc)
            return None
        return self._remember(key, fitted)

    async def prune(self, max_bytes: int = 300 * 1024 * 1024) -> int:
        """Delete the least recently used files until the directory holds at most `max_bytes`.

        Returns the number of files deleted.
        """
        return await asyncio.to_thread(_prune, self._directory, max_bytes)

    async def _source(self, url: str) -> Image.Image | None:
        """The full-size decoded image, loaded once for every concurrent caller of this url."""
        task = self._inflight.get(url)
        if task is None:
            task = asyncio.create_task(self._load(url), name=f"image cache: {url}")
            self._inflight[url] = task
            task.add_done_callback(lambda done: self._finished(url, done))
        # A cancelled caller must not cancel the load the other callers are still awaiting.
        return await asyncio.shield(task)

    def _finished(self, url: str, task: asyncio.Task[Image.Image | None]) -> None:
        if self._inflight.get(url) is task:
            del self._inflight[url]

    def _remember(self, key: _MemoryKey, image: Image.Image) -> Image.Image:
        existing = self._memory.get(key)
        if existing is not None:  # a concurrent caller for the same size finished first
            self._memory.move_to_end(key)
            return existing
        self._memory[key] = image
        while len(self._memory) > self._memory_items:
            self._memory.popitem(last=False)
        return image

    async def _load(self, url: str) -> Image.Image | None:
        """Disk, then network. Never raises (except when cancelled): all callers may have gone,
        and an exception nobody retrieves would only be reported at garbage collection."""
        path = self._directory / _file_name(url)
        try:
            image = await asyncio.to_thread(_read_cached, path)
        except _CorruptFile as exc:
            log.debug("Deleted corrupt cached image for %s (%s); downloading it again", url, exc)
        except Exception as exc:
            log.debug("Could not read cached image for %s: %r", url, exc)
        else:
            if image is not None:
                return image
        return await self._download(url, path)

    async def _download(self, url: str, path: Path) -> Image.Image | None:
        try:
            data = await self._fetch(url)
        except Exception as exc:
            log.debug("Could not download image %s: %r", url, exc)
            return None
        if data is None:
            return None
        try:
            image = await asyncio.to_thread(_decode, data)
        except Exception as exc:
            log.debug("Downloaded data for %s is not a usable image: %r", url, exc)
            return None
        try:
            await asyncio.to_thread(_write_atomic, path, data)
        except Exception as exc:
            log.debug("Could not store image %s in the cache: %r", url, exc)
        return image

    async def _fetch(self, url: str) -> bytes | None:
        """The response body, or None for a non-200 status or a body over the size limit."""
        async with (
            self._downloads,
            self._http.stream("GET", url, follow_redirects=True) as response,
        ):
            if response.status_code != 200:
                log.debug("Could not download image %s: HTTP %d", url, response.status_code)
                return None
            declared = response.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > MAX_DOWNLOAD_BYTES:
                log.debug("Image %s is too large (%s bytes)", url, declared)
                return None
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body += chunk
                if len(body) > MAX_DOWNLOAD_BYTES:
                    log.debug("Image %s is too large (over %d bytes)", url, MAX_DOWNLOAD_BYTES)
                    return None
            return bytes(body)


class _CorruptFile(Exception):
    """A cache file that failed to decode. It has already been deleted."""


def _file_name(url: str) -> str:
    try:
        suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
    except ValueError:
        suffix = ""
    if suffix not in _SUFFIXES:
        suffix = _DEFAULT_SUFFIX
    digest = hashlib.sha1(url.encode("utf-8", "surrogatepass"), usedforsecurity=False)
    return digest.hexdigest() + suffix


# Everything below runs in worker threads: pure functions of their arguments.


def _decode(data: bytes) -> Image.Image:
    """Fully decode image bytes into a detached RGB (or, with transparency, RGBA) image.

    Raises on anything Pillow cannot decode.
    """
    with Image.open(io.BytesIO(data)) as opened:
        opened.load()
        # convert() always returns a new, fully loaded image that outlives `opened`.
        return opened.convert("RGBA" if opened.has_transparency_data else "RGB")


def _fit(source: Image.Image, size: tuple[int, int]) -> Image.Image:
    return ImageOps.fit(source, size, Image.Resampling.LANCZOS)



def _read_cached(path: Path) -> Image.Image | None:
    """Decode a cache file and mark it recently used; None when there is no such file.

    A file that fails to decode is deleted and reported as `_CorruptFile`.
    """
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    try:
        image = _decode(data)
    except Exception as exc:
        with contextlib.suppress(OSError):
            path.unlink()
        raise _CorruptFile(repr(exc)) from exc
    with contextlib.suppress(OSError):
        os.utime(path)
    return image


def _write_atomic(path: Path, data: bytes) -> None:
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


def _prune(directory: Path, max_bytes: int) -> int:
    files: list[tuple[float, int, str]] = []
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    if entry.is_file(follow_symlinks=False):
                        # os.stat rather than entry.stat(): Windows' directory listing can
                        # lag behind a recent os.utime.
                        stat = os.stat(entry.path)
                        files.append((stat.st_mtime, stat.st_size, entry.path))
                except OSError:
                    continue  # vanished while listing
    except FileNotFoundError:
        return 0

    total = sum(size for _, size, _ in files)
    deleted = 0
    for _, size, file in sorted(files):
        if total <= max_bytes:
            break
        try:
            os.unlink(file)
        except FileNotFoundError:
            pass  # removed meanwhile: it no longer takes space either
        except OSError:
            continue  # in use (Windows); it still counts toward the total
        else:
            deleted += 1
        total -= size
    return deleted
