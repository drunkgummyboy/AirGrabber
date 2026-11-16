"""The words for what you have, by the library's source (app/services/library_source.py).

Plex users see exactly Plex's words ("✓ In Plex", "Plex doesn't have it yet"); with the folder
library as the source the same marks say "In your library". The main window hands over its
settings once (`follow`); a widget that says what you have asks `by_source` (or `have_tip`)
when it shows, so after a switch of source (the pages reload then) it has the new words.
Without settings (before the window, and in tests that don't follow any) Plex's words.

`reach_lines`: the folder library's lines on folders its last check couldn't read, said alike
by Settings' "Your own folders" box and the Library page.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.i18n import N_, t
from app.services.library_source import IN_PLEX_TEXT, have_text, library_source

if TYPE_CHECKING:
    from app.models.folder_library import FolderLibraryStatus
    from app.services.settings import SettingsService

CANT_REACH_TEXT = N_("Can't reach {folder}: what was found there last time still counts.")
SLOW_TEXT = N_(
    "Reading {folder} takes a while: what was found there last time still counts until it's done."
)

_settings: SettingsService | None = None


def follow(settings: SettingsService | None) -> None:
    """Take the words from `settings` from now on (None: Plex's words again)."""
    global _settings
    _settings = settings


def folders() -> bool:
    """The folder library is the source (else Plex's words, also without a library)."""
    settings = _settings
    return settings is not None and library_source(settings.current) == "folders"


def by_source(plex: str, folders_text: str) -> str:
    """`folders_text` while the folder library is the source, else `plex` (both as given, not
    translated: the callers translate, as with the constants)."""
    return folders_text if folders() else plex


def reach_lines(status: FolderLibraryStatus) -> list[str]:
    """One line per folder the folder library's last check couldn't read (translated): "Can't
    reach …", or for one that answered but wasn't read in time "Reading … takes a while"."""
    return [t(CANT_REACH_TEXT, folder=folder) for folder in status.unreachable] + [
        t(SLOW_TEXT, folder=folder) for folder in status.slow
    ]


def have_tip() -> str:
    """The quiet ✓'s tip, translated: "In Plex", or "In your library" (`have_text`)."""
    settings = _settings
    return t(IN_PLEX_TEXT) if settings is None else have_text(settings.current)
