"""A folder library video's subtitle files, for the phone (docs/folder-library/2026-11-16-companion-
contract.md, `subtitles`): the files next to it named after it ("Film.nl.srt", "Film.en.forced.srt",
"Film.srt", "Film.eng.ass"), and an SRT as WebVTT (what phones and TVs play).

Pure: a folder's file names and a subtitle's text in, nothing read here (the caller lists and reads,
in a thread). MediaForge's SUBTITLE_EXTENSIONS say what a subtitle is, imported where it's used:
AirGrabber's start loads no Forge code.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

# The formats the phone is told about (the contract's); a .sub (VobSub, MicroDVD) is none of them.
FORMATS = ("srt", "ass", "ssa", "vtt")
# What each is sent as; a VTT this module made is UTF-8.
CONTENT_TYPES = {
    "srt": "application/x-subrip",
    "ass": "text/x-ssa",
    "ssa": "text/x-ssa",
    "vtt": "text/vtt; charset=utf-8",
}
VTT_TYPE = CONTENT_TYPES["vtt"]
_MARKS = frozenset({"forced", "sdh", "cc"})  # words in a name that aren't its language
_LANGUAGE = re.compile(r"[a-z]{2,3}(?:-[a-z0-9]{2,4})?", re.IGNORECASE)  # "nl", "eng", "pt-BR"
_STAMP = r"(\d{1,3}):(\d{1,2}):(\d{1,2})[,.](\d{1,3})"
_TIMING = re.compile(rf"\s*{_STAMP}\s*-->\s*{_STAMP}")


class Sidecar(NamedTuple):
    path: Path
    language: str | None  # as the name says it, lowercase ("nl", "eng", "pt-br"); None: none said
    forced: bool
    format: str  # one of FORMATS


def sidecars(video: Path, names: Iterable[str]) -> list[Sidecar]:
    """The subtitle files among `names` (the video's folder's file names, in the order found)
    that belong to `video`: "<its name>.srt" or "<its name>.<words>.<ext>", any case."""
    from app.forge.core.subtitles import SUBTITLE_EXTENSIONS

    stem = video.stem
    found: list[Sidecar] = []
    for name in names:
        base, extension = os.path.splitext(name)
        extension = extension.lower()
        kind = extension[1:]
        if extension not in SUBTITLE_EXTENSIONS or kind not in FORMATS:
            continue
        if base[: len(stem)].casefold() != stem.casefold():
            continue
        rest = base[len(stem) :]
        if rest and not rest.startswith("."):
            continue  # another video's ("Film 2.nl.srt")
        words = [word for word in rest.split(".") if word]
        forced = any(word.casefold() == "forced" for word in words)
        language = next(
            (
                word.lower()
                for word in words
                if word.casefold() not in _MARKS and _LANGUAGE.fullmatch(word)
            ),
            None,
        )
        found.append(Sidecar(video.with_name(name), language, forced, kind))
    return found


def subtitle_text(data: bytes) -> str:
    """A subtitle file's text: UTF-16 or UTF-8 (a BOM or not), else Windows' Western encoding,
    which every old SRT editor wrote."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def srt_to_vtt(text: str) -> str:
    """An SRT's text as WebVTT: the header, "," before the milliseconds as ".", hours of two
    digits, a timing line's SRT positions dropped; the cue numbers stay (VTT's cue ids)."""
    lines = text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out = ["WEBVTT", ""]
    for line in lines:
        timing = _TIMING.match(line)
        if timing is not None:
            parts = timing.groups()
            line = f"{_stamp(*parts[:4])} --> {_stamp(*parts[4:])}"
        out.append(line)
    return "\n".join(out).rstrip("\n") + "\n"


def _stamp(hours: str, minutes: str, seconds: str, fraction: str) -> str:
    return f"{int(hours):02d}:{int(minutes):02d}:{int(seconds):02d}.{fraction.ljust(3, '0')}"
