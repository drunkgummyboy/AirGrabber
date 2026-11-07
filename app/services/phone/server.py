"""The phone connection's small HTTPS server: JSON over HTTP/1.1, one request per connection.

It answers only paired phones (`POST /v1/pair` being how a phone becomes one) and only addresses
on the home network. Never log a token, a request's query or body, or an exception's text: any of
them can hold a token or a file's path.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import ssl
import traceback
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import partial
from http.client import responses
from pathlib import Path
from typing import Any, Literal, NamedTuple, cast
from urllib.parse import parse_qsl, unquote

from app.i18n import t
from app.models import PairedPhone
from app.services.phone.devices import DeviceStore

log = logging.getLogger(__name__)

PORT = 58760
READ_TIMEOUT = 10.0  # seconds for a whole request to arrive; also for its answer to leave
TLS_TIMEOUT = 10.0  # seconds for the TLS greeting and goodbye, each
LINGER_TIME = 2.0  # seconds to wait for what's still coming after an early answer
MAX_BODY = 64 * 1024
MAX_CONNECTIONS = 32  # open at once, all sharing the loop with the window
_MAX_HEAD = 16 * 1024
_MAX_DISCARD = 4 * MAX_BODY  # thrown away after an early answer; a body just over the limit fits
_WARN_EVERY = 60.0  # seconds between warnings about one kind of refused connection
_PUBLIC_ROUTES = frozenset({("POST", "/v1/pair")})

_HOME_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "169.254.0.0/16",  # a link without a router
        "127.0.0.0/8",
        "::1/128",
        "fe80::/10",
    )
)

ErrorCode = Literal[
    "not_paired",
    "bad_request",
    "not_found",
    "forge_starting",
    "no_longer_there",
    "no_results",
    "failed",
]


@dataclass(frozen=True, slots=True)
class Request:
    """`query` also holds a route's `{id}`-like path values, which beat a query's own."""

    method: str
    path: str
    query: dict[str, str]
    body: dict[str, Any]
    phone: PairedPhone | None


@dataclass(frozen=True, slots=True)
class Reply:
    status: int
    body: dict[str, Any]


Handler = Callable[[Request], Awaitable[Reply]]


class _Answer(NamedTuple):
    data: bytes
    may_still_come: int = 0  # bytes of the request not read yet that the other side may send


def error_reply(status: int, code: ErrorCode, message: str) -> Reply:
    """An error the app can act on; `message` is one translated sentence saying what to do."""
    return Reply(status, {"error": code, "message": message})


def is_home_address(ip: str) -> bool:
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return any(address in network for network in _HOME_NETWORKS)


class _Refused(Exception):
    """A request that can't be read; becomes a `bad_request` reply."""

    def __init__(self, status: int = 400) -> None:
        super().__init__()
        self.status = status


def _bad_request(status: int) -> Reply:
    return error_reply(
        status,
        "bad_request",
        t("AirGrabber couldn't read that request, so try again or update the Companion app."),
    )


def bad_request() -> Reply:
    """For a handler: what the phone sent makes no sense."""
    return _bad_request(400)


def _not_paired() -> Reply:
    return error_reply(
        401,
        "not_paired",
        t(
            "This phone isn't connected to AirGrabber any more, "
            "so connect it again from AirGrabber's Settings."
        ),
    )


def _not_found() -> Reply:
    return error_reply(
        404,
        "not_found",
        t("AirGrabber doesn't know that request, so update AirGrabber and the Companion app."),
    )


def _failed() -> Reply:
    return error_reply(
        500, "failed", t("Something went wrong in AirGrabber, so try again in a moment.")
    )


def _as_http(reply: Reply) -> bytes:
    body = json.dumps(reply.body).encode()  # all ASCII: \u escapes, whatever the text holds
    head = (
        f"HTTP/1.1 {reply.status} {responses.get(reply.status, '')}\r\n"
        "Content-Type: application/json; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    )
    return head.encode() + body


def _parse_head(raw: bytes) -> tuple[str, str, dict[str, str]]:
    """(method, target, headers with lowercase names). Latin-1 can't fail on any byte."""
    request_line, *header_lines = raw.removesuffix(b"\r\n\r\n").decode("latin-1").split("\r\n")
    parts = request_line.split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise _Refused
    headers: dict[str, str] = {}
    for line in header_lines:
        name, colon, value = line.partition(":")
        if not colon:
            raise _Refused
        headers[name.strip().lower()] = value.strip()
    return parts[0], parts[1], headers


def _bearer_token(headers: Mapping[str, str]) -> str | None:
    """The token sent, if it could be one: tokens are plain ASCII, and `DeviceStore.find` can't
    take every text (a lone surrogate fails to encode)."""
    scheme, _, token = headers.get("authorization", "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token or not token.isascii():
        return None
    return token


def _body_size(headers: Mapping[str, str]) -> int:
    if "transfer-encoding" in headers:
        raise _Refused
    value = headers.get("content-length", "0")
    if not (value.isascii() and value.isdigit()):
        raise _Refused
    # Too long a number can't be a size, and int() refuses the longest ones.
    if len(value) > len(str(MAX_BODY)) or int(value) > MAX_BODY:
        raise _Refused(413)
    return int(value)


def _announced_size(headers: Mapping[str, str]) -> int:
    """How much body may come after answering without reading it."""
    try:
        return _body_size(headers)
    except _Refused:
        return _MAX_DISCARD


def _parse_body(raw: bytes) -> dict[str, Any] | None:
    """The JSON object sent (nothing sent is an empty one), or None if it isn't one."""
    if not raw:
        return {}
    try:
        body = json.loads(raw)
        # JSON's \ud800 gets past the parsing as a lone surrogate, which no handler should meet.
        json.dumps(body, ensure_ascii=False).encode()
    except (ValueError, RecursionError):  # UnicodeError is a ValueError
        return None
    return body if isinstance(body, dict) else None


def _path_values(pattern: str, path: str) -> dict[str, str] | None:
    """What the route's `{name}` parts hold in this path, or None if it isn't this route."""
    wanted = pattern.split("/")
    sent = path.split("/")
    if len(wanted) != len(sent):
        return None
    values: dict[str, str] = {}
    for want, have in zip(wanted, sent, strict=True):
        if want.startswith("{") and want.endswith("}"):
            if not have:
                return None
            values[want[1:-1]] = unquote(have)
        elif want != have:
            return None
    return values


class PhoneServer:
    def __init__(
        self,
        routes: Mapping[tuple[str, str], Handler],
        devices: DeviceStore,
        cert: Path,
        key: Path,
    ) -> None:
        self._routes = routes
        self._devices = devices
        self._cert = cert
        self._key = key
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.Task[None]] = set()
        self._next_warning: dict[str, float] = {}

    @property
    def running(self) -> bool:
        return self._server is not None

    async def start(self, port: int = PORT) -> None:
        """Listen on every address of this PC. Raises OSError when that fails (port in use)."""
        if self._server is not None:
            return
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self._cert, self._key)
        # TLS starts per connection, in _serve: whoever gets refused doesn't even get a handshake.
        self._server = await asyncio.start_server(
            partial(self._accept, context), host=None, port=port, limit=_MAX_HEAD
        )
        log.info("Phone connection listening on port %d", port)

    async def stop(self) -> None:
        """Stop listening and drop every open connection, also those still being worked on."""
        server, self._server = self._server, None
        if server is None:
            return
        server.close()
        server.abort_clients()
        connections = tuple(self._connections)
        for connection in connections:
            connection.cancel()
        await asyncio.gather(*connections, return_exceptions=True)
        await server.wait_closed()
        log.info("Phone connection stopped")

    def _accept(
        self,
        context: ssl.SSLContext,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer = writer.get_extra_info("peername")
        if peer is None or not is_home_address(str(peer[0])):
            self._warn_now_and_then("Refused a connection from outside the home network")
            writer.close()
            return
        if len(self._connections) >= MAX_CONNECTIONS:
            self._warn_now_and_then("Refused a connection: too many are open")
            writer.close()
            return
        # The phone's TLS greeting may be on its way already; left to the plain reader, it's lost.
        cast(asyncio.ReadTransport, writer.transport).pause_reading()
        connection = asyncio.create_task(
            self._serve(context, reader, writer), name="phone-connection"
        )
        self._connections.add(connection)
        connection.add_done_callback(self._connections.discard)

    def _warn_now_and_then(self, message: str) -> None:
        now = asyncio.get_running_loop().time()
        if now >= self._next_warning.get(message, 0.0):
            self._next_warning[message] = now + _WARN_EVERY
            log.warning(message)

    async def _serve(
        self,
        context: ssl.SSLContext,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        # A handshake that doesn't finish never reaches the streams, so writer.wait_closed() would
        # wait for nothing and keep the connection's place taken.
        try:
            await writer.start_tls(
                context, ssl_handshake_timeout=TLS_TIMEOUT, ssl_shutdown_timeout=TLS_TIMEOUT
            )
        except Exception:
            log.debug("A phone connection never finished its TLS handshake")
            writer.transport.abort()
            return
        except asyncio.CancelledError:
            writer.transport.abort()
            raise
        try:
            answer = await self._answer(reader)
            if answer is not None:
                async with asyncio.timeout(READ_TIMEOUT):
                    writer.write(answer.data)
                    await writer.drain()
                await _discard(reader, answer.may_still_come)
        except OSError:  # a timeout is one too
            log.debug("A phone connection ended early")
        finally:
            await _close(writer)

    async def _answer(self, reader: asyncio.StreamReader) -> _Answer | None:
        """What to send back, or None when the other side went away before finishing."""
        try:
            return await self._answer_request(reader)
        except _Refused as refused:
            return _Answer(_as_http(_bad_request(refused.status)), _MAX_DISCARD)
        except asyncio.LimitOverrunError:
            return _Answer(_as_http(_bad_request(400)), _MAX_DISCARD)
        except TimeoutError:
            return _Answer(_as_http(_bad_request(408)), _MAX_DISCARD)
        except (asyncio.IncompleteReadError, OSError):
            return None
        except Exception as exc:
            _log_failure("reading a request", exc)
            return _Answer(_as_http(_failed()), _MAX_DISCARD)

    async def _answer_request(self, reader: asyncio.StreamReader) -> _Answer:
        deadline = asyncio.get_running_loop().time() + READ_TIMEOUT
        async with asyncio.timeout_at(deadline):
            raw_head = await reader.readuntil(b"\r\n\r\n")
        method, target, headers = _parse_head(raw_head)
        path, _, query_text = target.partition("?")

        found = self._find_route(method, path)
        phone = None
        if found is None or (method, found[0]) not in _PUBLIC_ROUTES:
            token = _bearer_token(headers)
            phone = self._devices.find(token) if token is not None else None
            if phone is None:
                return _Answer(_as_http(_not_paired()), _announced_size(headers))
        if found is None:
            return _Answer(_as_http(_not_found()), _announced_size(headers))
        pattern, handler, path_values = found

        size = _body_size(headers)
        async with asyncio.timeout_at(deadline):
            raw_body = await reader.readexactly(size)
        body = _parse_body(raw_body)
        if body is None:
            return _Answer(_as_http(_bad_request(400)))
        query = dict(parse_qsl(query_text, keep_blank_values=True)) | path_values
        request = Request(method, path, query, body, phone)
        try:
            return _Answer(_as_http(await handler(request)))
        except Exception as exc:
            _log_failure(f"answering {method} {pattern}", exc)
            return _Answer(_as_http(_failed()))

    def _find_route(self, method: str, path: str) -> tuple[str, Handler, dict[str, str]] | None:
        for (route_method, pattern), handler in self._routes.items():
            if route_method == method:
                values = _path_values(pattern, path)
                if values is not None:
                    return pattern, handler, values
        return None


def _log_failure(where: str, exc: Exception) -> None:
    """Where it went wrong and the exception's kind; never its text, which may hold secrets."""
    frames = "".join(traceback.format_tb(exc.__traceback__))
    log.error("Phone connection: %s failed: %s\n%s", where, type(exc).__name__, frames)


async def _discard(reader: asyncio.StreamReader, most: int) -> None:
    """Throw away what's still coming, shortly. Closing with bytes unread resets the connection,
    which can cost the phone the answer it was just sent. (TLS can't half-close here: the answer's
    Content-Length tells the phone where it ends.)"""
    try:
        async with asyncio.timeout(LINGER_TIME):
            while most > 0:
                data = await reader.read(min(most, 65536))
                if not data:
                    return
                most -= len(data)
    except TimeoutError:
        pass


async def _close(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        async with asyncio.timeout(TLS_TIMEOUT):
            await writer.wait_closed()
    except (OSError, TimeoutError):
        writer.transport.abort()
