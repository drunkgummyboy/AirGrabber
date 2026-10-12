"""Grabbing a release: a .torrent file into the watch folder, else the magnet to Download Station.

The user's Download Station watches `Settings.download_dir`, so a `.torrent` file written there
is the normal path (as in 1.x). Only when no `.torrent` can be had — the indexer gave no direct
link (or it failed) and itorrents.org has no cached copy of the info hash, which is common for
brand-new releases — is the magnet link sent to Download Station through the DSM Web API.

Direct download links may embed an indexer/Prowlarr API key: they are never logged or shown.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import tempfile
import zlib
from collections.abc import Callable
from pathlib import Path

import httpx
from pydantic import ValidationError

from app.database import HistoryRepository
from app.models import DownloadRecord, GrabOutcome, GrabResult, GrabTarget, TorrentRelease
from app.services.settings import SettingsService
from app.services.synology import DownloadStationClient

log = logging.getLogger(__name__)

DownloadListener = Callable[[], None]

ITORRENTS_URL = "https://itorrents.org/torrent/{}.torrent"  # hash in UPPERCASE hex
MAX_TORRENT_BYTES = 10 * 1024 * 1024
MAX_NAME_LENGTH = 120
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
    ),
    "Accept": "application/x-bittorrent, application/octet-stream;q=0.9, */*;q=0.8",
}
_MAX_REDIRECTS = 5
_FORBIDDEN_CHARS = re.compile(r'[<>:"/\\|?*]')
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_WINDOWS_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{n}" for n in range(1, 10)}
    | {f"LPT{n}" for n in range(1, 10)}
)


class GrabError(Exception):
    """A release could not be grabbed (user-facing text)."""


class DownloadService:
    def __init__(
        self,
        http: httpx.AsyncClient,
        history: HistoryRepository,
        settings: SettingsService,
        station: DownloadStationClient,
    ) -> None:
        self._http = http
        self._history = history
        self._settings = settings
        self._station = station
        self._listeners: list[DownloadListener] = []

    def subscribe(self, listener: DownloadListener) -> Callable[[], None]:
        """Call `listener` after every successful grab. Returns a function that unsubscribes it."""
        self._listeners.append(listener)
        subscribed = True

        def unsubscribe() -> None:
            nonlocal subscribed
            if subscribed:
                subscribed = False
                self._listeners.remove(listener)

        return unsubscribe

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:
                log.exception("Download listener %r failed", listener)

    async def grabbed_hashes(self) -> frozenset[str]:
        """Info hashes (lowercase hex) of the releases in the download history."""
        return await self._history.info_hashes()

    async def grab(self, release: TorrentRelease, target: GrabTarget) -> GrabResult:
        """Save the release's .torrent into the watch folder, or else send its magnet to
        Download Station; record it in the download history and notify listeners.

        Raises GrabError, or DownloadStationError when Download Station refuses the magnet.
        The grab counts as done once the file is written or the task created: a failure to
        record it afterwards is logged, not raised, so the user isn't invited to grab it twice.
        """
        info_hash = release.info_hash
        magnet = _magnet_or_none(release.magnet)
        download_url = release.download_url or ""
        content: bytes | None = None

        if _is_http(download_url):
            content, redirect_magnet = await self._fetch_torrent(download_url, release.source)
            if redirect_magnet and magnet is None:  # e.g. Prowlarr links to magnet-only sources
                magnet = redirect_magnet
                info_hash = info_hash or _magnet_hash(redirect_magnet)
        elif _magnet_or_none(download_url) and magnet is None:
            magnet = download_url
            info_hash = info_hash or _magnet_hash(download_url)

        if content is None and info_hash:
            content, _ = await self._fetch_torrent(
                ITORRENTS_URL.format(info_hash.upper()), "itorrents.org"
            )

        if content is not None:
            path = await self._save(content, release.title)
            saved_path = str(path)
            result = GrabResult(
                outcome=GrabOutcome.SAVED, path=saved_path, message=f"Saved {path.name}"
            )
            log.info("Grabbed %r: saved %s", release.title, path)
        elif not self._station.configured:
            raise GrabError(
                "No .torrent file is available for this release and Download Station isn't set "
                "up in Settings."
            )
        elif magnet is None:
            raise GrabError(
                "No .torrent file is available for this release, and it has no magnet link to "
                "send to Download Station."
            )
        else:
            await self._station.add_magnet(magnet)
            result = GrabResult(outcome=GrabOutcome.SENT, message="Sent to Download Station")
            saved_path = "Download Station"
            log.info("Grabbed %r: magnet sent to Download Station", release.title)

        record = DownloadRecord(
            media_type=target.media_type,
            media_id=target.media_id,
            season=target.season,
            episode=target.episode,
            release_title=release.title,
            info_hash=info_hash,
            source=release.source,
            size_bytes=release.size_bytes or None,
            saved_path=saved_path,
        )
        try:
            await self._history.add(record)
        except Exception:
            log.exception("Could not record the grab of %r in the download history", release.title)
        self._notify()
        return result

    async def _fetch_torrent(self, url: str, label: str) -> tuple[bytes | None, str | None]:
        """GET a .torrent file, following up to five redirects by hand.

        Returns (torrent bytes, None), or (None, magnet) when the link redirects to a magnet
        link, or (None, None). Failures are logged by `label` only: the URL may hold an API key.
        """
        current = url
        for _ in range(_MAX_REDIRECTS + 1):
            body = bytearray()
            failure = ""
            next_url: str | None = None
            try:
                async with self._http.stream(
                    "GET", current, headers=BROWSER_HEADERS, follow_redirects=False
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("Location", "").strip()
                        if _magnet_or_none(location):
                            return None, location
                        next_url = str(response.request.url.join(location))
                    elif not response.is_success:
                        failure = f"HTTP {response.status_code}"
                    elif _declared_length(response) > MAX_TORRENT_BYTES:
                        failure = "file too large"
                    else:
                        async for chunk in response.aiter_bytes():
                            body += chunk
                            if len(body) > MAX_TORRENT_BYTES:
                                failure = "file too large"
                                break
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                failure = type(exc).__name__
            if next_url is not None:
                current = next_url
                continue
            if failure:
                log.info("No .torrent file from %s (%s)", label, failure)
                return None, None
            content = _maybe_gunzip(bytes(body))
            if content is None or not _is_torrent(content):
                log.info("No .torrent file from %s (the reply is not a torrent file)", label)
                return None, None
            return content, None
        log.info("No .torrent file from %s (too many redirects)", label)
        return None, None

    async def _save(self, content: bytes, title: str) -> Path:
        directory = self._settings.current.download_dir
        try:
            return await asyncio.to_thread(_write_torrent, directory, safe_filename(title), content)
        except OSError as exc:
            reason = exc.strerror or type(exc).__name__
        raise GrabError(f"Can't write to the download folder {directory}: {reason}")


def safe_filename(title: str) -> str:
    """A file name stem (no extension) from a release title, safe on Windows and NAS shares."""
    text = _CONTROL_CHARS.sub(" ", _FORBIDDEN_CHARS.sub("", title))
    text = " ".join(text.split()).strip(" .")
    text = text[:MAX_NAME_LENGTH].rstrip(" .")
    if not text:
        return "torrent"
    if text.split(".", 1)[0].upper() in _WINDOWS_DEVICE_NAMES:
        text = f"_{text}"[:MAX_NAME_LENGTH]
    return text


def _write_torrent(directory: Path, stem: str, content: bytes) -> Path:
    """Write atomically (temp file + os.replace) under the first free '<stem>[ (n)].torrent'."""
    directory.mkdir(parents=True, exist_ok=True)
    # The temp name doesn't end in .torrent, so the watch folder never sees a partial file.
    handle, temp = tempfile.mkstemp(dir=directory, prefix=".airgrabber-", suffix=".part")
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(content)
        target = _free_path(directory, stem)
        os.replace(temp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise
    return target


def _free_path(directory: Path, stem: str) -> Path:
    candidate = directory / f"{stem}.torrent"
    number = 2
    while candidate.exists():
        candidate = directory / f"{stem} ({number}).torrent"
        number += 1
    return candidate


def _is_http(url: str) -> bool:
    return url.lower().startswith(("http://", "https://"))


def _magnet_or_none(link: str | None) -> str | None:
    return link if link and link[:7].lower() == "magnet:" else None


def _magnet_hash(magnet: str) -> str | None:
    """The info hash in a magnet link, in the canonical form TorrentRelease uses."""
    try:
        return TorrentRelease(title="magnet", source="magnet", magnet_uri=magnet).info_hash
    except ValidationError:
        return None


def _declared_length(response: httpx.Response) -> int:
    try:
        return int(response.headers.get("Content-Length", "0"))
    except ValueError:
        return 0


def _maybe_gunzip(data: bytes) -> bytes | None:
    """Some torrent caches serve gzip without saying so (no Content-Encoding header)."""
    if not data.startswith(b"\x1f\x8b"):
        return data
    try:
        inflater = zlib.decompressobj(wbits=31)
        content = inflater.decompress(data, MAX_TORRENT_BYTES + 1)
    except zlib.error:
        return None
    return content if len(content) <= MAX_TORRENT_BYTES else None


def _is_torrent(data: bytes) -> bool:
    """A bencoded torrent: a dictionary holding an `info` key (an HTML page is not one)."""
    return len(data) < MAX_TORRENT_BYTES and data.startswith(b"d") and b"4:info" in data
