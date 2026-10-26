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
- `POST /api/v2/auth/logout`;
- the Downloads page (read-only): `GET /api/v2/torrents/info` (optionally `hashes=a|b`) → a JSON
  list of torrents with `hash` (`infohash_v1` from v4.4), `name`, `progress` (0…1) and `state`
  (downloading, stalledDL, queuedDL, pausedDL / stoppedDL (v5), uploading, …, error,
  missingFiles); `GET /api/v2/sync/maindata` → `server_state.free_space_on_disk`.

Without a user name AirGrabber doesn't sign in (qBittorrent can skip sign-in for this network:
"Bypass authentication for clients in whitelisted IP subnets"). Secret hygiene as in remote.py:
the password travels in the login POST body only, and the session cookie stays in the
short-lived client's cookie jar. The read-only status calls keep their session cookie in memory
(for the settings it was made with) and send it in the Cookie header, signing in again only
when qBittorrent answers 403 (the session ended); it is never logged or shown.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
from collections.abc import AsyncIterator, Collection, Mapping
from dataclasses import dataclass

import httpx
from pydantic import SecretStr

from app.i18n import t
from app.models import DownloadState, RemoteKind, RemoteStatus, RemoteTask, Settings
from app.services.remote import (
    DiskSpace,
    HttpClients,
    RemoteError,
    clamp_progress,
    failure_text,
    host_of,
    normal_hash,
    number,
    redirect_text,
)
from app.services.settings import SettingsService

log = logging.getLogger(__name__)

PROGRAM = "qBittorrent"
_VERSION_RE = re.compile(r"^v?\d+(?:\.\d+)+[\w.+-]*$")
# Multipart parts: (field name, (file name or None for a plain field, content[, content type])).
_Part = tuple[str, tuple[str | None, bytes | str] | tuple[str, bytes, str]]

# qBittorrent's torrent states (torrents/info "state"), v4.1 – v5.x.
_STATES: Mapping[str, DownloadState] = {
    "error": DownloadState.PROBLEM,
    "missingFiles": DownloadState.PROBLEM,
    "uploading": DownloadState.READY,
    "stalledUP": DownloadState.READY,
    "forcedUP": DownloadState.READY,
    "queuedUP": DownloadState.READY,
    "checkingUP": DownloadState.READY,
    "pausedUP": DownloadState.READY,
    "stoppedUP": DownloadState.READY,
    "downloading": DownloadState.DOWNLOADING,
    "forcedDL": DownloadState.DOWNLOADING,
    "stalledDL": DownloadState.DOWNLOADING,
    "metaDL": DownloadState.DOWNLOADING,
    "forcedMetaDL": DownloadState.DOWNLOADING,
    "moving": DownloadState.DOWNLOADING,
    "queuedDL": DownloadState.WAITING,
    "checkingDL": DownloadState.WAITING,
    "checkingResumeData": DownloadState.WAITING,
    "allocating": DownloadState.WAITING,
    "pausedDL": DownloadState.STOPPED,
    "stoppedDL": DownloadState.STOPPED,
}


@dataclass(frozen=True, slots=True)
class _KeptCookies:
    """The status calls' session cookie, for the settings it was made with."""

    key: tuple[str, str, SecretStr, bool]
    cookies: Mapping[str, str]


class _Refused(RemoteError):
    """qBittorrent answered with an error status (kept, so a status call can sign in again)."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


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
        self._kept: _KeptCookies | None = None  # the status calls' session

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

    async def status(self, hashes: Collection[str] = ()) -> RemoteStatus:
        """qBittorrent's torrents with their progress (read-only, on a kept session)."""
        params = {"hashes": "|".join(sorted(hashes))} if hashes else None
        response = await self._read("torrents/info", params)
        try:
            payload = response.json()
        except ValueError:  # JSONDecodeError and UnicodeDecodeError
            payload = None
        if not isinstance(payload, list):
            raise RemoteError(_unexpected(response))
        tasks = [task for item in payload if (task := _remote_task(item)) is not None]
        return RemoteStatus(program=PROGRAM, tasks=tuple(tasks))

    async def free_space(self) -> DiskSpace | None:
        """Free space on qBittorrent's default save disk (sync/maindata's server_state)."""
        response = await self._read("sync/maindata", None)
        try:
            payload = response.json()
        except ValueError:
            return None
        state = payload.get("server_state") if isinstance(payload, dict) else None
        free = number(state.get("free_space_on_disk")) if isinstance(state, dict) else None
        return DiskSpace(int(free)) if free is not None and free >= 0 else None

    async def _read(self, path: str, params: Mapping[str, str] | None) -> httpx.Response:
        """GET a read-only API path on the kept session; when qBittorrent ended that session
        (403), sign in once more and ask again."""
        settings = self._settings.current  # one snapshot for the whole operation
        key = _login_key(settings)
        kept = self._kept if self._kept is not None and self._kept.key == key else None
        if kept is not None:
            try:
                async with self._session(sign_in=False, cookies=kept.cookies) as session:
                    return await session.request("GET", path, params=params)
            except _Refused as exc:
                if exc.status not in (401, 403):
                    raise
            log.info("The qBittorrent session ended; signing in again")
            self._kept = None
        async with self._session(sign_out=False) as session:
            if session.cookies:
                self._kept = _KeptCookies(key, session.cookies)
            return await session.request("GET", path, params=params)

    @contextlib.asynccontextmanager
    async def _session(
        self,
        *,
        sign_in: bool = True,
        sign_out: bool = True,
        cookies: Mapping[str, str] | None = None,
    ) -> AsyncIterator[_Session]:
        """One operation's session: signed in (with a user name) and out again; the status
        calls keep it (`sign_out=False`) or reuse its `cookies` (`sign_in=False`)."""
        settings = self._settings.current  # one snapshot for the whole operation
        if not settings.remote_url:
            raise RemoteError(t("qBittorrent isn't set up: enter its address in Settings."))
        base = settings.remote_url.rstrip("/")
        headers = {"Referer": f"{_normalized(base)}/", "Origin": _origin(base)}
        if cookies:
            headers["Cookie"] = "; ".join(f"{name}={value}" for name, value in cookies.items())
        async with await self._http.client(settings.remote_verify_tls, headers=headers) as http:
            session = _Session(http, base, settings.remote_destination)
            if sign_in and settings.remote_username:
                await session.login(
                    settings.remote_username, settings.remote_password.get_secret_value()
                )
            try:
                yield session
            finally:
                if sign_out:
                    await session.logout()


class _Session:
    def __init__(self, http: httpx.AsyncClient, base_url: str, destination: str) -> None:
        self._http = http
        self._base = base_url
        self.host = host_of(base_url)
        self._destination = destination
        self._signed_in = False
        self.cookies: dict[str, str] = {}  # the sign-in's session cookie (SID)

    async def login(self, username: str, password: str) -> None:
        response = await self.request(
            "POST",
            "auth/login",
            data={"username": username, "password": password},
            refused="sign-in",
        )
        if response.text.strip() == "Fails.":
            raise RemoteError(t("qBittorrent: wrong user name or password."))
        self._signed_in = True
        self.cookies = dict(response.cookies.items())

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
                t(
                    "qBittorrent didn't add the torrent. It may have it already, or the save "
                    "folder may not be usable."
                )
            )

    async def request(
        self,
        method: str,
        path: str,
        *,
        data: Mapping[str, str] | None = None,
        files: list[_Part] | None = None,
        params: Mapping[str, str] | None = None,
        refused: str = "",
    ) -> httpx.Response:
        """Send a request; raise RemoteError (outside any `except`, so no httpx exception is
        its context) unless qBittorrent answered with a success status."""
        url = f"{self._base}/api/v2/{path}"
        response: httpx.Response | None = None
        failure = ""
        try:
            response = await self._http.request(
                method, url, data=data, files=files, params=params
            )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            failure = failure_text(exc, self.host, PROGRAM)
        if response is None:
            raise RemoteError(failure)
        if response.is_redirect:
            raise RemoteError(redirect_text(response, self.host))
        if response.is_success:
            return response
        raise _Refused(self._status_text(response.status_code, refused), response.status_code)

    def _status_text(self, status: int, refused: str) -> str:
        if refused == "sign-in" and status == 401:
            return t("qBittorrent: wrong user name or password.")
        if refused == "sign-in" and status == 403:
            return t(
                "qBittorrent has blocked this computer after too many failed sign-ins. "
                "Try again later, or restart qBittorrent."
            )
        if status in (401, 403):
            return t(
                "qBittorrent wants a user name and password: enter those of its Web UI in "
                "Settings."
            )
        if refused == "add" and status == 415:
            return t("qBittorrent says this .torrent file isn't valid.")
        if refused == "add" and status == 409:
            return t("qBittorrent didn't add the torrent. It may have it already.")
        if status == 404:
            return t(
                "No qBittorrent Web UI answered at {host}. Check the address and that "
                "the Web UI is on (Tools → Options → Web UI).",
                host=self.host,
            )
        return t("qBittorrent at {host} answered HTTP {status}.", host=self.host, status=status)


def _login_key(settings: Settings) -> tuple[str, str, SecretStr, bool]:
    """What a kept session depends on (SecretStr compares the passwords themselves)."""
    return (
        settings.remote_url,
        settings.remote_username,
        settings.remote_password,
        settings.remote_verify_tls,
    )


def _unexpected(response: httpx.Response) -> str:
    host = host_of(str(response.request.url))
    return t("qBittorrent at {host} sent a reply AirGrabber doesn't understand.", host=host)


def _remote_task(raw: object) -> RemoteTask | None:
    """One torrent of torrents/info."""
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    info_hash = normal_hash(raw.get("infohash_v1")) or normal_hash(raw.get("hash"))
    progress = clamp_progress(number(raw.get("progress")) or 0.0)
    state_name = raw.get("state")
    state = qbittorrent_state(state_name if isinstance(state_name, str) else "", progress)
    if state is DownloadState.READY:
        progress = 1.0
    return RemoteTask(
        name=name if isinstance(name, str) else "",
        info_hash=info_hash,
        progress=progress,
        state=state,
    )


def qbittorrent_state(state: str, progress: float) -> DownloadState:
    """A torrent's DownloadState from its qBittorrent state; an unknown one goes by its
    progress."""
    found = _STATES.get(state)
    if found is None:
        return DownloadState.READY if progress >= 1.0 else DownloadState.WAITING
    if found is DownloadState.STOPPED and progress >= 1.0:
        return DownloadState.READY
    return found


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
