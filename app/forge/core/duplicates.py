"""The same episode or film twice in the library folders (MediaForge's "Library ▾" → "Find
duplicates…"), and moving the extra copies aside.

Episodes: below the Series folder, a video's show is its first folder there and its episode the
"S01E02" (or "S01E02-E03") in its name; two videos of one show with the same episodes are the
same. Films: below the Movies folder, "Title (Year)" from the start of the name (else the whole
name, without its quality tags) is the film. Trailers, samples and extras are no copies.

The biggest copy is kept (usually the best picture); the others move to a "Duplicates" folder
beside the library folder, under the same path (`Duplicates/Series/Show/Season 01/…`), through
the executor: journaled, so History's Undo puts them back. Nothing is deleted. A share or drive
root (`//NAS/Movies`, `M:/`) has no folder beside it: then the owner chooses one
(`needs_a_place`, `place_problem`: never inside a library folder) and the copies go there under
the library folder's name. A top-level "Duplicates" folder in a library folder is never scanned.
The queue's "Replace the one in the library" (app/forge/core/auto.py's `replace_runs`) moves the
copy it replaces aside by the same rule (`quarantine_target`, `free_aside`, `aside_folder`).

Reading the folders is plain file-system work for a thread (`asyncio.to_thread`); nothing here
logs a path.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from app.forge.core.library import inside
from app.forge.core.parser import VIDEO_EXTENSIONS
from app.forge.models.plan import Plan, PlannedFile
from app.i18n import N_

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
PLACE_INSIDE = N_("Choose a folder outside your Series and Movies folders.")  # t() where shown


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
        if folder == str(root):  # copies moved aside earlier (a chosen place in the library)
            dirs[:] = [d for d in dirs if d.casefold() != QUARANTINE.casefold()]
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


def is_root(folder: Path) -> bool:
    """A share or drive root (`//NAS/Movies`, `M:/`): nothing beside it to put copies in."""
    return folder.parent == folder or folder.name == ""


def needs_a_place(roots: Sequence[Path]) -> bool:
    """Whether the owner must choose where the extra copies go: a library folder is a root."""
    return any(is_root(root) for root in roots)


def place_problem(place: Path, roots: Sequence[Path]) -> str | None:
    """Why a chosen place can't take the extra copies (in English: t() it), else None: inside
    a library folder, or a library folder would be its `<place>/<library folder's name>`."""
    if any(inside(place, root) for root in roots):
        return PLACE_INSIDE
    if any(inside(place / _root_name(r), root) for r in roots for root in roots):
        return PLACE_INSIDE
    return None


def _root_name(root: Path) -> str:
    """The library folder's name below the place: its own, a share's or a drive's letter."""
    if root.name:
        return root.name
    share = root.drive.replace("\\", "/").rstrip("/:")  # "//NAS/Movies", "M"
    return share.rsplit("/", 1)[-1] or QUARANTINE


def quarantine_target(path: Path, roots: Sequence[Path], place: Path | None = None) -> Path | None:
    """Where an extra copy goes: `Duplicates/<library folder>/<its path there>` beside the
    library folder it is in; for a share or drive root `<place>/<its name>/…` in the chosen
    place (only there: the other library folder keeps its Duplicates beside it). None when it
    is in none of `roots`, or in a root without a place (never inside the library itself)."""
    for root in roots:
        try:
            inner = path.relative_to(root)
        except ValueError:
            continue
        if not is_root(root):
            return root.parent / QUARANTINE / root.name / inner
        return None if place is None else place / _root_name(root) / inner
    return None


def aside_folder(library: Path, place: Path | None) -> Path | None:
    """The folder a library folder's replaced or extra copies go into: `Duplicates` beside it,
    for a share or drive root the chosen `place` (None without one). For a question naming it."""
    if not is_root(library):
        return library.parent / QUARANTINE
    return place


def free_aside(target: Path, exists: Callable[[Path], bool], taken: set[str]) -> Path:
    """`target`, or "name (n).ext" when a copy moved aside earlier has that name: one moved aside
    is never put over another. `taken`: names this plan gave already (normcased), updated."""
    n = 0
    while True:
        candidate = target if n == 0 else target.with_name(f"{target.stem} ({n}){target.suffix}")
        key = os.path.normcase(str(candidate))
        if key not in taken and not exists(candidate):
            taken.add(key)
            return candidate
        n += 1


def _same_path(a: Path, b: Path) -> bool:
    return os.path.normcase(str(a)) == os.path.normcase(str(b))


def quarantine_plan(
    duplicates: Iterable[Duplicate], roots: Sequence[Path], place: Path | None = None
) -> Plan:
    """A plan moving every extra copy aside (`quarantine_target`), for the executor."""
    files: list[PlannedFile] = []
    for duplicate in duplicates:
        for copy in duplicate.extra:
            target = quarantine_target(copy.path, roots, place)
            if target is None or _same_path(target, copy.path):
                continue  # a move onto itself is refused: it would say "moved" and do nothing
            files.append(PlannedFile(item_id=len(files) + 1, source=copy.path, target=target))
    return Plan(files=tuple(files))
