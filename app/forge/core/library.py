"""The library folders: what is still missing before MediaForge can watch, and folder tests.

Paths are compared the way Windows does (case-insensitive, `\\\\server\\share` included) and by
whole folder names, so `Series2` is not inside `Series`.
"""

from __future__ import annotations

import os
from pathlib import Path

from app.forge.models.settings import ForgeSettings
from app.i18n import N_, t

OVERLAP = N_("A watched folder can't be inside a library folder, or the other way round.")
NESTED = N_("A watched folder can't be inside another watched folder.")


def _parts(path: Path) -> tuple[str, ...]:
    return tuple(os.path.normcase(part).rstrip("\\/") for part in path.parts)


def inside(path: Path, folder: Path) -> bool:
    """Whether `path` is `folder` or anything below it."""
    mine, theirs = _parts(path), _parts(folder)
    return len(mine) >= len(theirs) and mine[: len(theirs)] == theirs


def library_problems(settings: ForgeSettings) -> list[str]:
    """What still needs choosing (plain sentences, translated); empty when watching can start.
    The watched folders include AirGrabber's (B-01: `airgrabber_folders.watched_folders`)."""
    from app.forge.core.airgrabber_folders import watched_folders  # it imports this module

    watch_folders = watched_folders(settings)
    problems: list[str] = []
    if not watch_folders:
        problems.append(t("Choose a download folder to watch."))
    if not settings.series_folder:
        problems.append(t("Choose the Series library folder."))
    if not settings.movies_folder:
        problems.append(t("Choose the Movies library folder."))
    libraries = [Path(f) for f in (settings.series_folder, settings.movies_folder) if f]
    watched = [Path(f) for f in watch_folders]
    if any(inside(w, lib) or inside(lib, w) for w in watched for lib in libraries):
        problems.append(t(OVERLAP))
    if any(inside(a, b) or inside(b, a) for i, a in enumerate(watched) for b in watched[i + 1 :]):
        problems.append(t(NESTED))
    return problems


def watched_root(path: Path, settings: ForgeSettings) -> Path | None:
    """The watched folder `path` lies in, if any."""
    from app.forge.core.airgrabber_folders import watched_folders  # it imports this module

    return next((Path(f) for f in watched_folders(settings) if inside(path, Path(f))), None)
