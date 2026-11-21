"""The video player's pure logic (`app/media_player.py`): no Tk, no VLC, stdlib only.

Times are in milliseconds. The spec's values (docs/player/2026-11-13-pc-player-design.md): the
controls fade after IDLE_S idle, AWAY_S with the pointer off the player (owner, 2026-11-15: quicker
than the first 3 s; not while paused or with the pointer on them), jumps of
JUMP_BACK_MS / JUMP_FORWARD_MS, the next episode countdown COUNTDOWN_S long, starting at Plex's
credits marker or NO_CREDITS_MS before the end; libVLC's end event only counts as the end
within END_SLACK_MS / END_SLACK_SHARE of it (`played_to_the_end`); the first window
WIDTH_SHARE of the work area, 16:9, centred, at most MAX_HEIGHT_SHARE tall. The mini player
(owner, 2026-11-14): MINI_SIZE in the bottom-right corner at first, tucked into a corner it is
dropped near, kept 16:9, never below MINI_LEAST; Ctrl+H (`is_mini_key`). VLC's sync and speed
keys (round 2, item 20): G / H the subtitles, J / K the sound SYNC_STEP_MS earlier / later,
[ / ] through SPEEDS, = back to 1×; N / P the next / previous episode, A Fit / Fill, Ctrl+W
closes (`is_close_key`; Esc only leaves full screen or the mini player). Round 3 (owner,
2026-11-21, ideas from other players): D clearer voices (VOICE_BANDS), L a brighter picture
(BRIGHTNESS), T the sleep timer (SLEEP_STEPS), PgUp / PgDn the chapters (`chapter_step`), ? or
F1 the keys' list, the subtitles' size (SUBTITLE_SIZES) and height, a press held on the picture
plays at HOLD_RATE.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType

IDLE_S = 1.5
AWAY_S = 0.5  # the pointer off the player's window: the controls go sooner
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


def controls_shown(
    idle_s: float, paused: bool, pointer_on_controls: bool, pointer_away: bool = False
) -> bool:
    """Whether the controls show: hidden after IDLE_S idle while playing (AWAY_S with the pointer
    off the player), never with the pointer on them."""
    limit = AWAY_S if pointer_away else IDLE_S
    return idle_s < limit or paused or pointer_on_controls


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
    "g": "subtitles_earlier",
    "h": "subtitles_later",  # Ctrl+H is the mini player (`is_mini_key`, checked first)
    "j": "audio_earlier",
    "k": "audio_later",
    "bracketleft": "slower",
    "bracketright": "faster",
    "equal": "normal_speed",
    "n": "next",
    "p": "previous",
    "a": "aspect",
    "d": "voices",
    "l": "brighter",
    "t": "sleep",
    "Prior": "previous_chapter",  # Page Up
    "Next": "next_chapter",  # Page Down
    "question": "shortcuts",
    "F1": "shortcuts",
}
KEYS: Mapping[str, str] = MappingProxyType(
    _KEYS | {keysym.upper(): action for keysym, action in _KEYS.items() if len(keysym) == 1}
)
"""Tk keysym → action, as in VLC (a letter with Shift or Caps Lock acts the same)."""


SYNC_STEP_MS = 50  # G / H, J / K: the subtitles' or the sound's delay changes this much
SPEEDS = (0.5, 0.75, 1.0, 1.25, 1.5, 2.0)  # [ / ] step through these; = back to 1.0


def signed_ms(ms: int) -> str:
    """A delay as its notice shows it: "+150", "−50" (a real minus), "0"."""
    if ms > 0:
        return f"+{ms}"
    if ms < 0:
        return f"−{-ms}"
    return "0"


def next_speed(rate: float, step: int) -> float:
    """The speed after `rate` in SPEEDS (`step` 1 up, -1 down); the last / first stays."""
    if step > 0:
        return next((speed for speed in SPEEDS if speed > rate + 1e-9), SPEEDS[-1])
    return next((speed for speed in reversed(SPEEDS) if speed < rate - 1e-9), SPEEDS[0])


def speed_text(rate: float, mark: str = ".") -> str:
    """A speed as its notice shows it ("1.25", "1", "0,5" with `mark` ",")."""
    return f"{rate:g}".replace(".", mark)


def fill_aspect(width: int, height: int) -> str | None:
    """The window's shape as libVLC's crop takes it ("16:9" for 1920×1080); None while it has
    no size."""
    if width <= 0 or height <= 0:
        return None
    common = math.gcd(width, height)
    return f"{width // common}:{height // common}"


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


def is_close_key(keysym: str, state: int) -> bool:
    """Ctrl+W: closes the player (Esc doesn't, as in VLC)."""
    return bool(state & _CONTROL) and keysym in ("w", "W")


# -- round 3: sound, picture, sleep timer, chapters ----------------------------------------------

# Clearer voices (D): libVLC's 10-band equalizer (60, 170, 310, 600 Hz, 1, 3, 6, 12, 14, 16 kHz)
# with the rumble and booms down and the voices' range up, in dB; instant, for every video.
VOICE_BANDS = (-9.0, -7.0, -3.0, 1.0, 4.0, 5.0, 3.0, 0.0, -1.0, -2.0)
VOICE_PREAMP = 2.0
# Brighter picture (L): off, brighter, brightest as libVLC's adjust filter's (brightness, gamma);
# the gamma lifts the dark parts most, the brightness also works with a graphics card's decoding.
BRIGHTNESS = ((1.0, 1.0), (1.08, 1.35), (1.16, 1.7))
# The subtitles' size (sub-text-scale, %): small, normal, large, extra large; raised: this many
# pixels higher than libVLC's own place (sub-margin).
SUBTITLE_SIZES = (75, 100, 130, 165)
NORMAL_SIZE = 1
RAISED_MARGIN = 120
# The sleep timer (T steps through, the More menu chooses): minutes of playing, or SLEEP_END:
# the end of this video. In its last SLEEP_FADE_S the sound fades, then the video pauses.
SLEEP_END = -1
SLEEP_STEPS = (0, 15, 30, 45, 60, 90, SLEEP_END)  # 0: off
SLEEP_FADE_S = 15.0
# A press held still on the picture this long plays at HOLD_RATE until it is let go.
HOLD_MS = 450
HOLD_RATE = 2.0
CHAPTER_BACK_MS = 3_000  # PgUp this far into a chapter: its start again, else the one before


# Round 4 (owner, 2026-11-21): the sound up to VOLUME_MAX (libVLC amplifies above 100).
VOLUME_MAX = 150


def ends_at(now_s: float, position_ms: int, duration_ms: int, rate: float) -> float | None:
    """When the video ends (seconds since the epoch, as `now_s`) at this speed; None while its
    length is unknown."""
    if duration_ms <= 0:
        return None
    left_s = max(0, duration_ms - position_ms) / 1000 / (rate if rate > 0 else 1.0)
    return now_s + left_s


def credits_button(
    position_ms: int, duration_ms: int, credits_ms: int | None, has_next: bool
) -> bool:
    """Skip credits shows from Plex's credits marker on, for a video no next episode follows
    (a film, a show's last episode: those have the countdown instead)."""
    if has_next or credits_ms is None or duration_ms <= 0:
        return False
    return credits_ms <= position_ms < duration_ms - 2_000 and credits_ms < duration_ms


def device_gone(previous: str | None, current: str | None, previous_active: bool) -> bool:
    """Windows' default sound output changed because the one before went away (Bluetooth
    headphones off, a cable out), not because another was picked or plugged in."""
    return previous is not None and current != previous and not previous_active


def next_sleep(current: int) -> int:
    """T: the sleep timer's next step after `current` (SLEEP_STEPS; one not in it: off)."""
    if current not in SLEEP_STEPS:
        return SLEEP_STEPS[0]
    return SLEEP_STEPS[(SLEEP_STEPS.index(current) + 1) % len(SLEEP_STEPS)]


def faded_volume(volume: int, left_s: float) -> int:
    """The sound in the sleep timer's last SLEEP_FADE_S: down to 0 as `left_s` reaches 0."""
    if left_s >= SLEEP_FADE_S:
        return volume
    return max(0, round(volume * max(0.0, left_s) / SLEEP_FADE_S))


Chapter = tuple[int, str]  # (start, ms; its name: "" when the file gives none)


def chapter_index(chapters: Sequence[Chapter], ms: int) -> int | None:
    """The chapter `ms` is in (None without chapters, or before the first)."""
    found = None
    for index, (start, _name) in enumerate(chapters):
        if start <= ms:
            found = index
        else:
            break
    return found


def chapter_step(chapters: Sequence[Chapter], ms: int, step: int) -> int | None:
    """PgDn (`step` 1): the next chapter's start; PgUp (-1): this chapter's start once past its
    first CHAPTER_BACK_MS, else the one before's. None: no chapter that way."""
    if not chapters:
        return None
    index = chapter_index(chapters, ms)
    if step > 0:
        following = 0 if index is None else index + 1
        return chapters[following][0] if following < len(chapters) else None
    if index is None:
        return None
    start = chapters[index][0]
    if ms - start >= CHAPTER_BACK_MS or index == 0:
        return start
    return chapters[index - 1][0]
