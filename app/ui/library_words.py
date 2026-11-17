"""The words for what you have, by the library's sources (app/services/library_source.py).

Plex users see exactly Plex's words ("✓ In Plex", "Plex doesn't have it yet"); with the folder
library as the only source the same marks say "In your library" (`folders`, part 1's words).
With both (two shelves, docs/folder-library/2026-11-16-alongside-plex-design.md) what Plex has
keeps "In Plex" and what only the folders have says "✓ Downloaded", the calendar's cue for what
is on the PC and not in Plex yet (`have_source`, `have_tip`). Texts about the whole library
(the gap finder, "Hide what I already have") take its words whenever the folders count
(`by_library`). The main window hands over its settings once (`follow`); a widget that says
what you have asks these when it shows, so after a switch of source (the pages reload then) it
has the new words. Without settings (before the window, and in tests that don't follow any)
Plex's words.

`reach_lines`: the folder library's lines on folders its last check couldn't read, said alike
by Settings' "Your own folders" box and the Library page.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from app.i18n import N_, t
from app.services.library_source import IN_LIBRARY_TEXT, folders_on, folders_only
from app.ui.missing_control import DOWNLOADED_TIP, PLEX_TIP

if TYPE_CHECKING:
    from app.models.folder_library import FolderLibraryStatus
    from app.services.settings import SettingsService

CANT_REACH_TEXT = N_("Can't reach {folder}: what was found there last time still counts.")
SLOW_TEXT = N_(
    "Reading {folder} takes a while: what was found there last time still counts until it's done."
)

# Where a thing you have is, in words: "plex" ("In Plex"), "downloaded" (beside Plex, only in
# the user's own folders: "Downloaded…"), "library" (the folders alone: "In your library").
HaveSource = Literal["plex", "downloaded", "library"]
HAVE_TIPS: dict[HaveSource, str] = {
    "plex": PLEX_TIP,
    "downloaded": DOWNLOADED_TIP,
    "library": IN_LIBRARY_TEXT,
}

_settings: SettingsService | None = None


def follow(settings: SettingsService | None) -> None:
    """Take the words from `settings` from now on (None: Plex's words again)."""
    global _settings
    _settings = settings


def folders() -> bool:
    """The folder library is the only source (`folders_only`: part 1's "In your library"
    words; else Plex's, also without a library and beside Plex)."""
    settings = _settings
    return settings is not None and folders_only(settings.current)


def by_source(plex: str, folders_text: str) -> str:
    """`folders_text` while the folder library is the only source, else `plex` (both as given,
    not translated: the callers translate, as with the constants)."""
    return folders_text if folders() else plex


def by_library(plex: str, library_text: str) -> str:
    """For a text about the whole library (what it lacks, what it has): `library_text` whenever
    the folders count (`folders_on`: alone, or beside Plex, the two together being "your
    library"), else `plex` (as given, not translated)."""
    settings = _settings
    return library_text if settings is not None and folders_on(settings.current) else plex


def have_source(*, from_folders: bool = False) -> HaveSource:
    """Where a thing you have is: "library" with the folders only; beside Plex "downloaded"
    for what only the folders have (`from_folders`), else "plex"."""
    if folders():
        return "library"
    return "downloaded" if from_folders else "plex"


def reach_lines(status: FolderLibraryStatus) -> list[str]:
    """One line per folder the folder library's last check couldn't read (translated): "Can't
    reach …", or for one that answered but wasn't read in time "Reading … takes a while"."""
    return [t(CANT_REACH_TEXT, folder=folder) for folder in status.unreachable] + [
        t(SLOW_TEXT, folder=folder) for folder in status.slow
    ]


def have_tip(source: HaveSource | None = None) -> str:
    """The quiet ✓'s tip, translated: "In Plex", "Downloaded. Plex doesn't have it yet." or "In
    your library"; without a `source` the main source's (`have_source()`)."""
    return t(HAVE_TIPS[source if source is not None else have_source()])
