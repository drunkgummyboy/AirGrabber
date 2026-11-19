"""The Settings window's pages (app/ui/settings_dialog.py; docs/settings/2026-11-19-settings-redesign.md
§2): grouped by what the user wants to do, not by which part of the app does it.

A page is its icon, its muted line and its parts, in order (`PAGES`); the sidebar lists the pages
under their groups (`GROUPS`: Overview above the groups, without a label). A part is one block of
boxes: AirGrabber's are built by the window when it opens (it knows each by its name); `FORGE`
marks where a page's MediaForge part goes (app/forge/ui/settings_pages.py's `FORGE_PARTS`), built
at the page's first visit, Forge started then. A page with nothing to show (its service not given)
is left out, and so is a group without a page.

A page's name (`PAGE_*`) is its key everywhere (the window's `section=`, `show_section`,
`go_to_spot`, the tests) and its shown name, through t(). Light: i18n only (AirGrabber's start
and Forge's openers import it)."""

from __future__ import annotations

from typing import NamedTuple

from app.i18n import N_

# -- the pages ------------------------------------------------------------------------------------

PAGE_OVERVIEW = N_("Overview")
PAGE_TORRENT = N_("Torrent program")
PAGE_QUALITY = N_("Quality & sources")
PAGE_AUTOMATIC = N_("Automatic")
PAGE_FOLDERS = N_("Folders")
PAGE_SORTING = N_("Sorting")
PAGE_NAMING = N_("Naming")
PAGE_SUBTITLES = N_("Subtitles")
PAGE_PLEX = N_("Plex & library")
PAGE_PLAYING = N_("Playing")
PAGE_PHONE = N_("Phone connection")  # app/ui/phone_settings.py: for the Companion app
PAGE_ACCOUNTS = N_("Accounts & keys")
PAGE_LOOKS = N_("Looks")
PAGE_PAGES = N_("Pages")
PAGE_WINDOWS = N_("Windows")
PAGE_UPDATES = N_("Updates & backup")

GROUP_DOWNLOADING = N_("Downloading")
GROUP_FILES = N_("Your files")
GROUP_WATCHING = N_("Watching")
GROUP_APP = N_("App")

# In a page's parts: where its MediaForge part goes.
FORGE = "forge"

# The Folders page's Series box (`go_to_spot`): MediaForge's "choose folders" opens it there.
FOLDERS_SPOT = "library-folders"


class PageSpec(NamedTuple):
    icon: str  # the sidebar's
    note: str  # the muted line under the page's heading, an N_() text
    parts: tuple[str, ...]  # its blocks of boxes, in order (AirGrabber's by name, or FORGE)


PAGES: dict[str, PageSpec] = {
    PAGE_OVERVIEW: PageSpec(
        "◎", N_("How AirGrabber is set up. Click a card to change it."), ("overview", "quick-setup")
    ),
    PAGE_TORRENT: PageSpec(
        "⬇", N_("What happens when you click Download."), ("download-choices",)
    ),
    PAGE_QUALITY: PageSpec(
        "★",
        N_("Which releases AirGrabber looks for, where, and which it skips."),
        ("quality", "sites", "skip"),
    ),
    PAGE_AUTOMATIC: PageSpec("⏱", N_("What AirGrabber downloads on its own."), ("automatic",)),
    PAGE_FOLDERS: PageSpec(
        "📁",
        N_("Set once: AirGrabber counts what's in them, and sorted files go into them."),
        ("folders", "finished", "count"),
    ),
    PAGE_SORTING: PageSpec(
        "⇅", N_("Finished downloads renamed and moved into your folders by themselves."), (FORGE,)
    ),
    PAGE_NAMING: PageSpec(
        "🏷", N_("How files and folders are named, and what comes along."), (FORGE,)
    ),
    PAGE_SUBTITLES: PageSpec(
        "💬", N_("Found on OpenSubtitles.com and saved next to your videos."), (FORGE,)
    ),
    PAGE_PLEX: PageSpec(
        "▶",
        N_("What you already have isn't shown as missing and isn't downloaded again."),
        ("plex", "folders-line", "library-compare"),
    ),
    PAGE_PLAYING: PageSpec("▷", N_("What plays when you click ▶ Play."), ("playing",)),
    PAGE_PHONE: PageSpec(
        "📱",
        N_(
            "The Companion app on your phone reaches AirGrabber over your home network, never "
            "from outside it."
        ),
        ("phone",),
    ),
    PAGE_ACCOUNTS: PageSpec(
        "🔑",
        N_("The services AirGrabber talks to. Keys and passwords stay on this PC."),
        ("tmdb", FORGE, "plex-account"),
    ),
    PAGE_LOOKS: PageSpec("◐", N_("How AirGrabber looks."), ("looks",)),
    PAGE_PAGES: PageSpec("▦", N_("Language and how the pages are laid out."), ("pages",)),
    PAGE_WINDOWS: PageSpec(
        "⧉", N_("How AirGrabber fits into Windows."), ("background", FORGE)
    ),
    PAGE_UPDATES: PageSpec(
        "⟳",
        N_("New versions, going back, and keeping your things safe."),
        ("version", "go-back", "backup", "report"),
    ),
}

# The sidebar: a small group label (upper case) above each group's pages; None: no label.
GROUPS: tuple[tuple[str | None, tuple[str, ...]], ...] = (
    (None, (PAGE_OVERVIEW,)),
    (GROUP_DOWNLOADING, (PAGE_TORRENT, PAGE_QUALITY, PAGE_AUTOMATIC)),
    (GROUP_FILES, (PAGE_FOLDERS, PAGE_SORTING, PAGE_NAMING, PAGE_SUBTITLES)),
    (GROUP_WATCHING, (PAGE_PLEX, PAGE_PLAYING, PAGE_PHONE)),
    (GROUP_APP, (PAGE_ACCOUNTS, PAGE_LOOKS, PAGE_PAGES, PAGE_WINDOWS, PAGE_UPDATES)),
)

# Every page in the sidebar's order.
PAGE_ORDER: tuple[str, ...] = tuple(page for _group, pages in GROUPS for page in pages)
