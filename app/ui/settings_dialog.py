"""Preferences dialog: General and Advanced tabs, in plain words. Every change goes through
SettingsService, which validates first."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Mapping, Sequence
from tkinter import filedialog
from pathlib import Path
from typing import Any, NamedTuple, Protocol

import customtkinter as ctk
from pydantic import ValidationError

from app.indexers import (
    ApibayIndexer,
    BitsearchIndexer,
    EztvIndexer,
    IndexerError,
    ProwlarrIndexer,
    YtsIndexer,
)
from app.models import DownloadMode, ProwlarrStatus, QualityProfile, UpdateInfo, Weekday
from app.services import (
    DiscoveryService,
    ProwlarrSetupService,
    RemoteDownloads,
    SettingsService,
    TorrentAppService,
    TorrentSearchService,
    UpdateError,
    UpdateService,
)
from app.services import startup
from app.services.startup import StartupError
from app.ui import theme
from app.ui.branding import set_window_icon
from app.ui.dialog_kit import (
    BUTTON_HEIGHT,
    BUTTON_RADIUS,
    DETAIL_SIZE,
    LINE_GAP,
    PAD_X,
    SMALL_SIZE,
    activate_later,
    place_over,
    primary_button,
    secondary_button,
    text_label,
)
from app.ui.prowlarr_panel import PANEL_PAD_X, ProwlarrPanel, shows_remote_fields
from app.ui.remote_program_form import FIELD_LABELS as REMOTE_LABELS
from app.ui.remote_program_form import RemoteProgramForm, folder_note
from app.ui.tasks import async_command, spawn
from app.ui.tmdb_key_guide import TmdbKeyDialog
from app.ui.torrent_app_status import INSTALL_BUTTON_WIDTH, TorrentAppStatus, TorrentStatus

TAB_GENERAL = "General"
TAB_ADVANCED = "Advanced"

# Logical (unscaled) CTk units.
_TABS_WIDTH = 720 - 2 * PAD_X
_TABS_HEIGHT = 200  # at least; the dialog takes General's tallest state, within the screen
SCREEN_RESERVE = 80  # left free for the taskbar and the title bar
_TAB_PAD = 24  # inside a tab
_LABEL_WIDTH = 170
_FIELD_WIDTH = _TABS_WIDTH - 2 * _TAB_PAD - _LABEL_WIDTH - 24  # tab view edges, scrollbar
_ROW_GAP = 20
_SECTION_GAP = 28
_INDENT = 30  # under a radio button's text
_HINT_GAP = 6
_SCROLL_PAD_LEFT = _TAB_PAD - 6  # a scrollable frame has its own inset
_TEST_BUTTON_WIDTH = 160
_UPDATE_BUTTON_WIDTH = 180
_RESULT_WIDTH = _FIELD_WIDTH - _UPDATE_BUTTON_WIDTH - 12
_PANEL_GAP = 18  # above the Prowlarr panel
_PANEL_INSET = PANEL_PAD_X + 1  # the Prowlarr panel's border and padding
# The Prowlarr panel spans both columns; its "another computer" fields line up with the others.
_PANEL_WIDTH = _LABEL_WIDTH + _FIELD_WIDTH
_PANEL_LABEL_WIDTH = _LABEL_WIDTH - _PANEL_INSET
_PANEL_FIELD_WIDTH = _FIELD_WIDTH - _PANEL_INSET
# The NAS / other computer form under its radio button. The field column really gets about 40
# less than _FIELD_WIDTH (the scroll area's scrollbar and insets); wider rows would be cut off.
_FIELD_ROOM = _FIELD_WIDTH - 40
_REMOTE_LABEL_WIDTH = 120
_REMOTE_FIELD_WIDTH = _FIELD_ROOM - _INDENT - _REMOTE_LABEL_WIDTH
# Too long for one line of the field column: broken where the sentence breaks best.
REMOTE_CHOICE = "Send to a torrent program on my NAS\nor another computer"
_REMOTE_GAP = 8  # above and below that form

# (stable id, label) of the websites AirGrabber searches itself, in the order the search runs
# them. Prowlarr (searched last) has its own panel.
_BUILT_IN_SITES = [
    (cls.name, cls.label) for cls in (ApibayIndexer, EztvIndexer, BitsearchIndexer, YtsIndexer)
]
_PROWLARR_FIELDS = frozenset({"prowlarr_url", "prowlarr_api_key"})

# Plain names for the quality profiles, in menu order. The stored values stay QualityProfile's.
QUALITY_NAMES: dict[QualityProfile, str] = {
    QualityProfile.FULL_HD: "Full HD (1080p)",
    QualityProfile.HD: "HD (720p)",
    QualityProfile.UHD: "4K (2160p)",
    QualityProfile.HEVC: "Smaller files (1080p x265)",
    QualityProfile.ANY: "Any quality",
}


def quality_name(profile: QualityProfile) -> str:
    return QUALITY_NAMES[profile]


def quality_from_name(name: str) -> QualityProfile:
    for profile, shown in QUALITY_NAMES.items():
        if shown == name:
            return profile
    raise ValueError(f"unknown quality name: {name!r}")


class DownloadRows(NamedTuple):
    """Which extra rows "When I download" shows under its three choices."""

    torrent_status: bool  # the torrent-program status line (+ Install qBittorrent)
    remote_form: bool  # the NAS / other computer form (program, address, account, Test)
    folder: bool  # the folder entry + Browse (and the magnet-only note)


def download_rows(mode: DownloadMode) -> DownloadRows:
    return DownloadRows(
        torrent_status=mode is DownloadMode.APP,
        remote_form=mode is DownloadMode.REMOTE,
        folder=mode is DownloadMode.FOLDER,
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
    screen minus room for the taskbar and title bar (the tabs scroll instead)."""
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


_FIELD_LABELS = {
    "download_mode": "When I download",
    "download_dir": "Save .torrent files to a folder",
    "quality": "Preferred quality",
    "first_day": "Week starts on",
    "tmdb_api_key": "Movies key",
    "auto_update": "Updates",
    "disabled_indexers": "Built-in sites",
    "excluded_keywords": "Skip releases with these words",
    "preferred_groups": "Prefer releases from",
    "prowlarr_url": "Prowlarr URL",
    "prowlarr_api_key": "Prowlarr API key",
    **REMOTE_LABELS,
}

_FIELD_TABS = {
    "disabled_indexers": TAB_ADVANCED,
    "excluded_keywords": TAB_ADVANCED,
    "preferred_groups": TAB_ADVANCED,
    "prowlarr_url": TAB_ADVANCED,
    "prowlarr_api_key": TAB_ADVANCED,
}  # everything else (the remote_* program form too) lives on General


def _describe(error: ValidationError) -> str:
    lines = []
    for item in error.errors():
        label = _FIELD_LABELS.get(str(item["loc"][0])) if item["loc"] else None
        message = item["msg"].removeprefix("Value error, ")
        lines.append(f"{label}: {message}" if label else message)
    return "\n".join(lines)


def _prowlarr_error(error: ValidationError) -> bool:
    return any(item["loc"] and item["loc"][0] in _PROWLARR_FIELDS for item in error.errors())


def _first_tab(error: ValidationError) -> str:
    errors = error.errors()
    if not errors or not errors[0]["loc"]:
        return TAB_GENERAL
    return _FIELD_TABS.get(str(errors[0]["loc"][0]), TAB_GENERAL)


class _Form:
    """A label column (muted) and a field column inside one tab, with roomy rows."""

    def __init__(
        self, master: Any, *, pad_left: int = _TAB_PAD, pad_right: int = _TAB_PAD
    ) -> None:
        self.frame = ctk.CTkFrame(master, fg_color="transparent")
        self.frame.pack(fill="both", expand=True, padx=(pad_left, pad_right), pady=(4, _TAB_PAD))
        # Plain Tk grid options are not DPI-scaled by CTk.
        scaling = ctk.ScalingTracker.get_widget_scaling(self.frame)
        self.frame.grid_columnconfigure(0, minsize=round(_LABEL_WIDTH * scaling))
        self.frame.grid_columnconfigure(1, weight=1)
        self._next_row = 0

    def row(self, label: str) -> ctk.CTkFrame:
        row = self._take_row()
        pady = (0 if row == 0 else _ROW_GAP, 0)
        ctk.CTkLabel(
            self.frame,
            text=label,
            height=BUTTON_HEIGHT,
            font=theme.font(DETAIL_SIZE),
            text_color=theme.TEXT_MUTED,
            anchor="w",
            justify="left",
            wraplength=_LABEL_WIDTH - 16,
        ).grid(row=row, column=0, sticky="nw", padx=(0, 16), pady=pady)
        cell = ctk.CTkFrame(self.frame, fg_color="transparent")
        cell.grid(row=row, column=1, sticky="ew", pady=pady)
        return cell

    def wide(self, widget: Any, *, gap: int = _ROW_GAP) -> None:
        """`widget` across both columns, `gap` below the previous row."""
        row = self._take_row()
        pady = (0 if row == 0 else gap, 0)
        widget.grid(row=row, column=0, columnspan=2, sticky="ew", pady=pady)

    def section(self, title: str, note: str) -> None:
        ctk.CTkLabel(
            self.frame, text=title, font=theme.font(16, bold=True), text_color=theme.TEXT
        ).grid(row=self._take_row(), column=0, columnspan=2, sticky="w", pady=(_SECTION_GAP, 0))
        text_label(
            self.frame,
            note,
            size=DETAIL_SIZE,
            color=theme.TEXT_MUTED,
            wraplength=_LABEL_WIDTH + _FIELD_WIDTH,
        ).grid(row=self._take_row(), column=0, columnspan=2, sticky="w", pady=(LINE_GAP, 0))

    def _take_row(self) -> int:
        row = self._next_row
        self._next_row += 1
        return row


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
    ) -> None:
        super().__init__(master, fg_color=theme.BG)
        self._startup = startup_shortcut if startup_shortcut is not None else WindowsStartup()
        self._settings = settings
        self._search = search
        self._remote = station
        self._updates = updates
        self._torrent_app = torrent_app
        self._discovery = discovery
        self._prowlarr = prowlarr
        self._on_update = on_update
        self._tests: dict[str, asyncio.Task[None]] = {}
        self._key_dialog: TmdbKeyDialog | None = None
        current = settings.current

        self.title("Settings")
        set_window_icon(self)
        self.resizable(False, False)
        self.transient(master)

        self._mode = ctk.StringVar(value=current.download_mode.value)
        self._quality = ctk.StringVar(value=quality_name(current.quality))
        self._first_day = ctk.StringVar(value=current.first_day.value)
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

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=PAD_X, pady=PAD_X)

        self._tabs = ctk.CTkTabview(
            body,
            width=_TABS_WIDTH,
            height=_TABS_HEIGHT,
            corner_radius=15,
            border_width=1,
            fg_color=theme.TAB_BG,
            border_color=theme.EDGE,
            segmented_button_fg_color=theme.SURFACE,
            segmented_button_selected_color=theme.ACCENT,
            segmented_button_selected_hover_color=theme.ACCENT_HOVER,
            segmented_button_unselected_color=theme.SURFACE,
            segmented_button_font=theme.font(15, bold=True),
        )
        self._general = self._scroll_tab(TAB_GENERAL)
        self._general_form = _Form(self._general, pad_left=_SCROLL_PAD_LEFT, pad_right=8)
        self._build_general(self._general_form)
        advanced = self._scroll_tab(TAB_ADVANCED)
        self._build_advanced(_Form(advanced, pad_left=_SCROLL_PAD_LEFT, pad_right=8))
        self._tabs.set(TAB_GENERAL)
        if update_info is not None:
            self._show_update(update_info)

        # Packed from the bottom before the tabs, so the buttons keep their place and the tabs
        # get what is left when the screen is short.
        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(side="bottom", anchor="e", pady=(10, 0))
        secondary_button(buttons, "Cancel", self.destroy).pack(side="right")
        self._save_button = primary_button(buttons, "Save", async_command(self._save))
        self._save_button.pack(side="right", padx=(0, 12))
        self._error = ctk.CTkLabel(
            body,
            text="",
            font=theme.font(14),
            text_color=theme.DANGER,
            justify="left",
            wraplength=_TABS_WIDTH,
        )
        self._error.pack(side="bottom", anchor="w", pady=(10, 0))
        self._tabs.pack(fill="both", expand=True)

        self.bind("<Escape>", lambda _event: self.destroy())
        self._fit_size()
        place_over(self, master)
        activate_later(self)

    # -- tabs ----------------------------------------------------------------------------------

    def _build_general(self, form: _Form) -> None:
        download = form.row("When I download")
        download.grid_columnconfigure(0, weight=1)
        self._radio(download, "Open in my torrent program on this PC", DownloadMode.APP).grid(
            row=0, column=0, sticky="w"
        )
        self._torrent = TorrentAppStatus(
            download,
            torrent_app=self._torrent_app,
            wraplength=_FIELD_WIDTH - _INDENT - INSTALL_BUTTON_WIDTH - 16,
        )
        self._radio(download, REMOTE_CHOICE, DownloadMode.REMOTE).grid(
            row=2, column=0, sticky="w", pady=(4, 0)
        )
        self._remote_form = RemoteProgramForm(
            download,
            settings=self._settings.current,
            run_test=self._test_remote,
            label_width=_REMOTE_LABEL_WIDTH,
            field_width=_REMOTE_FIELD_WIDTH,
            describe=_describe,
        )
        self._radio(download, "Save .torrent files to a folder", DownloadMode.FOLDER).grid(
            row=4, column=0, sticky="w", pady=(4, 0)
        )
        self._folder_row = ctk.CTkFrame(download, fg_color="transparent")
        self._entry(self._folder_row, self._download_dir).pack(side="left", fill="x", expand=True)
        secondary_button(self._folder_row, "Browse", self._browse, width=110).pack(
            side="left", padx=(10, 0)
        )
        self._folder_note = text_label(
            download, "", size=SMALL_SIZE, color=theme.TEXT_MUTED, wraplength=_FIELD_WIDTH - _INDENT
        )
        self._option_menu(
            form.row("Preferred quality"), self._quality, list(QUALITY_NAMES.values())
        )
        self._option_menu(form.row("Week starts on"), self._first_day, [d.value for d in Weekday])

        key = form.row("Movies key")
        self._entry(key, self._tmdb_key, show="•").pack(fill="x")
        link = ctk.CTkLabel(
            key,
            text="How do I get a key?",
            font=theme.font(DETAIL_SIZE, underline=True),
            text_color=theme.LINK,
            cursor="hand2",
        )
        link.bind("<Button-1>", lambda _event: self._open_key_guide())
        link.pack(anchor="w", pady=(_HINT_GAP, 0))

        updates = form.row("Updates")
        self._checkbox(updates, "Install updates automatically", self._auto_update).pack(anchor="w")
        text_label(
            updates, f"Version {self._updates.current}", size=SMALL_SIZE, color=theme.TEXT_MUTED
        ).pack(anchor="w", pady=(2, 10))
        check = ctk.CTkFrame(updates, fg_color="transparent")
        check.pack(fill="x")
        self._update_button = primary_button(
            check, "Check for updates", self._check_updates, width=_UPDATE_BUTTON_WIDTH
        )
        self._update_button.pack(side="left", anchor="n")
        self._update_result = text_label(check, "", size=SMALL_SIZE, wraplength=_RESULT_WIDTH)
        self._update_result.pack(side="left", padx=(12, 0), fill="x", expand=True)

        auto = form.row("Automatic downloads")
        self._checkbox(
            auto, "Keep running in the tray when I close the window", self._keep_in_tray
        ).pack(anchor="w")
        text_label(
            auto,
            "Needed for new episodes to download while the window is closed",
            size=SMALL_SIZE,
            color=theme.TEXT_MUTED,
            wraplength=_FIELD_WIDTH - _INDENT,
        ).pack(anchor="w", padx=(_INDENT, 0))
        self._checkbox(
            auto, "Start AirGrabber when Windows starts", self._start_with_windows
        ).pack(anchor="w", pady=(_ROW_GAP, 0))
        self._checkbox(
            auto, "Download new episodes for shows I start following", self._auto_new_shows
        ).pack(anchor="w", pady=(_ROW_GAP, 0))

    def _build_advanced(self, form: _Form) -> None:
        sites = form.row("Built-in sites")
        boxes = ctk.CTkFrame(sites, fg_color="transparent")
        boxes.pack(anchor="w")
        for column, (name, label) in enumerate(_BUILT_IN_SITES):
            self._checkbox(boxes, label, self._sources[name]).grid(
                row=0, column=column, sticky="w", padx=(0, 20)
            )
        self._hint(sites, "Websites AirGrabber searches by itself")
        self._build_prowlarr(form)

        excluded = form.row("Skip releases with these words")
        self._entry(excluded, self._excluded).pack(fill="x")
        self._hint(excluded, "Separate with commas, for example: cam, telesync")
        preferred = form.row("Prefer releases from")
        self._entry(preferred, self._preferred).pack(fill="x")
        self._hint(preferred, "Release group names, separated with commas")

    def _build_prowlarr(self, form: _Form) -> None:
        url = self._settings.current.prowlarr_url
        panel = ProwlarrPanel(
            form.frame,
            prowlarr=self._prowlarr,
            use=self._use_prowlarr,
            checkbox=self._checkbox,
            width=_PANEL_WIDTH,
            label_width=_PANEL_LABEL_WIDTH,
            expanded=shows_remote_fields(url),
            on_status=self._prowlarr_status_shown,
        )
        form.wide(panel, gap=_PANEL_GAP)
        self._prowlarr_panel = panel
        self._prowlarr_url = self._hinted_entry(
            panel.remote_row("URL"), url, "http://192.168.1.x:9696"
        )
        self._entry(panel.remote_row("API key"), self._prowlarr_key, show="•").pack(fill="x")
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

    def _hint(self, master: Any, text: str) -> None:
        text_label(
            master, text, size=SMALL_SIZE, color=theme.TEXT_MUTED, wraplength=_FIELD_WIDTH
        ).pack(anchor="w", pady=(_HINT_GAP, 0))

    def _option_menu(self, master: Any, variable: ctk.StringVar, values: list[str]) -> None:
        ctk.CTkOptionMenu(
            master,
            variable=variable,
            values=values,
            width=260,
            height=BUTTON_HEIGHT,
            corner_radius=BUTTON_RADIUS,
            font=theme.font(DETAIL_SIZE),
            dropdown_font=theme.font(DETAIL_SIZE),
            fg_color=theme.SURFACE,
            button_color=theme.EDGE,
            button_hover_color=theme.ACCENT_HOVER,
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
        return ctk.CTkRadioButton(
            master,
            text=text,
            variable=self._mode,
            value=mode.value,
            width=0,
            height=BUTTON_HEIGHT,
            radiobutton_width=22,
            radiobutton_height=22,
            font=theme.font(DETAIL_SIZE),
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            border_color=theme.EDGE,
            command=self._show_download_rows,
        )

    def _test_row(
        self, cell: ctk.CTkFrame, command: Callable[[], None], *, field_width: int = _FIELD_WIDTH
    ) -> tuple[ctk.CTkButton, ctk.CTkLabel]:
        button = primary_button(cell, "Test connection", command, width=_TEST_BUTTON_WIDTH)
        button.pack(side="left", anchor="n")
        result = text_label(
            cell, "", size=SMALL_SIZE, wraplength=field_width - _TEST_BUTTON_WIDTH - 12
        )
        result.pack(side="left", padx=(12, 0), fill="x", expand=True)
        return button, result

    # -- General -------------------------------------------------------------------------------

    def _scroll_tab(self, name: str) -> ctk.CTkScrollableFrame:
        """A tab whose content scrolls when the dialog is shorter than it (small screens)."""
        frame = ctk.CTkScrollableFrame(
            self._tabs.add(name),
            fg_color="transparent",
            scrollbar_button_color=theme.SURFACE,
            scrollbar_button_hover_color=theme.EDGE,
        )
        frame.pack(fill="both", expand=True)
        return frame

    def _fit_size(self) -> None:
        """Fix the dialog at the size of General's tallest state (any download choice, with
        the Install button or the magnet-only note showing), so it doesn't change between
        choices or tabs; on a short screen at the screen's height instead, the tabs scrolling."""
        self._torrent.show(TorrentStatus.MISSING)
        content = 0
        for mode in DownloadMode:
            self._grid_download_rows(download_rows(mode), note="Magnet-only releases …")
            self.update_idletasks()
            content = max(content, self._general_form.frame.winfo_reqheight())
        self._torrent.show(TorrentStatus.LOOKING)
        self._show_download_rows()
        scaling = ctk.ScalingTracker.get_window_scaling(self)
        # Let General's scroll area ask for all of its content, then measure the whole dialog.
        self._general.configure(height=math.ceil(content / scaling) + _TAB_PAD + 8)
        self.update_idletasks()
        width = math.ceil(self.winfo_reqwidth() / scaling)
        wanted = math.ceil(self.winfo_reqheight() / scaling)
        _screen_width, screen_height = self.wm_maxsize()  # physical pixels
        height = dialog_height(wanted, int(screen_height / scaling))
        self.geometry(f"{width}x{height}")  # CTk scales the size (not the position)

    def _show_download_rows(self) -> None:
        rows = download_rows(DownloadMode(self._mode.get()))
        # The remote form is hidden in FOLDER mode, so what it holds now is what Save stores.
        note = folder_note(self._remote_form.kind, self._remote_form.ready)
        self._grid_download_rows(rows, note=note)
        if rows.torrent_status:
            self._torrent.refresh()  # again each time: a program may have been installed

    def _grid_download_rows(self, rows: DownloadRows, *, note: str | None) -> None:
        if rows.torrent_status:
            self._torrent.grid(row=1, column=0, sticky="ew", padx=(_INDENT, 0))
        else:
            self._torrent.grid_remove()
        if rows.remote_form:
            self._remote_form.grid(
                row=3, column=0, sticky="ew", padx=(_INDENT, 0), pady=(_REMOTE_GAP, _REMOTE_GAP)
            )
        else:
            self._remote_form.grid_remove()
        if rows.folder:
            self._folder_row.grid(row=5, column=0, sticky="ew", padx=(_INDENT, 0), pady=(4, 0))
        else:
            self._folder_row.grid_remove()
        if rows.folder and note:
            self._folder_note.configure(text=note)
            self._folder_note.grid(row=6, column=0, sticky="w", padx=(_INDENT, 0), pady=(6, 0))
        else:
            self._folder_note.grid_remove()

    def _browse(self) -> None:
        # Native modal dialog: the event loop (and background tasks) pause until it closes.
        chosen = filedialog.askdirectory(parent=self, initialdir=self._download_dir.get() or None)
        if chosen:
            self._download_dir.set(chosen)

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
        await self._settings.update(
            download_mode=self._mode.get(),
            quality=quality_from_name(self._quality.get()),
            first_day=self._first_day.get(),
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
        )

    async def _save(self) -> None:
        self._error.configure(text="")
        self._save_button.configure(state="disabled")
        try:
            await self._store()
        except ValidationError as exc:
            if self.winfo_exists():
                self._error.configure(text=_describe(exc))
                self._tabs.set(_first_tab(exc))
                if _prowlarr_error(exc):  # the fields may be folded away
                    self._prowlarr_panel.set_expanded(True)
                self._save_button.configure(state="normal")
            return
        if await self._save_startup() and self.winfo_exists():
            self.destroy()

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
            self._error.configure(text=result.error)
            self._tabs.set(TAB_GENERAL)
            self._save_button.configure(state="normal")
        return False

    async def _test_remote(self) -> str:
        """The remote form's Test connection: save everything, then test the program."""
        self._error.configure(text="")
        await self._store()
        return await self._remote.test()

    def _start_test(self, kind: str, probe: Callable[[], Awaitable[str]]) -> None:
        running = self._tests.get(kind)
        if running is not None and not running.done():
            return
        button, result = self._prowlarr_button, self._prowlarr_result
        self._error.configure(text="")
        result.configure(text="Saving and testing…", text_color=theme.TEXT_MUTED)
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
        self._update_result.configure(text="Checking GitHub…", text_color=theme.TEXT_MUTED)
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
                text="✓ You have the latest version.", text_color=theme.SUCCESS
            )
        elif not info.installable:
            self._update_result.configure(
                text=f"{info.latest} is on GitHub. This development copy is not updated in place.",
                text_color=theme.TEXT_MUTED,
            )
        else:
            self._update_result.configure(
                text=f"Version {info.latest} is available. AirGrabber restarts to finish.",
                text_color=theme.TEXT_MUTED,
            )
            self._update_button.configure(
                text=f"Update to {info.latest}", command=self._start_update
            )

    def _start_update(self) -> None:
        if self._on_update is not None:
            self._on_update()

    def destroy(self) -> None:
        for task in self._tests.values():
            task.cancel()
        self._tests.clear()
        super().destroy()
