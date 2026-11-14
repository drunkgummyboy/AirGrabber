"""The video player's pure logic (`app/media_player.py`): no Tk, no VLC, stdlib only.

Times are in milliseconds. The spec's values (docs/player/2026-11-13-pc-player-design.md): the
controls fade after IDLE_S idle (not while paused or with the pointer on them), jumps of
JUMP_BACK_MS / JUMP_FORWARD_MS, the next episode countdown COUNTDOWN_S long, starting at Plex's
credits marker or NO_CREDITS_MS before the end; libVLC's end event only counts as the end
within END_SLACK_MS / END_SLACK_SHARE of it (`played_to_the_end`); the first window
WIDTH_SHARE of the work area, 16:9, centred, at most MAX_HEIGHT_SHARE tall. The mini player
(owner, 2026-11-14): MINI_SIZE in the bottom-right corner at first, tucked into a corner it is
dropped near, kept 16:9, never below MINI_LEAST; Ctrl+H (`is_mini_key`).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

IDLE_S = 3.0
JUMP_BACK_MS = 10_000
JUMP_FORWARD_MS = 30_000
COUNTDOWN_S = 10
NO_CREDITS_MS = 20_000
END_SLACK_MS = 60_000  # libVLC's end event counts as the end this close to it…
END_SLACK_SHARE = 0.05  # …or within this share of the length (the larger)
WIDTH_SHARE = 0.70
MAX_HEIGHT_SHARE = 0.90

OFF = "off"
"""Always pick's / a carried subtitle language's "no subtitles"."""

SUBTITLES_OFF = -1
"""`pick_track`'s answer for "subtitles off" (libVLC's track id for none)."""


def clock(ms: int) -> str:
    """A time as the controls show it: "1:12:40", "4:05" (never below "0:00")."""
    seconds = max(0, ms) // 1000
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def controls_shown(idle_s: float, paused: bool, pointer_on_controls: bool) -> bool:
    """Whether the controls show: hidden only after IDLE_S idle while playing, pointer elsewhere."""
    return idle_s < IDLE_S or paused or pointer_on_controls


# -- tracks --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Track:
    """An audio or subtitle track as libVLC lists it: its id, language code and name."""

    id: int
    language: str
    name: str


# The ways a track names its language (ISO 639-1, 639-2/B and /T, English, native and Dutch
# names) → the two-letter code. Anything else compares as itself, lowercased.
_LANGUAGES: tuple[tuple[str, ...], ...] = (
    ("en", "eng", "english", "engels"),
    ("nl", "dut", "nld", "dutch", "nederlands", "flemish", "vlaams"),
    ("de", "ger", "deu", "german", "deutsch", "duits"),
    ("fr", "fre", "fra", "french", "français", "francais", "frans"),
    ("es", "spa", "spanish", "español", "espanol", "castellano", "spaans"),
    ("it", "ita", "italian", "italiano", "italiaans"),
    ("pt", "por", "portuguese", "português", "portugues", "portugees"),
    ("sv", "swe", "swedish", "svenska", "zweeds"),
    ("da", "dan", "danish", "dansk", "deens"),
    ("no", "nor", "nob", "nno", "nb", "nn", "norwegian", "norsk", "noors"),
    ("fi", "fin", "finnish", "suomi", "fins"),
    ("pl", "pol", "polish", "polski", "pools"),
    ("tr", "tur", "turkish", "türkçe", "turks"),
    ("ru", "rus", "russian", "russisch"),
    ("ja", "jpn", "japanese", "japans"),
    ("ko", "kor", "korean", "koreaans"),
    ("zh", "chi", "zho", "chinese", "chinees", "mandarin", "cantonese"),
    ("ar", "ara", "arabic", "arabisch"),
)
_ALIASES: Mapping[str, str] = {name: names[0] for names in _LANGUAGES for name in names}
_UNKNOWN = frozenset(("", "und", "unknown", "mis", "zxx", "mul"))
# Full names only (every code has at most three letters, every name at least four).
_NAMES: Mapping[str, str] = {name: key for name, key in _ALIASES.items() if len(name) > 3}
_WORD = re.compile(r"[^\W\d_]+")
_BRACKET = re.compile(r"\[([^\[\]]*)\]\s*$")


def language_key(text: str) -> str:
    """`text`'s language as one key ("eng", "English", "en-US" → "en"; "" when unknown)."""
    word = text.strip().lower()
    word = re.split(r"[-_]", word, maxsplit=1)[0]
    if word in _UNKNOWN:
        return ""
    return _ALIASES.get(word, word)


def _speaks(track: Track, key: str) -> bool:
    own = language_key(track.language)
    if own:
        return own == key
    # No language on the track: its name may say it. libVLC's "Track 2 - [Dutch]" first: the
    # bracket at the end, read whole.
    name = track.name.lower()
    bracket = _BRACKET.search(name)
    if bracket:
        text = bracket.group(1)
        if language_key(text) == key:
            return True
        name = text
    # Otherwise only a language's full name among the words, never a code: "en", "it", "no",
    # "dan" are everyday words ("Ondertitels voor doven en slechthorenden").
    return any(_NAMES.get(word) == key for word in _WORD.findall(name))


def _find(tracks: Sequence[Track], language: str, subtitles: bool) -> int | None:
    if language.strip().lower() == OFF:
        return SUBTITLES_OFF if subtitles else None
    key = language_key(language)
    if not key:
        return None
    return next((track.id for track in tracks if _speaks(track, key)), None)


def pick_track(
    tracks: Sequence[Track], rule: str, carried: str, *, subtitles: bool = False
) -> int | None:
    """The track to switch to at a video's start, or None to keep the file's default.

    The language picked by hand in the episode before (`carried`) first, then Always pick
    (`rule`). "off" means no subtitles: SUBTITLES_OFF when picking `subtitles`, passed over for
    audio. A language no track has is passed over.
    """
    for language in (carried, rule):
        found = _find(tracks, language, subtitles)
        if found is not None:
            return found
    return None


# -- markers -------------------------------------------------------------------------------------


def intro_button(position_ms: int, intro: tuple[int, int] | None) -> bool:
    """Whether "Skip intro" shows: during Plex's intro marker (start included, end not)."""
    return intro is not None and intro[0] <= position_ms < intro[1]


def countdown_from(duration_ms: int, credits_ms: int | None) -> int:
    """Where the next episode countdown starts: the credits, else NO_CREDITS_MS before the end
    (also for a credits start outside the video, or while its length is unknown)."""
    if credits_ms is not None and 0 <= credits_ms < duration_ms:
        return credits_ms
    return max(0, duration_ms - NO_CREDITS_MS)


def played_to_the_end(position_ms: int, duration_ms: int, credits_ms: int | None) -> bool:
    """Whether libVLC's end event, the last real position at `position_ms`, is the video's end:
    within END_SLACK_MS or END_SLACK_SHARE of its length (the larger), or past where the next
    episode countdown starts. libVLC says "ended" too when a read fails part-way (a share or
    Wi-Fi gone, a cut-off file): that is no end, nor is anything while the length is unknown."""
    if duration_ms <= 0:
        return False
    slack = max(END_SLACK_MS, round(duration_ms * END_SLACK_SHARE))
    return position_ms >= duration_ms - slack or position_ms >= countdown_from(
        duration_ms, credits_ms
    )


# -- keys and the window -------------------------------------------------------------------------

_KEYS = {
    "space": "pause",
    "f": "fullscreen",
    "Escape": "escape",
    "Left": "back",
    "Right": "forward",
    "Up": "louder",
    "Down": "softer",
    "m": "mute",
    "v": "subtitle_track",
    "b": "audio_track",
}
KEYS: Mapping[str, str] = MappingProxyType(
    _KEYS | {keysym.upper(): action for keysym, action in _KEYS.items() if len(keysym) == 1}
)
"""Tk keysym → action, as in VLC (a letter with Shift or Caps Lock acts the same)."""


def first_geometry(work: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    """The first window's (x, y, width, height) in the work area (x, y, width, height):
    WIDTH_SHARE of its width, 16:9, at most MAX_HEIGHT_SHARE of its height, centred."""
    left, top, width, height = work
    w = width * WIDTH_SHARE
    h = w * 9 / 16
    if h > height * MAX_HEIGHT_SHARE:
        h = height * MAX_HEIGHT_SHARE
        w = h * 16 / 9
    w_px, h_px = round(w), round(h)
    return (left + (width - w_px) // 2, top + (height - h_px) // 2, w_px, h_px)


# -- the mini player -----------------------------------------------------------------------------

Rect = tuple[int, int, int, int]  # x, y, width, height (physical pixels)

# In customtkinter's units (times Windows' scaling for pixels): the first size, the least, the
# room left to the work area's edges in a corner, and how near a corner a drop tucks it in.
MINI_SIZE = (480, 270)
MINI_LEAST = (400, 225)
MINI_GAP = 16
MINI_REACH = 48
_CONTROL = 0x0004  # Tk's Control bit in an event's state


def first_mini(work: Rect, scale: float) -> Rect:
    """The mini player's first place: MINI_SIZE, in the work area's bottom-right corner."""
    left, top, width, height = work
    w, h = round(MINI_SIZE[0] * scale), round(MINI_SIZE[1] * scale)
    gap = round(MINI_GAP * scale)
    return (left + width - w - gap, top + height - h - gap, w, h)


def tucked(rect: Rect, work: Rect, scale: float) -> Rect:
    """`rect` dropped near a corner of the work area (within MINI_REACH of where it would sit
    there, both ways): moved into that corner. Anywhere else: where it was dropped."""
    x, y, w, h = rect
    left, top, width, height = work
    gap, reach = round(MINI_GAP * scale), round(MINI_REACH * scale)
    xs = (left + gap, left + width - w - gap)
    ys = (top + gap, top + height - h - gap)
    near_x = min(xs, key=lambda tucked_x: abs(x - tucked_x))
    near_y = min(ys, key=lambda tucked_y: abs(y - tucked_y))
    if abs(x - near_x) <= reach and abs(y - near_y) <= reach:
        return (near_x, near_y, w, h)
    return rect


def mini_shape(
    size: tuple[int, int], before: tuple[int, int], least: tuple[int, int]
) -> tuple[int, int]:
    """A resized mini player back to 16:9, from the side that changed more since `before`
    (the width when both did as much), at least `least`."""
    width, height = size
    if abs(height - before[1]) > abs(width - before[0]):
        width = round(height * 16 / 9)
    else:
        height = round(width * 9 / 16)
    if width < least[0] or height < least[1]:
        return least
    return (width, height)


def is_mini_key(keysym: str, state: int) -> bool:
    """Ctrl+H: VLC's minimal interface, here the mini player."""
    return bool(state & _CONTROL) and keysym in ("h", "H")
