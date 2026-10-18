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
  `download-dir`; the answer holds `torrent-added`, or `torrent-duplicate` (also a success).

Secret hygiene as in remote.py: the password only travels in the Authorization header.
"""

from __future__ import annotations

import base64
import contextlib
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

PROGRAM = "Transmission"
SESSION_HEADER = "X-Transmission-Session-Id"
_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")
_MAX_RESULT = 200  # characters of Transmission's own error text shown


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

    @contextlib.asynccontextmanager
    async def _session(self) -> AsyncIterator[_Session]:
        settings = self._settings.current  # one snapshot for the whole operation
        if not settings.remote_url:
            raise RemoteError("Transmission isn't set up: enter its address in Settings.")
        auth: httpx.Auth | None = None
        if settings.remote_username:
            auth = httpx.BasicAuth(
                settings.remote_username, settings.remote_password.get_secret_value()
            )
        async with await self._http.client(settings.remote_verify_tls, auth=auth) as http:
            yield _Session(
                http,
                rpc_url(settings.remote_url),
                host_of(settings.remote_url),
                settings.remote_destination,
                signed_in=auth is not None,
            )


class _Session:
    def __init__(
        self,
        http: httpx.AsyncClient,
        url: str,
        host: str,
        destination: str,
        *,
        signed_in: bool,
    ) -> None:
        self._http = http
        self._url = url
        self.host = host
        self._destination = destination
        self._signed_in = signed_in
        self._session_id: str | None = None
        self._tag = 0

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
                f"Transmission refused it: {reason}." if reason else self._unexpected()
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
                return "Transmission: wrong user name or password."
            return "Transmission wants a user name and password: enter them in Settings."
        if status == 403:
            return (
                "Transmission refused this computer: add its address to Transmission's RPC "
                "whitelist, or turn the whitelist off."
            )
        if status in (404, 409):
            return (
                f"No Transmission answered at {self.host}. Check the address and that remote "
                "access (RPC) is on."
            )
        return f"Transmission at {self.host} answered HTTP {status}."

    def _unexpected(self) -> str:
        return f"Transmission at {self.host} sent a reply AirGrabber doesn't understand."
