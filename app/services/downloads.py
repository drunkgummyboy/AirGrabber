"""Grabbing a release, in the user's chosen `DownloadMode`.

FOLDER: a .torrent file into the watch folder, else the magnet to the remote torrent program.
APP: the .torrent (kept in an app-owned cache folder) or else a magnet is opened with the
torrent program Windows has registered for it.
REMOTE: the .torrent or else a magnet is sent to the torrent program on a NAS or another
computer (Download Station, qBittorrent or Transmission: `RemoteDownloads`).

In FOLDER mode a program (often a NAS's Download Station) watches `Settings.download_dir`, so a
`.torrent` file written there is the normal path (as in 1.x). Only when no `.torrent` can be
had — the indexer gave no direct link (or it failed) and itorrents.org has no cached copy of the
info hash, which is common for brand-new releases — is the magnet link sent to the remote
torrent program set up in Settings.

Direct download links may embed an indexer/Prowlarr API key: they are never logged or shown,
and neither are magnet links.
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
from urllib.parse import quote

import httpx
from pydantic import ValidationError

from app.database import HistoryRepository
from app.models import (
    DownloadMode,
    DownloadRecord,
    GrabOutcome,
    GrabResult,
    GrabTarget,
    MediaType,
    RemoteKind,
    TorrentApp,
    TorrentRelease,
)
from app.services.remote_downloads import RemoteDownloads
from app.services.settings import SettingsService
from app.services.torrent_app import find_torrent_app

log = logging.getLogger(__name__)

DownloadListener = Callable[[], None]
Opener = Callable[[str], None]  # opens a file path or magnet link with its Windows handler

ITORRENTS_URL = "https://itorrents.org/torrent/{}.torrent"  # hash in UPPERCASE hex
MAX_TORRENT_BYTES = 10 * 1024 * 1024
MAX_NAME_LENGTH = 120
TORRENT_CACHE_KEEP = 50  # .torrent files kept in the APP-mode cache folder
NO_TORRENT_APP = (
    "No torrent program is set up on this PC. Install one, such as qBittorrent, or choose "
    'another option under "When I download" in Settings.'
)
NO_REMOTE_FALLBACK = (
    "No .torrent file is available for this release, and no torrent program on a NAS or "
    "another computer is set up in Settings to send its magnet link to."
)
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
        remote: RemoteDownloads,
        torrent_cache: Path,
        *,
        find_app: Callable[[], TorrentApp | None] = find_torrent_app,
        opener: Opener | None = None,
    ) -> None:
        self._http = http
        self._history = history
        self._settings = settings
        self._remote = remote  # the torrent program on a NAS or another computer
        self._torrent_cache = torrent_cache  # APP mode: .torrent files handed to the program
        self._find_app = find_app
        self._opener: Opener = opener or open_with_default_program
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

    async def has_grabbed(self, target: GrabTarget) -> bool:
        """Whether the download history has `target`: the film, the episode or its season's
        pack, or (a season target) the season pack."""
        if target.media_type is MediaType.MOVIE:
            return await self._history.has_movie(target.media_id)
        assert target.season is not None  # GrabTarget requires it for TV
        return await self._history.has_episode(target.media_id, target.season, target.episode)

    async def grab(self, release: TorrentRelease, target: GrabTarget) -> GrabResult:
        """Grab the release the way `Settings.download_mode` says; record it in the download
        history and notify listeners.

        FOLDER: save the .torrent into the watch folder, or else send its magnet to the remote
        torrent program. APP: open the .torrent (or else a magnet) in the user's torrent
        program. REMOTE: send the .torrent (or else a magnet) to the remote torrent program.

        Raises GrabError, or RemoteError when the remote program can't be reached or refuses.
        The grab counts as done once the file is written or opened or the task created: a
        failure to record it afterwards is logged, not raised, so the user isn't invited to
        grab it twice.
        """
        mode = self._settings.current.download_mode
        if mode is DownloadMode.APP:
            result, saved_path, info_hash = await self._grab_in_app(release)
        elif mode is DownloadMode.REMOTE:
            result, saved_path, info_hash = await self._grab_remote(release)
        else:
            result, saved_path, info_hash = await self._grab_to_folder(release)

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

    async def _grab_in_app(self, release: TorrentRelease) -> tuple[GrabResult, str, str | None]:
        """Open the .torrent (saved in the cache folder) or else a magnet in the torrent program.

        Returns (result, history saved_path, info hash). The remote program is not used.
        """
        app = await asyncio.to_thread(self._find_app)
        if app is None:
            raise GrabError(NO_TORRENT_APP)  # before downloading anything
        content, magnet, info_hash = await self._obtain(
            release, _magnet_or_none(release.magnet_uri)
        )
        message = "Opened in your torrent program"
        if content is not None:
            path = await self._save_to_cache(content, release.title)
            await self._open(str(path), app)
            await self._prune_cache()
            log.info("Grabbed %r: opened %s in %s", release.title, path.name, app.name)
            result = GrabResult(outcome=GrabOutcome.OPENED, path=str(path), message=message)
            return result, app.name, info_hash
        if magnet is None and info_hash:
            magnet = build_magnet(info_hash, release.title)
        if magnet is None:
            raise GrabError("No .torrent file or magnet link is available for this release.")
        await self._open(magnet, app)
        log.info("Grabbed %r: magnet opened in %s", release.title, app.name)
        return GrabResult(outcome=GrabOutcome.OPENED, message=message), app.name, info_hash

    async def _grab_remote(self, release: TorrentRelease) -> tuple[GrabResult, str, str | None]:
        """Send the .torrent (direct link, then itorrents.org) or else a magnet (the release's
        own, a Prowlarr redirect, or one built from the hash) to the remote torrent program.

        Returns (result, history saved_path = the program's name, info hash).
        """
        if not self._remote.configured:
            raise GrabError(_remote_not_set_up(self._remote.kind))  # before any download
        client = self._remote.client()
        name = client.kind.program
        content, magnet, info_hash = await self._obtain(
            release, _magnet_or_none(release.magnet_uri)
        )
        if content is not None:
            await client.add_torrent(content, f"{safe_filename(release.title)}.torrent")
            log.info("Grabbed %r: .torrent sent to %s", release.title, name)
        else:
            if magnet is None and info_hash:
                magnet = build_magnet(info_hash, release.title)
            if magnet is None:
                raise GrabError("No .torrent file or magnet link is available for this release.")
            await client.add_magnet(magnet)
            log.info("Grabbed %r: magnet sent to %s", release.title, name)
        return GrabResult(outcome=GrabOutcome.SENT, message=f"Sent to {name}"), name, info_hash

    async def _obtain(
        self, release: TorrentRelease, magnet: str | None
    ) -> tuple[bytes | None, str | None, str | None]:
        """(.torrent bytes or None, magnet or None, info hash): the release's direct link first
        (which may redirect to a magnet), then itorrents.org by info hash."""
        info_hash = release.info_hash
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
        return content, magnet, info_hash

    async def _grab_to_folder(self, release: TorrentRelease) -> tuple[GrabResult, str, str | None]:
        """Save the .torrent into the watch folder, or else send the magnet to the remote
        torrent program (when one is set up).

        Returns (result, history saved_path, info hash).
        """
        content, magnet, info_hash = await self._obtain(release, _magnet_or_none(release.magnet))

        if content is not None:
            path = await self._save(content, release.title)
            saved_path = str(path)
            result = GrabResult(
                outcome=GrabOutcome.SAVED, path=saved_path, message=f"Saved {path.name}"
            )
            log.info("Grabbed %r: saved %s", release.title, path)
        elif not self._remote.configured:
            raise GrabError(NO_REMOTE_FALLBACK)
        elif magnet is None:
            raise GrabError(
                "No .torrent file is available for this release, and it has no magnet link to "
                f"send to {self._remote.kind.program}."
            )
        else:
            client = self._remote.client()
            saved_path = client.kind.program
            await client.add_magnet(magnet)
            result = GrabResult(outcome=GrabOutcome.SENT, message=f"Sent to {saved_path}")
            log.info("Grabbed %r: magnet sent to %s", release.title, saved_path)
        return result, saved_path, info_hash

    async def _open(self, target: str, app: TorrentApp) -> None:
        """Hand a file path or magnet to its Windows handler. Never logs `target` (magnets)."""
        try:
            await asyncio.to_thread(self._opener, target)
        except OSError as exc:
            reason = exc.strerror or type(exc).__name__
            raise GrabError(f"Couldn't open this release in {app.name}: {reason}") from None

    async def _save_to_cache(self, content: bytes, title: str) -> Path:
        directory = self._torrent_cache
        try:
            return await asyncio.to_thread(_write_torrent, directory, safe_filename(title), content)
        except OSError as exc:
            reason = exc.strerror or type(exc).__name__
        raise GrabError(f"Can't write the .torrent file to {directory}: {reason}")

    async def _prune_cache(self) -> None:
        try:
            await asyncio.to_thread(prune_torrent_cache, self._torrent_cache, TORRENT_CACHE_KEEP)
        except OSError:
            log.warning("Could not prune the torrent cache folder", exc_info=True)

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


def open_with_default_program(target: str) -> None:
    """Open a file or a magnet link with its registered Windows handler. Blocking."""
    startfile = getattr(os, "startfile", None)
    if startfile is None:
        raise OSError(0, "Opening torrents is only supported on Windows")
    startfile(target)


def _remote_not_set_up(kind: RemoteKind) -> str:
    needs = "address, user name and password" if kind is RemoteKind.SYNOLOGY else "address"
    return f"{kind.program} isn't set up yet: enter its {needs} in Settings."


def build_magnet(info_hash: str, title: str) -> str:
    return f"magnet:?xt=urn:btih:{info_hash}&dn={quote(title, safe='')}"


def prune_torrent_cache(directory: Path, keep: int) -> None:
    """Delete all but the `keep` newest .torrent files in `directory`. Blocking."""
    if not directory.is_dir():
        return
    files: list[tuple[float, Path]] = []
    for path in directory.glob("*.torrent"):
        with contextlib.suppress(OSError):
            files.append((path.stat().st_mtime, path))
    files.sort(key=lambda item: item[0], reverse=True)
    for _, path in files[keep:]:
        with contextlib.suppress(OSError):
            path.unlink()


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
