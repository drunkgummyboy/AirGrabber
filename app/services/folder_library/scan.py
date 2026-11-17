"""Reading one of the folder library's folders (docs/folder-library/): which videos are there and
what each one is, from its name and the folders it sits in. No TMDB here (match.py), no SQL, and
never a change on disk: the folders are only read.

- **Reading** is plain file-system work for a thread (`asyncio.to_thread`, as MediaForge's
  looks), each folder on its own and limited in time: a NAS can take the connection and never
  answer. The limit is time without a step forward (`timeout_s`, MediaForge's
  LOOK_STEP_TIMEOUT_S for one step of its looks; a step is a listing, an entry or an .nfo), so
  a large or cold library that keeps answering is read whole, up to `max_s` in all. A folder
  that can't be read, or not in time, is not reached (`FolderRead.reached` False; `slow` when
  it did answer): the caller keeps what it stored for it, so marks don't vanish while a NAS
  sleeps. Its thread can't be stopped and is left to end on its own; when it does, its result
  is kept for the folder's next read (`LongReads`) and the caller hears it.
- **Files still arriving don't count:** a part-download ending (`PART_ENDINGS`) and a file
  changed less than `quiet_s` ago (MediaForge's QUIET_S) are left out. So are trailers, samples
  and extras (as MediaForge's duplicates.py leaves them out), and NAS housekeeping folders.
- **An unchanged file** (the same size and change time as its stored row in `known`) keeps what
  was found before and isn't read again.
- **Recognising** a new or changed file, in this order: a TMDB id in an `.nfo` (a film's beside
  it or its folder's `movie.nfo`; a show's `tvshow.nfo` in its show folder: an episode's own
  .nfo holds the episode's id, never read); the folder's name "Title (Year)"; the file name
  through MediaForge's `parse_filename`, or without guessit (`parser_ready()` False) the SxxEyy
  and "Title (Year)" patterns duplicates.py reads. An episode's show is its top folder below the
  series folder (a release's own folder, "Severance.S02.2160p…", cut at its season code or
  tags), or the series folder itself when that top folder is a season's ("Season 01",
  "Specials", "Seizoen 2": a single show's folder set as a series folder); a folder that names
  no show, or none (an episode right in the series folder), goes by the file's name. So does a
  show folder whose episodes' own names give two shows or more (MediaForge's "Series (flat)"
  one level below the series folder: every show in one folder, `_mixed_show_folders`). Names
  are parsed on the loop in small pieces, as MediaForge's queue does (guessit in a thread
  fights the window for the GIL).

MediaForge's code is imported inside the functions only: AirGrabber's start loads none of it.
Nothing here logs a path, a file name or a title.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePath
from typing import Literal

from app.models.domain import DomainModel
from app.models.folder_library import FileKind, LibraryFile

log = logging.getLogger(__name__)

FolderKind = Literal["movie", "show"]

# MediaForge's LOOK_STEP_TIMEOUT_S (app/forge/core/auto.py, not imported: AirGrabber's start loads
# no Forge code; a test holds them equal): a read this long without a step forward gives up.
READ_TIMEOUT_S = 15.0
MAX_READ_S = 300.0  # however well it goes, a folder's read gives up after this long in all
LATE_KEPT_S = 600.0  # a read that ended past its limit: its result serves a read this soon after
_LOOK_EVERY_S = 1.0  # how often the loop looks at a read's progress (at most)
# Endings of files still downloading (qBittorrent, browsers, µTorrent, BitComet, others).
PART_ENDINGS = (
    ".!qb", ".part", ".crdownload", ".!ut", ".bc!", ".tmp", ".partial", ".download", ".aria2",
)
# Folders never read: extras (as duplicates.py), copies MediaForge moved aside, NAS housekeeping.
_SKIPPED_FOLDERS = frozenset({
    "extras", "featurettes", "behind the scenes", "trailers", "sample", "samples",
    "@eadir", "#recycle", "#snapshot", "$recycle.bin", "system volume information", ".@__thumb",
})
_SKIPPED_AT_TOP = frozenset({"duplicates"})  # duplicates.py's QUARANTINE
# Windows' network errors: below the read folder they mean the share went away, so the whole
# folder isn't reached (never half of it). ERROR_BAD_NETPATH 53, ERROR_NETNAME_DELETED 64,
# ERROR_UNEXP_NET_ERR 59, ERROR_BAD_NET_NAME 67, ERROR_REM_NOT_LIST 51, ERROR_NETWORK_BUSY 54,
# ERROR_DEV_NOT_EXIST 55, ERROR_SEM_TIMEOUT 121, ERROR_NO_NETWORK 1222, ERROR_NETWORK_UNREACHABLE
# 1231, ERROR_HOST_UNREACHABLE 1232, ERROR_CONNECTION_ABORTED 1236; ERROR_NOT_READY 21,
# ERROR_BAD_NET_RESP 58, ERROR_NETWORK_ACCESS_DENIED 65, ERROR_VC_DISCONNECTED 240,
# ERROR_DEVICE_NOT_CONNECTED 1167, ERROR_CONNECTION_REFUSED 1225, ERROR_NOT_CONNECTED 2250.
_CONNECTION_ERRORS = frozenset({
    21, 51, 53, 54, 55, 58, 59, 64, 65, 67, 121, 240, 1167, 1222, 1225, 1231, 1232, 1236, 2250,
})
_scandir = os.scandir  # the walk's listing (a seam: tests make one folder fail)
_NOT_A_COPY = re.compile(
    r"(?:^|[ ._\-\[(])(sample|trailer|featurette|behind ?the ?scenes|deleted ?scenes?|extras?)"
    r"(?:$|[ ._\-\])])",
    re.IGNORECASE,
)
_EPISODE = re.compile(r"(?<![a-z0-9])s(\d{1,3})e(\d{1,4})(?:-?e(\d{1,4}))?(?![0-9])", re.IGNORECASE)
_TIDY = re.compile(r"^(.+?)\s*\((\d{4})\)")  # "Dune (2021)", "Dune (2021) - 2160p"
_TRAILING_YEAR = re.compile(r"^(.+?)\s*\(((?:19|20)\d{2})\)$")  # a show's "Show A (2022)"
_KEY_YEAR = re.compile(r"(?:19|20)\d{2}$")  # a year at the end of a show's key ("doctorwho2005")
_RAW_YEAR = re.compile(r"^(.+?)[ ._\-\[(]+((?:19|20)\d{2})(?![0-9])")  # "Dune.Part.Two.2024.…"
# A season code in a folder's name ("Severance.S02.2160p…", "Fargo Season 2"): a release's own
# folder, its name cut there.
_SEASON_CODE = re.compile(
    r"(?<![a-z0-9])(?:s\d{1,3}(?:e\d{1,4})?|season[ ._\-]?\d{1,3})(?![a-z0-9])", re.IGNORECASE
)
# A season's own folder: below it is no show's name ("Season 01", "Specials", "Seizoen 2", "S01").
_SEASON_FOLDER = re.compile(
    r"^(?:(?:season|seizoen)[ ._\-]*\d{1,3}|s\d{1,3}|specials)$", re.IGNORECASE
)
_TAGS = re.compile(
    r"[ ._\-\[(]+(2160p|1080p|720p|576p|480p|4k|uhd|hdr|web.?dl|webrip|bluray|x26[45]|hevc).*$",
    re.IGNORECASE,
)
_QUALITY = (
    (re.compile(r"(?<![a-z0-9])(2160p|4k|uhd)(?![a-z0-9])", re.IGNORECASE), "4K"),
    (re.compile(r"(?<![a-z0-9])1080[pi](?![a-z0-9])", re.IGNORECASE), "1080p"),
    (re.compile(r"(?<![a-z0-9])720p(?![a-z0-9])", re.IGNORECASE), "720p"),
    (re.compile(r"(?<![a-z0-9])(576p|480p|dvdrip|sdtv)(?![a-z0-9])", re.IGNORECASE), "SD"),
)
_TMDB_IN_NFO = re.compile(
    r"<uniqueid\b[^>]*\btype\s*=\s*[\"']tmdb[\"'][^>]*>\s*(\d{1,10})\s*</uniqueid>", re.IGNORECASE
)
_NFO_LIMIT = 256 * 1024  # an .nfo is a few lines; nothing bigger is read
PARSE_PIECE_S = 0.02  # parsing names: the loop gets a turn at least this often


class FoundFile(DomainModel):
    """One video of a folder as read: its path (never logged), size and change time; what it is
    (`kind`; None: its name says nothing), the title and year its name or folder gives (a film's
    or the show's), an episode's season and number(s), the picture's quality ("4K", "1080p",
    "720p", "SD") and a TMDB id from an .nfo or from its stored row."""

    path: str
    size: int
    mtime: float
    kind: FileKind | None
    title: str
    year: int | None
    season: int | None
    episode: int | None
    episode_end: int | None
    quality: str | None
    tmdb_id: int | None


class FolderRead(DomainModel):
    """One folder's read: `reached` False when it couldn't be read (in time); then `files` is
    empty and the caller keeps what it stored for that folder, and `slow` says whether it did
    answer (its read went past its limit, or such a read still runs) rather than not at all.
    `arriving`: the videos left out because they changed less than `quiet_s` ago (they count
    at a check once settled)."""

    folder: str
    reached: bool
    files: tuple[FoundFile, ...]
    arriving: int = 0
    slow: bool = False


class _Progress:
    """How far a read got: its thread counts every step (a listing, an entry, an .nfo); the
    loop only looks."""

    __slots__ = ("steps",)

    def __init__(self) -> None:
        self.steps = 0

    def step(self) -> None:
        self.steps += 1


@dataclass(frozen=True, slots=True)
class _Name:
    """What a file name says."""

    title: str
    year: int | None = None
    season: int | None = None
    episode: int | None = None
    episode_end: int | None = None


@dataclass(frozen=True, slots=True)
class _Seen:
    """A video the walk kept: `parts` are its folders below the read folder; `nfo_tmdb` the TMDB
    id of its .nfo (read only for new or changed files); `row` its stored row when unchanged;
    `show` the name of the folder that names an episode's show."""

    path: str
    size: int
    mtime: float
    parts: tuple[str, ...]
    nfo_tmdb: int | None
    row: LibraryFile | None
    show: str | None = None  # an episode's show folder's name (`_show_folder`)


@dataclass(frozen=True, slots=True)
class _Ended:
    """A read past its limit that ended at last: when (the loop's time) and what it found."""

    at: float
    seen: list[_Seen]
    arriving: int


class LongReads:
    """A caller's folder reads that went past their limit (`read_folder`'s `busy`). While one
    still runs in its thread its folder isn't read again (not reached; `slow` when it had
    answered). When it ends, `on_ended` is called (on the loop) and, if it succeeded, its result
    serves the folder's next read of the same kind within LATE_KEPT_S, once."""

    def __init__(self, on_ended: Callable[[], None] | None = None) -> None:
        self._running: dict[str, _Progress] = {}
        self._ended: dict[tuple[str, FolderKind], _Ended] = {}
        self._on_ended = on_ended

    def __contains__(self, folder: object) -> bool:
        return folder in self._running

    def answered(self, folder: str) -> bool:
        """Whether the read of `folder` still running had answered (took a step)."""
        progress = self._running.get(folder)
        return progress is not None and progress.steps > 0

    def _hold(
        self,
        folder: str,
        kind: FolderKind,
        work: asyncio.Future[tuple[list[_Seen], int]],
        progress: _Progress,
    ) -> None:
        self._running[folder] = progress
        loop = asyncio.get_running_loop()

        def ended(work: asyncio.Future[tuple[list[_Seen], int]]) -> None:
            if self._running.get(folder) is progress:
                del self._running[folder]
            if not work.cancelled() and work.exception() is None:
                seen, arriving = work.result()
                self._ended[(folder, kind)] = _Ended(loop.time(), seen, arriving)
            log.info("A library folder's long read has ended")
            if self._on_ended is not None:
                try:
                    self._on_ended()
                except Exception as exc:  # by its type only: a message could hold a path
                    log.warning("A long read's listener failed (%s)", type(exc).__name__)

        work.add_done_callback(ended)

    def _take(self, folder: str, kind: FolderKind) -> _Ended | None:
        ended = self._ended.pop((folder, kind), None)
        if ended is None or asyncio.get_running_loop().time() - ended.at > LATE_KEPT_S:
            return None
        return ended


def _connection_lost(error: OSError, root: str) -> bool:
    """Whether an error below the read folder means the folder itself went away (the share
    dropped, the NAS stopped answering) rather than one bad entry in it: a network or timeout
    error, or the read folder no longer there. Only then is the whole folder not reached."""
    if isinstance(error, TimeoutError | ConnectionError):
        return True
    if getattr(error, "winerror", None) in _CONNECTION_ERRORS:
        return True
    try:
        return not os.path.isdir(root)
    except OSError:  # pragma: no cover - isdir answers False rather than raising
        return True


def _walk(root: str, progress: _Progress | None = None) -> list[tuple[str, int, float]]:
    """Every file below `root` (not in a skipped folder) with its size and change time. Runs in
    a thread; `progress` counts its steps (the folder answering, each listing and entry).

    All or nothing for the folder itself: it raises OSError when `root` can't be listed, or when
    an error below it means the folder went away (`_connection_lost`: a network or timeout
    error, or `root` gone), so the folder isn't reached rather than half read. Any other error
    below it (a folder this user may not read, a broken junction, a path too long, a folder or
    file deleted meanwhile) leaves out only that entry; how many is logged, the count only."""
    if not os.path.isdir(root):
        raise FileNotFoundError("library folder not there")  # no path in the text
    step = progress.step if progress is not None else _no_step
    step()  # it answered
    found: list[tuple[str, int, float]] = []
    left_out = 0
    pending = [(root, True)]
    while pending:
        folder, top = pending.pop()
        try:
            with _scandir(folder) as entries:
                step()
                for entry in entries:
                    step()
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            name = entry.name.casefold()
                            skipped = name in _SKIPPED_FOLDERS or (top and name in _SKIPPED_AT_TOP)
                            if not skipped:
                                pending.append((entry.path, False))
                        elif entry.is_file():
                            stat = entry.stat()
                            found.append((entry.path, stat.st_size, stat.st_mtime))
                    except OSError as error:
                        if _connection_lost(error, root):
                            raise
                        left_out += 1  # this entry only
        except OSError as error:
            if top or _connection_lost(error, root):
                raise
            left_out += 1  # a folder below it that can't be read: the rest still counts
    if left_out:
        log.info("Reading a library folder: %d entries below it couldn't be read", left_out)
    return found


def _no_step() -> None:
    pass


def _nfo_tmdb(path: str) -> int | None:
    """The TMDB id an .nfo names (`<uniqueid type="tmdb">`); None without one or unreadable."""
    try:
        with open(path, "rb") as file:
            text = file.read(_NFO_LIMIT).decode("utf-8", "replace")
    except OSError:
        return None
    found = _TMDB_IN_NFO.search(text)
    return int(found.group(1)) if found else None


def _show_folder(root: str, parts: tuple[str, ...]) -> str | None:
    """The folder that holds an episode's show (None: the episode lies right in the read
    folder): its top folder below the read folder, or, when that is a season's own folder
    ("Season 01", "Specials", "Seizoen 2"), the read folder itself (a single show's folder set
    as a series folder)."""
    if not parts:
        return None
    if _SEASON_FOLDER.match(parts[0].strip()):
        return root
    return os.path.join(root, parts[0])


def _look(
    root: str,
    kind: FolderKind,
    known: Mapping[str, LibraryFile],
    *,
    quiet_s: float,
    now: float,
    videos: Collection[str],
    progress: _Progress | None = None,
) -> tuple[list[_Seen], int]:
    """The folder's videos that count (in a thread): settled, no part-download, trailer, sample
    or extra; the .nfo ids of the new and changed ones. Also how many videos were left out as
    not settled yet (changed less than `quiet_s` ago). `progress` counts its steps."""
    files = _walk(root, progress)
    present = {path.casefold(): path for path, _size, _mtime in files}
    nfos: dict[str, int | None] = {}

    def nfo(path: str | None) -> int | None:
        if path is None or (real := present.get(path.casefold())) is None:
            return None
        if real not in nfos:
            nfos[real] = _nfo_tmdb(real)
            if progress is not None:
                progress.step()
        return nfos[real]

    kept: list[tuple[str, int, float, tuple[str, ...]]] = []
    arriving = 0
    for path, size, mtime in files:
        name = os.path.basename(path)
        folded = name.casefold()
        stem, suffix = os.path.splitext(name)
        if (
            folded.endswith(PART_ENDINGS)
            or suffix.casefold() not in videos
            or _NOT_A_COPY.search(stem)
        ):
            continue
        if abs(now - mtime) < quiet_s:
            # Still being written: counts once quiet. A change time further in the future
            # than that (a clock off on the NAS) counts as settled, never as arriving forever.
            arriving += 1
            continue
        kept.append((path, size, mtime, PurePath(os.path.relpath(path, root)).parts[:-1]))
    mixed = _mixed_show_folders(root, kept) if kind == "show" else frozenset()
    seen: list[_Seen] = []
    for path, size, mtime, parts in kept:
        stem = os.path.splitext(os.path.basename(path))[0]
        show = _show_folder(root, parts) if kind == "show" else None
        if show in mixed:
            show = None  # several shows in one folder: each file goes by its own name
        row = known.get(path)
        if row is not None and row.size == size and row.mtime == mtime:
            seen.append(_Seen(path, size, mtime, parts, None, row, show))
            continue
        folder = os.path.dirname(path)
        if kind == "movie":
            found = nfo(os.path.join(folder, f"{stem}.nfo"))
            if found is None and parts:  # a film's own folder: never the films folder's
                found = nfo(os.path.join(folder, "movie.nfo"))
        else:
            found = nfo(os.path.join(show, "tvshow.nfo")) if show is not None else None
        seen.append(_Seen(path, size, mtime, parts, found, None, show))
    return seen, arriving


def _show_key(stem: str) -> str | None:
    """The show an episode's own name gives before its SxxEyy code (the rough patterns, no
    guessit: cheap enough for every file of every check), as a key to compare by: letters and
    digits only, a year at its end left out ("Doctor.Who.2005" and "Doctor Who (2005)" are one
    show). None when the name gives none ("S01E02 - Pilot", an episode's title alone)."""
    if _EPISODE.search(stem) is None:
        return None
    named = _rough_name(stem, "show")
    if named is None or named.episode is None:
        return None
    key = "".join(char for char in named.title.casefold() if char.isalnum())
    return _KEY_YEAR.sub("", key) or key or None


def _mixed_show_folders(
    root: str, files: Iterable[tuple[str, int, float, tuple[str, ...]]]
) -> frozenset[str]:
    """The show folders (`_show_folder`) whose episodes' own names give two shows or more: a
    flat folder of several shows ("Series (flat)" below the series folder), read file by file.
    Names of one show that one starts the other ("The Office" / "The Office US") count once."""
    keys: dict[str, set[str]] = {}
    for path, _size, _mtime, parts in files:
        show = _show_folder(root, parts)
        key = _show_key(os.path.splitext(os.path.basename(path))[0])
        if show is not None and key is not None:
            keys.setdefault(show, set()).add(key)
    mixed: list[str] = []
    for show, found in keys.items():
        apart: list[str] = []
        for key in sorted(found, key=len):  # the shorter first: a longer one may start with it
            if not any(key.startswith(other) for other in apart):
                apart.append(key)
        if len(apart) >= 2:
            mixed.append(show)
    if mixed:
        log.info("Reading a series folder: %d folders of several shows, read file by file",
                 len(mixed))
    return frozenset(mixed)


def _clean(text: str) -> str:
    return " ".join(text.replace(".", " ").replace("_", " ").split())


def _tidy(name: str) -> _Name | None:
    """"Title (Year)" at the start of a folder's or file's name."""
    found = _TIDY.match(name)
    if found is None or not (title := _clean(found.group(1))):
        return None
    return _Name(title, int(found.group(2)))


def _show_name(folder: str) -> _Name:
    """A show folder's title and year: "Severance (2022)" → Severance, 2022. A release's own
    folder ("Severance.S02.2160p.ATVP.WEB-DL…", "Doctor.Who.2005.S01.1080p…") is cut at its
    season code or its first quality tag, a year at its end taken as the year. The title may
    come out empty (a folder such as "Season 4 Complete 1080p"): the file's own name says then."""
    tidy = _tidy(folder)
    if tidy is not None:
        return tidy
    code = _SEASON_CODE.search(folder)
    cut = folder[: code.start()] if code is not None else folder
    cut = _TAGS.sub("", cut)
    if code is None and cut == folder:
        return _Name(_clean(folder))  # no code, no tags: the folder's name is the title
    if (raw := _RAW_YEAR.match(cut + " ")) is not None and (title := _clean(raw.group(1))):
        return _Name(title.rstrip(" -"), int(raw.group(2)))
    return _Name(_clean(cut).rstrip(" -"))


def _episode_numbers(stem: str) -> tuple[int, int, int | None] | None:
    found = _EPISODE.search(stem)
    if found is None:
        return None
    first = int(found.group(2))
    last = int(found.group(3)) if found.group(3) else None
    return int(found.group(1)), first, last if last != first else None


def _rough_name(stem: str, kind: FolderKind) -> _Name | None:
    """Without guessit: SxxEyy and "Title (Year)" / "Title.Year." patterns, as duplicates.py."""
    numbers = _episode_numbers(stem)
    code = _EPISODE.search(stem)
    if kind == "show" and numbers is not None and code is not None:
        title = _clean(_TAGS.sub("", stem[: code.start()])).rstrip(" -")
        year: int | None = None
        if (dated := _TRAILING_YEAR.match(title)) is not None:  # "Show A (2022) - S01E02 - …"
            title, year = _clean(dated.group(1)).rstrip(" -"), int(dated.group(2))
        if not title:
            return None
        season, episode, end = numbers
        return _Name(title, year, season, episode, end)
    named = _tidy(stem)
    if named is None and (raw := _RAW_YEAR.match(stem)) is not None:
        if title := _clean(raw.group(1)):
            named = _Name(title, int(raw.group(2)))
    if named is None and (title := _clean(_TAGS.sub("", stem))):
        named = _Name(title)
    return named


def _parse_name(name: str, kind: FolderKind) -> _Name | None:
    """What a raw file name says (the parse seam: tests count its calls): MediaForge's
    `parse_filename` when guessit is there, else the rough patterns."""
    from app.forge.core import parser  # MediaForge's: never loaded at AirGrabber's start
    from app.forge.models.media import TypeHint

    stem = os.path.splitext(name)[0]
    if not parser.parser_ready():
        return _rough_name(stem, kind)
    parsed = parser.parse_filename(name, TypeHint.MOVIE if kind == "movie" else TypeHint.TV)
    title = parsed.title if parsed.title != "_" else ""
    if not title.strip():
        return None
    if kind == "movie":
        return _Name(title, parsed.year)
    return _Name(title, parsed.year, parsed.season, parsed.episode, parsed.episode_end)


def quality_of(name: str) -> str | None:
    """The picture's quality a file name says: "4K", "1080p", "720p", "SD"; None if none."""
    for pattern, quality in _QUALITY:
        if pattern.search(name):
            return quality
    return None


def _found(seen: _Seen, kind: FolderKind, name: _Name | None, tmdb_id: int | None) -> FoundFile:
    file_kind: FileKind | None = None
    if name is not None or (tmdb_id is not None and kind == "movie"):
        file_kind = "movie" if kind == "movie" else "episode"  # a film's .nfo wins over its name
    return FoundFile(
        path=seen.path,
        size=seen.size,
        mtime=seen.mtime,
        kind=file_kind,
        title=name.title if name is not None else "",
        year=name.year if name is not None else None,
        season=name.season if name is not None else None,
        episode=name.episode if name is not None else None,
        episode_end=name.episode_end if name is not None else None,
        quality=quality_of(os.path.basename(seen.path)),
        tmdb_id=tmdb_id,
    )


def _tidy_film(seen: _Seen, stem: str) -> _Name | None:
    """A film's "Title (Year)": its folder's name, else its own."""
    folder = _tidy(seen.parts[-1]) if seen.parts else None
    return folder if folder is not None else _tidy(stem)


def _cheap_name(seen: _Seen, kind: FolderKind) -> _Name | None:
    """A title for a file read before, without parsing: its show folder's, its folder's or its
    name's "Title (Year)", else its name without the quality tags."""
    stem = os.path.splitext(os.path.basename(seen.path))[0]
    if kind == "show":
        show = _show_name(os.path.basename(seen.show)) if seen.show is not None else None
        return show if show is not None and show.title else _rough_name(stem, kind)
    tidy = _tidy_film(seen, stem)
    return tidy if tidy is not None else _rough_name(stem, kind)


def _reused(seen: _Seen, kind: FolderKind) -> FoundFile:
    """An unchanged file: its stored row, with a title made without parsing."""
    row = seen.row
    assert row is not None
    name = _cheap_name(seen, kind) if row.kind is not None else None
    return FoundFile(
        path=seen.path,
        size=seen.size,
        mtime=seen.mtime,
        kind=row.kind,
        title=name.title if name is not None else "",
        year=name.year if name is not None else None,
        season=row.season,
        episode=row.episode,
        episode_end=row.episode_end,
        quality=row.quality,
        tmdb_id=row.tmdb_id,
    )


def from_row(row: LibraryFile) -> FoundFile:
    """A stored file as if read (for a folder that couldn't be reached: its last result stays in
    the check). No title: `match_files` takes its id from the row and its title from the last
    check's."""
    return FoundFile(
        path=row.path,
        size=row.size,
        mtime=row.mtime,
        kind=row.kind,
        title="",
        year=None,
        season=row.season,
        episode=row.episode,
        episode_end=row.episode_end,
        quality=row.quality,
        tmdb_id=row.tmdb_id,
    )


def _recognise(seen: _Seen, kind: FolderKind) -> FoundFile:
    """A new or changed file: the folder's name, else its own name (parsed)."""
    file_name = os.path.basename(seen.path)
    stem = os.path.splitext(file_name)[0]
    if kind == "movie":
        name = _tidy_film(seen, stem)
        if name is None:
            name = _parse_name(file_name, kind)
        return _found(seen, kind, name, seen.nfo_tmdb)
    numbers = _episode_numbers(stem)
    if seen.show is not None:
        show = _show_name(os.path.basename(seen.show))
        parsed: _Name | None = None
        if numbers is None or not show.title:
            parsed = _parse_name(file_name, kind)
        if numbers is None and parsed is not None and parsed.episode is not None:
            numbers = (parsed.season or 1, parsed.episode, parsed.episode_end)
        if not show.title and parsed is not None:  # the folder names no show: the file does
            show = _Name(parsed.title, parsed.year)
        season, episode, end = numbers if numbers is not None else (None, None, None)
        name = _Name(show.title, show.year, season, episode, end) if show.title else None
        return _found(seen, kind, name, seen.nfo_tmdb)
    parsed = _parse_name(file_name, kind)
    if parsed is None or parsed.episode is None:
        return _found(seen, kind, None, None)  # no show folder and no episode: unreadable
    return _found(seen, kind, parsed, seen.nfo_tmdb)


async def read_folder(
    folder: str,
    kind: FolderKind,
    *,
    known: Mapping[str, LibraryFile],
    quiet_s: float,
    now: float,
    timeout_s: float = READ_TIMEOUT_S,
    max_s: float = MAX_READ_S,
    busy: LongReads | None = None,
) -> FolderRead:
    """Read one film (`kind` "movie") or series ("show") folder: its videos that count, each
    recognised by its name (or kept as `known`, the stored rows by path, when unchanged).
    `now`: the clock's time (POSIX seconds), against which a file must have been quiet for
    `quiet_s`. Not reached (logged, without the path) when it can't be read, when it goes
    `timeout_s` without a step forward, or past `max_s` in all (`slow` when it had answered).
    `busy`: the caller's reads that went past their limit (`LongReads`): a folder whose read
    still runs isn't read again until it has ended; then its result serves this read."""
    if busy is not None and folder in busy:
        log.info("A library folder's last read still runs: its last result stays")
        return FolderRead(folder=folder, reached=False, files=(), slow=busy.answered(folder))
    # MediaForge's, imported only here: AirGrabber's start loads none of it.
    from app.forge.core.parser import VIDEO_EXTENSIONS, prepare_parser

    late = busy._take(folder, kind) if busy is not None else None
    if late is not None:
        log.info("A library folder's long read is used")
        seen, arriving = late.seen, late.arriving
    else:
        stamps = dict(known)  # the thread gets its own copy
        progress = _Progress()
        work = asyncio.ensure_future(
            asyncio.to_thread(
                _look, folder, kind, stamps, quiet_s=quiet_s, now=now, videos=VIDEO_EXTENSIONS,
                progress=progress,
            )
        )
        try:
            outcome = await _waited(work, progress, timeout_s=timeout_s, max_s=max_s)
        except (OSError, TimeoutError) as exc:
            log.info(
                "A library folder can't be reached (%s): its last result stays", type(exc).__name__
            )
            return FolderRead(folder=folder, reached=False, files=())
        finally:
            if not work.done():  # past the limit, or cancelled: its thread ends on its own
                work.add_done_callback(_retrieve)
                if busy is not None:
                    busy._hold(folder, kind, work, progress)
        if outcome is None:
            answered = progress.steps > 0
            how = "answers, but reading it takes long" if answered else "can't be reached"
            log.info("A library folder %s: its last result stays", how)
            return FolderRead(folder=folder, reached=False, files=(), slow=answered)
        seen, arriving = outcome
    if any(one.row is None for one in seen):
        await prepare_parser()  # guessit's first import and rules, in pieces (once per run)
    found: list[FoundFile] = []
    started = time.perf_counter()
    for one in sorted(seen, key=lambda item: item.path):
        found.append(_reused(one, kind) if one.row is not None else _recognise(one, kind))
        if time.perf_counter() - started >= PARSE_PIECE_S:
            await asyncio.sleep(0.001)  # the window gets a turn
            started = time.perf_counter()
    reused = sum(1 for one in seen if one.row is not None)
    log.info("Read a library folder: %d videos (%d unchanged)", len(found), reused)
    return FolderRead(folder=folder, reached=True, files=tuple(found), arriving=arriving)


async def _waited(
    work: asyncio.Future[tuple[list[_Seen], int]],
    progress: _Progress,
    *,
    timeout_s: float,
    max_s: float,
) -> tuple[list[_Seen], int] | None:
    """The read's outcome (its OSError raised); None when it went `timeout_s` without a step
    forward, or past `max_s` in all. `work` is never cancelled here (its thread can't stop)."""
    loop = asyncio.get_running_loop()
    started = moved = loop.time()
    steps = progress.steps
    look_every = min(timeout_s, _LOOK_EVERY_S)
    while True:
        left = min(moved + timeout_s, started + max_s) - loop.time()
        if left <= 0:
            return None
        await asyncio.wait({work}, timeout=min(look_every, left))
        if work.done():
            return work.result()
        if progress.steps != steps:
            steps = progress.steps
            moved = loop.time()


def _retrieve(task: asyncio.Future[tuple[list[_Seen], int]]) -> None:
    """A read past its limit, ended at last: its outcome is not wanted."""
    with contextlib.suppress(BaseException):
        task.result()
