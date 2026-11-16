"""Which source is the library: what decides whether anything counts as had.

With a Plex token (`plex_connected`, app/services/plex.py) the library is Plex, as it has been
since 3.1.0, and the folder settings are not read. Without one, the folder library
(docs/folder-library/) is the library once the user switched it on ("Use these folders",
`folder_library_on`) with at least one film or series folder set. What decides is the folders
set up, not whether they can be reached right now: a NAS asleep at the start keeps its last
result, so the marks don't vanish and automatic downloads don't grab something twice.
Otherwise there is none, and the pages show no have / missing cues at all.

Both sources fill the same tables (`plex_titles`, `plex_episodes`, `plex_movies`), so every
have / missing query works unchanged; only the gate and the words differ ("In Plex" / "In your
library", `have_text`).
"""

from __future__ import annotations

from typing import Literal

from app.i18n import N_, t
from app.models import Settings
from app.services.plex import plex_connected

LibrarySource = Literal["plex", "folders"]

IN_PLEX_TEXT = N_("In Plex")
IN_LIBRARY_TEXT = N_("In your library")


def library_source(settings: Settings) -> LibrarySource | None:
    """The library's source: Plex with a token, else the folder library while switched on with
    a folder set, else None."""
    if plex_connected(settings):
        return "plex"
    if settings.folder_library_on and any(
        folder.strip() for folder in (*settings.film_folders, *settings.series_folders)
    ):
        return "folders"
    return None


def has_library(settings: Settings) -> bool:
    """There is a library (either source): only then do the pages show what you have and
    miss, and do automatic downloads skip what is there."""
    return library_source(settings) is not None


def have_text(settings: Settings) -> str:
    """What has a thing you have: "In your library" with the folder library, else "In Plex"
    (translated)."""
    return t(IN_LIBRARY_TEXT if library_source(settings) == "folders" else IN_PLEX_TEXT)
