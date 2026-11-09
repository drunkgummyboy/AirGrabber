"""Phone-sized copies (the owner, 2026-11-09; contract: the Companion's
docs/2026-11-09-phone-sized-copies.md).

A film Plex has only in 4K takes ~18 GB on a phone. The NAS can't shrink it in time, but this PC's
graphics card can (NVENC): the phone asks for a copy (`POST /v1/phone-copies`), AirGrabber fetches the
film's tallest version from Plex over the home network and converts it to a ~5 GB 1080p HEVC copy,
which the phone then copies from here (`GET /v1/phone-copies/<key>/file`, resumable) and drops
(`DELETE`). One job at a time; the files live in AirGrabber's local data folder (`phone-copies`),
never on a synced drive. A copy nobody touched for STALE_DAYS goes.

The recipe: CUDA decoding, `scale_cuda` to fit 1920 × 1080 (a 10-bit picture stays 10-bit, its
colour tags (HDR10) are kept), `hevc_nvenc` at ~6 Mbit/s (10 at the peaks); sound copied when it's
AAC / AC-3 / E-AC-3 (and other kinds a phone plays), TrueHD / DTS / FLAC / PCM to E-AC-3 640k;
subtitles and fonts copied; Matroska out. Should the card refuse to decode the picture, the same
again with the processor decoding. A film in Dolby Vision only (no HDR10 picture under it) fails
with a plain sentence rather than wrong colours.

ffmpeg and ffprobe are MediaForge's trailer tools (`AppPaths.forge / "tools" / "ffmpeg" / "bin"`):
without them, or without a card that encodes (`probe`, once), `available` is False and
`GET /v1/hello` says so (`phone_copies`). Logs give keys, sizes and durations: never a title, a
path, an address or ffmpeg's own words (they hold paths).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from app.i18n import N_, t
from app.models.plex import PlexFilmFile
from app.services.phone.server import (
    FileReply,
    Reply,
    Request,
    Route,
    bad_request,
    error_reply,
    file_range,
)

if TYPE_CHECKING:
    from app.services.plex import PlexService

log = logging.getLogger(__name__)

FOLDER = "phone-copies"  # in the data folder
STALE_DAYS = 3
CLEAN_EVERY_S = 3600.0
FETCH_SHARE = 25  # of the job's 100: fetching from Plex; converting is the rest
MAX_WIDTH, MAX_HEIGHT = 1920, 1080
VIDEO_RATE, VIDEO_PEAK, VIDEO_BUFFER = "6M", "10M", "20M"
SOUND_RATE = "640k"
OUT_BYTES_PER_S = 10_000_000 / 8  # the peak rate: room kept on the disk for the copy
DISK_MARGIN = 2 * 1024**3
PROBE_TIMEOUT_S = 30.0
COPY_TYPE = "video/x-matroska"
_KEY = re.compile(r"\d{1,12}")
_KEPT_SOUND = frozenset({"aac", "ac3", "eac3", "mp3", "opus", "vorbis", "mp2"})
_TEXT_SUBTITLES_TO_SRT = frozenset({"mov_text"})  # MP4's text: Matroska takes it as SRT

State = Literal["waiting", "fetching", "converting", "ready", "failed"]

NOT_POSSIBLE_TEXT = N_("This PC can't make phone-sized copies.")
NOT_THERE_TEXT = N_("Plex doesn't have this film any more.")
SMALL_ALREADY_TEXT = N_("This film is already small enough for the phone.")
NO_ROOM_TEXT = N_("There isn't enough free space on this PC to make the copy.")
FETCH_FAILED_TEXT = N_("Your PC couldn't get the film from Plex.")
DOLBY_VISION_TEXT = N_(
    "This film is in Dolby Vision only, and a phone-sized copy would get its colours wrong."
)
CONVERT_FAILED_TEXT = N_("Your PC couldn't convert this film.")


# -- what ffmpeg is asked ------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceInfo:
    """What ffprobe says of the fetched file, as far as the recipe needs."""

    width: int
    height: int
    ten_bit: bool
    duration_s: float
    primaries: str = ""
    transfer: str = ""
    matrix: str = ""
    dolby_vision_only: bool = False
    sounds: tuple[tuple[str, int], ...] = ()  # (codec, channels) per sound track
    subtitles: tuple[str, ...] = ()  # codec per subtitle track


def fit(width: int, height: int) -> tuple[int, int]:
    """The size within MAX_WIDTH × MAX_HEIGHT keeping the shape, even numbers; a smaller
    picture keeps its own."""
    if width <= MAX_WIDTH and height <= MAX_HEIGHT:
        return width - width % 2, height - height % 2
    scale = min(MAX_WIDTH / width, MAX_HEIGHT / height)
    return max(2, round(width * scale / 2) * 2), max(2, round(height * scale / 2) * 2)


def read_probe(data: dict[str, Any]) -> SourceInfo | None:
    """ffprobe's JSON (`-show_streams -show_format`) → SourceInfo; None without a picture."""
    streams = [s for s in data.get("streams", []) if isinstance(s, dict)]
    video = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video"
            and not (s.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    if video is None:
        return None
    pix_fmt = str(video.get("pix_fmt", ""))
    dolby = next(
        (side for side in video.get("side_data_list", []) or [] if _is_dolby_vision(side)), None
    )
    # A Dolby Vision file without a picture of its own under it (profile 5: compatibility 0).
    dolby_only = dolby is not None and int(dolby.get("dv_bl_signal_compatibility_id", 0) or 0) == 0
    duration = _seconds(data.get("format", {}).get("duration")) or _seconds(video.get("duration"))
    return SourceInfo(
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        ten_bit="10" in pix_fmt or "p010" in pix_fmt,
        duration_s=duration,
        primaries=_tag(video.get("color_primaries")),
        transfer=_tag(video.get("color_transfer")),
        matrix=_tag(video.get("color_space")),
        dolby_vision_only=dolby_only,
        sounds=tuple(
            (str(s.get("codec_name", "")), int(s.get("channels") or 0))
            for s in streams
            if s.get("codec_type") == "audio"
        ),
        subtitles=tuple(
            str(s.get("codec_name", "")) for s in streams if s.get("codec_type") == "subtitle"
        ),
    )


def _is_dolby_vision(side: Any) -> bool:
    kind = str(side.get("side_data_type", "")).lower() if isinstance(side, dict) else ""
    return "dovi" in kind or "dolby vision" in kind


def _seconds(value: Any) -> float:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return 0.0


def _tag(value: Any) -> str:
    text = str(value or "")
    return "" if text in ("unknown", "reserved", "unspecified") else text


def converted_sound(codec: str) -> bool:
    """Whether a sound track becomes E-AC-3 (TrueHD, DTS, FLAC, PCM and whatever else a phone
    may not play); AAC, AC-3, E-AC-3 and the like are copied."""
    return codec not in _KEPT_SOUND


def ffmpeg_args(
    ffmpeg: Path, source: Path, target: Path, info: SourceInfo, *, card_decodes: bool = True
) -> list[str]:
    """The conversion (see the module docstring). `card_decodes` False: the processor decodes,
    the card still encodes."""
    width, height = fit(info.width, info.height)
    args = [str(ffmpeg), "-hide_banner", "-nostdin", "-y", "-nostats", "-progress", "pipe:1"]
    if card_decodes:
        args += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
    args += ["-i", str(source)]
    args += ["-map", "0:v:0", "-map", "0:a?", "-map", "0:s?", "-map", "0:t?"]
    args += ["-map_metadata", "0", "-map_chapters", "0"]
    if card_decodes:
        args += ["-vf", f"scale_cuda=w={width}:h={height}"]
    else:
        pixels = "p010le" if info.ten_bit else "yuv420p"
        args += ["-vf", f"scale=w={width}:h={height}:flags=lanczos,format={pixels}"]
    args += [
        "-c:v", "hevc_nvenc", "-preset", "p5", "-tune", "hq", "-rc", "vbr",
        "-b:v", VIDEO_RATE, "-maxrate", VIDEO_PEAK, "-bufsize", VIDEO_BUFFER,
        "-profile:v", "main10" if info.ten_bit else "main",
    ]  # fmt: skip
    for option, value in (
        ("-color_primaries", info.primaries),
        ("-color_trc", info.transfer),
        ("-colorspace", info.matrix),
    ):
        if value:
            args += [option, value]
    args += ["-c:a", "copy"]
    for index, (codec, channels) in enumerate(info.sounds):
        if converted_sound(codec):
            args += [f"-c:a:{index}", "eac3", f"-b:a:{index}", SOUND_RATE]
            if channels > 6:
                args += [f"-ac:a:{index}", "6"]  # E-AC-3's encoder goes to 5.1
    args += ["-c:s", "copy"]
    for index, codec in enumerate(info.subtitles):
        if codec in _TEXT_SUBTITLES_TO_SRT:
            args += [f"-c:s:{index}", "srt"]
    args += ["-c:t", "copy", "-max_muxing_queue_size", "9999", "-f", "matroska", str(target)]
    return args


def progress_seconds(line: str) -> float | None:
    """How far ffmpeg's `-progress` says it is (an `out_time_us=` line), in seconds."""
    name, _, value = line.strip().partition("=")
    if name not in ("out_time_us", "out_time_ms") or not value.isdigit():
        return None
    return int(value) / 1_000_000  # both are microseconds, whatever the name says


# -- running programs ----------------------------------------------------------------------------

_QUIET = (
    subprocess.CREATE_NO_WINDOW | subprocess.BELOW_NORMAL_PRIORITY_CLASS
    if sys.platform == "win32"
    else 0
)


async def _start(args: Sequence[str]) -> asyncio.subprocess.Process:
    return await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        creationflags=_QUIET,
    )


async def _run(args: Sequence[str], timeout_s: float) -> tuple[int, bytes]:
    """A short run: (exit code, what it printed). -1 when it didn't end in time."""
    process = await _start(args)
    try:
        async with asyncio.timeout(timeout_s):
            out, _ = await process.communicate()
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        return -1, b""
    return process.returncode or 0, out


async def _convert(
    args: Sequence[str], duration_s: float, on_progress: Callable[[float], None]
) -> int:
    """ffmpeg converting, its `-progress` read as it goes (0..1 to `on_progress`); its exit
    code. Killed when cancelled."""
    process = await _start(args)

    async def drain() -> None:  # what it writes to stderr (paths: never logged)
        assert process.stderr is not None
        while await process.stderr.read(65536):
            pass

    drainer = asyncio.create_task(drain())
    try:
        assert process.stdout is not None
        while line := await process.stdout.readline():
            done = progress_seconds(line.decode("ascii", "replace"))
            if done is not None and duration_s > 0:
                on_progress(min(1.0, done / duration_s))
        await process.wait()
        await drainer
        return process.returncode or 0
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        drainer.cancel()
        raise


# -- the jobs ------------------------------------------------------------------------------------


class _Failed(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass
class _Job:
    key: str
    state: State = "waiting"
    progress: int = 0
    size: int | None = None
    message: str | None = None
    touched: float = field(default_factory=time.time)

    def answer(self) -> dict[str, Any]:
        answer: dict[str, Any] = {"key": self.key, "state": self.state, "progress": self.progress}
        if self.state == "ready" and self.size is not None:
            answer["size"] = self.size
        if self.state == "failed" and self.message:
            answer["message"] = self.message
        return answer


Run = Callable[[Sequence[str], float], Awaitable[tuple[int, bytes]]]
Convert = Callable[[Sequence[str], float, Callable[[float], None]], Awaitable[int]]


class PhoneCopies:
    def __init__(
        self,
        plex: PlexService,
        folder: Path,
        tools: Path,
        *,
        run: Run = _run,
        convert: Convert = _convert,
        clock: Callable[[], float] = time.time,
        free_space: Callable[[Path], int] = lambda path: shutil.disk_usage(path).free,
    ) -> None:
        """`folder`: where the copies go (local data). `tools`: ffmpeg's bin folder. The rest:
        for tests."""
        self._plex = plex
        self._folder = folder
        self._ffmpeg = tools / "ffmpeg.exe"
        self._ffprobe = tools / "ffprobe.exe"
        self._run = run
        self._convert = convert
        self._clock = clock
        self._free_space = free_space
        self._jobs: dict[str, _Job] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._current: tuple[str, asyncio.Task[None]] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._card: bool | None = None  # NVENC works (None: not tried yet)
        self._started = False

    @property
    def available(self) -> bool:
        """ffmpeg is there and the card encodes (`start` tried it)."""
        return bool(self._card) and self._ffmpeg.is_file() and self._ffprobe.is_file()

    def routes(self) -> dict[tuple[str, str], Route]:
        return {
            ("POST", "/v1/phone-copies"): self._ask,
            ("GET", "/v1/phone-copies/{key}"): self._status,
            ("GET", "/v1/phone-copies/{key}/file"): self._file,
            ("DELETE", "/v1/phone-copies/{key}"): self._drop,
        }

    # -- starting and stopping -----------------------------------------------------------------

    async def start(self) -> None:
        """Once the phone connection runs: the copies left from before, the card tried, the
        worker started. Never raises."""
        if self._started:
            return
        self._started = True
        try:
            await asyncio.to_thread(self._folder.mkdir, parents=True, exist_ok=True)
            await self._pick_up()
            self._card = await self._probe()
        except Exception as exc:
            log.warning("Phone-sized copies can't start (%s)", type(exc).__name__)
            self._card = False
        log.info("Phone-sized copies: %s", "possible" if self.available else "not possible here")
        self._worker = asyncio.create_task(self._work(), name="phone-copies")

    async def aclose(self) -> None:
        """AirGrabber closes: the job running stops (ffmpeg killed); its files go next time."""
        tasks = [task for task in (self._worker,) if task is not None]
        if self._current is not None:
            tasks.append(self._current[1])
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _probe(self) -> bool:
        """Whether the card encodes: a tiny black picture through hevc_nvenc."""
        if not self._ffmpeg.is_file() or not self._ffprobe.is_file():
            return False
        code, _ = await self._run(
            [
                str(self._ffmpeg), "-hide_banner", "-nostdin", "-f", "lavfi",
                "-i", "color=c=black:s=256x144:d=0.2", "-c:v", "hevc_nvenc", "-f", "null", "-",
            ],
            PROBE_TIMEOUT_S,
        )  # fmt: skip
        return code == 0

    async def _pick_up(self) -> None:
        """Finished copies from before are ready again; half-made files go."""
        for path in await asyncio.to_thread(lambda: list(self._folder.iterdir())):
            name = path.name
            key, _, rest = name.partition(".")
            if rest == "mkv" and _KEY.fullmatch(key):
                stat = path.stat()
                self._jobs[key] = _Job(
                    key, "ready", 100, size=stat.st_size, touched=stat.st_mtime
                )
            else:
                with contextlib.suppress(OSError):
                    await asyncio.to_thread(path.unlink)
        await self._clean()

    # -- the phone's requests ------------------------------------------------------------------

    async def _ask(self, request: Request) -> Reply:
        key, kind, title = (request.body.get(name) for name in ("key", "kind", "title"))
        if not isinstance(key, str) or not _KEY.fullmatch(key) or kind != "movie":
            return bad_request()
        if title is not None and (not isinstance(title, str) or len(title) > 300):
            return bad_request()
        job = self._jobs.get(key)
        if job is None:
            if not self.available:
                return error_reply(409, "failed", t(NOT_POSSIBLE_TEXT))
            job = self._jobs[key] = _Job(key, touched=self._clock())
            self._queue.put_nowait(key)
            log.info("Phone-sized copy %s asked for (%d waiting)", key, self._queue.qsize())
        job.touched = self._clock()
        return Reply(200, job.answer())

    async def _status(self, request: Request) -> Reply:
        job = self._job(request)
        if job is None:
            return _unknown()
        job.touched = self._clock()
        return Reply(200, job.answer())

    async def _file(self, request: Request) -> Reply | FileReply:
        job = self._job(request)
        if job is None:
            return _unknown()
        job.touched = self._clock()
        if job.state != "ready":
            return error_reply(
                409, "failed", t("The phone-sized copy isn't ready yet, so wait a moment.")
            )
        try:
            reply = await asyncio.to_thread(file_range, self._copy(job.key), COPY_TYPE, request.headers)
        except OSError:
            return _unknown()
        if reply is None:
            return error_reply(416, "bad_request", t("That part of the copy doesn't exist."))
        return reply

    async def _drop(self, request: Request) -> Reply:
        key = request.query.get("key", "")
        if _KEY.fullmatch(key):
            await self._forget(key)
        return Reply(204, {})

    def _job(self, request: Request) -> _Job | None:
        key = request.query.get("key", "")
        return self._jobs.get(key) if _KEY.fullmatch(key) else None

    async def _forget(self, key: str) -> None:
        """The job gone (stopped when it runs) and its files with it."""
        if self._jobs.pop(key, None) is None:
            return
        current = self._current
        if current is not None and current[0] == key:
            current[1].cancel()
            await asyncio.gather(current[1], return_exceptions=True)
        await asyncio.to_thread(self._remove_files, key)
        log.info("Phone-sized copy %s dropped", key)

    # -- the work ------------------------------------------------------------------------------

    def _copy(self, key: str) -> Path:
        return self._folder / f"{key}.mkv"

    def _remove_files(self, key: str) -> None:
        for name in (f"{key}.mkv", f"{key}.mkv.part", f"{key}.source"):
            with contextlib.suppress(OSError):
                (self._folder / name).unlink()

    async def _work(self) -> None:
        cleaned = self._clock()
        while True:
            try:
                async with asyncio.timeout(CLEAN_EVERY_S):
                    key = await self._queue.get()
            except TimeoutError:
                await self._clean()
                cleaned = self._clock()
                continue
            if self._clock() - cleaned > CLEAN_EVERY_S:
                await self._clean()
                cleaned = self._clock()
            job = self._jobs.get(key)
            if job is None or job.state != "waiting":
                continue  # dropped meanwhile
            task = asyncio.create_task(self._make(job), name="phone-copy")
            self._current = (key, task)
            try:
                await asyncio.gather(task, return_exceptions=True)
            finally:
                self._current = None

    async def _make(self, job: _Job) -> None:
        started = time.monotonic()
        try:
            await self._fetch_and_convert(job)
        except _Failed as failed:
            job.state, job.message = "failed", failed.message
            await asyncio.to_thread(self._remove_files, job.key)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("Phone-sized copy %s failed: %s", job.key, type(exc).__name__)
            job.state, job.message = "failed", t(CONVERT_FAILED_TEXT)
            await asyncio.to_thread(self._remove_files, job.key)
            return
        log.info(
            "Phone-sized copy %s ready: %.1f GB in %.0f min",
            job.key,
            (job.size or 0) / 1024**3,
            (time.monotonic() - started) / 60,
        )

    async def _fetch_and_convert(self, job: _Job) -> None:
        from app.services.plex import PlexError

        job.state, job.progress, job.touched = "fetching", 0, self._clock()
        try:
            film = await self._plex.film_file(job.key)
        except PlexError as exc:
            log.info("Phone-sized copy %s: Plex didn't say (%s)", job.key, type(exc).__name__)
            raise _Failed(t(FETCH_FAILED_TEXT)) from None
        if film is None:
            raise _Failed(t(NOT_THERE_TEXT))
        if 0 < film.height <= MAX_HEIGHT:
            raise _Failed(t(SMALL_ALREADY_TEXT))
        room = film.size + int(film.duration_s * OUT_BYTES_PER_S) + DISK_MARGIN
        if self._free_space(self._folder) < room:
            log.info("Phone-sized copy %s: not enough free space", job.key)
            raise _Failed(t(NO_ROOM_TEXT))
        source = self._folder / f"{job.key}.source"
        await self._fetch(job, film, source)
        info = await self._read_source(source)
        if info is None:
            raise _Failed(t(CONVERT_FAILED_TEXT))
        if info.dolby_vision_only:
            raise _Failed(t(DOLBY_VISION_TEXT))
        duration = info.duration_s or film.duration_s
        job.state = "converting"
        part = self._folder / f"{job.key}.mkv.part"

        def converted(share: float) -> None:
            job.progress = FETCH_SHARE + round(share * (100 - FETCH_SHARE))

        code = await self._convert(
            ffmpeg_args(self._ffmpeg, source, part, info), duration, converted
        )
        if code != 0:  # the card wouldn't decode it: the processor decodes, the card encodes
            log.info("Phone-sized copy %s: decoding on the card failed (%d); again", job.key, code)
            job.progress = FETCH_SHARE
            code = await self._convert(
                ffmpeg_args(self._ffmpeg, source, part, info, card_decodes=False),
                duration,
                converted,
            )
        if code != 0:
            log.warning("Phone-sized copy %s: ffmpeg ended with %d", job.key, code)
            raise _Failed(t(CONVERT_FAILED_TEXT))
        target = self._copy(job.key)
        await asyncio.to_thread(os.replace, part, target)
        await asyncio.to_thread(source.unlink)
        job.size = (await asyncio.to_thread(target.stat)).st_size
        job.state, job.progress, job.touched = "ready", 100, self._clock()

    async def _fetch(self, job: _Job, film: PlexFilmFile, source: Path) -> None:
        from app.services.plex import PlexError

        got = 0
        out = await asyncio.to_thread(source.open, "wb")
        try:

            async def write(piece: bytes) -> None:
                nonlocal got
                await asyncio.to_thread(out.write, piece)
                got += len(piece)
                job.progress = min(FETCH_SHARE, FETCH_SHARE * got // max(1, film.size))

            await self._plex.fetch_film(film, write)
        except PlexError as exc:
            log.info("Phone-sized copy %s: fetching failed (%s)", job.key, type(exc).__name__)
            raise _Failed(t(FETCH_FAILED_TEXT)) from None
        finally:
            await asyncio.to_thread(out.close)
        if got < film.size:
            log.info("Phone-sized copy %s: the file came short", job.key)
            raise _Failed(t(FETCH_FAILED_TEXT))
        job.progress = FETCH_SHARE

    async def _read_source(self, source: Path) -> SourceInfo | None:
        code, out = await self._run(
            [
                str(self._ffprobe), "-v", "error", "-print_format", "json",
                "-show_streams", "-show_format", str(source),
            ],
            PROBE_TIMEOUT_S * 4,
        )  # fmt: skip
        if code != 0:
            return None
        try:
            data = json.loads(out)
        except ValueError:
            return None
        return read_probe(data) if isinstance(data, dict) else None

    async def _clean(self) -> None:
        """Copies untouched for STALE_DAYS go (not the one being made)."""
        limit = self._clock() - STALE_DAYS * 86400
        running = self._current[0] if self._current is not None else None
        for key, job in list(self._jobs.items()):
            if job.touched < limit and key != running:
                log.info("Phone-sized copy %s untouched for %d days: removed", key, STALE_DAYS)
                await self._forget(key)


def _unknown() -> Reply:
    return error_reply(
        404, "not_found", t("AirGrabber has no phone-sized copy of that film, so ask for it again.")
    )
