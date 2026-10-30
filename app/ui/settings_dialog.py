"""Preferences dialog in plain words: a sidebar of sections (Downloading: where downloads go,
quality, automatic, subtitles; App: movie & show info, looks, updates, advanced) and one
scrolling page per section, its settings grouped in boxes that each say what they are for.
Every change goes through SettingsService, which validates first."""

from __future__ import annotations

import asyncio
import logging
import math
import tkinter as tk
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from tkinter import filedialog
from pathlib import Path
from typing import Any, NamedTuple, Protocol

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
    CardSize,
    DownloadMode,
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
    SubtitleService,
    TorrentAppService,
    TorrentSearchService,
    UpdateError,
    UpdateService,
)
from app.services import startup
from app.services.qbittorrent_local import set_up_by_airgrabber
from app.services.startup import StartupError
from app.services.tmdb import TMDBError
from app.ui import theme
from app.ui.branding import set_window_icon
from app.ui.card_preview import CardSizePreview
from app.ui.card_scale import CARD_SIZE_LABEL, CARD_SIZE_LABELS, CARD_SIZE_TIP, size_from_label
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
from app.ui.prowlarr_panel import PANEL_PAD_X, ProwlarrPanel, shows_remote_fields
from app.ui.quick_setup import OPEN_TEXT as QUICK_SETUP_TEXT
from app.ui.quick_setup import QuickSetupDialog
from app.ui.remote_program_form import FIELD_LABELS as REMOTE_LABELS
from app.ui.remote_program_form import FolderNote, RemoteProgramForm, folder_note
from app.ui.subtitle_settings import (
    ACCOUNT_HINT,
    ACCOUNT_LABEL,
    CREDIT_TEXT,
    CREDIT_URL,
    FOLDER_HINT,
    FOLDER_LABEL,
    OWN_KEY_LABEL,
    SIGN_UP_TEXT,
    SIGNUP_URL,
    SUBTITLES_NOTE,
    SWITCH_TEXT,
    UNAVAILABLE_TEXT,
    AccountRow,
    LanguageTags,
    OwnKeyRow,
    link,
    quota_line,
    subtitle_problem,
)
from app.ui.tasks import async_command, spawn
from app.ui.tmdb_key_guide import KeyCheck, TmdbKeyDialog, key_check_message
from app.ui.torrent_app_status import INSTALL_BUTTON_WIDTH, TorrentAppStatus, TorrentStatus

log = logging.getLogger(__name__)

# -- sections ----------------------------------------------------------------------------------
# A section's name is its key here and in the tests; the dialog shows it through t(), as every
# text constant below.

SECTION_WHERE = N_("Where downloads go")
SECTION_QUALITY = N_("Quality")
SECTION_AUTOMATIC = N_("Automatic")
SECTION_SUBTITLES = N_("Subtitles")
SECTION_INFO = N_("Movie & show info")
SECTION_LOOKS = N_("Looks")
SECTION_UPDATES = N_("Updates")
SECTION_ADVANCED = N_("Advanced")

# The sidebar: a small group label above each part of the list, then its sections in order.
SECTION_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (N_("Downloading"), (SECTION_WHERE, SECTION_QUALITY, SECTION_AUTOMATIC, SECTION_SUBTITLES)),
    (N_("App"), (SECTION_INFO, SECTION_LOOKS, SECTION_UPDATES, SECTION_ADVANCED)),
)
SECTIONS: tuple[str, ...] = tuple(name for _group, names in SECTION_GROUPS for name in names)
SECTION_ICONS: dict[str, str] = {
    SECTION_WHERE: "⬇",
    SECTION_QUALITY: "🎚",
    SECTION_AUTOMATIC: "⏱",
    SECTION_SUBTITLES: "💬",
    SECTION_INFO: "🎬",
    SECTION_LOOKS: "🖼",
    SECTION_UPDATES: "⟳",
    SECTION_ADVANCED: "🛠",
}
# Each page's one muted line under its heading (Subtitles uses its own texts).
SECTION_NOTES: dict[str, str] = {
    SECTION_WHERE: N_("What happens when you click Download."),
    SECTION_QUALITY: N_(
        "Which copy AirGrabber picks when you click Download, and for automatic downloads."
    ),
    SECTION_AUTOMATIC: N_("What AirGrabber does on its own, without you clicking anything."),
    SECTION_INFO: N_(
        "The Movies and Discover pages get their lists, posters and release dates from TMDB, "
        "a free film database."
    ),
    SECTION_LOOKS: N_("How AirGrabber's pages look."),
    SECTION_UPDATES: N_("Getting new versions of AirGrabber."),
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

SUBTITLE_SWITCH_TITLE = N_("Automatic subtitles")
SUBTITLE_SWITCH_TEXT = N_("AirGrabber looks for subtitles for what you download, every half hour.")
LANGUAGES_TITLE = N_("Languages")
LANGUAGES_TEXT = N_("AirGrabber looks for subtitles in each of these languages.")

TMDB_KEY_LABEL = N_("TMDB key")
TMDB_KEY_TEXT = N_(
    "A free code from themoviedb.org that lets AirGrabber ask TMDB for this information. "
    "Without it, the Movies and Discover pages stay empty. Your shows and the Calendar work "
    "without it."
)
KEY_GUIDE_LINK = N_("How do I get a free key?")

WEEK_TITLE = N_("Week starts on")
WEEK_TEXT = N_("The first day of each week on the Calendar.")

QUICK_SETUP_NOTE = N_("Look, download quality, card size and language, each with one click.")

THEME_TITLE = N_("Theme")
THEME_TEXT = N_("Light or dark. Follow Windows uses the same look as your other Windows apps.")

LANGUAGE_TITLE = N_("Language")
LANGUAGE_TEXT = N_("The language of AirGrabber's pages, buttons and messages.")
RESTART_TEXT = N_("Restart AirGrabber to switch the language")

AUTO_UPDATE_TITLE = N_("Automatic updates")
AUTO_UPDATE_TEXT = N_(
    "New versions download in the background and are installed when AirGrabber closes."
)
AUTO_UPDATE_SWITCH = N_("Install updates automatically")
CHECK_UPDATES_TEXT = N_("Check whether a newer version is out.")

SITES_TITLE = N_("Built-in sites")
SITES_TEXT = N_("Websites AirGrabber searches by itself.")
EXCLUDED_TITLE = N_("Skip releases with these words")
EXCLUDED_TEXT = N_("Separate with commas, for example: cam, telesync")
PREFERRED_TITLE = N_("Prefer releases from")
PREFERRED_TEXT = N_("Release group names, separated with commas")

# -- sizes (logical, unscaled CTk units) ---------------------------------------------------------

_SIDEBAR_WIDTH = 220  # at least; wider when a section's name in bold needs it
_SIDEBAR_PAD = 12
_ITEM_HEIGHT = 36
_ICON_WIDTH = 30
_ITEM_PAD = (8, 4, 10)  # left of the icon, between icon and name, right of the name
# The page column's content: the boxes' outer width. With the sidebar the dialog stays about
# 880 wide, so it fits a 1366 x 768 screen at 150 % (910 wide).
_PAGE_WIDTH = 580
_PAGE_PAD_LEFT = 22
_PAGE_PAD_RIGHT = 8  # the scrollbar has its own inset
_PAGE_TOP = 20
_PAGE_BOTTOM = 20  # under a page's last box
_MAX_PAGE_HEIGHT = 680  # a taller page scrolls; the dialog stays about as tall as it was
SCREEN_RESERVE = 80  # left free for the taskbar and the title bar
_HEADING_SIZE = 20
_BOX_RADIUS = 12  # as the Prowlarr panel's
_BOX_PAD_X = 18
_BOX_PAD_Y = 16
_BOX_GAP = 14  # between boxes
_FIRST_BOX_GAP = 18  # under a page's muted line
_TEXT_SIZE = SMALL_SIZE + 1  # a box's explanation
_BOX_INNER = _PAGE_WIDTH - 2 * (_BOX_PAD_X + 1)  # inside a box's border and padding
_CONTROL_GAP = 12  # between a box's explanation and its controls
_INDENT = 30  # under a radio button's text
_ROW_GAP = 14  # between two controls in one box
_TEST_BUTTON_WIDTH = 160
_UPDATE_BUTTON_WIDTH = 180
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
        settings.remote_verify_tls,
    )


def download_dir_to_save(mode: DownloadMode, entry: str, stored: Path) -> str | Path:
    """The folder entry only counts when it shows ("Save .torrent files to a folder");
    otherwise the stored folder stays, whatever the hidden entry holds."""
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


def dialog_height(wanted: int, screen_height: int, *, reserve: int = SCREEN_RESERVE) -> int:
    """The dialog's height (logical units): what its content wants, but never taller than the
    screen minus room for the taskbar and title bar (the pages scroll instead)."""
    return max(1, min(wanted, screen_height - reserve))


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
    "quality_tv": N_("Series quality"),
    "quality_movie": N_("Movies quality"),
    "size_tv_min_gb": N_("Series size"),
    "size_tv_max_gb": N_("Series size"),
    "size_movie_min_gb": N_("Movies size"),
    "size_movie_max_gb": N_("Movies size"),
    "first_day": WEEK_TITLE,
    "card_size": CARD_SIZE_LABEL,
    "theme_mode": THEME_TITLE,
    "language": LANGUAGE_TITLE,
    "tmdb_api_key": TMDB_KEY_LABEL,
    "auto_update": SECTION_UPDATES,
    "disabled_indexers": SITES_TITLE,
    "excluded_keywords": EXCLUDED_TITLE,
    "preferred_groups": PREFERRED_TITLE,
    "prowlarr_url": N_("Prowlarr URL"),
    "prowlarr_api_key": N_("Prowlarr API key"),
    "subtitle_languages": N_("Subtitle languages"),
    "subtitles_folder": FOLDER_LABEL,
    "opensubtitles_username": ACCOUNT_LABEL,
    "opensubtitles_password": ACCOUNT_LABEL,
    "opensubtitles_own_key": OWN_KEY_LABEL,
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
    "subtitles_enabled": SECTION_SUBTITLES,
    "subtitle_languages": SECTION_SUBTITLES,
    "subtitles_folder": SECTION_SUBTITLES,
    "opensubtitles_username": SECTION_SUBTITLES,
    "opensubtitles_password": SECTION_SUBTITLES,
    "opensubtitles_own_key": SECTION_SUBTITLES,
    "tmdb_api_key": SECTION_INFO,
    "card_size": SECTION_LOOKS,
    "first_day": SECTION_LOOKS,
    "theme_mode": SECTION_LOOKS,
    "language": SECTION_LOOKS,
    "auto_update": SECTION_UPDATES,
    "disabled_indexers": SECTION_ADVANCED,
    "excluded_keywords": SECTION_ADVANCED,
    "preferred_groups": SECTION_ADVANCED,
    "prowlarr_url": SECTION_ADVANCED,
    "prowlarr_api_key": SECTION_ADVANCED,
}


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


class _Box:
    """A rounded block of related settings: a bold name, a short muted explanation, then the
    controls (`add`, one below the other)."""

    def __init__(self, master: Any, title: str | None = None, text: str | None = None) -> None:
        self.frame = ctk.CTkFrame(
            master,
            fg_color=theme.TAB_BG,
            border_width=1,
            border_color=theme.PANEL_EDGE,
            corner_radius=_BOX_RADIUS,
        )
        self.body = ctk.CTkFrame(self.frame, fg_color="transparent")
        self.body.pack(fill="both", expand=True, padx=_BOX_PAD_X, pady=_BOX_PAD_Y)
        self.body.grid_columnconfigure(0, weight=1)
        self._next_row = 0
        if title is not None:
            self.add(
                ctk.CTkLabel(
                    self.body,
                    text=title,
                    font=theme.font(16, bold=True),
                    text_color=theme.TEXT,
                    anchor="w",
                ),
                gap=0,
                sticky="w",
            )
        if text is not None:
            self.add(self.text(text), gap=2, sticky="w")

    def text(self, text: str, *, indent: int = 0) -> ctk.CTkLabel:
        """A muted explanation line in this box (not placed yet)."""
        return text_label(
            self.body, text, size=_TEXT_SIZE, color=theme.TEXT_MUTED, wraplength=_BOX_INNER - indent
        )

    def add(
        self, widget: Any, *, gap: int = _CONTROL_GAP, sticky: str = "ew", padx: int = 0
    ) -> int:
        """Place `widget` (a child of `body`) on the next row, `gap` below the previous one.
        Returns its row."""
        row = self.take_row()
        widget.grid(
            row=row, column=0, sticky=sticky, padx=(padx, 0), pady=(0 if row == 0 else gap, 0)
        )
        return row

    def frame_row(self, *, gap: int = _CONTROL_GAP) -> ctk.CTkFrame:
        """A transparent frame on the next row, for a line of controls."""
        row = ctk.CTkFrame(self.body, fg_color="transparent")
        self.add(row, gap=gap)
        return row

    def take_row(self) -> int:
        row = self._next_row
        self._next_row += 1
        return row


class _Page:
    """One section's page: a heading, its muted line, then boxes (`box`) and other blocks
    (`add`), one below the other."""

    def __init__(self, master: Any, title: str, note: str) -> None:
        self.frame = ctk.CTkFrame(master, fg_color="transparent")
        self.frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            self.frame,
            text=title,
            font=theme.font(_HEADING_SIZE, bold=True),
            text_color=theme.TEXT,
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        text_label(
            self.frame, note, size=DETAIL_SIZE, color=theme.TEXT_MUTED, wraplength=_PAGE_WIDTH
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))
        self._next_row = 2

    def box(self, title: str | None = None, text: str | None = None) -> _Box:
        box = _Box(self.frame, title, text)
        self.add(box.frame)
        return box

    def add(self, widget: Any, *, gap: int | None = None) -> None:
        if gap is None:
            gap = _FIRST_BOX_GAP if self._next_row == 2 else _BOX_GAP
        widget.grid(row=self._next_row, column=0, sticky="ew", pady=(gap, 0))
        self._next_row += 1


class _SidebarItem(ctk.CTkFrame):
    """One section in the sidebar: its icon and name; a click shows its page."""

    def __init__(self, master: Any, section: str, on_click: Callable[[str], None]) -> None:
        super().__init__(master, fg_color="transparent", corner_radius=8, cursor="hand2")
        self.section = section
        self._on_click = on_click
        self._selected = False
        self._hovered = False
        self._icon = ctk.CTkLabel(
            self,
            text=SECTION_ICONS[section],
            width=_ICON_WIDTH,
            height=_ITEM_HEIGHT,
            font=theme.font(DETAIL_SIZE),
            text_color=theme.TEXT_MUTED,
        )
        self._icon.pack(side="left", padx=(_ITEM_PAD[0], _ITEM_PAD[1]))
        self._name = ctk.CTkLabel(
            self,
            text=t(section),
            height=_ITEM_HEIGHT,
            anchor="w",
            font=theme.font(DETAIL_SIZE),
            text_color=theme.TEXT,
        )
        self._name.pack(side="left", fill="x", expand=True, padx=(0, _ITEM_PAD[2]))
        for widget in (self, self._icon, self._name):
            widget.bind("<Button-1>", lambda _event: self.click(), add="+")
            widget.bind("<Enter>", lambda _event: self._hover(True), add="+")
            widget.bind("<Leave>", self._on_leave, add="+")

    @property
    def selected(self) -> bool:
        return self._selected

    def click(self) -> None:
        self._on_click(self.section)

    def set_selected(self, selected: bool) -> None:
        self._selected = selected
        self._paint()

    def _on_leave(self, event: tk.Event[Any]) -> None:
        # Moving between the item's own labels is not leaving it.
        try:
            inside = self.winfo_containing(event.x_root, event.y_root)
        except (KeyError, tk.TclError):
            inside = None
        while inside is not None:
            if inside is self:
                return
            inside = inside.master
        self._hover(False)

    def _hover(self, hovered: bool) -> None:
        self._hovered = hovered
        self._paint()

    def _paint(self) -> None:
        color: theme.Color
        if self._selected:
            color = theme.ROW_SELECTED
        else:
            color = theme.CHIP if self._hovered else "transparent"
        self.configure(fg_color=color)
        self._name.configure(font=theme.font(DETAIL_SIZE, bold=self._selected))


def sidebar_width() -> int:
    """The sidebar's width (logical): room for the longest section name in bold (the selected
    one), so the pages don't shift when another section is chosen."""
    bold = theme.font(DETAIL_SIZE, bold=True)
    widest = max(int(bold.measure(t(section))) for section in SECTIONS)
    item = sum(_ITEM_PAD) + _ICON_WIDTH + widest + 4  # + a little slack
    return max(_SIDEBAR_WIDTH, item + 2 * _SIDEBAR_PAD)


def _paint_background(widget: ctk.CTkFrame, color: theme.Color) -> None:
    """Give a transparent CTk frame that sits inside a box but belongs to the page (the remote
    form, the Prowlarr panel) the box's colour; its transparent children follow."""
    widget.configure(fg_color=color)


# -- the dialog --------------------------------------------------------------------------------


class SettingsDialog(ctk.CTkToplevel):
    def __init__(
        self,
        master: ctk.CTk,
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
        startup_shortcut: StartupShortcut | None = None,
        subtitles: SubtitleService | None = None,
        on_restart: Callable[[], None] | None = None,
        images: ImageCache | None = None,
        library: LibraryService | None = None,
    ) -> None:
        """`subtitles`: the Subtitles section shows when it is given. `on_restart`: restarts
        AirGrabber (the Language box's "Restart now"; without it only the note shows).
        `images`: the Card size box's preview card (none without it); `library`: a followed
        show's poster for it (the placeholder card without one)."""
        super().__init__(master, fg_color=theme.BG)
        self._images = images
        self._library = library
        self._card_preview: CardSizePreview | None = None
        self._quick_dialog: QuickSetupDialog | None = None
        self._preview_task: asyncio.Task[None] | None = None
        self._subtitles = subtitles
        self._subtitle_rows: ctk.CTkFrame | None = None
        self._language_tags: LanguageTags | None = None
        self._account: AccountRow | None = None
        self._own_key: OwnKeyRow | None = None
        self._startup = startup_shortcut if startup_shortcut is not None else WindowsStartup()
        self._settings = settings
        self._search = search
        self._remote = station
        self._updates = updates
        self._torrent_app = torrent_app
        self._discovery = discovery
        self._prowlarr = prowlarr
        self._on_update = on_update
        self._on_restart = on_restart
        self._tests: dict[str, asyncio.Task[None]] = {}
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
        self._card_size = ctk.StringVar(value=CARD_SIZE_LABELS[current.card_size])
        self._theme_mode = ctk.StringVar(value=theme_name(current.theme_mode))
        self._language = ctk.StringVar(value=current.language.native_name)
        self._download_dir = ctk.StringVar(value=str(current.download_dir))
        self._tmdb_key = ctk.StringVar(value=current.tmdb_api_key.get_secret_value())
        self._excluded = ctk.StringVar(value=", ".join(current.excluded_keywords))
        self._preferred = ctk.StringVar(value=", ".join(current.preferred_groups))
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
        self._subtitles_on = ctk.BooleanVar(value=current.subtitles_enabled)
        self._subtitles_folder = ctk.StringVar(value=current.subtitles_folder)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True)
        scaling = ctk.ScalingTracker.get_widget_scaling(body)
        # Plain Tk grid options are not DPI-scaled by CTk.
        body.grid_columnconfigure(0, minsize=round(sidebar_width() * scaling))
        body.grid_columnconfigure(2, weight=1)
        body.grid_rowconfigure(0, weight=1)

        self._sidebar = ctk.CTkFrame(body, fg_color="transparent")
        self._sidebar.grid(
            row=0, column=0, rowspan=2, sticky="nsew", padx=_SIDEBAR_PAD, pady=(_PAGE_TOP, 12)
        )
        # A 1-pixel line between the sidebar and the page (a CTkFrame that thin draws nothing).
        line = tk.Frame(body, width=1, highlightthickness=0)
        line.grid(row=0, column=1, rowspan=2, sticky="ns")

        def paint_line() -> None:
            line.configure(background=theme.pick(theme.PANEL_EDGE))

        paint_line()
        theme.on_mode_change(line, paint_line)
        self._scroll = ctk.CTkScrollableFrame(
            body,
            width=_PAGE_WIDTH,
            height=200,
            fg_color="transparent",
            scrollbar_button_color=theme.SCROLLBAR,
            scrollbar_button_hover_color=theme.EDGE,
        )
        self._scroll.grid(
            row=0,
            column=2,
            sticky="nsew",
            padx=(_PAGE_PAD_LEFT, _PAGE_PAD_RIGHT),
            pady=(_PAGE_TOP, 0),
        )
        self._scroll.grid_columnconfigure(0, weight=1)

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
        secondary_button(buttons, t("Cancel"), self.destroy).pack(side="right")
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
        if self._subtitles is not None:
            subtitles_service = self._subtitles
            builders[SECTION_SUBTITLES] = lambda: self._build_subtitles(subtitles_service)
        for section in SECTIONS:
            if section in builders:
                self._pages[section] = builders[section]()
        self._items: dict[str, _SidebarItem] = {}
        self._build_sidebar()
        self._section = SECTION_WHERE
        self._show_section(SECTION_WHERE)
        if update_info is not None:
            self._show_update(update_info)

        self.bind("<Escape>", lambda _event: self.destroy())
        self._fit_size()
        place_over(self, master)
        activate_later(self)

    # -- sidebar and pages ---------------------------------------------------------------------

    @property
    def section(self) -> str:
        """The section whose page shows."""
        return self._section

    def page(self, section: str) -> ctk.CTkFrame:
        """A section's page frame (tests look inside it)."""
        return self._pages[section].frame

    def _build_sidebar(self) -> None:
        first = True
        for group, sections in SECTION_GROUPS:
            shown = [section for section in sections if section in self._pages]
            if not shown:
                continue
            ctk.CTkLabel(
                self._sidebar,
                text=t(group).upper(),
                font=theme.font(12, bold=True),
                text_color=theme.TEXT_MUTED,
                anchor="w",
            ).pack(fill="x", padx=(10, 0), pady=(0 if first else 14, 4))
            first = False
            for section in shown:
                item = _SidebarItem(self._sidebar, section, self._show_section)
                item.pack(fill="x", pady=(0, 2))
                self._items[section] = item

    def _show_section(self, section: str) -> None:
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
        )
        self._torrent_row = app.take_row()

        remote = page.box()
        remote.add(
            self._radio(remote.body, t(REMOTE_CHOICE), DownloadMode.REMOTE), gap=0, sticky="w"
        )
        remote.add(remote.text(t(REMOTE_TEXT), indent=_INDENT), gap=2, sticky="w", padx=_INDENT)
        self._local_line = remote.text(t(LOCAL_QBITTORRENT_TEXT), indent=_INDENT)
        self._local_row = remote.take_row()
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
            field_width=_REMOTE_FIELD_WIDTH,
            describe=_describe,
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
            self._folder_note.grid_remove()
            self._folder_link.grid_remove()

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
        return page

    # -- Subtitles -----------------------------------------------------------------------------

    def _build_subtitles(self, subtitles: SubtitleService) -> _Page:
        """The Subtitles page: the switch (with today's downloads left, when known) and the
        credit line, and while it is on the languages, the finished-downloads folder, the
        OpenSubtitles account and the own key. Without an API key in this version: one quiet
        line instead."""
        if not subtitles.available:
            return self._new_page(SECTION_SUBTITLES, UNAVAILABLE_TEXT)
        current = self._settings.current
        page = self._new_page(SECTION_SUBTITLES, SUBTITLES_NOTE)
        switch = page.box(t(SUBTITLE_SWITCH_TITLE), t(SUBTITLE_SWITCH_TEXT))
        switch.add(
            ctk.CTkSwitch(
                switch.body,
                text=t(SWITCH_TEXT),
                variable=self._subtitles_on,
                command=self._show_subtitle_rows,
                height=BUTTON_HEIGHT,
                font=theme.font(DETAIL_SIZE),
                progress_color=theme.ACCENT,  # as the show window's "Download new episodes"
            ),
            sticky="w",
        )
        self._quota = switch.text("")
        self._quota_row = switch.take_row()
        switch.add(
            link(switch.body, t(CREDIT_TEXT), CREDIT_URL, size=SMALL_SIZE), gap=8, sticky="w"
        )

        rows = ctk.CTkFrame(page.frame, fg_color="transparent")
        rows.grid_columnconfigure(0, weight=1)
        page.add(rows, gap=0)
        self._subtitle_rows = rows
        self._show_quota()

        languages = self._rows_box(rows, t(LANGUAGES_TITLE), t(LANGUAGES_TEXT), first=True)
        self._language_tags = LanguageTags(languages.body, current.subtitle_languages)
        languages.add(self._language_tags, sticky="w")

        folder = self._rows_box(rows, t(FOLDER_LABEL), t(FOLDER_HINT))
        line = folder.frame_row()
        self._entry(line, self._subtitles_folder).pack(side="left", fill="x", expand=True)
        secondary_button(line, t("Choose…"), self._choose_subtitles_folder, width=110).pack(
            side="left", padx=(10, 0)
        )

        account = self._rows_box(rows, t(ACCOUNT_LABEL))
        hint = ctk.CTkFrame(account.body, fg_color="transparent")
        account.add(hint, gap=2, sticky="w")
        text_label(hint, t(ACCOUNT_HINT), size=_TEXT_SIZE, color=theme.TEXT_MUTED).pack(
            side="left"
        )
        link(hint, t(SIGN_UP_TEXT), SIGNUP_URL, size=_TEXT_SIZE).pack(side="left", padx=(4, 0))
        self._account = AccountRow(
            account.body,
            subtitles=subtitles,
            username=current.opensubtitles_username,
            password=current.opensubtitles_password.get_secret_value(),
            on_signed_in=lambda _account: self._show_quota(),
            field_width=_BOX_INNER,
        )
        account.add(self._account)

        own_key = self._rows_box(rows, t(OWN_KEY_LABEL))
        self._own_key = OwnKeyRow(
            own_key.body,
            subtitles=subtitles,
            key=current.opensubtitles_own_key.get_secret_value(),
            field_width=_BOX_INNER,
        )
        own_key.add(self._own_key)
        self._show_subtitle_rows()
        return page

    @staticmethod
    def _rows_box(
        rows: ctk.CTkFrame, title: str, text: str | None = None, *, first: bool = False
    ) -> _Box:
        """A box of the Subtitles rows that show while subtitles are on."""
        box = _Box(rows, title, text)
        box.frame.grid(row=len(rows.grid_slaves()), column=0, sticky="ew", pady=(_BOX_GAP, 0))
        return box

    def _show_subtitle_rows(self) -> None:
        if self._subtitle_rows is None:
            return
        if self._subtitles_on.get():
            self._subtitle_rows.grid()
        else:
            self._subtitle_rows.grid_remove()

    def _show_quota(self) -> None:
        if self._subtitles is None or self._subtitle_rows is None or not self.winfo_exists():
            return
        text = quota_line(self._subtitles.quota, datetime.now(UTC))
        if text:
            self._quota.configure(text=text)
            self._quota.grid(row=self._quota_row, column=0, sticky="w", pady=(2, 0))
        else:
            self._quota.grid_remove()

    def _choose_subtitles_folder(self) -> None:
        # Native modal dialog: the event loop (and background tasks) pause until it closes.
        chosen = filedialog.askdirectory(
            parent=self, initialdir=self._subtitles_folder.get() or None
        )
        if chosen:
            self._subtitles_folder.set(str(Path(chosen)))  # Windows separators, UNC kept

    def _subtitle_changes(self) -> dict[str, Any]:
        """The Subtitles page's part of Save (nothing without its form)."""
        if self._language_tags is None or self._account is None or self._own_key is None:
            return {}
        username, password = self._account.values
        return {
            "subtitles_enabled": self._subtitles_on.get(),
            "subtitle_languages": self._language_tags.languages,
            "subtitles_folder": self._subtitles_folder.get(),
            "opensubtitles_username": username,
            "opensubtitles_password": password,
            "opensubtitles_own_key": self._own_key.value,
        }

    def _subtitle_problem(self) -> str | None:
        changes = self._subtitle_changes()
        if not changes:
            return None
        return subtitle_problem(
            enabled=changes["subtitles_enabled"],
            folder=changes["subtitles_folder"],
            languages=changes["subtitle_languages"],
            username=changes["opensubtitles_username"],
            password=changes["opensubtitles_password"],
        )

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
        self._tmdb_key.trace_add("write", self._on_tmdb_key_edited)
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
            self._card_size.trace_add("write", lambda *_args: self._card_size_changed())
            if self._library is not None:
                self._preview_task = spawn(self._preview_show(self._library), name="card-preview")
        week = page.box(t(WEEK_TITLE), t(WEEK_TEXT))
        line = week.frame_row()
        self._option_menu(line, self._first_day, [weekday_name(day) for day in Weekday])
        return page

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
        entries = await library.tracked()
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
        """Show what Quick setup saved (look, quality, card size, language)."""
        if not self.winfo_exists():
            return
        current = self._settings.current
        self._theme_mode.set(theme_name(current.theme_mode))
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
        return page

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
        choice, with the program's button, the magnet note and its form, the Subtitles rows), at
        most _MAX_PAGE_HEIGHT, so it doesn't change between pages or choices; on a short screen
        at the screen's height instead, the pages scrolling."""
        self._torrent.show(TorrentStatus.SETUP_NEEDED)  # its tallest: the line and a button
        if self._subtitle_rows is not None:
            self._subtitle_rows.grid()  # measured with the Subtitles rows showing
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
        self._show_subtitle_rows()
        self._show_section(self._section)
        scaling = ctk.ScalingTracker.get_window_scaling(self)
        page_height = min(math.ceil(content / scaling) + _PAGE_BOTTOM, _MAX_PAGE_HEIGHT)
        self._scroll.configure(height=page_height)
        self.update_idletasks()
        width = math.ceil(self.winfo_reqwidth() / scaling)
        wanted = math.ceil(self.winfo_reqheight() / scaling)
        _screen_width, screen_height = self.wm_maxsize()  # physical pixels
        height = dialog_height(wanted, int(screen_height / scaling))
        self.geometry(f"{width}x{height}")  # CTk scales the size (not the position)

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
            card_size=size_from_label(self._card_size.get()),
            theme_mode=theme_from_name(self._theme_mode.get()),
            language=language_from_name(self._language.get()),
            download_dir=download_dir_to_save(
                DownloadMode(self._mode.get()),
                self._download_dir.get(),
                self._settings.current.download_dir,
            ),
            tmdb_api_key=self._tmdb_key.get(),
            excluded_keywords=self._excluded.get(),
            preferred_groups=self._preferred.get(),
            disabled_indexers=disabled,
            prowlarr_url=self._prowlarr_url.get(),
            prowlarr_api_key=self._prowlarr_key.get(),
            **self._remote_form.changes(),
            auto_update=self._auto_update.get(),
            keep_in_tray=self._keep_in_tray.get(),
            auto_download_new_shows=self._auto_new_shows.get(),
            **self._subtitle_changes(),
        )

    def _show_problem(self, text: str, section: str) -> None:
        """The error line, and the page with the problem."""
        self._error.configure(text=text)
        self._show_section(section)

    async def _save(self) -> None:
        if await self._save_all() and self.winfo_exists():
            self.destroy()

    async def _restart_now(self) -> None:
        """The Language box's Restart now: save like Save, then restart AirGrabber (which
        closes this dialog with the main window)."""
        if await self._save_all() and self._on_restart is not None:
            self._on_restart()

    async def _save_all(self) -> bool:
        """Check and store the whole form. False (the dialog shows why) when that failed."""
        self._error.configure(text="")
        problem = self._subtitle_problem()
        if problem is not None:
            self._show_problem(problem, SECTION_SUBTITLES)
            return False
        self._save_button.configure(state="disabled")
        if self._own_key is not None and self._own_key.needs_check:
            problem = await self._own_key.verify()  # a new own key: does OpenSubtitles take it?
            if not self.winfo_exists():
                return False
            if problem is not None:
                self._show_problem(f"{t(OWN_KEY_LABEL)}: {problem}", SECTION_SUBTITLES)
                self._subtitles_on.set(True)  # the key's row shows while subtitles are on
                self._show_subtitle_rows()
                self._save_button.configure(state="normal")
                return False
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
        return await self._save_startup()

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
        if not info.available:
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

    def destroy(self) -> None:
        stop_listening = getattr(self, "_stop_listening", None)
        if stop_listening is not None:
            stop_listening()
        for task in self._tests.values():
            task.cancel()
        self._tests.clear()
        if self._preview_task is not None:
            self._preview_task.cancel()
        super().destroy()
