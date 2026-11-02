"""Read title, year, season and episode from release-style filenames (via guessit).

guessit is an optional package (requirements.txt's "# optional" part): it is imported only
inside the functions below, so AirGrabber starts and runs without it. `parser_ready()` says
whether it can be used; `parse_filename` raises ImportError when it can't.

`prepare_parser()` (ForgeRuntime's start) is `parser_ready()` without holding the window: the
first time it imports guessit and builds its rules (its first parse, ~0.5 s of work) in pieces,
one piece per turn of the loop (F-33).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import functools
import importlib
import logging
import re
from collections.abc import Awaitable, Callable
from pathlib import Path, PurePath
from typing import Any

from app.forge.models.media import MediaKind, ParsedMedia, TypeHint

VIDEO_EXTENSIONS: frozenset[str] = frozenset(
    {".mkv", ".mp4", ".avi", ".mov", ".m4v", ".wmv", ".flv"}
)

# Properties guessit tends to get wrong for our purposes. Excluding them keeps those words
# in the title: "Dune Part Two", "Spider-Man" (not group "Spider"), "The Last of Us" (not "US").
_GUESSIT_OPTIONS: dict[str, Any] = {
    "excludes": [
        "part",
        "volume",
        "country",
        "release_group",
        "website",
        "streaming_service",
        "language",
        "subtitle_language",
    ]
}
_TAG_PROPERTIES = ("screen_size", "source", "video_codec", "audio_codec", "edition", "other")
_SPACES = re.compile(r"\s+")

log = logging.getLogger(__name__)


@functools.cache
def parser_ready() -> bool:
    """Whether guessit can be imported (asked once per run; it is an optional package)."""
    try:
        importlib.import_module("guessit")
    except Exception as exc:  # noqa: BLE001 - a broken install is as good as a missing one
        # The type only: a traceback or message could show paths with the user's name.
        log.warning("guessit can't be imported (%s): file names can't be read", type(exc).__name__)
        return False
    return True


# What guessit pulls in when it is imported and on its first parse, in pieces of at most a few
# tens of ms each (measured 2026-11 on the development machine: rebulk ~30 ms, babelfish ~55,
# each of its name converters ~20, dateutil ~30, guessit's own modules ~80, Windows' file types
# from the registry ~70-100, its rules ~60-75; all at once it held the window 0.3-1 s). Each
# piece runs on the loop with a pause after it, never in a worker thread: this is pure-Python
# CPU work, and a thread starves the Tk thread of the GIL (queue.py's _parse_all). A newer
# guessit that needs more only makes a later piece bigger.
_IMPORT_PIECES: tuple[tuple[str, ...], ...] = (
    ("rebulk", "rebulk.introspector"),
    ("babelfish",),
    ("dateutil.parser",),
    ("argparse", "shlex", "gettext", "ipaddress", "urllib.parse", "mimetypes"),
    ("yaml",),  # optional: guessit's options loader imports it when it is installed
)
# babelfish's name converters guessit's language rules read (loaded on first use, ~20 ms each).
_CONVERTERS: tuple[tuple[str, str], ...] = (
    ("language", "alpha3b"),
    ("language", "alpha2"),
    ("language", "name"),
    ("language", "opensubtitles"),
    ("country", "name"),
)
WARM_UP_NAME = "Alex.Example.Show.S01E02.1080p.WEB.x264-GRP.mkv"  # made up; builds the rules
PIECE_PAUSE_S = 0.005
_prepared = False  # prepare_parser() has run (once per process)


def _import_piece(names: tuple[str, ...]) -> Callable[[], None]:
    def piece() -> None:
        for name in names:
            importlib.import_module(name)

    return piece


def _converter_piece(kind: str, name: str) -> Callable[[], None]:
    def piece() -> None:
        import babelfish  # guessit's dependency: there when guessit is

        managers = {"language": babelfish.language_converters, "country": babelfish.country_converters}
        managers[kind][name]  # loads it

    return piece


def _pieces() -> list[Callable[[], None]]:
    return [
        *(_import_piece(names) for names in _IMPORT_PIECES),
        *(_converter_piece(kind, name) for kind, name in _CONVERTERS),
        _import_piece(("guessit",)),
    ]


async def _pause() -> None:
    # A timer, not sleep(0): a timer that came due during the piece (Tk's pump) runs first.
    await asyncio.sleep(PIECE_PAUSE_S)


def _read_file_types() -> None:
    import mimetypes

    if not mimetypes.inited:
        mimetypes.init()


async def prepare_parser(pause: Callable[[], Awaitable[None]] = _pause) -> bool:
    """`parser_ready()`, without holding the loop long: the first call imports guessit and
    builds its rules piece by piece (`pause` after each piece). Later calls only answer. Never
    raises.

    Windows' file types (the registry, which guessit's rules read) are read in a worker thread:
    those are registry calls that let go of the GIL, so the window isn't starved (measured)."""
    global _prepared
    if _prepared:
        return parser_ready()
    for piece in _pieces():
        try:
            piece()
        except Exception:  # noqa: BLE001, S110 - missing or broken: parser_ready() says why
            pass
        await pause()
    if not parser_ready():
        _prepared = True
        return False
    try:
        await asyncio.to_thread(_read_file_types)
        await pause()
        parse_filename(WARM_UP_NAME)  # guessit builds its rules on its first parse
    except Exception as exc:  # noqa: BLE001 - a broken guessit shows at the first real parse
        log.warning("Preparing the file-name reader failed (%s)", type(exc).__name__)
    _prepared = True
    return True


def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def human_title(text: str) -> str:
    """Capitalise words that are entirely lowercase; leave NCIS and VisionQuest alone."""
    words = _SPACES.sub(" ", text).strip().split(" ")
    return " ".join(w[:1].upper() + w[1:] if w.islower() else w for w in words)


def _as_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, list):
        return " ".join(str(v) for v in value)
    return str(value)


def _as_int(value: Any) -> int | None:
    if isinstance(value, list):
        value = value[0] if value else None
    return value if isinstance(value, int) else None


def _tags(found: dict[str, Any]) -> tuple[str, ...]:
    tags: list[str] = []
    for prop in _TAG_PROPERTIES:
        value = found.get(prop)
        values: list[Any] = value if isinstance(value, list) else [value]
        tags.extend(str(v) for v in values if v is not None)
    return tuple(tags)


def parse_filename(name: str, hint: TypeHint = TypeHint.AUTO) -> ParsedMedia:
    options = dict(_GUESSIT_OPTIONS)
    if hint is TypeHint.MOVIE:
        options["type"] = "movie"
    elif hint is TypeHint.TV:
        options["type"] = "episode"
    from guessit import guessit  # optional package: imported here, never at module level

    found: dict[str, Any] = dict(guessit(name, options))

    season = _as_int(found.get("season"))
    episodes = found.get("episode")
    episode_list: list[Any] = episodes if isinstance(episodes, list) else [episodes]
    numbers = [e for e in episode_list if isinstance(e, int)]
    episode = numbers[0] if numbers else None
    episode_end = numbers[-1] if len(numbers) > 1 else None

    if hint is TypeHint.MOVIE:
        kind = MediaKind.MOVIE
    elif hint is TypeHint.TV:
        kind = MediaKind.EPISODE
    else:
        kind = MediaKind.EPISODE if (season is not None or episode is not None) else MediaKind.MOVIE

    if kind is MediaKind.EPISODE and season is None and episode is not None:
        season = 1

    year = _as_int(found.get("year"))
    date = found.get("date")
    if year is None and isinstance(date, dt.date):
        year = date.year

    title = human_title(_as_text(found.get("title")) or "")
    if not title:
        stem = PurePath(name).stem
        title = human_title(stem.replace(".", " ").replace("_", " ")) or "_"

    episode_title: str | None = None
    if kind is MediaKind.EPISODE:
        episode_title = human_title(_as_text(found.get("episode_title")) or "") or None

    return ParsedMedia(
        source_name=name,
        kind=kind,
        title=title,
        year=year,
        season=season,
        episode=episode,
        episode_end=episode_end,
        episode_title=episode_title,
        tags=_tags(found),
    )
