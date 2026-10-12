"""Synology Download Station client (DSM Web API), used to hand magnet links to the NAS.

Protocol, per Synology's "Download Station Web API" guide: discover API paths and versions
with SYNO.API.Info (`/webapi/query.cgi`), log in with SYNO.API.Auth (`session=DownloadStation`,
`format=sid`), call the Download Station APIs with `_sid`, then log out. Tasks are created with
SYNO.DownloadStation2.Task `create` when the NAS offers it (Download Station 3.8+/DSM 7; on
`entry.cgi` with `requestFormat: JSON`, so every parameter value is JSON-encoded:
`type="url"`, `url=["magnet:…"]`, `create_list=false`), else with the DSM 6
SYNO.DownloadStation.Task `create` (`uri=magnet:…`).

Every operation logs in and out on its own short-lived httpx client, because TLS verification is
a setting. Secret hygiene: the password and the sid only ever travel in POST bodies, messages and
log records name the NAS by host and port only, and no httpx exception is chained to (or left as
the context of) a DownloadStationError.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import ssl
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app import APP_NAME, __version__
from app.models import Settings
from app.services.settings import SettingsService

log = logging.getLogger(__name__)

SESSION = "DownloadStation"

_INFO_API = "SYNO.API.Info"
_AUTH = "SYNO.API.Auth"
_TASK2 = "SYNO.DownloadStation2.Task"
_TASK = "SYNO.DownloadStation.Task"
_DS_INFO = "SYNO.DownloadStation.Info"
_DSM_INFO = "SYNO.DSM.Info"
_QUERY = ",".join((_AUTH, _TASK2, _TASK, _DS_INFO, _DSM_INFO))

_USER_AGENT = f"{APP_NAME}/{__version__}"
_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")

_TWO_STEP = (
    "This DSM account uses 2-step verification, which AirGrabber can't sign in with. "
    "Use a DSM account without 2-step verification."
)
_AUTH_ERRORS: Mapping[int, str] = {
    400: "DSM rejected the user name or password.",
    401: "This DSM account is disabled.",
    402: "This DSM account isn't allowed to sign in (permission denied).",
    403: _TWO_STEP,
    404: _TWO_STEP,
    406: _TWO_STEP,
    407: "The NAS has blocked this computer's IP address (DSM auto block).",
    408: "The DSM account's password has expired. Sign in to DSM to change it.",
    409: "The DSM account's password has expired. Sign in to DSM to change it.",
    410: "The DSM account's password must be changed. Sign in to DSM to change it.",
}
_COMMON_ERRORS: Mapping[int, str] = {
    102: "The NAS doesn't offer the Download Station API AirGrabber needs.",
    103: "The NAS doesn't offer the Download Station API AirGrabber needs.",
    104: "The NAS doesn't offer the Download Station API AirGrabber needs.",
    105: "This DSM account isn't allowed to use Download Station.",
    106: "The DSM session ended unexpectedly. Try again.",
    107: "The DSM session ended unexpectedly. Try again.",
    119: "The DSM session ended unexpectedly. Try again.",
}


def _task_errors(destination: str) -> Mapping[int, str]:
    where = f'"{destination}"' if destination else "its default folder"
    return {
        401: "Download Station has reached its maximum number of tasks.",
        402: f"Download Station isn't allowed to save to {where}.",
        403: f"The Download Station destination {where} doesn't exist.",
        406: (
            "Download Station has no default destination folder. "
            "Set one in Download Station or in AirGrabber's settings."
        ),
        407: f"Download Station couldn't save to {where}.",
    }


class DownloadStationError(Exception):
    """Download Station is not set up, unreachable, or refused the request (user-facing text)."""


@dataclass(frozen=True, slots=True)
class _Api:
    path: str
    min_version: int
    max_version: int
    json_params: bool  # requestFormat JSON: parameter values must be JSON-encoded

    def version(self, preferred: int) -> int:
        return max(self.min_version, min(self.max_version, preferred))

    def supports(self, version: int) -> bool:
        return self.min_version <= version <= self.max_version


class DownloadStationClient:
    def __init__(
        self,
        settings: SettingsService,
        *,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,  # tests only
    ) -> None:
        self._settings = settings
        self._timeout = timeout
        self._transport = transport
        self._ssl_contexts: dict[bool, ssl.SSLContext] = {}

    @property
    def configured(self) -> bool:
        """True when the NAS address, user name and password are all set."""
        return _is_configured(self._settings.current)

    async def test(self) -> str:
        """Log in, read the versions, log out. E.g. "Download Station 3.9.2 on DSM 7.2.1"."""
        async with self._session() as session:
            ds_version: str | None = None
            if session.has(_DS_INFO):
                data = await session.call(_DS_INFO, 1, "getinfo", {})
                ds_version = _short_version(data)
            dsm_version: str | None = None
            if session.has(_DSM_INFO):  # nice to have; some accounts may not read it
                try:
                    dsm_version = _short_version(await session.call(_DSM_INFO, 2, "getinfo", {}))
                except DownloadStationError as exc:
                    log.info("Could not read the DSM version on %s: %s", session.host, exc)
        text = "Download Station" + (f" {ds_version}" if ds_version else "")
        return text + (f" on DSM {dsm_version}" if dsm_version else "")

    async def add_magnet(self, magnet: str) -> None:
        """Log in, create a Download Station task for `magnet`, log out."""
        if not magnet.startswith("magnet:"):
            raise ValueError("not a magnet link")
        async with self._session() as session:
            destination = session.destination
            errors = _task_errors(destination)
            if session.has(_TASK2) and session.api(_TASK2).supports(2):
                params: dict[str, object] = {"type": "url", "url": [magnet], "create_list": False}
                if destination:
                    params["destination"] = destination
                await session.call(_TASK2, 2, "create", params, errors=errors)
            else:
                params = {"uri": magnet}
                if destination:
                    params["destination"] = destination
                await session.call(_TASK, 3, "create", params, errors=errors)
            log.info("Added a magnet task to Download Station on %s", session.host)

    @contextlib.asynccontextmanager
    async def _session(self) -> AsyncIterator[_Session]:
        settings = self._settings.current  # one snapshot for the whole operation
        if not _is_configured(settings):
            raise DownloadStationError(
                "Download Station isn't set up: enter the NAS address, user name and password "
                "in Settings."
            )
        async with await self._client(settings.ds_verify_tls) as http:
            session = _Session(http, settings.ds_url, settings.ds_destination)
            await session.discover()
            await session.login(settings.ds_username, settings.ds_password.get_secret_value())
            try:
                yield session
            finally:
                await session.logout()

    async def _client(self, verify: bool) -> httpx.AsyncClient:
        # No redirects: a redirected POST would lose its body, so a redirect is reported instead.
        headers = {"User-Agent": _USER_AGENT}
        if self._transport is not None:
            return httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout,
                follow_redirects=False,
                headers=headers,
            )
        context = self._ssl_contexts.get(verify)
        if context is None:  # loading the CA bundle blocks for a moment: keep it off the loop
            context = await asyncio.to_thread(httpx.create_ssl_context, verify=verify)
            self._ssl_contexts[verify] = context
        return httpx.AsyncClient(
            verify=context, timeout=self._timeout, follow_redirects=False, headers=headers
        )


class _Session:
    """One login on one NAS: discovered APIs, the sid, and request plumbing."""

    def __init__(self, http: httpx.AsyncClient, base_url: str, destination: str) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self.host = _host(base_url)
        self.destination = destination
        self._apis: dict[str, _Api] = {}
        self._sid: str | None = None
        self._auth_version = 2

    def has(self, name: str) -> bool:
        return name in self._apis

    def api(self, name: str) -> _Api:
        return self._apis[name]

    async def discover(self) -> None:
        not_dsm = f"No Synology DSM answered at {self.host}. Check the NAS address in Settings."
        params = {"api": _INFO_API, "version": "1", "method": "query", "query": _QUERY}
        payload = await self._request("GET", "query.cgi", params=params, not_found=not_dsm)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not (isinstance(payload, dict) and payload.get("success") is True) or not isinstance(
            data, dict
        ):
            raise DownloadStationError(not_dsm)
        for name, raw in data.items():
            if api := _parse_api(raw):
                self._apis[str(name)] = api
        if _AUTH not in self._apis:
            raise DownloadStationError(not_dsm)
        if _TASK not in self._apis and not (
            _TASK2 in self._apis and self._apis[_TASK2].supports(2)
        ):
            raise DownloadStationError(
                f"Download Station isn't installed or isn't running on the NAS at {self.host}."
            )

    async def login(self, account: str, password: str) -> None:
        version = self._apis[_AUTH].version(6)
        params = {"account": account, "passwd": password, "session": SESSION, "format": "sid"}
        data = await self.call(_AUTH, version, "login", params, errors=_AUTH_ERRORS)
        sid = data.get("sid") if isinstance(data, dict) else None
        if not isinstance(sid, str) or not sid:
            raise DownloadStationError("DSM accepted the sign-in but returned no session.")
        self._sid = sid
        self._auth_version = version

    async def logout(self) -> None:
        if self._sid is None:
            return
        try:
            await self.call(_AUTH, self._auth_version, "logout", {"session": SESSION})
        except Exception as exc:  # never mask the operation's own outcome
            reason = str(exc) if isinstance(exc, DownloadStationError) else type(exc).__name__
            log.warning("Download Station logout on %s failed: %s", self.host, reason)
        finally:
            self._sid = None

    async def call(
        self,
        name: str,
        preferred_version: int,
        method: str,
        params: Mapping[str, object],
        *,
        errors: Mapping[int, str] | None = None,
    ) -> object:
        """POST one API method (sid and secrets stay out of the URL); returns its `data`."""
        api = self._apis[name]
        form = {"api": name, "version": str(api.version(preferred_version)), "method": method}
        for key, value in params.items():
            form[key] = json.dumps(value) if api.json_params else _plain(value)
        if self._sid is not None:
            form["_sid"] = self._sid
        payload = await self._request("POST", api.path, data=form)
        if not isinstance(payload, dict):
            raise DownloadStationError(self._unexpected())
        if payload.get("success") is True:
            return payload.get("data")
        code = _error_code(payload.get("error"))
        message = None
        if code is not None:
            message = (errors or {}).get(code) or _COMMON_ERRORS.get(code)
        if message is None:
            message = f"Download Station reported error {code if code is not None else '?'}."
        raise DownloadStationError(message)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
        not_found: str | None = None,
    ) -> object:
        """Send a request and decode its JSON body. Raises happen outside `except` blocks, so no
        httpx exception (whose text may hold the URL) becomes a DownloadStationError's context."""
        url = f"{self._base}/webapi/{path}"
        response: httpx.Response | None = None
        failure = ""
        try:
            response = await self._http.request(method, url, params=params, data=data)
        except httpx.TimeoutException:
            failure = f"The NAS at {self.host} didn't answer in time."
        except httpx.ConnectError as exc:
            failure = _connect_failure(exc, self.host)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            failure = f"The connection to the NAS at {self.host} failed ({type(exc).__name__})."
        if response is None:
            raise DownloadStationError(failure)
        if response.is_redirect:
            raise DownloadStationError(_redirect_failure(response, self.host))
        if response.status_code == 404 and not_found:
            raise DownloadStationError(not_found)
        if not response.is_success:
            raise DownloadStationError(
                f"The NAS at {self.host} answered HTTP {response.status_code}."
            )
        try:
            return response.json()
        except ValueError:  # JSONDecodeError and UnicodeDecodeError
            pass
        raise DownloadStationError(not_found or self._unexpected())

    def _unexpected(self) -> str:
        return f"The NAS at {self.host} sent a reply AirGrabber doesn't understand."


def _is_configured(settings: Settings) -> bool:
    password = settings.ds_password.get_secret_value()
    return bool(settings.ds_url and settings.ds_username and password)


def _host(url: str) -> str:
    """'host:port' of an address: never its credentials, path or query."""
    try:
        parts = urlsplit(url)
        hostname, port = parts.hostname, parts.port
    except ValueError:
        return "the configured address"
    if not hostname:
        return "the configured address"
    if ":" in hostname:  # IPv6
        hostname = f"[{hostname}]"
    return f"{hostname}:{port}" if port else hostname


def _parse_api(raw: object) -> _Api | None:
    if not isinstance(raw, dict):
        return None
    path, low, high = raw.get("path"), raw.get("minVersion"), raw.get("maxVersion")
    if not isinstance(path, str) or not isinstance(low, int) or not isinstance(high, int):
        return None
    return _Api(path, low, high, json_params=str(raw.get("requestFormat", "")).upper() == "JSON")


def _plain(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list | tuple):
        return ",".join(str(item) for item in value)
    return str(value)


def _error_code(error: object) -> int | None:
    """DSM reports `"error": 400` or `"error": {"code": 400, ...}`."""
    if isinstance(error, dict):
        error = error.get("code")
    if isinstance(error, bool):
        return None
    return error if isinstance(error, int) else None


def _short_version(data: object) -> str | None:
    """'3.9.2' from '3.9.2-4382'; '7.2.1' from 'DSM 7.2.1-69057 Update 5'."""
    text = data.get("version_string") if isinstance(data, dict) else None
    if not isinstance(text, str):
        return None
    match = _VERSION_RE.search(text)
    return match.group(0) if match else None


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def _connect_failure(exc: httpx.ConnectError, host: str) -> str:
    chain = _exception_chain(exc)
    text = " ".join(str(item) for item in chain)  # inspected only, never shown
    if any(isinstance(item, ssl.SSLCertVerificationError) for item in chain) or (
        "CERTIFICATE_VERIFY_FAILED" in text
    ):
        return (
            f"Certificate not trusted: the NAS at {host} has a certificate this PC doesn't "
            "trust. Untick Verify TLS in Settings for a self-signed NAS certificate."
        )
    if any(isinstance(item, ssl.SSLError) for item in chain) or "[SSL" in text:
        return (
            f"Couldn't make a secure connection to the NAS at {host}. Check that the address "
            "uses https with the NAS's HTTPS port (usually 5001), or http with its HTTP port."
        )
    return f"Can't reach the NAS at {host}. Check the address in Settings and that the NAS is on."


def _redirect_failure(response: httpx.Response, host: str) -> str:
    target = ""
    location = response.headers.get("Location")
    if location:
        try:
            resolved = response.request.url.join(location)
            if resolved.scheme in ("http", "https") and resolved.host:
                target = f"{resolved.scheme}://{_host(str(resolved))}"
        except (httpx.InvalidURL, ValueError):
            target = ""
    if target:
        return f"The NAS at {host} redirects to {target}: use that address in Settings."
    return f"The NAS at {host} answered with a redirect: check the address in Settings."
