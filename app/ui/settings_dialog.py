"""Preferences dialog in plain words, the one Settings window of AirGrabber and MediaForge: a
sidebar of sections (Downloading: where downloads go, quality, automatic, Plex, phone connection;
MediaForge: FORGE_SECTIONS of app/forge/ui/settings_pages.py; App: movie & show info, looks,
updates, advanced) and one scrolling page per section, its settings grouped in boxes that each
say what they are for (the layout is app/ui/settings_layout.py's). Every change goes through
SettingsService, which validates first.

`master`: the main window it opens over and belongs to (transient, placed over it). MainWindow
keeps the one open dialog and closes it when the window hides.

MediaForge's pages (`forge=`, docs/forge/INTEGRATION.md 3.7) wear AirGrabber's look like the rest
and are built on their first visit: nothing of Forge starts before one is shown. One Save: the
built sections' `problem()` (`parts`) with the dialog's own checks before anything is stored, then
AirGrabber's settings, then each section's `save()` in the sidebar's order."""

from __future__ import annotations

import asyncio
import logging
import tkinter as tk
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from tkinter import filedialog
from pathlib import Path
from typing import TYPE_CHECKING, Any, NamedTuple, Protocol

import customtkinter as ctk
from pydantic import ValidationError

from app.i18n import N_, Language, capitalized, current_language, day_name, number, t
from app.indexers import (
    ApibayIndexer,
    BitsearchIndexer,
    EztvIndexer,
    IndexerError,
    ProwlarrIndexer,
    YtsIndexer,
)
from app.models import (
    Accent,
    CardSize,
    DownloadMode,
    CalendarLayout,
    InfoLayout,
    ProwlarrStatus,
    QualityProfile,
    Settings,
    ThemeMode,
    UpdateInfo,
    Weekday,
)
from app.services import (
    DiscoveryService,
    ImageCache,
    LibraryService,
    ProwlarrSetupService,
    RemoteDownloads,
    SettingsService,
    TorrentAppService,
    TorrentSearchService,
    UpdateError,
    UpdateService,
)
from app.services import startup
from app.services.qbittorrent_local import set_up_by_airgrabber
from app.services.startup import StartupError
from app.models.backup import BackupLoadResult
from app.models.settings import AUTO_WAIT_CHOICES, STUCK_CHOICES, TextSize
from app.services.backup import BackupService
from app.services.phone.service import PhoneService
from app.services.plex import PlexService
from app.services.tmdb import TMDBError
from app.ui import theme
from app.ui.accent_picker import AccentSwatches
from app.ui.borderless import make_borderless
from app.ui.branding import set_window_icon
from app.ui.backup_dialog import LoadBackupDialog, SaveBackupDialog, load_backup
from app.ui.calendar_layout_picture import CalendarLayoutPicture
from app.ui.card_preview import CardSizePreview
from app.ui.card_scale import CARD_SIZE_LABEL, CARD_SIZE_LABELS, CARD_SIZE_TIP, size_from_label
from app.ui.confirm_dialog import ConfirmDialog
from app.ui.dialog_kit import (
    BUTTON_HEIGHT,
    BUTTON_RADIUS,
    DETAIL_SIZE,
    PAD_X,
    SMALL_SIZE,
    activate_later,
    place_over,
    primary_button,
    secondary_button,
    text_label,
)
from app.ui.phone_settings import PhoneBox
from app.ui.plex_compare import PlexCompareBox
from app.ui.plex_settings import PlexBox
from app.ui.prowlarr_panel import PANEL_PAD_X, ProwlarrPanel, shows_remote_fields
from app.ui.quick_setup import OPEN_TEXT as QUICK_SETUP_TEXT
from app.ui.quick_setup import QuickSetupDialog
from app.ui.remote_program_form import FIELD_LABELS as REMOTE_LABELS
from app.ui.remote_program_form import (
    FILMS_HINT,
    FILMS_LABEL,
    FolderNote,
    RemoteProgramForm,
    folder_note,
)
from app.ui.settings_layout import (
    BOX_GAP,
    BOX_INNER,
    MAX_PAGE_HEIGHT,
    PAGE_BOTTOM,
    PAGE_PAD_LEFT,
    PAGE_WIDTH,
    TEXT_SIZE,
    SettingsBox,
    SettingsPage,
    build_layout,
    fill_sidebar,
    fit_window,
    page_height,
)
from app.ui.settings_layout import SCREEN_RESERVE as SCREEN_RESERVE
from app.ui.settings_layout import dialog_height as dialog_height
from app.ui.settings_layout import sidebar_width as _sidebar_width
from app.ui.settings_search import Match, Place, SettingsSearch, places
from app.ui.tasks import async_command, spawn
from app.ui.tmdb_key_guide import KeyCheck, TmdbKeyDialog, key_check_message
from app.ui.torrent_app_status import INSTALL_BUTTON_WIDTH, TorrentAppStatus, TorrentStatus

if TYPE_CHECKING:
    from app.forge.runtime import ForgeRuntime
    from app.forge.ui.settings_pages import ForgeSection, ForgeSectionPart

log = logging.getLogger(__name__)

# -- sections ----------------------------------------------------------------------------------
# A section's name is its key here and in the tests; the dialog shows it through t(), as every
# text constant below.

SECTION_WHERE = N_("Where downloads go")
SECTION_QUALITY = N_("Quality")
SECTION_AUTOMATIC = N_("Automatic")
SECTION_INFO = N_("Movie & show info")
SECTION_LOOKS = N_("Looks")
SECTION_UPDATES = N_("Updates")
SECTION_ADVANCED = N_("Advanced")
SECTION_BACKUP = N_("Backup")
SECTION_PLEX = N_("Plex")  # app/ui/plex_settings.py: what is in Plex counts as had
SECTION_PHONE = N_("Phone connection")  # app/ui/phone_settings.py: for the Companion app

# The sidebar: a small group label above each part of the list, then its sections in order.
# MediaForge's group goes between these two (the dialog adds it when it is given `forge`).
SECTION_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        N_("Downloading"),
        (SECTION_WHERE, SECTION_QUALITY, SECTION_AUTOMATIC, SECTION_PLEX, SECTION_PHONE),
    ),
    (
        N_("App"),
        (SECTION_INFO, SECTION_LOOKS, SECTION_UPDATES, SECTION_BACKUP, SECTION_ADVANCED),
    ),
)
SECTIONS: tuple[str, ...] = tuple(name for _group, names in SECTION_GROUPS for name in names)
SECTION_ICONS: dict[str, str] = {
    SECTION_WHERE: "⬇",
    SECTION_QUALITY: "🎚",
    SECTION_AUTOMATIC: "⏱",
    SECTION_PLEX: "▶",
    SECTION_PHONE: "📱",
    SECTION_INFO: "🎬",
    SECTION_LOOKS: "🖼",
    SECTION_UPDATES: "⟳",
    SECTION_BACKUP: "💾",
    SECTION_ADVANCED: "🛠",
}
# Each page's one muted line under its heading.
SECTION_NOTES: dict[str, str] = {
    SECTION_WHERE: N_("What happens when you click Download."),
    SECTION_QUALITY: N_(
        "Which release AirGrabber picks when you click Download, and for automatic downloads."
    ),
    SECTION_AUTOMATIC: N_("What AirGrabber does on its own, without you clicking anything."),
    SECTION_PLEX: N_(
        "What's in your Plex libraries counts as something you already have: it isn't missing "
        "and isn't downloaded again."
    ),
    SECTION_PHONE: N_(
        "The Companion app on your phone reaches AirGrabber over your home network, never from "
        "outside it."
    ),
    SECTION_INFO: N_(
        "The Movies and Discover pages get their lists, posters and release dates from TMDB, "
        "a free film database."
    ),
    SECTION_LOOKS: N_("How AirGrabber's pages look."),
    SECTION_UPDATES: N_("Getting new versions of AirGrabber."),
    SECTION_BACKUP: N_(
        "Keep a copy of your shows, watchlist and settings, or move them to another PC."
    ),
    SECTION_ADVANCED: N_(
        "Where AirGrabber searches, and which releases it skips. Most people never need to "
        "change these."
    ),
}

# -- texts ---------------------------------------------------------------------------------------

APP_CHOICE = N_("Open in my torrent program on this PC")
APP_TEXT = N_("Each download opens in your torrent program by itself. Best for most people.")
REMOTE_CHOICE = N_("Send to a torrent program on my NAS or another computer")
REMOTE_TEXT = N_("For a NAS or home server (Synology, qBittorrent, Transmission).")
# Under REMOTE_TEXT while the program is the qBittorrent on this PC that AirGrabber set up.
LOCAL_QBITTORRENT_TEXT = N_("qBittorrent on this PC (set up by AirGrabber)")
FOLDER_CHOICE = N_("Save .torrent files to a folder")
FOLDER_TEXT = N_("For a watch folder, for example on a NAS.")

SERIES_TITLE = N_("Series")
SERIES_TEXT = N_("For one episode. Season packs ignore the size.")
MOVIES_TITLE = N_("Movies")
MOVIES_TEXT = N_("For one film.")
QUALITY_NOTE = N_(
    "If nothing matches, AirGrabber takes the closest match: the right quality first, then the "
    "size nearest your range. Leave a size empty for no limit."
)

BACKGROUND_TITLE = N_("In the background")
BACKGROUND_TEXT = N_("Needed for new episodes to download while the window is closed.")
TRAY_TEXT = N_("Keep running in the tray when I close the window")
START_TEXT = N_("Start AirGrabber when Windows starts")
NEW_SHOWS_TITLE = N_("New shows")
NEW_SHOWS_TEXT = N_("You can still change this for each show.")
NEW_SHOWS_SWITCH = N_("Download new episodes for shows I start following")
WAIT_TITLE = N_("Wait after an episode airs")
WAIT_TEXT = N_(
    "Automatic downloads wait this long, so good releases can turn up. The first ones are more "
    "often fakes."
)
WAIT_HOURS = N_("{n} h")
STUCK_TITLE = N_("Swap a stuck download after")
STUCK_TEXT = N_(
    "When a download hasn't moved for this long, AirGrabber removes it from your torrent program "
    "and fetches the next best release."
)
STUCK_NEVER = N_("Never")

FOLDER_LABEL = N_("Where finished downloads end up")
FOLDER_HINT = N_(
    "Your torrent program's download folder. MediaForge watches it, and the Downloads page looks "
    "there for free space and your files."
)

TMDB_KEY_LABEL = N_("TMDB key")
TMDB_KEY_TEXT = N_(
    "A free code from themoviedb.org that lets AirGrabber ask TMDB for this information. "
    "Without it, the Movies and Discover pages stay empty. Your shows and the Calendar work "
    "without it."
)
KEY_GUIDE_LINK = N_("How do I get a free key?")
SOURCES_LABEL = N_("Where this information comes from")
SOURCES_TEXT = N_(
    "Show and film information and pictures come from TVmaze and TMDB. This product uses the "
    "TMDB API but is not endorsed or certified by TMDB."
)

CALENDAR_LAYOUT_TITLE = N_("Calendar view")
CALENDAR_LAYOUT_TEXT = N_(
    "Weeks: a grid of whole weeks, you choose how many on the Calendar. Rolling week: one row with "
    "today in the middle, three days back and three ahead."
)
CALENDAR_LAYOUT_NAMES: dict[CalendarLayout, str] = {
    CalendarLayout.WEEKS: N_("Weeks"),
    CalendarLayout.ROLLING: N_("Rolling week"),
}
WEEK_TITLE = N_("Week starts on")
WEEK_TEXT = N_("The first day of each week on the Calendar.")

QUICK_SETUP_NOTE = N_("Look, download quality, card size and language, each with one click.")

THEME_TITLE = N_("Theme")
THEME_TEXT = N_("Light or dark. Follow Windows uses the same look as your other Windows apps.")
ACCENT_TITLE = N_("Accent colour")
ACCENT_TEXT = N_("The colour of buttons, chosen items and links.")
ACCENT_RESTART_TEXT = N_("Applies after a restart")
TEXT_SIZE_TITLE = N_("Text size")
TEXT_SIZE_TEXT = N_("Every text in AirGrabber a little larger, apart from Windows' own scaling.")
TEXT_SIZE_NAMES = {
    TextSize.NORMAL: N_("Normal"),
    TextSize.LARGE: N_("Large"),
    TextSize.LARGER: N_("Larger"),
}
INFO_LAYOUT_TITLE = N_("Info window layout")
INFO_LAYOUT_TEXT = N_("Where a film's or series' info window shows its releases.")
SPOTLIGHT_TITLE = N_("Spotlight")
SPOTLIGHT_SWITCH = N_("Move the spotlight to the next title by itself")

LANGUAGE_TITLE = N_("Language")
LANGUAGE_TEXT = N_("The language of AirGrabber's pages, buttons and messages.")
RESTART_TEXT = N_("Restart AirGrabber to switch the language")

AUTO_UPDATE_TITLE = N_("Automatic updates")
AUTO_UPDATE_TEXT = N_(
    "New versions download in the background and are installed when AirGrabber closes."
)
AUTO_UPDATE_SWITCH = N_("Install updates automatically")
CHECK_UPDATES_TEXT = N_("Check whether a newer version is out.")
GO_BACK_TITLE = N_("Not happy with this version?")
GO_BACK_TEXT = N_(
    "AirGrabber keeps the version from before its last update. "
    "If you go back, it waits for a version newer than this one."
)
GO_BACK_WARNING = N_(
    "AirGrabber restarts on the older version. Your library and settings stay, unless this version "
    "changed how they are stored: then they return to how they were just before the update."
)

SITES_TITLE = N_("Built-in sites")
SITES_TEXT = N_("Websites AirGrabber searches by itself.")
EXCLUDED_TITLE = N_("Skip releases with these words")
EXCLUDED_TEXT = N_("Separate with commas, for example: cam, telesync")
PREFERRED_TITLE = N_("Prefer releases from")
PREFERRED_TEXT = N_("Release group names, separated with commas")
BLOCKED_TITLE = N_("Never use releases from")
BLOCKED_TEXT = N_(
    "Release group names, separated with commas. \"Wrong or fake?\" in a download's ⋯ menu adds "
    "one."
)
LEARNED_LINE = N_("Learned from the downloads you kept: {groups}")

BACKUP_TEXT = N_(
    "One file with your followed shows, film watchlist, download history and settings. Load "
    "it on this PC or another one: you see what's in it before anything changes."
)
SAVE_BACKUP_BUTTON = N_("Save a backup…")
LOAD_BACKUP_BUTTON = N_("Load a backup…")
_BACKUP_BUTTON_WIDTH = 190

# -- sizes (logical, unscaled CTk units) ---------------------------------------------------------

# The sidebar, the pages and their boxes: app/ui/settings_layout.py; the names this dialog uses
# below.
_PAGE_WIDTH = PAGE_WIDTH
_PAGE_PAD_LEFT = PAGE_PAD_LEFT
_PAGE_BOTTOM = PAGE_BOTTOM
_SPOT_TRIES = 100  # looks for a spot of a page still getting ready, this many times
_SPOT_WAIT_S = 0.05
_BOX_GAP = BOX_GAP
_TEXT_SIZE = TEXT_SIZE
_BOX_INNER = BOX_INNER
_INDENT = 30  # under a radio button's text
_TEST_BUTTON_WIDTH = 160
_UPDATE_BUTTON_WIDTH = 180
_GO_BACK_BUTTON_WIDTH = 240
_CHECK_BUTTON_WIDTH = 110
_RESTART_BUTTON_WIDTH = 150
_SIZE_ENTRY_WIDTH = 70
_QUALITY_MENU_WIDTH = 230
_REMOTE_LABEL_WIDTH = 130
# The NAS / other computer form under its radio button (or the magnet note), with some slack.
_REMOTE_FIELD_WIDTH = _BOX_INNER - _INDENT - _REMOTE_LABEL_WIDTH - 10
_REMOTE_GAP = 12  # above that form
_PANEL_INSET = PANEL_PAD_X + 1  # the Prowlarr panel's border and padding
_PANEL_LABEL_WIDTH = 120
_PANEL_FIELD_WIDTH = _PAGE_WIDTH - 2 * _PANEL_INSET - _PANEL_LABEL_WIDTH

# (stable id, label) of the websites AirGrabber searches itself, in the order the search runs
# them. Prowlarr (searched last) has its own panel.
_BUILT_IN_SITES = [
    (cls.name, cls.label) for cls in (ApibayIndexer, EztvIndexer, BitsearchIndexer, YtsIndexer)
]
_PROWLARR_FIELDS = frozenset({"prowlarr_url", "prowlarr_api_key"})

# Plain names for the quality profiles, in menu order. The stored values stay QualityProfile's.
QUALITY_NAMES: dict[QualityProfile, str] = {
    QualityProfile.FULL_HD: N_("Full HD (1080p)"),
    QualityProfile.HD: N_("HD (720p)"),
    QualityProfile.UHD: N_("4K (2160p)"),
    QualityProfile.HEVC: N_("Smaller files (1080p x265)"),
    QualityProfile.ANY: N_("Any quality"),
}


def quality_name(profile: QualityProfile) -> str:
    return t(QUALITY_NAMES[profile])


def quality_from_name(name: str) -> QualityProfile:
    for profile in QUALITY_NAMES:
        if quality_name(profile) == name:
            return profile
    raise ValueError(f"unknown quality name: {name!r}")


# Plain names for the themes, in the order the Theme box shows them.
THEME_NAMES: dict[ThemeMode, str] = {
    ThemeMode.SYSTEM: N_("Follow Windows"),
    ThemeMode.DARK: N_("Dark"),
    ThemeMode.LIGHT: N_("Light"),
}


def theme_name(mode: ThemeMode) -> str:
    return t(THEME_NAMES[mode])


def theme_from_name(name: str) -> ThemeMode:
    for mode in THEME_NAMES:
        if theme_name(mode) == name:
            return mode
    raise ValueError(f"unknown theme name: {name!r}")


# Settings → Looks → Info window layout, in the order the box shows them.
INFO_LAYOUT_NAMES: dict[InfoLayout, str] = {
    InfoLayout.SIDE: N_("Side by side"),
    InfoLayout.TABS: N_("Tabs"),
}


def accent_needs_restart(chosen: Accent, running: Accent) -> bool:
    """Whether the Accent colour box shows "Applies after a restart": the colours are set once
    at startup (theme.apply_accent)."""
    return chosen is not running


def choice_name[E: StrEnum](names: Mapping[E, str], value: E) -> str:
    """A choice's shown name (translated)."""
    return t(names[value])


def choice_from_name[E: StrEnum](names: Mapping[E, str], name: str, default: E) -> E:
    """The choice a shown name stands for; `default` for an unknown one."""
    for value, text in names.items():
        if t(text) == name:
            return value
    return default


def weekday_name(day: Weekday) -> str:
    """The Week starts on menu's name for `day`: "Monday" (its stored value) / "Maandag"."""
    return capitalized(day_name(day.weekday))


def weekday_from_name(name: str) -> Weekday:
    for day in Weekday:
        if weekday_name(day) == name:
            return day
    raise ValueError(f"unknown weekday name: {name!r}")


def language_from_name(name: str) -> Language:
    """The Language box's choice ("Nederlands") → its Language; names are never translated."""
    for language in Language:
        if language.native_name == name:
            return language
    raise ValueError(f"unknown language name: {name!r}")


def needs_restart(chosen: Language, running: Language) -> bool:
    """Whether the Language box shows "Restart AirGrabber to switch the language": the language
    takes effect at the next start."""
    return chosen is not running


def size_text(value: float | None) -> str:
    """A stored size limit as its entry shows it: "" for no limit, "2.5", "4" (not "4.0"); with a
    decimal comma in Dutch ("2,5", which Save reads back)."""
    return "" if value is None else number(value, "g")


class DownloadRows(NamedTuple):
    """Which extra rows "Where downloads go" shows under its three choices."""

    torrent_status: bool  # the torrent-program status line (+ Install qBittorrent)
    remote_form: bool  # the NAS / other computer form (program, address, account, Test)
    folder: bool  # the folder entry + Browse, and the magnet note with its link


def download_rows(mode: DownloadMode) -> DownloadRows:
    return DownloadRows(
        torrent_status=mode is DownloadMode.APP,
        remote_form=mode is DownloadMode.REMOTE,
        folder=mode is DownloadMode.FOLDER,
    )


def download_settings(settings: Settings) -> tuple[Any, ...]:
    """The stored download choice and remote program, for noticing a change made elsewhere."""
    return (
        settings.download_mode,
        settings.remote_kind,
        settings.remote_url,
        settings.remote_username,
        settings.remote_password,  # SecretStr compares the passwords themselves
        settings.remote_destination,
        settings.movie_remote_destination,
        settings.remote_verify_tls,
    )


def download_dir_to_save(
    mode: DownloadMode, entry: str, stored: Path | None
) -> str | Path | None:
    """A folder entry (the watch folder, the films folder) only counts when it shows ("Save
    .torrent files to a folder"); otherwise the stored folder stays, whatever the hidden entry
    holds."""
    return entry.strip() if mode is DownloadMode.FOLDER else stored


def disabled_indexers(
    sites: Mapping[str, bool], *, use_prowlarr: bool, unknown: Sequence[str] = ()
) -> tuple[str, ...]:
    """What Settings.disabled_indexers stores: the unticked built-in sites (`sites`: id -> box
    ticked, in search order), Prowlarr when "Use Prowlarr" is off, then the ids this version
    doesn't know (`unknown`, kept as they were)."""
    off = [name for name, ticked in sites.items() if not ticked]
    if not use_prowlarr:
        off.append(ProwlarrIndexer.name)
    return (*off, *unknown)


class StartupShortcut(Protocol):
    """Start with Windows (app/services/startup.py); tests inject a fake."""

    def is_enabled(self) -> bool: ...

    async def set_enabled(self, on: bool) -> None: ...


class WindowsStartup:
    """The real Startup-folder shortcut; `startup_dir` overrides the folder (scratch runs)."""

    def __init__(self, startup_dir: Path | None = None) -> None:
        self._startup_dir = startup_dir

    def is_enabled(self) -> bool:
        return startup.is_enabled(startup_dir=self._startup_dir)

    async def set_enabled(self, on: bool) -> None:
        await startup.set_enabled(on, startup_dir=self._startup_dir)


class StartupResult(NamedTuple):
    enabled: bool  # the shortcut's real state afterwards: what the setting stores
    error: str | None  # for the dialog's error line


async def apply_start_with_windows(
    wanted: bool, shown: bool, shortcut: StartupShortcut
) -> StartupResult:
    """Save's part for "Start AirGrabber when Windows starts": add or remove the shortcut only
    when the box changed from `shown` (the real state when the dialog opened)."""
    error: str | None = None
    if wanted != shown:
        try:
            await shortcut.set_enabled(wanted)
        except StartupError as exc:
            error = str(exc)
    return StartupResult(enabled=shortcut.is_enabled(), error=error)


# Shown through t() (`_describe`).
_FIELD_LABELS = {
    "download_mode": SECTION_WHERE,
    "download_dir": FOLDER_CHOICE,
    "movie_download_dir": FILMS_LABEL,
    "quality_tv": N_("Series quality"),
    "quality_movie": N_("Movies quality"),
    "size_tv_min_gb": N_("Series size"),
    "size_tv_max_gb": N_("Series size"),
    "size_movie_min_gb": N_("Movies size"),
    "size_movie_max_gb": N_("Movies size"),
    "first_day": WEEK_TITLE,
    "calendar_layout": CALENDAR_LAYOUT_TITLE,
    "card_size": CARD_SIZE_LABEL,
    "theme_mode": THEME_TITLE,
    "accent": ACCENT_TITLE,
    "text_size": TEXT_SIZE_TITLE,
    "info_layout": INFO_LAYOUT_TITLE,
    "spotlight_motion": SPOTLIGHT_TITLE,
    "language": LANGUAGE_TITLE,
    "tmdb_api_key": TMDB_KEY_LABEL,
    "auto_update": SECTION_UPDATES,
    "disabled_indexers": SITES_TITLE,
    "excluded_keywords": EXCLUDED_TITLE,
    "preferred_groups": PREFERRED_TITLE,
    "blocked_groups": BLOCKED_TITLE,
    "prowlarr_url": N_("Prowlarr URL"),
    "prowlarr_api_key": N_("Prowlarr API key"),
    "subtitles_folder": FOLDER_LABEL,
    **REMOTE_LABELS,
}

# Which section shows each setting; anything else (the remote_* program form too) lives on
# "Where downloads go".
_FIELD_SECTIONS: dict[str, str] = {
    "quality_tv": SECTION_QUALITY,
    "quality_movie": SECTION_QUALITY,
    "size_tv_min_gb": SECTION_QUALITY,
    "size_tv_max_gb": SECTION_QUALITY,
    "size_movie_min_gb": SECTION_QUALITY,
    "size_movie_max_gb": SECTION_QUALITY,
    "keep_in_tray": SECTION_AUTOMATIC,
    "start_with_windows": SECTION_AUTOMATIC,
    "auto_download_new_shows": SECTION_AUTOMATIC,
    "auto_wait_hours": SECTION_AUTOMATIC,
    "stuck_swap_hours": SECTION_AUTOMATIC,
    "tmdb_api_key": SECTION_INFO,
    "card_size": SECTION_LOOKS,
    "first_day": SECTION_LOOKS,
    "calendar_layout": SECTION_LOOKS,
    "theme_mode": SECTION_LOOKS,
    "accent": SECTION_LOOKS,
    "text_size": SECTION_LOOKS,
    "info_layout": SECTION_LOOKS,
    "spotlight_motion": SECTION_LOOKS,
    "language": SECTION_LOOKS,
    "auto_update": SECTION_UPDATES,
    "disabled_indexers": SECTION_ADVANCED,
    "excluded_keywords": SECTION_ADVANCED,
    "preferred_groups": SECTION_ADVANCED,
    "blocked_groups": SECTION_ADVANCED,
    "prowlarr_url": SECTION_ADVANCED,
    "prowlarr_api_key": SECTION_ADVANCED,
}


def stuck_name(hours: int) -> str:
    """"Never" for 0, else "6 h"."""
    return t(STUCK_NEVER) if hours <= 0 else t(WAIT_HOURS, n=hours)


def section_for_field(name: str) -> str:
    """The section a setting is on (where Save's validation error takes the user)."""
    return _FIELD_SECTIONS.get(name, SECTION_WHERE)


def _describe(error: ValidationError) -> str:
    lines = []
    for item in error.errors():
        label = _FIELD_LABELS.get(str(item["loc"][0])) if item["loc"] else None
        message = item["msg"].removeprefix("Value error, ")
        lines.append(f"{t(label)}: {message}" if label else message)
    return "\n".join(lines)


def _error_fields(error: ValidationError) -> list[str]:
    return [str(item["loc"][0]) for item in error.errors() if item["loc"]]


def _prowlarr_error(error: ValidationError) -> bool:
    return any(name in _PROWLARR_FIELDS for name in _error_fields(error))


def _remote_error(error: ValidationError) -> bool:
    return any(name in REMOTE_LABELS for name in _error_fields(error))


def _first_section(error: ValidationError) -> str:
    """The section with the first problem."""
    errors = error.errors()
    if not errors or not errors[0]["loc"]:
        return SECTION_WHERE
    return section_for_field(str(errors[0]["loc"][0]))


# -- layout pieces ---------------------------------------------------------------------------------

# The shared layout's page and box (app/ui/settings_layout.py), by the names this dialog uses.
_Box = SettingsBox
_Page = SettingsPage


def sidebar_width(others: Sequence[str] = ()) -> int:
    """The sidebar's width (logical): room for the longest section name in bold (the selected
    one, `others`: MediaForge's), so the pages don't shift when another section is chosen."""
    return _sidebar_width((*SECTIONS, *others))


def _paint_background(widget: ctk.CTkFrame, color: theme.Color) -> None:
    """Give a transparent CTk frame that sits inside a box but belongs to the page (the remote
    form, the Prowlarr panel) the box's colour; its transparent children follow."""
    widget.configure(fg_color=color)


# -- the dialog --------------------------------------------------------------------------------


class SettingsDialog(ctk.CTkToplevel):
    def __init__(
        self,
        master: Any,
        settings: SettingsService,
        *,
        search: TorrentSearchService,
        # The torrent program on a NAS or another computer (Services.remote; the keyword keeps
        # its older name, which main_window.py passes).
        station: RemoteDownloads,
        updates: UpdateService,
        torrent_app: TorrentAppService,
        discovery: DiscoveryService,
        prowlarr: ProwlarrSetupService,
        update_info: UpdateInfo | None = None,
        on_update: Callable[[], None] | None = None,
        on_go_back: Callable[[], None] | None = None,
        startup_shortcut: StartupShortcut | None = None,
        on_restart: Callable[[], None] | None = None,
        images: ImageCache | None = None,
        library: LibraryService | None = None,
        backup: BackupService | None = None,
        section: str | None = None,
        plex: PlexService | None = None,
        phone: PhoneService | None = None,
        forge: ForgeRuntime | None = None,
    ) -> None:
        """`on_restart`: restarts
        AirGrabber (the Language box's "Restart now"; without it only the note shows).
        `images`: the Card size box's preview card (none without it); `library`: a followed
        show's poster for it (the placeholder card without one). `backup`: the Backup section
        shows when it is given. `forge`: the MediaForge group shows when it is given. `section`:
        the page it opens at (Where downloads go when not given or not shown)."""
        super().__init__(master, fg_color=theme.BG)
        self._forge = forge
        self.parts: dict[str, ForgeSectionPart] = {}  # the Forge sections built so far
        # Imported here: AirGrabber's start doesn't load Forge's pages (INTEGRATION.md 3.6).
        self._forge_sections: Mapping[str, ForgeSection] = {}
        groups: list[tuple[str, Sequence[str]]] = list(SECTION_GROUPS)
        if forge is not None:
            from app.forge.ui.settings_pages import FORGE_GROUP, FORGE_SECTIONS

            self._forge_sections = FORGE_SECTIONS
            groups.insert(1, (FORGE_GROUP, tuple(FORGE_SECTIONS)))
        self._backup = backup
        self._plex = plex  # the Plex section shows when it is given
        self.plex_box: PlexBox | None = None
        self.plex_compare: PlexCompareBox | None = None
        self._phone = phone  # the Phone connection section shows when it is given and available
        self.phone_box: PhoneBox | None = None
        self._backup_dialog: SaveBackupDialog | LoadBackupDialog | None = None
        self._images = images
        self._library = library
        self._card_preview: CardSizePreview | None = None
        self._quick_dialog: QuickSetupDialog | None = None
        self._preview_task: asyncio.Task[None] | None = None
        self._startup = startup_shortcut if startup_shortcut is not None else WindowsStartup()
        self._settings = settings
        self._search = search
        self._remote = station
        self._updates = updates
        self._torrent_app = torrent_app
        self._discovery = discovery
        self._prowlarr = prowlarr
        self._on_update = on_update
        self._on_go_back = on_go_back
        self._on_restart = on_restart
        self._tests: dict[str, asyncio.Task[None]] = {}
        # Variable traces this dialog added, removed in destroy(): a trace's callback is held by
        # Tk, out of the garbage collector's sight, and would keep the closed dialog in memory.
        self._traces: list[tuple[tk.Variable, str]] = []
        self._key_dialog: TmdbKeyDialog | None = None
        self._form_in_folder = False  # the remote form opened under the magnet note
        current = settings.current

        self.title(t("Settings"))
        set_window_icon(self)
        self.resizable(False, False)
        self.transient(master)

        self._mode = ctk.StringVar(value=current.download_mode.value)
        self._quality_tv = ctk.StringVar(value=quality_name(current.quality_tv))
        self._quality_movie = ctk.StringVar(value=quality_name(current.quality_movie))
        self._size_tv_min = ctk.StringVar(value=size_text(current.size_tv_min_gb))
        self._size_tv_max = ctk.StringVar(value=size_text(current.size_tv_max_gb))
        self._size_movie_min = ctk.StringVar(value=size_text(current.size_movie_min_gb))
        self._size_movie_max = ctk.StringVar(value=size_text(current.size_movie_max_gb))
        self._first_day = ctk.StringVar(value=weekday_name(current.first_day))
        self._calendar_layout = ctk.StringVar(
            value=choice_name(CALENDAR_LAYOUT_NAMES, current.calendar_layout)
        )
        self._card_size = ctk.StringVar(value=CARD_SIZE_LABELS[current.card_size])
        self._theme_mode = ctk.StringVar(value=theme_name(current.theme_mode))
        self._accent_picker: AccentSwatches | None = None  # Looks → Accent colour
        self._info_layout = ctk.StringVar(
            value=choice_name(INFO_LAYOUT_NAMES, current.info_layout)
        )
        self._spotlight_motion = ctk.BooleanVar(value=current.spotlight_motion)
        self._text_size = ctk.StringVar(value=t(TEXT_SIZE_NAMES[current.text_size]))
        self._language = ctk.StringVar(value=current.language.native_name)
        self._download_dir = ctk.StringVar(value=str(current.download_dir))
        self._tmdb_key = ctk.StringVar(value=current.tmdb_api_key.get_secret_value())
        self._excluded = ctk.StringVar(value=", ".join(current.excluded_keywords))
        self._preferred = ctk.StringVar(value=", ".join(current.preferred_groups))
        self._blocked = ctk.StringVar(value=", ".join(current.blocked_groups))
        disabled = set(current.disabled_indexers)
        self._sources = {
            name: ctk.BooleanVar(value=name not in disabled) for name, _label in _BUILT_IN_SITES
        }
        self._use_prowlarr = ctk.BooleanVar(value=ProwlarrIndexer.name not in disabled)
        known = {name for name, _label in _BUILT_IN_SITES} | {ProwlarrIndexer.name}
        self._unknown_disabled = tuple(n for n in current.disabled_indexers if n not in known)
        self._prowlarr_key = ctk.StringVar(value=current.prowlarr_api_key.get_secret_value())
        # The Prowlarr URL and key as last shown (see _sync_prowlarr_fields).
        self._prowlarr_shown = (current.prowlarr_url, current.prowlarr_api_key.get_secret_value())
        self._auto_update = ctk.BooleanVar(value=current.auto_update)
        self._keep_in_tray = ctk.BooleanVar(value=current.keep_in_tray)
        # The real shortcut, not the setting: it may have been removed by hand.
        self._startup_shown = self._startup.is_enabled()
        self._start_with_windows = ctk.BooleanVar(value=self._startup_shown)
        self._auto_new_shows = ctk.BooleanVar(value=current.auto_download_new_shows)
        self._auto_wait = ctk.StringVar(value=t(WAIT_HOURS, n=current.auto_wait_hours))
        self._stuck_swap = ctk.StringVar(value=stuck_name(current.stuck_swap_hours))
        self._finished_folder = ctk.StringVar(value=current.subtitles_folder)

        layout = build_layout(self, sidebar_width(tuple(self._forge_sections)))
        body = layout.body
        self._sidebar = layout.sidebar
        self._sidebar_scroll = layout.sidebar_scroll
        self._scroll = layout.scroll

        # Save / Cancel under the page, with the error line above them.
        footer = ctk.CTkFrame(body, fg_color="transparent")
        footer.grid(row=1, column=2, sticky="ew", padx=(_PAGE_PAD_LEFT, PAD_X), pady=(10, 20))
        self._error = ctk.CTkLabel(
            footer,
            text="",
            font=theme.font(14),
            text_color=theme.DANGER,
            justify="left",
            anchor="w",
            wraplength=_PAGE_WIDTH,
        )
        self._error.pack(anchor="w")
        buttons = ctk.CTkFrame(footer, fg_color="transparent")
        buttons.pack(anchor="e", pady=(6, 0))
        self.cancel_button = secondary_button(buttons, t("Cancel"), self.cancel)
        self.cancel_button.pack(side="right")
        self._save_button = primary_button(buttons, t("Save"), async_command(self._save))
        self._save_button.pack(side="right", padx=(0, 12))

        self._pages: dict[str, _Page] = {}
        builders: dict[str, Callable[[], _Page]] = {
            SECTION_WHERE: self._build_where,
            SECTION_QUALITY: self._build_quality,
            SECTION_AUTOMATIC: self._build_automatic,
            SECTION_INFO: self._build_info,
            SECTION_LOOKS: self._build_looks,
            SECTION_UPDATES: self._build_updates,
            SECTION_ADVANCED: self._build_advanced,
        }
        if self._backup is not None:
            builders[SECTION_BACKUP] = self._build_backup
        if self._plex is not None:
            plex_service = self._plex
            builders[SECTION_PLEX] = lambda: self._build_plex(plex_service)
        if self._phone is not None and self._phone.available:
            phone_service = self._phone
            builders[SECTION_PHONE] = lambda: self._build_phone(phone_service)
        for name in SECTIONS:
            if name in builders:
                self._pages[name] = builders[name]()
        shown = [
            (group, [n for n in names if n in self._pages or n in self._forge_sections])
            for group, names in groups
        ]
        icons = {
            **SECTION_ICONS,
            **{name: part.icon for name, part in self._forge_sections.items()},
        }
        self.search_box = SettingsSearch(
            self._sidebar, look_up=self._search_places, on_pick=self._go_to_match
        )
        self.search_box.pack(fill="x", padx=(6, 6), pady=(0, 12))
        self._items = fill_sidebar(self._sidebar, shown, icons, self._show_section)
        self._section = section if section in self._items else SECTION_WHERE
        self._show_section(self._section)
        if update_info is not None:
            self._show_update(update_info)

        self.protocol("WM_DELETE_WINDOW", self.cancel)
        self.bind("<Escape>", lambda _event: self.cancel())
        self.borderless = make_borderless(
            self, drag_widgets=(self, body, self._sidebar), on_close=self.cancel
        )
        self._fit_size()
        place_over(self, master)
        activate_later(self)

    # -- sidebar and pages ---------------------------------------------------------------------

    @property
    def section(self) -> str:
        """The section whose page shows."""
        return self._section

    @property
    def sections(self) -> list[str]:
        """The sidebar's sections, in order."""
        return list(self._items)

    def page(self, section: str) -> ctk.CTkFrame:
        """A section's page frame (tests look inside it)."""
        return self._pages[section].frame

    def show_section(self, section: str) -> None:
        """Show `section`'s page (an open dialog asked for another one); a section that isn't
        shown changes nothing."""
        self._show_section(section)

    def _search_places(self) -> list[Place]:
        """What Settings' search looks through now: the built pages' boxes, and the MediaForge
        pages not built yet by their words."""
        from app.forge.ui.settings_pages import FORGE_SEARCH

        built = {name: page.boxes for name, page in self._pages.items()}
        unbuilt = {
            name: FORGE_SEARCH.get(name, ())
            for name in self._forge_sections
            if name not in self._pages
        }
        return places(built, unbuilt)

    def _go_to_match(self, match: Match) -> None:
        """A search match: its page, scrolled to its box (a MediaForge page's box once built)."""
        self._show_section(match.section)
        if match.title:
            spawn(self._scroll_to_box(match.section, match.title), name="settings-search")

    async def _scroll_to_box(self, section: str, title: str) -> None:
        for _ in range(_SPOT_TRIES):
            if not self.winfo_exists() or self._section != section:
                return
            page = self._pages.get(section)
            frame = next((f for name, f in page.boxes if name == title), None) if page else None
            if frame is not None and frame.winfo_ismapped():
                self._scroll_to_frame(frame)
                return
            await asyncio.sleep(_SPOT_WAIT_S)

    def _scroll_to_frame(self, frame: tk.Misc) -> None:
        canvas = self._scroll._parent_canvas
        canvas.update_idletasks()
        box = canvas.bbox("all")
        if box and box[3] > 0:
            canvas.yview_moveto(max(0, frame.winfo_rooty() - self._scroll.winfo_rooty()) / box[3])

    def go_to_spot(self, section: str, spot: str) -> None:
        """Show `section`'s page scrolled to its box `spot` (a part's `spots`), once that shows:
        a MediaForge page may still be getting ready."""
        self._show_section(section)
        spawn(self._scroll_to_spot(section, spot), name="settings-spot")

    async def _scroll_to_spot(self, section: str, spot: str) -> None:
        for _ in range(_SPOT_TRIES):
            if not self.winfo_exists() or self._section != section:
                return
            spots: dict[str, tk.Misc] = getattr(self.parts.get(section), "spots", {})
            frame = spots.get(spot)
            if frame is not None and frame.winfo_ismapped():
                canvas = self._scroll._parent_canvas
                canvas.update_idletasks()
                box = canvas.bbox("all")
                if box and box[3] > 0:
                    canvas.yview_moveto(max(0, frame.winfo_rooty() - self._scroll.winfo_rooty()) / box[3])
                return
            await asyncio.sleep(_SPOT_WAIT_S)

    def _show_section(self, section: str) -> None:
        if section in self._forge_sections and section not in self._pages:
            self._build_forge_section(section)
        if section not in self._pages:
            return
        self._section = section
        self._grid_page(section)
        self._scroll._parent_canvas.yview_moveto(0)  # each page starts at its top
        for name, item in self._items.items():
            item.set_selected(name == section)

    def _grid_page(self, section: str) -> None:
        for name, page in self._pages.items():
            if name != section:
                page.frame.grid_remove()
        self._pages[section].frame.grid(row=0, column=0, sticky="ew", pady=(0, _PAGE_BOTTOM))

    def _new_page(self, section: str, note: str | None = None) -> _Page:
        shown = note if note is not None else SECTION_NOTES[section]
        return _Page(self._scroll, t(section), t(shown))

    def _build_forge_section(self, section: str) -> None:
        """A MediaForge page, at its first visit (it starts Forge if nothing has yet)."""
        assert self._forge is not None
        forge_section = self._forge_sections[section]
        page = self._new_page(section, forge_section.note)
        self._pages[section] = page
        self.parts[section] = forge_section.build(page, self._forge)

    def _built_parts(self) -> list[tuple[str, ForgeSectionPart]]:
        """The Forge sections built so far, in the sidebar's order: what Save asks and stores."""
        return [(name, self.parts[name]) for name in self._forge_sections if name in self.parts]

    # -- Where downloads go ------------------------------------------------------------------

    def _build_where(self) -> _Page:
        page = self._new_page(SECTION_WHERE)

        app = page.box()
        app.add(self._radio(app.body, t(APP_CHOICE), DownloadMode.APP), gap=0, sticky="w")
        app.add(app.text(t(APP_TEXT), indent=_INDENT), gap=2, sticky="w", padx=_INDENT)
        self._torrent = TorrentAppStatus(
            app.body,
            torrent_app=self._torrent_app,
            wraplength=_BOX_INNER - _INDENT - INSTALL_BUTTON_WIDTH - 16,
            reinstall=True,
        )
        self._torrent_row = app.take_row()

        remote = page.box()
        remote.add(
            self._radio(remote.body, t(REMOTE_CHOICE), DownloadMode.REMOTE), gap=0, sticky="w"
        )
        remote.add(remote.text(t(REMOTE_TEXT), indent=_INDENT), gap=2, sticky="w", padx=_INDENT)
        self._local_line = remote.text(t(LOCAL_QBITTORRENT_TEXT), indent=_INDENT)
        self._local_row = remote.take_row()
        # That qBittorrent's status, its look and "Reinstall qBittorrent…", while it is chosen.
        self._local_torrent = TorrentAppStatus(
            remote.body,
            torrent_app=self._torrent_app,
            wraplength=_BOX_INNER - _INDENT - INSTALL_BUTTON_WIDTH - 16,
            reinstall=True,
        )
        self._local_torrent_row = remote.take_row()
        self._remote_box = remote
        self._remote_row = remote.take_row()

        folder = page.box()
        folder.add(
            self._radio(folder.body, t(FOLDER_CHOICE), DownloadMode.FOLDER), gap=0, sticky="w"
        )
        folder.add(folder.text(t(FOLDER_TEXT), indent=_INDENT), gap=2, sticky="w", padx=_INDENT)
        self._folder_box = folder
        self._folder_row = ctk.CTkFrame(folder.body, fg_color="transparent")
        self._entry(self._folder_row, self._download_dir).pack(side="left", fill="x", expand=True)
        secondary_button(self._folder_row, t("Browse"), self._browse, width=110).pack(
            side="left", padx=(10, 0)
        )
        self._folder_row_index = folder.take_row()
        # Films' own folder under it (empty: films go to the folder above too).
        self._films_label = folder.text(t(FILMS_LABEL), indent=_INDENT)
        self._films_label_row = folder.take_row()
        self._films_row = ctk.CTkFrame(folder.body, fg_color="transparent")
        films = self._settings.current.movie_download_dir
        self._movie_dir = self._hinted_entry(
            self._films_row, str(films) if films is not None else "", t(FILMS_HINT)
        )
        self._movie_dir.pack_configure(side="left", expand=True)
        secondary_button(self._films_row, t("Browse"), self._browse_films, width=110).pack(
            side="left", padx=(10, 0)
        )
        self._films_row_index = folder.take_row()
        self._folder_note = folder.text("", indent=_INDENT)
        self._folder_note_row = folder.take_row()
        self._folder_link = ctk.CTkLabel(
            folder.body,
            text="",
            font=theme.font(_TEXT_SIZE, underline=True),
            text_color=theme.LINK,
            cursor="hand2",
            anchor="w",
        )
        self._folder_link.bind("<Button-1>", lambda _event: self._reveal_remote_form())
        self._folder_link_row = folder.take_row()
        self._folder_form_row = folder.take_row()

        # Where the torrent program's finished downloads land, whichever way they go there:
        # MediaForge's watching, the free space and "Open the folder" read it.
        finished = page.box(t(FOLDER_LABEL), t(FOLDER_HINT))
        line = finished.frame_row()
        self._entry(line, self._finished_folder).pack(side="left", fill="x", expand=True)
        secondary_button(line, t("Choose…"), self._choose_finished_folder, width=110).pack(
            side="left", padx=(10, 0)
        )

        # One form, shown in the remote box or (folder mode) under the magnet note: it belongs to
        # the page and is placed into either box, painted in the boxes' colour.
        self._remote_form = RemoteProgramForm(
            page.frame,
            settings=self._settings.current,
            run_test=self._test_remote,
            label_width=_REMOTE_LABEL_WIDTH,
            field_width=_REMOTE_FIELD_WIDTH,
            describe=_describe,
            films=True,
        )
        self._remote_form.configure(corner_radius=0)
        _paint_background(self._remote_form, theme.TAB_BG)
        self._show_local_line(self._settings.current)
        # qBittorrent's setup button can point AirGrabber at qBittorrent's Web UI while this
        # dialog is open: the choice and the form then show that, so Save doesn't undo it.
        self._download_shown = download_settings(self._settings.current)
        self._storing = False
        self._stop_listening = self._settings.subscribe(self._settings_changed)
        return page

    def _show_local_line(self, settings: Settings) -> None:
        if set_up_by_airgrabber(settings):
            self._local_line.grid(
                row=self._local_row, column=0, sticky="w", padx=(_INDENT, 0), pady=(2, 0)
            )
        else:
            self._local_line.grid_remove()

    def _settings_changed(self, updated: Settings) -> None:
        """Settings stored elsewhere: show a changed download choice and remote program."""
        if not self.winfo_exists():
            return
        self._show_local_line(updated)
        if not set_up_by_airgrabber(updated):
            self._local_torrent.grid_remove()
        shown = download_settings(updated)
        if self._storing or shown == self._download_shown:
            return
        self._download_shown = shown
        self._mode.set(updated.download_mode.value)
        self._form_in_folder = False
        self._remote_form.load(updated)
        self._show_download_rows()

    def _mode_changed(self) -> None:
        self._form_in_folder = False
        self._show_download_rows()

    def _show_download_rows(self) -> None:
        rows = download_rows(DownloadMode(self._mode.get()))
        self._grid_download_rows(rows, form_in_folder=self._form_in_folder)
        if rows.torrent_status:
            self._torrent.refresh()  # again each time: a program may have been installed
        if rows.remote_form and set_up_by_airgrabber(self._settings.current):
            self._local_torrent.grid(
                row=self._local_torrent_row,
                column=0,
                sticky="ew",
                padx=(_INDENT, 0),
                pady=(10, 0),
            )
            self._local_torrent.refresh()
        else:
            self._local_torrent.grid_remove()

    def _current_note(self) -> FolderNote:
        # The remote form is hidden in FOLDER mode (until opened), so what it holds is what
        # Save stores.
        form = self._remote_form
        return folder_note(form.kind, form.ready, form.address)

    def _grid_download_rows(self, rows: DownloadRows, *, form_in_folder: bool) -> None:
        if rows.torrent_status:
            self._torrent.grid(
                row=self._torrent_row, column=0, sticky="ew", padx=(_INDENT, 0), pady=(10, 0)
            )
        else:
            self._torrent.grid_remove()
        if rows.remote_form:
            self._place_remote_form(self._remote_box, self._remote_row)
        elif rows.folder and form_in_folder:
            self._place_remote_form(self._folder_box, self._folder_form_row)
        else:
            self._remote_form.grid_remove()
        if rows.folder:
            note = self._current_note()
            self._folder_row.grid(
                row=self._folder_row_index, column=0, sticky="ew", padx=(_INDENT, 0), pady=(12, 0)
            )
            self._films_label.grid(
                row=self._films_label_row, column=0, sticky="w", padx=(_INDENT, 0), pady=(10, 0)
            )
            self._films_row.grid(
                row=self._films_row_index, column=0, sticky="ew", padx=(_INDENT, 0), pady=(4, 0)
            )
            self._folder_note.configure(text=note.text)
            self._folder_note.grid(
                row=self._folder_note_row, column=0, sticky="w", padx=(_INDENT, 0), pady=(10, 0)
            )
            if form_in_folder:
                self._folder_link.grid_remove()
            else:
                self._folder_link.configure(text=note.link)
                self._folder_link.grid(
                    row=self._folder_link_row, column=0, sticky="w", padx=(_INDENT, 0), pady=(2, 0)
                )
        else:
            self._folder_row.grid_remove()
            self._films_label.grid_remove()
            self._films_row.grid_remove()
            self._folder_note.grid_remove()
            self._folder_link.grid_remove()

    def _choose_finished_folder(self) -> None:
        # Native modal dialog: the event loop (and background tasks) pause until it closes.
        chosen = filedialog.askdirectory(
            parent=self, initialdir=self._finished_folder.get() or None
        )
        if chosen:
            self._finished_folder.set(str(Path(chosen)))  # Windows separators, UNC kept

    def _place_remote_form(self, box: _Box, row: int) -> None:
        self._remote_form.grid(
            in_=box.body,
            row=row,
            column=0,
            sticky="ew",
            padx=(_INDENT, 0),
            pady=(_REMOTE_GAP, 0),
        )
        self._remote_form.lift()  # above the box it is placed in

    def _reveal_remote_form(self) -> None:
        """The magnet note's link: open the remote form under it, staying in folder mode."""
        self._form_in_folder = True
        self._show_download_rows()

    def _refresh_folder_note(self) -> None:
        if self.winfo_exists() and DownloadMode(self._mode.get()) is DownloadMode.FOLDER:
            self._folder_note.configure(text=self._current_note().text)

    def _browse(self) -> None:
        # Native modal dialog: the event loop (and background tasks) pause until it closes.
        chosen = filedialog.askdirectory(parent=self, initialdir=self._download_dir.get() or None)
        if chosen:
            self._download_dir.set(chosen)

    def _browse_films(self) -> None:
        # Native modal dialog, as `_browse`; it starts in the films folder, else the one above.
        start = self._movie_dir.get() or self._download_dir.get()
        chosen = filedialog.askdirectory(parent=self, initialdir=start or None)
        if chosen:
            self._movie_dir.delete(0, "end")
            self._movie_dir.insert(0, chosen)

    # -- Quality -------------------------------------------------------------------------------

    def _build_quality(self) -> _Page:
        page = self._new_page(SECTION_QUALITY)
        for title, text, quality, low, high in (
            (SERIES_TITLE, SERIES_TEXT, self._quality_tv, self._size_tv_min, self._size_tv_max),
            (
                MOVIES_TITLE,
                MOVIES_TEXT,
                self._quality_movie,
                self._size_movie_min,
                self._size_movie_max,
            ),
        ):
            box = page.box(t(title), t(text))
            line = box.frame_row()
            names = [quality_name(profile) for profile in QUALITY_NAMES]
            self._option_menu(line, quality, names, _QUALITY_MENU_WIDTH)
            self._word(line, t("between")).pack(side="left", padx=(16, 8))
            self._entry(line, low, width=_SIZE_ENTRY_WIDTH).pack(side="left")
            self._word(line, t("and")).pack(side="left", padx=8)
            self._entry(line, high, width=_SIZE_ENTRY_WIDTH).pack(side="left")
            self._word(line, "GB").pack(side="left", padx=(8, 0))
        page.add(
            text_label(
                page.frame,
                t(QUALITY_NOTE),
                size=_TEXT_SIZE,
                color=theme.TEXT_MUTED,
                wraplength=_PAGE_WIDTH,
            ),
            gap=_BOX_GAP,
        )
        return page

    # -- Automatic -----------------------------------------------------------------------------

    def _build_automatic(self) -> _Page:
        page = self._new_page(SECTION_AUTOMATIC)
        background = page.box(t(BACKGROUND_TITLE), t(BACKGROUND_TEXT))
        background.add(
            self._checkbox(background.body, t(TRAY_TEXT), self._keep_in_tray), sticky="w"
        )
        background.add(
            self._checkbox(background.body, t(START_TEXT), self._start_with_windows),
            gap=8,
            sticky="w",
        )
        shows = page.box(t(NEW_SHOWS_TITLE), t(NEW_SHOWS_TEXT))
        shows.add(
            self._checkbox(shows.body, t(NEW_SHOWS_SWITCH), self._auto_new_shows), sticky="w"
        )
        wait = page.box(t(WAIT_TITLE), t(WAIT_TEXT))
        hours = [t(WAIT_HOURS, n=n) for n in AUTO_WAIT_CHOICES]
        wait.add(self._segmented(wait.body, hours, self._auto_wait, width=320), sticky="w")
        stuck = page.box(t(STUCK_TITLE), t(STUCK_TEXT))
        names = [stuck_name(n) for n in STUCK_CHOICES]
        stuck.add(self._segmented(stuck.body, names, self._stuck_swap, width=340), sticky="w")
        return page

    def _stuck_hours(self) -> int:
        """The swap time chosen in Automatic's row (0: never)."""
        for n in STUCK_CHOICES:
            if self._stuck_swap.get() == stuck_name(n):
                return n
        return self._settings.current.stuck_swap_hours

    def _wait_hours(self) -> int:
        """The wait chosen in Automatic's row of hours."""
        for n in AUTO_WAIT_CHOICES:
            if self._auto_wait.get() == t(WAIT_HOURS, n=n):
                return n
        return self._settings.current.auto_wait_hours

    # -- Movie & show info ---------------------------------------------------------------------

    def _build_info(self) -> _Page:
        page = self._new_page(SECTION_INFO)
        box = page.box(t(TMDB_KEY_LABEL), t(TMDB_KEY_TEXT))
        line = box.frame_row()
        self._tmdb_entry = self._entry(line, self._tmdb_key, show="•")
        self._tmdb_entry.pack(side="left", fill="x", expand=True)
        self._tmdb_entry.bind("<Return>", lambda _event: self._check_tmdb_key())
        self._tmdb_check = primary_button(
            line, t("Check"), self._check_tmdb_key, width=_CHECK_BUTTON_WIDTH
        )
        self._tmdb_check.pack(side="left", padx=(10, 0))
        result = box.frame_row(gap=8)
        self._tmdb_result = text_label(result, "", size=_TEXT_SIZE, wraplength=_BOX_INNER - 200)
        self._tmdb_dot = text_label(result, "·", size=_TEXT_SIZE, color=theme.TEXT_MUTED)
        self._tmdb_guide = ctk.CTkLabel(
            result,
            text=t(KEY_GUIDE_LINK),
            font=theme.font(_TEXT_SIZE, underline=True),
            text_color=theme.LINK,
            cursor="hand2",
        )
        self._tmdb_guide.bind("<Button-1>", lambda _event: self._open_key_guide())
        self._tmdb_guide.pack(side="left")
        self._trace(self._tmdb_key, self._on_tmdb_key_edited)
        page.box(t(SOURCES_LABEL), t(SOURCES_TEXT))
        return page

    def _check_tmdb_key(self) -> None:
        key = self._tmdb_key.get().strip()
        if not key:
            self._tmdb_entry.focus_set()
            return
        running = self._tests.get("tmdb")
        if running is not None and not running.done():
            return
        self._show_key_check(KeyCheck.CHECKING)
        self._tmdb_check.configure(state="disabled")
        self._tests["tmdb"] = spawn(self._run_tmdb_check(key), name="settings-tmdb-check")

    async def _run_tmdb_check(self, key: str) -> None:
        try:
            works = await self._discovery.check_tmdb_key(key)
            result = KeyCheck.OK if works else KeyCheck.REJECTED
        except TMDBError as exc:  # never the key or the URL: only what kind of failure
            log.info("TMDB key check failed: %s", type(exc).__name__)
            result = KeyCheck.UNREACHABLE
        if self.winfo_exists():
            self._tmdb_check.configure(state="normal")
            self._show_key_check(result)

    def _show_key_check(self, result: KeyCheck | None) -> None:
        """The result line before the guide link ("✓ The key works · How do I get…")."""
        self._tmdb_result.pack_forget()
        self._tmdb_dot.pack_forget()
        if result is None:
            return
        text, color = key_check_message(result)
        self._tmdb_result.configure(text=text, text_color=color)
        self._tmdb_result.pack(side="left", before=self._tmdb_guide)
        self._tmdb_dot.pack(side="left", padx=8, before=self._tmdb_guide)

    def _on_tmdb_key_edited(self, *_args: object) -> None:
        """A changed key needs a new check: forget the last result, stop a check in flight."""
        running = self._tests.get("tmdb")
        if running is not None and not running.done():
            running.cancel()
            self._tmdb_check.configure(state="normal")
        self._show_key_check(None)

    def _open_key_guide(self) -> None:
        if self._key_dialog is not None and self._key_dialog.winfo_exists():
            self._key_dialog.lift()
            self._key_dialog.focus_force()
            return
        self._key_dialog = TmdbKeyDialog(
            self,
            settings=self._settings,
            discovery=self._discovery,
            on_saved=self._key_saved,
        )

    def _key_saved(self, key: str) -> None:
        if self.winfo_exists():
            self._tmdb_key.set(key)
            self._show_key_check(KeyCheck.OK)  # the guide only saves a key TMDB accepted

    # -- Looks ---------------------------------------------------------------------------------

    def _build_looks(self) -> _Page:
        page = self._new_page(SECTION_LOOKS)
        # The most important choices of every section on one page (also the welcome's last step).
        quick = page.box(t(QUICK_SETUP_TEXT), t(QUICK_SETUP_NOTE))
        line = quick.frame_row()
        secondary_button(line, t(QUICK_SETUP_TEXT), self._open_quick_setup, width=180).pack(
            side="left"
        )
        self._build_language(page)
        looks = page.box(t(THEME_TITLE), t(THEME_TEXT))
        themes = [theme_name(mode) for mode in THEME_NAMES]
        looks.add(self._segmented(looks.body, themes, self._theme_mode, width=360), sticky="w")
        self._build_accent(page)
        self._build_text_size(page)
        cards = page.box(t(CARD_SIZE_LABEL), t(CARD_SIZE_TIP))
        cards.add(
            self._segmented(
                cards.body, [CARD_SIZE_LABELS[size] for size in CardSize], self._card_size
            ),
            sticky="w",
        )
        if self._images is not None:
            # A real card at the chosen size, and the saved size's outline beside it.
            saved = self._settings.current.card_size
            self._card_preview = CardSizePreview(
                cards.body, images=self._images, chosen=saved, saved=saved
            )
            cards.add(self._card_preview, sticky="w")
            self._trace(self._card_size, lambda *_args: self._card_size_changed())
            if self._library is not None:
                self._preview_task = spawn(self._preview_show(self._library), name="card-preview")
        layout = page.box(t(INFO_LAYOUT_TITLE), t(INFO_LAYOUT_TEXT))
        layouts = [choice_name(INFO_LAYOUT_NAMES, value) for value in INFO_LAYOUT_NAMES]
        layout.add(self._segmented(layout.body, layouts, self._info_layout), sticky="w")
        spotlight = page.box(t(SPOTLIGHT_TITLE))
        spotlight.add(
            ctk.CTkSwitch(
                spotlight.body,
                text=t(SPOTLIGHT_SWITCH),
                variable=self._spotlight_motion,
                height=BUTTON_HEIGHT,
                font=theme.font(DETAIL_SIZE),
                progress_color=theme.ACCENT,
            ),
            sticky="w",
        )
        calendar = page.box(t(CALENDAR_LAYOUT_TITLE), t(CALENDAR_LAYOUT_TEXT))
        row = calendar.frame_row()
        names = [t(name) for name in CALENDAR_LAYOUT_NAMES.values()]
        self._segmented(row, names, self._calendar_layout).pack(side="left")
        self._layout_picture = CalendarLayoutPicture(row, self._chosen_calendar_layout())
        self._layout_picture.pack(side="left", padx=(16, 0))
        self._trace(
            self._calendar_layout,
            lambda *_args: self._layout_picture.show(self._chosen_calendar_layout()),
        )
        week = page.box(t(WEEK_TITLE), t(WEEK_TEXT))
        line = week.frame_row()
        self._option_menu(line, self._first_day, [weekday_name(day) for day in Weekday])
        return page

    def _chosen_calendar_layout(self) -> CalendarLayout:
        return choice_from_name(
            CALENDAR_LAYOUT_NAMES, self._calendar_layout.get(), CalendarLayout.WEEKS
        )

    def _build_text_size(self, page: _Page) -> None:
        """Normal / Large / Larger, with "Applies after a restart" while another than this run's
        is chosen."""
        box = page.box(t(TEXT_SIZE_TITLE), t(TEXT_SIZE_TEXT))
        names = [t(name) for name in TEXT_SIZE_NAMES.values()]
        box.add(
            self._segmented(
                box.body, names, self._text_size, width=300,
                command=lambda _name: self._text_size_changed(),
            ),
            sticky="w",
        )
        self._text_size_restart = box.text(t(ACCENT_RESTART_TEXT))
        self._text_size_restart.configure(text_color=theme.WARNING)
        self._text_size_row = box.take_row()
        self._text_size_changed()

    @property
    def text_size(self) -> TextSize:
        """The text size the Text size box shows chosen."""
        chosen = self._text_size.get()
        for size, name in TEXT_SIZE_NAMES.items():
            if chosen == t(name):
                return size
        return self._settings.current.text_size

    def _text_size_changed(self) -> None:
        label = getattr(self, "_text_size_restart", None)
        if label is None:
            return
        if self.text_size.value != theme.current_text_size():
            label.grid()
        else:
            label.grid_remove()

    def _build_accent(self, page: _Page) -> None:
        """The Accent colour box: a swatch per palette, the chosen one ringed. Another colour
        than the one running shows "Applies after a restart" with Restart now (Save also stores
        it: it applies at the next start)."""
        box = page.box(t(ACCENT_TITLE), t(ACCENT_TEXT))
        self._accent_picker = AccentSwatches(
            box.body,
            value=self._settings.current.accent,
            background=theme.TAB_BG,
            command=lambda _accent: self._accent_changed(),
        )
        box.add(self._accent_picker, sticky="w")
        self._accent_restart_row = box.frame_row()
        text_label(
            self._accent_restart_row,
            t(ACCENT_RESTART_TEXT),
            size=_TEXT_SIZE,
            color=theme.WARNING,
            wraplength=_BOX_INNER - _RESTART_BUTTON_WIDTH - 12,
        ).pack(side="left", fill="x", expand=True)
        if self._on_restart is not None:
            primary_button(
                self._accent_restart_row,
                t("Restart now"),
                async_command(self._restart_now),
                width=_RESTART_BUTTON_WIDTH,
            ).pack(side="right", padx=(12, 0))
        self._accent_changed()

    @property
    def accent(self) -> Accent:
        """The accent colour the Accent colour box shows chosen."""
        if self._accent_picker is None:
            return self._settings.current.accent
        return self._accent_picker.value

    def _accent_changed(self) -> None:
        if accent_needs_restart(self.accent, theme.current_accent()):
            self._accent_restart_row.grid()
        else:
            self._accent_restart_row.grid_remove()

    def _card_size_changed(self) -> None:
        if self._card_preview is None:
            return
        try:
            chosen = size_from_label(self._card_size.get())
        except ValueError:
            return
        self._card_preview.show(chosen, self._settings.current.card_size)

    async def _preview_show(self, library: LibraryService) -> None:
        """The first followed show with a poster for the preview card (its poster is most
        likely in the poster cache already)."""
        entries = await library.tracked(profile_id=self._settings.current.current_profile)
        show = next((entry.show for entry in entries if entry.show.poster_url), None)
        if show is not None and self._card_preview is not None and self.winfo_exists():
            self._card_preview.use_show(show)

    # -- Quick setup ---------------------------------------------------------------------------

    def _open_quick_setup(self) -> None:
        """Quick setup on its own over Settings; what it saves shows here at once, so Save
        keeps it."""
        if self._quick_dialog is not None and self._quick_dialog.winfo_exists():
            self._quick_dialog.lift()
            return
        restart = async_command(self._restart_now) if self._on_restart is not None else None
        self._quick_dialog = QuickSetupDialog(
            self, settings=self._settings, on_saved=self._quick_saved, on_restart=restart
        )

    def _quick_saved(self) -> None:
        """Show what Quick setup saved (look, accent colour, quality, card size, language)."""
        if not self.winfo_exists():
            return
        current = self._settings.current
        self._theme_mode.set(theme_name(current.theme_mode))
        if self._accent_picker is not None:
            self._accent_picker.set(current.accent)
            self._accent_changed()
        self._quality_tv.set(quality_name(current.quality_tv))
        self._quality_movie.set(quality_name(current.quality_movie))
        self._card_size.set(CARD_SIZE_LABELS[current.card_size])
        self._language.set(current.language.native_name)
        self._language_changed()
        if self._card_preview is not None:
            self._card_preview.show(current.card_size, current.card_size)

    def _build_language(self, page: _Page) -> None:
        """The Language box: English / Nederlands, each in its own language. Another language
        than the one running shows "Restart AirGrabber to switch the language" with Restart now
        (Save also stores it: it applies at the next start)."""
        box = page.box(t(LANGUAGE_TITLE), t(LANGUAGE_TEXT))
        names = [language.native_name for language in Language]
        box.add(
            self._segmented(
                box.body, names, self._language, width=260, command=self._language_changed
            ),
            sticky="w",
        )
        self._restart_row = box.frame_row()
        text_label(
            self._restart_row,
            t(RESTART_TEXT),
            size=_TEXT_SIZE,
            color=theme.WARNING,
            wraplength=_BOX_INNER - _RESTART_BUTTON_WIDTH - 12,
        ).pack(side="left", fill="x", expand=True)
        if self._on_restart is not None:
            self._restart_button = primary_button(
                self._restart_row,
                t("Restart now"),
                async_command(self._restart_now),
                width=_RESTART_BUTTON_WIDTH,
            )
            self._restart_button.pack(side="right", padx=(12, 0))
        self._language_changed()

    def _language_changed(self, _name: str | None = None) -> None:
        chosen = language_from_name(self._language.get())
        if needs_restart(chosen, current_language()):
            self._restart_row.grid()
        else:
            self._restart_row.grid_remove()

    # -- Updates -------------------------------------------------------------------------------

    def _build_updates(self) -> _Page:
        page = self._new_page(SECTION_UPDATES)
        auto = page.box(t(AUTO_UPDATE_TITLE), t(AUTO_UPDATE_TEXT))
        auto.add(self._checkbox(auto.body, t(AUTO_UPDATE_SWITCH), self._auto_update), sticky="w")
        check = page.box(
            t("Version {version}", version=self._updates.current), t(CHECK_UPDATES_TEXT)
        )
        line = check.frame_row()
        self._update_button = primary_button(
            line, t("Check for updates"), self._check_updates, width=_UPDATE_BUTTON_WIDTH
        )
        self._update_button.pack(side="left", anchor="n")
        self._update_result = text_label(
            line, "", size=SMALL_SIZE, wraplength=_BOX_INNER - _UPDATE_BUTTON_WIDTH - 12
        )
        self._update_result.pack(side="left", padx=(12, 0), fill="x", expand=True)
        self._go_back_version = self._updates.previous_version
        self.go_back_button: ctk.CTkButton | None = None
        if self._go_back_version is not None:
            back = page.box(t(GO_BACK_TITLE), t(GO_BACK_TEXT))
            row = back.frame_row()
            self.go_back_button = secondary_button(
                row,
                t("Go back to version {version}", version=self._go_back_version),
                self._ask_go_back,
                width=_GO_BACK_BUTTON_WIDTH,
            )
            self.go_back_button.pack(side="left")
        return page

    def _ask_go_back(self) -> None:
        ConfirmDialog(
            self,
            title=t("Go back to version {version}?", version=self._go_back_version),
            text=t(GO_BACK_WARNING),
            confirm=t("Go back"),
            on_confirm=self._go_back,
        )

    def _go_back(self) -> None:
        if self._on_go_back is not None:
            self._on_go_back()

    # -- Backup --------------------------------------------------------------------------------

    def _build_backup(self) -> _Page:
        page = self._new_page(SECTION_BACKUP)
        box = page.box(None, t(BACKUP_TEXT))
        line = box.frame_row()
        secondary_button(
            line, t(SAVE_BACKUP_BUTTON), self._save_backup, width=_BACKUP_BUTTON_WIDTH
        ).pack(side="left")
        secondary_button(
            line, t(LOAD_BACKUP_BUTTON), self._load_backup, width=_BACKUP_BUTTON_WIDTH
        ).pack(side="left", padx=(12, 0))
        return page

    # -- Plex ----------------------------------------------------------------------------------

    def _build_plex(self, plex: PlexService) -> _Page:
        """The Plex box (app/ui/plex_settings.py), which acts at once, not on Save; under it what
        AirGrabber does with and without Plex (app/ui/plex_compare.py)."""
        page = self._new_page(SECTION_PLEX)
        self.plex_box = PlexBox(page, plex)
        phone = self._phone is not None and self._phone.available
        self.plex_compare = PlexCompareBox(page, phone=phone)
        return page

    def _build_phone(self, phone: PhoneService) -> _Page:
        """The phone connection's boxes (app/ui/phone_settings.py); they act at once, not on
        Save."""
        page = self._new_page(SECTION_PHONE)
        self.phone_box = PhoneBox(page, phone)
        return page

    def _backup_dialog_open(self) -> bool:
        dialog = self._backup_dialog
        if dialog is not None and dialog.winfo_exists():
            dialog.lift()
            return True
        return False

    def _save_backup(self) -> None:
        """Save a backup of what is stored (not this form's unsaved changes)."""
        if self._backup is not None and not self._backup_dialog_open():
            self._backup_dialog = SaveBackupDialog(self, backup=self._backup)

    def _load_backup(self) -> None:
        if self._backup is not None and not self._backup_dialog_open():
            self._backup_dialog = load_backup(
                self, backup=self._backup, on_restart=self._on_restart, on_done=self._backup_loaded
            )

    def _backup_loaded(self, result: BackupLoadResult | None) -> None:
        """A load that replaced the settings closes Settings: this form still shows the old
        ones, and Save would put them back."""
        if result is not None and result.settings_replaced and self.winfo_exists():
            self.destroy()

    # -- Advanced ------------------------------------------------------------------------------

    def _build_advanced(self) -> _Page:
        page = self._new_page(SECTION_ADVANCED)
        sites = page.box(t(SITES_TITLE), t(SITES_TEXT))
        boxes = sites.frame_row()
        for column, (name, label) in enumerate(_BUILT_IN_SITES):
            self._checkbox(boxes, label, self._sources[name]).grid(
                row=0, column=column, sticky="w", padx=(0, 20)
            )
        self._build_prowlarr(page)

        excluded = page.box(t(EXCLUDED_TITLE), t(EXCLUDED_TEXT))
        excluded.add(self._entry(excluded.body, self._excluded))
        preferred = page.box(t(PREFERRED_TITLE), t(PREFERRED_TEXT))
        preferred.add(self._entry(preferred.body, self._preferred))
        learned = self._settings.current.learned_groups
        if learned:
            preferred.text(t(LEARNED_LINE, groups=", ".join(learned)))
        blocked = page.box(t(BLOCKED_TITLE), t(BLOCKED_TEXT))
        blocked.add(self._entry(blocked.body, self._blocked))
        return page

    def _build_prowlarr(self, page: _Page) -> None:
        url = self._settings.current.prowlarr_url
        panel = ProwlarrPanel(
            page.frame,
            prowlarr=self._prowlarr,
            use=self._use_prowlarr,
            checkbox=self._checkbox,
            width=_PAGE_WIDTH,
            label_width=_PANEL_LABEL_WIDTH,
            expanded=shows_remote_fields(url),
            on_status=self._prowlarr_status_shown,
        )
        _paint_background(panel, theme.TAB_BG)  # a box like the others
        page.add(panel)
        self._prowlarr_panel = panel
        self._prowlarr_url = self._hinted_entry(
            panel.remote_row("URL"), url, "http://192.168.1.x:9696"
        )
        self._entry(panel.remote_row(t("API key")), self._prowlarr_key, show="•").pack(fill="x")
        self._prowlarr_button, self._prowlarr_result = self._test_row(
            panel.remote_row(""),
            lambda: self._start_test("prowlarr", self._search.test_prowlarr),
            field_width=_PANEL_FIELD_WIDTH,
        )
        panel.refresh()

    # -- widgets -------------------------------------------------------------------------------

    def _entry(self, master: Any, variable: ctk.StringVar, **kwargs: Any) -> ctk.CTkEntry:
        return ctk.CTkEntry(
            master,
            textvariable=variable,
            height=BUTTON_HEIGHT,
            corner_radius=BUTTON_RADIUS,
            font=theme.font(DETAIL_SIZE),
            fg_color=theme.SURFACE,
            border_color=theme.EDGE,
            **kwargs,
        )

    def _hinted_entry(self, master: Any, value: str, placeholder: str) -> ctk.CTkEntry:
        """An entry with placeholder text. CTkEntry hides placeholders on entries bound to a
        textvariable, so this one is read with `get()` (which returns "" for the placeholder)."""
        entry = ctk.CTkEntry(
            master,
            height=BUTTON_HEIGHT,
            corner_radius=BUTTON_RADIUS,
            font=theme.font(DETAIL_SIZE),
            placeholder_text=placeholder,
            fg_color=theme.SURFACE,
            border_color=theme.EDGE,
        )
        if value:
            entry.insert(0, value)
        entry.pack(fill="x")
        return entry

    @staticmethod
    def _word(master: Any, text: str) -> ctk.CTkLabel:
        return ctk.CTkLabel(master, text=text, font=theme.font(DETAIL_SIZE), text_color=theme.TEXT)

    @staticmethod
    def _segmented(
        master: Any,
        values: list[str],
        variable: ctk.StringVar,
        width: int = 260,
        command: Callable[[str], None] | None = None,
    ) -> ctk.CTkSegmentedButton:
        """A row of choices, the chosen one highlighted (Language, Theme, Card size)."""
        return ctk.CTkSegmentedButton(
            master,
            values=values,
            variable=variable,
            command=command,
            width=width,
            height=BUTTON_HEIGHT,
            corner_radius=BUTTON_RADIUS,
            font=theme.font(DETAIL_SIZE, bold=True),
            fg_color=theme.SURFACE,
            selected_color=theme.SELECTED,
            selected_hover_color=theme.SELECTED_HOVER,
            unselected_color=theme.SURFACE,
            unselected_hover_color=theme.CHIP_HOVER,
            text_color=theme.BUTTON_TEXT,
            dynamic_resizing=False,
        )

    def _option_menu(
        self, master: Any, variable: ctk.StringVar, values: list[str], width: int = 260
    ) -> None:
        ctk.CTkOptionMenu(
            master,
            variable=variable,
            values=values,
            width=width,
            height=BUTTON_HEIGHT,
            corner_radius=BUTTON_RADIUS,
            font=theme.font(DETAIL_SIZE),
            dropdown_font=theme.font(DETAIL_SIZE),
            fg_color=theme.SURFACE,
            button_color=theme.EDGE,
            button_hover_color=theme.HOVER,
            text_color=theme.BUTTON_TEXT,
        ).pack(side="left")

    def _checkbox(self, master: Any, text: str, variable: ctk.BooleanVar) -> ctk.CTkCheckBox:
        return ctk.CTkCheckBox(
            master,
            text=text,
            variable=variable,
            width=0,
            height=BUTTON_HEIGHT,
            checkbox_width=22,
            checkbox_height=22,
            corner_radius=6,
            font=theme.font(DETAIL_SIZE),
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            border_color=theme.EDGE,
        )

    def _radio(self, master: Any, text: str, mode: DownloadMode) -> ctk.CTkRadioButton:
        """A download choice; its text is the box's bold name."""
        return ctk.CTkRadioButton(
            master,
            text=text,
            variable=self._mode,
            value=mode.value,
            width=0,
            height=BUTTON_HEIGHT - 8,
            radiobutton_width=22,
            radiobutton_height=22,
            font=theme.font(16, bold=True),
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            border_color=theme.EDGE,
            command=self._mode_changed,
        )

    def _test_row(
        self, cell: ctk.CTkFrame, command: Callable[[], None], *, field_width: int
    ) -> tuple[ctk.CTkButton, ctk.CTkLabel]:
        button = primary_button(cell, t("Test connection"), command, width=_TEST_BUTTON_WIDTH)
        button.pack(side="left", anchor="n")
        result = text_label(
            cell, "", size=SMALL_SIZE, wraplength=field_width - _TEST_BUTTON_WIDTH - 12
        )
        result.pack(side="left", padx=(12, 0), fill="x", expand=True)
        return button, result

    # -- size ----------------------------------------------------------------------------------

    def _fit_size(self) -> None:
        """Fix the dialog at the size of its tallest page in its tallest state (any download
        choice, with the program's button, the magnet note and its form), at
        most MAX_PAGE_HEIGHT (app/ui/settings_layout.py), so it doesn't change between pages or choices; on a short screen
        at the screen's height instead, the pages scrolling. With MediaForge's pages, which aren't
        built yet, at MAX_PAGE_HEIGHT."""
        self._torrent.show(TorrentStatus.SETUP_NEEDED)  # its tallest: the line and a button
        content = 0
        for section, page in self._pages.items():
            self._grid_page(section)
            modes: list[DownloadMode | None] = (
                list(DownloadMode) if section == SECTION_WHERE else [None]
            )
            for mode in modes:
                if mode is not None:
                    self._grid_download_rows(download_rows(mode), form_in_folder=True)
                self.update_idletasks()
                content = max(content, page.frame.winfo_reqheight())
        self._torrent.show(TorrentStatus.LOOKING)
        self._show_download_rows()
        self._show_section(self._section)
        scaling = ctk.ScalingTracker.get_window_scaling(self)
        height = MAX_PAGE_HEIGHT if self._forge_sections else page_height(content, scaling)
        fit_window(self, self._scroll, height)

    # -- Prowlarr ------------------------------------------------------------------------------

    def _prowlarr_status_shown(self, _status: ProwlarrStatus) -> None:
        self._sync_prowlarr_fields()

    def _sync_prowlarr_fields(self) -> None:
        """Show a Prowlarr URL and key that were saved meanwhile (the automatic setup, Install),
        so Save keeps them; fields the user edited stay as they are."""
        current = self._settings.current
        stored = (current.prowlarr_url, current.prowlarr_api_key.get_secret_value())
        if stored == self._prowlarr_shown:
            return
        if (self._prowlarr_url.get(), self._prowlarr_key.get()) == self._prowlarr_shown:
            self._prowlarr_url.delete(0, "end")
            if stored[0]:
                self._prowlarr_url.insert(0, stored[0])
            self._prowlarr_key.set(stored[1])
        self._prowlarr_shown = stored

    # -- saving and testing --------------------------------------------------------------------

    async def _store(self) -> None:
        """Validate and persist the whole form. Raises ValidationError."""
        disabled = disabled_indexers(
            {name: var.get() for name, var in self._sources.items()},
            use_prowlarr=self._use_prowlarr.get(),
            unknown=self._unknown_disabled,
        )
        self._storing = True  # its own change: the form shows it already
        try:
            await self._store_fields(disabled)
        finally:
            self._storing = False
        self._download_shown = download_settings(self._settings.current)

    async def _store_fields(self, disabled: tuple[str, ...]) -> None:
        await self._settings.update(
            download_mode=self._mode.get(),
            quality_tv=quality_from_name(self._quality_tv.get()),
            quality_movie=quality_from_name(self._quality_movie.get()),
            size_tv_min_gb=self._size_tv_min.get(),
            size_tv_max_gb=self._size_tv_max.get(),
            size_movie_min_gb=self._size_movie_min.get(),
            size_movie_max_gb=self._size_movie_max.get(),
            first_day=weekday_from_name(self._first_day.get()),
            calendar_layout=self._chosen_calendar_layout(),
            card_size=size_from_label(self._card_size.get()),
            theme_mode=theme_from_name(self._theme_mode.get()),
            accent=self.accent,
            text_size=self.text_size,
            info_layout=choice_from_name(
                INFO_LAYOUT_NAMES, self._info_layout.get(), InfoLayout.SIDE
            ),
            spotlight_motion=self._spotlight_motion.get(),
            language=language_from_name(self._language.get()),
            download_dir=download_dir_to_save(
                DownloadMode(self._mode.get()),
                self._download_dir.get(),
                self._settings.current.download_dir,
            ),
            movie_download_dir=download_dir_to_save(
                DownloadMode(self._mode.get()),
                self._movie_dir.get(),
                self._settings.current.movie_download_dir,
            ),
            tmdb_api_key=self._tmdb_key.get(),
            excluded_keywords=self._excluded.get(),
            preferred_groups=self._preferred.get(),
            blocked_groups=self._blocked.get(),
            disabled_indexers=disabled,
            prowlarr_url=self._prowlarr_url.get(),
            prowlarr_api_key=self._prowlarr_key.get(),
            **self._remote_form.changes(),
            auto_update=self._auto_update.get(),
            keep_in_tray=self._keep_in_tray.get(),
            auto_download_new_shows=self._auto_new_shows.get(),
            auto_wait_hours=self._wait_hours(),
            stuck_swap_hours=self._stuck_hours(),
            subtitles_folder=self._finished_folder.get(),
        )

    def _show_problem(self, text: str, section: str) -> None:
        """The error line, and the page with the problem."""
        self._error.configure(text=text)
        self._show_section(section)

    async def _save(self) -> None:
        if await self._save_all() and self.winfo_exists():
            self.destroy()

    async def _restart_now(self) -> None:
        """The Language and Accent colour boxes' Restart now: save like Save, then restart
        AirGrabber (which closes this dialog with the main window)."""
        if await self._save_all() and self._on_restart is not None:
            self._on_restart()

    async def _save_all(self) -> bool:
        """Check and store the whole form. False (the dialog shows why) when that failed."""
        self._error.configure(text="")
        for name, part in self._built_parts():  # asked of each before anything is stored
            problem = part.problem()
            if problem is not None:
                self._show_problem(problem, name)
                return False
        self._save_button.configure(state="disabled")
        try:
            await self._store()
        except ValidationError as exc:
            if self.winfo_exists():
                self._show_problem(_describe(exc), _first_section(exc))
                if _prowlarr_error(exc):  # the fields may be folded away
                    self._prowlarr_panel.set_expanded(True)
                if _remote_error(exc) and DownloadMode(self._mode.get()) is DownloadMode.FOLDER:
                    self._reveal_remote_form()  # the form with the problem shows
                self._save_button.configure(state="normal")
            return False
        return await self._save_startup() and await self._save_forge()

    async def _save_forge(self) -> bool:
        """Each built Forge section stores its changes, in order. False (the dialog stays, the
        error line says why) when one couldn't."""
        parts = self._built_parts()
        for name, part in parts:
            problem = await part.save()
            if problem is not None:
                if self.winfo_exists():
                    self._show_problem(problem, name)
                    self._save_button.configure(state="normal")
                return False
        if parts and self.winfo_exists():
            self._save_button.configure(state="normal")
        return True

    async def _save_startup(self) -> bool:
        """Add or remove the Startup shortcut when its box changed, and store its real state.
        False (the dialog stays open, the error line says why) when that failed."""
        result = await apply_start_with_windows(
            self._start_with_windows.get(), self._startup_shown, self._startup
        )
        if self._settings.current.start_with_windows != result.enabled:
            await self._settings.update(start_with_windows=result.enabled)
        if result.error is None:
            return True
        if self.winfo_exists():
            self._startup_shown = result.enabled
            self._start_with_windows.set(result.enabled)
            self._show_problem(result.error, SECTION_AUTOMATIC)
            self._save_button.configure(state="normal")
        return False

    async def _test_remote(self) -> str:
        """The remote form's Test connection: save everything, then test the program."""
        self._error.configure(text="")
        await self._store()
        self._refresh_folder_note()
        return await self._remote.test()

    def _start_test(self, kind: str, probe: Callable[[], Awaitable[str]]) -> None:
        running = self._tests.get(kind)
        if running is not None and not running.done():
            return
        button, result = self._prowlarr_button, self._prowlarr_result
        self._error.configure(text="")
        result.configure(text=t("Saving and testing…"), text_color=theme.TEXT_MUTED)
        button.configure(state="disabled")
        # After a Prowlarr test the panel reads its status again.
        then = self._prowlarr_panel.refresh if kind == "prowlarr" else None
        self._tests[kind] = spawn(
            self._run_test(button, result, probe, then=then), name=f"settings-test-{kind}"
        )

    async def _run_test(
        self,
        button: ctk.CTkButton,
        result: ctk.CTkLabel,
        probe: Callable[[], Awaitable[str]],
        *,
        then: Callable[[], None] | None = None,
    ) -> None:
        text, color = "", theme.DANGER
        try:
            await self._store()
            text, color = f"✓ {await probe()}", theme.SUCCESS
        except ValidationError as exc:
            text = f"✗ {_describe(exc)}"
        except IndexerError as exc:
            text = f"✗ {exc}"
        finally:
            if self.winfo_exists():
                result.configure(text=text, text_color=color)
                button.configure(state="normal")
                if then is not None:
                    then()

    def _check_updates(self) -> None:
        running = self._tests.get("updates")
        if running is not None and not running.done():
            return
        self._update_result.configure(text=t("Checking GitHub…"), text_color=theme.TEXT_MUTED)
        self._update_button.configure(state="disabled")
        self._tests["updates"] = spawn(self._run_update_check(), name="settings-update-check")

    async def _run_update_check(self) -> None:
        try:
            info = await self._updates.check()
        except UpdateError as exc:
            if self.winfo_exists():
                self._update_result.configure(text=f"✗ {exc}", text_color=theme.DANGER)
                self._update_button.configure(state="normal")
            return
        if self.winfo_exists():
            self._update_button.configure(state="normal")
            self._show_update(info)

    def _show_update(self, info: UpdateInfo) -> None:
        if info.skipped:
            self._update_result.configure(
                text=t(
                    "Version {version} was undone on this PC. AirGrabber waits for a newer one.",
                    version=info.latest,
                ),
                text_color=theme.TEXT_MUTED,
            )
        elif not info.available:
            self._update_result.configure(
                text=t("✓ You have the latest version."), text_color=theme.SUCCESS
            )
        elif not info.installable:
            self._update_result.configure(
                text=t(
                    "{version} is on GitHub. This development copy is not updated in place.",
                    version=info.latest,
                ),
                text_color=theme.TEXT_MUTED,
            )
        else:
            self._update_result.configure(
                text=t(
                    "Version {version} is available. AirGrabber restarts to finish.",
                    version=info.latest,
                ),
                text_color=theme.TEXT_MUTED,
            )
            self._update_button.configure(
                text=t("Update to {version}", version=info.latest), command=self._start_update
            )

    def _start_update(self) -> None:
        if self._on_update is not None:
            self._on_update()

    def cancel(self) -> None:
        """Cancel, ✕ and Escape: nothing is stored."""
        if self.winfo_exists():
            self.destroy()

    def _trace(self, variable: tk.Variable, callback: Callable[..., object]) -> None:
        self._traces.append((variable, variable.trace_add("write", callback)))

    def destroy(self) -> None:
        stop_listening = getattr(self, "_stop_listening", None)
        if stop_listening is not None:
            stop_listening()
        for variable, name in self._traces:
            try:
                variable.trace_remove("write", name)
            except tk.TclError:
                pass  # gone with its interpreter
        self._traces.clear()
        for task in self._tests.values():
            task.cancel()
        self._tests.clear()
        if self._preview_task is not None:
            self._preview_task.cancel()
        super().destroy()
