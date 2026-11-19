"""Folders, set once (docs/settings/2026-11-19-settings-redesign.md §4): the one-time carry of
MediaForge's Series / Movies folders into AirGrabber's folder lists.

Settings' Folders page (app/ui/folders_page.py) shows one list per kind, the folder library's
(`Settings.series_folders` / `film_folders`), with MediaForge's destination marked in it
(`ForgeSettings.series_folder` / `movies_folder`). At Forge's start, once (`folders_carried`),
a destination that isn't in its kind's list yet is added at the end of that list, so it shows
there: nothing is ever taken out of a list, the folder library's switch (`folder_library_on`)
stays as it was and no folder is read here. Both stores keep their fields: an older version
reads them as before. Logs give counts only, never a folder.
"""

from __future__ import annotations

import logging
import ntpath
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.forge.core.settings import ForgeSettingsService
    from app.services.settings import SettingsService

log = logging.getLogger(__name__)


def same_folder(a: str, b: str) -> bool:
    """Whether `a` and `b` are the same folder as Windows compares them (case, slashes, a
    trailing separator)."""
    # Twin of app/ui/folders_page.py's `same_folder` (AirGrabber's start may not import Forge):
    # change both together.
    return _key(a) == _key(b)


def _key(folder: str) -> str:
    return ntpath.normcase(ntpath.normpath(folder.strip())).rstrip("\\/")


async def carry_library_folders(
    forge_settings: ForgeSettingsService, airgrabber_settings: SettingsService
) -> bool:
    """At Forge's start, once: each set destination missing from AirGrabber's list of its kind
    is appended to that list. Marked as carried, so a folder taken out of a list later stays
    out. A failure is logged and tried at the next start; it never stops Forge. True when a
    folder was added (AirGrabber's start then has the folders checked soon, while they count:
    `ForgeServices.create`'s `folders_added`)."""
    forge = forge_settings.current
    if forge.folders_carried:
        return False
    added = False
    try:
        current = airgrabber_settings.current
        changes: dict[str, tuple[str, ...]] = {}
        for destination, field, listed in (
            (forge.series_folder, "series_folders", current.series_folders),
            (forge.movies_folder, "film_folders", current.film_folders),
        ):
            if not destination.strip() or any(same_folder(destination, f) for f in listed):
                continue
            changes[field] = (*listed, destination)
        if changes:
            await airgrabber_settings.update(**changes)
            added = True
            log.info("%d library folder(s) added to the folder lists", len(changes))
        await forge_settings.update(folders_carried=True)
    except Exception as exc:  # noqa: BLE001 - airgrabber.db / forge.db: tried at the next start
        log.warning("Couldn't carry over the library folders (%s)", type(exc).__name__)
    return added
