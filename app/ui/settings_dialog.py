"""Preferences dialog. Every change goes through SettingsService, which validates first."""

from __future__ import annotations

import asyncio
import webbrowser
from collections.abc import Awaitable, Callable
from tkinter import filedialog
from typing import Any

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
from app.models import QualityProfile, UpdateInfo, Weekday
from app.services import (
    DownloadStationClient,
    SettingsService,
    TorrentSearchService,
    UpdateError,
    UpdateService,
)
from app.services.synology import DownloadStationError
from app.ui import theme
from app.ui.branding import set_window_icon
from app.ui.tasks import async_command, spawn

TMDB_KEY_URL = "https://www.themoviedb.org/settings/api"

TAB_GENERAL = "General"
TAB_INDEXERS = "Indexers"
TAB_STATION = "Download Station"

# (stable id, label) of every source, in the order the search runs them.
_SOURCES = [
    (cls.name, cls.label)
    for cls in (ApibayIndexer, EztvIndexer, BitsearchIndexer, YtsIndexer, ProwlarrIndexer)
]

_FIELD_LABELS = {
    "quality": "Preferred quality",
    "first_day": "Week starts on",
    "download_dir": "Torrent folder",
    "tmdb_api_key": "TMDB API key",
    "excluded_keywords": "Excluded keywords",
    "preferred_groups": "Preferred groups",
    "min_size_mb": "Minimum size",
    "max_size_mb": "Maximum size",
    "disabled_indexers": "Sources",
    "prowlarr_url": "Prowlarr URL",
    "prowlarr_api_key": "Prowlarr API key",
    "ds_url": "NAS address",
    "ds_username": "Username",
    "ds_password": "Password",
    "ds_verify_tls": "Verify TLS certificate",
    "ds_destination": "Destination folder",
}

_FIELD_TABS = {
    "excluded_keywords": TAB_INDEXERS,
    "preferred_groups": TAB_INDEXERS,
    "disabled_indexers": TAB_INDEXERS,
    "prowlarr_url": TAB_INDEXERS,
    "prowlarr_api_key": TAB_INDEXERS,
    "ds_url": TAB_STATION,
    "ds_username": TAB_STATION,
    "ds_password": TAB_STATION,
    "ds_verify_tls": TAB_STATION,
    "ds_destination": TAB_STATION,
}  # everything else (incl. the model-level size check) lives on General


def _describe(error: ValidationError) -> str:
    lines = []
    for item in error.errors():
        label = _FIELD_LABELS.get(str(item["loc"][0])) if item["loc"] else None
        message = item["msg"].removeprefix("Value error, ")
        lines.append(f"{label}: {message}" if label else message)
    return "\n".join(lines)


def _first_tab(error: ValidationError) -> str:
    errors = error.errors()
    if not errors or not errors[0]["loc"]:
        return TAB_GENERAL
    return _FIELD_TABS.get(str(errors[0]["loc"][0]), TAB_GENERAL)


class _Form:
    """A two-column label/field grid inside one tab."""

    def __init__(self, master: Any) -> None:
        self.frame = ctk.CTkFrame(master, fg_color="transparent")
        self.frame.pack(fill="both", expand=True, padx=10, pady=(6, 0))
        self.frame.grid_columnconfigure(1, weight=1)
        self._next_row = 0

    def row(self, label: str) -> ctk.CTkFrame:
        row = self._take_row()
        ctk.CTkLabel(self.frame, text=label, text_color=theme.TEXT_MUTED).grid(
            row=row, column=0, sticky="nw", padx=(0, 20), pady=6
        )
        cell = ctk.CTkFrame(self.frame, fg_color="transparent")
        cell.grid(row=row, column=1, sticky="ew", pady=6)
        return cell

    def full_width(self, widget: Any, **grid: Any) -> None:
        widget.grid(row=self._take_row(), column=0, columnspan=2, sticky="w", **grid)

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
        station: DownloadStationClient,
        updates: UpdateService,
        update_info: UpdateInfo | None = None,
        on_update: Callable[[], None] | None = None,
    ) -> None:
        super().__init__(master, fg_color=theme.BG)
        self._settings = settings
        self._search = search
        self._station = station
        self._updates = updates
        self._on_update = on_update
        self._tests: dict[str, asyncio.Task[None]] = {}
        current = settings.current

        self.title("Settings")
        set_window_icon(self)
        self.resizable(False, False)
        self.transient(master)

        self._quality = ctk.StringVar(value=current.quality.value)
        self._first_day = ctk.StringVar(value=current.first_day.value)
        self._download_dir = ctk.StringVar(value=str(current.download_dir))
        self._tmdb_key = ctk.StringVar(value=current.tmdb_api_key.get_secret_value())
        self._excluded = ctk.StringVar(value=", ".join(current.excluded_keywords))
        self._preferred = ctk.StringVar(value=", ".join(current.preferred_groups))
        self._min_size = ctk.StringVar(value=str(current.min_size_mb))
        self._max_size = ctk.StringVar(value=str(current.max_size_mb))
        disabled = set(current.disabled_indexers)
        self._sources = {
            name: ctk.BooleanVar(value=name not in disabled) for name, _label in _SOURCES
        }
        known = {name for name, _label in _SOURCES}
        self._unknown_disabled = tuple(n for n in current.disabled_indexers if n not in known)
        self._prowlarr_key = ctk.StringVar(value=current.prowlarr_api_key.get_secret_value())
        self._ds_username = ctk.StringVar(value=current.ds_username)
        self._ds_password = ctk.StringVar(value=current.ds_password.get_secret_value())
        self._ds_verify = ctk.BooleanVar(value=current.ds_verify_tls)

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=25, pady=20)
        ctk.CTkLabel(body, text="Settings", font=theme.font(20, bold=True)).pack(
            anchor="w", pady=(0, 8)
        )

        self._tabs = ctk.CTkTabview(
            body,
            width=600,
            height=440,
            corner_radius=15,
            border_width=1,
            fg_color=theme.TAB_BG,
            border_color=theme.EDGE,
            segmented_button_fg_color=theme.SURFACE,
            segmented_button_selected_color=theme.ACCENT,
            segmented_button_selected_hover_color=theme.ACCENT_HOVER,
            segmented_button_unselected_color=theme.SURFACE,
            segmented_button_font=theme.font(13, bold=True),
        )
        self._tabs.pack(fill="both", expand=True)
        self._build_general(_Form(self._tabs.add(TAB_GENERAL)))
        self._build_indexers(_Form(self._tabs.add(TAB_INDEXERS)))
        self._build_station(_Form(self._tabs.add(TAB_STATION)))
        self._tabs.set(TAB_GENERAL)
        if update_info is not None:
            self._show_update(update_info)

        self._error = ctk.CTkLabel(
            body, text="", text_color=theme.DANGER, justify="left", wraplength=560
        )
        self._error.pack(anchor="w", pady=(8, 0))

        buttons = ctk.CTkFrame(body, fg_color="transparent")
        buttons.pack(anchor="e", pady=(8, 0))
        ctk.CTkButton(
            buttons,
            text="Cancel",
            width=100,
            fg_color="transparent",
            border_width=1,
            border_color=theme.EDGE,
            hover_color=theme.SURFACE,
            command=self.destroy,
        ).pack(side="right")
        self._save_button = ctk.CTkButton(
            buttons,
            text="Save",
            width=100,
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            command=async_command(self._save),
        )
        self._save_button.pack(side="right", padx=(0, 8))

        self.bind("<Escape>", lambda _event: self.destroy())
        self._center_on(master)
        # CTkToplevel can open behind its parent on Windows; raise and grab once it is mapped.
        self.after(150, self._activate)

    # -- tabs ----------------------------------------------------------------------------------

    def _build_general(self, form: _Form) -> None:
        self._option_row(form, "Preferred quality", self._quality, [q.value for q in QualityProfile])
        self._option_row(form, "Week starts on", self._first_day, [d.value for d in Weekday])

        folder = form.row("Torrent folder")
        self._entry(folder, self._download_dir, width=230).pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            folder,
            text="Browse",
            width=70,
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            command=self._browse,
        ).pack(side="left", padx=(8, 0))

        key = form.row("TMDB API key")
        self._entry(key, self._tmdb_key, show="•").pack(fill="x")
        link = ctk.CTkLabel(
            key,
            text="Get a free key at themoviedb.org",
            text_color=theme.LINK,
            font=theme.font(11, underline=True),
            cursor="hand2",
        )
        link.pack(anchor="w")
        link.bind("<Button-1>", lambda _event: webbrowser.open(TMDB_KEY_URL))

        sizes = form.row("Size limits (MB)")
        self._entry(sizes, self._min_size, width=80).pack(side="left")
        ctk.CTkLabel(sizes, text="to").pack(side="left", padx=8)
        self._entry(sizes, self._max_size, width=80).pack(side="left")
        ctk.CTkLabel(sizes, text="0 = no limit", text_color=theme.TEXT_DIM).pack(side="left", padx=10)

        version = form.row("App version")
        ctk.CTkLabel(version, text=self._updates.current).pack(side="left", anchor="n")
        self._update_button = ctk.CTkButton(
            version,
            text="Check for updates",
            width=150,
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            command=self._check_updates,
        )
        self._update_button.pack(side="left", anchor="n", padx=(14, 0))
        self._update_result = ctk.CTkLabel(
            version, text="", justify="left", anchor="w", wraplength=230
        )
        self._update_result.pack(side="left", padx=(10, 0), fill="x", expand=True)

    def _build_indexers(self, form: _Form) -> None:
        sources = form.row("Sources")
        for name, label in _SOURCES:
            ctk.CTkCheckBox(
                sources,
                text=label,
                variable=self._sources[name],
                width=0,
                checkbox_width=20,
                checkbox_height=20,
                fg_color=theme.ACCENT,
                hover_color=theme.ACCENT_HOVER,
                border_color=theme.EDGE,
            ).pack(side="left", padx=(0, 12))
        self._entry(form.row("Excluded keywords"), self._excluded).pack(fill="x")
        self._entry(form.row("Preferred groups"), self._preferred).pack(fill="x")

        form.full_width(
            ctk.CTkLabel(form.frame, text="Prowlarr (optional)", font=theme.font(14, bold=True)),
            pady=(14, 0),
        )
        form.full_width(
            ctk.CTkLabel(
                form.frame,
                text="Routes searches through your own Prowlarr — brings back sites the "
                "built-in sources can't reach.",
                text_color=theme.TEXT_MUTED,
                justify="left",
                wraplength=520,
            ),
            pady=(0, 4),
        )
        self._prowlarr_url = self._hinted_entry(
            form.row("URL"), self._settings.current.prowlarr_url, "http://localhost:9696"
        )
        self._entry(form.row("API key"), self._prowlarr_key, show="•").pack(fill="x")
        self._prowlarr_button, self._prowlarr_result = self._test_row(
            form, lambda: self._start_test("prowlarr", self._search.test_prowlarr)
        )

    def _build_station(self, form: _Form) -> None:
        form.full_width(
            ctk.CTkLabel(
                form.frame,
                text="Only used for releases that have no .torrent file (magnet-only). Everything "
                "else is saved as a .torrent file into the torrent folder, as before.",
                text_color=theme.TEXT_MUTED,
                justify="left",
                wraplength=460,
            ),
            pady=(0, 6),
        )
        self._ds_url = self._hinted_entry(
            form.row("NAS address"), self._settings.current.ds_url, "http://192.168.1.x:5000"
        )
        self._entry(form.row("Username"), self._ds_username).pack(fill="x")
        self._entry(form.row("Password"), self._ds_password, show="•").pack(fill="x")
        ctk.CTkCheckBox(
            form.row(""),
            text="Verify TLS certificate",
            variable=self._ds_verify,
            checkbox_width=20,
            checkbox_height=20,
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            border_color=theme.EDGE,
        ).pack(anchor="w")
        self._ds_destination = self._hinted_entry(
            form.row("Destination folder"),
            self._settings.current.ds_destination,
            "video/tv — empty: Download Station's default",
        )
        self._station_button, self._station_result = self._test_row(
            form, lambda: self._start_test("station", self._station.test)
        )

    # -- widgets -------------------------------------------------------------------------------

    def _entry(self, master: Any, variable: ctk.StringVar, **kwargs: Any) -> ctk.CTkEntry:
        kwargs.setdefault("width", 310)
        return ctk.CTkEntry(
            master, textvariable=variable, fg_color=theme.SURFACE, border_color=theme.EDGE, **kwargs
        )

    def _hinted_entry(self, master: Any, value: str, placeholder: str) -> ctk.CTkEntry:
        """An entry with placeholder text. CTkEntry hides placeholders on entries bound to a
        textvariable, so this one is read with `get()` (which returns "" for the placeholder)."""
        entry = ctk.CTkEntry(
            master,
            width=310,
            placeholder_text=placeholder,
            fg_color=theme.SURFACE,
            border_color=theme.EDGE,
        )
        if value:
            entry.insert(0, value)
        entry.pack(fill="x")
        return entry

    def _option_row(
        self, form: _Form, label: str, variable: ctk.StringVar, values: list[str]
    ) -> None:
        ctk.CTkOptionMenu(
            form.row(label),
            variable=variable,
            values=values,
            fg_color=theme.SURFACE,
            button_color=theme.EDGE,
            button_hover_color=theme.ACCENT_HOVER,
        ).pack(side="left")

    def _test_row(
        self, form: _Form, command: Callable[[], None]
    ) -> tuple[ctk.CTkButton, ctk.CTkLabel]:
        cell = form.row("")
        button = ctk.CTkButton(
            cell,
            text="Test connection",
            width=130,
            fg_color=theme.ACCENT,
            hover_color=theme.ACCENT_HOVER,
            command=command,
        )
        button.pack(side="left", anchor="n")
        result = ctk.CTkLabel(cell, text="", justify="left", anchor="w", wraplength=250)
        result.pack(side="left", padx=(10, 0), fill="x", expand=True)
        return button, result

    def _center_on(self, master: ctk.CTk) -> None:
        self.update_idletasks()
        x = master.winfo_rootx() + (master.winfo_width() - self.winfo_reqwidth()) // 2
        y = master.winfo_rooty() + (master.winfo_height() - self.winfo_reqheight()) // 3
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def _activate(self) -> None:
        if self.winfo_exists():
            self.lift()
            self.focus_force()
            self.grab_set()

    def _browse(self) -> None:
        # Native modal dialog: the event loop (and background tasks) pause until it closes.
        chosen = filedialog.askdirectory(parent=self, initialdir=self._download_dir.get() or None)
        if chosen:
            self._download_dir.set(chosen)

    # -- saving and testing --------------------------------------------------------------------

    async def _store(self) -> None:
        """Validate and persist the whole form. Raises ValidationError."""
        disabled = tuple(name for name, var in self._sources.items() if not var.get())
        await self._settings.update(
            quality=self._quality.get(),
            first_day=self._first_day.get(),
            download_dir=self._download_dir.get().strip(),
            tmdb_api_key=self._tmdb_key.get(),
            excluded_keywords=self._excluded.get(),
            preferred_groups=self._preferred.get(),
            min_size_mb=self._min_size.get().strip() or 0,
            max_size_mb=self._max_size.get().strip() or 0,
            disabled_indexers=disabled + self._unknown_disabled,
            prowlarr_url=self._prowlarr_url.get(),
            prowlarr_api_key=self._prowlarr_key.get(),
            ds_url=self._ds_url.get(),
            ds_username=self._ds_username.get(),
            ds_password=self._ds_password.get(),
            ds_verify_tls=self._ds_verify.get(),
            ds_destination=self._ds_destination.get(),
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
                self._save_button.configure(state="normal")
            return
        if self.winfo_exists():
            self.destroy()

    def _start_test(self, kind: str, probe: Callable[[], Awaitable[str]]) -> None:
        running = self._tests.get(kind)
        if running is not None and not running.done():
            return
        button, result = (
            (self._prowlarr_button, self._prowlarr_result)
            if kind == "prowlarr"
            else (self._station_button, self._station_result)
        )
        self._error.configure(text="")
        result.configure(text="Saving and testing…", text_color=theme.TEXT_MUTED)
        button.configure(state="disabled")
        self._tests[kind] = spawn(
            self._run_test(button, result, probe), name=f"settings-test-{kind}"
        )

    async def _run_test(
        self,
        button: ctk.CTkButton,
        result: ctk.CTkLabel,
        probe: Callable[[], Awaitable[str]],
    ) -> None:
        text, color = "", theme.DANGER
        try:
            await self._store()
            text, color = f"✓ {await probe()}", theme.SUCCESS
        except ValidationError as exc:
            text = f"✗ {_describe(exc)}"
        except (IndexerError, DownloadStationError) as exc:
            text = f"✗ {exc}"
        finally:
            if self.winfo_exists():
                result.configure(text=text, text_color=color)
                button.configure(state="normal")

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
