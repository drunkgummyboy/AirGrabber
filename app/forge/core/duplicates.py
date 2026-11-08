"""The same episode or film twice in the library folders (MediaForge's "+ Folder ▾" → "Find
duplicates…"), and moving the extra copies aside.

Episodes: below the Series folder, a video's show is its first folder there and its episode the
"S01E02" (or "S01E02-E03") in its name; two videos of one show with the same episodes are the
same. Films: below the Movies folder, "Title (Year)" from the start of the name (else the whole
name, without its quality tags) is the film. Trailers, samples and extras are no copies.

The biggest copy is kept (usually the best picture); the others move to a "Duplicates" folder
beside the library folder, under the same path (`Duplicates/Series/Show/Season 01/…`), through
the executor: journaled, so History's Undo puts them back. Nothing is deleted.

Reading the folders is plain file-system work for a thread (`asyncio.to_thread`); nothing here
logs a path.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.forge.core.parser import VIDEO_EXTENSIONS
from app.forge.models.plan import Plan, PlannedFile

QUARANTINE = "Duplicates"  # the folder beside the library folder the extra copies go to
_EPISODE = re.compile(r"(?<![a-z0-9])s(\d{1,3})e(\d{1,4})(?:-?e(\d{1,4}))?(?![0-9])", re.IGNORECASE)
_FILM = re.compile(r"^(.+?\(\d{4}\))")
_NOT_A_COPY = re.compile(
    r"(?:^|[ ._\-\[(])(sample|trailer|featurette|behind ?the ?scenes|deleted ?scenes?|extras?)"
    r"(?:$|[ ._\-\])])",
    re.IGNORECASE,
)
_TAGS = re.compile(r"[ ._\-\[(]+(2160p|1080p|720p|480p|4k|uhd|hdr|web.?dl|bluray|x26[45]|hevc).*$", re.I)
_NOT_COPIES_FOLDERS = frozenset({"extras", "featurettes", "behind the scenes", "trailers"})


@dataclass(frozen=True, slots=True)
class Copy:
    path: Path
    size: int


@dataclass(frozen=True, slots=True)
class Duplicate:
    """One episode or film found more than once: `label` ("Show S01E02", "Dune (2021)"), the
    copy kept (the biggest) and the `extra` ones."""

    label: str
    keep: Copy
    extra: tuple[Copy, ...]


def _videos(root: Path) -> Iterable[tuple[Path, int]]:
    """Every video below `root` (not in an extras folder, no trailer or sample), with its size."""
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d.casefold() not in _NOT_COPIES_FOLDERS]
        for name in files:
            path = Path(folder, name)
            if path.suffix.lower() not in VIDEO_EXTENSIONS or _NOT_A_COPY.search(path.stem):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue  # gone meanwhile, or out of reach
            yield path, size


def episode_key(root: Path, path: Path) -> tuple[str, tuple[str, int, int, int]] | None:
    """("Show S01E02", (show, season, first, last)) for a video below the Series folder `root`;
    None when it names no episode or sits right in the root."""
    parts = path.relative_to(root).parts
    if len(parts) < 2:
        return None
    match = _EPISODE.search(path.stem)
    if match is None:
        return None
    season, first = int(match.group(1)), int(match.group(2))
    last = int(match.group(3)) if match.group(3) else first
    code = f"S{season:02d}E{first:02d}" + (f"-E{last:02d}" if last != first else "")
    return f"{parts[0]} {code}", (parts[0].casefold(), season, first, last)


def film_key(path: Path) -> tuple[str, str]:
    """("Dune (2021)", "dune (2021)"): the film a video below the Movies folder is."""
    stem = path.stem
    match = _FILM.match(stem)
    title = match.group(1) if match else _TAGS.sub("", stem)
    title = " ".join(title.replace(".", " ").split())
    return title, title.casefold()


def _groups(found: dict[object, tuple[str, list[Copy]]]) -> list[Duplicate]:
    duplicates = []
    for label, copies in found.values():
        if len(copies) < 2:
            continue
        ordered = sorted(copies, key=lambda copy: (-copy.size, str(copy.path)))
        duplicates.append(Duplicate(label, ordered[0], tuple(ordered[1:])))
    return sorted(duplicates, key=lambda duplicate: duplicate.label.casefold())


def find_duplicates(series: Path | None, movies: Path | None) -> list[Duplicate]:
    """The duplicates in the Series and Movies folders (either may be None), by label."""
    found: dict[object, tuple[str, list[Copy]]] = {}
    if series is not None and series.is_dir():
        for path, size in _videos(series):
            key = episode_key(series, path)
            if key is not None:
                label, episode = key
                found.setdefault(("tv", episode), (label, []))[1].append(Copy(path, size))
    if movies is not None and movies.is_dir():
        for path, size in _videos(movies):
            label, film = film_key(path)
            found.setdefault(("film", film), (label, []))[1].append(Copy(path, size))
    return _groups(found)


def quarantine_target(path: Path, roots: Sequence[Path]) -> Path | None:
    """Where an extra copy goes: `Duplicates/<library folder>/<its path there>` beside the
    library folder it is in; None when it is in none of `roots`."""
    for root in roots:
        try:
            inner = path.relative_to(root)
        except ValueError:
            continue
        return root.parent / QUARANTINE / root.name / inner
    return None


def quarantine_plan(duplicates: Iterable[Duplicate], roots: Sequence[Path]) -> Plan:
    """A plan moving every extra copy aside (`quarantine_target`), for the executor."""
    files: list[PlannedFile] = []
    for duplicate in duplicates:
        for copy in duplicate.extra:
            target = quarantine_target(copy.path, roots)
            if target is not None:
                files.append(PlannedFile(item_id=len(files) + 1, source=copy.path, target=target))
    return Plan(files=tuple(files))
