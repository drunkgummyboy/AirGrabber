"""What a renamed video really holds: its audio languages and its picture height (ffprobe).

The executor asks `FileCheck.check` for every video it renamed, and says each finding in the
Activity strip as a warning. Two findings:

- Audio: the video's audio tracks name their languages and none is one the user understands
  (English, or a language chosen in Settings › Subtitles): "Audio is Russian". When such a track
  is there but another plays first (a "DUAL" release with a dub as its default): "Plays Russian
  first; English is there too". Tracks without a language say nothing.
- Picture: the release name promises a resolution (1080p) and the picture is clearly smaller
  (under SMALLER_SHARE of it): "Says 1080p, but the picture is 720p" (an upscale or a fake).

ffprobe comes with the trailer tools (`AppPaths.forge / "tools" / "ffmpeg" / "bin"`); without it
nothing is checked. It runs once per video, no window, for at most PROBE_TIMEOUT_S. Its output is
never logged (it names the user's folders); the log gets counts only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.forge.core import fsops
from app.i18n import N_, t
from app.models.settings import SUBTITLE_LANGUAGES

log = logging.getLogger(__name__)

PROBE_TIMEOUT_S = 20.0
SMALLER_SHARE = 0.8  # a picture under this share of the promised height is a different one
FFPROBE = Path("ffmpeg") / "bin" / "ffprobe.exe"  # under the trailer tools' folder

AUDIO_ONLY = N_("{name}: the audio is {language}")
AUDIO_FIRST = N_("{name}: plays {language} first; {wanted} is there too")
PICTURE_SMALLER = N_("{name}: says {promised}p, but the picture is {height}p")

# ISO 639-2 (what video files name) -> the Settings › Subtitles code (ISO 639-1).
_ISO_639_2 = {
    "eng": "en", "dut": "nl", "nld": "nl", "ger": "de", "deu": "de", "fre": "fr", "fra": "fr",
    "spa": "es", "ita": "it", "por": "pt", "swe": "sv", "dan": "da", "nor": "no", "nob": "no",
    "nno": "no", "fin": "fi", "pol": "pl", "cze": "cs", "ces": "cs", "hun": "hu", "rum": "ro",
    "ron": "ro", "gre": "el", "ell": "el", "tur": "tr", "rus": "ru", "ukr": "uk", "ara": "ar",
    "heb": "he", "hin": "hi", "ind": "id", "jpn": "ja", "kor": "ko", "chi": "zh", "zho": "zh",
}
_NO_LANGUAGE = frozenset({"", "und", "unk", "mul", "zxx", "mis"})
_PROMISED = re.compile(r"(?<![0-9])(2160|1080|720|576|480)[pi](?![a-z0-9])", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class AudioTrack:
    language: str  # the Settings › Subtitles base code ("ru"), or the file's own tag
    default: bool


@dataclass(frozen=True, slots=True)
class Probe:
    """What ffprobe found: the video's height class (`read_probe`), the audio tracks in file
    order."""

    height: int | None
    audio: tuple[AudioTrack, ...]


def _base(code: str) -> str:
    """"pt-BR" -> "pt", "eng" -> "en"; lower case."""
    code = code.strip().lower()
    return _ISO_639_2.get(code, code.split("-")[0])


def language_name(code: str) -> str:
    """A base code's plain name ("ru" -> "Russian"), translated; else the code itself."""
    for known, name in SUBTITLE_LANGUAGES.items():
        if _base(known) == code:
            return t(name)
    return code


def read_probe(output: str) -> Probe | None:
    """ffprobe's JSON (`-show_entries stream=codec_type,width,height:stream_tags=language:
    stream_disposition=default -of json`); None when it isn't that. The height is the tallest
    video stream's, a wide picture's counted by its width (`max(height, width × 9/16)`)."""
    try:
        data: Any = json.loads(output)
        streams = data.get("streams", [])
    except (ValueError, AttributeError):
        return None
    if not isinstance(streams, list):
        return None
    heights: list[int] = []
    audio: list[AudioTrack] = []
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        kind = stream.get("codec_type")
        height = stream.get("height")
        if kind == "video" and isinstance(height, int):
            width = stream.get("width")
            # A wide film's picture is lower than its class (1920×800 is 1080p): count the
            # width as a 16:9 picture's too.
            if isinstance(width, int):
                height = max(height, round(width * 9 / 16))
            heights.append(height)
        elif kind == "audio":
            tags = stream.get("tags") or {}
            language = tags.get("language", "") if isinstance(tags, dict) else ""
            disposition = stream.get("disposition") or {}
            default = isinstance(disposition, dict) and disposition.get("default") == 1
            audio.append(AudioTrack(_base(str(language)), default))
    return Probe(height=max(heights) if heights else None, audio=tuple(audio))


def promised_height(name: str) -> int | None:
    """The resolution a release name promises: "…1080p…" -> 1080."""
    match = _PROMISED.search(name)
    return int(match.group(1)) if match else None


def findings(probe: Probe, name: str, understood: Iterable[str], shown_name: str) -> list[str]:
    """The warnings for one video (`name`: the release name it came with; `understood`: base
    codes; `shown_name`: what the warning calls it), translated."""
    wanted = {_base(code) for code in understood} | {"en"}
    found: list[str] = []
    named = [track for track in probe.audio if track.language not in _NO_LANGUAGE]
    if named:
        first = next((track for track in named if track.default), named[0])
        good = [track for track in named if track.language in wanted]
        if not good:
            found.append(
                t(AUDIO_ONLY, name=shown_name, language=language_name(first.language))
            )
        elif first.language not in wanted:
            found.append(
                t(
                    AUDIO_FIRST,
                    name=shown_name,
                    language=language_name(first.language),
                    wanted=language_name(good[0].language),
                )
            )
    promised = promised_height(name)
    if promised is not None and probe.height is not None and probe.height < promised * SMALLER_SHARE:
        found.append(
            t(PICTURE_SMALLER, name=shown_name, promised=promised, height=_nearest(probe.height))
        )
    return found


def _nearest(height: int) -> int:
    """A picture height as the usual name ("1080" for 1036)."""
    for usual in (2160, 1440, 1080, 720, 576, 480):
        if height >= usual * 0.74:
            return usual
    return height


class FileCheck:
    """Probes renamed videos with the trailer tools' ffprobe."""

    def __init__(self, tools_dir: Path, understood: Callable[[], Iterable[str]]) -> None:
        self._ffprobe = tools_dir / FFPROBE
        self._understood = understood

    def available(self) -> bool:
        return self._ffprobe.exists()

    async def probe(self, video: Path) -> Probe | None:
        """ffprobe's reading of `video`; None without ffprobe, or when it fails."""
        if not await asyncio.to_thread(self.available):
            return None
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            proc = await asyncio.create_subprocess_exec(
                str(self._ffprobe),
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,width,height:stream_tags=language:stream_disposition=default",
                "-of",
                "json",
                fsops.long_path(video),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=flags,
            )
        except OSError as exc:
            log.warning("ffprobe couldn't start: %s", type(exc).__name__)
            return None
        try:
            output, _ = await asyncio.wait_for(proc.communicate(), PROBE_TIMEOUT_S)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            log.warning("ffprobe took too long and was stopped")
            return None
        except asyncio.CancelledError:
            proc.kill()
            await asyncio.shield(proc.wait())
            raise
        if proc.returncode:
            log.info("ffprobe couldn't read a video (exit code %s)", proc.returncode)
            return None
        return read_probe(output.decode("utf-8", "replace"))

    async def check(self, video: Path, release_name: str) -> list[str]:
        """The warnings for `video` (renamed from `release_name`); [] when it can't be read."""
        probe = await self.probe(video)
        if probe is None:
            return []
        return findings(probe, release_name, self._understood(), video.name)
