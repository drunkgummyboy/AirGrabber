"""The messages between AirGrabber and its video player (`app/media_player.py`, its own process).

Stdlib only: the player process imports this before anything else. One message is one line of
JSON (`encode`; the sender adds the newline) with a "type" and the message's fields. `decode`
never raises: an unknown type, a missing field, a field of the wrong type or a line that isn't
JSON gives None. Fields a newer side adds are ignored.

App → player: `Play` (a video, in the window that is open or a new one), `LoadSubtitle` (a file
Find subtitles fetched), `Notice` (a line shown over the video for a few seconds, already
translated). Player → app: `Ready`, `Position` (while playing), `Stopped` (closed or replaced),
`Ended`, `WantNext` (the countdown reached 0 or Play now; `by_hand`: N or ⏭), `WantPrevious`
(P or ⏮: the episode before), `FindSubtitles`, `Tracks` (the
languages picked by hand, for the next episode: "" none, "off"), `AlwaysPick` (the viewer chose
Always pick in a track menu: the app stores it in Settings), `OpenFile` (a file dropped from
Explorer on the player: the app plays it as one from "Open with", or says it isn't a video),
`WantEpisode` (one picked in the player's Episodes panel: its index in the Play's list).

Nothing secret travels here: `Play` has a local file and texts, never a token or a URL.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import MISSING, asdict, dataclass, fields
from typing import Any, Literal, TypeAlias

# -- app → player --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Play:
    """Play `file` from `start_ms` (asking Resume / Start over first when `ask_resume`).

    `intro` is Plex's intro marker (start, end) in ms, `credits_ms` its credits marker;
    `next_label` the next episode ("S01E04") or None for no countdown. `always_*` are Settings'
    Always pick ("" none, a language code, "off" for subtitles); `carry_*` the language picked by
    hand in the episode before ("" none). `find_subtitles`: whether the Subtitles menu offers
    Find subtitles (not for a file from Explorer's "Open with"; a line without it: True).
    `previous_label` the episode before ("S01E02"; None: none, or a line from before). With
    either label the window shows ⏮ / ⏭ and takes N / P. `episodes`: the show's episodes in
    order ("S01E03 · Name"; empty for a film or a file), each one's season (0: a special) and
    whether it was watched, `episode_index` this one's place among them (-1: not there): the
    player's Episodes panel (round 4).
    """

    file: str
    title: str
    line: str
    start_ms: int
    ask_resume: bool
    intro: tuple[int, int] | None
    credits_ms: int | None
    next_label: str | None
    always_audio: str
    always_subtitles: str
    carry_audio: str
    carry_subtitles: str
    find_subtitles: bool = True
    previous_label: str | None = None
    episodes: tuple[str, ...] = ()
    episode_seasons: tuple[int, ...] = ()
    episodes_watched: tuple[bool, ...] = ()
    episode_index: int = -1


@dataclass(frozen=True)
class LoadSubtitle:
    path: str


@dataclass(frozen=True)
class Notice:
    text: str


# -- player → app --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Ready:
    pass


@dataclass(frozen=True)
class Position:
    ms: int
    duration_ms: int
    state: Literal["playing", "paused"]


@dataclass(frozen=True)
class Stopped:
    """The video was closed or replaced at `ms`."""

    ms: int
    duration_ms: int


@dataclass(frozen=True)
class Ended:
    duration_ms: int


@dataclass(frozen=True)
class WantNext:
    """The next episode, please: the countdown or Play now, or by hand (N, ⏭: `by_hand`, so
    the one playing isn't marked watched for it)."""

    by_hand: bool = False


@dataclass(frozen=True)
class WantPrevious:
    """The episode before, please (P, ⏮)."""


@dataclass(frozen=True)
class WantEpisode:
    """Episode `index` of the Play's `episodes`, please (picked in the Episodes panel)."""

    index: int


@dataclass(frozen=True)
class FindSubtitles:
    language: str


@dataclass(frozen=True)
class Tracks:
    audio: str
    subtitles: str


@dataclass(frozen=True)
class AlwaysPick:
    """Always pick `language` ("" no rule, "off" no subtitles, else a code) for `kind`."""

    kind: Literal["audio", "subtitles"]
    language: str


@dataclass(frozen=True)
class OpenFile:
    """A file dropped on the player's window (the first of a drop)."""

    file: str


Message: TypeAlias = (
    Play
    | LoadSubtitle
    | Notice
    | Ready
    | Position
    | Stopped
    | Ended
    | WantNext
    | WantPrevious
    | WantEpisode
    | FindSubtitles
    | Tracks
    | AlwaysPick
    | OpenFile
)

TYPES: Mapping[type[Any], str] = {
    Play: "play",
    LoadSubtitle: "load_subtitle",
    Notice: "notice",
    Ready: "ready",
    Position: "position",
    Stopped: "stopped",
    Ended: "ended",
    WantNext: "want_next",
    WantPrevious: "want_previous",
    WantEpisode: "want_episode",
    FindSubtitles: "find_subtitles",
    Tracks: "tracks",
    AlwaysPick: "always_pick",
    OpenFile: "open_file",
}
_BY_NAME: Mapping[str, type[Any]] = {name: kind for kind, name in TYPES.items()}


def encode(msg: Message) -> str:
    """`msg` as one line of JSON (ASCII only, so no pipe's code page can garble it)."""
    return json.dumps({"type": TYPES[type(msg)], **asdict(msg)}, ensure_ascii=True)


# -- reading -------------------------------------------------------------------------------------


class _Wrong(Exception):
    """A field is missing or has the wrong type."""


def _int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise _Wrong
    return value


def _str(value: object) -> str:
    if not isinstance(value, str):
        raise _Wrong
    return value


def _bool(value: object) -> bool:
    if not isinstance(value, bool):
        raise _Wrong
    return value


def _optional_int(value: object) -> int | None:
    return None if value is None else _int(value)


def _optional_str(value: object) -> str | None:
    return None if value is None else _str(value)


def _span(value: object) -> tuple[int, int] | None:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 2:
        raise _Wrong
    return (_int(value[0]), _int(value[1]))


def _state(value: object) -> Literal["playing", "paused"]:
    if value == "playing":
        return "playing"
    if value == "paused":
        return "paused"
    raise _Wrong


def _kind(value: object) -> Literal["audio", "subtitles"]:
    if value == "audio":
        return "audio"
    if value == "subtitles":
        return "subtitles"
    raise _Wrong


def _many[T](read: Callable[[object], T]) -> Callable[[object], tuple[T, ...]]:
    def many(value: object) -> tuple[T, ...]:
        if not isinstance(value, list):
            raise _Wrong
        return tuple(read(item) for item in value)

    return many


_Reader = Callable[[object], object]

# How each field is read, by its annotation (a message with another kind of field fails at import).
_READERS: Mapping[str, _Reader] = {
    "str": _str,
    "int": _int,
    "bool": _bool,
    "int | None": _optional_int,
    "str | None": _optional_str,
    "tuple[int, int] | None": _span,
    "tuple[str, ...]": _many(_str),
    "tuple[int, ...]": _many(_int),
    "tuple[bool, ...]": _many(_bool),
    "Literal['playing', 'paused']": _state,
    "Literal['audio', 'subtitles']": _kind,
}
_FIELDS: Mapping[type[Any], Mapping[str, _Reader]] = {
    kind: {field.name: _READERS[str(field.type)] for field in fields(kind)} for kind in TYPES
}
# The fields a line may leave out (they have a default).
_DEFAULTED: Mapping[type[Any], frozenset[str]] = {
    kind: frozenset(field.name for field in fields(kind) if field.default is not MISSING)
    for kind in TYPES
}


def decode(line: str) -> Message | None:
    """The message on `line`, or None for anything that isn't one (never raises)."""
    try:
        data = json.loads(line)
    except (ValueError, TypeError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    kind = _BY_NAME.get(data["type"]) if isinstance(data.get("type"), str) else None
    if kind is None:
        return None
    values: dict[str, object] = {}
    try:
        for name, read in _FIELDS[kind].items():
            if name in data:
                values[name] = read(data[name])
            elif name not in _DEFAULTED[kind]:
                return None
    except _Wrong:
        return None
    message: Message = kind(**values)
    return message

