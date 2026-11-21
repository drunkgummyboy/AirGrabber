"""Preferences dialog in plain words, the one Settings window of AirGrabber and MediaForge: a
sidebar of pages grouped by what the user wants to do (app/ui/settings_pages_registry.py:
Overview; Downloading; Your files; Watching; App) and one scrolling page each, its settings
grouped in boxes that each say what they are for (the layout is app/ui/settings_layout.py's).
It opens on Overview unless a page is asked for.

Every change stores itself when it is made (app/ui/settings_commit.py's Committer; spec §3):
switches, ticks, choices and menus at once, a text field on Enter or when the keyboard leaves it
(only when changed), each through SettingsService.update of its own fields (a quality's min and
max together, the sites' ticks and "Use Prowlarr" as `disabled_indexers`). A refusal is said in
red in its box and stores nothing; a stored change shows "✓ Saved · Undo" for a few seconds.
There is no Save or Cancel: ✕ and Escape close (a text field's unstored text is stored first).
A value stored elsewhere while the window is open (Quick setup, qBittorrent's setup, a phone, the
TMDB guide, an Undo) shows in its control, unless that control is being edited.

`master`: the main window it opens over and belongs to (transient, placed over it). MainWindow
keeps the one open dialog and closes it when the window hides.

MediaForge's parts (`forge=`, FORGE_PARTS of app/forge/ui/settings_pages.py) wear AirGrabber's
look like the rest, are built at their page's first visit (nothing of Forge starts before one is
shown) and store their own changes through the same Committer."""

from __future__ import annotations

import asyncio
import functools
import logging
import time
import tkinter as tk
from collections.abc import Awaitable, Callable, Mapping, Sequence
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
from app.models.settings import QUALITY_NAMES as QUALITY_NAMES  # the menu's names, in order
from app.services.backup import BackupService
from app.services.folder_library.service import FolderLibraryService
from app.services.phone.service import PhoneService
from app.services.plex import PlexService
from app.services.problem_report import ProblemReports
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
    SMALL_SIZE,
    activate_later,
    place_over,
    primary_button,
    screen_area,
    secondary_button,
    text_label,
)
from app.ui.phone_app import PhoneAppDialog
from app.ui.phone_settings import PhoneBox
from app.ui.plex_compare import PlexCompareBox
from app.ui.folder_library_settings import FolderLibraryBox, FoldersBusy
from app.ui.folders_page import FoldersPage
from app.ui.plex_settings import FoldersLine, PlexAccountBox, PlexBox
from app.ui.prowlarr_panel import PANEL_PAD_X, ProwlarrPanel, shows_remote_fields
from app.ui.quick_setup import OPEN_TEXT as QUICK_SETUP_TEXT
from app.ui.quick_setup import QuickSetupDialog
from app.ui.remote_program_form import FIELD_LABELS as REMOTE_LABELS
from app.ui.remote_program_form import (
    FILMS_HINT,
    TEST_ONLY_TEXT,
    FILMS_LABEL,
    FolderNote,
    RemoteProgramForm,
    folder_note,
)
from app.ui.settings_layout import (
    BOX_GAP,
    MAX_PAGE_HEIGHT,
    PAGE_BOTTOM,
    TEXT_SIZE,
    WIDE_PAGE_WIDTH,
    PageSlot,
    SettingsBox,
    SettingsPage,
    box_inner,
    build_layout,
    fill_sidebar,
    fit_window,
    fitting_page_width,
    page_height,
    page_width,
    use_page_width,
    wrap_toggle,
)
from app.ui.settings_pages_registry import (
    FORGE,
    GROUPS,
    PAGE_FOLDERS,
    PAGE_ORDER,
    PAGE_OVERVIEW,
    PAGE_PLAYING,
    PAGE_PLEX,
    PAGE_TORRENT,
    PAGES,
)
from app.ui.settings_commit import (
    Committer,
    Refused,
    TextCommit,
    commit_fields,
    defocus_on_click,
    text_commit,
)
from app.ui.settings_chips import ChipList
from app.ui.settings_layout import SCREEN_RESERVE as SCREEN_RESERVE
from app.ui.settings_layout import dialog_height as dialog_height
from app.ui.settings_layout import sidebar_width as _sidebar_width
from app.ui.settings_overview import SORTING_UNAVAILABLE, Card, OverviewCards, overview_cards
from app.ui.settings_search import Match, Place, SettingsSearch, places
from app.ui.reflow import follow_width
from app.ui.tasks import spawn
from app.ui.tmdb_key_guide import KeyCheck, TmdbKeyDialog, key_check_message
from app.ui.torrent_app_status import INSTALL_BUTTON_WIDTH, TorrentAppStatus, TorrentStatus

if TYPE_CHECKING:
    from app.forge.runtime import ForgeRuntime
    from app.forge.ui.settings_pages import ForgePart, ForgeSectionPart
    from app.models.plex import PlexStatus

log = logging.getLogger(__name__)

# -- pages -------------------------------------------------------------------------------------
# The pages, their groups, icons, notes and parts: app/ui/settings_pages_registry.py. A page's
# name is its key here and in the tests; the dialog shows it through t(), as every text constant
# below.

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
NEW_TITLE = N_("For new shows and films")
NEW_TEXT = N_("You can still change this for each show and each film.")
NEW_SHOWS_SWITCH = N_("Download new episodes for shows I start following")
NEW_FILMS_SWITCH = N_("Download films I add to my watchlist when they're out")
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

FOLDER_LABEL = N_("Finished downloads")
FOLDER_HINT = N_(
    "Your torrent program's download folder. MediaForge watches it, and the Downloads page looks "
    "there for free space and your files."
)

TMDB_TITLE = N_("TMDB")
TMDB_KEY_LABEL = N_("TMDB key")  # in a problem's text
TMDB_KEY_TEXT = N_(
    "A free code from themoviedb.org that lets AirGrabber ask TMDB for this information. "
    "Without it, the Movies and Discover pages stay empty. Your shows and the Calendar work "
    "without it."
)
KEY_GUIDE_LINK = N_("How do I get a free key?")
SOURCES_TEXT = N_(
    "Show and film information and pictures come from TVmaze and TMDB. This product uses the "
    "TMDB API but is not endorsed or certified by TMDB."
)

CALENDAR_TITLE = N_("Calendar")
CALENDAR_LAYOUT_TITLE = N_("Calendar view")  # in a problem's text
CALENDAR_LAYOUT_TEXT = N_(
    "Weeks: a grid of whole weeks, you choose how many on the Calendar. Rolling week: one row with "
    "today in the middle, three days back and three ahead."
)
CALENDAR_LAYOUT_NAMES: dict[CalendarLayout, str] = {
    CalendarLayout.WEEKS: N_("Weeks"),
    CalendarLayout.ROLLING: N_("Rolling week"),
}
WEEK_TITLE = N_("Week starts on")

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
INFO_LAYOUT_TITLE = N_("Info window")
INFO_LAYOUT_TEXT = N_("Where a film's or series' info window shows its releases.")
SPOTLIGHT_TITLE = N_("Spotlight")
SPOTLIGHT_SWITCH = N_("Move the spotlight to the next title by itself")

LANGUAGE_TITLE = N_("Language")
LANGUAGE_TEXT = N_("The language of AirGrabber's pages, buttons and messages.")
RESTART_TEXT = N_("Restart AirGrabber to switch the language")

AUTO_UPDATE_TEXT = N_(
    "New versions download in the background and are installed when AirGrabber closes."
)
AUTO_UPDATE_SWITCH = N_("Install updates automatically")
# The hidden switch for the phone features (the owner, 2026-11-10): "Version …" clicked
# SWITCH_CLICKS times, each within SWITCH_GAP_S of the one before. No cue on screen.
SWITCH_CLICKS = 7
SWITCH_GAP_S = 1.5
PHONE_ON_TITLE = N_("Phone features on")
PHONE_OFF_TITLE = N_("Phone features off")
PHONE_SWITCH_TEXT = N_(
    "This counts from the next time AirGrabber starts. Your paired phones are kept."
)
PHONE_SWITCH_FAILED = N_("AirGrabber couldn't switch the phone features.")
REPORT_TITLE = N_("Something not working?")
REPORT_TEXT = N_(
    "This puts AirGrabber's logs in one file on your desktop, so you can send it to whoever helps "
    "you with AirGrabber. It holds no passwords, keys or file names, and nothing is sent by itself."
)
REPORT_BUTTON = N_("Make a report file")
REPORT_ON_DESKTOP = N_("The report file is on your desktop: send it to whoever helps you.")
REPORT_IN_FOLDER = N_(
    "The report file is in the folder that just opened: send it to whoever helps you."
)
REPORT_FAILED = N_("AirGrabber couldn't make the report file, so try again.")
GO_BACK_TITLE = N_("Not happy with this version?")
GO_BACK_TEXT = N_(
    "AirGrabber keeps the version from before its last update. "
    "If you go back, it waits for a version newer than this one."
)
GO_BACK_WARNING = N_(
    "AirGrabber restarts on the older version. Your library and settings stay, unless this version "
    "changed how they are stored: then they return to how they were just before the update."
)

SITES_TITLE = N_("Sites")
SITES_TEXT = N_("Websites AirGrabber searches by itself.")
EXCLUDED_TITLE = N_("Skip releases with these words")
EXCLUDED_TEXT = N_("Separate with commas, for example: cam, telesync")
PREFERRED_TITLE = N_("Prefer releases from")
PREFERRED_TEXT = N_("Release groups whose releases come first.")
BLOCKED_TITLE = N_("Never use releases from")
BLOCKED_TEXT = N_(
    "Release groups never shown or used. \"Wrong or fake?\" in a download's ⋯ menu adds one."
)
LEARNED_LINE = N_("Learned from the downloads you kept: {groups}")

BACKUP_TITLE = N_("Backup")
BACKUP_TEXT = N_(
    "One file with your followed shows, film watchlist, download history and settings. Load "
    "it on this PC or another one: you see what's in it before anything changes."
)
SAVE_BACKUP_BUTTON = N_("Save a backup…")
LOAD_BACKUP_BUTTON = N_("Load a backup…")
_BACKUP_BUTTON_WIDTH = 190

# -- sizes (logical, unscaled CTk units) ---------------------------------------------------------

# The sidebar, the pages and their boxes: app/ui/settings_layout.py; the names this dialog uses
# below. The page's width is the open window's (`page_width()` / `box_inner()`, decided as it
# opens from the screen it opens on).
_PAGE_BOTTOM = PAGE_BOTTOM
_SPOT_TRIES = 100  # looks for a spot of a page still getting ready, this many times
_SPOT_WAIT_S = 0.05
_BOX_GAP = BOX_GAP
_TEXT_SIZE = TEXT_SIZE
_INDENT = 30  # under a radio button's text
_TEST_BUTTON_WIDTH = 160
_REFLOW_WAIT_MS = 60  # after the last size change, the texts follow
_UPDATE_BUTTON_WIDTH = 180
_GO_BACK_BUTTON_WIDTH = 240
_CHECK_BUTTON_WIDTH = 110
_RESTART_BUTTON_WIDTH = 150
_SIZE_ENTRY_WIDTH = 70
_QUALITY_MENU_WIDTH = 230
_REMOTE_LABEL_WIDTH = 130
_REMOTE_GAP = 12  # above that form
_PANEL_INSET = PANEL_PAD_X + 1  # the Prowlarr panel's border and padding
_PANEL_LABEL_WIDTH = 120


def _remote_field_width() -> int:
    """The NAS / other computer form's fields under its radio button (or the magnet note), with
    some slack."""
    return box_inner() - _INDENT - _REMOTE_LABEL_WIDTH - 10


def _panel_field_width() -> int:
    """The Prowlarr panel's fields."""
    return box_inner() - 2 * _PANEL_INSET - _PANEL_LABEL_WIDTH

# (stable id, label) of the websites AirGrabber searches itself, in the order the search runs
# them. Prowlarr (searched last) has its own panel.
_BUILT_IN_SITES = [
    (cls.name, cls.label) for cls in (ApibayIndexer, EztvIndexer, BitsearchIndexer, YtsIndexer)
]

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


# Settings → Pages → Info window, in the order the box shows them.
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


def _folder_text(folder: Path | None) -> str:
    """A folder as its entry shows it ("" for none)."""
    return str(folder) if folder is not None else ""


def size_text(value: float | None) -> str:
    """A stored size limit as its entry shows it: "" for no limit, "2.5", "4" (not "4.0"); with a
    decimal comma in Dutch ("2,5", which storing reads back)."""
    return "" if value is None else number(value, "g")


class DownloadRows(NamedTuple):
    """Which extra rows Torrent program shows under its three choices."""

    torrent_status: bool  # the torrent-program status line (+ Install qBittorrent)
    remote_form: bool  # the NAS / other computer form (program, address, account, Test)
    folder: bool  # the folder entry + Browse, and the magnet note with its link


def download_rows(mode: DownloadMode) -> DownloadRows:
    return DownloadRows(
        torrent_status=mode is DownloadMode.APP,
        remote_form=mode is DownloadMode.REMOTE,
        folder=mode is DownloadMode.FOLDER,
    )


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
    error: str | None  # why it couldn't be changed (the Windows box says it in red)


async def apply_start_with_windows(
    wanted: bool, shown: bool, shortcut: StartupShortcut
) -> StartupResult:
    """"Start AirGrabber when Windows starts": add or remove the shortcut only when `wanted`
    differs from `shown` (its real state as the box showed it)."""
    error: str | None = None
    if wanted != shown:
        try:
            await shortcut.set_enabled(wanted)
        except StartupError as exc:
            error = str(exc)
    return StartupResult(enabled=shortcut.is_enabled(), error=error)


# Shown through t() (`_describe`).
_FIELD_LABELS = {
    "download_mode": PAGE_TORRENT,
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
    "auto_update": AUTO_UPDATE_SWITCH,
    "disabled_indexers": SITES_TITLE,
    "excluded_keywords": EXCLUDED_TITLE,
    "preferred_groups": PREFERRED_TITLE,
    "blocked_groups": BLOCKED_TITLE,
    "prowlarr_url": N_("Prowlarr URL"),
    "prowlarr_api_key": N_("Prowlarr API key"),
    "subtitles_folder": FOLDER_LABEL,
    **REMOTE_LABELS,
}

def stuck_name(hours: int) -> str:
    """"Never" for 0, else "6 h"."""
    return t(STUCK_NEVER) if hours <= 0 else t(WAIT_HOURS, n=hours)


def _describe(error: ValidationError) -> str:
    lines = []
    for item in error.errors():
        label = _FIELD_LABELS.get(str(item["loc"][0])) if item["loc"] else None
        message = item["msg"].removeprefix("Value error, ")
        lines.append(f"{t(label)}: {message}" if label else message)
    return "\n".join(lines)


# -- layout pieces ---------------------------------------------------------------------------------

# The shared layout's page and box (app/ui/settings_layout.py), by the names this dialog uses.
_Box = SettingsBox
_Page = SettingsPage


def sidebar_width() -> int:
    """The sidebar's width (logical): room for the longest page name in bold (the selected
    one), so the pages don't shift when another page is chosen."""
    return _sidebar_width(PAGE_ORDER)


def _screen_width(master: Any, window: Any) -> float:
    """The width (logical) of the screen's work area `master` is on (where Settings opens)."""
    left, _top, right, _bottom = screen_area(master)
    scaling = float(ctk.ScalingTracker.get_window_scaling(window))
    return (right - left) / scaling


def _inside(widget: tk.Misc, frame: tk.Misc) -> bool:
    """Whether `widget` is `frame` or shows inside it: inside its children, or placed into it
    (the remote program's form is the page's, gridded into a box: `grid(in_=…)`)."""
    found: tk.Misc | None = widget
    while found is not None:
        if found is frame:
            return True
        found = _holder(found)
    return False


def _holder(widget: tk.Misc) -> tk.Misc | None:
    """What `widget` shows in: where it is gridded / packed / placed, else its master."""
    try:
        manager = str(widget.winfo_manager())
        info: dict[str, Any] = {}
        if manager == "grid":
            info = dict(widget.grid_info())  # type: ignore[attr-defined]
        elif manager == "pack":
            info = dict(widget.pack_info())  # type: ignore[attr-defined]
        elif manager == "place":
            info = dict(widget.place_info())  # type: ignore[attr-defined]
    except (tk.TclError, KeyError):
        info = {}
    holder = info.get("in")
    if isinstance(holder, tk.Misc) and holder is not widget:
        return holder
    return widget.master


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
        folder_library: FolderLibraryService | None = None,
        problem_reports: ProblemReports | None = None,
    ) -> None:
        """`on_restart`: restarts
        AirGrabber (the Language box's "Restart now"; without it only the note shows).
        `images`: the Card size box's preview card (none without it); `library`: a followed
        show's poster for it (the placeholder card without one). `backup`: the Backup box
        shows when it is given. `forge`: MediaForge's parts (Sorting, Naming, Subtitles and
        their boxes on other pages) show when it is given. `section`: the page it opens at
        (Overview when not given or not shown). `plex` / `folder_library`: Plex & library shows
        when either is given (Plex's boxes, Playing's too, with `plex`; the folders' on Folders).
        `problem_reports`: the "Something not working?" box shows when it is given."""
        super().__init__(master, fg_color=theme.BG)
        # The pages' width, decided once for the screen it opens on (logical units): as wide as
        # it fits, so that the whole window fits a 1366 x 768 screen at 150 % too. Every page,
        # MediaForge's later ones too, is built for it.
        use_page_width(fitting_page_width(_screen_width(master, self), sidebar_width()))
        self._forge = forge
        # The MediaForge parts built so far, by page (a part with boxes on two pages under both).
        self.parts: dict[str, ForgeSectionPart] = {}
        # Stores every change of this window, says a refusal in its box, shows the Undo bar.
        self.committer = Committer(self, describe=_describe)
        self._texts: list[TextCommit] = []  # the text fields, shown again by `_show_stored`
        # ✕ / Escape: the fields' stores under way (`close`), and the texts refused then (a
        # second ✕ closes without storing them).
        self._closing: asyncio.Task[None] | None = None
        self._refused_at_close: dict[TextCommit, list[str]] = {}
        # Imported here: AirGrabber's start doesn't load Forge's pages (INTEGRATION.md 3.6).
        self._forge_parts: Mapping[str, ForgePart] = {}
        if forge is not None:
            from app.forge.ui.settings_pages import FORGE_PARTS

            self._forge_parts = FORGE_PARTS
        self._backup = backup
        self._plex = plex  # Plex & library shows when it or `folder_library` is given
        self._folder_library = folder_library
        self._problem_reports = problem_reports
        self.report_button: ctk.CTkButton | None = None
        self.report_result: ctk.CTkLabel | None = None
        self.plex_box: PlexBox | None = None
        self.folder_box: FolderLibraryBox | None = None  # Folders' "Count what's in them"
        self.folders_page: FoldersPage | None = None  # Folders' Series and Movies boxes
        self._folders_busy = FoldersBusy()  # the two share it: one change of the folders at a time
        self.folders_line: FoldersLine | None = None  # Plex & library's line about them
        self.plex_compare: PlexCompareBox | None = None
        self.plex_account: PlexAccountBox | None = None
        self._phone = phone  # the Phone connection page shows when it is given and available
        self.phone_box: PhoneBox | None = None
        self.phone_switch_dialog: PhoneAppDialog | None = None  # the hidden switch's (tests)
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
        self.overview: OverviewCards | None = None  # Overview's cards
        self._overview_task: asyncio.Task[None] | None = None
        self._plex_status: PlexStatus | None = None  # Plex's, once read (the Overview's)
        self._forge_start_ended = False  # MediaForge's start ended while this window was open
        current = settings.current

        self.title(t("Settings"))
        set_window_icon(self)
        self.resizable(True, True)  # wider and taller by hand; never narrower than it opens
        self.transient(master)
        self._built_width = 0  # logical, as it opened (`_fit_size`); its texts follow beyond it
        self._reflow_job: str | None = None

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
        self.learned_line: ctk.CTkLabel | None = None  # "Learned from …" under Prefer
        disabled = set(current.disabled_indexers)
        self._sources = {
            name: ctk.BooleanVar(value=name not in disabled) for name, _label in _BUILT_IN_SITES
        }
        self._use_prowlarr = ctk.BooleanVar(value=ProwlarrIndexer.name not in disabled)
        known = {name for name, _label in _BUILT_IN_SITES} | {ProwlarrIndexer.name}
        self._unknown_disabled = tuple(n for n in current.disabled_indexers if n not in known)
        self._prowlarr_key = ctk.StringVar(value=current.prowlarr_api_key.get_secret_value())
        self._auto_update = ctk.BooleanVar(value=current.auto_update)
        self._keep_in_tray = ctk.BooleanVar(value=current.keep_in_tray)
        # The real shortcut, not the setting: it may have been removed by hand.
        self._start_with_windows = ctk.BooleanVar(value=self._startup.is_enabled())
        self._auto_new_shows = ctk.BooleanVar(value=current.auto_download_new_shows)
        self._auto_new_films = ctk.BooleanVar(value=current.auto_download_new_films)
        self._auto_wait = ctk.StringVar(value=t(WAIT_HOURS, n=current.auto_wait_hours))
        self._stuck_swap = ctk.StringVar(value=stuck_name(current.stuck_swap_hours))
        self._finished_folder = ctk.StringVar(value=current.subtitles_folder)

        layout = build_layout(self, sidebar_width())
        body = layout.body
        self._sidebar = layout.sidebar
        self._sidebar_scroll = layout.sidebar_scroll
        self._scroll = layout.scroll

        # Every page with its parts in order (app/ui/settings_pages_registry.py); a MediaForge
        # part gets its place now and is built at its page's first visit. A page with nothing
        # to show is left out.
        self._pages: dict[str, _Page] = {}
        self._slots: dict[str, PageSlot] = {}  # MediaForge's places, by page
        self.go_back_button: ctk.CTkButton | None = None
        self._go_back_version = self._updates.previous_version
        builders: dict[str, Callable[[_Page], bool]] = {
            "overview": self._build_overview,
            "quick-setup": self._build_quick_setup,
            "download-choices": self._build_download_choices,
            "quality": self._build_quality,
            "sites": self._build_sites,
            "skip": self._build_skip,
            "automatic": self._build_automatic,
            "folders": self._build_folders,
            "finished": self._build_finished,
            "count": self._build_count,
            "plex": self._build_plex,
            "folders-line": self._build_folders_line,
            "library-compare": self._build_library_compare,
            "playing": self._build_playing,
            "phone": self._build_phone,
            "tmdb": self._build_tmdb,
            "plex-account": self._build_plex_account,
            "looks": self._build_looks,
            "pages": self._build_pages,
            "background": self._build_background,
            "version": self._build_version,
            "go-back": self._build_go_back,
            "backup": self._build_backup,
            "report": self._build_report,
        }
        # All of them first: a part may put boxes on a later page (Plex's on Playing).
        self._made = {name: self._new_page(name) for name in PAGE_ORDER}
        for name in PAGE_ORDER:
            page, shown = self._made[name], False
            for part in PAGES[name].parts:
                if part == FORGE:
                    if name in self._forge_parts:
                        self._slots[name] = page.slot()
                        shown = True
                elif builders[part](page):
                    shown = True
            if shown:
                self._pages[name] = page
        for name, page in self._made.items():
            if name not in self._pages:
                page.frame.destroy()
        self._made.clear()
        shown_groups = [
            (group, [name for name in names if name in self._pages]) for group, names in GROUPS
        ]
        icons = {name: spec.icon for name, spec in PAGES.items()}
        self.search_box = SettingsSearch(
            self._sidebar, look_up=self._search_places, on_pick=self._go_to_match
        )
        self.search_box.pack(fill="x", padx=(6, 6), pady=(0, 12))
        self._items = fill_sidebar(self._sidebar, shown_groups, icons, self._show_section)
        self.draw_overview()  # before the window is sized: the cards are part of the page
        self._section = section if section is not None and section in self._items else PAGE_OVERVIEW
        self._show_section(self._section)
        if update_info is not None:
            self._show_update(update_info)

        # Stored elsewhere meanwhile (Quick setup, qBittorrent's setup, a phone, an Undo):
        # each control shows it, unless it is being edited.
        self._stop_listening = self._settings.subscribe(self._settings_changed)
        # The Overview follows Plex, the folder library and MediaForge's start too.
        self._stop_overview: list[Callable[[], None]] = []
        if self._plex is not None:
            self._stop_overview.append(self._plex.subscribe(self.refresh_overview))
        if self._folder_library is not None:
            self._stop_overview.append(self._folder_library.subscribe(self.draw_overview))
        # MediaForge's start ending (ready, or why it can't): Folders' and Sorting's cards say
        # more. Forgotten when the window goes (`destroy`): a start may never come.
        when_start_ends = getattr(forge, "when_start_ends", None)
        if forge is not None and forge.services is None and not forge.reason and when_start_ends:
            self._stop_overview.append(when_start_ends(self._forge_ready))
        self.refresh_overview()
        defocus_on_click(self)  # a click outside a text field: the field stores what it holds
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda _event: self.close())
        self.borderless = make_borderless(
            self, drag_widgets=(self, body, self._sidebar), on_close=self.close
        )
        self._fit_size()
        self.bind("<Configure>", self._on_configure, add="+")
        # The program's status redraws the Overview from here on: not for the one `_fit_size`
        # shows only to measure the page.
        self._torrent.on_status = self.draw_overview
        self._local_torrent.on_status = self.draw_overview
        place_over(self, master)
        activate_later(self)

    # -- sidebar and pages ---------------------------------------------------------------------

    @property
    def section(self) -> str:
        """The page that shows."""
        return self._section

    @property
    def sections(self) -> list[str]:
        """The sidebar's pages, in order."""
        return list(self._items)

    def page(self, section: str) -> ctk.CTkFrame:
        """A page's frame (tests look inside it)."""
        return self._pages[section].frame

    def box(self, section: str, title: str) -> _Box:
        """A page's box by its shown title (tests look at its refusal)."""
        found = self._pages[section].find_box(title)
        if found is None:
            raise KeyError(title)
        return found

    def show_section(self, section: str) -> None:
        """Show `section`'s page (an open dialog asked for another one); a page that isn't
        shown changes nothing."""
        self._show_section(section)

    def _search_places(self) -> list[Place]:
        """What Settings' search looks through now: the pages' boxes, and the MediaForge parts
        not built yet by their words."""
        built = {name: page.boxes for name, page in self._pages.items()}
        unbuilt = {
            name: self._forge_parts[name].words for name in self._slots if name not in self.parts
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
        a MediaForge part may still be getting ready. Then the part's `spot_shown(spot)`, when
        it has one (Sorting's `rule:<id>` opens that rule's editor)."""
        self._show_section(section)
        spawn(self._scroll_to_spot(section, spot), name="settings-spot")

    async def _scroll_to_spot(self, section: str, spot: str) -> None:
        for _ in range(_SPOT_TRIES):
            if not self.winfo_exists() or self._section != section:
                return
            part: Any = self.parts.get(section)
            if part is None and section == PAGE_FOLDERS:
                part = self.folders_page  # AirGrabber's part: its Series box
            spots: dict[str, tk.Misc] = getattr(part, "spots", {})
            frame = spots.get(spot)
            if frame is not None and frame.winfo_ismapped():
                canvas = self._scroll._parent_canvas
                canvas.update_idletasks()
                box = canvas.bbox("all")
                if box and box[3] > 0:
                    canvas.yview_moveto(max(0, frame.winfo_rooty() - self._scroll.winfo_rooty()) / box[3])
                shown: Callable[[str], object] | None = getattr(part, "spot_shown", None)
                if shown is not None:
                    shown(spot)  # e.g. a rule's row: its editor opens
                return
            await asyncio.sleep(_SPOT_WAIT_S)

    def _show_section(self, section: str) -> None:
        if section not in self._pages:
            return
        if section in self._slots and section not in self.parts:
            self._build_forge_part(section)
        if section == PAGE_FOLDERS and self.folders_page is not None:
            self.folders_page.page_shown()  # MediaForge started for the marks (once)
        if section == PAGE_OVERVIEW and section != getattr(self, "_section", None):
            self.refresh_overview()  # what other pages changed meanwhile
        self._section = section
        self._grid_page(section)
        self.reflow()  # a page built at its first visit, or cards drawn again
        self._scroll._parent_canvas.yview_moveto(0)  # each page starts at its top
        for name, item in self._items.items():
            item.set_selected(name == section)

    def _grid_page(self, section: str) -> None:
        for name, page in self._pages.items():
            if name != section:
                page.frame.grid_remove()
        self._pages[section].frame.grid(row=0, column=0, sticky="ew", pady=(0, _PAGE_BOTTOM))

    def _new_page(self, section: str) -> _Page:
        return _Page(self._scroll, t(section), t(PAGES[section].note))

    def _build_forge_part(self, section: str) -> None:
        """A page's MediaForge part, at the page's first visit (it starts Forge if nothing has
        yet); a part with boxes on other pages too is built for all of them at once."""
        assert self._forge is not None
        owner = self._forge_parts[section].owner or section
        build = self._forge_parts[owner].build
        assert build is not None
        pages = [owner, *(name for name, p in self._forge_parts.items() if p.owner == owner)]
        slots = {name: self._slots[name] for name in pages if name in self._slots}
        part = build(slots, self._forge, self.committer)
        for name in slots:
            self.parts[name] = part

    # -- Overview ------------------------------------------------------------------------------

    def _build_overview(self, page: _Page) -> bool:
        """The cards (app/ui/settings_overview.py): drawn once every page is built
        (`draw_overview`), again when what they show changes and each time the page shows."""
        self.overview = OverviewCards(page, on_open=self.show_section)
        return True

    def overview_now(self) -> list[Card]:
        """The Overview's cards for what is known now (no service asked; MediaForge's facts
        only while it runs: the Overview never starts it)."""
        forge_facts, reason = None, ""
        if self._forge is not None:
            from app.forge.ui.settings_pages import overview_facts  # with FORGE_PARTS

            forge_facts = overview_facts(self._forge)  # the rules only, while it runs
            reason = self._forge.reason
            if forge_facts is None and not reason and self._forge_start_ended:
                reason = t(SORTING_UNAVAILABLE)  # not "getting ready" for ever
        torrent: TorrentAppStatus | None = getattr(self, "_torrent", None)
        local: TorrentAppStatus | None = getattr(self, "_local_torrent", None)
        return overview_cards(
            self._settings.current,
            pages=list(self._pages),
            version=self._updates.current,
            start_with_windows=self._startup.is_enabled(),
            torrent=torrent.status if torrent is not None else None,
            torrent_name=torrent.name if torrent is not None else "",
            local_torrent=local.status if local is not None else None,
            plex=self._plex_status,
            folders=self._folder_library.status if self._folder_library is not None else None,
            forge=forge_facts,
            forge_reason=reason,
        )

    def draw_overview(self) -> None:
        """Draw the cards again for what is known now."""
        if self.overview is not None and self._pages and self.winfo_exists():
            self.overview.show(self.overview_now())

    def refresh_overview(self) -> None:
        """Draw the cards now, then once Plex's status is read (as the Plex page reads it; the
        newest call wins)."""
        self.draw_overview()
        if self._overview_task is not None and not self._overview_task.done():
            self._overview_task.cancel()
        self._overview_task = spawn(self._load_overview(), name="settings-overview")

    async def _load_overview(self) -> None:
        if self._plex is not None:
            status = await self._plex.status()
            if not self.winfo_exists():
                return
            self._plex_status = status
        self.draw_overview()

    def _forge_ready(self) -> None:
        """MediaForge's start has ended meanwhile (ready, or not): Folders' and Sorting's cards
        can say more."""
        try:
            alive = bool(self.winfo_exists())
        except tk.TclError:
            alive = False
        if alive:
            self._forge_start_ended = True
            self.refresh_overview()

    def _build_quick_setup(self, page: _Page) -> bool:
        # The most important choices of every page in one dialog (also the welcome's last step).
        quick = page.box(t(QUICK_SETUP_TEXT), t(QUICK_SETUP_NOTE))
        line = quick.frame_row()
        secondary_button(line, t(QUICK_SETUP_TEXT), self._open_quick_setup, width=180).pack(
            side="left"
        )
        return True

    # -- Torrent program ---------------------------------------------------------------------

    def _build_download_choices(self, page: _Page) -> bool:
        app = page.box(search_title=t(APP_CHOICE))
        app.add(self._radio(app.body, t(APP_CHOICE), DownloadMode.APP), gap=0, sticky="w")
        app.add(app.text(t(APP_TEXT), indent=_INDENT), gap=2, sticky="w", padx=_INDENT)
        self._torrent = TorrentAppStatus(
            app.body,
            torrent_app=self._torrent_app,
            wraplength=box_inner() - _INDENT - INSTALL_BUTTON_WIDTH - 16,
            reinstall=True,
        )
        self._torrent_row = app.take_row()

        remote = page.box(search_title=t(REMOTE_CHOICE))
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
            wraplength=box_inner() - _INDENT - INSTALL_BUTTON_WIDTH - 16,
            reinstall=True,
        )
        self._local_torrent_row = remote.take_row()
        self._remote_box = remote
        self._remote_row = remote.take_row()

        folder = page.box(search_title=t(FOLDER_CHOICE))
        folder.add(
            self._radio(folder.body, t(FOLDER_CHOICE), DownloadMode.FOLDER), gap=0, sticky="w"
        )
        folder.add(folder.text(t(FOLDER_TEXT), indent=_INDENT), gap=2, sticky="w", padx=_INDENT)
        self._folder_box = folder
        self._mode_boxes = {DownloadMode.APP: app, DownloadMode.REMOTE: remote, DownloadMode.FOLDER: folder}
        self._folder_row = ctk.CTkFrame(folder.body, fg_color="transparent")
        folder_entry = self._entry(self._folder_row, self._download_dir)
        folder_entry.pack(side="left", fill="x", expand=True)
        self._download_dir_field = self._text_field(
            folder_entry,
            stored=lambda: str(self._settings.current.download_dir),
            commit=lambda text: self._commit(folder, download_dir=text.strip()),
        )
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
        self._movie_dir_field = self._text_field(
            self._movie_dir,
            stored=lambda: _folder_text(self._settings.current.movie_download_dir),
            commit=lambda text: self._commit(folder, movie_download_dir=text.strip()),
        )
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

        # One form, shown in the remote box or (folder mode) under the magnet note: it belongs to
        # the page and is placed into either box, painted in the boxes' colour.
        self._remote_form = RemoteProgramForm(
            page.frame,
            settings=self._settings.current,
            run_test=self._test_remote,
            label_width=_REMOTE_LABEL_WIDTH,
            field_width=_remote_field_width(),
            describe=_describe,
            films=True,
            stored=lambda: self._settings.current,
            on_change=self._remote_changed,
            testing_text=TEST_ONLY_TEXT,
        )
        for field in self._remote_form.text_fields:
            self.committer.watch(field)
        self._remote_form.configure(corner_radius=0)
        _paint_background(self._remote_form, theme.TAB_BG)
        self._show_local_line(self._settings.current)
        return True

    # -- Folders -------------------------------------------------------------------------------

    def _build_folders(self, page: _Page) -> bool:
        """The Series and Movies boxes (app/ui/folders_page.py): one list per kind, the folder
        library's, with MediaForge's destination marked in it (`LibraryDestinations`)."""
        destinations = None
        if self._forge is not None:
            from app.forge.ui.settings_pages import LibraryDestinations  # with FORGE_PARTS

            destinations = LibraryDestinations(self._forge)
        self.folders_page = FoldersPage(
            page,
            self._settings,
            self.committer,
            folders=self._folder_library,
            destinations=destinations,
            busy=self._folders_busy,
        )
        return True

    def _build_finished(self, page: _Page) -> bool:
        """Where the torrent program's finished downloads land, whichever way they go there:
        MediaForge's watching, the free space and "Open the folder" read it."""
        finished = page.box(t(FOLDER_LABEL), t(FOLDER_HINT))
        line = finished.frame_row()
        entry = self._entry(line, self._finished_folder)
        entry.pack(side="left", fill="x", expand=True)
        self._finished_field = self._text_field(
            entry,
            stored=lambda: self._settings.current.subtitles_folder,
            commit=lambda text: self._commit(finished, subtitles_folder=text),
        )
        secondary_button(line, t("Choose…"), self._choose_finished_folder, width=110).pack(
            side="left", padx=(10, 0)
        )
        return True

    def _build_count(self, page: _Page) -> bool:
        """The "Count what's in them" box (app/ui/folder_library_settings.py): the folder
        library's switch, through its own service (FolderLibraryService)."""
        folder_library = self._folder_library
        if folder_library is None:
            return False
        self.folder_box = FolderLibraryBox(
            page, folder_library, self._settings, self.committer, busy=self._folders_busy
        )
        return True

    def _show_local_line(self, settings: Settings) -> None:
        if set_up_by_airgrabber(settings):
            self._local_line.grid(
                row=self._local_row, column=0, sticky="w", padx=(_INDENT, 0), pady=(2, 0)
            )
        else:
            self._local_line.grid_remove()

    def _show_download_choice(self, updated: Settings) -> None:
        """A download choice and remote program stored elsewhere (qBittorrent's setup pointed
        AirGrabber at its Web UI, an Undo): the choice, its rows and the form follow."""
        self._show_local_line(updated)
        own = set_up_by_airgrabber(updated)
        if not own:
            self._local_torrent.grid_remove()
        if self._mode.get() != updated.download_mode.value:
            self._mode.set(updated.download_mode.value)
            self._form_in_folder = False
            self._show_download_rows()
        elif (
            own
            and updated.download_mode is DownloadMode.REMOTE
            and not self._local_torrent.winfo_manager()
        ):
            # Pointed at AirGrabber's own qBittorrent by the form, an Undo or its setup: its
            # status shows (and the Overview's card says it) without a visit to the page.
            self._show_download_rows()
        self._remote_form.show_stored(updated)
        self._refresh_folder_note()

    def _mode_changed(self) -> None:
        """A download choice clicked: its rows show, and it is stored."""
        self._form_in_folder = False
        self._show_download_rows()
        mode = DownloadMode(self._mode.get())
        self._commit(self._mode_boxes[mode], download_mode=mode)

    def _remote_changed(self, changes: dict[str, Any]) -> asyncio.Task[bool]:
        """A field of the remote form changed: stored on its own (said in the box it is in)."""
        in_folder = self._form_in_folder and DownloadMode(self._mode.get()) is DownloadMode.FOLDER
        return self._commit(self._folder_box if in_folder else self._remote_box, **changes)

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
        # What the remote form holds: its fields store themselves as they change.
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
            self._finished_field.commit_now()

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
            self._download_dir.set(str(Path(chosen)))  # Windows separators, as stored
            self._download_dir_field.commit_now()

    def _browse_films(self) -> None:
        # Native modal dialog, as `_browse`; it starts in the films folder, else the one above.
        start = self._movie_dir.get() or self._download_dir.get()
        chosen = filedialog.askdirectory(parent=self, initialdir=start or None)
        if chosen:
            self._movie_dir.delete(0, "end")
            self._movie_dir.insert(0, str(Path(chosen)))  # Windows separators, as stored
            self._movie_dir_field.commit_now()

    # -- Quality & sources ---------------------------------------------------------------------

    def _build_quality(self, page: _Page) -> bool:
        self._size_fields: dict[str, TextCommit] = {}
        for title, text, quality, low, high, kind in (
            (
                SERIES_TITLE,
                SERIES_TEXT,
                self._quality_tv,
                self._size_tv_min,
                self._size_tv_max,
                "tv",
            ),
            (
                MOVIES_TITLE,
                MOVIES_TEXT,
                self._quality_movie,
                self._size_movie_min,
                self._size_movie_max,
                "movie",
            ),
        ):
            box = page.box(t(title), t(text))
            line = box.frame_row()
            names = [quality_name(profile) for profile in QUALITY_NAMES]
            self._option_menu(
                line,
                quality,
                names,
                _QUALITY_MENU_WIDTH,
                command=functools.partial(self._quality_chosen, box, f"quality_{kind}"),
            )
            self._word(line, t("between")).pack(side="left", padx=(16, 8))
            low_entry = self._entry(line, low, width=_SIZE_ENTRY_WIDTH)
            low_entry.pack(side="left")
            self._word(line, t("and")).pack(side="left", padx=8)
            high_entry = self._entry(line, high, width=_SIZE_ENTRY_WIDTH)
            high_entry.pack(side="left")
            self._word(line, "GB").pack(side="left", padx=(8, 0))
            # The min and the max are one change: the model checks them together.
            field = TextCommit(
                [low_entry, high_entry],
                stored=functools.partial(self._stored_sizes, kind),
                commit=functools.partial(self._commit_sizes, box, kind),
            )
            self._size_fields[kind] = self.committer.watch(field)
            self._texts.append(field)
        page.add(
            text_label(
                page.frame,
                t(QUALITY_NOTE),
                size=_TEXT_SIZE,
                color=theme.TEXT_MUTED,
                wraplength=page_width(),
            ),
            gap=_BOX_GAP,
        )
        return True

    def _stored_sizes(self, kind: str) -> list[str]:
        current = self._settings.current
        return [
            size_text(getattr(current, f"size_{kind}_min_gb")),
            size_text(getattr(current, f"size_{kind}_max_gb")),
        ]

    def _commit_sizes(self, box: _Box, kind: str, texts: list[str]) -> asyncio.Task[bool]:
        """A quality's sizes, the min and the max together (as typed: the model reads them)."""
        low, high = texts
        return self._commit(box, **{f"size_{kind}_min_gb": low, f"size_{kind}_max_gb": high})

    def _quality_chosen(self, box: _Box, field: str, name: str) -> None:
        self._commit(box, **{field: quality_from_name(name)})

    def _build_sites(self, page: _Page) -> bool:
        """The built-in sites' ticks and the Prowlarr panel, in one box."""
        sites = page.box(t(SITES_TITLE), t(SITES_TEXT))
        self._sites_box = sites
        boxes = sites.frame_row()
        for column, (name, label) in enumerate(_BUILT_IN_SITES):
            self._checkbox(boxes, label, self._sources[name], self._sites_changed).grid(
                row=0, column=column, sticky="w", padx=(0, 20)
            )
        self._build_prowlarr(sites)
        return True

    def _sites_changed(self) -> None:
        """A site's tick or "Use Prowlarr": the sites AirGrabber skips, as one setting (the ids
        this version doesn't know kept)."""
        disabled = disabled_indexers(
            {name: var.get() for name, var in self._sources.items()},
            use_prowlarr=self._use_prowlarr.get(),
            unknown=self._unknown_disabled,
        )
        self._commit(self._sites_box, disabled_indexers=disabled)

    def _build_skip(self, page: _Page) -> bool:
        """The words to skip (a text field), and the release groups to prefer and never to use
        as chips: each add or × stores the list as typing it did (names separated with commas:
        the same tuple, in lower case)."""
        box = page.box(t(EXCLUDED_TITLE), t(EXCLUDED_TEXT))
        entry = self._entry(box.body, self._excluded)
        box.add(entry)
        self._word_fields: dict[str, TextCommit] = {
            "excluded_keywords": self._text_field(
                entry,
                stored=functools.partial(self._stored_words, "excluded_keywords"),
                commit=functools.partial(self._commit_text, box, "excluded_keywords"),
            )
        }
        self.group_chips: dict[str, ChipList] = {}
        for title, text, field in (
            (PREFERRED_TITLE, PREFERRED_TEXT, "preferred_groups"),
            (BLOCKED_TITLE, BLOCKED_TEXT, "blocked_groups"),
        ):
            groups = page.box(t(title), t(text))
            chips = ChipList(
                groups.body,
                names=getattr(self._settings.current, field),
                on_change=functools.partial(self._groups_changed, groups, field),
                width=box_inner(),
                committer=self.committer,
            )
            groups.add(chips, gap=10, sticky="ew")
            self.group_chips[field] = chips
            learned = self._settings.current.learned_groups
            if field == "preferred_groups" and learned:
                line = groups.text(t(LEARNED_LINE, groups=", ".join(learned)))
                groups.add(line, gap=8, sticky="w")
                self.learned_line = line
        return True

    def _groups_changed(self, box: _Box, field: str, names: list[str]) -> asyncio.Task[bool]:
        """A chip added or taken away: the list, stored as one text (the model splits it)."""
        return self._commit(box, **{field: ", ".join(names)})

    def _stored_words(self, field: str) -> str:
        return ", ".join(getattr(self._settings.current, field))

    def _commit_text(self, box: _Box, field: str, text: str) -> asyncio.Task[bool]:
        return self._commit(box, **{field: text})

    # -- Automatic -----------------------------------------------------------------------------

    def _build_automatic(self, page: _Page) -> bool:
        new = page.box(t(NEW_TITLE), t(NEW_TEXT))
        new.add(
            self._tick(new, t(NEW_SHOWS_SWITCH), self._auto_new_shows, "auto_download_new_shows"),
            sticky="w",
        )
        new.add(
            self._tick(new, t(NEW_FILMS_SWITCH), self._auto_new_films, "auto_download_new_films"),
            gap=8,
            sticky="w",
        )
        wait = page.box(t(WAIT_TITLE), t(WAIT_TEXT))
        hours = [t(WAIT_HOURS, n=n) for n in AUTO_WAIT_CHOICES]
        wait.add(
            self._segmented(
                wait.body,
                hours,
                self._auto_wait,
                width=320,
                command=lambda _name: self._commit(wait, auto_wait_hours=self._wait_hours()),
            ),
            sticky="w",
        )
        stuck = page.box(t(STUCK_TITLE), t(STUCK_TEXT))
        names = [stuck_name(n) for n in STUCK_CHOICES]
        stuck.add(
            self._segmented(
                stuck.body,
                names,
                self._stuck_swap,
                width=340,
                command=lambda _name: self._commit(stuck, stuck_swap_hours=self._stuck_hours()),
            ),
            sticky="w",
        )
        return True

    # -- Windows -------------------------------------------------------------------------------

    def _build_background(self, page: _Page) -> bool:
        background = page.box(t(BACKGROUND_TITLE), t(BACKGROUND_TEXT))
        self._background_box = background
        background.add(
            self._tick(background, t(TRAY_TEXT), self._keep_in_tray, "keep_in_tray"),
            sticky="w",
        )
        background.add(
            self._switch(
                background.body,
                t(START_TEXT),
                self._start_with_windows,
                lambda: spawn(
                    self.commit_start_with_windows(self._start_with_windows.get()),
                    name="settings-startup",
                ),
            ),
            gap=8,
            sticky="w",
        )
        return True

    async def commit_start_with_windows(self, on: bool) -> bool:
        """Start with Windows: the Startup shortcut first, then the setting stores its real
        state. A shortcut that couldn't be changed is said in the box, and the tick goes back."""
        box = self._background_box

        async def set_to(wanted: bool) -> bool:
            shown = self._startup.is_enabled()
            result = await apply_start_with_windows(wanted, shown, self._startup)
            changed = result.enabled != shown
            if self._settings.current.start_with_windows != result.enabled:
                await self._settings.update(start_with_windows=result.enabled)
                changed = True
            if result.error is not None:
                raise Refused(result.error)
            return changed

        before = self._startup.is_enabled()
        stored = await self.committer.commit(
            lambda: set_to(on), lambda: set_to(before), box=box
        )
        if self.winfo_exists():
            self._start_with_windows.set(self._startup.is_enabled())
        return stored

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

    # -- Accounts & keys -----------------------------------------------------------------------

    def _build_tmdb(self, page: _Page) -> bool:
        """TMDB's key (Check, the guide) and where the information comes from."""
        box = page.box(t(TMDB_TITLE), t(TMDB_KEY_TEXT))
        line = box.frame_row()
        self._tmdb_entry = self._entry(line, self._tmdb_key, show="•")
        self._tmdb_entry.pack(side="left", fill="x", expand=True)
        # Stored on Enter or leaving the field, unchecked (Check stays optional); Enter checks.
        self._tmdb_field = self._text_field(
            self._tmdb_entry,
            stored=lambda: self._settings.current.tmdb_api_key.get_secret_value(),
            commit=lambda text: self._commit(box, tmdb_api_key=text),
        )
        self._tmdb_entry.bind("<Return>", lambda _event: self._check_tmdb_key(), add="+")
        self._tmdb_check = primary_button(
            line, t("Check"), self._check_tmdb_key, width=_CHECK_BUTTON_WIDTH
        )
        self._tmdb_check.pack(side="left", padx=(10, 0))
        result = box.frame_row(gap=8)
        self._tmdb_result = text_label(result, "", size=_TEXT_SIZE, wraplength=box_inner() - 200)
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
        box.add(box.text(t(SOURCES_TEXT)), sticky="w")
        return True

    def _build_plex_account(self, page: _Page) -> bool:
        """Whether Plex is connected, and the way to Plex & library (with Plex only)."""
        if self._plex is None:
            return False
        self.plex_account = PlexAccountBox(
            page, self._plex, on_open=lambda: self.show_section(PAGE_PLEX)
        )
        return True

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

    def _build_looks(self, page: _Page) -> bool:
        looks = page.box(t(THEME_TITLE), t(THEME_TEXT))
        themes = [theme_name(mode) for mode in THEME_NAMES]
        looks.add(
            self._segmented(
                looks.body,
                themes,
                self._theme_mode,
                width=360,
                command=lambda name: self._commit(looks, theme_mode=theme_from_name(name)),
            ),
            sticky="w",
        )
        self._build_accent(page)
        self._build_text_size(page)
        cards = page.box(t(CARD_SIZE_LABEL), t(CARD_SIZE_TIP))
        cards.add(
            self._segmented(
                cards.body,
                [CARD_SIZE_LABELS[size] for size in CardSize],
                self._card_size,
                command=lambda name: self._commit(cards, card_size=size_from_label(name)),
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
        return True

    # -- Pages ---------------------------------------------------------------------------------

    def _build_pages(self, page: _Page) -> bool:
        self._build_language(page)
        calendar = page.box(t(CALENDAR_TITLE), t(CALENDAR_LAYOUT_TEXT))
        row = calendar.frame_row()
        names = [t(name) for name in CALENDAR_LAYOUT_NAMES.values()]
        self._segmented(
            row,
            names,
            self._calendar_layout,
            command=lambda _name: self._commit(
                calendar, calendar_layout=self._chosen_calendar_layout()
            ),
        ).pack(side="left")
        self._layout_picture = CalendarLayoutPicture(row, self._chosen_calendar_layout())
        self._layout_picture.pack(side="left", padx=(16, 0))
        self._trace(
            self._calendar_layout,
            lambda *_args: self._layout_picture.show(self._chosen_calendar_layout()),
        )
        week = calendar.frame_row()
        self._word(week, t(WEEK_TITLE)).pack(side="left", padx=(0, 12))
        self._option_menu(
            week,
            self._first_day,
            [weekday_name(day) for day in Weekday],
            command=lambda name: self._commit(calendar, first_day=weekday_from_name(name)),
        )
        layout = page.box(t(INFO_LAYOUT_TITLE), t(INFO_LAYOUT_TEXT))
        layouts = [choice_name(INFO_LAYOUT_NAMES, value) for value in INFO_LAYOUT_NAMES]
        layout.add(
            self._segmented(
                layout.body,
                layouts,
                self._info_layout,
                command=lambda name: self._commit(
                    layout, info_layout=choice_from_name(INFO_LAYOUT_NAMES, name, InfoLayout.SIDE)
                ),
            ),
            sticky="w",
        )
        spotlight = page.box(t(SPOTLIGHT_TITLE))
        spotlight.add(
            self._switch(
                spotlight.body,
                t(SPOTLIGHT_SWITCH),
                self._spotlight_motion,
                lambda: self._commit(spotlight, spotlight_motion=self._spotlight_motion.get()),
            ),
            sticky="w",
        )
        return True

    def _chosen_calendar_layout(self) -> CalendarLayout:
        return choice_from_name(
            CALENDAR_LAYOUT_NAMES, self._calendar_layout.get(), CalendarLayout.WEEKS
        )

    def _build_text_size(self, page: _Page) -> None:
        """Normal / Large / Larger, with "Applies after a restart" while another than this run's
        is chosen."""
        box = page.box(t(TEXT_SIZE_TITLE), t(TEXT_SIZE_TEXT))
        names = [t(name) for name in TEXT_SIZE_NAMES.values()]

        def chosen(_name: str) -> None:
            self._text_size_changed()
            self._commit(box, text_size=self.text_size)

        box.add(
            self._segmented(box.body, names, self._text_size, width=300, command=chosen),
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
        """The Accent colour box: a swatch per palette, the chosen one ringed. A click stores
        it; another colour than the one running shows "Applies after a restart" with Restart
        now."""
        box = page.box(t(ACCENT_TITLE), t(ACCENT_TEXT))

        def chosen(accent: Accent) -> None:
            self._accent_changed()
            self._commit(box, accent=accent)

        self._accent_picker = AccentSwatches(
            box.body,
            value=self._settings.current.accent,
            background=theme.TAB_BG,
            command=chosen,
        )
        box.add(self._accent_picker, sticky="w")
        self._accent_restart_row = box.frame_row()
        text_label(
            self._accent_restart_row,
            t(ACCENT_RESTART_TEXT),
            size=_TEXT_SIZE,
            color=theme.WARNING,
            wraplength=box_inner() - _RESTART_BUTTON_WIDTH - 12,
        ).pack(side="left", fill="x", expand=True)
        if self._on_restart is not None:
            primary_button(
                self._accent_restart_row,
                t("Restart now"),
                self._restart_now,
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
        self._card_preview.show(chosen, chosen)  # stored at once: the chosen size is the saved one

    async def _preview_show(self, library: LibraryService) -> None:
        """The first followed show with a poster for the preview card (its poster is most
        likely in the poster cache already)."""
        entries = await library.tracked(profile_id=self._settings.current.current_profile)
        show = next((entry.show for entry in entries if entry.show.poster_url), None)
        if show is not None and self._card_preview is not None and self.winfo_exists():
            self._card_preview.use_show(show)

    # -- Quick setup ---------------------------------------------------------------------------

    def _open_quick_setup(self) -> None:
        """Quick setup on its own over Settings; what it stores shows here at once (the
        settings' listener, `_show_stored`)."""
        if self._quick_dialog is not None and self._quick_dialog.winfo_exists():
            self._quick_dialog.lift()
            return
        restart = self._restart_now if self._on_restart is not None else None
        self._quick_dialog = QuickSetupDialog(
            self, settings=self._settings, on_saved=self._quick_saved, on_restart=restart
        )

    def _quick_saved(self) -> None:
        """Show what Quick setup stored (look, accent colour, quality, card size, language)."""
        if self.winfo_exists():
            self._show_stored(self._settings.current)

    def _build_language(self, page: _Page) -> None:
        """The Language box: English / Nederlands, each in its own language. A click stores it;
        another language than the one running shows "Restart AirGrabber to switch the language"
        with Restart now (it applies at the next start)."""
        box = page.box(t(LANGUAGE_TITLE), t(LANGUAGE_TEXT))
        names = [language.native_name for language in Language]

        def chosen(name: str) -> None:
            self._language_changed()
            self._commit(box, language=language_from_name(name))

        box.add(
            self._segmented(box.body, names, self._language, width=260, command=chosen),
            sticky="w",
        )
        self._restart_row = box.frame_row()
        text_label(
            self._restart_row,
            t(RESTART_TEXT),
            size=_TEXT_SIZE,
            color=theme.WARNING,
            wraplength=box_inner() - _RESTART_BUTTON_WIDTH - 12,
        ).pack(side="left", fill="x", expand=True)
        if self._on_restart is not None:
            self._restart_button = primary_button(
                self._restart_row,
                t("Restart now"),
                self._restart_now,
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

    # -- Updates & backup ----------------------------------------------------------------------

    def _build_version(self, page: _Page) -> bool:
        """"Version …" with the automatic updates' tick and Check for updates; its heading is
        the hidden phone switch."""
        check = page.box(
            t("Version {version}", version=self._updates.current), t(AUTO_UPDATE_TEXT)
        )
        check.add(self._tick(check, t(AUTO_UPDATE_SWITCH), self._auto_update, "auto_update"), sticky="w")
        line = check.frame_row()
        self._update_button = primary_button(
            line, t("Check for updates"), self._check_updates, width=_UPDATE_BUTTON_WIDTH
        )
        self._update_button.pack(side="left", anchor="n")
        self._update_result = text_label(
            line, "", size=SMALL_SIZE, wraplength=box_inner() - _UPDATE_BUTTON_WIDTH - 12
        )
        self._update_result.pack(side="left", padx=(12, 0), fill="x", expand=True)
        self._version_clicks: list[float] = []
        if self._phone is not None and self._phone.can_switch and check.heading is not None:
            check.heading.bind("<Button-1>", lambda _event: self.version_clicked(), add="+")
        return True

    def _build_go_back(self, page: _Page) -> bool:
        if self._go_back_version is None:
            return False
        back = page.box(t(GO_BACK_TITLE), t(GO_BACK_TEXT))
        row = back.frame_row()
        self.go_back_button = secondary_button(
            row,
            t("Go back to version {version}", version=self._go_back_version),
            self._ask_go_back,
            width=_GO_BACK_BUTTON_WIDTH,
        )
        self.go_back_button.pack(side="left")
        return True

    def _build_report(self, page: _Page) -> bool:
        if self._problem_reports is None:
            return False
        report = page.box(t(REPORT_TITLE), t(REPORT_TEXT))
        row = report.frame_row()
        self.report_button = secondary_button(
            row, t(REPORT_BUTTON), self._make_report, width=_GO_BACK_BUTTON_WIDTH
        )
        self.report_button.pack(side="left", anchor="n")
        self.report_result = text_label(
            row, "", size=SMALL_SIZE, wraplength=box_inner() - _GO_BACK_BUTTON_WIDTH - 12
        )
        self.report_result.pack(side="left", padx=(12, 0), fill="x", expand=True)
        return True

    def _make_report(self) -> None:
        if self.report_button is not None:
            self.report_button.configure(state="disabled")
        spawn(self._save_report(), name="problem-report")

    async def _save_report(self) -> None:
        """The report file, then Explorer with it selected."""
        reports = self._problem_reports
        assert reports is not None
        try:
            path, on_desktop = await reports.save()
        except OSError as exc:
            log.warning("The problem report couldn't be made (%s)", type(exc).__name__)
            text, colour = t(REPORT_FAILED), theme.DANGER
        else:
            from app.forge.ui.file_browser import open_explorer

            open_explorer(path, select=True)
            text = t(REPORT_ON_DESKTOP if on_desktop else REPORT_IN_FOLDER)
            colour = theme.TEXT_MUTED
        if not self.winfo_exists():
            return
        if self.report_result is not None:
            self.report_result.configure(text=text, text_color=colour)
        if self.report_button is not None:
            self.report_button.configure(state="normal")

    def version_clicked(self, now: float | None = None) -> None:
        """One click on "Version …": the SWITCH_CLICKSth quick one switches the phone features."""
        now = time.monotonic() if now is None else now
        clicks = self._version_clicks
        if clicks and now - clicks[-1] > SWITCH_GAP_S:
            clicks.clear()
        clicks.append(now)
        if len(clicks) >= SWITCH_CLICKS:
            clicks.clear()
            spawn(self._switch_phone(), name="phone-switch")

    async def _switch_phone(self) -> None:
        phone = self._phone
        if phone is None:
            return
        on = not phone.switched_on_here()
        try:
            await phone.switch_here(on)
        except OSError as exc:
            log.warning("The phone features couldn't be switched (%s)", type(exc).__name__)
            if self.winfo_exists():
                self._update_result.configure(text=t(PHONE_SWITCH_FAILED), text_color=theme.DANGER)
            return
        if not self.winfo_exists():
            return
        # On: the phone app's download code with it (app/ui/phone_app.py).
        self.phone_switch_dialog = PhoneAppDialog(
            self,
            title=t(PHONE_ON_TITLE if on else PHONE_OFF_TITLE),
            note=t(PHONE_SWITCH_TEXT),
            show_code=on,
            on_restart=self._restart_now if self._on_restart is not None else None,
        )

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

    def _build_backup(self, page: _Page) -> bool:
        if self._backup is None:
            return False
        box = page.box(t(BACKUP_TITLE), t(BACKUP_TEXT))
        line = box.frame_row()
        secondary_button(
            line, t(SAVE_BACKUP_BUTTON), self._save_backup, width=_BACKUP_BUTTON_WIDTH
        ).pack(side="left")
        secondary_button(
            line, t(LOAD_BACKUP_BUTTON), self._load_backup, width=_BACKUP_BUTTON_WIDTH
        ).pack(side="left", padx=(12, 0))
        return True

    # -- Plex & library, Playing, Phone connection ---------------------------------------------

    def _build_plex(self, page: _Page) -> bool:
        """The Plex box (app/ui/plex_settings.py), which acts through its own service; its "Play
        in" and "Explorer" boxes go on Playing."""
        if self._plex is None:
            return False
        self.plex_box = PlexBox(
            page,
            self._plex,
            settings=self._settings,
            folders=self._folder_library,
            play_page=self._made[PAGE_PLAYING],
            committer=self.committer,
        )
        return True

    def _build_folders_line(self, page: _Page) -> bool:
        """The line about the user's own folders while they count, with the way to Folders."""
        if self._folder_library is None:
            return False
        self.folders_line = FoldersLine(
            page,
            self._folder_library,
            self._settings,
            on_open=lambda: self._show_section(PAGE_FOLDERS),
        )
        return True

    def _build_library_compare(self, page: _Page) -> bool:
        """What AirGrabber does with and without a library (app/ui/plex_compare.py)."""
        if self._plex is None and self._folder_library is None:
            return False
        phone = self._phone is not None and self._phone.available
        self.plex_compare = PlexCompareBox(page, phone=phone)
        return True

    def _build_playing(self, _page: _Page) -> bool:
        """Playing's boxes come with the Plex box (`_build_plex`)."""
        return self.plex_box is not None and self.plex_box.play_box is not None

    def _build_phone(self, page: _Page) -> bool:
        """The phone connection's boxes (app/ui/phone_settings.py); they act through the phone
        service."""
        phone = self._phone
        if phone is None or not phone.available:
            return False
        self.phone_box = PhoneBox(page, phone)
        return True

    def _backup_dialog_open(self) -> bool:
        dialog = self._backup_dialog
        if dialog is not None and dialog.winfo_exists():
            dialog.lift()
            return True
        return False

    def _save_backup(self) -> None:
        """Save a backup of what is stored."""
        if self._backup is not None and not self._backup_dialog_open():
            self._backup_dialog = SaveBackupDialog(self, backup=self._backup)

    def _load_backup(self) -> None:
        if self._backup is not None and not self._backup_dialog_open():
            self._backup_dialog = load_backup(
                self, backup=self._backup, on_restart=self._on_restart, on_done=self._backup_loaded
            )

    def _backup_loaded(self, result: BackupLoadResult | None) -> None:
        """A load that replaced the settings closes Settings: its pages were built from the old
        ones (and MediaForge's parts may hold the old lists)."""
        if result is not None and result.settings_replaced and self.winfo_exists():
            self.destroy()

    # -- Prowlarr panel ------------------------------------------------------------------------

    def _build_prowlarr(self, box: _Box) -> None:
        """The Prowlarr panel, in the Sites box under the built-in sites."""
        url = self._settings.current.prowlarr_url
        panel = ProwlarrPanel(
            box.body,
            prowlarr=self._prowlarr,
            use=self._use_prowlarr,
            checkbox=lambda master, text, variable: self._checkbox(
                master, text, variable, self._sites_changed
            ),
            width=box_inner(),
            label_width=_PANEL_LABEL_WIDTH,
            expanded=shows_remote_fields(url),
        )
        _paint_background(panel, theme.TAB_BG)  # the box's own
        box.add(panel, gap=_BOX_GAP)
        self._prowlarr_panel = panel
        self._prowlarr_url = self._hinted_entry(
            panel.remote_row("URL"), url, "http://192.168.1.x:9696"
        )
        self._prowlarr_url_field = self._text_field(
            self._prowlarr_url,
            stored=lambda: self._settings.current.prowlarr_url,
            commit=lambda text: self._commit(box, prowlarr_url=text),
        )
        key = self._entry(panel.remote_row(t("API key")), self._prowlarr_key, show="•")
        key.pack(fill="x")
        self._prowlarr_key_field = self._text_field(
            key,
            stored=lambda: self._settings.current.prowlarr_api_key.get_secret_value(),
            commit=lambda text: self._commit(box, prowlarr_api_key=text),
        )
        self._prowlarr_button, self._prowlarr_result = self._test_row(
            panel.remote_row(""),
            lambda: self._start_test("prowlarr", self._test_prowlarr),
            field_width=_panel_field_width(),
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
        command: Callable[[str], object] | None = None,
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
        self,
        master: Any,
        variable: ctk.StringVar,
        values: list[str],
        width: int = 260,
        *,
        command: Callable[[str], object] | None = None,
    ) -> None:
        ctk.CTkOptionMenu(
            master,
            variable=variable,
            values=values,
            command=command,
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

    def _checkbox(
        self,
        master: Any,
        text: str,
        variable: ctk.BooleanVar,
        command: Callable[[], object] | None = None,
    ) -> ctk.CTkCheckBox:
        return ctk.CTkCheckBox(
            master,
            text=text,
            variable=variable,
            command=command,
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

    @staticmethod
    def _switch(
        master: Any,
        text: str,
        variable: ctk.BooleanVar,
        command: Callable[[], object] | None = None,
    ) -> ctk.CTkSwitch:
        """An on/off (spec §6: every on/off in Settings is a switch); its text wraps inside a
        box."""
        switch = ctk.CTkSwitch(
            master,
            text=text,
            variable=variable,
            command=command,
            height=BUTTON_HEIGHT,
            font=theme.font(DETAIL_SIZE),
            progress_color=theme.ACCENT,
            text_color=theme.TEXT,
        )
        wrap_toggle(switch, box_inner())
        return switch

    def _tick(
        self, box: _Box, text: str, variable: ctk.BooleanVar, field: str
    ) -> ctk.CTkSwitch:
        """A switch in `box` that stores `field` at once."""
        return self._switch(
            box.body, text, variable, lambda: self._commit(box, **{field: bool(variable.get())})
        )

    def _text_field(
        self,
        entry: ctk.CTkEntry,
        *,
        stored: Callable[[], str],
        commit: Callable[[str], object],
        strip: bool = True,
    ) -> TextCommit:
        """A text field stored on Enter or when the keyboard leaves it (only when changed)."""
        field = text_commit(entry, stored=stored, commit=commit, strip=strip)
        self._texts.append(field)
        return self.committer.watch(field)

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
        choice, with the program's button, the magnet note and its form), at most
        MAX_PAGE_HEIGHT (app/ui/settings_layout.py), so it doesn't change between pages or
        choices; on a short screen at the screen's height instead, the pages scrolling. With
        MediaForge's parts, which aren't built yet, at MAX_PAGE_HEIGHT."""
        self._torrent.show(TorrentStatus.SETUP_NEEDED)  # its tallest: the line and a button
        content = 0
        for section, page in self._pages.items():
            self._grid_page(section)
            modes: list[DownloadMode | None] = (
                list(DownloadMode) if section == PAGE_TORRENT else [None]
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
        height = MAX_PAGE_HEIGHT if self._forge_parts else page_height(content, scaling)
        self._built_width = fit_window(self, self._scroll, height)

    def _on_configure(self, event: tk.Event[Any]) -> None:
        """The window's size changed (dragged by an edge): its texts follow once it rests."""
        if event.widget is not self or not self._built_width:
            return
        if self._reflow_job is not None:
            self.after_cancel(self._reflow_job)
        self._reflow_job = self.after(_REFLOW_WAIT_MS, self.reflow)

    def reflow(self) -> None:
        """Every page's wrapping texts get the room the window has beyond its first width
        (app/ui/reflow.py)."""
        self._reflow_job = None
        if not self._built_width or not self.winfo_exists():
            return
        scaling = ctk.ScalingTracker.get_window_scaling(self)
        extra = round(self.winfo_width() / scaling) - self._built_width
        follow_width(self._scroll, extra)

    # -- storing ---------------------------------------------------------------------------------

    def _commit(self, box: _Box, **fields: Any) -> asyncio.Task[bool]:
        """Store `fields` as one change (started now: the window may close meanwhile)."""
        return spawn(self.commit_settings(box, **fields), name="settings-commit")

    async def commit_settings(self, box: _Box, **fields: Any) -> bool:
        """Store `fields` of AirGrabber's settings as one change: False (said in `box`, nothing
        stored) when refused. Undo stores what they were."""
        return await commit_fields(self.committer, self._settings, box, **fields)

    def _settings_changed(self, updated: Settings) -> None:
        if self.winfo_exists():
            self._show_stored(updated)
            self.refresh_overview()

    def _show_stored(self, current: Settings) -> None:
        """Show what is stored now in every control (a change made here, elsewhere or undone);
        a text field being edited keeps what it holds."""
        self._show_download_choice(current)
        variables: list[tuple[Any, Any]] = [
            (self._quality_tv, quality_name(current.quality_tv)),
            (self._quality_movie, quality_name(current.quality_movie)),
            (self._first_day, weekday_name(current.first_day)),
            (self._calendar_layout, choice_name(CALENDAR_LAYOUT_NAMES, current.calendar_layout)),
            (self._card_size, CARD_SIZE_LABELS[current.card_size]),
            (self._theme_mode, theme_name(current.theme_mode)),
            (self._info_layout, choice_name(INFO_LAYOUT_NAMES, current.info_layout)),
            (self._spotlight_motion, current.spotlight_motion),
            (self._text_size, t(TEXT_SIZE_NAMES[current.text_size])),
            (self._language, current.language.native_name),
            (self._auto_update, current.auto_update),
            (self._keep_in_tray, current.keep_in_tray),
            (self._start_with_windows, self._startup.is_enabled()),  # the real shortcut
            (self._auto_new_shows, current.auto_download_new_shows),
            (self._auto_new_films, current.auto_download_new_films),
            (self._auto_wait, t(WAIT_HOURS, n=current.auto_wait_hours)),
            (self._stuck_swap, stuck_name(current.stuck_swap_hours)),
        ]
        disabled = set(current.disabled_indexers)
        variables += [(var, name not in disabled) for name, var in self._sources.items()]
        variables.append((self._use_prowlarr, ProwlarrIndexer.name not in disabled))
        for variable, value in variables:
            if variable.get() != value:
                variable.set(value)
        known = {name for name, _label in _BUILT_IN_SITES} | {ProwlarrIndexer.name}
        self._unknown_disabled = tuple(n for n in current.disabled_indexers if n not in known)
        if self._accent_picker is not None and self._accent_picker.value is not current.accent:
            self._accent_picker.set(current.accent)
        self._accent_changed()
        self._text_size_changed()
        self._language_changed()
        texts: list[tuple[TextCommit, list[str]]] = [
            (self._download_dir_field, [str(current.download_dir)]),
            (self._movie_dir_field, [_folder_text(current.movie_download_dir)]),
            (self._finished_field, [current.subtitles_folder]),
            (self._tmdb_field, [current.tmdb_api_key.get_secret_value()]),
            (self._prowlarr_url_field, [current.prowlarr_url]),
            (self._prowlarr_key_field, [current.prowlarr_api_key.get_secret_value()]),
            (self._size_fields["tv"], self._stored_sizes("tv")),
            (self._size_fields["movie"], self._stored_sizes("movie")),
        ]
        for field, values in texts:
            field.show(values)
        self._word_fields["excluded_keywords"].show([self._stored_words("excluded_keywords")])
        for name, chips in self.group_chips.items():
            chips.show(getattr(current, name))
        if self._card_preview is not None:
            self._card_preview.show(current.card_size, current.card_size)

    # -- testing ----------------------------------------------------------------------------------

    async def _test_remote(self) -> str:
        """The remote form's Test connection: the program as the form holds it, nothing stored
        (each field stores itself as it changes). Raises ValidationError, RemoteError."""
        form = Settings.model_validate(
            {**self._settings.current.model_dump(), **self._remote_form.changes()}
        )
        return await self._remote.test(form)

    async def _test_prowlarr(self) -> str:
        """Prowlarr's Test connection: the URL and key as the fields hold them, nothing stored.
        Raises ValidationError, IndexerError."""
        form = Settings.model_validate(
            {
                **self._settings.current.model_dump(),
                "prowlarr_url": self._prowlarr_url.get(),
                "prowlarr_api_key": self._prowlarr_key.get(),
            }
        )
        return await self._search.test_prowlarr(
            form.prowlarr_url, form.prowlarr_api_key.get_secret_value()
        )

    def _start_test(self, kind: str, probe: Callable[[], Awaitable[str]]) -> None:
        running = self._tests.get(kind)
        if running is not None and not running.done():
            return
        button, result = self._prowlarr_button, self._prowlarr_result
        result.configure(text=t(TEST_ONLY_TEXT), text_color=theme.TEXT_MUTED)
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

    def _restart_now(self) -> None:
        """The Language and Accent colour boxes' Restart now: AirGrabber restarts (and this
        window closes with the main window). Everything is stored already."""
        if self._on_restart is not None:
            self._on_restart()

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

    def close(self) -> None:
        """✕ and Escape: what a text field holds unstored is stored (as leaving it would), then
        the window closes. When a text is refused then, the window stays on that box's page,
        its red line saying why; ✕ / Escape again closes without storing that text (and a ✕
        while the texts are still being stored closes at once)."""
        if not self.winfo_exists():
            return
        if self._closing is not None:
            self.destroy()
            return
        started = self.committer.flush(skip=self._refused_at_close)
        if not started:
            self.destroy()
            return
        self._closing = spawn(self._close_once_stored(started), name="settings-close")

    def close_now(self) -> None:
        """Close at once (an update or going back restarts AirGrabber, the main window goes to
        the tray): what a text field holds is stored meanwhile, a refusal unseen."""
        if self.winfo_exists():
            self.committer.flush()
            self.destroy()

    async def _close_once_stored(
        self, started: list[tuple[TextCommit, asyncio.Future[Any]]]
    ) -> None:
        try:
            await asyncio.wait([task for _field, task in started])
        finally:
            self._closing = None
        if not self.winfo_exists():
            return
        refused = [
            field
            for field, task in started
            if not task.cancelled() and task.exception() is None and task.result() is False
        ]
        if not refused:
            self.destroy()
            return
        # Added to the ones refused at an earlier close (not instead of them): two fields
        # refused in turn would otherwise keep the window from ever closing.
        self._refused_at_close.update({field: field.texts for field in refused})
        self._show_refused(refused[0])

    def _show_refused(self, field: TextCommit) -> None:
        """The page of the box `field` is in, scrolled to the box (its red line says why)."""
        entry = field.entries[0]
        for section, page in self._pages.items():
            for box in page.settings_boxes:
                if _inside(entry, box.frame):
                    self._show_section(section)
                    self._scroll_to_frame(box.frame)
                    entry.focus_set()
                    return

    def _trace(self, variable: tk.Variable, callback: Callable[..., object]) -> None:
        self._traces.append((variable, variable.trace_add("write", callback)))

    def destroy(self) -> None:
        job = getattr(self, "_reflow_job", None)
        if job is not None:
            self._reflow_job = None
            try:
                self.after_cancel(job)
            except tk.TclError:
                pass
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
        if self._closing is not None:  # closed meanwhile: what was being stored still lands
            self._closing.cancel()
            self._closing = None
        if self._overview_task is not None:
            self._overview_task.cancel()
        for stop in getattr(self, "_stop_overview", ()):
            stop()
        self._texts.clear()
        use_page_width(WIDE_PAGE_WIDTH)  # the next window decides its own
        super().destroy()
