"""The phone connection's small HTTPS server: JSON over HTTP/1.1, one request per connection.

It answers only paired phones (`POST /v1/pair` being how a phone becomes one) and only addresses
on the home network; `manager_only` keeps a route to a library manager's phone. Never log a
token, a request's query or body, or an exception's text: any of them can hold a token or a file's
path.

A handler answers with a `Reply` (JSON), a `FileReply`: a file sent in pieces (a phone-sized
copy, gigabytes), `Range: bytes=N-` giving 206 from byte N (`file_range`, `range_start`), so a
broken copy resumes; or a `DataReply`: bytes already read (a subtitle).

`Request.local_address` is the address the phone reached this PC on (play links are made on it).
The play link server (play_links.py) shares the reading of a request's head (`parse_head`),
`range_span` (exact `bytes=N-M`, as TVs ask), `file_head`, `send_file` and `close_writer`.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import ssl
import traceback
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from functools import partial, wraps
from http.client import responses
from pathlib import Path
from typing import Any, BinaryIO, Literal, NamedTuple, cast
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
FILE_CHUNK = 1024 * 1024  # a file's piece: read off the disk, then sent
MAX_CONNECTIONS = 32  # open at once, all sharing the loop with the window
_MAX_HEAD = 16 * 1024
_MAX_DISCARD = 4 * MAX_BODY  # thrown away after an early answer; a body just over the limit fits
_WARN_EVERY = 60.0  # seconds between warnings about one kind of refused connection
_PUBLIC_ROUTES = frozenset({("POST", "/v1/pair")})
# What a Companion can do, said on every request as a comma-separated list
# (docs/folder-library/2026-11-16-companion-contract.md).
CAPABILITY_HEADER = "x-companion-can"
FOLDER_LIBRARY = "folder-library"  # it knows the folder library's routes and answers

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
    "no_plex",
    "no_library",
    "plex_library",
    "not_manager",
    "bad_request",
    "not_found",
    "forge_starting",
    "no_longer_there",
    "no_results",
    "failed",
    "no_tmdb_key",  # the actor and title pages: no TMDB key in Settings (person.py, title.py)
    "tmdb_key_refused",  # the actor and title pages: TMDB refuses the key in Settings
    "no_ffmpeg",  # Cast sound streams: MediaForge's ffmpeg isn't on this PC (cast_stream.py)
]


@dataclass(frozen=True, slots=True)
class Request:
    """`query` also holds a route's `{id}`-like path values, which beat a query's own."""

    method: str
    path: str
    query: dict[str, str]
    body: dict[str, Any]
    phone: PairedPhone | None
    headers: Mapping[str, str] = field(default_factory=dict)  # lowercase names (Range)
    local_address: str = ""  # this PC's address the phone reached it on (the connection's own)


@dataclass(frozen=True, slots=True)
class Reply:
    status: int
    body: dict[str, Any]


@dataclass(frozen=True, slots=True)
class FileReply:
    """A file to send: the bytes from `start` to `end` (included; None: the last byte) of the
    file at `path`, `size` bytes in all (206 with Content-Range when `partial`)."""

    path: Path
    content_type: str
    size: int
    start: int = 0
    partial: bool = False
    end: int | None = None

    @property
    def last(self) -> int:
        """The last byte sent (-1 for an empty file)."""
        return self.size - 1 if self.end is None else self.end

    @property
    def length(self) -> int:
        return self.last + 1 - self.start


@dataclass(frozen=True, slots=True)
class DataReply:
    """Bytes already read (a subtitle file, small), sent as they are with a 200."""

    content_type: str
    data: bytes


AnyReply = Reply | FileReply | DataReply
Handler = Callable[[Request], Awaitable[Reply]]
Route = Callable[[Request], Awaitable[AnyReply]]  # what the server takes: a Handler too


class _Answer(NamedTuple):
    data: bytes
    may_still_come: int = 0  # bytes of the request not read yet that the other side may send
    file: FileReply | None = None  # sent after `data` (its head), in pieces


_MOST_DIGITS = 19  # a byte position past this many digits is no file's


def _range_parts(headers: Mapping[str, str]) -> tuple[int, str] | None:
    """The request's `Range: bytes=N-…`: N and what follows the dash (as sent); None when there
    is none, or it isn't one."""
    wanted = headers.get("range", "").strip()
    if not wanted:
        return None
    unit, _, spec = wanted.partition("=")
    first, dash, last = spec.partition("-")
    if unit.strip().lower() != "bytes" or not dash or not _a_number(first):
        return None
    return int(first), last.strip()


def _a_number(text: str) -> bool:
    """Plain digits a byte position can be (int() refuses the longest texts)."""
    return text.isascii() and text.isdigit() and len(text) <= _MOST_DIGITS


def range_start(headers: Mapping[str, str], size: int) -> int | None | Literal[False]:
    """Where a file of `size` bytes is to be sent from by the request's `Range: bytes=N-` (or
    `bytes=N-M`, read as to the end): N; None for the whole file (no Range, or one not
    understood); False when it starts past the end (416)."""
    parts = _range_parts(headers)
    if parts is None:
        return None
    start = parts[0]
    return False if start >= size else start


def range_span(headers: Mapping[str, str], size: int) -> tuple[int, int] | None | Literal[False]:
    """The bytes of a file of `size` bytes the request's `Range: bytes=N-M` (or `bytes=N-`: to
    the end) asks for, both ends included, the end cut to the file's: (N, M); None for the whole
    file (no Range, or one not understood); False when it starts past the end (416)."""
    parts = _range_parts(headers)
    if parts is None:
        return None
    start, last_text = parts
    if not last_text:
        last = size - 1
    elif _a_number(last_text):
        last = min(int(last_text), size - 1)
    else:
        return None
    if start >= size:
        return False
    return None if last < start else (start, last)


def file_range(path: Path, content_type: str, headers: Mapping[str, str]) -> FileReply | None:
    """The file at `path` as a FileReply, from where the request's Range asks (`range_start`);
    None when it starts past the end (416). Blocking: it stats the file."""
    size = path.stat().st_size
    start = range_start(headers, size)
    if start is False:
        return None
    if start is None:
        return FileReply(path, content_type, size)
    return FileReply(path, content_type, size, start=start, partial=True)


def knows_folders(request: Request) -> bool:
    """The Companion asking knows the folder library (`X-Companion-Can: folder-library`)."""
    said = request.headers.get(CAPABILITY_HEADER, "")
    return FOLDER_LIBRARY in (part.strip().casefold() for part in said.split(","))


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


class Refused(Exception):
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


def _not_manager() -> Reply:
    return error_reply(
        403,
        "not_manager",
        t(
            "Only a library manager's phone can do this, so ask the person who set up AirGrabber "
            "to make this phone a library manager in AirGrabber's Settings."
        ),
    )


def manager_only(handler: Handler) -> Handler:
    """`handler`, for a library manager's phone only: any other gets a `not_manager` reply."""

    @wraps(handler)
    async def for_a_manager(request: Request) -> Reply:
        if request.phone is None or not request.phone.manager:
            return _not_manager()
        return await handler(request)

    return for_a_manager


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
    # All ASCII (\u escapes, whatever the text holds); a 204 has none (a phone's HTTP library
    # refuses a body there).
    body = b"" if reply.status == 204 else json.dumps(reply.body).encode()
    head = (
        f"HTTP/1.1 {reply.status} {responses.get(reply.status, '')}\r\n"
        "Content-Type: application/json; charset=utf-8\r\n"
        f"Content-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    )
    return head.encode() + body


def file_head(reply: FileReply, extra: Sequence[tuple[str, str]] = ()) -> bytes:
    """The answer's head for `reply`, with the `extra` headers (name, value) besides."""
    status = 206 if reply.partial else 200
    head = (
        f"HTTP/1.1 {status} {responses[status]}\r\n"
        f"Content-Type: {reply.content_type}\r\n"
        f"Content-Length: {reply.length}\r\n"
        "Accept-Ranges: bytes\r\n"
    )
    if reply.partial:
        head += f"Content-Range: bytes {reply.start}-{reply.last}/{reply.size}\r\n"
    head += "".join(f"{name}: {value}\r\n" for name, value in extra)
    return (head + "Connection: close\r\n\r\n").encode()


def _data_http(reply: DataReply) -> bytes:
    head = (
        "HTTP/1.1 200 OK\r\n"
        f"Content-Type: {reply.content_type}\r\n"
        f"Content-Length: {len(reply.data)}\r\n"
        "Connection: close\r\n\r\n"
    )
    return head.encode() + reply.data


def parse_head(raw: bytes) -> tuple[str, str, dict[str, str]]:
    """(method, target, headers with lowercase names). Latin-1 can't fail on any byte."""
    request_line, *header_lines = raw.removesuffix(b"\r\n\r\n").decode("latin-1").split("\r\n")
    parts = request_line.split(" ")
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise Refused
    headers: dict[str, str] = {}
    for line in header_lines:
        name, colon, value = line.partition(":")
        if not colon:
            raise Refused
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
        raise Refused
    value = headers.get("content-length", "0")
    if not (value.isascii() and value.isdigit()):
        raise Refused
    # Too long a number can't be a size, and int() refuses the longest ones.
    if len(value) > len(str(MAX_BODY)) or int(value) > MAX_BODY:
        raise Refused(413)
    return int(value)


def _announced_size(headers: Mapping[str, str]) -> int:
    """How much body may come after answering without reading it."""
    try:
        return _body_size(headers)
    except Refused:
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
        routes: Mapping[tuple[str, str], Route],
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
            answer = await self._answer(reader, _local_address(writer))
            if answer is not None:
                async with asyncio.timeout(READ_TIMEOUT):
                    writer.write(answer.data)
                    await writer.drain()
                if answer.file is not None:
                    await send_file(writer, answer.file)
                await _discard(reader, answer.may_still_come)
        except OSError:  # a timeout is one too
            log.debug("A phone connection ended early")
        finally:
            await close_writer(writer)

    async def _answer(self, reader: asyncio.StreamReader, local_address: str) -> _Answer | None:
        """What to send back, or None when the other side went away before finishing."""
        try:
            return await self._answer_request(reader, local_address)
        except Refused as refused:
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

    async def _answer_request(
        self, reader: asyncio.StreamReader, local_address: str
    ) -> _Answer:
        deadline = asyncio.get_running_loop().time() + READ_TIMEOUT
        async with asyncio.timeout_at(deadline):
            raw_head = await reader.readuntil(b"\r\n\r\n")
        method, target, headers = parse_head(raw_head)
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
        request = Request(method, path, query, body, phone, headers, local_address)
        try:
            reply = await handler(request)
            if isinstance(reply, FileReply):
                return _Answer(file_head(reply), file=reply)
            if isinstance(reply, DataReply):
                return _Answer(_data_http(reply))
            return _Answer(_as_http(reply))
        except Exception as exc:
            _log_failure(f"answering {method} {pattern}", exc)
            return _Answer(_as_http(_failed()))

    def _find_route(self, method: str, path: str) -> tuple[str, Route, dict[str, str]] | None:
        for (route_method, pattern), handler in self._routes.items():
            if route_method == method:
                values = _path_values(pattern, path)
                if values is not None:
                    return pattern, handler, values
        return None


def _local_address(writer: asyncio.StreamWriter) -> str:
    """This PC's address the connection came in on ("" when it can't be told)."""
    own = writer.get_extra_info("sockname")
    return str(own[0]) if isinstance(own, tuple) and own else ""


def _log_failure(where: str, exc: Exception) -> None:
    """Where it went wrong and the exception's kind; never its text, which may hold secrets."""
    frames = "".join(traceback.format_tb(exc.__traceback__))
    log.error("Phone connection: %s failed: %s\n%s", where, type(exc).__name__, frames)


async def send_file(
    writer: asyncio.StreamWriter, reply: FileReply, *, send_timeout: float = READ_TIMEOUT
) -> None:
    """The file's bytes (`reply.start` to its `last`) in FILE_CHUNK pieces. Opened, read and
    closed in a worker thread (the disk, or a share, mustn't hold up the window), the open and
    each piece's read given READ_TIMEOUT, its sending `send_timeout`: a share that drops mid-copy
    ends the connection, not just the copy. Raises OSError (TimeoutError is one) when it can't go
    on."""
    opening = asyncio.ensure_future(asyncio.to_thread(_opened_at, reply.path, reply.start))
    try:
        async with asyncio.timeout(READ_TIMEOUT):
            source = await asyncio.shield(opening)
    except BaseException:
        opening.add_done_callback(_close_when_opened)  # opened too late: closed all the same
        raise
    try:
        left = reply.length
        while left > 0:
            async with asyncio.timeout(READ_TIMEOUT):
                piece = await asyncio.to_thread(source.read, min(FILE_CHUNK, left))
            if not piece:
                raise OSError("the file got shorter while it was being sent")
            left -= len(piece)
            async with asyncio.timeout(send_timeout):
                writer.write(piece)
                await writer.drain()
    finally:
        _close_later(source)


def _close_later(source: BinaryIO) -> None:
    """Close the file in a worker thread, without waiting: after a read that gave up, closing
    waits for that read to end."""
    closing = asyncio.get_running_loop().run_in_executor(None, source.close)
    closing.add_done_callback(_retrieve)


def _close_when_opened(opening: asyncio.Future[BinaryIO]) -> None:
    if not opening.cancelled() and opening.exception() is None:
        _close_later(opening.result())


def _retrieve(done: asyncio.Future[Any]) -> None:
    """An outcome nobody waits for (its exception's text may hold a path: never logged)."""
    if not done.cancelled():
        done.exception()


def _opened_at(path: Path, start: int) -> BinaryIO:
    source = path.open("rb")
    try:
        source.seek(start)
    except BaseException:
        source.close()
        raise
    return source


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


async def close_writer(writer: asyncio.StreamWriter) -> None:
    writer.close()
    try:
        async with asyncio.timeout(TLS_TIMEOUT):
            await writer.wait_closed()
    except (OSError, TimeoutError):
        writer.transport.abort()
