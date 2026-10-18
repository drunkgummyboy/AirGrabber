"""What the torrent programs on a NAS or another computer have in common: the RemoteClient
protocol, RemoteError, and the HTTP plumbing the clients share.

The clients (synology.py, qbittorrent.py, transmission.py) read the `remote_*` settings on every
operation and use a short-lived httpx client per operation, because certificate checking is a
setting (`remote_verify_tls`, for self-signed NAS certificates).

Secret hygiene: passwords travel in POST bodies or an Authorization header only. Messages and
log records name the program by host and port, never by URL, and no httpx exception (whose text
may hold the URL) is chained to, or left as the context of, a RemoteError.
"""

from __future__ import annotations

import asyncio
import ssl
from collections.abc import Mapping
from typing import Literal, Protocol
from urllib.parse import urlsplit

import httpx

from app import APP_NAME, __version__
from app.models import RemoteKind

USER_AGENT = f"{APP_NAME}/{__version__}"
CERTIFICATE_SETTING = "Verify the certificate (https)"  # the Settings checkbox, for messages


class RemoteError(Exception):
    """The remote torrent program isn't set up, can't be reached, or refused (user-facing)."""


class RemoteClient(Protocol):
    """One kind of torrent program on a NAS or another computer."""

    @property
    def kind(self) -> RemoteKind: ...

    async def test(self) -> str:
        """Connect and sign in; a short description such as "qBittorrent v5.0.2"."""
        ...

    async def add_magnet(self, magnet: str) -> None: ...

    async def add_torrent(self, content: bytes, filename: str) -> None:
        """Add a .torrent file (`filename` ends in .torrent)."""
        ...


class HttpClients:
    """Short-lived httpx clients for one operation each, honouring `remote_verify_tls`.

    No redirects: a redirected POST would lose its body, so a redirect is reported instead.
    SSL contexts are cached per setting and made off the loop (loading the CA bundle blocks).
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
    ) -> httpx.AsyncClient:
        all_headers = {"User-Agent": USER_AGENT, **(headers or {})}
        if self._transport is not None:
            return httpx.AsyncClient(
                transport=self._transport,
                timeout=self._timeout,
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
            timeout=self._timeout,
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
        return "the configured address"
    if not hostname:
        return "the configured address"
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
        return f"{program} at {host} didn't answer in time."
    if isinstance(exc, httpx.ConnectError):
        problem = tls_problem(exc)
        if problem == "certificate":
            return (
                f"Certificate not trusted: {host} has a certificate this PC doesn't trust. "
                f'Untick "{CERTIFICATE_SETTING}" in Settings for a self-signed certificate.'
            )
        if problem == "tls":
            return (
                f"Couldn't make a secure connection to {host}. Use an http:// address unless "
                f"{program} has HTTPS turned on."
            )
        return f"Couldn't reach {host}. Check the address in Settings and that {program} is on."
    return f"The connection to {program} at {host} failed ({type(exc).__name__})."


def redirect_text(response: httpx.Response, host: str) -> str:
    target = redirect_target(response)
    if target:
        return f"{host} redirects to {target}: use that address in Settings."
    return f"{host} answered with a redirect: check the address in Settings."
