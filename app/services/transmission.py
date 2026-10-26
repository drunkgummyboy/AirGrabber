"""Transmission client (RPC): a RemoteClient for magnets and .torrent files.

Protocol, per Transmission's RPC spec (docs/rpc-spec.md; the method names below are the
original ones, which Transmission 4.1's JSON-RPC 2.0 still accepts):
- POST a JSON object `{"method": …, "arguments": {…}, "tag": n}` to `<address>/transmission/rpc`
  (the address as given when it already ends in `/rpc`); the answer is
  `{"result": "success" | <error text>, "arguments": {…}, "tag": n}`;
- CSRF protection: a request without the current `X-Transmission-Session-Id` header gets
  HTTP 409 with the right id in that header; the request is sent again once with it;
- authentication, when Transmission asks for it, is HTTP Basic (HTTP 401 when it fails), and
  HTTP 403 means this computer isn't on the RPC whitelist;
- `session-get` → `version` ("4.0.6 (38c164933e)") for Test connection;
- `torrent-add` with `filename` (a magnet link) or `metainfo` (the .torrent, base64), plus
  `download-dir`; the answer holds `torrent-added`, or `torrent-duplicate` (also a success);
- the Downloads page (read-only): `torrent-get` with `fields` (hashString, name, percentDone,
  status 0–6, error 0–3, isFinished) and, when every grab asked about has one, `ids` = their
  hashes; `free-space` with `path` (the destination in Settings, else `session-get`'s
  `download-dir`) → `size-bytes` and, from 4.0, `total_size`.

Secret hygiene as in remote.py: the password only travels in the Authorization header. The
status calls keep the session id Transmission handed out (in memory, per address), so they
skip the 409 handshake; Transmission has no sign-in of its own to keep.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import re
from collections.abc import AsyncIterator, Collection, Mapping

import httpx

from app.i18n import t
from app.models import DownloadState, RemoteKind, RemoteStatus, RemoteTask
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

PROGRAM = "Transmission"
SESSION_HEADER = "X-Transmission-Session-Id"
_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")
_MAX_RESULT = 200  # characters of Transmission's own error text shown
_FIELDS = ("hashString", "name", "percentDone", "status", "error", "isFinished")
_LOCAL_ERROR = 3  # torrent "error" 3 = a local error (disk full, files gone); 1/2 = tracker's
# torrent "status": 0 stopped, 1 check-wait, 2 checking, 3 download-wait, 4 downloading,
# 5 seed-wait, 6 seeding.
_STATUSES: Mapping[int, DownloadState] = {
    0: DownloadState.STOPPED,
    1: DownloadState.WAITING,
    2: DownloadState.WAITING,
    3: DownloadState.WAITING,
    4: DownloadState.DOWNLOADING,
    5: DownloadState.READY,
    6: DownloadState.READY,
}


def rpc_url(address: str) -> str:
    """Transmission's RPC endpoint for the address in Settings."""
    base = address.rstrip("/")
    return base if base.endswith("/rpc") else f"{base}/transmission/rpc"


class TransmissionClient:
    kind = RemoteKind.TRANSMISSION

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
        self._kept_id: tuple[str, str] | None = None  # (RPC address, session id): status calls

    async def test(self) -> str:
        """Read the version. E.g. "Transmission 4.0.6"."""
        async with self._session() as session:
            arguments = await session.call("session-get", {})
        version = arguments.get("version")
        match = _VERSION_RE.search(version) if isinstance(version, str) else None
        return f"{PROGRAM} {match.group(0)}" if match else PROGRAM

    async def add_magnet(self, magnet: str) -> None:
        if not magnet.startswith("magnet:"):
            raise ValueError("not a magnet link")
        async with self._session() as session:
            await session.add({"filename": magnet})
            log.info("Added a magnet link to Transmission on %s", session.host)

    async def add_torrent(self, content: bytes, filename: str) -> None:
        async with self._session() as session:
            await session.add({"metainfo": base64.b64encode(content).decode("ascii")})
            log.info("Added a .torrent file to Transmission on %s", session.host)

    async def status(self, hashes: Collection[str] = ()) -> RemoteStatus:
        """Transmission's torrents with their progress (read-only)."""
        arguments: dict[str, object] = {"fields": list(_FIELDS)}
        if hashes:
            arguments["ids"] = sorted(hashes)
        async with self._session(keep_id=True) as session:
            answer = await session.call("torrent-get", arguments)
        torrents = answer.get("torrents")
        if not isinstance(torrents, list):
            raise RemoteError(session.unexpected())
        tasks = [task for item in torrents if (task := _remote_task(item)) is not None]
        return RemoteStatus(program=PROGRAM, tasks=tuple(tasks))

    async def free_space(self) -> DiskSpace | None:
        """Free space where Transmission saves: the destination in Settings, else its own
        download folder."""
        async with self._session(keep_id=True) as session:
            path = self._settings.current.remote_destination.strip()
            if not path:
                answer = await session.call("session-get", {"fields": ["download-dir"]})
                folder = answer.get("download-dir")
                path = folder if isinstance(folder, str) else ""
            if not path:
                return None
            answer = await session.call("free-space", {"path": path})
        free = number(answer.get("size-bytes"))
        total = number(answer.get("total_size"))
        if free is None or free < 0:
            return None
        return DiskSpace(int(free), int(total) if total is not None and total > 0 else None)

    @contextlib.asynccontextmanager
    async def _session(self, *, keep_id: bool = False) -> AsyncIterator[_Session]:
        """One operation's session; `keep_id`: start with (and keep) the session id of the
        status calls before, so they skip the 409 handshake."""
        settings = self._settings.current  # one snapshot for the whole operation
        if not settings.remote_url:
            raise RemoteError(t("Transmission isn't set up: enter its address in Settings."))
        auth: httpx.Auth | None = None
        if settings.remote_username:
            auth = httpx.BasicAuth(
                settings.remote_username, settings.remote_password.get_secret_value()
            )
        url = rpc_url(settings.remote_url)
        kept = self._kept_id
        async with await self._http.client(settings.remote_verify_tls, auth=auth) as http:
            session = _Session(
                http,
                url,
                host_of(settings.remote_url),
                settings.remote_destination,
                signed_in=auth is not None,
                session_id=kept[1] if keep_id and kept is not None and kept[0] == url else None,
            )
            try:
                yield session
            finally:
                if keep_id and session.session_id is not None:
                    self._kept_id = (url, session.session_id)


class _Session:
    def __init__(
        self,
        http: httpx.AsyncClient,
        url: str,
        host: str,
        destination: str,
        *,
        signed_in: bool,
        session_id: str | None = None,
    ) -> None:
        self._http = http
        self._url = url
        self.host = host
        self._destination = destination
        self._signed_in = signed_in
        self._session_id: str | None = session_id
        self._tag = 0

    @property
    def session_id(self) -> str | None:
        return self._session_id

    async def add(self, arguments: dict[str, object]) -> None:
        if self._destination:
            arguments["download-dir"] = self._destination
        added = await self.call("torrent-add", arguments)
        if "torrent-added" not in added and "torrent-duplicate" not in added:
            raise RemoteError(self._unexpected())

    async def call(self, method: str, arguments: Mapping[str, object]) -> dict[str, object]:
        """Call one RPC method; returns its `arguments`."""
        self._tag += 1
        body = {"method": method, "arguments": dict(arguments), "tag": self._tag}
        response = await self._post(body)
        if response.status_code == 409 and SESSION_HEADER in response.headers:
            self._session_id = response.headers[SESSION_HEADER]
            response = await self._post(body)  # once: the id Transmission just handed out
        if not response.is_success:
            raise RemoteError(self._status_text(response.status_code))
        try:
            payload = response.json()
        except ValueError:  # JSONDecodeError and UnicodeDecodeError
            payload = None
        if not isinstance(payload, dict):
            raise RemoteError(self._unexpected())
        result = payload.get("result")
        if result != "success":
            reason = result.strip()[:_MAX_RESULT] if isinstance(result, str) else ""
            raise RemoteError(
                t("Transmission refused it: {reason}.", reason=reason)
                if reason
                else self._unexpected()
            )
        answer = payload.get("arguments")
        return answer if isinstance(answer, dict) else {}

    async def _post(self, body: Mapping[str, object]) -> httpx.Response:
        """POST without raising inside `except`: no httpx exception becomes a RemoteError's
        context. Redirects are reported."""
        headers = {SESSION_HEADER: self._session_id} if self._session_id else {}
        response: httpx.Response | None = None
        failure = ""
        try:
            response = await self._http.post(self._url, json=body, headers=headers)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            failure = failure_text(exc, self.host, PROGRAM)
        if response is None:
            raise RemoteError(failure)
        if response.is_redirect:
            raise RemoteError(redirect_text(response, self.host))
        return response

    def _status_text(self, status: int) -> str:
        if status == 401:
            if self._signed_in:
                return t("Transmission: wrong user name or password.")
            return t("Transmission wants a user name and password: enter them in Settings.")
        if status == 403:
            return t(
                "Transmission refused this computer: add its address to Transmission's RPC "
                "whitelist, or turn the whitelist off."
            )
        if status in (404, 409):
            return t(
                "No Transmission answered at {host}. Check the address and that remote "
                "access (RPC) is on.",
                host=self.host,
            )
        return t("Transmission at {host} answered HTTP {status}.", host=self.host, status=status)

    def _unexpected(self) -> str:
        return t("Transmission at {host} sent a reply AirGrabber doesn't understand.", host=self.host)

    def unexpected(self) -> str:
        return self._unexpected()


def _field(raw: Mapping[str, object], name: str, snake: str) -> object:
    """A torrent field by its RPC name, or by Transmission 4.1's JSON-RPC (snake_case) one."""
    return raw.get(name, raw.get(snake))


def _remote_task(raw: object) -> RemoteTask | None:
    """One torrent of torrent-get."""
    if not isinstance(raw, dict):
        return None
    name = raw.get("name")
    progress = clamp_progress(number(_field(raw, "percentDone", "percent_done")) or 0.0)
    status = number(raw.get("status"))
    error = number(raw.get("error"))
    finished = _field(raw, "isFinished", "is_finished") is True
    state = transmission_state(
        int(status) if status is not None else None,
        int(error) if error is not None else 0,
        progress,
        finished=finished,
    )
    if state is DownloadState.READY:
        progress = 1.0
    return RemoteTask(
        name=name if isinstance(name, str) else "",
        info_hash=normal_hash(_field(raw, "hashString", "hash_string")),
        progress=progress,
        state=state,
    )


def transmission_state(
    status: int | None, error: int, progress: float, *, finished: bool = False
) -> DownloadState:
    """A torrent's DownloadState. Tracker warnings and errors (error 1, 2) don't stop a
    download; a local error (3) does."""
    if error == _LOCAL_ERROR:
        return DownloadState.PROBLEM
    if finished or progress >= 1.0:
        return DownloadState.READY
    if status is None:
        return DownloadState.WAITING
    return _STATUSES.get(status, DownloadState.WAITING)
