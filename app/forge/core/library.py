"""The library folders: what is still missing before MediaForge can watch, and folder tests.

Paths are compared the way Windows does (case-insensitive, `\\\\server\\share` included) and by
whole folder names, so `Series2` is not inside `Series`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from app.forge.models.media import MediaKind
from app.forge.models.plan import PlanInput
from app.forge.models.preset import PresetOptions
from app.forge.models.settings import ForgeSettings
from app.i18n import N_, t

if TYPE_CHECKING:
    from app.forge.core.presets_service import PresetService
    from app.forge.core.settings import ForgeSettingsService

log = logging.getLogger(__name__)

OVERLAP = N_("A watched folder can't be inside a library folder, or the other way round.")
NESTED = N_("A watched folder can't be inside another watched folder.")
_MOVING_PRESETS = frozenset({"Series (Folder)", "Movie (Folder)"})


def _parts(path: Path) -> tuple[str, ...]:
    return tuple(os.path.normcase(part).rstrip("\\/") for part in path.parts)


def inside(path: Path, folder: Path) -> bool:
    """Whether `path` is `folder` or anything below it."""
    mine, theirs = _parts(path), _parts(folder)
    return len(mine) >= len(theirs) and mine[: len(theirs)] == theirs


def library_folder(kind: MediaKind, settings: ForgeSettings) -> Path | None:
    """The library folder for `kind`, or None while it isn't chosen."""
    folder = settings.series_folder if kind is MediaKind.EPISODE else settings.movies_folder
    return Path(folder) if folder else None


def route_inputs(
    inputs: Sequence[PlanInput], options: PresetOptions, settings: ForgeSettings
) -> tuple[list[PlanInput], list[PlanInput]]:
    """Files added by hand under a preset's `options`: those to plan, each going to its library
    folder when the options say so (its naming rule then starts there instead of beside the file),
    and those left alone because that folder isn't chosen. What was dropped stays their
    `dropped_folder`: Clean junk and Empty folders look there, never in the library."""
    if not options.move_to_library:
        return list(inputs), []
    routed: list[PlanInput] = []
    left: list[PlanInput] = []
    for item in inputs:
        library = library_folder(item.media.kind, settings)
        if library is None:
            left.append(item)
        else:
            routed.append(item.model_copy(update={"root": library}))
    return routed, left


async def carry_move_option(settings: ForgeSettingsService, presets: PresetService) -> None:
    """At Forge's start, once: Series (Folder) and Movie (Folder) get Move to library folder; other
    presets keep working as they did. Marked as carried, so turning it off stays off. A failure
    is logged and tried at the next start; it never stops Forge."""
    if settings.current.move_carried:
        return
    try:
        for preset in await presets.list():
            if preset.name in _MOVING_PRESETS and not preset.options.move_to_library:
                options = preset.options.model_copy(update={"move_to_library": True})
                await presets.save(preset.model_copy(update={"options": options}))
        await settings.update(move_carried=True)
    except Exception as exc:  # noqa: BLE001 - forge.db: tried again at the next start
        log.warning("Couldn't set the presets' move option (%s)", type(exc).__name__)


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
