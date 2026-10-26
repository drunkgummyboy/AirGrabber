"""Synology Download Station client (DSM Web API): a RemoteClient for magnets and .torrent files.

Protocol, per Synology's "Download Station Web API" guide: discover API paths and versions
with SYNO.API.Info (`/webapi/query.cgi`), log in with SYNO.API.Auth (`session=DownloadStation`,
`format=sid`), call the Download Station APIs with `_sid`, then log out. Tasks are created with
SYNO.DownloadStation2.Task `create` when the NAS offers it (Download Station 3.8+/DSM 7; on
`entry.cgi` with `requestFormat: JSON`, so every parameter value is JSON-encoded:
`type="url"`, `url=["magnet:…"]`, `create_list=false`), else with the DSM 6
SYNO.DownloadStation.Task `create` (`uri=magnet:…`).

A .torrent file goes up as multipart/form-data: DownloadStation2 `type="file"`,
`file=["torrent"]` (the name of the file part), `destination` ("" = the default folder) and
`create_list=false`, the file in a part named `torrent`; the old API takes it in a part named
`file`, sent last. (DSM 6.2.4 / DSM 7 removed the old API's upload; DownloadStation2 replaced it.)

Every operation logs in and out on its own short-lived httpx client, because TLS verification is
a setting. The Downloads page's `status` (the task list, every 10 s while the page shows) is the
exception: it keeps its sign-in (the discovered APIs and the sid, in memory only, for the
settings it was made with) and signs in again only when DSM ended the session. It reads the
tasks with SYNO.DownloadStation.Task `list` (`additional=detail,transfer`; documented in the
Web API guide, still offered next to DownloadStation2 on DSM 7), else with DownloadStation2
`list`. Download Station has no info hash per task: a magnet task's `uri` holds it, a .torrent
task is matched by its file name or title (app/services/download_status.py).

Secret hygiene: the password and the sid only ever travel in POST bodies, messages and log
records name the NAS by host and port only, a task's `uri` (which may hold a tracker passkey) is
read for its hash or file name and then dropped, and no httpx exception is chained to (or left as
the context of) a RemoteError.
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

from app.i18n import N_, t
from app.models import DownloadState, RemoteKind, RemoteStatus, RemoteTask, Settings
from app.services.remote import (
    CERTIFICATE_SETTING,
    DiskSpace,
    HttpClients,
    RemoteError,
    fraction,
    host_of,
    magnet_hash,
    number,
    redirect_target,
    tls_problem,
)
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

_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")

# The error texts are raised through t() (`call`).
_TWO_STEP = N_(
    "This DSM account uses 2-step verification, which AirGrabber can't sign in with. "
    "Use a DSM account without 2-step verification."
)
_EXPIRED = N_("The DSM account's password has expired. Sign in to DSM to change it.")
_NO_API = N_("The NAS doesn't offer the Download Station API AirGrabber needs.")
_SESSION_LOST = N_("The DSM session ended unexpectedly. Try again.")
_AUTH_ERRORS: Mapping[int, str] = {
    400: N_("DSM rejected the user name or password."),
    401: N_("This DSM account is disabled."),
    402: N_("This DSM account isn't allowed to sign in (permission denied)."),
    403: _TWO_STEP,
    404: _TWO_STEP,
    406: _TWO_STEP,
    407: N_("The NAS has blocked this computer's IP address (DSM auto block)."),
    408: _EXPIRED,
    409: _EXPIRED,
    410: N_("The DSM account's password must be changed. Sign in to DSM to change it."),
}
_COMMON_ERRORS: Mapping[int, str] = {
    102: _NO_API,
    103: _NO_API,
    104: _NO_API,
    105: N_("This DSM account isn't allowed to use Download Station."),
    106: _SESSION_LOST,
    107: _SESSION_LOST,
    119: _SESSION_LOST,
}


def _task_errors(destination: str) -> Mapping[int, str]:
    where = f'"{destination}"' if destination else t("its default folder")
    return {
        401: t("Download Station has reached its maximum number of tasks."),
        402: t("Download Station isn't allowed to save to {where}.", where=where),
        403: t("The Download Station destination {where} doesn't exist.", where=where),
        406: t(
            "Download Station has no default destination folder. "
            "Set one in Download Station or in AirGrabber's settings."
        ),
        407: t("Download Station couldn't save to {where}.", where=where),
    }


# The older name of RemoteError, from when Download Station was the only remote program;
# main_window.py still catches it.
DownloadStationError = RemoteError

# DSM's answers for a session that ended (no permission with this sid, timed out, ended by
# another sign-in, unknown sid): `status` signs in again once.
_SESSION_ENDED = frozenset({105, 106, 107, 119})

# Download Station's task states (SYNO.DownloadStation.Task `list`, "status").
_DS_STATES: Mapping[str, DownloadState] = {
    "waiting": DownloadState.WAITING,
    "downloading": DownloadState.DOWNLOADING,
    "paused": DownloadState.STOPPED,
    "finishing": DownloadState.DOWNLOADING,
    "finished": DownloadState.READY,
    "hash_checking": DownloadState.WAITING,
    "seeding": DownloadState.READY,
    "filehosting_waiting": DownloadState.WAITING,
    "extracting": DownloadState.DOWNLOADING,
    "error": DownloadState.PROBLEM,
}
# DownloadStation2 numbers its states. Only the first ones are taken as known (the order of the
# names above); a number of 100 or more is an error; any other number goes by the progress.
_DS2_STATES: Mapping[int, str] = {
    1: "waiting",
    2: "downloading",
    3: "paused",
    4: "finishing",
    5: "finished",
    6: "hash_checking",
}


class _ApiError(RemoteError):
    """A DSM API error answer, with its code (so `status` can tell a session that ended)."""

    def __init__(self, message: str, code: int | None) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class _KeptLogin:
    """`status`'s sign-in, for the settings it was made with (never logged or shown)."""

    key: tuple[str, str, SecretStr, bool]
    apis: Mapping[str, _Api]
    sid: str
    auth_version: int

# A multipart file part: (file name, content, content type).
_FilePart = tuple[str, bytes, str]


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
    kind = RemoteKind.SYNOLOGY

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
        self._kept: _KeptLogin | None = None  # `status`'s sign-in

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
        if dsm_version:
            return t("{program} on DSM {version}", program=text, version=dsm_version)
        return text

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

    async def add_torrent(self, content: bytes, filename: str) -> None:
        """Log in, upload a .torrent file as a Download Station task, log out."""
        async with self._session() as session:
            destination = session.destination
            errors = _task_errors(destination)
            if session.has(_TASK2) and session.api(_TASK2).supports(2):
                params: dict[str, object] = {
                    "type": "file",
                    "file": ["torrent"],  # the name of the file part below
                    "destination": destination,  # "" = Download Station's default folder
                    "create_list": False,
                }
                files = {"torrent": (filename, content, "application/x-bittorrent")}
                await session.call(_TASK2, 2, "create", params, errors=errors, files=files)
            else:
                params = {"destination": destination} if destination else {}
                files = {"file": (filename, content, "application/x-bittorrent")}
                await session.call(_TASK, 3, "create", params, errors=errors, files=files)
            log.info("Added a .torrent task to Download Station on %s", session.host)

    async def status(self, hashes: Collection[str] = ()) -> RemoteStatus:
        """Download Station's tasks with their progress (read-only). Keeps its sign-in between
        calls; signs in again once when DSM ended the session."""
        settings = self._settings.current  # one snapshot for the whole operation
        _check_configured(settings)
        key = _login_key(settings)
        async with await self._http.client(settings.remote_verify_tls) as http:
            session = _Session(http, settings.remote_url, "")
            kept = self._kept if self._kept is not None and self._kept.key == key else None
            if kept is not None:
                session.restore(kept.apis, kept.sid, kept.auth_version)
                expired = False
                try:
                    tasks = await session.list_tasks()
                except _ApiError as exc:
                    if exc.code not in _SESSION_ENDED:
                        raise
                    expired = True
                if not expired:
                    return RemoteStatus(program=self.kind.program, tasks=tuple(tasks))
                log.info("The Download Station session on %s ended; signing in again", session.host)
                self._kept = None
                session = _Session(http, settings.remote_url, "")
            await session.discover()
            await session.login(
                settings.remote_username, settings.remote_password.get_secret_value()
            )
            self._kept = session.kept(key)
            tasks = await session.list_tasks()
        return RemoteStatus(program=self.kind.program, tasks=tuple(tasks))

    async def free_space(self) -> DiskSpace | None:
        """Download Station's API doesn't report free space."""
        return None

    @contextlib.asynccontextmanager
    async def _session(self) -> AsyncIterator[_Session]:
        settings = self._settings.current  # one snapshot for the whole operation
        _check_configured(settings)
        # Download Station wants "shared folder/sub folder", without leading or trailing slashes.
        destination = settings.remote_destination.strip().strip("/").strip()
        async with await self._http.client(settings.remote_verify_tls) as http:
            session = _Session(http, settings.remote_url, destination)
            await session.discover()
            await session.login(
                settings.remote_username, settings.remote_password.get_secret_value()
            )
            try:
                yield session
            finally:
                await session.logout()


class _Session:
    """One login on one NAS: discovered APIs, the sid, and request plumbing."""

    def __init__(self, http: httpx.AsyncClient, base_url: str, destination: str) -> None:
        self._http = http
        self._base = base_url.rstrip("/")
        self.host = host_of(base_url)
        self.destination = destination
        self._apis: dict[str, _Api] = {}
        self._sid: str | None = None
        self._auth_version = 2

    def has(self, name: str) -> bool:
        return name in self._apis

    def restore(self, apis: Mapping[str, _Api], sid: str, auth_version: int) -> None:
        """Use a kept sign-in instead of discovering and signing in."""
        self._apis = dict(apis)
        self._sid = sid
        self._auth_version = auth_version

    def kept(self, key: tuple[str, str, SecretStr, bool]) -> _KeptLogin:
        """This session's sign-in, to keep (after `login`)."""
        assert self._sid is not None
        return _KeptLogin(key, dict(self._apis), self._sid, self._auth_version)

    async def list_tasks(self) -> list[RemoteTask]:
        """Every Download Station task with its state and progress."""
        if self.has(_TASK) and self.api(_TASK).supports(1):
            params: dict[str, object] = {
                "additional": "detail,transfer",
                "offset": 0,
                "limit": -1,  # all of them
            }
            data = await self.call(_TASK, 1, "list", params)
        else:  # DSM 7 boxes offering only DownloadStation2
            params = {"additional": ["detail", "transfer"], "offset": 0, "limit": -1}
            data = await self.call(_TASK2, 2, "list", params)
        raw = None
        if isinstance(data, dict):
            raw = data.get("tasks", data.get("task"))
        if not isinstance(raw, list):
            raise DownloadStationError(self._unexpected())
        return [task for item in raw if (task := _remote_task(item)) is not None]

    def api(self, name: str) -> _Api:
        return self._apis[name]

    async def discover(self) -> None:
        not_dsm = t(
            "No Synology DSM answered at {host}. Check the NAS address in Settings.", host=self.host
        )
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
                t(
                    "Download Station isn't installed or isn't running on the NAS at {host}.",
                    host=self.host,
                )
            )

    async def login(self, account: str, password: str) -> None:
        version = self._apis[_AUTH].version(6)
        params = {"account": account, "passwd": password, "session": SESSION, "format": "sid"}
        data = await self.call(_AUTH, version, "login", params, errors=_AUTH_ERRORS)
        sid = data.get("sid") if isinstance(data, dict) else None
        if not isinstance(sid, str) or not sid:
            raise DownloadStationError(t("DSM accepted the sign-in but returned no session."))
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
        files: Mapping[str, _FilePart] | None = None,
    ) -> object:
        """POST one API method (sid and secrets stay out of the URL); returns its `data`.
        With `files`, as multipart/form-data: api, version and method first, the files last."""
        api = self._apis[name]
        form = {"api": name, "version": str(api.version(preferred_version)), "method": method}
        for key, value in params.items():
            form[key] = json.dumps(value) if api.json_params else _plain(value)
        if self._sid is not None:
            form["_sid"] = self._sid
        payload = await self._request("POST", api.path, data=form, files=files)
        if not isinstance(payload, dict):
            raise DownloadStationError(self._unexpected())
        if payload.get("success") is True:
            return payload.get("data")
        code = _error_code(payload.get("error"))
        message = None
        if code is not None:
            message = (errors or {}).get(code) or _COMMON_ERRORS.get(code)
        if message is None:
            message = t(
                "Download Station reported error {code}.", code=code if code is not None else "?"
            )
        raise _ApiError(t(message), code)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
        files: Mapping[str, _FilePart] | None = None,
        not_found: str | None = None,
    ) -> object:
        """Send a request and decode its JSON body. Raises happen outside `except` blocks, so no
        httpx exception (whose text may hold the URL) becomes a RemoteError's context."""
        url = f"{self._base}/webapi/{path}"
        response: httpx.Response | None = None
        failure = ""
        try:
            response = await self._http.request(
                method, url, params=params, data=data, files=files
            )
        except httpx.TimeoutException:
            failure = t("The NAS at {host} didn't answer in time.", host=self.host)
        except httpx.ConnectError as exc:
            failure = _connect_failure(exc, self.host)
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            failure = t(
                "The connection to the NAS at {host} failed ({error}).",
                host=self.host,
                error=type(exc).__name__,
            )
        if response is None:
            raise DownloadStationError(failure)
        if response.is_redirect:
            raise DownloadStationError(_redirect_failure(response, self.host))
        if response.status_code == 404 and not_found:
            raise DownloadStationError(not_found)
        if not response.is_success:
            raise DownloadStationError(
                t(
                    "The NAS at {host} answered HTTP {status}.",
                    host=self.host,
                    status=response.status_code,
                )
            )
        try:
            return response.json()
        except ValueError:  # JSONDecodeError and UnicodeDecodeError
            pass
        raise DownloadStationError(not_found or self._unexpected())

    def _unexpected(self) -> str:
        return t("The NAS at {host} sent a reply AirGrabber doesn't understand.", host=self.host)


def _is_configured(settings: Settings) -> bool:
    password = settings.remote_password.get_secret_value()
    return bool(settings.remote_url and settings.remote_username and password)


def _check_configured(settings: Settings) -> None:
    if not _is_configured(settings):
        raise DownloadStationError(
            t(
                "Download Station isn't set up: enter the NAS address, user name and password "
                "in Settings."
            )
        )


def _login_key(settings: Settings) -> tuple[str, str, SecretStr, bool]:
    """What a kept sign-in depends on (SecretStr compares the passwords themselves)."""
    return (
        settings.remote_url,
        settings.remote_username,
        settings.remote_password,
        settings.remote_verify_tls,
    )


def _remote_task(raw: object) -> RemoteTask | None:
    """A task of SYNO.DownloadStation.Task / DownloadStation2.Task `list`. Its `uri` gives the
    info hash (a magnet) or the .torrent's file name, and is dropped."""
    if not isinstance(raw, dict):
        return None
    title = raw.get("title")
    additional = raw.get("additional")
    additional = additional if isinstance(additional, dict) else {}
    detail = additional.get("detail")
    detail = detail if isinstance(detail, dict) else {}
    transfer = additional.get("transfer")
    transfer = transfer if isinstance(transfer, dict) else {}
    uri = detail.get("uri")
    uri = uri.strip() if isinstance(uri, str) else ""
    status = _status_name(raw.get("status"))
    progress = fraction(number(transfer.get("size_downloaded")), number(raw.get("size")))
    state = ds_state(status, progress)
    if state is DownloadState.READY:
        progress = 1.0
    return RemoteTask(
        name=title if isinstance(title, str) else "",
        info_hash=magnet_hash(uri),
        file_name=_file_name(uri),
        progress=progress,
        state=state,
    )


def _status_name(value: object) -> str:
    """DS1's status name; DownloadStation2's number as such a name ("error" from 100 on)."""
    if isinstance(value, str):
        return value.strip().lower()
    if isinstance(value, int) and not isinstance(value, bool):
        if value >= 100:
            return "error"
        return _DS2_STATES.get(value, "")
    return ""


def ds_state(status: str, progress: float) -> DownloadState:
    """A task's DownloadState from its status name; an unknown one goes by its progress."""
    state = _DS_STATES.get(status)
    if state is None:
        return DownloadState.READY if progress >= 1.0 else DownloadState.WAITING
    if state is DownloadState.STOPPED and progress >= 1.0:
        return DownloadState.READY
    return state


def _file_name(uri: str) -> str | None:
    """The file name of a task made from a .torrent file; None for a magnet or a web link."""
    if not uri or uri[:7].lower() == "magnet:" or "://" in uri:
        return None
    name = uri.replace("\\", "/").rsplit("/", 1)[-1].strip()
    return name or None


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


def _connect_failure(exc: httpx.ConnectError, host: str) -> str:
    problem = tls_problem(exc)
    if problem == "certificate":
        return t(
            "Certificate not trusted: the NAS at {host} has a certificate this PC doesn't "
            'trust. Untick "{setting}" in Settings for a self-signed NAS certificate.',
            host=host,
            setting=t(CERTIFICATE_SETTING),
        )
    if problem == "tls":
        return t(
            "Couldn't make a secure connection to the NAS at {host}. Check that the address "
            "uses https with the NAS's HTTPS port (usually 5001), or http with its HTTP port.",
            host=host,
        )
    return t(
        "Can't reach the NAS at {host}. Check the address in Settings and that the NAS is on.",
        host=host,
    )


def _redirect_failure(response: httpx.Response, host: str) -> str:
    target = redirect_target(response)
    if target:
        return t(
            "The NAS at {host} redirects to {target}: use that address in Settings.",
            host=host,
            target=target,
        )
    return t("The NAS at {host} answered with a redirect: check the address in Settings.", host=host)
