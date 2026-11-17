"""Which sources the library has: what decides whether anything counts as had.

Two shelves (docs/folder-library/2026-11-16-alongside-plex-design.md): Plex, with a token
(`plex_connected`, app/services/plex.py), and the user's own folders (docs/folder-library/)
once switched on ("Use these folders", `folder_library_on`) with at least one film or series
folder set (`folders_on`), with or without Plex. What decides is the folders set up, not whether
they can be reached right now: a NAS asleep at the start keeps its last result, so the marks
don't vanish and automatic downloads don't grab something twice. With neither there is no
library, and the pages show no have / missing cues at all.

Each source fills tables of its own (`plex_*` and `folder_*`, app/database.py) and neither
touches the other's; the have / missing queries count both. `library_source` is the main
source: Plex whenever there is a token, else the folders. The words follow it: "In Plex", or
part 1's "In your library" with the folders only (`folders_only`, `have_text`).
"""

from __future__ import annotations

from typing import Literal

from app.i18n import N_, t
from app.models import Settings
from app.services.plex import plex_connected

LibrarySource = Literal["plex", "folders"]

IN_PLEX_TEXT = N_("In Plex")
IN_LIBRARY_TEXT = N_("In your library")


def folders_on(settings: Settings) -> bool:
    """The user's own folders count: switched on with a folder set, with or without Plex."""
    return settings.folder_library_on and any(
        folder.strip() for folder in (*settings.film_folders, *settings.series_folders)
    )


def folders_only(settings: Settings) -> bool:
    """The folders are the only source: on (`folders_on`) and no Plex token."""
    return folders_on(settings) and not plex_connected(settings)


def library_source(settings: Settings) -> LibrarySource | None:
    """The library's main source: Plex with a token, else the folders while on (`folders_on`),
    else None."""
    if plex_connected(settings):
        return "plex"
    return "folders" if folders_on(settings) else None


def has_library(settings: Settings) -> bool:
    """There is a library (Plex, or the folders on): only then do the pages show what you have
    and miss, and do automatic downloads skip what is there."""
    return plex_connected(settings) or folders_on(settings)


def have_text(settings: Settings) -> str:
    """What has a thing you have: "In your library" with the folders only, else "In Plex"
    (translated)."""
    return t(IN_LIBRARY_TEXT if folders_only(settings) else IN_PLEX_TEXT)
