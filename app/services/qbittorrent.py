"""qBittorrent client (Web UI, Web API v2): a RemoteClient for magnets and .torrent files.

Protocol, per qBittorrent's "WebUI API" wiki (v4.1 – v5.x) and its source:
- `POST /api/v2/auth/login` with form fields `username` and `password`: HTTP 200 with "Ok." and
  a session cookie (`SID`) on success, "Fails." for a wrong user name or password (newer builds
  answer 401), HTTP 403 when the IP is banned after too many failed sign-ins;
- every request carries `Referer` and `Origin` set to the Web UI's own address, because the Web
  UI's CSRF check rejects requests whose Origin / Referer differ from the Host header;
- `GET /api/v2/app/version` → "v5.0.2" (HTTP 403 without a session);
- `POST /api/v2/torrents/add`, multipart/form-data with `urls` (magnet links, one per line) or
  `torrents` (file parts), plus `savepath`: "Ok." or "Fails." (none added, e.g. a duplicate);
  HTTP 415 for an invalid .torrent file; newer builds answer JSON counts or HTTP 409;
- `POST /api/v2/auth/logout`.

Without a user name AirGrabber doesn't sign in (qBittorrent can skip sign-in for this network:
"Bypass authentication for clients in whitelisted IP subnets"). Secret hygiene as in remote.py:
the password travels in the login POST body only, and the session cookie stays in the
short-lived client's cookie jar.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from collections.abc import AsyncIterator, Mapping

import httpx

from app.models import RemoteKind
from app.services.remote import (
    HttpClients,
    RemoteError,
    failure_text,
    host_of,
    redirect_text,
)
from app.services.settings import SettingsService

log = logging.getLogger(__name__)

PROGRAM = "qBittorrent"
_VERSION_RE = re.compile(r"^v?\d+(?:\.\d+)+[\w.+-]*$")
# Multipart parts: (field name, (file name or None for a plain field, content[, content type])).
_Part = tuple[str, tuple[str | None, bytes | str] | tuple[str, bytes, str]]


class QBittorrentClient:
    kind = RemoteKind.QBITTORRENT

    def __init__(
        self,
        settings: SettingsService,
        *,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,  # tests only
        http: HttpClients | None = None,  # shared with the other remote clients
    ) -> None:
        self._settings = settings
        self._http = http or HttpClients(timeout=timeout, transport=transport)

    async def test(self) -> str:
        """Sign in, read the version, sign out. E.g. "qBittorrent v5.0.2"."""
        async with self._session() as session:
            version = (await session.request("GET", "app/version")).text.strip()
        return f"{PROGRAM} {version}" if _VERSION_RE.match(version) else PROGRAM

    async def add_magnet(self, magnet: str) -> None:
        if not magnet.startswith("magnet:"):
            raise ValueError("not a magnet link")
        async with self._session() as session:
            await session.add([("urls", (None, magnet))])
            log.info("Added a magnet link to qBittorrent on %s", session.host)

    async def add_torrent(self, content: bytes, filename: str) -> None:
        async with self._session() as session:
            await session.add([("torrents", (filename, content, "application/x-bittorrent"))])
            log.info("Added a .torrent file to qBittorrent on %s", session.host)

    @contextlib.asynccontextmanager
    async def _session(self) -> AsyncIterator[_Session]:
        settings = self._settings.current  # one snapshot for the whole operation
        if not settings.remote_url:
            raise RemoteError("qBittorrent isn't set up: enter its address in Settings.")
        base = settings.remote_url.rstrip("/")
        headers = {"Referer": f"{_normalized(base)}/", "Origin": _origin(base)}
        async with await self._http.client(settings.remote_verify_tls, headers=headers) as http:
            session = _Session(http, base, settings.remote_destination)
            if settings.remote_username:
                await session.login(
                    settings.remote_username, settings.remote_password.get_secret_value()
                )
            try:
                yield session
            finally:
                await session.logout()


class _Session:
    def __init__(self, http: httpx.AsyncClient, base_url: str, destination: str) -> None:
        self._http = http
        self._base = base_url
        self.host = host_of(base_url)
        self._destination = destination
        self._signed_in = False

    async def login(self, username: str, password: str) -> None:
        response = await self.request(
            "POST",
            "auth/login",
            data={"username": username, "password": password},
            refused="sign-in",
        )
        if response.text.strip() == "Fails.":
            raise RemoteError("qBittorrent: wrong user name or password.")
        self._signed_in = True

    async def logout(self) -> None:
        if not self._signed_in:
            return
        try:
            await self.request("POST", "auth/logout")
        except RemoteError as exc:  # never mask the operation's own outcome
            log.warning("qBittorrent logout on %s failed: %s", self.host, exc)
        finally:
            self._signed_in = False

    async def add(self, parts: list[_Part]) -> None:
        fields = list(parts)
        if self._destination:
            fields.append(("savepath", (None, self._destination)))
        response = await self.request("POST", "torrents/add", files=fields, refused="add")
        if not _added(response):
            raise RemoteError(
                "qBittorrent didn't add the torrent. It may have it already, or the save "
                "folder may not be usable."
            )

    async def request(
        self,
        method: str,
        path: str,
        *,
        data: Mapping[str, str] | None = None,
        files: list[_Part] | None = None,
        refused: str = "",
    ) -> httpx.Response:
        """Send a request; raise RemoteError (outside any `except`, so no httpx exception is
        its context) unless qBittorrent answered with a success status."""
        url = f"{self._base}/api/v2/{path}"
        response: httpx.Response | None = None
        failure = ""
        try:
            response = await self._http.request(method, url, data=data, files=files)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            failure = failure_text(exc, self.host, PROGRAM)
        if response is None:
            raise RemoteError(failure)
        if response.is_redirect:
            raise RemoteError(redirect_text(response, self.host))
        if response.is_success:
            return response
        raise RemoteError(self._status_text(response.status_code, refused))

    def _status_text(self, status: int, refused: str) -> str:
        if refused == "sign-in" and status == 401:
            return "qBittorrent: wrong user name or password."
        if refused == "sign-in" and status == 403:
            return (
                "qBittorrent has blocked this computer after too many failed sign-ins. "
                "Try again later, or restart qBittorrent."
            )
        if status in (401, 403):
            return (
                "qBittorrent wants a user name and password: enter those of its Web UI in "
                "Settings."
            )
        if refused == "add" and status == 415:
            return "qBittorrent says this .torrent file isn't valid."
        if refused == "add" and status == 409:
            return "qBittorrent didn't add the torrent. It may have it already."
        if status == 404:
            return (
                f"No qBittorrent Web UI answered at {self.host}. Check the address and that "
                "the Web UI is on (Tools → Options → Web UI)."
            )
        return f"qBittorrent at {self.host} answered HTTP {status}."


def _normalized(base_url: str) -> str:
    """The address as httpx requests it (a default port left out)."""
    try:
        return str(httpx.URL(base_url)).rstrip("/")
    except httpx.InvalidURL:
        return base_url


def _origin(base_url: str) -> str:
    """scheme://host[:port] as httpx sends it in the Host header (default ports left out),
    which qBittorrent compares the Origin with."""
    try:
        url = httpx.URL(base_url)
        return f"{url.scheme}://{url.netloc.decode('ascii')}"
    except (httpx.InvalidURL, UnicodeDecodeError):
        return base_url


def _added(response: httpx.Response) -> bool:
    """Whether torrents/add added something: "Ok." (v2.x up to qBittorrent 5.1), or JSON
    counts (newer builds), or an empty success."""
    text = response.text.strip()
    if text == "Fails.":
        return False
    if text.startswith("{"):
        try:
            counts = json.loads(text)
        except ValueError:
            return True
        if isinstance(counts, dict):
            added = counts.get("success_count", 0)
            pending = counts.get("pending_count", 0)
            if isinstance(added, int) and isinstance(pending, int) and "success_count" in counts:
                return added + pending > 0
    return True
