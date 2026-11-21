"""Play links (docs/folder-library/2026-11-16-companion-contract.md): short-lived, unguessable
plain-http links to a folder library video and its subtitles, for streaming on the phone's own
player (libVLC plays plain http without pinning) and on a TV (Cast, an LG through DLNA), which
can't do the phone connection's TLS pin or send its token.

`PhoneLibrary`'s `POST /v1/library/<video key>/play-link` makes one (`make`) on the address the
phone reached the PC on (`play_url_host`) and the port this server got (`port`: PLAY_PORTS in
order, 58762 first); this server, started and stopped with the phone connection, answers it:

- `/p/<token>/video.<ext>`: the video, `Range: bytes=N-M` / `bytes=N-` giving 206 with exactly
  those bytes (TVs ask that way), past the end 416;
- `/p/<token>/sub/<i>.vtt`: the link's subtitle `i`, an SRT as WebVTT on the way, a VTT as it
  is (any other file isn't served);
- `/c/<token>/stream.mp4[?t=<seconds>]`: a Cast sound stream (cast_stream.py, its own tokens):
  handed to `CastStreams.answer`, whose stream writes the rest of the connection itself.

`GET` and `HEAD` only, one request per connection, home addresses only (`is_home_address`). Every
answer carries `Access-Control-Allow-Origin: *` (Cast's subtitles) and DLNA's headers. A token is
`secrets.token_urlsafe(TOKEN_BYTES)`, good for PLAY_LINK_HOURS, kept in memory only (gone with the
server); an unknown or expired one is 404. A new link doesn't end the old ones; expired ones are
dropped when a new one is made. Every stat, open and read of the file (often a NAS's share) runs
in a worker thread under a time limit: a share asleep is 404, never a window that waits.

Never logged: a link, a token, an address, a path or a title; nor an exception's text.
"""

from __future__ import annotations

import asyncio
import functools
import ipaddress
import logging
import mimetypes
import re
import secrets
import stat
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http.client import responses
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple
from urllib.parse import unquote

from app.i18n import N_, t
from app.services.folder_library.scan import READ_TIMEOUT_S
from app.services.folder_library.sidecars import VTT_TYPE, srt_to_vtt, subtitle_text
from app.services.phone.copies import COPY_TYPE
from app.services.phone.server import (
    _WARN_EVERY,
    MAX_CONNECTIONS,
    READ_TIMEOUT,
    FileReply,
    Refused,
    Reply,
    close_writer,
    error_reply,
    file_head,
    is_home_address,
    listen,
    parse_head,
    range_span,
    send_file,
)

if TYPE_CHECKING:
    from app.services.phone.cast_stream import CastStreams

log = logging.getLogger(__name__)

PLAY_PORT = 58762
# Tried in this order (server.py's `listen`; 47762 is outside Windows' dynamic range, 0 a free
# port Windows picks): the links name the port it got (`port`), hello says it (`play_port`).
PLAY_PORTS = (PLAY_PORT, 47762, 0)
PLAY_LINK_HOURS = 6
TOKEN_BYTES = 24
MAX_SUBTITLE_BYTES = 5 * 1024 * 1024  # a subtitle file bigger than this isn't one
# A TV that has enough buffered stops reading for a while: that long, its connection stays.
PAUSE_S = 300.0
_MAX_HEAD = 16 * 1024
_SUBTITLE = re.compile(r"(0|[1-9][0-9]{0,5})\.vtt")
_VIDEO_TYPES = {".mkv": COPY_TYPE}  # what Python's own table doesn't know
NOT_SERVING_TEXT = N_(
    "AirGrabber's player links aren't running right now, so try again in a moment."
)
_EVERY_ANSWER = (
    ("Access-Control-Allow-Origin", "*"),  # Cast fetches the subtitles from its own page
    ("transferMode.dlna.org", "Streaming"),
    (
        "contentFeatures.dlna.org",
        "DLNA.ORG_OP=01;DLNA.ORG_CI=0;DLNA.ORG_FLAGS=01700000000000000000000000000000",
    ),
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class _Link:
    video: Path
    subtitles: tuple[Path, ...]
    expires: datetime


# What answers a connection itself once its request is read (a Cast sound stream).
Stream = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


class _Answer(NamedTuple):
    head: bytes
    body: bytes = b""
    file: FileReply | None = None  # sent after the head, in pieces
    stream: Stream | None = None  # run after the head (and writes its own)


def play_url_host(local_address: str) -> str | None:
    """How a link names this PC at `local_address` (the address the phone reached it on): an
    IPv4 address as it is (also one written as IPv6, "::ffff:a.b.c.d"), an IPv6 one in brackets
    (a zone's "%" as "%25"); None when it isn't a home address."""
    try:
        address = ipaddress.ip_address(local_address)
    except ValueError:
        return None
    if not is_home_address(local_address):
        return None
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return f"[{str(address).replace('%', '%25')}]"
    return str(address)


def not_serving() -> Reply:
    """503: no link can be made while this server isn't running (its port couldn't be bound;
    the phone connection tries again by itself)."""
    return error_reply(503, "failed", t(NOT_SERVING_TEXT))


def video_type(path: Path) -> str:
    """The Content-Type a video's extension says ("video/x-matroska", "video/mp4", …)."""
    suffix = path.suffix.lower()
    found = _VIDEO_TYPES.get(suffix) or _types().guess_type(f"video{suffix}")[0]
    return found or "application/octet-stream"


@functools.cache
def _types() -> mimetypes.MimeTypes:
    """Python's own table only: Windows' registry gives whatever programs put there."""
    return mimetypes.MimeTypes()


def subtitle_bytes(path: Path, *, convert: bool, most: int = MAX_SUBTITLE_BYTES) -> bytes | None:
    """The subtitle file's bytes (`convert`: an SRT's as WebVTT, UTF-8); None when it's over
    `most` bytes. Blocking: for a worker thread."""
    with path.open("rb") as source:
        data = source.read(most + 1)
    if len(data) > most:
        return None
    return srt_to_vtt(subtitle_text(data)).encode() if convert else data


def file_size(path: Path) -> int | None:
    """The size of the file at `path`; None when it's no file. Raises OSError (ValueError for a
    path no file can have) when it can't be reached."""
    found = path.stat()
    return found.st_size if stat.S_ISREG(found.st_mode) else None


def _plain(status: int, *extra: tuple[str, str]) -> _Answer:
    """An answer without a body."""
    head = (
        f"HTTP/1.1 {status} {responses.get(status, '')}\r\n"
        + "".join(f"{name}: {value}\r\n" for name, value in (*_EVERY_ANSWER, *extra))
        + "Content-Length: 0\r\nConnection: close\r\n\r\n"
    )
    return _Answer(head.encode())


def plain_head(status: int, *extra: tuple[str, str]) -> bytes:
    """The head of an answer without a body, with the headers every answer here carries."""
    return _plain(status, *extra).head


def _data(content_type: str, data: bytes, *, with_body: bool) -> _Answer:
    head = (
        "HTTP/1.1 200 OK\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(data)}\r\n"
        + "".join(f"{name}: {value}\r\n" for name, value in _EVERY_ANSWER)
        + "Connection: close\r\n\r\n"
    )
    return _Answer(head.encode(), data if with_body else b"")


class PlayLinks:
    def __init__(
        self, *, now: Callable[[], datetime] = _utc_now, cast: CastStreams | None = None
    ) -> None:
        """`cast`: the Cast sound streams served under `/c/` (none: those are 404)."""
        self._now = now
        self._cast = cast
        self._links: dict[str, _Link] = {}
        self._server: asyncio.Server | None = None
        self._port: int | None = None
        self._connections: set[asyncio.Task[None]] = set()
        self._next_warning: dict[str, float] = {}

    @property
    def running(self) -> bool:
        return self._server is not None

    @property
    def port(self) -> int | None:
        """The port the links are served on, while it runs (None: not running)."""
        return self._port

    def make(self, video: Path, subtitles: Sequence[Path]) -> tuple[str, datetime]:
        """A new link to `video` and its `subtitles` (served as `sub/<their place>.vtt`): its
        token and when it stops working. The links before it stay; expired ones are dropped."""
        now = self._now()
        self._links = {token: link for token, link in self._links.items() if link.expires > now}
        token = secrets.token_urlsafe(TOKEN_BYTES)
        expires = now + timedelta(hours=PLAY_LINK_HOURS)
        self._links[token] = _Link(video, tuple(subtitles), expires)
        return token, expires

    async def start(self, ports: int | Sequence[int] = PLAY_PORTS) -> None:
        """Listen on every address of this PC, on the first of `ports` that can be bound.
        Raises OSError when none can be (each in use, or kept by Windows)."""
        if self._server is not None:
            return

        async def open_on(host: str | None, port: int) -> asyncio.Server:
            return await asyncio.start_server(self._accept, host=host, port=port, limit=_MAX_HEAD)

        self._server, self._port = await listen(
            open_on, (ports,) if isinstance(ports, int) else ports
        )
        if self._cast is not None:
            self._cast.serving(self._port)
        log.info("Play link server listening on port %d", self._port)

    async def stop(self) -> None:
        """Stop listening, drop every open connection and forget every link."""
        server, self._server = self._server, None
        self._port = None
        self._links = {}
        if self._cast is not None:
            self._cast.forget()  # its ffmpegs end with the connections below
            self._cast.serving(None)
        if server is None:
            return
        server.close()
        server.abort_clients()
        connections = tuple(self._connections)
        for connection in connections:
            connection.cancel()
        await asyncio.gather(*connections, return_exceptions=True)
        await server.wait_closed()
        log.info("Play link server stopped")

    def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        if peer is None or not is_home_address(str(peer[0])):
            self._warn_now_and_then("Refused a play link connection from outside the home network")
            writer.close()
            return
        if len(self._connections) >= MAX_CONNECTIONS:
            self._warn_now_and_then("Refused a play link connection: too many are open")
            writer.close()
            return
        connection = asyncio.create_task(self._serve(reader, writer), name="play-link")
        self._connections.add(connection)
        connection.add_done_callback(self._connections.discard)

    def _warn_now_and_then(self, message: str) -> None:
        now = asyncio.get_running_loop().time()
        if now >= self._next_warning.get(message, 0.0):
            self._next_warning[message] = now + _WARN_EVERY
            log.warning(message)

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            answer = await self._answer(reader)
            if answer is not None:
                async with asyncio.timeout(READ_TIMEOUT):
                    writer.write(answer.head + answer.body)
                    await writer.drain()
                if answer.file is not None:
                    await send_file(writer, answer.file, send_timeout=PAUSE_S)
                if answer.stream is not None:
                    await answer.stream(reader, writer)
        except OSError:  # a timeout is one too
            log.debug("A play link connection ended early")
        except Exception as exc:
            log.error("Play links: answering failed: %s", type(exc).__name__)
        finally:
            await close_writer(writer)

    async def _answer(self, reader: asyncio.StreamReader) -> _Answer | None:
        """What to send back, or None when the other side went away before finishing."""
        try:
            async with asyncio.timeout(READ_TIMEOUT):
                raw_head = await reader.readuntil(b"\r\n\r\n")
            method, target, headers = parse_head(raw_head)
        except (Refused, asyncio.LimitOverrunError):
            return _plain(400)
        except TimeoutError:
            return _plain(408)
        except (asyncio.IncompleteReadError, OSError):
            return None
        if method not in ("GET", "HEAD"):
            return _plain(405, ("Allow", "GET, HEAD"))
        with_body = method == "GET"
        path, _, query = target.partition("?")
        parts = path.split("/")
        if len(parts) == 4 and parts[1] == "c" and self._cast is not None:
            head, stream = self._cast.answer(
                parts[2], unquote(parts[3]), query, with_body=with_body
            )
            return _Answer(head, stream=stream)
        if len(parts) not in (4, 5) or parts[:2] != ["", "p"]:
            return _plain(404)
        link = self._link(parts[2])
        if link is None:
            return _plain(404)
        name = [unquote(part) for part in parts[3:]]
        if len(name) == 1:
            if name[0].casefold() != f"video{link.video.suffix}".casefold():
                return _plain(404)
            return await self._video(link.video, headers, with_body=with_body)
        matched = _SUBTITLE.fullmatch(name[1]) if name[0] == "sub" else None
        if matched is None or int(matched[1]) >= len(link.subtitles):
            return _plain(404)
        return await self._subtitle(link.subtitles[int(matched[1])], with_body=with_body)

    def _link(self, token: str) -> _Link | None:
        """The link with this token while it works; None for any other."""
        link = self._links.get(token)
        if link is None:
            return None
        if link.expires <= self._now():
            del self._links[token]
            return None
        return link

    async def _video(self, path: Path, headers: dict[str, str], *, with_body: bool) -> _Answer:
        try:
            async with asyncio.timeout(READ_TIMEOUT_S):
                size = await asyncio.to_thread(file_size, path)
        except (OSError, TimeoutError, ValueError) as exc:
            log.info("Play links: a video can't be reached: %s", type(exc).__name__)
            return _plain(404)
        if size is None:
            return _plain(404)
        span = range_span(headers, size)
        if span is False:
            return _plain(416, ("Content-Range", f"bytes */{size}"))
        if span is None:
            reply = FileReply(path, video_type(path), size)
        else:
            start, end = span
            reply = FileReply(path, video_type(path), size, start=start, partial=True, end=end)
        return _Answer(file_head(reply, _EVERY_ANSWER), file=reply if with_body else None)

    async def _subtitle(self, path: Path, *, with_body: bool) -> _Answer:
        kind = path.suffix.lower()
        if kind not in (".srt", ".vtt"):
            return _plain(404)
        try:
            async with asyncio.timeout(READ_TIMEOUT_S):
                data = await asyncio.to_thread(subtitle_bytes, path, convert=kind == ".srt")
        except (OSError, TimeoutError, ValueError) as exc:
            log.info("Play links: a subtitle file can't be read: %s", type(exc).__name__)
            return _plain(404)
        if data is None:
            log.info("Play links: a subtitle file is too big to send")
            return _plain(404)
        return _data(VTT_TYPE, data, with_body=with_body)
