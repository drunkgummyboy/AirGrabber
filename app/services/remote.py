"""What the torrent programs on a NAS or another computer have in common: the RemoteClient
protocol, RemoteError, and the HTTP plumbing the clients share.

The clients (synology.py, qbittorrent.py, transmission.py) read the `remote_*` settings on every
operation and use a short-lived httpx client per operation, because certificate checking is a
setting (`remote_verify_tls`, for self-signed NAS certificates).

Secret hygiene: passwords travel in POST bodies or an Authorization header only. Messages and
log records name the program by host and port, never by URL, and no httpx exception (whose text
may hold the URL) is chained to, or left as the context of, a RemoteError.

Beside the Downloads page's protocols (RemoteStatusClient, RemoteTaskActions), RemoteTaskFiles is
the magnet guard's (app/services/program_guard.py): a task's file names and its removal with
what it downloaded.
"""

from __future__ import annotations

import asyncio
import base64
import re
import ssl
from collections.abc import Collection, Mapping
from datetime import UTC, datetime
from typing import Literal, NamedTuple, Protocol, runtime_checkable
from urllib.parse import parse_qsl, urlsplit

import httpx

from app import APP_NAME, __version__
from app.i18n import N_, t
from app.models import RemoteKind, RemoteStatus, RemoteTask, Settings

USER_AGENT = f"{APP_NAME}/{__version__}"
# The Downloads page's status calls (`RemoteStatusClient`): a NAS that hangs is given up on
# soon, and the page says so (adding a grab keeps HttpClients' own, longer limit).
STATUS_TIMEOUT = httpx.Timeout(8.0, connect=3.0)
# The Settings checkbox, for messages (through t(), as every message here).
CERTIFICATE_SETTING = N_("Verify the certificate (https)")


class RemoteError(Exception):
    """The remote torrent program isn't set up, can't be reached, or refused (user-facing)."""


class ReleaseRefused(RemoteError):
    """The remote program works but turned down this one release (qBittorrent's "Fails." /
    409 / 415 on torrents/add, Transmission's torrent-add result): another release may go. A
    one-click download tries its next pick; the automatic downloads move on to the next
    episode, as for a ReleaseError (app/services/downloads.py)."""


class RemoteClient(Protocol):
    """One kind of torrent program on a NAS or another computer."""

    @property
    def kind(self) -> RemoteKind: ...

    async def test(self) -> str:
        """Connect and sign in; a short description such as "qBittorrent v5.0.2"."""
        ...

    async def add_magnet(self, magnet: str, *, movie: bool = False) -> None:
        """Add a magnet link; a film (`movie`) to the films destination (`destination`)."""
        ...

    async def add_torrent(self, content: bytes, filename: str, *, movie: bool = False) -> None:
        """Add a .torrent file (`filename` ends in .torrent); a film as `add_magnet`."""
        ...


def destination(settings: Settings, *, movie: bool) -> str:
    """Where the program saves a grab, as Settings has it: a film in `movie_remote_destination`
    when that is filled in, everything else (and a film without it) in `remote_destination`;
    "" = the program's own default folder."""
    films = settings.movie_remote_destination.strip() if movie else ""
    return films or settings.remote_destination.strip()


class DiskSpace(NamedTuple):
    """Free (and, when known, total) bytes on the torrent program's download disk."""

    free: int
    total: int | None = None


@runtime_checkable
class RemoteStatusClient(Protocol):
    """A RemoteClient that can also tell what its tasks are doing (the Downloads page).

    Read-only calls, made every few seconds while the page shows: a client keeps its sign-in
    (session id or cookie, in memory only) between them instead of signing in for each.
    """

    @property
    def kind(self) -> RemoteKind: ...

    async def status(self, hashes: Collection[str] = ()) -> RemoteStatus:
        """The program's tasks with their progress. `hashes` (lowercase hex): the ones asked
        about, when every one has a hash; a program may then answer with just those. Raises
        RemoteError."""
        ...

    async def free_space(self) -> DiskSpace | None:
        """Free space on the program's download disk; None when the program doesn't say.
        Raises RemoteError."""
        ...


@runtime_checkable
class RemoteTaskActions(Protocol):
    """A RemoteClient that can pause, resume and remove its tasks (the Downloads page's ⋯
    menu), by `RemoteTask.task_id`, on the status calls' kept sign-in. Removing never deletes
    a file the program finished; see each client for what happens to an unfinished one."""

    @property
    def kind(self) -> RemoteKind: ...

    async def pause(self, task_ids: Collection[str]) -> None:
        """Pause (stop) these tasks. Raises RemoteError."""
        ...

    async def resume(self, task_ids: Collection[str]) -> None:
        """Resume (start) these tasks. Raises RemoteError."""
        ...

    async def remove(self, task_ids: Collection[str]) -> None:
        """Remove these tasks from the program, keeping their files. A task that is gone
        already counts as removed. Raises RemoteError."""
        ...


class FilesUnknown(RemoteError):
    """This program (or this version of it) can't tell AirGrabber a task's files at all, as
    opposed to not knowing them yet (`RemoteTaskFiles.file_names` answering None)."""


@runtime_checkable
class RemoteTaskFiles(Protocol):
    """A RemoteClient that can tell a task's files and remove a task with what it downloaded
    (app/services/program_guard.py: a magnet that turns out to hold a program), on the status
    calls' kept sign-in."""

    @property
    def kind(self) -> RemoteKind: ...

    async def file_names(self, task: RemoteTask) -> tuple[str, ...] | None:
        """The names of the files `task` (from `status`) holds, without their folders; None
        while the program doesn't know them yet (a magnet still fetching its metadata) or no
        longer has the task. Raises RemoteError (FilesUnknown: it never can)."""
        ...

    async def remove_with_files(self, task_ids: Collection[str]) -> bool:
        """Remove these tasks and delete what they downloaded. True when the program deleted
        everything, finished files too; False when a file it finished stays where the program
        put it (Download Station: only an unfinished download goes with its task). A task that
        is gone already counts as removed. Raises RemoteError."""
        ...


class HttpClients:
    """Short-lived httpx clients for one operation each, honouring `remote_verify_tls`.

    No redirects: a redirected POST would lose its body, so a redirect is reported instead.
    SSL contexts are cached per setting and made off the loop (loading the CA bundle blocks).
    `client(timeout=…)` overrides the time limit for one operation (STATUS_TIMEOUT).
    """

    def __init__(
        self,
        *,
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,  # tests only
    ) -> None:
        self._timeout = timeout
        self._transport = transport
        self._ssl_contexts: dict[bool, ssl.SSLContext] = {}

    async def client(
        self,
        verify: bool,
        *,
        headers: Mapping[str, str] | None = None,
        auth: httpx.Auth | None = None,
        timeout: httpx.Timeout | float | None = None,
    ) -> httpx.AsyncClient:
        all_headers = {"User-Agent": USER_AGENT, **(headers or {})}
        limit = timeout if timeout is not None else self._timeout
        if self._transport is not None:
            return httpx.AsyncClient(
                transport=self._transport,
                timeout=limit,
                follow_redirects=False,
                headers=all_headers,
                auth=auth,
            )
        context = self._ssl_contexts.get(verify)
        if context is None:
            context = await asyncio.to_thread(httpx.create_ssl_context, verify=verify)
            self._ssl_contexts[verify] = context
        return httpx.AsyncClient(
            verify=context,
            timeout=limit,
            follow_redirects=False,
            headers=all_headers,
            auth=auth,
        )


def host_of(url: str) -> str:
    """'host:port' of an address: never its credentials, path or query."""
    try:
        parts = urlsplit(url)
        hostname, port = parts.hostname, parts.port
    except ValueError:
        return t("the configured address")
    if not hostname:
        return t("the configured address")
    if ":" in hostname:  # IPv6
        hostname = f"[{hostname}]"
    return f"{hostname}:{port}" if port else hostname


def _exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain:
        chain.append(current)
        current = current.__cause__ or current.__context__
    return chain


def tls_problem(exc: httpx.ConnectError) -> Literal["certificate", "tls"] | None:
    """Why a connection failed, when TLS is to blame: an untrusted certificate, or another TLS
    failure (often https on an http port). The exception texts are inspected, never shown."""
    chain = _exception_chain(exc)
    text = " ".join(str(item) for item in chain)
    if any(isinstance(item, ssl.SSLCertVerificationError) for item in chain) or (
        "CERTIFICATE_VERIFY_FAILED" in text
    ):
        return "certificate"
    if any(isinstance(item, ssl.SSLError) for item in chain) or "[SSL" in text:
        return "tls"
    return None


def redirect_target(response: httpx.Response) -> str:
    """'https://host:port' a redirect leads to (no path or query), or '' when unknown."""
    location = response.headers.get("Location")
    if not location:
        return ""
    try:
        resolved = response.request.url.join(location)
    except (httpx.InvalidURL, ValueError):
        return ""
    if resolved.scheme in ("http", "https") and resolved.host:
        return f"{resolved.scheme}://{host_of(str(resolved))}"
    return ""


def failure_text(exc: httpx.HTTPError | httpx.InvalidURL, host: str, program: str) -> str:
    """The message for a request that got no answer from `program` at `host` (qBittorrent,
    Transmission; Download Station words its own)."""
    if isinstance(exc, httpx.TimeoutException):
        return t("{program} at {host} didn't answer in time.", program=program, host=host)
    if isinstance(exc, httpx.ConnectError):
        problem = tls_problem(exc)
        if problem == "certificate":
            return t(
                "Certificate not trusted: {host} has a certificate this PC doesn't trust. "
                'Untick "{setting}" in Settings for a self-signed certificate.',
                host=host,
                setting=t(CERTIFICATE_SETTING),
            )
        if problem == "tls":
            return t(
                "Couldn't make a secure connection to {host}. Use an http:// address unless "
                "{program} has HTTPS turned on.",
                host=host,
                program=program,
            )
        return t(
            "Couldn't reach {host}. Check the address in Settings and that {program} is on.",
            host=host,
            program=program,
        )
    return t(
        "The connection to {program} at {host} failed ({error}).",
        program=program,
        host=host,
        error=type(exc).__name__,
    )


def redirect_text(response: httpx.Response, host: str) -> str:
    target = redirect_target(response)
    if target:
        return t("{host} redirects to {target}: use that address in Settings.", host=host, target=target)
    return t("{host} answered with a redirect: check the address in Settings.", host=host)


_HEX_HASH = re.compile(r"[0-9a-fA-F]{40}")
_BASE32_HASH = re.compile(r"[A-Za-z2-7]{32}")


def normal_hash(value: object) -> str | None:
    """A BitTorrent v1 info hash as 40 lowercase hex characters (base32 converted); None for
    anything else."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    if _HEX_HASH.fullmatch(text):
        return text.lower()
    if _BASE32_HASH.fullmatch(text):
        return base64.b32decode(text.upper()).hex()
    return None


def magnet_hash(link: str) -> str | None:
    """The v1 info hash in a magnet link (`xt=urn:btih:…`); None without one. The link itself
    is never kept, logged or shown."""
    if not link[:7].lower() == "magnet:":
        return None
    query = link.split("?", 1)[1] if "?" in link else ""
    for key, value in parse_qsl(query):
        if key.lower().startswith("xt") and value.lower().startswith("urn:btih:"):
            found = normal_hash(value[len("urn:btih:") :])
            if found is not None:
                return found
    return None


def base_name(path: str) -> str:
    """A file's name without its folders ("Show/Season 1/x.mkv" → "x.mkv"), either slash."""
    return path.replace("\\", "/").rsplit("/", 1)[-1].strip()


def fraction(done: float | None, total: float | None) -> float:
    """`done` / `total` clamped to 0…1 (0 without a total)."""
    if done is None or not total or total <= 0:
        return 0.0
    return clamp_progress(done / total)


def clamp_progress(value: float) -> float:
    return min(1.0, max(0.0, float(value)))


def number(value: object) -> float | None:
    """A JSON number, or a number in a string ("9427312332"); None otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def byte_count(value: object) -> int | None:
    """A task's size in bytes (a number or a number in a string); None when unknown."""
    found = number(value)
    if found is None or found < 0 or found != found:  # NaN
        return None
    return int(found)


NO_ETA = 8_640_000  # qBittorrent's "infinity" (100 days): a time left this long isn't known


def seconds_left(value: object) -> int | None:
    """A task's time left in seconds (qBittorrent's and Transmission's `eta`); None when not
    known: negative (Transmission's -1 "not available", -2 "unknown") or NO_ETA and over."""
    found = number(value)
    if found is None or found < 0 or found != found or found >= NO_ETA:  # NaN
        return None
    return int(found)


def epoch_time(value: object) -> datetime | None:
    """A Unix time in seconds (Download Station's create_time, qBittorrent's added_on,
    Transmission's addedDate) as an aware UTC datetime; None for 0, -1 or nonsense."""
    seconds = number(value)
    if seconds is None or seconds <= 0:
        return None
    try:
        return datetime.fromtimestamp(seconds, UTC)
    except (OverflowError, OSError, ValueError):
        return None
