"""Finding the video files of a grab in the finished-downloads folder, and saving subtitles next
to them. Pure functions and blocking file I/O: the subtitle service runs the I/O in a thread.

AirGrabber hands torrents to a torrent program, so it doesn't know where a download lands. A
scan walks the folder (bounded depth and size; hidden, system and NAS index folders such as
"@eaDir" and "#recycle" skipped) and lists the video files, the partial downloads (.part,
.!qB, ...) and the subtitle files already there. A grab owns:

- the files inside a folder named like its release title, or a file named like it (names are
  compared normalised: lowercase, words only, "[site]" tags and file extensions dropped), and
- files named for what it is: an episode "Show Name S02E04 ..." (a season pack: every
  "Show Name S02E.." file), a film "Title 2024 ..." (title and year from its release title).

Samples are never a grab's video. A file changed in the last few minutes is still growing: it
belongs to its grab but isn't handled yet.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from app.models import DownloadRecord, MediaType

VIDEO_EXTENSIONS = frozenset(
    {
        ".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts", ".webm", ".mpg",
        ".mpeg", ".divx", ".ogm",
    }
)  # fmt: skip
PARTIAL_EXTENSIONS = frozenset({".part", ".!qb", ".!ut", ".crdownload", ".tmp", ".bc!", ".partial"})
SUBTITLE_EXTENSIONS = frozenset({".srt", ".ass", ".ssa", ".sub", ".vtt"})
SAMPLE_WORDS = frozenset({"sample", "samples"})
MAX_DEPTH = 4  # folders below the finished-downloads folder that are looked into
MAX_ENTRIES = 50_000  # directory entries one scan reads at most
SETTLE_S = 300.0  # a file changed within this many seconds may still be growing

_SKIP_PREFIXES = (".", "@", "#", "$", "~")  # hidden, Synology/QNAP index and recycle folders
_TAG_RE = re.compile(r"\[[^\]]*\]|\{[^}]*\}")
_SITE_PREFIX_RE = re.compile(r"^\s*(?:www\.)?[a-z0-9-]+\.(?:com|org|net|to|re|se)\s+-\s+", re.I)
_EPISODE_RE = re.compile(r"\bs(\d{1,2}) ?e(\d{1,3})(?!\d)|\b(\d{1,2})x(\d{2,3})\b")
_YEAR_RE = re.compile(r"\b(?:19|20)\d\d\b")


@dataclass(frozen=True, slots=True)
class VideoFile:
    path: str  # absolute, as the scan found it (the subtitle bookkeeping's key)
    parts: tuple[str, ...]  # the folders below the scanned folder, then the file name
    size: int
    growing: bool = False  # changed within SETTLE_S: maybe still downloading

    @property
    def stem(self) -> str:
        return os.path.splitext(self.parts[-1])[0]


@dataclass(slots=True)
class FolderScan:
    videos: list[VideoFile] = field(default_factory=list)
    partial: list[tuple[str, ...]] = field(default_factory=list)  # parts of partial downloads
    subtitles: set[str] = field(default_factory=set)  # lowercase paths of subtitle files
    truncated: bool = False  # stopped at MAX_ENTRIES


@dataclass(frozen=True, slots=True)
class GrabVideo:
    """A video file of a grab, with the episode it holds (season packs: its own)."""

    video: VideoFile
    season: int | None
    episode: int | None


@dataclass(frozen=True, slots=True)
class GrabFiles:
    videos: tuple[GrabVideo, ...] = ()
    incomplete: bool = False  # a matching file is still growing or partial


# -- names -------------------------------------------------------------------------------------


def normalize(name: str) -> str:
    """Lowercase words separated by single spaces: "[site]" tags, apostrophes and punctuation
    go, "&" reads "and". "Marvel's.Agents.of.S.H.I.E.L.D" -> "marvels agents of s h i e l d"."""
    text = _SITE_PREFIX_RE.sub("", name)
    text = _TAG_RE.sub(" ", text).lower().replace("&", " and ")
    text = re.sub(r"['’`]", "", text)
    return " ".join(re.sub(r"[^0-9a-z]+", " ", text).split())


def release_name(name: str) -> str:
    """A file or folder name normalised for comparing with a release title (a video or partial
    file's extension dropped)."""
    base, ext = os.path.splitext(name)
    while ext.lower() in PARTIAL_EXTENSIONS or ext.lower() in VIDEO_EXTENSIONS:
        name = base
        base, ext = os.path.splitext(name)
    return normalize(name)


def parse_episode(name: str) -> tuple[int, int] | None:
    """(season, episode) of "Show.S02E04..." / "Show 2x04" (a double episode: the first)."""
    match = _EPISODE_RE.search(normalize(name))
    if match is None:
        return None
    season, episode = match.group(1) or match.group(3), match.group(2) or match.group(4)
    return int(season), int(episode)


def movie_identity(release_title: str) -> tuple[str, str] | None:
    """(normalised title, year) of a film's release title: "Dune.Part.Two.2024.1080p..." ->
    ("dune part two", "2024"). The year is the last one with a title before it ("Blade Runner
    2049 2017" -> 2017)."""
    text = normalize(release_title)
    for match in reversed(list(_YEAR_RE.finditer(text))):
        title = text[: match.start()].strip()
        if title:
            return title, match.group(0)
    return None


def is_sample(parts: Iterable[str]) -> bool:
    for part in parts:
        if SAMPLE_WORDS & set(release_name(part).split()):
            return True
    return False


# -- scanning ----------------------------------------------------------------------------------


def scan_folder(
    root: Path,
    *,
    now: float,
    max_depth: int = MAX_DEPTH,
    max_entries: int = MAX_ENTRIES,
    settle_s: float = SETTLE_S,
) -> FolderScan:
    """The videos, partial downloads and subtitle files below `root` (blocking; run it in a
    thread). Raises OSError when `root` itself can't be read."""
    scan = FolderScan()
    entries = 0
    pending: list[tuple[Path, tuple[str, ...]]] = [(root, ())]
    first = True
    while pending:
        folder, parts = pending.pop()
        try:
            listing = os.scandir(folder)
        except OSError:
            if first:
                raise
            continue
        first = False
        with listing:
            for entry in listing:
                entries += 1
                if entries > max_entries:
                    scan.truncated = True
                    return scan
                name = entry.name
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if len(parts) < max_depth and not name.startswith(_SKIP_PREFIXES):
                            pending.append((Path(entry.path), (*parts, name)))
                        continue
                    if not entry.is_file():
                        continue
                    ext = os.path.splitext(name)[1].lower()
                    if ext in SUBTITLE_EXTENSIONS:
                        scan.subtitles.add(entry.path.lower())
                    elif ext in PARTIAL_EXTENSIONS:
                        scan.partial.append((*parts, name))
                    elif ext in VIDEO_EXTENSIONS and not is_sample((*parts, name)):
                        stat = entry.stat()
                        scan.videos.append(
                            VideoFile(
                                path=entry.path,
                                parts=(*parts, name),
                                size=stat.st_size,
                                growing=now - stat.st_mtime < settle_s,
                            )
                        )
                except OSError:
                    continue  # vanished or unreadable meanwhile
    return scan


# -- matching ----------------------------------------------------------------------------------


def _named_like(parts: tuple[str, ...], title: str) -> bool:
    """A folder on the way to the file, or the file itself, is named like the release."""
    return any(release_name(part) == title for part in parts)


def _episode_pattern(show_name: str) -> re.Pattern[str]:
    """"show name [year] [country] s02e04 ..." at the start of a normalised name."""
    words = re.escape(normalize(show_name))
    return re.compile(
        rf"^{words}(?: (?:19|20)\d\d)?(?: (?:us|uk|au|nz|ca))?"
        r" (?:s\d{1,2} ?e\d{1,3}|\d{1,2}x\d{2,3})\b"
    )


def _movie_named(parts: tuple[str, ...], identity: tuple[str, str]) -> bool:
    title, year = identity
    prefix = f"{title} {year}"
    return any(
        (name := release_name(part)) == prefix or name.startswith(prefix + " ") for part in parts
    )


def grab_files(
    record: DownloadRecord, scan: FolderScan, *, show_name: str | None = None
) -> GrabFiles:
    """The video files of a grab (see the module docstring). `show_name`: the TV show's name,
    for files named "Show Name S02E04" outside a folder named like the release."""
    title = normalize(record.release_title)
    if not title:
        return GrabFiles()
    if record.media_type is MediaType.TV:
        pattern = _episode_pattern(show_name) if show_name and normalize(show_name) else None

        def owns(parts: tuple[str, ...]) -> bool:
            if _named_like(parts, title):
                return True
            return pattern is not None and pattern.match(release_name(parts[-1])) is not None

    else:
        identity = movie_identity(record.release_title)

        def owns(parts: tuple[str, ...]) -> bool:
            if _named_like(parts, title):
                return True
            return identity is not None and _movie_named(parts, identity)

    found: list[GrabVideo] = []
    incomplete = any(owns(parts) for parts in scan.partial)
    for video in scan.videos:
        if is_sample(video.parts) or not owns(video.parts):
            continue
        episode = parse_episode(video.parts[-1])
        if record.media_type is MediaType.MOVIE:
            if episode is not None:
                continue
            item = GrabVideo(video, None, None)
        elif record.episode is not None:  # one episode
            if episode is not None and episode != (record.season, record.episode):
                continue
            item = GrabVideo(video, record.season, record.episode)
        else:  # a season pack: every episode of that season, each its own
            if episode is None or episode[0] != record.season:
                continue
            item = GrabVideo(video, episode[0], episode[1])
        if video.growing:
            incomplete = True
            continue
        found.append(item)
    return GrabFiles(videos=tuple(found), incomplete=incomplete)


# -- subtitle files ----------------------------------------------------------------------------


def subtitle_path(video_path: str, language: str) -> str:
    """"…/Show.S01E02.mkv" + "en" -> "…/Show.S01E02.en.srt" (next to the video)."""
    base = os.path.splitext(video_path)[0]
    return f"{base}.{language}.srt"


def has_subtitle(video_path: str, language: str, existing: Iterable[str]) -> bool:
    """A subtitle for this language is next to the video already: "<stem>.<lang>.srt", or a
    variant such as "<stem>.<lang>.forced.srt" / ".<lang>.ass" (`existing`: lowercase paths)."""
    prefix = f"{os.path.splitext(video_path)[0]}.{language}.".lower()
    return any(path.startswith(prefix) and _subtitle_ext(path) for path in existing)


def _subtitle_ext(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in SUBTITLE_EXTENSIONS


def write_new_file(path: str, content: bytes) -> bool:
    """Write `content` to `path` atomically, never over an existing file: written to a
    temporary file next to it, then linked (or, where links aren't possible, renamed — which
    Windows refuses over an existing file) into place. False when `path` exists already.
    Blocking; raises OSError when the folder can't be written."""
    if os.path.exists(path):
        return False
    temporary = f"{path}.airgrabber-{os.getpid()}.tmp"
    with open(temporary, "xb") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())
    try:
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        except OSError:  # no hard links here (FAT, some network shares)
            if os.path.exists(path):
                return False
            try:
                os.rename(temporary, path)
            except FileExistsError:
                return False
            return True
        return True
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)

