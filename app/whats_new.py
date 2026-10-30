"""What's new in each release: the short notes shown once after an update (MainWindow, with
app/ui/whats_new_dialog.py). Each release adds its entry to NOTES: 3–6 short lines in plain
words, each in N_() with its Dutch line in app/i18n_nl.py (the dialog shows them through t()).
Stdlib only."""

from __future__ import annotations

from collections.abc import Mapping
from typing import NamedTuple

from app import APP_NAME
from app.bootstrap import version_tuple
from app.i18n import N_, t

# Version -> its notes, one line each.
NOTES: dict[str, tuple[str, ...]] = {
    "2.0.6": (
        N_(
            "New installs come with qBittorrent: downloads start without a pop-up, and the "
            "Downloads page shows their progress."
        ),
        N_(
            "Already use qBittorrent? Set it up for AirGrabber in Settings → Where downloads go."
        ),
        N_(
            "qBittorrent gets AirGrabber's look (light or dark like Windows) when set up; switch "
            "it in Settings → Where downloads go."
        ),
        N_(
            "Click a series or film for its new info screen: a big picture, the trailer, the "
            "cast and the full story."
        ),
        N_("Settings → Looks shows a real card at the size you pick."),
        N_(
            "Quick setup chooses the look, download quality, card size and language in one go "
            "(Settings → Looks)."
        ),
    ),
    "2.0.5": (
        N_(
            "My shows can be sorted (A–Z, next episode, latest download) and filtered (missing "
            "episodes, Auto on, ended)."
        ),
        N_("A new Downloads page shows how far along your downloads are on your NAS."),
        N_(
            "The calendar marks the episodes you still need, and shows several episodes of one "
            "show on the same day as one card."
        ),
        N_("Filter Movies and Discover by genre."),
        N_("AirGrabber now speaks Dutch too, and there's a light theme."),
        N_("Shortcuts: Ctrl+F to search, F5 to refresh the page, Esc to close a window."),
    ),
}


class WhatsNew(NamedTuple):
    version: str
    lines: tuple[str, ...]


def whats_new(
    last_run: str, running: str, notes: Mapping[str, tuple[str, ...]] = NOTES
) -> WhatsNew | None:
    """The notes to show when AirGrabber starts on `running` after `last_run` ran: the newest
    entry above `last_run` and at most `running`. None on a first start (`last_run` empty: the
    welcome shows instead), when the version didn't go up, or without such an entry."""
    old, new = version_tuple(last_run), version_tuple(running)
    if not last_run.strip() or old is None or new is None or new <= old:
        return None
    best: tuple[tuple[int, ...], str] | None = None
    for version, lines in notes.items():
        key = version_tuple(version)
        if key is None or not lines or not old < key <= new:
            continue
        if best is None or key > best[0]:
            best = key, version
    return WhatsNew(best[1], tuple(notes[best[1]])) if best is not None else None


def dialog_title(version: str) -> str:
    """ "What's new in AirGrabber 2.0.5"."""
    return t("What's new in {app} {version}", app=APP_NAME, version=version)
