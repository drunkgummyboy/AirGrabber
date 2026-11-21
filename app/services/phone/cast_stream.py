"""Cast sound streams (docs/companion/2026-11-21-cast-sound-stream.md): a film or an episode for a
Cast TV (Google TV / Chromecast), its picture copied as it is and only its sound converted to AAC.

A Cast receiver plays no E-AC-3 / Dolby / DTS / TrueHD: a 4K film cast as it is plays without
sound, and Plex's own conversion always converts 4K pictures for those TVs, which the NAS can't keep
up with. Copying the picture and converting the sound is light work for this PC.

`POST /v1/cast-stream` (`{"video", "audio", "channels", "part"}`, a phone connection route) finds
the file on THIS PC, never through Plex: a Plex ratingKey's as ▶ Play does (`PlexService.cast_video`
says the server's path of the version the phone chose (`part`) or ▶ Play's own choice; the
PlaybackService's `locate` finds it through MediaForge's Series / Movies folders), a folder library
video's (`movie:603`, `episode:1399:1:2`) from the folder library (`own_file`, as the copies' local
source). ffprobe reads it once (the picture's codec, each sound track's channels, the length), then
a link is made: `http://<the address the phone used>:<the play link server's port>/c/<token>/
stream.mp4` (58762 unless that couldn't be bound: `serving`), good for PLAY_LINK_HOURS, in memory
only. The answer also says the picture's codec, height and HDR and the sound's codec before and
after (docs/companion/2026-11-21-portability-contract.md). A share that didn't answer in time
(PlayProblem.TIMED_OUT) is 504 `folder_timeout`, not 404.

The play link server (play_links.py) hands `/c/…` to `answer`: `GET …/stream.mp4[?t=<seconds>]`
runs the video tools' ffmpeg (`stream_args`; app/services/media_tools.py) as a subprocess of the
loop and pumps its fragmented MP4 to the TV in chunks (HTTP/1.1 chunked, the next piece only once
the last one left: backpressure). No Range: every request is a 200 from `t`. HEAD gives the headers without starting ffmpeg. A TV that
goes away, a newer request on the same token, more than MAX_STREAMS at once (the oldest goes) and
the server stopping each end ffmpeg, which is killed and reaped.

Never logged: a link, a token, an address, a path, a file name, a title, a TMDB id, ffmpeg's own
words (its stderr is read so it can't block, and dropped) or an exception's text. A Plex ratingKey
may be logged as the copies do; a folder key only as its kind.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import re
import secrets
import subprocess
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.parse import parse_qsl

from app.i18n import N_, t
from app.models.plex import LibraryKind, PlexTitle, PlexVideo
from app.services.folder_library.playing import canonical_item, logged_key
from app.services.media_tools import TOOLS_COMING_TEXT
from app.services.phone.library import TIMED_OUT
from app.services.phone.play_links import (
    PAUSE_S,
    PLAY_LINK_HOURS,
    TOKEN_BYTES,
    Stream,
    not_serving,
    plain_head,
    play_url_host,
)
from app.services.phone.server import (
    READ_TIMEOUT,
    Reply,
    Request,
    Route,
    bad_request,
    error_reply,
    folder_timeout,
)
from app.services.playback import Located, PlayOutcome, PlayProblem
from app.services.plex import PlexError

log = logging.getLogger(__name__)

STREAM_NAME = "stream.mp4"
STREAM_TYPE = "video/mp4"
MAX_STREAMS = 2  # ffmpegs at once, across every link: a third ends the oldest
MAX_AUDIO = 63  # the highest sound track a phone may ask for
MAX_PART_KEY = 500
MAX_START_S = 7 * 24 * 3600.0  # no film is longer than a week
STEREO_RATE, SURROUND_RATE = "256k", "384k"
PROBE_TIMEOUT_S = 30.0  # ffprobe reading the file's head (a NAS asleep wakes in that time)
QUICK_PROBE_S = 5.0  # a play link's codecs (`quick_probe`): the share has just answered
FIRST_BYTES_S = 60.0  # for ffmpeg's first piece of the stream (it opens and seeks first)
REAP_S = 5.0  # for a killed ffmpeg to be gone
CHUNK = 64 * 1024
_PLEX_KEY = re.compile(r"[0-9]{1,12}")
# A stream converted on the way: DLNA's "no byte seeking" and "converted" flags.
_STREAM_HEADERS = (
    ("Content-Type", STREAM_TYPE),
    ("Transfer-Encoding", "chunked"),
    ("Accept-Ranges", "none"),
    ("Cache-Control", "no-store"),
    ("Access-Control-Allow-Origin", "*"),
    ("transferMode.dlna.org", "Streaming"),
    (
        "contentFeatures.dlna.org",
        "DLNA.ORG_OP=00;DLNA.ORG_CI=1;DLNA.ORG_FLAGS=01700000000000000000000000000000",
    ),
)

# Without the video tools (they come with AirGrabber, app/services/media_tools.py): what the
# tools' fetcher says (being fetched, or couldn't be yet and tried again), this without one.
NO_FFMPEG_TEXT = TOOLS_COMING_TEXT
NOT_FOUND_TEXT = N_(
    "AirGrabber can't reach this video's file from this PC right now, so try again in a moment."
)
# The phone connection's own sentences (service.py, plex.py's callers): one Dutch entry each.
NO_PLEX_TEXT = N_("AirGrabber isn't connected to Plex, so connect Plex in AirGrabber on your PC.")
PLEX_AWAY_TEXT = N_("Plex didn't answer, so try again in a moment.")


def _utc_now() -> datetime:
    return datetime.now(UTC)


# -- what ffmpeg is asked ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Probed:
    """What ffprobe says of a file, as far as the stream and the phone's choice need: the
    picture's codec (lower case), each sound track's channels (in the file's order: `0:a:<n>`),
    the length (None: not said), the picture's height (None: not said), whether it is HDR
    (HDR10 / HLG's colour transfer or Dolby Vision's side data), each sound track's codec (lower
    case, the same order) and the default sound track's place (None: no sound)."""

    video_codec: str
    sounds: tuple[int, ...]
    duration_s: float | None
    height: int | None = None
    hdr: bool | None = None
    sound_codecs: tuple[str, ...] = ()
    default_sound: int | None = None

    def sound_codec(self, place: int | None) -> str | None:
        """The codec of sound track `place` (None: no sound, or not said)."""
        if place is None or not 0 <= place < len(self.sound_codecs):
            return None
        return self.sound_codecs[place] or None


_HDR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67"})  # HDR10 (and HDR10+), HLG


def read_probe(data: dict[str, Any]) -> Probed | None:
    """ffprobe's JSON (`-show_streams -show_format`) → Probed; None without a picture (a cover
    picture isn't one)."""
    streams = [one for one in data.get("streams", []) if isinstance(one, dict)]
    video = next(
        (
            one
            for one in streams
            if one.get("codec_type") == "video"
            and not (one.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    if video is None:
        return None
    format_ = data.get("format")
    duration = _seconds((format_ or {}).get("duration") if isinstance(format_, dict) else None)
    sounds = [one for one in streams if one.get("codec_type") == "audio"]
    default = next(
        (
            place
            for place, one in enumerate(sounds)
            if (one.get("disposition") or {}).get("default")
        ),
        0 if sounds else None,
    )
    return Probed(
        video_codec=str(video.get("codec_name", "")).lower(),
        sounds=tuple(_count(one.get("channels")) for one in sounds),
        duration_s=duration or _seconds(video.get("duration")),
        height=_count(video.get("height")) or None,
        hdr=_is_hdr(video),
        sound_codecs=tuple(str(one.get("codec_name", "")).lower() for one in sounds),
        default_sound=default,
    )


def _is_hdr(video: dict[str, Any]) -> bool:
    if str(video.get("color_transfer", "")).lower() in _HDR_TRANSFERS:
        return True
    side_data = video.get("side_data_list")
    return isinstance(side_data, list) and any(
        isinstance(one, dict) and "dovi" in str(one.get("side_data_type", "")).lower()
        for one in side_data
    )


def _seconds(value: Any) -> float | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def _count(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def stream_args(
    ffmpeg: Path,
    source: Path,
    *,
    start_s: float = 0.0,
    audio: int | None = 0,
    channels: int = 2,
    hevc: bool = False,
) -> list[str]:
    """ffmpeg's command for a stream from `start_s`: the first picture (`0:V:0`: never a cover
    picture) copied, tagged `hvc1` when it's HEVC (Apple's and Cast's MP4 tag; the muxer's default
    `hev1` isn't played, and the tag can't go on another codec), the sound track `audio` (None:
    the file has none) as AAC in stereo (256k) or 5.1 (`channels` 6, 384k), fragmented MP4 to
    stdout. `-ss` before `-i`: the copied picture starts at the keyframe at or before `start_s`;
    `-avoid_negative_ts make_zero` moves both tracks together so the first picture is at 0."""
    args = [str(ffmpeg), "-nostdin", "-hide_banner", "-loglevel", "error"]
    if start_s > 0:
        args += ["-ss", f"{start_s:.3f}"]
    args += ["-i", str(source), "-map", "0:V:0"]
    if audio is not None:
        args += ["-map", f"0:a:{audio}"]
    args += ["-c:v", "copy"]
    if hevc:
        args += ["-tag:v", "hvc1"]
    if audio is not None:
        rate = SURROUND_RATE if channels == 6 else STEREO_RATE
        args += ["-c:a", "aac", "-b:a", rate, "-ac", "6" if channels == 6 else "2"]
    args += [
        "-avoid_negative_ts", "make_zero", "-max_muxing_queue_size", "9999",
        "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof", "pipe:1",
    ]  # fmt: skip
    return args


# -- running programs ----------------------------------------------------------------------------

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


class Process(Protocol):
    """What the stream needs of a running ffmpeg (asyncio's Process; tests fake it)."""

    @property
    def stdout(self) -> asyncio.StreamReader | None: ...

    @property
    def stderr(self) -> asyncio.StreamReader | None: ...

    @property
    def returncode(self) -> int | None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


Start = Callable[[Sequence[str]], Awaitable[Process]]
Run = Callable[[Sequence[str], float], Awaitable[tuple[int, bytes]]]


async def _spawn(args: Sequence[str]) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=_NO_WINDOW,
    )


async def _start(args: Sequence[str]) -> Process:
    """ffmpeg streaming (normal priority: it has to keep up with the TV)."""
    return await _spawn(args)


async def _run(args: Sequence[str], timeout_s: float) -> tuple[int, bytes]:
    """A short run (ffprobe): (exit code, what it printed); -1 when it didn't end in time."""
    process = await _spawn(args)
    try:
        async with asyncio.timeout(timeout_s):
            out, _ = await process.communicate()
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        return -1, b""
    return process.returncode or 0, out


async def _drain(stream: asyncio.StreamReader | None) -> None:
    """Read a pipe to its end and drop it (ffmpeg's stderr holds paths: never logged)."""
    if stream is None:
        return
    with contextlib.suppress(OSError):
        while await stream.read(CHUNK):
            pass


async def _reap(process: Process) -> int | None:
    """Kill the process unless it has ended, and wait (REAP_S at most) until it is gone."""
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()
    try:
        async with asyncio.timeout(REAP_S):
            return await process.wait()
    except TimeoutError:
        log.warning("Cast stream: ffmpeg didn't end in time after being stopped")
        return None


# -- the links -----------------------------------------------------------------------------------


class CastPlex(Protocol):
    @property
    def connected(self) -> bool: ...

    async def cast_video(
        self, key: str, part_key: str | None = None
    ) -> tuple[LibraryKind, PlexVideo] | None: ...


class CastPlayback(Protocol):
    async def locate(self, title: PlexTitle, video: PlexVideo) -> Located | PlayOutcome: ...


# A folder video's file now; TIMED_OUT (library.py's) when a share didn't answer in time.
OwnFile = Callable[[str], Awaitable[Path | Literal["timed_out"] | None]]


@dataclass(frozen=True, slots=True)
class _Link:
    path: Path
    audio: int | None  # None: the file has no sound track
    channels: int
    hevc: bool
    logged: str  # the video's key as the log may hold it
    expires: datetime


@dataclass(eq=False, slots=True)
class _Running:
    token: str
    task: asyncio.Task[Any]  # the connection pumping it
    process: Process | None = None
    ended: str = ""  # why it was ended from outside ("" while it runs its course)


class _Refused(Exception):
    def __init__(self, reply: Reply) -> None:
        super().__init__()
        self.reply = reply


class CastStreams:
    def __init__(
        self,
        plex: CastPlex,
        playback: CastPlayback,
        tools: Path,
        *,
        own_file: OwnFile | None = None,
        missing_text: Callable[[], str] | None = None,
        start: Start = _start,
        run: Run = _run,
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        """`tools`: the video tools' ffmpeg bin folder (as the phone-sized copies';
        app/services/media_tools.py). `own_file(key)`: the folder library's file of a video key
        that is there now (None: no folder library). `missing_text()`: what to say while the
        tools aren't there (`MediaTools.missing_text`). `start` / `run` / `now`: for tests (no
        real ffmpeg)."""
        self._plex = plex
        self._playback = playback
        self._ffmpeg = tools / "ffmpeg.exe"
        self._ffprobe = tools / "ffprobe.exe"
        self._own_file = own_file
        self._missing_text = missing_text
        self._start = start
        self._run = run
        self._now = now
        self._links: dict[str, _Link] = {}
        self._running: list[_Running] = []  # the oldest first
        self._port: int | None = None  # the play link server's, while it runs (`serving`)

    def serving(self, port: int | None) -> None:
        """The play link server listens on `port` now (None: it stopped); the links name it."""
        self._port = port

    @property
    def available(self) -> bool:
        """The video tools' ffmpeg and ffprobe are there (`/v1/hello`'s `cast_sound`)."""
        return self._ffmpeg.is_file() and self._ffprobe.is_file()

    @property
    def streams(self) -> int:
        """ffmpegs running now."""
        return len(self._running)

    def routes(self) -> dict[tuple[str, str], Route]:
        return {("POST", "/v1/cast-stream"): self._make}

    def forget(self) -> None:
        """Every link stops working (the play link server stopped; its connections, and so the
        ffmpegs, are ended by it)."""
        self._links = {}

    # -- making a link -------------------------------------------------------------------------

    async def _make(self, request: Request) -> Reply:
        try:
            video, audio, channels, part = _wanted(request.body)
        except _Refused as refused:
            return refused.reply
        host = play_url_host(request.local_address)
        if host is None:
            return bad_request()  # the phone server always knows it: can't happen through it
        if not self.available:
            text = self._missing_text() if self._missing_text is not None else t(NO_FFMPEG_TEXT)
            return error_reply(409, "no_ffmpeg", text)
        try:
            path, plex_duration = await self._file_of(video, part)
            probed = await self._probe(path)
        except _Refused as refused:
            return refused.reply
        if probed.sounds and audio >= len(probed.sounds):
            return bad_request()  # no such sound track
        port = self._port
        if port is None:  # its port couldn't be bound (or it stopped meanwhile)
            return not_serving()
        sound = audio if probed.sounds else None
        surround = channels == 6 and sound is not None and probed.sounds[sound] >= 6
        now = self._now()
        self._links = {token: link for token, link in self._links.items() if link.expires > now}
        token = secrets.token_urlsafe(TOKEN_BYTES)
        expires = now + timedelta(hours=PLAY_LINK_HOURS)
        logged = logged_key(video)
        self._links[token] = _Link(
            path, sound, 6 if surround else 2, probed.video_codec == "hevc", logged, expires
        )
        log.info(
            "Cast stream for %s made (%s picture, %d sound tracks, %s)",
            logged,
            probed.video_codec or "unknown",
            len(probed.sounds),
            "5.1" if surround else "stereo",
        )
        return Reply(
            200,
            {
                "url": f"http://{host}:{port}/c/{token}/{STREAM_NAME}",
                "duration_s": probed.duration_s or plex_duration,
                "expires_at": expires.isoformat(),
                # For the phone's choice (docs/companion/2026-11-21-portability-contract.md):
                # the picture as it goes (copied), the chosen sound track as it was and as it goes.
                "video_codec": probed.video_codec or None,
                "height": probed.height,
                "hdr": probed.hdr,
                "audio_codec": probed.sound_codec(sound),
                "audio_codec_out": "aac" if sound is not None else None,
            },
        )

    async def _file_of(self, video: str, part: str | None) -> tuple[Path, float | None]:
        """The video's file from this PC and Plex's length of it (None for a folder video).
        Raises _Refused with the answer when there's none."""
        item = canonical_item(video)
        if item is not None:
            found = await self._own_file(item) if self._own_file is not None else None
            if found == TIMED_OUT:  # a share asleep: the file may be there
                log.info("Cast stream: a %s's folder didn't answer in time", logged_key(item))
                raise _Refused(folder_timeout())
            if not isinstance(found, Path):
                log.info("Cast stream: no %s file in the folders can be reached", logged_key(item))
                raise _Refused(_not_found())
            return found, None
        if not self._plex.connected:
            raise _Refused(error_reply(409, "no_plex", t(NO_PLEX_TEXT)))
        try:
            said = await self._plex.cast_video(video, part)
        except PlexError as exc:
            log.info("Cast stream: Plex didn't say about %s (%s)", video, type(exc).__name__)
            raise _Refused(error_reply(502, "failed", t(PLEX_AWAY_TEXT))) from None
        if said is None:
            log.info("Cast stream: Plex has no video %s", video)
            raise _Refused(_not_found())
        kind, plex_video = said
        located = await self._playback.locate(
            PlexTitle(kind=kind, key=video, title=plex_video.title), plex_video
        )
        if isinstance(located, PlayOutcome):
            log.info("Cast stream: %s %s not here (%s)", kind, video, located.problem)
            if located.problem == PlayProblem.NOT_CONNECTED:
                raise _Refused(error_reply(409, "no_plex", t(NO_PLEX_TEXT)))
            if located.problem == PlayProblem.PLEX_AWAY:
                raise _Refused(error_reply(502, "failed", t(PLEX_AWAY_TEXT)))
            if located.problem == PlayProblem.TIMED_OUT:  # a share asleep: the file may be there
                raise _Refused(folder_timeout())
            raise _Refused(_not_found())
        length = plex_video.duration_ms / 1000 if plex_video.duration_ms > 0 else None
        return Path(located.path), length

    async def _probe(self, path: Path) -> Probed:
        """What ffprobe says of the file; _Refused (404) when it can't read it in time."""
        probed = await self._probed(path, PROBE_TIMEOUT_S, "Cast stream")
        if probed is None:
            raise _Refused(_not_found())
        return probed

    async def quick_probe(self, path: Path) -> Probed | None:
        """What a quick ffprobe (QUICK_PROBE_S) says of the file (a play link's codecs); None
        without the video tools, or when it couldn't say in time."""
        if not self._ffprobe.is_file():
            return None
        return await self._probed(path, QUICK_PROBE_S, "Play link")

    async def _probed(self, path: Path, timeout_s: float, what: str) -> Probed | None:
        """ffprobe's word on the file within `timeout_s`; None when it has none (its output
        holds the file's path: never logged)."""
        args = [
            str(self._ffprobe), "-v", "error", "-print_format", "json",
            "-show_streams", "-show_format", str(path),
        ]  # fmt: skip
        try:
            code, out = await self._run(args, timeout_s)
        except OSError as exc:
            log.warning("%s: ffprobe didn't start (%s)", what, type(exc).__name__)
            return None
        probed: Probed | None = None
        if code == 0:
            with contextlib.suppress(ValueError):
                data = json.loads(out)
                probed = read_probe(data) if isinstance(data, dict) else None
        if probed is None:
            log.info("%s: ffprobe couldn't read the file (%d)", what, code)
        return probed

    # -- the stream (the play link server's /c/…) ------------------------------------------------

    def answer(
        self, token: str, name: str, query: str, *, with_body: bool
    ) -> tuple[bytes, Stream | None]:
        """What the play link server sends for `/c/<token>/<name>?<query>`: a head, and for a
        GET that works the stream that follows it (it writes its own head)."""
        link = self._link(token)
        if link is None or name != STREAM_NAME:
            return plain_head(404), None
        start_s = _start_of(query)
        if start_s is None:
            return plain_head(400), None
        if not with_body:
            return _stream_head(), None

        async def stream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await self._stream(token, link, start_s, reader, writer)

        return b"", stream

    def _link(self, token: str) -> _Link | None:
        link = self._links.get(token)
        if link is None:
            return None
        if link.expires <= self._now():
            del self._links[token]
            return None
        return link

    async def _stream(
        self,
        token: str,
        link: _Link,
        start_s: float,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        task = asyncio.current_task()
        assert task is not None
        for earlier in [one for one in self._running if one.token == token]:
            await self._end(earlier, "a newer request")
        while len(self._running) >= MAX_STREAMS:
            await self._end(self._running[0], "too many at once")
        me = _Running(token, task)
        self._running.append(me)
        helpers: list[asyncio.Task[None]] = []
        try:
            try:
                process = await self._start(
                    stream_args(
                        self._ffmpeg,
                        link.path,
                        start_s=start_s,
                        audio=link.audio,
                        channels=link.channels,
                        hevc=link.hevc,
                    )
                )
            except OSError as exc:
                log.warning("Cast stream: ffmpeg didn't start (%s)", type(exc).__name__)
                await _send_head(writer, plain_head(502))
                return
            me.process = process
            log.info("Cast stream for %s started (%d running)", link.logged, len(self._running))
            helpers.append(asyncio.create_task(_drain(process.stderr), name="cast-stderr"))
            gone = asyncio.Event()
            helpers.append(asyncio.create_task(_watch(reader, process, gone), name="cast-watch"))
            await self._pump(link, process, writer, gone)
        finally:
            reaping = asyncio.ensure_future(_reap(me.process)) if me.process else None
            for helper in helpers:
                helper.cancel()
            if me in self._running:
                self._running.remove(me)
            if reaping is not None:  # to its end even when this connection is cancelled again
                await asyncio.shield(reaping)

    async def _pump(
        self, link: _Link, process: Process, writer: asyncio.StreamWriter, gone: asyncio.Event
    ) -> None:
        """ffmpeg's stdout to the TV: the head once its first piece is there, then each piece
        as one chunk, the next only when the last one left."""
        stdout = process.stdout
        assert stdout is not None
        try:
            async with asyncio.timeout(FIRST_BYTES_S):
                piece = await stdout.read(CHUNK)
        except TimeoutError:
            log.info("Cast stream for %s: ffmpeg gave nothing in time", link.logged)
            await _send_head(writer, plain_head(502))
            return
        if not piece:
            if not gone.is_set():
                code = await _reap(process)
                log.info("Cast stream for %s: ffmpeg ended at once (%s)", link.logged, code)
                await _send_head(writer, plain_head(502))
            return
        sent = 0
        head = _stream_head()
        try:
            while piece:
                async with asyncio.timeout(PAUSE_S):  # a TV with enough buffered reads later
                    writer.write(head + b"%x\r\n%b\r\n" % (len(piece), piece))
                    await writer.drain()
                head = b""
                sent += len(piece)
                piece = await stdout.read(CHUNK)
        except OSError as exc:  # a timeout is one too
            log.info(
                "Cast stream for %s: the TV stopped taking it (%s, %d MB sent)",
                link.logged, type(exc).__name__, sent // (1024 * 1024),
            )  # fmt: skip
            return
        code = await _reap(process)
        megabytes = sent // (1024 * 1024)
        if gone.is_set():
            log.info("Cast stream for %s: the TV went away (%d MB sent)", link.logged, megabytes)
        elif code == 0:
            with contextlib.suppress(OSError):
                async with asyncio.timeout(READ_TIMEOUT):
                    writer.write(b"0\r\n\r\n")  # the last chunk: the whole stream went
                    await writer.drain()
            log.info("Cast stream for %s ended (%d MB sent)", link.logged, megabytes)
        else:  # no last chunk: the TV sees it was cut short
            log.warning("Cast stream for %s: ffmpeg ended with %s", link.logged, code)

    async def _end(self, running: _Running, why: str) -> None:
        """End a stream from outside (its connection cancelled: ffmpeg is killed and reaped),
        and wait until it has."""
        if not running.ended:
            running.ended = why
            log.info("Cast stream ended: %s", why)
            running.task.cancel()
        await asyncio.wait({running.task}, timeout=REAP_S + 1)
        if running in self._running:  # one that didn't end in time counts no longer
            self._running.remove(running)


async def _watch(reader: asyncio.StreamReader, process: Process, gone: asyncio.Event) -> None:
    """The TV's side of the connection: once it closes (nothing more is read), ffmpeg goes, so
    the pump sees its end at once even while ffmpeg is still opening the file."""
    with contextlib.suppress(OSError):
        while await reader.read(CHUNK):
            pass
    gone.set()
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError, OSError):
            process.kill()


async def _send_head(writer: asyncio.StreamWriter, head: bytes) -> None:
    with contextlib.suppress(OSError):
        async with asyncio.timeout(READ_TIMEOUT):
            writer.write(head)
            await writer.drain()


def _stream_head() -> bytes:
    return (
        "HTTP/1.1 200 OK\r\n"
        + "".join(f"{name}: {value}\r\n" for name, value in _STREAM_HEADERS)
        + "Connection: close\r\n\r\n"
    ).encode()


def _start_of(query: str) -> float | None:
    """Where the stream starts by the query's `t` (seconds, ≥ 0; 0 without one); None when `t`
    isn't one."""
    found = [value for name, value in parse_qsl(query, keep_blank_values=True) if name == "t"]
    if not found:
        return 0.0
    try:
        start_s = float(found[-1])
    except ValueError:
        return None
    return start_s if math.isfinite(start_s) and 0 <= start_s <= MAX_START_S else None


def _wanted(body: dict[str, Any]) -> tuple[str, int, int, str | None]:
    """(video, audio, channels, part) of a request's body; raises _Refused (400) for one that
    makes no sense."""
    video = body.get("video")
    audio = body.get("audio", 0)
    channels = body.get("channels", 2)
    part = body.get("part")
    if not isinstance(video, str) or not (
        _PLEX_KEY.fullmatch(video) or canonical_item(video) is not None
    ):
        raise _Refused(bad_request())
    if type(audio) is not int or not 0 <= audio <= MAX_AUDIO:
        raise _Refused(bad_request())
    if type(channels) is not int or channels not in (2, 6):
        raise _Refused(bad_request())
    if part is not None and (not isinstance(part, str) or not 0 < len(part) <= MAX_PART_KEY):
        raise _Refused(bad_request())
    return video, audio, channels, part


def _not_found() -> Reply:
    return error_reply(404, "not_found", t(NOT_FOUND_TEXT))
